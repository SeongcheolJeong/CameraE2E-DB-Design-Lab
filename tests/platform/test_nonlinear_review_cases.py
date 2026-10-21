"""Independent review probes for nonlinear identity, quantization and noise order."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import nonlinear_hdr, reference_hdr
from camera_platform import nonlinear_readout as readout


def model():
    source = {"kind": "estimated", "description": "Independent numerical review fixture"}
    return {
        "schema": readout.SCHEMA, "full_well_e": 2000., "dark_current_e_per_s": 0.,
        "reference_temperature_c": 25., "dark_current_doubling_c": None,
        "digital_gain": 1.,
        "adc": {"bit_depth": 12, "full_scale_v": .4095, "black_level_dn": 64.},
        "fixed_pattern": {"prnu_sigma": 0., "dsnu_e_per_s": 0., "seed": 0},
        "charge_voltage_response": {
            "schema": "camera_charge_voltage_response_v1",
            "units": {"charge": "electron", "voltage": "V"},
            "charge_e": [0., 1000., 2000.], "signal_voltage_v": [0., .1, .15],
            "valid_domain": {"temperature_c": [20., 30.], "analog_gain": [.5, 4.]},
            "signed_inverse": {"minimum_voltage_v": -.02, "method": "origin_tangent"},
            "source": source.copy(),
        },
        "read_noise": {
            "schema": readout.NOISE_SCHEMA, "plane": readout.NOISE_PLANE,
            "includes": readout.NOISE_INCLUDES, "variance_v2": 0.,
            "temperature_range_c": [20., 30.], "analog_gain_range": [.5, 4.],
            "source": source.copy(),
        },
        "source": source.copy(),
    }


def calibration(m=None, shape=(4, 4), exposure=.01, dark=0, noise=False):
    return readout.make_calibration(
        model() if m is None else m, analog_gain=1., temperature_c=25., exposure_s=exposure,
        geometry={"full_sensor_shape": list(shape), "crop_origin": [0, 0]},
        dark_e=np.full(shape, float(dark)), noise_enabled=noise,
    )


def test_dark_map_byte_change_cannot_keep_an_accepted_calibration_identity():
    cal = calibration()
    changed = deepcopy(cal)
    changed["dark_e"][1, 1] = .25
    with pytest.raises(ValueError, match="[Dd]ark|hash"):
        readout.validate_calibration(changed, (4, 4))


def test_dark_map_reshape_cannot_keep_the_original_calibration_shape_identity():
    cal = readout.make_calibration(
        model(), analog_gain=1., temperature_c=25., exposure_s=.01,
        geometry={"full_sensor_shape": [8, 8], "crop_origin": [0, 0]},
        dark_e=np.arange(16.).reshape(4, 4), noise_enabled=False,
    )
    changed = deepcopy(cal)
    changed["dark_e"] = changed["dark_e"].reshape(2, 8)
    with pytest.raises(ValueError, match="[Dd]ark|shape|hash"):
        readout.validate_calibration(changed, (2, 8))


def test_real_rng_stage_is_one_poisson_then_voltage_noise_after_nonlinear_curve(monkeypatch):
    events = []
    sampled = np.array([[500., 1500.], [2100., 1000.]])
    noise_v = np.array([[.001, -.001], [.002, -.002]])

    class ScriptedRNG:
        def poisson(self, means):
            events.append(("poisson", means.copy()))
            return sampled.copy()

        def normal(self, mean, sigma, shape):
            events.append(("voltage_normal", mean, sigma, shape))
            return noise_v.copy()

    monkeypatch.setattr(readout.electrical, "_patterns", lambda *args: (
        np.ones((2, 2)), np.full((2, 2), 100.), {}
    ))
    monkeypatch.setattr(np.random, "default_rng", lambda seed: ScriptedRNG())
    m = model()
    m["read_noise"]["variance_v2"] = 1e-6
    photo = np.array([[490., 1490.], [2090., 990.]])
    result = readout.capture_electrons(
        photo, .1, m, analog_gain=2., temperature_c=25.,
        geometry={"full_sensor_shape": [2, 2], "crop_origin": [0, 0]},
        noise_enabled=True, seed=17,
    )
    assert [event[0] for event in events] == ["poisson", "voltage_normal"]
    np.testing.assert_array_equal(events[0][1], photo + 10)
    assert events[1][1:] == (0, .001, (2, 2))
    # Hand-computed clipped-charge voltages, then signed noise, then 2x gain.
    expected_v = np.array([[.05, .125], [.15, .1]]) + noise_v
    expected_raw = np.rint(64 + expected_v * 2 * 10000).astype(np.int32)
    np.testing.assert_array_equal(result["arrays"]["raw_dn"], expected_raw)
    np.testing.assert_array_equal(result["arrays"]["well_saturated_mask"],
                                  [[False, False], [True, False]])


@pytest.mark.parametrize("black", [8., 8.5])
@pytest.mark.parametrize("digital", [.25, .5, 1., 1.3, 2., 8.])
def test_actual_quantizer_voltage_preimages_enclose_half_code_probes(black, digital):
    m = model()
    m["adc"].update(bit_depth=8, full_scale_v=.255, black_level_dn=black)
    m["digital_gain"] = digital
    voltages = np.array([.001 * k + offset for k in range(-7, 148)
                         for offset in (-.0005, 0., .0005)])[None, :]
    # Use the actual encoder only to select a code. Check its inverse against
    # the independently specified two-segment charge relation.
    raw = readout.encode_voltage(voltages, m, 1.)["raw_dn"]
    cal = calibration(m, shape=raw.shape)
    d = readout.decode_raw(raw, cal, guard_sigma=0, saturation_margin_dn=0)["arrays"]
    charge = np.where(voltages <= .1, voltages / .0001,
                      1000 + (voltages - .1) / .00005)
    valid = d["valid_mask"]
    assert valid.any()
    # The contract is a closed real-arithmetic enclosure. Reserve a documented
    # 8-ULP arithmetic allowance at exact binary64 half-code representations.
    allowance = 8 * np.spacing(np.maximum(abs(charge), 1.))
    assert np.all(d["charge_low_e"][valid] <= (charge + allowance)[valid])
    assert np.all(d["charge_high_e"][valid] >= (charge - allowance)[valid])


def test_hdr_reference_spatial_disagreement_and_all_invalid_are_not_nan_contaminated():
    raw = np.full((3, 4, 4), 564, dtype=np.int32)  # 500e at each .01s exposure.
    raw[0, 0, 0] = 4095  # Invalid reference must never be filled.
    raw[1, 2, 2] = 864   # A single disagreement guards neighboring reference pixels.
    raw[:, 3, 0] = 4095  # Entirely invalid location.
    cals = [calibration() for _ in range(3)]
    geom = {"kind": "native_sensor", "full_sensor_shape": [4, 4], "crop_origin": [0, 0]}
    ordinary = nonlinear_hdr.merge_raw_bracket(
        raw, [.01] * 3, cals, geometry=geom, profile=nonlinear_hdr.default_profile()
    )["arrays"]
    assert ordinary["valid_mask"][0, 0]
    assert not ordinary["valid_mask"][3, 0]
    assert np.isnan(ordinary["merged_rate"][3, 0])
    assert (ordinary["weights"][:, 3, 0] == 0).all()
    result = reference_hdr.merge_reference_raw_bracket(
        raw, [.01] * 3, cals, geometry=geom, profile=reference_hdr.default_profile_v2()
    )["arrays"]
    assert not result["valid_mask"][0, 0]
    assert np.isnan(result["merged_rate"][0, 0])
    assert result["reference_fallback_mask"][2, 2]
    assert result["weights"][0, 2, 2] == 1
    assert (result["weights"][1:, 2, 2] == 0).all()
    assert np.isfinite(result["merged_rate"][result["valid_mask"]]).all()
    assert np.isfinite(result["quantization_bound_rate"][result["valid_mask"]]).all()
    assert (result["rgb"][~result["linear_rgb_valid_mask"]] == 0).all()


def test_temperature_law_failure_is_rejected_before_fixed_pattern_allocation(monkeypatch):
    m = model()
    m["dark_current_doubling_c"] = 1e-6
    m["reference_temperature_c"] = 20.

    def forbidden_allocation(*args):
        pytest.fail("Unsupported dark temperature law reached sensor-array allocation")

    monkeypatch.setattr(readout.electrical, "_patterns", forbidden_allocation)
    with pytest.raises(ValueError):
        readout.capture_electrons(
            np.full((4, 4), 1.), .01, m, analog_gain=1., temperature_c=25.,
            geometry={"full_sensor_shape": [4, 4], "crop_origin": [0, 0]},
            noise_enabled=False, seed=1,
        )


def test_ill_conditioned_lut_enclosure_accounts_for_voltage_arithmetic_amplification():
    m = model()
    m["full_well_e"] = 3.
    m["adc"].update(bit_depth=12, full_scale_v=3., black_level_dn=1364.)
    m["charge_voltage_response"]["charge_e"] = [0., 1., 2., 3.]
    m["charge_voltage_response"]["signal_voltage_v"] = [
        0., 1., float(1 + 5 * np.finfo(float).eps), 2.
    ]
    captured = readout.capture_electrons(
        np.full((2, 2), 1.29), .01, m, analog_gain=.5, temperature_c=25.,
        geometry={"full_sensor_shape": [2, 2], "crop_origin": [0, 0]},
        noise_enabled=False, seed=1,
    )
    np.testing.assert_array_equal(captured["arrays"]["raw_dn"], 2046)
    result = readout.decode_raw(captured["arrays"]["raw_dn"], captured["calibration"],
                                guard_sigma=0, saturation_margin_dn=0)
    arrays = result["arrays"]
    assert arrays["valid_mask"].all()
    assert (arrays["charge_low_e"] <= 1.29).all()
    assert (arrays["charge_high_e"] >= 1.29).all()
    assert (arrays["quantization_bound_e"] >= abs(arrays["total_charge_e"] - 1.29)).all()
    assert result["summary"]["numerical_voltage_allowance_v"] > 0
    assert result["summary"]["numerical_charge_allowance_e"] > 0
    assert "not a formal IEEE interval proof" in result["summary"]["quantization"]


def test_numerical_enclosure_allowance_does_not_change_original_raw_midpoint_estimator():
    m = model()
    m["full_well_e"] = 3.
    m["adc"].update(bit_depth=12, full_scale_v=3., black_level_dn=272.49999999999886)
    m["charge_voltage_response"]["charge_e"] = [0., 1., 2., 3.]
    m["charge_voltage_response"]["signal_voltage_v"] = [
        0., 1., float(1 + 5 * np.finfo(float).eps), 2.
    ]
    cal = readout.make_calibration(
        m, analog_gain=1.3, temperature_c=25., exposure_s=.01,
        geometry={"full_sensor_shape": [2, 2], "crop_origin": [0, 0]},
        dark_e=np.zeros((2, 2)), noise_enabled=False,
    )
    result = readout.decode_raw(np.full((2, 2), 2047, dtype=np.int32), cal,
                                guard_sigma=0, saturation_margin_dn=0)
    # Independent scalar binary64 evaluation of the declared unexpanded RAW
    # interval midpoint: 1+2eps V lies 2/5 along the narrow 1..2e segment.
    scale = 3. / 4095 / 1.3
    lo = (2047 - .5 - m["adc"]["black_level_dn"]) * scale
    hi = (2047 + .5 - m["adc"]["black_level_dn"]) * scale
    middle = lo + (hi - lo) / 2
    assert middle == 1 + 2 * np.finfo(float).eps
    np.testing.assert_array_equal(result["arrays"]["total_charge_e"], 1.4)
