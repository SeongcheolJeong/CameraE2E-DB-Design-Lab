"""Persistent, versioned sensor simulations for CameraE2E.

SQLite holds searchable summaries and run history; immutable JSON/NPZ artifacts
hold complete assumptions and computed arrays. Successful numerical checks do
not promote an estimated response to measured product accuracy.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

_SCHEMA = "camerae2e_sensor_simulations_v1"
_STATUSES = {"simulated", "excluded", "failed", "quarantined"}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def sensor_simulation_db_path(root: str | Path | None = None) -> Path:
    """Resolve the SQLite path; ``root`` is the CameraE2E DB directory."""
    if root is None and os.environ.get("PYISETCAM_SENSOR_SIMULATION_DB_PATH"):
        return Path(os.environ["PYISETCAM_SENSOR_SIMULATION_DB_PATH"]).expanduser().resolve()
    base = Path(
        root
        or os.environ.get("PYISETCAM_CAMERA_DB_ROOT")
        or (Path(__file__).resolve().parents[2] / "camerae2e_db")
    ).expanduser()
    return (base / "image_sensor/simulations/sensor_simulations.sqlite").resolve()


def _connect(path: Path, *, write: bool = False) -> sqlite3.Connection:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, report_code TEXT NOT NULL, sensor_id TEXT NOT NULL,
                device_name TEXT NOT NULL, manufacturer TEXT NOT NULL, model TEXT,
                status TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
                config_sha256 TEXT NOT NULL, result_path TEXT NOT NULL,
                result_sha256 TEXT NOT NULL, arrays_path TEXT, arrays_sha256 TEXT,
                summary_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sensors (
                report_code TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS runs_by_sensor ON runs(sensor_id, created_at);
        """)
        conn.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", ("schema", _json(_SCHEMA)))
        conn.commit()
    else:
        if not path.is_file():
            raise FileNotFoundError(f"Sensor simulation DB not found: {path}")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        schema = conn.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if schema is None or json.loads(schema[0]) != _SCHEMA:
            raise ValueError("Unsupported sensor simulation database schema.")
    except Exception:
        conn.close()
        raise
    return conn


def _resolve_artifact(db: Path, relative: str) -> Path:
    base = db.parent.resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Simulation artifact must remain inside the database directory.")
    return path


def _summary_row(row: sqlite3.Row, db: Path) -> dict[str, Any]:
    result = {
        key: row[key]
        for key in (
            "sensor_id",
            "report_code",
            "device_name",
            "manufacturer",
            "model",
            "status",
            "reason",
            "run_id",
            "created_at",
            "config_sha256",
            "result_sha256",
        )
    }
    result.update(json.loads(row["summary_json"]))
    result["result_path"] = str(_resolve_artifact(db, row["result_path"]))
    return result


