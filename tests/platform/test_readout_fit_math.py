"""Independent Poisson/voltage/quantizer evidence, not physical sensor validation."""

import json
import math
from copy import deepcopy
from fractions import Fraction
from statistics import NormalDist

import numpy as np
import pytest
from test_nonlinear_dcg import model as base_paired_model
from test_nonlinear_readout import model as base_single_model

from camera_platform import readout_fit_math as fitting


def single_model():
    model = base_single_model()
    model["full_well_e"] = 32.0
    model["adc"].update(bit_depth=10, black_level_dn=128.0)
    model["digital_gain"] = 1.3
    model["charge_voltage_response"].update(
        charge_e=[0.0, 8.0, 32.0], signal_voltage_v=[0.0, 0.12, 0.30]
    )
    model["read_noise"]["variance_v2"] = 0.005**2
    return model


def paired_model():
    model = base_paired_model()
    model["shared_charge_capacity_e"] = 32.0
    for path, capacity, voltage, digital in zip(
        model["paths"],
        [24.0, 32.0],
        [[0.0, 0.18, 0.38], [0.0, 0.09, 0.25]],
        [1.3, 0.8],
        strict=True,
    ):
        path["input_linear_ceiling_e"] = capacity
        path["digital_gain"] = digital
        path["adc"].update(bit_depth=10, black_level_dn=128.0)
        path["charge_voltage_response"].update(
            charge_e=[0.0, 8.0, capacity], signal_voltage_v=voltage
        )
    cross = 0.35 * 0.005 * 0.008
    model["read_noise"]["covariance_v2"] = [[0.005**2, cross], [cross, 0.008**2]]
    return model


def scalar_voltage(charge, curve):
    knots, voltage = curve["charge_e"], curve["signal_voltage_v"]
    index = next(i for i in range(len(knots) - 1) if charge <= knots[i + 1])
    slope = (voltage[index + 1] - voltage[index]) / (knots[index + 1] - knots[index])
    return voltage[index] + slope * (charge - knots[index])


def scalar_code(voltage, path, analog_gain):
    adc = path["adc"]
    white, black = 2 ** adc["bit_depth"] - 1, adc["black_level_dn"]
    first = max(0, min(white, round(black + voltage * analog_gain * white / adc["full_scale_v"])))
    return max(0, min(white, round(black + path["digital_gain"] * (first - black))))


def sampled_groups(model, means, *, count=512, seed=7933, analog_gain=1.5):
    """Author samples without calling a production response or capture primitive."""
    rng = np.random.default_rng(seed)
    paired = "paths" in model
    paths = model["paths"] if paired else [model]
    covariance = (
        np.asarray(model["read_noise"]["covariance_v2"])
        if paired
        else np.array([[model["read_noise"]["variance_v2"]]])
    )
    groups = []
    for mean in means:
        # Exactly one Poisson draw feeds both paired paths.
        charge = rng.poisson(mean, count)
        noise = rng.multivariate_normal(np.zeros(len(paths)), covariance, count)
        raw = np.zeros((count, len(paths)), dtype=np.int32)
        for row in range(count):
            for index, path in enumerate(paths):
                capacity = (
                    min(model["shared_charge_capacity_e"], path["input_linear_ceiling_e"])
                    if paired
                    else model["full_well_e"]
                )
                voltage = scalar_voltage(
                    min(charge[row], capacity), path["charge_voltage_response"]
                )
                gain = path["analog_gain"] if paired else analog_gain
                raw[row, index] = scalar_code(voltage + noise[row, index], path, gain)
        groups.append({"mean_charge_e": float(mean), "raw_dn": raw})
    return groups


