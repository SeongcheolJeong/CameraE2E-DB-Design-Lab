"""One-node strict executed-OI-kernel versus admitted-ray geometric OTF.

This is a pure numerical diagnostic of already verified source/q2/q4 PSF
planes and their admitted image-plane ray intersections. It reproduces the
strict raytrace kernel's pixel-center support, OI grid, linear interpolation,
DC normalization and float32 storage at angle zero. It does not run Scene,
Sensor, convolution, RAW or an MTF estimator and cannot qualify a lens.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from scipy.ndimage import map_coordinates, rotate

from pyisetcam.optics import _raytrace_psf_grid, _raytrace_psf_support_axes

SCHEMA = "camera_strict_executed_kernel_vs_admitted_ray_otf_v1"
LEVELS = ("source", "q2", "q4")
LEVEL_FACTORS = {"source": 1, "q2": 2, "q4": 4}
MAX_SOURCE_PSF_CELLS = 4_194_304
MAX_ADMITTED_RAYS_PER_LEVEL = 100_000
DEFAULT_MAX_ESTIMATED_WORKING_BYTES = 512 * 1024**2


def _digest(array: np.ndarray, dtype: str) -> str:
    values = np.ascontiguousarray(array, dtype=dtype)
    checksum = hashlib.sha256()
    checksum.update(str(values.shape).encode("ascii"))
    checksum.update(values.dtype.str.encode("ascii"))
    checksum.update(values.tobytes())
    return checksum.hexdigest()


def _pitch(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (tuple, list, np.ndarray)) or len(value) != 2:
        raise ValueError(f"{name} needs row and column values in millimeters.")
    pair = tuple(float(item) for item in value)
    if any(not math.isfinite(item) or item <= 0 for item in pair):
        raise ValueError(f"{name} must have two finite positive values.")
    return pair  # type: ignore[return-value]


def _frequencies(values: Any, name: str) -> tuple[float, ...]:
    if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= 16:
        raise ValueError(f"{name} needs 1..16 declared frequencies.")
    numbers = tuple(float(item) for item in values)
    if (
        any(not math.isfinite(item) or item <= 0 for item in numbers)
        or any(right <= left for left, right in zip(numbers, numbers[1:], strict=False))
    ):
        raise ValueError(f"{name} must increase strictly with positive finite values.")
    return numbers


def _complex_pairs(values: np.ndarray) -> list[list[float]]:
    return [[float(item.real), float(item.imag)] for item in values]


def _strict_kernel(
    plane: np.ndarray, source_pitch_mm: tuple[float, float],
    oi_pitch_mm: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Mirror angle-0 strict PSF sampling, including float32 kernel storage."""
    table = {
        "function": plane[:, :, None, None],
        "sample_spacing_mm": np.asarray(source_pitch_mm, dtype=float),
        "coordinate_convention": "pixel_centers",
    }
    source_x, source_y = _raytrace_psf_support_axes(table)
    target_x, target_y = _raytrace_psf_grid(oi_pitch_mm, table)
    if source_x.size == 1:
        col_coords = np.zeros(target_x.size)
    else:
        col_coords = (target_x - source_x[0]) / (source_x[1] - source_x[0])
    if source_y.size == 1:
        row_coords = np.zeros(target_y.size)
    else:
        row_coords = (target_y - source_y[0]) / (source_y[1] - source_y[0])
    rr, cc = np.meshgrid(row_coords, col_coords, indexing="ij")
    # _strict_raytrace_spectral first converts the source table to float64
    # and normalizes each monochromatic node before angle/grid sampling.
    monochromatic = np.asarray(plane, dtype=float)
    monochromatic = monochromatic / monochromatic.sum(dtype=float)
    rotated = rotate(
        monochromatic, 0.0, reshape=False, order=1, mode="constant",
        cval=0.0, prefilter=False,
    )
    sampled = map_coordinates(
        rotated, [rr, cc], order=1,
        mode="constant", cval=0.0, prefilter=False,
    )
    mass = float(sampled.sum())
    if not math.isfinite(mass) or mass <= 0:
        raise ValueError("Strict executed PSF has no energy at this OI pitch.")
    return np.asarray(sampled / mass, dtype=np.float32), target_x, target_y, mass


