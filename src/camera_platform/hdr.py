"""Calibrated RAW bracket fusion with explicit censoring and estimated noise.

This declared software method consumes acquired integer codes and calibration,
never expected electrons or a scene reference. Its inverse-variance weights use
a plug-in shot-noise estimate. They are not a calibrated total uncertainty or an
EMVA certification. Timing, independent acquisition and a common scene target
must be established by the caller; a residual check cannot establish alignment.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

import numpy as np

SCHEMA = "camera_hdr_merge_v1"
MAX_FRAME_PIXELS = 1_048_576
CALIBRATION_FIELDS = {
    "dn_per_e", "black_level_dn", "white_level_dn", "full_well_e", "dark_e",
    "read_noise_variance_e2", "quantization_variance_e2", "linear_ceiling_dn", "linear_floor_dn",
}


def _exact(value, fields, name):
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{name} requires exactly {sorted(fields)}")


def _number(value, name, *, minimum=0.0, positive=False):
    if (isinstance(value, (bool, np.bool_)) or not np.isscalar(value)
            or not isinstance(value, (int, float, np.integer, np.floating))):
        raise ValueError(f"{name} requires a finite real number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} requires a finite real number") from error
    if not np.isfinite(number) or number < minimum or (positive and number <= 0):
        raise ValueError(f"{name} is outside its finite supported domain")
    return number


def _array(value, shape, name):
    original = np.asarray(value)
    if original.shape != shape or original.dtype.kind not in "iuf":
        raise ValueError(f"{name} requires a real numeric array with shape {shape}")
    array = original.astype(np.float64)
    if not np.isfinite(array).all():
        raise ValueError(f"{name} requires finite values")
    return array


def default_profile():
    """Explicit illustrative tuning; display scaling is not a radiometric fit."""
    return {
        "schema": SCHEMA, "method": "inverse_variance_raw",
        "saturation_margin_dn": 1.0, "read_noise_guard_sigma": 3.0,
        "consistency_sigma": 5.0, "reference_exposure_s": 0.01,
        "tone": {"curve": "reinhard", "white_point": 4.0, "exposure_scale": 0.00001},
        "white_balance_rgb": [1.0, 1.0, 1.0],
        "ccm": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    }


def validate_profile(profile):
    _exact(profile, set(default_profile()), "profile")
    if profile["schema"] != SCHEMA or profile["method"] != "inverse_variance_raw":
        raise ValueError("Unsupported HDR schema or merge method")
    tone = profile["tone"]
    _exact(tone, {"curve", "white_point", "exposure_scale"}, "tone")
    if tone["curve"] != "reinhard":
        raise ValueError("Only declared extended Reinhard preview is supported")
    wb = _array(profile["white_balance_rgb"], (3,), "white_balance_rgb")
    if np.any(wb <= 0):
        raise ValueError("White balance gains must be positive")
    ccm = _array(profile["ccm"], (3, 3), "ccm")
    return {
        "schema": SCHEMA, "method": profile["method"],
        "saturation_margin_dn": _number(profile["saturation_margin_dn"],
                                        "saturation_margin_dn", minimum=1),
        "read_noise_guard_sigma": _number(profile["read_noise_guard_sigma"],
                                          "read_noise_guard_sigma"),
        "consistency_sigma": _number(profile["consistency_sigma"],
                                      "consistency_sigma", positive=True),
        "reference_exposure_s": _number(profile["reference_exposure_s"],
                                         "reference_exposure_s", positive=True),
        "tone": {"curve": "reinhard", **{
            key: _number(tone[key], "tone." + key, positive=True)
            for key in ("white_point", "exposure_scale")}},
        "white_balance_rgb": wb.tolist(), "ccm": ccm.tolist(),
    }


def _geometry(geometry, shape):
    _exact(geometry, {"kind", "full_sensor_shape", "crop_origin"}, "geometry")
    if geometry["kind"] != "native_sensor":
        raise ValueError("HDR requires unchanged native sensor geometry")
    pairs = {}
    for name in ("full_sensor_shape", "crop_origin"):
        values = geometry[name]
        if (not isinstance(values, (list, tuple)) or len(values) != 2
                or any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer))
                       for v in values)):
            raise ValueError(f"geometry.{name} requires two integers")
        pairs[name] = [int(v) for v in values]
    if any(not 2 <= full <= 1_000_000 or not 0 <= start <= full - length
           for full, start, length in zip(pairs["full_sensor_shape"], pairs["crop_origin"],
                                         shape, strict=True)):
        raise ValueError("Native HDR crop must fit its declared full sensor")
    pattern = np.roll(np.array([[1, 2], [2, 3]]),
                      tuple(-v % 2 for v in pairs["crop_origin"]), axis=(0, 1))
    return {"kind": "native_sensor", **pairs}, pattern


def _calibration(value, shape):
    _exact(value, CALIBRATION_FIELDS, "calibration (RAW and calibration only; no truth arrays)")
    result = {key: _number(value[key], key, positive=key in {"dn_per_e", "full_well_e"})
              for key in CALIBRATION_FIELDS - {"dark_e"}}
    white, black = result["white_level_dn"], result["black_level_dn"]
    if not white.is_integer() or not 1 <= white <= 2**24 - 1 or black >= white:
        raise ValueError("Calibration requires an integer ADC white and black below white")
    if result["linear_floor_dn"] > black:
        raise ValueError("linear_floor_dn must lie between zero and black")
    ceiling = result["linear_ceiling_dn"]
    physical_ceiling = min(white, black + result["full_well_e"] * result["dn_per_e"])
    if not black < ceiling <= physical_ceiling:
        raise ValueError("linear_ceiling_dn must respect full-well and ADC capacity")
    dark = _array(value["dark_e"], shape, "dark_e")
    if np.any(dark < 0):
        raise ValueError("Expected dark charge must be nonnegative")
    return result, dark


def _rgb(rate, valid, pattern, profile):
    from pyisetcam.ip import _ie_bilinear

    rows, cols = rate.shape
    cfa = np.tile(pattern, (rows // 2, cols // 2))
    planes = np.zeros((rows, cols, 3), dtype=np.float64)
    for channel in range(3):
        selected = valid & (cfa == channel + 1)
        planes[..., channel][selected] = rate[selected]
    demosaiced = _ie_bilinear(planes, pattern)
    # Every bilinear dependency is inside this reflected 3x3 stencil. Requiring
    # all nine sites is conservative for green/known CFA samples, and remains
    # valid after a general cross-channel CCM. No invalid hole is interpolated.
    padded = np.pad(valid, 1, mode="reflect")
    rgb_valid = np.logical_and.reduce([
        padded[y:y + rows, x:x + cols] for y in range(3) for x in range(3)
    ])
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        linear = (demosaiced * profile["white_balance_rgb"]) @ np.asarray(profile["ccm"]).T
        tone = profile["tone"]
        x = np.maximum(linear, 0) * profile["reference_exposure_s"] * tone["exposure_scale"]
        preview = x / (1 + x) * (1 + (x / tone["white_point"]) / tone["white_point"])
    if not np.isfinite(linear[rgb_valid]).all() or not np.isfinite(preview[rgb_valid]).all():
        raise ValueError("HDR color or preview scaling exceeds its numerical domain")
    linear[~rgb_valid] = np.nan
    preview = np.clip(preview, 0.0, 1.0)
    preview[~rgb_valid] = 0.0
    return linear, rgb_valid, preview


def merge_raw_bracket(raw, exposure_s, calibrations, *, geometry, profile):
    """Merge immutable integer RAW codes into signed photoelectron-rate estimates.

    A common physical RGGB sensor/crop is required. ``dark_e`` is each exposure's
    expected dark charge in electrons, not a truth photoelectron image. The final
    RAW linear floor and ceiling are censored using the larger of a
    DN margin and a read-noise guard. Below-black signals are preserved. Validity
    does not assert temporal consistency, physical calibration or deghosting.
    """
    if (not isinstance(raw, np.ndarray) or raw.ndim != 3 or raw.dtype.kind not in "iu"
            or not 1 <= raw.shape[0] <= 64 or any(v < 2 or v > 4096 or v % 2
                                              for v in raw.shape[1:])
            or raw.size > MAX_FRAME_PIXELS):
        raise ValueError("RAW requires integer [1..64,even H,even W] within 1048576 pixels")
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    count, rows, cols = raw.shape
    exposures = _array(exposure_s, (count,), "exposure_s")
    if np.any(exposures <= 0):
        raise ValueError("Every exposure must be positive")
    if not isinstance(calibrations, (list, tuple)) or len(calibrations) != count:
        raise ValueError("Exactly one calibration is required per RAW exposure")
    selected = validate_profile(profile)
    geom, pattern = _geometry(geometry, (rows, cols))
    rates = np.empty(raw.shape, dtype=np.float64)
    variances = np.empty_like(rates)
    valid = np.empty(raw.shape, dtype=bool)
    calibration_summary, frame_diagnostics = [], []
    for index, (value, exposure) in enumerate(zip(calibrations, exposures, strict=True)):
        calibration, dark = _calibration(value, (rows, cols))
        if np.any(raw[index] > calibration["white_level_dn"]) or np.any(raw[index] < 0):
            raise ValueError("RAW lies outside its declared ADC code domain")
        readvar = calibration["read_noise_variance_e2"]
        with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
            total = ((raw[index].astype(np.float64) - calibration["black_level_dn"])
                     / calibration["dn_per_e"])
            rates[index] = (total - dark) / exposure
            variances[index] = (np.maximum(total, 0) + readvar
                                + calibration["quantization_variance_e2"]) / exposure**2
            guard = max(selected["saturation_margin_dn"], selected["read_noise_guard_sigma"]
                        * np.sqrt(readvar) * calibration["dn_per_e"])
        if (not np.isfinite(rates[index]).all() or not np.isfinite(variances[index]).all()
                or not np.isfinite(guard)):
            raise ValueError("RAW calibration/exposure exceeds its numerical domain")
        lower = raw[index] <= calibration["linear_floor_dn"] + guard
        upper = raw[index] >= calibration["linear_ceiling_dn"] - guard
        undefined = variances[index] <= 0
        valid[index] = ~(lower | upper | undefined)
        calibration_summary.append({
            **calibration, "dark_e_shape": [rows, cols],
            "dark_e_sha256": hashlib.sha256(
                np.ascontiguousarray(dark, dtype="<f8").tobytes()).hexdigest(),
            "dark_hash_encoding": "C-order little-endian float64 electrons",
        })
        frame_diagnostics.append({
            "frame_index": index, "guard_dn": float(guard),
            "lower_censored_pixels": int(lower.sum()), "upper_censored_pixels": int(upper.sum()),
            "undefined_variance_pixels": int(undefined.sum()),
            "usable_pixels": int(valid[index].sum()),
        })

    usable_count = valid.sum(axis=0)
    output_valid = usable_count > 0
    # Scaling reciprocal variances by their local minimum avoids overflow for
    # small positive variances, without changing the normalized weights.
    minimum = np.min(np.where(valid, variances, np.inf), axis=0)
    reciprocal = np.zeros_like(rates)
    np.divide(minimum[None, ...], variances, out=reciprocal, where=valid)
    weights = np.zeros_like(rates)
    np.divide(reciprocal, reciprocal.sum(axis=0)[None, ...], out=weights,
              where=output_valid[None, ...])
    merged = np.sum(weights * rates, axis=0)
    estimated_variance = np.sum(weights**2 * variances, axis=0)
    merged[~output_valid] = np.nan
    estimated_variance[~output_valid] = np.nan
    unknown = usable_count < 2
    consistent = ~unknown
    # Pairwise comparisons use independent-exposure variance; using residuals
    # against the weighted mean with v_i+v_merge would double-count covariance.
    with np.errstate(over="ignore", invalid="ignore"):
        for i in range(count):
            for j in range(i + 1, count):
                joint = valid[i] & valid[j]
                threshold = selected["consistency_sigma"] * np.hypot(
                    np.sqrt(variances[i]), np.sqrt(variances[j]))
                difference = np.abs(rates[i] - rates[j])
                if (not np.isfinite(threshold[joint]).all()
                        or not np.isfinite(difference[joint]).all()):
                    raise ValueError("Temporal consistency exceeds its numerical domain")
                consistent &= ~joint | (difference <= threshold)
    if (not np.isfinite(merged[output_valid]).all()
            or not np.isfinite(estimated_variance[output_valid]).all()):
        raise ValueError("HDR merge exceeds its numerical domain")
    linear, rgb_valid, preview = _rgb(merged, output_valid, pattern, selected)
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        raise RuntimeError("RAW input changed during HDR fusion")
    arrays = {
        "exposure_rate": rates, "exposure_estimated_variance": variances,
        "exposure_valid_mask": valid, "weights": weights, "merged_rate": merged,
        "estimated_variance": estimated_variance, "valid_mask": output_valid,
        "temporal_consistency_mask": consistent, "temporal_consistency_unknown_mask": unknown,
        "linear_rgb": linear, "linear_rgb_valid_mask": rgb_valid, "rgb": preview,
    }
    summary = {
        "schema": "camera_hdr_merge_result_v1", "method": selected["method"],
        "profile": selected, "geometry": geom, "local_cfa_rgb_indices": (pattern - 1).tolist(),
        "frames": count, "exposure_s": exposures.tolist(), "input_shape": list(raw.shape),
        "input_dtype": raw.dtype.str, "input_array_bytes_sha256": before,
        "input_hash_scope": "C-order RAW array bytes; original container retained by caller",
        "calibrations": calibration_summary, "frame_diagnostics": frame_diagnostics,
        "valid_pixels": int(output_valid.sum()), "invalid_pixels": int((~output_valid).sum()),
        "temporal_consistent_pixels": int(consistent.sum()),
        "temporal_inconsistent_pixels": int((~consistent & ~unknown).sum()),
        "temporal_consistency_unknown_pixels": int(unknown.sum()),
        "single_usable_exposure_pixels": int((usable_count == 1).sum()),
        "linear_rgb_valid_pixels": int(rgb_valid.sum()),
        "rate_estimator": "((raw-black)/dn_per_e-dark_e)/exposure_s",
        "variance_estimator": "(max((raw-black)/dn_per_e,0)+read_var+quant_var)/exposure_s**2",
        "estimated_variance_scope": "plug-in independent shot/read/quantization variance only",
        "weight_normalization": "per-pixel sum=1 over usable exposures; all-invalid weights=0",
        "consistency_test": "all usable pairs: abs(rate_i-rate_j)<=sigma*sqrt(var_i+var_j)",
        "consistency_scope": "noise-scaled diagnostic; unknown below two usable exposures",
        "invalid_policy": "NaN radiometric NPY values with false mask; preview alone uses black",
        "demosaic": "pyisetcam.ip._ie_bilinear; reflected native CFA; no re-quantization",
        "rgb_validity": "all nine reflected 3x3 Bayer samples valid; "
                        "conservative for every channel",
        "linear_negative_policy": "preserved through rate, merge, demosaic, WB and CCM",
        "tone_formula": "x=max(linear_rgb,0)*reference_exposure_s*exposure_scale; "
                        "preview=clip(x/(1+x)*(1+x/white_point**2),0,1)",
        "tone_units": {"exposure_scale": "1/electron", "white_point": "dimensionless x"},
        "array_units": {
            "exposure_rate": "electron/pixel/s",
            "exposure_estimated_variance": "(electron/pixel/s)^2",
            "merged_rate": "electron/pixel/s", "estimated_variance": "(electron/pixel/s)^2",
            "linear_rgb": "electron/pixel/s", "weights": "1", "rgb": "1",
            **{name: "boolean" for name in arrays if name.endswith("mask")},
        },
        "physical_validation": "not_established", "uncertainty": {"kind": "unknown"},
        "limitations": [
            "Calibration values are supplied assumptions unless separately evidenced; "
            "not EMVA certified.",
            "Caller must establish common scene/target, clock, nonoverlap "
            "and independent readouts.",
            "No motion alignment, deghosting, PWM correction or correlated-noise model is applied.",
            "Inconsistent pixels remain in the radiometric result and are explicitly flagged.",
            "A single usable exposure does not establish temporal consistency.",
            "Full-well censoring is calibrated/guarded; noisy RAW alone cannot prove "
            "absence of clipping.",
            "No physical colorimetry, total uncertainty, optical MTF "
            "or hardware ISP equivalence is inferred.",
        ],
    }
    return {"arrays": arrays, "summary": summary}
