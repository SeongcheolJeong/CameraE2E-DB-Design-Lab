"""RAW-only fusion for one shared charge observed through HCG/LCG paths.

Nonnegative weights minimize the declared read covariance only. The common
Poisson shot term is not divided by the number of readouts, and deterministic
rounding bounds are never relabeled as independent quantization variances.
This module consumes integer codes and calibration, not simulator truth.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from fractions import Fraction

import numpy as np

SCHEMA = "camera_dcg_merge_v1"
CALIBRATION_SCHEMA = "camera_shared_charge_calibration_v1"
STREAM_IDS = ["HCG", "LCG"]
MAX_PIXELS = 1_000_000
CALIBRATION_FIELDS = {
    "schema", "stream_ids", "exposure_s", "paths", "read_noise_covariance_e2", "dark_e",
    "geometry",
}
PATH_FIELDS = {
    "stream_id", "dn_per_e", "black_level_dn", "white_level_dn", "linear_floor_dn",
    "linear_ceiling_dn", "rounding_bound_e",
}


def _exact(value, fields, name):
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{name} requires exactly {sorted(fields)}; no truth/oracle fields")


def _number(value, name, *, positive=False):
    if (isinstance(value, (bool, np.bool_))
            or not isinstance(value, (int, float, np.integer, np.floating))):
        raise ValueError(f"{name} requires a finite real number")
    try:
        value = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} exceeds the finite numerical domain") from exc
    if not np.isfinite(value) or value < 0 or (positive and value == 0):
        domain = "positive" if positive else "nonnegative"
        raise ValueError(f"{name} requires a finite {domain} value")
    return value


def _array(value, shape, name):
    # Inspect booleans before NumPy can silently coerce a mixed list to floats.
    def boolean(node):
        if isinstance(node, (bool, np.bool_)):
            return True
        if isinstance(node, (list, tuple)):
            return any(boolean(item) for item in node)
        return isinstance(node, np.ndarray) and (node.dtype.kind == "b"
            or node.dtype.kind == "O" and any(boolean(item) for item in node.flat))

    if boolean(value):
        raise ValueError(f"{name} requires numbers, not booleans")
    try:
        original = np.asarray(value)
        if original.shape != shape or original.dtype.kind not in "iuf":
            raise ValueError(f"{name} requires a real numeric array with shape {shape}")
        result = original.astype(np.float64)
    except (TypeError, OverflowError) as exc:
        raise ValueError(f"{name} exceeds the finite numerical domain") from exc
    if not np.isfinite(result).all():
        raise ValueError(f"{name} requires finite values")
    return result


def default_profile():
    """Explicit guard and tie policy; no display scaling or uncertainty claim."""
    return {
        "schema": SCHEMA, "method": "nonnegative_read_covariance_raw",
        "saturation_margin_dn": 1.0, "read_noise_guard_sigma": 3.0,
        "gain_path_agreement_sigma": 5.0, "tie_break_stream": "HCG",
    }


def validate_profile(profile):
    _exact(profile, set(default_profile()), "DCG fusion profile")
    if (profile["schema"] != SCHEMA or profile["method"] != "nonnegative_read_covariance_raw"
            or profile["tie_break_stream"] not in STREAM_IDS):
        raise ValueError("Unsupported DCG fusion schema, method or tie-break stream")
    margin = _number(profile["saturation_margin_dn"], "saturation_margin_dn")
    if margin < 1:
        raise ValueError("saturation_margin_dn must be at least one code")
    return {
        "schema": SCHEMA, "method": profile["method"], "saturation_margin_dn": margin,
        "read_noise_guard_sigma": _number(profile["read_noise_guard_sigma"],
                                           "read_noise_guard_sigma"),
        "gain_path_agreement_sigma": _number(profile["gain_path_agreement_sigma"],
                                              "gain_path_agreement_sigma"),
        "tie_break_stream": profile["tie_break_stream"],
    }


def _covariance(value):
    matrix = _array(value, (2, 2), "read_noise_covariance_e2")
    if matrix[0, 1] != matrix[1, 0] or np.any(np.diag(matrix) < 0):
        raise ValueError("Read covariance must be exactly symmetric and positive semidefinite")
    a, b, c = (Fraction.from_float(float(x))
               for x in (matrix[0, 0], matrix[0, 1], matrix[1, 1]))
    if a * c - b * b < 0:
        raise ValueError("Read covariance must be positive semidefinite; no tolerance or jitter")
    return matrix, (a, b, c)


def _geometry(value, shape):
    _exact(value, {"full_sensor_shape", "crop_origin"}, "calibration.geometry")
    pairs = {}
    for key in ("full_sensor_shape", "crop_origin"):
        values = value[key]
        if (not isinstance(values, (list, tuple)) or len(values) != 2
                or any(isinstance(v, (bool, np.bool_))
                       or not isinstance(v, (int, np.integer)) for v in values)):
            raise ValueError("Native DCG geometry requires explicit integer pairs")
        pairs[key] = [int(v) for v in values]
    if any(not 2 <= full <= 1_000_000 or not 0 <= offset <= full - count
           for full, offset, count in zip(pairs["full_sensor_shape"], pairs["crop_origin"],
                                         shape, strict=True)):
        raise ValueError("Native DCG crop must fit the declared full sensor")
    return pairs


def _calibration(value, shape):
    _exact(value, CALIBRATION_FIELDS, "shared-charge calibration")
    if value["schema"] != CALIBRATION_SCHEMA or value["stream_ids"] != STREAM_IDS:
        raise ValueError("Calibration requires the explicit HCG/LCG shared-charge stream order")
    exposure = _number(value["exposure_s"], "exposure_s", positive=True)
    if not isinstance(value["paths"], list) or len(value["paths"]) != 2:
        raise ValueError("Calibration requires exactly two ordered gain paths")
    paths = []
    for index, path in enumerate(value["paths"]):
        _exact(path, PATH_FIELDS, "calibration.paths")
        if path["stream_id"] != STREAM_IDS[index]:
            raise ValueError("Calibration path order must be HCG, LCG")
        item = {key: _number(path[key], key, positive=key == "dn_per_e")
                for key in PATH_FIELDS - {"stream_id"}}
        black, white = item["black_level_dn"], item["white_level_dn"]
        if (not white.is_integer() or not 1 <= white <= 2**24 - 1
                or not item["linear_floor_dn"] <= black < item["linear_ceiling_dn"] <= white):
            raise ValueError("Path floor/black/linear ceiling must lie in the integer ADC domain")
        paths.append({"stream_id": path["stream_id"], **item})
    if paths[0]["white_level_dn"] != paths[1]["white_level_dn"]:
        raise ValueError("Shared-charge streams require the same ADC white code")
    dark = _array(value["dark_e"], shape, "dark_e")
    if np.any(dark < 0):
        raise ValueError("Declared dark expectation must be nonnegative")
    matrix, fractions = _covariance(value["read_noise_covariance_e2"])
    geometry = _geometry(value["geometry"], shape)
    return exposure, paths, dark, matrix, fractions, geometry


def _minimum_read_weights(fractions, tie_stream):
    """Solve the one-dimensional convex quadratic exactly before float output."""
    a, b, c = fractions
    curvature = a + c - 2 * b
    if curvature == 0:
        weight = Fraction(int(tie_stream == "HCG"))
        reason = "flat_quadratic_explicit_stream_tie_break"
    else:
        weight = min(Fraction(1), max(Fraction(0), (c - b) / curvature))
        reason = ("interior_convex_minimum" if 0 < weight < 1
                  else "constrained_endpoint_minimum")
    w = float(weight)
    weights = np.array([w, 1.0 - w])
    # Exact rational evaluation of the actual returned floating weights avoids
    # catastrophic cancellation at perfectly anticorrelated PSD boundaries.
    high, low = (Fraction.from_float(float(v)) for v in weights)
    try:
        variance = float(high * high * a + 2 * high * low * b + low * low * c)
        difference_variance = float(curvature)
    except OverflowError as exc:
        raise ValueError("Read covariance calculation exceeds the finite numerical domain") from exc
    if not np.isfinite(variance) or not np.isfinite(difference_variance):
        raise ValueError("Read covariance calculation exceeds the finite numerical domain")
    return weights, variance, difference_variance, reason


def merge_shared_charge_raw(raw, calibration, profile):
    """Merge one acquired [HCG,LCG,H,W] RAW group without changing its bytes.

    Usability is a conservative RAW-boundary guard, not saturation truth. Gain
    path disagreement is retained as a diagnostic; it does not replace samples
    or silently alter weights. Only the caller can establish calibration and
    same-charge acquisition provenance.
    """
    if (not isinstance(raw, np.ndarray) or raw.ndim != 3 or raw.dtype.kind not in "iu"
            or raw.shape[0] != 2 or any(n < 2 or n > 4096 or n % 2 for n in raw.shape[1:])
            or raw.shape[1] * raw.shape[2] > MAX_PIXELS):
        raise ValueError("RAW requires integer [2,even H,even W] within 1000000 native pixels")
    shape = raw.shape[1:]
    selected = validate_profile(profile)
    exposure, paths, dark, covariance, fractions, geometry = _calibration(calibration, shape)
    optimum, optimum_variance, difference_variance, weight_reason = _minimum_read_weights(
        fractions, selected["tie_break_stream"])
    if any(np.any(raw[j] < 0) or np.any(raw[j] > p["white_level_dn"])
           for j, p in enumerate(paths)):
        raise ValueError("RAW is outside its declared ADC domain; no silent clipping")
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    total = np.empty(raw.shape, dtype=np.float64)
    usable = np.empty(raw.shape, dtype=bool)
    diagnostics = []
    with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
        for index, path in enumerate(paths):
            total[index] = (raw[index].astype(float) - path["black_level_dn"]) / path["dn_per_e"]
            guard = (max(selected["saturation_margin_dn"], selected["read_noise_guard_sigma"]
                         * np.sqrt(covariance[index, index]) * path["dn_per_e"])
                     + path["rounding_bound_e"] * path["dn_per_e"])
            if not np.isfinite(guard):
                raise ValueError("RAW guard exceeds the finite numerical domain")
            lower = raw[index] <= path["linear_floor_dn"] + guard
            upper = raw[index] >= path["linear_ceiling_dn"] - guard
            usable[index] = ~(lower | upper)
            diagnostics.append({"stream_id": STREAM_IDS[index], "raw_guard_dn": float(guard),
                "lower_guarded_pixels": int(lower.sum()), "upper_guarded_pixels": int(upper.sum()),
                "usable_pixels": int(usable[index].sum())})
        valid = usable.any(axis=0)
        joint = usable.all(axis=0)
        weights = np.zeros(raw.shape)
        weights[0] = np.where(joint, optimum[0], usable[0])
        weights[1] = np.where(joint, optimum[1], usable[1])
        combined_total = (weights * total).sum(axis=0)
        merged = (combined_total - dark) / exposure
        read_variance = np.where(joint, optimum_variance,
                                 weights[0] * covariance[0, 0] + weights[1] * covariance[1, 1])
        shot = np.maximum(combined_total, 0)
        variance = shot + read_variance
        rate_variance = variance / exposure / exposure
        bound = sum(weights[j] * p["rounding_bound_e"] for j, p in enumerate(paths))
        rate_bound = bound / exposure
        path_rate = (total - dark) / exposure
        difference = total[0] - total[1]
        threshold = (selected["gain_path_agreement_sigma"] * np.sqrt(difference_variance)
                     + sum(p["rounding_bound_e"] for p in paths))
    finite = (total, merged, read_variance, shot, variance, rate_variance, bound, rate_bound,
              path_rate, difference)
    if not all(np.isfinite(a).all() for a in finite) or not np.isfinite(threshold):
        raise ValueError("RAW calibration, covariance or exposure exceeds the numerical domain")
    agreement = joint & (np.abs(difference) <= threshold)
    arrays = {
        "path_rate": path_rate, "usable_mask": usable, "weights": weights,
        "valid_mask": valid, "merged_rate": merged,
        "common_shot_variance_estimate_e2": shot, "read_variance_e2": read_variance,
        "shared_shot_read_variance_estimate_e2": variance,
        "shared_shot_read_rate_variance_estimate": rate_variance,
        "rounding_bound_e": bound, "rounding_bound_rate": rate_bound,
        "gain_path_difference_e": difference, "gain_path_agreement_mask": agreement,
        "gain_path_agreement_unknown_mask": ~joint,
    }
    path_rate[~usable] = np.nan
    difference[~joint] = np.nan
    for name in ("merged_rate", "common_shot_variance_estimate_e2", "read_variance_e2",
                 "shared_shot_read_variance_estimate_e2", "shared_shot_read_rate_variance_estimate",
                 "rounding_bound_e", "rounding_bound_rate"):
        arrays[name][~valid] = np.nan
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        raise RuntimeError("RAW bytes changed during shared-charge fusion")
    summary = {
        "schema": "camera_dcg_merge_result_v1", "method": selected["method"], "profile": selected,
        "stream_ids": STREAM_IDS.copy(), "acquisitions": 1, "readouts": 2,
        "geometry": geometry, "exposure_s": exposure, "input_shape": list(raw.shape),
        "input_dtype": raw.dtype.str, "input_array_bytes_sha256": before,
        "input_hash_scope": "C-order RAW bytes; original container retained by caller",
        "calibration": {"schema": CALIBRATION_SCHEMA, "stream_ids": STREAM_IDS.copy(),
            "paths": paths, "read_noise_covariance_e2": covariance.tolist(),
            "dark_e_shape": list(shape), "dark_e_sha256": hashlib.sha256(
                np.ascontiguousarray(dark, dtype="<f8").tobytes()).hexdigest(),
            "dark_hash_encoding": "C-order little-endian float64 electrons"},
        "path_diagnostics": diagnostics, "both_usable_weights": optimum.tolist(),
        "weight_solution": weight_reason, "covariance_regularization": "none",
        "weight_objective": "minimize w^T R w with nonnegative weights and sum(w)=1",
        "valid_pixels": int(valid.sum()), "invalid_pixels": int((~valid).sum()),
        "both_usable_pixels": int(joint.sum()),
        "single_usable_path_pixels": int((usable.sum(axis=0) == 1).sum()),
        "gain_path_agreement_pixels": int(agreement.sum()),
        "gain_path_disagreement_pixels": int((joint & ~agreement).sum()),
        "gain_path_agreement_unknown_pixels": int((~joint).sum()),
        "gain_path_difference_read_variance_e2": difference_variance,
        "gain_path_agreement_threshold_e": float(threshold),
        "rate_estimator": "(sum(w_j*(RAW_j-black_j)/dn_per_e_j)-dark_e)/exposure_s",
        "raw_guard": "max(margin_dn,sigma*sqrt(R_jj)*dn_per_e_j)+rounding_bound_e_j*dn_per_e_j",
        "mask_scope": "RAW boundary guards only; not simulator clipping truth or recovery proof",
        "gain_path_agreement": "abs(z_H-z_L)<=sigma*sqrt(R_HH+R_LL-2*R_HL)+bound_H+bound_L",
        "agreement_scope": "same-charge gain-path diagnostic; no temporal consistency claim",
        "common_shot_estimator": "max(sum(w_j*(RAW_j-black_j)/dn_per_e_j),0)",
        "variance_scope": "Poisson common-shot plug-in estimate plus declared read covariance; "
                          "unclipped fixed-weight formula, without RAW-selection conditioning; "
                          "not total uncertainty or measured noise-disabled capture variance",
        "rounding_scope": "weighted deterministic bounds, separate from stochastic variance; "
                          "valid only for an unclipped linear transfer, which usability "
                          "does not certify; "
                          "no independent ADC quantization noise assumption",
        "invalid_policy": "NaN radiometry and false masks; all-invalid weights are zero",
        "physical_validation": "not_established", "uncertainty": {"kind": "unknown"},
        "array_units": {
            "path_rate": "electron/pixel/s", "merged_rate": "electron/pixel/s", "weights": "1",
            "common_shot_variance_estimate_e2": "electron^2", "read_variance_e2": "electron^2",
            "shared_shot_read_variance_estimate_e2": "electron^2",
            "shared_shot_read_rate_variance_estimate": "(electron/pixel/s)^2",
            "rounding_bound_e": "electron", "rounding_bound_rate": "electron/pixel/s",
            "gain_path_difference_e": "electron",
            **{name: "boolean" for name in arrays if name.endswith("mask")},
        },
        "limitations": [
            "Caller must establish one shared-charge acquisition and matched calibration.",
            "Signed photoelectron estimates are preserved; shot plug-in is nonnegative.",
            "Dark expectation removal does not remove already generated common dark shot noise.",
            "Disagreeing usable paths remain in the result with explicit diagnostic flags.",
            "Noise may move clipped RAW inside a guard; usability cannot certify nonclipping.",
            "RAW-dependent guards select noise samples; selection bias and conditional "
            "variance are not modeled by the fixed-weight covariance expression.",
            "Rounding bounds do not bound clipping error, stochastic noise or total error.",
            "Quantization covariance, calibration uncertainty and model mismatch are unmodeled.",
            "No motion correction, DCG circuit equivalence or physical sensor accuracy is implied.",
        ],
    }
    return {"summary": summary, "arrays": arrays}
