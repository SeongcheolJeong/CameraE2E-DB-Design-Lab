"""Bounded Scene-grid ladder for two already verified geometric PSF optics.

The caller must load and pin the registered Lens/Sensor and construct the
candidate optics with the verified dense-geometric-PSF overlay.  This helper
does not promote that candidate: it executes the *same* physical edge at each
Scene grid for both optics and reports mean-electron/edge-phase sensitivity.
The dense candidate keeps the source histogram pitch, so this is neither a
PSF-bin refinement nor an independent pupil-density convergence proof.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .lens_sensor_scene_geometry_tile import _optical_state_fingerprint
from .lens_sensor_streamed_full_raw import _sensor_identity

SCHEMA = "camera_lens_sensor_joint_scene_dense_psf_ladder_v1"
MAX_GRIDS = 5
MAX_TRANSIENT_ROI_AXIS = 128


def _ladder(shapes: Sequence[Sequence[int]]) -> list[tuple[int, int]]:
    if not isinstance(shapes, (tuple, list)) or not 3 <= len(shapes) <= MAX_GRIDS:
        raise ValueError("Joint ladder requires 3..5 explicit fixed-FOV Scene grids.")
    result: list[tuple[int, int]] = []
    for shape in shapes:
        if (
            not isinstance(shape, (tuple, list)) or len(shape) != 2
            or any(type(axis) is not int or axis < 32 or axis % 2 for axis in shape)
        ):
            raise ValueError("Each Scene grid needs two even integer axes >= 32.")
        pair = (shape[0], shape[1])
        if result and pair != (result[-1][0] * 2, result[-1][1] * 2):
            raise ValueError("Joint ladder grids must double both axes at fixed aspect.")
        result.append(pair)
    return result


def _tolerance(value: float, name: str) -> float:
    if (
        type(value) not in (int, float) or not math.isfinite(value)
        or not 1e-6 <= value <= 0.1
    ):
        raise ValueError(f"{name} must be finite and within [1e-6, 0.1].")
    return float(value)


def _capture_arrays(values: Any) -> tuple[np.ndarray, np.ndarray] | None:
    if values is None:
        return None
    if not isinstance(values, (tuple, list)) or len(values) != 2:
        raise ValueError("A completed grid needs two transient mean-electron phases.")
    means = tuple(np.asarray(value) for value in values)
    if any(
        array.ndim != 2 or not 0 < min(array.shape)
        or max(array.shape) > MAX_TRANSIENT_ROI_AXIS
        or not (
            np.issubdtype(array.dtype, np.floating)
            or np.issubdtype(array.dtype, np.integer)
        )
        or not np.isfinite(array).all() or np.any(array < 0)
        for array in means
    ) or means[0].shape != means[1].shape:
        raise ValueError("Transient means must be finite nonnegative matching <=128x128 tiles.")
    return means  # type: ignore[return-value]


def _physical_input(result: Mapping[str, Any], *, across_grids: bool) -> dict[str, Any]:
    physical = result.get("fixed_physical_input")
    if not isinstance(physical, dict):
        raise ValueError("Capture omitted its fixed physical input.")
    excluded = {"optics_state_sha256"}
    if across_grids:
        excluded.add("reference_scene_aspect_row_col")
    return {key: value for key, value in physical.items() if key not in excluded}


def _phase_step(result: Mapping[str, Any], shape: tuple[int, int]) -> dict[str, Any]:
    steps = result.get("steps")
    if not isinstance(steps, list) or len(steps) != 1:
        raise ValueError("Each joint capture must execute exactly one grid.")
    step = steps[0]
    if (
        not isinstance(step, dict) or step.get("scene_shape_row_col") != list(shape)
        or not isinstance(step.get("mean_electrons_sha256_by_phase"), list)
        or len(step["mean_electrons_sha256_by_phase"]) != 2
    ):
        raise ValueError("Capture grid or phase hash differs from the requested ladder.")
    return step


def _full_scale_delta(
    earlier: tuple[np.ndarray, np.ndarray], later: tuple[np.ndarray, np.ndarray],
) -> dict[str, float]:
    if any(a.shape != b.shape for a, b in zip(earlier, later, strict=True)):
        raise ValueError("Compared mean-electron tiles must share native ROI coordinates.")
    scale = max(
        1e-12,
        *(float(np.max(np.abs(value))) for value in (*earlier, *later)),
    )
    mean_delta = max(
        float(np.max(np.abs(b - a))) / scale
        for a, b in zip(earlier, later, strict=True)
    )
    phase_delta = float(np.max(np.abs(
        (later[1] - later[0]) - (earlier[1] - earlier[0])
    ))) / scale
    return {
        "mean_electron_full_scale_normalized_max_delta": mean_delta,
        "shift_response_full_scale_normalized_max_delta": phase_delta,
        "full_scale_electrons_denominator": scale,
    }


def run_joint_scene_dense_psf_ladder(
    source_optics: dict[str, Any],
    candidate_optics: dict[str, Any],
    sensor: Any,
    *,
    scene_grid_ladder_row_col: Sequence[Sequence[int]],
    runner_kwargs: Mapping[str, Any],
    mean_delta_tolerance: float,
    shift_delta_tolerance: float,
) -> dict[str, Any]:
    """Execute both optics at every requested grid, with no early pass stop.

    Two consecutive 2x comparisons must pass for *each* optical variant at
    the terminal grid.  The source-vs-candidate comparison at each grid is
    sensitivity only.  No transient mean array is put in the returned report.
    The registered runner provides the bounded tile/memory preflight and
    native-pixel floor for each capture.
    """
    from .lens_sensor_registered_roi_frequency_execution import (
        run_registered_roi_frequency_sampling,
    )

    shapes = _ladder(scene_grid_ladder_row_col)
    mean_tol = _tolerance(mean_delta_tolerance, "Mean-electron tolerance")
    shift_tol = _tolerance(shift_delta_tolerance, "Shift-response tolerance")
    if not isinstance(runner_kwargs, Mapping) or any(
        key in runner_kwargs
        for key in ("first_scene_shape_row_col", "max_refinements", "return_transient_means")
    ):
        raise ValueError("Supply common runner kwargs without grid/refinement overrides.")
    source_before = _optical_state_fingerprint(source_optics)
    candidate_before = _optical_state_fingerprint(candidate_optics)
    sensor_before = _sensor_identity(sensor)
    if source_before == candidate_before:
        raise ValueError("Candidate optical state must differ from the source state.")
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "inconclusive_joint_scene_grid_refinement",
        "requested_scene_grid_ladder_row_col": [list(shape) for shape in shapes],
        "source_optical_state_sha256": source_before,
        "candidate_optical_state_sha256": candidate_before,
        "sensor_identity": sensor_before,
        "mean_electron_full_scale_normalized_max_delta_tolerance": mean_tol,
        "shift_response_full_scale_normalized_max_delta_tolerance": shift_tol,
        "required_consecutive_refinement_passes_per_optics": 2,
        "all_grids_executed": False,
        "scene_grid_mean_phase_converged_for_both_optics": False,
        "frequency_target_points_status": "not_assessed_by_joint_mean_phase_helper",
        "source_vs_candidate_interpretation": "same_grid_geometric_psf_shape_sensitivity_only",
        "independent_pupil_density_convergence_established": False,
        "psf_histogram_pitch_refinement_assessed": False,
        "diffraction_included_by_this_helper": False,
        "physical_accuracy_qualified": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "readout_or_isp_executed": False,
        "full_scale_normalization_reference": (
            "maximum observed absolute mean electrons over the two compared grids or "
            "optical variants and both edge phases, with 1e-12 floor; not sensor full well"
        ),
        "steps": [],
    }
    previous: dict[str, tuple[np.ndarray, np.ndarray] | None] = {
        "source": None, "candidate": None,
    }
    previous_signal_valid = {"source": False, "candidate": False}
    passes = {"source": 0, "candidate": 0}
    reference_physical: dict[str, Any] | None = None
    reference_sensor_identity: str | None = None
    try:
        for index, shape in enumerate(shapes):
            common = dict(runner_kwargs)
            common.update(first_scene_shape_row_col=shape, max_refinements=0)
            pairs = {}
            for label, optics in (("source", source_optics), ("candidate", candidate_optics)):
                capture, values = run_registered_roi_frequency_sampling(
                    optics, sensor, return_transient_means=True, **common
                )
                arrays = _capture_arrays(values)
                pairs[label] = (capture, arrays)
            source_result, source_means = pairs["source"]
            candidate_result, candidate_means = pairs["candidate"]
            step: dict[str, Any] = {"refinement": index, "scene_shape_row_col": list(shape)}
            report["steps"].append(step)
            if source_means is None or candidate_means is None:
                step["capture_status_by_optics"] = {
                    "source": source_result.get("status"),
                    "candidate": candidate_result.get("status"),
                }
                report["status"] = "inconclusive_joint_capture_unavailable"
                report["blocked_refinement"] = index
                break
            source_step = _phase_step(source_result, shape)
            candidate_step = _phase_step(candidate_result, shape)
            if (
                source_result.get("sensor_identity") != sensor_before
                or candidate_result.get("sensor_identity") != sensor_before
                or _physical_input(source_result, across_grids=False)
                != _physical_input(candidate_result, across_grids=False)
                or source_step.get("source_identity_by_phase")
                != candidate_step.get("source_identity_by_phase")
                or source_step.get("oi_pitch_um") != candidate_step.get("oi_pitch_um")
                or any(a.shape != b.shape for a, b in zip(
                    source_means, candidate_means, strict=True
                ))
            ):
                raise ValueError("Source/candidate captures used different physical inputs.")
            current_physical = _physical_input(source_result, across_grids=True)
            if reference_physical is None:
                reference_physical = current_physical
                reference_sensor_identity = source_result["sensor_identity"]
            elif (
                reference_physical != current_physical
                or reference_sensor_identity != source_result["sensor_identity"]
            ):
                raise ValueError("Grid refinement changed FOV, Scene or Sensor conditions.")
            step["oi_pitch_um"] = source_step["oi_pitch_um"]
            step["source_identity_by_phase"] = source_step["source_identity_by_phase"]
            step["capture_status_by_optics"] = {
                "source": source_result["status"],
                "candidate": candidate_result["status"],
            }
            step["mean_phase_signal_valid_by_optics"] = {
                "source": source_step.get("mean_phase_signal_valid") is True,
                "candidate": candidate_step.get("mean_phase_signal_valid") is True,
            }
            step["mean_electrons_sha256_by_phase_by_optics"] = {
                "source": source_step["mean_electrons_sha256_by_phase"],
                "candidate": candidate_step["mean_electrons_sha256_by_phase"],
            }
            step["source_vs_candidate_fixed_grid"] = {
                **_full_scale_delta(source_means, candidate_means),
                "edge_signal_valid_for_both_optics": all(
                    step["mean_phase_signal_valid_by_optics"].values()
                ),
                "independent_pupil_density_convergence_established": False,
            }
            for label, means in (("source", source_means), ("candidate", candidate_means)):
                earlier = previous[label]
                if earlier is not None:
                    delta = _full_scale_delta(earlier, means)
                    stable = (
                        previous_signal_valid[label]
                        and step["mean_phase_signal_valid_by_optics"][label]
                        and delta["mean_electron_full_scale_normalized_max_delta"] <= mean_tol
                        and delta["shift_response_full_scale_normalized_max_delta"] <= shift_tol
                    )
                    passes[label] = passes[label] + 1 if stable else 0
                    step[f"{label}_grid_delta_from_previous"] = {
                        **delta,
                        "within_declared_tolerances": stable,
                        "consecutive_passes": passes[label],
                    }
                previous[label] = means
                previous_signal_valid[label] = step["mean_phase_signal_valid_by_optics"][label]
            step["consecutive_refinement_passes_by_optics"] = dict(passes)
            report["frequency_target_points_status"] = (
                "inconclusive_mosaic_channel_contract"
                if source_result.get("frequency_target_points_status")
                == candidate_result.get("frequency_target_points_status")
                == "inconclusive_mosaic_channel_contract"
                else "not_assessed_by_joint_mean_phase_helper"
            )
        else:
            report["all_grids_executed"] = True
            if all(count >= 2 for count in passes.values()):
                report["status"] = (
                    "numeric_joint_source_and_candidate_scene_grid_mean_phase_"
                    "converged_model_only"
                )
                report["scene_grid_mean_phase_converged_for_both_optics"] = True
                report["selected_scene_shape_row_col"] = list(shapes[-1])
    finally:
        if (
            _optical_state_fingerprint(source_optics) != source_before
            or _optical_state_fingerprint(candidate_optics) != candidate_before
            or _sensor_identity(sensor) != sensor_before
        ):
            raise ValueError("Optical or Sensor model mutated during the joint ladder.")
    return report
