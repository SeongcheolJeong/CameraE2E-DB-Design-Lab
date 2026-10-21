"""Read-only migration of sensor reports and immutable simulation history."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._catalog_store import CatalogWriter, digest, file_hash, json_text


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _unit(name: str) -> str | None:
    if name in {"resolution_x", "resolution_y"}:
        return "pixel"
    for suffix, unit in (
        ("_e_per_s", "electron/s"),
        ("_um", "um"),
        ("_mm", "mm"),
        ("_px", "pixel"),
        ("_nm", "nm"),
        ("_e", "electron"),
        ("_dn", "DN"),
        ("_s", "s"),
        ("_deg", "degree"),
    ):
        if name.endswith(suffix):
            return unit
    return None


def _resolve(value: str | Path, base: Path) -> Path:
    """Prefer the explicitly selected catalog's relocated copies."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        return (base / path).resolve()
    for marker in ("sensor_db", "image_sensor_db"):
        if marker in path.parts:
            index = path.parts.index(marker)
            candidate = base.joinpath(*path.parts[index + 1 :])
            if candidate.is_file():
                return candidate.resolve()
    return path.resolve()


def _semantic(value: Any) -> Any:
    """Exclude absolute storage locations from model identity, retaining contents elsewhere."""
    if isinstance(value, dict):
        return {
            key: _semantic(item)
            for key, item in value.items()
            if key not in {"sensor_id", "generated_files", "source_dirs", "source_files"}
        }
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    if isinstance(value, str) and value.startswith("/"):
        return {"absolute_storage_location": "recorded_in_source_artifact"}
    return value


def _provider(raw: dict[str, Any], catalog: dict[str, Any]) -> str:
    metadata = _mapping(raw.get("metadata"))
    explicit = (
        raw.get("provider")
        or metadata.get("provider")
        or raw.get("source_provider")
        or catalog.get("provider")
    )
    if explicit:
        return str(explicit).strip().casefold()
    code = str(raw.get("code") or raw.get("report_code") or metadata.get("code") or "")
    catalog_source = " ".join(
        str(catalog.get(key, "")) for key in ("source_root", "source_policy", "schema")
    )
    if re.match(r"^(DEF|DEP|EXR)-\d", code, re.I) or "techinsight" in catalog_source.lower():
        return "techinsights"
    return "unspecified_provider"


def _code(raw: dict[str, Any]) -> str:
    metadata = _mapping(raw.get("metadata"))
    return str(
        raw.get("code")
        or raw.get("report_code")
        or metadata.get("code")
        or metadata.get("report_id")
        or ""
    ).strip()


def _legacy_aliases(writer: CatalogWriter, component_id: str, raw: dict[str, Any]) -> None:
    code = _code(raw)
    if code:
        writer.alias(component_id, "sensor_report_code", code)
    explicit = raw.get("sensor_id")
    if explicit:
        writer.alias(component_id, "legacy_sensor_id", str(explicit))
    stack = _mapping(raw.get("generated_files")).get("stack_config")
    if stack:
        old_id = Path(stack).stem
    else:
        metadata = _mapping(raw.get("metadata"))
        old_id = "_".join(
            filter(
                None,
                re.split(
                    r"[^\w]+",
                    "_".join(
                        str(x)
                        for x in (
                            code,
                            metadata.get("manufacturer", ""),
                            metadata.get("device_name", ""),
                        )
                    ).lower(),
                ),
            )
        )
    if old_id:
        writer.alias(component_id, "legacy_sensor_id", old_id)


