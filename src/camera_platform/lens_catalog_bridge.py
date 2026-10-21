"""Read-only, revision-pinned geometric lens comparison from the component DB.

The patent PSF is a geometric ray histogram at the ideal image plane. It is
useful for a bounded spot/CRA preview, but is not a pixel-integrated R1 PSF or
an absolute radiometric response. This bridge deliberately cannot return an
R1 optical component or a RAW rate request.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np

from pyisetcam._catalog_store import connect, digest, file_hash
from pyisetcam.component_catalog import component_catalog_path
from pyisetcam.lens_patents import lens_patent_raytrace_optics, lens_patent_validation


class LensCatalogBridgeError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise LensCatalogBridgeError(code, message)


def _local_source(path: Path, root: Path) -> Path:
    _require(
        path.is_absolute() and path.is_relative_to(root),
        "unsafe_source_path",
        "Lens DB must stay inside the component catalog root",
    )
    relative = path.relative_to(root)
    _require(
        ".." not in relative.parts
        and all(not (root.joinpath(*relative.parts[:index])).is_symlink()
                for index in range(1, len(relative.parts) + 1)),
        "unsafe_source_path",
        "Lens DB must not use a symlink",
    )
    resolved = path.resolve()
    _require(
        resolved.is_relative_to(root) and resolved.is_file(),
        "unsafe_source_path",
        "Lens DB must be a regular file inside the component catalog root",
    )
    return resolved


def _source_path(uri: str, root: Path, expected_sha256: str) -> Path:
    _require(
        isinstance(uri, str),
        "unsupported_source_uri",
        "Require a local file URI for the pinned lens DB",
    )
    parsed = urlparse(uri)
    _require(
        parsed.scheme == "file"
        and parsed.netloc in ("", "localhost")
        and not parsed.query
        and not parsed.fragment,
        "unsupported_source_uri",
        "Require a local file URI for the pinned lens DB",
    )
    _require(
        isinstance(expected_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None,
        "source_hash_invalid",
        "Lens source needs a pinned SHA-256 hash",
    )
    root = root.resolve()
    path = Path(unquote(parsed.path))
    _require(
        path.is_absolute(),
        "unsafe_source_path",
        "Lens DB URI must name an absolute local file",
    )
    if path.is_relative_to(root):
        # A source already bound inside this root must be valid at that exact
        # path. Do not hide a changed or missing source with another copy.
        return _local_source(path, root)

    # Older catalogs preserve an import-time absolute URI. A copied catalog
    # can bind to the same bytes only in the selected workspace's known lens
    # layout; the old URI is retained as historical provenance, never read.
    _require(
        path.parent.name == "lens_patents"
        and path.parent.parent.name == "data"
        and path.parent.parent.parent.parent.name == "lens_db"
        and re.fullmatch(r"lens_patent_simulation_v[0-9]+\.sqlite", path.name) is not None,
        "unsafe_source_path",
        "Relocation supports only the imported lens DB layout",
    )
    lens_dir = root / "lens_db"
    _require(
        not lens_dir.is_symlink(),
        "unsafe_source_path",
        "Lens DB search directory must not be a symlink",
    )
    candidates = list(lens_dir.glob(f"*/data/lens_patents/{path.name}"))
    _require(
        len(candidates) <= 64,
        "source_relocation_ambiguous",
        "Too many relocated lens DB candidates",
    )
    matching = []
    for candidate in candidates:
        local = _local_source(candidate, root)
        try:
            if file_hash(local) == expected_sha256:
                matching.append(local)
        except OSError as exc:
            raise LensCatalogBridgeError(
                "source_relocation_unavailable", "Relocated lens DB became unavailable"
            ) from exc
    _require(
        len(matching) == 1,
        "source_relocation_ambiguous"
        if len(matching) > 1
        else ("source_hash_mismatch" if candidates else "source_relocation_unavailable"),
        "Require exactly one local lens DB with the pinned source hash",
    )
    return matching[0]


def _revision_source(revision_id: str, root: Path) -> tuple[dict, dict, Path, str]:
    catalog_path = component_catalog_path(root)
    with closing(connect(catalog_path)) as conn:
        row = conn.execute(
            """SELECT r.*, c.family, c.entity_kind FROM model_revisions r
            JOIN components c ON c.component_id=r.component_id
            WHERE r.revision_id=?""",
            (revision_id,),
        ).fetchone()
        _require(row is not None, "revision_missing", f"Unknown lens revision: {revision_id}")
        revision = dict(row)
        _require(
            revision["family"] == "lens" and revision["model_kind"] == "patent_prescription",
            "unsupported_revision",
            "Only imported patent-prescription lens revisions are supported",
        )
        snapshot = json.loads(revision["snapshot_json"])
        _require(
            digest(snapshot) == revision["content_sha256"],
            "revision_hash_mismatch",
            "Catalog lens revision content differs from its pinned hash",
        )
        source_row = conn.execute(
            "SELECT * FROM sources WHERE source_id=?", (revision["source_id"],)
        ).fetchone()
        _require(
            source_row is not None and source_row["kind"] == "legacy_lens_database",
            "unsupported_source",
            "Revision has no imported lens DB source",
        )
        source = dict(source_row)
    identity = snapshot.get("identity", {})
    _require(
        identity.get("source") == "lens_patent_db"
        and isinstance(identity.get("publication_number"), str)
        and type(identity.get("prescription_id")) in (int, str)
        and isinstance(snapshot.get("configuration"), str),
        "unsupported_revision",
        "Revision has no unambiguous patent identity/configuration",
    )
    path = _source_path(source["uri"], root, source["sha256"])
    _require(
        file_hash(path) == source["sha256"],
        "source_hash_mismatch",
        "Lens source DB changed after this revision was imported; reimport first",
    )
    return revision, snapshot, path, source["sha256"]


def _asset(
    revision: dict, snapshot: dict, db_path: Path, root: Path
) -> tuple[str, Path, dict, dict]:
    identity = snapshot["identity"]
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as legacy:
        legacy.row_factory = sqlite3.Row
        matches = legacy.execute(
            """SELECT s.simulation_id FROM simulation_results s
            JOIN lenses l ON l.lens_id=s.lens_id
            WHERE l.publication_number=? AND l.prescription_id=? AND s.configuration=?""",
            (
                identity["publication_number"],
                identity["prescription_id"],
                snapshot["configuration"],
            ),
        ).fetchall()
        _require(
            len(matches) == 1,
            "simulation_ambiguous",
            "Lens revision must resolve to exactly one source simulation",
        )
        simulation_id = matches[0]["simulation_id"]
        checks = legacy.execute(
            """SELECT * FROM raytrace_asset_validation WHERE simulation_id=?
            AND asset_relative_path LIKE 'raytrace_psf_v2/%'""",
            (simulation_id,),
        ).fetchall()
        _require(
            len(checks) == 1,
            "validated_psf_missing",
            f"No unique v2 geometric PSF validation for {simulation_id}",
        )
        check = dict(checks[0])
        _require(
            check["valid_contract"] == 1 and check["contract_version"] == 2,
            "psf_contract_invalid",
            "Geometric PSF v2 contract was not validated",
        )

    relative = Path(check["asset_relative_path"])
    _require(
        not relative.is_absolute()
        and len(relative.parts) == 2
        and relative.parts[0] == "raytrace_psf_v2"
        and relative.suffix == ".npz"
        and relative.name not in (".", ".."),
        "unsafe_asset_path",
        "Geometric PSF must be a local v2 NPZ",
    )
    psf_dir = db_path.parent / "raytrace_psf_v2"
    asset_path = psf_dir / relative.name
    _require(
        not psf_dir.is_symlink()
        and not asset_path.is_symlink()
        and asset_path.is_file()
        and asset_path.stat().st_size <= 128 * 1024**2,
        "asset_unavailable",
        "Pinned geometric PSF is unavailable or unbounded",
    )
    manifest_path = psf_dir / "manifest.json"
    _require(
        manifest_path.is_file() and not manifest_path.is_symlink(),
        "manifest_unavailable",
        "Geometric PSF manifest is unavailable",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_rows = [
        row
        for row in manifest.get("rows", [])
        if row.get("simulation_id") == simulation_id
        and row.get("status") in ("generated", "exists")
    ]
    _require(
        len(manifest_rows) == 1 and manifest_rows[0].get("file") == relative.name,
        "manifest_mismatch",
        "PSF manifest differs from the validated asset",
    )
    _require(
        file_hash(asset_path) == check["asset_sha256"],
        "asset_hash_mismatch",
        "Geometric PSF changed after validation",
    )

    artifact_relative = asset_path.relative_to(root).as_posix()
    with closing(connect(component_catalog_path(root))) as conn:
        rows = conn.execute(
            """SELECT a.* FROM artifacts a JOIN artifact_links l
            ON l.artifact_id=a.artifact_id
            WHERE l.revision_id=? AND a.path=?""",
            (revision["revision_id"], artifact_relative),
        ).fetchall()
        _require(
            len(rows) == 1,
            "artifact_not_pinned",
            "Validated PSF is not pinned to this catalog lens revision",
        )
        artifact = dict(rows[0])
    _require(
        artifact["kind"] == "geometric_psf"
        and artifact["sha256"] == check["asset_sha256"]
        and artifact["expected_sha256"] == check["asset_sha256"]
        and artifact["availability"] == "available"
        and artifact["integrity_status"] == "passed",
        "artifact_not_pinned",
        "Catalog PSF identity/integrity does not match source validation",
    )
    prescription = lens_patent_validation(simulation_id, db_path=db_path)
    _require(
        prescription["valid"]
        and prescription["prescription_sha256"] == check["source_prescription_sha256"],
        "prescription_mismatch",
        "PSF is not pinned to the currently valid prescription",
    )
    return simulation_id, asset_path, check, artifact


def _spot(psf: np.ndarray, row_pitch_mm: float, col_pitch_mm: float) -> dict:
    """Geometric ray-histogram statistics about its own intensity centroid."""
    rows, cols = psf.shape
    y = ((rows - 1) / 2 - np.arange(rows)) * row_pitch_mm * 1000
    x = (np.arange(cols) - (cols - 1) / 2) * col_pitch_mm * 1000
    total = float(psf.sum())
    _require(math.isfinite(total) and total > 0, "psf_invalid", "PSF slice has no finite energy")
    yc = float((psf.sum(axis=1) @ y) / total)
    xc = float((psf.sum(axis=0) @ x) / total)
    radius = np.hypot(y[:, None] - yc, x[None, :] - xc).ravel()
    weights = (psf / total).ravel()
    order = np.argsort(radius)
    cumulative = np.cumsum(weights[order])
    ee80 = float(radius[order[min(np.searchsorted(cumulative, 0.8), len(order) - 1)]])
    rms = float(np.sqrt(np.dot(weights, radius * radius)))
    return {
        "ee80_radius_um": ee80,
        "rms_radius_um": rms,
        "centroid_offset_um": float(math.hypot(xc, yc)),
    }


def geometric_lens_preview(
    revision_id: str,
    *,
    catalog_root: str | Path | None = None,
    wavelength_nm: float = 550.0,
) -> dict:
    """Compare an imported DB revision only at an exact stored wavelength.

    No proxy is substituted when a v2 PSF or its validated lineage is missing.
    The result is descriptive geometric evidence and cannot be fed to R1 RAW.
    """
    _require(
        isinstance(revision_id, str) and bool(revision_id),
        "revision_invalid",
        "Choose an explicit lens revision ID",
    )
    _require(
        type(wavelength_nm) in (int, float) and math.isfinite(wavelength_nm),
        "wavelength_invalid",
        "Wavelength must be finite nm",
    )
    root = component_catalog_path(catalog_root).parent.resolve()
    revision, snapshot, db_path, source_hash = _revision_source(revision_id, root)
    sim_id, asset_path, check, artifact = _asset(revision, snapshot, db_path, root)
    optics = lens_patent_raytrace_optics(
        sim_id,
        db_path=db_path,
        psf_dir=asset_path.parent,
        allow_approximate_radiometry=True,
    )
    ray = optics["raytrace"]
    _require(
        ray.get("asset_sha256") == check["asset_sha256"]
        and ray.get("source_prescription_sha256") == check["source_prescription_sha256"]
        and ray.get("contract_version") == 2
        and ray.get("psf_kind") == "geometric"
        and ray.get("diffraction_included") is False,
        "psf_contract_invalid",
        "Loaded geometric PSF differs from the pinned validation",
    )
    table = ray["psf"]
    waves = np.asarray(table["wavelength_nm"], dtype=float)
    indices = np.flatnonzero(np.isclose(waves, wavelength_nm, rtol=0, atol=1e-9))
    _require(
        indices.size == 1,
        "wavelength_outside_asset",
        "Request an exact stored PSF wavelength; interpolation is not authorized",
    )
    wi = int(indices[0])
    fields = np.asarray(table["field_height_mm"], dtype=float)
    psf = np.asarray(table["function"], dtype=float)
    pitches = np.asarray(table["sample_spacing_mm"], dtype=float).reshape(-1)
    if pitches.size == 1:
        pitches = np.repeat(pitches, 2)
    cra = np.asarray(ray.get("chief_ray_angle_deg"), dtype=float)
    retained = np.asarray(ray.get("retained_energy_fraction"), dtype=float)
    _require(
        psf.ndim == 4
        and psf.shape[2:] == (fields.size, waves.size)
        and cra.shape == retained.shape == (fields.size, waves.size)
        and np.isfinite(cra).all()
        and np.isfinite(retained).all()
        and pitches.size == 2
        and np.isfinite(pitches).all()
        and np.all(pitches > 0),
        "psf_contract_invalid",
        "PSF axes, CRA, retained energy or pitch are inconsistent",
    )
    selected = []
    for label, fi in (("center", 0), ("edge", fields.size - 1)):
        selected.append(
            {
                "position": label,
                "ideal_image_height_mm": float(fields[fi]),
                "geometric_chief_ray_angle_deg": float(cra[fi, wi]),
                "retained_energy_fraction": float(retained[fi, wi]),
                **_spot(psf[:, :, fi, wi], pitches[0], pitches[1]),
            }
        )
    distance = float(ray["object_distance_m"])
    max_fov_deg = float(ray["max_fov_deg"])
    _require(
        math.isfinite(max_fov_deg) and 0 < max_fov_deg <= 180,
        "psf_contract_invalid",
        "Stored lens PSF has no valid angular field limit",
    )
    _require(
        file_hash(db_path) == source_hash and file_hash(asset_path) == check["asset_sha256"],
        "source_changed_during_preview",
        "Lens source or PSF changed during preview",
    )
    ri_status = ray["relative_illumination"].get("status", "unknown")
    raw_reasons = [
        "pixel_integrated_diffraction_psf_not_available",
        "angular_irradiance_weights_not_available",
    ]
    if check["radiometry_status"] != "validated":
        raw_reasons.append("absolute_radiometry_not_validated")
    if ri_status != "validated":
        raw_reasons.append("relative_illumination_not_validated")
    if check["sampling_status"] != "validated":
        raw_reasons.append("sensor_sampling_not_validated")
    return {
        "schema": "camerae2e_lens_geometric_preview_v1",
        "source_kind": "simulated",
        "physical_validation": "not_established",
        "scope": "geometric_spot_preview_not_calibrated_image_quality",
        "identity": {
            "component_id": revision["component_id"],
            "revision_id": revision_id,
            "revision_sha256": revision["content_sha256"],
            "source_id": revision["source_id"],
            "source_db_sha256": source_hash,
            "simulation_id": sim_id,
            "asset_id": artifact["artifact_id"],
            "asset_sha256": check["asset_sha256"],
            "prescription_sha256": check["source_prescription_sha256"],
            "publication_number": snapshot["identity"]["publication_number"],
            "configuration": snapshot["configuration"],
        },
        "model": {
            "response_kind": "geometric_ray_histogram_psf",
            "reference_plane": "ideal_image_height_at_image_plane",
            "field_coordinate": table.get("field_coordinate"),
            "field_support_mm": [float(fields[0]), float(fields[-1])],
            "field_samples_mm": [float(value) for value in fields],
            "psf_normalization": "unit_sum_conditional",
            "coordinate_convention": table.get("coordinate_convention"),
            "sample_spacing_mm_row_col": pitches.tolist(),
            "psf_samples_row_col": [int(psf.shape[0]), int(psf.shape[1])],
            "object_distance_m": "infinity" if math.isinf(distance) else distance,
            "max_fov_deg": max_fov_deg,
            "effective_focal_length_mm": float(ray["effective_focal_length_m"]) * 1000,
            "nominal_focal_length_mm": float(optics["focal_length_m"]) * 1000,
            "effective_f_number": float(ray["effective_f_number"]),
            "catalog_reported_f_number": snapshot["specifications"].get("f_number"),
            "diffraction_included": False,
            "radiometry_status": check["radiometry_status"],
            "sensor_sampling_status": check["sampling_status"],
            "relative_illumination_status": ri_status,
            "relative_illumination_used": False,
            "angular_irradiance_weights_used": False,
        },
        "wavelength_nm": float(waves[wi]),
        "fields": selected,
        "r1_raw_eligibility": {
            "status": "blocked",
            "reasons": raw_reasons,
        },
    }


def compare_geometric_lenses(
    revision_ids: list[str],
    *,
    catalog_root: str | Path | None = None,
    wavelength_nm: float = 550.0,
) -> dict:
    """Return side-by-side spot evidence for 2..4 explicitly pinned revisions."""
    _require(
        isinstance(revision_ids, list)
        and 2 <= len(revision_ids) <= 4
        and all(isinstance(value, str) for value in revision_ids)
        and len(set(revision_ids)) == len(revision_ids),
        "comparison_invalid",
        "Choose 2..4 distinct lens revision IDs",
    )
    items = [
        geometric_lens_preview(r, catalog_root=catalog_root, wavelength_nm=wavelength_nm)
        for r in revision_ids
    ]
    distances = {item["model"]["object_distance_m"] for item in items}
    _require(
        len(distances) == 1,
        "condition_mismatch",
        "Compared geometric PSFs must share an object distance",
    )
    return {
        "schema": "camerae2e_lens_geometric_comparison_v1",
        "comparison_scope": "descriptive_same_wavelength_and_object_distance_no_ranking",
        "wavelength_nm": items[0]["wavelength_nm"],
        "lenses": items,
    }


def lens_revision_catalog(
    *,
    catalog_root: str | Path | None = None,
    query: str | None = None,
) -> dict:
    """List lens revisions and preview eligibility without loading PSF arrays.

    A ready item passed current source, prescription, manifest and artifact
    hash checks. The geometric preview itself still validates its full LUT.
    """
    _require(query is None or isinstance(query, str), "query_invalid", "Search query must be text")
    root = component_catalog_path(catalog_root).parent.resolve()
    with closing(connect(component_catalog_path(root))) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT r.revision_id,r.component_id,r.model_kind,r.content_sha256,
            r.snapshot_json,r.source_id,c.manufacturer,c.model AS name,
            s.kind AS source_kind,s.uri AS source_uri,s.sha256 AS source_sha256
            FROM model_revisions r JOIN components c ON c.component_id=r.component_id
            LEFT JOIN sources s ON s.source_id=r.source_id
            WHERE c.family='lens'"""
            )
        ]
        candidates = {
            row[0]
            for row in conn.execute(
                """SELECT DISTINCT l.revision_id FROM artifact_links l
            JOIN artifacts a ON a.artifact_id=l.artifact_id
            WHERE a.kind='geometric_psf' AND a.path LIKE '%/raytrace_psf_v2/%.npz'"""
            )
        }
    source_maps: dict[str, tuple[Path | None, dict, str | None]] = {}
    for row in rows:
        source_id = row["source_id"]
        if row["model_kind"] != "patent_prescription" or source_id in source_maps:
            continue
        try:
            _require(
                row["source_kind"] == "legacy_lens_database",
                "unsupported_source",
                "Lens revision has no supported source DB",
            )
            source_path = _source_path(row["source_uri"], root, row["source_sha256"])
            _require(
                file_hash(source_path) == row["source_sha256"],
                "source_hash_mismatch",
                "Lens source DB changed after import",
            )
            with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as legacy:
                mapping: dict[tuple[str, str, str], list[str]] = {}
                for pub, prescription_id, config, sim_id in legacy.execute(
                    """SELECT l.publication_number,l.prescription_id,s.configuration,
                    s.simulation_id FROM simulation_results s
                    JOIN lenses l ON l.lens_id=s.lens_id"""
                ):
                    mapping.setdefault((str(pub), str(prescription_id), str(config)), []).append(
                        sim_id
                    )
            source_maps[source_id] = (source_path, mapping, None)
        except (LensCatalogBridgeError, sqlite3.Error) as exc:
            reason = exc.code if isinstance(exc, LensCatalogBridgeError) else "source_db_invalid"
            source_maps[source_id] = (None, {}, reason)

    revisions = []
    for row in rows:
        status, reason, sim_id, configuration = "blocked", None, None, None
        snapshot = json.loads(row["snapshot_json"])
        configuration = snapshot.get("configuration") if isinstance(snapshot, dict) else None
        identity = snapshot.get("identity", {}) if isinstance(snapshot, dict) else {}
        if not isinstance(identity, dict):
            identity = {}
        publication = identity.get("publication_number")
        if row["model_kind"] != "patent_prescription":
            reason = "unsupported_response_contract"
        elif digest(snapshot) != row["content_sha256"]:
            reason = "revision_hash_mismatch"
        else:
            source_path, mapping, source_reason = source_maps[row["source_id"]]
            if source_reason:
                reason = source_reason
            else:
                key = (
                    str(identity.get("publication_number")),
                    str(identity.get("prescription_id")),
                    str(configuration),
                )
                sim_ids = mapping.get(key, [])
                if len(sim_ids) != 1:
                    reason = "simulation_ambiguous"
                else:
                    sim_id = sim_ids[0]
                    if row["revision_id"] not in candidates:
                        reason = "validated_psf_missing"
                    else:
                        try:
                            revision = {"revision_id": row["revision_id"]}
                            _asset(revision, snapshot, source_path, root)
                            status = "ready"
                        except LensCatalogBridgeError as exc:
                            reason = exc.code
        item = {
            "revision_id": row["revision_id"],
            "component_id": row["component_id"],
            "manufacturer": row["manufacturer"],
            "name": row["name"],
            "model_kind": row["model_kind"],
            "configuration": configuration,
            "publication_number": publication,
            "simulation_id": sim_id,
            "preview_status": status,
            "reason_code": reason,
            "r1_raw_status": "blocked",
        }
        if (
            query is None
            or query.casefold() in " ".join(str(value) for value in item.values()).casefold()
        ):
            revisions.append(item)
    revisions.sort(
        key=lambda item: (
            item["preview_status"] != "ready",
            str(item["manufacturer"]),
            str(item["name"]),
            str(item["configuration"]),
            item["revision_id"],
        )
    )
    return {
        "schema": "camerae2e_lens_revision_catalog_v1",
        "total": len(revisions),
        "preview_ready_count": sum(item["preview_status"] == "ready" for item in revisions),
        "revisions": revisions,
    }
