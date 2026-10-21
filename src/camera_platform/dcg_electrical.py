"""Ideal shared-charge HCG/LCG readout, with explicitly paired read noise.

One Poisson charge sample is read twice without destruction or transfer loss.
This is a declared behavioral topology, not a model of a vendor DCG circuit.
The existing independent-exposure electrical model is not changed or invoked.
"""

from __future__ import annotations

import math
from fractions import Fraction

import numpy as np

from .electrical import (
    MAX_CHARGE_E,
    MAX_NATIVE_PIXELS,
    MAX_PIXELS,
    _delivered_charge,
    _delivered_descriptor,
    _geometry,
    _integer,
    _keys,
    _number,
    _patterns,
    default_electrical_model,
)

SCHEMA = "camera_shared_charge_readout_v1"
CALIBRATION_SCHEMA = "camera_shared_charge_calibration_v1"
STREAM_IDS = ["HCG", "LCG"]
TOPOLOGY = "ideal_shared_charge_two_conversion_paths"
MODEL_KEYS = {
    "schema", "topology", "stream_ids", "shared_charge_capacity_e",
    "dark_current_e_per_s", "reference_temperature_c", "dark_current_doubling_c",
    "fixed_pattern", "paths", "read_noise", "source",
}
PATH_KEYS = {
    "stream_id", "conversion_gain_v_per_e", "analog_gain", "input_linear_ceiling_e",
    "digital_gain", "adc",
}
NOISE_INCLUDES = "all_temporal_read_components_input_referred"


def _source(value, name):
    _keys(value, {"kind", "description"}, name)
    if value["kind"] not in ("estimated", "simulated", "measured"):
        raise ValueError(f"{name}.kind must be estimated, simulated or measured")
    description = value["description"]
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 4000:
        raise ValueError(f"{name}.description requires 1..4000 nonblank characters")
    return {"kind": value["kind"], "description": description.strip()}


def _covariance(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(not isinstance(row, (list, tuple)) or len(row) != 2 for row in value)):
        raise ValueError("Read covariance must be an explicit 2x2 matrix")
    result = [[_number(v, "read_noise.covariance_e2", -1e12, 1e12) for v in row]
              for row in value]
    a, b, c = result[0][0], result[0][1], result[1][1]
    if a < 0 or c < 0 or result[1][0] != b:
        raise ValueError("Read covariance requires nonnegative variances and exact symmetry")
    # Test the declared binary64 values exactly. No tolerance, PSD projection,
    # jitter, or silently removed negative eigenvalue changes the input model.
    determinant = Fraction(a) * Fraction(c) - Fraction(b) ** 2
    if determinant < 0:
        raise ValueError("Read covariance must be positive semidefinite; no jitter is added")
    return result


def _calibration_path(path, capacity):
    adc = path["adc"]
    white, black = 2 ** adc["bit_depth"] - 1, adc["black_level_dn"]
    adc_slope = (path["conversion_gain_v_per_e"] * path["analog_gain"]
                 * white / adc["full_scale_v"])
    slope = adc_slope * path["digital_gain"]
    floor = max(0., min(float(white), black * (1 - path["digital_gain"])))
    ceiling = min(float(white),
                  black + min(capacity, path["input_linear_ceiling_e"]) * slope,
                  black + path["digital_gain"] * (white - black))
    rounding = .5 / adc_slope + (0. if path["digital_gain"] == 1 else .5 / slope)
    if (not all(math.isfinite(x) for x in (adc_slope, slope, floor, ceiling, rounding))
            or not 0 < slope or not floor <= black < ceiling):
        raise ValueError("Path linear RAW span exceeds numeric range or floating precision")
    return {
        "stream_id": path["stream_id"], "dn_per_e": slope, "black_level_dn": black,
        "white_level_dn": white, "linear_floor_dn": floor, "linear_ceiling_dn": ceiling,
        "rounding_bound_e": rounding,
    }


