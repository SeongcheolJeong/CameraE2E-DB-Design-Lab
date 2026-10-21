#!/usr/bin/env python3
"""Observed P0014 1x/2x off-axis mean-electron refinement diagnostic.

The virtual sensor is an explicitly hypothetical default linear sensor. This
script does not establish a DB product mode, physical PSF accuracy, MTF, RAW,
or a full-frame memory bound. The RSS observer polls and is not a hard quota.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from camera_platform.lens_catalog_bridge import _asset, _revision_source
from camera_platform.lens_sensor_process_guard import run_observed_child
from camera_platform.lens_sensor_scene_geometry_tile import (
    make_analytic_strict_geometry_provider,
)
from camera_platform.lens_sensor_streamed_analytic_capture import (
    AnalyticCaptureResourcePolicy,
)
from camera_platform.lens_sensor_streamed_offaxis_mean import (
    compute_streamed_analytic_offaxis_mean,
    preflight_streamed_analytic_offaxis_mean,
)
from pyisetcam.lens_patents import lens_patent_raytrace_optics
from pyisetcam.sensor import sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor

ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = ROOT / "reports/camera_platform/mtf-support-20260928"
INPUT = REPORT_ROOT / "p0014-fixed-fov-offaxis-grid-preflight-dev41.json"
OUTPUT = REPORT_ROOT / "p0014-fixed-fov-offaxis-mean-observation.json"
REVISION_ID = "6c48e967-74b4-5901-8186-ba6fafe4797b"
PSF_SHA256 = "1f4fad1d2858e93e8eb911281743ff81897f5787a98a997c4cda269b5b09da6e"
# A fixed object-angle displacement of about one native virtual-sensor pixel.
EDGE_SHIFT_DEG = 0.001
ANGLES = np.arange(0.0, 361.0, 10.0)
TILE_SHAPE = (128, 128)


def _source() -> dict[str, Any]:
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    if (
        data["lens_revision_id"] != REVISION_ID
        or data["source_psf_asset_sha256"] != PSF_SHA256
        or data["virtual_sensor"]["origin"] != "hypothetical_default_sensor_not_DB_active_mode"
        or data["virtual_sensor"]["shape_row_col"] != [2048, 14000]
        or data["virtual_sensor"]["tile_bounds_row_col_exclusive"]
        != [1020, 1028, 12659, 12667]
    ):
        raise ValueError("The pinned dev41 diagnostic input changed.")
    return data


def _optics() -> tuple[dict[str, Any], dict[str, Any]]:
    root = ROOT / "camerae2e_db"
    revision, snapshot, lens_db, source_sha = _revision_source(REVISION_ID, root)
    simulation_id, psf_path, validation, _ = _asset(revision, snapshot, lens_db, root)
    if validation["asset_sha256"] != PSF_SHA256:
        raise ValueError("The selected PSF asset changed.")
    optics = lens_patent_raytrace_optics(
        simulation_id,
        db_path=lens_db,
        psf_dir=psf_path.parent,
        allow_approximate_radiometry=True,
    )
    ray = optics["raytrace"]
    if (
        ray.get("asset_sha256") != PSF_SHA256
        or ray.get("psf_kind") != "geometric"
        or ray.get("diffraction_included") is not False
        or ray.get("radiometry_status") == "validated"
    ):
        raise ValueError("Loaded optics differ from the pinned geometric preview.")
    return optics, {
        "lens_revision_id": REVISION_ID,
        "lens_source_database_sha256": source_sha,
        "simulation_id": simulation_id,
        "source_psf_asset_sha256": PSF_SHA256,
        "psf_kind": "stored_geometric_raytrace",
        "diffraction_included": False,
        "absolute_radiometry_validated": False,
    }


def _virtual_sensor(data: dict[str, Any]):
    spec = data["virtual_sensor"]
    wavelengths = np.asarray(data["wavelength_nm"], dtype=float)
    sensor = sensor_create(
        "default", {"size": tuple(spec["shape_row_col"]), "wave": wavelengths}
    )
    sensor = sensor_set(
        sensor, "pixel size same fill factor", float(spec["pixel_pitch_um"]) * 1e-6
    )
    sensor = sensor_set(sensor, "integration time", float(spec["exposure_s"]))
    sensor = sensor_set(sensor, "auto exposure", False)
    return configure_e2e_sensor(sensor)


def _policy(data: dict[str, Any]) -> AnalyticCaptureResourcePolicy:
    values = data["resource_policy"]
    return AnalyticCaptureResourcePolicy(
        estimated_working_budget_bytes=values["estimated_working_budget_bytes"],
        max_geometry_query_cells=values["max_geometry_query_cells"],
        max_geometry_query_chunk_cells=values["max_geometry_query_chunk_cells"],
        max_source_tiles=values["max_source_tiles"],
        max_output_roi_cells=values["max_output_roi_cells"],
        max_source_tile_cells=values["max_source_tile_cells"],
    )


def _digest(values: np.ndarray) -> str:
    return "sha256:" + hashlib.sha256(
        np.ascontiguousarray(values, dtype="<f8").tobytes()
    ).hexdigest()


def _run_factor(factor: int) -> dict[str, Any]:
    if factor not in (1, 2):
        raise ValueError("Only 1x and 2x are authorized for actual execution.")
    data = _source()
    expected = next(item for item in data["results"] if item["refinement_factor"] == factor)
    optics, lineage = _optics()
    sensor = _virtual_sensor(data)
    baseline = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=tuple(expected["scene_shape_row_col"]),
        scene_horizontal_fov_deg=data["horizontal_fov_deg"],
        wavelengths_nm=np.asarray(data["wavelength_nm"], dtype=float),
        bright_side_photons=np.asarray(data["bright_side_photons"], dtype=float),
        analytic_slanted_edge_offset_deg=data["edge_offset_deg"],
        declared_oi_pitch_um=expected["oi_pitch_um"],
        max_tile_cells=data["resource_policy"]["max_source_tile_cells"],
    )
    if baseline.source_identity != expected["source_identity"]:
        raise ValueError("Analytic source identity differs from dev41 preflight.")
    shifted = replace(baseline, edge_offset_deg=baseline.edge_offset_deg + EDGE_SHIFT_DEG)
    args = {
        "sensor_tile_bounds_row_col_exclusive": tuple(
            data["virtual_sensor"]["tile_bounds_row_col_exclusive"]
        ),
        "source_tile_shape_row_col": TILE_SHAPE,
        "sample_angles_deg": ANGLES,
        "resource_policy": _policy(data),
    }
    # Check both phases before optical convolution starts.
    plans = [
        preflight_streamed_analytic_offaxis_mean(provider, sensor, **args)
        for provider in (baseline, shifted)
    ]
    if any(plan["status"] != "ready_under_declared_policy" for plan in plans):
        raise ValueError(f"Two-phase preflight blocked: {[plan['status'] for plan in plans]}")
    outputs = []
    for provider in (baseline, shifted):
        started = time.monotonic()
        mean, report = compute_streamed_analytic_offaxis_mean(provider, sensor, **args)
        if not np.isfinite(mean).all() or np.any(mean < 0):
            raise ValueError("Mean electron tile contains invalid samples.")
        outputs.append((mean, report, time.monotonic() - started))
    arrays = [item[0] for item in outputs]
    scale = max(1e-12, *(float(np.max(np.abs(array))) for array in arrays))
    response = arrays[1] - arrays[0]
    return {
        "factor": factor,
        "lineage": lineage,
        "virtual_scene_shape_row_col": expected["scene_shape_row_col"],
        "virtual_sensor_origin": data["virtual_sensor"]["origin"],
        "virtual_sensor_shape_row_col": data["virtual_sensor"]["shape_row_col"],
        "virtual_sensor_contract": {
            "constructor": "pyisetcam.sensor_create(default)",
            "is_saved_sensor_DB_active_mode": False,
            "pixel_pitch_um": data["virtual_sensor"]["pixel_pitch_um"],
            "exposure_s": data["virtual_sensor"]["exposure_s"],
            "cfa_pattern": np.asarray(sensor.fields["pattern"], dtype=int).tolist(),
            "n_samples_per_pixel": sensor.fields["n_samples_per_pixel"],
            "sensor_output_mode": sensor.fields["sensor_output_mode"],
            "electrical_readout_executed": False,
        },
        "sensor_tile_bounds_row_col_exclusive": data["virtual_sensor"][
            "tile_bounds_row_col_exclusive"
        ],
        "horizontal_fov_deg": data["horizontal_fov_deg"],
        "oi_pitch_um": baseline.oi_pitch_um,
        "full_optical_state_sha256": baseline.optics_state_sha256,
        "source_identity_by_phase": [baseline.source_identity, shifted.source_identity],
        "preflight_status_by_phase": [plan["status"] for plan in plans],
        "sampled_psf_kernel_shape_row_col_by_phase": [
            plan["sampled_psf_kernel_shape_row_col"] for plan in plans
        ],
        "estimated_working_bytes_by_phase": [
            plan["combined_policy_estimate_bytes"] for plan in plans
        ],
        "phase_elapsed_s": [item[2] for item in outputs],
        "source_tiles_processed_by_phase": [
            item[1]["optical_execution"]["source_tiles_processed"] for item in outputs
        ],
        "mean_electrons_shape_row_col": list(arrays[0].shape),
        "mean_electrons_sha256_by_phase": [_digest(array) for array in arrays],
        "mean_electrons_by_phase": [array.tolist() for array in arrays],
        "normalized_edge_contrast_by_phase": [
            float(np.percentile(array, 90) - np.percentile(array, 10)) / scale
            for array in arrays
        ],
        "normalized_shift_response_amplitude": float(np.max(np.abs(response))) / scale,
        "readout_or_isp_executed": False,
        "full_frame_executed": False,
        "product_fov_qualified": False,
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child-factor", type=int, choices=(1, 2))
    parser.add_argument("--child-result", type=Path)
    parser.add_argument("--report", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if args.child_factor is not None:
        if args.child_result is None or not args.child_result.is_absolute():
            parser.error("--child-result must be an absolute path")
        try:
            result = {"status": "succeeded", "result": _run_factor(args.child_factor)}
            _atomic_json(args.child_result, result)
            return 0
        except Exception as exc:
            _atomic_json(
                args.child_result,
                {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)},
            )
            return 1

    import sys

    start = time.monotonic()
    observed = []
    with tempfile.TemporaryDirectory(prefix="camera-p0014-means-") as directory:
        for factor in (1, 2):
            child_result = Path(directory) / f"factor-{factor}.json"
            limit = (3 if factor == 1 else 4) * 1024**3
            guard = run_observed_child(
                [
                    str(Path(sys.executable).resolve()), "-B", str(Path(__file__).resolve()),
                    "--child-factor", str(factor), "--child-result", str(child_result),
                ],
                state_root=directory,
                observed_rss_limit_bytes=limit,
                timeout_s=240.0 if factor == 1 else 480.0,
                poll_interval_s=0.02,
            )
            payload = (
                json.loads(child_result.read_text(encoding="utf-8"))
                if child_result.exists() else None
            )
            observed.append({"factor": factor, "observer": guard, "payload": payload})
            if (
                guard["status"] != "child_exited"
                or guard["child_exit_code"] != 0
                or not payload
                or payload["status"] != "succeeded"
            ):
                break

    successful = len(observed) == 2 and all(
        item["payload"] and item["payload"]["status"] == "succeeded" for item in observed
    )
    report: dict[str, Any] = {
        "schema": "camera_p0014_fixed_fov_offaxis_mean_observation_v1",
        "status": "two_levels_executed" if successful else "inconclusive_child_or_resource_failure",
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "source_preflight_report": str(INPUT.relative_to(ROOT)),
        "edge_shift_deg": EDGE_SHIFT_DEG,
        "elapsed_s": time.monotonic() - start,
        "levels": observed,
        "observer_is_hard_memory_limit": False,
        "parent_claims": {
            "numeric_two_level_stability_established": False,
            "physical_psf_qualified": False,
            "physical_mtf_allowed": False,
            "product_fov_qualified": False,
            "full_frame_execution_qualified": False,
            "raw_or_isp_executed": False,
        },
    }
    if successful:
        first = observed[0]["payload"]["result"]
        second = observed[1]["payload"]["result"]
        a = [np.asarray(value, dtype=float) for value in first["mean_electrons_by_phase"]]
        b = [np.asarray(value, dtype=float) for value in second["mean_electrons_by_phase"]]
        if any(x.shape != y.shape for x, y in zip(a, b, strict=True)):
            raise ValueError("Refinement changed the fixed virtual-sensor output tile.")
        scale = max(1e-12, *(float(np.max(np.abs(array))) for array in (*a, *b)))
        mean_delta = max(
            float(np.max(np.abs(x - y))) for x, y in zip(a, b, strict=True)
        ) / scale
        shift_delta = float(
            np.max(np.abs((b[1] - b[0]) - (a[1] - a[0])))
        ) / scale
        report["two_level_numerical_comparison"] = {
            "mean_electrons_relative_max_both_phases": mean_delta,
            "shift_response_relative_max": shift_delta,
            "reference_scale_electrons": scale,
            "maximum_allowed_single_step_tolerance_by_diagnostic_api": 0.10,
            "mean_change_exceeds_that_maximum": mean_delta > 0.10,
            "declared_convergence_pass": None,
            "reason": (
                "The mean change exceeds the diagnostic API's maximum 10% "
                "single-step tolerance, and two successive passing refinements "
                "would be required to establish numerical convergence."
            ),
        }
    _atomic_json(args.report, report)
    print(json.dumps({
        "status": report["status"], "report": str(args.report),
        "two_level_numerical_comparison": report.get("two_level_numerical_comparison"),
        "observer_statuses": [item["observer"]["status"] for item in observed],
    }))
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(_main())
