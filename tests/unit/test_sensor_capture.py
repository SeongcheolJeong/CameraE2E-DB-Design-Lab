"""Physical output-contract checks for the explicit CameraE2E RAW path."""

import numpy as np
import pytest

from pyisetcam.assets import ie_read_spectra
from pyisetcam.ip import _sensor_space_from_data, demosaic, ip_compute, ip_create
from pyisetcam.sensor import sensor_compute, sensor_create, sensor_get, sensor_set
from pyisetcam.sensor_capture import (
    configure_e2e_sensor,
    normalized_sensor_dn,
    resample_sensor_capture,
)
from pyisetcam.types import OpticalImage
from pyisetcam.utils import energy_to_quanta, tile_pattern


@pytest.fixture
def optical_input(asset_store):
    wave = np.arange(400.0, 701.0, 10.0)
    sensor = sensor_create("default", {"size": (8, 8), "wave": wave}, asset_store=asset_store)
    sensor = sensor_set(sensor, "integration time", 0.01)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", 0)
    photons = np.linspace(1e13, 1e14, 64).reshape(8, 8, 1) * np.ones((1, 1, wave.size))
    oi = OpticalImage(
        fields={"wave": wave, "width_m": 8 * 2.8e-6},
        data={"photons": photons},
    )
    return sensor, oi


def cached_sensor(asset_store, mean=1000.0, *, size=(8, 8), noise_flag=0, **readout):
    sensor = sensor_create("default", {"size": size}, asset_store=asset_store)
    sensor = sensor_set(sensor, "integration time", 0.01)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", noise_flag)
    sensor = configure_e2e_sensor(sensor, readout={"dark_current_e_per_s": 0.0, **readout})
    sensor.data["mean_electrons"] = np.full(size, mean)
    return sensor


def test_gain_is_physical_and_optical_mean_is_independent(optical_input):
    sensor, oi = optical_input
    one = sensor_compute(configure_e2e_sensor(sensor, readout={"gain": 1.0}), oi)
    two = sensor_compute(configure_e2e_sensor(sensor, readout={"gain": 2.0}), oi)
    np.testing.assert_array_equal(one.data["mean_electrons"], two.data["mean_electrons"])
    np.testing.assert_allclose(two.data["volts"], 2 * one.data["volts"])
    np.testing.assert_allclose(two.data["reference_dn"] - 64, 2 * (one.data["reference_dn"] - 64))
    np.testing.assert_allclose(two.data["raw_dn"] - 64, 2 * (one.data["raw_dn"] - 64), atol=1)
    assert sensor_get(two, "analog gain") == 0.5
    assert sensor_get(two, "analog offset") == 0.0


def test_mean_electrons_does_not_use_inverse_getter(optical_input, monkeypatch):
    import pyisetcam.sensor as sensor_module

    original_get = sensor_module.sensor_get

    def forbid_inverse(sensor, parameter, *args):
        assert parameter != "electrons"
        return original_get(sensor, parameter, *args)

    monkeypatch.setattr(sensor_module, "sensor_get", forbid_inverse)
    sensor, oi = optical_input
    sensor = sensor_set(sensor, "analog gain", 0.5)
    sensor = sensor_set(sensor, "analog offset", 0.01)
    captured = sensor_compute(configure_e2e_sensor(sensor), oi)
    assert captured.data["mean_electrons"].mean() > 0
    assert captured.fields["sensor_readout"]["gain"] == 2.0


def test_reconfiguration_preserves_settings_and_resampling_uses_cached_mean(
    asset_store, monkeypatch
):
    import pyisetcam.sensor as sensor_module

    def forbid_optics(*args, **kwargs):
        raise AssertionError("Resampling must not repeat the optical calculation.")

    monkeypatch.setattr(sensor_module, "sensor_compute_noise_free", forbid_optics)
    base = cached_sensor(asset_store, full_well_e=20000, black_level_dn=32)
    first = resample_sensor_capture(base, seed=1)
    updated = configure_e2e_sensor(first, readout={"gain": 2})
    assert updated.fields["sensor_readout"]["full_well_e"] == 20000
    assert updated.fields["sensor_readout"]["black_level_dn"] == 32
    np.testing.assert_array_equal(updated.data["mean_electrons"], first.data["mean_electrons"])
    second = resample_sensor_capture(updated, seed=2)
    np.testing.assert_allclose(
        second.data["reference_dn"] - 32, 2 * (first.data["reference_dn"] - 32)
    )


