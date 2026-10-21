"""Read-only, metadata-first view of the existing design asset catalogs.

No importer is called here: catalog readers in older versions can initialize a
database.  This adapter opens SQLite with ``mode=ro`` and exports only selected
metadata, never report bodies, source paths, or proprietary report images.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA = "camera_component_explorer_catalog_v1"
_LENS_KINDS = ("patent_prescription", "public_optical_prescription")
_PARAMETERS = (
    "focal_length_mm",
    "f_number",
    "image_height_mm",
    "image_circle_mm",
    "pixel_pitch_um",
    "optical_stack_height_um",
    "cfa_pattern",
    "pixel_architecture",
    "dti_type",
)
_CACHE: dict[str, tuple[tuple, dict, dict]] = {}
_LOCK = threading.RLock()


def _json(value: Any, default=None):
    if value is None:
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {} if default is None else default


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _text(value: Any, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    # Paths and embedded source text are not part of the public DTO contract.
    if not text or re.search(r"(?:^|\s)(?:/|[A-Za-z]:\\)|file://", text):
        return None
    return text[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return _number(value)
    return _text(value) if isinstance(value, str) else None


def _paths(workspace: str | Path) -> tuple[Path, Path]:
    base = Path(workspace).expanduser().resolve() / "camerae2e_db"
    return base / "catalog.sqlite", base / "fdtd_tcad/image_sensor_db/sensor_catalog.json"


def _stamp(db: Path, reports: Path) -> tuple:
    result = []
    for path in (db, Path(str(db) + "-wal"), reports):
        try:
            stat = path.stat()
            result.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            result.append((str(path), None))
    return tuple(result)


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _asset_id(family: str, revision: str, configuration: str | None) -> str:
    return family + "-" + _digest([revision, configuration])[:24]


def _build(db: Path, reports_path: Path) -> tuple[dict, dict]:
    if not db.is_file():
        raise FileNotFoundError("The existing component catalog is unavailable.")
    reports = _json(reports_path.read_text()) if reports_path.is_file() else {}
    report_records = {}
    for record in reports.get("records", []):
        code = record.get("code") or record.get("metadata", {}).get("code")
        if code:
            # Keep duplicate records instead of silently choosing a last writer.
            report_records.setdefault(str(code), []).append(record)
    with _connect(db) as con:
        rows = con.execute("""
          SELECT m.revision_id,m.component_id,m.model_kind,m.content_sha256,
                 c.family,c.manufacturer,c.model,s.sha256 AS source_sha256,
                 o.operating_point_id,o.label AS configuration_label,
                 l.focal_length_mm,l.f_number,l.image_height_mm,l.image_circle_mm,
                 json_extract(m.snapshot_json,'$.identity.publication_number')
                   AS publication_number,
                 json_extract(m.snapshot_json,'$.specifications.source_filename')
                   AS source_filename,
                 json_extract(m.snapshot_json,'$.metadata.code') AS report_code,
                 json_extract(m.snapshot_json,'$.metadata.device_name') AS device_name,
                 json_extract(m.snapshot_json,'$.derived_specs') AS derived_specs,
                 json_array_length(json_extract(m.snapshot_json,'$.surfaces')) AS surface_count
          FROM model_revisions m JOIN components c USING(component_id)
          LEFT JOIN sources s ON s.source_id=m.source_id
          LEFT JOIN operating_points o ON o.revision_id=m.revision_id
          LEFT JOIN lens_configurations l ON l.operating_point_id=o.operating_point_id
          WHERE m.model_kind IN (
            'patent_prescription','public_optical_prescription','sensor_report_extraction')
          ORDER BY c.family,c.manufacturer,c.model,m.revision_id,o.operating_point_id
        """).fetchall()
        component_count = dict(
            con.execute("SELECT family,count(*) FROM components GROUP BY family")
        )
    items = []
    # Only selected scalar metadata is retained. Full prescriptions load on demand.
    for row in rows:
        row = dict(row)
        family = row["family"]
        specs = _json(row["derived_specs"]) if family == "sensor" else {}
        parameters = {key: None for key in _PARAMETERS}
        if family == "lens":
            for key in _PARAMETERS[:4]:
                parameters[key] = _number(row.get(key))
            label = (
                _text(row["publication_number"] or row["source_filename"])
                or "Published optical design"
            )
            name = _text(row["model"]) or label
            structure = bool(row["surface_count"])
        else:
            for key in _PARAMETERS[4:]:
                parameters[key] = _scalar(specs.get(key))
            label = "TechInsights · " + (_text(row["report_code"]) or "Unidentified report")
            name = _text(row["device_name"] or row["model"]) or label
            structure = any(
                specs.get(key) is not None
                for key in (
                    "optical_stack_height_um",
                    "active_si_thickness_um",
                    "cfa_thickness_um",
                    "cfa_thickness_min_um",
                    "dti_depth_um",
                )
            )
        configuration = row["operating_point_id"] if family == "lens" else None
        items.append(
            {
                "id": _asset_id(family, row["revision_id"], configuration),
                "family": family,
                "component_id": row["component_id"],
                "revision_id": row["revision_id"],
                "configuration_id": configuration,
                "configuration_label": _text(row["configuration_label"])
                if family == "lens"
                else None,
                "name": name,
                "manufacturer": _text(row["manufacturer"]) or "Unknown",
                "source_label": label,
                "source_kind": row["model_kind"],
                "content_sha256": row["content_sha256"],
                "source_sha256": row["source_sha256"],
                "source_hash_scope": "normalized_extracted_record"
                if family == "sensor"
                else "source_catalog_or_file",
                "report_code": _text(row["report_code"]),
                "publication_number": _text(row["publication_number"]),
                "parameters": parameters,
                "structure_available": structure,
            }
        )
    lenses = [item for item in items if item["family"] == "lens"]
    sensors = [item for item in items if item["family"] == "sensor"]
    counts = {
        "total_assets": len(items),
        "lens_components": len({item["component_id"] for item in lenses}),
        "lens_prescription_revisions": len({item["revision_id"] for item in lenses}),
        "lens_configurations": len(
            {item["configuration_id"] for item in lenses if item["configuration_id"]}
        ),
        "sensor_reports": len(sensors),
        "sensor_model_labels": len({(item["manufacturer"], item["name"]) for item in sensors}),
        "sensor_json_records": len(reports.get("records", [])),
        "catalog_components": component_count,
    }
    facets = {}
    for family, subset in (("lens", lenses), ("sensor", sensors)):
        facets[family] = {
            "manufacturers": sorted({item["manufacturer"] for item in subset}),
            "source_kinds": sorted({item["source_kind"] for item in subset}),
            "cfa_patterns": sorted(
                {
                    item["parameters"]["cfa_pattern"]
                    for item in subset
                    if item["parameters"]["cfa_pattern"]
                }
            ),
            "pixel_architectures": sorted(
                {
                    item["parameters"]["pixel_architecture"]
                    for item in subset
                    if item["parameters"]["pixel_architecture"]
                }
            ),
            "dti_types": sorted(
                {
                    item["parameters"]["dti_type"]
                    for item in subset
                    if item["parameters"]["dti_type"]
                }
            ),
        }
    provenance = {
        "catalog_sha256": _hash_file(db),
        "sensor_catalog_sha256": _hash_file(reports_path) if reports_path.is_file() else None,
    }
    result = {
        "schema": SCHEMA,
        "snapshot_id": _digest([provenance, items]),
        "counts": counts,
        "items": items,
        "facets": facets,
        "source_hashes": provenance,
        "notes": [
            "Lens components, prescription revisions and configurations are counted separately.",
            "Sensor reports and model-name labels are not counts of unique physical products.",
            "Source-derived dimensions are not product performance measurements "
            "or solver validation.",
        ],
    }
    return result, report_records


def _cached(workspace: str | Path) -> tuple[dict, dict]:
    db, reports = _paths(workspace)
    key = str(db)
    stamp = _stamp(db, reports)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is None or cached[0] != stamp:
            result, report_records = _build(db, reports)
            # Do not cache a mixed snapshot when an external process changes it.
            if _stamp(db, reports) != stamp:
                raise ValueError("The source catalog changed during reading; retry the request.")
            cached = (stamp, result, report_records)
            _CACHE[key] = cached
        return cached[1], cached[2]


def index(workspace: str | Path) -> dict:
    """Return one immutable metadata snapshot; callers may filter the copy."""
    return copy.deepcopy(_cached(workspace)[0])


def _unit(field: str) -> str | None:
    for suffix, unit in (("_um", "µm"), ("_nm", "nm"), ("_mm", "mm"), ("_deg", "degree")):
        if field.endswith(suffix):
            return unit
    return None


def _lens_structure(snapshot: dict) -> dict:
    convention = snapshot.get("prescription_conventions", {})
    elements, notes = [], set()
    for surface in snapshot.get("surfaces", []):
        raw = surface.get("raw", {})
        kind = _text(surface.get("surface_kind") or raw.get("surface_kind")) or "surface"
        aperture_convention = (
            _text(
                surface.get("aperture_convention") or convention.get("aperture_radius_or_diameter")
            )
            or "unknown"
        )
        coefficients = {
            str(k): _number(v)
            for k, v in surface.get("coefficients", {}).items()
            if _number(v) is not None
        }
        radius = _number(surface.get("radius_mm"))
        radius_state = surface.get("radius_state")
        if radius_state is None and str(raw.get("radius", "")).lower() in ("infinity", "inf", "∞"):
            radius_state = "plane"
        aspheric = bool(surface.get("is_aspheric") or coefficients)
        if aspheric:
            notes.add(
                "Aspheric coefficients are retained as metadata; "
                "the diagram does not reconstruct unverified aspheric sag."
            )
        if aperture_convention == "unknown":
            notes.add(
                "The legacy aperture radius/diameter convention is unknown: "
                "no exact physical outline is inferred."
            )
        elements.append(
            {
                "surface_order": surface.get("surface_order"),
                "surface_kind": kind,
                "radius_mm": radius,
                "radius_state": _text(radius_state)
                or ("finite" if radius is not None else "unknown"),
                "thickness_mm": _number(surface.get("thickness_mm")),
                "thickness_state": _text(surface.get("thickness_state")),
                "aperture_mm": _number(
                    surface.get("effective_aperture_mm", surface.get("aperture_mm"))
                ),
                "aperture_convention": aperture_convention,
                "material": _text(surface.get("material")),
                "nd": _number(surface.get("nd")),
                "vd": _number(surface.get("vd")),
                "is_aspheric": aspheric,
                "conic": _number(surface.get("conic")),
                "coefficients": coefficients,
                "source_class": "source_derived",
            }
        )
    if snapshot.get("parse_issues"):
        issue_counts = Counter(
            str(issue.get("code", "unclassified")) for issue in snapshot["parse_issues"]
        )
        notes.add(
            "Source parser flags: "
            + ", ".join(f"{key} ({value})" for key, value in sorted(issue_counts.items()))
        )
    return {
        "kind": "lens",
        "status": "prescription_dimensions" if elements else "unavailable",
        "elements": elements,
        "notes": sorted(notes),
    }


def _sensor_structure(specs: dict) -> dict:
    elements = []
    for field, name in (
        ("optical_stack_height_um", "Optical stack: microlens to silicon"),
        ("cfa_thickness_um", "CFA thickness"),
        ("active_si_thickness_um", "Active silicon thickness"),
        ("dti_depth_um", "DTI depth"),
        ("dti_width_um", "DTI width"),
    ):
        value = _number(specs.get(field))
        minimum = (
            _number(specs.get("cfa_thickness_min_um")) if field == "cfa_thickness_um" else None
        )
        maximum = (
            _number(specs.get("cfa_thickness_max_um")) if field == "cfa_thickness_um" else None
        )
        if value is not None or minimum is not None or maximum is not None:
            elements.append(
                {
                    "kind": "dimension",
                    "field": field,
                    "name": name,
                    "thickness_um": value,
                    "min_um": minimum,
                    "max_um": maximum,
                    "source_class": "source_derived",
                }
            )
    return {
        "kind": "sensor",
        "status": "reported_dimensions" if elements else "unavailable",
        "elements": elements,
        "notes": [
            "Dimensions can overlap: optical-stack height includes CFA. "
            "These are not additive layers or a verified cross-section.",
            "DTI, OCL and CFA observations describe extracted structure, "
            "not evidence that their optical/electrical effects were simulated.",
            "Missing dimensions, material identities and layer order are not synthesized.",
        ],
    }


def detail(workspace: str | Path, asset_id: str) -> dict:
    """Read the selected revision only, retaining ambiguity and source evidence."""
    snapshot_index, report_records = _cached(workspace)
    item = next((item for item in snapshot_index["items"] if item["id"] == asset_id), None)
    if item is None:
        raise KeyError("Unknown design asset.")
    db, _ = _paths(workspace)
    with _connect(db) as con:
        revision = con.execute(
            "SELECT snapshot_json,content_sha256 FROM model_revisions WHERE revision_id=?",
            (item["revision_id"],),
        ).fetchone()
        revisions = [
            dict(row)
            for row in con.execute(
                "SELECT revision_id,model_kind,content_sha256 FROM model_revisions "
                "WHERE component_id=? ORDER BY model_kind,revision_id",
                (item["component_id"],),
            )
        ]
    if revision is None or revision["content_sha256"] != item["content_sha256"]:
        raise ValueError("The selected source revision changed; reload the catalog.")
    snapshot = _json(revision["snapshot_json"])
    result = copy.deepcopy(item)
    evidence, limitations = [], []
    if item["family"] == "lens":
        specs = snapshot.get("specifications", {})
        for field, value in item["parameters"].items():
            if value is None:
                continue
            origin = str(specs.get(field.removesuffix("_mm") + "_source", ""))
            source_class = (
                "model_assumption"
                if any(word in origin.lower() for word in ("proxy", "estimate", "infer"))
                else "source_derived"
            )
            evidence.append(
                {
                    "field": field,
                    "value": value,
                    "unit": _unit(field),
                    "source_class": source_class,
                    "source_label": item["source_label"],
                }
            )
        result["structure"] = _lens_structure(snapshot)
        limitations.extend(result["structure"]["notes"])
        limitations.append(
            "Published prescriptions are not confirmed production lenses. "
            "Metadata availability does not establish executable or validated optics."
        )
    else:
        specs = snapshot.get("derived_specs", {})
        for field, value in sorted(specs.items()):
            if _scalar(value) is not None:
                evidence.append(
                    {
                        "field": field,
                        "value": _scalar(value),
                        "unit": _unit(field),
                        "source_class": "source_derived",
                        "source_label": item["source_label"],
                    }
                )
        for record in report_records.get(item["report_code"], []):
            for field, value in record.get("derived_specs", {}).items():
                if field in specs and _scalar(value) is not None and value != specs[field]:
                    evidence.append(
                        {
                            "field": field,
                            "value": _scalar(value),
                            "unit": _unit(field),
                            "source_class": "conflicting",
                            "source_label": "Current extracted report catalog",
                        }
                    )
                    limitations.append(
                        f"{field}: report catalog differs from the selected immutable revision; "
                        "values were not merged."
                    )
        result["structure"] = _sensor_structure(specs)
        limitations.extend(result["structure"]["notes"])
        limitations.extend(
            [
                "Reports with the same model name remain separate source identities.",
                "Generated sensor and readout models use assumptions; "
                "a report entry is not a measured product-performance ranking.",
            ]
        )
    result.update(
        {
            "schema": SCHEMA,
            "snapshot_id": snapshot_index["snapshot_id"],
            "source_evidence": evidence,
            "revisions": revisions,
            "limitations": list(dict.fromkeys(limitations)),
        }
    )
    return result
