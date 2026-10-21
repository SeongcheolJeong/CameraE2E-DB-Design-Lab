"""Electrical readout comparisons preserve charge, units and noise structure."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import electrical
from camera_platform import reference_readout_lab as lab
from camera_platform import reference_sensor_modes as core


def readout():
    return dict(full_well_e=10000., read_noise_e=10., dark_current_e_per_s=10.,
                gain=1., bit_depth=12, black_level_dn=64.)


def capture(recipe="gain_budget", electrons=25., shape=(32, 32), **kwargs):
    settings = kwargs.pop("settings", {})
    return lab.readout_modes(np.full(shape, electrons / .004), readout(), recipe=recipe,
                             settings=settings, **kwargs)


@pytest.mark.parametrize("settings", [
    {"unknown": 1}, {"gain_total": True}, {"gain_total": 33},
    {"post_gain_noise_e": -1}, {"cds_rms_e": float("nan")},
    {"cds_correlation": 1.1}, {"column_fraction": -.1},
    {"adc_bits": [10, 10]}, {"adc_bits": [10.0]}, {"adc_bits": [True]},
    {"adc_bits": [8, 10, 12, 14]}, {"adc_bits": []},
])
def test_settings_reject_unsupported_or_ambiguous_values(settings):
    with pytest.raises(ValueError):
        lab.validate_settings(settings)


def test_settings_are_copied_and_partial_settings_resolve_explicit_defaults():
    settings = {"adc_bits": [10, 12]}
    original = deepcopy(settings)
    result = lab.validate_settings(settings)
    assert result["gain_total"] == 8
    result["adc_bits"].append(14)
    assert settings == original


def test_gain_modes_reacquire_from_same_mean_and_calibrate_to_equal_electrons(monkeypatch):
    original = electrical.capture_electrons
    calls = []

    def record(photo, *args, **kwargs):
        calls.append((photo.copy(), kwargs["analog_gain"], kwargs["seed"]))
        return original(photo, *args, **kwargs)

    monkeypatch.setattr(electrical, "capture_electrons", record)
    result = capture(electrons=123.4, frames=2, noise_enabled=False)
    assert len(calls) == 6
    for value, _, _ in calls:
        np.testing.assert_array_equal(value, calls[0][0])
    for mode in result["modes"].values():
        assert mode["raw_frames"].dtype == np.int32
        assert mode["metadata"]["raw_reused"] is False
        assert (mode["metadata"]["analog_gain"] * mode["metadata"]["digital_gain"]
                == pytest.approx(8))
        assert mode["electron_frames"].mean() == pytest.approx(123.4, abs=1.3)
        rendered = core.process_electrons(mode["electron_frames"][0], np.eye(3),
                                         valid=mode["valid_frames"][0], display_white=1000)
        assert rendered["linear"][rendered["valid"]].mean() == pytest.approx(123.4, abs=1.3)
        json.dumps(mode["metadata"], allow_nan=False)


def test_gain_budget_separates_downstream_noise_and_preserves_input_read_noise():
    result = capture(frames=2)
    analog, digital = result["modes"]["analog_gain"], result["modes"]["digital_gain"]
    a, d = analog["metadata"]["noise_budget"], digital["metadata"]["noise_budget"]
    assert a["input_read_e2"] == d["input_read_e2"] == 100
    assert a["post_gain_read_e2"] == pytest.approx(16 / 64)
    assert d["post_gain_read_e2"] == pytest.approx(16)
    assert a["adc_quantization_e2"] == pytest.approx(d["adc_quantization_e2"] / 64)
    assert a["digital_quantization_e2"] == 0
    assert analog["metadata"]["post_gain_noise_v"] == pytest.approx(.0004)
    np.testing.assert_array_equal(analog["mean_total_charge_e"], digital["mean_total_charge_e"])


def test_adc_and_digital_clipping_are_separate_and_not_valid_measurements():
    result = capture(electrons=2000, noise_enabled=False, frames=1)
    analog, digital = result["modes"]["analog_gain"], result["modes"]["digital_gain"]
    assert not analog["well_saturated_frames"].any()
    assert analog["adc_saturated_frames"].all()
    assert not analog["digital_added_saturated_frames"].any()
    assert not digital["adc_saturated_frames"].any()
    assert digital["digital_saturated_frames"].all()
    assert digital["digital_added_saturated_frames"].all()
    assert not analog["valid_frames"].any() and not digital["valid_frames"].any()
    diagnostics = lab.mode_diagnostics(digital, chart="uniform")
    assert diagnostics["temporal_noise_e"] is None
    assert diagnostics["digital_added_saturation_fraction"] == 1
    assert diagnostics["noise_power_spectrum"]["status"] == "unavailable"


def test_unity_digital_gain_does_not_claim_adc_endpoint_rounding_as_added_clipping(monkeypatch):
    original = electrical._encode_delivered_charge

    def encode_with_boundary_read_noise(charge, model, analog, noise_enabled, rng):
        # A signed read-noise realization leaves continuous ADC code +0.2. The
        # ADC rounds to zero without its continuous clipping mask being true.
        adc = model["adc"]
        slope = model["conversion_gain_v_per_e"] * analog * (2**adc["bit_depth"] - 1)
        slope /= adc["full_scale_v"]
        target_charge = (0.2 - adc["black_level_dn"]) / slope
        read_draw = np.full(charge.shape, target_charge) - charge

        class FixedReadNoise:
            def normal(self, loc, scale, shape):
                assert loc == 0 and scale == model["read_noise_e"] and shape == charge.shape
                return read_draw.copy()

        return original(charge, model, analog, noise_enabled, FixedReadNoise())

    monkeypatch.setattr(electrical, "_encode_delivered_charge", encode_with_boundary_read_noise)
    result = capture(electrons=0, frames=1, settings={"gain_total": 1, "post_gain_noise_e": 0})
    for mode in result["modes"].values():
        assert mode["metadata"]["digital_gain"] == 1
        assert not mode["adc_saturated_frames"].any()
        assert (mode["adc_frames"] == 0).all()
        assert mode["digital_saturated_frames"].all()
        assert not mode["digital_added_saturated_frames"].any()
        assert lab.mode_diagnostics(mode)["digital_added_saturation_fraction"] == 0


def test_cds_equal_marginal_noise_exposes_column_covariance_and_correlation_benefit():
    result = capture("noise_structure", electrons=0, shape=(64, 64), frames=32)
    pixel = result["modes"]["pixel_iid"]
    column = result["modes"]["column_noise"]
    cds = result["modes"]["cds_correlated"]
    for mode in (pixel, column):
        residual = mode["cds_residual_noise_e"]
        assert np.mean(residual**2) == pytest.approx(4, rel=.06)
        assert mode["metadata"]["noise_budget"]["cds_residual_e2"] == pytest.approx(4)
        assert mode["metadata"]["electrical_model"]["read_noise_e"] == 0
    pr = pixel["cds_residual_noise_e"]
    cr = column["cds_residual_noise_e"]
    assert abs(np.mean(pr[:, 1:] * pr[:, :-1])) < .15
    assert np.mean(cr[:, 1:] * cr[:, :-1]) == pytest.approx(3, rel=.08)
    assert np.mean(cds["cds_residual_noise_e"]**2) == pytest.approx(.8, rel=.08)
    np.testing.assert_allclose(cds["cds_signal_sample_noise_e"] - cds["cds_reset_sample_noise_e"],
                               cds["cds_residual_noise_e"], atol=2e-15)
    profile_pixel = lab.mode_diagnostics(pixel, chart="dark")["column_temporal_rms_e"]
    profile_column = lab.mode_diagnostics(column, chart="dark")["column_temporal_rms_e"]
    assert np.mean(profile_column) > 3 * np.mean(profile_pixel)


def test_adc_resolution_preserves_voltage_pedestal_and_full_scale_headroom():
    result = capture("adc_headroom", electrons=123.4, noise_enabled=False, frames=1)
    original = electrical.default_electrical_model(readout())
    fraction = original["adc"]["black_level_dn"] / 4095
    budgets = []
    for mode in result["modes"].values():
        model = mode["metadata"]["electrical_model"]
        bits = model["adc"]["bit_depth"]
        assert model["adc"]["black_level_dn"] / (2**bits - 1) == pytest.approx(fraction)
        assert model["adc"]["full_scale_v"] == original["adc"]["full_scale_v"]
        assert mode["metadata"]["analog_gain"] == mode["metadata"]["digital_gain"] == 1
        budgets.append(mode["metadata"]["noise_budget"]["adc_quantization_e2"])
        assert mode["electron_frames"].mean() == pytest.approx(123.4, abs=5)
    assert budgets[0] > budgets[1] > budgets[2]


def test_ptc_uses_pixelwise_temporal_variance_per_site_and_refuses_invalid_roi():
    mode = capture("adc_headroom", frames=8, settings={"adc_bits": [12]})["modes"]["adc_12bit"]
    yy, xx = np.indices((32, 32))
    mode["electron_frames"] = (np.arange(8)[:, None, None] + yy[None] * 10 + xx[None]).astype(float)
    mode["valid_frames"][:] = True
    result = lab.mode_diagnostics(mode, chart="uniform")
    assert len(result["ptc"]["points"]) == 4
    for point in result["ptc"]["points"]:
        assert point["temporal_variance_e2"] == pytest.approx(np.var(np.arange(8), ddof=1))
    mode["valid_frames"][2, 8, 9] = False  # Gr support
    result = lab.mode_diagnostics(mode, chart="uniform")
    bad = next(point for point in result["ptc"]["points"] if point["site"] == "Gr")
    assert bad["status"] == "unavailable" and bad["mean_electrons"] is None
    assert result["ptc"]["status"] == "partial"
    assert lab.mode_diagnostics(mode, chart="texture")["ptc"]["status"] == "unavailable"


def test_nps_and_profiles_remove_scene_pattern_and_preserve_native_frequency_units():
    mode = capture("noise_structure", shape=(64, 64), frames=8)["modes"]["column_noise"]
    before = lab.mode_diagnostics(mode, chart="uniform")
    mode["electron_frames"] += np.random.default_rng(8).normal(0, 100, (64, 64))
    after = lab.mode_diagnostics(mode, chart="uniform")
    for key in ("row_temporal_rms_e", "column_temporal_rms_e"):
        np.testing.assert_allclose(before[key], after[key], rtol=1e-12, atol=1e-14)
    nps = before["noise_power_spectrum"]
    assert nps["status"] == "ok" and nps["radial_support"] == [0, .25]
    assert nps["frequency_cycles_per_pixel"][-1] == .25
    np.testing.assert_allclose(nps["radial_power"], after["noise_power_spectrum"]["radial_power"],
                               rtol=1e-12)
    assert nps["integrated_2d_nps_variance"] == pytest.approx(
        nps["window_weighted_temporal_variance"], rel=1e-13)


def test_source_arrays_settings_and_models_unchanged_and_runtime_bounds_explicit():
    rate = np.ones((32, 32))
    rate.setflags(write=False)
    source = readout()
    model = electrical.default_electrical_model(source)
    before = deepcopy(model)
    lab.readout_modes(rate, source, recipe="gain_budget", settings={}, frames=1,
                      electrical_model=model)
    assert model == before
    with pytest.raises(ValueError, match="25 C"):
        capture(temperature_c=30)
    with pytest.raises(ValueError, match="256x256"):
        capture(shape=(260, 260))
    with pytest.raises(ValueError, match="RGGB-even"):
        capture(geometry={"full_sensor_shape": [34, 34], "crop_origin": [1, 1]})
