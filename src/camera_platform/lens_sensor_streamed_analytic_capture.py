"""Diagnostic analytic Scene -> strict tiled optics -> fixed digital RAW.

This in-process library path has an explicit admission policy. Its estimates
are not peak-RSS measurements or a hard process memory boundary. The analytic
Scene, geometric tile provider, strict optical ROI, and CameraE2E sensor are
kept as distinct operations with their existing numerical contracts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from pyisetcam.optics import (
    _image_distance_m,
    _raytrace_expected_kernel_shape,
    _raytrace_requested_sample_angles,
    _strict_raytrace_apply_psf_bounded_roi_provider,
    _strict_raytrace_geometry_axes,
)
from pyisetcam.types import Scene, Sensor

from .lens_sensor_centered_oi import _required_axis, _sensor_contract
from .lens_sensor_optical_source_halo import (
    DEFAULT_MAX_GEOMETRY_QUERY_CHUNK_CELLS,
    preflight_strict_optical_source_halo,
)
from .lens_sensor_roi_capture import capture_fixed_sensor_from_optical_roi
from .lens_sensor_scene_geometry_tile import (
    AnalyticStrictGeometryTileProvider,
    _optical_state_fingerprint,
)
from .lens_sensor_tile_plan import (
    DEFAULT_POLICY_BUDGET_BYTES,
    _geometry_query_bounds,
    _memory_policy,
    _psf_grid_preflight,
)

SCHEMA = "camera_lens_sensor_streamed_analytic_capture_v1"
MAX_DIAGNOSTIC_SOURCE_TILES = 4096
MAX_DIAGNOSTIC_OUTPUT_ROI_CELLS = 1_048_576


@dataclass(frozen=True)
class AnalyticCaptureResourcePolicy:
    """Caller-declared admission limits, independent of physical memory size.

    Geometry-query cells are a total work budget, not a resident-array size.
    The default is deliberately conservative; callers may opt into more work
    while the halo inverse-map scan remains bounded by its separate chunk cap.
    """

    estimated_working_budget_bytes: int
    max_geometry_query_cells: int
    max_source_tiles: int
    max_output_roi_cells: int
    max_source_tile_cells: int
    max_geometry_query_chunk_cells: int = DEFAULT_MAX_GEOMETRY_QUERY_CHUNK_CELLS

    def __post_init__(self) -> None:
        limits = {
            "estimated_working_budget_bytes": DEFAULT_POLICY_BUDGET_BYTES,
            "max_source_tiles": MAX_DIAGNOSTIC_SOURCE_TILES,
            "max_output_roi_cells": MAX_DIAGNOSTIC_OUTPUT_ROI_CELLS,
            "max_source_tile_cells": MAX_DIAGNOSTIC_OUTPUT_ROI_CELLS,
        }
        for name, ceiling in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(
                    f"{name} must be a positive integer at most {ceiling}; "
                    "these are diagnostic admission policies, not hardware limits."
                )
        if type(self.max_geometry_query_cells) is not int or self.max_geometry_query_cells < 1:
            raise ValueError("max_geometry_query_cells must be a positive integer work budget.")
        if (
            type(self.max_geometry_query_chunk_cells) is not int
            or not 1
            <= self.max_geometry_query_chunk_cells
            <= DEFAULT_MAX_GEOMETRY_QUERY_CHUNK_CELLS
        ):
            raise ValueError(
                "max_geometry_query_chunk_cells must be a positive integer at most "
                f"{DEFAULT_MAX_GEOMETRY_QUERY_CHUNK_CELLS}."
            )

    def as_dict(self) -> dict[str, int]:
        return {
            name: getattr(self, name)
            for name in (
                "estimated_working_budget_bytes",
                "max_geometry_query_cells",
                "max_source_tiles",
                "max_output_roi_cells",
                "max_source_tile_cells",
                "max_geometry_query_chunk_cells",
            )
        }


def _bounds(
    value: tuple[int, int, int, int], shape: tuple[int, int]
) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, (tuple, list)) or len(value) != 4
        or any(type(item) is not int for item in value)
    ):
        raise ValueError("Optical ROI requires four integer half-open bounds.")
    r0, r1, c0, c1 = value
    if not (0 <= r0 < r1 <= shape[0] and 0 <= c0 < c1 <= shape[1]):
        raise ValueError("Optical ROI lies outside the global Scene/OI grid.")
    if r0 + r1 != shape[0] or c0 + c1 != shape[1]:
        raise ValueError("Off-center optical ROI cannot preserve the current sensor origin.")
    if r1 - r0 < 2 or c1 - c0 < 2:
        raise ValueError("Centered optical ROI needs at least two samples per axis.")
    return r0, r1, c0, c1


def _tile_indices(
    lower: int, upper: int, pad: int, tile: int, global_count: int
) -> range:
    first = max(0, (lower - pad) // tile)
    last = min((global_count - 1) // tile, (upper + pad - 1) // tile)
    return range(first, last + 1)


def _blocked(base: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {**base, "status": status, "reason": reason}


def _optics_array_payload_bytes(provider: AnalyticStrictGeometryTileProvider) -> int:
    """Count known resident ndarray payloads once, excluding Python overhead."""
    visited: set[int] = set()

    def walk(value: Any) -> int:
        identity = id(value)
        if identity in visited:
            return 0
        visited.add(identity)
        if isinstance(value, np.ndarray):
            return int(value.nbytes)
        if isinstance(value, dict):
            return sum(walk(item) for item in value.values())
        if isinstance(value, (tuple, list)):
            return sum(walk(item) for item in value)
        return 0

    return (walk(provider.optics) + walk(provider.wavelengths_nm) + walk(provider.bright_photons)
            + int(getattr(provider, "resident_source_payload_bytes", 0)))


def preflight_streamed_analytic_capture(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    *,
    optical_roi_bounds: tuple[int, int, int, int],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    resource_policy: AnalyticCaptureResourcePolicy,
) -> dict[str, Any]:
    """Admit only bounded, centered native RAW diagnostics before PSF work."""
    if not isinstance(provider, AnalyticStrictGeometryTileProvider):
        raise ValueError("An analytic strict geometry tile provider is required.")
    if not isinstance(resource_policy, AnalyticCaptureResourcePolicy):
        raise ValueError("An explicit analytic capture resource policy is required.")
    if _optical_state_fingerprint(provider.optics) != provider.optics_state_sha256:
        raise ValueError("The provider optics changed after construction.")
    image_distance = float(_image_distance_m(
        provider.optics, Scene(fields={"wave": provider.wavelengths_nm}, data={})
    ))
    expected_pitch_um = (
        2 * image_distance * math.tan(math.radians(provider.horizontal_fov_deg) / 2)
        / provider.global_shape[1] * 1e6
    )
    if (
        not math.isfinite(expected_pitch_um) or expected_pitch_um <= 0
        or not math.isclose(
            float(provider.oi_pitch_um), expected_pitch_um, rel_tol=1e-9, abs_tol=1e-9
        )
        or not math.isclose(
            float(provider.oi_pitch_mm), expected_pitch_um * 1e-3,
            rel_tol=1e-9, abs_tol=1e-12,
        )
    ):
        raise ValueError("Provider OI pitch disagrees with FOV and image distance.")
    shape = provider.global_shape
    bounds = _bounds(optical_roi_bounds, shape)
    if (
        not isinstance(source_tile_shape_row_col, (tuple, list))
        or len(source_tile_shape_row_col) != 2
        or any(type(value) is not int or value < 1 for value in source_tile_shape_row_col)
    ):
        raise ValueError("Source tile shape requires two positive integers.")
    tile_shape = tuple(source_tile_shape_row_col)
    sensor_shape, pixel_size, samples = _sensor_contract(sensor)
    angles = _raytrace_requested_sample_angles(1.0, sample_angles_deg)
    if not np.isclose(angles[-1], 360.0):
        raise ValueError("Strict PSF angles must end at 360 degrees.")
    roi_cells = (bounds[1] - bounds[0]) * (bounds[3] - bounds[2])
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "preflight_incomplete",
        "source_identity": provider.source_identity,
        "full_optical_state_sha256": provider.optics_state_sha256,
        "global_scene_shape_row_col": list(shape),
        "optical_roi_bounds_row_col_exclusive": list(bounds),
        "source_tile_shape_row_col": list(tile_shape),
        "wavelength_nm": provider.wavelengths_nm.tolist(),
        "sample_angles_deg": angles.tolist(),
        "sensor_shape_row_col": list(sensor_shape),
        "sensor_pixel_size_m_row_col": pixel_size.tolist(),
        "resource_policy": resource_policy.as_dict(),
        "static_optics_ndarray_payload_bytes": _optics_array_payload_bytes(provider),
        "static_optics_payload_scope": "known_provider_ndarrays_excluding_python_overhead",
        "resource_policy_is_hardware_limit": False,
        "estimated_working_bytes_is_peak_rss_bound": False,
        "actual_peak_rss_bytes_measured": None,
        "hard_process_rss_limit_enforced": False,
        "scene_halo_qualified": False,
        "full_frame_execution_qualified": False,
        "physical_fov_qualified": False,
        "worker_submission": False,
    }
    if roi_cells > resource_policy.max_output_roi_cells:
        return _blocked(
            base, "blocked_output_roi_policy", "Optical ROI exceeds declared output cells."
        )
    if tile_shape[0] * tile_shape[1] > min(
        provider.max_tile_cells, resource_policy.max_source_tile_cells
    ):
        return _blocked(
            base, "blocked_source_tile_policy", "Source tile exceeds declared tile cells."
        )

    pitch_um = provider.oi_pitch_um
    psf_grid = _psf_grid_preflight(provider.optics["raytrace"], pitch_um)
    base["psf_grid_preflight"] = psf_grid
    if psf_grid["status"] != "within_planner_grid_cap":
        return _blocked(base, "blocked_psf_grid_policy", psf_grid["status"])
    kernel_shape = _raytrace_expected_kernel_shape(provider.optics, pitch_um * 1e-6)
    if any(side <= 0 for side in kernel_shape):
        raise ValueError("Strict PSF has no sampled kernel support.")
    padding = tuple(2 * math.ceil((side // 2 + 2) / 2) for side in kernel_shape)
    base["sampled_psf_kernel_shape_row_col"] = list(kernel_shape)
    base["strict_psf_padding_row_col"] = list(padding)

    row_tiles = _tile_indices(bounds[0], bounds[1], padding[0], tile_shape[0], shape[0])
    col_tiles = _tile_indices(bounds[2], bounds[3], padding[1], tile_shape[1], shape[1])
    tile_count = len(row_tiles) * len(col_tiles)
    base["candidate_source_tile_count"] = tile_count
    if tile_count > resource_policy.max_source_tiles:
        return _blocked(
            base, "blocked_source_tile_count_policy", "Source tile count exceeds declared limit."
        )
    bands = len(provider.wavelengths_nm)
    max_queries = tile_count * tile_shape[0] * tile_shape[1] * bands
    base["conservative_geometry_query_cells"] = max_queries
    if max_queries > resource_policy.max_geometry_query_cells:
        return _blocked(
            base, "blocked_geometry_query_policy", "Geometry queries exceed declared limit."
        )

    # The reusable halo check handles centered and arbitrary off-center ROIs.
    # It proves only that this finite stored-PSF/inverse-geometry operator does
    # not need the declared constant-zero continuation for this ROI.
    halo = preflight_strict_optical_source_halo(
        provider.optics,
        global_scene_shape_row_col=shape,
        optical_roi_row_col_exclusive=bounds,
        oi_pitch_um=pitch_um,
        wavelengths_nm=provider.wavelengths_nm,
        max_geometry_query_cells=resource_policy.max_geometry_query_cells,
        max_geometry_query_chunk_cells=resource_policy.max_geometry_query_chunk_cells,
    )
    if halo["status"] == "blocked_geometry_query_policy":
        return _blocked(
            {**base, "stored_psf_numeric_halo": halo},
            "blocked_halo_query_policy",
            "Stored-PSF halo query exceeds the declared geometry policy.",
        )
    if halo["status"] not in {
        "inside_declared_scene", "global_constant_zero_boundary_touched"
    }:
        return _blocked(
            {**base, "stored_psf_numeric_halo": halo},
            "blocked_psf_grid_policy", halo["status"],
        )
    halo_queries = int(halo["geometry_query_cells"])
    base["additional_halo_geometry_query_cells"] = halo_queries
    if max_queries + halo_queries > resource_policy.max_geometry_query_cells:
        return _blocked(
            {**base, "stored_psf_numeric_halo": halo},
            "blocked_halo_query_policy",
            "Stored-PSF halo query exceeds the declared geometry policy.",
        )
    # Retain these compact report keys for the centered diagnostic API.
    halo["full_sampled_psf_half_support_row_col"] = halo[
        "sampled_psf_envelope_radius_samples_row_col"
    ]
    halo["required_pregeometry_row_col_exclusive"] = halo[
        "sampled_psf_envelope_row_col_exclusive_unclipped"
    ]
    halo["pregeometry_inside_declared_scene"] = not halo[
        "sampled_psf_envelope_touches_global_scene_boundary"
    ]
    halo["required_source_scene_row_col_exclusive"] = halo[
        "source_query_union_row_col_exclusive_unclipped"
    ]
    halo["stored_psf_numeric_halo_inside_declared_scene"] = halo[
        "declared_source_support_fits"
    ]
    base["stored_psf_numeric_halo"] = halo

    full_shape = (shape[0] + 2 * padding[0], shape[1] + 2 * padding[1])
    padded_origin = (bounds[0] + padding[0], bounds[2] + padding[1])
    roi_shape = (bounds[1] - bounds[0], bounds[3] - bounds[2])
    # _required_axis creates 1D coordinate vectors. Count them under the same
    # explicit query policy before allocating even those small vectors.
    axis_cells = sum(full_shape) + sum(sensor_shape) * samples
    if axis_cells > resource_policy.max_geometry_query_cells:
        return _blocked(
            base, "blocked_sensor_axis_policy", "Sensor coordinate axes exceed declared limit."
        )
    dependencies = [
        _required_axis(full, target, pitch_um * 1e-6, float(pixel), samples)
        for full, target, pixel in zip(full_shape, sensor_shape, pixel_size, strict=True)
    ]
    for axis, (origin, extent, sensor_pixels, pixel) in enumerate(zip(
        padded_origin, roi_shape, sensor_shape, pixel_size, strict=True
    )):
        first, last = dependencies[axis]["required_full_oi_source_indices_inclusive"]
        if origin > first or origin + extent - 1 < last:
            raise ValueError(
                f"Optical ROI omits sensor interpolation/convolution halo on axis {axis}."
            )
        if extent * pitch_um * 1e-6 + 1e-15 < sensor_pixels * float(pixel):
            raise ValueError(f"Optical ROI misses active pixel area on axis {axis}.")
    base["sensor_dependency_by_axis"] = dependencies

    raytrace = provider.optics["raytrace"]
    source_psf_shape = tuple(np.asarray(raytrace["psf"]["function"]).shape[:2])
    field_count = len(raytrace["psf"]["field_height_mm"])
    pitch_mm = pitch_um * 1e-3
    maximum_estimate = 0
    worst_tile: dict[str, Any] | None = None
    for row_index in row_tiles:
        r0 = row_index * tile_shape[0]
        r1 = min(r0 + tile_shape[0], shape[0])
        for col_index in col_tiles:
            c0 = col_index * tile_shape[1]
            c1 = min(c0 + tile_shape[1], shape[1])
            queries = []
            for wavelength in provider.wavelengths_nm:
                ideal, distorted = _strict_raytrace_geometry_axes(raytrace, float(wavelength))
                queries.append(_geometry_query_bounds(
                    (r0, r1, c0, c1), shape, pitch_mm, ideal, distorted
                ))
            source = (
                max(0, min(item[0] for item in queries)),
                min(shape[0], max(item[1] for item in queries)),
                max(0, min(item[2] for item in queries)),
                min(shape[1], max(item[3] for item in queries)),
            )
            source_shape = (
                max(1, source[1] - source[0]), max(1, source[3] - source[2])
            )
            source_cells = source_shape[0] * source_shape[1]
            if source_cells > min(
                provider.max_tile_cells, resource_policy.max_source_tile_cells
            ):
                return _blocked(
                    {**base, "worst_source_query_row_col_exclusive": list(source)},
                    "blocked_source_query_tile_policy",
                    "A distorted tile requires more Scene source cells than declared.",
                )
            memory = _memory_policy(
                source_shape, (r1 - r0, c1 - c0), kernel_shape,
                source_psf_shape, field_count, bands,
            )
            estimate = memory["combined_policy_estimate_bytes"]
            if estimate > maximum_estimate:
                maximum_estimate = estimate
                worst_tile = {
                    "tile_row_col_exclusive": [r0, r1, c0, c1],
                    "source_query_row_col_exclusive": list(source),
                    "tile_memory_policy": memory,
                }
    output_bytes = roi_cells * bands * 8
    sensor_bytes = sensor_shape[0] * sensor_shape[1] * 16 * 8
    total_estimate = (
        base["static_optics_ndarray_payload_bytes"]
        + maximum_estimate + output_bytes + sensor_bytes
    )
    base.update({
        "full_uncropped_oi_shape_row_col": list(full_shape),
        "optical_roi_float64_bytes": output_bytes,
        "sensor_scratch_policy_bytes": sensor_bytes,
        "maximum_per_source_tile_policy_estimate_bytes": maximum_estimate,
        "combined_policy_estimate_bytes": total_estimate,
        "worst_source_tile": worst_tile,
    })
    if total_estimate > resource_policy.estimated_working_budget_bytes:
        return _blocked(
            base, "blocked_working_memory_policy", "Working estimate exceeds declared policy."
        )
    return {**base, "status": "ready_under_declared_policy"}


def capture_streamed_analytic_center_roi(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    *,
    optical_roi_bounds: tuple[int, int, int, int],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    seed: int | None,
    resource_policy: AnalyticCaptureResourcePolicy,
) -> tuple[Sensor, dict[str, Any]]:
    """Execute admitted in-process diagnostic RAW for one analytic center ROI."""
    preflight = preflight_streamed_analytic_capture(
        provider, sensor,
        optical_roi_bounds=optical_roi_bounds,
        source_tile_shape_row_col=source_tile_shape_row_col,
        sample_angles_deg=sample_angles_deg,
        resource_policy=resource_policy,
    )
    if preflight["status"] != "ready_under_declared_policy":
        raise ValueError(f"resource_blocked: {preflight['status']}: {preflight['reason']}")
    wave = provider.wavelengths_nm
    spacing_m = float(provider.oi_pitch_um * 1e-6)
    padding = tuple(preflight["strict_psf_padding_row_col"])
    roi, optical_evidence = _strict_raytrace_apply_psf_bounded_roi_provider(
        provider.global_shape, provider.read_tile, provider.optics, wave,
        spacing_m, padding, np.asarray(sample_angles_deg, dtype=float),
        output_roi_row_col=optical_roi_bounds,
        source_identity=provider.source_identity,
        source_tile_shape_row_col=source_tile_shape_row_col,
    )
    image_distance = _image_distance_m(
        provider.optics, Scene(fields={"wave": wave}, data={})
    )
    captured, sensor_evidence = capture_fixed_sensor_from_optical_roi(
        roi, provider.global_shape, optical_roi_bounds,
        provider.optics, wave, spacing_m, image_distance,
        sensor, optical_evidence, seed,
    )
    if _optical_state_fingerprint(provider.optics) != provider.optics_state_sha256:
        raise ValueError("The provider optics changed during diagnostic capture.")
    report = {
        "schema": SCHEMA,
        "status": "diagnostic_raw_executed",
        "provider": provider.metadata,
        "full_optical_state_sha256": provider.optics_state_sha256,
        "full_optical_state_verified_before_and_after": True,
        "resource_preflight": preflight,
        "stored_psf_numeric_halo": preflight["stored_psf_numeric_halo"],
        "optical_execution": optical_evidence,
        "sensor_execution": sensor_evidence,
        "raw_shape_row_col": list(captured.data["raw_dn"].shape),
        "scene_halo_qualified": False,
        "full_frame_execution_qualified": False,
        "physical_fov_qualified": False,
        "worker_submission": False,
        "actual_peak_rss_bytes_measured": None,
        "hard_process_rss_limit_enforced": False,
    }
    return captured, report
