"""Same-physical-FOV refinement of a streamed off-axis sensor mean tile.

This is a numerical diagnostic for the selected strict ray-trace PSF and
supported linear sensor operator. It does not qualify the physical PSF, Scene
outside the declared finite raster, a product FOV, readout, or system MTF.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from typing import Any

import numpy as np

from pyisetcam.types import Sensor

from .lens_sensor_scene_geometry_tile import make_analytic_strict_geometry_provider
from .lens_sensor_streamed_analytic_capture import AnalyticCaptureResourcePolicy
from .lens_sensor_streamed_offaxis_mean import (
    compute_streamed_analytic_offaxis_mean,
    preflight_streamed_analytic_offaxis_mean,
)

SCHEMA = "camera_lens_sensor_streamed_offaxis_mean_convergence_v1"


def _shape(value: tuple[int, int]) -> tuple[int, int]:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or any(type(item) is not int or item < 16 or item % 2 for item in value)
    ):
        raise ValueError("First Scene grid requires two even integer dimensions >= 16.")
    return int(value[0]), int(value[1])


def _positive(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return result


def _array_digest(values: np.ndarray) -> str:
    return "sha256:" + hashlib.sha256(
        np.ascontiguousarray(values, dtype="<f8").tobytes()
    ).hexdigest()


def run_streamed_offaxis_mean_sampling_convergence(
    optics: dict[str, Any],
    sensor: Sensor,
    *,
    first_scene_shape_row_col: tuple[int, int],
    max_refinements: int,
    scene_horizontal_fov_deg: float,
    wavelengths_nm: np.ndarray,
    bright_side_photons: np.ndarray,
    analytic_slanted_edge_offset_deg: float,
    fixed_shift_deg: float,
    sensor_tile_bounds_row_col_exclusive: tuple[int, int, int, int],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    resource_policy: AnalyticCaptureResourcePolicy,
    mean_electron_relative_tolerance: float = 0.01,
    shift_response_relative_tolerance: float = 0.02,
    minimum_normalized_edge_contrast: float = 0.05,
    minimum_normalized_shift_response: float = 1e-4,
) -> dict[str, Any]:
    """Double both Scene axes at fixed FOV until two successive steps agree.

    The same physical edge is re-rasterized at every OI pitch. Two edge phases
    are preflighted before either is executed at a given grid. The virtual
    sensor, its global tile, CFA phase, exposure and spectral response remain
    fixed; the returned arrays are pre-readout mean electrons on that native
    tile. Grid agreement is only evidence about this selected numerical model.
    """
    first_shape = _shape(first_scene_shape_row_col)
    if type(max_refinements) is not int or not 2 <= max_refinements <= 8:
        raise ValueError("max_refinements must allow at least two and at most eight doublings.")
    fov = _positive(scene_horizontal_fov_deg, "Scene horizontal FOV")
    baseline_offset = float(analytic_slanted_edge_offset_deg)
    shift = _positive(fixed_shift_deg, "Fixed edge shift")
    if not math.isfinite(baseline_offset) or not all(
        abs(offset) <= fov / 2 for offset in (baseline_offset, baseline_offset + shift)
    ):
        raise ValueError("Both fixed edge phases must lie inside the horizontal half-FOV.")
    mean_tol = _positive(mean_electron_relative_tolerance, "Mean-electron tolerance")
    shift_tol = _positive(shift_response_relative_tolerance, "Shift-response tolerance")
    if mean_tol > 0.1 or shift_tol > 0.1:
        raise ValueError("Diagnostic convergence tolerances cannot exceed 10%.")
    edge_floor = _positive(minimum_normalized_edge_contrast, "Minimum edge contrast")
    shift_floor = _positive(minimum_normalized_shift_response, "Minimum shift response")
    if edge_floor >= 1 or shift_floor >= 1:
        raise ValueError("Minimum normalized signal thresholds must be below one.")

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "inconclusive_refinement_limit",
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "physical_psf_qualified": False,
        "physical_scene_halo_qualified": False,
        "full_frame_execution_qualified": False,
        "readout_or_isp_executed": False,
        "same_physical_scene": {
            "source_kind": "continuous_tangent_plane_slanted_half_plane_v1",
            "horizontal_fov_deg": fov,
            "aspect_ratio_row_col": [first_shape[0], first_shape[1]],
            "edge_offset_deg": baseline_offset,
            "fixed_shift_deg": shift,
            "wavelength_nm": np.asarray(wavelengths_nm, dtype=float).tolist(),
            "bright_side_photons": np.asarray(bright_side_photons, dtype=float).tolist(),
            "virtual_sensor_shape_row_col": list(sensor.fields["size"]),
            "sensor_pixel_pitch_um_row_col": (
                np.asarray(sensor.fields["pixel"]["size_m"], dtype=float) * 1e6
            ).tolist(),
            "sensor_exposure_s": float(
                np.asarray(sensor.fields["integration_time"]).reshape(-1)[0]
            ),
            "sensor_cfa_pattern": np.asarray(sensor.fields["pattern"], dtype=int).tolist(),
            "sensor_tile_bounds_row_col_exclusive": list(sensor_tile_bounds_row_col_exclusive),
            "source_tile_shape_row_col": list(source_tile_shape_row_col),
            "sample_angles_deg": np.asarray(sample_angles_deg, dtype=float).tolist(),
        },
        "numerical_model": "stored_strict_geometric_psf_plus_supported_pre_readout_sensor_mean",
        "required_consecutive_refinement_passes": 2,
        "tolerances": {
            "mean_electron_relative": mean_tol,
            "shift_response_relative": shift_tol,
            "minimum_normalized_edge_contrast": edge_floor,
            "minimum_normalized_shift_response": shift_floor,
        },
        "resource_policy": resource_policy.as_dict(),
        "resource_policy_is_hardware_limit": False,
        "steps": [],
    }
    previous: tuple[np.ndarray, np.ndarray] | None = None
    consecutive_passes = 0
    for refinement in range(max_refinements + 1):
        shape = (first_shape[0] * (1 << refinement), first_shape[1] * (1 << refinement))
        baseline = make_analytic_strict_geometry_provider(
            optics,
            scene_shape_row_col=shape,
            scene_horizontal_fov_deg=fov,
            wavelengths_nm=wavelengths_nm,
            bright_side_photons=bright_side_photons,
            analytic_slanted_edge_offset_deg=baseline_offset,
        )
        shifted = replace(baseline, edge_offset_deg=baseline_offset + shift)
        if refinement == 0:
            result["same_physical_scene"]["vertical_fov_deg"] = baseline.vertical_fov_deg
            result["same_physical_scene"]["full_optical_state_sha256"] = (
                baseline.optics_state_sha256
            )
        arguments = {
            "sensor_tile_bounds_row_col_exclusive": sensor_tile_bounds_row_col_exclusive,
            "source_tile_shape_row_col": source_tile_shape_row_col,
            "sample_angles_deg": sample_angles_deg,
            "resource_policy": resource_policy,
        }
        # Neither phase may execute if the other's support or budget is blocked.
        plans = [
            preflight_streamed_analytic_offaxis_mean(provider, sensor, **arguments)
            for provider in (baseline, shifted)
        ]
        step: dict[str, Any] = {
            "scene_shape_row_col": list(shape),
            "scene_horizontal_fov_deg": fov,
            "oi_pitch_um": baseline.oi_pitch_um,
            "source_identity_by_phase": [baseline.source_identity, shifted.source_identity],
            "preflight_status_by_phase": [plan["status"] for plan in plans],
            "sampled_psf_kernel_shape_row_col_by_phase": [
                plan.get("sampled_psf_kernel_shape_row_col") for plan in plans
            ],
            "numeric_source_halo_status_by_phase": [
                (plan.get("stored_psf_numeric_halo") or {}).get("status") for plan in plans
            ],
            "combined_policy_estimate_bytes_by_phase": [
                plan.get("combined_policy_estimate_bytes") for plan in plans
            ],
        }
        result["steps"].append(step)
        if any(plan["status"] != "ready_under_declared_policy" for plan in plans):
            result["status"] = "inconclusive_resource_or_support_limit"
            result["blocked_refinement"] = refinement
            step["block_reason_by_phase"] = [plan.get("reason") for plan in plans]
            return result

        captured = [
            compute_streamed_analytic_offaxis_mean(provider, sensor, **arguments)
            for provider in (baseline, shifted)
        ]
        pair = tuple(np.asarray(item[0], dtype=float) for item in captured)
        if (
            pair[0].ndim != 2
            or pair[0].shape != pair[1].shape
            or any(not np.isfinite(array).all() or np.any(array < 0) for array in pair)
        ):
            raise ValueError("Streamed off-axis means must be finite nonnegative native tiles.")
        step["mean_electrons_sha256_by_phase"] = [_array_digest(array) for array in pair]
        step["output_mean_electrons_shape_row_col"] = list(pair[0].shape)
        step["optical_roi_row_col_exclusive_by_phase"] = [
            item[1]["resource_preflight"]["optical_roi_row_col_exclusive"] for item in captured
        ]
        step["source_tiles_processed_by_phase"] = [
            item[1]["optical_execution"]["source_tiles_processed"] for item in captured
        ]
        scale = max(1e-12, *(float(np.max(np.abs(array))) for array in pair))
        contrasts = [
            float(np.percentile(array, 90) - np.percentile(array, 10)) / scale
            for array in pair
        ]
        response = pair[1] - pair[0]
        response_amplitude = float(np.max(np.abs(response))) / scale
        step["normalized_edge_contrast_by_phase"] = contrasts
        step["normalized_shift_response_amplitude"] = response_amplitude
        signal_valid = min(contrasts) >= edge_floor and response_amplitude >= shift_floor
        step["edge_and_shift_signal_valid"] = signal_valid
        if previous is not None:
            previous_scale = max(
                scale, *(float(np.max(np.abs(array))) for array in previous)
            )
            mean_delta = max(
                float(np.max(np.abs(current - old)))
                for current, old in zip(pair, previous, strict=True)
            ) / previous_scale
            shift_delta = (
                float(np.max(np.abs(response - (previous[1] - previous[0]))))
                / previous_scale
            )
            passed = signal_valid and mean_delta <= mean_tol and shift_delta <= shift_tol
            step["relative_change_from_previous"] = {
                "mean_electrons_max_both_phases": mean_delta,
                "shift_response_max": shift_delta,
            }
            step["declared_numerical_tolerance_met"] = bool(passed)
            consecutive_passes = consecutive_passes + 1 if passed else 0
            step["consecutive_passes"] = consecutive_passes
            if consecutive_passes == 2:
                result["status"] = "numeric_offaxis_mean_sampling_converged_model_only"
                return result
        else:
            step["consecutive_passes"] = 0
        previous = pair
    return result