def validate_model(model):
    """Return a detached finite JSON model, checking domains before capture allocation."""
    _keys(model, MODEL_KEYS, "shared-charge model")
    if model["schema"] != SCHEMA or model["topology"] != TOPOLOGY:
        raise ValueError("Unsupported shared-charge readout schema or topology")
    if model["stream_ids"] != STREAM_IDS:
        raise ValueError("Shared-charge stream order must be [HCG, LCG]")
    result = {"schema": SCHEMA, "topology": TOPOLOGY, "stream_ids": STREAM_IDS.copy()}
    for key, bounds in {
        "shared_charge_capacity_e": (1e-6, 1e12),
        "dark_current_e_per_s": (0, 1e12),
        "reference_temperature_c": (-273.15, 1000),
    }.items():
        result[key] = _number(model[key], key, *bounds)
    doubling = model["dark_current_doubling_c"]
    result["dark_current_doubling_c"] = (
        None if doubling is None else _number(doubling, "dark_current_doubling_c", 1e-6, 1000)
    )
    fixed = model["fixed_pattern"]
    _keys(fixed, {"prnu_sigma", "dsnu_e_per_s", "seed"}, "fixed_pattern")
    result["fixed_pattern"] = {
        "prnu_sigma": _number(fixed["prnu_sigma"], "fixed_pattern.prnu_sigma", 0, 10),
        "dsnu_e_per_s": _number(fixed["dsnu_e_per_s"], "fixed_pattern.dsnu_e_per_s", 0, 1e12),
        "seed": _integer(fixed["seed"], "fixed_pattern.seed", 0, 2**32 - 1),
    }
    paths = model["paths"]
    if not isinstance(paths, (list, tuple)) or len(paths) != 2:
        raise ValueError("Shared-charge readout requires exactly two ordered paths")
    result["paths"] = []
    for index, path in enumerate(paths):
        _keys(path, PATH_KEYS, "conversion path")
        if path["stream_id"] != STREAM_IDS[index]:
            raise ValueError("Conversion paths must retain [HCG, LCG] order")
        normalized = {"stream_id": STREAM_IDS[index]}
        for key, bounds in {
            "conversion_gain_v_per_e": (1e-15, 1e3), "analog_gain": (1e-6, 1e6),
            "input_linear_ceiling_e": (1e-6, 1e12), "digital_gain": (1e-6, 1e6),
        }.items():
            normalized[key] = _number(path[key], "paths." + key, *bounds)
        adc = path["adc"]
        _keys(adc, {"bit_depth", "full_scale_v", "black_level_dn"}, "ADC")
        bits = _integer(adc["bit_depth"], "adc.bit_depth", 1, 24)
        white = 2**bits - 1
        black = _number(adc["black_level_dn"], "adc.black_level_dn", 0, white)
        if black >= white:
            raise ValueError("ADC black level must be below white level")
        normalized["adc"] = {
            "bit_depth": bits, "black_level_dn": black,
            "full_scale_v": _number(adc["full_scale_v"], "adc.full_scale_v", 1e-12, 1e6),
        }
        _calibration_path(normalized, result["shared_charge_capacity_e"])
        result["paths"].append(normalized)
    high, low = result["paths"]
    if high["conversion_gain_v_per_e"] <= low["conversion_gain_v_per_e"]:
        raise ValueError("HCG conversion gain must exceed LCG conversion gain")
    if high["adc"]["bit_depth"] != low["adc"]["bit_depth"]:
        raise ValueError("Both readout paths must have the same ADC bit depth")
    noise = model["read_noise"]
    _keys(noise, {"schema", "stream_ids", "covariance_e2", "temperature_range_c",
                  "source", "includes"}, "paired read noise")
    if (noise["schema"] != "camera_paired_read_noise_v1" or noise["stream_ids"] != STREAM_IDS
            or noise["includes"] != NOISE_INCLUDES):
        raise ValueError("Paired noise requires ordered input-referred total read components")
    domain = noise["temperature_range_c"]
    if not isinstance(domain, (list, tuple)) or len(domain) != 2:
        raise ValueError("Read covariance requires an explicit [min,max] temperature domain")
    domain = [_number(v, "read_noise.temperature_range_c", -273.15, 1000) for v in domain]
    if domain[0] > domain[1]:
        raise ValueError("Read covariance temperature domain is reversed")
    result["read_noise"] = {
        "schema": "camera_paired_read_noise_v1", "stream_ids": STREAM_IDS.copy(),
        "covariance_e2": _covariance(noise["covariance_e2"]), "temperature_range_c": domain,
        "source": _source(noise["source"], "read_noise.source"), "includes": NOISE_INCLUDES,
    }
    result["source"] = _source(model["source"], "source")
    return result


