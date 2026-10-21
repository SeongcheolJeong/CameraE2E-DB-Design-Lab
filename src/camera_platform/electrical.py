"""Bounded electron-to-RAW readout for independent sequential exposures.

Photocharge already includes the optical/CFA/collection response. Fixed sensor
patterns persist across exposures and native crops; temporal draws do not.
This is an explicit model, not a measured circuit or shared-charge DCG model.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Real

import numpy as np

SCHEMA = "camera_electrical_readout_v1"
MAX_PIXELS = 1_000_000
MAX_NATIVE_PIXELS = 16_777_216
MAX_CHARGE_E = 1e13
DELIVERED_READOUT_SCHEMA = "camera_delivered_charge_readout_v1"
CDS_NOISE_SCHEMA = "camera_cds_correlated_read_noise_v1"
CDS_RESULT_SCHEMA = "camera_cds_correlated_readout_result_v1"
MODEL_KEYS = {
    "schema",
    "full_well_e",
    "conversion_gain_v_per_e",
    "read_noise_e",
    "dark_current_e_per_s",
    "reference_temperature_c",
    "dark_current_doubling_c",
    "post_gain_noise_v",
    "digital_gain",
    "adc",
    "fixed_pattern",
}


def _keys(value, expected, name):
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{name} requires exactly {sorted(expected)}")


def _number(value, name, low, high):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} is outside the supported range") from error
    if not np.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} is outside the supported range")
    return result


def _integer(value, name, low, high):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be an integer")
    result = int(value)
    if not low <= result <= high:
        raise ValueError(f"{name} is outside the supported range")
    return result


def validate_electrical_model(model):
    """Return a new normalized model; unknown fields and implicit modes are refused."""
    _keys(model, MODEL_KEYS, "electrical model")
    if model["schema"] != SCHEMA:
        raise ValueError("Unsupported electrical readout schema")
    result = {"schema": SCHEMA}
    bounds = {
        "full_well_e": (1e-6, 1e12),
        "conversion_gain_v_per_e": (1e-15, 1e3),
        "read_noise_e": (0, 1e6),
        "dark_current_e_per_s": (0, 1e12),
        "reference_temperature_c": (-273.15, 1000),
        "post_gain_noise_v": (0, 1e3),
        "digital_gain": (1e-6, 1e6),
    }
    for key, (low, high) in bounds.items():
        result[key] = _number(model[key], key, low, high)
    doubling = model["dark_current_doubling_c"]
    result["dark_current_doubling_c"] = (
        None if doubling is None else _number(doubling, "dark_current_doubling_c", 1e-6, 1000)
    )
    adc = model["adc"]
    _keys(adc, {"bit_depth", "full_scale_v", "black_level_dn"}, "ADC")
    bits = _integer(adc["bit_depth"], "adc.bit_depth", 1, 24)
    white = 2**bits - 1
    black = _number(adc["black_level_dn"], "adc.black_level_dn", 0, white)
    if black >= white:
        raise ValueError("ADC black level must be below white level")
    result["adc"] = {
        "bit_depth": bits,
        "full_scale_v": _number(adc["full_scale_v"], "adc.full_scale_v", 1e-12, 1e6),
        "black_level_dn": black,
    }
    fixed = model["fixed_pattern"]
    _keys(fixed, {"prnu_sigma", "dsnu_e_per_s", "seed"}, "fixed_pattern")
    result["fixed_pattern"] = {
        "prnu_sigma": _number(fixed["prnu_sigma"], "fixed_pattern.prnu_sigma", 0, 10),
        "dsnu_e_per_s": _number(fixed["dsnu_e_per_s"], "fixed_pattern.dsnu_e_per_s", 0, 1e12),
        "seed": _integer(fixed["seed"], "fixed_pattern.seed", 0, 2**32 - 1),
    }
    return result


def validate_capture_conditions(model, exposure_s, analog_gain, temperature_c):
    """Reject impossible v1 dark-temperature conditions before native-map allocation.

    This checks the declared mean law, not the unbounded tails of a stochastic
    DSNU distribution. The latter remains checked on the actual native map.
    """
    model = validate_electrical_model(model)
    exposure = _number(exposure_s, "exposure_s", 0, 3600)
    analog = _number(analog_gain, "analog_gain", 1e-6, 1e6)
    temperature = _number(temperature_c, "temperature_c", -273.15, 1000)
    _linear_ceiling(model, analog)
    doubling = model["dark_current_doubling_c"]
    factor = 1.0
    if doubling is not None:
        exponent = (temperature - model["reference_temperature_c"]) / doubling
        if not -1074 <= exponent <= 1023:
            raise ValueError("Dark-current temperature scaling exceeds supported numeric range")
        factor = math.exp2(exponent)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("Dark-current temperature scaling exceeds supported numeric range")
    with np.errstate(over="ignore"):
        expected_rate = model["dark_current_e_per_s"] * factor
        expected_dark = expected_rate * exposure
    if not math.isfinite(expected_rate) or expected_rate > MAX_CHARGE_E:
        raise ValueError("Temperature-adjusted dark current exceeds the supported range")
    if not math.isfinite(expected_dark) or expected_dark > MAX_CHARGE_E:
        raise ValueError(
            "Total mean charge from temperature-adjusted dark current exceeds "
            "the supported 1e13 electron range"
        )
    return {
        "exposure_s": exposure,
        "analog_gain": analog,
        "temperature_c": temperature,
        "dark_current_temperature_scale": factor,
    }


def _linear_ceiling(model, analog):
    """Use the readout's exact arithmetic order for an early span check."""
    adc = model["adc"]
    white, black = 2 ** adc["bit_depth"] - 1, adc["black_level_dn"]
    volts_per_e = model["conversion_gain_v_per_e"] * analog
    adc_dn_per_e = volts_per_e * white / adc["full_scale_v"]
    dn_per_e = adc_dn_per_e * model["digital_gain"]
    ceiling = min(
        white,
        black + model["full_well_e"] * dn_per_e,
        black + model["digital_gain"] * (white - black),
    )
    if ceiling <= black:
        raise ValueError("Electron-to-RAW linear span is below supported floating precision")
    return ceiling


