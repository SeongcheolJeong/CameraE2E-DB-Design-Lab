"""Versioned shared-charge voltage readout and calibrated RAW-only DCG fusion.

Each path has its own monotone conversion response. One Poisson charge sample is
shared; paired voltage read noise is added after the path response, before analog
gain. Local inverse-slope covariance is an approximation, not total uncertainty.
The v1 linear transfer, calibrations, and stored evidence are not upgraded here.
"""

from __future__ import annotations

import hashlib
import math

import numpy as np

from . import charge_voltage_response as response
from . import dcg, dcg_electrical, electrical, nonlinear_readout
from .registry import digest

SCHEMA = "camera_shared_charge_readout_v2"
CALIBRATION_SCHEMA = "camera_shared_charge_calibration_v2"
MERGE_SCHEMA = "camera_dcg_merge_v2"
NOISE_SCHEMA = "camera_paired_voltage_read_noise_v1"
METHOD = "nonnegative_local_read_covariance_nonlinear_raw"
STREAM_IDS = ["HCG", "LCG"]
PATH_KEYS = (dcg_electrical.PATH_KEYS - {"conversion_gain_v_per_e"}) | {"charge_voltage_response"}
CALIBRATION_KEYS = {
    "schema",
    "stream_ids",
    "model",
    "model_sha256",
    "acquisition",
    "acquisition_sha256",
    "paths",
    "dark_e",
}
ACQUISITION_KEYS = {"exposure_s", "temperature_c", "geometry", "noise_enabled"}
COMMON_KEYS = {
    "dark_current_e_per_s",
    "reference_temperature_c",
    "dark_current_doubling_c",
    "fixed_pattern",
}


def _path_model(model, index):
    """Derive the entire single-path model; no independently authored calibration fields."""
    path, noise = model["paths"][index], model["read_noise"]
    return nonlinear_readout.validate_model(
        {
            "schema": nonlinear_readout.SCHEMA,
            "full_well_e": min(model["shared_charge_capacity_e"], path["input_linear_ceiling_e"]),
            **{key: model[key] for key in COMMON_KEYS},
            "charge_voltage_response": path["charge_voltage_response"],
            "digital_gain": path["digital_gain"],
            "adc": path["adc"],
            "read_noise": {
                "schema": nonlinear_readout.NOISE_SCHEMA,
                "plane": nonlinear_readout.NOISE_PLANE,
                "includes": nonlinear_readout.NOISE_INCLUDES,
                "variance_v2": noise["covariance_v2"][index][index],
                "temperature_range_c": noise["temperature_range_c"],
                "analog_gain_range": [path["analog_gain"], path["analog_gain"]],
                "source": noise["source"],
            },
            "source": model["source"],
        }
    )


