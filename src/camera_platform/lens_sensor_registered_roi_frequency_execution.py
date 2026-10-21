"""Bounded frequency-targeted refinement of one registered off-axis ROI.

The runner re-rasterizes one continuous physical slanted edge at successively
finer Scene/OI grids.  The strict stored PSF is also resampled at each grid, so
a passing result is *joint Scene plus executed-PSF-grid* convergence for this
numerical model, never independent source-PSF or physical lens qualification.
Only a uniform single-site (monochrome) sensor has the channel contract needed
by the current slanted-edge frequency estimator.  The convergence gate tests
only declared discrete frequency points, never every frequency between them.
Bayer still receives an executable mean/phase study, but its frequency result
remains inconclusive.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from typing import Any

import numpy as np

from pyisetcam.iso import iso12233
from pyisetcam.types import Sensor

from .lens_sensor_frequency_band_convergence import compare_frequency_band_mtf_curves
from .lens_sensor_scene_geometry_tile import make_analytic_strict_geometry_provider
from .lens_sensor_streamed_analytic_capture import AnalyticCaptureResourcePolicy
from .lens_sensor_streamed_full_raw import _sensor_identity
from .lens_sensor_streamed_offaxis_mean import (
    _compute_preflighted_streamed_analytic_offaxis_mean,
    preflight_streamed_analytic_offaxis_mean,
)

SCHEMA = "camera_lens_sensor_registered_roi_frequency_execution_v1"
_REGISTERED_MAX_SCENE_AXIS = 65_536
_REGISTERED_MAX_VIRTUAL_CELLS = 536_870_912
EXTENDED_SPARSE_ROI_COORDINATE_POLICY = "extended_sparse_roi_128k_v1"
EXTENDED_SPARSE_ROI_MAX_SCENE_AXIS = 131_072
EXTENDED_SPARSE_ROI_MAX_VIRTUAL_CELLS = 2_147_483_648


def _positive(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite and positive.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be finite and positive.")
    return number


def _array_digest(values: np.ndarray) -> str:
    return "sha256:" + hashlib.sha256(
        np.ascontiguousarray(values, dtype="<f8").tobytes()
    ).hexdigest()


def _edge_curve(
    mean_electrons: np.ndarray,
    *,
    pixel_pitch_um_col: float,
    minimum_normalized_signal_contrast: float,
) -> dict[str, Any]:
    """Measure a horizontal-axis SFR only when a mono edge is identifiable.

    The fit guard is intentionally conservative.  It rejects small tiles,
    flat input, edges clipped by the ROI, nearly horizontal edges, and curved
    edges for which a single local line would be misleading.  A successful
    result is a numerical ISO12233 estimate for this ROI, not certified ISO
    chart acquisition or a calibrated product MTF.
    """
    image = np.asarray(mean_electrons, dtype=float)
    result: dict[str, Any] = {
        "status": "inconclusive_edge_frequency_support",
        "estimator": "iso12233_mono_pre_readout_mean_with_local_edge_guard",
        "orientation": "image_plane_column_x_right",
    }
    if image.ndim != 2 or min(image.shape) < 48:
        result["reason"] = "A monochrome edge tile needs at least 48 pixels on both axes."
        return result
    if not np.isfinite(image).all() or np.any(image < 0):
        raise ValueError("Mean-electron tile must be finite and nonnegative.")
    scale = max(float(np.max(image)), 1e-12)
    contrast = float(np.percentile(image, 90) - np.percentile(image, 10)) / scale
    result["normalized_signal_contrast"] = contrast
    if contrast < minimum_normalized_signal_contrast:
        result["reason"] = "The output edge has insufficient unnormalized contrast."
        return result

    # Each row must see the same interior edge.  This check is independent of
    # the ISO helper's own line fit, which can return a finite curve for an
    # unrelated gradient or an edge clipped by the supplied ROI.
    derivative = np.diff(image, axis=1)
    margin = max(4, image.shape[1] // 12)
    interior = np.abs(derivative[:, margin:-margin])
    if interior.shape[1] < 8:
        result["reason"] = "The edge leaves too little interior support."
        return result
    peak = np.max(interior, axis=1)
    if np.min(peak) / scale < max(0.005, minimum_normalized_signal_contrast / 10):
        result["reason"] = "One or more rows lack an identifiable edge."
        return result
    centers = margin + np.argmax(interior, axis=1)
    rows = np.arange(image.shape[0], dtype=float)
    slope, intercept = np.polyfit(rows, centers.astype(float), 1)
    residual = centers - (slope * rows + intercept)
    span = abs(slope) * (image.shape[0] - 1)
    result["edge_fit"] = {
        "slope_column_per_row": float(slope),
        "span_columns": float(span),
        "max_abs_residual_columns": float(np.max(np.abs(residual))),
    }
    if span < 2 or span > image.shape[1] / 3 or np.max(np.abs(residual)) > 0.75:
        result["reason"] = "The local edge lacks stable slant or straight-line support."
        return result
    if np.min(centers) < margin + 2 or np.max(centers) > image.shape[1] - margin - 3:
        result["reason"] = "The edge touches the guarded ROI boundary."
        return result
    try:
        estimate = iso12233(image, delta_x=pixel_pitch_um_col * 1e-3)
    except (ValueError, IndexError, FloatingPointError, ZeroDivisionError) as exc:
        result["reason"] = f"The slanted-edge estimator rejected this tile: {exc}"
        return result
    frequencies = np.asarray(estimate.freq, dtype=float).reshape(-1)
    mtf = np.asarray(estimate.mtf, dtype=float).reshape(-1)
    if (
        frequencies.size < 3 or mtf.size != frequencies.size
        or not np.isfinite(frequencies).all() or not np.isfinite(mtf).all()
        or np.any(np.diff(frequencies) <= 0) or np.any(mtf < 0)
        or abs(mtf[0] - 1) > 1e-6
    ):
        result["reason"] = "The edge estimator did not return a finite normalized curve."
        return result
    result.update({
        "status": "numeric_mono_edge_curve_available",
        "frequency_cycles_per_mm": frequencies.tolist(),
        "mtf": mtf.tolist(),
        "frequency_support_cycles_per_mm": [float(frequencies[0]), float(frequencies[-1])],
        "sensor_aperture_and_supported_mean_operator_included": True,
        "physical_mtf_qualified": False,
    })
    return result


def run_registered_roi_frequency_sampling(
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
    target_frequency_cycles_per_mm: tuple[float, ...] | list[float] | np.ndarray,
    source_rasterization: str = "cell_area_integrated",
    mean_electron_full_scale_normalized_max_delta_tolerance: float = 0.005,
    shift_response_full_scale_normalized_max_delta_tolerance: float = 0.01,
    maximum_absolute_mtf_delta: float = 0.01,
    minimum_normalized_edge_contrast: float = 0.05,
    minimum_normalized_shift_response: float = 1e-4,
    max_scene_axis: int = _REGISTERED_MAX_SCENE_AXIS,
    max_virtual_scene_cells: int = _REGISTERED_MAX_VIRTUAL_CELLS,
    coordinate_policy: str = "registered_roi_v1",
    return_transient_means: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], tuple[np.ndarray, np.ndarray] | None]:
    """Execute a bounded fixed-FOV grid ladder on one native sensor ROI.

    ``max_refinements=0`` executes one explicit grid without a convergence
    claim.  Adaptive convergence needs two successful *consecutive* 2x
    comparisons.  All phase plans are admitted before either phase executes.
    A Bayer tile cannot be treated as a monochrome SFR measurement; its target
    point status remains inconclusive even if mean/phase arrays become stable.
    """
    shape = first_scene_shape_row_col
    if (
        not isinstance(shape, (tuple, list)) or len(shape) != 2
        or any(type(axis) is not int or axis < 32 or axis % 2 for axis in shape)
    ):
        raise ValueError("The first Scene grid needs two even integer axes >= 32.")
    if type(max_refinements) is not int or max_refinements not in {0, *range(2, 9)}:
        raise ValueError("Use zero for one explicit grid or 2..8 adaptive refinements.")
    if coordinate_policy == "registered_roi_v1":
        valid_coordinate_limits = (
            type(max_scene_axis) is int
            and 32 <= max_scene_axis <= _REGISTERED_MAX_SCENE_AXIS
            and type(max_virtual_scene_cells) is int
            and 1024 <= max_virtual_scene_cells <= _REGISTERED_MAX_VIRTUAL_CELLS
        )
    elif coordinate_policy == EXTENDED_SPARSE_ROI_COORDINATE_POLICY:
        valid_coordinate_limits = (
            type(max_scene_axis) is int
            and max_scene_axis == EXTENDED_SPARSE_ROI_MAX_SCENE_AXIS
            and type(max_virtual_scene_cells) is int
            and max_virtual_scene_cells == EXTENDED_SPARSE_ROI_MAX_VIRTUAL_CELLS
        )
    else:
        valid_coordinate_limits = False
    if not valid_coordinate_limits:
        raise ValueError("Scene limits must match the declared frequency ROI coordinate policy.")
    if not isinstance(resource_policy, AnalyticCaptureResourcePolicy):
        raise ValueError("An explicit resource policy is required.")
    if type(return_transient_means) is not bool:
        raise ValueError("Transient mean return must be a boolean opt-in.")
    if return_transient_means:
        bounds = sensor_tile_bounds_row_col_exclusive
        if (
            not isinstance(bounds, (tuple, list)) or len(bounds) != 4
            or any(type(value) is not int for value in bounds)
            or not 0 < bounds[1] - bounds[0] <= 128
            or not 0 < bounds[3] - bounds[2] <= 128
        ):
            raise ValueError("Transient means require a registered ROI no larger than 128x128.")
    fov = _positive(scene_horizontal_fov_deg, "Horizontal FOV")
    if fov >= 180:
        raise ValueError("Horizontal FOV must be below 180 degrees.")
    edge_offset = float(analytic_slanted_edge_offset_deg)
    shift = _positive(fixed_shift_deg, "Fixed edge shift")
    if not math.isfinite(edge_offset) or any(
        abs(value) > fov / 2 for value in (edge_offset, edge_offset + shift)
    ):
        raise ValueError("Both fixed edge phases must lie inside the Scene half-FOV.")
    if source_rasterization not in {"point_sampled", "cell_area_integrated"}:
        raise ValueError("Declare a supported analytic Scene rasterization.")
    mean_tol = _positive(
        mean_electron_full_scale_normalized_max_delta_tolerance,
        "Full-scale-normalized mean-electron max-delta tolerance",
    )
    shift_tol = _positive(
        shift_response_full_scale_normalized_max_delta_tolerance,
        "Full-scale-normalized shift-response max-delta tolerance",
    )
    if max(mean_tol, shift_tol) > 0.1:
        raise ValueError("Mean and phase tolerances cannot exceed 10%.")
    mtf_tol = float(maximum_absolute_mtf_delta)
    if not math.isfinite(mtf_tol) or not 0 <= mtf_tol <= 0.1:
        raise ValueError("MTF tolerance must be finite and within [0, 0.1].")
    edge_floor = _positive(minimum_normalized_edge_contrast, "Minimum edge contrast")
    phase_floor = _positive(minimum_normalized_shift_response, "Minimum phase response")
    if edge_floor >= 1 or phase_floor >= 1:
        raise ValueError("Signal floors must be below one.")
    if not isinstance(sensor, Sensor):
        raise ValueError("A loaded Sensor is required.")
    pixel = np.asarray(sensor.fields.get("pixel", {}).get("size_m"), dtype=float).reshape(-1)
    if pixel.size != 2 or not np.isfinite(pixel).all() or np.any(pixel <= 0):
        raise ValueError("Sensor needs two finite positive pixel pitches.")
    pixel_um = pixel * 1e6
    nyquist = float(500 / pixel_um[1])
    target = np.asarray(target_frequency_cycles_per_mm, dtype=float)
    if (
        target.ndim != 1 or target.size < 2 or not np.isfinite(target).all()
        or target[0] <= 0 or target[-1] >= nyquist or np.any(np.diff(target) <= 0)
    ):
        raise ValueError("Declare at least two increasing target frequencies below column Nyquist.")
    pattern = np.asarray(sensor.fields.get("pattern"))
    mono = (
        pattern.ndim == 2 and pattern.size > 0
        and np.issubdtype(pattern.dtype, np.integer)
        and np.all(pattern == pattern.flat[0])
    )
    square_pixel = math.isclose(float(pixel_um[0]), float(pixel_um[1]), rel_tol=1e-9)
    bounds = sensor_tile_bounds_row_col_exclusive
    roi_frequency_support = (
        isinstance(bounds, (tuple, list)) and len(bounds) == 4
        and all(type(value) is int for value in bounds)
        and bounds[1] - bounds[0] >= 48
        and bounds[3] - bounds[2] >= 48
    )
    frequency_capable = mono and square_pixel and roi_frequency_support
    frequency_block = (
        None if frequency_capable
        else "inconclusive_roi_frequency_support" if mono and not roi_frequency_support
        else "inconclusive_anisotropic_pixel_edge_geometry" if mono
        else "inconclusive_mosaic_channel_contract"
    )
    sensor_identity = _sensor_identity(sensor)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "inconclusive_refinement_limit",
        "mean_phase_status": "not_assessed",
        "frequency_target_points_status": (
            "not_assessed" if frequency_capable else frequency_block
        ),
        "psf_source_status": "inconclusive_independent_psf_source_refinement_not_run",
        "observable": "mosaic_pre_readout_mean_electrons",
        "frequency_observable_when_supported": "uniform_single_site_pre_readout_mean_electrons",
        "frequency_direction": "image_plane_column_x_right",
        "frequency_coverage_scope": "declared_discrete_frequency_points_only",
        "continuous_frequency_band_between_targets_assessed": False,
        "target_frequency_cycles_per_mm": target.tolist(),
        "native_nyquist_cycles_per_mm_column": nyquist,
        "pixel_pitch_um_row_col": pixel_um.tolist(),
        "mono_frequency_estimator_support": frequency_capable,
        "roi_minimum_48_by_48_frequency_support": roi_frequency_support,
        "sensor_identity": sensor_identity,
        "fixed_physical_input": {
            "source_kind": "continuous_tangent_plane_slanted_half_plane_v1",
            "source_rasterization": source_rasterization,
            "scene_horizontal_fov_deg": fov,
            "reference_scene_aspect_row_col": list(shape),
            "baseline_edge_offset_deg": edge_offset,
            "fixed_shift_deg": shift,
            "wavelength_nm": np.asarray(wavelengths_nm, dtype=float).tolist(),
            "bright_side_photons": np.asarray(bright_side_photons, dtype=float).tolist(),
            "sensor_global_shape_row_col": list(sensor.fields["size"]),
            "sensor_tile_bounds_row_col_exclusive": list(sensor_tile_bounds_row_col_exclusive),
            "sensor_exposure_s": float(
                np.asarray(sensor.fields["integration_time"]).reshape(-1)[0]
            ),
            "sensor_cfa_pattern": pattern.tolist(),
            "source_tile_shape_row_col": list(source_tile_shape_row_col),
            "sample_angles_deg": np.asarray(sample_angles_deg, dtype=float).tolist(),
        },
        "tolerances": {
            "mean_electron_full_scale_normalized_max_delta_tolerance": mean_tol,
            "shift_response_full_scale_normalized_max_delta_tolerance": shift_tol,
            "maximum_absolute_mtf_delta_at_target_points": mtf_tol,
            "minimum_normalized_edge_contrast": edge_floor,
            "minimum_normalized_phase_response": phase_floor,
        },
        "full_scale_normalization_reference": (
            "maximum absolute observed mean electrons across both compared grids and "
            "both edge phases, with a 1e-12 electron floor; not sensor full well"
        ),
        "local_signal_relative_mean_error_assessed": False,
        "required_consecutive_refinement_passes": 2,
        "max_refinements": max_refinements,
        "resource_policy": resource_policy.as_dict(),
        "max_scene_axis_policy": max_scene_axis,
        "max_virtual_scene_cells_policy": max_virtual_scene_cells,
        "policy_is_hardware_limit": False,
        "joint_scene_and_executed_psf_grid_refinement": True,
        "independent_psf_source_convergence_established": False,
        "frequency_magnitude_only": True,
        "complex_phase_transfer_assessed": False,
        "above_native_nyquist_alias_response_assessed": False,
        "exact_native_nyquist_endpoint_assessed": False,
        "physical_scene_halo_qualified": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "readout_or_isp_executed": False,
        "steps": [],
    }
    if coordinate_policy == EXTENDED_SPARSE_ROI_COORDINATE_POLICY:
        result["coordinate_policy"] = coordinate_policy
    previous_means: tuple[np.ndarray, np.ndarray] | None = None
    previous_curves: tuple[dict[str, Any], dict[str, Any]] | None = None
    mean_passes = 0
    joint_passes = 0

    def finished() -> dict[str, Any] | tuple[
        dict[str, Any], tuple[np.ndarray, np.ndarray] | None
    ]:
        if not return_transient_means:
            return result
        if previous_means is None:
            return result, None
        copies = tuple(np.array(mean, dtype=float, copy=True) for mean in previous_means)
        for mean in copies:
            mean.setflags(write=False)
        return result, copies

    for refinement in range(max_refinements + 1):
        grid = (shape[0] * (1 << refinement), shape[1] * (1 << refinement))
        if max(grid) > max_scene_axis or grid[0] * grid[1] > max_virtual_scene_cells:
            result.update({
                "status": "inconclusive_scene_grid_policy",
                "blocked_refinement": refinement,
                "blocked_scene_shape_row_col": list(grid),
            })
            break
        baseline = make_analytic_strict_geometry_provider(
            optics,
            scene_shape_row_col=grid,
            scene_horizontal_fov_deg=fov,
            wavelengths_nm=wavelengths_nm,
            bright_side_photons=bright_side_photons,
            analytic_slanted_edge_offset_deg=edge_offset,
            source_rasterization=source_rasterization,
        )
        shifted = replace(baseline, edge_offset_deg=edge_offset + shift)
        if refinement == 0:
            result["fixed_physical_input"]["vertical_fov_deg"] = baseline.vertical_fov_deg
            result["fixed_physical_input"]["optics_state_sha256"] = baseline.optics_state_sha256
            width_um = baseline.oi_pitch_um * grid[1]
            image_distance_um = width_um / (2 * math.tan(math.radians(fov) / 2))
            phase_shift_um = image_distance_um * (
                math.tan(math.radians(edge_offset + shift))
                - math.tan(math.radians(edge_offset))
            )
            if not 0 < phase_shift_um < float(pixel_um[1]):
                raise ValueError(
                    "Fixed edge phase shift must be subpixel on the source tangent plane."
                )
            result["fixed_physical_input"]["edge_phase_shift_tangent_plane_um"] = (
                float(phase_shift_um)
            )
            result["fixed_physical_input"]["distorted_local_edge_shift_qualified"] = False
        elif (
            baseline.vertical_fov_deg != result["fixed_physical_input"]["vertical_fov_deg"]
            or baseline.optics_state_sha256 != result["fixed_physical_input"]["optics_state_sha256"]
        ):
            raise ValueError("Grid refinement changed the physical FOV or optical model.")
        step: dict[str, Any] = {
            "refinement": refinement,
            "scene_shape_row_col": list(grid),
            "scene_horizontal_fov_deg": fov,
            "oi_pitch_um": float(baseline.oi_pitch_um),
            "executed_psf_convolution_pitch_um": float(baseline.oi_pitch_um),
            "source_identity_by_phase": [baseline.source_identity, shifted.source_identity],
        }
        result["steps"].append(step)
        # The registered ROI Worker imposes this additional contract.  A
        # diagnostic must never quietly run a coarser Scene than the Worker
        # would accept, even when its lower-level tile preflight permits it.
        if baseline.oi_pitch_um > float(np.min(pixel_um)) * (1 + 1e-9):
            step["preflight_status_by_phase"] = [
                "blocked_scene_below_native_pixel_floor"
            ] * 2
            step["block_reason_by_phase"] = [
                "Scene/OI pitch exceeds the smallest native sensor pixel pitch."
            ] * 2
            result.update({
                "status": "inconclusive_scene_below_native_pixel_floor",
                "blocked_refinement": refinement,
            })
            break
        arguments = {
            "sensor_tile_bounds_row_col_exclusive": sensor_tile_bounds_row_col_exclusive,
            "source_tile_shape_row_col": source_tile_shape_row_col,
            "sample_angles_deg": sample_angles_deg,
            "resource_policy": resource_policy,
        }
        plans = [
            preflight_streamed_analytic_offaxis_mean(provider, sensor, **arguments)
            for provider in (baseline, shifted)
        ]
        step.update({
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
        })
        if any(plan["status"] != "ready_under_declared_policy" for plan in plans):
            result.update({
                "status": "inconclusive_resource_or_halo_support",
                "blocked_refinement": refinement,
            })
            step["block_reason_by_phase"] = [plan.get("reason") for plan in plans]
            break
        captured = [
            _compute_preflighted_streamed_analytic_offaxis_mean(
                provider, sensor, admitted_plan=plan, **arguments
            )
            for provider, plan in zip((baseline, shifted), plans, strict=True)
        ]
        means = tuple(np.asarray(item[0], dtype=float) for item in captured)
        if (
            means[0].ndim != 2 or means[0].shape != means[1].shape
            or any(not np.isfinite(mean).all() or np.any(mean < 0) for mean in means)
        ):
            raise ValueError("Streamed means must be finite nonnegative native tiles.")
        if _sensor_identity(sensor) != sensor_identity:
            raise ValueError("Sensor model changed during fixed-condition refinement.")
        step["mean_electrons_sha256_by_phase"] = [_array_digest(mean) for mean in means]
        step["output_mean_electrons_shape_row_col"] = list(means[0].shape)
        result["last_executed_scene_shape_row_col"] = list(grid)
        step["source_tiles_processed_by_phase"] = [
            item[1]["optical_execution"]["source_tiles_processed"] for item in captured
        ]
        scale = max(1e-12, *(float(np.max(np.abs(mean))) for mean in means))
        contrasts = [
            float(np.percentile(mean, 90) - np.percentile(mean, 10)) / scale
            for mean in means
        ]
        phase_response = means[1] - means[0]
        phase_amplitude = float(np.max(np.abs(phase_response))) / scale
        signal_valid = min(contrasts) >= edge_floor and phase_amplitude >= phase_floor
        step["normalized_edge_contrast_by_phase"] = contrasts
        step["normalized_phase_response_amplitude"] = phase_amplitude
        step["mean_phase_signal_valid"] = bool(signal_valid)

        curves: tuple[dict[str, Any], dict[str, Any]] | None = None
        if frequency_capable:
            curves = tuple(
                _edge_curve(
                    mean,
                    pixel_pitch_um_col=float(pixel_um[1]),
                    minimum_normalized_signal_contrast=edge_floor,
                )
                for mean in means
            )
            step["frequency_curve_status_by_phase"] = [curve["status"] for curve in curves]
            if any(curve["status"] != "numeric_mono_edge_curve_available" for curve in curves):
                frequency_block = "inconclusive_edge_frequency_support"
                step["frequency_curve_reason_by_phase"] = [
                    curve.get("reason") for curve in curves
                ]
        else:
            step["frequency_curve_status_by_phase"] = [
                frequency_block
            ] * 2
        if previous_means is not None:
            previous_scale = max(
                scale, *(float(np.max(np.abs(mean))) for mean in previous_means)
            )
            mean_delta = max(
                float(np.max(np.abs(current - prior)))
                for current, prior in zip(means, previous_means, strict=True)
            ) / previous_scale
            phase_delta = float(np.max(np.abs(
                phase_response - (previous_means[1] - previous_means[0])
            ))) / previous_scale
            mean_pass = signal_valid and mean_delta <= mean_tol and phase_delta <= shift_tol
            mean_passes = mean_passes + 1 if mean_pass else 0
            step["full_scale_normalized_max_delta_from_previous"] = {
                "mean_electron_full_scale_normalized_max_delta": mean_delta,
                "shift_response_full_scale_normalized_max_delta": phase_delta,
                "full_scale_electrons_denominator": previous_scale,
            }
            step["mean_phase_tolerance_met"] = bool(mean_pass)
            step["consecutive_mean_phase_passes"] = mean_passes
            if mean_passes >= 2:
                result["mean_phase_status"] = (
                    "numeric_joint_scene_oi_grid_mean_phase_converged_model_only"
                )
                result["mean_phase_converged_scene_shape_row_col"] = list(grid)
            else:
                # A later failed comparison revokes an earlier two-pass
                # result for the terminal grid. Historical passes remain in
                # steps, but cannot be presented as current convergence.
                result["mean_phase_status"] = "inconclusive_mean_phase_refinement"
                result.pop("mean_phase_converged_scene_shape_row_col", None)

            band_pass = False
            if curves is not None and previous_curves is not None and all(
                curve["status"] == "numeric_mono_edge_curve_available"
                for curve in (*curves, *previous_curves)
            ):
                comparisons = [
                    compare_frequency_band_mtf_curves(
                        prior["frequency_cycles_per_mm"], prior["mtf"],
                        current["frequency_cycles_per_mm"], current["mtf"],
                        target_frequency_cycles_per_mm=target,
                        native_nyquist_cycles_per_mm=nyquist,
                        maximum_absolute_mtf_delta=mtf_tol,
                        coarse_normalized_signal_contrast=prior["normalized_signal_contrast"],
                        fine_normalized_signal_contrast=current["normalized_signal_contrast"],
                        minimum_normalized_signal_contrast=edge_floor,
                    )
                    for prior, current in zip(previous_curves, curves, strict=True)
                ]
                step["frequency_target_points_comparison_by_phase"] = comparisons
                band_pass = all(
                    comparison.get("within_declared_single_step_tolerance") is True
                    for comparison in comparisons
                )
                if any(
                    comparison["status"].startswith("inconclusive")
                    for comparison in comparisons
                ):
                    frequency_block = "inconclusive_frequency_curve_target_support"
            step["frequency_target_points_tolerance_met"] = (
                bool(band_pass) if frequency_capable else None
            )
            joint_passes = joint_passes + 1 if mean_pass and band_pass else 0
            step["consecutive_joint_target_point_passes"] = joint_passes
            if joint_passes >= 2:
                previous_means = means
                result.update({
                    "status": (
                        "numeric_joint_scene_oi_grid_discrete_frequency_targets_"
                        "converged_model_only"
                    ),
                    "mean_phase_status": (
                        "numeric_joint_scene_oi_grid_mean_phase_converged_model_only"
                    ),
                    "frequency_target_points_status": (
                        "numeric_mono_discrete_frequency_targets_converged_model_only"
                    ),
                    "selected_scene_shape_row_col": list(grid),
                })
                return finished()
            if mean_passes >= 2 and not frequency_capable:
                previous_means = means
                result.update({
                    "status": (
                        "inconclusive_frequency_mosaic_channel_contract"
                        if not mono else frequency_block
                    ),
                    "selected_mean_phase_scene_shape_row_col": list(grid),
                })
                return finished()
        else:
            step["consecutive_mean_phase_passes"] = 0
            step["consecutive_joint_target_point_passes"] = 0
        previous_means = means
        previous_curves = curves
    if result["mean_phase_status"] == "not_assessed":
        result["mean_phase_status"] = (
            "inconclusive_single_grid_no_refinement" if max_refinements == 0
            else "inconclusive_mean_phase_refinement"
        )
    if frequency_capable and result["frequency_target_points_status"] == "not_assessed":
        result["frequency_target_points_status"] = (
            frequency_block or (
                "inconclusive_single_grid_no_refinement" if max_refinements == 0
                else "inconclusive_discrete_frequency_target_refinement_limit"
            )
        )
    return finished()
