"""Plan fixed-FOV Scene sampling for an explicitly declared frequency band.

This is a cheap numerical *starting-grid* planner, not a convergence test.
The strict Lens x Sensor solver resamples its pre-pixel PSF onto the Scene/OI
grid. A finer executed PSF grid cannot restore information absent from the
stored ray histogram. Callers must separately qualify the PSF source and run
fixed-physical-Scene refinements before making a band-accuracy claim.
"""

from __future__ import annotations

import math
from typing import Any

SCHEMA = "camera_lens_sensor_frequency_sampling_plan_v1"


def _positive(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite and positive.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be finite and positive.")
    return number


def _shape(value: tuple[int, int], label: str) -> tuple[int, int]:
    if (
        not isinstance(value, (tuple, list)) or len(value) != 2
        or any(type(axis) is not int or axis < 1 for axis in value)
    ):
        raise ValueError(f"{label} requires two positive integer axes.")
    return value[0], value[1]


def _pair(value: tuple[float, float], label: str) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(f"{label} requires row and column values.")
    return _positive(value[0], f"{label} row"), _positive(value[1], f"{label} column")


def _same_aspect(shape: tuple[int, int], reference: tuple[int, int]) -> bool:
    return shape[0] * reference[1] == shape[1] * reference[0]


def _first_even_aspect_grid(
    reference: tuple[int, int], required_cols: int,
) -> tuple[int, int]:
    divisor = math.gcd(*reference)
    unit_rows, unit_cols = reference[0] // divisor, reference[1] // divisor
    multiplier = max(
        math.ceil(required_cols / unit_cols),
        math.ceil(16 / unit_rows), math.ceil(16 / unit_cols),
    )
    if unit_rows % 2 or unit_cols % 2:
        multiplier += multiplier % 2
    return unit_rows * multiplier, unit_cols * multiplier


def _first_grid_for_ladder(
    reference: tuple[int, int], target_grid: tuple[int, int], refinements: int,
) -> tuple[int, int]:
    """Put the frequency seed at the finest rung, preserving exact aspect."""
    divisor = math.gcd(*reference)
    unit_rows, unit_cols = reference[0] // divisor, reference[1] // divisor
    target_multiplier = target_grid[0] // unit_rows
    multiplier = max(
        math.ceil(target_multiplier / 2**refinements),
        math.ceil(16 / unit_rows), math.ceil(16 / unit_cols),
    )
    if unit_rows % 2 or unit_cols % 2:
        multiplier += multiplier % 2
    return unit_rows * multiplier, unit_cols * multiplier


def plan_frequency_sampling(
    *,
    native_pixel_pitch_um_row_col: tuple[float, float],
    stored_psf_pitch_um_row_col: tuple[float, float],
    image_distance_m: float,
    scene_horizontal_fov_deg: float,
    reference_scene_shape_row_col: tuple[int, int],
    target_fraction_native_nyquist: float = 1.0,
    minimum_scene_samples_per_target_period: int = 4,
    alias_probe_multiple_native_nyquist: float | None = None,
    minimum_scene_samples_per_alias_period: int = 4,
    manual_scene_shape_row_col: tuple[int, int] | None = None,
    max_refinements: int = 2,
    max_scene_axis: int = 65_536,
    max_virtual_scene_cells: int = 536_870_912,
) -> dict[str, Any]:
    """Choose a search seed and enumerate affordable 2x fixed-FOV grids.

    Frequencies are image-plane cycles/mm. The Scene is parameterized on the
    undistorted tangent plane used by the current analytic ROI provider, so
    field-dependent distortion may change local object-angle sampling. The
    heuristic samples-per-period and alias probe only seed convergence; they
    are never presented as achieved optical or physical accuracy.
    """
    pixel = _pair(native_pixel_pitch_um_row_col, "Native pixel pitch")
    psf = _pair(stored_psf_pitch_um_row_col, "Stored PSF pitch")
    distance = _positive(image_distance_m, "Image distance")
    fov = _positive(scene_horizontal_fov_deg, "Scene horizontal FOV")
    if fov >= 180:
        raise ValueError("Scene horizontal FOV must be below 180 degrees.")
    reference = _shape(reference_scene_shape_row_col, "Reference Scene shape")
    fraction = _positive(target_fraction_native_nyquist, "Target Nyquist fraction")
    if fraction > 1:
        raise ValueError("Target fraction cannot exceed native sampling Nyquist.")
    samples = minimum_scene_samples_per_target_period
    if type(samples) is not int or not 2 <= samples <= 32:
        raise ValueError("Declare 2..32 Scene samples per target period.")
    alias_samples = minimum_scene_samples_per_alias_period
    if type(alias_samples) is not int or not 3 <= alias_samples <= 32:
        raise ValueError("Declare 3..32 Scene samples per alias-probe period.")
    alias_multiple = None
    if alias_probe_multiple_native_nyquist is not None:
        alias_multiple = _positive(
            alias_probe_multiple_native_nyquist, "Alias probe multiple"
        )
        if not 1 < alias_multiple <= 8:
            raise ValueError("Alias probe multiple must be within (1, 8].")
    if type(max_refinements) is not int or not 2 <= max_refinements <= 8:
        raise ValueError("At least two and at most eight refinements are required.")
    if type(max_scene_axis) is not int or max_scene_axis < 16:
        raise ValueError("Maximum Scene axis must be an integer >= 16.")
    if type(max_virtual_scene_cells) is not int or max_virtual_scene_cells < 256:
        raise ValueError("Maximum virtual Scene cells must be an integer >= 256.")

    width_um = 2 * distance * 1e6 * math.tan(math.radians(fov) / 2)
    if not math.isfinite(width_um) or width_um <= 0:
        raise ValueError("Projected tangent-plane extent is not finite and positive.")
    height_um = width_um * reference[0] / reference[1]
    native_nyquist = tuple(500 / axis for axis in pixel)
    target = tuple(fraction * axis for axis in native_nyquist)
    alias = (
        tuple(alias_multiple * axis for axis in native_nyquist)
        if alias_multiple is not None else None
    )
    # 1000 converts mm to um. Both axes share one OI pitch in this provider.
    target_period_seed = min(1000 / (samples * frequency) for frequency in target)
    alias_period_seed = (
        min(1000 / (alias_samples * frequency) for frequency in alias)
        if alias is not None else math.inf
    )
    desired_pitch_um = min(target_period_seed, alias_period_seed)
    required_cols = math.ceil(width_um / desired_pitch_um)
    target_grid = _first_even_aspect_grid(reference, required_cols)
    coarse_ladder_start = _first_grid_for_ladder(reference, target_grid, max_refinements)
    # The registered ROI Worker currently rejects Scene pitch coarser than a
    # native pixel. A mathematical convergence ladder that cannot execute is
    # not a useful automatic starting recommendation for this workflow.
    native_grid = _first_even_aspect_grid(reference, math.ceil(width_um / min(pixel)))
    suggested = max((coarse_ladder_start, native_grid), key=lambda shape: shape[1])
    if manual_scene_shape_row_col is None:
        selected = suggested
        mode = "adaptive_starting_grid"
    else:
        selected = _shape(manual_scene_shape_row_col, "Manual Scene shape")
        if any(axis < 16 or axis % 2 for axis in selected):
            raise ValueError("Manual Scene axes must be even integers >= 16.")
        if not _same_aspect(selected, reference):
            raise ValueError("Manual Scene aspect changes the fixed physical vertical FOV.")
        mode = "explicit_grid"

    def within_policy(shape: tuple[int, int]) -> bool:
        return (max(shape) <= max_scene_axis
                and shape[0] * shape[1] <= max_virtual_scene_cells)

    grids = []
    for refinement in range(max_refinements + 1):
        shape = (selected[0] * 2**refinement, selected[1] * 2**refinement)
        if not within_policy(shape):
            break
        grids.append({
            "scene_shape_row_col": list(shape),
            "tangent_plane_pitch_um": width_um / shape[1],
            "virtual_scene_cells": shape[0] * shape[1],
        })
    selected_pitch = width_um / selected[1]
    psf_nyquist = tuple(500 / axis for axis in psf)
    first_seed_rung = next((
        index for index, grid in enumerate(grids)
        if grid["tangent_plane_pitch_um"] <= desired_pitch_um
    ), None)
    return {
        "schema": SCHEMA,
        "status": (
            "inconclusive_registered_scene_below_native_pixel"
            if selected_pitch > min(pixel)
            else "planning_only" if len(grids) == max_refinements + 1
            else "inconclusive_refinement_policy" if grids
            else "inconclusive_starting_grid_policy"
        ),
        "mode": mode,
        "frequency_coordinate": "image_plane_cycles_per_mm",
        "scene_coordinate": "undistorted_tangent_plane_before_strict_geometry",
        "native_pixel_pitch_um_row_col": list(pixel),
        "native_nyquist_cycles_per_mm_row_col": list(native_nyquist),
        "target_fraction_native_nyquist": fraction,
        "target_frequency_cycles_per_mm_row_col": list(target),
        "alias_probe_multiple_native_nyquist": alias_multiple,
        "alias_probe_frequency_cycles_per_mm_row_col": (
            list(alias) if alias is not None else None
        ),
        "minimum_scene_samples_per_target_period_seed": samples,
        "minimum_scene_samples_per_alias_period_seed": alias_samples,
        "scene_pitch_search_seed_um": desired_pitch_um,
        "scene_tangent_extent_um_row_col": [height_um, width_um],
        "fixed_scene_horizontal_fov_deg": fov,
        "reference_scene_aspect_row_col": list(reference),
        "suggested_starting_shape_row_col": list(suggested),
        "minimum_registered_native_pixel_shape_row_col": list(native_grid),
        "target_band_grid_shape_row_col": list(target_grid),
        "selected_starting_shape_row_col": list(selected),
        "selected_starting_pitch_um": selected_pitch,
        "selected_starting_grid_meets_registered_native_pixel_floor": (
            selected_pitch <= min(pixel)
        ),
        "selected_grid_meets_frequency_search_seed": selected_pitch <= desired_pitch_um,
        "frequency_seed_first_met_refinement": first_seed_rung,
        "planned_finest_grid_meets_frequency_search_seed": first_seed_rung is not None,
        "stored_psf_pitch_um_row_col": list(psf),
        "stored_psf_grid_nyquist_cycles_per_mm_row_col": list(psf_nyquist),
        "stored_psf_grid_covers_target_band_necessary_only": [
            psf_nyquist[index] >= target[index] for index in (0, 1)
        ],
        "stored_psf_grid_covers_alias_probe_necessary_only": (
            [psf_nyquist[index] >= alias[index] for index in (0, 1)]
            if alias is not None else None
        ),
        "stored_psf_pitch_meets_scene_search_seed": [
            axis <= desired_pitch_um for axis in psf
        ],
        "psf_generation_pitch_search_seed_um": desired_pitch_um,
        "psf_regeneration_required_for_finer_source": any(
            axis > desired_pitch_um for axis in psf
        ),
        "psf_generation_seed_requires_independent_retrace_or_wavefront": True,
        "psf_generation_seed_is_executed_or_accuracy_proof": False,
        "strict_executed_psf_convolution_pitch_um": selected_pitch,
        "psf_source_resolution_gained_by_interpolation": False,
        "independent_psf_reference_required": True,
        "sampled_system_sfr_at_exact_native_nyquist_qualified": False,
        "strict_nyquist_endpoint_requires_separate_otf_reference": fraction == 1,
        "local_distortion_jacobian_qualified": False,
        "cfa_channel_specific_nyquist_qualified": False,
        "above_nyquist_scene_alias_response_qualified": False,
        "fixed_fov_refinement_grids": grids,
        "required_consecutive_refinement_passes": 2,
        "max_refinements": max_refinements,
        "max_scene_axis_policy": max_scene_axis,
        "max_virtual_scene_cells_policy": max_virtual_scene_cells,
        "policy_is_hardware_limit": False,
        "model_band_accuracy_qualified": False,
        "physical_accuracy_qualified": False,
    }
