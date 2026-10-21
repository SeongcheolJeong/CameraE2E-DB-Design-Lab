"""Numerical grid refinement through streamed strict optics and sensor means."""

from __future__ import annotations

import math

import numpy as np

from camera_platform import lens_sensor_streamed_offaxis_convergence as convergence
from camera_platform.lens_sensor_streamed_analytic_capture import AnalyticCaptureResourcePolicy
from pyisetcam.sensor import sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor

WAVE = np.array([500.0, 600.0])
ANGLES = np.arange(0.0, 361.0, 90.0)


def _optics() -> dict:
    field = np.array([0.0, 0.1])

    def table(values: np.ndarray) -> dict:
        return {
            "field_height_mm": field.copy(),
            "wavelength_nm": WAVE.copy(),
            "function": values,
            "field_coordinate": "ideal_image_height",
        }

    psf = np.zeros((3, 3, 2, 2), dtype=float)
    psf[1, 1, :, :] = 0.75
    psf[1, 2, :, :] = 0.25
    psf_table = table(psf)
    psf_table.update(
        {
            "sample_spacing_mm": np.array([0.001, 0.001]),
            "coordinate_convention": "pixel_centers",
            "row_axis": "y_up",
            "psf_reference_axis": "+x",
        }
    )
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
            "max_fov_deg": 1.0,
            "geometry": table(np.array([[0.0, 0.0], [0.08, 0.09]])),
            "relative_illumination": table(np.array([[1.0, 1.0], [0.65, 0.75]])),
            "psf": psf_table,
        },
    }


def _sensor():
    sensor = sensor_create("default", {"size": (8, 8), "wave": WAVE.copy()})
    sensor = sensor_set(sensor, "pixel size same fill factor", 1e-6)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    return configure_e2e_sensor(sensor)


def _arguments() -> dict:
    fov = math.degrees(2 * math.atan(32e-6 / (2 * 0.01)))
    return {
        "first_scene_shape_row_col": (32, 32),
        "max_refinements": 6,
        "scene_horizontal_fov_deg": fov,
        "wavelengths_nm": WAVE,
        "bright_side_photons": np.array([1e13, 1.2e13]),
        "analytic_slanted_edge_offset_deg": 0.0,
        "fixed_shift_deg": 0.004,
        "sensor_tile_bounds_row_col_exclusive": (1, 7, 1, 7),
        "source_tile_shape_row_col": (16, 16),
        "sample_angles_deg": ANGLES,
        "resource_policy": AnalyticCaptureResourcePolicy(
            estimated_working_budget_bytes=512 * 1024**2,
            max_geometry_query_cells=8_000_000,
            max_source_tiles=4096,
            max_output_roi_cells=1_048_576,
            max_source_tile_cells=1_048_576,
        ),
        "mean_electron_relative_tolerance": 0.05,
        "shift_response_relative_tolerance": 0.05,
    }


def test_end_to_end_refinement_keeps_physical_fov_and_requires_two_passes():
    result = convergence.run_streamed_offaxis_mean_sampling_convergence(
        _optics(), _sensor(), **_arguments()
    )
    assert result["status"] == "numeric_offaxis_mean_sampling_converged_model_only"
    assert result["physical_mtf_comparison_allowed"] is False
    assert result["physical_scene_halo_qualified"] is False
    steps = result["steps"]
    assert [step["scene_shape_row_col"] for step in steps] == [
        [32, 32], [64, 64], [128, 128], [256, 256], [512, 512], [1024, 1024]
    ]
    assert [step["consecutive_passes"] for step in steps] == [0, 0, 0, 0, 1, 2]
    assert all(
        step["preflight_status_by_phase"] == ["ready_under_declared_policy"] * 2
        for step in steps
    )
    assert all(step["edge_and_shift_signal_valid"] for step in steps)
    assert all(step["output_mean_electrons_shape_row_col"] == [6, 6] for step in steps)
    assert all(
        later["oi_pitch_um"] == earlier["oi_pitch_um"] / 2
        for earlier, later in zip(steps, steps[1:], strict=False)
    )
    assert len({step["scene_horizontal_fov_deg"] for step in steps}) == 1
    assert all(
        step["source_identity_by_phase"][0] != step["source_identity_by_phase"][1]
        for step in steps
    )
    assert all(
        max(step["relative_change_from_previous"].values()) <= 0.05
        for step in steps[-2:]
    )


def test_both_phases_are_preflighted_before_either_executes(monkeypatch):
    checked_offsets = []
    executions = []

    def fake_preflight(provider, _sensor, **_kwargs):
        checked_offsets.append(provider.edge_offset_deg)
        return {
            "status": (
                "ready_under_declared_policy"
                if provider.edge_offset_deg == 0.0 else "blocked_scene_halo"
            ),
            "reason": "Phase source halo touched the boundary.",
        }

    def fake_compute(*_args, **_kwargs):
        executions.append(True)
        raise AssertionError("Blocked phase must prevent both executions")

    monkeypatch.setattr(convergence, "preflight_streamed_analytic_offaxis_mean", fake_preflight)
    monkeypatch.setattr(convergence, "compute_streamed_analytic_offaxis_mean", fake_compute)
    result = convergence.run_streamed_offaxis_mean_sampling_convergence(
        _optics(), _sensor(), **_arguments()
    )
    assert result["status"] == "inconclusive_resource_or_support_limit"
    assert result["blocked_refinement"] == 0
    assert checked_offsets == [0.0, 0.004]
    assert executions == []


def test_refinement_limit_does_not_promote_one_stable_step():
    arguments = _arguments()
    arguments["max_refinements"] = 2
    arguments["mean_electron_relative_tolerance"] = 1e-12
    arguments["shift_response_relative_tolerance"] = 1e-12
    result = convergence.run_streamed_offaxis_mean_sampling_convergence(
        _optics(), _sensor(), **arguments
    )
    assert result["status"] == "inconclusive_refinement_limit"
    assert result["physical_mtf_comparison_allowed"] is False
    assert len(result["steps"]) == 3
    assert any(
        step.get("declared_numerical_tolerance_met") is False
        for step in result["steps"][1:]
    )


def test_loose_tolerance_cannot_be_reported_as_converged():
    arguments = _arguments()
    arguments["mean_electron_relative_tolerance"] = 0.5
    try:
        convergence.run_streamed_offaxis_mean_sampling_convergence(
            _optics(), _sensor(), **arguments
        )
    except ValueError as exc:
        assert "10%" in str(exc)
    else:
        raise AssertionError("A 50% change must not pass a convergence gate")
