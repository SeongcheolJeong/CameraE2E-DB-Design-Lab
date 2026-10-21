"""Large-Scene refusal and independent Scene/PSF sampling gates."""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.ndimage import map_coordinates, rotate

from camera_platform.lens_sensor_large_scene_preflight import (
    preflight_large_scene,
    require_large_scene_resources,
)
from camera_platform.lens_sensor_raw_bridge import _scene_samples
from camera_platform.lens_sensor_raw_job import (
    default_large_common_scene_spec,
    specification,
)
from camera_platform.lens_sensor_sampling_convergence import (
    adaptive_lens_sensor_raw_preview,
    run_scene_sampling_convergence,
)
from camera_platform.lens_sensor_sampling_evidence import (
    _kernel_metrics,
    optical_psf_sampling_evidence,
)


def _lens_model() -> dict:
    return {
        "psf_samples_row_col": [512, 512],
        "sample_spacing_mm_row_col": [0.000612884917, 0.000612884917],
        "field_samples_mm": [0, 0.2, 0.4],
        "nominal_focal_length_mm": 12.0,
        "effective_focal_length_mm": 12.0,
    }


def test_legacy_scene_cap_and_opt_in_1024_process_guard_refusal():
    with pytest.raises(ValueError, match="through 256"):
        _scene_samples([1024, 1024], [32, 32])
    assert _scene_samples([1024, 1024], [32, 32], max_samples=1024) == (1024, 1024)
    spec = default_large_common_scene_spec()
    spec.update(
        lens_revision_id="lens", sensor_revision={"identifier": "sensor", "run_id": "run"},
        accept_approximate_lens_radiometry=True,
        scene_horizontal_fov_deg=0.479997368132,
        sensor_size=[32, 32],
    )
    assert specification(spec)["scene_samples_row_col"] == [1024, 1024]
    with pytest.raises(ValueError, match="through 256"):
        old = dict(spec)
        old.pop("resource_policy")
        old["schema"] = "camerae2e_lens_sensor_raw_preview_study_v5"
        specification(old)
    preflight = preflight_large_scene(
        _lens_model(), 0.61, [32, 32],
        scene_horizontal_fov_deg=0.479997368132,
        scene_samples_row_col=[1024, 1024],
    )
    assert preflight["scene_samples_row_col"] == [1024, 1024]
    assert preflight["array_classes"]["initial_d65_spectral_scene_float64_bytes"] == (
        1024 * 1024 * 31 * 8
    )
    assert preflight["worst_case_sparse_fft_samples_row_col"][0] > 1024
    assert preflight["enforced_child_process_rss_guard"] is False
    assert preflight["estimate_is_peak_rss_upper_bound"] is False
    with pytest.raises(ValueError, match="resource_blocked"):
        require_large_scene_resources(preflight)


def test_analytic_scene_refinement_tracks_electrons_mtf_and_fixed_shift():
    calls = []

    def evaluator(samples: int, offset: float) -> dict:
        calls.append((samples, offset))
        row, col = np.indices((16, 16))
        physical_x = (col + 0.5 - 8) * 0.01 - (row + 0.5 - 8) * 0.01 / 12
        # Same continuous edge and fixed native output; only numerical
        # sampling error changes with source grid density.
        electrons = 100 + 800 / (1 + np.exp(-(physical_x - offset) / 0.018))
        electrons += 20 / samples
        return {
            "source_kind": "analytic_continuous", "scene_horizontal_fov_deg": 0.12,
            "lens_identity": "L1", "sensor_identity": "S1", "exposure_s": 0.004,
            "roi": [0, 16, 0, 16], "psf_sampling_model": "optical_pre_pixel",
            "noise_flag": 0, "mean_electrons": electrons,
            "edge_mtf50": 0.28 + 1 / samples,
            "source_halo_verified": False, "psf_resampling_verified": False,
        }

    result = run_scene_sampling_convergence(
        evaluator, first_samples=64, max_samples=256, subpixel_shift_deg=0.002,
        mean_electron_relative_tolerance=0.01,
        edge_mtf50_relative_tolerance=0.03,
        shift_response_relative_tolerance=0.01,
    )
    assert result["attempted_grids"][:2] == [[64, 64], [128, 128]]
    assert calls[:4] == [(64, 0.0), (64, 0.002), (128, 0.0), (128, 0.002)]
    assert result["physical_mtf_comparison_allowed"] is False
    assert result["status"] == "numeric_sampling_converged_input_support_inconclusive"
    assert result["attempted_grids"] == [[64, 64], [128, 128], [256, 256]]
    assert result["steps"][-1]["consecutive_passes"] == 2
    assert result["steps"][-1]["normalized_native_edge_contrast"][0] > 0.05
    one_pass = run_scene_sampling_convergence(
        evaluator, first_samples=64, max_samples=256, subpixel_shift_deg=0.002,
        mean_electron_relative_tolerance=0.01,
        edge_mtf50_relative_tolerance=0.02,
        shift_response_relative_tolerance=0.01,
    )
    assert one_pass["status"] == "inconclusive_resource_limit"
    assert one_pass["steps"][-1]["consecutive_passes"] == 1