def default_electrical_model(legacy_readout):
    """Map the legacy six-field transfer; pass its gain as capture analog_gain.

    Synthetic 1 V full-well swing and the pedestal-reserved ADC scale reproduce
    legacy DN/e. The resulting model keeps those quantities independently editable.
    """
    _keys(
        legacy_readout,
        {
            "full_well_e",
            "read_noise_e",
            "dark_current_e_per_s",
            "gain",
            "bit_depth",
            "black_level_dn",
        },
        "legacy readout",
    )
    full_well = _number(legacy_readout["full_well_e"], "full_well_e", 1e-6, 1e12)
    _number(legacy_readout["gain"], "gain", 1e-6, 1e6)
    bits = _integer(legacy_readout["bit_depth"], "bit_depth", 1, 24)
    white = 2**bits - 1
    black = _number(legacy_readout["black_level_dn"], "black_level_dn", 0, white)
    if black >= white:
        raise ValueError("Legacy black level must be below white level")
    return validate_electrical_model(
        {
            "schema": SCHEMA,
            "full_well_e": full_well,
            "conversion_gain_v_per_e": 1 / full_well,
            "read_noise_e": legacy_readout["read_noise_e"],
            "dark_current_e_per_s": legacy_readout["dark_current_e_per_s"],
            "reference_temperature_c": 25,
            "dark_current_doubling_c": None,
            "post_gain_noise_v": 0,
            "digital_gain": 1,
            "adc": {
                "bit_depth": bits,
                "full_scale_v": white / (white - black),
                "black_level_dn": black,
            },
            "fixed_pattern": {"prnu_sigma": 0, "dsnu_e_per_s": 0, "seed": 0},
        }
    )