def test_quantization_reaches_isp_and_diagnostic_volts_cannot_bypass_it(asset_store):
    sensor = cached_sensor(asset_store, mean=100, bit_depth=2, black_level_dn=0)
    low = resample_sensor_capture(sensor, seed=1)
    high = resample_sensor_capture(configure_e2e_sensor(low, readout={"bit_depth": 12}), seed=1)
    np.testing.assert_array_equal(low.data["volts"], high.data["volts"])
    ip = ip_create(asset_store=asset_store)
    low_ip = ip_compute(ip, low, asset_store=asset_store)
    high_ip = ip_compute(ip, high, asset_store=asset_store)
    assert not np.any(low_ip.data["result"])
    assert np.max(high_ip.data["result"]) > 0
    high.data["volts"] = np.full((8, 8), 1000.0)
    high.data["dv"] = np.zeros((8, 8))
    changed = ip_compute(ip, high, asset_store=asset_store)
    np.testing.assert_array_equal(changed.data["result"], high_ip.data["result"])
    np.testing.assert_array_equal(demosaic(low_ip, high), high_ip.data["sensorspace"])


@pytest.mark.parametrize("render_scale", [False, True])
def test_isp_preserves_gain_and_exposure_brightness(asset_store, render_scale):
    base = cached_sensor(asset_store, mean=500, bit_depth=20)
    one = resample_sensor_capture(base, seed=1)
    gain_two = resample_sensor_capture(configure_e2e_sensor(one, readout={"gain": 2}), seed=1)
    exposure_two = base.clone()
    exposure_two.data["mean_electrons"] *= 2
    exposure_two.fields["integration_time"] *= 2
    exposure_two = resample_sensor_capture(exposure_two, seed=1)
    ip = ip_create(asset_store=asset_store)
    ip.fields["render"]["scale"] = render_scale
    first_ip = ip_compute(ip, one, asset_store=asset_store)
    assert np.max(first_ip.data["result"]) > 0.01
    for captured in (gain_two, exposure_two):
        second_ip = ip_compute(ip, captured, asset_store=asset_store)
        np.testing.assert_allclose(second_ip.data["xyz"], 2 * first_ip.data["xyz"], rtol=3e-5)
        np.testing.assert_allclose(second_ip.data["result"], 2 * first_ip.data["result"], rtol=3e-5)
    assert first_ip.metadata["sensor_input"]["black_subtractions"] == 1
    assert first_ip.metadata["sensor_input"]["frame_max_normalization"] is False


@pytest.mark.parametrize("fraction", [0.1, 0.5, 0.9])
def test_fixed_d65_reference_gives_meaningful_linear_white_level(asset_store, fraction):
    sensor = cached_sensor(asset_store, mean=0, bit_depth=24)
    wave = np.asarray(sensor.fields["wave"])
    d65 = energy_to_quanta(ie_read_spectra("D65", wave, asset_store=asset_store), wave)
    channel_charge = np.asarray(d65).reshape(-1) @ sensor_get(sensor, "spectral qe")
    channel_charge = channel_charge / np.max(channel_charge) * 10000 * fraction
    pattern = tile_pattern(sensor.fields["pattern"], 8, 8)
    sensor.data["mean_electrons"] = channel_charge[pattern - 1]
    captured = resample_sensor_capture(sensor, seed=1)
    result = ip_compute(ip_create(asset_store=asset_store), captured, asset_store=asset_store)
    np.testing.assert_allclose(result.data["xyz"][2:-2, 2:-2, 1], fraction, atol=2e-7)
    assert np.max(result.data["result"][2:-2, 2:-2]) == pytest.approx(fraction, abs=2e-7)
    reference = result.metadata["sensor_input"]["fixed_reference"]
    assert reference["illuminant"] == "D65"
    assert reference["input_dependent"] is False
    # The public matrices include calibration; cached linear processing is exact.
    xyz = np.maximum(result.data["sensorspace"] @ result.data["transforms"][0], 0)
    rgb = np.maximum(xyz @ result.data["transforms"][2], 0)
    np.testing.assert_array_equal(xyz, result.data["xyz"])
    np.testing.assert_array_equal(rgb, result.data["result"])