def exhaustive_pmf(model, mean, analog_gain):
    """Enumerate latent charge atoms and every first ADC code, then regroup."""
    capacity = model["full_well_e"]
    cutoff = math.ceil(capacity)
    weights = [math.exp(-mean)]
    for charge in range(1, cutoff):
        weights.append(weights[-1] * mean / charge)
    weights.append(1.0 - math.fsum(weights))
    charges = [*range(cutoff), capacity]
    white = 2 ** model["adc"]["bit_depth"] - 1
    black = model["adc"]["black_level_dn"]
    scale = analog_gain * white / model["adc"]["full_scale_v"]
    sigma = math.sqrt(model["read_noise"]["variance_v2"])
    output = np.zeros(white + 1)
    normal = NormalDist()
    for charge, weight in zip(charges, weights, strict=True):
        voltage = scalar_voltage(charge, model["charge_voltage_response"])
        if sigma == 0:
            output[scalar_code(voltage, model, analog_gain)] += weight
            continue
        for adc_code in range(white + 1):
            lo = -math.inf if adc_code == 0 else (adc_code - 0.5 - black) / scale
            hi = math.inf if adc_code == white else (adc_code + 0.5 - black) / scale
            probability = normal.cdf((hi - voltage) / sigma) - normal.cdf((lo - voltage) / sigma)
            raw = max(0, min(white, round(black + model["digital_gain"] * (adc_code - black))))
            output[raw] += weight * probability
    return output


def perturbed_seed(model):
    seed = deepcopy(model)
    for path in seed.get("paths", [seed]):
        curve = path["charge_voltage_response"]
        increments = np.diff(curve["signal_voltage_v"]) * [0.8, 1.25]
        curve["signal_voltage_v"] = [0.0, *np.cumsum(increments).tolist()]
    if "paths" in seed:
        seed["read_noise"]["covariance_v2"] = (
            np.asarray(seed["read_noise"]["covariance_v2"]) * 1.8**2
        ).tolist()
    else:
        seed["read_noise"]["variance_v2"] *= 1.8**2
    return seed


@pytest.mark.parametrize("mean", [True, -1.0, float("nan"), float("inf")])
def test_invalid_poisson_means_are_refused(mean):
    groups = [{"mean_charge_e": mean, "raw_dn": np.array([[128]], dtype=np.int32)}]
    with pytest.raises(ValueError):
        fitting.evaluate(single_model(), groups, analog_gain=1.5)


@pytest.mark.parametrize(
    "raw",
    [
        np.ones((4, 1), dtype=bool),
        np.ones((4, 1)),
        np.ones(4, dtype=int),
        np.ones((1, 4, 1), dtype=int),
        np.ones((4, 2), dtype=int),
        np.empty((0, 1), dtype=int),
        [[128]],
        np.array([[-1]]),
        np.array([[1024]]),
    ],
)
def test_raw_requires_nonempty_integer_n_by_path_shape_and_in_range_codes(raw):
    with pytest.raises(ValueError):
        fitting.evaluate(single_model(), [{"mean_charge_e": 1.0, "raw_dn": raw}], analog_gain=1.5)


@pytest.mark.parametrize("alteration", ["unknown-group-field", "missing-mean", "empty-groups"])
def test_groups_have_an_exact_contract(alteration):
    groups = sampled_groups(single_model(), [0.0], count=4)
    if alteration == "unknown-group-field":
        groups[0]["oracle_charge_e"] = np.zeros(4)
    elif alteration == "missing-mean":
        groups[0].pop("mean_charge_e")
    else:
        groups = []
    with pytest.raises(ValueError):
        fitting.evaluate(single_model(), groups, analog_gain=1.5)


@pytest.mark.parametrize("analog_gain", [None, True, 0.0, 9.0, float("nan")])
def test_single_path_requires_an_explicit_valid_analog_gain(analog_gain):
    with pytest.raises(ValueError):
        fitting.evaluate(
            single_model(), sampled_groups(single_model(), [0.0], count=4), analog_gain=analog_gain
        )


def test_paired_path_refuses_analog_override():
    model = paired_model()
    with pytest.raises(ValueError):
        fitting.evaluate(model, sampled_groups(model, [0.0], count=4), analog_gain=1.0)


@pytest.mark.parametrize("levels", [(True,), (0.0,), (1.0,), (float("nan"),), ()])
def test_coverage_levels_are_strict_finite_interior_probabilities(levels):
    with pytest.raises(ValueError):
        fitting.evaluate(
            single_model(),
            sampled_groups(single_model(), [0.0], count=4),
            analog_gain=1.5,
            coverage_levels=levels,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slope_scale_bounds": (0.0, 2.0)},
        {"slope_scale_bounds": (2.0, 1.0)},
        {"slope_scale_bounds": (True, 2.0)},
        {"sigma_v_bounds": (0.0, 0.1)},
        {"sigma_v_bounds": (-1.0, 0.1)},
        {"sigma_v_bounds": (0.1, 0.01)},
        {"sigma_v_bounds": (1e-8, float("inf"))},
        {"max_iterations": True},
        {"max_iterations": 0},
        {"max_iterations": 1.5},
    ],
)
def test_fit_configuration_refuses_invalid_or_boolean_bounds(kwargs):
    groups = sampled_groups(single_model(), [0.0, 3.0, 14.0], count=4)
    with pytest.raises(ValueError):
        fitting.fit(single_model(), groups, analog_gain=1.5, **kwargs)


