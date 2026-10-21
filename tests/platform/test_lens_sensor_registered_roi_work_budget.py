"""Cheap and model-dependent total-work policy boundaries for registered ROIs."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from camera_platform.lens_sensor_registered_roi_work_budget import (
    RegisteredRoiActualWorkCounter,
    RegisteredRoiWorkPolicy,
    cheap_registered_roi_work_admission,
)


def _cheap(**changes):
    request = {
        "scene_shape_row_col": (4096, 28000),
        "active_shape_row_col": (2048, 14000),
        "roi_bounds_row_col_exclusive": (1000, 1008, 12000, 12008),
        "sensor_tile_shape_row_col": (4, 8),
        "source_tile_shape_row_col": (64, 64),
        "scene_fov_deg": 42.0,
        "pixel_pitch_um": 1.22,
        "policy": RegisteredRoiWorkPolicy(),
    }
    request.update(changes)
    return cheap_registered_roi_work_admission(**request)


def _plan(source_tiles: int, kernel_shape=(289, 289)) -> dict:
    return {
        "schema": "camera_lens_sensor_streamed_offaxis_mean_v1",
        "status": "ready_under_declared_policy",
        "candidate_source_tile_count": source_tiles,
        "sampled_psf_kernel_shape_row_col": list(kernel_shape),
    }


def test_cheap_stage_records_exact_tiles_and_unbounded_proxy_without_loading_optics():
    checked = _cheap(scene_shape_row_col=(8192, 56000))
    assert checked["status"] == "ready_for_model_dependent_preflight"
    assert checked["sensor_tile_count"] == 2
    assert checked["nominal_core_source_tile_proxy"] == 2
    assert checked["scene_cells"] == 8192 * 56000
    assert checked["model_dependent_psf_halo_and_distortion_checked"] is False
    assert checked["nominal_core_source_tile_proxy_is_work_bound"] is False
    assert checked["policy_is_hardware_limit"] is False
    assert checked["worker_deadline_guaranteed"] is False
    json.dumps(checked, allow_nan=False)


def test_cheap_stage_blocks_large_declared_grids_and_many_sensor_tiles():
    assert _cheap(scene_shape_row_col=(8193, 65536))["status"] == (
        "blocked_scene_cells_coordinate_policy"
    )
    assert _cheap(
        roi_bounds_row_col_exclusive=(0, 128, 0, 128),
        sensor_tile_shape_row_col=(2, 2),
    )["status"] == "blocked_sensor_tile_count_policy"
    assert _cheap(
        roi_bounds_row_col_exclusive=(0, 8, 0, 8),
        sensor_tile_shape_row_col=(8, 8),
    )["status"] == "blocked_sensor_tile_count_policy"
    informational = _cheap(
        policy=replace(
            RegisteredRoiWorkPolicy(), nominal_core_source_tile_proxy_review_threshold=1
        ),
    )
    assert informational["status"] == "ready_for_model_dependent_preflight"
    assert informational["nominal_core_source_tile_proxy_exceeds_review_threshold"] is True


def test_cheap_stage_density_proxy_has_no_lens_or_psf_bound_claim():
    checked = _cheap(
        scene_shape_row_col=(8192, 56000),
        roi_bounds_row_col_exclusive=(1000, 1033, 12000, 12031),
        sensor_tile_shape_row_col=(16, 16),
        source_tile_shape_row_col=(16, 16),
    )
    assert checked["status"] == "ready_for_model_dependent_preflight"
    assert checked["sensor_tile_count"] == 6
    assert checked["nominal_core_source_tile_proxy"] >= 6
    assert checked["fov_and_pitch_without_lens_do_not_establish_oi_sampling"] is True


def test_model_dependent_stage_blocks_aggregate_before_optical_work():
    counter = RegisteredRoiActualWorkCounter(RegisteredRoiWorkPolicy())
    first = counter.add_plan(_plan(50))
    assert first["status"] == "ready_under_declared_total_work_policy"
    assert first["model_dependent_candidate_source_tile_count"] == 50
    assert first["sampled_psf_kernel_cell_tile_product"] == 50 * 289 * 289
    blocked = counter.add_plan(_plan(4096))
    assert blocked["status"] == "blocked_total_candidate_source_tile_policy"
    assert blocked["proposed_candidate_source_tiles"] == 4146
    assert counter.snapshot()["model_dependent_candidate_source_tile_count"] == 50
    assert counter.snapshot()["sensor_tiles_with_model_dependent_plans"] == 1
    json.dumps(blocked, allow_nan=False)


def test_total_work_limit_is_policy_not_hardware_or_deadline_guarantee():
    counter = RegisteredRoiActualWorkCounter(
        replace(RegisteredRoiWorkPolicy(), max_model_dependent_candidate_source_tiles=2)
    )
    assert counter.add_plan(_plan(2))["status"] == "ready_under_declared_total_work_policy"
    blocked = counter.add_plan(_plan(1))
    assert blocked["policy_is_hardware_limit"] is False
    assert blocked["worker_deadline_guaranteed"] is False
    assert blocked["optical_calculation_executed"] is False


@pytest.mark.parametrize("shape", [[0, 4], [4, True], [2], "2048,14000"])
def test_cheap_stage_rejects_bad_shapes(shape):
    with pytest.raises(ValueError, match="two positive integer axes"):
        _cheap(active_shape_row_col=shape)


def test_model_dependent_stage_rejects_unadmitted_plan():
    counter = RegisteredRoiActualWorkCounter(RegisteredRoiWorkPolicy())
    with pytest.raises(ValueError, match="admitted"):
        counter.add_plan({**_plan(1), "status": "blocked_working_memory_policy"})