def test_flat_or_invalid_edge_cannot_pass_even_if_arrays_are_unchanged():
    def flat(samples: int, offset: float) -> dict:
        return {
            "source_kind": "analytic_continuous", "scene_horizontal_fov_deg": 0.12,
            "lens_identity": "L", "sensor_identity": "S", "exposure_s": 0.004,
            "roi": [0, 16, 0, 16], "psf_sampling_model": "optical_pre_pixel",
            "noise_flag": 0, "mean_electrons": np.ones((16, 16)) * 100,
            "edge_mtf50": 0.0, "source_halo_verified": True,
            "psf_resampling_verified": True,
        }
    result = run_scene_sampling_convergence(
        flat, first_samples=64, max_samples=128, subpixel_shift_deg=0.002,
        mean_electron_relative_tolerance=0.01,
        edge_mtf50_relative_tolerance=0.01,
        shift_response_relative_tolerance=0.01,
    )
    assert result["status"] == "inconclusive_invalid_edge_metric"
    assert result["physical_mtf_comparison_allowed"] is False


def test_adaptive_raw_wrapper_uses_analytic_offsets_and_below_seed_ladder(monkeypatch):
    from camera_platform import lens_catalog_bridge, lens_sensor_raw_bridge, sensor_db_bridge
    from camera_platform import lens_sensor_sampling_convergence as convergence

    monkeypatch.setattr(lens_catalog_bridge, "geometric_lens_preview", lambda *_a, **_k: {
        "model": _lens_model(),
    })
    monkeypatch.setattr(sensor_db_bridge, "inspect_sensor_revision", lambda *_a, **_k: {
        "executable": True,
        "native_response": {"kind": "db_estimate_v1", "pixel_pitch_um": 0.61},
    })
    calls = []

    def preview(*_args, **kwargs):
        n = kwargs["scene_samples_row_col"][0]
        offset = kwargs["analytic_slanted_edge_offset_deg"]
        calls.append((n, offset))
        row, col = np.indices((16, 16))
        image = 100 + 800 / (1 + np.exp(-((col - 8) - (row - 8) / 12 - offset * 500) / 2))
        return {
            "mean_electrons": {"values": (image + 1 / n).tolist()},
            "scene": {"horizontal_fov_deg": kwargs["scene_horizontal_fov_deg"],
                      "sampling_evidence": {
                          "source_kind": "analytic_continuous_half_plane",
                          "optical_psf": {
                              "sampling_model": "optical_psf_before_mosaic_pixel_integration"
                          },
                      }},
            "lens_identity": "L1", "sensor_revision": "S1",
            "capture": {"exposure_s": kwargs["exposure_s"]},
        }

    monkeypatch.setattr(lens_sensor_raw_bridge, "preview_lens_sensor_raw", preview)
    monkeypatch.setattr(convergence, "iso12233", lambda *_a, **_k: SimpleNamespace(mtf50=0.3))
    result = adaptive_lens_sensor_raw_preview(
        "L1", "S1", sensor_run_id="R1", root=".", workspace=".",
        scene_horizontal_fov_deg=0.48, sensor_size=(16, 16), max_scene_samples=256,
    )
    assert result["starting_pitch_bound_unmet"] is True
    assert result["attempted_grids"][:2] == [[32, 32], [64, 64]]
    assert calls[0][1] == 0.0 and calls[1][1] > 0.0
    assert all(offset == calls[1][1] for _, offset in calls[1::2])
    assert result["edge_estimator"].endswith("numeric_proxy")
    assert result["physical_mtf_comparison_allowed"] is False


