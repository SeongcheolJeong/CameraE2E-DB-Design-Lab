"""Physical invariants independent of the inherited MATLAB formulas."""

import numpy as np
import pytest

from pyisetcam.sensor import (
    analog_to_digital,
    pixel_dr,
    sensor_add_noise,
    sensor_compute,
    sensor_compute_image,
    sensor_compute_noise,
    sensor_compute_noise_free,
    sensor_compute_samples,
    sensor_create,
    sensor_dr,
    sensor_get,
    sensor_set,
)
from pyisetcam.types import OpticalImage


@pytest.fixture
def sensor_input(asset_store):
    sensor = sensor_create(
        "monochrome",
        {"size": (8, 8), "wave": np.array([550.0])},
        asset_store=asset_store,
    )
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "integration time", 0.01)
    oi = OpticalImage(
        fields={"wave": np.array([550.0]), "width_m": 8 * 2.8e-6},
        data={"photons": np.full((8, 8, 1), 2e16)},
    )
    return sensor, oi


def test_calibrated_pixel_and_column_maps_affect_main_capture(sensor_input):
    sensor, oi = sensor_input
    sensor = sensor_set(sensor, "noise flag", 1)
    baseline = sensor_compute(sensor, oi, seed=7).data["volts"]
    sensor = sensor_set(sensor, "offset fpn image", np.full((8, 8), 0.1))
    sensor = sensor_set(sensor, "gain fpn image", np.full((8, 8), 1.2))
    sensor = sensor_set(sensor, "col offset fpn vector", np.linspace(0, 0.07, 8))
    sensor = sensor_set(sensor, "col gain fpn vector", np.linspace(0.9, 1.1, 8))
    actual = sensor_compute(sensor, oi, seed=7)
    expected = (baseline * 1.2 + 0.1) * np.linspace(0.9, 1.1, 8) + np.linspace(0, 0.07, 8)
    np.testing.assert_allclose(actual.data["volts"], expected)


def test_fpn_only_has_zero_temporal_variance(sensor_input):
    sensor, oi = sensor_input
    oi.data["photons"].fill(0)
    sensor = sensor_set(sensor, "pixel dark voltage", 0.0)
    sensor = sensor_set(sensor, "pixel read noise volts", 0.0)
    sensor = sensor_set(sensor, "dsnu level", 0.01)
    sensor = sensor_set(sensor, "column fpn", [0.01, 0.03])
    sensor = sensor_set(sensor, "analog offset", 0.1)
    noise_free = sensor_compute_noise_free(sensor, oi)
    frames = sensor_compute_samples(noise_free, 20, 2, seed=7)
    np.testing.assert_array_equal(frames, np.repeat(frames[:, :, :1], 20, axis=2))
    assert np.std(frames[:, :, 0]) > 0.005
    # Exposure bracketing also retains the same device realization.
    sensor = sensor_set(sensor, "integration time", [0.01, 0.02, 0.03])
    bracket = sensor_compute(sensor, oi, seed=11).data["volts"]
    np.testing.assert_array_equal(bracket, np.repeat(bracket[:, :, :1], 3, axis=2))


def test_temporal_seed_does_not_change_sensor_fixed_pattern(sensor_input):
    sensor, oi = sensor_input
    sensor = sensor_set(sensor, "dsnu level", 0.01)
    sensor = sensor_set(sensor, "prnu level", 2.0)
    sensor = sensor_set(sensor, "column fpn", [0.01, 0.02])
    first = sensor_compute(sensor, oi, seed=7)
    second = sensor_compute(sensor, oi, seed=8)
    replay = sensor_compute(sensor, oi, seed=7)
    assert not np.array_equal(first.data["volts"], second.data["volts"])
    np.testing.assert_array_equal(first.data["volts"], replay.data["volts"])
    for key in ("offset_fpn_image", "gain_fpn_image", "column_offset_fpn", "column_gain_fpn"):
        np.testing.assert_array_equal(first.fields[key], second.fields[key])
    changed_device = sensor_set(first.clone(), "fixed pattern seed", 2)
    changed_device = sensor_compute(changed_device, oi, seed=7)
    assert not np.array_equal(
        first.fields["offset_fpn_image"], changed_device.fields["offset_fpn_image"]
    )
    # Computing captures does not mutate the original input sensor.
    assert "offset_fpn_image" not in sensor.fields