def _geometry(
    writer: CatalogWriter, revision_id: str, model: dict[str, Any], *, origin: str
) -> None:
    geom = _mapping(model.get("sensor_geometry")) or model
    geometry_um = _mapping(model.get("geometry_um"))
    writer.put(
        "sensor_geometry",
        revision_id=revision_id,
        pixel_pitch_um=_number(geom.get("pixel_pitch_um", geometry_um.get("pitch"))),
        width_px=_integer(
            geom.get(
                "width_px",
                geom.get("active_width_px", geom.get("array_width_px", geom.get("resolution_x"))),
            )
        ),
        height_px=_integer(
            geom.get(
                "height_px",
                geom.get("active_height_px", geom.get("array_height_px", geom.get("resolution_y"))),
            )
        ),
        active_width_mm=_number(geom.get("active_width_mm")),
        active_height_mm=_number(geom.get("active_height_mm")),
        cfa_pattern=geom.get("cfa_pattern"),
        metadata_json=json_text(
            {"origin_kind": origin, "dimensions_not_inferred_from_resolution": True}
        ),
    )
    # A dictionary of thicknesses does not establish layer ordering or material identity.
    layers = model.get("layers")
    if isinstance(layers, list):
        for order, layer in enumerate(layers):
            if not isinstance(layer, dict):
                continue
            writer.put(
                "sensor_layers",
                revision_id=revision_id,
                layer_order=order,
                name=layer.get("name"),
                material=layer.get("material"),
                thickness_um=_number(layer.get("thickness_um")),
                metadata_json=json_text({"origin_kind": origin, "source_layer": layer}),
            )


def _readout(
    writer: CatalogWriter,
    component_id: str,
    revision_id: str,
    profile: dict[str, Any],
    source_id: str,
    settings: dict[str, Any] | None = None,
) -> str:
    provenance = _mapping(profile.get("provenance"))
    conditions = {
        "readout": provenance.get("conditions", profile.get("conditions", {})),
        "simulation_settings": settings or {},
    }
    name = str(
        profile.get("operating_point")
        or profile.get("mode_name")
        or profile.get("profile")
        or "unspecified"
    )
    point = writer.operating_point(revision_id, name, conditions)
    sensor = _mapping(profile.get("sensor"))
    values = (
        _mapping(profile.get("values")) or _mapping(profile.get("readout")) or sensor or profile
    )
    fields = _mapping(provenance.get("fields"))
    bit = values.get("bit_depth", _mapping(fields.get("bit_depth")).get("value"))
    writer.put(
        "sensor_readout_modes",
        operating_point_id=point,
        mode_name=name,
        bit_depth=_integer(bit),
        metadata_json=json_text(profile),
    )
    for key, evidence in fields.items():
        item = _mapping(evidence)
        classification = str(
            item.get("classification", provenance.get("classification", "unknown"))
        )
        origin = "assumed" if "assum" in classification or "proxy" in classification else "reported"
        writer.observation(
            component_id,
            key,
            item.get("value"),
            unit=_unit(key),
            origin_kind=origin,
            source_id=source_id,
            revision_id=revision_id,
            operating_point_id=point,
            conditions=conditions,
            source_locator={
                "json_pointer": f"/metadata/readout_profile/provenance/fields/{key}",
                "evidence": item,
                "product_measurement_verified": False,
            },
            missing_reason="value_not_provided" if item.get("value") is None else None,
            extraction_method="legacy_saved_readout_profile",
        )
    return point


