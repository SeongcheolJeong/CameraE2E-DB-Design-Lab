"""Bounded conditional fits of the existing quantized Poisson/voltage model.

Charge means are independently supplied TOTAL pre-well Poisson means. Dark is
already included. Black, ADC, gains, capacities, charge knots and dark parameters
are fixed. Paired paths use a marginal composite objective; correlation is an
inherited assumption, never estimated by this module. No physical acceptance is
implied by optimizer convergence or by empirical marginal coverage.
"""

from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from fractions import Fraction

import numpy as np
from scipy.optimize import minimize
from scipy.special import log_ndtr, logsumexp
from scipy.stats import poisson

from . import charge_voltage_response as response
from . import nonlinear_dcg, nonlinear_readout
from .registry import canonical, digest

MAX_GROUPS = 128
MAX_OBSERVATIONS = 8192
MAX_CAPACITY_E = 65536
MAX_KNOTS = 8
MAX_LIKELIHOOD_TERMS = 4_000_000
MAX_CDF_TERMS = 32_000_000
CHUNK_TERMS = 131_072
_LOG_SQRT_2PI = math.log(2 * math.pi) / 2
_QUAD_NODES, _QUAD_WEIGHTS = np.polynomial.legendre.leggauss(8)


def _number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} requires a finite number in [{low}, {high}]")
    return float(value)


def _bounds(value, name, low, high):
    if type(value) not in (list, tuple) or len(value) != 2:
        raise ValueError(f"{name} requires two bounds")
    result = [_number(v, name, low, high) for v in value]
    if result[0] >= result[1]:
        raise ValueError(f"{name} requires strictly increasing bounds")
    return result


def _prepare(model, groups, analog_gain):
    if type(model) is not dict:
        raise ValueError("Electrical model must be a dictionary")
    paired = model.get("schema") == nonlinear_dcg.SCHEMA
    normalized = (
        nonlinear_dcg.validate_model(model) if paired else nonlinear_readout.validate_model(model)
    )
    if paired:
        if analog_gain is not None:
            raise ValueError("Paired fitting uses its fixed path analog gains")
        paths = [nonlinear_dcg._path_model(normalized, i) for i in range(2)]
        gains = [p["analog_gain"] for p in normalized["paths"]]
    else:
        gains = [_number(analog_gain, "analog_gain", 1e-6, 1e6)]
        paths = [normalized]
    for path, gain in zip(paths, gains, strict=True):
        curve = path["charge_voltage_response"]
        if path["full_well_e"] > MAX_CAPACITY_E or len(curve["charge_e"]) > MAX_KNOTS:
            raise ValueError("Fitting capacity or knot resource limit exceeded")
        if (
            not curve["valid_domain"]["analog_gain"][0]
            <= gain
            <= curve["valid_domain"]["analog_gain"][1]
        ):
            raise ValueError("Analog gain is outside response domain")
        if (
            not path["read_noise"]["analog_gain_range"][0]
            <= gain
            <= path["read_noise"]["analog_gain_range"][1]
        ):
            raise ValueError("Analog gain is outside read-noise domain")
    if type(groups) is not list or not 1 <= len(groups) <= MAX_GROUPS:
        raise ValueError("Require 1..128 charge groups")
    seen, rows, code_samples, terms, cdf_terms, prepared = set(), 0, 0, 0, 0, []
    input_hash = hashlib.sha256()
    for group in groups:
        if type(group) is not dict or set(group) != {"mean_charge_e", "raw_dn"}:
            raise ValueError("Charge groups require exact mean_charge_e and raw_dn fields")
        mean = _number(group["mean_charge_e"], "mean_charge_e", 0, response.MAX_CHARGE_E)
        if mean in seen:
            raise ValueError("Duplicate charge means must be explicitly grouped")
        seen.add(mean)
        raw = group["raw_dn"]
        if (
            type(raw) is not np.ndarray
            or raw.dtype.kind not in "iu"
            or raw.ndim != 2
            or raw.shape[1] != len(paths)
            or raw.shape[0] == 0
        ):
            raise ValueError("RAW groups require integer [N,paths] arrays")
        rows += raw.shape[0]
        code_samples += raw.size
        if code_samples > MAX_OBSERVATIONS:
            raise ValueError("RAW code sample resource limit exceeded")
        input_hash.update(canonical([mean, list(raw.shape)]).encode())
        bins = []
        for index, (path, gain) in enumerate(zip(paths, gains, strict=True)):
            adc = path["adc"]
            white = 2 ** adc["bit_depth"] - 1
            if np.any(raw[:, index] < 0) or np.any(raw[:, index] > white):
                raise ValueError("RAW codes outside ADC range")
            codes, counts = np.unique(raw[:, index], return_counts=True)
            codes = codes.astype(np.int64)
            lo, hi = nonlinear_readout._adc_preimage(
                codes, adc["black_level_dn"], white, path["digital_gain"]
            )
            if np.any(lo > hi):
                raise ValueError("Impossible digital quantizer output code")
            unit = adc["full_scale_v"] / white / gain
            low = np.where(lo == 0, -np.inf, (lo - 0.5 - adc["black_level_dn"]) * unit)
            high = np.where(hi == white, np.inf, (hi + 0.5 - adc["black_level_dn"]) * unit)
            support = math.ceil(path["full_well_e"]) + 1
            terms += support * len(codes)
            # Five binary-search quantiles plus four endpoint CDFs for default coverage.
            cdf_terms += support * (5 * adc["bit_depth"] + 4)
            bins.append(
                {
                    "codes": codes,
                    "counts": counts,
                    "low": low,
                    "high": high,
                    "censored": (lo == 0) | (hi == white),
                }
            )
        input_hash.update(np.ascontiguousarray(raw, dtype="<u4").tobytes())
        prepared.append({"mean": mean, "raw": raw, "bins": bins})
    if terms > MAX_LIKELIHOOD_TERMS or cdf_terms > MAX_CDF_TERMS:
        raise ValueError("Finite-support likelihood/CDF resource budget exceeded")
    resources = {
        "schema": "camera_readout_fit_resources_v1",
        "group_count": len(groups),
        "path_count": len(paths),
        "sample_rows": rows,
        "raw_code_samples": code_samples,
        "likelihood_terms_per_evaluation": terms,
        "default_evaluation_cdf_terms_bound": cdf_terms,
        "max_capacity_e": max(p["full_well_e"] for p in paths),
        "maximum_chunk_terms": CHUNK_TERMS,
        "raw_input_sha256": input_hash.hexdigest(),
        "model_sha256": digest(normalized),
    }
    return normalized, paths, gains, prepared, resources


