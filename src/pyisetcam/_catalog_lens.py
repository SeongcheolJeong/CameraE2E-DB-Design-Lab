"""Read-only migration of legacy lens catalogs into the component catalog."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from ._catalog_store import CatalogWriter, digest, file_hash, json_text


def _safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(v) for v in value]
    return value


def _decoded(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if key.endswith("_json") and isinstance(value, str):
            try:
                result[key[:-5]] = _safe(json.loads(value))
                del result[key]
            except (ValueError, TypeError):
                pass  # Keep the original text as evidence if malformed.
    return _safe(result)


def _rows(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return []
    return [_decoded(dict(row)) for row in con.execute(f'SELECT * FROM "{table}"')]


def _manifest_assets(root: Path, warnings: list[str]) -> list[dict[str, Any]]:
    assets = []
    for directory in ("raytrace_psf", "raytrace_psf_highres", "raytrace_psf_v2"):
        path = root / directory / "manifest.json"
        if not path.is_file():
            continue
        try:
            manifest = _safe(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError) as exc:
            warnings.append(f"Cannot read PSF manifest {path}: {exc}")
            continue
        for row in manifest.get("rows", []):
            if not isinstance(row, dict):
                continue
            asset = dict(row)
            asset["asset_tier"] = directory
            asset["manifest_schema"] = manifest.get("schema")
            asset["settings"] = row.get("settings", manifest.get("settings", {}))
            asset["asset_relative_path"] = (
                str(Path(directory) / row["file"]) if row.get("file") else None
            )
            assets.append(asset)
    return assets


def _resolve_asset(root: Path, relative: str) -> Path:
    path = Path(relative)
    # Package files can only refer inside the package. Preserve unavailable or
    # unsafe declarations as missing references instead of reading outside it.
    candidate = (root / path).resolve()
    if path.is_absolute() or not candidate.is_relative_to(root):
        return root / "__unavailable_external_reference__" / digest(relative)
    return candidate


def import_lenses(writer: CatalogWriter, path: str | Path) -> dict[str, Any]:
    """Import frozen designs, legacy run snapshots, and declared PSF assets.

    Database and sidecars are never modified. Reimporting identical content uses
    deterministic writer identities; a changed prescription becomes a revision.
    Legacy scientific validation is retained as evidence, never re-certified.
    """
    path = Path(path).expanduser().resolve()
    counts = {
        "components": 0,
        "revisions": 0,
        "configurations": 0,
        "surfaces": 0,
        "runs": 0,
        "artifacts": 0,
        "observations": 0,
    }
    warnings: list[str] = []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        tables = {
            name: _rows(con, name)
            for name in (
                "metadata",
                "lenses",
                "lens_surfaces",
                "simulation_results",
                "camerae2e_quality",
                "camerae2e_psf_assets",
                "prescription_validation",
                "raytrace_asset_validation",
            )
        }
    metadata = {row["key"]: row["value"] for row in tables["metadata"]}
    try:
        build = _safe(json.loads(metadata.get("build_info", "{}")))
    except ValueError:
        build = {"unparsed_build_info": metadata.get("build_info")}
    source_version = str(build.get("source_version", "unknown"))
    db_hash = file_hash(path)
    source_id = writer.source(
        "legacy_lens_database",
        f"lens_patent_db:{db_hash}",
        uri=path.as_uri(),
        sha256=db_hash,
        metadata={
            "build_info": build,
            "legacy_metadata": metadata,
            "source_version": source_version,
            "read_only_import": True,
        },
    )
    surfaces: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in tables["lens_surfaces"]:
        surfaces[(row["lens_id"], row["configuration"])].append(row)
    sims: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in tables["simulation_results"]:
        sims[row["lens_id"]].append(row)
    quality = {r["simulation_id"]: r for r in tables["camerae2e_quality"]}
    prescription_checks = {r["simulation_id"]: r for r in tables["prescription_validation"]}
    asset_checks: dict[tuple[str, str], dict[str, Any]] = {
        (r["simulation_id"], r["asset_relative_path"]): r
        for r in tables["raytrace_asset_validation"]
    }
    assets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in tables["camerae2e_psf_assets"]:
        directory = "raytrace_psf_highres" if row.get("asset_tier") == "highres" else "raytrace_psf"
        relative = str(Path(directory) / row["file"]) if row.get("file") else ""
        assets[(row["simulation_id"], relative or directory)] = {
            **row,
            "asset_relative_path": relative or None,
        }
    for row in _manifest_assets(path.parent, warnings):
        key = (row.get("simulation_id", ""), row.get("asset_relative_path") or row["asset_tier"])
        assets[key] = {**assets.get(key, {}), **row}
    # v2 assets may exist only in the validation table, not the historical asset
    # table. Retain those too; registration does not imply validation.
    for key, check in asset_checks.items():
        if key not in assets:
            assets[key] = {
                "simulation_id": key[0],
                "asset_relative_path": key[1],
                "status": "unknown",
                "contract_version": check.get("contract_version"),
            }
    assets_by_sim: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (sim_id, _), row in assets.items():
        assets_by_sim[sim_id].append(row)

    imported_sim_ids: set[str] = set()
    for lens in tables["lenses"]:
        legacy_id = lens["lens_id"]
        natural = {
            "source": "lens_patent_db",
            "publication_number": lens.get("publication_number"),
            "prescription_id": lens.get("prescription_id", legacy_id),
        }
        component_id = writer.component(
            "lens",
            json_text(natural),
            lens.get("company") or None,
            lens.get("title") or lens.get("publication_number") or legacy_id,
            "patent_design",
        )
        counts["components"] += 1
        writer.alias(component_id, f"lens_patent_db:{source_version}:lens", legacy_id)
        writer.observation(
            component_id,
            "patent_design_identity",
            natural,
            source_id=source_id,
            source_locator={
                "table": "lenses",
                "lens_id": legacy_id,
                "source_url": lens.get("source_url"),
                "pdf_url": lens.get("pdf_url"),
            },
            extraction_method="legacy_database_import",
        )
        counts["observations"] += 1
        configurations = {r["configuration"]: r for r in sims[legacy_id]}
        for lid, config in surfaces:
            if lid == legacy_id:
                configurations.setdefault(config, {"configuration": config})
        if not configurations:
            configurations["unknown"] = {"configuration": "unknown"}
        for config, sim in sorted(configurations.items()):
            sim_id = sim.get("simulation_id")
            if sim_id:
                imported_sim_ids.add(sim_id)
                writer.alias(component_id, f"lens_patent_db:{source_version}:simulation", sim_id)
            config_surfaces = sorted(
                surfaces.get((legacy_id, config), []), key=lambda r: r["surface_order"]
            )
            orders = [r["surface_order"] for r in config_surfaces]
            if len(orders) != len(set(orders)):
                raise ValueError(f"Duplicate lens surface order in {legacy_id}:{config}")
            frozen_surfaces = [
                {k: v for k, v in r.items() if k not in {"id", "lens_id"}} for r in config_surfaces
            ]
            specs = {
                k: sim.get(k)
                for k in (
                    "focal_length_mm",
                    "focal_length_source",
                    "f_number",
                    "f_number_source",
                    "image_height_mm",
                    "half_field_deg",
                    "field_source",
                    "aperture_diameter_mm",
                    "aperture_source",
                    "simulation_model",
                )
            }
            snapshot = {
                "identity": natural,
                "configuration": config,
                "surfaces": frozen_surfaces,
                "specifications": specs,
                "prescription_conventions": {
                    "length_unit": "mm",
                    "aperture_radius_or_diameter": "unknown",
                    "asphere_coefficient_convention": "unknown",
                    "material_dispersion": "unknown",
                },
            }
            revision_id = writer.revision(
                component_id, "patent_prescription", snapshot, source_id=source_id
            )
            point_id = writer.operating_point(
                revision_id,
                config,
                {
                    "configuration": config,
                    "object_distance_m": None,
                    "temperature_c": None,
                    "focus_condition": None,
                    "unknown_conditions_reason": "not_declared_by_legacy_configuration",
                },
            )
            counts["revisions"] += 1
            counts["configurations"] += 1
            writer.put(
                "lens_configurations",
                operating_point_id=point_id,
                focal_length_mm=sim.get("focal_length_mm"),
                f_number=sim.get("f_number"),
                image_height_mm=sim.get("image_height_mm"),
                image_circle_mm=None,
                metadata_json=json_text(
                    {
                        "legacy_lens": lens,
                        "specifications": specs,
                        "image_circle_missing_reason": "not_reported",
                    }
                ),
            )
            for row in config_surfaces:
                writer.put(
                    "lens_surfaces",
                    revision_id=revision_id,
                    surface_order=row["surface_order"],
                    radius_mm=row.get("radius_mm"),
                    thickness_mm=row.get("thickness_mm"),
                    aperture_mm=row.get("effective_aperture_mm"),
                    material=row.get("material") or None,
                    nd=row.get("nd"),
                    vd=row.get("vd"),
                    conic=row.get("conic"),
                    coefficients_json=json_text(row.get("coefficients", {})),
                    metadata_json=json_text(
                        {
                            "source_row": row,
                            "aperture_convention": "unknown",
                            "source_table": "lens_surfaces",
                            "coefficient_convention": "unknown",
                        }
                    ),
                )
                counts["surfaces"] += 1
            for prop, unit, source_key in (
                ("focal_length_mm", "mm", "focal_length_source"),
                ("f_number", None, "f_number_source"),
                ("image_height_mm", "mm", None),
                ("half_field_deg", "deg", "field_source"),
                ("aperture_diameter_mm", "mm", "aperture_source"),
            ):
                evidence = sim.get(source_key) if source_key else None
                origin = (
                    "derived"
                    if evidence and ("proxy" in evidence or "surface:" in evidence)
                    else "reported"
                    if evidence and str(evidence).startswith("variable_distance:")
                    else "unknown"
                )
                writer.observation(
                    component_id,
                    prop,
                    sim.get(prop),
                    unit=unit,
                    origin_kind=origin,
                    source_id=source_id,
                    revision_id=revision_id,
                    operating_point_id=point_id,
                    source_locator={
                        "table": "simulation_results",
                        "simulation_id": sim_id,
                        "column": prop,
                        "source_field": evidence,
                        "source_surfaces_csv": sim.get("source_surfaces_csv"),
                    },
                    missing_reason="not_reported" if sim.get(prop) is None else None,
                    extraction_method="legacy_database_import",
                )
                counts["observations"] += 1
            writer.validation(
                "revision",
                revision_id,
                "scientific_accuracy",
                "unknown",
                {
                    "scope": "patent_disclosed_design_not_confirmed_production_lens",
                    "image_quality_validated": False,
                },
            )
            if sim_id in prescription_checks:
                writer.validation(
                    "revision",
                    revision_id,
                    "historical_prescription_validation",
                    "unknown",
                    {
                        "historical_result": prescription_checks[sim_id],
                        "revalidated_on_import": False,
                    },
                )
            if sim_id:
                writer.run(
                    revision_id,
                    point_id,
                    "legacy_proxy:" + digest(sim),
                    str(sim.get("simulation_status") or "unknown"),
                    solver=sim.get("simulation_model"),
                    source_id=source_id,
                    settings={
                        "legacy_optics": sim.get("optics"),
                        "wavelength_nm_for_airy_metrics": 550,
                        "coc_um_for_dof_metrics": 10,
                        "object_distance_m_for_dof": 1,
                    },
                    input_sha256=digest(snapshot),
                    metadata={
                        "legacy_result": sim,
                        "legacy_quality": quality.get(sim_id),
                        "execution_time_missing_reason": "not_recorded",
                        "scope": "legacy_proxy_result_snapshot_not_measured_accuracy",
                    },
                )
                counts["runs"] += 1
            for asset in assets_by_sim.get(sim_id, []):
                relative = asset.get("asset_relative_path")
                check = asset_checks.get((sim_id, relative))
                asset_path = _resolve_asset(path.parent, relative) if relative else None
                content_hash = (
                    file_hash(asset_path) if asset_path and asset_path.is_file() else None
                )
                settings = asset.get("settings", {})
                run_id = writer.run(
                    revision_id,
                    point_id,
                    "legacy_psf:" + digest({"asset": asset, "sha256": content_hash}),
                    str(asset.get("status") or "unknown"),
                    solver="rayoptics_geometric_histogram",
                    settings=settings,
                    source_id=source_id,
                    input_sha256=digest(snapshot),
                    created_at=asset.get("built_at"),
                    metadata={
                        "legacy_asset": asset,
                        "scope": "geometric_preview_not_calibrated_IQ",
                        "image_quality_validated": False,
                    },
                )
                counts["runs"] += 1
                if not relative:
                    continue  # Failed attempt with no artifact declaration.
                artifact_id = writer.artifact(
                    asset_path,
                    "geometric_psf",
                    run_id=run_id,
                    revision_id=revision_id,
                    expected_sha256=check.get("asset_sha256") if check else None,
                    metadata={
                        "legacy_asset": asset,
                        "declared_relative_path": relative,
                        "axes": {
                            k: asset.get(k)
                            for k in (
                                "psf_shape",
                                "field_height_mm",
                                "wavelength_nm",
                                "sample_spacing_mm",
                            )
                        },
                        "axes_source": "stored_metadata_not_array_inspection",
                        "normalization": "unknown",
                    },
                )
                counts["artifacts"] += 1
                writer.validation(
                    "artifact",
                    artifact_id,
                    "scientific_accuracy",
                    "unknown",
                    {
                        "scope": "geometric_preview_not_calibrated_IQ",
                        "image_quality_validated": False,
                    },
                )
                if check:
                    writer.validation(
                        "artifact",
                        artifact_id,
                        "historical_raytrace_contract",
                        "unknown",
                        {"historical_result": check, "revalidated_on_import": False},
                    )
    orphan_assets = set(assets_by_sim) - imported_sim_ids
    if orphan_assets:
        warnings.append(
            f"PSF rows refer to {len(orphan_assets)} absent configurations; "
            "source metadata retained in source database."
        )
    return {**counts, "source_version": source_version, "warnings": warnings}