def _report(
    writer: CatalogWriter,
    raw: dict[str, Any],
    catalog: dict[str, Any],
    base: Path,
    warnings: list[str],
) -> tuple[str, str, str]:
    metadata = _mapping(raw.get("metadata"))
    code = _code(raw)
    provider = _provider(raw, catalog)
    identity = (
        f"{provider}:report:{code}" if code else f"{provider}:unidentified:{digest(_semantic(raw))}"
    )
    if not code:
        warnings.append(
            "Sensor report lacks report code/ID; using a content identity pending curation."
        )
    component = writer.component(
        "sensor",
        identity,
        metadata.get("manufacturer", "Unknown"),
        metadata.get("device_name", raw.get("device_name", "")),
        "report_subject",
    )
    _legacy_aliases(writer, component, raw)
    source_id = writer.source(
        "analysis_report",
        identity,
        uri=metadata.get("url"),
        sha256=digest(_semantic(raw)),
        metadata={
            "provider": provider,
            "report_code": code,
            "source_record": raw,
            "hash_scope": "normalized_extracted_record_not_original_report",
        },
    )
    specs = _mapping(raw.get("derived_specs"))
    snapshot = {
        "derived_specs": specs,
        "derived_evidence": _semantic(raw.get("derived_evidence", {})),
        "table_evidence": _semantic(raw.get("table_evidence", [])),
        "metadata": _semantic(metadata),
        "origin_kind": "extracted",
    }
    revision = writer.revision(component, "sensor_report_extraction", snapshot, source_id=source_id)
    _geometry(writer, revision, specs, origin="extracted")
    evidence_by_field = _mapping(raw.get("derived_evidence"))
    document_hashes: dict[Path, str | None] = {}
    properties = set(specs) | {
        "pixel_pitch_um",
        "width_px",
        "height_px",
        "active_width_mm",
        "active_height_mm",
        "cfa_pattern",
    }
    for name in sorted(properties):
        source_name = name
        if name not in specs and name in {"width_px", "height_px"}:
            source_name = {"width_px": "resolution_x", "height_px": "resolution_y"}[name]
        value = specs.get(source_name)
        evidence = evidence_by_field.get(source_name) or []
        if not isinstance(evidence, list):
            evidence = [evidence]
        for entry in evidence or [{}]:
            locator = _mapping(entry)
            has_location = any(
                locator.get(key) for key in ("page", "page_number", "table", "figure", "section")
            )
            source_file = locator.get("source_file")
            field_source = source_id
            if source_file:
                path = _resolve(source_file, base)
                if path not in document_hashes:
                    document_hashes[path] = file_hash(path) if path.is_file() else None
                field_source = writer.source(
                    "report_document",
                    f"{identity}:{digest(str(source_file))}",
                    uri=str(path),
                    sha256=document_hashes[path],
                    metadata={
                        "report_source_id": source_id,
                        "available": path.is_file(),
                        "original_source_file": source_file,
                    },
                )
            writer.observation(
                component,
                name,
                value,
                unit=_unit(name),
                origin_kind="extracted",
                source_id=field_source,
                revision_id=revision,
                source_locator={
                    "catalog_property": f"derived_specs.{source_name}",
                    "evidence": locator,
                    "source_location_status": "provided" if has_location else "missing",
                    "source_location_missing_reason": None
                    if has_location
                    else "page_table_or_figure_not_recorded",
                },
                missing_reason="not_reported" if value is None else None,
                extraction_method=str(locator.get("source") or "legacy_catalog_extraction"),
            )
    for mode in raw.get("readout_modes", []) or []:
        if isinstance(mode, dict):
            _readout(writer, component, revision, mode, source_id)
    for kind, reference in _mapping(raw.get("generated_files")).items():
        if not isinstance(reference, str) or not reference:
            continue
        path = _resolve(reference, base)
        try:
            generated = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            writer.artifact(
                path,
                f"sensor_generated_{kind}",
                revision_id=revision,
                metadata={"origin_kind": "assumed", "read_error": str(exc)},
            )
            warnings.append(f"{code}: generated {kind} unavailable: {path}")
            continue
        material_files = {}
        for material, descriptor in _mapping(_mapping(generated).get("materials")).items():
            reference_path = _mapping(descriptor).get("nk_table")
            if reference_path:
                material_path = _resolve(reference_path, path.parent)
                resolution_basis = "selected_config_relative"
                if not material_path.is_file() and not Path(reference_path).is_absolute():
                    # Follow the same dependency from the original, explicitly recorded
                    # config location only. Never substitute a similarly named material.
                    original_config = Path(reference).expanduser()
                    if not original_config.is_absolute():
                        original_config = base / original_config
                    original_material = (original_config.parent / reference_path).resolve()
                    if original_material.is_file():
                        material_path = original_material
                        resolution_basis = "original_config_relative"
                material_hash = file_hash(material_path) if material_path.is_file() else None
                material_files[material] = (
                    material_path,
                    material_hash,
                    {
                        "source_origin_path": str(material_path),
                        "source_origin_sha256": material_hash,
                        "material_reference": reference_path,
                        "resolution_basis": resolution_basis,
                    },
                )
        generated_snapshot = {
            "config": _semantic(generated),
            "material_dependencies": {
                key: {"sha256": value[1], "availability": "available" if value[1] else "missing"}
                for key, value in material_files.items()
            },
        }
        model_revision = writer.revision(
            component, f"sensor_generated_{kind}", generated_snapshot, source_id=source_id
        )
        writer.artifact(
            path,
            f"sensor_generated_{kind}",
            revision_id=model_revision,
            expected_sha256=file_hash(path),
            metadata={
                "origin_kind": "assumed",
                "original_reference": reference,
                "measured_accuracy_verified": False,
            },
        )
        if isinstance(generated, dict):
            _geometry(writer, model_revision, generated, origin="assumed")
        for material, (material_path, material_hash, material_metadata) in material_files.items():
            writer.artifact(
                material_path,
                "sensor_material_table",
                revision_id=model_revision,
                expected_sha256=material_hash,
                metadata={
                    "material_key": material,
                    "origin_kind": "assumed",
                    **material_metadata,
                },
            )
        writer.validation(
            "revision",
            model_revision,
            "scientific_accuracy",
            "unknown",
            {
                "reason": "Generated legacy proxy is not calibrated product evidence.",
                "historical_calibration_status": _mapping(generated).get("calibration_status"),
            },
        )
    return component, revision, provider


