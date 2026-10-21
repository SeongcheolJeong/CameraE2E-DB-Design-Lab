#!/usr/bin/env python3
"""Observe P0014 fixed-FOV off-axis 2x/4x mean-electron refinement.

Each edge phase runs in a disposable child with a finite wall-time and polled
direct-child RSS cutoff. Polling is fault containment, not a hard memory limit.
Only an 8x8 virtual-sensor mean tile is evaluated; no RAW, ISP, full active
sensor, product FOV, physical PSF accuracy, or MTF is qualified here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import verify_camera_p0014_offaxis_mean_convergence as previous_verifier

from camera_platform.lens_sensor_process_guard import run_observed_child
from camera_platform.lens_sensor_scene_geometry_tile import (
    make_analytic_strict_geometry_provider,
)
from camera_platform.lens_sensor_streamed_offaxis_mean import (
    compute_streamed_analytic_offaxis_mean,
    preflight_streamed_analytic_offaxis_mean,
)

ROOT = previous_verifier.ROOT
INPUT = previous_verifier.INPUT
PRIOR_REPORT = previous_verifier.OUTPUT
OUTPUT = (
    ROOT / "reports/camera_platform/mtf-support-20260928/"
    "p0014-fixed-fov-offaxis-mean-2x-4x-observation.json"
)
NUMERICAL_SOURCES = (
    "src/camera_platform/lens_sensor_streamed_offaxis_mean.py",
    "src/camera_platform/lens_sensor_scene_geometry_tile.py",
    "src/camera_platform/lens_sensor_offaxis_sensor_tile.py",
    "src/camera_platform/lens_sensor_optical_source_halo.py",
    "src/camera_platform/lens_sensor_tile_plan.py",
    "src/pyisetcam/optics.py",
    "src/pyisetcam/sensor.py",
)
RSS_CUTOFF_BYTES = {2: 4 * 1024**3, 4: 6 * 1024**3}
PHASE_TIMEOUT_S = {2: 600.0, 4: 1200.0}
MIN_SYSTEM_FREE_PERCENT_POLICY = 60


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _numerical_source_hashes() -> dict[str, str]:
    return {name: _file_sha256(ROOT / name) for name in NUMERICAL_SOURCES}


def _memory_free_percent() -> int:
    completed = subprocess.run(
        ["memory_pressure", "-Q"], capture_output=True, text=True,
        check=True, timeout=8,
    )
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", completed.stdout)
    if match is None:
        raise ValueError("Host memory-pressure observation is unavailable.")
    return int(match.group(1))


def _prior_two_level() -> dict[str, Any]:
    prior = json.loads(PRIOR_REPORT.read_text(encoding="utf-8"))
    levels = prior.get("levels", [])
    if (
        prior.get("status") != "two_levels_executed"
        or [item.get("factor") for item in levels] != [1, 2]
        or any(item.get("payload", {}).get("status") != "succeeded" for item in levels)
    ):
        raise ValueError("The pinned 1x/2x observation is incomplete.")
    two = levels[1]["payload"]["result"]
    if (
        two["lineage"]["lens_revision_id"] != previous_verifier.REVISION_ID
        or two["lineage"]["source_psf_asset_sha256"] != previous_verifier.PSF_SHA256
        or two["virtual_sensor_origin"] != "hypothetical_default_sensor_not_DB_active_mode"
    ):
        raise ValueError("The prior 2x observation has different model provenance.")
    return prior


def _run_phase(factor: int, phase: int) -> dict[str, Any]:
    if factor not in (2, 4) or phase not in (0, 1):
        raise ValueError("Only pinned 2x/4x baseline and shifted phases are supported.")
    data = previous_verifier._source()
    expected = next(item for item in data["results"] if item["refinement_factor"] == factor)
    if expected["status"] != "ready_under_declared_policy":
        raise ValueError("The selected fixed-FOV refinement did not pass stored preflight.")
    optics, lineage = previous_verifier._optics()
    sensor = previous_verifier._virtual_sensor(data)
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
        raise ValueError("Analytic source identity differs from pinned preflight.")
    provider = (
        baseline if phase == 0 else replace(
            baseline,
            edge_offset_deg=baseline.edge_offset_deg + previous_verifier.EDGE_SHIFT_DEG,
        )
    )
    arguments = {
        "sensor_tile_bounds_row_col_exclusive": tuple(
            data["virtual_sensor"]["tile_bounds_row_col_exclusive"]
        ),
        "source_tile_shape_row_col": previous_verifier.TILE_SHAPE,
        "sample_angles_deg": previous_verifier.ANGLES,
        "resource_policy": previous_verifier._policy(data),
    }
    plan = preflight_streamed_analytic_offaxis_mean(provider, sensor, **arguments)
    if plan["status"] != "ready_under_declared_policy":
        raise ValueError(f"Selected off-axis tile preflight failed: {plan['status']}.")
    started = time.monotonic()
    mean, execution = compute_streamed_analytic_offaxis_mean(provider, sensor, **arguments)
    elapsed = time.monotonic() - started
    if mean.shape != (8, 8) or not np.isfinite(mean).all() or np.any(mean < 0):
        raise ValueError("Computed mean-electron tile is invalid.")
    if (
        execution["provider"]["source_identity"] != provider.source_identity
        or execution["full_optical_state_sha256"] != baseline.optics_state_sha256
        or execution["sensor_execution"]["sensor_tile_bounds_row_col_exclusive"]
        != data["virtual_sensor"]["tile_bounds_row_col_exclusive"]
    ):
        raise ValueError("Off-axis tile execution changed source or global coordinates.")
    return {
        "factor": factor,
        "phase": "baseline" if phase == 0 else "edge_shifted",
        "lineage": lineage,
        "virtual_sensor_origin": data["virtual_sensor"]["origin"],
        "virtual_sensor_shape_row_col": data["virtual_sensor"]["shape_row_col"],
        "sensor_tile_bounds_row_col_exclusive": data["virtual_sensor"][
            "tile_bounds_row_col_exclusive"
        ],
        "scene_shape_row_col": expected["scene_shape_row_col"],
        "horizontal_fov_deg": data["horizontal_fov_deg"],
        "oi_pitch_um": expected["oi_pitch_um"],
        "sampled_psf_kernel_shape_row_col": plan["sampled_psf_kernel_shape_row_col"],
        "source_identity": provider.source_identity,
        "full_optical_state_sha256": baseline.optics_state_sha256,
        "resource_preflight_status": plan["status"],
        "resource_policy_estimate_bytes": plan["combined_policy_estimate_bytes"],
        "optical_elapsed_s": elapsed,
        "source_tiles_processed": execution["optical_execution"]["source_tiles_processed"],
        "generated_psf_kernel_count": execution["optical_execution"][
            "generated_kernel_count"
        ],
        "mean_electrons_sha256": previous_verifier._digest(mean),
        "mean_electrons": mean.tolist(),
        "readout_or_isp_executed": False,
        "full_frame_executed": False,
        "product_fov_qualified": False,
    }


def _compare(observations: list[dict[str, Any]], prior: dict[str, Any]) -> dict[str, Any]:
    by_key = {
        (item["factor"], item["phase"]): item["payload"]["result"]
        for item in observations
    }
    phases = ("baseline", "edge_shifted")
    two = [np.asarray(by_key[(2, phase)]["mean_electrons"], dtype=float) for phase in phases]
    four = [np.asarray(by_key[(4, phase)]["mean_electrons"], dtype=float) for phase in phases]
    prior_two = prior["levels"][1]["payload"]["result"]
    prior_two_hashes = prior_two["mean_electrons_sha256_by_phase"]
    new_two_hashes = [by_key[(2, phase)]["mean_electrons_sha256"] for phase in phases]
    prior_two_source_identities = prior_two["source_identity_by_phase"]
    new_two_source_identities = [by_key[(2, phase)]["source_identity"] for phase in phases]
    optical_state_hashes = {
        by_key[(factor, phase)]["full_optical_state_sha256"]
        for factor in (2, 4) for phase in phases
    }
    prior_two_model_matches = optical_state_hashes == {
        prior_two["full_optical_state_sha256"]
    }
    scale = max(1e-12, *(float(np.max(np.abs(item))) for item in (*two, *four)))
    mean_change = max(float(np.max(np.abs(a - b))) for a, b in zip(two, four, strict=True))
    shift_change = float(np.max(np.abs((four[1] - four[0]) - (two[1] - two[0]))))
    previous_change = float(
        prior["two_level_numerical_comparison"]["mean_electrons_relative_max_both_phases"]
    )
    threshold = 0.10
    return {
        "reference_scale_electrons": scale,
        "mean_electrons_absolute_max_change": mean_change,
        "mean_electrons_relative_max_both_phases": mean_change / scale,
        "edge_shift_response_relative_max_change": shift_change / scale,
        "prior_1x_to_2x_mean_relative_max": previous_change,
        "prior_2x_mean_hashes_match_current_reexecution": prior_two_hashes == new_two_hashes,
        "prior_2x_source_identities_match_current_reexecution": (
            prior_two_source_identities == new_two_source_identities
        ),
        "prior_2x_optical_state_matches_all_current_phases": prior_two_model_matches,
        "maximum_allowed_single_step_tolerance_by_diagnostic_api": threshold,
        "current_2x_to_4x_mean_step_below_tolerance": mean_change / scale <= threshold,
        "two_consecutive_passing_steps": (
            previous_change <= threshold and mean_change / scale <= threshold
            and prior_two_hashes == new_two_hashes
            and prior_two_source_identities == new_two_source_identities
            and prior_two_model_matches
        ),
        "declared_numerical_convergence_pass": False,
        "reason": (
            "A 1x-to-2x step exceeded 10%; two consecutive passing refinements "
            "are absent even if the 2x-to-4x step is below that tolerance. "
            "This compares 8x8 virtual-sensor mean electrons, not MTF or product FOV."
        ),
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child-factor", type=int, choices=(2, 4))
    parser.add_argument("--child-phase", type=int, choices=(0, 1))
    parser.add_argument("--child-result", type=Path)
    parser.add_argument("--report", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if args.child_factor is not None:
        if (
            args.child_phase is None
            or args.child_result is None
            or not args.child_result.is_absolute()
        ):
            parser.error("Children require phase 0/1 and an absolute result path.")
        try:
            previous_verifier._atomic_json(
                args.child_result,
                {"status": "succeeded", "result": _run_phase(args.child_factor, args.child_phase)},
            )
            return 0
        except Exception as exc:
            previous_verifier._atomic_json(
                args.child_result,
                {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)},
            )
            return 1
    if args.child_phase is not None or args.child_result is not None:
        parser.error("Child phase/result options require --child-factor.")

    prior = _prior_two_level()
    data = previous_verifier._source()
    expected = {item["refinement_factor"]: item for item in data["results"]}
    if any(expected[factor]["status"] != "ready_under_declared_policy" for factor in (2, 4)):
        raise ValueError("The pinned 2x/4x preflights are not ready under declared policy.")
    start_sources = _numerical_source_hashes()
    start = time.monotonic()
    report: dict[str, Any] = {
        "schema": "camera_p0014_fixed_fov_offaxis_mean_2x_4x_observation_v1",
        "status": "running_diagnostic",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "source_preflight_report": str(INPUT.relative_to(ROOT)),
        "prior_1x_2x_report": str(PRIOR_REPORT.relative_to(ROOT)),
        "prior_1x_2x_report_sha256": _file_sha256(PRIOR_REPORT),
        "lens_revision_id": previous_verifier.REVISION_ID,
        "source_psf_asset_sha256": previous_verifier.PSF_SHA256,
        "fixed_horizontal_fov_deg": data["horizontal_fov_deg"],
        "fixed_virtual_sensor_shape_row_col": data["virtual_sensor"]["shape_row_col"],
        "fixed_sensor_tile_bounds_row_col_exclusive": data["virtual_sensor"][
            "tile_bounds_row_col_exclusive"
        ],
        "edge_shift_deg": previous_verifier.EDGE_SHIFT_DEG,
        "refinement_factors": [2, 4],
        "numerical_source_file_sha256_before": start_sources,
        "host_memory_observation_command": "memory_pressure -Q",
        "minimum_free_percent_admission_policy": MIN_SYSTEM_FREE_PERCENT_POLICY,
        "observed_rss_cutoff_bytes_by_factor": RSS_CUTOFF_BYTES,
        "per_phase_timeout_s_by_factor": PHASE_TIMEOUT_S,
        "observer_is_hard_memory_limit": False,
        "observer_monitors_descendants": False,
        "observations": [],
        "claims": {
            "physical_psf_qualified": False,
            "physical_mtf_allowed": False,
            "product_fov_qualified": False,
            "full_frame_execution_qualified": False,
            "raw_or_isp_executed": False,
        },
    }
    previous_verifier._atomic_json(args.report, report)
    with tempfile.TemporaryDirectory(prefix="camera-p0014-2x-4x-") as directory:
        for factor in (2, 4):
            for phase in (0, 1):
                free_percent = _memory_free_percent()
                if free_percent < MIN_SYSTEM_FREE_PERCENT_POLICY:
                    report.update(
                        status="inconclusive_host_memory_policy",
                        reason="System-wide free-memory observation fell below declared policy.",
                    )
                    break
                child_result = Path(directory) / f"factor-{factor}-phase-{phase}.json"
                observer = run_observed_child(
                    [
                        str(Path(sys.executable).resolve()), "-B", str(Path(__file__).resolve()),
                        "--child-factor", str(factor),
                        "--child-phase", str(phase),
                        "--child-result", str(child_result),
                    ],
                    state_root=directory,
                    observed_rss_limit_bytes=RSS_CUTOFF_BYTES[factor],
                    timeout_s=PHASE_TIMEOUT_S[factor],
                    poll_interval_s=0.02,
                )
                payload = (
                    json.loads(child_result.read_text(encoding="utf-8"))
                    if child_result.exists() else None
                )
                report["observations"].append({
                    "factor": factor,
                    "phase": "baseline" if phase == 0 else "edge_shifted",
                    "host_memory_free_percent_before": free_percent,
                    "observer": observer,
                    "payload": payload,
                })
                report["elapsed_s"] = time.monotonic() - start
                previous_verifier._atomic_json(args.report, report)
                if (
                    observer["status"] != "child_exited"
                    or observer["child_exit_code"] != 0
                    or not payload
                    or payload.get("status") != "succeeded"
                ):
                    report.update(
                        status="inconclusive_child_or_resource_failure",
                        reason="A required 2x/4x phase did not complete successfully.",
                    )
                    break
            if report["status"] != "running_diagnostic":
                break
    report["numerical_source_file_sha256_after"] = _numerical_source_hashes()
    report["numerical_sources_unchanged_during_observation"] = (
        report["numerical_source_file_sha256_after"] == start_sources
    )
    report["elapsed_s"] = time.monotonic() - start
    report["completed_at_utc"] = datetime.now(UTC).isoformat()
    if not report["numerical_sources_unchanged_during_observation"]:
        report.update(
            status="inconclusive_numerical_source_changed",
            reason="A numerical source file changed during the observation.",
        )
    elif report["status"] == "running_diagnostic":
        if len(report["observations"]) != 4:
            raise ValueError("Expected exactly four completed phase observations.")
        report["comparison"] = _compare(report["observations"], prior)
        report["status"] = "two_refinement_levels_executed"
    previous_verifier._atomic_json(args.report, report)
    print(json.dumps({
        "status": report["status"],
        "report": str(args.report),
        "completed_phase_count": len(report["observations"]),
        "comparison": report.get("comparison"),
    }))
    return 0 if report["status"] == "two_refinement_levels_executed" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
