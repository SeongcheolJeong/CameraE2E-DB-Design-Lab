"""Read-only source-boundary preflight for a strict optical ROI.

The full sampled PSF envelope determines which pre-PSF optical samples can
affect an ROI. Strict inverse geometry then identifies the Scene samples those
optical samples can query. The bounds are deliberately conservative: they do
not discount PSF zeros or pixels masked by the ray-trace field stop. A bound
inside the declared raster proves only numerical support for this finite
Scene, not physical Scene halo, product FOV, or PSF accuracy.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from pyisetcam.optics import (
    _raytrace_expected_kernel_shape,
    _raytrace_is_strict,
    _strict_raytrace_axis,
    _strict_raytrace_field_positions,
    _strict_raytrace_geometry_axes,
    _validate_strict_raytrace_optics,
)

from .lens_sensor_tile_plan import (
    DEFAULT_MAX_GEOMETRY_QUERIES,
    _geometry_query_bounds,
    _psf_grid_preflight,
)

SCHEMA = "camera_strict_optical_source_halo_preflight_v1"
DEFAULT_MAX_GEOMETRY_QUERY_CHUNK_CELLS = 65_536


def _shape(value: tuple[int, int] | list[int]) -> tuple[int, int]:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or any(type(item) is not int or item < 2 for item in value)
    ):
        raise ValueError("Global Scene shape requires two integer dimensions >= 2.")
    return value[0], value[1]


def _roi(
    value: tuple[int, int, int, int] | list[int], shape: tuple[int, int]
) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 4
        or any(type(item) is not int for item in value)
    ):
        raise ValueError("Optical ROI requires four integer half-open bounds.")
    r0, r1, c0, c1 = value
    if not (0 <= r0 < r1 <= shape[0] and 0 <= c0 < c1 <= shape[1]):
        raise ValueError("Optical ROI lies outside the declared global Scene grid.")
    return r0, r1, c0, c1


def _outside(rect: tuple[int, int, int, int], shape: tuple[int, int]) -> bool:
    return rect[0] < 0 or rect[1] > shape[0] or rect[2] < 0 or rect[3] > shape[1]


def _intersection(
    rect: tuple[int, int, int, int], shape: tuple[int, int]
) -> tuple[int, int, int, int]:
    return (
        max(0, min(shape[0], rect[0])), max(0, min(shape[0], rect[1])),
        max(0, min(shape[1], rect[2])), max(0, min(shape[1], rect[3])),
    )


def _chunked_geometry_query_bounds(
    rect: tuple[int, int, int, int],
    shape: tuple[int, int],
    pitch_mm: float,
    ideal_mm: np.ndarray,
    distorted_mm: np.ndarray,
    *,
    max_chunk_cells: int,
) -> tuple[tuple[int, int, int, int], int, int]:
    """Scan the exact strict inverse map in bounded, exhaustive subrectangles.

    The existing geometry helper computes extrema over each pixel center and
    then expands to bilinear source neighbors. Unioning its integer bounds is
    identical to scanning the whole rectangle: min/max and the monotone
    floor/ceil expansion commute with the subrectangle union. Its transient
    coordinate vectors are bounded by ``max_chunk_cells`` here, regardless of
    the declared global Scene dimensions.
    """
    r0, r1, c0, c1 = rect
    col_step = min(c1 - c0, max_chunk_cells)
    row_step = max(1, max_chunk_cells // col_step)
    union: tuple[int, int, int, int] | None = None
    count = 0
    largest = 0
    for row_start in range(r0, r1, row_step):
        row_stop = min(r1, row_start + row_step)
        for col_start in range(c0, c1, col_step):
            col_stop = min(c1, col_start + col_step)
            current = _geometry_query_bounds(
                (row_start, row_stop, col_start, col_stop),
                shape,
                pitch_mm,
                ideal_mm,
                distorted_mm,
            )
            union = current if union is None else (
                min(union[0], current[0]),
                max(union[1], current[1]),
                min(union[2], current[2]),
                max(union[3], current[3]),
            )
            count += 1
            largest = max(largest, (row_stop - row_start) * (col_stop - col_start))
    assert union is not None  # A valid optical ROI has nonempty pregeometry.
    return union, count, largest


def preflight_strict_optical_source_halo(
    optics: dict[str, Any],
    *,
    global_scene_shape_row_col: tuple[int, int] | list[int],
    optical_roi_row_col_exclusive: tuple[int, int, int, int] | list[int],
    oi_pitch_um: float,
    wavelengths_nm: tuple[float, ...] | list[float] | np.ndarray,
    max_geometry_query_cells: int = DEFAULT_MAX_GEOMETRY_QUERIES,
    max_geometry_query_chunk_cells: int = DEFAULT_MAX_GEOMETRY_QUERY_CHUNK_CELLS,
) -> dict[str, Any]:
    """Locate all Scene queries for an arbitrary global-coordinate optical ROI.

    The PSF envelope uses the *entire* sampled kernel at every field, azimuth,
    and wavelength. It is not an encircled-energy or effective-support cut.
    Geometry query bounds include both bilinear neighbors plus a small floating
    boundary margin. ``inside_declared_scene`` is a numerical raster statement
    only; the zero continuation outside the raster remains physically untested.
    """
    if not isinstance(optics, dict) or optics.get("model") != "raytrace" or not _raytrace_is_strict(
        optics.get("raytrace", {})
    ):
        raise ValueError("Optical halo preflight requires strict ray-trace optics.")
    shape = _shape(global_scene_shape_row_col)
    roi = _roi(optical_roi_row_col_exclusive, shape)
    pitch = float(oi_pitch_um)
    if not math.isfinite(pitch) or pitch <= 0.0:
        raise ValueError("OI pitch must be finite and positive in micrometers.")
    if (
        type(max_geometry_query_cells) is not int
        or max_geometry_query_cells < 1
    ):
        raise ValueError("Geometry query policy must be a positive integer work budget.")
    if (
        type(max_geometry_query_chunk_cells) is not int
        or not 1 <= max_geometry_query_chunk_cells <= DEFAULT_MAX_GEOMETRY_QUERY_CHUNK_CELLS
    ):
        raise ValueError(
            "Geometry query chunk policy must be a positive integer at most "
            f"{DEFAULT_MAX_GEOMETRY_QUERY_CHUNK_CELLS}."
        )
    wave = _strict_raytrace_axis(wavelengths_nm, "requested wavelength axis")
    _validate_strict_raytrace_optics(optics)
    raytrace = optics["raytrace"]
    for name in ("geometry", "psf", "relative_illumination"):
        covered = _strict_raytrace_axis(
            raytrace[name].get("wavelength_nm", []), f"{name} wavelength axis"
        )
        if wave[0] < covered[0] - 1e-9 or wave[-1] > covered[-1] + 1e-9:
            raise ValueError(
                f"Requested wavelengths exceed the strict {name} coverage "
                f"[{covered[0]:g}, {covered[-1]:g}] nm."
            )

    psf_preflight = _psf_grid_preflight(raytrace, pitch)
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "global_scene_shape_row_col": list(shape),
        "optical_roi_row_col_exclusive": list(roi),
        "oi_pitch_um": pitch,
        "wavelength_nm": wave.tolist(),
        "psf_grid_preflight": psf_preflight,
        "max_geometry_query_cells": max_geometry_query_cells,
        "max_geometry_query_chunk_cells": max_geometry_query_chunk_cells,
        "geometry_query_work_budget_is_time_or_hardware_limit": False,
        "source_boundary_mode": "global_constant_zero",
        "all_field_azimuth_wavelength_sampled_psf_envelope": True,
        "source_query_bounds_conservative": True,
        "physical_psf_qualified": False,
        "physical_scene_halo_qualified": False,
        "product_fov_qualified": False,
        "full_frame_execution_qualified": False,
        "optical_or_sensor_calculation_executed": False,
    }
    if psf_preflight["status"] != "within_planner_grid_cap":
        return {
            **base,
            "status": "blocked_psf_grid_policy",
            "reason": psf_preflight["status"],
        }

    kernel = _raytrace_expected_kernel_shape(optics, pitch * 1e-6)
    if any(size < 1 or size % 2 != 1 for size in kernel):
        raise ValueError("Strict PSF must sample to a nonempty odd-sized kernel.")
    radius = ((kernel[0] - 1) // 2, (kernel[1] - 1) // 2)
    envelope = (
        roi[0] - radius[0], roi[1] + radius[0],
        roi[2] - radius[1], roi[3] + radius[1],
    )
    pregeometry = _intersection(envelope, shape)
    query_cells = (
        (pregeometry[1] - pregeometry[0])
        * (pregeometry[3] - pregeometry[2])
        * wave.size
    )
    base.update({
        "sampled_psf_kernel_shape_row_col": list(kernel),
        "sampled_psf_envelope_radius_samples_row_col": list(radius),
        "sampled_psf_envelope_row_col_exclusive_unclipped": list(envelope),
        "pregeometry_oi_row_col_exclusive": list(pregeometry),
        "sampled_psf_envelope_touches_global_scene_boundary": _outside(envelope, shape),
        "geometry_query_cells": int(query_cells),
    })
    if query_cells > max_geometry_query_cells:
        return {
            **base,
            "status": "blocked_geometry_query_policy",
            "reason": "Conservative pregeometry samples times wavelengths exceed the query policy.",
        }

    per_wave: list[dict[str, Any]] = []
    query_rects: list[tuple[int, int, int, int]] = []
    chunk_count = 0
    largest_chunk = 0
    for wavelength in wave:
        for name in ("psf", "relative_illumination"):
            _strict_raytrace_field_positions(
                raytrace, raytrace[name], float(wavelength)
            )
        ideal, distorted = _strict_raytrace_geometry_axes(raytrace, float(wavelength))
        query, wave_chunks, wave_largest = _chunked_geometry_query_bounds(
            pregeometry,
            shape,
            pitch * 1e-3,
            ideal,
            distorted,
            max_chunk_cells=max_geometry_query_chunk_cells,
        )
        chunk_count += wave_chunks
        largest_chunk = max(largest_chunk, wave_largest)
        query_rects.append(query)
        per_wave.append({
            "wavelength_nm": float(wavelength),
            "source_query_row_col_exclusive_unclipped": list(query),
            "source_query_row_col_exclusive_clipped": list(_intersection(query, shape)),
            "source_query_touches_global_scene_boundary": _outside(query, shape),
        })
    union = (
        min(query[0] for query in query_rects),
        max(query[1] for query in query_rects),
        min(query[2] for query in query_rects),
        max(query[3] for query in query_rects),
    )
    psf_touches = bool(base["sampled_psf_envelope_touches_global_scene_boundary"])
    source_touches = _outside(union, shape)
    boundary_touched = psf_touches or source_touches
    return {
        **base,
        "status": (
            "global_constant_zero_boundary_touched" if boundary_touched else "inside_declared_scene"
        ),
        "source_query_by_wavelength": per_wave,
        "geometry_query_chunk_count": chunk_count,
        "largest_geometry_query_chunk_cells": largest_chunk,
        "source_query_union_row_col_exclusive_unclipped": list(union),
        "source_query_union_row_col_exclusive_clipped": list(_intersection(union, shape)),
        "source_query_margin_to_global_boundary_samples_row_col": [
            union[0], shape[0] - union[1], union[2], shape[1] - union[3]
        ],
        "source_query_touches_global_scene_boundary": source_touches,
        "global_constant_zero_boundary_touched": boundary_touched,
        "declared_source_support_fits": not boundary_touched,
    }