def preflight(model, groups, *, analog_gain=None):
    """Validate bounded data/settings without optimizing or allocating support matrices.

    This checks resources, not training identifiability. ``sample_rows`` counts
    paired acquisition-site rows; ``raw_code_samples`` counts path observations.
    Statistical independence must be established by the calling evidence layer.
    """
    return _prepare(model, groups, analog_gain)[-1]


def _support(path, mean):
    capacity = path["full_well_e"]
    first_capped = math.ceil(capacity)
    charge = np.r_[np.arange(first_capped, dtype=float), capacity]
    log_mass = np.r_[
        poisson.logpmf(np.arange(first_capped), mean), poisson.logsf(first_capped - 1, mean)
    ]
    mass_error = abs(float(np.exp(logsumexp(log_mass))) - 1.0)
    if mass_error > 1e-8:
        raise ValueError("Finite Poisson mass failed numerical normalization check")
    # No truncation or renormalization: retain every integer charge and capped tail.
    curve = path["charge_voltage_response"]
    q, v = np.asarray(curve["charge_e"]), np.asarray(curve["signal_voltage_v"])
    basis = np.clip(charge[:, None] - q[:-1], 0, np.diff(q)) * (np.diff(v) / np.diff(q))
    return charge, log_mass, basis, mass_error


def _log_intervals(low, high, means, sigma):
    lower = (low[:, None] - means) / sigma
    upper = (high[:, None] - means) / sigma
    reflect = lower >= 0
    large = log_ndtr(np.where(reflect, -lower, upper))
    small = log_ndtr(np.where(reflect, -upper, lower))
    with np.errstate(divide="ignore", invalid="ignore"):
        result = large + np.log(-np.expm1(small - large))
    # Very short finite intervals lose relative precision in even a log-CDF
    # subtraction. Bounded Gaussian quadrature over that interval avoids it.
    finite_rows = np.isfinite(low) & np.isfinite(high)
    width = np.full((len(low), 1), np.inf)
    midpoint = np.zeros_like(result)
    width[finite_rows, 0] = (high[finite_rows] - low[finite_rows]) / sigma
    midpoint[finite_rows] = (
        low[finite_rows, None] + (high[finite_rows] - low[finite_rows])[:, None] / 2 - means
    ) / sigma
    short = finite_rows[:, None] & (width * (1 + abs(midpoint)) < 1e-3)
    if np.any(short):
        widths = np.broadcast_to(width, result.shape)[short]
        mids = midpoint[short]
        points = mids[:, None] + widths[:, None] * _QUAD_NODES / 2
        result[short] = (
            np.log(widths / 2)
            - _LOG_SQRT_2PI
            + logsumexp(np.log(_QUAD_WEIGHTS) - points**2 / 2, axis=1)
        )
    return result, lower, upper


