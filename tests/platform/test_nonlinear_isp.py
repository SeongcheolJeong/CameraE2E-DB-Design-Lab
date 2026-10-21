"""Calibrated nonlinear RAW preview: signed charge, CFA and invalid support."""

import hashlib
import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import isp, nonlinear_isp, nonlinear_readout


def model():
    return {
        "schema": "camera_electrical_readout_v2", "full_well_e": 2000,
        "dark_current_e_per_s": 0, "reference_temperature_c": 25,
        "dark_current_doubling_c": None, "digital_gain": 1,
        "adc": {"bit_depth": 12, "full_scale_v": .4095, "black_level_dn": 64},
        "fixed_pattern": {"prnu_sigma": 0, "dsnu_e_per_s": 0, "seed": 7},
        "charge_voltage_response": {
            "schema": "camera_charge_voltage_response_v1",
            "units": {"charge": "electron", "voltage": "V"},
            "charge_e": [0, 1000, 2000], "signal_voltage_v": [0, .1, .15],
            "valid_domain": {"temperature_c": [20, 30], "analog_gain": [1, 4]},
            "signed_inverse": {"minimum_voltage_v": -.02, "method": "origin_tangent"},
            "source": {"kind": "estimated", "description": "Authored monotone fixture"},
        },
        "read_noise": {
            "schema": "camera_voltage_read_noise_v1",
            "plane": "conversion_output_before_analog_gain",
            "variance_v2": 0, "temperature_range_c": [20, 30], "analog_gain_range": [1, 4],
            "includes": "all_temporal_read_components",
            "source": {"kind": "estimated", "description": "Noiseless fixture"},
        },
        "source": {"kind": "estimated", "description": "Authored nonlinear readout fixture"},
    }


def geometry(origin=(0, 0)):
    return {"kind": "native_sensor", "full_sensor_shape": [20, 24], "crop_origin": list(origin)}


def calibration(shape=(8, 10), origin=(0, 0), dark=0):
    geom = geometry(origin)
    return nonlinear_readout.make_calibration(
        model(), analog_gain=1, temperature_c=25, exposure_s=1,
        geometry={key: geom[key] for key in ("full_sensor_shape", "crop_origin")},
        dark_e=np.full(shape, float(dark)), noise_enabled=False,
    )


def mosaic(charge_rgb=(500, 1200, 1500), origin=(0, 0), shape=(8, 10)):
    # Independent scalar transfer: first segment 1 DN/e, second .5 DN/e.
    codes = [int(round(64 + (charge if charge <= 1000 else 1000 + (charge - 1000) / 2)))
             for charge in charge_rgb]
    yy = np.arange(shape[0])[:, None] + origin[0]
    xx = np.arange(shape[1])[None, :] + origin[1]
    sites = np.array([[0, 1], [1, 2]])[yy % 2, xx % 2]
    return np.asarray(codes, dtype=np.uint16)[sites]


def linear_profile():
    profile = isp.default_profile()
    profile["tone"]["curve"] = "linear"
    return profile


