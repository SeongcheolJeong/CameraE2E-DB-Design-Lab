"""Immutable synthetic DB transfer, identity guards and electron-domain CCM units."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_ccm, calibration_spectral
from camera_platform.calibration_db_color_contracts import freeze_model, model_spec
from camera_platform.registry import digest


def rehash(record):
    record["content_hash"] = digest({k: v for k, v in record.items() if k != "content_hash"})


def case(wavelengths=(380, 530, 780)):
    wave = np.asarray(wavelengths, dtype=float)
    qe = np.array([[0.1, 0.1, 0.6], [0.2, 0.7, 0.2], [0.7, 0.1, 0.1]])
    conditions = {
        "camera_id": "original-synthetic",
        "mode_id": "raw",
        "gain": 1,
        "temperature_c": 25,
    }
    provenance = {
        "calibration_id": "synthetic-radiometry",
        "instrument_id": "synthetic-instrument",
        "session_id": "scan",
        "acquisition_id": "scan",
    }
    scan = {
        "schema": "camera_qe_scan_v1",
        "wavelength_nm": wave.tolist(),
        "channels": ["R", "G", "B"],
        "mean_dn": (64 + 500 * qe).tolist(),
        "dark_dn": np.full((3, 3), 64).tolist(),
        "dn_per_e": 0.5,
        "exposure_s": [0.01] * 3,
        "bandwidth_nm": 5,
        "adc_white_dn": 4095,
        "reference_plane": "sensor_surface",
        "conditions": conditions,
        "source_kind": "synthetic",
        "provenance": provenance,
        "included_effects": ["cfa", "microlens", "photodiode"],
        "incident_photons_per_pixel": [1000] * 3,
        "radiometry": {
            "kind": "absolute_calibrated",
            "calibration_id": "synthetic-radiometry",
            "unit": "photons/pixel",
        },
        "clipped_fraction": np.zeros((3, 3)).tolist(),
        "dark_clipped_fraction": np.zeros((3, 3)).tolist(),
    }
    profile = calibration_spectral.derive_qe(scan)
    dose = np.random.default_rng(7).uniform(1, 5, (24, 3))
    electron = np.trapezoid(dose[:, :, None] * qe[None], wave, axis=1)
    raw = {
        "schema": "camera_spectral_raw_validation_v1",
        "wavelength_nm": wave.tolist(),
        "photon_spectral_dose": dose.tolist(),
        "photon_dose_unit": "photons/pixel/nm",
        "measured_raw_rgb_dn": (64 + 0.5 * electron).tolist(),
        "black_level_dn": 64,
        "dn_per_e": 0.5,
        "adc_white_dn": 4095,
        "exposure_s": 0.01,
        "conditions": conditions,
        "source_kind": "synthetic",
        "provenance": {
            **provenance,
            "session_id": "raw-validation",
            "acquisition_id": "raw-validation",
        },
        "reference_plane": "sensor_surface",
        "dose_includes": ["lens", "filters"],
        "radiometry": {
            "kind": "absolute_calibrated",
            "calibration_id": "synthetic-radiometry",
            "unit": "photons/pixel/nm",
        },
        "normalization": "none",
        "clipped_fraction": np.zeros((24, 3)).tolist(),
        "tolerance": {"channel_rmse_dn": [1e-8] * 3, "max_abs_dn": 1e-8},
    }
    matrix = np.array([[1.3, -0.1, 0.05], [0.02, 1.2, -0.2], [-0.15, 0.01, 1.1]])
    wb, scale, white = np.array([1.1, 1, 1.2]), 0.8, 2000

    def ccm_payload(seed):
        rgb = np.random.default_rng(seed).uniform(0.02, 0.5, (24, 3))
        return {
            "camera_rgb": rgb.tolist(),
            "target_rgb": ((rgb * wb * scale) @ matrix).tolist(),
            "target_space": "camera_linear_rgb",
            "white_balance": wb.tolist(),
            "exposure_scale": scale,
            "session_id": f"session-{seed}",
            "acquisition_id": f"acquisition-{seed}",
            "source_kind": "synthetic",
            "illuminant_id": "D65",
            "illuminant_spd_sha256": "a" * 64,
            "conditions": conditions,
            "whitepoint": "D65",
            "normalization": "fixed_reference_electrons",
            "input_domain": "camera_linear_rgb",
            "normalization_metadata": {
                "reference_white_e": white,
                "dn_per_e": 0.5,
                "black_level_dn": 64,
            },
            "clipped_patch_indices": [],
            "source_artifact_sha256": str(seed) * 64,
        }

    fitted = calibration_ccm.fit_ccm(ccm_payload(1))
    heldout = ccm_payload(2)
    heldout["thresholds"] = {"max_rmse": 1e-8}
    ccm = fitted["profile"]
    record = {
        "id": "color-calibration-" + "b" * 32,
        "created_at": "2026-10-17T00:00:00Z",
        "schema": "camera_spectral_color_record_v1",
        "source_kind": "synthetic",
        "physical_validation": "not_established",
        "status": "pass",
        "qe": {"profile": profile},
        "ccm": {"profile": ccm, "validation": calibration_ccm.validate_ccm(ccm, heldout)},
        "raw_validation": {"candidate": calibration_spectral.validate_raw(profile, raw)},
        "evidence": {"inputs": {"raw_validation": raw}},
        "candidate_binding": digest({"qe": profile, "ccm": ccm}),
        "raw_unchanged_by_ccm": True,
    }
    rehash(record)
    source = {
        "run_id": "run-" + "c" * 32,
        "candidate_id": "candidate-03",
        "condition_id": "condition-006",
    }
    context = {
        **source,
        **{
            k: "d" * 64
            for k in (
                "run_request_sha256",
                "run_result_sha256",
                "manifest_sha256",
                "candidate_sha256",
                "condition_sha256",
            )
        },
    }
    spec = {
        "name": "Derived synthetic DB model",
        "source": source,
        "color_calibration_id": record["id"],
        "binding": {
            "mode": "synthetic_model_transfer",
            "angular_assumption": "angle_independent",
            "green_sites": "shared_green",
            "target_camera_id": "derived-synthetic",
        },
    }
    projection = {
        "cfa": "RGGB",
        "pixel_pitch_um": 2.9,
        "conditions": {"gain": 1, "temperature_c": 25},
        "electronics": {
            "dn_per_e": 0.4031,
            "bit_depth": 12,
            "black_level_dn": 64,
            "full_well_e": 10000,
            "read_noise_e": 10,
            "dark_current_e_per_s": 10,
        },
        "exposure_ms": 1,
        "wavelength_nm": np.arange(450, 651, 10).tolist(),
    }
    return spec, context, record, projection


def test_resolved_profile_is_explicit_detached_model_transfer_with_electron_units():
    args = case()
    original = deepcopy(args)
    result = freeze_model(*args)
    assert args == original
    assert result["profile_sha256"] == digest(
        {k: v for k, v in result.items() if k != "profile_sha256"}
    )
    assert result["physical_validation"] == "not_established"
    assert result["compatibility"]["source_validation_transferred"] is False
    assert result["compatibility"]["db_validation_status"] == "not_evaluated"
    assert result["electronics"]["dn_per_e"] == 0.4031
    assert result["provenance"]["origin_scan_dn_per_e"] == 0.5
    response = result["applied_response"]
    assert response["application"] == "absolute_replacement"
    np.testing.assert_allclose(
        np.array(response["qe_rggb"])[:, 1], np.array(response["qe_rggb"])[:, 2]
    )
    isp = result["isp_adapter"]
    electrons = np.array([300, 700, 200])
    expected = (electrons / 2000 * np.array([1.1, 1, 1.2]) * 0.8) @ np.array(isp["matrix"])
    np.testing.assert_allclose(electrons @ np.array(isp["electron_to_output_matrix"]), expected)
    assert isp["delta_e_status"] == "not_applicable_camera_output_reproduction"
    assert isp["post_matrix_white_balance"] == [1, 1, 1]
    assert result["qe_profile"]["conditions"]["camera_id"] == "original-synthetic"


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "measured_same_camera"),
        ("angular_assumption", "inherit_source"),
        ("green_sites", "invent_independent_greens"),
        ("target_camera_id", "../escape"),
    ],
)
def test_request_rejects_implicit_or_measured_transfer(field, value):
    spec, *_ = case()
    spec["binding"][field] = value
    with pytest.raises(ValueError):
        model_spec(spec)


def test_request_unknown_fields_cannot_bypass_identity_checks():
    spec, *_ = case()
    spec["binding"]["camera_identity_matches"] = True
    with pytest.raises(ValueError, match="unsupported fields"):
        model_spec(spec)


@pytest.mark.parametrize(
    "mutation", ["source", "hash", "same_camera", "gain", "temperature", "cfa", "adc", "band"]
)
def test_exact_source_and_db_condition_gates(mutation):
    spec, context, record, projection = case()
    if mutation == "source":
        context["condition_id"] = "condition-007"
    if mutation == "hash":
        context["manifest_sha256"] = "invalid"
    if mutation == "same_camera":
        spec["binding"]["target_camera_id"] = "original-synthetic"
    if mutation == "gain":
        projection["conditions"]["gain"] = 2
    if mutation == "temperature":
        projection["conditions"]["temperature_c"] = 30
    if mutation == "cfa":
        projection["cfa"] = "BGGR"
    if mutation == "adc":
        projection["electronics"]["bit_depth"] = 10
    if mutation == "band":
        projection["wavelength_nm"][0] = 440
    with pytest.raises(ValueError):
        freeze_model(spec, context, record, projection)


def test_qe_coverage_refuses_extrapolation_even_after_successful_original_validation():
    with pytest.raises(ValueError, match="extrapolation"):
        freeze_model(*case((460, 530, 640)))


@pytest.mark.parametrize(
    "mutation",
    [
        "record_hash",
        "measured",
        "raw_fail",
        "ccm_fail",
        "raw_forge",
        "qe_hash",
        "ccm_hash",
        "binding",
    ],
)
def test_validation_hashes_and_actual_passes_are_required(mutation):
    spec, context, record, projection = case()
    if mutation == "record_hash":
        record["content_hash"] = "0" * 64
    if mutation == "measured":
        record["source_kind"] = "measured"
    if mutation == "raw_fail":
        record["evidence"]["inputs"]["raw_validation"]["measured_raw_rgb_dn"][0][0] += 10
    if mutation == "ccm_fail":
        record["ccm"]["validation"]["validation_payload"]["target_rgb"][0][0] += 1
    if mutation == "raw_forge":
        record["raw_validation"]["candidate"]["metrics"]["overall_rmse_dn"] = 8
    if mutation == "qe_hash":
        record["qe"]["profile"]["qe"][0][0] += 0.01
    if mutation == "ccm_hash":
        record["ccm"]["profile"]["matrix"][0][0] += 0.01
    if mutation == "binding":
        record["candidate_binding"] = "f" * 64
    if mutation != "record_hash":
        rehash(record)
    with pytest.raises(ValueError):
        freeze_model(spec, context, record, projection)


def test_new_source_conditions_do_not_inherit_origin_exposure_validation():
    result = freeze_model(*case())
    assert result["compatibility"]["qe_scan_exposure_range_s"] == [0.01, 0.01]
    assert result["compatibility"]["db_exposure_ms"] == 1
    assert result["compatibility"]["scan_spectral_band_nm"] == [380, 780]
    assert result["compatibility"]["db_spectral_band_nm"] == [450, 650]
    assert result["compatibility"]["physical_camera_identity_match"] is False