def _code_probabilities(path, gain, bins, support, scales, sigma, *, gradient=False):
    charge, log_mass, basis, _ = support
    voltage = basis @ scales
    if sigma == 0:
        codes = nonlinear_readout.encode_voltage(voltage, path, gain)["raw_dn"]
        log_prob = np.array([logsumexp(log_mass[codes == code]) for code in bins["codes"]])
        return log_prob, None
    probabilities, gradients = [], []
    chunk = max(1, CHUNK_TERMS // len(charge))
    for start in range(0, len(bins["codes"]), chunk):
        selected = slice(start, start + chunk)
        log_bin, lower, upper = _log_intervals(
            bins["low"][selected], bins["high"][selected], voltage, sigma
        )
        log_probability = logsumexp(log_bin + log_mass, axis=1)
        if not np.isfinite(log_probability).all():
            raise ValueError("Gaussian mixture probability could not be resolved numerically")
        probabilities.append(log_probability)
        if gradient:
            with np.errstate(over="ignore", under="ignore", invalid="ignore"):
                low_density = np.exp(
                    log_mass - lower**2 / 2 - _LOG_SQRT_2PI - log_probability[:, None]
                )
                high_density = np.exp(
                    log_mass - upper**2 / 2 - _LOG_SQRT_2PI - log_probability[:, None]
                )
                mean_gradient = (low_density - high_density) @ basis / sigma
                sigma_gradient = np.sum(
                    np.where(np.isfinite(lower), lower, 0) * low_density
                    - np.where(np.isfinite(upper), upper, 0) * high_density,
                    axis=1,
                )
            part = np.column_stack((mean_gradient, sigma_gradient))
            if not np.isfinite(part).all():
                raise ValueError("Mixture derivative is not numerically finite")
            gradients.append(part)
    return np.concatenate(probabilities), np.concatenate(gradients) if gradient else None


def _cdf(path, gain, support, sigma, code):
    white = 2 ** path["adc"]["bit_depth"] - 1
    if code < 0:
        return 0.0
    if code >= white:
        return 1.0
    _, log_mass, basis, _ = support
    voltage = basis.sum(axis=1)
    if sigma == 0:
        encoded = nonlinear_readout.encode_voltage(voltage, path, gain)["raw_dn"]
        return float(np.exp(logsumexp(log_mass[encoded <= code])))
    adc = path["adc"]
    _, hi = nonlinear_readout._adc_preimage(
        np.asarray([code]), adc["black_level_dn"], white, path["digital_gain"]
    )
    if hi[0] < 0:
        return 0.0
    if hi[0] >= white:
        return 1.0
    threshold = (hi[0] + 0.5 - adc["black_level_dn"]) * adc["full_scale_v"] / white / gain
    return float(
        np.clip(np.exp(logsumexp(log_mass + log_ndtr((threshold - voltage) / sigma))), 0, 1)
    )


def _quantile(path, gain, support, sigma, probability):
    low, high = 0, 2 ** path["adc"]["bit_depth"] - 1
    while low < high:
        mid = (low + high) // 2
        if _cdf(path, gain, support, sigma, mid) >= probability:
            high = mid
        else:
            low = mid + 1
    return low


def _assumptions(paired):
    return {
        "charge": "supplied total pre-full-well Poisson mean; dark already included",
        "support": "all integer charges below effective capacity plus exact capped tail mass",
        "read_noise": "zero-mean Gaussian voltage after response before analog gain",
        "quantization": "two actual nearest-even quantizers; exact integer preimages; "
        "half-infinite intervals for censored ADC endpoints",
        "numerics": "float64 Poisson/log-CDF mixture; short Gaussian intervals use 8-point "
        "quadrature; no omitted Poisson tail or invented quantization variance",
        "objective": "paired marginal composite negative log likelihood"
        if paired
        else "univariate negative log likelihood",
        "paired_independence": "not_assumed" if paired else "not_applicable",
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
        "anchor_uncertainty": "conditional on supplied means; external uncertainty not propagated",
    }


def evaluate(model, groups, *, analog_gain=None, coverage_levels=(0.68, 0.95)):
    """Evaluate marginal observed-bin likelihood and discrete central coverage.

    Quantized predictive medians are exact numerical CDF inversions. Mean RAW is
    observed only: a predictive mean is deliberately not approximated. Empirical
    fractions are descriptive, not binomial intervals or a joint DCG guarantee.
    """
    normalized, paths, gains, prepared, resources = _prepare(model, groups, analog_gain)
    if type(coverage_levels) not in (tuple, list) or not 1 <= len(coverage_levels) <= 4:
        raise ValueError("Require 1..4 marginal coverage levels")
    levels = [_number(v, "coverage", 1e-6, 1 - 1e-6) for v in coverage_levels]
    if len(set(levels)) != len(levels):
        raise ValueError("Coverage levels must be unique")
    cdf_terms = sum(
        (math.ceil(p["full_well_e"]) + 1)
        * ((1 + 2 * len(levels)) * p["adc"]["bit_depth"] + 2 * len(levels))
        for p in paths
    ) * len(prepared)
    if cdf_terms > MAX_CDF_TERMS:
        raise ValueError("Coverage CDF resource budget exceeded")
    result = {
        "schema": "camera_readout_fit_evaluation_v1",
        "resources": resources,
        "assumptions": _assumptions(len(paths) == 2),
        "paths": [],
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "predicted_mean_dn": None,
        "mean_not_available_reason": "Exact quantized mean requires a separate full-code "
        "integration budget; report uses predictive medians.",
    }
    total_nll, impossible = 0.0, 0
    for index, (path, gain) in enumerate(zip(paths, gains, strict=True)):
        sigma = math.sqrt(path["read_noise"]["variance_v2"])
        path_report = {
            "readout_id": ["HCG", "LCG"][index] if len(paths) == 2 else "single",
            "sigma_v": sigma,
            "groups": [],
            "sample_count": resources["sample_rows"],
        }
        path_nll, path_impossible = 0.0, 0
        for group in prepared:
            support = _support(path, group["mean"])
            bins = group["bins"][index]
            logp, _ = _code_probabilities(
                path,
                gain,
                bins,
                support,
                np.ones(len(path["charge_voltage_response"]["charge_e"]) - 1),
                sigma,
            )
            bad = ~np.isfinite(logp)
            bad_count = int(bins["counts"][bad].sum())
            nll = None if bad_count else float(-np.dot(bins["counts"], logp))
            path_impossible += bad_count
            path_nll += nll if nll is not None else 0.0
            raw = group["raw"][:, index]
            median = _quantile(path, gain, support, sigma, 0.5)
            row = {
                "mean_charge_e": group["mean"],
                "sample_count": len(raw),
                "observed_mean_dn": float(raw.mean()),
                "observed_median_dn": float(np.median(raw)),
                "predicted_median_dn": median,
                "median_residual_dn": float(np.median(raw) - median),
                "negative_log_likelihood": nll,
                "zero_model_probability_samples": bad_count,
                "poisson_mass_roundoff": support[3],
                "observed_bins": [
                    {
                        "raw_dn": int(code),
                        "count": int(count),
                        "probability": float(np.exp(log_probability)),
                        "log_probability": float(log_probability)
                        if np.isfinite(log_probability)
                        else None,
                    }
                    for code, count, log_probability in zip(
                        bins["codes"], bins["counts"], logp, strict=True
                    )
                ],
                "coverage": [],
            }
            for level in levels:
                low = _quantile(path, gain, support, sigma, (1 - level) / 2)
                high = _quantile(path, gain, support, sigma, (1 + level) / 2)
                count = int(np.count_nonzero((raw >= low) & (raw <= high)))
                row["coverage"].append(
                    {
                        "nominal_probability": level,
                        "lower_dn": low,
                        "upper_dn": high,
                        "model_probability": _cdf(path, gain, support, sigma, high)
                        - _cdf(path, gain, support, sigma, low - 1),
                        "empirical_fraction": count / len(raw),
                        "covered_samples": count,
                    }
                )
            path_report["groups"].append(row)
        path_report.update(
            negative_log_likelihood=None if path_impossible else path_nll,
            zero_model_probability_samples=path_impossible,
        )
        result["paths"].append(path_report)
        total_nll += path_nll
        impossible += path_impossible
    result["negative_log_likelihood"] = None if impossible else total_nll
    result["zero_model_probability_samples"] = impossible
    return result


def _fit_data(path, groups, index):
    means = np.array([g["mean"] for g in groups])
    q = path["charge_voltage_response"]["charge_e"]
    if len(means) < max(3, len(q)) or any(len(g["raw"]) < 4 for g in groups):
        raise ValueError("Fit requires repeated samples and at least segments+1 distinct levels")
    low_groups = [g for g in groups if g["mean"] <= 1 and len(g["raw"]) >= 16]
    if not low_groups or not any(len(g["bins"][index]["codes"]) > 1 for g in low_groups):
        raise ValueError("Fit requires varied low-charge (<=1e) data with at least 16 repeats")
    if all(g["bins"][index]["censored"].all() for g in groups):
        raise ValueError("All observed codes are censored; response is not identified")
    for low, high in zip(q[:-1], q[1:], strict=True):
        if not any(low < mean < min(high, path["full_well_e"]) for mean in means):
            raise ValueError("Undercovered response segment: require an interior anchored mean")
    supports = [_support(path, g["mean"]) for g in groups]
    design = np.array([np.exp(s[1]) @ s[2] for s in supports])
    norms = np.linalg.norm(design, axis=0)
    if np.any(norms == 0):
        raise ValueError("Rank-deficient response support")
    singular = np.linalg.svd(design / norms, compute_uv=False)
    if singular[-1] <= singular[0] * 1e-8:
        raise ValueError("Rank-deficient or poorly conditioned anchored response levels")
    return supports, float(singular[0] / singular[-1])


def _parameter_bounds(path, scales_bound, sigma_bound):
    curve = path["charge_voltage_response"]
    slopes = np.diff(curve["signal_voltage_v"]) / np.diff(curve["charge_e"])
    lower = np.maximum(scales_bound[0], response.MIN_SLOPE_V_PER_E / slopes)
    upper = np.minimum(scales_bound[1], response.MAX_SLOPE_V_PER_E / slopes)
    if (
        np.any(lower >= upper)
        or curve["signal_voltage_v"][-1] * scales_bound[1] > response.MAX_VOLTAGE_V
    ):
        raise ValueError("Requested increment bounds exceed supported response domain")
    return list(zip(lower, upper, strict=True)) + [tuple(np.log(sigma_bound))]


def _correlation(model, paths):
    if len(paths) == 1:
        return None
    covariance = model["read_noise"]["covariance_v2"]
    if covariance[0][0] <= 0 or covariance[1][1] <= 0:
        raise ValueError("Inherited paired correlation is undefined with zero base variance")
    denominator = math.sqrt(covariance[0][0]) * math.sqrt(covariance[1][1])
    ratio = covariance[0][1] / denominator
    # Exact PSD was checked by the model validator. Only bound final division
    # roundoff at rank one; multiplying the variances first can underflow.
    return math.copysign(min(abs(ratio), 1.0), ratio)


def inherited_correlation(model):
    """Return the externally inherited correlation from a strict paired model."""
    normalized = nonlinear_dcg.validate_model(model)
    return _correlation(normalized, normalized["paths"])


def fitted_covariance(model, sigmas):
    """Represent inherited correlation at fitted sigmas with explicit PSD rounding.

    No jitter or correlation estimation is performed. The report allows readers
    to reconstruct the exact published binary64 covariance independently of fit.
    """
    correlation = inherited_correlation(model)
    if (
        not isinstance(sigmas, (list, tuple))
        or len(sigmas) != 2
        or any(
            type(s) not in (int, float) or not math.isfinite(s) or not 1e-15 <= s <= 1000
            for s in sigmas
        )
    ):
        raise ValueError("Fitted covariance requires two finite supported positive sigmas")
    cross = correlation * sigmas[0] * sigmas[1]
    original_cross = cross
    diagonal = [s**2 for s in sigmas]
    steps = 0
    while Fraction(diagonal[0]) * Fraction(diagonal[1]) < Fraction(cross) ** 2:
        cross = math.nextafter(cross, 0.0)
        steps += 1
        if steps > 4:
            raise ValueError("Inherited correlation cannot be represented with bounded rounding")
    return {
        "covariance_v2": [[diagonal[0], cross], [cross, diagonal[1]]],
        "representation": {
            "cross_before_rounding_v2": original_cross,
            "cross_published_v2": cross,
            "toward_zero_ulp_steps": steps,
            "jitter_added": False,
            "represented_correlation": cross / (math.sqrt(diagonal[0]) * math.sqrt(diagonal[1])),
        },
    }


def training_preflight(
    model,
    groups,
    *,
    analog_gain=None,
    slope_scale_bounds=(0.5, 2.0),
    sigma_v_bounds=(1e-8, 0.1),
    max_iterations=100,
):
    """Run every fit resource/data/bound qualification before a job is queued.

    Bounded full-support expectations are evaluated for the rank check, but no
    optimization is performed. Generic preflight deliberately permits sparse
    heldout data; this training-specific entry point does not.
    """
    normalized, paths, _, prepared, resources = _prepare(model, groups, analog_gain)
    scale = _bounds(slope_scale_bounds, "slope_scale_bounds", 1e-6, 1e6)
    sigma = _bounds(sigma_v_bounds, "sigma_v_bounds", 1e-15, 1000)
    if type(max_iterations) is not int or not 1 <= max_iterations <= 300:
        raise ValueError("max_iterations must be an integer in 1..300")
    correlation = _correlation(normalized, paths)
    qualification = []
    for index, path in enumerate(paths):
        _parameter_bounds(path, scale, sigma)
        _, condition = _fit_data(path, prepared, index)
        qualification.append(
            {
                "readout_id": ["HCG", "LCG"][index] if len(paths) == 2 else "single",
                "mean_response_design_condition": condition,
            }
        )
    return {
        **resources,
        "training_qualification": qualification,
        "inherited_correlation": correlation,
        "objective_calls_limit_per_path": max_iterations * 4 + 20,
        "maximum_optimizer_likelihood_terms": (
            resources["likelihood_terms_per_evaluation"] * (max_iterations * 4 + 20)
        ),
    }


def fit(
    model,
    groups,
    *,
    analog_gain=None,
    slope_scale_bounds=(0.5, 2.0),
    sigma_v_bounds=(1e-8, 0.1),
    max_iterations=100,
):
    """Fit relative positive segment increments and read sigma conditionally.

    A converged result is an estimated candidate with no publication authority.
    Low-noise bounds and inherited correlation are exposed explicitly. Training
    sufficiency checks are conservative first-version rules, not general proofs
    of physical or statistical identifiability.
    """
    resources = training_preflight(
        model,
        groups,
        analog_gain=analog_gain,
        slope_scale_bounds=slope_scale_bounds,
        sigma_v_bounds=sigma_v_bounds,
        max_iterations=max_iterations,
    )
    normalized, paths, gains, prepared, _ = _prepare(model, groups, analog_gain)
    scales_bound = _bounds(slope_scale_bounds, "slope_scale_bounds", 1e-6, 1e6)
    sigma_bound = _bounds(sigma_v_bounds, "sigma_v_bounds", 1e-15, 1000)
    if type(max_iterations) is not int or not 1 <= max_iterations <= 300:
        raise ValueError("max_iterations must be an integer in 1..300")
    correlation = _correlation(normalized, paths)
    candidate, reports, sigmas = deepcopy(normalized), [], []
    for index, (path, gain) in enumerate(zip(paths, gains, strict=True)):
        supports, condition = _fit_data(path, prepared, index)
        curve = path["charge_voltage_response"]
        bounds = _parameter_bounds(path, scales_bound, sigma_bound)
        lower, upper = np.array(bounds[:-1]).T
        segment_count = len(lower)
        initial_sigma = np.clip(math.sqrt(path["read_noise"]["variance_v2"]), *sigma_bound)
        initial = np.r_[np.clip(np.ones(segment_count), lower, upper), np.log(initial_sigma)]
        call_count = [0]

        def objective(
            parameters, supports=supports, index=index, path=path, gain=gain, call_count=call_count
        ):
            call_count[0] += 1
            if call_count[0] > resources["objective_calls_limit_per_path"]:
                raise ValueError("Optimizer exhausted its explicit likelihood evaluation budget")
            sigma = math.exp(parameters[-1])
            value, derivative = 0.0, np.zeros(len(parameters))
            for group, support in zip(prepared, supports, strict=True):
                bins = group["bins"][index]
                logp, gradient = _code_probabilities(
                    path, gain, bins, support, parameters[:-1], sigma, gradient=True
                )
                value -= float(np.dot(bins["counts"], logp))
                derivative -= bins["counts"] @ gradient
            # Scaling changes no optimum and makes stopping criteria sample-count independent.
            return value / resources["sample_rows"], derivative / resources["sample_rows"]

        optimized = minimize(
            objective,
            initial,
            jac=True,
            bounds=bounds,
            method="L-BFGS-B",
            options={
                "maxiter": max_iterations,
                "maxfun": resources["objective_calls_limit_per_path"],
                "ftol": 1e-12,
                "gtol": 1e-7,
                "maxls": 25,
            },
        )
        if not np.isfinite(optimized.x).all() or not math.isfinite(float(optimized.fun)):
            raise ValueError("Optimizer returned nonfinite candidate")
        fitted_curve = deepcopy(curve)
        fitted_curve["signal_voltage_v"] = np.r_[
            0.0, np.cumsum(np.diff(curve["signal_voltage_v"]) * optimized.x[:-1])
        ].tolist()
        fitted_curve["source"] = {
            "kind": "estimated",
            "description": "Conditional quantized Poisson/Gaussian response fit; "
            "external charge/ADC/dark assumptions retained.",
        }
        fitted_curve = response.validate_response(fitted_curve)
        sigma = math.exp(optimized.x[-1])
        sigmas.append(sigma)
        if len(paths) == 1:
            candidate["charge_voltage_response"] = fitted_curve
            candidate["read_noise"]["variance_v2"] = sigma**2
        else:
            candidate["paths"][index]["charge_voltage_response"] = fitted_curve
        at_bounds = [
            name
            for name, value, (lo, hi) in zip(
                [f"slope_scale_{j}" for j in range(segment_count)] + ["log_sigma_v"],
                optimized.x,
                bounds,
                strict=True,
            )
            if min(abs(value - lo), abs(value - hi)) <= 1e-5 * max(1.0, abs(hi - lo))
        ]
        reports.append(
            {
                "readout_id": ["HCG", "LCG"][index] if len(paths) == 2 else "single",
                "converged": bool(optimized.success),
                "optimizer_status": int(optimized.status),
                "optimizer_message": str(optimized.message),
                "iterations": int(optimized.nit),
                "objective_evaluations": int(optimized.nfev),
                "slope_scales": optimized.x[:-1].tolist(),
                "sigma_v": sigma,
                "boundary_parameters": at_bounds,
                "gradient_infinity_norm": float(np.max(np.abs(optimized.jac))),
                "mean_response_design_condition": condition,
                "identifiability": "bounded data/segment/rank checks only; no confidence region",
            }
        )
    covariance_rounding = None
    if correlation is not None:
        covariance = fitted_covariance(normalized, sigmas)
        candidate["read_noise"]["covariance_v2"] = covariance["covariance_v2"]
        covariance_rounding = covariance["representation"]
    candidate["read_noise"]["source"] = {
        "kind": "estimated",
        "description": "Conditional marginal Gaussian read-sigma estimate; "
        "paired correlation, if present, inherited and not fitted.",
    }
    candidate["source"] = {
        "kind": "estimated",
        "description": "Fitted candidate conditional on supplied anchored Poisson means "
        "and frozen ADC/gain/dark/capacity; physical validation unestablished.",
    }
    candidate = (
        nonlinear_dcg.validate_model(candidate)
        if len(paths) == 2
        else nonlinear_readout.validate_model(candidate)
    )
    report = {
        "schema": "camera_readout_fit_math_v1",
        "resources": resources,
        "converged": all(p["converged"] for p in reports),
        "paths": reports,
        "assumptions": _assumptions(len(paths) == 2),
        "inherited_correlation": correlation,
        "covariance_representation": covariance_rounding,
        "correlation_fitted": False,
        "joint_coverage": "not_claimed",
        "publication_authority": "none",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "optimizer": "bounded L-BFGS-B analytic gradient",
        "global_optimum": "not_guaranteed",
        "base_model_sha256": digest(normalized),
        "candidate_model_sha256": digest(candidate),
        "fixed_fields": [
            "ADC",
            "black",
            "analog/digital gains",
            "charge knots",
            "capacities",
            "dark parameters",
            "operating domains",
            "paired correlation assumption",
        ],
        "training": evaluate(candidate, groups, analog_gain=analog_gain),
    }
    return {"electrical_model": candidate, "report": report}
