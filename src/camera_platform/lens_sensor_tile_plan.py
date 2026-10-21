"""Planning-only, global-coordinate halo preflight for strict Lens x Sensor tiles.

This module does not execute a tile. Separate diagnostic operators can now
generate an analytic Scene tile, apply strict geometry and PSF on global
coordinates, and capture a fixed centered RAW. This plan still does not
qualify their peak RSS, general Scene inputs, Scene halo, full active FOV, or
ISP and cannot admit a large job to the shared Worker.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.fft import next_fast_len

from pyisetcam.optics import (
    _STRICT_ROI_PSF_CACHE_BUDGET_BYTES,
    _STRICT_ROI_PSF_CACHE_MAX_BUDGET_BYTES,
    _raytrace_expected_kernel_shape,
    _raytrace_is_strict,
    _strict_raytrace_cache_signature,
    _strict_raytrace_field_positions,
    _strict_raytrace_geometry_axes,
    _validate_strict_raytrace_optics,
)

SCHEMA = "camera_lens_sensor_strict_scene_tile_plan_v1"
DEFAULT_POLICY_BUDGET_BYTES = 8 * 1024**3
DEFAULT_MAX_GEOMETRY_QUERIES = 8_000_000
MAX_RESAMPLED_PSF_AXIS_SAMPLES = 4097
MAX_RESAMPLED_PSF_CELLS = 8_000_000


def _pair(value: Any, name: str, *, integer: bool = False) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} requires two row/column values.")
    if integer and any(type(item) is not int for item in value):
        raise ValueError(f"{name} requires integer row/column values.")
    try:
        pair = (float(value[0]), float(value[1]))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} requires finite row/column values.") from exc
    if not all(math.isfinite(item) for item in pair):
        raise ValueError(f"{name} requires finite row/column values.")
    return pair


def _positive_pair(value: Any, name: str, *, integer: bool = False) -> tuple[float, float]:
    pair = _pair(value, name, integer=integer)
    if any(item <= 0 for item in pair):
        raise ValueError(f"{name} must be positive.")
    return pair


def _rect(value: Any, shape: tuple[int, int], name: str) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(type(item) is not int for item in value)
    ):
        raise ValueError(f"{name} requires four integer half-open bounds.")
    r0, r1, c0, c1 = value
    if not (0 <= r0 < r1 <= shape[0] and 0 <= c0 < c1 <= shape[1]):
        raise ValueError(f"{name} exceeds the declared grid.")
    return r0, r1, c0, c1


def _angular_bounds(
    rect: tuple[int, int, int, int],
    shape: tuple[int, int],
    pitch_um: float,
    image_distance_mm: float,
) -> dict[str, list[float]]:
    """Return raster row-down/column-right tangent-plane cell-edge angles."""
    r0, r1, c0, c1 = rect
    scale = pitch_um * 1e-3 / image_distance_mm
    return {
        "row_down_deg": [
            math.degrees(math.atan((edge - shape[0] / 2) * scale)) for edge in (r0, r1)
        ],
        "col_right_deg": [
            math.degrees(math.atan((edge - shape[1] / 2) * scale)) for edge in (c0, c1)
        ],
    }


def _geometry_query_bounds(
    rect: tuple[int, int, int, int],
    shape: tuple[int, int],
    pitch_mm: float,
    ideal_mm: np.ndarray,
    distorted_mm: np.ndarray,
) -> tuple[int, int, int, int]:
    """Mirror strict geometry's pixel-center radial map on the *global* grid.

    Every required pre-PSF OI sample is inspected. The bilinear source query
    includes floor and the next sample; no corner-only radial approximation is
    used. Outside-field samples are included conservatively even though the
    strict solver may multiply them by zero.
    """
    r0, r1, c0, c1 = rect
    center_r = (shape[0] - 1) / 2
    center_c = (shape[1] - 1) / 2
    x = (np.arange(c0, c1, dtype=float) - center_c) * pitch_mm
    minimum_r = minimum_c = math.inf
    maximum_r = maximum_c = -math.inf
    for row in range(r0, r1):
        y = (row - center_r) * pitch_mm
        radius = np.hypot(x, y)
        input_radius = np.interp(radius, distorted_mm, ideal_mm)
        factor = np.divide(input_radius, radius, out=np.ones_like(radius), where=radius > 0.0)
        query_r = center_r + y * factor / pitch_mm
        query_c = center_c + x * factor / pitch_mm
        minimum_r = min(minimum_r, float(query_r.min()))
        maximum_r = max(maximum_r, float(query_r.max()))
        minimum_c = min(minimum_c, float(query_c.min()))
        maximum_c = max(maximum_c, float(query_c.max()))
    # One ulp-scale margin is deliberately conservative at integer boundaries.
    return (
        math.floor(minimum_r - 1e-9),
        math.ceil(maximum_r + 1e-9) + 1,
        math.floor(minimum_c - 1e-9),
        math.ceil(maximum_c + 1e-9) + 1,
    )


def _strict_roi_psf_cache_budget_for_declared_policy(
    kernel_shape: tuple[int, int], declared_working_budget_bytes: int
) -> int:
    """Select bounded kernel reuse from the caller's opt-in working budget.

    Keep the 32 MiB default when the declared budget is 512 MiB or less.
    Larger admission budgets may retain up to sixteen sampled float32 kernels,
    but never more than 1/16 of the caller's budget or the 256 MiB cap. This
    is a cache policy, not an estimate of process RSS or a physics refinement.
    """
    if (
        len(kernel_shape) != 2
        or any(type(side) is not int or side < 1 for side in kernel_shape)
        or type(declared_working_budget_bytes) is not int
        or declared_working_budget_bytes < 1
    ):
        raise ValueError("Strict ROI PSF cache selection requires a positive grid and budget.")
    desired = max(
        _STRICT_ROI_PSF_CACHE_BUDGET_BYTES,
        16 * kernel_shape[0] * kernel_shape[1] * np.dtype(np.float32).itemsize,
    )
    share = max(_STRICT_ROI_PSF_CACHE_BUDGET_BYTES, declared_working_budget_bytes // 16)
    return min(desired, share, _STRICT_ROI_PSF_CACHE_MAX_BUDGET_BYTES)


def _memory_policy(
    source_shape: tuple[int, int],
    pregeometry_shape: tuple[int, int],
    kernel_shape: tuple[int, int],
    source_psf_shape: tuple[int, int],
    field_count: int,
    band_count: int,
    *,
    shared_psf_cache_budget_bytes: int | None = None,
) -> dict[str, Any]:
    rows, cols = pregeometry_shape
    kr, kc = kernel_shape
    source_area = source_shape[0] * source_shape[1]
    optical_area = rows * cols
    # Same even padding rule as the strict solver, applied to a prospective
    # global-coordinate tile. This is policy arithmetic, not measured RSS.
    pad_r = 2 * math.ceil((math.ceil(kr // 2) + 2) / 2)
    pad_c = 2 * math.ceil((math.ceil(kc // 2) + 2) / 2)
    padded = (rows + 2 * pad_r, cols + 2 * pad_c)
    fft = (next_fast_len(rows + kr - 1), next_fast_len(cols + kc - 1))
    fft_real = fft[0] * fft[1] * 8
    fft_complex = fft[0] * (fft[1] // 2 + 1) * 16
    source_psf_workspace = (
        4 * source_psf_shape[0] * source_psf_shape[1] * field_count * band_count * 8
    )
    kernel_heuristic = 128 * kr * kc * 8 + source_psf_workspace
    spectral_source = source_area * 31 * 8
    # Distortion can make the source and pre-PSF optical rectangles differ.
    # Budget both before projecting a source tile through strict geometry.
    geometry_planes = (source_area + optical_area) * 20 * 8
    padded_output = padded[0] * padded[1] * band_count * 8
    fft_intermediates = 6 * fft_complex + 4 * fft_real
    # The strict bounded ROI solver now retains prepared PSFs across source
    # tiles within one solve. Its byte-capped cache is additional resident
    # state even though the estimate remains a policy heuristic, not peak RSS.
    shared_psf_cache = (
        _STRICT_ROI_PSF_CACHE_BUDGET_BYTES
        if shared_psf_cache_budget_bytes is None
        else shared_psf_cache_budget_bytes
    )
    if (
        type(shared_psf_cache) is not int
        or not 0 <= shared_psf_cache <= _STRICT_ROI_PSF_CACHE_MAX_BUDGET_BYTES
    ):
        raise ValueError("Strict ROI PSF cache budget exceeds its bounded policy.")
    estimate = (
        kernel_heuristic
        + 8 * spectral_source
        + geometry_planes
        + 3 * padded_output
        + fft_intermediates
        + shared_psf_cache
    )
    return {
        "source_tile_samples_row_col": list(source_shape),
        "pregeometry_optical_tile_samples_row_col": list(pregeometry_shape),
        "padded_output_samples_row_col": list(padded),
        "worst_case_fft_samples_row_col": list(fft),
        "source_31_band_float64_bytes": spectral_source,
        "three_or_more_band_source_float64_bytes": source_area * band_count * 8,
        "pregeometry_optical_float64_bytes": optical_area * band_count * 8,
        "strict_kernel_128x_heuristic_plus_source_workspace_bytes": kernel_heuristic,
        "geometry_20_float64_planes_bytes": geometry_planes,
        "padded_output_float64_bytes": padded_output,
        "fft_six_complex_four_real_policy_bytes": fft_intermediates,
        "strict_shared_psf_preparation_cache_policy_bytes": shared_psf_cache,
        "combined_policy_estimate_bytes": estimate,
        "estimate_is_peak_rss_upper_bound": False,
    }


def _psf_grid_preflight(raytrace: dict[str, Any], pitch_um: float) -> dict[str, Any]:
    """Bound `_raytrace_expected_kernel_shape` before it creates target axes."""
    table = raytrace["psf"]
    shape = np.asarray(table["function"]).shape[:2]
    source_spacing = np.asarray(table["sample_spacing_mm"], dtype=float).reshape(-1)
    if source_spacing.size == 1:
        source_spacing = np.repeat(source_spacing, 2)
    target_pitch_mm = pitch_um * 1e-3
    support_mm = [(shape[axis] - 1) * float(source_spacing[axis]) / 2 for axis in range(2)]
    result = {
        "source_psf_shape_row_col": list(shape),
        "source_psf_half_support_mm_row_col": support_mm,
        "declared_oi_pitch_um": pitch_um,
        "max_resampled_axis_samples": MAX_RESAMPLED_PSF_AXIS_SAMPLES,
        "max_resampled_cells": MAX_RESAMPLED_PSF_CELLS,
    }
    if target_pitch_mm <= 0 or not math.isfinite(target_pitch_mm):
        return {**result, "status": "blocked_target_pitch_underflow"}
    ratios = [extent / target_pitch_mm for extent in support_mm]
    max_radius = (MAX_RESAMPLED_PSF_AXIS_SAMPLES - 1) // 2
    if any(not math.isfinite(ratio) or ratio > max_radius for ratio in ratios):
        return {**result, "status": "blocked_psf_axis_budget"}
    # The small margin covers floating evaluation differences between this
    # scalar bound and `_raytrace_psf_grid`'s sampled support axes.
    upper_shape = [2 * math.ceil(ratio + 1e-9) + 1 for ratio in ratios]
    if (
        any(size > MAX_RESAMPLED_PSF_AXIS_SAMPLES for size in upper_shape)
        or upper_shape[0] * upper_shape[1] > MAX_RESAMPLED_PSF_CELLS
    ):
        return {
            **result,
            "status": "blocked_psf_grid_cell_budget",
            "conservative_target_kernel_shape_row_col": upper_shape,
        }
    return {
        **result,
        "status": "within_planner_grid_cap",
        "conservative_target_kernel_shape_row_col": upper_shape,
    }


def plan_strict_scene_tile(
    optics: dict[str, Any] | None,
    *,
    scene_shape_row_col: tuple[int, int] | list[int],
    scene_horizontal_fov_deg: float,
    scene_vertical_fov_deg: float,
    declared_oi_pitch_um: float,
    source_scene_kind: str | None,
    source_scene_id: str | None,
    native_shape_row_col: tuple[int, int] | list[int],
    native_core_row_col_exclusive: tuple[int, int, int, int] | list[int],
    native_pixel_pitch_um_row_col: tuple[float, float] | list[float],
    sensor_operator: dict[str, Any] | None,
    isp_operator: dict[str, Any] | None,
    wavelengths_nm: tuple[float, ...] | list[float],
    policy_budget_bytes: int = DEFAULT_POLICY_BUDGET_BYTES,
    max_geometry_queries: int = DEFAULT_MAX_GEOMETRY_QUERIES,
) -> dict[str, Any]:
    """Plan one native RAW core against the full strict Scene/OI coordinate grid.

    A successful result is only a conditional *numerical source-region plan*.
    It does not prove optical fidelity, pixel physics, tile equivalence, a hard
    memory limit, or execute the high-resolution image.
    """
    missing = [
        name
        for name, value in (
            ("strict_optics", optics),
            ("source_scene_kind", source_scene_kind),
            ("source_scene_id", source_scene_id),
            ("executed_sensor_operator", sensor_operator),
            ("fixed_isp_halo_operator", isp_operator),
        )
        if value is None or value == ""
    ]
    if missing:
        return {
            "schema": SCHEMA,
            "status": "blocked_missing_operator_contracts",
            "missing_operator_contracts": missing,
            "execution_qualified": False,
            "full_frame_execution_qualified": False,
        }
    assert optics is not None and sensor_operator is not None and isp_operator is not None
    if (
        not isinstance(optics, dict)
        or not isinstance(sensor_operator, dict)
        or not isinstance(isp_operator, dict)
    ):
        raise ValueError("Optical, sensor and ISP operator contracts must be objects.")
    if source_scene_kind not in {"analytic_continuous", "declared_raster"}:
        raise ValueError("source_scene_kind must identify an analytic or declared raster source.")
    if not isinstance(source_scene_id, str) or not source_scene_id.strip():
        raise ValueError("A nonempty source_scene_id is required.")
    shape = tuple(
        int(item)
        for item in _positive_pair(scene_shape_row_col, "scene_shape_row_col", integer=True)
    )
    native_shape = tuple(
        int(item)
        for item in _positive_pair(native_shape_row_col, "native_shape_row_col", integer=True)
    )
    core = _rect(native_core_row_col_exclusive, native_shape, "native_core_row_col_exclusive")
    native_pitch = _positive_pair(native_pixel_pitch_um_row_col, "native_pixel_pitch_um_row_col")
    if (
        type(policy_budget_bytes) is not int
        or policy_budget_bytes <= 0
        or type(max_geometry_queries) is not int
        or max_geometry_queries <= 0
    ):
        raise ValueError("Finite positive integer resource budgets are required.")
    if not isinstance(wavelengths_nm, (tuple, list)) or not wavelengths_nm:
        raise ValueError("At least one wavelength is required.")
    wavelengths = np.asarray(wavelengths_nm, dtype=float)
    if (
        wavelengths.ndim != 1
        or not np.isfinite(wavelengths).all()
        or np.any(np.diff(wavelengths) <= 0)
        or np.any(wavelengths <= 0)
    ):
        raise ValueError("Wavelengths must be finite, positive and strictly increasing.")
    raytrace = optics.get("raytrace", {})
    if not _raytrace_is_strict(raytrace):
        raise ValueError("A strict raytrace optical contract is required.")
    _validate_strict_raytrace_optics(optics)
    image_distance_mm = float(raytrace.get("effective_focal_length_m", math.nan)) * 1000
    pitch_um = float(declared_oi_pitch_um)
    if not all(math.isfinite(item) and item > 0 for item in (image_distance_mm, pitch_um)):
        raise ValueError("Effective image distance and declared OI pitch must be positive.")
    if not all(
        math.isfinite(float(fov)) and 0 < float(fov) < 180
        for fov in (scene_horizontal_fov_deg, scene_vertical_fov_deg)
    ):
        raise ValueError("Both Scene FOV dimensions must be finite and positive.")
    calculated_pitch_col = (
        2
        * image_distance_mm
        * math.tan(math.radians(scene_horizontal_fov_deg) / 2)
        / shape[1]
        * 1000
    )
    calculated_pitch_row = (
        2 * image_distance_mm * math.tan(math.radians(scene_vertical_fov_deg) / 2) / shape[0] * 1000
    )
    if not all(
        math.isclose(calculated, pitch_um, rel_tol=1e-8, abs_tol=0.0)
        for calculated in (calculated_pitch_row, calculated_pitch_col)
    ):
        raise ValueError("Scene angular sampling and OI convolution pitch differ.")
    diagonal_fov = math.degrees(
        2
        * math.atan(
            math.hypot(
                math.tan(math.radians(scene_horizontal_fov_deg) / 2),
                math.tan(math.radians(scene_vertical_fov_deg) / 2),
            )
        )
    )
    if diagonal_fov > float(raytrace.get("max_fov_deg", math.inf)) + 1e-9:
        raise ValueError("Scene diagonal FOV exceeds the strict raytrace field.")

    sensor_required = (
        "status",
        "native_pixel_sample_pitch_um_row_col",
        "oi_pitch_um_row_col",
        "gaussian_kernel_shape_row_col",
        "interpolation",
        "n_samples_per_pixel",
        "mosaic",
        "ocl_group_proxy_mode",
        "pixel_aperture_already_in_optical_psf",
        "fdtd_sensor_attached",
        "tcad_sensor_attached",
        "fdtd_sensor_config",
        "tcad_sensor_config",
        "physical_estimate_config",
        "fdtd_estimate_config",
    )
    isp_required = ("implementation_id", "halo_native_pixels_row_col", "boundary_mode")
    missing = [
        f"sensor_operator.{key}" for key in sensor_required if key not in sensor_operator
    ] + [f"isp_operator.{key}" for key in isp_required if key not in isp_operator]
    if missing:
        return {
            "schema": SCHEMA,
            "status": "blocked_missing_operator_contracts",
            "missing_operator_contracts": missing,
            "execution_qualified": False,
            "full_frame_execution_qualified": False,
        }
    if (
        sensor_operator["status"] != "executed_operator_parameters_recorded"
        or sensor_operator["interpolation"] != "bilinear_constant_zero"
        or sensor_operator["n_samples_per_pixel"] != 1
        or sensor_operator["mosaic"] is not True
        or sensor_operator["ocl_group_proxy_mode"] != "off"
        or sensor_operator["pixel_aperture_already_in_optical_psf"] is not False
    ):
        raise ValueError(
            "The declared sensor operator is not the supported digital RAW mosaic path."
        )
    if (
        sensor_operator["physical_estimate_config"] is not None
        or sensor_operator["fdtd_estimate_config"] is not None
    ):
        raise ValueError(
            "Physical/FDTD estimate current-density paths lack a tile support contract."
        )
    sensor_physics = {}
    for name in ("fdtd", "tcad"):
        attached = sensor_operator[f"{name}_sensor_attached"]
        config = sensor_operator[f"{name}_sensor_config"]
        if type(attached) is not bool or attached != (config is not None):
            raise ValueError(f"{name.upper()} attachment and executed config disagree.")
        if config is not None and not isinstance(config, dict):
            raise ValueError(f"{name.upper()} executed config must be an object or null.")
        # Mirror sensor.py's enabled-default-true behavior. A present but
        # disabled LUT does not apply a spatial/spectral response.
        enabled = isinstance(config, dict) and bool(config.get("enabled", True))
        if enabled:
            raise ValueError(f"Enabled {name.upper()} response lacks a tile support contract.")
        sensor_physics[f"{name}_attached"] = attached
        sensor_physics[f"{name}_effective_enabled"] = enabled
    sensor_pitch = _positive_pair(
        sensor_operator["native_pixel_sample_pitch_um_row_col"], "sensor operator native pitch"
    )
    sensor_oi_pitch = _positive_pair(
        sensor_operator["oi_pitch_um_row_col"], "sensor operator OI pitch"
    )
    if not all(
        math.isclose(a, b, rel_tol=1e-9, abs_tol=0.0)
        for a, b in zip(sensor_pitch, native_pitch, strict=True)
    ) or not all(math.isclose(a, pitch_um, rel_tol=1e-9, abs_tol=0.0) for a in sensor_oi_pitch):
        raise ValueError("Executed sensor and requested Scene/OI sample pitches differ.")
    kernel_pixel = tuple(
        int(item)
        for item in _positive_pair(
            sensor_operator["gaussian_kernel_shape_row_col"],
            "gaussian_kernel_shape_row_col",
            integer=True,
        )
    )
    expected_pixel_kernel = tuple(
        max(1, math.ceil(sensor_pitch[axis] / sensor_oi_pitch[axis])) for axis in range(2)
    )
    if kernel_pixel != expected_pixel_kernel:
        raise ValueError("Sensor integration kernel differs from the executed pitch rule.")
    isp_halo = tuple(
        int(item)
        for item in _pair(isp_operator["halo_native_pixels_row_col"], "ISP halo", integer=True)
    )
    if (
        any(item < 0 for item in isp_halo)
        or not isinstance(isp_operator["implementation_id"], str)
        or not isp_operator["implementation_id"]
    ):
        raise ValueError("An identified finite nonnegative ISP halo is required.")
    if isp_operator["boundary_mode"] not in {"constant_zero", "reflect", "nearest"}:
        raise ValueError("The full-frame ISP boundary mode must be explicit.")
    # convolve2d(..., mode='same') has at most floor(k/2) samples on either
    # side, followed by bilinear OI sampling of two adjacent cell centers.
    pixel_support = (kernel_pixel[0] // 2 + 1, kernel_pixel[1] // 2 + 1)
    nr0, nr1, nc0, nc1 = core
    native_extended = (
        max(0, nr0 - isp_halo[0]),
        min(native_shape[0], nr1 + isp_halo[0]),
        max(0, nc0 - isp_halo[1]),
        min(native_shape[1], nc1 + isp_halo[1]),
    )
    center_scene = ((shape[0] - 1) / 2, (shape[1] - 1) / 2)
    center_native = ((native_shape[0] - 1) / 2, (native_shape[1] - 1) / 2)
    first_r = center_scene[0] + (native_extended[0] - center_native[0]) * native_pitch[0] / pitch_um
    last_r = (
        center_scene[0] + (native_extended[1] - 1 - center_native[0]) * native_pitch[0] / pitch_um
    )
    first_c = center_scene[1] + (native_extended[2] - center_native[1]) * native_pitch[1] / pitch_um
    last_c = (
        center_scene[1] + (native_extended[3] - 1 - center_native[1]) * native_pitch[1] / pitch_um
    )
    oi_post_psf = (
        math.floor(first_r - pixel_support[0]),
        math.ceil(last_r + pixel_support[0]) + 1,
        math.floor(first_c - pixel_support[1]),
        math.ceil(last_c + pixel_support[1]) + 1,
    )
    psf_grid_preflight = _psf_grid_preflight(raytrace, pitch_um)
    if psf_grid_preflight["status"] != "within_planner_grid_cap":
        return {
            "schema": SCHEMA,
            "status": psf_grid_preflight["status"],
            "psf_grid_preflight": psf_grid_preflight,
            "execution_qualified": False,
            "full_frame_execution_qualified": False,
        }
    kernel_shape = _raytrace_expected_kernel_shape(optics, pitch_um * 1e-6)
    if any(
        actual > upper
        for actual, upper in zip(
            kernel_shape,
            psf_grid_preflight["conservative_target_kernel_shape_row_col"],
            strict=True,
        )
    ):
        raise ValueError("Executed PSF target grid exceeded its scalar preflight bound.")
    if any(item < 1 or item % 2 != 1 for item in kernel_shape):
        raise ValueError("Strict sampled PSF needs an odd positive full kernel envelope.")
    psf_support = (kernel_shape[0] // 2, kernel_shape[1] // 2)
    pad = tuple(2 * math.ceil((math.ceil(kernel_shape[axis] // 2) + 2) / 2) for axis in range(2))
    padded_optical_output_fits = (
        oi_post_psf[0] >= -pad[0]
        and oi_post_psf[1] <= shape[0] + pad[0]
        and oi_post_psf[2] >= -pad[1]
        and oi_post_psf[3] <= shape[1] + pad[1]
    )
    current_optical_roi_helper_accepts = (
        0 <= oi_post_psf[0] < oi_post_psf[1] <= shape[0]
        and 0 <= oi_post_psf[2] < oi_post_psf[3] <= shape[1]
    )
    pregeometry_unclipped = (
        oi_post_psf[0] - psf_support[0],
        oi_post_psf[1] + psf_support[0],
        oi_post_psf[2] - psf_support[1],
        oi_post_psf[3] + psf_support[1],
    )
    pregeometry = (
        max(0, pregeometry_unclipped[0]),
        min(shape[0], pregeometry_unclipped[1]),
        max(0, pregeometry_unclipped[2]),
        min(shape[1], pregeometry_unclipped[3]),
    )
    base = {
        "schema": SCHEMA,
        "execution_qualified": False,
        "full_frame_execution_qualified": False,
        "tile_equivalence_qualified": False,
        "source_scene": {
            "id": source_scene_id,
            "kind": source_scene_kind,
            "shape_row_col": list(shape),
            "horizontal_fov_deg": scene_horizontal_fov_deg,
            "vertical_fov_deg": scene_vertical_fov_deg,
            "tangent_plane_step_row_col": [pitch_um * 1e-3 / image_distance_mm] * 2,
            "declared_source_to_oi_pitch_um_row_col": [pitch_um] * 2,
        },
        "optics": {
            "strict_optics_signature_sha256": _strict_raytrace_cache_signature(optics),
            "psf_source_asset_sha256": raytrace.get("asset_sha256"),
            "full_sampled_kernel_row_col": list(kernel_shape),
            "all_field_azimuth_wavelength_kernel_envelope_radius_oi_samples_row_col": list(
                psf_support
            ),
            "psf_support_basis": "full_resampled_kernel_shape_not_energy_threshold",
            "stored_psf_field_count": len(raytrace["psf"]["field_height_mm"]),
            "wavelength_nm": wavelengths.tolist(),
        },
        "native_core_row_col_exclusive": list(core),
        "native_isp_extended_row_col_exclusive": list(native_extended),
        "isp_boundary_mode": isp_operator["boundary_mode"],
        "sensor_operator_status": sensor_operator["status"],
        "sensor_physics_effective_state": sensor_physics,
        "psf_grid_preflight": psf_grid_preflight,
        "sensor_mosaic_integration_radius_oi_samples_row_col": list(pixel_support),
        "isp_halo_native_pixels_row_col": list(isp_halo),
        "required_post_psf_oi_row_col_exclusive": list(oi_post_psf),
        "required_post_psf_oi_angular_cell_edges_deg": _angular_bounds(
            oi_post_psf, shape, pitch_um, image_distance_mm
        ),
        "strict_optical_output_padding_samples_row_col": list(pad),
        "required_post_psf_oi_fits_strict_output_padding": padded_optical_output_fits,
        "required_post_psf_oi_within_current_roi_helper": current_optical_roi_helper_accepts,
        "required_pregeometry_oi_row_col_exclusive": list(pregeometry_unclipped),
        "available_pregeometry_oi_row_col_exclusive": list(pregeometry),
        "geometry_coordinate_convention": "global_full_scene_pixel_centers_row_down_col_right",
        "remaining_execution_requirements": [
            "general_scene_renderer_or_declared_raster_reader",
            "target_grid_analytic_geometry_raw_equivalence",
            "general_sensor_and_isp_tile_executor",
            "hard_process_memory_boundary",
            "same_grid_full_vs_tiled_numerical_equivalence_at_target_grid",
            "active_sensor_fov_and_scene_halo_qualification",
            "independent_optical_psf_and_physical_response_qualification",
        ],
    }
    if pregeometry[0] >= pregeometry[1] or pregeometry[2] >= pregeometry[3]:
        return {**base, "status": "blocked_no_pregeometry_overlap"}
    if not padded_optical_output_fits:
        return {**base, "status": "blocked_strict_output_padding"}
    if not current_optical_roi_helper_accepts:
        return {**base, "status": "blocked_current_optical_roi_helper_domain"}
    # The old full-frame strict path computes only within this Scene support;
    # data outside it is constant-zero. Expose this separately from missing
    # physical halo rather than silently claiming unbounded source radiance.
    global_boundary_touched = pregeometry != pregeometry_unclipped
    query_count = (
        (pregeometry[1] - pregeometry[0]) * (pregeometry[3] - pregeometry[2]) * len(wavelengths)
    )
    base["geometry_query_count"] = query_count
    base["max_geometry_queries"] = max_geometry_queries
    if query_count > max_geometry_queries:
        return {**base, "status": "blocked_geometry_planner_budget"}
    pitch_mm = pitch_um * 1e-3
    per_wavelength = []
    query_rects = []
    for wavelength in wavelengths:
        ideal, distorted = _strict_raytrace_geometry_axes(raytrace, float(wavelength))
        # Require matching PSF/RI spectral and field tables; the full kernel
        # envelope then covers every field and azimuth interpolation member.
        psf_fields = _strict_raytrace_field_positions(raytrace, raytrace["psf"], float(wavelength))
        ri_fields = _strict_raytrace_field_positions(
            raytrace, raytrace["relative_illumination"], float(wavelength)
        )
        query = _geometry_query_bounds(pregeometry, shape, pitch_mm, ideal, distorted)
        query_rects.append(query)
        per_wavelength.append(
            {
                "wavelength_nm": float(wavelength),
                "source_query_row_col_exclusive_including_bilinear_neighbors": list(query),
                "distorted_geometry_field_max_mm": float(distorted[-1]),
                "psf_field_max_mm": float(psf_fields[-1]),
                "relative_illumination_field_max_mm": float(ri_fields[-1]),
            }
        )
    source_required = (
        min(item[0] for item in query_rects),
        max(item[1] for item in query_rects),
        min(item[2] for item in query_rects),
        max(item[3] for item in query_rects),
    )
    source_available = (
        max(0, source_required[0]),
        min(shape[0], source_required[1]),
        max(0, source_required[2]),
        min(shape[1], source_required[3]),
    )
    source_support_fits = source_required == source_available and not global_boundary_touched
    base.update(
        {
            "strict_geometry_by_wavelength": per_wavelength,
            "required_source_scene_row_col_exclusive": list(source_required),
            "available_source_scene_row_col_exclusive": list(source_available),
            "required_source_angular_cell_edges_deg": _angular_bounds(
                source_required, shape, pitch_um, image_distance_mm
            ),
            "available_source_angular_cell_edges_deg": _angular_bounds(
                source_available, shape, pitch_um, image_distance_mm
            ),
            "full_sampled_psf_envelope_touches_global_scene_boundary": global_boundary_touched,
            "declared_source_support_fits": source_support_fits,
        }
    )
    if source_available[0] >= source_available[1] or source_available[2] >= source_available[3]:
        return {**base, "status": "blocked_no_source_overlap"}
    psf_source_shape = tuple(np.asarray(raytrace["psf"]["function"]).shape[:2])
    memory = _memory_policy(
        (source_available[1] - source_available[0], source_available[3] - source_available[2]),
        (pregeometry[1] - pregeometry[0], pregeometry[3] - pregeometry[2]),
        kernel_shape,
        psf_source_shape,
        len(raytrace["psf"]["field_height_mm"]),
        len(wavelengths),
    )
    memory["declared_policy_budget_bytes"] = policy_budget_bytes
    memory["fits_declared_policy"] = memory["combined_policy_estimate_bytes"] <= policy_budget_bytes
    base["tile_memory_policy"] = memory
    if not source_support_fits:
        return {**base, "status": "blocked_declared_source_halo"}
    if not memory["fits_declared_policy"]:
        return {**base, "status": "blocked_tile_memory_policy"}
    return {**base, "status": "planning_only", "planning_feasible_under_declared_policy": True}