def _kernel_axis_otf(
    kernel: np.ndarray, target_x_mm: np.ndarray, target_y_mm: np.ndarray,
    row_targets: tuple[float, ...], col_targets: tuple[float, ...],
) -> tuple[np.ndarray, np.ndarray]:
    row_mass = kernel.sum(axis=1, dtype=np.float64)
    col_mass = kernel.sum(axis=0, dtype=np.float64)
    return (
        np.asarray([
            np.sum(row_mass * np.exp(-2j * math.pi * frequency * target_y_mm))
            for frequency in row_targets
        ]),
        np.asarray([
            np.sum(col_mass * np.exp(-2j * math.pi * frequency * target_x_mm))
            for frequency in col_targets
        ]),
    )


def _ray_axis_otf(
    intersections_mm_xy: np.ndarray,
    row_targets: tuple[float, ...], col_targets: tuple[float, ...],
) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.asarray([
            np.mean(np.exp(-2j * math.pi * frequency * intersections_mm_xy[:, 1]))
            for frequency in row_targets
        ]),
        np.asarray([
            np.mean(np.exp(-2j * math.pi * frequency * intersections_mm_xy[:, 0]))
            for frequency in col_targets
        ]),
    )


def diagnose_executed_kernel_vs_admitted_rays(
    psf_planes_by_level: Mapping[str, np.ndarray],
    admitted_intersections_mm_xy_by_level: Mapping[str, np.ndarray],
    *,
    physical_source_pitch_mm_row_col: Sequence[float],
    physical_source_support_shape_row_col: Sequence[int],
    stored_field_index: int,
    stored_wavelength_nm: float,
    executed_oi_pitch_um: float,
    target_frequency_cycles_per_mm_row_col: Sequence[Sequence[float]],
    maximum_complex_otf_abs_gap: float = 0.01,
    max_estimated_working_bytes: int = DEFAULT_MAX_ESTIMATED_WORKING_BYTES,
) -> dict[str, Any]:
    """Compare source/q2/q4 strict sampled kernels to their direct ray OTFs.

    The declared rays are already admitted at one stored field/wavelength node.
    The caller owns their pinned provenance. Even a zero gap only establishes
    this selected-node numerical agreement at angle 0 and the listed points.
    """
    if (
        not isinstance(psf_planes_by_level, Mapping)
        or not isinstance(admitted_intersections_mm_xy_by_level, Mapping)
        or set(psf_planes_by_level) != set(LEVELS)
        or set(admitted_intersections_mm_xy_by_level) != set(LEVELS)
    ):
        raise ValueError("Provide source, q2 and q4 PSF/ray data at one pinned node.")
    if type(stored_field_index) is not int or stored_field_index < 0:
        raise ValueError("Declare the nonnegative stored field index.")
    if (
        type(stored_wavelength_nm) not in (int, float)
        or not math.isfinite(stored_wavelength_nm)
        or stored_wavelength_nm <= 0
    ):
        raise ValueError("Declare the finite positive stored wavelength.")
    source_pitch = _pitch(physical_source_pitch_mm_row_col, "Physical source pitch")
    support = physical_source_support_shape_row_col
    if (
        not isinstance(support, (tuple, list)) or len(support) != 2
        or any(type(axis) is not int or axis < 3 for axis in support)
        or support[0] * support[1] > MAX_SOURCE_PSF_CELLS
    ):
        raise ValueError("Physical PSF support must be a bounded 2D shape.")
    if (
        isinstance(executed_oi_pitch_um, bool)
        or not math.isfinite(float(executed_oi_pitch_um))
        or float(executed_oi_pitch_um) <= 0
    ):
        raise ValueError("Executed OI pitch must be finite and positive in micrometers.")
    oi_pitch_mm = float(executed_oi_pitch_um) * 1e-3
    targets = target_frequency_cycles_per_mm_row_col
    if not isinstance(targets, (tuple, list)) or len(targets) != 2:
        raise ValueError("Declare row and column target frequencies.")
    row_targets = _frequencies(targets[0], "Row target frequencies")
    col_targets = _frequencies(targets[1], "Column target frequencies")
    if (
        type(maximum_complex_otf_abs_gap) not in (int, float)
        or not math.isfinite(maximum_complex_otf_abs_gap)
        or not 0 < maximum_complex_otf_abs_gap <= 0.1
    ):
        raise ValueError("Complex OTF gap tolerance must be within (0, 0.1].")
    if (
        type(max_estimated_working_bytes) is not int
        or not 64 * 1024**2 <= max_estimated_working_bytes <= 1024**3
    ):
        raise ValueError("Analysis byte policy must be 64 MiB..1 GiB.")
    planes: dict[str, np.ndarray] = {}
    rays: dict[str, np.ndarray] = {}
    level_pitches: dict[str, tuple[float, float]] = {}
    level_shapes: dict[str, tuple[int, int]] = {}
    target_shapes: dict[str, list[int]] = {}
    target_cells_by_level: dict[str, int] = {}
    for level in LEVELS:
        factor = LEVEL_FACTORS[level]
        expected_shape = tuple((axis - 1) * factor + 1 for axis in support)
        level_pitch = tuple(item / factor for item in source_pitch)
        plane = np.asarray(psf_planes_by_level[level])
        points = np.asarray(admitted_intersections_mm_xy_by_level[level])
        if (
            plane.ndim != 2 or tuple(plane.shape) != expected_shape
            or plane.size > MAX_SOURCE_PSF_CELLS
            or not np.issubdtype(plane.dtype, np.floating)
            or not np.isfinite(plane).all() or np.any(plane < 0)
            or not math.isclose(float(plane.sum(dtype=np.float64)), 1.0,
                                rel_tol=0, abs_tol=1e-4)
        ):
            raise ValueError(
                f"{level} must be a unit-mass PSF on its exact physical support "
                f"{expected_shape} at source pitch / {factor}."
            )
        if (
            points.ndim != 2 or points.shape[1:] != (2,)
            or not 1 <= len(points) <= MAX_ADMITTED_RAYS_PER_LEVEL
            or not np.issubdtype(points.dtype, np.floating)
            or not np.isfinite(points).all()
        ):
            raise ValueError(f"{level} needs bounded finite admitted x/y ray intersections.")
        planes[level] = plane
        rays[level] = points
        level_pitches[level] = level_pitch
        level_shapes[level] = expected_shape
        table = {
            "function": plane[:, :, None, None],
            "sample_spacing_mm": np.asarray(level_pitch, dtype=float),
            "coordinate_convention": "pixel_centers",
        }
        target_x, target_y = _raytrace_psf_grid(oi_pitch_mm, table)
        target_shapes[level] = [int(target_y.size), int(target_x.size)]
        target_cells_by_level[level] = int(target_x.size * target_y.size)
    estimated_bytes = int(
        36 * max(target_cells_by_level.values())
        + 16 * max(plane.size for plane in planes.values())
        + sum(plane.nbytes for plane in planes.values())
        + sum(points.nbytes for points in rays.values())
    )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "inconclusive_executed_grid_or_target_support",
        "scope": "one_stored_field_wavelength_node_angle_zero_only",
        "stored_field_index": stored_field_index,
        "stored_wavelength_nm": float(stored_wavelength_nm),
        "angle_deg": 0.0,
        "strict_sampling_method": (
            "pixel_centers_support_to_odd_oi_grid_linear_map_coordinates_"
            "constant_zero_mass_normalize_float32"
        ),
        "physical_source_pitch_mm_row_col": list(source_pitch),
        "physical_source_support_shape_row_col": list(support),
        "physical_pitch_mm_row_col_by_level": {
            level: list(level_pitches[level]) for level in LEVELS
        },
        "physical_support_shape_row_col_by_level": {
            level: list(level_shapes[level]) for level in LEVELS
        },
        "physical_support_half_extent_mm_row_col_by_level": {
            level: [
                (level_shapes[level][axis] - 1) * level_pitches[level][axis] / 2
                for axis in (0, 1)
            ] for level in LEVELS
        },
        "executed_oi_pitch_um": float(executed_oi_pitch_um),
        "executed_kernel_shape_row_col_by_level": target_shapes,
        "target_frequency_cycles_per_mm_row_col": [
            list(row_targets), list(col_targets),
        ],
        "grid_nyquist_cycles_per_mm_row_col": {
            level: [1 / (2 * item) for item in level_pitches[level]]
            for level in LEVELS
        } | {
            "executed_oi": [1 / (2 * oi_pitch_mm)] * 2,
        },
        "maximum_complex_otf_abs_gap_tolerance": float(maximum_complex_otf_abs_gap),
        "estimated_working_bytes_policy_not_peak_rss": estimated_bytes,
        "max_estimated_working_bytes": max_estimated_working_bytes,
        "ray_and_psf_source_provenance_verified_by_this_function": False,
        "selected_node_only": True,
        "continuous_frequency_band_assessed": False,
        "pupil_density_convergence_established": False,
        "psf_bin_convergence_established": False,
        "scene_or_sensor_sampling_convergence_established": False,
        "sensor_or_raw_executed": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "physical_accuracy_qualified": False,
    }
    if estimated_bytes > max_estimated_working_bytes:
        report["status"] = "inconclusive_executed_kernel_analysis_memory_policy"
        return report
    grid_limits = report["grid_nyquist_cycles_per_mm_row_col"]
    for axis, frequencies in enumerate((row_targets, col_targets)):
        if any(
            frequency >= min(
                grid_limits[level][axis], grid_limits["executed_oi"][axis]
            ) for level in LEVELS for frequency in frequencies
        ):
            report["status"] = "inconclusive_target_outside_source_or_executed_grid_band"
            return report
    reports: dict[str, Any] = {}
    maximum_gap = 0.0
    for level in LEVELS:
        try:
            kernel, x_mm, y_mm, sampled_mass = _strict_kernel(
                planes[level], level_pitches[level], oi_pitch_mm
            )
        except ValueError as exc:
            if str(exc) != "Strict executed PSF has no energy at this OI pitch.":
                raise
            report["status"] = "inconclusive_strict_kernel_zero_energy_on_oi_grid"
            report["blocked_level"] = level
            return report
        executed = _kernel_axis_otf(kernel, x_mm, y_mm, row_targets, col_targets)
        direct = _ray_axis_otf(rays[level], row_targets, col_targets)
        row_gap = np.abs(executed[0] - direct[0])
        col_gap = np.abs(executed[1] - direct[1])
        level_max = float(max(np.max(row_gap), np.max(col_gap)))
        maximum_gap = max(maximum_gap, level_max)
        reports[level] = {
            "psf_array_sha256": _digest(planes[level], "<f4"),
            "admitted_intersections_xy_mm_sha256": _digest(rays[level], "<f8"),
            "admitted_ray_count": int(len(rays[level])),
            "source_psf_pitch_mm_row_col": list(level_pitches[level]),
            "source_psf_shape_row_col": list(level_shapes[level]),
            "executed_kernel_shape_row_col": list(kernel.shape),
            "executed_kernel_sha256": _digest(kernel, "<f4"),
            "pre_normalization_sampled_kernel_mass": sampled_mass,
            "executed_kernel_dc_mass_float32": float(kernel.sum(dtype=np.float64)),
            "executed_complex_otf_row_y_up": _complex_pairs(executed[0]),
            "executed_complex_otf_col_x_right": _complex_pairs(executed[1]),
            "direct_ray_complex_otf_row_y_up": _complex_pairs(direct[0]),
            "direct_ray_complex_otf_col_x_right": _complex_pairs(direct[1]),
            "complex_otf_abs_gap_row_y_up": row_gap.tolist(),
            "complex_otf_abs_gap_col_x_right": col_gap.tolist(),
            "maximum_complex_otf_abs_gap": level_max,
            "within_declared_gap_tolerance_selected_node_only": (
                level_max <= maximum_complex_otf_abs_gap
            ),
        }
    report.update({
        "status": "numeric_executed_kernel_vs_ray_otf_selected_node_angle_zero_only",
        "level_reports": reports,
        "maximum_complex_otf_abs_gap_all_levels": maximum_gap,
        "all_levels_within_declared_gap_tolerance_selected_node_only": (
            maximum_gap <= maximum_complex_otf_abs_gap
        ),
    })
    return report