@pytest.mark.parametrize("fault", ["capacity", "knots", "groups", "observations", "terms"])
def test_explicit_resource_bounds_are_refused(fault):
    model = single_model()
    model["digital_gain"] = 1.0
    groups = [{"mean_charge_e": 0.0, "raw_dn": np.array([[128]], dtype=np.int32)}]
    if fault in {"capacity", "terms"}:
        capacity = 65537.0 if fault == "capacity" else 65536.0
        model["full_well_e"] = capacity
        model["charge_voltage_response"]["charge_e"] = [0.0, capacity / 4, capacity]
    if fault == "knots":
        model["charge_voltage_response"].update(
            charge_e=np.linspace(0, 32, 9).tolist(),
            signal_voltage_v=np.linspace(0, 0.3, 9).tolist(),
        )
    elif fault == "groups":
        groups *= 129
    elif fault == "observations":
        groups[0]["raw_dn"] = np.full((8193, 1), 128, dtype=np.int32)
    elif fault == "terms":
        groups[0]["raw_dn"] = np.arange(128, 193, dtype=np.int32)[:, None]
    with pytest.raises(ValueError):
        fitting.evaluate(model, groups, analog_gain=1.5)


@pytest.mark.parametrize(
    "means,count",
    [
        ([0.0, 12.0], 8),
        ([0.0, 0.0, 12.0], 8),
        ([0.0, 3.0, 14.0], 3),
        ([3.0, 7.0, 14.0], 8),
        ([0.0, 3.0, 7.0], 8),
    ],
)
def test_fit_refuses_unidentifiable_experimental_coverage(means, count):
    model = single_model()
    with pytest.raises(ValueError):
        fitting.fit(model, sampled_groups(model, means, count=count), analog_gain=1.5)


def test_fit_refuses_all_censored_groups_even_with_declared_mean_coverage():
    model = single_model()
    groups = [
        {
            "mean_charge_e": mean,
            "raw_dn": np.tile(np.array([[0], [1023]], dtype=np.int32), (16, 1)),
        }
        for mean in [0.0, 3.0, 14.0]
    ]
    with pytest.raises(ValueError, match="censored"):
        fitting.fit(model, groups, analog_gain=1.5)


def test_digital_gain_impossible_code_is_refused_before_fitting():
    model = single_model()
    model["digital_gain"] = 2.0
    groups = [{"mean_charge_e": 1.0, "raw_dn": np.array([[129]], dtype=np.int32)}]
    with pytest.raises(ValueError):
        fitting.evaluate(model, groups, analog_gain=1.5)