def _trusted_document(path: Path, expected: str | None) -> tuple[dict[str, Any], str | None]:
    if not path.is_file():
        return {}, "missing"
    if not expected:
        return {}, "expected_hash_missing"
    if file_hash(path) != expected:
        return {}, "hash_mismatch"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}, "document_not_object"
        return value, None
    except (OSError, ValueError):
        return {}, "invalid_json"


def _array_metadata(document: dict[str, Any]) -> dict[str, Any]:
    metadata = _mapping(document.get("metadata"))
    optical = _mapping(metadata.get("optical_capabilities"))
    axes = _mapping(metadata.get("array_axes"))
    for key in ("spectral_axes", "angular_axes", "pupil_axes", "pupil_coordinate_columns"):
        if key in optical:
            axes[key] = optical[key]
    return {
        "array_manifest": document.get("array_manifest", {}),
        "array_schema": metadata.get("array_schema", {}),
        "array_units": metadata.get("array_units", {}),
        "axis_metadata": axes,
        "native_filter_names": _mapping(metadata.get("model_snapshot")).get("filter_names"),
        "pupil_definition": optical.get("pupil_definition"),
        "coordinate_system": metadata.get("coordinate_system"),
        "normalization": metadata.get("normalization"),
        "missing_metadata_policy": "Unrecorded axes, coordinates and normalization remain unknown.",
        "conditions": metadata.get("simulation_settings", {}),
        "origin_kind": "simulated",
        "measured_accuracy_verified": False,
    }


