"""Independent sampled-CDS arithmetic, native covariance and registered RAW evidence."""

import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import load_array
from test_hdr_workflow import workflow as registered_hdr_workflow  # noqa: F401

from camera_platform import capture_programs, electrical, sequences
from camera_platform.evidence import build_bundle
from camera_platform.hdr_runtime import validate_capture_program


def _model():
    model = electrical.default_electrical_model({
        "full_well_e": 1_000_000.0,
        "read_noise_e": 0.0,
        "dark_current_e_per_s": 0.0,
        "gain": 1.0,
        "bit_depth": 24,
        "black_level_dn": 100_000.0,
    })
    model["conversion_gain_v_per_e"] = 1_000_000.0 / (2**24 - 1)
    model["adc"]["full_scale_v"] = 1_000_000.0
    return model


def _profile():
    value = electrical.default_cds_noise_profile()
    for axis in ("pixel", "row", "column"):
        value[axis] = {"reset_sigma_e": 0.0, "signal_sigma_e": 0.0,
                       "sample_correlation": 0.0}
    return value


def _capture(profile, shape, *, full=None, origin=(0, 0), seed=712, noise=True):
    return electrical.capture_electrons(
        np.zeros(shape, dtype=np.float64), 0.01, _model(),
        analog_gain=1.0, temperature_c=25.0,
        geometry={"full_sensor_shape": list(full or shape), "crop_origin": list(origin)},
        noise_enabled=noise, seed=seed, cds_profile=profile,
    )


def test_cds_shared_reset_node_cancels_before_adc_and_never_changes_raw():
    profile = _profile()
    first = _capture(profile, (16, 20))
    assert first["arrays"]["cds_reset_sample_noise_e"].std() > 0
    np.testing.assert_array_equal(
        first["arrays"]["cds_reset_sample_noise_e"],
        first["arrays"]["cds_signal_sample_noise_e"],
    )
    assert not first["arrays"]["cds_residual_noise_e"].any()
    np.testing.assert_array_equal(first["arrays"]["raw_dn"], 100_000)
    changed = deepcopy(profile)
    changed["reset_common_sigma_e"] = 900_000.0
    second = _capture(changed, (16, 20))
    np.testing.assert_array_equal(first["arrays"]["raw_dn"], second["arrays"]["raw_dn"])
    np.testing.assert_array_equal(
        first["arrays"]["cds_residual_noise_e"],
        second["arrays"]["cds_residual_noise_e"],
    )
    assert second["calibration"]["read_noise_variance_e2"] == 0
    assert second["summary"]["physical_validation"] == "not_established"


def test_row_column_cds_covariance_and_native_crop_are_independent_of_capture_region():
    profile = _profile()
    profile["reset_common_sigma_e"] = 0.0
    profile["row"] = {"reset_sigma_e": 2.0, "signal_sigma_e": 3.0,
                      "sample_correlation": 0.5}
    profile["column"] = {"reset_sigma_e": 1.0, "signal_sigma_e": 2.0,
                         "sample_correlation": -0.25}
    full = _capture(profile, (40, 48), seed=100)
    crop = _capture(profile, (12, 18), full=(40, 48), origin=(7, 11), seed=100)
    for name in ("cds_reset_sample_noise_e", "cds_signal_sample_noise_e",
                 "cds_residual_noise_e", "raw_dn"):
        np.testing.assert_array_equal(full["arrays"][name][7:19, 11:29], crop["arrays"][name])
    residual = full["arrays"]["cds_residual_noise_e"]
    np.testing.assert_allclose(
        residual[1, 2] + residual[0, 0], residual[1, 0] + residual[0, 2], atol=1e-14
    )
    expected = 2**2 + 3**2 - 2 * 0.5 * 2 * 3 + 1**2 + 2**2 - 2 * (-0.25) * 1 * 2
    assert full["calibration"]["read_noise_variance_e2"] == expected
    disabled = _capture(profile, (12, 18), full=(40, 48), origin=(7, 11), noise=False)
    assert not disabled["arrays"]["cds_residual_noise_e"].any()
    assert disabled["calibration"]["read_noise_variance_e2"] == 0