def test_unit_sum_does_not_hide_asymmetric_rotation_clipping():
    row, col = np.indices((41, 41), dtype=float)
    source = np.exp(-((row - 35) / 3) ** 2 - ((col - 35) / 6) ** 2)
    source /= source.sum()
    rotated = rotate(source, 45, reshape=False, order=1, mode="constant", cval=0)
    assert rotated.sum() < 0.85
    normalized = rotated / rotated.sum()
    assert math.isclose(normalized.sum(), 1.0, rel_tol=1e-12)
    before = _kernel_metrics(source, (1, 1))
    after = _kernel_metrics(normalized, (1, 1))
    assert before["rms_width_um_row_col"] != after["rms_width_um_row_col"]


def test_central_psf_diagnostic_uses_executed_resampling_grid(monkeypatch):
    from camera_platform import lens_sensor_sampling_evidence as evidence

    row, col = np.indices((5, 5), dtype=float)
    source = np.exp(-((row - 2) ** 2 + (col - 2) ** 2) / 2)
    source /= source.sum()
    table = {
        "function": np.zeros((5, 5, 1, 3)),
        "sample_spacing_mm": np.array([0.001, 0.001]),
    }
    def interp(_optics, *, x_grid_m=None, y_grid_m=None, **_kwargs):
        if x_grid_m is None:
            return source
        r = (y_grid_m * 1e6 - 2) / -1
        c = (x_grid_m * 1e6 + 2)
        raw = map_coordinates(source, [r, c], order=1, mode="constant", cval=0)
        return raw / raw.sum()

    monkeypatch.setattr(evidence, "rt_psf_interp", interp)
    result = optical_psf_sampling_evidence(
        {"raytrace": {"psf": table}}, convolution_pitch_um=0.5,
        scene_shape_row_col=(4, 4), wavelengths_nm=(450.0,),
    )
    assert result["bands"][0]["resampled_for_optical_convolution"]["shape_row_col"] == [9, 9]
    assert result["bands"][0]["assumed_point_density_quadrature_ratio"] > 0
    assert result["rotation_clipping_and_full_field_energy_preservation"] == "not_assessed"


def test_derived_psf_sampling_evidence_keeps_parent_pitch_distinct(monkeypatch):
    from camera_platform import lens_sensor_sampling_evidence as evidence

    row, col = np.indices((5, 5), dtype=float)
    kernel = np.exp(-((row - 2) ** 2 + (col - 2) ** 2) / 2)
    kernel /= kernel.sum()
    table = {
        "function": kernel[:, :, None, None],
        "sample_spacing_mm": np.array([0.001, 0.001]),
        "derived_kernel_sha256": "derived-hash",
    }

    def interp(_optics, *, x_grid_m=None, y_grid_m=None, **_kwargs):
        assert (x_grid_m is None and y_grid_m is None) or x_grid_m.shape == (5, 5)
        return kernel

    monkeypatch.setattr(evidence, "rt_psf_interp", interp)
    result = optical_psf_sampling_evidence(
        {"raytrace": {
            "psf": table,
            "asset_sha256": "parent-asset",
            "geometric_psf_rehistogram": {
                "source_psf_asset_sha256": "parent-asset",
                "source_psf_pitch_um_row_col": [2.0, 2.0],
                "source_psf_shape_row_col": [3, 3],
                "derived_psf_pitch_um_row_col": [1.0, 1.0],
                "derived_psf_shape_row_col": [5, 5],
                "derived_psf_signature_sha256": "derived-hash",
                "selected_off_axis_45_degree_rotation_diagnostic": {"qualified": False},
            },
        }},
        convolution_pitch_um=1.0,
        scene_shape_row_col=(4, 4),
        wavelengths_nm=(550.0,),
    )
    assert result["stored_psf_pitch_um_row_col"] == [2.0, 2.0]
    assert result["derived_psf_pitch_um_row_col"] == [1.0, 1.0]
    assert result["stored_psf_shape_row_col"] == [3, 3]
    assert result["derived_psf_shape_row_col"] == [5, 5]
    assert "derived_geometric_psf_table" in result["bands"][0]
    assert "source" not in result["bands"][0]
    assert result["independent_ray_rehistogram_validation"].startswith("same_admitted_rays")
    assert result["physical_validation"] == "not_established"
