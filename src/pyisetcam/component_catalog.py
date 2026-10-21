"""Persistent component identities, evidence, frozen models and camera combinations.

This catalog complements the existing runtime database registry. Importing it
does not change legacy databases, select runtime defaults, or run a simulation.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ._catalog_store import (
    SCHEMA_NAME,
    CatalogWriter,
    connect,
    digest,
    file_hash,
    json_text,
    now,
    stable_id,
    write_json,
)


def component_catalog_path(root: str | Path | None = None) -> Path:
    """Return catalog path; root is the CameraE2E data root, not a source subdirectory."""
    base = (
        root
        or os.environ.get("PYISETCAM_CAMERA_DB_ROOT")
        or (Path(__file__).resolve().parents[2] / "camerae2e_db")
    )
    return Path(base).expanduser().resolve() / "catalog.sqlite"


def _rows(conn: sqlite3.Connection, sql: str, values: Any = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, values)]


def _decoded(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in list(result):
        if key.endswith("_json"):
            value = result.pop(key)
            result[key[:-5]] = json.loads(value) if value is not None else None
    return result


def _resolve_component(conn: sqlite3.Connection, identity: str) -> dict[str, Any]:
    exact = conn.execute("SELECT * FROM components WHERE component_id=?", (identity,)).fetchone()
    if exact:
        return dict(exact)
    rows = _rows(
        conn,
        """SELECT DISTINCT c.* FROM components c JOIN aliases a
        ON a.component_id=c.component_id WHERE a.alias=? COLLATE NOCASE""",
        (identity,),
    )
    if not rows:
        raise KeyError(f"Unknown component ID or alias: {identity}")
    if len(rows) != 1:
        raise ValueError(
            f"Ambiguous component alias {identity!r}; choose a component_id: "
            + ", ".join(r["component_id"] for r in rows)
        )
    return rows[0]


def _revision(
    conn: sqlite3.Connection, revision_id: str, family: str | None = None
) -> dict[str, Any]:
    row = conn.execute(
        """SELECT r.*,c.family FROM model_revisions r JOIN components c
        ON c.component_id=r.component_id WHERE r.revision_id=?""",
        (revision_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown model revision: {revision_id}")
    if family and row["family"] != family:
        raise ValueError(f"Expected {family} revision, received {row['family']}")
    return dict(row)


def _discover_lens(root: Path) -> Path | None:
    paths = list((root / "lens_db").glob("*/data/lens_patents/lens_patent_simulation_v*.sqlite"))
    if not paths:
        return None

    def version(path: Path) -> int:
        match = re.search(r"_v(\d+)\.sqlite$", path.name)
        return int(match.group(1)) if match else -1

    latest = max(version(p) for p in paths)
    candidates = [p for p in paths if version(p) == latest]
    if len(candidates) != 1:
        raise ValueError("Multiple lens masters at the latest version; specify lens_db explicitly.")
    return candidates[0]


def build_component_catalog(
    *,
    root: str | Path | None = None,
    lens_db: str | Path | None = None,
    sensor_catalog: str | Path | None = None,
    sensor_simulations: str | Path | None = None,
) -> dict[str, Any]:
    """Import canonical legacy inputs, preserving previous revisions and runs.

    Explicit missing paths are errors. Missing optional defaults are reported;
    custom output roots never silently import data from another workspace.
    """
    from ._catalog_lens import import_lenses
    from ._catalog_sensor import import_sensors

    database = component_catalog_path(root)
    base = database.parent
    explicit = dict(
        lens_db=lens_db, sensor_catalog=sensor_catalog, sensor_simulations=sensor_simulations
    )
    candidates = {
        "lens_db": lens_db if lens_db is not None else _discover_lens(base),
        "sensor_catalog": sensor_catalog
        if sensor_catalog is not None
        else (base / "fdtd_tcad/sensor_db/sensor_catalog.json"),
        "sensor_simulations": sensor_simulations
        if sensor_simulations is not None
        else (base / "image_sensor/simulations/sensor_simulations.sqlite"),
    }
    paths, warnings = {}, []
    for key, value in candidates.items():
        path = Path(value).expanduser().resolve() if value is not None else None
        if path is not None and path.is_file():
            paths[key] = path
        elif explicit[key] is not None:
            raise FileNotFoundError(f"Explicit {key} does not exist: {path}")
        else:
            warnings.append(f"Optional default input unavailable: {key}")
    if "sensor_simulations" in paths and "sensor_catalog" not in paths:
        raise ValueError("Sensor simulation import requires a sensor_catalog for identity mapping.")
    if not ({"lens_db", "sensor_catalog"} & paths.keys()):
        raise FileNotFoundError(
            "No lens or sensor source found under this root; provide input paths."
        )
    inputs = {key: {"path": str(path), "sha256": file_hash(path)} for key, path in paths.items()}
    report = {"warnings": warnings, "inputs": inputs}
    with closing(connect(database, write=True)) as conn, conn:
        writer = CatalogWriter(conn, base)
        if "lens_db" in paths:
            report["lens"] = import_lenses(writer, paths["lens_db"])
        if "sensor_catalog" in paths:
            report["sensor"] = import_sensors(
                writer, paths["sensor_catalog"], paths.get("sensor_simulations")
            )
        for family in ("lens", "sensor"):
            warnings.extend(report.get(family, {}).get("warnings", []))
        # A partially changing source must not publish a mixed snapshot.
        for key, path in paths.items():
            if file_hash(path) != inputs[key]["sha256"]:
                raise ValueError(
                    f"Source changed during import: {path}; retry after writes finish."
                )
        importer_hashes = {
            name: file_hash(Path(__file__).with_name(name))
            for name in (
                "_catalog_lens.py",
                "_catalog_sensor.py",
                "_catalog_store.py",
                "component_catalog.py",
            )
        }
        source_manifest = {
            "schema": "camerae2e_component_sources_v1",
            "inputs": inputs,
            "importer_sha256": importer_hashes,
            "sources": [
                _decoded(row) for row in conn.execute("SELECT * FROM sources ORDER BY source_id")
            ],
        }
        batch_id = stable_id("import", source_manifest)
        relative_manifest = Path("sources") / f"{batch_id}.json"
        write_json(base / relative_manifest, source_manifest)
        report["source_manifest"] = relative_manifest.as_posix()
        writer.put(
            "import_batches",
            batch_id=batch_id,
            created_at=now(),
            inputs_json=json_text(inputs),
            report_json=json_text(report),
        )
    return {
        **component_catalog_summary(root=base),
        "import_report": report,
        "warnings": warnings,
        "batch_id": batch_id,
    }


def component_catalog_summary(*, root: str | Path | None = None) -> dict[str, Any]:
    path = component_catalog_path(root)
    result = {"schema": SCHEMA_NAME, "db_path": str(path), "status": "missing", "counts": {}}
    if not path.is_file():
        return result
    with closing(connect(path)) as conn:
        counts = {
            name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in (
                "components",
                "sources",
                "spec_observations",
                "model_revisions",
                "operating_points",
                "simulation_runs",
                "artifacts",
                "validations",
                "camera_configurations",
                "releases",
            )
        }
        counts["by_family"] = dict(
            conn.execute("SELECT family,count(*) FROM components GROUP BY family")
        )
        counts["runs_by_status"] = dict(
            conn.execute("SELECT status,count(*) FROM simulation_runs GROUP BY status")
        )
        counts["artifacts_by_availability"] = dict(
            conn.execute("SELECT availability,count(*) FROM artifacts GROUP BY availability")
        )
    return {**result, "status": "available", "counts": counts, "measured_accuracy_verified": False}


def component_catalog_search(
    query: str | None = None,
    *,
    family: str | None = None,
    manufacturer: str | None = None,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Search identities/aliases without reading source files or materializing arrays."""
    path = component_catalog_path(root)
    if not path.is_file():
        return []
    clauses, values = [], []
    if family is not None:
        clauses.append("c.family=?")
        values.append(family)
    if manufacturer is not None:
        clauses.append("c.manufacturer=? COLLATE NOCASE")
        values.append(manufacturer)
    if query:
        clauses.append("""(instr(lower(c.model),lower(?)) OR instr(lower(c.manufacturer),lower(?))
          OR c.component_id=? OR EXISTS(SELECT 1 FROM aliases a WHERE a.component_id=c.component_id
          AND instr(lower(a.alias),lower(?))))""")
        values.extend([query] * 4)
    sql = """SELECT c.*, p.revision_id AS selected_revision_id,
      (SELECT count(*) FROM model_revisions r WHERE r.component_id=c.component_id) AS revision_count
      FROM components c LEFT JOIN component_preferences p ON p.component_id=c.component_id"""
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with closing(connect(path)) as conn:
        return _rows(conn, sql + " ORDER BY c.family,c.manufacturer,c.model,c.component_id", values)


