"""Explicit synthetic spectral/ISP model transfer onto one immutable DB source.

This does not register a physical camera calibration. Source records and their
electrical conditions remain unchanged; the DB capture uses its own readout.
The service must verify typed artifact ownership and source manifests before
calling these pure contract functions, then freeze the returned document.
"""

from __future__ import annotations

import re
from copy import deepcopy

import numpy as np

from . import calibration_ccm, calibration_spectral
from .registry import canonical, digest

SCHEMA = "camera_db_color_resolved_profile_v1"
WAVELENGTH_NM = np.arange(450.0, 651.0, 10.0)
SOURCE_FIELDS = {"run_id", "candidate_id", "condition_id"}
BINDING_FIELDS = {"mode", "angular_assumption", "green_sites", "target_camera_id"}


def _object(value, name, *, allowed=None, required=()):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    if (allowed is not None and set(value) - set(allowed)) or set(required) - set(value):
        raise ValueError(f"{name} has missing or unsupported fields")
    try:
        content = canonical(value)
    except (TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must contain finite JSON values") from exc
    if len(content.encode()) > 32 * 1024**2:
        raise ValueError(f"{name} exceeds the 32 MiB contract limit")
    return deepcopy(value)


def _identity(value, name, pattern=r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}"):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"Invalid {name}")
    return value


def _sha(value, name):
    return _identity(value, name, r"[0-9a-f]{64}")


def _number(value, name, *, low=0, positive=False):
    if type(value) not in (int, float) or not np.isfinite(value) or value < low:
        raise ValueError(f"{name} requires a finite number >= {low}")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)


def _source(value):
    source = _object(value, "source", allowed=SOURCE_FIELDS, required={"run_id", "candidate_id"})
    _identity(source["run_id"], "run_id", r"run-[0-9a-f]{32}")
    _identity(source["candidate_id"], "candidate_id", r"candidate-[0-9]{2,6}")
    if "condition_id" in source:
        _identity(source["condition_id"], "condition_id", r"condition-[0-9]{3,6}")
    return source


def model_spec(payload):
    """Normalize the create-model request; measured transfer is deliberately unsupported."""
    fields = {"name", "source", "color_calibration_id", "binding"}
    data = _object(payload, "DB color model", allowed=fields, required=fields)
    if not isinstance(data["name"], str) or not 1 <= len(data["name"].strip()) <= 200:
        raise ValueError("Model name requires 1..200 nonempty characters")
    data["name"] = data["name"].strip()
    data["source"] = _source(data["source"])
    _identity(
        data["color_calibration_id"], "color_calibration_id", r"color-calibration-[0-9a-f]{32}"
    )
    binding = _object(data["binding"], "binding", allowed=BINDING_FIELDS, required=BINDING_FIELDS)
    if binding["mode"] != "synthetic_model_transfer":
        raise ValueError(
            "Only explicit synthetic_model_transfer is supported; measured transfer is not"
        )
    if binding["angular_assumption"] != "angle_independent":
        raise ValueError(
            "Spectral scans do not identify angular response; declare angle_independent"
        )
    if binding["green_sites"] != "shared_green":
        raise ValueError("Three-channel QE requires explicit shared_green for native Gr and Gb")
    _identity(binding["target_camera_id"], "new synthetic target_camera_id")
    data["binding"] = binding
    return data


def _color_profiles(record, identity):
    color = _object(record, "color calibration record")
    if color.get("id") != identity or color.get("schema") != "camera_spectral_color_record_v1":
        raise ValueError("Color calibration identity/schema differs from the requested record")
    checksum = _sha(color.get("content_hash"), "color record content_hash")
    if digest({k: v for k, v in color.items() if k != "content_hash"}) != checksum:
        raise ValueError("Color calibration record content hash mismatch")
    if color.get("source_kind") != "synthetic":
        raise ValueError("Measured calibration cannot be relabeled as synthetic model transfer")
    if color.get("status") != "pass" or color.get("physical_validation") != "not_established":
        raise ValueError(
            "Transfer requires a completed numerical pass with physical status unestablished"
        )
    try:
        qe, wave, values = calibration_spectral._profile(color["qe"]["profile"])
        ccm = calibration_ccm._profile(color["ccm"]["profile"])
        raw_saved = color["raw_validation"]["candidate"]
        ccm_saved = color["ccm"]["validation"]
        inputs = color["evidence"]["inputs"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "Color calibration is missing its frozen profiles/validation evidence"
        ) from exc
    if qe["source_kind"] != "synthetic" or ccm["source_kind"] != "synthetic":
        raise ValueError("Both QE and CCM must retain synthetic source_kind")
    if ccm["conditions"] != qe["conditions"]:
        raise ValueError("QE and CCM use different frozen camera conditions")
    if color.get("candidate_binding") != digest({"qe": qe, "ccm": ccm}):
        raise ValueError("Color candidate binding differs from its QE/CCM pair")
    if color.get("raw_unchanged_by_ccm") is not True:
        raise ValueError("Color calibration does not establish untouched RAW evidence")
    try:
        raw = calibration_spectral.validate_raw(qe, inputs["raw_validation"])
        evaluated = calibration_ccm.validate_ccm(ccm, ccm_saved["validation_payload"])
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "Transfer requires preserved RAW and independent CCM validation inputs"
        ) from exc
    if raw["status"] != "pass" or evaluated["status"] != "pass":
        raise ValueError("Transfer requires passed RAW and independently held-out CCM checks")
    if digest(raw) != digest(raw_saved) or digest(evaluated) != digest(ccm_saved):
        raise ValueError("Saved validation differs from the frozen QE/CCM re-evaluation")
    if ccm["target_space"] == "camera_linear_rgb":
        metrics = evaluated["comparison"]["metrics"]["candidate"]
        if any(
            metrics.get(key) is not None for key in ("delta_e00", "delta_e00_mean", "delta_e00_max")
        ):
            raise ValueError("Camera-linear output reproduction does not establish Delta E")
    return color, qe, ccm, wave, values