def test_zero_sigma_impossible_poisson_observation_has_explicit_zero_probability():
    model = single_model()
    model["read_noise"]["variance_v2"] = 0.0
    groups = [{"mean_charge_e": 0.0, "raw_dn": np.array([[141]], dtype=np.int32)}]
    report = fitting.evaluate(model, groups, analog_gain=1.5)
    assert report["negative_log_likelihood"] is None
    assert report["zero_model_probability_samples"] == 1
    observed = report["paths"][0]["groups"][0]["observed_bins"][0]
    assert observed["probability"] == 0.0
    assert observed["log_probability"] is None
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("capacity", [2.5, 3.0])
@pytest.mark.parametrize("digital", [0.5, 1.0, 1.5, 2.0])
@pytest.mark.parametrize("sigma", [0.0, 0.09])
def test_exhaustive_low_bit_probabilities_include_clipped_tail_and_both_quantizers(
    capacity,
    digital,
    sigma,
):
    model = single_model()
    model["full_well_e"] = capacity
    model["adc"].update(bit_depth=3, black_level_dn=1.5)
    model["digital_gain"] = digital
    model["read_noise"]["variance_v2"] = sigma**2
    model["charge_voltage_response"].update(
        charge_e=[0.0, 1.0, capacity], signal_voltage_v=[0.0, 1 / 7, 3 / 7]
    )
    expected = exhaustive_pmf(model, 2.1, 1.0)
    assert expected.sum() == pytest.approx(1.0, abs=1e-14)
    codes = np.flatnonzero(expected > 1e-14).astype(np.int32)
    groups = [{"mean_charge_e": 2.1, "raw_dn": codes[:, None]}]
    report = fitting.evaluate(model, groups, analog_gain=1.0)
    detail = report["paths"][0]["groups"][0]
    assert detail["negative_log_likelihood"] == pytest.approx(-np.log(expected[codes]).sum())
    for observed in detail["observed_bins"]:
        probability = expected[observed["raw_dn"]]
        assert observed["count"] == 1
        assert observed["probability"] == pytest.approx(probability, abs=2e-14, rel=2e-10)
        assert observed["log_probability"] == pytest.approx(math.log(probability), abs=1e-9)
    # Extreme observed codes retain first-ADC censored mass after digital attenuation.
    for interval in detail["coverage"]:
        probability = expected[interval["lower_dn"] : interval["upper_dn"] + 1].sum()
        assert interval["model_probability"] == pytest.approx(probability, abs=1e-12)
    json.dumps(report, allow_nan=False)


def test_poisson_mixture_crosses_nonlinear_knot_instead_of_converting_only_mean_charge():
    model = single_model()
    model["digital_gain"] = 1.0
    model["read_noise"]["variance_v2"] = 0.0
    expected = exhaustive_pmf(model, 8.0, 1.5)
    mean_code = scalar_code(scalar_voltage(8.0, model["charge_voltage_response"]), model, 1.5)
    # The concave conversion curve has E[f(Q)] < f(E[Q]) near its kink.
    assert mean_code - np.dot(np.arange(len(expected)), expected) > 8.0
    codes = np.flatnonzero(expected > 1e-14).astype(np.int32)
    groups = [{"mean_charge_e": 8.0, "raw_dn": codes[:, None]}]
    report = fitting.evaluate(model, groups, analog_gain=1.5)
    observed = report["paths"][0]["groups"][0]["observed_bins"]
    assert len(observed) > 10
    for item in observed:
        assert item["probability"] == pytest.approx(expected[item["raw_dn"]], abs=1e-14)
    # mean_charge_e already contains dark charge; it must not be added a second time.
    model["dark_current_e_per_s"] = 100.0
    repeated = fitting.evaluate(model, groups, analog_gain=1.5)
    assert repeated["paths"][0]["groups"][0]["observed_bins"] == observed


@pytest.mark.parametrize("paired", [False, True])
def test_independent_stochastic_data_recovers_increments_and_voltage_noise(paired):
    truth = paired_model() if paired else single_model()
    seed = perturbed_seed(truth)
    original = deepcopy(seed)
    groups = sampled_groups(truth, [0.0, 0.5, 3.0, 7.0, 14.0, 22.0])
    arrays_before = [group["raw_dn"].copy() for group in groups]
    arguments = {} if paired else {"analog_gain": 1.5}
    result = fitting.fit(seed, groups, **arguments)
    recovered, report = result["electrical_model"], result["report"]
    assert report["converged"] is True
    assert report["publication_authority"] == "none"
    assert seed == original
    for group, before in zip(groups, arrays_before, strict=True):
        np.testing.assert_array_equal(group["raw_dn"], before)
    for actual, target in zip(
        recovered.get("paths", [recovered]), truth.get("paths", [truth]), strict=True
    ):
        np.testing.assert_allclose(
            np.diff(actual["charge_voltage_response"]["signal_voltage_v"]),
            np.diff(target["charge_voltage_response"]["signal_voltage_v"]),
            rtol=0.10,
        )
        assert (
            actual["charge_voltage_response"]["charge_e"]
            == target["charge_voltage_response"]["charge_e"]
        )
        for key in ["adc", "digital_gain"] + (
            ["analog_gain", "input_linear_ceiling_e"] if paired else ["full_well_e"]
        ):
            assert actual[key] == target[key]
    if paired:
        covariance = np.asarray(recovered["read_noise"]["covariance_v2"])
        sigma = np.sqrt(np.diag(covariance))
        np.testing.assert_allclose(sigma, [0.005, 0.008], rtol=0.12)
        assert covariance[0, 1] / np.prod(sigma) == pytest.approx(0.35, abs=1e-12)
        assert recovered["shared_charge_capacity_e"] == truth["shared_charge_capacity_e"]
    else:
        assert math.sqrt(recovered["read_noise"]["variance_v2"]) == pytest.approx(0.005, rel=0.12)
    baseline = fitting.evaluate(seed, groups, **arguments)
    assert sum(path["negative_log_likelihood"] for path in report["training"]["paths"]) < sum(
        path["negative_log_likelihood"] for path in baseline["paths"]
    )
    for key in [
        "fixed_pattern",
        "dark_current_e_per_s",
        "reference_temperature_c",
        "dark_current_doubling_c",
    ]:
        assert recovered[key] == truth[key]
    json.dumps(result, allow_nan=False)