def validate_model(model):
    electrical._keys(model, dcg_electrical.MODEL_KEYS, "nonlinear shared-charge model")
    if (
        model["schema"] != SCHEMA
        or model["topology"] != dcg_electrical.TOPOLOGY
        or model["stream_ids"] != STREAM_IDS
    ):
        raise ValueError("Nonlinear shared-charge model requires v2 and ordered HCG/LCG paths")
    result = {
        "schema": SCHEMA,
        "topology": dcg_electrical.TOPOLOGY,
        "stream_ids": STREAM_IDS.copy(),
        **{key: model[key] for key in COMMON_KEYS},
    }
    result["shared_charge_capacity_e"] = electrical._number(
        model["shared_charge_capacity_e"], "shared_charge_capacity_e", 1e-6, 1e12
    )
    noise = model["read_noise"]
    electrical._keys(
        noise,
        {
            "schema",
            "stream_ids",
            "plane",
            "covariance_v2",
            "temperature_range_c",
            "source",
            "includes",
        },
        "paired voltage noise",
    )
    if (
        noise["schema"] != NOISE_SCHEMA
        or noise["stream_ids"] != STREAM_IDS
        or noise["plane"] != nonlinear_readout.NOISE_PLANE
        or noise["includes"] != nonlinear_readout.NOISE_INCLUDES
    ):
        raise ValueError(
            "Paired voltage noise must include all temporal read terms before analog gain"
        )
    covariance = dcg_electrical._covariance(noise["covariance_v2"])
    if any(abs(value) > 1e6 for row in covariance for value in row):
        raise ValueError("Paired voltage covariance exceeds the supported V^2 domain")
    result["read_noise"] = {
        "schema": NOISE_SCHEMA,
        "stream_ids": STREAM_IDS.copy(),
        "plane": nonlinear_readout.NOISE_PLANE,
        "includes": nonlinear_readout.NOISE_INCLUDES,
        "covariance_v2": covariance,
        "temperature_range_c": nonlinear_readout._range(
            noise["temperature_range_c"], "paired noise temperature", -273.15, 1000
        ),
        "source": dcg_electrical._source(noise["source"], "read_noise.source"),
    }
    result["source"] = dcg_electrical._source(model["source"], "source")
    if not isinstance(model["paths"], (list, tuple)) or len(model["paths"]) != 2:
        raise ValueError("Nonlinear shared-charge model requires exactly two paths")
    paths = []
    for index, path in enumerate(model["paths"]):
        electrical._keys(path, PATH_KEYS, "nonlinear conversion path")
        if path["stream_id"] != STREAM_IDS[index]:
            raise ValueError("Nonlinear paths must retain HCG, LCG order")
        paths.append(
            {
                "stream_id": path["stream_id"],
                "analog_gain": electrical._number(path["analog_gain"], "analog_gain", 1e-6, 1e6),
                "input_linear_ceiling_e": electrical._number(
                    path["input_linear_ceiling_e"], "input_linear_ceiling_e", 1e-6, 1e12
                ),
                "charge_voltage_response": response.validate_response(
                    path["charge_voltage_response"]
                ),
                "digital_gain": path["digital_gain"],
                "adc": path["adc"],
            }
        )
    result["paths"] = paths
    for index, path in enumerate(paths):
        normalized = _path_model(result, index)
        result.update({key: normalized[key] for key in COMMON_KEYS})
        path.update(adc=normalized["adc"], digital_gain=normalized["digital_gain"])
        # The paired covariance is calibrated only for each fixed path setting.
        low, high = path["charge_voltage_response"]["valid_domain"]["analog_gain"]
        if not low <= path["analog_gain"] <= high:
            raise ValueError("Fixed path analog gain is outside its response domain")
    if paths[0]["adc"]["bit_depth"] != paths[1]["adc"]["bit_depth"]:
        raise ValueError("Both nonlinear paths must have the same ADC bit depth")
    origin_slopes = [
        path["charge_voltage_response"]["signal_voltage_v"][1]
        / path["charge_voltage_response"]["charge_e"][1]
        for path in paths
    ]
    if origin_slopes[0] <= origin_slopes[1]:
        raise ValueError("HCG origin conversion slope must exceed LCG before analog gain")
    return result


def validate_capture_conditions(model, exposure_s, temperature_c):
    model = validate_model(model)
    exposure = electrical._number(exposure_s, "exposure_s", 1e-12, 3600)
    temperature = electrical._number(temperature_c, "temperature_c", -273.15, 1000)
    for index, path in enumerate(model["paths"]):
        nonlinear_readout.validate_capture_conditions(
            _path_model(model, index), exposure, path["analog_gain"], temperature
        )
    factor = 1.0
    if model["dark_current_doubling_c"] is not None:
        exponent = (temperature - model["reference_temperature_c"]) / model[
            "dark_current_doubling_c"
        ]
        if not -1074 <= exponent <= 1023:
            raise ValueError("Dark-current temperature scaling exceeds supported numeric range")
        factor = math.exp2(exponent)
    if (
        not math.isfinite(factor)
        or factor <= 0
        or model["dark_current_e_per_s"] * factor > electrical.MAX_CHARGE_E
        or model["dark_current_e_per_s"] * factor * exposure > electrical.MAX_CHARGE_E
    ):
        raise ValueError("Temperature-adjusted dark mean exceeds supported numeric range")
    return {
        "exposure_s": exposure,
        "temperature_c": temperature,
        "dark_current_temperature_scale": factor,
    }


