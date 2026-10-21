"""Two-stage work admission for a registered off-axis ROI diagnostic.

The first stage only uses scalar request/declaration fields.  Its nominal
source-tile count is a coordinate-density proxy: lens image distance,
distortion, and PSF halo are unavailable here, so it is neither a lower nor
an upper bound on optical work.  The second stage accumulates candidate
source tiles from model-dependent, admitted optical tile plans.  Both stages
are software admission policies, not runtime or hardware guarantees.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA = "camera_lens_sensor_registered_roi_work_budget_v1"
_OPTICAL_PLAN_SCHEMA = "camera_lens_sensor_streamed_offaxis_mean_v1"


@dataclass(frozen=True)
class RegisteredRoiWorkPolicy:
    """Provisional admission limits; the aggregate cap is not time-calibrated."""

    max_active_axis: int = 65_536
    max_active_cells: int = 268_435_456
    max_scene_axis: int = 65_536
    max_scene_cells: int = 536_870_912
    min_sensor_tiles: int = 2
    max_sensor_tiles: int = 256
    nominal_core_source_tile_proxy_review_threshold: int = 4096
    # Mirrors the existing single-optical-tile cap, but now bounds the sum.
    # It must be calibrated against measured CPU time and peak RSS by PSF size.
    max_model_dependent_candidate_source_tiles: int = 4096

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer policy limit.")
        if self.min_sensor_tiles > self.max_sensor_tiles:
            raise ValueError("Minimum sensor tiles cannot exceed maximum sensor tiles.")

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _shape(value: Any, name: str) -> tuple[int, int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(item) is not int or item < 1 for item in value)
    ):
        raise ValueError(f"{name} requires two positive integer axes.")
    return value[0], value[1]


def _positive_finite(value: Any, name: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a positive finite number.")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number.") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number.")
    return result


def _nominal_axis_work(
    roi_extent: int, sensor_tile_extent: int, scene_extent: int,
    active_extent: int, source_tile_extent: int,
) -> int:
    """Count a row/column density proxy analytically, without a tile loop."""
    full, tail = divmod(roi_extent, sensor_tile_extent)
    denominator = active_extent * source_tile_extent

    def nominal_tile_count(sensor_extent: int) -> int:
        return max(1, (sensor_extent * scene_extent + denominator - 1) // denominator)

    return full * nominal_tile_count(sensor_tile_extent) + (
        nominal_tile_count(tail) if tail else 0
    )


def cheap_registered_roi_work_admission(
    *,
    scene_shape_row_col: tuple[int, int] | list[int],
    active_shape_row_col: tuple[int, int] | list[int],
    roi_bounds_row_col_exclusive: tuple[int, int, int, int] | list[int],
    sensor_tile_shape_row_col: tuple[int, int] | list[int],
    source_tile_shape_row_col: tuple[int, int] | list[int],
    scene_fov_deg: float,
    pixel_pitch_um: float,
    policy: RegisteredRoiWorkPolicy,
) -> dict[str, Any]:
    """Reject excessive declared-grid work before any model or PSF is loaded.

    A ready status means only that the request may enter model-dependent
    preflight.  The density proxy intentionally does not use FOV or pitch to
    infer optical magnification because lens image distance is unknown here.
    """
    if not isinstance(policy, RegisteredRoiWorkPolicy):
        raise ValueError("A registered ROI work admission policy is required.")
    scene = _shape(scene_shape_row_col, "scene_shape_row_col")
    active = _shape(active_shape_row_col, "active_shape_row_col")
    sensor_tile = _shape(sensor_tile_shape_row_col, "sensor_tile_shape_row_col")
    source_tile = _shape(source_tile_shape_row_col, "source_tile_shape_row_col")
    fov = _positive_finite(scene_fov_deg, "scene_fov_deg")
    pitch = _positive_finite(pixel_pitch_um, "pixel_pitch_um")
    bounds = roi_bounds_row_col_exclusive
    if (
        not isinstance(bounds, (list, tuple))
        or len(bounds) != 4
        or any(type(item) is not int for item in bounds)
        or not (0 <= bounds[0] < bounds[1] <= active[0])
        or not (0 <= bounds[2] < bounds[3] <= active[1])
    ):
        raise ValueError("ROI bounds must be a half-open rectangle within the active grid.")
    roi = (bounds[1] - bounds[0], bounds[3] - bounds[2])
    sensor_tiles = math.ceil(roi[0] / sensor_tile[0]) * math.ceil(roi[1] / sensor_tile[1])
    nominal_core_work = _nominal_axis_work(
        roi[0], sensor_tile[0], scene[0], active[0], source_tile[0]
    ) * _nominal_axis_work(
        roi[1], sensor_tile[1], scene[1], active[1], source_tile[1]
    )
    base = {
        "schema": SCHEMA,
        "status": "ready_for_model_dependent_preflight",
        "reason": None,
        "scene_shape_row_col": list(scene),
        "scene_cells": scene[0] * scene[1],
        "active_shape_row_col": list(active),
        "active_cells": active[0] * active[1],
        "roi_bounds_row_col_exclusive": list(bounds),
        "roi_shape_row_col": list(roi),
        "sensor_tile_shape_row_col": list(sensor_tile),
        "sensor_tile_count": sensor_tiles,
        "source_tile_shape_row_col": list(source_tile),
        "nominal_core_source_tile_proxy": nominal_core_work,
        "nominal_core_source_tile_proxy_is_work_bound": False,
        "nominal_core_source_tile_proxy_exceeds_review_threshold": (
            nominal_core_work > policy.nominal_core_source_tile_proxy_review_threshold
        ),
        "model_dependent_psf_halo_and_distortion_checked": False,
        "scene_fov_deg": fov,
        "pixel_pitch_um": pitch,
        "fov_and_pitch_without_lens_do_not_establish_oi_sampling": True,
        "policy": policy.as_dict(),
        "policy_is_hardware_limit": False,
        "policy_is_peak_rss_bound": False,
        "worker_deadline_guaranteed": False,
        "full_active_area_execution_qualified": False,
    }
    if active[0] > policy.max_active_axis or active[1] > policy.max_active_axis:
        return {**base, "status": "blocked_active_axis_coordinate_policy",
                "reason": "Declared active axis exceeds the coordinate policy."}
    if active[0] * active[1] > policy.max_active_cells:
        return {**base, "status": "blocked_active_cells_coordinate_policy",
                "reason": "Declared active cells exceed the coordinate policy."}
    if scene[0] > policy.max_scene_axis or scene[1] > policy.max_scene_axis:
        return {**base, "status": "blocked_scene_axis_coordinate_policy",
                "reason": "Declared virtual Scene axis exceeds the coordinate policy."}
    if scene[0] * scene[1] > policy.max_scene_cells:
        return {**base, "status": "blocked_scene_cells_coordinate_policy",
                "reason": "Declared virtual Scene cells exceed the coordinate policy."}
    if sensor_tiles < policy.min_sensor_tiles:
        return {**base, "status": "blocked_sensor_tile_count_policy",
                "reason": "ROI needs at least two smaller sensor tiles."}
    if sensor_tiles > policy.max_sensor_tiles:
        return {**base, "status": "blocked_sensor_tile_count_policy",
                "reason": "ROI has more sensor tiles than the total-work policy admits."}
    return base


class RegisteredRoiActualWorkCounter:
    """Incrementally bound model-dependent *planned*, not executed, source work."""

    def __init__(self, policy: RegisteredRoiWorkPolicy):
        if not isinstance(policy, RegisteredRoiWorkPolicy):
            raise ValueError("A registered ROI work admission policy is required.")
        self.policy = policy
        self.sensor_tiles_seen = 0
        self.candidate_source_tiles = 0
        self.sampled_psf_kernel_cell_tile_product = 0

    def add_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        if (
            not isinstance(plan, dict)
            or plan.get("schema") != _OPTICAL_PLAN_SCHEMA
            or plan.get("status") != "ready_under_declared_policy"
        ):
            raise ValueError("Expected one admitted, model-dependent optical tile plan.")
        source_tiles = plan.get("candidate_source_tile_count")
        kernel_shape = plan.get("sampled_psf_kernel_shape_row_col")
        if type(source_tiles) is not int or source_tiles < 1:
            raise ValueError("Optical plan lacks a positive candidate source-tile count.")
        kernel_rows, kernel_cols = _shape(kernel_shape, "sampled_psf_kernel_shape_row_col")
        proposed_tiles = self.candidate_source_tiles + source_tiles
        proposed_kernel_work = (
            self.sampled_psf_kernel_cell_tile_product
            + source_tiles * kernel_rows * kernel_cols
        )
        if proposed_tiles > self.policy.max_model_dependent_candidate_source_tiles:
            return {
                **self.snapshot(),
                "status": "blocked_total_candidate_source_tile_policy",
                "reason": "Model-dependent source-tile work exceeds the total-work policy.",
                "proposed_candidate_source_tiles": proposed_tiles,
                "proposed_sampled_psf_kernel_cell_tile_product": proposed_kernel_work,
            }
        self.sensor_tiles_seen += 1
        self.candidate_source_tiles = proposed_tiles
        self.sampled_psf_kernel_cell_tile_product = proposed_kernel_work
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "status": (
                "ready_under_declared_total_work_policy"
                if self.sensor_tiles_seen else "preflight_incomplete"
            ),
            "reason": None,
            "sensor_tiles_with_model_dependent_plans": self.sensor_tiles_seen,
            "model_dependent_candidate_source_tile_count": self.candidate_source_tiles,
            "sampled_psf_kernel_cell_tile_product": (
                self.sampled_psf_kernel_cell_tile_product
            ),
            "sampled_psf_kernel_cell_tile_product_is_runtime_prediction": False,
            "optical_calculation_executed": False,
            "policy": self.policy.as_dict(),
            "policy_is_hardware_limit": False,
            "worker_deadline_guaranteed": False,
        }