@pytest.mark.parametrize("origin", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_native_phases_decode_charge_before_bilinear_with_independent_scalar_oracle(origin):
    raw = mosaic(origin=origin)
    result = nonlinear_isp.process_raw(raw, calibration(origin=origin), linear_profile(),
                                       geometry=geometry(origin))
    expected = np.broadcast_to([.25, .6, .75], (1, 8, 10, 3))
    np.testing.assert_allclose(result["arrays"]["demosaiced_rgb_frames"], expected, rtol=1e-14)
    np.testing.assert_allclose(result["arrays"]["rgb_frames"], expected, rtol=1e-14)
    assert result["arrays"]["raw_valid_mask_frames"].all()
    assert result["arrays"]["linear_valid_mask_frames"].all()
    # A linear-DN normalization would yield 500/4031, not the recovered 500/2000.
    assert result["arrays"]["rgb"][0, 0, 0] != pytest.approx(500 / 4031)
    summary = result["summary"]
    assert summary["geometry"] == geometry(origin)
    assert summary["executed_graph"][0] == "nonlinear_raw_interval_decode"
    assert summary["color_settings_profile_fields_used"] == ["white_balance", "ccm", "tone"]
    assert summary["preview"]["isp_profile_role"] == "fixed_color_settings_only"
    assert "profile" not in summary
    assert "physical_validation" in summary and summary["physical_validation"] == "not_established"


def test_zero_dark_subtraction_and_signed_residual_are_never_early_clipped():
    zero = nonlinear_isp.process_raw(mosaic((0, 0, 0)), calibration(), linear_profile(),
                                     geometry=geometry())
    np.testing.assert_array_equal(zero["arrays"]["rgb"], 0)
    assert zero["arrays"]["linear_valid_mask_frames"].all()
    result = nonlinear_isp.process_raw(mosaic((-10, 500, 1500)), calibration(dark=10),
                                       linear_profile(), geometry=geometry())
    linear = result["arrays"]["ccm_rgb_frames"]
    np.testing.assert_allclose(linear[0, 3, 3], [-.01, .245, .745], atol=1e-14)
    np.testing.assert_allclose(result["arrays"]["rgb"][3, 3], [0, .245, .745], atol=1e-14)
    assert np.nanmin(result["arrays"]["black_subtracted_normalized_frames"]) < 0
    assert np.nanmax(result["arrays"]["demosaiced_rgb_frames"][..., 0]) < 0


def test_nonidentity_fixed_wb_ccm_and_srgb_match_separate_scalar_reference():
    profile = linear_profile()
    profile["white_balance"]["gains_rgb"] = [2, .5, 1]
    profile["ccm"]["matrix_rgb"] = [[0, 1, 0], [1, 0, -.25], [.2, .3, .5]]
    result = nonlinear_isp.process_raw(mosaic(), calibration(), profile, geometry=geometry())
    np.testing.assert_allclose(result["arrays"]["white_balanced_rgb_frames"][0, 3, 3],
                               [.5, .3, .75])
    corrected = np.array([.3, .3125, .565])
    np.testing.assert_allclose(result["arrays"]["ccm_rgb_frames"][0, 3, 3], corrected)
    profile["tone"]["curve"] = "srgb"
    encoded = nonlinear_isp.process_raw(mosaic(), calibration(), profile, geometry=geometry())
    expected = 1.055 * corrected ** (1 / 2.4) - .055
    np.testing.assert_allclose(encoded["arrays"]["rgb"][3, 3], expected, rtol=1e-14)


@pytest.mark.parametrize("position", [(3, 4), (0, 0), (0, 9), (7, 0), (7, 9)])
def test_invalid_raw_expands_conservative_support_without_filling_physical_nan(position):
    raw = mosaic((500, 500, 500))
    raw[position] = 0
    result = nonlinear_isp.process_raw(raw, calibration(), linear_profile(), geometry=geometry())
    arrays = result["arrays"]
    y, x = position
    expected = np.ones(raw.shape, dtype=bool)
    expected[max(0, y - 1):y + 2, max(0, x - 1):x + 2] = False
    np.testing.assert_array_equal(arrays["linear_valid_mask_frames"][0], expected)
    assert np.isnan(arrays["black_subtracted_normalized_frames"][0, y, x])
    for key in ("demosaiced_rgb_frames", "white_balanced_rgb_frames", "ccm_rgb_frames"):
        assert np.isnan(arrays[key][0][~expected]).all()
        np.testing.assert_allclose(arrays[key][0][expected], .25, rtol=1e-14)
    np.testing.assert_array_equal(arrays["rgb"][~expected], 0)
    np.testing.assert_allclose(arrays["rgb"][expected], .25, rtol=1e-14)


def test_all_censored_raw_has_nan_physical_arrays_and_finite_black_preview():
    raw = np.full((8, 10), 4095, dtype=np.uint16)
    result = nonlinear_isp.process_raw(raw, calibration(), linear_profile(), geometry=geometry())
    for key in ("black_subtracted_normalized_frames", "demosaiced_rgb_frames",
                "white_balanced_rgb_frames", "ccm_rgb_frames"):
        assert np.isnan(result["arrays"][key]).all()
    assert not result["arrays"]["linear_valid_mask_frames"].any()
    np.testing.assert_array_equal(result["arrays"]["rgb"], 0)
    json.dumps(result["summary"], allow_nan=False)


def test_readonly_raw_and_all_arguments_remain_immutable_with_detached_outputs():
    raw = mosaic()
    raw.flags.writeable = False
    before = raw.tobytes()
    cal, geom, profile = calibration(), geometry(), linear_profile()
    metadata_before = deepcopy((cal, geom, profile))
    first = nonlinear_isp.process_raw(raw, cal, profile, geometry=geom)
    second = nonlinear_isp.process_raw(raw, cal, profile, geometry=geom)
    assert raw.tobytes() == before
    assert (geom, profile) == metadata_before[1:]
    np.testing.assert_array_equal(cal["dark_e"], metadata_before[0]["dark_e"])
    assert {k: v for k, v in cal.items() if k != "dark_e"} == {
        k: v for k, v in metadata_before[0].items() if k != "dark_e"
    }
    assert first["summary"] == second["summary"]
    assert first["summary"]["input_array_bytes_sha256"] == hashlib.sha256(before).hexdigest()
    assert set(first["summary"]["array_units"]) == set(first["arrays"])
    for key, array in first["arrays"].items():
        assert not np.shares_memory(array, raw)
        np.testing.assert_array_equal(array, second["arrays"][key])
    first["arrays"]["rgb"][0, 0] = 0
    assert not np.array_equal(first["arrays"]["rgb"], first["arrays"]["rgb_frames"][0])
    first["summary"]["color_settings_profile"]["tone"]["curve"] = "srgb"
    assert profile["tone"]["curve"] == "linear"


@pytest.mark.parametrize("mutate", [
    lambda g: g.update(kind="representative_normalized_field_raster"),
    lambda g: g.update(crop_origin=[1, 0]),
    lambda g: g.update(full_sensor_shape=[22, 24]),
    lambda g: g.update(extra=True),
    lambda g: g.update(crop_origin=[True, 0]),
])
def test_geometry_must_match_fixed_calibration_without_native_phase_guessing(mutate):
    geom = geometry()
    mutate(geom)
    with pytest.raises(ValueError):
        nonlinear_isp.process_raw(mosaic(), calibration(), linear_profile(), geometry=geom)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema="camera_isp_profile_v2"),
    lambda p: p.update(graph=list(reversed(p["graph"]))),
    lambda p: p["black_level"].update(negative_policy="clip"),
    lambda p: p["demosaic"].update(method="other"),
    lambda p: p["white_balance"].update(mode="auto"),
])
def test_unsupported_advanced_or_inconsistent_profiles_are_explicitly_refused(mutate):
    profile = linear_profile()
    mutate(profile)
    with pytest.raises(ValueError):
        nonlinear_isp.process_raw(mosaic(), calibration(), profile, geometry=geometry())


