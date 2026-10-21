"""Executable target-band refinement without product-accuracy promotion."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from camera_platform import lens_sensor_registered_roi_frequency_execution as execution
from camera_platform.lens_sensor_streamed_analytic_capture import AnalyticCaptureResourcePolicy
from pyisetcam.sensor import sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor

WAVE = np.array([500.0, 600.0])
ANGLES = np.arange(0.0, 361.0, 90.0)


def _optics() -> dict:
    field = np.array([0.0, 0.5])

    def table(values: np.ndarray) -> dict:
        return {
            "field_height_mm": field.copy(),
            "wavelength_nm": WAVE.copy(),
            "function": values,
            "field_coordinate": "ideal_image_height",
        }

    psf = np.zeros((3, 3, 2, 2), dtype=float)
    psf[1, 1, :, :] = 0.8
    psf[1, 2, :, :] = 0.2
    psf_table = table(psf)
    psf_table.update({
        "sample_spacing_mm": np.array([0.001, 0.001]),
        "coordinate_convention": "pixel_centers",
        "row_axis": "y_up",
        "psf_reference_axis": "+x",
    })
    return {
        "model": "raytrace",
        "focal_length_m": 0.01,
        "f_number": 2.0,
        "offaxis_method": "skip",
        "transmittance": {"wave": WAVE.copy(), "scale": np.array([0.7, 0.9])},
        "raytrace": {
            "contract_version": 2,
            "physics_policy": "strict",
            "coordinate_convention": "pixel_centers",
            "object_distance_m": math.inf,
            "effective_focal_length_m": 0.01,
            "effective_f_number": 2.0,
            "max_fov_deg": 10.0,
            "geometry": table(np.array([[0.0, 0.0], [0.45, 0.45]])),
            "relative_illumination": table(np.ones((2, 2))),
            "psf": psf_table,
        },
    }


def _sensor(*, mono: bool) -> object:
    sensor = sensor_create("default", {"size": (96, 96), "wave": WAVE.copy()})
    sensor = sensor_set(sensor, "pixel size same fill factor", 1e-6)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = configure_e2e_sensor(sensor)
    if mono:
        sensor.fields["pattern"] = np.array([[1]], dtype=int)
    return sensor


def _arguments(*, max_refinements: int = 2) -> dict:
    fov = math.degrees(2 * math.atan(256e-6 / (2 * 0.01)))
    return {
        "first_scene_shape_row_col": (256, 256),
        "max_refinements": max_refinements,
        "scene_horizontal_fov_deg": fov,
        "wavelengths_nm": WAVE,
        "bright_side_photons": np.array([1e13, 1.2e13]),
        "analytic_slanted_edge_offset_deg": 0.0,
        "fixed_shift_deg": 0.004,
        "sensor_tile_bounds_row_col_exclusive": (16, 80, 16, 80),
        "source_tile_shape_row_col": (16, 16),
        "sample_angles_deg": ANGLES,
        "resource_policy": AnalyticCaptureResourcePolicy(
            estimated_working_budget_bytes=512 * 1024**2,
            max_geometry_query_cells=8_000_000,
            max_source_tiles=4096,
            max_output_roi_cells=1_048_576,
            max_source_tile_cells=1_048_576,
        ),
        "target_frequency_cycles_per_mm": [100.0, 200.0, 350.0],
    }


def _mock_solver(monkeypatch) -> list[str]:
    called: list[str] = []

    def fake_preflight(provider, _sensor, **_kwargs):
        called.append(f"preflight-{provider.edge_offset_deg}")
        return {"status": "ready_under_declared_policy"}

    def fake_compute(provider, _sensor, *, admitted_plan, **_kwargs):
        assert admitted_plan["status"] == "ready_under_declared_policy"
        called.append(f"compute-{provider.edge_offset_deg}")
        row, col = np.indices((64, 64), dtype=float)
        position = col - row / 12 - 29.3 - (0.5 if provider.edge_offset_deg else 0)
        mean = np.where(position > 0, 100.0, 2.0)
        return mean, {"optical_execution": {"source_tiles_processed": 1}}

    monkeypatch.setattr(execution, "preflight_streamed_analytic_offaxis_mean", fake_preflight)
    monkeypatch.setattr(
        execution, "_compute_preflighted_streamed_analytic_offaxis_mean", fake_compute
    )
    return called


def test_extended_coordinate_limits_require_explicit_policy(monkeypatch):
    arguments = _arguments(max_refinements=0)
    arguments["max_scene_axis"] = 131_072
    arguments["max_virtual_scene_cells"] = 2_147_483_648
    with pytest.raises(ValueError, match="coordinate policy"):
        execution.run_registered_roi_frequency_sampling(
            _optics(), _sensor(mono=False), **arguments
        )
    arguments["coordinate_policy"] = "extended_sparse_roi_128k_v1"
    _mock_solver(monkeypatch)
    report = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=False), **arguments
    )
    assert report["coordinate_policy"] == "extended_sparse_roi_128k_v1"
    assert report["max_scene_axis_policy"] == 131_072
    assert report["max_virtual_scene_cells_policy"] == 2_147_483_648
    assert report["policy_is_hardware_limit"] is False
    assert report["physical_mtf_comparison_allowed"] is False
    arguments["max_scene_axis"] = 131_071
    with pytest.raises(ValueError, match="coordinate policy"):
        execution.run_registered_roi_frequency_sampling(
            _optics(), _sensor(mono=False), **arguments
        )


def test_mono_discrete_targets_need_two_refinements_and_stay_model_only(monkeypatch):
    called = _mock_solver(monkeypatch)
    report = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=True), **_arguments()
    )
    assert report["status"] == (
        "numeric_joint_scene_oi_grid_discrete_frequency_targets_converged_model_only"
    )
    assert report["mean_phase_status"].startswith("numeric_")
    assert report["frequency_target_points_status"] == (
        "numeric_mono_discrete_frequency_targets_converged_model_only"
    )
    assert report["frequency_coverage_scope"] == "declared_discrete_frequency_points_only"
    assert report["continuous_frequency_band_between_targets_assessed"] is False
    assert report["exact_native_nyquist_endpoint_assessed"] is False
    assert report["psf_source_status"].startswith("inconclusive_")
    assert report["selected_scene_shape_row_col"] == [1024, 1024]
    assert [
        step["consecutive_joint_target_point_passes"] for step in report["steps"]
    ] == [0, 1, 2]
    assert report["tolerances"]["mean_electron_full_scale_normalized_max_delta_tolerance"] == 0.005
    assert report["tolerances"]["shift_response_full_scale_normalized_max_delta_tolerance"] == 0.01
    assert report["steps"][1]["full_scale_normalized_max_delta_from_previous"] == {
        "mean_electron_full_scale_normalized_max_delta": 0.0,
        "shift_response_full_scale_normalized_max_delta": 0.0,
        "full_scale_electrons_denominator": 100.0,
    }
    assert len(report["steps"][2]["frequency_target_points_comparison_by_phase"]) == 2
    assert [step["oi_pitch_um"] for step in report["steps"]] == [1.0, 0.5, 0.25]
    assert report["physical_mtf_comparison_allowed"] is False
    assert report["product_ranking_allowed"] is False
    assert called[:4] == [
        "preflight-0.0", "preflight-0.004", "compute-0.0", "compute-0.004"
    ]


def test_bayer_mean_phase_can_converge_without_frequency_claim(monkeypatch):
    _mock_solver(monkeypatch)
    report = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=False), **_arguments()
    )
    assert report["status"] == "inconclusive_frequency_mosaic_channel_contract"
    assert report["mean_phase_status"].startswith("numeric_")
    assert report["frequency_target_points_status"] == "inconclusive_mosaic_channel_contract"
    assert all(
        step["frequency_curve_status_by_phase"] == [
            "inconclusive_mosaic_channel_contract"
        ] * 2
        for step in report["steps"]
    )


def test_explicit_single_grid_executes_without_convergence_claim(monkeypatch):
    called = _mock_solver(monkeypatch)
    report = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=True), **_arguments(max_refinements=0)
    )
    assert report["status"] == "inconclusive_refinement_limit"
    assert report["mean_phase_status"] == "inconclusive_single_grid_no_refinement"
    assert report["frequency_target_points_status"] == "inconclusive_single_grid_no_refinement"
    assert len(report["steps"]) == 1
    assert len(called) == 4


def test_opt_in_transient_means_are_bounded_read_only_and_not_in_json(monkeypatch):
    _mock_solver(monkeypatch)
    report, means = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=True),
        **_arguments(max_refinements=0), return_transient_means=True,
    )
    assert report["last_executed_scene_shape_row_col"] == [256, 256]
    assert means is not None
    assert [mean.shape for mean in means] == [(64, 64), (64, 64)]
    assert all(mean.flags.writeable is False for mean in means)
    assert not any(isinstance(value, np.ndarray) for value in report.values())
    assert report["steps"][0]["mean_electrons_sha256_by_phase"][0].startswith("sha256:")
    json.dumps(report, allow_nan=False)


def test_policy_blocks_both_phases_before_any_execution(monkeypatch):
    called: list[str] = []

    def fake_preflight(provider, _sensor, **_kwargs):
        called.append(f"preflight-{provider.edge_offset_deg}")
        return {
            "status": (
                "ready_under_declared_policy"
                if provider.edge_offset_deg == 0 else "blocked_scene_halo"
            ),
            "reason": "Scene halo touches boundary",
        }

    monkeypatch.setattr(execution, "preflight_streamed_analytic_offaxis_mean", fake_preflight)
    monkeypatch.setattr(
        execution,
        "_compute_preflighted_streamed_analytic_offaxis_mean",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Must not execute")),
    )
    report = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=False), **_arguments()
    )
    assert report["status"] == "inconclusive_resource_or_halo_support"
    assert called == ["preflight-0.0", "preflight-0.004"]


def test_transient_means_are_none_when_preflight_blocks(monkeypatch):
    monkeypatch.setattr(
        execution, "preflight_streamed_analytic_offaxis_mean",
        lambda *_args, **_kwargs: {"status": "blocked_scene_halo", "reason": "Halo"},
    )
    report, means = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=True), **_arguments(max_refinements=0),
        return_transient_means=True,
    )
    assert report["status"] == "inconclusive_resource_or_halo_support"
    assert means is None


def test_registered_native_pixel_floor_precedes_tile_preflight(monkeypatch):
    monkeypatch.setattr(
        execution,
        "preflight_streamed_analytic_offaxis_mean",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Must not preflight")),
    )
    arguments = _arguments(max_refinements=0)
    arguments["first_scene_shape_row_col"] = (128, 128)
    report = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=True), **arguments
    )
    assert report["status"] == "inconclusive_scene_below_native_pixel_floor"
    assert report["steps"][0]["preflight_status_by_phase"] == [
        "blocked_scene_below_native_pixel_floor"
    ] * 2


def test_mono_frequency_support_rejects_flat_and_small_tiles():
    flat = execution._edge_curve(
        np.ones((64, 64)), pixel_pitch_um_col=1.0,
        minimum_normalized_signal_contrast=0.05,
    )
    small = execution._edge_curve(
        np.ones((8, 8)), pixel_pitch_um_col=1.0,
        minimum_normalized_signal_contrast=0.05,
    )
    assert flat["status"] == "inconclusive_edge_frequency_support"
    assert small["status"] == "inconclusive_edge_frequency_support"


def test_stable_means_cannot_mask_nonconvergent_frequency_curve(monkeypatch):
    _mock_solver(monkeypatch)
    count = 0

    def changing_curve(_mean, **_kwargs):
        nonlocal count
        rung = count // 2
        count += 1
        # Each rung has the same output mean, but a materially different
        # target-band curve. This prevents RAW/mean parity from passing MTF.
        level = [0.9, 0.5, 0.2][rung]
        return {
            "status": "numeric_mono_edge_curve_available",
            "frequency_cycles_per_mm": [0.0, 100.0, 200.0, 350.0, 500.0],
            "mtf": [1.0, level, level, level, 0.1],
            "normalized_signal_contrast": 0.9,
        }

    monkeypatch.setattr(execution, "_edge_curve", changing_curve)
    report = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=True), **_arguments()
    )
    assert report["mean_phase_status"].startswith("numeric_")
    assert report["frequency_target_points_status"] == (
        "inconclusive_discrete_frequency_target_refinement_limit"
    )
    assert report["status"] == "inconclusive_refinement_limit"
    assert all(
        step["frequency_target_points_tolerance_met"] is False
        for step in report["steps"][1:]
    )


def test_later_mean_failure_revokes_prior_two_pass_status(monkeypatch):
    def fake_preflight(_provider, _sensor, **_kwargs):
        return {"status": "ready_under_declared_policy"}

    def fake_compute(provider, _sensor, *, admitted_plan, **_kwargs):
        assert admitted_plan["status"] == "ready_under_declared_policy"
        row, col = np.indices((64, 64), dtype=float)
        position = col - row / 12 - 29.3 - (0.5 if provider.edge_offset_deg else 0)
        level = 2.0 if provider.global_shape[1] >= 2048 else 1.0
        mean = level * np.where(position > 0, 100.0, 2.0)
        return mean, {"optical_execution": {"source_tiles_processed": 1}}

    monkeypatch.setattr(execution, "preflight_streamed_analytic_offaxis_mean", fake_preflight)
    monkeypatch.setattr(
        execution, "_compute_preflighted_streamed_analytic_offaxis_mean", fake_compute
    )
    monkeypatch.setattr(
        execution, "compare_frequency_band_mtf_curves",
        lambda *_args, **_kwargs: {
            "status": "outside_declared_target_tolerance_model_only_single_step",
            "within_declared_single_step_tolerance": False,
        },
    )
    arguments = _arguments(max_refinements=3)
    report = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=True), **arguments
    )
    assert [step["consecutive_mean_phase_passes"] for step in report["steps"]] == [
        0, 1, 2, 0,
    ]
    assert report["steps"][2]["mean_phase_tolerance_met"] is True
    assert report["steps"][3]["mean_phase_tolerance_met"] is False
    assert report["status"] == "inconclusive_refinement_limit"
    assert report["mean_phase_status"] == "inconclusive_mean_phase_refinement"
    assert "mean_phase_converged_scene_shape_row_col" not in report


def test_one_real_strict_optics_and_sensor_tile_is_executed():
    arguments = _arguments(max_refinements=0)
    arguments["sensor_tile_bounds_row_col_exclusive"] = (44, 52, 44, 52)
    report = execution.run_registered_roi_frequency_sampling(
        _optics(), _sensor(mono=False), **arguments
    )
    assert report["status"] == "inconclusive_refinement_limit"
    assert report["steps"][0]["preflight_status_by_phase"] == [
        "ready_under_declared_policy"
    ] * 2
    assert report["steps"][0]["source_tiles_processed_by_phase"][0] > 0
    assert report["steps"][0]["output_mean_electrons_shape_row_col"] == [8, 8]
    assert report["frequency_target_points_status"] == "inconclusive_mosaic_channel_contract"
    assert report["readout_or_isp_executed"] is False