def component_catalog_get(
    component_id_or_alias: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Read complete evidence/history for one unambiguous component. No array IO."""
    with closing(connect(component_catalog_path(root))) as conn:
        component = _resolve_component(conn, component_id_or_alias)
        identity = component["component_id"]
        revision_sql = "SELECT revision_id FROM model_revisions WHERE component_id=?"
        data = {
            "component": component,
            "aliases": _rows(conn, "SELECT * FROM aliases WHERE component_id=?", (identity,)),
            "revisions": [
                _decoded(r)
                for r in _rows(
                    conn,
                    "SELECT * FROM model_revisions WHERE component_id=? "
                    "ORDER BY created_at,revision_id",
                    (identity,),
                )
            ],
            "observations": [
                _decoded(r)
                for r in _rows(
                    conn,
                    "SELECT * FROM spec_observations WHERE component_id=? "
                    "ORDER BY property_name,observation_id",
                    (identity,),
                )
            ],
            "operating_points": [
                _decoded(r)
                for r in _rows(
                    conn,
                    f"SELECT * FROM operating_points WHERE revision_id IN ({revision_sql})",
                    (identity,),
                )
            ],
            "runs": [
                _decoded(r)
                for r in _rows(
                    conn,
                    f"SELECT * FROM simulation_runs WHERE revision_id IN ({revision_sql}) "
                    "ORDER BY created_at,run_id",
                    (identity,),
                )
            ],
            "artifacts": [
                _decoded(r)
                for r in _rows(
                    conn,
                    f"""SELECT DISTINCT a.* FROM artifacts a JOIN artifact_links l
                ON l.artifact_id=a.artifact_id WHERE l.revision_id IN ({revision_sql})""",
                    (identity,),
                )
            ],
        }
        data["artifact_links"] = _rows(
            conn, f"SELECT * FROM artifact_links WHERE revision_id IN ({revision_sql})", (identity,)
        )
        source_ids = {
            r["source_id"]
            for r in data["revisions"] + data["observations"] + data["runs"]
            if r.get("source_id")
        }
        data["sources"] = [
            _decoded(conn.execute("SELECT * FROM sources WHERE source_id=?", (s,)).fetchone())
            for s in sorted(source_ids)
        ]
        target_ids = (
            {identity}
            | {r["revision_id"] for r in data["revisions"]}
            | {r["run_id"] for r in data["runs"]}
            | {r["artifact_id"] for r in data["artifacts"]}
        )
        data["validations"] = [
            _decoded(r)
            for r in conn.execute("SELECT * FROM validations")
            if r["target_id"] in target_ids
        ]
        pref = conn.execute(
            "SELECT revision_id FROM component_preferences WHERE component_id=?", (identity,)
        ).fetchone()
        data["selected_revision_id"] = pref[0] if pref else None
        return data


def component_catalog_select_revision(
    component_id_or_alias: str,
    revision_id: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Explicitly choose a representative model; imports never change this selection."""
    with closing(connect(component_catalog_path(root), write=True)) as conn, conn:
        component = _resolve_component(conn, component_id_or_alias)
        revision = _revision(conn, revision_id)
        if revision["component_id"] != component["component_id"]:
            raise ValueError("Selected revision belongs to a different component.")
        conn.execute(
            """INSERT INTO component_preferences VALUES (?,?)
          ON CONFLICT(component_id) DO UPDATE SET revision_id=excluded.revision_id""",
            (component["component_id"], revision_id),
        )
    return {"component_id": component["component_id"], "selected_revision_id": revision_id}


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"Catalog path escapes data root: {relative}")
    return path


