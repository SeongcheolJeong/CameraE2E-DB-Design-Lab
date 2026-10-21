"""Frequency-targeted grid plans are seeds, never accuracy qualifications."""

import math

import pytest

from camera_platform.lens_sensor_frequency_sampling import plan_frequency_sampling


def _plan(**changes):
    width_um = 400.0
    image_distance_m = 0.012
    fov = math.degrees(2 * math.atan(width_um * 1e-6 / (2 * image_distance_m)))
    arguments = {
        "native_pixel_pitch_um_row_col": (1.0, 1.0),
        "stored_psf_pitch_um_row_col": (0.4, 0.4),
        "image_distance_m": image_distance_m,
        "scene_horizontal_fov_deg": fov,
        "reference_scene_shape_row_col": (4, 6),
        "max_scene_axis": 10000,
        "max_virtual_scene_cells": 100_000_000,
    }
    arguments.update(changes)
    return plan_frequency_sampling(**arguments)


def test_native_nyquist_target_and_alias_probe_seed_fixed_fov_refinement():
    result = _plan()
    assert result["native_nyquist_cycles_per_mm_row_col"] == [500, 500]
    assert result["target_frequency_cycles_per_mm_row_col"] == [500, 500]
    assert result["alias_probe_frequency_cycles_per_mm_row_col"] is None
    assert result["scene_pitch_search_seed_um"] == pytest.approx(0.5)
    assert result["psf_generation_pitch_search_seed_um"] == pytest.approx(0.5)
    assert result["psf_regeneration_required_for_finer_source"] is False
    assert result["status"] == "planning_only"
    assert result["selected_grid_meets_frequency_search_seed"] is False
    assert result["frequency_seed_first_met_refinement"] == 1
    assert result["selected_starting_grid_meets_registered_native_pixel_floor"] is True
    assert result["planned_finest_grid_meets_frequency_search_seed"] is True
    shapes = [step["scene_shape_row_col"] for step in result["fixed_fov_refinement_grids"]]
    assert len(shapes) == 3
    assert all(row * 3 == col * 2 for row, col in shapes)
    assert shapes[1] == [2 * axis for axis in shapes[0]]
    assert shapes[2] == [2 * axis for axis in shapes[1]]
    assert result["scene_tangent_extent_um_row_col"] == pytest.approx([400 * 2 / 3, 400])
    assert result["strict_nyquist_endpoint_requires_separate_otf_reference"] is True
    assert result["sampled_system_sfr_at_exact_native_nyquist_qualified"] is False
    assert result["model_band_accuracy_qualified"] is False
    assert result["physical_accuracy_qualified"] is False


def test_manual_coarse_grid_and_stored_psf_limit_are_reported_independently():
    result = _plan(
        manual_scene_shape_row_col=(32, 48),
        stored_psf_pitch_um_row_col=(2.0, 2.0),
    )
    assert result["mode"] == "explicit_grid"
    assert result["selected_grid_meets_frequency_search_seed"] is False
    assert result["stored_psf_grid_covers_target_band_necessary_only"] == [False, False]
    assert result["stored_psf_grid_covers_alias_probe_necessary_only"] is None
    assert result["psf_regeneration_required_for_finer_source"] is True
    assert result["psf_generation_seed_is_executed_or_accuracy_proof"] is False
    assert result["psf_source_resolution_gained_by_interpolation"] is False
    assert result["status"] == "inconclusive_registered_scene_below_native_pixel"


def test_virtual_coordinate_policy_can_stop_ladder_without_hardware_claim():
    limited = _plan(max_scene_axis=500)
    assert limited["status"] == "inconclusive_refinement_policy"
    assert len(limited["fixed_fov_refinement_grids"]) == 1
    assert limited["frequency_seed_first_met_refinement"] is None
    assert limited["policy_is_hardware_limit"] is False
    blocked = _plan(max_scene_axis=100)
    assert blocked["status"] == "inconclusive_starting_grid_policy"
    assert blocked["fixed_fov_refinement_grids"] == []


def test_extended_sparse_roi_coordinate_plan_can_enumerate_fourth_grid():
    common = dict(
        reference_scene_shape_row_col=(2048, 16000),
        manual_scene_shape_row_col=(2048, 16000),
        max_refinements=3,
    )
    old = _plan(**common, max_scene_axis=65_536, max_virtual_scene_cells=536_870_912)
    assert old["status"] == "inconclusive_refinement_policy"
    assert old["fixed_fov_refinement_grids"][-1]["scene_shape_row_col"] == [8192, 64000]
    extended = _plan(
        **common, max_scene_axis=131_072,
        max_virtual_scene_cells=2_147_483_648,
    )
    assert extended["fixed_fov_refinement_grids"][-1]["scene_shape_row_col"] == [
        16384, 128000
    ]
    assert extended["policy_is_hardware_limit"] is False


def test_above_nyquist_alias_probe_is_explicit_and_costs_more_scene_samples():
    baseline = _plan()
    alias = _plan(alias_probe_multiple_native_nyquist=2.0)
    assert alias["alias_probe_frequency_cycles_per_mm_row_col"] == [1000, 1000]
    assert alias["scene_pitch_search_seed_um"] == pytest.approx(0.25)
    assert alias["target_band_grid_shape_row_col"][1] > (
        baseline["target_band_grid_shape_row_col"][1]
    )
    assert alias["stored_psf_grid_covers_alias_probe_necessary_only"] == [True, True]
    assert alias["above_nyquist_scene_alias_response_qualified"] is False


def test_manual_aspect_change_and_invalid_accuracy_targets_are_rejected():
    with pytest.raises(ValueError, match="aspect changes"):
        _plan(manual_scene_shape_row_col=(32, 50))
    with pytest.raises(ValueError, match="cannot exceed"):
        _plan(target_fraction_native_nyquist=1.01)
    with pytest.raises(ValueError, match="2..32"):
        _plan(minimum_scene_samples_per_target_period=1)
    with pytest.raises(ValueError, match="3..32"):
        _plan(minimum_scene_samples_per_alias_period=2)
    with pytest.raises(ValueError, match="pitch"):
        _plan(stored_psf_pitch_um_row_col=(0, 0.4))
