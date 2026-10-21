"""Diagnostic analytic Scene -> strict PSF -> off-axis sensor mean tile.

The optical output is restricted to the global OI samples required by one
sensor tile.  This path computes no full OI, full sensor RAW, ISP, or product
FOV.  Its resource estimates are admission policies, not hard RSS limits.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from pyisetcam.optics import (
    _image_distance_m,
    _raytrace_expected_kernel_shape,
    _raytrace_requested_sample_angles,
    _strict_raytrace_apply_psf_bounded_roi_provider,
    _strict_raytrace_cache_signature,
    _strict_raytrace_geometry_axes,
)
from pyisetcam.types import Scene, Sensor

from .lens_sensor_centered_oi import _sensor_contract
from .lens_sensor_offaxis_sensor_tile import (
    compute_fixed_sensor_tile_mean_electrons,
    preflight_fixed_sensor_tile_mean_electrons,
)
from .lens_sensor_optical_source_halo import preflight_strict_optical_source_halo
from .lens_sensor_scene_geometry_tile import (
    AnalyticStrictGeometryTileProvider,
    _optical_state_fingerprint,
)
from .lens_sensor_streamed_analytic_capture import (
    AnalyticCaptureResourcePolicy,
    _optics_array_payload_bytes,
    _tile_indices,
)
from .lens_sensor_tile_plan import (
    _geometry_query_bounds,
    _memory_policy,
    _psf_grid_preflight,
    _strict_roi_psf_cache_budget_for_declared_policy,
)

SCHEMA = "camera_lens_sensor_streamed_offaxis_mean_v1"


def _blocked(base: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {**base, "status": status, "reason": reason}


def _source_tile_shape(value: Any) -> tuple[int, int]:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or any(type(item) is not int or item < 1 for item in value)
    ):
        raise ValueError("Source tile shape requires two positive integers.")
    return value[0], value[1]


def preflight_streamed_analytic_offaxis_mean(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    *,
    sensor_tile_bounds_row_col_exclusive: tuple[int, int, int, int],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    resource_policy: AnalyticCaptureResourcePolicy,
) -> dict[str, Any]:
    """Plan an off-center mean tile before any Scene/PSF array work.

    This first variant requires the sensor's complete optical dependency and
    the entire sampled-PSF plus inverse-geometry source halo to fit inside
    the declared Scene.  Global zero continuation at Scene boundaries is
    therefore deliberately blocked, even when a native calculation exists.
    """
    if not isinstance(provider, AnalyticStrictGeometryTileProvider):
        raise ValueError("An analytic strict geometry tile provider is required.")
    if not isinstance(resource_policy, AnalyticCaptureResourcePolicy):
        raise ValueError("An explicit analytic capture resource policy is required.")
    if _optical_state_fingerprint(provider.optics) != provider.optics_state_sha256:
        raise ValueError("The provider optics changed after construction.")
    tile_shape = _source_tile_shape(source_tile_shape_row_col)
    sensor_shape, _, samples = _sensor_contract(sensor)
    angles = _raytrace_requested_sample_angles(1.0, sample_angles_deg)
    if not np.isclose(angles[-1], 360.0):
        raise ValueError("Strict PSF angles must end at 360 degrees.")
    image_distance = float(
        _image_distance_m(provider.optics, Scene(fields={"wave": provider.wavelengths_nm}, data={}))
    )
    expected_pitch_um = (
        2
        * image_distance
        * math.tan(math.radians(provider.horizontal_fov_deg) / 2)
        / provider.global_shape[1]
        * 1e6
    )
    if (
        not math.isfinite(expected_pitch_um)
        or expected_pitch_um <= 0
        or not math.isclose(provider.oi_pitch_um, expected_pitch_um, rel_tol=1e-9, abs_tol=1e-9)
        or not math.isclose(
            provider.oi_pitch_mm, expected_pitch_um * 1e-3, rel_tol=1e-9, abs_tol=1e-12
        )
    ):
        raise ValueError("Provider OI pitch disagrees with FOV and image distance.")
    shape = provider.global_shape
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "preflight_incomplete",
        "source_identity": provider.source_identity,
        "full_optical_state_sha256": provider.optics_state_sha256,
        "global_scene_shape_row_col": list(shape),
        "sensor_global_shape_row_col": list(sensor_shape),
        "sensor_tile_bounds_row_col_exclusive": list(sensor_tile_bounds_row_col_exclusive),
        "source_tile_shape_row_col": list(tile_shape),
        "sample_angles_deg": angles.tolist(),
        "wavelength_nm": provider.wavelengths_nm.tolist(),
        "resource_policy": resource_policy.as_dict(),
        "resource_policy_is_hardware_limit": False,
        "estimated_working_bytes_is_peak_rss_bound": False,
        "actual_peak_rss_bytes_measured": None,
        "hard_process_rss_limit_enforced": False,
        "physical_scene_halo_qualified": False,
        "full_frame_execution_qualified": False,
        "physical_fov_qualified": False,
        "readout_or_isp_executed": False,
    }
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
    psf_cache_budget = _strict_roi_psf_cache_budget_for_declared_policy(
        kernel_shape, resource_policy.estimated_working_budget_bytes
    )
    padding = tuple(2 * math.ceil((side // 2 + 2) / 2) for side in kernel_shape)
    full_oi_shape = (shape[0] + 2 * padding[0], shape[1] + 2 * padding[1])
    base.update(
        {
            "sampled_psf_kernel_shape_row_col": list(kernel_shape),
            "strict_psf_preparation_cache_budget_bytes": psf_cache_budget,
            "strict_psf_padding_row_col": list(padding),
            "full_padded_oi_shape_row_col": list(full_oi_shape),
        }
    )
    # The sensor preflight uses only requested tile subpixel coordinates.
    # Check the requested span before it allocates even those small vectors.
    bounds = sensor_tile_bounds_row_col_exclusive
    if (
        not isinstance(bounds, (tuple, list))
        or len(bounds) != 4
        or any(type(item) is not int for item in bounds)
        or not (0 <= bounds[0] < bounds[1] <= sensor_shape[0])
        or not (0 <= bounds[2] < bounds[3] <= sensor_shape[1])
    ):
        raise ValueError("Sensor tile requires four half-open bounds inside the full sensor.")
    tile_axis_cells = ((bounds[1] - bounds[0]) + (bounds[3] - bounds[2])) * samples
    base["sensor_tile_subpixel_axis_cells"] = tile_axis_cells
    if tile_axis_cells > resource_policy.max_geometry_query_cells:
        return _blocked(
            base,
            "blocked_sensor_axis_policy",
            "Sensor tile coordinate axes exceed the declared limit.",
        )
    sensor_plan = preflight_fixed_sensor_tile_mean_electrons(
        sensor,
        full_oi_shape_row_col=full_oi_shape,
        sensor_tile_bounds_row_col_exclusive=sensor_tile_bounds_row_col_exclusive,
        oi_sample_spacing_m=pitch_um * 1e-6,
    )
    base["sensor_preflight"] = sensor_plan
    (sr0, sr1), (sc0, sc1) = sensor_plan["required_global_oi_indices_inclusive_by_axis"]
    roi = (sr0 - padding[0], sr1 + 1 - padding[0], sc0 - padding[1], sc1 + 1 - padding[1])
    base["optical_roi_row_col_exclusive"] = list(roi)
    if (
        roi[0] < 0
        or roi[1] > shape[0]
        or roi[2] < 0
        or roi[3] > shape[1]
        or roi[1] - roi[0] < 2
        or roi[3] - roi[2] < 2
    ):
        return _blocked(
            base,
            "blocked_sensor_optical_boundary",
            "Sensor tile requires padded OI samples outside the Scene interior.",
        )
    output_cells = (roi[1] - roi[0]) * (roi[3] - roi[2])
    base["optical_roi_cells"] = output_cells
    if output_cells > resource_policy.max_output_roi_cells:
        return _blocked(
            base, "blocked_output_roi_policy", "Optical ROI exceeds declared output cells."
        )
    halo = preflight_strict_optical_source_halo(
        provider.optics,
        global_scene_shape_row_col=shape,
        optical_roi_row_col_exclusive=roi,
        oi_pitch_um=pitch_um,
        wavelengths_nm=provider.wavelengths_nm,
        max_geometry_query_cells=resource_policy.max_geometry_query_cells,
        max_geometry_query_chunk_cells=resource_policy.max_geometry_query_chunk_cells,
    )
    base["stored_psf_numeric_halo"] = halo
    if halo["status"] == "blocked_geometry_query_policy":
        return _blocked(
            base,
            "blocked_halo_query_policy",
            "Stored-PSF halo query exceeds the declared geometry policy.",
        )
    if halo["status"] != "inside_declared_scene":
        return _blocked(
            base,
            (
                "blocked_scene_halo"
                if halo["status"] == "global_constant_zero_boundary_touched"
                else "blocked_psf_grid_policy"
            ),
            (
                "Stored PSF or inverse geometry queries the declared Scene boundary."
                if halo["status"] == "global_constant_zero_boundary_touched"
                else f"Stored-PSF halo preflight failed: {halo['status']}"
            ),
        )

    row_tiles = _tile_indices(roi[0], roi[1], padding[0], tile_shape[0], shape[0])
    col_tiles = _tile_indices(roi[2], roi[3], padding[1], tile_shape[1], shape[1])
    tile_count = len(row_tiles) * len(col_tiles)
    bands = len(provider.wavelengths_nm)
    base["candidate_source_tile_count"] = tile_count
    if tile_count > resource_policy.max_source_tiles:
        return _blocked(
            base, "blocked_source_tile_count_policy", "Source tile count exceeds declared limit."
        )
    conservative_queries = tile_count * tile_shape[0] * tile_shape[1] * bands
    base["conservative_geometry_query_cells"] = conservative_queries
    if (
        conservative_queries + int(halo["geometry_query_cells"])
        > resource_policy.max_geometry_query_cells
    ):
        return _blocked(
            base,
            "blocked_geometry_query_policy",
            "Source tiles plus halo queries exceed declared geometry policy.",
        )

    raytrace = provider.optics["raytrace"]
    source_psf_shape = tuple(np.asarray(raytrace["psf"]["function"]).shape[:2])
    field_count = len(raytrace["psf"]["field_height_mm"])
    maximum_tile_estimate = 0
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
                queries.append(
                    _geometry_query_bounds(
                        (r0, r1, c0, c1), shape, pitch_um * 1e-3, ideal, distorted
                    )
                )
            source = (
                max(0, min(item[0] for item in queries)),
                min(shape[0], max(item[1] for item in queries)),
                max(0, min(item[2] for item in queries)),
                min(shape[1], max(item[3] for item in queries)),
            )
            source_shape = (
                max(1, source[1] - source[0]),
                max(1, source[3] - source[2]),
            )
            if source_shape[0] * source_shape[1] > min(
                provider.max_tile_cells, resource_policy.max_source_tile_cells
            ):
                return _blocked(
                    {**base, "worst_source_query_row_col_exclusive": list(source)},
                    "blocked_source_query_tile_policy",
                    "A distorted tile requires more Scene source cells than declared.",
                )
            memory = _memory_policy(
                source_shape,
                (r1 - r0, c1 - c0),
                kernel_shape,
                source_psf_shape,
                field_count,
                bands,
                shared_psf_cache_budget_bytes=psf_cache_budget,
            )
            if memory["combined_policy_estimate_bytes"] > maximum_tile_estimate:
                maximum_tile_estimate = memory["combined_policy_estimate_bytes"]
                worst_tile = {
                    "tile_row_col_exclusive": [r0, r1, c0, c1],
                    "source_query_row_col_exclusive": list(source),
                    "tile_memory_policy": memory,
                }
    sensor_tile = sensor_plan["sensor_tile_bounds_row_col_exclusive"]
    sensor_tile_cells = (sensor_tile[1] - sensor_tile[0]) * (sensor_tile[3] - sensor_tile[2])
    output_bytes = output_cells * bands * 8
    # Conservative scratch for current-density planes, subpixel mosaics,
    # interpolation, and the returned mean. Not a process peak-RSS bound.
    sensor_scratch_bytes = (
        output_cells * bands * 16 + sensor_tile_cells * samples**2 * (bands + 8) * 8
    )
    static_bytes = _optics_array_payload_bytes(provider)
    total = static_bytes + maximum_tile_estimate + output_bytes + sensor_scratch_bytes
    base.update(
        {
            "static_optics_ndarray_payload_bytes": static_bytes,
            "optical_roi_float64_bytes": output_bytes,
            "sensor_scratch_policy_bytes": sensor_scratch_bytes,
            "maximum_per_source_tile_policy_estimate_bytes": maximum_tile_estimate,
            "combined_policy_estimate_bytes": total,
            "worst_source_tile": worst_tile,
        }
    )
    if total > resource_policy.estimated_working_budget_bytes:
        return _blocked(
            base, "blocked_working_memory_policy", "Working estimate exceeds declared policy."
        )
    return {**base, "status": "ready_under_declared_policy"}


def compute_streamed_analytic_offaxis_mean(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    *,
    sensor_tile_bounds_row_col_exclusive: tuple[int, int, int, int],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    resource_policy: AnalyticCaptureResourcePolicy,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Run an admitted off-center optical/sensor tile through mean electrons."""
    plan = preflight_streamed_analytic_offaxis_mean(
        provider,
        sensor,
        sensor_tile_bounds_row_col_exclusive=sensor_tile_bounds_row_col_exclusive,
        source_tile_shape_row_col=source_tile_shape_row_col,
        sample_angles_deg=sample_angles_deg,
        resource_policy=resource_policy,
    )
    if plan["status"] != "ready_under_declared_policy":
        raise ValueError(f"resource_blocked: {plan['status']}: {plan['reason']}")
    return _compute_preflighted_streamed_analytic_offaxis_mean(
        provider,
        sensor,
        sensor_tile_bounds_row_col_exclusive=sensor_tile_bounds_row_col_exclusive,
        source_tile_shape_row_col=source_tile_shape_row_col,
        sample_angles_deg=sample_angles_deg,
        resource_policy=resource_policy,
        admitted_plan=plan,
    )


