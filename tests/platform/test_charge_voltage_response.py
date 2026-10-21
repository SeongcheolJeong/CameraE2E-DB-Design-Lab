"""Independent scalar references for a static monotone charge/voltage model."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import charge_voltage_response as response


def model():
    return {
        "schema": "camera_charge_voltage_response_v1",
        "units": {"charge": "electron", "voltage": "V"},
        "charge_e": [0, 1000, 2000],
        "signal_voltage_v": [0, .1, .15],
        "valid_domain": {"temperature_c": [20, 30], "analog_gain": [1, 4]},
        "signed_inverse": {"minimum_voltage_v": -.02, "method": "origin_tangent"},
        "source": {"kind": "estimated", "description": "Authored static response fixture"},
    }


def test_normalization_is_detached_finite_json_without_validation_promotion():
    original = model()
    snapshot = deepcopy(original)
    normalized = response.validate_response(original)
    assert normalized == original
    assert response.validate_response(normalized) == normalized
    assert json.loads(json.dumps(normalized, allow_nan=False)) == original
    assert all(type(x) is float for x in normalized["charge_e"])
    assert "physical_validation" not in normalized
    normalized["charge_e"][1] = 12
    normalized["signal_voltage_v"][1] = 1
    normalized["units"]["voltage"] = "mV"
    normalized["valid_domain"]["temperature_c"][0] = 19
    normalized["signed_inverse"]["minimum_voltage_v"] = -1
    normalized["source"]["kind"] = "measured"
    assert original == snapshot


@pytest.mark.parametrize("kind", ["estimated", "simulated", "measured"])
def test_source_kind_is_preserved_without_physical_validation_assertion(kind):
    data = model()
    data["source"]["kind"] = kind
    validated = response.validate_response(data)
    assert validated["source"]["kind"] == kind
    assert "physical_validation" not in validated


def test_nonlinear_forward_matches_independent_scalar_500_1500_and_knots():
    charge = np.array([[0, 500, 1000], [1500, 1800, 2000]], dtype=float)
    expected = np.array([[0, .05, .1], [.125, .14, .15]])
    before = charge.copy()
    actual = response.evaluate_voltage(charge, model())
    np.testing.assert_allclose(actual, expected, rtol=1e-14, atol=0)
    np.testing.assert_array_equal(charge, before)
    assert not np.shares_memory(actual, charge)
    for scalar, voltage in [(0, 0), (500, .05), (1500, .125), (2000, .15)]:
        result = response.evaluate_voltage(scalar, model())
        assert type(result) is np.ndarray and result.shape == ()
        assert result == pytest.approx(voltage)


def test_linear_curve_regression_and_zero_signed_inverse():
    data = model()
    data["signal_voltage_v"] = [0, .1, .2]
    charge = np.array([0, 1, 500, 1000, 1500, 2000])
    voltage = response.evaluate_voltage(charge, data)
    np.testing.assert_allclose(voltage, charge * .0001, rtol=1e-15)
    interval = response.invert_voltage_interval(np.array([-.02, 0, .05]),
                                                np.array([-.01, 0, .15]), data)
    np.testing.assert_allclose(interval["charge_low_e"], [-200, 0, 500])
    np.testing.assert_allclose(interval["charge_high_e"], [-100, 0, 1500])
    np.testing.assert_allclose(interval["charge_mid_e"], [-150, 0, 1000])
    np.testing.assert_allclose(interval["inverse_slope_e_per_v"], 10000)
    assert interval["valid_mask"].all()
    assert not interval["knot_crossing_mask"].any()


def test_inverse_nonlinear_interval_uses_voltage_midpoint_and_local_derivative():
    # 0.09..0.12 V crosses the 0.1 V knot. Inverse(mid V)=1100 e;
    # averaging inverted endpoint charges would instead give 1150 e.
    result = response.invert_voltage_interval(.09, .12, model())
    assert result["valid_mask"]
    assert result["knot_crossing_mask"]
    assert result["charge_low_e"] == pytest.approx(900)
    assert result["charge_high_e"] == pytest.approx(1400)
    assert result["charge_mid_e"] == pytest.approx(1100)
    assert result["inverse_slope_e_per_v"] == pytest.approx(20000)
    assert all(type(value) is np.ndarray and value.shape == () for value in result.values())


@pytest.mark.parametrize("low,high,crossing,slope", [
    (.05, .05, False, 10000), (.125, .125, False, 20000),
    (.1, .1, False, 20000), (.15, .15, False, 20000),
    (.09, .1, False, 10000), (.1, .11, False, 20000),
    (.09, .11, True, 20000), (-.01, .01, False, 10000),
    (-.02, .15, True, 10000),
])
def test_interval_knot_and_signed_boundary_conventions(low, high, crossing, slope):
    result = response.invert_voltage_interval(low, high, model())
    assert bool(result["valid_mask"])
    assert bool(result["knot_crossing_mask"]) is crossing
    assert result["inverse_slope_e_per_v"] == pytest.approx(slope)


def test_inverse_mixed_domain_never_clamps_and_does_not_mutate_inputs():
    low = np.array([-.02, -.021, .14, -.03, 1e308])
    high = np.array([-.01, 0, .151, .16, 1e308])
    before = (low.copy(), high.copy())
    result = response.invert_voltage_interval(low, high, model())
    np.testing.assert_array_equal(result["valid_mask"], [True, False, False, False, False])
    np.testing.assert_array_equal(result["knot_crossing_mask"], [False] * 5)
    for key in ("charge_low_e", "charge_high_e", "charge_mid_e", "inverse_slope_e_per_v"):
        assert np.isnan(result[key][1:]).all()
        assert np.isfinite(result[key][0])
        assert not np.shares_memory(result[key], low)
    assert result["charge_low_e"][0] == pytest.approx(-200)
    np.testing.assert_array_equal(low, before[0])
    np.testing.assert_array_equal(high, before[1])


def test_conditions_are_inclusive_and_capacity_must_be_fully_covered():
    for temperature in (20, 25, 30):
        for gain in (1, 2, 4):
            assert response.validate_conditions(model(), temperature, gain, 2000) == model()
    data = model()
    data["valid_domain"] = {"temperature_c": [25, 25], "analog_gain": [2, 2]}
    assert response.validate_conditions(data, 25, 2, 1800) == data


@pytest.mark.parametrize("temperature,gain,capacity", [
    (19.999, 1, 2000), (30.001, 1, 2000), (25, .999, 2000), (25, 4.001, 2000),
    (25, 1, 2000.001), (25, 1, 0), (25, 1, -1), (25, 1, True),
    (True, 1, 2000), (25, True, 2000), (25, 1, float("nan")),
    (float("inf"), 1, 2000), (25, float("nan"), 2000),
])
def test_conditions_refuse_unsupported_settings(temperature, gain, capacity):
    with pytest.raises(ValueError):
        response.validate_conditions(model(), temperature, gain, capacity)


@pytest.mark.parametrize("path,value", [
    (("schema",), "camera_charge_voltage_response_v2"), (("schema",), True),
    (("units",), {"charge": "electron", "voltage": "mV"}),
    (("units",), {"charge": "e", "voltage": "V"}),
    (("units",), {"charge": "electron", "voltage": "V", "extra": 1}),
    (("charge_e",), [1, 1000, 2000]), (("signal_voltage_v",), [.01, .1, .15]),
    (("charge_e",), [0, 1000, 1000]), (("signal_voltage_v",), [0, .1, .1]),
    (("charge_e",), [0, 1000, 999]), (("signal_voltage_v",), [0, .1, .09]),
    (("charge_e",), [0]), (("charge_e",), []), (("charge_e",), [0, 2000]),
    (("charge_e",), list(range(257))), (("signal_voltage_v",), [0, .15]),
    (("charge_e",), (0, 1000, 2000)), (("charge_e",), np.array([0, 1000, 2000])),
    (("charge_e",), [[0], [1000], [2000]]), (("signal_voltage_v",), [0, [.1], .15]),
    (("charge_e",), [0, True, 2000]), (("signal_voltage_v",), [False, .1, .15]),
    (("charge_e",), [0, np.int64(1000), 2000]), (("charge_e",), [0, "1000", 2000]),
    (("charge_e",), [0, 1000, float("inf")]),
    (("signal_voltage_v",), [0, float("nan"), .15]),
    (("charge_e",), [0, 1000, 1e13 + 1]), (("signal_voltage_v",), [0, .1, 1e6 + 1]),
    (("charge_e",), [0, 1000, 10**400]), (("signal_voltage_v",), [0, -.1, .15]),
    (("valid_domain",), {"temperature_c": [20, 30]}),
    (("valid_domain", "temperature_c"), [30, 20]),
    (("valid_domain", "temperature_c"), [-274, 30]),
    (("valid_domain", "temperature_c"), [20, 1001]),
    (("valid_domain", "temperature_c"), [20, True]),
    (("valid_domain", "temperature_c"), [20, float("nan")]),
    (("valid_domain", "analog_gain"), [0, 4]),
    (("valid_domain", "analog_gain"), [1, 1e6 + 1]),
    (("valid_domain", "analog_gain"), [1, float("inf")]),
    (("valid_domain", "analog_gain"), (1, 4)),
    (("valid_domain", "analog_gain"), [1, 2, 4]),
    (("signed_inverse",), {"method": "origin_tangent"}),
    (("signed_inverse", "method"), "clamp_zero"),
    (("signed_inverse", "minimum_voltage_v"), 0),
    (("signed_inverse", "minimum_voltage_v"), .01),
    (("signed_inverse", "minimum_voltage_v"), True),
    (("signed_inverse", "minimum_voltage_v"), float("nan")),
    (("signed_inverse", "minimum_voltage_v"), -float("inf")),
    (("signed_inverse", "minimum_voltage_v"), -1e6 - 1),
    (("source",), {"kind": "estimated", "description": "fixture", "validated": True}),
    (("source", "kind"), "validated"), (("source", "kind"), True),
    (("source", "description"), "  "), (("source", "description"), "a" * 4001),
    (("source", "description"), 1),
])
def test_malformed_authored_contract_is_rejected(path, value):
    data = model()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        response.validate_response(data)


@pytest.mark.parametrize("value", [None, [], True, "response"])
def test_non_object_contract_is_rejected(value):
    with pytest.raises(ValueError):
        response.validate_response(value)


def test_exact_top_level_keys_and_untrusted_response_mutation_are_revalidated():
    data = model()
    data["extra"] = "unrecognized"
    with pytest.raises(ValueError):
        response.validate_response(data)
    data.pop("extra")
    data.pop("source")
    with pytest.raises(ValueError):
        response.validate_response(data)
    data = response.validate_response(model())
    data["charge_e"][1] = 0
    for operation in (lambda: response.evaluate_voltage(500, data),
                      lambda: response.invert_voltage_interval(0, .1, data),
                      lambda: response.validate_conditions(data, 25, 1, 2000)):
        with pytest.raises(ValueError):
            operation()


@pytest.mark.parametrize("charge,voltage,minimum", [
    ([0, 1000], [0, 1e-13], -.00001),  # Unsupported tiny positive slope.
    ([0, .000001], [0, 1], -.01),  # Unsupported excessive slope.
    ([0, 5e-324], [0, 1], -.01),  # Division overflow cannot pass validation.
    ([0, 1e13], [0, 5e-324], -.01),  # Underflow cannot become a flat inverse.
    ([0, 1], [0, 1e-15], -.02),  # Signed coordinate exceeds bounded charge.
    ([0, 1e13 - 1e-4, 1e13], [0, .1, .2], -.01),  # Float64 knots collapse.
])
def test_numerically_unsupported_slopes_and_signed_range_are_rejected(charge, voltage, minimum):
    data = model()
    data.update(charge_e=charge, signal_voltage_v=voltage)
    data["signed_inverse"]["minimum_voltage_v"] = minimum
    with pytest.raises(ValueError):
        response.validate_response(data)


def test_maximum_knot_count_and_boundary_conditions_remain_usable():
    data = model()
    data["charge_e"] = list(range(256))
    data["signal_voltage_v"] = [i * .0001 for i in range(256)]
    data["valid_domain"] = {"temperature_c": [-273.15, 1000], "analog_gain": [1e-6, 1e6]}
    assert response.validate_conditions(data, -273.15, 1e6, 255)["charge_e"][-1] == 255
    assert response.evaluate_voltage(255, data) == data["signal_voltage_v"][-1]


@pytest.mark.parametrize("value", [
    True, np.bool_(True), "500", [500], (500,), 1 + 1j, float("nan"), float("inf"),
    10**400, np.array([True]), np.array([1 + 1j]), np.array(["500"]),
    np.array([500], dtype=object), np.array([]), np.zeros((1, 1, 1)),
    np.array([float("nan")]), np.array([float("inf")]),
    np.ma.array([500.], mask=[True]),
])
def test_runtime_samples_reject_non_numeric_nonfinite_bool_or_shape(value):
    with pytest.raises(ValueError):
        response.evaluate_voltage(value, model())
    with pytest.raises(ValueError):
        response.invert_voltage_interval(value, value, model())


@pytest.mark.parametrize("value", [-1e-20, 2000.00001, np.array([0, -1]), np.array([0, 2001])])
def test_forward_requires_only_covered_physical_charge(value):
    with pytest.raises(ValueError, match="no extrapolation"):
        response.evaluate_voltage(value, model())


def test_inverse_requires_exact_shape_order_and_resource_bound(monkeypatch):
    for low, high in [(0, np.array([.1])), (np.zeros((1, 2)), np.ones(2)),
                      (.1, .09), (np.array([.01, .1]), np.array([.02, .09]))]:
        with pytest.raises(ValueError):
            response.invert_voltage_interval(low, high, model())
    monkeypatch.setattr(response, "MAX_SAMPLES", 2)
    with pytest.raises(ValueError):
        response.evaluate_voltage(np.zeros(3), model())
    with pytest.raises(ValueError):
        response.invert_voltage_interval(np.zeros(3), np.zeros(3), model())


def test_numeric_numpy_inputs_2d_and_no_mutable_result_aliases():
    for scalar in (np.int64(500), np.float32(500), np.array(500)):
        assert response.evaluate_voltage(scalar, model()) == pytest.approx(.05)
    low = np.array([[0, .05], [.1, .125]])
    high = low.copy()
    first = response.invert_voltage_interval(low, high, model())
    second = response.invert_voltage_interval(low, high, model())
    np.testing.assert_allclose(first["charge_mid_e"], [[0, 500], [1000, 1500]])
    first["charge_mid_e"][0, 0] = 999
    assert second["charge_mid_e"][0, 0] == 0
    assert first["charge_low_e"][0, 0] == 0
    assert first["charge_high_e"][0, 0] == 0