def _make_calibration(model, *, exposure_s, temperature_c, geometry, dark_e, noise_enabled):
    model = validate_model(model)
    conditions = validate_capture_conditions(model, exposure_s, temperature_c)
    paths, shared_dark = [], None
    for index, path in enumerate(model["paths"]):
        cal = nonlinear_readout.make_calibration(
            _path_model(model, index),
            analog_gain=path["analog_gain"],
            exposure_s=conditions["exposure_s"],
            temperature_c=conditions["temperature_c"],
            geometry=geometry,
            dark_e=dark_e,
            noise_enabled=noise_enabled,
        )
        shared_dark = cal.pop("dark_e")
        paths.append(cal)
    acquisition = {
        "exposure_s": conditions["exposure_s"],
        "temperature_c": conditions["temperature_c"],
        "geometry": paths[0]["acquisition"]["geometry"],
        "noise_enabled": noise_enabled,
    }
    return {
        "schema": CALIBRATION_SCHEMA,
        "stream_ids": STREAM_IDS.copy(),
        "model": model,
        "model_sha256": digest(model),
        "acquisition": acquisition,
        "acquisition_sha256": digest(acquisition),
        "paths": paths,
        "dark_e": shared_dark,
    }


def validate_calibration(calibration, shape=None):
    electrical._keys(calibration, CALIBRATION_KEYS, "nonlinear shared-charge calibration")
    if calibration["schema"] != CALIBRATION_SCHEMA or calibration["stream_ids"] != STREAM_IDS:
        raise ValueError("Calibration requires nonlinear shared-charge HCG/LCG schema")
    electrical._keys(calibration["acquisition"], ACQUISITION_KEYS, "DCG acquisition")
    expected = _make_calibration(
        calibration["model"], **calibration["acquisition"], dark_e=calibration["dark_e"]
    )
    if shape is not None and expected["dark_e"].shape != shape:
        raise ValueError("Shared dark calibration and RAW shape differ")
    for key in ("model_sha256", "acquisition_sha256", "paths"):
        if calibration[key] != expected[key]:
            raise ValueError("Shared-charge calibration model, path or acquisition binding differs")
    return expected


def calibration_for_path(calibration, index):
    index = electrical._integer(index, "path index", 0, 1)
    checked = validate_calibration(calibration)
    return {**checked["paths"][index], "dark_e": checked["dark_e"].copy()}


def _encode_delivered_charge(charge, model, noise_enabled, rng):
    covariance = model["read_noise"]["covariance_v2"] if noise_enabled else [[0.0, 0.0], [0.0, 0.0]]
    noise = np.zeros((2, *charge.shape))
    if any(v != 0 for row in covariance for v in row):
        noise = np.einsum(
            "ij,jhw->ihw",
            dcg_electrical._read_factor(covariance),
            rng.standard_normal((2, *charge.shape)),
        )
    arrays = {"read_noise_v": noise}
    per_path = []
    for index, path in enumerate(model["paths"]):
        clipped = np.minimum(charge, path["input_linear_ceiling_e"])
        voltage = response.evaluate_voltage(clipped, path["charge_voltage_response"]) + noise[index]
        encoded = nonlinear_readout.encode_voltage(voltage, path, path["analog_gain"])
        encoded["path_saturated_mask"] = charge >= path["input_linear_ceiling_e"]
        per_path.append(encoded)
    arrays.update({key: np.stack([path[key] for path in per_path]) for key in per_path[0]})
    return arrays, covariance