def default_model(legacy_readout):
    """An explicit synthetic two-path example; legacy data does not establish covariance.

    LCG preserves the legacy mean slope. HCG gain, ceiling and read noise are
    authored assumptions, with a one-point covariance temperature domain.
    """
    legacy = default_electrical_model(legacy_readout)
    paths = []
    for stream, multiplier in zip(STREAM_IDS, (4., 1.), strict=True):
        paths.append({
            "stream_id": stream,
            "conversion_gain_v_per_e": multiplier * legacy["conversion_gain_v_per_e"],
            "analog_gain": float(legacy_readout["gain"]),
            "input_linear_ceiling_e": legacy["full_well_e"] / multiplier,
            "digital_gain": 1., "adc": dict(legacy["adc"]),
        })
    variance = legacy["read_noise_e"] ** 2
    return validate_model({
        "schema": SCHEMA, "topology": TOPOLOGY, "stream_ids": STREAM_IDS.copy(),
        "shared_charge_capacity_e": legacy["full_well_e"],
        "dark_current_e_per_s": legacy["dark_current_e_per_s"],
        "reference_temperature_c": 25., "dark_current_doubling_c": None,
        "fixed_pattern": dict(legacy["fixed_pattern"]), "paths": paths,
        "read_noise": {
            "schema": "camera_paired_read_noise_v1", "stream_ids": STREAM_IDS.copy(),
            "covariance_e2": [[variance / 4, 0.], [0., variance]],
            "temperature_range_c": [25., 25.], "includes": NOISE_INCLUDES,
            "source": {"kind": "estimated", "description":
                       "Authored example: HCG RMS is half the legacy RMS; LCG uses legacy RMS. "
                       "Zero cross covariance is an explicit independence assumption at 25 C, "
                       "not inferred or measured paired read noise. All read terms included."},
        },
        "source": {"kind": "estimated", "description":
                   "Authored ideal nondestructive two-path example derived from legacy readout. "
                   "HCG conversion gain is 4x LCG with one-quarter path charge ceiling. "
                   "This does not identify a vendor DCG circuit or establish physical accuracy."},
    })


def _read_factor(covariance):
    a, b, c = covariance[0][0], covariance[0][1], covariance[1][1]
    if a == 0:
        return np.array([[0., 0.], [0., math.sqrt(c)]])
    first = math.sqrt(a)
    remainder = (Fraction(a) * Fraction(c) - Fraction(b)**2) / Fraction(a)
    return np.array([[first, 0.], [b / first, math.sqrt(float(remainder))]])


def validate_capture_conditions(model, exposure_s, temperature_c):
    """Check known scalar acquisition limits without allocating native maps or drawing noise."""
    model = validate_model(model)
    exposure = _number(exposure_s, "exposure_s", 1e-12, 3600)
    temperature = _number(temperature_c, "temperature_c", -273.15, 1000)
    low_temp, high_temp = model["read_noise"]["temperature_range_c"]
    if not low_temp <= temperature <= high_temp:
        raise ValueError("Temperature is outside the declared paired read covariance domain")
    # Refuse impossible temperature laws before allocating full native FPN maps.
    doubling = model["dark_current_doubling_c"]
    factor = 1.
    if doubling is not None:
        exponent = (temperature - model["reference_temperature_c"]) / doubling
        if not -1074 <= exponent <= 1023:
            raise ValueError("Dark-current temperature scaling exceeds supported numeric range")
        factor = math.exp2(exponent)
    if (not math.isfinite(factor) or factor <= 0
            or model["dark_current_e_per_s"] * factor > MAX_CHARGE_E
            or model["dark_current_e_per_s"] * factor * exposure > MAX_CHARGE_E):
        raise ValueError("Temperature-adjusted dark mean exceeds supported numeric range")
    return {"exposure_s": exposure, "temperature_c": temperature,
            "dark_current_temperature_scale": factor}


