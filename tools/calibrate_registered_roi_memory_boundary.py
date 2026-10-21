"""Isolated diagnostic of the registered 4x virtual Scene memory policy.

This tool never changes the server policy. It runs one fresh Registry in a
private child process and raises only that process's optical-tile admission
budget from 512 MiB to 1 GiB. The parent observes the child process tree when
the OS permits it, or the direct child alone in restricted macOS sandboxes,
and stops its process group after 900 s or 4 GiB observed RSS.

The monitor is not a hard OS memory limit: RSS can grow between samples. The
result is a numerical diagnostic, not full-frame or physical FOV validation.
Run --self-check before a real launch; a real launch requires fresh output paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
BASELINE_REPORT = (
    PROJECT
    / "reports/camera_platform/mtf-support-20260928/registered-roi-4x-preflight-only.json"
)
BASELINE_OPTICAL_BUDGET_BYTES = 512 * 1024**2
DIAGNOSTIC_OPTICAL_BUDGET_BYTES = 1024**3
OBSERVED_PROCESS_TREE_RSS_CUTOFF_BYTES = 4 * 1024**3
WALL_CUTOFF_SECONDS = 900.0
POLL_SECONDS = 0.25
PENDING_CONTRACT = "camerae2e_registered_roi_worker_preflight_submission_v2"


def _json_file(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _imports(expected_root: Path) -> tuple[Any, Any]:
    import camera_platform
    import pyisetcam

    for name, module in (("camera_platform", camera_platform), ("pyisetcam", pyisetcam)):
        location = Path(module.__file__).resolve(strict=True)
        if not location.is_relative_to(expected_root):
            raise RuntimeError(f"{name} imports outside the declared package root")
    return camera_platform, pyisetcam


def _diagnostic_policy() -> tuple[Any, Any]:
    from camera_platform.lens_sensor_out_of_core_raw_job import _resource_policy

    original = _resource_policy()
    if original.optical_tile.estimated_working_budget_bytes != BASELINE_OPTICAL_BUDGET_BYTES:
        raise RuntimeError("Production optical-tile policy changed; recalibrate explicitly")
    elevated = replace(
        original,
        optical_tile=replace(
            original.optical_tile,
            estimated_working_budget_bytes=DIAGNOSTIC_OPTICAL_BUDGET_BYTES,
        ),
    )
    return original, elevated


def _baseline() -> dict[str, Any]:
    data = BASELINE_REPORT.read_bytes()
    report = json.loads(data)
    if (
        report.get("roi_preflight_status") != "blocked_working_memory_policy"
        or report.get("analytic_scene_shape_row_col") != [8192, 56000]
        or report.get("optical_tile_working_policy_cap_bytes")
        != BASELINE_OPTICAL_BUDGET_BYTES
    ):
        raise RuntimeError("Prior 512 MiB preflight proof no longer matches the calibration")
    return {
        "path": str(BASELINE_REPORT),
        "sha256": hashlib.sha256(data).hexdigest(),
        "status": report["roi_preflight_status"],
        "combined_policy_estimate_bytes": report[
            "first_tile_combined_policy_estimate_bytes"
        ],
        "optical_tile_budget_bytes": BASELINE_OPTICAL_BUDGET_BYTES,
        "optical_or_sensor_calculation_executed": False,
    }


def _paths(
    state_dir: Path | None, report: Path | None, *, child: bool = False
) -> tuple[Path, Path, Path, Path]:
    if state_dir is None or report is None:
        raise ValueError("--state-dir and --report are required for a calibration run")
    state = state_dir.expanduser().resolve()
    output = report.expanduser().resolve()
    private_tmp = Path("/private/tmp").resolve(strict=True)
    if (
        not state.is_relative_to(private_tmp)
        or not state.name.startswith("camerae2e-roi-calibration-")
        or state == private_tmp
    ):
        raise ValueError(
            "Calibration state must be a fresh "
            "/private/tmp/camerae2e-roi-calibration-* directory"
        )
    checkpoint = output.with_name(output.stem + "-child.json")
    log_path = output.with_name(output.stem + "-child.log")
    protected = (state, output, checkpoint) if child else (state, output, checkpoint, log_path)
    if any(path.exists() or path.is_symlink() for path in protected):
        raise ValueError("Calibration state, report, checkpoint and log must all be new")
    if not state.parent.is_dir() or not output.parent.is_dir():
        raise ValueError("State and report parent directories must already exist")
    return state, output, checkpoint, log_path


def _build_spec(service: Any) -> dict[str, Any]:
    from verify_camera_lens_sensor_registered_roi_db import (
        BOUNDS,
        LENS_REVISION,
        SENSOR_CODE,
        SENSOR_RUN,
    )

    from camera_platform.lens_sensor_active_mode_asset import (
        register_active_mode_declaration,
    )
    from camera_platform.lens_sensor_fullfield_study_preflight import MODE_SCHEMA
    from camera_platform.lens_sensor_registered_roi_disk_raw_job import default_spec
    from camera_platform.sensor_db_bridge import inspect_sensor_revision

    inspection = inspect_sensor_revision(
        SENSOR_CODE, root=PROJECT / "camerae2e_db", run_id=SENSOR_RUN
    )
    if inspection.get("executable") is not True:
        raise RuntimeError("Pinned saved Sensor DB model is not executable")
    revision = inspection["revision"]
    mode = {
        "schema": MODE_SCHEMA,
        "mode_id": "engineering-2048x14000-zero-origin",
        "sensor_run_id": SENSOR_RUN,
        "sensor_report_code": revision["report_code"],
        "sensor_result_sha256": revision["result_sha256"],
        "active_rows": 2048,
        "active_cols": 14000,
        "pixel_pitch_um": 1.22,
        "sensor_center_offset_from_optical_axis_um_row_col": [0.0, 0.0],
        "sensor_plane": "perpendicular_to_optical_axis",
        "sensor_orientation_deg_clockwise": 0,
        "cfa_origin_row_col": [0, 0],
        "image_plane_binding": {
            "reference_plane": "saved_prescription_image_plane",
            "axial_offset_um": 0.0,
        },
    }
    declaration = register_active_mode_declaration(
        service.registry,
        SENSOR_CODE,
        sensor_run_id=SENSOR_RUN,
        active_mode=mode,
        source_kind="engineering_declaration",
        root=PROJECT / "camerae2e_db",
    )
    spec = default_spec()
    spec["lens_revision_id"] = LENS_REVISION
    spec["sensor_revision"] = {"identifier": SENSOR_CODE, "run_id": SENSOR_RUN}
    spec["active_mode_declaration_id"] = declaration["id"]
    spec["sensor_roi_bounds_row_col_exclusive"] = BOUNDS
    spec["sensor_tile_shape_row_col"] = [4, 8]
    spec["readout_row_chunk_rows"] = 4
    spec["analytic_scene"].update(
        shape_row_col=[8192, 56000],
        horizontal_fov_deg=42.0,
        edge_offset_deg=14.0,
    )
    spec["accept_approximate_lens_radiometry"] = True
    spec["acknowledge_unverified_mode_and_uniform_sensor_response"] = True
    return spec


def _arrays(service: Any, summary: dict[str, Any]) -> dict[str, Any]:
    import numpy as np

    result = {}
    for name in ("mean_electrons", "raw_dn", "reference_dn", "volts"):
        descriptor = summary["array_artifacts"][name]
        array = np.load(
            service.registry.blob_path(descriptor["sha256"]),
            mmap_mode="r",
            allow_pickle=False,
        )
        if array.shape != (8, 8):
            raise RuntimeError(f"{name} output shape differs from the declared 8x8 ROI")
        result[name] = {
            "shape_row_col": list(array.shape),
            "dtype": str(array.dtype),
            "min": float(np.min(array)),
            "max": float(np.max(array)),
            "sha256": descriptor["sha256"],
            "bytes": descriptor["bytes"],
        }
        del array
    return result


def _self_high_water_rss() -> dict[str, Any]:
    """Read the kernel-maintained process peak immediately before child exit."""
    raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        unit = "bytes"
        normalized = raw
    elif sys.platform.startswith("linux"):
        unit = "KiB"
        normalized = raw * 1024
    else:
        unit = "platform_defined"
        normalized = None
    return {
        "ru_maxrss_raw": raw,
        "ru_maxrss_raw_unit": unit,
        "ru_maxrss_bytes": normalized,
        "scope": "calibration_child_self_only",
        "source": "resource.getrusage(RUSAGE_SELF).ru_maxrss",
    }


def _child(state: Path, checkpoint: Path, expected_root: Path) -> int:
    platform, engine = _imports(expected_root)
    from camera_platform import lens_sensor_registered_roi_disk_raw_job as roi_job
    from camera_platform.service import PlatformService

    original, elevated = _diagnostic_policy()
    if roi_job.PENDING_SUBMISSION_SCHEMA != PENDING_CONTRACT:
        raise RuntimeError("Asynchronous Worker-preflight source is required")
    # The override exists only in this short-lived diagnostic process.
    roi_job._resource_policy = lambda: elevated
    started = time.monotonic()
    checkpoint_data: dict[str, Any] = {
        "schema": "camera_registered_roi_memory_calibration_child_v1",
        "status": "initializing",
        "state_directory": str(state),
        "platform_version": platform.__version__,
        "engine_version": engine.__version__,
        "platform_import_path": str(Path(platform.__file__).resolve(strict=True)),
        "engine_import_path": str(Path(engine.__file__).resolve(strict=True)),
        "production_policy": original.as_dict(),
        "diagnostic_process_local_policy": elevated.as_dict(),
        "override_applied_only_in_child_process": True,
        "worker_harness": "fresh_registry_direct_claim_execute_in_monitored_child",
        "physical_fov_qualified": False,
        "product_full_frame_qualified": False,
        "sampling_convergence": "not_assessed",
    }
    _json_file(checkpoint, checkpoint_data)
    service = PlatformService(state, PROJECT)
    spec = _build_spec(service)
    queued = roi_job.submit(service, spec)
    if queued.get("submission_preflight_status") != "worker_preflight_pending":
        raise RuntimeError("Submission unexpectedly ran model-dependent preflight")
    checkpoint_data.update(status="queued", job_id=queued["id"])
    _json_file(checkpoint, checkpoint_data)
    claimed = service.registry.claim()
    if claimed is None or claimed["id"] != queued["id"]:
        raise RuntimeError("Fresh calibration Registry did not claim its only job")
    checkpoint_data.update(status="running")
    _json_file(checkpoint, checkpoint_data)
    try:
        result = roi_job.execute(service, claimed)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        evidence_result = getattr(exc, "evidence_result", None)
        service.registry.finish(queued["id"], result=evidence_result, error=error)
    else:
        error = None
        service.registry.finish(queued["id"], result=result)
    finished = roi_job.get(service, queued["id"])
    checkpoint_data.update(
        status=finished["status"],
        job_error=finished.get("error"),
        child_elapsed_seconds=round(time.monotonic() - started, 3),
    )
    summary = (finished.get("result") or {}).get("summary")
    failure = (finished.get("result") or {}).get("preflight_failure")
    if summary is not None:
        checkpoint_data["resource_preflight"] = summary.get("resource_preflight")
        checkpoint_data["arrays"] = _arrays(service, summary)
        checkpoint_data["optical_execution_status"] = summary.get("status")
    if failure is not None:
        checkpoint_data["preflight_failure"] = failure
        checkpoint_data["optical_execution_status"] = "blocked_before_optical_execution"
    checkpoint_data["child_high_water_rss"] = _self_high_water_rss()
    _json_file(checkpoint, checkpoint_data)
    return 0 if finished["status"] == "succeeded" else 2


def _tree_rss(
    child: subprocess.Popen[bytes], *, include_descendants: bool
) -> tuple[int, int, bool]:
    import psutil

    try:
        root = psutil.Process(child.pid)
        processes = [root]
        if include_descendants:
            try:
                processes += root.children(recursive=True)
            except (PermissionError, psutil.AccessDenied):
                # macOS's process-table sysctl is unavailable in some
                # sandboxes. The owned child RSS remains queryable.
                include_descendants = False
    except psutil.NoSuchProcess:
        return 0, 0, include_descendants
    rss = 0
    counted = 0
    for process in processes:
        try:
            rss += process.memory_info().rss
            counted += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rss, counted, include_descendants


def _swap_used() -> tuple[int | None, str | None]:
    import psutil

    try:
        return int(psutil.swap_memory().used), None
    except (OSError, PermissionError, psutil.AccessDenied) as exc:
        return None, type(exc).__name__


def _stop_group(child: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=3)


def _reconcile_isolated_registry(
    state: Path, job_id: str | None, *, child_exit_code: int, cutoff: str | None
) -> tuple[str | None, str | None]:
    status = _registry_status(state, job_id)
    if status != "running" or job_id is None:
        return status, None
    from camera_platform.registry import SQLiteRegistry

    reason = (
        f"Calibration monitor stopped the child: {cutoff}"
        if cutoff is not None
        else f"Calibration child exited without publishing a result: code {child_exit_code}"
    )
    try:
        SQLiteRegistry(state).finish(job_id, error=reason)
    except Exception as exc:
        return _registry_status(state, job_id), f"{type(exc).__name__}: {exc}"
    return _registry_status(state, job_id), None


def _registry_status(state: Path, job_id: str | None) -> str | None:
    database = state / "registry.sqlite"
    if job_id is None or not database.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1.0) as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            return None if row is None else str(row[0])
    except sqlite3.Error:
        return None


def _run(state: Path, output: Path, checkpoint: Path, log_path: Path, expected_root: Path) -> int:
    import psutil

    platform, engine = _imports(expected_root)
    original, elevated = _diagnostic_policy()
    baseline = _baseline()
    physical_memory_bytes_at_start = psutil.virtual_memory().total
    available_start = psutil.virtual_memory().available
    minimum_available = available_start
    swap_start, swap_start_error = _swap_used()
    if roi_source_is_pending() is not True:
        raise RuntimeError("Wait for the asynchronous Worker-preflight source freeze")
    command = [
        sys.executable,
        str(Path(__file__).resolve(strict=True)),
        "--_child",
        "--state-dir", str(state),
        "--report", str(output),
        "--expected-import-root", str(expected_root),
    ]
    started = time.monotonic()
    highest_rss = 0
    highest_process_count = 0
    cutoff = None
    descendants_monitored = True
    with log_path.open("xb") as log:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        try:
            while child.poll() is None:
                rss, process_count, descendants_monitored = _tree_rss(
                    child, include_descendants=descendants_monitored
                )
                highest_rss = max(highest_rss, rss)
                highest_process_count = max(highest_process_count, process_count)
                minimum_available = min(
                    minimum_available, psutil.virtual_memory().available
                )
                elapsed = time.monotonic() - started
                if rss >= OBSERVED_PROCESS_TREE_RSS_CUTOFF_BYTES:
                    cutoff = "observed_process_tree_rss_cutoff"
                    _stop_group(child)
                    break
                if elapsed >= WALL_CUTOFF_SECONDS:
                    cutoff = "wall_clock_cutoff"
                    _stop_group(child)
                    break
                time.sleep(POLL_SECONDS)
        except BaseException:
            _stop_group(child)
            raise
        child.wait()
        # The direct numerical path should not spawn a solver, but never leave
        # a surviving child/descendant in its private process group.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elapsed = time.monotonic() - started
    available_end = psutil.virtual_memory().available
    minimum_available = min(minimum_available, available_end)
    swap_end, swap_end_error = _swap_used()
    child_data = json.loads(checkpoint.read_text()) if checkpoint.is_file() else None
    job_id = child_data.get("job_id") if child_data else None
    registry_status, reconciliation_error = _reconcile_isolated_registry(
        state, job_id, child_exit_code=child.returncode, cutoff=cutoff
    )
    report = {
        "schema": "camera_registered_roi_memory_calibration_v1",
        "status": "terminated_by_monitor" if cutoff else "child_completed",
        "monitor_cutoff_reason": cutoff,
        "child_exit_code": child.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "observed_peak_monitored_rss_bytes": highest_rss,
        "child_high_water_rss": (
            child_data.get("child_high_water_rss") if child_data is not None else None
        ),
        "observed_peak_process_count": highest_process_count,
        "descendant_process_rss_monitored": descendants_monitored,
        "observed_rss_scope": (
            "child_and_descendants" if descendants_monitored else "direct_child_only"
        ),
        "selected_first_party_execution_path_subprocess_calls_in_static_scan": 0,
        "observed_rss_cutoff_bytes": OBSERVED_PROCESS_TREE_RSS_CUTOFF_BYTES,
        "wall_clock_cutoff_seconds": WALL_CUTOFF_SECONDS,
        "rss_poll_interval_seconds": POLL_SECONDS,
        "rss_cutoff_is_hard_os_limit": False,
        "rss_between_samples_can_exceed_cutoff": True,
        "physical_memory_bytes_at_start": physical_memory_bytes_at_start,
        "system_available_bytes_at_start": available_start,
        "system_available_bytes_minimum_observed": minimum_available,
        "system_available_bytes_at_end": available_end,
        "system_swap_used_bytes_at_start": swap_start,
        "system_swap_used_bytes_at_end": swap_end,
        "system_swap_used_delta_bytes": (
            None if swap_start is None or swap_end is None else swap_end - swap_start
        ),
        "system_swap_telemetry_error_at_start": swap_start_error,
        "system_swap_telemetry_error_at_end": swap_end_error,
        "production_optical_budget_bytes": original.optical_tile.estimated_working_budget_bytes,
        "diagnostic_optical_budget_bytes": elevated.optical_tile.estimated_working_budget_bytes,
        "production_policy_changed": False,
        "baseline_512_mib_preflight": baseline,
        "state_directory": str(state),
        "child_log": str(log_path),
        "child_checkpoint": str(checkpoint) if checkpoint.is_file() else None,
        "job_id": job_id,
        "registry_status_after_child_exit": registry_status,
        "registry_reconciliation_error": reconciliation_error,
        "child_evidence": child_data,
        "platform_version": platform.__version__,
        "engine_version": engine.__version__,
        "physical_fov_qualified": False,
        "product_full_frame_qualified": False,
        "sampling_convergence": "not_assessed",
        "limitation": (
            "Only the pinned 8x8 ROI is computed against an engineering-declared active "
            "mode and a field-uniform saved sensor response. A process-local policy override "
            "and observed RSS monitor do not establish hardware capacity, full-frame "
            "feasibility, physical FOV, MTF, or sampling convergence. This direct child "
            "harness is not the normal Worker timeout/publishing path. If the "
            "monitor falls back to direct-child RSS, spawned descendants are "
            "not included in its memory cutoff. System available and swap "
            "changes include unrelated host activity and are not attributable "
            "to this calibration alone."
        ),
    }
    _json_file(output, report)
    print(json.dumps({
        "report": str(output),
        "status": report["status"],
        "job_status": report["registry_status_after_child_exit"],
        "peak_monitored_rss_bytes": highest_rss,
        "observed_rss_scope": report["observed_rss_scope"],
        "elapsed_seconds": report["elapsed_seconds"],
    }, sort_keys=True), flush=True)
    return 0 if child.returncode == 0 and cutoff is None else 2


def roi_source_is_pending() -> bool:
    from camera_platform import lens_sensor_registered_roi_disk_raw_job as roi_job

    return roi_job.PENDING_SUBMISSION_SCHEMA == PENDING_CONTRACT


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--expected-import-root", type=Path, required=True)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    expected_root = args.expected_import_root.expanduser().resolve(strict=True)
    if args.self_check:
        import psutil

        platform, engine = _imports(expected_root)
        original, elevated = _diagnostic_policy()
        baseline = _baseline()
        print(json.dumps({
            "status": "ready_for_source_freeze_then_explicit_launch",
            "platform_version": platform.__version__,
            "engine_version": engine.__version__,
            "asynchronous_submission_contract_available": roi_source_is_pending(),
            "baseline": baseline,
            "production_optical_budget_bytes": original.optical_tile.estimated_working_budget_bytes,
            "diagnostic_optical_budget_bytes": elevated.optical_tile.estimated_working_budget_bytes,
            "observed_rss_cutoff_bytes": OBSERVED_PROCESS_TREE_RSS_CUTOFF_BYTES,
            "wall_cutoff_seconds": WALL_CUTOFF_SECONDS,
            "process_tree_monitor": f"psutil {psutil.__version__}",
            "swap_telemetry_available": _swap_used()[0] is not None,
            "optical_execution_started": False,
        }, sort_keys=True), flush=True)
        return 0
    state, output, checkpoint, log_path = _paths(
        args.state_dir, args.report, child=args._child
    )
    if args._child:
        return _child(state, checkpoint, expected_root)
    return _run(state, output, checkpoint, log_path, expected_root)


if __name__ == "__main__":
    raise SystemExit(main())