def test_independent_heldout_coverage_and_median_residual_are_calibrated():
    model = single_model()
    heldout = sampled_groups(model, [0.0, 2.0, 8.0, 18.0, 31.0], count=1200, seed=642)
    report = fitting.evaluate(model, heldout, analog_gain=1.5)
    assert report["paths"][0]["sample_count"] == 6000
    for detail in report["paths"][0]["groups"]:
        assert abs(detail["median_residual_dn"]) <= 12.0
        assert detail["median_residual_dn"] == pytest.approx(
            detail["observed_median_dn"] - detail["predicted_median_dn"]
        )
        for interval in detail["coverage"]:
            assert interval["model_probability"] >= interval["nominal_probability"] - 1e-12
            assert interval["empirical_fraction"] == pytest.approx(
                interval["model_probability"], abs=0.055
            )
            assert interval["empirical_fraction"] == interval["covered_samples"] / 1200
    json.dumps(report, allow_nan=False)


def test_paired_marginal_evaluation_is_invariant_to_row_pairing():
    model = paired_model()
    groups = sampled_groups(model, [0.0, 5.0, 14.0], count=128)
    shuffled = deepcopy(groups)
    for group in shuffled:
        group["raw_dn"][:, 1] = group["raw_dn"][::-1, 1]
    report = fitting.evaluate(model, groups)
    alternate = fitting.evaluate(model, shuffled)
    assert report["paths"] == alternate["paths"]


def test_paired_fit_refuses_undefined_inherited_correlation():
    model = paired_model()
    groups = sampled_groups(model, [0.0, 0.5, 3.0, 14.0], count=32)
    model["read_noise"]["covariance_v2"] = [[0.0, 0.0], [0.0, 0.008**2]]
    with pytest.raises(ValueError):
        fitting.fit(model, groups)


@pytest.mark.parametrize(
    "means,count", [([0.0, 3.0, 7.0], 32), ([3.0, 7.0, 14.0], 32), ([0.0, 3.0, 14.0], 3)]
)
def test_training_preflight_refuses_insufficient_data_without_optimizing(monkeypatch, means, count):
    def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must not run the optimizer")

    monkeypatch.setattr(fitting, "minimize", forbidden)
    groups = sampled_groups(single_model(), means, count=count)
    # Generic holdout/resource checks remain usable for sparse observations.
    assert fitting.preflight(single_model(), groups, analog_gain=1.5)["raw_code_samples"] > 0
    with pytest.raises(ValueError):
        fitting.training_preflight(single_model(), groups, analog_gain=1.5)


def test_training_preflight_checks_bounds_and_reports_paired_resource_count(monkeypatch):
    model = paired_model()
    groups = sampled_groups(model, [0.0, 0.5, 3.0, 7.0, 14.0, 22.0], count=32)
    monkeypatch.setattr(fitting, "minimize", lambda *a, **k: pytest.fail("Unexpected optimizer"))
    report = fitting.training_preflight(model, groups)
    assert report["sample_rows"] == 192
    assert report["raw_code_samples"] == 384
    assert len(report["training_qualification"]) == 2
    assert report["inherited_correlation"] == pytest.approx(0.35)
    assert report["objective_calls_limit_per_path"] == 420
    with pytest.raises(ValueError, match="sigma_v_bounds"):
        fitting.training_preflight(model, groups, sigma_v_bounds=(0, 0.1))