def freeze_model(spec, source_context, color_record, projection):
    """Resolve a synthetic replacement while retaining original sensor identity/evidence.

    ``projection`` comes from the verified DB source adapter, with electronics,
    pixel_pitch_um, cfa and fixed conditions; optional wavelength_nm must equal
    this adapter's native 450:10:650 nm grid. It is never supplied by the client.
    The source CCM was evaluated on its original spectral/exposure conditions;
    its pass is not a validation of this new DB model.
    """
    request = model_spec(spec)
    context = _object(source_context, "source_context")
    source = request["source"]
    if any(context.get(key) != source.get(key) for key in SOURCE_FIELDS):
        raise ValueError("Resolved DB source differs from the requested exact source tuple")
    pins = {}
    for key in ("run_request_sha256", "run_result_sha256", "manifest_sha256", "candidate_sha256"):
        pins[key] = _sha(context.get(key), key)
    if source.get("condition_id"):
        pins["condition_sha256"] = _sha(context.get("condition_sha256"), "condition_sha256")
    color, qe, ccm, wave, values = _color_profiles(color_record, request["color_calibration_id"])
    if request["binding"]["target_camera_id"] == qe["conditions"]["camera_id"]:
        raise ValueError(
            "Transfer requires a new derived synthetic camera ID; preserve the original"
        )
    db = _object(projection, "verified source projection")
    if db.get("cfa") != "RGGB":
        raise ValueError("DB color transfer requires the native RGGB site order")
    _number(db.get("pixel_pitch_um"), "DB pixel pitch", positive=True)
    electronics = _object(db.get("electronics"), "DB electronics")
    bits = electronics.get("bit_depth")
    if type(bits) is not int or not 1 <= bits <= 32:
        raise ValueError("DB readout requires integer bit_depth in 1..32")
    if 2**bits - 1 != qe["adc_white_dn"]:
        raise ValueError("QE and source DB ADC ranges differ; readout conversion is not inferred")
    for key in ("dn_per_e", "full_well_e"):
        _number(electronics.get(key), "DB " + key, positive=True)
    for key in ("black_level_dn", "read_noise_e", "dark_current_e_per_s"):
        _number(electronics.get(key), "DB " + key)
    if electronics["black_level_dn"] >= 2**bits - 1:
        raise ValueError("DB pedestal reaches ADC white")
    conditions = _object(db.get("conditions"), "DB conditions")
    for key in ("gain", "temperature_c"):
        if conditions.get(key) != qe["conditions"][key]:
            raise ValueError(f"DB {key} differs from the frozen spectral calibration")
    source_wave = np.asarray(db.get("wavelength_nm", WAVELENGTH_NM.tolist()))
    if source_wave.shape != (21,) or not np.array_equal(source_wave, WAVELENGTH_NM):
        raise ValueError("This adapter requires the frozen DB 450:10:650 nm wavelength grid")
    if wave[0] > WAVELENGTH_NM[0] or wave[-1] < WAVELENGTH_NM[-1]:
        raise ValueError("QE scan does not cover the DB spectral band; extrapolation is forbidden")
    rgb = np.column_stack([np.interp(WAVELENGTH_NM, wave, values[:, c]) for c in range(3)])
    normalization = _object(ccm.get("normalization_metadata"), "CCM normalization_metadata")
    if (
        ccm["normalization"] != "fixed_reference_electrons"
        or ccm["input_domain"] != "camera_linear_rgb"
    ):
        raise ValueError("DB CCM requires fixed_reference_electrons camera-linear input")
    reference_white = _number(
        normalization.get("reference_white_e"), "reference_white_e", positive=True
    )
    scan_gain = _number(normalization.get("dn_per_e"), "scan normalization DN/e", positive=True)
    if scan_gain != qe["dn_per_e"]:
        raise ValueError("CCM origin normalization DN/e differs from its QE calibration")
    wb = np.asarray(ccm["white_balance"], dtype=float)
    if wb.shape != (3,) or not np.isfinite(wb).all() or np.any(wb <= 0):
        raise ValueError("CCM white balance requires three positive finite channel multipliers")
    exposure_scale = _number(ccm["exposure_scale"], "CCM exposure_scale", positive=True)
    multiplier = np.diag(wb * exposure_scale / reference_white)
    electron_matrix = multiplier @ np.asarray(ccm["matrix"])
    baseline_electron_matrix = multiplier @ np.asarray(ccm["baseline_matrix"])
    if not np.isfinite(electron_matrix).all() or not np.isfinite(baseline_electron_matrix).all():
        raise ValueError("CCM electron-domain conversion is nonfinite")
    resolved = {
        "schema": SCHEMA,
        "source": source,
        "binding": request["binding"],
        "source_kind": "synthetic",
        "physical_validation": "not_established",
        "qe_profile": qe,
        "ccm_profile": ccm,
        "electronics": electronics,
        "pixel_pitch_um": db["pixel_pitch_um"],
        "cfa": "RGGB",
        "conditions": conditions,
        "applied_response": {
            "wavelength_nm": WAVELENGTH_NM.tolist(),
            "qe_rgb": rgb.tolist(),
            "qe_rggb": rgb[:, [0, 1, 1, 2]].tolist(),
            "sites": ["R", "Gr", "Gb", "B"],
            "reference_plane": "sensor_surface",
            "unit": "collected_electrons_per_incident_photon",
            "included_effects": qe["included_effects"],
            "angular_policy": "angle_independent",
            "green_policy": "shared_green",
            "application": "absolute_replacement",
        },
        "isp_adapter": {
            "reference_white_e": reference_white,
            "white_balance": wb.tolist(),
            "exposure_scale": exposure_scale,
            "baseline_matrix": ccm["baseline_matrix"],
            "matrix": ccm["matrix"],
            "electron_to_output_matrix": electron_matrix.tolist(),
            "baseline_electron_to_output_matrix": baseline_electron_matrix.tolist(),
            "matrix_convention": "row_vector",
            "target_space": ccm["target_space"],
            "delta_e_status": "not_applicable_camera_output_reproduction"
            if ccm["target_space"] == "camera_linear_rgb"
            else "requires_explicit_colorimetric_reference",
            "application": "demosaiced_electrons @ electron_to_output_matrix",
            "post_matrix_white_balance": [1.0, 1.0, 1.0],
            "post_matrix_ccm": np.eye(3).tolist(),
        },
        "compatibility": {
            "status": "qualified_synthetic_model_transfer",
            "physical_camera_identity_match": False,
            "source_validation_transferred": False,
            "db_validation_status": "not_evaluated",
            "db_readout_preserved": True,
            "scan_dn_per_e_copied": False,
            "qe_scan_exposure_range_s": qe["exposure_range_s"],
            "db_exposure_ms": db.get("exposure_ms"),
            "scan_spectral_band_nm": [float(wave[0]), float(wave[-1])],
            "db_spectral_band_nm": [450.0, 650.0],
        },
        "provenance": {
            "source_pins": pins,
            "color_calibration_id": color["id"],
            "color_record_sha256": color["content_hash"],
            "candidate_binding": color["candidate_binding"],
            "origin_camera_id": qe["conditions"]["camera_id"],
            "qe_profile_sha256": qe["profile_sha256"],
            "ccm_profile_sha256": ccm["profile_sha256"],
            "origin_scan_dn_per_e": qe["dn_per_e"],
            "source_projection_sha256": digest(db),
        },
        "limitations": [
            "An explicit new synthetic model; the source sensor is not physically calibrated.",
            "Three-channel normal-incidence QE replaces absolute response at all pupil angles.",
            "Gr and Gb share green QE; angular response and site differences are not identified.",
            "Lens/filter transport remains upstream; old QE must not multiply the replacement.",
            "DB readout converts DN to electrons; origin scan DN/e is not copied into the DB.",
            "WB, exposure scale and reference electron normalization are applied exactly once.",
            "Original RAW/CCM pass does not validate the new DB exposure or spectral band.",
        ],
    }
    resolved["profile_sha256"] = digest(resolved)
    return resolved