def _encode_delivered_charge(charge, model, noise_enabled, rng):
    covariance = model["read_noise"]["covariance_e2"] if noise_enabled else [[0., 0.], [0., 0.]]
    if any(value != 0 for row in covariance for value in row):
        noise = np.einsum("ij,jhw->ihw", _read_factor(covariance),
                          rng.standard_normal((2, *charge.shape)))
    else:
        noise = np.zeros((2, *charge.shape))
    arrays = {"read_noise_e": noise}
    per_path = {key: [] for key in ("raw_dn", "adc_dn", "path_saturated_mask", "adc_low_mask",
                                   "adc_high_mask", "digital_low_mask", "digital_high_mask")}
    calibration_paths = []
    for index, path in enumerate(model["paths"]):
        calibration = _calibration_path(path, model["shared_charge_capacity_e"])
        calibration_paths.append(calibration)
        black, white = calibration["black_level_dn"], calibration["white_level_dn"]
        path_mask = charge >= path["input_linear_ceiling_e"]
        path_charge = np.minimum(charge, path["input_linear_ceiling_e"]) + noise[index]
        adc_slope = calibration["dn_per_e"] / path["digital_gain"]
        continuous_adc = black + path_charge * adc_slope
        adc = np.clip(np.rint(continuous_adc), 0, white).astype(np.int32)
        continuous_raw = black + path["digital_gain"] * (adc.astype(float) - black)
        raw = np.clip(np.rint(continuous_raw), 0, white).astype(np.int32)
        for key, value in {
            "raw_dn": raw, "adc_dn": adc, "path_saturated_mask": path_mask,
            "adc_low_mask": continuous_adc <= 0, "adc_high_mask": continuous_adc >= white,
            "digital_low_mask": continuous_raw <= 0, "digital_high_mask": continuous_raw >= white,
        }.items():
            per_path[key].append(value)
    arrays.update({key: np.stack(value) for key, value in per_path.items()})
    return arrays, covariance, calibration_paths


def read_delivered_charge(delivered_charge_e, model, *, temperature_c, noise_enabled, rng):
    """Read the same delivered population through both paths with paired noise.

    Population sampling and the shared well bound belong to the caller; only the
    declared per-path input ceilings remain here. No exposure inverse is created.
    """
    model = validate_model(model)
    temperature = _number(temperature_c, "temperature_c", -273.15, 1000)
    low, high = model["read_noise"]["temperature_range_c"]
    if not low <= temperature <= high:
        raise ValueError("Temperature is outside the declared paired read covariance domain")
    charge = _delivered_charge(
        delivered_charge_e, model["shared_charge_capacity_e"], noise_enabled, rng, "standard_normal"
    )
    arrays, covariance, _ = _encode_delivered_charge(charge, model, noise_enabled, rng)
    return {
        "arrays": arrays,
        "readout_descriptor": _delivered_descriptor(
            model, temperature, noise_enabled, charge.shape, STREAM_IDS,
            read_noise_covariance_e2=[row.copy() for row in covariance],
            processing_order=[
                "per_path_input_linear_ceiling_clip", "joint_input_referred_read_noise",
                "conversion_and_analog_gain", "ADC_nearest_even_clip",
                "black_relative_digital_nearest_even_clip",
            ],
        ),
    }