@pytest.mark.parametrize("sign", [-1, 1])
def test_rank_one_paired_correlation_has_explicit_exact_psd_representation(sign):
    model = paired_model()
    model["read_noise"]["covariance_v2"] = [[1e-4, sign * 1e-4], [sign * 1e-4, 1e-4]]
    groups = sampled_groups(model, [0.0, 0.5, 3.0, 7.0, 14.0, 22.0], count=128)
    result = fitting.fit(model, groups)
    a, cross = result["electrical_model"]["read_noise"]["covariance_v2"][0]
    b = result["electrical_model"]["read_noise"]["covariance_v2"][1][1]
    assert Fraction(a) * Fraction(b) >= Fraction(cross) ** 2
    detail = result["report"]["covariance_representation"]
    assert detail["jitter_added"] is False
    assert 0 <= detail["toward_zero_ulp_steps"] <= 4
    assert detail["represented_correlation"] == pytest.approx(sign, abs=1e-14)
    assert result["report"]["correlation_fitted"] is False


def test_log_likelihood_retains_gaussian_tail_when_probability_underflows():
    model = single_model()
    model["read_noise"]["variance_v2"] = 1e-12
    model["digital_gain"] = 1.0
    group = {"mean_charge_e": 0.0, "raw_dn": np.array([[1022]], dtype=np.int32)}
    report = fitting.evaluate(model, [group], analog_gain=1.5)
    item = report["paths"][0]["groups"][0]["observed_bins"][0]
    assert item["probability"] == 0.0  # Float64 display underflow; finite log mass is retained.
    assert item["log_probability"] < -1e8
    assert math.isfinite(report["negative_log_likelihood"])
    assert report["zero_model_probability_samples"] == 0


def test_analytic_mixture_gradient_matches_independent_central_difference():
    model = single_model()
    groups = sampled_groups(model, [3.0], count=32)
    _, paths, gains, prepared, _ = fitting._prepare(model, groups, 1.5)
    path, group = paths[0], prepared[0]
    support = fitting._support(path, group["mean"])
    parameters = np.array([1.04, 0.96, math.log(0.006)])

    def value(point):
        logp, gradient = fitting._code_probabilities(
            path,
            gains[0],
            group["bins"][0],
            support,
            point[:-1],
            math.exp(point[-1]),
            gradient=True,
        )
        return logp.sum(), gradient.sum(axis=0)

    _, actual = value(parameters)
    expected = []
    for index in range(len(parameters)):
        delta = np.zeros(len(parameters))
        delta[index] = 1e-6
        expected.append((value(parameters + delta)[0] - value(parameters - delta)[0]) / 2e-6)
    np.testing.assert_allclose(actual, expected, rtol=2e-6, atol=2e-6)


def test_training_preflight_preserves_tiny_positive_covariance_correlation():
    model = paired_model()
    groups = sampled_groups(model, [0.0, 0.5, 3.0, 7.0, 14.0, 22.0], count=32)
    model["read_noise"]["covariance_v2"] = [[1e-300, 0.35e-300], [0.35e-300, 1e-300]]
    report = fitting.training_preflight(model, groups)
    assert report["inherited_correlation"] == pytest.approx(0.35)


@pytest.mark.parametrize(
    "sigmas", [[0, 0.1], [True, 0.1], [float("nan"), 0.1], [0.1], [1e-16, 0.1]]
)
def test_public_covariance_reconstruction_refuses_unsupported_sigmas(sigmas):
    with pytest.raises(ValueError, match="sigmas"):
        fitting.fitted_covariance(paired_model(), sigmas)


def test_public_covariance_helpers_match_published_representation():
    model = paired_model()
    result = fitting.fit(model, sampled_groups(model, [0, 0.5, 3, 7, 14, 22], count=32))
    expected = fitting.fitted_covariance(model, [p["sigma_v"] for p in result["report"]["paths"]])
    assert fitting.inherited_correlation(model) == result["report"]["inherited_correlation"]
    assert expected["covariance_v2"] == result["electrical_model"]["read_noise"]["covariance_v2"]
    assert expected["representation"] == result["report"]["covariance_representation"]
