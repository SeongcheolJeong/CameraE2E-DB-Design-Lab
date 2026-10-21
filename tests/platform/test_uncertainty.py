"""Independent bounds, nonlinear clipping, source contracts and unknown coverage."""

from copy import deepcopy
from decimal import Decimal, localcontext

import numpy as np
import pytest

from camera_platform.runtime import QuickEstimateRuntime, ResponseDBRuntime, RuntimeRequestError
from camera_platform.uncertainty import (
    REQUEST_PATHS,
    Interval,
    checked_bound,
    expectation_intervals,
    observe_budget,
    propagate,
)


@pytest.fixture
def request_data():
    return {
        "wavelength_nm": [400, 500, 600, 700],
        "spectral_irradiance_w_m2_nm": [1e-5] * 4,
        "qe": [[0.3] * 4, [0.5] * 4, [0.2] * 4],
        "pixel_pitch_um": 4.0,
        "exposure_s": 0.004,
        "readout": {
            "full_well_e": 10000,
            "read_noise_e": 2,
            "dark_current_e_per_s": 3,
            "gain": 1,
            "bit_depth": 16,
            "black_level_dn": 256,
        },
        "lens": {
            "field_radius": [0, 0.5, 1],
            "theta_deg": [0, 20],
            "angular_weights": [[1, 0], [0.5, 0.5], [0, 1]],
            "relative_illumination": [1, 0.9, 0.7],
        },
        "sensor_angular_response": {
            "theta_deg": [0, 10, 20],
            "relative_response": [[[1, 0.8, 0.5]] * 4, [[1, 1, 1]] * 4, [[1, 0.9, 0.8]] * 4],
        },
        "output_size": [8, 8],
        "frames": 2,
        "seed": 81,
    }


def bound(value, unit):
    return {
        "kind": "absolute_bound",
        "unit": unit,
        "value": value,
        "source_kind": "estimated",
        "source_uri": "fixture://declared-bound",
        "method_and_version": "authored-assumption/1",
        "reason": "",
    }


def add(request, path, value):
    request.setdefault("parameter_uncertainty", {})[path] = {
        "definition": bound(value, REQUEST_PATHS[path][0]),
        "source_record_id": "fixture-model",
        "source_content_hash": "0" * 64,
        "source_path": path,
    }


def test_decimal_analytic_charge_is_enclosed_even_for_roundoff_only(request_data):
    add(request_data, "qe", 0.0)
    intervals = expectation_intervals(
        request_data, QuickEstimateRuntime()._checked(request_data), False
    )
    with localcontext() as ctx:
        ctx.prec = 70
        # Constant E and QE make the wavelength integral analytically exact.
        for channel, qe in enumerate([0.3, 0.5, 0.2]):
            charge = (Decimal.from_float(4.0) * Decimal("1e-6")) ** 2 * Decimal.from_float(0.004)
            charge *= Decimal.from_float(1e-5) * Decimal.from_float(qe) * Decimal("1e-9")
            charge *= Decimal(700**2 - 400**2) / 2 / (Decimal("6.62607015e-34") * 299792458)
            actual = intervals["channel_electrons"]
            assert (
                Decimal.from_float(actual.lo[0, channel])
                <= charge
                <= Decimal.from_float(actual.hi[0, channel])
            )
            assert actual.hi[0, channel] - actual.lo[0, channel] < float(charge) * 1e-12


def test_declared_input_bound_changes_envelope_not_nominal_raw(request_data):
    plugin = ResponseDBRuntime()
    original = plugin.execute(request_data)
    add(request_data, "qe", 0.01)
    changed = plugin.execute(request_data)
    np.testing.assert_array_equal(changed["arrays"]["raw_frames"], original["arrays"]["raw_frames"])
    budget = changed["summary"]["uncertainty_budget"]
    assert budget["status"] == "partial" and budget["total_uncertainty"] == {"kind": "unknown"}
    ratios = budget["conditional_intervals"]["channel_ratios"]
    central = np.asarray(changed["summary"]["channel_ratios"])
    assert (ratios["lower"] < central).all() and (ratios["upper"] > central).all()
    assert any(x["path"] == "model_discrepancy" for x in budget["unknown_contributions"])


@pytest.mark.parametrize("gain,light", [(1.0, 1e-5), (4.0, 0.005), (0.5, 0.02)])
def test_joint_parameter_draws_and_saturation_stay_within_enclosure(request_data, gain, light):
    request_data["readout"]["gain"] = gain
    request_data["spectral_irradiance_w_m2_nm"] = [light] * 4
    deltas = {
        "qe": 0.015,
        "pixel_pitch_um": 0.1,
        "exposure_s": 0.0002,
        "readout.full_well_e": 500,
        "readout.gain": gain * 0.1,
        "readout.dark_current_e_per_s": 2,
        "readout.black_level_dn": 10,
        "spectral_irradiance_w_m2_nm": light * 0.1,
    }
    for path, delta in deltas.items():
        add(request_data, path, delta)
    plugin = QuickEstimateRuntime()
    envelope = propagate(request_data, plugin._checked(request_data), response_db=False)[
        "conditional_intervals"
    ]
    rng = np.random.default_rng(1729)
    for _ in range(80):
        varied = deepcopy(request_data)
        varied.pop("parameter_uncertainty")
        for path, delta in deltas.items():
            keys = path.split(".")
            target = varied
            for key in keys[:-1]:
                target = target[key]
            original = np.asarray(target[keys[-1]])
            # Correlated and independent samples are both allowed by the box enclosure.
            target[keys[-1]] = (original + rng.uniform(-delta, delta, size=original.shape)).tolist()
        summary = plugin.execute(varied)["summary"]
        for key in ["channel_electrons", "channel_reference_signal_dn"]:
            values = np.asarray(summary[key])
            lo = np.asarray(envelope[key]["lower"])
            hi = np.asarray(envelope[key]["upper"])
            assert (lo <= values).all() and (values <= hi).all()