def _geometry(geometry, shape):
    _keys(geometry, {"full_sensor_shape", "crop_origin"}, "geometry")
    full, origin = geometry["full_sensor_shape"], geometry["crop_origin"]
    if not isinstance(full, (list, tuple)) or len(full) != 2:
        raise ValueError("full_sensor_shape must contain two dimensions")
    if not isinstance(origin, (list, tuple)) or len(origin) != 2:
        raise ValueError("crop_origin must contain two coordinates")
    full = [_integer(v, "full_sensor_shape", 1, MAX_NATIVE_PIXELS) for v in full]
    origin = [_integer(v, "crop_origin", 0, MAX_NATIVE_PIXELS - 1) for v in origin]
    if full[0] * full[1] > MAX_NATIVE_PIXELS:
        raise ValueError("Full native sensor exceeds the 16777216 pixel limit")
    if any(
        start + count > maximum for start, count, maximum in zip(origin, shape, full, strict=True)
    ):
        raise ValueError("Crop is outside the full native sensor")
    return full, origin


def _patterns(model, full, origin, shape, temperature):
    fixed = model["fixed_pattern"]
    prnu_seed, dark_seed = np.random.SeedSequence(fixed["seed"]).spawn(2)
    sigma_log = np.sqrt(np.log1p(fixed["prnu_sigma"] ** 2))
    prnu = np.random.default_rng(prnu_seed).lognormal(
        mean=-0.5 * sigma_log**2, sigma=sigma_log, size=full
    )
    dark = model["dark_current_e_per_s"] + np.random.default_rng(dark_seed).normal(
        0, fixed["dsnu_e_per_s"], size=full
    )
    clipped = dark < 0
    dark = np.maximum(dark, 0)
    doubling = model["dark_current_doubling_c"]
    with np.errstate(over="ignore", under="ignore"):
        factor = (
            1.0
            if doubling is None
            else float(np.exp2((temperature - model["reference_temperature_c"]) / doubling))
        )
    if not np.isfinite(factor) or factor <= 0:
        raise ValueError("Dark-current temperature scaling exceeds the supported numeric range")
    region = tuple(slice(start, start + count) for start, count in zip(origin, shape, strict=True))
    with np.errstate(over="ignore"):
        dark = dark[region] * factor
    if not np.isfinite(dark).all() or np.any(dark > MAX_CHARGE_E):
        raise ValueError("Temperature-adjusted dark current exceeds the supported range")
    return (
        prnu[region].copy(),
        dark,
        {
            "temperature_scale": factor,
            "negative_dark_current_clipped_fraction_full_sensor": float(clipped.mean()),
            "negative_dark_current_clipped_fraction_crop": float(clipped[region].mean()),
        },
    )


