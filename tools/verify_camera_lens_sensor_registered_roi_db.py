"""Retain a Worker-child proof of registered-mode ROI RAW or policy denial.

The 2048x14000 active rectangle is an unverified engineering declaration.
Only an 8x8 ROI is requested; policy denial renders none. This does not
establish product FOV, complete active-area capture, sampling convergence,
or measured optical performance.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

import camera_platform
import pyisetcam
from camera_platform.lens_sensor_active_mode_asset import register_active_mode_declaration
from camera_platform.lens_sensor_fullfield_study_preflight import MODE_SCHEMA
from camera_platform.lens_sensor_registered_roi_disk_raw_job import default_spec, get, submit
from camera_platform.sensor_db_bridge import inspect_sensor_revision
from camera_platform.service import PlatformService
from camera_platform.worker import work

PROJECT = Path(__file__).resolve().parents[1]
LENS_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
SENSOR_CODE = "DEP-2209-801"
SENSOR_RUN = "74e15e696e8d14827bc641346dc92d113a24997da6ec13efb51d9e2583a4f188"
BOUNDS = [1000, 1008, 12000, 12008]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--expected-import-root", type=Path, required=True)
    parser.add_argument("--scene-scale", type=int, choices=(2, 4), default=2)
    args = parser.parse_args()
    state = args.state_dir.resolve()
    report_path = args.report.resolve()
    import_root = args.expected_import_root.resolve(strict=True)
    platform_module = Path(camera_platform.__file__).resolve(strict=True)
    engine_module = Path(pyisetcam.__file__).resolve(strict=True)
    if not platform_module.is_relative_to(import_root) or not engine_module.is_relative_to(
        import_root
    ):
        raise RuntimeError("Camera Platform and pyisetcam must import from expected root")
    if state.exists() or report_path.exists():
        parser.error("State and report targets must not already exist")
    if not state.parent.is_dir() or not report_path.parent.is_dir():
        parser.error("State and report parent directories must exist")
    inspection = inspect_sensor_revision(
        SENSOR_CODE, root=PROJECT / "camerae2e_db", run_id=SENSOR_RUN
    )
    if inspection.get("executable") is not True:
        raise RuntimeError("Pinned Sensor DB run is not executable")
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
    service = PlatformService(state, PROJECT)
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
        shape_row_col=[2048 * args.scene_scale, 14000 * args.scene_scale],
        horizontal_fov_deg=42.0,
        edge_offset_deg=14.0,
    )
    spec["accept_approximate_lens_radiometry"] = True
    spec["acknowledge_unverified_mode_and_uniform_sensor_response"] = True
    started = time.monotonic()
    queued = submit(service, spec)
    admission_seconds = time.monotonic() - started
    worker_started = time.monotonic()
    work(service, once=True)
    worker_seconds = time.monotonic() - worker_started
    finished = get(service, queued["id"])
    binding = queued["registered_mode_binding"]
    proof = {
        "schema": "camera_lens_sensor_registered_roi_db_proof_v2",
        "execution_mode": (
            "source_checkout_worker_child"
            if import_root == (PROJECT / "src").resolve(strict=True)
            else "installed_target_worker_child"
        ),
        "platform_import_path": str(platform_module),
        "engine_import_path": str(engine_module),
        "expected_import_root": str(import_root),
        "platform_version": camera_platform.__version__,
        "engine_version": pyisetcam.__version__,
        "state_directory": str(state),
        "job_id": queued["id"],
        "job_status": finished["status"],
        "job_error": finished.get("error"),
        "submission_preflight_status": queued["submission_preflight_status"],
        "cheap_work_admission": queued["cheap_work_admission"],
        "roi_preflight_status": "not_completed",
        "roi_execution_status": "not_completed",
        "admission_seconds_observed": round(admission_seconds, 3),
        "worker_seconds_observed": round(worker_seconds, 3),
        "scene_scale_relative_to_active_axes": args.scene_scale,
        "scene_shape_row_col": spec["analytic_scene"]["shape_row_col"],
        "registered_mode_source_kind": binding["source_kind"],
        "active_shape_row_col": binding["active_shape_row_col"],
        "global_sensor_roi_bounds_row_col_exclusive": BOUNDS,
        "full_boundary_saved_field_status": None,
        "full_boundary_saved_field_reason": None,
        "physical_mode_verified": False,
        "physical_fov_qualified": False,
        "product_full_frame_qualified": False,
        "sampling_convergence": "not_assessed",
        "hard_process_rss_limit_enforced": False,
    }
    if finished["status"] == "succeeded":
        summary = finished["result"]["summary"]
        proof["roi_preflight_status"] = summary["resource_preflight"]["status"]
        proof["full_boundary_saved_field_status"] = summary[
            "registered_active_mode"
        ]["full_boundary_saved_field_status"]
        proof["full_boundary_saved_field_reason"] = summary[
            "registered_active_mode"
        ]["full_boundary_saved_field_reason"]
        arrays = {}
        for key in ("mean_electrons", "raw_dn", "reference_dn", "volts"):
            descriptor = summary["array_artifacts"][key]
            mapped = np.load(
                service.registry.blob_path(descriptor["sha256"]),
                mmap_mode="r",
                allow_pickle=False,
            )
            arrays[key] = {
                "dtype": str(mapped.dtype),
                "shape_row_col": list(mapped.shape),
                "minimum": float(np.min(mapped)),
                "maximum": float(np.max(mapped)),
                "sha256": descriptor["sha256"],
                "bytes": descriptor["bytes"],
            }
            del mapped
        if (
            any(item["shape_row_col"] != [8, 8] for item in arrays.values())
            or arrays["mean_electrons"]["maximum"] <= 0
            or any(
                summary["registered_active_mode"].get(key) != value
                for key, value in binding.items()
            )
            or summary["sampling"]["sampling_convergence"] != "not_assessed"
            or summary["physical_fov_qualified"] is not False
            or summary["product_full_frame_qualified"] is not False
            or summary["sensor_response_field_uniform_assumption"] is not True
            or summary["physical_pixel_field_response_verified"] is not False
        ):
            raise RuntimeError("Saved ROI output or qualification contract differs")
        proof["arrays"] = arrays
        proof["result"] = summary
        proof["roi_execution_status"] = summary["status"]
    elif finished["status"] == "failed" and args.scene_scale == 4:
        failure = finished["result"]["preflight_failure"]
        resource = failure["resource_evidence"]
        if (
            failure["stage"] != "model_dependent_preflight"
            or failure["cause_status"] != "blocked_working_memory_policy"
            or resource["combined_policy_estimate_bytes"]
            <= resource["estimated_working_budget_bytes"]
            or failure["optical_convolution_executed"] is not False
            or finished["artifacts"]
        ):
            raise RuntimeError("4x Worker policy denial evidence differs")
        proof["roi_preflight_status"] = failure["cause_status"]
        proof["preflight_failure"] = failure
    report_path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "job_id": queued["id"],
                "status": finished["status"],
                "worker_seconds": proof["worker_seconds_observed"],
                "full_boundary_saved_field_status": proof["full_boundary_saved_field_status"],
                "full_boundary_saved_field_reason": proof["full_boundary_saved_field_reason"],
                "report": str(report_path),
                "state": str(state),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    expected = "succeeded" if args.scene_scale == 2 else "failed"
    if finished["status"] != expected:
        raise RuntimeError(
            f"Registered ROI Worker-child status {finished['status']} differs from {expected}: "
            f"{finished.get('error')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
