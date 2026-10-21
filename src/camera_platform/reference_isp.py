"""Native RAW ISP adapter for the Reference optical/pixel bench.

The existing v2 spatial filters operate in normalized DN. Color remains the
bench's fixed electron-to-linear-sRGB transform; display white is never fitted
to a frame. Noise hints are model assumptions, not measured calibration or
propagated post-filter covariance.
"""

from __future__ import annotations

from copy import deepcopy
from numbers import Real

import numpy as np
from scipy.ndimage import minimum_filter

from pyisetcam.bayer_demosaic import demosaic_bayer

from . import isp_v2

SCHEMA = "camera_reference_isp_v2"
METHODS = ("bilinear", "laplacian", "adaptive_laplacian")


def default_profile(legacy=None):
    """Explicitly migrate common display settings, never legacy filter strength."""
    result = {
        "schema": SCHEMA,
        "lsc_enabled": False,
        "wb": [1.0, 1.0, 1.0],
        "ccm": np.eye(3).tolist(),
        "tone_gamma": 2.2,
        "display_white_level": 1.0,
        "black_offset_dn": 0.0,
        "demosaic_method": "adaptive_laplacian",
        "raw_denoise": {"strength": 0.0, "range_floor_dn2": 1.0},
        "linear_sharpen": {"amount": 0.0, "threshold": 0.01, "max_delta": 0.05},
        "color_calibration_id": None,
    }
    if legacy is not None:
        if not isinstance(legacy, dict):
            raise ValueError("Existing ISP settings must be an object.")
        if legacy.get("schema") == SCHEMA:
            return validate_profile(legacy)
        if "schema" in legacy:
            raise ValueError("Cannot migrate an unrelated versioned ISP profile.")
        for key in (
            "lsc_enabled",
            "wb",
            "ccm",
            "tone_gamma",
            "display_white_level",
            "black_offset_dn",
        ):
            if key in legacy:
                result[key] = deepcopy(legacy[key])
        if legacy.get("lsc_strength", 0) != 0:
            raise ValueError("Reference ISP uses the frozen native LSC, not a radial proxy.")
    return validate_profile(result)


def _number(value, name, low, high):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(name + " must be a finite number.")
    value = float(value)
    if not np.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be in [{low}, {high}].")
    return value