def validate_cds_noise_profile(profile):
    """Normalize an opt-in sampled reset/signal noise contract in input electrons.

    This replaces, rather than augments, both legacy aggregate read-noise terms.
    A row or column term is shared by all sites on that native row or column.
    """
    _keys(
        profile,
        {"schema", "reference_plane", "sample_order", "reset_common_sigma_e",
         "pixel", "row", "column", "temperature_range_c", "source"},
        "CDS read-noise profile",
    )
    if (profile["schema"] != CDS_NOISE_SCHEMA
            or profile["reference_plane"] != "read_node_input_referred_electron"
            or profile["sample_order"] != ["reset", "signal", "signal_minus_reset"]):
        raise ValueError("CDS profile requires the declared sample chronology and reference plane")
    domain = profile["temperature_range_c"]
    if not isinstance(domain, (list, tuple)) or len(domain) != 2:
        raise ValueError("CDS temperature range requires [minimum, maximum]")
    domain = [_number(value, "CDS temperature_c", -273.15, 1000) for value in domain]
    if domain[0] > domain[1]:
        raise ValueError("CDS temperature range is reversed")
    source = profile["source"]
    _keys(source, {"kind", "description"}, "CDS source")
    if source["kind"] not in {"estimated", "simulated", "measured"}:
        raise ValueError("CDS source kind must be estimated, simulated or measured")
    if (not isinstance(source["description"], str)
            or not 1 <= len(source["description"].strip()) <= 4000):
        raise ValueError("CDS source description requires 1..4000 nonblank characters")
    result = {
        "schema": CDS_NOISE_SCHEMA,
        "reference_plane": "read_node_input_referred_electron",
        "sample_order": ["reset", "signal", "signal_minus_reset"],
        "reset_common_sigma_e": _number(
            profile["reset_common_sigma_e"], "reset_common_sigma_e", 0, 1e6
        ),
        "temperature_range_c": domain,
        "source": {"kind": source["kind"], "description": source["description"].strip()},
    }
    for axis in ("pixel", "row", "column"):
        value = profile[axis]
        _keys(
            value, {"reset_sigma_e", "signal_sigma_e", "sample_correlation"},
            f"CDS {axis} noise",
        )
        result[axis] = {
            "reset_sigma_e": _number(value["reset_sigma_e"], f"{axis}.reset_sigma_e", 0, 1e6),
            "signal_sigma_e": _number(
                value["signal_sigma_e"], f"{axis}.signal_sigma_e", 0, 1e6
            ),
            "sample_correlation": _number(
                value["sample_correlation"], f"{axis}.sample_correlation", -1, 1
            ),
        }
    return result


def default_cds_noise_profile():
    """A visibly authored example, not a sensor calibration or measured CDS result."""
    return validate_cds_noise_profile({
        "schema": CDS_NOISE_SCHEMA,
        "reference_plane": "read_node_input_referred_electron",
        "sample_order": ["reset", "signal", "signal_minus_reset"],
        "reset_common_sigma_e": 3.0,
        "pixel": {"reset_sigma_e": 0.8, "signal_sigma_e": 0.8,
                  "sample_correlation": 0.5},
        "row": {"reset_sigma_e": 0.2, "signal_sigma_e": 0.2,
                "sample_correlation": 0.5},
        "column": {"reset_sigma_e": 0.2, "signal_sigma_e": 0.2,
                   "sample_correlation": 0.5},
        "temperature_range_c": [25.0, 25.0],
        "source": {"kind": "estimated", "description":
                   "Authored synthetic reset/signal, row and column sample noise at 25 C. "
                   "The shared reset-node term cancels under ideal CDS; no measured circuit "
                   "accuracy or temporal cross-frame covariance is established."},
    })


def _cds_variance(profile):
    return sum(
        (part["reset_sigma_e"] - part["sample_correlation"]
         * part["signal_sigma_e"]) ** 2
        + (1 - part["sample_correlation"] ** 2) * part["signal_sigma_e"] ** 2
        for part in (profile[axis] for axis in ("pixel", "row", "column"))
    )


