"""Verify saved Bayer, Quad and monochrome sensor reuse without source DB access."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sqlite3
import sys
import traceback
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from pyisetcam import camera_get, camerae2e_run_scenario
from pyisetcam.sensor_simulation_db import (
    sensor_simulation_db_config,
    sensor_simulation_db_get,
    sensor_simulation_db_path,
)

_SENSORS = ("DEF-2206-805", "DEF-2403-801", "DEF-1902-803", "DEP-1902-801")
_BLOCKED_FUNCTIONS = {
    "image_sensor_db": (
        "image_sensor_db_root", "image_sensor_db_catalog_path", "image_sensor_db_load",
        "image_sensor_db_records", "image_sensor_db_get", "image_sensor_db_config",
    ),
    "sensor_estimate": ("image_sensor_db_model", "_enriched_context"),
    "physical_estimate": ("image_sensor_physical_model",),
    "cfa_materials": ("image_sensor_cfa_materials",),
    "sensor_techinsights": ("sensor_techinsights_evidence",),
    "sensor_readout_profiles": ("resolve_sensor_readout",),
    "sensor_simulation_batch": ("simulate_sensor_record",),
    "sensor_simulation_db": ("build_sensor_simulation_db", "_input_manifest"),
}


def _file_identity(path: Path) -> dict:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    stat = path.stat()
    return {"sha256": digest, "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _source_guards(stack: ExitStack) -> tuple[dict[str, Mock], list[str]]:
    originals, guards = {}, {}
    for suffix, names in _BLOCKED_FUNCTIONS.items():
        module = importlib.import_module(f"pyisetcam.{suffix}")
        for name in names:
            function = getattr(module, name)
            label = f"{module.__name__}.{name}"
            guard = Mock(side_effect=AssertionError(f"Source recomputation forbidden: {label}"))
            originals[id(function)] = guard
            guards[label] = guard
    # Guard imported aliases as well as the defining module. Replacing objects
    # in memory leaves all implementation-file hashes unchanged.
    aliases = []
    for module_name, module in tuple(sys.modules.items()):
        if module_name != "pyisetcam" and not module_name.startswith("pyisetcam."):
            continue
        if module is None:
            continue
        for name, value in tuple(vars(module).items()):
            if id(value) in originals:
                stack.enter_context(patch.object(module, name, originals[id(value)]))
                aliases.append(f"{module_name}.{name}")
    return guards, sorted(aliases)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="CameraE2E DB root; default active DB")
    parser.add_argument(
        "--output", type=Path,
        default=Path("reports/sensor_simulation_db/runtime_reuse.json"),
    )
    args = parser.parse_args()
    db = sensor_simulation_db_path(args.root)
    before = _file_identity(db)
    report = {
        "schema": "camerae2e_sensor_simulation_runtime_reuse_v1",
        "created_at": datetime.now(UTC).isoformat(),
        "script_sha256": _file_identity(Path(__file__))["sha256"],
        "db_path": str(db),
        "db_before": before,
        "scene": {"type": "uniform ee", "size": [8, 8]},
        "capture": {"integration_time_s": 0.004, "noise_flag": 2, "seed": 7},
        "measured_accuracy_verified": False,
        "cases": [],
    }
    connections = []
    connect = sqlite3.connect

    def readonly_connect(database, *values, **options):
        if not options.get("uri") or "?mode=ro" not in str(database):
            raise AssertionError("Runtime verification permits only read-only SQLite connections")
        connections.append(str(database))
        return connect(database, *values, **options)

    with ExitStack() as stack:
        guards, aliases = _source_guards(stack)
        stack.enter_context(patch.object(sqlite3, "connect", readonly_connect))
        for code in _SENSORS:
            case = {"report_code": code, "status": "failed"}
            try:
                config = sensor_simulation_db_config(code, root=args.root)
                binding = config["scenario"]["sensor_model"]["simulation_db"]
                stored = sensor_simulation_db_get(code, root=args.root, run_id=binding["run_id"])
                native = stored["metadata"]["native_cfa"]
                result = camerae2e_run_scenario(
                    {
                        **config["scenario"],
                        "scene": {"type": "uniform ee", "args": [8]},
                        "sensor": {
                            "size": [8, 8], "integration_time": 0.004, "noise_flag": 2,
                        },
                        "sensor_resize": False,
                    },
                    seed=7,
                )
                sensor = camera_get(result["camera"], "sensor")
                raw = sensor.data["raw_dn"]
                pattern = np.asarray(sensor.fields["pattern"])
                expected_pattern = np.asarray(native["pattern"])
                actual_binding = sensor.metadata["sensor_simulation_db"]
                checks = {
                    "raw_int32": raw.dtype == np.dtype("int32"),
                    "raw_shape_8x8": raw.shape == (8, 8),
                    "native_pattern_preserved": np.array_equal(pattern, expected_pattern),
                    "native_filter_names_preserved": (
                        list(sensor.fields["filter_names"]) == list(native["filter_names"])
                    ),
                    "run_id_preserved": actual_binding["run_id"] == binding["run_id"],
                    "binding_preserved": actual_binding == binding,
                    "finite_nonnegative_photoelectrons": bool(
                        np.all(np.isfinite(sensor.data["mean_electrons"]))
                        and np.all(sensor.data["mean_electrons"] >= 0)
                    ),
                }
                case.update(
                    status="passed" if all(checks.values()) else "failed",
                    sensor_id=stored["db_entry"]["sensor_id"],
                    device_name=stored["db_entry"]["device_name"],
                    model=stored["db_entry"]["model"],
                    run_id=binding["run_id"],
                    result_sha256=binding["result_sha256"],
                    config_sha256=stored["db_entry"]["config_sha256"],
                    raw_dtype=str(raw.dtype), raw_shape=list(raw.shape),
                    raw_range_dn=[int(raw.min()), int(raw.max())],
                    native_pattern=pattern.tolist(),
                    filter_names=list(sensor.fields["filter_names"]),
                    native_pattern_index_base=native["pattern_index_base"],
                    saved_cfa_pattern=stored["metadata"]["model_snapshot"].get("cfa_pattern"),
                    checks=checks,
                )
            except Exception as exc:
                case.update(error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
            report["cases"].append(case)
        report["blocked_source_functions"] = sorted(guards)
        report["blocked_aliases"] = aliases
        report["source_calls"] = {label: guard.call_count for label, guard in guards.items()}
    report["db_after"] = _file_identity(db)
    report["db_unchanged"] = report["db_after"] == before
    report["readonly_sqlite_connection_count"] = len(connections)
    report["source_recomputation_blocked"] = not any(report["source_calls"].values())
    report["status"] = "passed" if (
        report["db_unchanged"] and report["source_recomputation_blocked"]
        and all(case["status"] == "passed" for case in report["cases"])
    ) else "failed"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "status": report["status"], "output": str(args.output),
        "db_unchanged": report["db_unchanged"],
        "source_recomputation_blocked": report["source_recomputation_blocked"],
        "cases": [
            {key: case.get(key) for key in ("report_code", "status", "model", "error")}
            for case in report["cases"]
        ],
    }, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
