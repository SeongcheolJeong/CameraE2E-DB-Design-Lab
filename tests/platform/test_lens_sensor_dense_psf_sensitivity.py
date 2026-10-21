"""A dense PSF candidate may change only the declared geometric PSF planes."""

from __future__ import annotations

import copy

import numpy as np
import pytest

from camera_platform.lens_dense_geometric_psf_candidate import SCHEMA as ASSET_SCHEMA
from camera_platform.lens_geometric_psf_rehistogram import _array_sha
from camera_platform.lens_sensor_dense_psf_sensitivity import (
    compare_fixed_grid_dense_geometric_psf,
    optics_with_dense_geometric_psf_candidate,
)


def _fixture():
    source_psf = np.zeros((3, 3, 2, 1), dtype=np.float32)
    source_psf[1, 1, :, :] = 1.0
    candidate_psf = source_psf.copy()
    candidate_psf[1, 1, 1, 0] = 0.9
    candidate_psf[1, 2, 1, 0] = 0.1
    pitch = np.array([0.0005, 0.0005], dtype=float)
    fields = np.array([0.0, 1.0], dtype=float)
    waves = np.array([550.0], dtype=float)
    source = {
        "model": "raytrace",
        "focal_length_m": 0.004,
        "raytrace": {
            "contract_version": 2,
            "psf_kind": "geometric",
            "diffraction_included": False,
            "asset_sha256": "a" * 64,
            "chief_ray_angle_deg": np.zeros((2, 1)),
            "relative_illumination": {"transmission": np.ones((2, 1))},
            "psf": {
                "function": source_psf,
                "sample_spacing_mm": pitch,
                "field_height_mm": fields,
                "wavelength_nm": waves,
            },
        },
    }
    report = {
        "schema": ASSET_SCHEMA,
        "status": "all_stored_nodes_candidate_generated_model_only",
        "all_stored_field_wavelength_nodes_completed": True,
        "optical_psf_semantics": "geometric_pre_pixel_ray_histogram",
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "source_psf_asset_sha256": "a" * 64,
        "candidate_asset_sha256": "b" * 64,
        "source_psf_array_sha256": _array_sha(source_psf, "<f4"),
        "candidate_psf_array_sha256": _array_sha(candidate_psf, "<f4"),
        "candidate_psf_shape_row_col_field_wave": [3, 3, 2, 1],
        "fixed_physical_histogram_pitch_mm_row_col": pitch.tolist(),
        "field_height_mm": fields.tolist(),
        "wavelength_nm": waves.tolist(),
    }
    arrays = {
        "psf_function": candidate_psf,
        "sample_spacing_mm": pitch,
        "field_height_mm": fields,
        "wavelength_nm": waves,
    }
    return source, report, arrays


def test_overlay_changes_only_psf_planes_and_identity():
    source, report, arrays = _fixture()
    source_before = copy.deepcopy(source)
    selected = optics_with_dense_geometric_psf_candidate(source, report, arrays)
    assert selected["raytrace"]["asset_sha256"] == "b" * 64
    assert selected["raytrace"]["source_psf_asset_sha256"] == "a" * 64
    assert selected["raytrace"]["psf"]["function"][1, 2, 1, 0] == pytest.approx(0.1)
    assert np.array_equal(
        selected["raytrace"]["chief_ray_angle_deg"],
        source["raytrace"]["chief_ray_angle_deg"],
    )
    assert np.array_equal(
        source["raytrace"]["psf"]["function"],
        source_before["raytrace"]["psf"]["function"],
    )
    arrays["psf_function"][1, 2, 1, 0] = 0.0
    assert selected["raytrace"]["psf"]["function"][1, 2, 1, 0] == pytest.approx(0.1)
    assert selected["raytrace"]["geometric_pupil_density_candidate"][
        "physical_mtf_comparison_allowed"
    ] is False