def _simulation_runs(
    writer: CatalogWriter,
    path: Path,
    records: dict[str, list[tuple[str, str, str]]],
    warnings: list[str],
) -> int:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    count = 0
    try:
        # Deliberately query runs, not the legacy latest-run sensors index.
        for row in conn.execute("SELECT * FROM runs ORDER BY created_at, run_id"):
            legacy = dict(row)
            code = str(legacy["report_code"])
            matches = records.get(code, [])
            if len(matches) == 1:
                component = matches[0][0]
            else:
                # A missing or ambiguous report must not silently attach to another provider.
                component = writer.component(
                    "sensor",
                    f"legacy_simulations:report:{code}",
                    legacy.get("manufacturer", "Unknown"),
                    legacy.get("device_name", ""),
                    "report_subject",
                )
                writer.alias(component, "sensor_report_code", code)
                warnings.append(
                    f"{code}: historical run has no unique catalog report; kept separate."
                )
            writer.alias(component, "legacy_sensor_id", legacy["sensor_id"])
            source = writer.source(
                "sensor_simulation_record",
                str(legacy["run_id"]),
                sha256=digest(
                    {k: v for k, v in legacy.items() if k not in {"result_path", "arrays_path"}}
                ),
                uri=str(path),
                metadata={"legacy_record": legacy},
            )
            result_path = _resolve(legacy["result_path"], path.parent)
            document, error = _trusted_document(result_path, legacy.get("result_sha256"))
            if document and (
                document.get("run_id") != legacy["run_id"]
                or _mapping(document.get("metadata")).get("report_code", code) != code
            ):
                document, error = {}, "identity_mismatch"
            metadata = _mapping(document.get("metadata"))
            model = _mapping(metadata.get("model_snapshot"))
            snapshot = {
                "model_snapshot": _semantic(model),
                "config": _semantic(metadata.get("config")),
                "readout_profile": _semantic(metadata.get("readout_profile")),
                "snapshot_status": "verified_result_hash" if not error else error,
            }
            if not model:
                snapshot.update(
                    legacy_config_sha256=legacy.get("config_sha256"), model_snapshot_missing=True
                )
            revision = writer.revision(
                component,
                legacy.get("model") or "sensor_unavailable_model",
                snapshot,
                source_id=source,
            )
            if model:
                _geometry(writer, revision, model, origin="simulated")
            settings = _mapping(metadata.get("simulation_settings"))
            point = _readout(
                writer,
                component,
                revision,
                _mapping(metadata.get("readout_profile")),
                source,
                settings,
            )
            try:
                summary = json.loads(legacy.get("summary_json") or "{}")
            except ValueError:
                summary = {"invalid_legacy_summary": legacy.get("summary_json")}
            legacy_digest = digest(
                {k: v for k, v in legacy.items() if k not in {"result_path", "arrays_path"}}
            )
            run = writer.run(
                revision,
                point,
                f"legacy_sensor:{legacy['run_id']}:{legacy_digest}",
                legacy["status"],
                solver=legacy.get("model"),
                settings=settings,
                source_id=source,
                input_sha256=document.get("input_digest")
                or _mapping(_mapping(summary).get("provenance")).get("input_digest"),
                metadata={
                    "legacy_run_id": legacy["run_id"],
                    "reason": legacy.get("reason"),
                    "snapshot_integrity": error or "verified",
                    "historical_summary": summary,
                    "implementation_sha256": metadata.get("implementation_sha256"),
                    "source_scope_policy": metadata.get("source_scope_policy"),
                    "estimate_only": True,
                    "measured_accuracy_verified": False,
                },
                created_at=legacy.get("created_at"),
            )
            writer.artifact(
                result_path,
                "sensor_simulation_result",
                run_id=run,
                revision_id=revision,
                expected_sha256=legacy.get("result_sha256"),
                metadata={"legacy_run_id": legacy["run_id"]},
            )
            if legacy.get("arrays_path"):
                writer.artifact(
                    _resolve(legacy["arrays_path"], path.parent),
                    "sensor_simulation_arrays",
                    run_id=run,
                    revision_id=revision,
                    expected_sha256=legacy.get("arrays_sha256"),
                    metadata=_array_metadata(document),
                )
            input_digest = document.get("input_digest")
            if isinstance(input_digest, str) and re.fullmatch(r"[0-9a-f]{64}", input_digest):
                # The legacy digest is over canonical JSON, not serialized file bytes.
                input_path = path.parent / "inputs" / f"{input_digest}.json"
                writer.artifact(
                    input_path,
                    "sensor_simulation_input",
                    run_id=run,
                    metadata={"legacy_semantic_digest": input_digest},
                )
            writer.validation(
                "run",
                run,
                "frozen_snapshot_integrity",
                "failed" if error else "passed",
                {"reason": error, "scope": "saved_result_hash_and_identity_only"},
            )
            writer.validation(
                "run",
                run,
                "scientific_accuracy",
                "unknown",
                {
                    "historical_checks": metadata.get(
                        "checks", _mapping(summary).get("checks", {})
                    ),
                    "reason": (
                        "Historical checks are preserved evidence; "
                        "no measured accuracy was verified during import."
                    ),
                },
            )
            if error:
                warnings.append(
                    f"{code}/{legacy['run_id']}: frozen result {error}; "
                    "no model reconstructed from current source."
                )
            count += 1
    finally:
        conn.close()
    return count


def import_sensors(
    writer: CatalogWriter, catalog_path: str | Path, simulation_path: str | Path | None = None
) -> dict[str, Any]:
    """Import source reports and all saved sensor runs without changing either source."""
    path = Path(catalog_path).expanduser().resolve()
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("records"), list):
        raise ValueError("Sensor catalog must be an object containing a records list.")
    warnings: list[str] = []
    records: dict[str, list[tuple[str, str, str]]] = {}
    components: set[str] = set()
    for raw in catalog.get("records", []):
        if not isinstance(raw, dict):
            warnings.append("Skipped non-object sensor catalog record.")
            continue
        imported = _report(writer, raw, catalog, path.parent, warnings)
        components.add(imported[0])
        group = records.setdefault(_code(raw), [])
        if imported not in group:
            group.append(imported)
    runs = 0
    if simulation_path is not None:
        db = Path(simulation_path).expanduser().resolve()
        if db.is_file():
            runs = _simulation_runs(writer, db, records, warnings)
        else:
            warnings.append(f"Sensor simulation database unavailable: {db}")
    return {"components": len(components), "runs": runs, "warnings": warnings}
