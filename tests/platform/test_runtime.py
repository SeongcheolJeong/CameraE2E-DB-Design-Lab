"""Physics invariants and real CameraE2E readout/ISP adapter checks."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform.runtime import (
    LIGHT_M_S,
    PLANCK_J_S,
    RuntimeRequestError,
    get_plugin,
    run_plugin,
)


@pytest.fixture
def request_data():
    """An explicitly analytic test fixture, never a measured sensor model."""
    return {
        "wavelength_nm": [400, 500, 600, 700],
        "spectral_irradiance_w_m2_nm": [1e-5] * 4,
        "qe": [[0.3] * 4, [0.5] * 4, [0.2] * 4],
        "pixel_pitch_um": 4.0,
        "exposure_s": 0.004,
        "temperature_c": 25,
        "readout": {"full_well_e": 10000, "read_noise_e": 2,
                    "dark_current_e_per_s": 0, "gain": 1,
                    "bit_depth": 16, "black_level_dn": 256},
        "lens": {"field_radius": [0, 0.5, 1], "theta_deg": [0, 20],
                 "angular_weights": [[1, 0], [0.5, 0.5], [0, 1]],
                 "relative_illumination": [1, 0.9, 0.7]},
        "sensor_angular_response": {
            "theta_deg": [0, 20],
            "relative_response": [[[1, 0.5]] * 4, [[1, 1]] * 4, [[1, 0.8]] * 4],
        },
        "output_size": [32, 32], "frames": 8, "seed": 120,
    }


def test_quick_absolute_units_and_exposure_area_scaling(request_data):
    one = run_plugin("quick_estimate", request_data)
    # For constant E and QE, integral(lambda d lambda) has an exact analytic solution.
    integral_nm = (700**2 - 400**2) / 2
    incident = (4e-6)**2 * 0.004 * 1e-5 * 1e-9 * integral_nm / (PLANCK_J_S * LIGHT_M_S)
    np.testing.assert_allclose(one["summary"]["channel_electrons"],
                               [incident * np.array([0.3, 0.5, 0.2])], rtol=1e-14)
    request_data["exposure_s"] *= 2
    request_data["pixel_pitch_um"] *= 2
    changed = run_plugin("quick_estimate", request_data)
    np.testing.assert_allclose(changed["arrays"]["field_channel_electrons"],
                               one["arrays"]["field_channel_electrons"] * 8)
    assert one["summary"]["uncertainty"] == {"kind": "unknown"}
    json.dumps(one["summary"], allow_nan=False)


def test_zero_light_is_finite_json_and_has_no_invented_snr(request_data):
    request_data["spectral_irradiance_w_m2_nm"] = [0] * 4
    result = run_plugin("quick_estimate", request_data)["summary"]
    assert result["channel_electrons"] == [[0, 0, 0]]
    assert result["theoretical_snr_db"] == [[None, None, None]]
    assert result["prediction"]["values"] == [None, None]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("patch", [
    {"wavelength_nm": [400, 600, 500, 700]},
    {"spectral_irradiance_w_m2_nm": [0, -1, 0, 0]},
    {"qe": [[1.1] * 4] * 3},
    {"exposure_s": float("nan")},
    {"irradiance_reference_plane": "scene_radiance"},
    {"pixel_pitch_um": True},
])
def test_bad_physical_inputs_refused(request_data, patch):
    request_data.update(patch)
    assert get_plugin("quick_estimate").validate_request(request_data)
    with pytest.raises(RuntimeRequestError):
        run_plugin("quick_estimate", request_data)


def test_angular_domain_weights_and_workload_refused(request_data):
    for mutate in (
        lambda value: value["lens"].update(theta_deg=[0, 25]),
        lambda value: value["lens"].update(angular_weights=[[1, 1]] * 3),
        lambda value: value.update(output_size=[2048, 2048]),
        lambda value: value.update(frames=1000),
        lambda value: value["sensor_angular_response"].update(relative_response=[[[3, 3]] * 4] * 3),
        lambda value: value["sensor_angular_response"].update(
            relative_response=[[[0.9, 1]] * 4] * 3),
    ):
        data = deepcopy(request_data)
        mutate(data)
        with pytest.raises(RuntimeRequestError):
            run_plugin("response_db", data)


def test_real_engine_reproducibility_angular_shading_and_quantized_raw(request_data):
    one = run_plugin("response_db", request_data)
    two = run_plugin("response_db", request_data)
    arrays, summary = one["arrays"], one["summary"]
    assert arrays["raw_frames"].shape == (8, 32, 32)
    assert np.issubdtype(arrays["raw_frames"].dtype, np.integer)
    assert arrays["rgb"].shape == (32, 32, 3)
    assert np.max(arrays["rgb"]) > 0
    assert np.all((arrays["rgb"] >= 0) & (arrays["rgb"] <= 1))
    for name in arrays:
        np.testing.assert_array_equal(arrays[name], two["arrays"][name])
    assert not np.array_equal(arrays["raw_frames"][0], arrays["raw_frames"][1])
    expected_ratio = [[0.6, 0.4], [0.45, 0.36], [0.3, 0.32]]
    np.testing.assert_allclose(summary["channel_ratios"], expected_ratio)
    assert summary["isp"]["color_matrix"] == "identity"
    assert summary["physical_validation"] == "not_established"
    assert summary["prediction"]["uncertainty"] == {"kind": "unknown"}
    json.dumps(summary, allow_nan=False)
    assert request_data["seed"] == 120


def test_temporal_variance_matches_poisson_read_noise_and_is_not_spatial_std(request_data):
    result = run_plugin("response_db", request_data)
    arrays, summary = result["arrays"], result["summary"]
    dn_per_e = summary["capture_metadata"][0]["dn_per_electron"]
    expected_variance = (arrays["mean_electrons"].mean() + 2**2) * dn_per_e**2
    observed_variance = arrays["raw_temporal_variance_dn2"].mean()
    assert observed_variance == pytest.approx(expected_variance, rel=0.12)
    assert summary["raw_temporal_noise_dn"] == pytest.approx(np.sqrt(observed_variance))
    assert summary["raw_temporal_snr_db"] is not None
    assert arrays["raw_frames"][0].std() > summary["raw_temporal_noise_dn"] * 2


def test_exposure_changes_actual_raw_and_rgb_and_gain_keeps_photons_fixed(request_data):
    request_data.update(noise_enabled=False, frames=1)
    one = run_plugin("response_db", request_data)
    request_data["exposure_s"] *= 2
    two = run_plugin("response_db", request_data)
    np.testing.assert_allclose(two["arrays"]["mean_electrons"],
                               one["arrays"]["mean_electrons"] * 2)
    assert two["arrays"]["raw_frames"].mean() > one["arrays"]["raw_frames"].mean()
    assert two["arrays"]["rgb"].mean() > one["arrays"]["rgb"].mean()
    request_data["readout"]["gain"] = 2
    gain = run_plugin("response_db", request_data)
    np.testing.assert_array_equal(gain["arrays"]["mean_electrons"],
                                  two["arrays"]["mean_electrons"])
    assert gain["arrays"]["raw_frames"].mean() > two["arrays"]["raw_frames"].mean()
    assert one["summary"]["raw_temporal_snr_db"] is None


def test_saturation_withholds_snr_and_dark_frame_is_black_pedestal(request_data):
    request_data["exposure_s"] = 1
    saturated = run_plugin("response_db", request_data)
    assert saturated["summary"]["saturation_fraction"] > 0.001
    assert saturated["summary"]["raw_temporal_snr_db"] is None
    assert saturated["summary"]["theoretical_snr_db"] == [[None] * 3] * 3
    request_data.update(exposure_s=0, noise_enabled=False, frames=1)
    dark = run_plugin("response_db", request_data)
    assert np.all(dark["arrays"]["raw_frames"] == 256)
    assert np.max(dark["arrays"]["rgb"]) == 0


def test_no_silent_runtime_fallback(request_data):
    with pytest.raises(RuntimeRequestError, match="Unsupported runtime"):
        run_plugin("fdtd", request_data)