def test_quad_cfa_preserves_uniform_channel_levels_everywhere(asset_store):
    sensor = cached_sensor(asset_store, mean=0, size=(24, 24), bit_depth=24)
    quad = np.array([[1, 1, 2, 2], [1, 1, 2, 2], [2, 2, 3, 3], [2, 2, 3, 3]])
    sensor = sensor_set(sensor, "pattern", quad)
    tiled = tile_pattern(quad, 24, 24)
    sensor.data["mean_electrons"] = np.array([2000.0, 4000.0, 6000.0])[tiled - 1]
    captured = resample_sensor_capture(sensor, seed=1)
    raw = normalized_sensor_dn(captured)
    planes = _sensor_space_from_data(captured, raw)
    for channel in range(3):
        actual_level = raw[tiled == channel + 1][0]
        np.testing.assert_allclose(planes[:, :, channel], actual_level, atol=1e-15)
    result = ip_compute(ip_create(asset_store=asset_store), captured, asset_store=asset_store)
    np.testing.assert_array_equal(result.data["sensorspace"], planes)
    metadata = result.metadata["sensor_input"]["demosaic"]
    assert metadata["applied"] == "normalized masked triangular"
    assert metadata["cfa_period"] == [4, 4]
    assert metadata["measured_samples_preserved"] is True


def test_quad_interpolation_preserves_measured_edge_samples_and_bounds(asset_store):
    sensor = cached_sensor(asset_store, mean=0, size=(24, 24))
    quad = np.array([[1, 1, 2, 2], [1, 1, 2, 2], [2, 2, 3, 3], [2, 2, 3, 3]])
    sensor = sensor_set(sensor, "pattern", quad)
    tiled = tile_pattern(quad, 24, 24)
    # A real vertical intensity edge has a distinct contrast in each channel.
    low = np.array([0.1, 0.2, 0.3])
    high = np.array([0.8, 0.7, 0.6])
    raw = np.where(np.arange(24)[None, :] < 12, low[tiled - 1], high[tiled - 1])
    planes = _sensor_space_from_data(sensor, raw)
    for channel in range(3):
        mask = tiled == channel + 1
        np.testing.assert_array_equal(planes[:, :, channel][mask], raw[mask])
        np.testing.assert_allclose(planes[:, :8, channel], low[channel], atol=1e-15)
        np.testing.assert_allclose(planes[:, 16:, channel], high[channel], atol=1e-15)
        assert np.min(planes[:, :, channel]) >= low[channel] - 1e-15
        assert np.max(planes[:, :, channel]) <= high[channel] + 1e-15
    # Reusing the Bayer kernel previously caused periodic overshoot even on flats.
    assert np.any((planes[:, 10:14, 0] > low[0]) & (planes[:, 10:14, 0] < high[0]))


def test_standard_bayer_demosaic_is_unchanged_by_digital_mode(asset_store):
    sensor = cached_sensor(asset_store, size=(24, 24))
    values = np.arange(24 * 24, dtype=float).reshape(24, 24) / (24 * 24)
    digital = _sensor_space_from_data(sensor, values)
    legacy = configure_e2e_sensor(sensor, mode="legacy")
    np.testing.assert_array_equal(digital, _sensor_space_from_data(legacy, values))


@pytest.mark.parametrize("black", [0, 64, 512])
def test_black_pedestal_is_applied_and_subtracted_once(asset_store, black):
    sensor = cached_sensor(asset_store, mean=0, black_level_dn=black)
    dark = resample_sensor_capture(sensor, seed=1)
    np.testing.assert_array_equal(dark.data["raw_dn"], np.full((8, 8), black))
    assert not np.any(normalized_sensor_dn(dark))
    ip = ip_compute(ip_create(sensor=dark, asset_store=asset_store), dark, asset_store=asset_store)
    assert not np.any(ip.data["result"])
    sensor.data["mean_electrons"].fill(1000)
    lit = resample_sensor_capture(sensor, seed=1)
    np.testing.assert_allclose(normalized_sensor_dn(lit), 0.1, atol=0.5 / (4095 - black))


def test_signed_read_noise_survives_around_black(asset_store):
    sensor = cached_sensor(
        asset_store, mean=0, size=(512, 512), noise_flag=2,
        read_noise_e=10, black_level_dn=4096, bit_depth=16,
    )
    actual = resample_sensor_capture(sensor, seed=17)
    centered = (actual.data["raw_dn"] - 4096) / actual.metadata["sensor_capture"]["dn_per_electron"]
    assert np.min(centered) < 0 < np.max(centered)
    assert abs(np.mean(centered)) < 0.1
    assert np.var(centered) == pytest.approx(100, rel=0.02)
    assert np.min(actual.data["volts"]) < 0
    assert not np.any(actual.data["mean_electrons"])