def sensor_simulation_db_search(
    query: str | None = None,
    *,
    model: str | None = None,
    status: str | None = None,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Search the current run of each report without loading response arrays."""
    path = sensor_simulation_db_path(root)
    if not path.is_file():
        return []
    sql = "SELECT r.* FROM sensors s JOIN runs r ON r.run_id=s.run_id WHERE 1=1"
    values = []
    for field, value in (("model", model), ("status", status)):
        if value is not None:
            sql += f" AND r.{field}=?"
            values.append(value)
    with _connect(path) as conn:
        rows = conn.execute(sql + " ORDER BY r.manufacturer,r.device_name,r.report_code", values)
        result = [_summary_row(row, path) for row in rows]
    if query:
        needle = str(query).casefold()
        result = [
            r
            for r in result
            if needle
            in " ".join(
                str(r[k])
                for k in ("sensor_id", "report_code", "device_name", "manufacturer", "model")
            ).casefold()
        ]
    return result


def sensor_simulation_db_summary(*, root: str | Path | None = None) -> dict[str, Any]:
    """Return coverage, provenance and integrity identity of the persisted DB."""
    path = sensor_simulation_db_path(root)
    counts = {"total": 0, **dict.fromkeys(sorted(_STATUSES), 0), "by_model": {}}
    result = {
        "schema": _SCHEMA,
        "status": "missing",
        "db_path": str(path),
        "counts": counts,
        "provenance": {},
        "db_sha256": None,
        "measured_accuracy_verified": False,
    }
    if not path.is_file():
        return result
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT r.status,r.model FROM sensors s JOIN runs r ON r.run_id=s.run_id"
        ).fetchall()
        counts["total"] = len(rows)
        for row in rows:
            counts[row["status"]] += 1
        counts["by_model"] = dict(Counter(r["model"] for r in rows if r["status"] == "simulated"))
        metadata = {
            row["key"]: json.loads(row["value"]) for row in conn.execute("SELECT * FROM metadata")
        }
        history = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    return {
        **result,
        "status": "available",
        "counts": counts,
        "history_count": history,
        "provenance": metadata,
        "db_sha256": _hash(path),
    }


def sensor_simulation_db_get(
    sensor_id: str,
    *,
    root: str | Path | None = None,
    load_arrays: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Read a report/ID's current or pinned run and verify artifact hashes.

    Device-name searches must first choose a report: separate reports are never
    silently merged. ``load_arrays`` reads numeric NPZ data with pickle disabled.
    """
    return _read_result(sensor_simulation_db_path(root), sensor_id, load_arrays, run_id)


def _read_result(db: Path, sensor_id: str, load_arrays: bool, run_id: str | None) -> dict[str, Any]:
    with _connect(db) as conn:
        if run_id is None:
            sql = "SELECT r.* FROM sensors s JOIN runs r ON r.run_id=s.run_id WHERE "
            tail, values = "", []
        else:
            sql = "SELECT r.* FROM runs r WHERE "
            tail, values = " AND r.run_id=?", [run_id]
        row = conn.execute(
            sql + "(lower(r.sensor_id)=lower(?) OR lower(r.report_code)=lower(?))" + tail,
            [sensor_id, sensor_id, *values],
        ).fetchone()
    if row is None:
        raise KeyError(f"No stored sensor simulation for {sensor_id!r} / {run_id or 'current'}.")
    result_path = _resolve_artifact(db, row["result_path"])
    if _hash(result_path) != row["result_sha256"]:
        raise ValueError(f"Sensor simulation result hash mismatch: {row['report_code']}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result["metadata"]["report_code"] != row["report_code"]:
        raise ValueError("Stored sensor simulation identity mismatch.")
    result["db_entry"] = _summary_row(row, db)
    fingerprint = result["input_digest"]
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Invalid simulation input fingerprint.")
    manifest_path = db.parent / "inputs" / f"{fingerprint}.json"
    if not manifest_path.is_file():
        raise ValueError("Stored simulation input manifest is unavailable.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if _digest(manifest) != fingerprint:
        raise ValueError("Stored simulation input manifest hash mismatch.")
    result["input_manifest"] = manifest
    if row["arrays_path"]:
        path = _resolve_artifact(db, row["arrays_path"])
        if _hash(path) != row["arrays_sha256"]:
            raise ValueError(f"Sensor simulation array hash mismatch: {row['report_code']}")
        if load_arrays:
            with np.load(path, allow_pickle=False) as archive:
                result["arrays"] = {key: archive[key].copy() for key in archive.files}
    elif load_arrays:
        result["arrays"] = {}
    return result


def sensor_simulation_db_config(
    sensor_id: str,
    *,
    root: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Return a CameraE2E configuration bound to the stored optical/readout snapshot."""
    stored = sensor_simulation_db_get(sensor_id, root=root, run_id=run_id)
    row, metadata = stored["db_entry"], stored["metadata"]
    if row["status"] != "simulated":
        raise ValueError(f"{row['report_code']} is {row['status']}: {row['reason']}")
    config = copy.deepcopy(metadata["config"])
    descriptor = {
        "kind": row["model"],
        "sensor_id": row["sensor_id"],
        "simulation_db": {
            "db_path": str(sensor_simulation_db_path(root)),
            "run_id": row["run_id"],
            "report_code": row["report_code"],
            "result_sha256": row["result_sha256"],
        },
    }
    config["scenario"]["sensor_model"] = descriptor
    # The optics/readout snapshot is self-contained; source DB authoring paths
    # remain provenance, not runtime dependencies.
    binding = config["scenario"].get("image_sensor_db", {}).get("optical_response_binding")
    if isinstance(binding, dict):
        binding["descriptor_identity"] = copy.deepcopy(descriptor)
    config["simulation_db"] = row
    return config


def _load_runtime_snapshot(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    binding = descriptor["simulation_db"]
    if set(descriptor) != {"kind", "sensor_id", "simulation_db"}:
        raise ValueError(
            "Stored sensor optical parameters are frozen; "
            "use scenario readout/illumination overrides."
        )
    # Read a specific absolute DB path without modifying environment variables.
    db = Path(binding["db_path"]).expanduser().resolve()
    stored = _get_at_path(db, binding["report_code"], binding["run_id"])
    row, metadata = stored["db_entry"], stored["metadata"]
    if (
        row["status"] != "simulated"
        or row["sensor_id"] != descriptor["sensor_id"]
        or row["model"] != descriptor["kind"]
        or row["result_sha256"] != binding["result_sha256"]
    ):
        raise ValueError("Stored CameraE2E simulation binding changed or is not usable.")
    package = Path(__file__).parent
    runtime_names = {
        "sensor.py",
        "physical_estimate.py",
        "sensor_estimate.py",
        "sensor_capture.py",
        "sensor_readout_profiles.py",
        "cfa_materials.py",
        "fdtd_estimate.py",
        "ip.py",
    }
    code_hashes = {
        Path(path).name: value
        for path, value in stored["input_manifest"]["files"].items()
        if Path(path).name in runtime_names and Path(path).parent.name == "pyisetcam"
    }
    code_hashes.update(metadata.get("implementation_sha256", {}))
    for name, expected in code_hashes.items():
        if Path(name).name != name:
            raise ValueError("Invalid stored implementation filename.")
        if _hash(package / name) != expected:
            raise ValueError(
                f"Stored sensor implementation changed ({name}); "
                "rebuild the simulation DB before running this saved model."
            )
    return metadata


def _get_at_path(db: Path, sensor_id: str, run_id: str) -> dict[str, Any]:
    return _read_result(db, sensor_id, False, run_id)


def _input_manifest(catalog_root: Any, settings: Mapping[str, Any]) -> dict[str, Any]:
    import importlib.metadata
    import platform

    from .assets import DEFAULT_UPSTREAM_SHA, AssetStore
    from .image_sensor_db import image_sensor_db_catalog_path, image_sensor_db_root
    from .sensor_estimate import _enriched_context

    selected = image_sensor_db_root(catalog_root).resolve()
    enriched, _ = _enriched_context(catalog_root)
    paths = {image_sensor_db_catalog_path(selected), enriched / "sensor_catalog.json"}
    for directory in (enriched / "optical_qe_db/models", enriched / "generated_stack_configs"):
        paths.update(directory.glob("*.json"))
    package = Path(__file__).parent
    # Conservative closure: camera/scene/optics, pupil helpers, spectral and
    # capture code can all affect outputs. Hashing only the top-level runner
    # would incorrectly reuse results after one of these dependencies changed.
    paths.update(package.rglob("*.py"))
    paths.update((package / "data/sensor_estimate").glob("*.json"))
    upstream = AssetStore.default().snapshot_root.resolve()
    for directory in ("lights", "human", "surfaces", "sensor", "optics"):
        paths.update((upstream / "data" / directory).rglob("*.mat"))
    return {
        "schema": _SCHEMA,
        "settings": dict(settings),
        "source_catalog_root": str(selected),
        "files": {str(path): _hash(path) for path in sorted(paths)},
        "upstream_asset_root": str(upstream),
        "upstream_version": DEFAULT_UPSTREAM_SHA,
        "runtime": {
            "python": platform.python_version(),
            "packages": {
                name: importlib.metadata.version(name)
                for name in ("numpy", "scipy", "scikit-image")
            },
        },
        "truth_boundary": "numerically_checked_estimates_not_measured_product_response",
    }


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        name = stream.name
        stream.write(data)
    os.replace(name, path)


def _store_result(
    conn: sqlite3.Connection,
    db: Path,
    record: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    run_id: str,
    input_digest: str,
) -> None:
    metadata = copy.deepcopy(dict(result["metadata"]))
    status = metadata["status"]
    if status not in _STATUSES:
        raise ValueError(f"Unsupported sensor simulation status: {status}")
    code = str(record["code"])
    if metadata.get("report_code", code) != code:
        raise ValueError("Simulation result belongs to another report.")
    if status == "simulated":
        model = metadata["model_snapshot"]
        if (
            model["sensor_id"] != record["sensor_id"]
            or model["kind"] != metadata["model_kind"]
            or not metadata["checks"]["numerical_passed"]
        ):
            raise ValueError("Simulated result lacks a matching model and passed checks.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        raise ValueError("Report code must be safe for artifact naming.")
    metadata.update(
        report_code=code,
        sensor_id=record["sensor_id"],
        device_name=record["device_name"],
        manufacturer=record["manufacturer"],
    )
    arrays = dict(result.get("arrays", {}))
    for name, array in arrays.items():
        value = np.asarray(array)
        if value.dtype.hasobject or not np.issubdtype(value.dtype, np.number):
            raise ValueError(f"Simulation array {name} must be numeric, not pickled objects.")
        if not np.all(np.isfinite(value)):
            raise ValueError(f"Simulation array {name} contains nonfinite values.")
    relative_dir = Path("artifacts") / code / run_id
    folder = db.parent / relative_dir
    folder.mkdir(parents=True, exist_ok=True)
    array_path, array_hash = None, None
    if arrays:
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".npz", delete=False) as stream:
            temp_name = stream.name
            np.savez_compressed(stream, **arrays)
        os.replace(temp_name, folder / "arrays.npz")
        array_path = str(relative_dir / "arrays.npz")
        array_hash = _hash(folder / "arrays.npz")
    document = {
        "schema": _SCHEMA,
        "run_id": run_id,
        "input_digest": input_digest,
        "metadata": metadata,
        "array_manifest": {
            name: {"shape": list(np.asarray(value).shape), "dtype": str(np.asarray(value).dtype)}
            for name, value in arrays.items()
        },
        "arrays_sha256": array_hash,
    }
    result_path = relative_dir / "result.json"
    _atomic_write(db.parent / result_path, (_json(document) + "\n").encode())
    reason = metadata.get("reason")
    summary = {
        "estimate_only": True,
        "measured_accuracy_verified": False,
        "checks": metadata.get("checks", {}),
        "capabilities": metadata.get(
            "capabilities",
            {
                "spectral_response": bool(arrays),
                "angular_response": metadata.get("optical_capabilities", {}).get(
                    "angular_response_supported", False
                ),
                "pupil_response": metadata.get("optical_capabilities", {}).get(
                    "pupil_response_supported", False
                ),
                "raw_capture": bool(metadata.get("cases")),
                "isp": metadata.get("isp", {}).get("available", False),
            },
        ),
        "provenance": {
            "input_digest": input_digest,
            "source": "CameraE2E sensor simulation with saved model snapshot",
        },
    }
    values = (
        run_id,
        code,
        record["sensor_id"],
        record["device_name"],
        record["manufacturer"],
        metadata.get("model_kind"),
        status,
        reason,
        _now(),
        _digest(metadata.get("config", {"settings": metadata.get("simulation_settings")})),
        str(result_path),
        _hash(db.parent / result_path),
        array_path,
        array_hash,
        _json(summary),
    )
    with conn:
        conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        conn.execute(
            "INSERT INTO sensors VALUES (?,?) ON CONFLICT(report_code) "
            "DO UPDATE SET run_id=excluded.run_id",
            (code, run_id),
        )


def build_sensor_simulation_db(
    *,
    root: str | Path | None = None,
    catalog_root: str | Path | None = None,
    sensor_ids: Sequence[str] | None = None,
    settings: Mapping[str, Any] | None = None,
    force: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Simulate all selected catalog reports, resume identical runs, save exclusions.

    A new input/code fingerprint creates new history; previous artifacts remain
    intact. ``force`` creates a fresh run even if inputs match. Each result is
    committed separately, so an interrupted campaign can resume.
    """
    from .image_sensor_db import (
        image_sensor_db_config,
        image_sensor_db_get,
        image_sensor_db_records,
    )
    from .sensor_simulation_batch import resolved_simulation_settings, simulate_sensor_record
    from .sensor_simulation_policy import sensor_simulation_policy

    settings = resolved_simulation_settings(settings)
    records = (
        [image_sensor_db_get(key, root=catalog_root) for key in sensor_ids]
        if sensor_ids is not None
        else image_sensor_db_records(root=catalog_root)
    )
    if len({r["code"] for r in records}) != len(records):
        raise ValueError("Select each sensor report only once.")
    manifest = _input_manifest(catalog_root, settings)
    fingerprint = _digest(manifest)
    db = sensor_simulation_db_path(root)
    counters = Counter()
    conn = _connect(db, write=True)
    try:
        old = conn.execute("SELECT value FROM metadata WHERE key='last_input_manifest'").fetchone()
        # Preserve the previous build's manifest when upgrading an earlier DB.
        manifests = [manifest] if old is None else [json.loads(old[0]), manifest]
        for item in manifests:
            _atomic_write(
                db.parent / "inputs" / f"{_digest(item)}.json", (_json(item) + "\n").encode()
            )
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("last_input_manifest", _json(manifest)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("last_build_started", _json(_now())),
            )
        for index, record in enumerate(records):
            run_key = _digest(
                {"input": fingerprint, "report": record["code"], "force": _now() if force else None}
            )
            existing = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_key,)).fetchone()
            if existing is not None:
                # Corruption is an error, not a reason to silently reuse or overwrite.
                sensor_simulation_db_get(record["code"], root=root, run_id=run_key)
                with conn:
                    conn.execute(
                        "INSERT INTO sensors VALUES (?,?) ON CONFLICT(report_code) "
                        "DO UPDATE SET run_id=excluded.run_id",
                        (record["code"], run_key),
                    )
                status = existing["status"]
                counters["reused"] += 1
            else:
                policy = sensor_simulation_policy(record["code"])
                try:
                    if policy["action"] == "exclude":
                        raise ValueError(policy["reason"])
                    config = image_sensor_db_config(
                        record["code"],
                        root=catalog_root,
                        model=policy.get("model_override", "auto"),
                    )
                except (ValueError, FileNotFoundError) as exc:
                    result = {
                        "metadata": {
                            "status": "excluded",
                            "reason": str(exc),
                            "source_scope_policy": policy,
                            "simulation_settings": settings,
                        },
                        "arrays": {},
                    }
                else:
                    try:
                        result = simulate_sensor_record(
                            record["code"], root=catalog_root, settings=settings
                        )
                        result["metadata"].setdefault("config", config)
                        if not result["metadata"]["checks"]["numerical_passed"]:
                            result["metadata"]["status"] = "quarantined"
                            result["metadata"].setdefault(
                                "reason", "Numerical simulation checks failed."
                            )
                    except Exception as exc:
                        result = {
                            "metadata": {
                                "status": "failed",
                                "reason": f"{type(exc).__name__}: {exc}",
                                "config": config,
                                "model_kind": config["model"],
                                "simulation_settings": settings,
                            },
                            "arrays": {},
                        }
                _store_result(conn, db, record, result, run_id=run_key, input_digest=fingerprint)
                status = result["metadata"]["status"]
                counters["computed"] += 1
            counters[status] += 1
            if progress is not None:
                progress(
                    {
                        "index": index + 1,
                        "total": len(records),
                        "report_code": record["code"],
                        "device_name": record["device_name"],
                        "status": status,
                        "reused": existing is not None,
                    }
                )
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("last_build_finished", _json(_now())),
            )
            conn.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("last_build_counts", _json(dict(counters))),
            )
    finally:
        conn.close()
    return {**sensor_simulation_db_summary(root=root), "build_counts": dict(counters)}


def sensor_simulation_db_validate(*, root: str | Path | None = None) -> dict[str, Any]:
    """Read every current artifact and verify identity, hashes and numeric arrays."""
    issues = []
    records = sensor_simulation_db_search(root=root)
    if not records:
        issues.append({"report_code": None, "error": "No simulation records available."})
    for row in records:
        try:
            result = sensor_simulation_db_get(row["report_code"], root=root, load_arrays=True)
            for key, array in result["arrays"].items():
                if not np.all(np.isfinite(array)):
                    raise ValueError(f"Nonfinite result array: {key}")
            if row["status"] == "simulated":
                if not result["arrays"] or not result["metadata"]["checks"]["numerical_passed"]:
                    raise ValueError("Simulated status lacks arrays or passed numerical checks.")
                if result["metadata"]["model_snapshot"]["kind"] != row["model"]:
                    raise ValueError("Stored model kind mismatch.")
        except (ValueError, KeyError, OSError) as exc:
            issues.append({"report_code": row["report_code"], "error": str(exc)})
    return {
        "schema": _SCHEMA,
        "status": "passed" if not issues else "failed",
        "records_checked": len(records),
        "issues": issues,
        "measured_accuracy_verified": False,
    }
