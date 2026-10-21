"""Stitch strict optical/sensor mean tiles before one native digital RAW readout.

This diagnostic keeps the global sensor coordinates and CFA phase of every
tile. The complete mean-electron image is assembled first; CameraE2E then
draws shot/read noise, fixed patterns, gain and ADC values once over the full
sensor. The optical Scene/OI need not be materialized, but the native readout
still constructs several full-size arrays. Admission estimates are policies,
not a hard peak-RSS limit or physical/product FOV qualification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from pyisetcam.sensor_capture import resample_sensor_capture
from pyisetcam.types import Sensor

from .lens_sensor_centered_oi import _sensor_contract
from .lens_sensor_scene_geometry_tile import (
    AnalyticStrictGeometryTileProvider,
    _optical_state_fingerprint,
)
from .lens_sensor_streamed_analytic_capture import AnalyticCaptureResourcePolicy
from .lens_sensor_streamed_offaxis_mean import (
    compute_streamed_analytic_offaxis_mean,
    preflight_streamed_analytic_offaxis_mean,
)

SCHEMA = "camera_lens_sensor_streamed_full_raw_v1"
# NumPy's native readout builds multiple full-sized float64, int32 and FPN
# arrays and deep-copies the Sensor. This factor is deliberately conservative
# but cannot bound interpreter, allocator, or backend peak RSS.
READOUT_POLICY_BYTES_PER_SENSOR_CELL = 256


@dataclass(frozen=True)
class StitchedRawResourcePolicy:
    """Explicit numerical admission policy; none of its caps are hardware facts."""

    optical_tile: AnalyticCaptureResourcePolicy
    estimated_working_budget_bytes: int
    max_full_sensor_cells: int
    max_sensor_tiles: int

    def __post_init__(self) -> None:
        if not isinstance(self.optical_tile, AnalyticCaptureResourcePolicy):
            raise ValueError("An optical tile resource policy is required.")
        for name in (
            "estimated_working_budget_bytes",
            "max_full_sensor_cells",
            "max_sensor_tiles",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer policy.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "optical_tile": self.optical_tile.as_dict(),
            "estimated_working_budget_bytes": self.estimated_working_budget_bytes,
            "max_full_sensor_cells": self.max_full_sensor_cells,
            "max_sensor_tiles": self.max_sensor_tiles,
        }


def _sensor_identity(sensor: Sensor) -> str:
    # Reject unsupported mutable/callable model values rather than identifying
    # them by an unstable repr. Capture arrays are not model input here.
    return _optical_state_fingerprint(
        {
            "name": sensor.name,
            "type": sensor.type,
            "fields": sensor.fields,
            "metadata": sensor.metadata,
        }
    )


def _array_payload_bytes(sensor: Sensor) -> int:
    """Count known resident ndarray payloads once, excluding Python overhead."""
    visited: set[int] = set()

    def walk(item: Any) -> int:
        identity = id(item)
        if identity in visited:
            return 0
        visited.add(identity)
        if isinstance(item, np.ndarray):
            return int(item.nbytes)
        if isinstance(item, dict):
            return sum(walk(child) for child in item.values())
        if isinstance(item, (tuple, list)):
            return sum(walk(child) for child in item)
        return 0

    return walk(sensor.fields) + walk(sensor.metadata)


def _complete_tiles(
    tiles: tuple[tuple[int, int, int, int], ...] | list[tuple[int, int, int, int]],
    shape: tuple[int, int],
) -> tuple[tuple[int, int, int, int], ...]:
    """Verify exact rectangular coverage without allocating a full pixel mask."""
    if not isinstance(tiles, (tuple, list)) or not tiles:
        raise ValueError("A nonempty finite sensor tile list is required.")
    result: list[tuple[int, int, int, int]] = []
    starts: dict[int, list[int]] = {}
    ends: dict[int, list[int]] = {}
    row_edges = {0, shape[0]}
    for index, value in enumerate(tiles):
        if (
            not isinstance(value, (tuple, list))
            or len(value) != 4
            or any(type(item) is not int for item in value)
        ):
            raise ValueError("Sensor tiles require four integer half-open bounds.")
        r0, r1, c0, c1 = value
        if not (0 <= r0 < r1 <= shape[0] and 0 <= c0 < c1 <= shape[1]):
            raise ValueError("Sensor tile lies outside the declared full sensor.")
        result.append((r0, r1, c0, c1))
        starts.setdefault(r0, []).append(index)
        ends.setdefault(r1, []).append(index)
        row_edges.update((r0, r1))

    active: set[int] = set()
    edges = sorted(row_edges)
    for row in edges[:-1]:
        active.difference_update(ends.get(row, ()))
        active.update(starts.get(row, ()))
        cursor = 0
        for c0, c1 in sorted((result[index][2], result[index][3]) for index in active):
            if c0 != cursor:
                issue = "overlap" if c0 < cursor else "gap"
                raise ValueError(f"Sensor tiles have a {issue} in row band starting {row}.")
            cursor = c1
        if cursor != shape[1]:
            raise ValueError(f"Sensor tiles have a gap in row band starting {row}.")
    return tuple(sorted(result))


def _blocked(base: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {**base, "status": status, "reason": reason}


def preflight_streamed_full_raw(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    *,
    sensor_tiles_row_col_exclusive: tuple[tuple[int, int, int, int], ...]
    | list[tuple[int, int, int, int]],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    resource_policy: StitchedRawResourcePolicy,
) -> dict[str, Any]:
    """Preflight every sensor tile and complete-array readout before optics."""
    if not isinstance(provider, AnalyticStrictGeometryTileProvider):
        raise ValueError("An analytic strict geometry provider is required.")
    if not isinstance(sensor, Sensor):
        raise ValueError("A CameraE2E Sensor is required.")
    if not isinstance(resource_policy, StitchedRawResourcePolicy):
        raise ValueError("An explicit stitched RAW resource policy is required.")
    if sensor.data or "sensor_capture" in sensor.metadata:
        raise ValueError("A fresh, uncomputed sensor is required to avoid stale capture state.")
    shape, _, _ = _sensor_contract(sensor)
    if _optical_state_fingerprint(provider.optics) != provider.optics_state_sha256:
        raise ValueError("The provider optics changed after construction.")
    if not isinstance(sensor_tiles_row_col_exclusive, (tuple, list)):
        raise ValueError("A sensor tile list is required.")
    if len(sensor_tiles_row_col_exclusive) > resource_policy.max_sensor_tiles:
        return _blocked(
            {"schema": SCHEMA, "resource_policy": resource_policy.as_dict()},
            "blocked_sensor_tile_count_policy",
            "Sensor tile count exceeds the declared policy.",
        )
    tiles = _complete_tiles(sensor_tiles_row_col_exclusive, shape)
    cells = shape[0] * shape[1]
    source_id = provider.source_identity
    sensor_id = _sensor_identity(sensor)
    # 256 bytes/pixel approximates the worst combination of full mean,
    # cloned sensor data, reference DN, RAW/DV, volts, fixed-pattern maps,
    # intermediate shot/readout arrays and temporary expressions. Additional
    # resident sensor arrays are accounted for separately. Not a hard bound.
    readout_bytes = READOUT_POLICY_BYTES_PER_SENSOR_CELL * cells
    sensor_payload = _array_payload_bytes(sensor)
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "preflight_incomplete",
        "source_identity": source_id,
        "full_optical_state_sha256": provider.optics_state_sha256,
        "sensor_model_state_sha256": sensor_id,
        "full_sensor_shape_row_col": list(shape),
        "full_sensor_cells": cells,
        "sensor_tiles_row_col_exclusive": [list(tile) for tile in tiles],
        "sensor_tile_coverage": "complete_exact_nonoverlapping",
        "tile_preflight_order": "all_sensor_tiles_before_any_optical_execution",
        "sensor_tile_count": len(tiles),
        "source_tile_shape_row_col": list(source_tile_shape_row_col),
        "sample_angles_deg": np.asarray(sample_angles_deg, dtype=float).tolist(),
        "resource_policy": resource_policy.as_dict(),
        "full_readout_policy_bytes_per_sensor_cell": READOUT_POLICY_BYTES_PER_SENSOR_CELL,
        "full_readout_policy_estimate_bytes": readout_bytes,
        "resident_sensor_ndarray_payload_bytes": sensor_payload,
        "estimated_working_bytes_is_peak_rss_bound": False,
        "hard_process_rss_limit_enforced": False,
        "resource_policy_is_hardware_limit": False,
        "full_scene_or_oi_allocated": False,
        "declared_scene_boundary_rule": (
            "every sensor tile must retain the stored-PSF numerical source halo "
            "inside the declared Scene"
        ),
        "physical_scene_halo_qualified": False,
        "physical_fov_qualified": False,
        "product_full_frame_qualified": False,
    }
    if cells > resource_policy.max_full_sensor_cells:
        return _blocked(
            base, "blocked_full_sensor_cells_policy", "Full sensor exceeds declared cells."
        )
    # Reserve for up to four copies of resident input model arrays across
    # clone/reconfigure operations. The dynamic readout estimate is separate.
    input_residency_bytes = 4 * sensor_payload
    base["resident_sensor_clone_policy_estimate_bytes"] = input_residency_bytes
    if readout_bytes + input_residency_bytes > resource_policy.estimated_working_budget_bytes:
        return _blocked(
            base,
            "blocked_full_readout_memory_policy",
            "Full sensor readout estimate exceeds the declared working budget.",
        )
    tile_plans: list[dict[str, Any]] = []
    worst_tile_bytes = 0
    for tile in tiles:
        plan = preflight_streamed_analytic_offaxis_mean(
            provider,
            sensor,
            sensor_tile_bounds_row_col_exclusive=tile,
            source_tile_shape_row_col=source_tile_shape_row_col,
            sample_angles_deg=sample_angles_deg,
            resource_policy=resource_policy.optical_tile,
        )
        tile_plans.append(plan)
        if plan["status"] != "ready_under_declared_policy":
            return _blocked(
                {**base, "sensor_tile_preflights": tile_plans},
                "blocked_sensor_tile_preflight",
                f"Sensor tile {list(tile)}: {plan['status']}: {plan['reason']}",
            )
        if (
            plan["source_identity"] != source_id
            or plan["full_optical_state_sha256"] != provider.optics_state_sha256
        ):
            raise ValueError("A sensor tile preflight changed optical source identity.")
        worst_tile_bytes = max(worst_tile_bytes, int(plan["combined_policy_estimate_bytes"]))
    estimate = readout_bytes + input_residency_bytes + worst_tile_bytes
    base.update(
        {
            "sensor_tile_preflights": tile_plans,
            "maximum_optical_sensor_tile_policy_estimate_bytes": worst_tile_bytes,
            "combined_policy_estimate_bytes": estimate,
        }
    )
    if estimate > resource_policy.estimated_working_budget_bytes:
        return _blocked(
            base,
            "blocked_combined_working_memory_policy",
            "Stitched readout plus largest optical/sensor tile exceeds declared budget.",
        )
    if provider.source_identity != source_id or _sensor_identity(sensor) != sensor_id:
        raise ValueError("Model source identity changed during stitched RAW preflight.")
    return {**base, "status": "ready_under_declared_policy"}


def compute_streamed_full_raw(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    *,
    sensor_tiles_row_col_exclusive: tuple[tuple[int, int, int, int], ...]
    | list[tuple[int, int, int, int]],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    resource_policy: StitchedRawResourcePolicy,
    seed: int | None,
) -> tuple[Sensor, dict[str, Any]]:
    """Execute all mean tiles, then one native full-array RAW sampling call."""
    angles = np.asarray(sample_angles_deg, dtype=float).copy()
    plan = preflight_streamed_full_raw(
        provider,
        sensor,
        sensor_tiles_row_col_exclusive=sensor_tiles_row_col_exclusive,
        source_tile_shape_row_col=source_tile_shape_row_col,
        sample_angles_deg=angles,
        resource_policy=resource_policy,
    )
    if plan["status"] != "ready_under_declared_policy":
        raise ValueError(f"resource_blocked: {plan['status']}: {plan['reason']}")
    shape = tuple(plan["full_sensor_shape_row_col"])
    source_tile_shape = tuple(plan["source_tile_shape_row_col"])
    mean = np.empty(shape, dtype=float)
    tile_evidence: list[dict[str, Any]] = []
    for tile in (tuple(value) for value in plan["sensor_tiles_row_col_exclusive"]):
        if (
            provider.source_identity != plan["source_identity"]
            or _optical_state_fingerprint(provider.optics) != plan["full_optical_state_sha256"]
            or _sensor_identity(sensor) != plan["sensor_model_state_sha256"]
            or sensor.data
        ):
            raise ValueError("Provider or sensor model mutated during stitched RAW capture.")
        values, evidence = compute_streamed_analytic_offaxis_mean(
            provider,
            sensor,
            sensor_tile_bounds_row_col_exclusive=tile,
            source_tile_shape_row_col=source_tile_shape,
            sample_angles_deg=angles,
            resource_policy=resource_policy.optical_tile,
        )
        r0, r1, c0, c1 = tile
        if (
            values.shape != (r1 - r0, c1 - c0)
            or not np.isfinite(values).all()
            or np.any(values < 0)
        ):
            raise ValueError("Sensor tile returned invalid mean electrons.")
        if (
            evidence["provider"]["source_identity"] != plan["source_identity"]
            or evidence["full_optical_state_sha256"] != plan["full_optical_state_sha256"]
            or evidence["sensor_execution"]["sensor_tile_bounds_row_col_exclusive"] != list(tile)
        ):
            raise ValueError("Sensor tile execution lost its model or global bounds.")
        mean[r0:r1, c0:c1] = values
        tile_evidence.append(evidence)
    if (
        provider.source_identity != plan["source_identity"]
        or _optical_state_fingerprint(provider.optics) != plan["full_optical_state_sha256"]
        or _sensor_identity(sensor) != plan["sensor_model_state_sha256"]
        or sensor.data
    ):
        raise ValueError("Provider or sensor model mutated before the native readout.")
    configured = sensor.clone()
    configured.data = {"mean_electrons": mean}
    selected_seed = sensor.fields.get("noise_seed", 0) if seed is None else seed
    captured = resample_sensor_capture(configured, seed=selected_seed)
    if captured.data["raw_dn"].shape != shape:
        raise ValueError("Native RAW readout returned the wrong global sensor shape.")
    report = {
        "schema": SCHEMA,
        "status": "diagnostic_complete_sensor_raw_executed",
        "preflight": plan,
        "source_identity": plan["source_identity"],
        "full_optical_state_sha256": plan["full_optical_state_sha256"],
        "sensor_model_state_sha256": plan["sensor_model_state_sha256"],
        "sensor_tile_execution": tile_evidence,
        "full_sensor_mean_assembled": True,
        "native_readout_calls": 1,
        "native_readout_seed": selected_seed,
        "native_readout_scope": "complete_sensor_array_once",
        "full_scene_or_oi_array_intentionally_allocated_by_stitcher": False,
        "provider_internal_scene_tile_residency": "not_measured",
        "full_sensor_arrays_materialized_by_native_readout": True,
        "physical_scene_halo_qualified": False,
        "physical_fov_qualified": False,
        "product_full_frame_qualified": False,
        "estimated_working_bytes_is_peak_rss_bound": False,
        "hard_process_rss_limit_enforced": False,
    }
    return captured, report