def _compute_preflighted_streamed_analytic_offaxis_mean(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    *,
    sensor_tile_bounds_row_col_exclusive: tuple[int, int, int, int],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    resource_policy: AnalyticCaptureResourcePolicy,
    admitted_plan: dict[str, Any],
) -> tuple[np.ndarray, dict[str, Any]]:
    """Execute one plan admitted by this process's complete-tile preflight.

    This private path avoids repeating the expensive inverse-geometry and halo
    scans for each tile. The caller must validate the complete plan spool before
    any optics run, then retain the same sensor/provider state during execution.
    The public entry point above still performs its own full preflight.
    """
    plan = admitted_plan
    angles = _raytrace_requested_sample_angles(1.0, sample_angles_deg)
    sensor_shape, _, _ = _sensor_contract(sensor)
    selected_cache_budget = _strict_roi_psf_cache_budget_for_declared_policy(
        tuple(plan["sampled_psf_kernel_shape_row_col"]),
        resource_policy.estimated_working_budget_bytes,
    )
    if (
        plan.get("schema") != SCHEMA
        or plan.get("status") != "ready_under_declared_policy"
        or plan.get("source_identity") != provider.source_identity
        or plan.get("full_optical_state_sha256") != provider.optics_state_sha256
        or _optical_state_fingerprint(provider.optics) != provider.optics_state_sha256
        or plan.get("global_scene_shape_row_col") != list(provider.global_shape)
        or plan.get("sensor_global_shape_row_col") != list(sensor_shape)
        or plan.get("sensor_tile_bounds_row_col_exclusive")
        != list(sensor_tile_bounds_row_col_exclusive)
        or plan.get("source_tile_shape_row_col") != list(source_tile_shape_row_col)
        or plan.get("sample_angles_deg") != angles.tolist()
        or plan.get("wavelength_nm") != provider.wavelengths_nm.tolist()
        or plan.get("resource_policy") != resource_policy.as_dict()
        or plan.get("strict_psf_preparation_cache_budget_bytes") != selected_cache_budget
    ):
        raise ValueError("Admitted optical tile plan or bound model changed before execution.")
    roi_bounds = tuple(plan["optical_roi_row_col_exclusive"])
    padding = tuple(plan["strict_psf_padding_row_col"])
    spacing_m = provider.oi_pitch_um * 1e-6
    optical, optical_evidence = _strict_raytrace_apply_psf_bounded_roi_provider(
        provider.global_shape,
        provider._read_tile_after_integrity_preflight,
        provider.optics,
        provider.wavelengths_nm,
        spacing_m,
        padding,
        np.asarray(sample_angles_deg, dtype=float),
        output_roi_row_col=roi_bounds,
        source_identity=provider.source_identity,
        source_tile_shape_row_col=source_tile_shape_row_col,
        shared_psf_cache_budget_bytes=selected_cache_budget,
        _validated_optics_once_per_roi=True,
    )
    if (
        optical_evidence.get("source_identity") != provider.source_identity
        or optical_evidence.get("source_signature")
        != _strict_raytrace_cache_signature(provider.optics)
        or optical_evidence.get("output_roi_row_col") != list(roi_bounds)
        or optical_evidence.get("shared_psf_preparation_cache_budget_bytes")
        != selected_cache_budget
    ):
        raise ValueError("Executed optical ROI lost provider identity or global bounds.")
    padded_roi_bounds = (
        roi_bounds[0] + padding[0],
        roi_bounds[1] + padding[0],
        roi_bounds[2] + padding[1],
        roi_bounds[3] + padding[1],
    )
    mean, sensor_evidence = compute_fixed_sensor_tile_mean_electrons(
        sensor,
        optical,
        full_oi_shape_row_col=tuple(plan["full_padded_oi_shape_row_col"]),
        optical_roi_bounds_row_col_exclusive=padded_roi_bounds,
        sensor_tile_bounds_row_col_exclusive=sensor_tile_bounds_row_col_exclusive,
        wavelength_nm=provider.wavelengths_nm,
        oi_sample_spacing_m=spacing_m,
    )
    if (
        sensor_evidence["required_global_oi_indices_inclusive_by_axis"]
        != plan["sensor_preflight"]["required_global_oi_indices_inclusive_by_axis"]
    ):
        raise ValueError("Sensor dependency changed after optical ROI preflight.")
    if _optical_state_fingerprint(provider.optics) != provider.optics_state_sha256:
        raise ValueError("The provider optics changed during off-axis mean calculation.")
    report = {
        "schema": SCHEMA,
        "status": "diagnostic_offaxis_mean_executed",
        "provider": provider.metadata,
        "full_optical_state_sha256": provider.optics_state_sha256,
        "full_optical_state_verified_before_and_after": True,
        "resource_preflight": plan,
        "optical_execution": optical_evidence,
        "sensor_execution": sensor_evidence,
        "output_mean_electrons_shape_row_col": list(mean.shape),
        "readout_or_isp_executed": False,
        "noise_or_fixed_pattern_executed": False,
        "full_frame_execution_qualified": False,
        "physical_fov_qualified": False,
        "physical_scene_halo_qualified": False,
        "actual_peak_rss_bytes_measured": None,
        "hard_process_rss_limit_enforced": False,
    }
    return mean, report