def capture_shared_electrons(
    photo_mean, exposure_s, model, *, temperature_c, geometry, noise_enabled, seed
):
    """Generate one common charge and paired read noise; retain truth only as diagnostics."""
    model = validate_model(model)
    if (not isinstance(photo_mean, np.ndarray) or photo_mean.dtype.kind not in "iuf"
            or photo_mean.ndim != 2 or not 0 < photo_mean.size <= MAX_PIXELS):
        raise ValueError("photo_mean requires a real numeric HxW NumPy array, <=1000000 pixels")
    if (not np.isfinite(photo_mean).all() or np.any(photo_mean < 0)
            or np.any(photo_mean > MAX_CHARGE_E)):
        raise ValueError("Photoelectron mean is outside the nonnegative 1e13 electron bound")
    conditions = validate_capture_conditions(model, exposure_s, temperature_c)
    exposure, temperature = conditions["exposure_s"], conditions["temperature_c"]
    seed = _integer(seed, "seed", 0, 2**32 - 1)
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean")
    full, origin = _geometry(geometry, photo_mean.shape)
    photo = np.asarray(photo_mean, dtype=np.float64)
    prnu, dark_rate, pattern_summary = _patterns(model, full, origin, photo.shape, temperature)
    dark_e = dark_rate * exposure
    mean = photo * prnu + dark_e
    if not np.isfinite(mean).all() or np.any(mean > MAX_CHARGE_E):
        raise ValueError("Total mean charge exceeds the supported 1e13 electron range")
    rng = np.random.default_rng(seed)
    charge = rng.poisson(mean).astype(float) if noise_enabled else mean.copy()
    well_mask = charge >= model["shared_charge_capacity_e"]
    charge = np.clip(charge, 0, model["shared_charge_capacity_e"])
    encoded, covariance, calibration_paths = _encode_delivered_charge(
        charge, model, noise_enabled, rng
    )
    arrays = {
        "shared_charge_e": charge, "read_noise_e": encoded["read_noise_e"],
        "mean_total_charge_e": mean,
        "well_saturated_mask": well_mask, "photo_response_map": prnu,
        "dark_current_e_per_s_map": dark_rate,
    }
    arrays.update({key: value for key, value in encoded.items() if key != "read_noise_e"})
    return {
        "arrays": arrays,
        "calibration": {
            "schema": CALIBRATION_SCHEMA, "stream_ids": STREAM_IDS.copy(),
            "exposure_s": exposure, "paths": calibration_paths,
            "read_noise_covariance_e2": [row.copy() for row in covariance],
            "dark_e": dark_e.copy(), "geometry": {"full_sensor_shape": full, "crop_origin": origin},
        },
        "summary": {
            "schema": SCHEMA, "model": model, "stream_ids": STREAM_IDS.copy(),
            "geometry": {"full_sensor_shape": full, "crop_origin": origin,
                         "capture_shape": list(photo.shape)},
            "exposure_s": exposure, "temperature_c": temperature,
            "noise_enabled": noise_enabled, "noise_seed": seed,
            "read_covariance_factorization": "exact_PSD_2x2_no_jitter",
            "readout_count": 2, "shared_charge_sample_count_per_pixel": 1,
            "reset_model": "independent_reset_between_acquisitions",
            "topology": TOPOLOGY, **pattern_summary,
            "limits": {"max_capture_pixels": MAX_PIXELS, "max_native_pixels": MAX_NATIVE_PIXELS},
            "processing_order": ["PRNU_photo_and_temperature_dark_mean",
                                 "one_Poisson_charge_if_noise_enabled_else_mean",
                                 "shared_capacity_clip", "per_path_input_linear_ceiling_clip",
                                 "joint_input_referred_read_noise", "conversion_and_analog_gain",
                                 "ADC_nearest_even_clip",
                                 "black_relative_digital_nearest_even_clip"],
            "mask_definition": "well/path charge >= ceiling before read noise; ADC/digital "
                               "continuous code <=0 or >=white before nearest-even rounding",
            "calibration_linear_bounds": "final RAW transfer bounds before final rounding; "
                                         "include shared capacity, path ceiling and ADC/digital "
                                         "bounds",
            "rounding_bound_scope": "absolute input-electron bound in uncensored linear region; "
                                    "ADC half step plus final half step when digital gain !=1; "
                                    "not a variance or independence assumption",
            "simulation_oracle": {
                "arrays": ["shared_charge_e", "read_noise_e", "mean_total_charge_e"]
                          + [key for key in arrays if key.endswith("_mask")],
                "used_by_fusion": False,
            },
            "saturation_fractions": {key: float(value.mean()) for key, value in arrays.items()
                                     if key.endswith("_mask")},
            "array_units": {
                "raw_dn": "DN", "adc_dn": "DN", "shared_charge_e": "electron",
                "read_noise_e": "electron", "mean_total_charge_e": "electron",
                "photo_response_map": "1", "dark_current_e_per_s_map": "electron/pixel/s",
                **{key: "boolean" for key in arrays if key.endswith("_mask")},
            },
            "physical_validation": "not_established", "uncertainty": {"kind": "unknown"},
            "limitations": [
                "Ideal nondestructive two-path readout of one shared charge, not a vendor circuit.",
                "No charge splitting/transfer, reset/switch transient, CDS, RTS, 1/f or blooming.",
                "All temporal read terms are in paired covariance; no extra read noise is added.",
                "Shot noise is common to both paths and cannot be halved by averaging them.",
                "No independent quantization variance; deterministic rounding bound retained.",
                "No QE, CFA, fill factor or pixel-area multiplier is applied by this readout.",
                "Native PRNU/DSNU stay active with temporal noise off; no per-path fixed pattern.",
                "Full native fixed maps are allocated; budget full sensor, not crop alone.",
                "Read covariance covers only fixed path settings and declared temperature domain.",
                "RAW-only censoring cannot perfectly detect saturation masked by read noise.",
            ],
        },
    }