def _keys(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(name + " requires exactly " + ", ".join(sorted(keys)) + ".")


def validate_profile(profile):
    keys = {
        "schema",
        "lsc_enabled",
        "wb",
        "ccm",
        "tone_gamma",
        "display_white_level",
        "black_offset_dn",
        "demosaic_method",
        "raw_denoise",
        "linear_sharpen",
        "color_calibration_id",
    }
    if not isinstance(profile, dict) or set(profile) not in (keys, keys | {"lsc_tuning"}):
        raise ValueError("Reference ISP requires its standard fields and optional lsc_tuning.")
    if profile["schema"] != SCHEMA:
        raise ValueError("An explicit Reference ISP v2 schema is required.")
    if type(profile["lsc_enabled"]) is not bool:
        raise ValueError("lsc_enabled must be boolean.")
    if not isinstance(profile["demosaic_method"], str) or profile["demosaic_method"] not in METHODS:
        raise ValueError("Select bilinear, laplacian or adaptive_laplacian explicitly.")
    color_id = profile["color_calibration_id"]
    if color_id is not None and (not isinstance(color_id, str) or not 1 <= len(color_id) <= 200):
        raise ValueError("color_calibration_id must be a nonempty ID or null.")
    wb, ccm = profile["wb"], profile["ccm"]
    if not isinstance(wb, list) or len(wb) != 3:
        raise ValueError("WB requires three channel gains.")
    if (
        not isinstance(ccm, list)
        or len(ccm) != 3
        or any(not isinstance(row, list) or len(row) != 3 for row in ccm)
    ):
        raise ValueError("CCM requires a three by three matrix.")
    _keys(profile["raw_denoise"], {"strength", "range_floor_dn2"}, "RAW denoise")
    _keys(profile["linear_sharpen"], {"amount", "threshold", "max_delta"}, "Linear sharpen")
    result = deepcopy(profile)
    result["wb"] = [_number(v, "WB", 0.1, 8) for v in wb]
    result["ccm"] = [[_number(v, "CCM", -8, 8) for v in row] for row in ccm]
    result["tone_gamma"] = _number(profile["tone_gamma"], "tone_gamma", 0.5, 4)
    result["display_white_level"] = _number(
        profile["display_white_level"], "display_white_level", 1, 100
    )
    result["black_offset_dn"] = _number(profile["black_offset_dn"], "black_offset_dn", 0, 0)
    result["raw_denoise"] = {
        "strength": _number(profile["raw_denoise"]["strength"], "RAW denoise strength", 0, 1),
        "range_floor_dn2": _number(
            profile["raw_denoise"]["range_floor_dn2"], "RAW noise floor", 1e-12, 2**48
        ),
    }
    result["linear_sharpen"] = {
        key: _number(profile["linear_sharpen"][key], "sharpen " + key, 0, high)
        for key, high in (("amount", 1), ("threshold", 1), ("max_delta", 1))
    }
    if "lsc_tuning" in profile:
        _keys(profile["lsc_tuning"], {"strength", "gain_cap"}, "LSC tuning")
        result["lsc_tuning"] = {
            "strength": _number(profile["lsc_tuning"]["strength"], "LSC strength", 0, 1),
            "gain_cap": _number(profile["lsc_tuning"]["gain_cap"], "LSC gain cap", 1, 4),
        }
    return result


def effective_lsc_gain(frozen_gain, profile):
    """Derive a new map from a validated profile without modifying calibration.

    Omitted tuning retains the original map exactly, including gains below one.
    Explicit full strength/cap four has the same numerical result as omission.
    """
    gains = np.asarray(frozen_gain, dtype=float)
    tuning = profile.get("lsc_tuning")
    if tuning is None or tuning == {"strength": 1.0, "gain_cap": 4.0}:
        return gains.copy()
    return np.minimum(1 + tuning["strength"] * (gains - 1), tuning["gain_cap"])


def _readout(readout):
    if not isinstance(readout, dict):
        raise ValueError("Stored linear readout settings are required.")
    required = {
        "gain",
        "full_well_e",
        "read_noise_e",
        "dark_current_e_per_s",
        "bit_depth",
        "black_level_dn",
    }
    if not required <= readout.keys():
        raise ValueError("Stored readout is missing gain, capacity, noise or ADC settings.")
    bits = readout["bit_depth"]
    if type(bits) is not int or not 1 <= bits <= 24:
        raise ValueError("Readout bit depth must be an integer in 1..24.")
    white = float(2**bits - 1)
    black = _number(readout["black_level_dn"], "black level", 0, white - 1)
    full_well = _number(readout["full_well_e"], "full well", 1e-6, 1e12)
    gain = _number(readout["gain"], "gain", 1e-6, 1e6)
    noise = _number(readout["read_noise_e"], "read noise", 0, 1e6)
    _number(readout["dark_current_e_per_s"], "dark current", 0, 1e12)
    if readout.get("response_type", "linear") != "linear" or readout.get("digital_gain", 1) != 1:
        raise ValueError("Reference ISP noise hints support linear single-gain readout only.")
    fixed = readout.get("fixed_pattern", {})
    if not isinstance(fixed, dict) or any(
        readout.get(key, 0) != 0 or fixed.get(key, 0) != 0
        for key in ("prnu_sigma", "dsnu_e_per_s", "post_gain_noise_v")
    ):
        raise ValueError("Reference ISP noise hints do not cover nonzero FPN or post-gain noise.")
    return white, black, full_well, gain, noise


def readout_dn_per_e(readout):
    """Resolve effective DN/e at the stored gain, independent of physical capacity.

    Older captures retain their explicit legacy conversion convention. Calibrated
    captures may provide DN/e without changing full well or inventing analog gain.
    """
    white, black, full_well, gain, _ = _readout(readout)
    if "dn_per_e" in readout:
        return _number(readout["dn_per_e"], "readout DN/e", 1e-12, 1e12)
    return (white - black) * gain / full_well


def process(raw, readout, matrix, tuning, *, lsc_gain=None, return_stages=False):
    """Replay a native RGGB frame; linear/processed retain unscaled bench sRGB.

    ``display`` alone is divided by the declared display white and gamma encoded.
    ``matrix`` is the already resolved, source-bound electron-to-color transform;
    calibration lookup belongs to the worker, not this numerical adapter.
    """
    selected = validate_profile(tuning)
    values = np.asarray(raw)
    if (
        values.ndim != 2
        or values.dtype.kind not in "iu"
        or min(values.shape) < 2
        or any(size % 2 for size in values.shape)
        or values.size > 1_048_576
    ):
        raise ValueError("Reference ISP requires a bounded, even native integer RGGB frame.")
    white, black, full_well, gain, read_noise = _readout(readout)
    if np.any(values < 0) or np.any(values > white):
        raise ValueError("RAW code is outside the stored ADC range.")
    transform = np.asarray(matrix)
    if (
        transform.shape != (3, 3)
        or transform.dtype.kind not in "iuf"
        or not np.isfinite(transform).all()
    ):
        raise ValueError("Color transform must be a finite electron-to-RGB matrix.")
    transform = transform.astype(float)
    if selected["lsc_enabled"]:
        gains = np.asarray(lsc_gain)
        if (
            gains.shape != values.shape
            or gains.dtype.kind not in "iuf"
            or not np.isfinite(gains).all()
            or np.any(gains <= 0)
            or np.any(gains > 4)
        ):
            raise ValueError("Enabled LSC needs the frozen native gain map with gains in (0,4].")
        frozen_gains = gains.astype(float)
        gains = effective_lsc_gain(frozen_gains, selected)
    else:
        if lsc_gain is not None:
            raise ValueError("A gain map was supplied while LSC is disabled.")
        gains = np.ones(values.shape, dtype=float)
        frozen_gains = gains
    norm = white - black
    dn_per_e = readout_dn_per_e(readout)
    signal = values.astype(float) - black
    blc = signal / norm
    lsc = blc * gains
    raw_valid = (values > 0) & (values < white)
    hint_dn2 = dn_per_e * np.maximum(signal, 0) + (dn_per_e * read_noise) ** 2 + 1 / 12
    hint = hint_dn2 * gains**2 / norm**2
    nr = selected["raw_denoise"]
    denoised = isp_v2.same_site_denoise(
        lsc[None], hint[None], raw_valid[None], nr["strength"], nr["range_floor_dn2"] / norm**2
    )[0]
    electron_scale = norm / dn_per_e if "dn_per_e" in readout else full_well / gain
    electrons = demosaic_bayer(
        denoised * electron_scale, selected["demosaic_method"], cfa_pattern="RGGB"
    )
    linear = (electrons @ transform) * np.asarray(selected["wb"])
    linear = linear @ np.asarray(selected["ccm"]).T
    radius = 1 if selected["demosaic_method"] == "bilinear" else 5
    valid = minimum_filter(raw_valid, size=2 * radius + 1, mode="constant", cval=0).astype(bool)
    sharp = selected["linear_sharpen"]
    sharpen_enabled = sharp["amount"] > 0 and sharp["max_delta"] > 0
    sharp_valid = minimum_filter(valid, size=3, mode="constant", cval=0).astype(bool)
    display_white = selected["display_white_level"]
    delta = np.zeros(values.shape, dtype=float)
    if sharpen_enabled:
        delta = (
            isp_v2.bounded_detail_delta(
                (linear / display_white).mean(axis=-1)[None], sharp_valid[None], **sharp
            )[0]
            * display_white
        )
    processed = linear + delta[..., None] if sharpen_enabled else linear.copy()
    display = np.clip(processed / display_white, 0, 1) ** (1 / selected["tone_gamma"])
    if not np.isfinite(processed).all():
        raise ValueError("Reference ISP exceeded its finite numerical domain.")
    if not return_stages:
        return linear, processed, display
    return {
        "linear": linear,
        "processed": processed,
        "display": display,
        "stages": {
            "blc_normalized": blc,
            "lsc_normalized": lsc,
            "denoised_normalized": denoised,
            "demosaiced_electrons": electrons,
            "color_linear": linear,
            "sharpen_delta": delta,
            "processed_linear": processed,
            "display": display,
        },
        "valid_mask": sharp_valid if sharpen_enabled else valid,
        "raw_valid_mask": raw_valid,
        "noise_hint": hint,
        "noise_hint_dn2": hint_dn2,
        "lsc_gain": gains.copy(),
        "frozen_lsc_gain": frozen_gains.copy(),
        "effective_lsc": {
            "enabled": selected["lsc_enabled"],
            "strength": selected.get("lsc_tuning", {}).get("strength", 1.0),
            "gain_cap": selected.get("lsc_tuning", {}).get("gain_cap", 4.0),
            "frozen_min": float(frozen_gains.min()),
            "frozen_max": float(frozen_gains.max()),
            "effective_min": float(gains.min()),
            "effective_max": float(gains.max()),
            "effective_mean": float(gains.mean()),
            "policy": "min(1 + strength * (frozen_gain - 1), gain_cap); once before RAW NR",
        },
        "profile": selected,
        "noise_hint_basis": "model-derived shot/read/ADC hint; not output covariance",
        "noise_hint_units": "normalized_DN_squared_after_LSC",
        "color_units": "linear_srgb_before_display_white_scaling",
        "comparison_halo_native_pixels": (2 if nr["strength"] > 0 else 0)
        + radius
        + (1 if sharpen_enabled else 0),
    }