def test_empirical_row_column_and_pixel_noise_match_separate_variances():
    profile = _profile()
    profile["reset_common_sigma_e"] = 0.0
    for axis, sigma in (("pixel", 1.0), ("row", 2.0), ("column", 3.0)):
        profile[axis]["reset_sigma_e"] = sigma
    result = _capture(profile, (512, 512), seed=233)
    noise = result["arrays"]["cds_residual_noise_e"]
    row_means = noise.mean(axis=1)
    column_means = noise.mean(axis=0)
    pixel_remainder = noise - row_means[:, None] - column_means[None, :] + noise.mean()
    assert np.var(row_means) == pytest.approx(4.0, rel=0.15)
    assert np.var(column_means) == pytest.approx(9.0, rel=0.15)
    assert np.var(pixel_remainder) == pytest.approx(1.0, rel=0.02)
    assert result["calibration"]["read_noise_variance_e2"] == 14.0
    assert result["summary"]["cds_variance_interpretation"].startswith("per_pixel_marginal_only")


def test_cds_profile_rejects_double_counting_unknown_conditions_and_hdr_fusion():
    profile = _profile()
    for invalid in (
        {**profile, "reset_common_sigma_e": -1},
        {**profile, "pixel": {**profile["pixel"], "sample_correlation": 1.0001}},
        {**profile, "temperature_range_c": [26, 25]},
        {**profile, "source": {"kind": "measured", "description": " "}},
        {**profile, "extra": True},
    ):
        with pytest.raises(ValueError):
            electrical.validate_cds_noise_profile(invalid)
    model = _model()
    model["read_noise_e"] = 1.0
    with pytest.raises(ValueError, match="replaces aggregate"):
        electrical.capture_electrons(
            np.zeros((4, 4)), 0.01, model, analog_gain=1, temperature_c=25,
            geometry={"full_sensor_shape": [4, 4], "crop_origin": [0, 0]},
            noise_enabled=True, seed=1, cds_profile=profile,
        )
    with pytest.raises(ValueError, match="CDS noise domain"):
        electrical.capture_electrons(
            np.zeros((4, 4)), 0.01, _model(), analog_gain=1, temperature_c=30,
            geometry={"full_sensor_shape": [4, 4], "crop_origin": [0, 0]},
            noise_enabled=True, seed=1, cds_profile=profile,
        )
    from camera_platform.hdr_runtime import default_capture_program

    program = default_capture_program({
        "full_well_e": 1_000_000.0, "read_noise_e": 0.0,
        "dark_current_e_per_s": 0.0, "gain": 1.0,
        "bit_depth": 24, "black_level_dn": 100_000.0,
    })
    program["schema"] = "camera_capture_program_v3"
    program["cds_read_noise"] = profile
    with pytest.raises(ValueError, match="independent-pixel HDR merge"):
        validate_capture_program(program)
    program["hdr_merge"] = None
    assert validate_capture_program(program)["cds_read_noise"] == profile


def test_registered_cds_program_runs_through_frozen_test_raw_isp_and_bundle(request):
    service, _, assembly, scene, control, _, _, request_data = request.getfixturevalue(
        "registered_hdr_workflow"
    )
    draft = capture_programs.template(service, assembly["id"], "independent_bracket_cds")
    assert draft["payload"]["schema"] == "camera_capture_program_v3"
    program = capture_programs.create(service, draft)
    test = service.create_test(sequences.test_template(
        service, assembly["id"], scene["id"], control["id"], program["id"]
    ))
    queued = service.submit_run({**request_data, "capture_program_id": program["id"],
                                 "test_id": test["id"]})
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    run = service.registry.finish(job["id"], result=service.execute_run(job))
    summary = run["result"]["summary"]
    assert summary["capture_program_hash"] == program["content_hash"]
    assert summary["prediction"]["included_effects"] == test["included_effects"]
    assert "reset_signal_cds_native_row_column_correlated_read_noise" in test["included_effects"]
    assert summary["capture_metadata"][0]["schema"] == electrical.CDS_RESULT_SCHEMA
    assert summary["hdr_merge_applied"] is False
    assert summary["physical_validation"] == "not_established"
    noise = load_array(service, run, "electrical_cds_residual_noise_e")
    assert noise.shape == (4, 16, 16) and np.isfinite(noise).all()
    assert noise.std() > 0
    assert load_array(service, run, "raw_frames").dtype.kind in "iu"
    assert load_array(service, run, "rgb_frames").shape == (4, 16, 16, 3)
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        members = set(archive.namelist())
        for artifact in program["artifacts"]:
            assert f"artifacts/{artifact['sha256']}/{artifact['name']}" in members
        assert any(name.endswith("electrical_cds_residual_noise_e.npy") for name in members)
    assert service.registry.audit()["integrity"] == "verified"
