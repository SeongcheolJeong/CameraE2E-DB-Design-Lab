"""Electrical transfer ordering, physical-stage limits and noise statistics."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform.electrical import (
    capture_electrons,
    default_electrical_model,
    validate_electrical_model,
)


@pytest.fixture
def model():
    return default_electrical_model(
        {
            "full_well_e": 10000,
            "read_noise_e": 4,
            "dark_current_e_per_s": 10,
            "gain": 1,
            "bit_depth": 12,
            "black_level_dn": 64,
        }
    )


def capture(
    model,
    photo=1000,
    *,
    shape=(16, 16),
    exposure=0.01,
    gain=1,
    temperature=25,
    noise=False,
    seed=17,
    geometry=None,
):
    mean = np.full(shape, photo, dtype=float) if np.isscalar(photo) else photo
    return capture_electrons(
        mean,
        exposure,
        model,
        analog_gain=gain,
        temperature_c=temperature,
        geometry=geometry or {"full_sensor_shape": list(mean.shape), "crop_origin": [0, 0]},
        noise_enabled=noise,
        seed=seed,
    )


@pytest.mark.parametrize("gain", [0.5, 1, 2, 8])
def test_default_model_retains_legacy_transfer_without_coupling_new_parameters(model, gain):
    photo = np.array([[0, 5, 250, 1000], [2000, 9000, 10000, 20000]], dtype=float)
    result = capture(model, photo, exposure=0.5, gain=gain)
    white, black, full_well = 4095, 64, 10000
    expected = np.clip(
        np.rint(black + np.minimum(photo + 5, full_well) * (white - black) * gain / full_well),
        0,
        white,
    )
    np.testing.assert_array_equal(result["arrays"]["raw_dn"], expected.astype(np.int32))
    assert result["calibration"]["dn_per_e"] == pytest.approx((white - black) * gain / full_well)
    assert result["summary"]["physical_validation"] == "not_established"


def test_analog_gain_changes_codes_but_not_incident_or_total_photocharge(model):
    photo = np.full((8, 8), 501.0)
    original = photo.copy()
    one, two = capture(model, photo), capture(model, photo, gain=2)
    np.testing.assert_array_equal(photo, original)
    for name in ("mean_total_charge_e", "photo_response_map", "dark_current_e_per_s_map"):
        np.testing.assert_array_equal(one["arrays"][name], two["arrays"][name])
    assert two["calibration"]["dn_per_e"] == 2 * one["calibration"]["dn_per_e"]
    assert np.all(two["arrays"]["raw_dn"] > one["arrays"]["raw_dn"])


def test_physical_full_well_and_adc_ceiling_are_independent(model):
    model.update(full_well_e=100, conversion_gain_v_per_e=0.01, dark_current_e_per_s=0)
    model["adc"].update(bit_depth=10, black_level_dn=0, full_scale_v=10)
    well_limited = capture(model, 200)
    assert well_limited["arrays"]["well_saturated_mask"].all()
    assert not well_limited["arrays"]["adc_high_mask"].any()
    assert well_limited["arrays"]["raw_dn"][0, 0] == 102
    well_calibration = well_limited["calibration"]
    assert well_calibration["linear_ceiling_dn"] == pytest.approx(102.3)

    model["full_well_e"] = 1000
    model["adc"]["full_scale_v"] = 1
    adc_limited = capture(model, 200)
    assert not adc_limited["arrays"]["well_saturated_mask"].any()
    assert adc_limited["arrays"]["adc_high_mask"].all()
    assert adc_limited["arrays"]["raw_dn"][0, 0] == 1023
    assert adc_limited["calibration"]["linear_ceiling_dn"] == 1023
    # Changing capacity alone does not change an unsaturated electron-to-DN gain.
    low_before = capture(model, 25)
    model["full_well_e"] = 5000
    low_after = capture(model, 25)
    np.testing.assert_array_equal(low_before["arrays"]["raw_dn"], low_after["arrays"]["raw_dn"])


def test_digital_gain_is_after_adc_quantization_and_keeps_black_pedestal(model):
    model.update(conversion_gain_v_per_e=1, dark_current_e_per_s=0, digital_gain=2)
    model["adc"].update(bit_depth=8, full_scale_v=255, black_level_dn=10)
    result = capture(model, 1.49)
    assert result["arrays"]["adc_dn"][0, 0] == 11
    assert result["arrays"]["raw_dn"][0, 0] == 12  # pre-ADC gain would have yielded 13
    dark = capture(model, 0)
    assert dark["arrays"]["raw_dn"][0, 0] == 10
    clipped = capture(model, 150)
    assert not clipped["arrays"]["adc_high_mask"].any()
    assert clipped["arrays"]["digital_high_mask"].all()
    assert clipped["arrays"]["raw_dn"][0, 0] == 255


def test_adc_rounding_is_nearest_even(model):
    model.update(conversion_gain_v_per_e=1, dark_current_e_per_s=0)
    model["adc"].update(bit_depth=8, full_scale_v=255, black_level_dn=0)
    result = capture(model, np.array([[0.5, 1.5, 2.5, 3.5]]))
    np.testing.assert_array_equal(result["arrays"]["adc_dn"], [[0, 2, 2, 4]])


@pytest.mark.parametrize("digital_gain", [0.01, 0.5, 1, 2])
def test_raw_calibration_exposes_hidden_adc_floor_after_digital_gain(model, digital_gain):
    model.update(
        conversion_gain_v_per_e=1,
        read_noise_e=100,
        dark_current_e_per_s=0,
        digital_gain=digital_gain,
    )
    model["adc"]["full_scale_v"] = 4095  # ADC slope 1 DN/e before digital gain
    result = capture(model, 0, shape=(32, 32), noise=True, seed=1)
    arrays, calibration = result["arrays"], result["calibration"]
    clipped = arrays["adc_low_mask"]
    assert clipped.sum() > 200  # Actual censored acquisition, not a fabricated RAW fixture.
    floor = calibration["linear_floor_dn"]
    assert 0 <= floor <= calibration["black_level_dn"] < calibration["linear_ceiling_dn"]
    assert np.all(arrays["adc_dn"][clipped] == 0)
    assert np.all(arrays["raw_dn"][clipped] == np.rint(floor))
    guard = max(1, 3 * np.sqrt(calibration["read_noise_variance_e2"]) * calibration["dn_per_e"])
    assert np.all(arrays["raw_dn"][clipped] <= floor + guard)
    if digital_gain == 0.01:
        assert floor == pytest.approx(63.36)
        assert np.all(arrays["raw_dn"][clipped] > guard)  # Zero-based HDR guard missed these.
    other = capture(model, 200, shape=(32, 32), noise=True, seed=22)
    assert other["calibration"]["linear_floor_dn"] == floor  # Model constant, not oracle signal.


def test_native_fixed_patterns_persist_across_exposures_noise_seeds_and_crops(model):
    model["fixed_pattern"].update(prnu_sigma=0.1, dsnu_e_per_s=2, seed=53)
    original_model = deepcopy(model)
    full = capture(model, shape=(20, 24), exposure=0.01, seed=1)
    region = (slice(3, 11), slice(5, 15))
    cropped = capture(
        model,
        shape=(8, 10),
        exposure=0.02,
        seed=999,
        geometry={"full_sensor_shape": [20, 24], "crop_origin": [3, 5]},
    )
    for name in ("photo_response_map", "dark_current_e_per_s_map"):
        np.testing.assert_array_equal(full["arrays"][name][region], cropped["arrays"][name])
    assert np.std(cropped["arrays"]["photo_response_map"]) > 0
    np.testing.assert_array_equal(
        cropped["calibration"]["dark_e"], 2 * full["calibration"]["dark_e"][region]
    )
    assert model == original_model
    repeat = capture(model, shape=(20, 24), exposure=0.01, seed=999)
    np.testing.assert_array_equal(full["arrays"]["raw_dn"], repeat["arrays"]["raw_dn"])


def test_prnu_is_positive_unit_mean_lognormal_and_precedes_poisson(model):
    model.update(dark_current_e_per_s=0, read_noise_e=0)
    model["fixed_pattern"].update(prnu_sigma=0.2, seed=71)
    result = capture(model, 100, shape=(512, 512), noise=True)
    response = result["arrays"]["photo_response_map"]
    assert response.min() > 0
    assert response.mean() == pytest.approx(1, abs=0.002)
    assert response.std() == pytest.approx(0.2, rel=0.01)
    np.testing.assert_array_equal(result["arrays"]["mean_total_charge_e"], 100 * response)
    # Shot variance follows the spatially modified mean, not PRNU squared times it.
    reconstructed = (result["arrays"]["raw_dn"] - 64) / result["calibration"]["dn_per_e"]
    residual = reconstructed - result["arrays"]["mean_total_charge_e"]
    assert np.mean(residual**2) == pytest.approx(100, rel=0.03)


def test_dsnu_is_dark_current_rate_with_reported_negative_clipping_and_temperature(model):
    model.update(dark_current_e_per_s=1, dark_current_doubling_c=5)
    model["fixed_pattern"].update(dsnu_e_per_s=5, seed=31)
    normal = capture(model, 0, shape=(128, 128), exposure=0.5)
    hot = capture(model, 0, shape=(128, 128), exposure=1, temperature=35)
    assert normal["summary"]["negative_dark_current_clipped_fraction_full_sensor"] > 0.3
    assert normal["arrays"]["dark_current_e_per_s_map"].min() == 0
    np.testing.assert_array_equal(
        hot["arrays"]["dark_current_e_per_s_map"], 4 * normal["arrays"]["dark_current_e_per_s_map"]
    )
    np.testing.assert_array_equal(hot["calibration"]["dark_e"], 8 * normal["calibration"]["dark_e"])
    dark_zero = capture(model, 0, exposure=0, temperature=35)
    assert not dark_zero["arrays"]["mean_total_charge_e"].any()


@pytest.mark.parametrize(
    "photo,read_noise,post_noise,gain",
    [
        (1000, 0, 0, 1),
        (0, 4, 0, 1),
        (0, 0, 0.01, 1),
        (0, 0, 0.01, 2),
    ],
)
def test_shot_read_and_post_gain_voltage_noise_have_expected_input_variance(
    model, photo, read_noise, post_noise, gain
):
    model.update(
        full_well_e=1e6,
        conversion_gain_v_per_e=0.001,
        read_noise_e=read_noise,
        dark_current_e_per_s=10,
        post_gain_noise_v=post_noise,
    )
    model["adc"].update(bit_depth=24, full_scale_v=1000, black_level_dn=65536)
    result = capture(model, photo, shape=(512, 512), exposure=2, gain=gain, noise=True)
    c = result["calibration"]
    values = (result["arrays"]["raw_dn"].astype(float) - c["black_level_dn"]) / c["dn_per_e"]
    expected_mean = photo + 20
    expected_read_variance = read_noise**2 + (post_noise / (0.001 * gain)) ** 2
    expected_variance = expected_mean + expected_read_variance + c["quantization_variance_e2"]
    assert values.mean() == pytest.approx(expected_mean, abs=0.2)
    assert values.var() == pytest.approx(expected_variance, rel=0.015)
    assert c["read_noise_variance_e2"] == expected_read_variance
    assert not result["arrays"]["well_saturated_mask"].any()
    assert not result["arrays"]["adc_high_mask"].any()


def test_calibration_is_raw_only_and_noise_disabled_still_quantizes(model):
    result = capture(model, 300, exposure=0.5, gain=2)
    calibration = result["calibration"]
    assert set(calibration) == {
        "dn_per_e",
        "black_level_dn",
        "white_level_dn",
        "full_well_e",
        "dark_e",
        "read_noise_variance_e2",
        "quantization_variance_e2",
        "linear_floor_dn",
        "linear_ceiling_dn",
    }
    assert calibration["read_noise_variance_e2"] == 0
    assert calibration["quantization_variance_e2"] > 0
    estimate = (result["arrays"]["raw_dn"] - calibration["black_level_dn"]) / calibration[
        "dn_per_e"
    ] - calibration["dark_e"]
    np.testing.assert_allclose(estimate, 300, atol=0.5 / calibration["dn_per_e"])
    assert not {"photo_mean", "mean_total_charge_e", "photo_response_map"} & calibration.keys()


@pytest.mark.parametrize(
    "field,value",
    [
        ("full_well_e", True),
        ("conversion_gain_v_per_e", 0),
        ("read_noise_e", -1),
        ("dark_current_e_per_s", np.inf),
        ("digital_gain", np.nan),
        ("dark_current_doubling_c", 0),
        ("reference_temperature_c", -274),
        ("schema", "dcg"),
        ("extra", "unknown"),
    ],
)
def test_invalid_or_unsupported_electrical_models_are_rejected(model, field, value):
    model[field] = value
    with pytest.raises(ValueError):
        validate_electrical_model(model)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("adc", "bit_depth", 25),
        ("adc", "bit_depth", 12.0),
        ("adc", "black_level_dn", 4095),
        ("adc", "full_scale_v", 0),
        ("adc", "nonlinearity", 1),
        ("fixed_pattern", "prnu_sigma", -1),
        ("fixed_pattern", "seed", True),
        ("fixed_pattern", "dsnu_e_per_s", np.nan),
    ],
)
def test_nested_contracts_are_strict(model, section, field, value):
    model[section][field] = value
    with pytest.raises(ValueError):
        validate_electrical_model(model)


@pytest.mark.parametrize(
    "photo",
    [
        np.zeros((0, 2)),
        np.zeros((2, 2, 1)),
        np.full((2, 2), np.nan),
        np.full((2, 2), -1),
        np.ones((2, 2), dtype=bool),
        np.ones((2, 2), dtype=complex),
        np.full((2, 2), 1e14),
    ],
)
def test_invalid_charge_arrays_are_refused(model, photo):
    with pytest.raises(ValueError):
        capture(model, photo)


@pytest.mark.parametrize(
    "geometry",
    [
        {"full_sensor_shape": [4097, 4096], "crop_origin": [0, 0]},
        {"full_sensor_shape": [16, 16], "crop_origin": [1, 0]},
        {"full_sensor_shape": [True, 16], "crop_origin": [0, 0]},
        {"full_sensor_shape": [16, 16], "crop_origin": [-1, 0]},
        {"full_sensor_shape": [16, 16], "crop_origin": [0, 0], "binning": 2},
    ],
)
def test_native_geometry_is_bounded_and_explicit(model, geometry):
    with pytest.raises(ValueError):
        capture(model, geometry=geometry)


def test_small_native_sensor_crop_above_one_megapixel_preserves_fixed_pattern_identity(model):
    model["fixed_pattern"].update(prnu_sigma=0.05, dsnu_e_per_s=2, seed=31)
    full_shape = [1440, 1920]
    first = capture(
        model,
        shape=(16, 16),
        geometry={
            "full_sensor_shape": full_shape,
            "crop_origin": [1400, 1900],
        },
    )
    inside = capture(
        model,
        shape=(8, 8),
        exposure=0.02,
        seed=99,
        geometry={
            "full_sensor_shape": full_shape,
            "crop_origin": [1404, 1904],
        },
    )
    for name in ("photo_response_map", "dark_current_e_per_s_map"):
        np.testing.assert_array_equal(first["arrays"][name][4:12, 4:12], inside["arrays"][name])
    np.testing.assert_array_equal(
        first["calibration"]["dark_e"][4:12, 4:12] * 2, inside["calibration"]["dark_e"]
    )
    assert first["summary"]["limits"] == {
        "max_capture_pixels": 1_000_000,
        "max_native_pixels": 16_777_216,
    }


def test_larger_native_budget_does_not_expand_capture_array_limit(model):
    with pytest.raises(ValueError, match="at most 1000000 pixels"):
        capture(
            model,
            np.zeros((1001, 1000)),
            geometry={
                "full_sensor_shape": [1440, 1920],
                "crop_origin": [0, 0],
            },
        )


def test_temperature_overflow_and_excess_charge_are_rejected_before_sampling(model):
    model["dark_current_doubling_c"] = 1e-6
    with pytest.raises(ValueError, match="temperature scaling"):
        capture(model, temperature=30)
    model["dark_current_doubling_c"] = None
    model["dark_current_e_per_s"] = 1e12
    with pytest.raises(ValueError, match="Total mean charge"):
        capture(model, exposure=3600)


def test_unsafe_temperature_is_rejected_before_native_pattern_allocation(
    model, monkeypatch
):
    model["dark_current_doubling_c"] = 1e-6

    def forbidden_patterns(*args, **kwargs):
        raise AssertionError("full native fixed-pattern maps must not be allocated")

    monkeypatch.setattr("camera_platform.electrical._patterns", forbidden_patterns)
    with pytest.raises(ValueError, match="temperature scaling"):
        capture(
            model,
            shape=(2, 2),
            temperature=30,
            geometry={"full_sensor_shape": [4096, 4096], "crop_origin": [0, 0]},
        )


def test_authored_dark_temperature_law_reaches_integer_raw(model):
    model["dark_current_doubling_c"] = 10
    cold = capture(model, 0, shape=(2, 2), exposure=1, temperature=25)
    hot = capture(model, 0, shape=(2, 2), exposure=1, temperature=35)
    white = 2 ** model["adc"]["bit_depth"] - 1
    black = model["adc"]["black_level_dn"]
    dn_per_e = model["conversion_gain_v_per_e"] * white / model["adc"]["full_scale_v"]
    assert cold["arrays"]["dark_current_e_per_s_map"][0, 0] == 10
    assert hot["arrays"]["dark_current_e_per_s_map"][0, 0] == 20
    assert cold["arrays"]["raw_dn"][0, 0] == round(black + 10 * dn_per_e)
    assert hot["arrays"]["raw_dn"][0, 0] == round(black + 20 * dn_per_e)
    assert hot["arrays"]["raw_dn"][0, 0] > cold["arrays"]["raw_dn"][0, 0]


def test_unresolvable_raw_span_cannot_publish_an_invalid_hdr_calibration(model):
    model.update(full_well_e=1e-6, conversion_gain_v_per_e=1e-15, digital_gain=1e-6)
    model["adc"]["full_scale_v"] = 1e6
    with pytest.raises(ValueError, match="linear span"):
        capture(model, gain=1e-6)


@pytest.mark.parametrize(
    "kwargs",
    [{"noise": 1}, {"gain": True}, {"seed": -1}, {"exposure": -1}, {"temperature": np.inf}],
)
def test_capture_controls_reject_implicit_or_unbounded_values(model, kwargs):
    with pytest.raises(ValueError):
        capture(model, **kwargs)