def test_overlay_rejects_mismatched_grid_and_unverified_planes():
    source, report, arrays = _fixture()
    arrays["sample_spacing_mm"] = np.array([0.0004, 0.0004])
    with pytest.raises(ValueError, match="sample_spacing_mm"):
        optics_with_dense_geometric_psf_candidate(source, report, arrays)
    source, report, arrays = _fixture()
    arrays["psf_function"][1, 1, 1, 0] = 0.8
    with pytest.raises(ValueError, match="shape, dtype or values"):
        optics_with_dense_geometric_psf_candidate(source, report, arrays)
    source, report, arrays = _fixture()
    report["pixel_aperture_included"] = True
    with pytest.raises(ValueError, match="pre-pixel geometric"):
        optics_with_dense_geometric_psf_candidate(source, report, arrays)


def test_fixed_grid_comparison_keeps_inputs_and_disclaims_mtf(monkeypatch):
    source, report, arrays = _fixture()
    from camera_platform import lens_sensor_registered_roi_frequency_execution as execution

    calls = []

    def fake_run(optics, _sensor, *, return_transient_means, **kwargs):
        assert return_transient_means is True
        assert kwargs == {"max_refinements": 0}
        candidate = optics["raytrace"]["asset_sha256"] == "b" * 64
        calls.append(candidate)
        response = {
            "status": "inconclusive_refinement_limit",
            "sensor_identity": "pinned-sensor",
            "fixed_physical_input": {
                "wavelength_nm": [550.0], "bright_side_photons": [1.0],
            },
            "steps": [{
                "source_identity_by_phase": ["source-zero", "source-shift"],
                "scene_shape_row_col": [64, 64],
                "oi_pitch_um": 0.5,
                "mean_phase_signal_valid": True,
                "mean_electrons_sha256_by_phase": [
                    "candidate-0" if candidate else "source-0",
                    "candidate-1" if candidate else "source-1",
                ],
            }],
        }
        means = (
            np.array([[10.0, 20.0]]),
            np.array([[11.0, 21.0]]) if not candidate else np.array([[12.0, 22.0]]),
        )
        return response, means

    monkeypatch.setattr(execution, "run_registered_roi_frequency_sampling", fake_run)
    result = compare_fixed_grid_dense_geometric_psf(
        source, object(), report, arrays, runner_kwargs={"max_refinements": 0}
    )
    assert calls == [False, True]
    assert result["same_scene_and_sensor_inputs"] is True
    assert result["mean_electron_full_scale_normalized_max_delta"] == pytest.approx(1 / 22)
    assert result["shift_response_full_scale_normalized_max_delta"] == pytest.approx(1 / 22)
    assert result["scene_sampling_converged"] is False
    assert result["physical_mtf_comparison_allowed"] is False
    assert result["product_ranking_allowed"] is False


def test_fixed_grid_without_supported_edge_signal_stays_inconclusive(monkeypatch):
    source, report, arrays = _fixture()
    from camera_platform import lens_sensor_registered_roi_frequency_execution as execution

    def flat_capture(_optics, _sensor, *, return_transient_means, **_kwargs):
        assert return_transient_means is True
        return {
            "status": "inconclusive_refinement_limit",
            "sensor_identity": "same-sensor",
            "fixed_physical_input": {
                "wavelength_nm": [550.0], "bright_side_photons": [1.0],
            },
            "steps": [{
                "source_identity_by_phase": ["same-base", "same-shift"],
                "scene_shape_row_col": [64, 64], "oi_pitch_um": 0.5,
                "mean_electrons_sha256_by_phase": ["flat-0", "flat-1"],
                "mean_phase_signal_valid": False,
            }],
        }, (np.ones((2, 2)), np.ones((2, 2)))

    monkeypatch.setattr(execution, "run_registered_roi_frequency_sampling", flat_capture)
    result = compare_fixed_grid_dense_geometric_psf(
        source, object(), report, arrays, runner_kwargs={"max_refinements": 0}
    )
    assert result["same_scene_and_sensor_inputs"] is True
    assert result["edge_signal_valid_for_both_optics"] is False
    assert result["status"] == "inconclusive_edge_signal_support"
    assert result["physical_mtf_comparison_allowed"] is False