def component_catalog_validate(
    *,
    root: str | Path | None = None,
    verify_files: bool = False,
) -> dict[str, Any]:
    """Validate DB relations/identity and optionally artifact bytes, not measured accuracy."""
    path = component_catalog_path(root)
    errors, warnings = [], []
    if not path.is_file():
        return {"status": "failed", "errors": ["Component catalog is missing"], "warnings": []}
    try:
        with closing(connect(path)) as conn:
            checks = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            if checks != ["ok"]:
                errors.extend(checks)
            errors.extend(
                f"Foreign key violation: {tuple(r)}"
                for r in conn.execute("PRAGMA foreign_key_check")
            )
            for row in conn.execute("SELECT * FROM model_revisions"):
                snapshot = json.loads(row["snapshot_json"])
                if digest(snapshot) != row["content_sha256"]:
                    errors.append(f"Model content hash mismatch: {row['revision_id']}")
                if verify_files:
                    payload = _safe_path(path.parent, row["snapshot_path"])
                    if (
                        not payload.is_file()
                        or digest(json.loads(payload.read_text())) != row["content_sha256"]
                    ):
                        errors.append(f"Model snapshot unavailable/corrupt: {row['revision_id']}")
            for table, column in (
                ("simulation_runs", "operating_point_id"),
                ("spec_observations", "operating_point_id"),
            ):
                bad = conn.execute(f"""SELECT count(*) FROM {table} r JOIN operating_points o
                  ON o.operating_point_id=r.{column} WHERE r.revision_id IS NOT NULL
                  AND r.revision_id<>o.revision_id""").fetchone()[0]
                if bad:
                    errors.append(f"{bad} revision/operating-point mismatches in {table}")
            for row in conn.execute("SELECT * FROM artifacts"):
                if row["integrity_status"] == "failed":
                    errors.append(
                        f"Imported artifact hash disagrees with source: {row['artifact_id']}"
                    )
                if row["availability"] == "missing":
                    warnings.append(f"Missing source artifact: {row['artifact_id']}")
                elif verify_files:
                    payload = _safe_path(path.parent, row["path"])
                    if not payload.is_file() or file_hash(payload) != row["sha256"]:
                        errors.append(f"Artifact unavailable/corrupt: {row['artifact_id']}")
    except (ValueError, OSError, sqlite3.Error) as exc:
        errors.append(str(exc))
    return {
        "status": "failed" if errors else "passed",
        "errors": errors,
        "warnings": warnings,
        "files_verified": verify_files,
        "measured_accuracy_verified": False,
    }


