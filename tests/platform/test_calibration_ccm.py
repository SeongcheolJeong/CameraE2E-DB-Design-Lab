"""Directional CCM recovery, frozen holdout evaluation and colorimetric boundaries."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_ccm as ccm

TRUTH = np.array([[0.80, 0.12, 0.03], [0.05, 0.75, 0.13], [0.10, 0.04, 0.90]])


def payload(seed=1, *, target_space="linear_srgb"):
    camera = np.random.default_rng(seed).uniform(0.03, 0.6, (24, 3))
    wb, scale = [1.2, 1.0, 0.8], 0.7
    return {
        "camera_rgb": camera.tolist(),
        "target_rgb": ((camera * wb * scale) @ TRUTH).tolist(),
        "target_space": target_space,
        "white_balance": wb,
        "exposure_scale": scale,
        "session_id": "session-" + str(seed),
        "acquisition_id": "capture-" + str(seed),
        "source_kind": "synthetic",
        "illuminant_id": "measured-D65-reference",
        "illuminant_spd_sha256": "a" * 64,
        "source_artifact_sha256": ("b" if seed == 1 else "c") * 64,
        "conditions": {"gain": 1, "temperature_c": 25, "mode_id": "linear"},
        "whitepoint": "D65",
        "normalization": "unit_reflectance_linear",
        "normalization_metadata": {"black_subtracted": True, "reference_white_e": 4000},
        "input_domain": "camera_linear_rgb",
        "clipped_patch_indices": [],
    }


def holdout(**kwargs):
    p = payload(2, **kwargs)
    p["thresholds"] = {"max_rmse": 1e-8}
    return p


def test_nonsymmetric_ground_truth_uses_camera_to_target_row_vector_convention():
    p = payload()
    original = deepcopy(p)
    fitted = ccm.fit_ccm(p)
    profile = fitted["profile"]
    np.testing.assert_allclose(profile["matrix"], TRUTH, atol=1e-12)
    assert not np.allclose(profile["matrix"], TRUTH.T)
    assert profile["matrix_convention"] == "row_vector"
    assert profile["application"] == ccm.APPLICATION
    assert fitted["comparison"]["metrics"]["candidate"]["rmse"] < 1e-14
    assert fitted["comparison"]["metrics"]["baseline"]["rmse"] > 0.01
    assert p == original
    assert "raw_response" not in fitted and "qe" not in profile
    assert [r["parameter"] for r in fitted["changed_parameters"]] == ["isp.color_matrix"]
    assert fitted["physical_validation"] == "not_established"
    assert profile["training"]["source_artifact_sha256"] == p["source_artifact_sha256"]


def test_holdout_applies_frozen_matrix_without_optimization(monkeypatch):
    profile = ccm.fit_ccm(payload())["profile"]
    preserved = deepcopy(profile)

    def forbidden(*args, **kwargs):
        raise AssertionError("Independent validation must not optimize")

    monkeypatch.setattr(ccm, "lsq_linear", forbidden)
    validated = ccm.validate_ccm(profile, holdout())
    assert validated["status"] == "pass"
    assert validated["refit_performed"] is False
    assert validated["profile_sha256"] == profile["profile_sha256"]
    assert validated["physical_validation"] == "not_established"
    assert profile == preserved


def test_bad_holdout_fails_without_revising_profile():
    profile = ccm.fit_ccm(payload())["profile"]
    h = holdout()
    h["target_rgb"] = (np.asarray(h["target_rgb"]) + 0.1).tolist()
    result = ccm.validate_ccm(profile, h)
    assert result["status"] == "fail"
    assert result["checks"][0]["status"] == "fail"
    assert result["comparison"]["metrics"]["candidate"]["rmse"] == pytest.approx(0.1)
    assert result["profile_sha256"] == profile["profile_sha256"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_kind", "measured"),
        ("illuminant_spd_sha256", "d" * 64),
        ("conditions", {"gain": 2}),
        ("normalization_metadata", {"black_subtracted": True, "reference_white_e": 2000}),
        ("white_balance", [1, 1, 1]),
        ("exposure_scale", 1),
    ],
)
def test_fixed_metadata_mismatch_rejected(field, value):
    profile = ccm.fit_ccm(payload())["profile"]
    h = holdout()
    h[field] = value
    with pytest.raises(ValueError, match="metadata differs"):
        ccm.validate_ccm(profile, h)


@pytest.mark.parametrize("field", ["session_id", "acquisition_id", "source_artifact_sha256"])
def test_training_identity_or_original_reuse_rejected(field):
    p = payload()
    h = holdout()
    h[field] = p[field]
    with pytest.raises(ValueError, match="new session, acquisition and source"):
        ccm.validate_ccm(ccm.fit_ccm(p)["profile"], h)


def test_renamed_or_reordered_training_observations_rejected():
    p, h = payload(), holdout()
    h["camera_rgb"] = p["camera_rgb"][::-1]
    h["target_rgb"] = p["target_rgb"][::-1]
    with pytest.raises(ValueError, match="reuses training camera observations"):
        ccm.validate_ccm(ccm.fit_ccm(p)["profile"], h)


def test_same_target_patches_allowed_when_camera_measurement_is_new():
    p, h = payload(), holdout()
    h["target_rgb"] = deepcopy(p["target_rgb"])
    h["camera_rgb"] = (np.asarray(p["camera_rgb"]) + 1e-5).tolist()
    h["thresholds"] = {"max_rmse": 1e-4, "max_delta_e00": 0.1}
    result = ccm.validate_ccm(ccm.fit_ccm(p)["profile"], h)
    assert result["status"] == "pass"


def test_tampered_frozen_profile_rejected():
    profile = ccm.fit_ccm(payload())["profile"]
    profile["matrix"][0][1] += 0.001
    with pytest.raises(ValueError, match="profile hash mismatch"):
        ccm.validate_ccm(profile, holdout())


@pytest.mark.parametrize(
    "change",
    [
        {"input_domain": "srgb_gamma_encoded"},
        {"clipped_patch_indices": [3]},
        {"whitepoint": "D50"},
        {"illuminant_spd_sha256": "unknown"},
        {"normalization_metadata": {}},
        {"qe": {"values": [0.5]}},
    ],
)
def test_nonlinear_clipped_or_unproven_inputs_are_refused(change):
    p = payload()
    p.update(change)
    with pytest.raises(ValueError):
        ccm.fit_ccm(p)


def test_nonfinite_and_rank_deficient_inputs_are_refused():
    p = payload()
    p["camera_rgb"][0][0] = float("nan")
    with pytest.raises(ValueError):
        ccm.fit_ccm(p)
    p = payload()
    p["camera_rgb"] = [[0.2, 0.2, 0.2]] * 24
    with pytest.raises(ValueError, match="rank-three"):
        ccm.fit_ccm(p)


def test_bounded_ridge_fit_records_active_bounds_and_anchor():
    p = payload()
    p["coefficient_bound"] = 0.3
    bounded = ccm.fit_ccm(p)
    assert bounded["profile"]["fit"]["bound_active"] is True
    assert np.max(np.abs(bounded["profile"]["matrix"])) <= 0.3
    p = payload()
    p["ridge"] = 1e5
    fitted = ccm.fit_ccm(p)
    assert fitted["profile"]["fit"]["ridge_anchor"] == "baseline_matrix"
    assert np.linalg.norm(np.asarray(fitted["profile"]["matrix"]) - np.eye(3)) < 1e-4


def test_ciede2000_uses_actual_d65_xyz_and_linear_srgb_conversion():
    black = np.array([[0.0, 0.0, 0.0]])
    white = np.array([[0.95047, 1.0, 1.08883]])
    measured = ccm._metrics(black, white, "xyz")
    assert measured["delta_e00_mean"] == pytest.approx(100, abs=1e-8)
    fitted = ccm.fit_ccm(payload())
    assert fitted["comparison"]["metrics"]["candidate"]["delta_e00_mean"] < 1e-10
    assert fitted["comparison"]["metrics"]["baseline"]["delta_e00_mean"] > 0


def test_camera_output_reproduction_never_claims_delta_e():
    fitted = ccm.fit_ccm(payload(target_space="camera_linear_rgb"))
    metrics = fitted["comparison"]["metrics"]["candidate"]
    assert metrics["delta_e00_mean"] is None
    assert metrics["delta_e00_status"] == "not_applicable_camera_output_reproduction"
    h = holdout(target_space="camera_linear_rgb")
    h["thresholds"]["max_delta_e00"] = 3
    with pytest.raises(ValueError, match="cannot use a CIEDE2000"):
        ccm.validate_ccm(fitted["profile"], h)


def test_signed_post_ccm_targets_fit_and_validate_without_clipping():
    # The live Macbeth demo's authored CCM has negative cross-channel terms.
    # A red patch therefore has a valid negative pre-clipping blue output.
    matrix = np.array([[1.30, -0.12, -0.08], [-0.10, 1.24, -0.08], [0.03, -0.10, 1.20]])
    p = payload(target_space="camera_linear_rgb")
    p["normalization"] = "fixed_reference_electrons"
    p["camera_rgb"][0] = [0.6, 0.4, 0.01]
    p["target_rgb"] = (
        (np.asarray(p["camera_rgb"]) * p["white_balance"] * p["exposure_scale"]) @ matrix
    ).tolist()
    assert p["target_rgb"][0][2] < 0
    original = deepcopy(p)
    fitted = ccm.fit_ccm(p)
    np.testing.assert_allclose(fitted["profile"]["matrix"], matrix, atol=1e-12)
    assert fitted["comparison"]["candidate"][0][2] < 0
    assert fitted["comparison"]["matrix_outputs_clipped"] is False
    assert fitted["comparison"]["target_interpretation"] == (
        "signed_post_ccm_pre_clipping_linear_isp_output"
    )
    assert fitted["comparison"]["metrics"]["candidate"]["delta_e00_mean"] is None
    h = holdout(target_space="camera_linear_rgb")
    h["normalization"] = p["normalization"]
    h["camera_rgb"][0] = [0.61, 0.39, 0.012]
    h["target_rgb"] = (
        (np.asarray(h["camera_rgb"]) * h["white_balance"] * h["exposure_scale"]) @ matrix
    ).tolist()
    result = ccm.validate_ccm(fitted["profile"], h)
    assert result["status"] == "pass"
    assert result["comparison"]["candidate"][0][2] < 0
    assert p == original


def test_signed_camera_targets_preserve_xyz_input_and_absolute_bounds():
    p = payload(target_space="xyz")
    p["target_rgb"][0][0] = -0.01
    with pytest.raises(ValueError, match="Target values"):
        ccm.fit_ccm(p)
    p["target_space"] = "camera_linear_rgb"
    p["camera_rgb"][0][0] = -0.01
    with pytest.raises(ValueError, match="Camera RGB requires nonnegative"):
        ccm.fit_ccm(p)
    p["camera_rgb"][0][0] = 0.1
    p["target_rgb"][0][0] = -16.01
    with pytest.raises(ValueError, match="Target values"):
        ccm.fit_ccm(p)


def test_fixed_electron_reference_normalization_only_for_camera_output():
    p = payload(target_space="camera_linear_rgb")
    p["normalization"] = "fixed_reference_electrons"
    p["normalization_metadata"] = {
        "black_level_dn": 64,
        "dn_per_e": 0.5,
        "reference_white_e": 2000,
        "formula": "(native_mean_dn-black_level_dn)/dn_per_e/reference_white_e",
    }
    fitted = ccm.fit_ccm(p)
    assert fitted["comparison"]["metrics"]["candidate"]["delta_e00_mean"] is None
    p["target_space"] = "xyz"
    with pytest.raises(ValueError, match="only for camera-output reproduction"):
        ccm.fit_ccm(p)


def test_extended_outputs_are_not_clipped_and_negative_xyz_has_no_delta_e():
    predicted = np.array([[-0.1, 0.4, 0.3], [1.2, 0.4, 0.3]])
    target = np.array([[0.1, 0.4, 0.3], [1.1, 0.4, 0.3]])
    metrics = ccm._metrics(predicted, target, "xyz")
    assert metrics["rmse"] == pytest.approx(np.sqrt(0.05 / 6))
    assert metrics["delta_e00_mean"] is None
    assert metrics["delta_e00_status"] == "unavailable_negative_xyz_no_clipping_applied"
    assert metrics["negative_output_components"] == 1
    assert metrics["output_components_above_one"] == 1


def test_delta_e_threshold_is_inconclusive_for_negative_predicted_xyz():
    p = payload(target_space="xyz")
    matrix = np.array([[-0.5, 0.1, 0.2], [1.0, 0.7, 0.3], [0.2, 0.2, 0.7]])
    camera = np.asarray(p["camera_rgb"])
    camera[:, 0] *= 0.1
    p["camera_rgb"] = camera.tolist()
    p["target_rgb"] = ((camera * p["white_balance"] * p["exposure_scale"]) @ matrix).tolist()
    profile = ccm.fit_ccm(p)["profile"]
    h = holdout(target_space="xyz")
    h["camera_rgb"] = [[0.8, 0.01, 0.01]]
    h["target_rgb"] = [[0.02, 0.02, 0.02]]
    h["thresholds"] = {"max_rmse": 10, "max_delta_e00": 3}
    result = ccm.validate_ccm(profile, h)
    assert result["status"] == "inconclusive"
    assert result["checks"][0]["status"] == "pass"
    assert result["checks"][1]["status"] == "inconclusive"