def _cds_samples(profile, full, origin, shape, seed, noise_enabled):
    """Draw in full native coordinates so crop identity does not shift noise sites."""
    reset = np.zeros(shape, dtype=np.float64)
    signal = np.zeros(shape, dtype=np.float64)
    residual = np.zeros(shape, dtype=np.float64)
    if not noise_enabled:
        return reset, signal, residual
    streams = np.random.SeedSequence(seed).spawn(7)
    region = tuple(slice(start, start + count) for start, count in zip(origin, shape, strict=True))
    if profile["reset_common_sigma_e"]:
        common = np.random.default_rng(streams[0]).standard_normal(full)[region].copy()
        common *= profile["reset_common_sigma_e"]
        reset += common
        signal += common
    for index, axis in enumerate(("pixel", "row", "column")):
        setting = profile[axis]
        if not setting["reset_sigma_e"] and not setting["signal_sigma_e"]:
            continue
        dimensions = full if axis == "pixel" else (full[0] if axis == "row" else full[1])
        region_axis = (region if axis == "pixel" else region[0] if axis == "row" else region[1])
        z0 = np.random.default_rng(streams[1 + 2 * index]).standard_normal(dimensions)
        z1 = np.random.default_rng(streams[2 + 2 * index]).standard_normal(dimensions)
        z0 = z0[region_axis].copy()
        z1 = z1[region_axis].copy()
        if axis == "row":
            z0, z1 = z0[:, None], z1[:, None]
        elif axis == "column":
            z0, z1 = z0[None, :], z1[None, :]
        rho = setting["sample_correlation"]
        reset_part = setting["reset_sigma_e"] * z0
        signal_part = setting["signal_sigma_e"] * (
            rho * z0 + math.sqrt(1 - rho * rho) * z1
        )
        reset += reset_part
        signal += signal_part
        residual += signal_part - reset_part
    # Subtract the common reset node algebraically, before adding the delivered
    # charge. This avoids loss of small CDS residuals beside a large charge.
    return reset, signal, residual


def _preflight_seeded_dark_charge(model, geometry, shape, temperature, max_exposure_s):
    """Check the actual native DSNU tail and maximum dark population before optics.

    The model and temperature were already validated by the acquisition path.
    Native geometry is checked again here before allocating its deterministic
    full-sensor pattern. Photocharge still requires the later optical check.
    """
    if not model["fixed_pattern"]["dsnu_e_per_s"]:
        return
    exposure = _number(max_exposure_s, "max_exposure_s", 0, 3600)
    full, origin = _geometry(
        {key: geometry[key] for key in ("full_sensor_shape", "crop_origin")}, shape
    )
    _, dark_rate, _ = _patterns(model, full, origin, shape, temperature)
    with np.errstate(over="ignore"):
        peak_dark_e = float(np.max(dark_rate)) * exposure
    if not math.isfinite(peak_dark_e) or peak_dark_e > MAX_CHARGE_E:
        raise ValueError("Seeded native dark charge exceeds the supported 1e13 electron range")


def _delivered_charge(delivered_charge_e, capacity_e, noise_enabled, rng, rng_method):
    """Check an already capacity-bounded population; never repair or resample it."""
    if (
        not isinstance(delivered_charge_e, np.ndarray)
        or delivered_charge_e.dtype.kind not in "iuf"
        or delivered_charge_e.ndim != 2
        or not 0 < delivered_charge_e.size <= MAX_PIXELS
    ):
        raise ValueError("delivered_charge_e requires a bounded real numeric HxW NumPy array")
    charge = delivered_charge_e.astype(np.float64, copy=True)
    if not np.isfinite(charge).all() or np.any(charge < 0) or np.any(charge > capacity_e):
        raise ValueError("Delivered charge must already lie within the read-node capacity")
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean")
    if not callable(getattr(rng, rng_method, None)):
        raise ValueError(f"rng must provide {rng_method} for the declared read-noise stream")
    return charge


def _delivered_descriptor(model, temperature, noise_enabled, shape, stream_ids, **extra):
    """Forward transfer evidence; it is deliberately not an exposure calibration."""
    return {
        "schema": DELIVERED_READOUT_SCHEMA,
        "source_model_schema": model["schema"],
        "model": model,
        "input_quantity": "delivered_total_read_node_charge_e",
        "input_history": "external_population_may_include_photo_dark_and_retained_charge",
        "input_shape": list(shape),
        "stream_ids": stream_ids.copy(),
        "temperature_c": temperature,
        "noise_enabled": noise_enabled,
        "population_processing": "none_already_sampled_and_capacity_bounded",
        "inverse_calibration": "not_generated_no_current_exposure_or_photocharge_claim",
        "physical_validation": "not_established",
        **extra,
    }


