"""Unit/provenance guards for QE scans and a separate frozen-QE RAW comparison."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform.calibration_spectral import derive_qe, profile_sha256, validate_raw


def scan_fixture():
    # Unequal wavelength/exposure/dose spacing detects hidden source normalization.
    qe = np.array([[0.08, 0.12, 0.70], [0.12, 0.76, 0.18], [0.66, 0.15, 0.04]])
    photons = np.array([1000.0, 2400.0, 1500.0])
    dark = np.array([[64.0, 65.0, 66.0], [66.0, 67.0, 68.0], [70.0, 71.0, 72.0]])
    return {
        "schema": "camera_qe_scan_v1",
        "wavelength_nm": [420.0, 530.0, 690.0],
        "channels": ["R", "G", "B"],
        "mean_dn": (dark + 0.5 * photons[:, None] * qe).tolist(),
        "dark_dn": dark.tolist(),
        "dn_per_e": 0.5,
        "exposure_s": [0.01, 0.02, 0.03],
        "bandwidth_nm": [10.0, 12.0, 8.0],
        "adc_white_dn": 4095,
        "reference_plane": "sensor_surface",
        "conditions": {
            "camera_id": "synthetic-camera-A",
            "mode_id": "linear-RGGB",
            "gain": 1.0,
            "temperature_c": 25.0,
        },
        "source_kind": "synthetic",
        "provenance": {
            "calibration_id": "photon-calibration-A",
            "session_id": "scan-training-A",
            "acquisition_id": "wavelength-scan-A",
            "instrument_id": "synthetic-monochromator",
            "source_sha256": "a" * 64,
        },
        "included_effects": ["cfa", "microlens", "photodiode"],
        "incident_photons_per_pixel": photons.tolist(),
        "radiometry": {
            "kind": "absolute_calibrated",
            "calibration_id": "photon-calibration-A",
            "unit": "photons/pixel",
        },
        "clipped_fraction": np.zeros((3, 3)).tolist(),
        "dark_clipped_fraction": np.zeros((3, 3)).tolist(),
    }, qe


def raw_fixture(profile):
    photons = np.array([[4.0, 5.0, 1.0], [1.0, 2.0, 6.0]])
    # Explicit independent trapezoid sum, not the production observer.
    product = photons[:, :, None] * np.asarray(profile["qe"])[None, :, :]
    electron = (product[:, 0] + product[:, 1]) * 110 / 2
    electron += (product[:, 1] + product[:, 2]) * 160 / 2
    black = np.array([64.0, 65.0, 66.0])
    return {
        "schema": "camera_spectral_raw_validation_v1",
        "wavelength_nm": profile["wavelength_nm"].copy(),
        "photon_spectral_dose": photons.tolist(),
        "photon_dose_unit": "photons/pixel/nm",
        "measured_raw_rgb_dn": (black + 0.5 * electron).tolist(),
        "black_level_dn": black.tolist(),
        "dn_per_e": 0.5,
        "adc_white_dn": 4095,
        "exposure_s": 0.02,
        "conditions": deepcopy(profile["conditions"]),
        "source_kind": "synthetic",
        "provenance": {
            "calibration_id": "chart-radiometry-A",
            "session_id": "raw-heldout-B",
            "acquisition_id": "spectral-chart-B",
            "instrument_id": "synthetic-radiometer",
            "source_sha256": "b" * 64,
        },
        "reference_plane": "sensor_surface",
        "dose_includes": ["lens", "filters"],
        "radiometry": {
            "kind": "absolute_calibrated",
            "calibration_id": "chart-radiometry-A",
            "unit": "photons/pixel/nm",
        },
        "normalization": "none",
        "clipped_fraction": np.zeros((2, 3)).tolist(),
        "tolerance": {"channel_rmse_dn": [1e-9] * 3, "max_abs_dn": 1e-9, "ratio_abs": 1e-9},
        "patch_ids": ["blue", "red"],
    }, electron


def test_absolute_qe_units_dark_subtraction_and_immutable_integrity():
    scan, expected = scan_fixture()
    original = deepcopy(scan)
    profile = derive_qe(scan)
    np.testing.assert_allclose(profile["qe"], expected, atol=1e-15)
    assert scan == original
    assert profile["response_kind"] == "absolute_qe"
    assert profile["normalization"] == "none"
    assert profile["profile_sha256"] == profile_sha256(profile)
    assert profile["profile_sha256"] == derive_qe(scan)["profile_sha256"]
    assert profile["source_kind"] == "synthetic"
    assert profile["physical_validation"] == "not_established"
    assert profile["provenance"]["source_sha256"] == "a" * 64


def relative_fixture():
    scan, qe = scan_fixture()
    photons = np.array(scan.pop("incident_photons_per_pixel"))
    scan["photon_flux_per_second"] = (photons / scan["exposure_s"]).tolist()
    scan["radiometry"] = {
        "kind": "relative_source_corrected",
        "calibration_id": "relative-photon-calibration-A",
        "unit": "relative_photons/s",
        "source_spectrum_corrected": True,
    }
    return scan, qe


def test_relative_scan_corrects_source_and_exposure_but_cannot_predict_dn():
    scan, qe = relative_fixture()
    profile = derive_qe(scan)
    assert "qe" not in profile
    assert profile["response_kind"] == "relative_spectral_response"
    np.testing.assert_allclose(profile["relative_spectral_response"], qe / qe.max(axis=0))
    absolute = derive_qe(scan_fixture()[0])
    raw, _ = raw_fixture(absolute)
    with pytest.raises(ValueError, match="requires absolute_qe"):
        validate_raw(profile, raw)


@pytest.mark.parametrize("corrected", [None, False, 1, "true"])
def test_relative_raw_spectrum_without_source_correction_is_refused(corrected):
    scan, _ = relative_fixture()
    if corrected is None:
        del scan["radiometry"]["source_spectrum_corrected"]
    else:
        scan["radiometry"]["source_spectrum_corrected"] = corrected
    with pytest.raises(ValueError, match="source spectrum correction"):
        derive_qe(scan)


def test_frozen_qe_prediction_matches_manual_nonuniform_quadrature_without_refitting():
    profile = derive_qe(scan_fixture()[0])
    raw, electron = raw_fixture(profile)
    original_profile, original_raw = deepcopy(profile), deepcopy(raw)
    result = validate_raw(profile, raw)
    np.testing.assert_allclose(result["predicted_electrons"], electron)
    np.testing.assert_allclose(result["predicted_raw_rgb_dn"], raw["measured_raw_rgb_dn"])
    assert result["status"] == "pass"
    assert result["numerical_tolerance_pass"] is True
    assert result["physical_validation"] == "not_established"
    assert result["qe_unchanged"] is True
    assert result["qe_profile_sha256_before"] == result["qe_profile_sha256_after"]
    assert result["qe_profile_sha256_before"] == profile["profile_sha256"]
    ratio = result["patches"][0]["ratios"]
    assert ratio["predicted_rg"] == pytest.approx(electron[0, 0] / electron[0, 1])
    assert ratio["error_bg"] == pytest.approx(0)
    assert profile == original_profile and raw == original_raw
    # Integrated dose does not get a second exposure multiplication.
    raw["exposure_s"] = 0.03
    np.testing.assert_allclose(validate_raw(profile, raw)["predicted_electrons"], electron)


def test_mismatch_is_a_failed_result_with_signed_dn_errors_not_an_auto_fit():
    profile = derive_qe(scan_fixture()[0])
    raw, _ = raw_fixture(profile)
    raw["measured_raw_rgb_dn"] = (np.array(raw["measured_raw_rgb_dn"]) + [5, -3, 2]).tolist()
    result = validate_raw(profile, raw)
    assert result["status"] == "fail"
    np.testing.assert_allclose(result["metrics"]["channel_rmse_dn"], [5, 3, 2])
    np.testing.assert_allclose(result["metrics"]["channel_bias_dn"], [-5, 3, -2])
    assert result["metrics"]["max_abs_dn"] == pytest.approx(5)
    assert result["qe_unchanged"] is True


def test_authored_baseline_requires_marked_provenance_and_rehash():
    profile = derive_qe(scan_fixture()[0])
    raw, _ = raw_fixture(profile)
    baseline = deepcopy(profile)
    baseline["qe"] = (np.array(baseline["qe"]) * 0.9).tolist()
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_raw(baseline, raw)
    baseline["method"] = "authored_baseline_v1"
    baseline["provenance"]["parent_profile_sha256"] = profile["profile_sha256"]
    baseline["provenance"]["baseline_reason"] = "Declared baseline sensitivity perturbation"
    baseline["profile_sha256"] = profile_sha256(baseline)
    result = validate_raw(baseline, raw)
    assert result["status"] == "fail"
    assert result["qe_unchanged"] is True
    baseline["source_kind"] = "measured"
    baseline["profile_sha256"] = profile_sha256(baseline)
    with pytest.raises(ValueError, match="authored baseline is synthetic"):
        validate_raw(baseline, raw)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("wavelength_nm", [420, 420, 690]),
        ("wavelength_nm", [420, 690, 530]),
        ("wavelength_nm", [420, float("nan"), 690]),
        ("bandwidth_nm", 300),
        ("bandwidth_nm", [0, 12, 8]),
        ("mean_dn", [[64, 65, 66]]),
        ("mean_dn", [[True] * 3] * 3),
        ("mean_dn", [[1, 1, 1]] * 3),
        ("mean_dn", [[4095, 4095, 4095]] * 3),
        ("dn_per_e", 0),
        ("incident_photons_per_pixel", [1, 1, 1]),
        ("incident_photons_per_pixel", [1000, 0, 1500]),
        ("clipped_fraction", [[0.01] * 3] * 3),
        ("dark_clipped_fraction", [[0.01] * 3] * 3),
        ("reference_plane", "lens_entrance"),
        ("source_kind", []),
        ("included_effects", ["photodiode"]),
        ("included_effects", ["cfa", "photodiode", "lens"]),
    ],
)
def test_invalid_scan_never_produces_qe(key, value):
    scan, _ = scan_fixture()
    scan[key] = value
    with pytest.raises(ValueError):
        derive_qe(scan)


def test_missing_calibration_and_unknown_fit_knobs_refused():
    scan, _ = scan_fixture()
    del scan["radiometry"]["calibration_id"]
    with pytest.raises(ValueError, match="missing"):
        derive_qe(scan)
    scan, _ = scan_fixture()
    scan["white_balance"] = [1, 1, 1]
    with pytest.raises(ValueError, match="unknown fields"):
        derive_qe(scan)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("dn_per_e", 0.51),
        ("adc_white_dn", 1023),
        ("exposure_s", 0.001),
        ("exposure_s", 0.031),
        ("reference_plane", "lens_entrance"),
        ("photon_dose_unit", "photons/pixel"),
        ("normalization", "fit_gain"),
        ("dose_includes", ["lens"]),
        ("clipped_fraction", [[0.01] * 3] * 2),
        ("measured_raw_rgb_dn", [[4095] * 3] * 2),
        ("photon_spectral_dose", [[1000] * 3] * 2),
        ("photon_spectral_dose", [[-1] * 3] * 2),
        ("photon_spectral_dose", [[float("inf")] * 3] * 2),
        ("patch_ids", [["unhashable"], "second"]),
        ("patch_ids", ["same", "same"]),
    ],
)
def test_invalid_raw_linear_domain_and_units_refused(key, value):
    profile = derive_qe(scan_fixture()[0])
    raw, _ = raw_fixture(profile)
    raw[key] = value
    with pytest.raises(ValueError):
        validate_raw(profile, raw)


@pytest.mark.parametrize("key,value", [("gain", 2), ("temperature_c", 24), ("camera_id", "B")])
def test_raw_conditions_must_equal_frozen_scan_conditions(key, value):
    profile = derive_qe(scan_fixture()[0])
    raw, _ = raw_fixture(profile)
    raw["conditions"][key] = value
    with pytest.raises(ValueError, match="conditions differ"):
        validate_raw(profile, raw)


@pytest.mark.parametrize("wave", [[430, 530, 690], [410, 530, 690], [420, 550, 690]])
def test_narrow_coverage_extrapolation_and_missing_qe_knots_refused(wave):
    profile = derive_qe(scan_fixture()[0])
    raw, _ = raw_fixture(profile)
    raw["wavelength_nm"] = wave
    with pytest.raises(ValueError, match="complete QE center band|retain every QE"):
        validate_raw(profile, raw)


def test_rehashed_out_of_range_qe_still_refused():
    profile = derive_qe(scan_fixture()[0])
    raw, _ = raw_fixture(profile)
    profile["qe"][0][0] = 1.01
    profile["profile_sha256"] = profile_sha256(profile)
    with pytest.raises(ValueError, match="must be <= 1"):
        validate_raw(profile, raw)


def test_zero_signal_ratio_is_unavailable_and_optional_gate_fails_honestly():
    profile = derive_qe(scan_fixture()[0])
    raw, _ = raw_fixture(profile)
    raw["photon_spectral_dose"][0] = [0, 0, 0]
    raw["measured_raw_rgb_dn"][0] = raw["black_level_dn"].copy()
    result = validate_raw(profile, raw)
    assert result["metrics"]["ratio_unavailable_patches"] == 1
    assert result["patches"][0]["ratios"]["predicted_rg"] is None
    assert result["status"] == "fail"
    del raw["tolerance"]["ratio_abs"]
    result = validate_raw(profile, raw)
    assert result["status"] == "pass"
    assert any(check["status"] == "not_evaluated" for check in result["checks"])


@pytest.mark.parametrize("key", ["fitted_gain", "white_balance", "ccm"])
def test_validation_never_accepts_color_fit_parameters(key):
    profile = derive_qe(scan_fixture()[0])
    raw, _ = raw_fixture(profile)
    raw[key] = 1
    with pytest.raises(ValueError, match="unknown fields"):
        validate_raw(profile, raw)
