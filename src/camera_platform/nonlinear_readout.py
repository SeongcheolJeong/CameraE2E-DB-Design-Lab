"""Versioned monotone voltage readout and RAW-only interval calibration.

Quantization is a deterministic preimage interval, never an invented independent
noise variance. Read noise is declared at the conversion output before analog
gain. The signed inverse below black is an explicit estimator continuation.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np

from . import charge_voltage_response as response
from . import electrical
from .dcg_electrical import _source
from .registry import digest

SCHEMA = "camera_electrical_readout_v2"
CALIBRATION_SCHEMA = "camera_nonlinear_raw_calibration_v1"
NOISE_SCHEMA = "camera_voltage_read_noise_v1"
NOISE_PLANE = "conversion_output_before_analog_gain"
NOISE_INCLUDES = "all_temporal_read_components"
MODEL_KEYS = (
    electrical.MODEL_KEYS - {"conversion_gain_v_per_e", "read_noise_e", "post_gain_noise_v"}
) | {"charge_voltage_response", "read_noise", "source"}
CALIBRATION_KEYS = {
    "schema",
    "model",
    "model_sha256",
    "acquisition",
    "acquisition_sha256",
    "dark_e",
    "dark_e_sha256",
    "dark_e_shape",
    "dark_hash_encoding",
}
DARK_ENCODING = "C-order little-endian float64 electrons"


def _range(value, name, low, high):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} requires an explicit two-value range")
    result = [electrical._number(v, name, low, high) for v in value]
    if result[0] > result[1]:
        raise ValueError(f"{name} is reversed")
    return result


def validate_model(model):
    """Normalize v2 without upgrading or changing any v1 noise assumption."""
    electrical._keys(model, MODEL_KEYS, "nonlinear electrical model")
    if model["schema"] != SCHEMA:
        raise ValueError("Unsupported nonlinear electrical schema")
    # Reuse the established finite ADC/dark/FPN domains, not the v1 transfer.
    common = electrical.validate_electrical_model(
        {
            **{k: v for k, v in model.items() if k in electrical.MODEL_KEYS},
            "schema": electrical.SCHEMA,
            "conversion_gain_v_per_e": 1.0,
            "read_noise_e": 0.0,
            "post_gain_noise_v": 0.0,
        }
    )
    result = {k: v for k, v in common.items() if k in MODEL_KEYS}
    result["schema"] = SCHEMA
    result["charge_voltage_response"] = response.validate_response(model["charge_voltage_response"])
    if result["full_well_e"] > result["charge_voltage_response"]["charge_e"][-1]:
        raise ValueError("Response curve does not cover the physical charge capacity")
    noise = model["read_noise"]
    electrical._keys(
        noise,
        {
            "schema",
            "plane",
            "variance_v2",
            "temperature_range_c",
            "analog_gain_range",
            "source",
            "includes",
        },
        "voltage read noise",
    )
    if (
        noise["schema"] != NOISE_SCHEMA
        or noise["plane"] != NOISE_PLANE
        or noise["includes"] != NOISE_INCLUDES
    ):
        raise ValueError("Voltage read noise must declare all read terms before analog gain")
    result["read_noise"] = {
        "schema": NOISE_SCHEMA,
        "plane": NOISE_PLANE,
        "includes": NOISE_INCLUDES,
        "variance_v2": electrical._number(noise["variance_v2"], "read variance V2", 0, 1e6),
        "temperature_range_c": _range(
            noise["temperature_range_c"], "noise temperature", -273.15, 1000
        ),
        "analog_gain_range": _range(noise["analog_gain_range"], "noise analog gain", 1e-6, 1e6),
        "source": _source(noise["source"], "read_noise.source"),
    }
    result["source"] = _source(model["source"], "source")
    return result


def validate_capture_conditions(model, exposure_s, analog_gain, temperature_c):
    model = validate_model(model)
    exposure = electrical._number(exposure_s, "exposure_s", 0, 3600)
    analog = electrical._number(analog_gain, "analog_gain", 1e-6, 1e6)
    temperature = electrical._number(temperature_c, "temperature_c", -273.15, 1000)
    response.validate_conditions(
        model["charge_voltage_response"], temperature, analog, model["full_well_e"]
    )
    for value, field in ((temperature, "temperature_range_c"), (analog, "analog_gain_range")):
        lo, hi = model["read_noise"][field]
        if not lo <= value <= hi:
            raise ValueError(f"Capture is outside declared voltage read noise {field}")
    doubling = model["dark_current_doubling_c"]
    factor = 1.
    if doubling is not None:
        exponent = (temperature - model["reference_temperature_c"]) / doubling
        if not -1074 <= exponent <= 1023:
            raise ValueError("Dark-current temperature scaling exceeds supported numeric range")
        factor = math.exp2(exponent)
    if (not math.isfinite(factor) or factor <= 0
            or model["dark_current_e_per_s"] * factor > electrical.MAX_CHARGE_E
            or model["dark_current_e_per_s"] * factor * exposure > electrical.MAX_CHARGE_E):
        raise ValueError("Temperature-adjusted dark mean exceeds supported numeric range")
    return {"exposure_s": exposure, "analog_gain": analog, "temperature_c": temperature}


def encode_voltage(voltage_v, model, analog_gain):
    """Apply the two actual nearest-even quantizers; input includes voltage read noise."""
    adc = model["adc"]
    white, black = 2 ** adc["bit_depth"] - 1, adc["black_level_dn"]
    with np.errstate(over="ignore", invalid="ignore"):
        continuous = black + voltage_v * analog_gain * white / adc["full_scale_v"]
    if not np.isfinite(continuous).all():
        raise ValueError("Voltage transfer exceeds finite ADC domain")
    adc_dn = np.clip(np.rint(continuous), 0, white).astype(np.int32)
    final = black + model["digital_gain"] * (adc_dn.astype(float) - black)
    raw = np.clip(np.rint(final), 0, white).astype(np.int32)
    return {
        "raw_dn": raw,
        "adc_dn": adc_dn,
        "adc_low_mask": continuous <= 0,
        "adc_high_mask": continuous >= white,
        "digital_low_mask": final <= 0,
        "digital_high_mask": final >= white,
    }


def make_calibration(
    model, *, analog_gain, temperature_c, exposure_s, geometry, dark_e, noise_enabled
):
    """Bind declared model/settings; retain no sampled/noiseless charge or truth mask."""
    model = validate_model(model)
    conditions = validate_capture_conditions(model, exposure_s, analog_gain, temperature_c)
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean")
    dark = np.asarray(dark_e)
    if (
        not isinstance(dark_e, np.ndarray)
        or dark.dtype.kind not in "iuf"
        or dark.ndim != 2
        or not 0 < dark.size <= electrical.MAX_PIXELS
        or not np.isfinite(dark).all()
        or np.any(dark < 0)
        or np.any(dark > electrical.MAX_CHARGE_E)
    ):
        raise ValueError("Expected dark calibration requires a finite nonnegative HxW array")
    full, origin = electrical._geometry(geometry, dark.shape)
    acquisition = {
        **conditions,
        "geometry": {"full_sensor_shape": full, "crop_origin": origin},
        "noise_enabled": noise_enabled,
    }
    dark = np.ascontiguousarray(dark, dtype="<f8")
    return {
        "schema": CALIBRATION_SCHEMA,
        "model": model,
        "model_sha256": digest(model),
        "acquisition": acquisition,
        "acquisition_sha256": digest(acquisition),
        "dark_e": dark.astype(float, copy=True),
        "dark_e_sha256": hashlib.sha256(dark.tobytes()).hexdigest(),
        "dark_e_shape": list(dark.shape),
        "dark_hash_encoding": DARK_ENCODING,
    }


def validate_calibration(calibration, shape):
    electrical._keys(calibration, CALIBRATION_KEYS, "RAW-only nonlinear calibration")
    if calibration["schema"] != CALIBRATION_SCHEMA:
        raise ValueError("Unsupported nonlinear calibration schema")
    a = calibration["acquisition"]
    electrical._keys(
        a,
        {"exposure_s", "analog_gain", "temperature_c", "geometry", "noise_enabled"},
        "calibration acquisition",
    )
    result = make_calibration(calibration["model"], **a, dark_e=calibration["dark_e"])
    if result["dark_e"].shape != shape:
        raise ValueError("Dark calibration and RAW shape differ")
    if (type(calibration["dark_e_shape"]) is not list
            or any(type(v) is not int for v in calibration["dark_e_shape"])):
        raise ValueError("Dark calibration shape requires explicit integer dimensions")
    if (
        calibration["model_sha256"] != result["model_sha256"]
        or calibration["acquisition_sha256"] != result["acquisition_sha256"]
        or calibration["dark_e_sha256"] != result["dark_e_sha256"]
        or calibration["dark_e_shape"] != result["dark_e_shape"]
        or calibration["dark_hash_encoding"] != DARK_ENCODING
    ):
        raise ValueError("Nonlinear calibration model, acquisition or dark hash differs")
    return result


def _delivered_conditions(model, analog_gain, temperature_c):
    """Validate the readout domain without imposing an unrelated dark/exposure law."""
    analog = electrical._number(analog_gain, "analog_gain", 1e-6, 1e6)
    temperature = electrical._number(temperature_c, "temperature_c", -273.15, 1000)
    response.validate_conditions(
        model["charge_voltage_response"], temperature, analog, model["full_well_e"]
    )
    for value, field in ((temperature, "temperature_range_c"), (analog, "analog_gain_range")):
        lo, hi = model["read_noise"][field]
        if not lo <= value <= hi:
            raise ValueError(f"Readout is outside declared voltage read noise {field}")
    return analog, temperature


def _encode_delivered_charge(charge, model, analog, noise_enabled, rng):
    voltage = response.evaluate_voltage(charge, model["charge_voltage_response"])
    if noise_enabled and model["read_noise"]["variance_v2"]:
        voltage = voltage + rng.normal(0, np.sqrt(model["read_noise"]["variance_v2"]), charge.shape)
    return encode_voltage(voltage, model, analog)


def read_delivered_charge(
    delivered_charge_e, model, *, analog_gain, temperature_c, noise_enabled, rng
):
    """Encode already delivered total charge, without generation or history inversion.

    The caller supplies an already sampled and capacity-bounded population and a
    read-noise RNG. This never applies PRNU/dark/Poisson or alters the input state.
    """
    model = validate_model(model)
    analog, temperature = _delivered_conditions(model, analog_gain, temperature_c)
    charge = electrical._delivered_charge(
        delivered_charge_e, model["full_well_e"], noise_enabled, rng, "normal"
    )
    return {
        "arrays": _encode_delivered_charge(charge, model, analog, noise_enabled, rng),
        "readout_descriptor": electrical._delivered_descriptor(
            model, temperature, noise_enabled, charge.shape, ["single"], analog_gain=analog,
            processing_order=[
                "charge_voltage_response", "conversion_output_voltage_read_noise", "analog_gain",
                "ADC_nearest_even_clip", "black_relative_digital_nearest_even_clip",
            ],
        ),
    }


def capture_electrons(
    photo_mean, exposure_s, model, *, analog_gain, temperature_c, geometry, noise_enabled, seed
):
    model = validate_model(model)
    cond = validate_capture_conditions(model, exposure_s, analog_gain, temperature_c)
    if (
        not isinstance(photo_mean, np.ndarray)
        or photo_mean.dtype.kind not in "iuf"
        or photo_mean.ndim != 2
        or not 0 < photo_mean.size <= electrical.MAX_PIXELS
    ):
        raise ValueError("photo_mean requires a bounded real numeric HxW NumPy array")
    photo = photo_mean.astype(float)
    if not np.isfinite(photo).all() or np.any(photo < 0) or np.any(photo > electrical.MAX_CHARGE_E):
        raise ValueError("Photocharge is outside the supported finite nonnegative domain")
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean")
    seed = electrical._integer(seed, "seed", 0, 2**32 - 1)
    full, origin = electrical._geometry(geometry, photo.shape)
    prnu, dark_rate, pattern = electrical._patterns(
        model, full, origin, photo.shape, cond["temperature_c"]
    )
    dark = dark_rate * cond["exposure_s"]
    mean = photo * prnu + dark
    if not np.isfinite(mean).all() or np.any(mean > electrical.MAX_CHARGE_E):
        raise ValueError("Total mean charge exceeds supported range")
    rng = np.random.default_rng(seed)
    sampled = rng.poisson(mean).astype(float) if noise_enabled else mean.copy()
    well = sampled >= model["full_well_e"]
    charge = np.minimum(sampled, model["full_well_e"])
    arrays = {
        **_encode_delivered_charge(charge, model, cond["analog_gain"], noise_enabled, rng),
        "photo_response_map": prnu,
        "dark_current_e_per_s_map": dark_rate,
        "mean_total_charge_e": mean,
        "well_saturated_mask": well,
    }
    calibration = make_calibration(
        model, **cond, geometry=geometry, dark_e=dark, noise_enabled=noise_enabled
    )
    units = {
        "raw_dn": "DN",
        "adc_dn": "DN",
        "photo_response_map": "1",
        "dark_current_e_per_s_map": "electron/pixel/s",
        "mean_total_charge_e": "electron",
        **{k: "boolean" for k in arrays if k.endswith("_mask")},
    }
    return {
        "arrays": arrays,
        "calibration": calibration,
        "summary": {
            "schema": SCHEMA,
            "model": model,
            "model_sha256": digest(model),
            **cond,
            "geometry": {
                "full_sensor_shape": full,
                "crop_origin": origin,
                "capture_shape": list(photo.shape),
            },
            "noise_seed": seed,
            "noise_enabled": noise_enabled,
            **pattern,
            "array_units": units,
            "processing_order": [
                "fixed_photo_response_and_dark_mean",
                "one_Poisson_draw",
                "full_well_clip",
                "charge_voltage_response",
                "conversion_output_voltage_read_noise",
                "analog_gain",
                "ADC_nearest_even_clip",
                "black_relative_digital_nearest_even_clip",
            ],
            "saturation_fractions": {
                k: float(v.mean()) for k, v in arrays.items() if k.endswith("_mask")
            },
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "limitations": [
                "Authored static monotone response; no circuit settling or CDS dynamics.",
                "Declared read noise plane does not establish actual physical noise locations.",
                "No charge transport/trapping/blooming; "
                "fixed patterns are not fitted measurements.",
            ],
        },
    }


def _adc_preimage(raw, black, white, digital):
    """Find the exact integer preimage of the implemented monotone digital quantizer.

    Binary search avoids guessing a rounding tolerance or enumerating 2**24 ADC
    values. Empty/skipped final codes stay invalid. ADC endpoints remain censored.
    """

    def boundary(strict):
        lo = np.zeros(raw.shape, dtype=np.int64)
        hi = np.full(raw.shape, white + 1, dtype=np.int64)
        while np.any(lo < hi):
            mid = (lo + hi) // 2
            code = np.clip(np.rint(black + digital * (mid.astype(float) - black)), 0, white)
            choose = (code > raw) if strict else (code >= raw)
            active = lo < hi
            hi = np.where(active & choose, mid, hi)
            lo = np.where(active & ~choose, mid + 1, lo)
        return lo

    return boundary(False), boundary(True) - 1


def decode_raw(raw, calibration, *, guard_sigma=3.0, saturation_margin_dn=1.0):
    """Decode only integer RAW plus declared calibration, with explicit invalid masks.

    Returned bounds invert the quantized noisy-voltage observation into a signed
    charge coordinate; they are not confidence bounds on latent photocharge. Local
    transformed read/shot variance is an approximation, especially near knots.
    """
    if (
        not isinstance(raw, np.ndarray)
        or raw.dtype.kind not in "iu"
        or raw.ndim != 2
        or not 0 < raw.size <= electrical.MAX_PIXELS
    ):
        raise ValueError("RAW requires a bounded integer HxW NumPy array")
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    cal = validate_calibration(calibration, raw.shape)
    m, a = cal["model"], cal["acquisition"]
    guard_sigma = electrical._number(guard_sigma, "guard_sigma", 0, 1e6)
    margin = electrical._number(saturation_margin_dn, "saturation_margin_dn", 0, 1e6)
    adc = m["adc"]
    black, white = adc["black_level_dn"], 2 ** adc["bit_depth"] - 1
    if np.any(raw < 0) or np.any(raw > white):
        raise ValueError("RAW lies outside declared ADC code domain")
    low, high = _adc_preimage(raw, black, white, m["digital_gain"])
    adc_to_volts = adc["full_scale_v"] / white / a["analog_gain"]
    low_v, high_v = (low - 0.5 - black) * adc_to_volts, (high + 0.5 - black) * adc_to_volts
    nominal_mid_v = low_v + (high_v - low_v) / 2
    curve = m["charge_voltage_response"]
    # A tiny voltage rounding error can cross a very steep inverse segment.
    # Expand in voltage BEFORE inversion, then account for charge arithmetic.
    # These visible floating-point allowances are not a formal interval proof
    # for every platform's IEEE implementation, nor a statistical noise term.
    epsilon = np.finfo(np.float64).eps
    voltage_allowance = 16 * epsilon * max(
        abs(black * adc_to_volts), adc["full_scale_v"] / a["analog_gain"],
        curve["signal_voltage_v"][-1], abs(curve["signed_inverse"]["minimum_voltage_v"]),
    )
    charge_allowance = 16 * epsilon * curve["charge_e"][-1]
    low_v = np.nextafter(low_v - voltage_allowance, -np.inf)
    high_v = np.nextafter(high_v + voltage_allowance, np.inf)
    possible = low <= high
    # Invalid/skipped codes use a harmless interval only during primitive evaluation.
    inv = response.invert_voltage_interval(
        np.where(possible, low_v, 0.0),
        np.where(possible, high_v, 0.0),
        m["charge_voltage_response"],
    )
    inv["charge_low_e"] = np.nextafter(inv["charge_low_e"] - charge_allowance, -np.inf)
    inv["charge_high_e"] = np.nextafter(inv["charge_high_e"] + charge_allowance, np.inf)
    point_v = np.where(possible, nominal_mid_v, 0.)
    point = response.invert_voltage_interval(point_v, point_v, curve)
    inv["charge_mid_e"] = point["charge_mid_e"]
    inv["inverse_slope_e_per_v"] = point["inverse_slope_e_per_v"]
    inv["valid_mask"] &= point["valid_mask"]
    voltage_variance = m["read_noise"]["variance_v2"] if a["noise_enabled"] else 0.0
    guard_v = max(
        guard_sigma * np.sqrt(voltage_variance), margin * adc_to_volts / m["digital_gain"]
    )
    capacity_v = float(
        response.evaluate_voltage(np.asarray(m["full_well_e"]), m["charge_voltage_response"])
    )
    lower = (low <= 0) | (low_v <= (-black * adc_to_volts + guard_v))
    upper = (high >= white) | (high_v >= min((white - black) * adc_to_volts, capacity_v) - guard_v)
    valid = possible & inv["valid_mask"] & ~lower & ~upper
    total = inv["charge_mid_e"].copy()
    slope = inv["inverse_slope_e_per_v"].copy()
    with np.errstate(over="ignore", invalid="ignore"):
        read_var = slope**2 * voltage_variance
        shot_var = np.maximum(total, 0) if a["noise_enabled"] else np.zeros(raw.shape)
        bound = np.maximum(total - inv["charge_low_e"], inv["charge_high_e"] - total)
    finite = np.isfinite(read_var) & np.isfinite(shot_var) & np.isfinite(bound)
    valid &= finite
    arrays = {
        "total_charge_e": total,
        "charge_low_e": inv["charge_low_e"].copy(),
        "charge_high_e": inv["charge_high_e"].copy(),
        "inverse_slope_e_per_v": slope,
        "read_variance_e2": read_var,
        "shot_variance_estimate_e2": shot_var,
        "stochastic_variance_estimate_e2": read_var + shot_var,
        "quantization_bound_e": bound,
        "valid_mask": valid,
        "quantization_knot_crossing_mask": inv["knot_crossing_mask"] & valid,
        "lower_censored_mask": lower,
        "upper_censored_mask": upper,
        "impossible_code_mask": ~possible,
        "inverse_outside_domain_mask": ~inv["valid_mask"],
    }
    for value in arrays.values():
        if value.dtype.kind == "f":
            value[~valid] = np.nan
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        raise RuntimeError("RAW input changed during nonlinear calibration")
    return {
        "arrays": arrays,
        "summary": {
            "schema": "camera_nonlinear_raw_decode_v1",
            "model_sha256": cal["model_sha256"],
            "acquisition_sha256": cal["acquisition_sha256"],
            "dark_e_sha256": cal["dark_e_sha256"],
            "dark_e_shape": cal["dark_e_shape"],
            "dark_hash_encoding": cal["dark_hash_encoding"],
            "raw_array_sha256": before,
            "valid_pixels": int(valid.sum()),
            "invalid_pixels": int((~valid).sum()),
            "voltage_read_variance_v2": voltage_variance,
            "guard_voltage_v": float(guard_v),
            "numerical_voltage_allowance_v": float(voltage_allowance),
            "numerical_charge_allowance_e": float(charge_allowance),
            "point_estimator": "inverse of the unexpanded voltage interval midpoint; "
            "numerical allowances widen bounds without shifting the estimator",
            "quantization": "numerically evaluated ADC/digital preimage bounds with explicit "
            "float64 allowance; not a formal IEEE interval proof or latent-charge confidence bound",
            "variance": "local inverse-slope transformed voltage read variance "
            "plus plug-in shot variance; no quantization variance",
            "censoring": "RAW-derived ADC endpoint and nominal capacity guards; "
            "not simulator truth or a probability guarantee",
            "signed_policy": "declared origin tangent below black; not physical negative charge",
            "uncertainty": {"kind": "unknown"},
            "physical_validation": "not_established",
        },
    }