def test_roi_averages_precede_ratio_bounds_and_preserve_dark_choice(request_data):
    add(request_data, "qe", 0.02)
    plugin = ResponseDBRuntime()
    budget = propagate(request_data, plugin._checked(request_data), response_db=True)
    weights = [[[0.5, 0.5, 0], [0, 0.5, 0.5], [0.25, 0.25, 0.5]]]
    sampling = {"channel_field_weights": weights}
    for dark in ["mean_dark_frame", "black_level_only"]:
        observed = observe_budget(budget, sampling, dark)
        key = (
            "channel_black_corrected_signal_dn"
            if dark == "black_level_only"
            else "channel_reference_signal_dn"
        )
        old = budget["conditional_intervals"][key]
        for endpoint in ["lower", "upper"]:
            expected = np.einsum("rcf,fc->rc", weights, np.asarray(old[endpoint]))
            actual = np.asarray(observed["conditional_intervals"]["channel_means_dn"][endpoint])
            assert (actual <= expected).all() if endpoint == "lower" else (actual >= expected).all()
        assert observed["total_uncertainty"]["kind"] == "unknown"
        assert "upstream_conditional_intervals" in observed


@pytest.mark.parametrize("value", [-1, True, [True, 0.1], "0.1", float("nan"), [[0.1, 0.1]]])
def test_invalid_bound_values_refused(request_data, value):
    add(request_data, "qe", value)
    with pytest.raises(RuntimeRequestError):
        QuickEstimateRuntime().execute(request_data)


def test_sigma_and_wrong_units_are_not_interpreted_as_bounds():
    for definition in [{**bound(0.1, "1"), "kind": "standard_deviation"}, bound(0.1, "percent")]:
        with pytest.raises(ValueError):
            checked_bound(0.5, definition, "1", 0, 1)


def test_zero_green_envelope_has_no_fabricated_finite_ratio(request_data):
    add(request_data, "qe", 0.2)
    request_data["qe"][1] = [0.2] * 4
    plugin = QuickEstimateRuntime()
    budget = propagate(request_data, plugin._checked(request_data), response_db=False)
    ratios = budget["conditional_intervals"]["channel_ratios"]
    assert ratios["defined"] == [[False, False]]
    assert ratios["lower"] == [[None, None]] and ratios["upper"] == [[None, None]]


def test_no_declared_bounds_stays_unknown_and_does_not_invent_zero(request_data):
    result = QuickEstimateRuntime().execute(request_data)["summary"]["uncertainty_budget"]
    assert result["status"] == "unknown" and "conditional_intervals" not in result
    assert "qe" in {x["path"] for x in result["unknown_contributions"]}


def test_interval_zero_denominator_is_rejected():
    with pytest.raises(ValueError, match="zero"):
        Interval.point(1) / Interval(np.array(-1.0), np.array(1.0))


@pytest.mark.parametrize("spectral", [False, True])
def test_angular_interpolation_and_joint_lens_sensor_bounds_enclose_corner_cases(
    request_data, spectral
):
    request_data["lens"]["theta_deg"] = [0, 15]
    ri_delta = np.array([0.0, 0.02, 0.02])
    weights_delta = np.array([[0.0, 0.0], [0.1, 0.1], [0.0, 0.0]])
    if spectral:
        request_data["lens"]["wavelength_nm"] = request_data["wavelength_nm"]
        for name in ["angular_weights", "relative_illumination"]:
            request_data["lens"][name] = [request_data["lens"][name]] * 4
        ri_delta = np.broadcast_to(ri_delta, (4, 3))
        weights_delta = np.broadcast_to(weights_delta, (4, 3, 2))
    angular_delta = np.zeros((3, 4, 3))
    angular_delta[..., 1:] = 0.02
    add(request_data, "lens.relative_illumination", ri_delta.tolist())
    add(request_data, "lens.angular_weights", weights_delta.tolist())
    add(request_data, "sensor_angular_response.relative_response", angular_delta.tolist())
    plugin = ResponseDBRuntime()
    interval = expectation_intervals(request_data, plugin._checked(request_data), True)
    for sign in [-1, 1]:
        varied = deepcopy(request_data)
        varied.pop("parameter_uncertainty")
        varied["lens"]["relative_illumination"] = (
            np.asarray(varied["lens"]["relative_illumination"]) + sign * ri_delta
        ).tolist()
        # The two weights change together and retain a total power fraction of 1.
        weight_change = weights_delta * np.array([1, -1])
        varied["lens"]["angular_weights"] = (
            np.asarray(varied["lens"]["angular_weights"]) + sign * weight_change
        ).tolist()
        varied["sensor_angular_response"]["relative_response"] = (
            np.asarray(varied["sensor_angular_response"]["relative_response"])
            + sign * angular_delta
        ).tolist()
        charge = np.asarray(plugin.execute(varied)["summary"]["channel_electrons"])
        assert (interval["channel_electrons"].lo <= charge).all()
        assert (charge <= interval["channel_electrons"].hi).all()


def test_legacy_relative_response_alias_remains_compatible(request_data):
    add(request_data, "qe", 0.005)
    plugin = ResponseDBRuntime()
    expected = plugin.execute(request_data)["summary"]["uncertainty_budget"]
    sensor = request_data["sensor_angular_response"]
    sensor["response"] = sensor.pop("relative_response")
    actual = plugin.execute(request_data)["summary"]["uncertainty_budget"]
    assert actual == expected
