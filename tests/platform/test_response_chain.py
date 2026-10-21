"""Actual chain effects against independent area/angle/ADC references."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from run_camera_response_comparison import (
    WAVE, PITCH, PIXELS, EXPOSURE, base_request, native_response, pupil_lens,
    independent_charge, reference_raw, airy_bins,
)
from camera_platform import electrical, spatial
from camera_platform.isp import default_profile, process_raw
from camera_platform.response_chain import evaluate_response


@pytest.fixture
def request_data():
    psf = spatial.request_template()["psf"]
    psf.update(wavelength_nm=WAVE, sample_pitch_um=PITCH, values=[[[1.0]], [[1.0]]])
    lens, _ = pupil_lens(4.0)
    return base_request(psf, lens, native_response(PIXELS["angular_sensitive"]), 1e-4, 1.0)


def capture(request, gain=1, **kwargs):
    model = electrical.default_electrical_model(request["readout"])
    return evaluate_response(request, model, exposure_s=EXPOSURE, analog_gain=gain, **kwargs)


def test_independent_angle_area_spectral_charge_and_adc(request_data):
    output = capture(request_data)
    expected = independent_charge(request_data, PIXELS["angular_sensitive"], 4.0)
    np.testing.assert_allclose(output["arrays"]["mean_photocharge_e"], expected, rtol=2e-6)
    model = electrical.default_electrical_model(request_data["readout"])
    raw = reference_raw(output["arrays"]["mean_photocharge_e"], model, 1)
    np.testing.assert_array_equal(output["arrays"]["raw_dn"], raw)
    assert len(output["summary"]["site_metrics"]) == 4
    assert set(output["arrays"]) <= set(output["summary"]["array_units"])


def test_gain_changes_only_readout_and_never_input_or_charge(request_data):
    before = deepcopy(request_data)
    a, b = capture(request_data), capture(request_data, 2)
    np.testing.assert_array_equal(a["arrays"]["mean_photocharge_e"], b["arrays"]["mean_photocharge_e"])
    np.testing.assert_array_equal(request_data["scene"]["spectral_irradiance"], before["scene"]["spectral_irradiance"])
    assert request_data["lens"] == before["lens"]
    assert request_data["native_pixel_response"] == before["native_pixel_response"]
    assert not np.array_equal(a["arrays"]["raw_dn"], b["arrays"]["raw_dn"])
    black = request_data["readout"]["black_level_dn"]
    error = b["arrays"]["raw_dn"]-black-2*(a["arrays"]["raw_dn"]-black)
    assert np.max(np.abs(error)) <= 1


def test_explicit_basic_isp_consumes_original_integer_raw_without_recapture(request_data):
    profile = default_profile()
    profile["tone"]["curve"] = "linear"
    first = capture(request_data, isp_profile=profile)
    tuned = deepcopy(profile)
    tuned["white_balance"]["gains_rgb"] = [2.0, 1.0, 1.0]
    second = capture(request_data, isp_profile=tuned)
    for name in ("incident_spectral_irradiance", "mean_photocharge_e", "raw_dn"):
        np.testing.assert_array_equal(first["arrays"][name], second["arrays"][name])
    raw = first["arrays"]["raw_dn"]
    assert np.issubdtype(raw.dtype, np.integer)
    np.testing.assert_array_equal(
        first["arrays"]["black_subtracted_normalized_frames"][0],
        (raw.astype(float) - 64) / (4095 - 64),
    )
    assert first["summary"]["isp"]["input_array_bytes_sha256"] == hashlib.sha256(
        raw.tobytes(order="C")
    ).hexdigest()
    assert first["summary"]["method_and_version"] == "explicit_response_chain_v2"
    assert first["arrays"]["rgb_frames"].shape == (1, *raw.shape, 3)
    assert np.max(first["arrays"]["rgb"][..., 0]) < 0.5
    np.testing.assert_allclose(second["arrays"]["rgb"][..., 0],
                               2 * first["arrays"]["rgb"][..., 0])
    np.testing.assert_array_equal(second["arrays"]["rgb"][..., 1:],
                                  first["arrays"]["rgb"][..., 1:])
    replay = process_raw(raw, first["summary"]["isp"]["raw_format"], tuned,
                         geometry=first["summary"]["isp"]["geometry"])
    np.testing.assert_array_equal(replay["arrays"]["rgb_frames"],
                                  second["arrays"]["rgb_frames"])
    assert replay["summary"]["input_array_bytes_sha256"] == first["summary"]["isp"][
        "input_array_bytes_sha256"
    ]


def test_pixel_change_changes_charge_without_changing_blur(request_data):
    changed = deepcopy(request_data)
    changed["native_pixel_response"] = native_response(PIXELS["wide_angle"])
    a, b = capture(request_data), capture(changed)
    np.testing.assert_array_equal(a["arrays"]["blurred_spectral_irradiance"], b["arrays"]["blurred_spectral_irradiance"])
    assert np.all(b["arrays"]["mean_photocharge_e"] > a["arrays"]["mean_photocharge_e"])
    assert np.mean(b["arrays"]["raw_dn"]) > np.mean(a["arrays"]["raw_dn"])


def test_throughput_is_not_implicitly_applied_twice(request_data):
    changed = deepcopy(request_data)
    changed["scene"]["spectral_irradiance"] *= 0.25
    a, b = capture(request_data), capture(changed)
    np.testing.assert_allclose(b["arrays"]["mean_photocharge_e"], a["arrays"]["mean_photocharge_e"]*0.25, rtol=1e-14)


def test_adc_clipping_can_precede_full_well(request_data):
    request_data["scene"]["spectral_irradiance"] *= 25
    a, b = capture(request_data), capture(request_data, 2)
    np.testing.assert_array_equal(a["arrays"]["well_saturated_mask"], b["arrays"]["well_saturated_mask"])
    assert np.any(b["arrays"]["adc_high_mask"] & ~b["arrays"]["well_saturated_mask"])


def test_continuous_airy_broadening_has_expected_direction():
    a, b = airy_bins(4), airy_bins(8)
    assert np.all(a[:, 15, 15] > b[:, 15, 15])
    assert np.all((a.sum(axis=(1, 2)) > 0.95) & (a.sum(axis=(1, 2)) < 1))
    np.testing.assert_allclose(a, a[:, ::-1, :], atol=1e-14)


@pytest.mark.parametrize("gain", [True, 0, float("nan")])
def test_invalid_gain_does_not_start_optics(request_data, gain):
    with pytest.raises(ValueError, match="analog_gain"):
        capture(request_data, gain)