def _encode_delivered_charge(charge, model, analog, noise_enabled, rng):
    # The order and arithmetic grouping are shared with the legacy capture path.
    # charge is owned by this call; a public caller's retained state is never mutated.
    if noise_enabled and model["read_noise_e"]:
        charge += rng.normal(0, model["read_noise_e"], charge.shape)
    volts_per_e = model["conversion_gain_v_per_e"] * analog
    volts = charge * volts_per_e
    if noise_enabled and model["post_gain_noise_v"]:
        volts += rng.normal(0, model["post_gain_noise_v"], charge.shape)
    adc = model["adc"]
    white, black = 2 ** adc["bit_depth"] - 1, adc["black_level_dn"]
    adc_dn_per_e = volts_per_e * white / adc["full_scale_v"]
    continuous_adc = black + volts * white / adc["full_scale_v"]
    adc_low, adc_high = continuous_adc <= 0, continuous_adc >= white
    adc_dn = np.clip(np.rint(continuous_adc), 0, white).astype(np.int32)
    continuous_raw = black + model["digital_gain"] * (adc_dn.astype(float) - black)
    digital_low, digital_high = continuous_raw <= 0, continuous_raw >= white
    raw = np.clip(np.rint(continuous_raw), 0, white).astype(np.int32)
    dn_per_e = adc_dn_per_e * model["digital_gain"]
    linear_floor = float(np.clip(black * (1 - model["digital_gain"]), 0, white))
    linear_ceiling = _linear_ceiling(model, analog)
    adc_quantization = 1 / (12 * adc_dn_per_e**2)
    final_quantization = 0 if model["digital_gain"] == 1 else 1 / (12 * dn_per_e**2)
    return {
        "raw_dn": raw,
        "adc_dn": adc_dn,
        "adc_low_mask": adc_low,
        "adc_high_mask": adc_high,
        "digital_low_mask": digital_low,
        "digital_high_mask": digital_high,
    }, {
        "volts_per_e": volts_per_e,
        "dn_per_e": dn_per_e,
        "black_level_dn": black,
        "white_level_dn": white,
        "linear_floor_dn": linear_floor,
        "linear_ceiling_dn": linear_ceiling,
        "adc_quantization": adc_quantization,
        "final_quantization": final_quantization,
    }


def read_delivered_charge(
    delivered_charge_e, model, *, analog_gain, temperature_c, noise_enabled, rng
):
    """Encode total delivered read-node charge with the supplied read-noise RNG.

    The caller owns population sampling, PRNU/dark generation and well capacity.
    Mixed retained/photo/dark charge cannot be labeled current-exposure photocharge.
    No legacy inverse calibration is generated, and input state is not mutated.
    """
    model = validate_electrical_model(model)
    analog = _number(analog_gain, "analog_gain", 1e-6, 1e6)
    temperature = _number(temperature_c, "temperature_c", -273.15, 1000)
    charge = _delivered_charge(
        delivered_charge_e, model["full_well_e"], noise_enabled, rng, "normal"
    )
    arrays, _ = _encode_delivered_charge(charge, model, analog, noise_enabled, rng)
    return {
        "arrays": arrays,
        "readout_descriptor": _delivered_descriptor(
            model, temperature, noise_enabled, charge.shape, ["single"], analog_gain=analog,
            processing_order=[
                "input_read_noise", "conversion_and_analog_gain", "post_gain_voltage_noise",
                "ADC_nearest_even_clip", "black_relative_digital_gain_nearest_even_clip",
            ],
        ),
    }


