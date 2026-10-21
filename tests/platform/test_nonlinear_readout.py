"""Independent scalar/quantizer and physical noise checks for v2 readout."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import electrical, hdr, nonlinear_hdr, reference_hdr
from camera_platform import nonlinear_readout as nr


def model():
    source = {
        "kind": "estimated",
        "description": "Authored numerical fixture; not a measured sensor",
    }
    return {
        "schema": nr.SCHEMA,
        "full_well_e": 2000.0,
        "dark_current_e_per_s": 0.0,
        "reference_temperature_c": 25.0,
        "dark_current_doubling_c": None,
        "digital_gain": 1.0,
        "adc": {"bit_depth": 16, "full_scale_v": 1.0, "black_level_dn": 1000.0},
        "fixed_pattern": {"prnu_sigma": 0.0, "dsnu_e_per_s": 0.0, "seed": 0},
        "charge_voltage_response": {
            "schema": "camera_charge_voltage_response_v1",
            "units": {"charge": "electron", "voltage": "V"},
            "charge_e": [0.0, 1000.0, 2000.0],
            "signal_voltage_v": [0.0, 0.1, 0.15],
            "valid_domain": {"temperature_c": [20.0, 30.0], "analog_gain": [0.5, 4.0]},
            "signed_inverse": {"minimum_voltage_v": -0.02, "method": "origin_tangent"},
            "source": source.copy(),
        },
        "read_noise": {
            "schema": nr.NOISE_SCHEMA,
            "plane": nr.NOISE_PLANE,
            "variance_v2": 0.0,
            "temperature_range_c": [20.0, 30.0],
            "analog_gain_range": [0.5, 4.0],
            "source": source.copy(),
            "includes": nr.NOISE_INCLUDES,
        },
        "source": source.copy(),
    }


def capture(photo, m=None, *, exposure=0.01, gain=1.0, noise=False, seed=7, shape=(4, 4)):
    photo = (
        np.full(shape, photo, dtype=float) if np.isscalar(photo) else np.asarray(photo, dtype=float)
    )
    return nr.capture_electrons(
        photo,
        exposure,
        model() if m is None else m,
        analog_gain=gain,
        temperature_c=25.0,
        noise_enabled=noise,
        seed=seed,
        geometry={"full_sensor_shape": list(photo.shape), "crop_origin": [0, 0]},
    )


def test_scalar_forward_and_inverse_on_both_curve_segments():
    result = capture(np.array([[0.0, 500.0, 1000.0, 1500.0]]))
    expected_voltage = np.array([[0.0, 0.05, 0.1, 0.125]])
    expected_codes = np.rint(1000 + expected_voltage * 65535).astype(np.int32)
    np.testing.assert_array_equal(result["arrays"]["raw_dn"], expected_codes)
    decoded = nr.decode_raw(
        expected_codes, result["calibration"], guard_sigma=0, saturation_margin_dn=0
    )
    d = decoded["arrays"]
    assert d["valid_mask"].all()
    expected_charge = np.array([[0.0, 500.0, 1000.0, 1500.0]])
    assert np.all(d["charge_low_e"] <= expected_charge)
    assert np.all(d["charge_high_e"] >= expected_charge)
    assert abs(d["total_charge_e"][0, 3] - 1500) < 0.2
    # The old constant-slope inverse is wrong beyond the first segment.
    wrong = (expected_codes[0, 3] - 1000) / (65535 * 0.0001)
    assert abs(wrong - 1500) > 200
    assert "dn_per_e" not in result["calibration"]


def test_zero_and_linear_noiseless_equivalence_to_v1():
    m = model()
    m["charge_voltage_response"]["signal_voltage_v"] = [0.0, 0.1, 0.2]
    old = {k: v for k, v in m.items() if k in electrical.MODEL_KEYS}
    old.update(
        schema=electrical.SCHEMA,
        conversion_gain_v_per_e=0.0001,
        read_noise_e=0.0,
        post_gain_noise_v=0.0,
    )
    photo = np.array([[0.0, 1.0, 499.5, 1500.0], [5.0, 99.0, 1999.0, 3000.0]])
    new = capture(photo, m)
    v1 = electrical.capture_electrons(
        photo,
        0.01,
        old,
        analog_gain=1,
        temperature_c=25,
        geometry={"full_sensor_shape": [2, 4], "crop_origin": [0, 0]},
        noise_enabled=False,
        seed=7,
    )
    np.testing.assert_array_equal(new["arrays"]["raw_dn"], v1["arrays"]["raw_dn"])
    assert new["arrays"]["raw_dn"][0, 0] == 1000
    assert new["summary"]["physical_validation"] == "not_established"


@pytest.mark.parametrize("digital", [0.01, 0.5, 1.0, 1.3, 2.0, 8.0])
@pytest.mark.parametrize("black", [0.0, 3.5, 17.0])
def test_integer_preimage_matches_exhaustive_quantizer_enumeration(digital, black):
    white = 255
    codes = np.arange(white + 1, dtype=np.int32)[None, :]
    lo, hi = nr._adc_preimage(codes, black, white, digital)
    actual = [min(white, max(0, round(black + digital * (k - black)))) for k in range(white + 1)]
    for r in range(white + 1):
        predecessors = [k for k, value in enumerate(actual) if value == r]
        if predecessors:
            assert (lo[0, r], hi[0, r]) == (min(predecessors), max(predecessors))
        else:
            assert lo[0, r] > hi[0, r]


def test_hidden_adc_clipping_after_digital_attenuation_is_not_valid_charge():
    m = model()
    m["adc"]["full_scale_v"] = 0.02
    m["digital_gain"] = 0.1
    result = capture(500.0, m)
    assert result["arrays"]["adc_high_mask"].all()
    assert np.all(result["arrays"]["raw_dn"] < 65535)
    decoded = nr.decode_raw(result["arrays"]["raw_dn"], result["calibration"])
    assert not decoded["arrays"]["valid_mask"].any()
    assert decoded["arrays"]["upper_censored_mask"].all()
    assert np.isnan(decoded["arrays"]["total_charge_e"]).all()


def test_signed_below_black_and_knot_interval_are_explicit():
    m = model()
    m["adc"]["full_scale_v"] = 0.97
    c = capture(1000.0, m)["calibration"]
    raw = np.full((4, 4), 990, dtype=np.int32)
    raw[0, 0] = round(1000 + 0.1 * 65535 / 0.97)
    d = nr.decode_raw(raw, c, guard_sigma=0, saturation_margin_dn=0)["arrays"]
    assert d["valid_mask"].all()
    assert d["total_charge_e"][1, 1] < 0
    assert d["quantization_knot_crossing_mask"][0, 0]
    assert not d["quantization_knot_crossing_mask"][1, 1]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["model"]["adc"].update(black_level_dn=999),
        lambda c: c["acquisition"].update(analog_gain=2),
        lambda c: c.update(noiseless_photocharge=np.ones((4, 4))),
        lambda c: c.update(schema="camera_shared_charge_calibration_v1"),
    ],
)
def test_calibration_substitution_and_oracle_fields_are_rejected(mutation):
    result = capture(500.0)
    c = deepcopy(result["calibration"])
    mutation(c)
    with pytest.raises(ValueError):
        nr.decode_raw(result["arrays"]["raw_dn"], c)


@pytest.mark.parametrize("field,value", [("analog_gain", 5), ("temperature_c", 31)])
def test_capture_conditions_are_validated_without_arrays(field, value):
    args = {"exposure_s": 0.01, "analog_gain": 1.0, "temperature_c": 25.0}
    args[field] = value
    with pytest.raises(ValueError):
        nr.validate_capture_conditions(model(), **args)


def test_voltage_read_noise_is_after_nonlinear_conversion_and_before_analog_gain():
    m = model()
    m["read_noise"]["variance_v2"] = 1e-8
    m["adc"].update(bit_depth=24, black_level_dn=1000000)
    # At 1500e the local slope is .00005V/e: 0.0001V RMS means ~2e read RMS.
    result = capture(1500.0, m, noise=True, shape=(256, 256), gain=2.0)
    d = nr.decode_raw(result["arrays"]["raw_dn"], result["calibration"])["arrays"]
    assert d["valid_mask"].all()
    assert np.mean(d["read_variance_e2"]) == pytest.approx(4.0)
    assert np.var(d["total_charge_e"] - 1500) == pytest.approx(1504.0, rel=0.025)
    # No PRNU, dark, or second independently sampled read-noise-electron term.
    assert d["total_charge_e"].mean() == pytest.approx(1500.0, abs=0.5)


def test_nonlinear_hdr_recovers_equal_rates_across_different_segments_and_noiseless_variance():
    captures = [capture(500.0, exposure=0.01), capture(1500.0, exposure=0.03)]
    raw = np.stack([c["arrays"]["raw_dn"] for c in captures])
    before = raw.copy()
    result = nonlinear_hdr.merge_raw_bracket(
        raw,
        [0.01, 0.03],
        [c["calibration"] for c in captures],
        geometry={"kind": "native_sensor", "full_sensor_shape": [4, 4], "crop_origin": [0, 0]},
        profile=nonlinear_hdr.default_profile(),
    )
    a = result["arrays"]
    assert a["valid_mask"].all()
    assert a["temporal_consistency_mask"].all()
    assert np.all(abs(a["merged_rate"] - 50000) <= a["quantization_bound_rate"])
    assert not a["estimated_variance"].any()
    np.testing.assert_array_equal(a["weights"], np.full(raw.shape, 0.5))
    np.testing.assert_array_equal(raw, before)
    assert np.all(a["quantization_bound_rate"] > 0)


def test_hdr_one_valid_exposure_and_reference_invalid_are_not_filled_or_nan_contaminated():
    captures = [capture(500.0, exposure=0.01), capture(3000.0, exposure=0.06)]
    raw = np.stack([c["arrays"]["raw_dn"] for c in captures])
    cal = [c["calibration"] for c in captures]
    geom = {"kind": "native_sensor", "full_sensor_shape": [4, 4], "crop_origin": [0, 0]}
    fused = nonlinear_hdr.merge_raw_bracket(
        raw, [0.01, 0.06], cal, geometry=geom, profile=nonlinear_hdr.default_profile()
    )
    assert fused["arrays"]["valid_mask"].all()
    assert np.isfinite(fused["arrays"]["merged_rate"]).all()
    profile = reference_hdr.default_profile_v2()
    profile["reference_frame_index"] = 1
    result = reference_hdr.merge_reference_raw_bracket(
        raw, [0.01, 0.06], cal, geometry=geom, profile=profile
    )
    assert not result["arrays"]["valid_mask"].any()
    assert np.isnan(result["arrays"]["merged_rate"]).all()
    profile["reference_frame_index"] = 0
    good = reference_hdr.merge_reference_raw_bracket(
        raw, [0.01, 0.06], cal, geometry=geom, profile=profile
    )
    assert np.isfinite(good["arrays"]["merged_rate"]).all()
    assert "quantization noise" not in good["summary"]["estimated_variance_scope"]


def test_hdr_refuses_exposure_geometry_and_legacy_calibration_mismatch():
    c = capture(500.0)
    raw = np.stack([c["arrays"]["raw_dn"]] * 2)
    with pytest.raises(ValueError, match="exposure"):
        nonlinear_hdr.merge_raw_bracket(
            raw,
            [0.01, 0.02],
            [c["calibration"]] * 2,
            geometry={"kind": "native_sensor", "full_sensor_shape": [4, 4], "crop_origin": [0, 0]},
            profile=nonlinear_hdr.default_profile(),
        )
    with pytest.raises(ValueError, match="profile"):
        nonlinear_hdr.validate_profile(hdr.default_profile())
