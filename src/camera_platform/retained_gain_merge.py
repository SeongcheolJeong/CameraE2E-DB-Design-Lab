"""RAW-only gain-path merge of one stateful, delivered charge population.

The estimate is delivered *total* charge, including retained and dark charge.
It is never a current-exposure photocharge or scene-radiance estimate. The
underlying DCG math uses the actual exposure for calibration validity, then
multiplies its rate coordinate back to charge with zero dark subtraction.
"""

from __future__ import annotations

import hashlib

import numpy as np

from . import dcg, dcg_electrical, nonlinear_dcg

SCHEMA = "camera_retained_gain_merge_result_v1"
ARRAY_UNITS = {
    "delivered_charge_e": "electron/pixel",
    "path_estimate_e": "electron/pixel",
    "valid_mask": "boolean",
    "path_usable_mask": "boolean",
    "path_weights": "1",
    "rounding_bound_e": "electron/pixel",
    "path_agreement_mask": "boolean",
    "path_agreement_unknown_mask": "boolean",
}


def validate_profile(model, profile):
    """Match the merge profile to the declared linear or nonlinear pair."""
    schema = model.get("schema") if isinstance(model, dict) else None
    if schema == dcg_electrical.SCHEMA:
        dcg_electrical.validate_model(model)
        return dcg.validate_profile(profile)
    if schema == nonlinear_dcg.SCHEMA:
        nonlinear_dcg.validate_model(model)
        return nonlinear_dcg.validate_profile(profile)
    raise ValueError("Retained gain merge requires a shared HCG/LCG readout model")


def default_profile(model):
    schema = model.get("schema") if isinstance(model, dict) else None
    if schema == dcg_electrical.SCHEMA:
        return validate_profile(model, dcg.default_profile())
    if schema == nonlinear_dcg.SCHEMA:
        return validate_profile(model, nonlinear_dcg.default_profile())
    raise ValueError("Retained gain merge requires a shared HCG/LCG readout model")


def merge_delivered_raw(
    raw, model, profile, *, exposure_s, temperature_c, noise_enabled, geometry
):
    """Merge original HCG/LCG codes without state truth, dark or exposure inverse.

    The caller must establish that both paths read the same delivered charge.
    A calibrated camera must replace the authored model with validated measured
    response/covariance before claiming predictive or physical accuracy.
    """
    selected = validate_profile(model, profile)
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean")
    if not isinstance(raw, np.ndarray) or raw.ndim != 3 or raw.shape[0] != 2:
        raise ValueError("Retained gain merge requires original [HCG,LCG,H,W] RAW")
    if not isinstance(geometry, dict) or set(geometry) != {
        "full_sensor_shape", "crop_origin"
    }:
        raise ValueError("Retained gain merge requires explicit native geometry")
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    dark_zero = np.zeros(raw.shape[1:], dtype=np.float64)
    if model["schema"] == dcg_electrical.SCHEMA:
        checked = dcg_electrical.validate_model(model)
        exposure = dcg_electrical.validate_capture_conditions(
            checked, exposure_s, temperature_c
        )["exposure_s"]
        covariance = (
            checked["read_noise"]["covariance_e2"] if noise_enabled
            else [[0.0, 0.0], [0.0, 0.0]]
        )
        calibration = {
            "schema": dcg.CALIBRATION_SCHEMA,
            "stream_ids": dcg.STREAM_IDS.copy(),
            "exposure_s": exposure,
            "paths": [
                dcg_electrical._calibration_path(path, checked["shared_charge_capacity_e"])
                for path in checked["paths"]
            ],
            "read_noise_covariance_e2": covariance,
            "dark_e": dark_zero,
            "geometry": geometry,
        }
        merged = dcg.merge_shared_charge_raw(raw, calibration, selected)
    else:
        checked = nonlinear_dcg.validate_model(model)
        exposure = nonlinear_dcg.validate_capture_conditions(
            checked, exposure_s, temperature_c
        )["exposure_s"]
        calibration = nonlinear_dcg._make_calibration(
            checked, exposure_s=exposure, temperature_c=temperature_c,
            geometry=geometry, dark_e=dark_zero, noise_enabled=noise_enabled,
        )
        merged = nonlinear_dcg.merge_shared_charge_raw(raw, calibration, selected)
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        raise RuntimeError("Original RAW changed during retained gain merge")
    source = merged["arrays"]
    arrays = {
        "delivered_charge_e": source["merged_rate"] * exposure,
        "path_estimate_e": source["path_rate"] * exposure,
        "valid_mask": source["valid_mask"],
        "path_usable_mask": source["usable_mask"],
        "path_weights": source["weights"],
        "rounding_bound_e": source["rounding_bound_e"],
        "path_agreement_mask": source["gain_path_agreement_mask"],
        "path_agreement_unknown_mask": source["gain_path_agreement_unknown_mask"],
    }
    return {
        "arrays": arrays,
        "summary": {
            "schema": SCHEMA,
            "quantity": "signed_delivered_total_charge_estimate_e",
            "source_raw_sha256": before,
            "source_raw_shape": list(raw.shape),
            "readout_ids": ["HCG", "LCG"],
            "model_schema": checked["schema"],
            "profile": selected,
            "calibration_kind": "model_derived_not_measured",
            "internal_numeric_coordinate": "actual_exposure_rate_then_multiply_back_zero_dark",
            "actual_exposure_s": exposure,
            "actual_exposure_normalization": False,
            "dark_or_history_correction": False,
            "current_exposure_photocharge_estimation": False,
            "shared_population_provenance": "caller_must_establish",
            "valid_pixels": int(arrays["valid_mask"].sum()),
            "invalid_pixels": int((~arrays["valid_mask"]).sum()),
            "agreement_pixels": int(arrays["path_agreement_mask"].sum()),
            "array_units": ARRAY_UNITS.copy(),
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "limitations": [
                "RAW boundary guards cannot certify absence of clipping hidden by read noise.",
                "The estimate contains charge from earlier frames, dark and trap release.",
                "No current-exposure radiance, temporal HDR fusion or motion correction.",
                "Authored gain response and covariance are not measured camera calibration.",
            ],
        },
    }