def capture_electrons(
    photo_mean, exposure_s, model, *, analog_gain, temperature_c, geometry, noise_enabled, seed,
    cds_profile=None,
):
    """Sample one independently reset exposure, preserving native fixed-pattern identity.

    Calibration contains declared RAW-domain conversion/noise parameters only;
    it does not reconstruct a scene or use the supplied noiseless photocharge.
    """
    model = validate_electrical_model(model)
    profile = None if cds_profile is None else validate_cds_noise_profile(cds_profile)
    if profile is not None and (model["read_noise_e"] or model["post_gain_noise_v"]):
        raise ValueError("CDS profile replaces aggregate input and post-gain read noise")
    if not isinstance(photo_mean, np.ndarray) or photo_mean.dtype.kind not in "iuf":
        raise ValueError("photo_mean must be a real numeric NumPy array")
    if photo_mean.ndim != 2 or not photo_mean.size or photo_mean.size > MAX_PIXELS:
        raise ValueError("photo_mean requires a nonempty HxW array of at most 1000000 pixels")
    photo = np.asarray(photo_mean, dtype=np.float64)
    if not np.isfinite(photo).all() or np.any(photo < 0) or np.any(photo > MAX_CHARGE_E):
        raise ValueError("Photoelectron mean is outside the supported nonnegative range")
    conditions = validate_capture_conditions(model, exposure_s, analog_gain, temperature_c)
    exposure = conditions["exposure_s"]
    analog = conditions["analog_gain"]
    temperature = conditions["temperature_c"]
    if profile is not None and not (
        profile["temperature_range_c"][0] <= temperature <= profile["temperature_range_c"][1]
    ):
        raise ValueError("Temperature is outside the declared CDS noise domain")
    seed = _integer(seed, "seed", 0, 2**32 - 1)
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean")
    full, origin = _geometry(geometry, photo.shape)
    prnu, dark_rate, pattern_summary = _patterns(model, full, origin, photo.shape, temperature)
    dark_e = dark_rate * exposure
    mean = photo * prnu + dark_e
    if not np.isfinite(mean).all() or np.any(mean > MAX_CHARGE_E):
        raise ValueError("Total mean charge exceeds the supported 1e13 electron range")
    rng = np.random.default_rng(seed)
    charge = rng.poisson(mean).astype(float) if noise_enabled else mean.copy()
    well_mask = charge >= model["full_well_e"]
    charge = np.clip(charge, 0, model["full_well_e"])
    cds_reset = cds_signal = cds_residual = None
    if profile is not None:
        cds_reset, cds_signal, cds_residual = _cds_samples(
            profile, full, origin, photo.shape, seed, noise_enabled
        )
        charge += cds_residual
    encoded, transfer = _encode_delivered_charge(charge, model, analog, noise_enabled, rng)
    volts_per_e, dn_per_e = transfer["volts_per_e"], transfer["dn_per_e"]
    black, white = transfer["black_level_dn"], transfer["white_level_dn"]
    linear_floor, linear_ceiling = transfer["linear_floor_dn"], transfer["linear_ceiling_dn"]
    adc_quantization = transfer["adc_quantization"]
    final_quantization = transfer["final_quantization"]
    arrays = {
        "raw_dn": encoded["raw_dn"],
        "adc_dn": encoded["adc_dn"],
        "photo_response_map": prnu,
        "dark_current_e_per_s_map": dark_rate,
        "mean_total_charge_e": mean,
        "well_saturated_mask": well_mask,
        "adc_low_mask": encoded["adc_low_mask"],
        "adc_high_mask": encoded["adc_high_mask"],
        "digital_low_mask": encoded["digital_low_mask"],
        "digital_high_mask": encoded["digital_high_mask"],
        **({
            "cds_reset_sample_noise_e": cds_reset,
            "cds_signal_sample_noise_e": cds_signal,
            "cds_residual_noise_e": cds_residual,
        } if profile is not None else {}),
    }
    return {
        "arrays": arrays,
        "calibration": {
            "dn_per_e": dn_per_e,
            "black_level_dn": black,
            "white_level_dn": white,
            "full_well_e": model["full_well_e"],
            "dark_e": dark_e.copy(),
            "read_noise_variance_e2": (
                _cds_variance(profile) if profile is not None else
                model["read_noise_e"] ** 2 + (model["post_gain_noise_v"] / volts_per_e) ** 2
            )
            if noise_enabled
            else 0.0,
            "quantization_variance_e2": adc_quantization + final_quantization,
            "linear_floor_dn": linear_floor,
            "linear_ceiling_dn": linear_ceiling,
        },
        "summary": {
            "schema": CDS_RESULT_SCHEMA if profile is not None else SCHEMA,
            "model": model,
            **({"cds_read_noise": profile,
                "cds_variance_interpretation":
                "per_pixel_marginal_only; row/column covariance is retained in arrays; "
                "not an independent-pixel HDR weight"} if profile is not None else {}),
            "geometry": {
                "full_sensor_shape": full,
                "crop_origin": origin,
                "capture_shape": list(photo.shape),
            },
            "exposure_s": exposure,
            "analog_gain": analog,
            "temperature_c": temperature,
            "noise_enabled": noise_enabled,
            "noise_seed": seed,
            "limits": {"max_capture_pixels": MAX_PIXELS, "max_native_pixels": MAX_NATIVE_PIXELS},
            **pattern_summary,
            "reset_model": (
                "fresh_reset_sample_and_signal_sample_per_sequential_exposure"
                if profile is not None else "independent_reset_per_sequential_exposure"
            ),
            "adc_full_scale_definition": "voltage span before adding the digital code pedestal",
            "calibration_linear_bounds": "final RAW transfer bounds before final rounding; "
            "floor includes ADC-zero clipping after black-relative digital gain",
            "mask_definition": "well charge >= capacity; stage continuous code <= 0 or >= white, "
            "before nearest-even rounding and clipping",
            "fixed_pattern_model": "full_native_seeded_lognormal_PRNU_and_clipped_normal_dark_DSNU",
            "processing_order": [
                "PRNU_photo_and_temperature_dark_mean",
                "Poisson_charge",
                "full_well_clip",
                ("reset_node_draw_reset_sample_signal_sample_CDS_subtraction"
                 if profile is not None else "input_read_noise"),
                "conversion_and_analog_gain",
                "post_gain_voltage_noise",
                "ADC_nearest_even_clip",
                "black_relative_digital_gain_nearest_even_clip",
            ],
            "saturation_fractions": {
                key: float(value.mean()) for key, value in arrays.items() if key.endswith("_mask")
            },
            "array_units": {
                "raw_dn": "DN",
                "adc_dn": "DN",
                "photo_response_map": "1",
                "dark_current_e_per_s_map": "electron/pixel/s",
                "mean_total_charge_e": "electron",
                **({"cds_reset_sample_noise_e": "electron",
                    "cds_signal_sample_noise_e": "electron",
                    "cds_residual_noise_e": "electron"} if profile is not None else {}),
                **{key: "boolean" for key in arrays if key.endswith("_mask")},
            },
            "quantization_variance_model": {
                "adc_input_e2": adc_quantization,
                "digital_rounding_input_e2": final_quantization,
                "meaning": "independent uniform rounding approximation; not exact at low signal, "
                "integer digital gain or clipping",
            },
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "limitations": [
                ("Independent sequential exposures with native row/column within-frame "
                 "covariance; no cross-frame or shared-charge DCG covariance."
                 if profile is not None else
                 "Independent sequential exposures; no shared-charge DCG or "
                 "read-noise covariance."),
                ("Ideal sampled CDS; no circuit bandwidth, 1/f spectrum, reset memory, "
                 "blooming, charge transfer or transport."
                 if profile is not None else
                 "Ideal ADC; no INL/DNL, reset memory, blooming, charge transfer or transport."),
                "Dark-current doubling is an authored model, not measured temperature accuracy.",
                "Clipping negative DSNU-adjusted current changes its mean and distribution.",
                "FPN remains active when temporal noise is disabled; no additional QE/CFA factors.",
                "Native fixed maps are generated in full (up to 16777216 pixels); capture crops "
                "remain limited to 1000000 pixels and hosts must budget full-map memory.",
                "RAW calibration does not remove PRNU or establish physical HDR accuracy.",
            ],
        },
    }
