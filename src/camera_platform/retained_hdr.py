"""Causal RAW-only photo-rate HDR for an authored trap-free retained PD.

The inverse follows the declared partial-transfer recurrence. It never reads
simulated reservoir/photons and refuses unknown state after a censored read
until a declared PD clear. Static illumination is established by the runtime.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

import numpy as np

from . import electrical, hdr, retained_charge

SCHEMA = "camera_retained_photo_hdr_profile_v1"
RESULT_SCHEMA = "camera_retained_photo_hdr_result_v1"
ARRAY_UNITS = {
    "delivered_estimate_e": "electron/pixel",
    "residual_estimate_e": "electron/pixel",
    "photo_estimate_e": "electron/pixel",
    "photo_rate_e_s": "electron/pixel/s",
    "exposure_valid_mask": "boolean",
    "state_known_mask": "boolean",
    "temporal_consistency_mask": "boolean",
    "temporal_consistency_unknown_mask": "boolean",
    "merged_rate_e_s": "electron/pixel/s",
    "valid_mask": "boolean",
    "selected_frame_index": "index",
    "linear_rgb": "electron/pixel/s",
    "linear_rgb_valid_mask": "boolean",
    "rgb": "1",
}


def default_profile():
    return {
        "schema": SCHEMA,
        "saturation_margin_dn": 2.0,
        "read_noise_guard_sigma": 3.0,
        "consistency_sigma": 5.0,
        "preview": hdr.default_profile(),
    }


def validate_profile(value):
    if not isinstance(value, Mapping) or set(value) != set(default_profile()):
        raise ValueError("Retained photo HDR requires its exact profile fields")
    if value["schema"] != SCHEMA:
        raise ValueError("Unsupported retained photo HDR profile")
    selected = {"schema": SCHEMA, "preview": hdr.validate_profile(value["preview"])}
    for name, positive in (("saturation_margin_dn", True),
                           ("read_noise_guard_sigma", False),
                           ("consistency_sigma", True)):
        selected[name] = hdr._number(value[name], name, positive=positive)
    return selected


def reconstruct_and_select(
    raw, electrical_model, reservoir_model, profile, *, exposures_s, gaps_s,
    clears, analog_gains, temperature_c, noise_enabled, geometry,
    photo_exposures_s=None,
):
    """Select the longest valid exposure per pixel after causal state inverse.

    Output photo rate includes PRNU and excludes model-derived gap/exposure
    dark. When supplied, photo_exposures_s is the exact per-row integral of
    an authored light waveform; dark still accumulates for the full physical
    exposure. The estimator does not use the runtime's actual charge ledger.
    """
    model = electrical.validate_electrical_model(electrical_model)
    reservoir = retained_charge.validate_model(reservoir_model)
    selected = validate_profile(profile)
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean")
    if (not isinstance(raw, np.ndarray) or raw.ndim != 3 or raw.dtype.kind not in "iu"
            or not 2 <= raw.shape[0] <= 16 or any(n < 2 or n % 2 for n in raw.shape[1:])):
        raise ValueError("Retained photo HDR requires 2..16 native integer RAW frames")
    count, height, width = raw.shape
    if raw.size > hdr.MAX_FRAME_PIXELS:
        raise ValueError("Retained photo HDR exceeds the total pixel budget")
    if not isinstance(geometry, Mapping) or set(geometry) != {
        "kind", "full_sensor_shape", "crop_origin"
    }:
        raise ValueError("Retained photo HDR requires exact native geometry")
    geom, pattern = hdr._geometry(geometry, (height, width))
    if geom["crop_origin"] != [0, 0] or geom["full_sensor_shape"] != [height, width]:
        raise ValueError("Retained photo HDR requires the full native sensor")
    exposure = hdr._array(exposures_s, (count,), "exposures_s")
    if np.any(exposure <= 0) or np.unique(exposure).size < 2:
        raise ValueError("Retained photo HDR requires two distinct positive exposures")
    photo_exposure = (
        np.broadcast_to(exposure[:, None], (count, height)).copy()
        if photo_exposures_s is None
        else hdr._array(photo_exposures_s, (count, height), "photo_exposures_s")
    )
    if np.any(photo_exposure < 0):
        raise ValueError("Retained photo HDR requires nonnegative photo exposure integrals")
    gaps = hdr._array(gaps_s, (count, height), "gaps_s")
    if np.any(gaps < 0) or np.any(gaps[0] != 0):
        raise ValueError("Retained photo HDR requires nonnegative gaps and zero first gap")
    gain = hdr._array(analog_gains, (count,), "analog_gains")
    if np.any(gain <= 0):
        raise ValueError("Retained photo HDR analog gains must be positive")
    if (not isinstance(clears, (list, tuple)) or len(clears) != count
            or any(type(value) is not bool for value in clears)):
        raise ValueError("Retained photo HDR needs one declared PD clear per frame")
    if model["full_well_e"] != reservoir["capacity_e"]:
        raise ValueError("Retained photo HDR well capacities must agree")
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    _, dark, _ = electrical._patterns(
        model, (height, width), (0, 0), (height, width), temperature_c
    )
    site = ((np.arange(height)[:, None] % 2) * 2
            + np.arange(width)[None, :] % 2)
    efficiency = np.asarray(reservoir["transfer_efficiency"])[site]
    if np.any(efficiency <= 0):
        raise ValueError("Zero transfer efficiency makes photocharge unobservable")
    black = model["adc"]["black_level_dn"]
    white = 2 ** model["adc"]["bit_depth"] - 1
    estimates = np.full(raw.shape, np.nan, dtype=np.float64)
    delivered = np.full_like(estimates, np.nan)
    residual = np.full_like(estimates, np.nan)
    rates = np.full_like(estimates, np.nan)
    valid = np.zeros(raw.shape, dtype=bool)
    known = np.zeros(raw.shape, dtype=bool)
    conditional_variance = np.full_like(estimates, np.nan)
    previous_residual = np.zeros((height, width), dtype=np.float64)
    previous_variance = np.zeros((height, width), dtype=np.float64)
    previous_known = np.ones((height, width), dtype=bool)
    frame_diagnostics = []
    for i in range(count):
        volts_per_e = model["conversion_gain_v_per_e"] * gain[i]
        adc_dn_per_e = volts_per_e * white / model["adc"]["full_scale_v"]
        dn_per_e = adc_dn_per_e * model["digital_gain"]
        ceiling = min(white, black + model["full_well_e"] * dn_per_e,
                      black + model["digital_gain"] * (white - black))
        floor = float(np.clip(black * (1 - model["digital_gain"]), 0, white))
        readvar = (model["read_noise_e"] ** 2
                   + (model["post_gain_noise_v"] / volts_per_e) ** 2) if noise_enabled else 0.0
        guard = max(selected["saturation_margin_dn"],
                    selected["read_noise_guard_sigma"] * np.sqrt(readvar) * dn_per_e)
        code = raw[i].astype(np.float64)
        delivered_i = (code - black) / dn_per_e
        pretransfer = delivered_i / efficiency
        # The transfer population may saturate before the ADC. A guard in
        # electron units also rejects ambiguous capacity-adjacent samples.
        capacity_guard = np.maximum(
            1.0, selected["read_noise_guard_sigma"]
            * np.sqrt(readvar + np.maximum(delivered_i, 0))
        )
        read_valid = ((code > floor + guard) & (code < ceiling - guard)
                      & (pretransfer < reservoir["capacity_e"] - capacity_guard)
                      & (pretransfer >= 0))
        inherited = np.ones_like(previous_known) if clears[i] else previous_known
        gap_charge = dark * gaps[i, :, None]
        gap_safe = np.ones_like(previous_known) if clears[i] else (
            previous_residual + gap_charge < reservoir["capacity_e"]
        )
        illuminated = photo_exposure[i, :, None] > 0
        current_state_known = read_valid & inherited & gap_safe
        current_valid = current_state_known & illuminated
        incoming = 0.0 if clears[i] else previous_residual + gap_charge
        photo = pretransfer - incoming - dark * exposure[i]
        rate = np.divide(
            photo, photo_exposure[i, :, None],
            out=np.full_like(photo, np.nan), where=illuminated,
        )
        residual_i = pretransfer - delivered_i
        quantum_var = 1 / (12 * adc_dn_per_e ** 2)
        if model["digital_gain"] != 1:
            quantum_var += 1 / (12 * dn_per_e ** 2)
        # This is a conditional diagnostic. Shot/transfer/read components and
        # inter-frame covariance need a measured uncertainty model to certify.
        total_var = np.maximum(delivered_i, 0) + readvar + quantum_var
        rate_var = np.divide(
            total_var / efficiency ** 2 + (0 if clears[i] else previous_variance),
            photo_exposure[i, :, None] ** 2,
            out=np.full_like(photo, np.nan), where=illuminated,
        )
        estimates[i][current_valid] = photo[current_valid]
        delivered[i][read_valid] = delivered_i[read_valid]
        residual[i][read_valid] = residual_i[read_valid]
        rates[i][current_valid] = rate[current_valid]
        conditional_variance[i][current_valid] = rate_var[current_valid]
        valid[i] = current_valid
        known[i] = current_state_known
        previous_residual = residual_i
        previous_variance = ((1 - efficiency) / efficiency) ** 2 * total_var
        previous_known = current_state_known
        frame_diagnostics.append({
            "frame_index": i, "valid_pixels": int(current_valid.sum()),
            "invalid_pixels": int((~current_valid).sum()),
            "read_censored_pixels": int((~read_valid).sum()),
            "clear_before_exposure": clears[i],
            "analog_gain": float(gain[i]),
            "unilluminated_rows": int(np.count_nonzero(~illuminated[:, 0])),
        })
    usable = valid.sum(axis=0)
    consistency = np.ones((height, width), dtype=bool)
    for i in range(count):
        for j in range(i + 1, count):
            both = valid[i] & valid[j]
            threshold = selected["consistency_sigma"] * np.sqrt(
                np.maximum(conditional_variance[i], 0)
                + np.maximum(conditional_variance[j], 0)
            )
            consistency &= ~both | (np.abs(rates[i] - rates[j]) <= threshold)
    unknown = usable < 2
    # Static-scene provenance is required by the caller; an inconsistent pixel
    # is unknown rather than silently choosing one conflicting exposure.
    output_valid = (usable > 0) & (unknown | consistency)
    choices = np.where(valid, photo_exposure[:, :, None], -np.inf).argmax(axis=0)
    merged = np.take_along_axis(rates, choices[None], axis=0)[0]
    merged[~output_valid] = np.nan
    choices = np.where(output_valid, choices, -1).astype(np.int16)
    linear, rgb_valid, preview = hdr._rgb(
        merged, output_valid, pattern, selected["preview"]
    )
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        raise RuntimeError("Original retained RAW changed during HDR estimation")
    arrays = {
        "delivered_estimate_e": delivered,
        "residual_estimate_e": residual,
        "photo_estimate_e": estimates,
        "photo_rate_e_s": rates,
        "exposure_valid_mask": valid,
        "state_known_mask": known,
        "temporal_consistency_mask": consistency,
        "temporal_consistency_unknown_mask": unknown,
        "merged_rate_e_s": merged,
        "valid_mask": output_valid,
        "selected_frame_index": choices,
        "linear_rgb": linear,
        "linear_rgb_valid_mask": rgb_valid,
        "rgb": preview,
    }
    return {"arrays": arrays, "summary": {
        "schema": RESULT_SCHEMA,
        "quantity": "model_conditioned_current_exposure_photo_rate_after_PRNU",
        "source_raw_sha256": before,
        "source_raw_shape": list(raw.shape),
        "profile": selected,
        "calibration_kind": "model_derived_not_measured",
        "state_initialization": "zero_at_first_row_exposure",
        "inverse": "RAW_to_delivered_to_pretransfer_minus_prior_residual_gap_dark_exposure_dark",
        "photo_time_basis": (
            "physical_exposure_s" if photo_exposures_s is None
            else "authored_waveform_row_integral_s"
        ),
        "state_truth_used": False,
        "static_scene_required": True,
        "constant_light_required": photo_exposures_s is None,
        "known_waveform_required": photo_exposures_s is not None,
        "frame_diagnostics": frame_diagnostics,
        "valid_pixels": int(output_valid.sum()),
        "invalid_pixels": int((~output_valid).sum()),
        "single_usable_exposure_pixels": int((usable == 1).sum()),
        "temporal_inconsistent_pixels": int((~consistency & ~unknown).sum()),
        "selected_frame_counts": [int(((choices == i) & output_valid).sum())
                                  for i in range(count)],
        "array_units": ARRAY_UNITS.copy(),
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "limitations": [
            "Only one authored trap-free retained PD species and linear single-path readout.",
            "Model-derived transfer, dark and gain response are not measured calibration.",
            "Unknown residual state propagates until a declared PD clear.",
            "RAW censoring cannot certify absence of saturation hidden by read noise.",
            "Longest-valid photo-integral selection and pairwise agreement do not register motion.",
            "Authored PWM timing is not measured LED/clock calibration or physical LFM accuracy.",
            "No trap inversion or physical HDR accuracy established.",
        ],
    }}