@pytest.mark.parametrize("noise_flag", [-2, -1, 0, 1, 2])
@pytest.mark.parametrize("exposure", [0.01, [0.01, 0.02], [[0.01, 0.02], [0.03, 0.04]]])
@pytest.mark.parametrize("response", ["linear", "log"])
def test_main_and_staged_capture_agree(sensor_input, noise_flag, exposure, response):
    sensor, oi = sensor_input
    sensor = sensor_set(sensor, "pattern", np.ones((2, 2), dtype=int))
    sensor = sensor_set(sensor, "integration time", exposure)
    sensor = sensor_set(sensor, "noise flag", noise_flag)
    sensor = sensor_set(sensor, "response type", response)
    sensor = sensor_set(sensor, "analog gain", 2.0)
    sensor = sensor_set(sensor, "analog offset", 0.03)
    sensor = sensor_set(sensor, "dsnu level", 0.01)
    sensor = sensor_set(sensor, "prnu level", 2.0)
    sensor = sensor_set(sensor, "column fpn", [0.005, 0.01])
    sensor = sensor_set(sensor, "quantization method", "linear")
    direct = sensor_compute(sensor, oi, seed=3)
    staged = sensor_compute_noise(sensor_compute_noise_free(sensor, oi), seed=3)
    np.testing.assert_allclose(direct.data["volts"], staged.data["volts"], rtol=0, atol=1e-14)
    np.testing.assert_array_equal(direct.data["dv"], staged.data["dv"])


def test_zero_duration_dark_frame_retains_read_noise(sensor_input):
    sensor, _ = sensor_input
    sensor = sensor_set(sensor, "volts", np.zeros((8, 8)))
    sensor = sensor_set(sensor, "integration time", 0.0)
    sensor = sensor_set(sensor, "pixel read noise volts", 0.01)
    actual = sensor_add_noise(sensor, seed=3).data["volts"]
    assert np.std(actual) > 0.005
    assert np.min(actual) < 0 < np.max(actual)


def test_log_image_wrapper_applies_response_once(sensor_input):
    sensor, oi = sensor_input
    sensor = sensor_set(sensor, "response type", "log")
    sensor = sensor_set(sensor, "analog gain", 2.0)
    sensor = sensor_set(sensor, "dsnu level", 0.01)
    image, _, _ = sensor_compute_image(oi, sensor, seed=3)
    direct = sensor_compute(sensor, oi, seed=3)
    np.testing.assert_allclose(np.clip(image, 0.0, 1.0), direct.data["volts"])


@pytest.mark.parametrize("bits", [1, 4, 10, 12])
def test_adc_has_exactly_unsigned_bit_range(sensor_input, bits):
    sensor, oi = sensor_input
    sensor = sensor_set(sensor, "nbits", bits)
    sensor = sensor_set(sensor, "quantization method", "linear")
    sensor = sensor_set(sensor, "volts", np.array([[-0.1, 0.0, 0.5, 1.0, 1.1]]))
    digital, _ = analog_to_digital(sensor)
    assert np.min(digital) == 0
    assert np.max(digital) == 2**bits - 1
    assert sensor_get(sensor, "max digital value") == 2**bits - 1
    np.testing.assert_array_equal(digital[:, :2], [[0, 0]])
    np.testing.assert_array_equal(digital[:, -2:], [[2**bits - 1, 2**bits - 1]])


def test_analog_recompute_removes_previous_digital_values(sensor_input):
    sensor, oi = sensor_input
    sensor = sensor_set(sensor, "quantization method", "linear")
    digital = sensor_compute(sensor, oi)
    assert digital.data.get("dv") is not None
    analog = sensor_set(digital, "quantization method", "analog")
    analog = sensor_compute(analog, oi)
    assert analog.data.get("dv") is None
    np.testing.assert_array_equal(sensor_get(analog, "dv or volts"), analog.data["volts"])


def test_dynamic_range_uses_voltage_noise_variance_and_20log(sensor_input):
    sensor, _ = sensor_input
    sensor = sensor_set(sensor, "pixel voltage swing", 1.0)
    sensor = sensor_set(sensor, "pixel conversion gain", 1e-4)
    sensor = sensor_set(sensor, "pixel read noise volts", 1e-3)
    sensor = sensor_set(sensor, "integration time", 1.0)
    sensor = sensor_set(sensor, "pixel dark voltage", 0.0)
    assert sensor_dr(sensor) == pytest.approx(60.0)
    assert pixel_dr(sensor) == pytest.approx(60.0)
    sensor = sensor_set(sensor, "pixel dark voltage", 0.001)
    # Ten mean dark electrons add 10 e-² variance to 100 e-² read variance.
    expected = 20 * np.log10(9990 / np.sqrt(110))
    assert sensor_dr(sensor) == pytest.approx(expected)
    assert pixel_dr(sensor) == pytest.approx(expected)