def _point(conn: sqlite3.Connection, revision_id: str, requested: str | None) -> str | None:
    if requested is None:
        rows = conn.execute(
            "SELECT operating_point_id FROM operating_points WHERE revision_id=?", (revision_id,)
        ).fetchall()
        return rows[0][0] if len(rows) == 1 else None
    row = conn.execute(
        "SELECT revision_id FROM operating_points WHERE operating_point_id=?", (requested,)
    ).fetchone()
    if row is None or row[0] != revision_id:
        raise ValueError("Operating point must belong to the selected revision.")
    return requested


def camera_configuration_save(
    name: str,
    lens_revision_id: str,
    sensor_revision_id: str,
    *,
    root: str | Path | None = None,
    lens_operating_point_id: str | None = None,
    sensor_operating_point_id: str | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save an immutable lens/sensor combination with explicitly limited compatibility checks."""
    path = component_catalog_path(root)
    with closing(connect(path, write=True)) as conn, conn:
        _revision(conn, lens_revision_id, "lens")
        _revision(conn, sensor_revision_id, "sensor")
        lens_point = _point(conn, lens_revision_id, lens_operating_point_id)
        sensor_point = _point(conn, sensor_revision_id, sensor_operating_point_id)
        lens = conn.execute(
            "SELECT image_circle_mm FROM lens_configurations WHERE operating_point_id=?",
            (lens_point,),
        ).fetchone()
        sensor = conn.execute(
            "SELECT active_width_mm,active_height_mm FROM sensor_geometry WHERE revision_id=?",
            (sensor_revision_id,),
        ).fetchone()
        coverage = {
            "status": "unknown",
            "reason": "Explicit image circle or active sensor dimensions unavailable",
        }
        dimensions = [lens[0], sensor[0], sensor[1]] if lens and sensor else []
        valid_dimensions = len(dimensions) == 3 and all(
            isinstance(value, (int, float)) and math.isfinite(value) and value > 0
            for value in dimensions
        )
        if dimensions and all(value is not None for value in dimensions) and not valid_dimensions:
            coverage["reason"] = (
                "Image circle and active dimensions must be finite positive numbers"
            )
        if valid_dimensions:
            diagonal = math.hypot(sensor[0], sensor[1])
            coverage = {
                "status": "passed" if lens[0] >= diagonal else "failed",
                "image_circle_mm": lens[0],
                "active_diagonal_mm": diagonal,
                "scope": "Centered geometrical coverage only; "
                "source observations may be unverified",
            }
        compatibility = {
            "coverage": coverage,
            "angular_response": {
                "status": "unknown",
                "reason": "No matched CRA/acceptance validation",
            },
            "mechanical": {"status": "unknown", "reason": "Mount and placement not validated"},
            "status": "failed" if coverage["status"] == "failed" else "unknown",
        }
        record = dict(
            name=name,
            lens_revision_id=lens_revision_id,
            sensor_revision_id=sensor_revision_id,
            lens_operating_point_id=lens_point,
            sensor_operating_point_id=sensor_point,
            settings_json=json_text(settings or {}),
            compatibility_json=json_text(compatibility),
        )
        identity = stable_id("camera_configuration", record)
        CatalogWriter(conn, path.parent).put(
            "camera_configurations", configuration_id=identity, **record, created_at=now()
        )
    return camera_configuration_get(identity, root=path.parent)


def camera_configuration_get(
    configuration_id: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    with closing(connect(component_catalog_path(root))) as conn:
        row = conn.execute(
            "SELECT * FROM camera_configurations WHERE configuration_id=?", (configuration_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown camera configuration: {configuration_id}")
        return _decoded(row)


def component_catalog_release(
    name: str,
    *,
    root: str | Path | None = None,
    revision_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Pin explicit or user-selected models and their artifact hashes in a release manifest."""
    path = component_catalog_path(root)
    with closing(connect(path, write=True)) as conn, conn:
        ids = sorted(
            set(
                revision_ids
                if revision_ids is not None
                else [r[0] for r in conn.execute("SELECT revision_id FROM component_preferences")]
            )
        )
        if not ids:
            raise ValueError(
                "Select model revisions first or provide revision_ids for this release."
            )
        models = [_revision(conn, r) for r in ids]
        artifacts = []
        for revision_id in ids:
            artifacts.extend(
                _rows(
                    conn,
                    """SELECT a.* FROM artifacts a JOIN artifact_links l
              ON l.artifact_id=a.artifact_id WHERE l.revision_id=?""",
                    (revision_id,),
                )
            )
        manifest = {
            "schema": "camerae2e_component_release_v1",
            "name": name,
            "catalog_schema": SCHEMA_NAME,
            "models": [
                {
                    k: r[k]
                    for k in (
                        "revision_id",
                        "component_id",
                        "model_kind",
                        "content_sha256",
                        "snapshot_path",
                    )
                }
                for r in models
            ],
            "artifacts": sorted(
                {r["artifact_id"]: r for r in artifacts}.values(), key=lambda r: r["artifact_id"]
            ),
            "measured_accuracy_verified": False,
        }
        identity = stable_id("release", manifest)
        relative = Path("releases") / f"{identity}.json"
        write_json(path.parent / relative, manifest)
        CatalogWriter(conn, path.parent).put(
            "releases",
            release_id=identity,
            name=name,
            manifest_json=json_text(manifest),
            path=relative.as_posix(),
            created_at=now(),
        )
    return {"release_id": identity, "path": str(path.parent / relative), "manifest": manifest}


def component_catalog_export(
    *,
    root: str | Path | None = None,
    output_dir: str | Path | None = None,
    manufacturer: str | None = None,
) -> dict[str, Any]:
    """Create derived CSV and JSON evidence exports; never maintain a second writable master."""
    base = component_catalog_path(root).parent
    with closing(connect(base / "catalog.sqlite")):
        pass
    rows = component_catalog_search(root=base, manufacturer=manufacturer)
    destination = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else base
        / "exports"
        / (re.sub(r"[^a-z0-9]+", "-", manufacturer.lower()).strip("-") if manufacturer else "all")
    )
    destination.mkdir(parents=True, exist_ok=True)
    csv_path, json_path = destination / "components.csv", destination / "components.json"
    keys = [
        "component_id",
        "family",
        "entity_kind",
        "manufacturer",
        "model",
        "selected_revision_id",
        "revision_count",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    with json_path.open("w", encoding="utf-8") as stream:
        stream.write('{"schema":"camerae2e_component_export_v1","components":[')
        for index, row in enumerate(rows):
            if index:
                stream.write(",")
            stream.write(json_text(component_catalog_get(row["component_id"], root=base)))
        stream.write("]}\n")
    return {
        "status": "exported",
        "count": len(rows),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
        "manufacturer": manufacturer,
        "derived_from": str(component_catalog_path(base)),
    }
