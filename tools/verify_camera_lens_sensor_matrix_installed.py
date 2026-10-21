#!/usr/bin/env python3
"""Run a 2x2 Lens/Sensor RAW matrix from an installed wheel and child Worker.

The wheel must already be installed into --installed-root. Run this tool with
PYTHONPATH set to that root and an external workspace/state directory. It does
not import the development src tree, rank products, or validate lens radiometry.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

import numpy as np

LENSES = (
    "4daeca8f-5d81-57b0-8ae3-928f23de2b36",
    "444be61b-3bbb-51f9-938a-c4823e702d13",
)
SENSORS = ("DEF-2512-801", "DEF-2412-804")
CONDITIONS = {
    "scene_kind": "slanted_edge",
    "sensor_size": [8, 8],
    "luminance_cd_m2": 1000.0,
    "exposure_s": 0.004,
    "noise_flag": 0,
    "seed": 7,
}
SCHEMA = "camerae2e_installed_lens_sensor_raw_matrix_v1"


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _wheel_package_identity(wheel: Path, installed_root: Path) -> dict:
    """Match every installed CameraE2E package file to the exact wheel bytes."""
    if not wheel.is_file() or wheel.suffix != ".whl" or wheel.is_symlink():
        raise ValueError("--wheel must be a regular wheel file.")
    if not installed_root.is_dir() or installed_root.is_symlink():
        raise ValueError("--installed-root must be a separate package directory.")
    expected: dict[str, str] = {}
    with ZipFile(wheel) as archive:
        for member in archive.infolist():
            name = member.filename
            if member.is_dir() or not name.startswith(("camera_platform/", "pyisetcam/")):
                continue
            expected[name] = _sha(archive.read(member))
    if not expected or "camera_platform/__init__.py" not in expected:
        raise ValueError("Wheel has no complete CameraE2E package payload.")
    for name, checksum in expected.items():
        path = installed_root / name
        if not path.is_file() or path.is_symlink() or _sha(path.read_bytes()) != checksum:
            raise ValueError(f"Installed package differs from the wheel: {name}")
    extras = {
        path.relative_to(installed_root).as_posix()
        for package in ("camera_platform", "pyisetcam")
        for path in (installed_root / package).rglob("*.py")
    } - set(expected)
    if extras:
        raise ValueError(f"Installed package has extra Python source: {sorted(extras)[:4]}")
    return {
        "wheel_sha256": _sha(wheel.read_bytes()),
        "wheel_path": str(wheel),
        "installed_root": str(installed_root),
        "package_file_count": len(expected),
        "package_bytes_match_wheel": True,
    }


def _download(transport, descriptor: dict) -> bytes:
    response = transport.get(descriptor["url"])
    response.raise_for_status()
    content = response.content
    if (
        _sha(content) != descriptor.get("sha256")
        or len(content) != descriptor.get("bytes")
    ):
        raise RuntimeError("Registry artifact differs from its SHA-256/size descriptor.")
    return content


def _difference(first: np.ndarray, second: np.ndarray) -> dict:
    if first.shape != second.shape or first.dtype != second.dtype:
        raise RuntimeError("Cannot compare RAW arrays with different shape or dtype.")
    delta = np.abs(first.astype(np.int64) - second.astype(np.int64))
    return {
        "byte_equal": bool(first.tobytes(order="C") == second.tobytes(order="C")),
        "changed_pixel_count": int(np.count_nonzero(delta)),
        "mean_absolute_dn_difference": float(np.mean(delta)),
        "maximum_absolute_dn_difference": int(np.max(delta)),
        "interpretation": "Difference only; no image-quality or product ranking.",
    }


def _validate_paths(args) -> tuple[Path, Path, Path, Path, Path]:
    wheel = args.wheel.expanduser().absolute()
    installed = args.installed_root.expanduser().absolute()
    workspace = args.workspace.expanduser().absolute()
    state = args.state_dir.expanduser().absolute()
    output = args.output.expanduser().absolute()
    checkout = Path(__file__).resolve().parents[1]
    if any(
        path == checkout or path.is_relative_to(checkout)
        for path in (installed, workspace, state)
    ):
        raise ValueError("Installed package, workspace and state must be outside this checkout.")
    if not (workspace / "camerae2e_db/catalog.sqlite").is_file():
        raise ValueError("External workspace has no CameraE2E DB catalog.")
    if state.exists() or state.is_symlink():
        raise ValueError("--state-dir must be a new path; cached jobs cannot prove a fresh Worker.")
    if output.exists() or output.is_symlink():
        raise ValueError("--output already exists; prior evidence cannot be overwritten.")
    if installed == workspace or installed == state or workspace == state:
        raise ValueError("Installed package, workspace and state must be separate paths.")
    return wheel, installed, workspace, state, output


def _new_report(workspace: Path, state: Path) -> dict:
    return {
        "schema": SCHEMA,
        "status": "incomplete",
        "created_at": datetime.now(UTC).isoformat(),
        "workspace": str(workspace),
        "state_dir": str(state),
        "conditions": CONDITIONS,
        "condition_sha256": _sha(_canonical(CONDITIONS)),
        "lens_revision_ids": list(LENSES),
        "sensor_report_codes": list(SENSORS),
        "cases": [],
        "comparisons": {},
        "limits": {
            "lens_transmission": "unknown_unity_assumption",
            "relative_illumination": "unknown_unity_assumption",
            "physical_validation": "not_established",
            "product_ranking_claimed": False,
        },
    }


def verify(
    wheel: Path,
    installed_root: Path,
    workspace: Path,
    state: Path,
    output: Path,
    report: dict,
) -> dict:
    from fastapi.testclient import TestClient

    import camera_platform
    import pyisetcam
    from camera_platform.api import create_app
    from camera_platform.lens_catalog_bridge import geometric_lens_preview
    from camera_platform.sdk import PlatformClient
    from camera_platform.sensor_db_bridge import inspect_sensor_revision
    from pyisetcam.dataset import _sha256_array

    package = _wheel_package_identity(wheel, installed_root)
    for module in (camera_platform, pyisetcam):
        if not Path(module.__file__).resolve().is_relative_to(installed_root):
            raise RuntimeError(f"Development source leaked into installed run: {module.__file__}")
    package["module_paths"] = {
        "camera_platform": str(Path(camera_platform.__file__).resolve()),
        "pyisetcam": str(Path(pyisetcam.__file__).resolve()),
    }
    package["version"] = camera_platform.__version__
    package["python_executable"] = sys.executable
    report["package"] = package
    state.mkdir(parents=True)
    app = create_app(state, workspace, start_worker=True, seed_demo=False)
    evidence_dir = output.parent / f"{output.stem}-artifacts"
    if evidence_dir.exists() or evidence_dir.is_symlink():
        raise ValueError("Evidence artifact directory already exists.")
    evidence_dir.mkdir(parents=True)
    raw_arrays: dict[tuple[int, int], np.ndarray] = {}
    with TestClient(app) as transport:
        worker = app.state.worker
        if worker is None or worker.poll() is not None or worker.pid == os.getpid():
            raise RuntimeError("A live, separate installed Worker process is required.")
        command = worker.args
        if (
            not isinstance(command, (list, tuple))
            or len(command) < 3
            or command[:3] != [sys.executable, "-m", "camera_platform.worker"]
        ):
            raise RuntimeError("Worker was not launched from the installed module.")
        report["worker"] = {
            "child_pid": worker.pid,
            "command": command,
            "separate_process": True,
            "execution_evidence": (
                "Jobs are submitted through API; this process never calls execute."
            ),
        }
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()[
            "token"
        ]
        template = client.lens_sensor_raw_template()
        available_lenses = {
            row["revision_id"]: row for row in template["lens_catalog"]["revisions"]
        }
        available_sensors = {row["report_code"]: row for row in template["sensor_catalog"]}
        lens_identity = {}
        for lens_id in LENSES:
            row = available_lenses.get(lens_id)
            if row is None or row["preview_status"] != "ready":
                raise RuntimeError(f"Requested Lens revision is not ready: {lens_id}")
            lens_identity[lens_id] = geometric_lens_preview(
                lens_id, catalog_root=workspace / "camerae2e_db"
            )["identity"]
        sensor_identity = {}
        for code in SENSORS:
            row = available_sensors.get(code)
            if row is None or row["status"] != "simulated" or row["model"] != "db_estimate_v1":
                raise RuntimeError(f"Requested saved Sensor revision is unavailable: {code}")
            inspected = inspect_sensor_revision(
                code, root=workspace / "camerae2e_db", run_id=row["run_id"]
            )
            cfa = inspected.get("native_response", {}).get("native_cfa") or {}
            if (
                not inspected["executable"]
                or cfa.get("pattern") != [[1, 2], [2, 3]]
                or cfa.get("filter_names") != ["red", "green", "blue"]
            ):
                raise RuntimeError(f"Requested Sensor is not executable native 2x2 RGGB: {code}")
            sensor_identity[code] = inspected["revision"]
        report["selected_sources"] = {
            "lenses": lens_identity,
            "sensors": sensor_identity,
        }
        for lens_index, lens_id in enumerate(LENSES):
            for sensor_index, code in enumerate(SENSORS):
                key = f"L{lens_index + 1}S{sensor_index + 1}"
                spec = dict(template["specification"])
                spec.update(
                    name=f"installed 2x2 RAW {key}",
                    lens_revision_id=lens_id,
                    sensor_revision={"identifier": code, "run_id": sensor_identity[code]["run_id"]},
                    **CONDITIONS,
                    accept_approximate_lens_radiometry=True,
                )
                queued = client.submit_lens_sensor_raw(spec)
                finished = client.wait(queued["id"], timeout=600, expected_kind="lens_sensor_raw")
                if finished["status"] != "succeeded":
                    raise RuntimeError(f"{key} Lens RAW job failed: {finished.get('error')}")
                summary = finished["result"]["summary"]
                preview = summary["preview"]
                descriptor = preview["raw_artifact"]
                artifacts = {item["name"]: item for item in finished["artifacts"]}
                result_descriptor = artifacts.get("lens-sensor-raw-preview-result.json")
                if (
                    len(finished["artifacts"]) != 2
                    or set(artifacts) != {
                        "lens-sensor-original-raw.npy",
                        "lens-sensor-raw-preview-result.json",
                    }
                    or artifacts.get("lens-sensor-original-raw.npy") != descriptor
                    or result_descriptor is None
                    or summary["specification"] != spec
                    or summary["source_identities"] != {
                        "lens": lens_identity[lens_id], "sensor": sensor_identity[code]
                    }
                    or preview["lens_identity"] != lens_identity[lens_id]
                    or preview["sensor_revision"] != sensor_identity[code]
                    or preview["scene"]["kind"] != "slanted_edge_d65"
                    or preview["capture"] != {
                        **{
                            k: CONDITIONS[k]
                            for k in ("exposure_s", "noise_flag", "seed", "sensor_size")
                        },
                        "saturation": preview["capture"].get("saturation", {}),
                    }
                    or preview["physical_validation"] != "not_established"
                ):
                    raise RuntimeError(f"{key} result lost its pinned source or common conditions.")
                raw_bytes = _download(transport, descriptor)
                result_bytes = _download(transport, result_descriptor)
                if result_bytes != _canonical(summary):
                    raise RuntimeError(f"{key} result JSON differs from the completed job summary.")
                raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
                if (
                    raw.dtype != np.dtype("int32")
                    or raw.shape != (8, 8)
                    or raw.tolist() != preview["raw"]["values"]
                ):
                    raise RuntimeError(f"{key} original integer RAW differs from the result.")
                saved_raw = evidence_dir / f"{key}-original-raw.npy"
                saved_result = evidence_dir / f"{key}-result.json"
                saved_raw.write_bytes(raw_bytes)
                saved_result.write_bytes(result_bytes)
                raw_arrays[(lens_index, sensor_index)] = raw.copy()
                report["cases"].append({
                    "key": key,
                    "job_id": finished["id"],
                    "status": finished["status"],
                    "lens_revision_id": lens_id,
                    "sensor_revision": sensor_identity[code],
                    "source_identities": summary["source_identities"],
                    "condition_sha256": report["condition_sha256"],
                    "upstream_assets": summary["upstream_assets"],
                    "raw_artifact": descriptor,
                    "raw_content_sha256": _sha256_array(raw),
                    "raw_shape": list(raw.shape),
                    "raw_dtype": str(raw.dtype),
                    "raw_min_dn": int(raw.min()),
                    "raw_max_dn": int(raw.max()),
                    "result_artifact": result_descriptor,
                    "preserved_original_raw": str(saved_raw),
                    "preserved_result_json": str(saved_result),
                })
        if worker.poll() is not None:
            raise RuntimeError("Installed Worker exited before completing all four jobs.")
    if len(report["cases"]) != 4 or len({case["job_id"] for case in report["cases"]}) != 4:
        raise RuntimeError("The matrix did not produce four independent saved RAW jobs.")
    if len({_canonical(case["upstream_assets"]) for case in report["cases"]}) != 1:
        raise RuntimeError("Upstream assets changed across the Lens/Sensor matrix.")
    comparisons = {}
    for sensor_index, code in enumerate(SENSORS):
        comparisons[f"lens_swap_at_{code}"] = _difference(
            raw_arrays[(0, sensor_index)], raw_arrays[(1, sensor_index)]
        )
    for lens_index, lens_id in enumerate(LENSES):
        comparisons[f"sensor_swap_at_{lens_id}"] = _difference(
            raw_arrays[(lens_index, 0)], raw_arrays[(lens_index, 1)]
        )
    report["comparisons"] = comparisons
    report["distinguishability"] = {
        "lens_swaps_changed": all(
            comparisons[f"lens_swap_at_{code}"]["changed_pixel_count"] > 0
            for code in SENSORS
        ),
        "sensor_swaps_changed": all(
            comparisons[f"sensor_swap_at_{lens_id}"]["changed_pixel_count"] > 0
            for lens_id in LENSES
        ),
        "claim": "numeric RAW sensitivity at these authored conditions only",
    }
    report["status"] = "completed"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--installed-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel, installed, workspace, state, output = _validate_paths(args)
    report = _new_report(workspace, state)
    try:
        verify(wheel, installed, workspace, state, output, report)
    except Exception as exc:
        report["status"] = "failed"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
    print(
        json.dumps(
            {"status": report["status"], "cases": len(report["cases"]), "output": str(output)},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