@pytest.mark.parametrize("raw", [
    np.zeros((1, 8, 10), dtype=np.uint16), np.zeros((8, 10), dtype=float),
    np.zeros((8, 10), dtype=bool), np.zeros((8, 10), dtype=np.uint64),
    np.zeros((7, 10), dtype=np.uint16), np.zeros((1, 10), dtype=np.uint16),
    np.zeros((0, 10), dtype=np.uint16), np.zeros((8, 10), dtype=object),
])
def test_raw_input_refuses_unsupported_shape_type_and_stack(raw):
    with pytest.raises(ValueError):
        nonlinear_isp.process_raw(raw, calibration(), linear_profile(), geometry=geometry())


def test_calibration_hash_tamper_and_backend_invalid_values_are_not_hidden(monkeypatch):
    cal = calibration()
    cal["model"]["full_well_e"] = 1900
    with pytest.raises(ValueError, match="hash"):
        nonlinear_isp.process_raw(mosaic(), cal, linear_profile(), geometry=geometry())
    import pyisetcam.ip
    monkeypatch.setattr(pyisetcam.ip, "_ie_bilinear", lambda planes, pattern: planes * np.nan)
    with pytest.raises(ValueError, match="nonfinite valid"):
        nonlinear_isp.process_raw(mosaic(), calibration(), linear_profile(), geometry=geometry())