@pytest.mark.parametrize("photo, dark, exposure", [(1000, 0, 0.01), (0, 10, 2.0)])
def test_poisson_photo_and_dark_variance(asset_store, photo, dark, exposure):
    sensor = cached_sensor(
        asset_store, mean=photo, size=(512, 512), noise_flag=2,
        read_noise_e=0, dark_current_e_per_s=dark, bit_depth=20,
    )
    sensor.fields["integration_time"] = exposure
    actual = resample_sensor_capture(sensor, seed=23)
    electrons = (actual.data["raw_dn"] - 64) / actual.metadata["sensor_capture"]["dn_per_electron"]
    expected_mean = photo + dark * exposure
    assert np.mean(electrons) == pytest.approx(expected_mean, rel=0.01)
    assert np.var(electrons) == pytest.approx(expected_mean, rel=0.02)
    np.testing.assert_array_equal(actual.data["mean_electrons"], np.full((512, 512), photo))


def test_device_patterns_persist_and_temporal_draws_change(asset_store):
    sensor = cached_sensor(asset_store, noise_flag=2, bit_depth=20)
    sensor = sensor_set(sensor, "dsnu level", 0.001)
    sensor = sensor_set(sensor, "prnu level", 1.0)
    sensor = sensor_set(sensor, "column fpn", [0.001, 0.01])
    one = resample_sensor_capture(sensor, seed=1)
    two = resample_sensor_capture(one, seed=2)
    replay = resample_sensor_capture(one, seed=1)
    keys = ("gain_fpn_image", "offset_fpn_image", "column_offset_fpn", "column_gain_fpn")
    for key in keys:
        np.testing.assert_array_equal(one.fields[key], two.fields[key])
    np.testing.assert_array_equal(one.data["reference_dn"], two.data["reference_dn"])
    np.testing.assert_array_equal(one.data["raw_dn"], replay.data["raw_dn"])
    assert not np.array_equal(one.data["raw_dn"], two.data["raw_dn"])


@pytest.mark.parametrize("noise_flag", [1, 2])
def test_dark_fixed_bias_retains_both_signs_and_is_exposure_independent(asset_store, noise_flag):
    sensor = cached_sensor(
        asset_store, mean=0, size=(128, 128), noise_flag=noise_flag,
        read_noise_e=0, black_level_dn=65536, bit_depth=20,
    )
    conversion_gain = sensor.fields["pixel"]["conversion_gain_v_per_electron"]
    sensor.fields["pixel"]["dsnu_sigma_v"] = 4 * conversion_gain
    sensor.fields["column_fpn"] = np.array([2 * conversion_gain, 0.0])
    one = resample_sensor_capture(sensor, seed=11)
    expected_e = (
        one.fields["offset_fpn_image"] + one.fields["column_offset_fpn"]
    ) / conversion_gain
    scale = one.metadata["sensor_capture"]["dn_per_electron"]
    np.testing.assert_allclose(one.data["reference_dn"], 65536 + scale * expected_e)
    np.testing.assert_allclose(one.data["volts"], conversion_gain * expected_e)
    actual_e = (one.data["raw_dn"].astype(float) - 65536) / scale
    assert np.min(actual_e) < -8 and np.max(actual_e) > 8
    assert abs(actual_e.mean()) < .5
    np.testing.assert_allclose(actual_e, expected_e, atol=.5 / scale)
    assert one.metadata["sensor_capture"]["saturation"]["well_fraction"] == 0
    assert "fixed_readout_bias" in one.metadata["sensor_capture"]["dsnu_definition"]
    # The fixed offset is per capture, not a dark-current term multiplied by t.
    changed = one.clone()
    changed.fields["integration_time"] = 2.0
    two = resample_sensor_capture(changed, seed=91)
    np.testing.assert_array_equal(one.data["raw_dn"], two.data["raw_dn"])


def test_fixed_bias_follows_full_well_without_changing_physical_saturation(asset_store):
    sensor = cached_sensor(
        asset_store, mean=20000, noise_flag=2, read_noise_e=0,
        full_well_e=10000, bit_depth=20, black_level_dn=65536,
    )
    conversion_gain = sensor.fields["pixel"]["conversion_gain_v_per_electron"]
    bias = np.tile([-100.0, 100.0], (8, 4))
    sensor.fields["offset_fpn_image"] = bias * conversion_gain
    sensor.fields["gain_fpn_image"] = np.full((8, 8), 2.0)
    actual = resample_sensor_capture(sensor, seed=17)
    np.testing.assert_allclose(actual.data["volts"] / conversion_gain, 10000 + bias)
    saturation = actual.metadata["sensor_capture"]["saturation"]
    assert saturation["well_fraction"] == 1
    assert saturation["adc_white_fraction"] == .5
    # Negative readout offsets remain observable even when the physical well
    # has already saturated; positive offsets clip only at the ADC afterward.
    assert np.all(actual.data["raw_dn"][:, ::2] < 2**20 - 1)
    assert np.all(actual.data["raw_dn"][:, 1::2] == 2**20 - 1)
    dark = sensor.clone()
    dark.data["mean_electrons"].fill(0)
    dark.fields["offset_fpn_image"] = np.full((8, 8), 20000 * conversion_gain)
    clipped_adc = resample_sensor_capture(dark, seed=17)
    assert clipped_adc.metadata["sensor_capture"]["saturation"]["well_fraction"] == 0
    assert clipped_adc.metadata["sensor_capture"]["saturation"]["adc_white_fraction"] == 1