def read_delivered_charge(delivered_charge_e, model, *, temperature_c, noise_enabled, rng):
    """Encode one delivered total-charge population through two nonlinear paths.

    Shared sampling/well clipping, photo/dark generation and retained history
    belong to the caller. Paired voltage noise and path ceilings remain active.
    """
    model = validate_model(model)
    temperature = electrical._number(temperature_c, "temperature_c", -273.15, 1000)
    for index, path in enumerate(model["paths"]):
        nonlinear_readout._delivered_conditions(
            _path_model(model, index), path["analog_gain"], temperature
        )
    charge = electrical._delivered_charge(
        delivered_charge_e, model["shared_charge_capacity_e"], noise_enabled, rng, "standard_normal"
    )
    arrays, covariance = _encode_delivered_charge(charge, model, noise_enabled, rng)
    return {
        "arrays": arrays,
        "readout_descriptor": electrical._delivered_descriptor(
            model, temperature, noise_enabled, charge.shape, STREAM_IDS,
            read_noise_covariance_v2=[row.copy() for row in covariance],
            processing_order=[
                "per_path_input_linear_ceiling_clip", "charge_voltage_response",
                "joint_conversion_output_voltage_read_noise", "analog_gain",
                "ADC_nearest_even_clip", "black_relative_digital_nearest_even_clip",
            ],
        ),
    }


def capture_shared_electrons(
    photo_mean, exposure_s, model, *, temperature_c, geometry, noise_enabled, seed
):
    model = validate_model(model)
    conditions = validate_capture_conditions(model, exposure_s, temperature_c)
    if (
        not isinstance(photo_mean, np.ndarray)
        or photo_mean.dtype.kind not in "iuf"
        or photo_mean.ndim != 2
        or not 0 < photo_mean.size <= electrical.MAX_PIXELS
    ):
        raise ValueError("photo_mean requires a bounded real numeric HxW NumPy array")
    photo = photo_mean.astype(float)
    if not np.isfinite(photo).all() or np.any(photo < 0) or np.any(photo > electrical.MAX_CHARGE_E):
        raise ValueError("Photocharge is outside the finite nonnegative domain")
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean")
    seed = electrical._integer(seed, "seed", 0, 2**32 - 1)
    full, origin = electrical._geometry(geometry, photo.shape)
    prnu, dark_rate, pattern = electrical._patterns(
        model, full, origin, photo.shape, conditions["temperature_c"]
    )
    dark = dark_rate * conditions["exposure_s"]
    mean = photo * prnu + dark
    if not np.isfinite(mean).all() or np.any(mean > electrical.MAX_CHARGE_E):
        raise ValueError("Total charge mean exceeds the supported numeric domain")
    rng = np.random.default_rng(seed)
    sampled = rng.poisson(mean).astype(float) if noise_enabled else mean.copy()
    charge = np.minimum(sampled, model["shared_charge_capacity_e"])
    encoded, _ = _encode_delivered_charge(charge, model, noise_enabled, rng)
    arrays = {
        "shared_charge_e": charge,
        "read_noise_v": encoded["read_noise_v"],
        "mean_total_charge_e": mean,
        "well_saturated_mask": sampled >= model["shared_charge_capacity_e"],
        "photo_response_map": prnu,
        "dark_current_e_per_s_map": dark_rate,
    }
    arrays.update({key: value for key, value in encoded.items() if key != "read_noise_v"})
    calibration = _make_calibration(
        model,
        exposure_s=conditions["exposure_s"],
        temperature_c=conditions["temperature_c"],
        geometry=geometry,
        dark_e=dark,
        noise_enabled=noise_enabled,
    )
    units = {
        "raw_dn": "DN",
        "adc_dn": "DN",
        "shared_charge_e": "electron",
        "read_noise_v": "V",
        "mean_total_charge_e": "electron",
        "photo_response_map": "1",
        "dark_current_e_per_s_map": "electron/pixel/s",
        **{key: "boolean" for key in arrays if key.endswith("_mask")},
    }
    return {
        "arrays": arrays,
        "calibration": calibration,
        "summary": {
            "schema": SCHEMA,
            "model": model,
            "model_sha256": digest(model),
            "stream_ids": STREAM_IDS.copy(),
            "exposure_s": conditions["exposure_s"],
            "temperature_c": conditions["temperature_c"],
            "noise_seed": seed,
            "noise_enabled": noise_enabled,
            "topology": dcg_electrical.TOPOLOGY,
            "gain_path_ordering": "HCG origin conversion slope exceeds LCG before analog gain; "
            "local slope ordering may change with charge",
            "geometry": {
                "full_sensor_shape": full,
                "crop_origin": origin,
                "capture_shape": list(photo.shape),
            },
            **pattern,
            "readout_count": 2,
            "shared_charge_sample_count_per_pixel": 1,
            "read_covariance_factorization": "exact_PSD_2x2_no_jitter",
            "reset_model": "independent_reset_between_acquisitions",
            "array_units": units,
            "processing_order": [
                "PRNU_photo_and_temperature_dark_mean",
                "one_Poisson_charge",
                "shared_capacity_clip",
                "per_path_input_ceiling_clip",
                "per_path_charge_voltage_response",
                "joint_conversion_output_voltage_read_noise",
                "analog_gain",
                "ADC_nearest_even_clip",
                "black_relative_digital_nearest_even_clip",
            ],
            "simulation_oracle": {
                "arrays": ["shared_charge_e", "read_noise_v", "mean_total_charge_e"]
                + [key for key in arrays if key.endswith("_mask")],
                "used_by_fusion": False,
            },
            "saturation_fractions": {
                key: float(value.mean()) for key, value in arrays.items() if key.endswith("_mask")
            },
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "limitations": [
                "Ideal shared charge; no charge splitting, CDS dynamics or vendor circuit claim.",
                "All temporal read components belong to declared paired voltage covariance; "
                "none added twice.",
                "Shot charge is common to paths; averaging readouts cannot halve shot variance.",
                "Fixed response and noise domains are authored calibration assumptions, "
                "not measured accuracy.",
                "RAW guards cannot prove that noise did not hide physical clipping.",
            ],
        },
    }


