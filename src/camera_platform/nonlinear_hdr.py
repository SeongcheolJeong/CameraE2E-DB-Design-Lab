"""Sequential HDR from calibrated nonlinear RAW; no simulator truth inputs."""

from __future__ import annotations

import hashlib

import numpy as np

from . import hdr, nonlinear_readout

SCHEMA = "camera_hdr_merge_v2"
METHOD = "inverse_local_variance_nonlinear_raw"
MAX_FRAME_PIXELS = hdr.MAX_FRAME_PIXELS


def default_profile():
    return {**hdr.default_profile(), "schema": SCHEMA, "method": METHOD}


def validate_profile(profile):
    hdr._exact(profile, set(default_profile()), "nonlinear HDR profile")
    if profile["schema"] != SCHEMA or profile["method"] != METHOD:
        raise ValueError("Unsupported nonlinear HDR profile")
    normalized = hdr.validate_profile(
        {**profile, "schema": hdr.SCHEMA, "method": "inverse_variance_raw"}
    )
    return {**normalized, "schema": SCHEMA, "method": METHOD}


def merge_raw_bracket(raw, exposure_s, calibrations, *, geometry, profile):
    if (
        not isinstance(raw, np.ndarray)
        or raw.ndim != 3
        or raw.dtype.kind not in "iu"
        or not 1 <= raw.shape[0] <= 64
        or any(v < 2 or v > 4096 or v % 2 for v in raw.shape[1:])
        or raw.size > MAX_FRAME_PIXELS
    ):
        raise ValueError("RAW requires integer [1..64,even H,even W] within 1048576 pixels")
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    count, rows, cols = raw.shape
    exposures = hdr._array(exposure_s, (count,), "exposure_s")
    if np.any(exposures <= 0):
        raise ValueError("Every HDR exposure must be positive")
    if not isinstance(calibrations, (list, tuple)) or len(calibrations) != count:
        raise ValueError("One nonlinear calibration is required per RAW exposure")
    selected = validate_profile(profile)
    geom, pattern = hdr._geometry(geometry, (rows, cols))
    rates, variances, bounds, validity, knot_flags, summaries, cal_summaries = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    native = {k: geom[k] for k in ("full_sensor_shape", "crop_origin")}
    for index, (calibration, exposure) in enumerate(zip(calibrations, exposures, strict=True)):
        cal = nonlinear_readout.validate_calibration(calibration, (rows, cols))
        if cal["acquisition"]["exposure_s"] != exposure or cal["acquisition"]["geometry"] != native:
            raise ValueError("HDR exposure or native geometry differs from fixed calibration")
        decoded = nonlinear_readout.decode_raw(
            raw[index],
            cal,
            guard_sigma=selected["read_noise_guard_sigma"],
            saturation_margin_dn=selected["saturation_margin_dn"],
        )
        d = decoded["arrays"]
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            rate = (d["total_charge_e"] - cal["dark_e"]) / exposure
            variance = d["stochastic_variance_estimate_e2"] / exposure / exposure
            bound = d["quantization_bound_e"] / exposure
        valid = d["valid_mask"] & np.isfinite(rate) & np.isfinite(variance) & np.isfinite(bound)
        for value in (rate, variance, bound):
            value[~valid] = np.nan
        rates.append(rate)
        variances.append(variance)
        bounds.append(bound)
        validity.append(valid)
        knot_flags.append(d["quantization_knot_crossing_mask"])
        summaries.append({"frame_index": index, **decoded["summary"]})
        cal_summaries.append(
            {
                "schema": cal["schema"],
                "model": cal["model"],
                "model_sha256": cal["model_sha256"],
                "acquisition": cal["acquisition"],
                "acquisition_sha256": cal["acquisition_sha256"],
                "dark_e_shape": [rows, cols],
                "dark_e_sha256": hashlib.sha256(
                    np.ascontiguousarray(cal["dark_e"], dtype="<f8").tobytes()
                ).hexdigest(),
                "dark_hash_encoding": "C-order little-endian float64 electrons",
            }
        )
    rates, variances, bounds, valid = map(np.stack, (rates, variances, bounds, validity))
    output_valid = valid.any(axis=0)
    # Exact zero modeled stochastic variance is allowed in a noiseless fixture.
    # Quantization remains an interval; do not invent quantization noise to make
    # an inverse variance weight defined. Zero-variance inputs receive equal weight.
    zero = valid & (variances == 0)
    any_zero = zero.any(axis=0)
    positive = valid & (variances > 0) & ~any_zero[None, ...]
    minimum = np.min(np.where(positive, variances, np.inf), axis=0)
    reciprocal = np.zeros(raw.shape, dtype=float)
    np.divide(minimum[None, ...], variances, out=reciprocal, where=positive)
    reciprocal[zero] = 1.0
    weights = np.zeros_like(reciprocal)
    np.divide(
        reciprocal, reciprocal.sum(axis=0)[None, ...], out=weights, where=output_valid[None, ...]
    )
    safe_rates, safe_var, safe_bounds = (
        np.where(valid, v, 0.0) for v in (rates, variances, bounds)
    )
    merged = np.sum(weights * safe_rates, axis=0)
    estimated_variance = np.sum(weights**2 * safe_var, axis=0)
    merged_bound = np.sum(weights * safe_bounds, axis=0)
    for value in (merged, estimated_variance, merged_bound):
        value[~output_valid] = np.nan
    usable_count = valid.sum(axis=0)
    unknown = usable_count < 2
    consistent = ~unknown
    for i in range(count):
        for j in range(i + 1, count):
            joint = valid[i] & valid[j]
            with np.errstate(over="ignore", invalid="ignore"):
                threshold = (
                    selected["consistency_sigma"]
                    * np.hypot(np.sqrt(safe_var[i]), np.sqrt(safe_var[j]))
                    + safe_bounds[i]
                    + safe_bounds[j]
                )
                difference = np.abs(safe_rates[i] - safe_rates[j])
            if not np.isfinite(threshold[joint]).all() or not np.isfinite(difference[joint]).all():
                raise ValueError("HDR consistency comparison exceeds finite domain")
            consistent &= ~joint | (difference <= threshold)
    for value in (merged, estimated_variance, merged_bound):
        if not np.isfinite(value[output_valid]).all():
            raise ValueError("HDR fusion exceeds finite numerical domain")
    linear, rgb_valid, preview = hdr._rgb(merged, output_valid, pattern, selected)
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        raise RuntimeError("RAW changed during nonlinear HDR fusion")
    arrays = {
        "exposure_rate": rates,
        "exposure_estimated_variance": variances,
        "exposure_quantization_bound_rate": bounds,
        "exposure_valid_mask": valid,
        "exposure_quantization_knot_crossing_mask": np.stack(knot_flags),
        "weights": weights,
        "merged_rate": merged,
        "estimated_variance": estimated_variance,
        "quantization_bound_rate": merged_bound,
        "valid_mask": output_valid,
        "temporal_consistency_mask": consistent,
        "temporal_consistency_unknown_mask": unknown,
        "linear_rgb": linear,
        "linear_rgb_valid_mask": rgb_valid,
        "rgb": preview,
    }
    units = {
        "exposure_rate": "electron/pixel/s",
        "merged_rate": "electron/pixel/s",
        "linear_rgb": "electron/pixel/s",
        "exposure_estimated_variance": "(electron/pixel/s)^2",
        "estimated_variance": "(electron/pixel/s)^2",
        "exposure_quantization_bound_rate": "electron/pixel/s",
        "quantization_bound_rate": "electron/pixel/s",
        "weights": "1",
        "rgb": "1",
        **{k: "boolean" for k, v in arrays.items() if v.dtype.kind == "b"},
    }
    return {
        "arrays": arrays,
        "summary": {
            "schema": "camera_hdr_merge_result_v2",
            "method": METHOD,
            "profile": selected,
            "geometry": geom,
            "local_cfa_rgb_indices": (pattern - 1).tolist(),
            "frames": count,
            "exposure_s": exposures.tolist(),
            "input_shape": list(raw.shape),
            "input_dtype": raw.dtype.str,
            "input_array_bytes_sha256": before,
            "calibrations": cal_summaries,
            "frame_diagnostics": summaries,
            "valid_pixels": int(output_valid.sum()),
            "invalid_pixels": int((~output_valid).sum()),
            "temporal_consistent_pixels": int(consistent.sum()),
            "temporal_inconsistent_pixels": int((~consistent & ~unknown).sum()),
            "temporal_consistency_unknown_pixels": int(unknown.sum()),
            "single_usable_exposure_pixels": int((usable_count == 1).sum()),
            "linear_rgb_valid_pixels": int(rgb_valid.sum()),
            "array_units": units,
            "rate_estimator": "(RAW interval inverse midpoint - declared dark_e) / exposure_s",
            "estimated_variance_scope": "local inverse-slope voltage read variance and plug-in "
            "independent shot only; unknown total uncertainty",
            "quantization_scope": "charge preimage bounds including declared float64 allowances; "
            "propagated separately, not independent noise or a formal IEEE interval proof",
            "zero_variance_weights": "equal among usable zero-modeled-stochastic-variance inputs",
            "consistency_test": "all usable pairs: rate difference <= "
            "sigma*sqrt(var_i+var_j)+bound_i+bound_j",
            "consistency_scope": "diagnostic only; no motion registration "
            "or independent physical validation",
            "invalid_policy": "NaN physical arrays and false masks; "
            "display alone maps invalid pixels to black",
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
        },
    }