def test_only_configured_patterns_are_present_and_calibrated_maps_are_honored(asset_store):
    sensor = cached_sensor(asset_store, mean=0, noise_flag=2, read_noise_e=0, bit_depth=20)
    one = resample_sensor_capture(sensor, seed=1)
    assert not any("fpn" in key and "image" in key for key in one.fields)
    assert "column_offset_fpn" not in one.fields
    sensor = sensor_set(sensor, "offset fpn image", np.full((8, 8), 0.01))
    sensor = sensor_set(sensor, "gain fpn image", np.full((8, 8), 1.2))
    sensor = sensor_set(sensor, "col offset fpn vector", np.full(8, 0.005))
    sensor = sensor_set(sensor, "col gain fpn vector", np.full(8, 1.1))
    one = resample_sensor_capture(sensor, seed=1)
    two = resample_sensor_capture(one, seed=2)
    # At zero photons and read noise, DSNU is stable: (100 e * 1.1) + 50 e.
    np.testing.assert_allclose(one.data["volts"], 0.016)
    np.testing.assert_array_equal(one.data["raw_dn"], two.data["raw_dn"])


def test_full_well_precedes_read_noise_and_adc_clipping(asset_store):
    sensor = cached_sensor(
        asset_store, mean=20000, size=(256, 256), noise_flag=2,
        full_well_e=10000, read_noise_e=100, bit_depth=16,
    )
    actual = resample_sensor_capture(sensor, seed=2)
    stats = actual.metadata["sensor_capture"]["saturation"]
    assert stats["well_fraction"] == 1
    assert stats["adc_white_fraction"] == pytest.approx(0.5, abs=0.01)
    assert np.max(actual.data["raw_dn"]) == 65535
    assert np.min(actual.data["raw_dn"]) >= 0
    assert np.any(actual.data["volts"] > 1.0)
    np.testing.assert_array_equal(actual.data["reference_dn"], np.full((256, 256), 65535))


def test_zero_exposure_is_an_actual_dark_capture(optical_input):
    sensor, oi = optical_input
    sensor = sensor_set(sensor, "integration time", 0)
    actual = sensor_compute(configure_e2e_sensor(sensor), oi)
    assert not np.any(actual.data["mean_electrons"])
    assert actual.fields["integration_time"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"response_type": "log"},
        {"integration_time": [0.01, 0.02]},
        {"integration_time": [[0.01, 0.02], [0.03, 0.04]]},
        {"mosaic": False},
        {"sensor_compute_method": "binning"},
    ],
)
def test_unsupported_captures_require_explicit_legacy_mode(optical_input, changes):
    sensor, _ = optical_input
    sensor.fields.update(changes)
    with pytest.raises(ValueError, match="legacy|mosaic"):
        configure_e2e_sensor(sensor)
    assert configure_e2e_sensor(sensor, mode="legacy").fields["sensor_output_mode"] == "legacy"


@pytest.mark.parametrize(
    "readout",
    [
        {"gain": 0}, {"read_noise_e": -1}, {"dark_current_e_per_s": np.inf},
        {"bit_depth": 1.5}, {"bit_depth": 0}, {"bit_depth": 25},
        {"black_level_dn": 4095}, {"effective_efficiency": 0.75},
    ],
)
def test_invalid_readout_is_rejected(optical_input, readout):
    with pytest.raises(ValueError):
        configure_e2e_sensor(optical_input[0], readout=readout)


def test_unmarked_sensor_keeps_legacy_output(optical_input):
    sensor, oi = optical_input
    legacy = sensor_compute(sensor, oi)
    explicit = sensor_compute(configure_e2e_sensor(sensor, mode="legacy"), oi)
    assert "raw_dn" not in legacy.data
    np.testing.assert_array_equal(explicit.data["volts"], legacy.data["volts"])