def default_profile():
    return {**dcg.default_profile(), "schema": MERGE_SCHEMA, "method": METHOD}


def validate_profile(profile):
    electrical._keys(profile, set(default_profile()), "nonlinear DCG profile")
    if (
        profile["schema"] != MERGE_SCHEMA
        or profile["method"] != METHOD
        or profile["tie_break_stream"] != "HCG"
    ):
        raise ValueError("Nonlinear DCG requires v2 covariance fusion with explicit HCG tie policy")
    checked = dcg.validate_profile(
        {**profile, "schema": dcg.SCHEMA, "method": "nonnegative_read_covariance_raw"}
    )
    return {**checked, "schema": MERGE_SCHEMA, "method": METHOD}


def merge_shared_charge_raw(raw, calibration, profile):
    if (
        not isinstance(raw, np.ndarray)
        or raw.ndim != 3
        or raw.dtype.kind not in "iu"
        or raw.shape[0] != 2
        or any(n < 2 or n > 4096 or n % 2 for n in raw.shape[1:])
        or raw.shape[1] * raw.shape[2] > electrical.MAX_PIXELS
    ):
        raise ValueError("RAW requires integer [2,even H,even W] within 1000000 pixels")
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    selected = validate_profile(profile)
    cal = validate_calibration(calibration, raw.shape[1:])
    decoded = [
        nonlinear_readout.decode_raw(
            raw[index],
            {**cal["paths"][index], "dark_e": cal["dark_e"]},
            guard_sigma=selected["read_noise_guard_sigma"],
            saturation_margin_dn=selected["saturation_margin_dn"],
        )
        for index in range(2)
    ]
    total, slope, bounds = (
        np.stack([item["arrays"][key] for item in decoded])
        for key in ("total_charge_e", "inverse_slope_e_per_v", "quantization_bound_e")
    )
    usable = np.stack([item["arrays"]["valid_mask"] for item in decoded])
    valid, joint = usable.any(axis=0), usable.all(axis=0)
    total, slope, bounds = (np.where(usable, value, 0.0) for value in (total, slope, bounds))
    noise_on = cal["acquisition"]["noise_enabled"]
    covariance = (
        cal["model"]["read_noise"]["covariance_v2"] if noise_on else [[0.0, 0.0], [0.0, 0.0]]
    )
    factor = dcg_electrical._read_factor(covariance)
    # Transform the exact-PSD factor, not a rounded covariance followed by repair.
    # Squared factor sums stay nonnegative at perfectly anticorrelated boundaries.
    h0, l0, l1 = slope[0] * factor[0, 0], slope[1] * factor[1, 0], slope[1] * factor[1, 1]
    c00, c01, c11 = h0**2, h0 * l0, l0**2 + l1**2
    difference_variance = (h0 - l0) ** 2 + l1**2
    high_weight = np.ones(raw.shape[1:])
    np.divide(
        l0 * (l0 - h0) + l1**2, difference_variance, out=high_weight, where=difference_variance > 0
    )
    high_weight = np.clip(high_weight, 0.0, 1.0)
    weights = np.stack(
        [np.where(joint, high_weight, usable[0]), np.where(joint, 1.0 - high_weight, usable[1])]
    )
    combined = np.sum(weights * total, axis=0)
    read_variance = (weights[0] * h0 + weights[1] * l0) ** 2 + (weights[1] * l1) ** 2
    shot = np.maximum(combined, 0.0) if noise_on else np.zeros(raw.shape[1:])
    variance = shot + read_variance
    exposure = cal["acquisition"]["exposure_s"]
    merged = (combined - cal["dark_e"]) / exposure
    path_rate = (total - cal["dark_e"]) / exposure
    bound = np.sum(weights * bounds, axis=0)
    difference = total[0] - total[1]
    threshold = selected["gain_path_agreement_sigma"] * np.sqrt(difference_variance) + bounds.sum(
        axis=0
    )
    agreement = joint & (np.abs(difference) <= threshold)
    arrays = {
        "path_rate": path_rate,
        "usable_mask": usable,
        "weights": weights,
        "valid_mask": valid,
        "merged_rate": merged,
        "common_shot_variance_estimate_e2": shot,
        "read_variance_e2": read_variance,
        "shared_shot_read_variance_estimate_e2": variance,
        "shared_shot_read_rate_variance_estimate": variance / exposure / exposure,
        "rounding_bound_e": bound,
        "rounding_bound_rate": bound / exposure,
        "gain_path_difference_e": difference,
        "gain_path_agreement_mask": agreement,
        "gain_path_agreement_unknown_mask": ~joint,
        "gain_path_difference_read_variance_e2": difference_variance,
        "gain_path_agreement_threshold_e": threshold,
        "local_read_covariance_e2": np.stack([np.stack([c00, c01]), np.stack([c01, c11])]),
        "quantization_knot_crossing_mask": np.stack(
            [item["arrays"]["quantization_knot_crossing_mask"] for item in decoded]
        ),
    }
    for key, value in arrays.items():
        if value.dtype.kind == "f" and not np.isfinite(value).all():
            raise ValueError(
                "Nonlinear DCG calibration or fusion exceeds the finite domain: " + key
            )
    path_rate[~usable] = np.nan
    arrays["local_read_covariance_e2"][:, :, ~joint] = np.nan
    for key in (
        "gain_path_difference_e",
        "gain_path_difference_read_variance_e2",
        "gain_path_agreement_threshold_e",
    ):
        arrays[key][~joint] = np.nan
    for key in (
        "merged_rate",
        "common_shot_variance_estimate_e2",
        "read_variance_e2",
        "shared_shot_read_variance_estimate_e2",
        "shared_shot_read_rate_variance_estimate",
        "rounding_bound_e",
        "rounding_bound_rate",
    ):
        arrays[key][~valid] = np.nan
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        raise RuntimeError("RAW changed during nonlinear DCG fusion")
    units = {key: "boolean" for key, value in arrays.items() if value.dtype.kind == "b"}
    units.update(
        {
            "path_rate": "electron/pixel/s",
            "merged_rate": "electron/pixel/s",
            "weights": "1",
            **{
                key: "electron^2"
                for key in (
                    "common_shot_variance_estimate_e2",
                    "read_variance_e2",
                    "shared_shot_read_variance_estimate_e2",
                    "gain_path_difference_read_variance_e2",
                    "local_read_covariance_e2",
                )
            },
            "shared_shot_read_rate_variance_estimate": "(electron/pixel/s)^2",
            "rounding_bound_e": "electron",
            "rounding_bound_rate": "electron/pixel/s",
            "gain_path_difference_e": "electron",
            "gain_path_agreement_threshold_e": "electron",
        }
    )
    return {
        "arrays": arrays,
        "summary": {
            "schema": "camera_dcg_merge_result_v2",
            "method": METHOD,
            "profile": selected,
            "stream_ids": STREAM_IDS.copy(),
            "acquisitions": 1,
            "readouts": 2,
            "geometry": cal["acquisition"]["geometry"],
            "exposure_s": exposure,
            "input_shape": list(raw.shape),
            "input_dtype": raw.dtype.str,
            "input_array_bytes_sha256": before,
            "model_sha256": cal["model_sha256"],
            "acquisition_sha256": cal["acquisition_sha256"],
            "path_diagnostics": [
                {"stream_id": STREAM_IDS[i], **item["summary"]} for i, item in enumerate(decoded)
            ],
            "valid_pixels": int(valid.sum()),
            "invalid_pixels": int((~valid).sum()),
            "both_usable_pixels": int(joint.sum()),
            "single_usable_path_pixels": int((usable.sum(axis=0) == 1).sum()),
            "gain_path_agreement_pixels": int(agreement.sum()),
            "gain_path_disagreement_pixels": int((joint & ~agreement).sum()),
            "gain_path_agreement_unknown_pixels": int((~joint).sum()),
            "array_units": units,
            "covariance_regularization": "none; transform exact-PSD declared factor "
            "by local inverse slope",
            "weight_objective": "per-pixel minimize read covariance only; "
            "nonnegative sum-one weights",
            "zero_curvature_policy": "HCG tie; deterministic quantization bounds "
            "do not create a variance",
            "rate_estimator": "(sum(w_j * decoded_total_charge_j) - shared_dark_e) / exposure_s",
            "common_shot_estimator": "one max(weighted total charge,0); "
            "zero with temporal noise disabled",
            "gain_path_agreement": "abs(z_H-z_L)<=sigma*sqrt(C00+C11-2*C01)+bound_H+bound_L",
            "variance_scope": "local inverse-slope paired voltage read covariance "
            "plus one shared shot plug-in; not total uncertainty",
            "rounding_scope": "weighted numerical RAW preimage bounds with float64 allowance; "
            "not a formal IEEE interval proof or independent quantization variance",
            "invalid_policy": "NaN physical arrays and false masks; all-invalid weights zero",
            "simulation_oracle_used": False,
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "limitations": [
                "Inverse-slope covariance is local and approximate near nonlinear knots.",
                "RAW-derived guards do not certify absence of clipping "
                "or undo censoring selection bias.",
                "Common shot variance is counted once, never independently per readout.",
                "Agreement flags are diagnostics and never silently replace data "
                "or reweight disagreement.",
                "Calibration uncertainty, model mismatch and total physical uncertainty "
                "remain unknown.",
            ],
        },
    }
