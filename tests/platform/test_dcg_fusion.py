"""Shared-charge RAW fusion: independent algebra, clipping and noise topology checks."""

import hashlib
import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform.dcg import default_profile, merge_shared_charge_raw, validate_profile


def calibration(shape=(4, 6), covariance=None, exposure=.5, dark=5):
    return {
        "schema": "camera_shared_charge_calibration_v1", "stream_ids": ["HCG", "LCG"],
        "exposure_s": exposure,
        "paths": [
            {"stream_id": "HCG", "dn_per_e": 4, "black_level_dn": 64,
             "white_level_dn": 4095, "linear_floor_dn": 0, "linear_ceiling_dn": 4000,
             "rounding_bound_e": .125},
            {"stream_id": "LCG", "dn_per_e": 1, "black_level_dn": 32,
             "white_level_dn": 4095, "linear_floor_dn": 0, "linear_ceiling_dn": 4000,
             "rounding_bound_e": .5},
        ],
        "read_noise_covariance_e2": [[4, 1], [1, 9]] if covariance is None else covariance,
        "dark_e": np.full(shape, dark, dtype=float),
        "geometry": {"full_sensor_shape": [20, 30], "crop_origin": [1, 3]},
    }


def codes(total, cal):
    values = np.asarray(total)
    if values.ndim == 0:
        values = np.full(cal["dark_e"].shape, total)
    return np.stack([np.rint(path["black_level_dn"] + values * path["dn_per_e"])
                     for path in cal["paths"]]).astype(np.int32)


def merge(raw, cal=None, **profile):
    return merge_shared_charge_raw(raw, cal or calibration(raw.shape[1:]),
                                   {**default_profile(), **profile})


def test_scalar_optimum_dark_and_common_shot_match_independent_algebra():
    cal = calibration()
    raw = codes(100, cal)
    raw[1] += 4  # z_H=100, z_L=104; every pixel remains linear.
    before = raw.copy()
    output = merge(raw, cal)
    a, s = output["arrays"], output["summary"]
    w = np.array([8 / 11, 3 / 11])
    total = w @ [100, 104]
    np.testing.assert_allclose(a["weights"][:, 0, 0], w, rtol=1e-15)
    np.testing.assert_allclose(a["merged_rate"], (total - 5) / .5)
    np.testing.assert_allclose(a["common_shot_variance_estimate_e2"], total)
    np.testing.assert_allclose(a["read_variance_e2"], 35 / 11)
    np.testing.assert_allclose(a["shared_shot_read_variance_estimate_e2"], total + 35 / 11)
    np.testing.assert_allclose(a["shared_shot_read_rate_variance_estimate"],
                               (total + 35 / 11) / .25)
    np.testing.assert_allclose(a["rounding_bound_e"], w @ [.125, .5])
    np.testing.assert_array_equal(raw, before)
    assert s["input_array_bytes_sha256"] == hashlib.sha256(before.tobytes()).hexdigest()
    assert s["covariance_regularization"] == "none"
    assert s["gain_path_difference_read_variance_e2"] == 11
    assert set(a) == set(s["array_units"])
    assert s["uncertainty"] == {"kind": "unknown"}
    assert s["physical_validation"] == "not_established"
    json.dumps(s, allow_nan=False)


@pytest.mark.parametrize("covariance,weights,variance,reason", [
    ([[0, 0], [0, 0]], [1, 0], 0, "flat_quadratic_explicit_stream_tie_break"),
    ([[4, 4], [4, 4]], [1, 0], 4, "flat_quadratic_explicit_stream_tie_break"),
    ([[4, -6], [-6, 9]], [.6, .4], 0, "interior_convex_minimum"),
    ([[1, 2], [2, 4]], [1, 0], 1, "constrained_endpoint_minimum"),
    ([[4, 2], [2, 1]], [0, 1], 1, "constrained_endpoint_minimum"),
    ([[0, 0], [0, 9]], [1, 0], 0, "constrained_endpoint_minimum"),
])
def test_psd_degenerate_positive_and_negative_correlations(covariance, weights, variance, reason):
    cal = calibration(covariance=covariance)
    output = merge(codes(100, cal), cal)
    np.testing.assert_allclose(output["arrays"]["weights"][:, 0, 0], weights, rtol=0, atol=1e-15)
    np.testing.assert_allclose(output["arrays"]["read_variance_e2"], variance, atol=1e-28)
    assert output["summary"]["weight_solution"] == reason


def test_tie_policy_only_changes_a_flat_objective_not_near_degenerate_minimum():
    cal = calibration(covariance=[[4, 4], [4, 4]])
    output = merge(codes(100, cal), cal, tie_break_stream="LCG")
    np.testing.assert_array_equal(output["arrays"]["weights"][1], 1)
    # Naive float a+c-2b rounds this positive curvature to zero.
    cal["read_noise_covariance_e2"] = [[1, 1], [1, float(np.nextafter(1, np.inf))]]
    output = merge(codes(100, cal), cal, tie_break_stream="LCG")
    np.testing.assert_array_equal(output["arrays"]["weights"][0], 1)
    assert output["summary"]["weight_solution"] == "constrained_endpoint_minimum"


def test_hidden_hcg_clipping_uses_lcg_and_common_limit_leaves_no_radiometry():
    cal = calibration(covariance=[[0, 0], [0, 0]], dark=0, exposure=1)
    cal["paths"][0]["linear_ceiling_dn"] = 464  # HCG range ends at 100 e before ADC white.
    cal["paths"][1]["linear_ceiling_dn"] = 232  # Common capacity 200 e.
    raw = codes(50, cal)
    raw[:, :, 2:4] = np.array([464, 182])[:, None, None]  # HCG clipped; LCG sees 150 e.
    raw[:, :, 4:] = np.array([464, 232])[:, None, None]
    a = merge(raw, cal, read_noise_guard_sigma=0)["arrays"]
    np.testing.assert_array_equal(a["merged_rate"][:, :2], 50)
    np.testing.assert_array_equal(a["merged_rate"][:, 2:4], 150)
    assert not a["usable_mask"][0, :, 2:].any()
    assert a["usable_mask"][1, :, 2:4].all()
    np.testing.assert_array_equal(a["weights"][1, :, 2:4], 1)
    assert a["gain_path_agreement_unknown_mask"][:, 2:].all()
    assert not a["valid_mask"][:, 4:].any()
    for key in ("merged_rate", "common_shot_variance_estimate_e2", "read_variance_e2",
                "shared_shot_read_variance_estimate_e2", "rounding_bound_e"):
        assert np.isnan(a[key][:, 4:]).all()
    np.testing.assert_array_equal(a["weights"][:, :, 4:], 0)
    assert raw.max() < 4095  # Hidden clipping must not be mistaken for ADC headroom.


def test_common_clipping_with_correlated_noise_can_pass_raw_guards_without_recovery_proof():
    cal = calibration(covariance=[[4, 4], [4, 4]], dark=0, exposure=1)
    capacity, preclip_charge, common_read = 100, 150, -8
    for path in cal["paths"]:
        path["linear_ceiling_dn"] = path["black_level_dn"] + capacity * path["dn_per_e"]
    # A possible correlated -4 sigma read moves common-clipped charge inside both
    # guards. The exact same codes could arise from an unclipped 92 e observation.
    raw = codes(min(preclip_charge, capacity) + common_read, cal)
    np.testing.assert_array_equal(raw, codes(92, cal))
    output = merge(raw, cal)
    arrays, summary = output["arrays"], output["summary"]
    assert arrays["usable_mask"].all()
    assert arrays["gain_path_agreement_mask"].all()
    np.testing.assert_array_equal(arrays["merged_rate"], 92)
    clipping_error = abs(arrays["merged_rate"] - (preclip_charge + common_read))
    assert np.all(clipping_error > 100 * arrays["rounding_bound_e"])
    assert "not simulator clipping truth or recovery proof" in summary["mask_scope"]
    assert "valid only for an unclipped linear transfer" in summary["rounding_scope"]
    assert "does not certify" in summary["rounding_scope"]


def test_boundary_selection_has_bias_and_conditional_variance_outside_nominal_formula():
    shape = (200, 300)
    cal = calibration(shape=shape, covariance=[[100, 100], [100, 100]], dark=0, exposure=1)
    cal["geometry"] = {"full_sensor_shape": list(shape), "crop_origin": [0, 0]}
    for path in cal["paths"]:
        path["linear_ceiling_dn"] = path["black_level_dn"] + 100 * path["dn_per_e"]
    # Isolate temporal read noise at a fixed, unclipped 85 e charge. Near the
    # guard, accepting codes selects the negative tail even with correct R.
    read = np.random.default_rng(7933).normal(0, 10, shape)
    output = merge(codes(85 + read, cal), cal)
    arrays, summary = output["arrays"], output["summary"]
    joint = arrays["usable_mask"].all(axis=0)
    assert 1000 < joint.sum() < 10000
    selected_noise = arrays["merged_rate"][joint] - 85
    assert selected_noise.mean() < -15
    assert selected_noise.var() < 40  # Different from nominal read variance 100.
    np.testing.assert_array_equal(arrays["read_variance_e2"][joint], 100)
    assert "without RAW-selection conditioning" in summary["variance_scope"]


def test_digital_attenuation_positive_floor_and_amplification_rounding_guard():
    cal = calibration(covariance=[[0, 0], [0, 0]], dark=0, exposure=1)
    cal["paths"][0].update(dn_per_e=1, linear_floor_dn=48, rounding_bound_e=.625)
    raw = codes(20, cal)
    raw[0, 0, :4] = [48, 49, 50, 51]
    a = merge(raw, cal, read_noise_guard_sigma=0)["arrays"]
    assert a["usable_mask"][0, 0, :4].tolist() == [False, False, True, True]
    assert a["path_rate"][0, 0, 2] == -14  # Below black is signed, not clipped to zero.
    cal["paths"][0].update(dn_per_e=8, linear_floor_dn=0, linear_ceiling_dn=864.4,
                            rounding_bound_e=.5625)
    raw = codes(50, cal)
    raw[0, 0, :2] = [858, 860]
    output = merge(raw, cal, read_noise_guard_sigma=0)
    assert output["arrays"]["usable_mask"][0, 0, :2].tolist() == [True, False]
    assert output["summary"]["path_diagnostics"][0]["raw_guard_dn"] == 5.5


def test_signed_dark_corrected_rate_keeps_common_dark_shot_estimate():
    cal = calibration(covariance=[[0, 0], [0, 0]], dark=20, exposure=2)
    a = merge(codes(5, cal), cal)["arrays"]
    np.testing.assert_array_equal(a["merged_rate"], -7.5)
    np.testing.assert_array_equal(a["common_shot_variance_estimate_e2"], 5)
    np.testing.assert_array_equal(a["shared_shot_read_rate_variance_estimate"], 1.25)


def test_gain_path_difference_cancels_common_shot_and_common_read_noise():
    cal = calibration(covariance=[[4, 4], [4, 4]], exposure=1, dark=0)
    for p in cal["paths"]:
        p.update(dn_per_e=1, black_level_dn=1024, white_level_dn=2**20 - 1,
                 linear_ceiling_dn=2**20 - 1, rounding_bound_e=.5)
    raw = codes(10000, cal)
    raw[1, :, 2:4] += 1  # Within paired rounding bounds.
    raw[1, :, 4:] += 2  # Shared shot/read cannot explain this difference.
    output = merge(raw, cal)
    a, s = output["arrays"], output["summary"]
    assert a["gain_path_agreement_mask"][:, :4].all()
    assert not a["gain_path_agreement_mask"][:, 4:].any()
    assert a["valid_mask"].all()  # A diagnostic does not silently discard RAW.
    assert s["gain_path_difference_read_variance_e2"] == 0
    assert s["gain_path_agreement_threshold_e"] == 1
    assert not any("temporal_consistency" in name for name in a)


def test_rounding_bounds_are_separate_and_do_not_act_as_independent_variance():
    cal = calibration(covariance=[[1, 0], [0, 1]], exposure=1, dark=0)
    raw = codes(100, cal)
    original = merge(raw, cal)
    for p in cal["paths"]:
        p["rounding_bound_e"] *= 5
    changed = merge(raw, cal)
    for name in ("weights", "merged_rate", "shared_shot_read_variance_estimate_e2"):
        np.testing.assert_array_equal(original["arrays"][name], changed["arrays"][name])
    np.testing.assert_allclose(changed["arrays"]["rounding_bound_e"],
                               original["arrays"]["rounding_bound_e"] * 5)
    assert not any("quantization_variance" in name for name in original["arrays"])


def test_joint_rounding_phase_sweep_stays_within_deterministic_bound():
    cal = calibration(shape=(4, 512), covariance=[[1, 0], [0, 1]], exposure=1, dark=0)
    cal["geometry"] = {"full_sensor_shape": [4, 512], "crop_origin": [0, 0]}
    cal["paths"][0].update(dn_per_e=2, rounding_bound_e=.25)
    total = np.broadcast_to(20 + np.arange(512) / 64, (4, 512))
    raw = codes(total, cal)
    a = merge(raw, cal)["arrays"]
    assert a["valid_mask"].all()
    assert np.all(np.abs(a["merged_rate"] - total) <= a["rounding_bound_e"])
    error_h = (raw[0] - 64) / 2 - total
    error_l = raw[1] - 32 - total
    assert abs(np.mean(error_h * error_l)) > .001  # The two roundings are not independent.


@pytest.mark.parametrize("covariance", [[[4, 1], [1, 9]], [[4, -3], [-3, 9]]])
def test_independent_shared_poisson_and_correlated_read_samples_do_not_halve_shot(covariance):
    # Sample count and tolerances are declared in advance, not fitted to an observed run.
    shape, count, shot_mean = (300, 400), 120_000, 100
    cal = calibration(shape=shape, covariance=covariance, exposure=1, dark=10)
    cal["geometry"] = {"full_sensor_shape": list(shape), "crop_origin": [0, 0]}
    for p, slope in zip(cal["paths"], [20, 5], strict=True):
        p.update(dn_per_e=slope, black_level_dn=10000, white_level_dn=2**24 - 1,
                 linear_ceiling_dn=2**24 - 1, rounding_bound_e=.5 / slope)
    rng = np.random.default_rng(21422)
    common = rng.poisson(shot_mean, count)
    read = rng.standard_normal((count, 2)) @ np.linalg.cholesky(covariance).T
    raw = np.stack([np.rint(10000 + slope * (common + read[:, j])).reshape(shape)
                     for j, slope in enumerate([20, 5])]).astype(np.int32)
    output = merge(raw, cal)
    a, s = output["arrays"], output["summary"]
    assert a["usable_mask"].all()
    weights = np.asarray(s["both_usable_weights"])
    read_var = float(weights @ covariance @ weights)
    expected_variance = shot_mean + read_var
    observed = float(np.var(a["merged_rate"], ddof=1))
    assert observed == pytest.approx(expected_variance, abs=2.0)
    wrong_independent = shot_mean * np.dot(weights, weights) + read_var
    assert observed > 1.4 * wrong_independent
    expected_difference = covariance[0][0] + covariance[1][1] - 2 * covariance[0][1]
    assert np.var(a["gain_path_difference_e"], ddof=1) == pytest.approx(expected_difference, abs=.4)
    assert np.mean(a["common_shot_variance_estimate_e2"]) == pytest.approx(shot_mean, abs=.2)


@pytest.mark.parametrize("origin", [[0, 0], [0, 1], [1, 0], [1, 1]])
def test_native_crop_geometry_and_unequal_greens_are_not_reordered(origin):
    cal = calibration(covariance=[[0, 0], [0, 0]], exposure=1, dark=0)
    cal["geometry"]["crop_origin"] = origin
    sites = np.array([[20, 40], [70, 100]])[(np.arange(4)[:, None] + origin[0]) % 2,
                                          (np.arange(6)[None, :] + origin[1]) % 2]
    output = merge(codes(sites, cal), cal)
    np.testing.assert_array_equal(output["arrays"]["merged_rate"], sites)
    assert output["summary"]["geometry"]["crop_origin"] == origin


def test_all_invalid_has_nan_radiometry_zero_weights_false_masks_and_finite_summary():
    cal = calibration()
    raw = np.zeros((2, 4, 6), dtype=np.uint16)
    raw[1] = 4095
    output = merge(raw, cal)
    a = output["arrays"]
    assert not a["valid_mask"].any() and not a["usable_mask"].any()
    assert not a["gain_path_agreement_mask"].any()
    assert a["gain_path_agreement_unknown_mask"].all()
    np.testing.assert_array_equal(a["weights"], 0)
    for name, array in a.items():
        if name != "weights" and not name.endswith("mask"):
            assert np.isnan(array).all(), name
    json.dumps(output["summary"], allow_nan=False)


def test_actual_shared_readout_calibration_hidden_clipping_and_oracle_isolation():
    from camera_platform.dcg_electrical import capture_shared_electrons, default_model

    model = default_model({"full_well_e": 1000, "read_noise_e": 2,
        "dark_current_e_per_s": 0, "gain": 1, "bit_depth": 12, "black_level_dn": 64})
    model["shared_charge_capacity_e"] = 400
    model["paths"][0]["input_linear_ceiling_e"] = 50
    model["paths"][1]["input_linear_ceiling_e"] = 500
    photo = np.tile([20., 20., 100., 100., 800., 800.], (4, 1))
    captured = capture_shared_electrons(photo, .5, model, temperature_c=25,
        geometry={"full_sensor_shape": [4, 6], "crop_origin": [0, 0]},
        noise_enabled=False, seed=7)
    original = merge(captured["arrays"]["raw_dn"], captured["calibration"])
    a = original["arrays"]
    assert a["usable_mask"][:, :, :2].all()
    assert not a["usable_mask"][0, :, 2:].any()
    assert a["usable_mask"][1, :, 2:4].all()
    assert not a["valid_mask"][:, 4:].any()
    assert captured["arrays"]["raw_dn"].max() < 4095
    assert np.all(np.abs(a["merged_rate"][:, :4] - photo[:, :4] / .5)
                  <= a["rounding_bound_rate"][:, :4])
    assert captured["arrays"]["well_saturated_mask"][:, 4:].all()
    for name in captured["summary"]["simulation_oracle"]["arrays"]:
        captured["arrays"][name][:] = (True if captured["arrays"][name].dtype.kind == "b"
                                      else 123456789)
    repeated = merge(captured["arrays"]["raw_dn"], captured["calibration"])
    for name, expected in original["arrays"].items():
        np.testing.assert_array_equal(repeated["arrays"][name], expected)
    assert repeated["summary"] == original["summary"]


def test_input_calibration_and_readonly_raw_are_unchanged():
    cal = calibration()
    raw = codes(100, cal)
    raw.flags.writeable = False
    copied = deepcopy(cal)
    merge(raw, cal)
    for key in cal:
        if key == "dark_e":
            np.testing.assert_array_equal(cal[key], copied[key])
        else:
            assert cal[key] == copied[key]


def test_native_pixel_bound_is_checked_before_output_work(monkeypatch):
    import camera_platform.dcg as dcg

    cal = calibration()
    raw = codes(100, cal)
    monkeypatch.setattr(dcg, "MAX_PIXELS", 23)
    monkeypatch.setattr(dcg, "_calibration", lambda *args: pytest.fail("Allocated after bound"))
    with pytest.raises(ValueError, match="native pixels"):
        merge(raw, cal)


@pytest.mark.parametrize("extra", ["mean_total_charge_e", "poisson_charge_e", "well_saturated_mask",
                                  "noiseless_reference", "quantization_variance_e2"])
def test_truth_or_independent_quantization_fields_cannot_enter_calibration(extra):
    cal = calibration()
    raw = codes(100, cal)
    cal[extra] = np.full((4, 6), 999)
    with pytest.raises(ValueError, match="exactly"):
        merge(raw, cal)


@pytest.mark.parametrize("change", [
    lambda c: c.update(stream_ids=["LCG", "HCG"]),
    lambda c: c["paths"].reverse(),
    lambda c: c.update(exposure_s=0),
    lambda c: c.update(exposure_s=float("nan")),
    lambda c: c.update(exposure_s=1e-300),
    lambda c: c["paths"][0].update(dn_per_e=0),
    lambda c: c["paths"][0].update(dn_per_e=True),
    lambda c: c["paths"][0].update(rounding_bound_e=-1),
    lambda c: c["paths"][0].update(linear_floor_dn=65),
    lambda c: c["paths"][0].update(linear_ceiling_dn=64),
    lambda c: c["paths"][0].update(white_level_dn=2047),
    lambda c: c["paths"][0].update(expected_electrons=1),
    lambda c: c.update(dark_e=np.full((4, 6), -1)),
    lambda c: c.update(dark_e=np.ones((3, 6))),
    lambda c: c["geometry"].update(crop_origin=[18, 0]),
    lambda c: c["geometry"].update(crop_origin=[False, 0]),
    lambda c: c.update(read_noise_covariance_e2=[[1, 2], [2, 1]]),
    lambda c: c.update(read_noise_covariance_e2=[[1, 0], [.000000000001, 1]]),
    lambda c: c.update(read_noise_covariance_e2=[[1, np.nextafter(1, np.inf)],
                                               [np.nextafter(1, np.inf), 1]]),
    lambda c: c.update(read_noise_covariance_e2=[[True, 0], [0, 1]]),
    lambda c: c.update(read_noise_covariance_e2=[[1e308, -1e308], [-1e308, 1e308]]),
])
def test_malformed_and_numerically_unsupported_calibration_is_refused(change):
    cal = calibration()
    raw = codes(100, cal)
    change(cal)
    with pytest.raises(ValueError):
        merge(raw, cal)


@pytest.mark.parametrize("raw", [np.zeros((2, 4, 6)), np.zeros((1, 4, 6), dtype=int),
                               np.zeros((2, 3, 6), dtype=int),
                               np.full((2, 4, 6), -1, dtype=int),
                               np.full((2, 4, 6), 4096, dtype=int)])
def test_raw_shape_integer_and_adc_domain_are_strict(raw):
    with pytest.raises(ValueError):
        merge(raw, calibration())


def test_profile_is_detached_and_requires_explicit_supported_guards():
    profile = default_profile()
    original = deepcopy(profile)
    validated = validate_profile(profile)
    validated["saturation_margin_dn"] = 99
    assert profile == original
    for change in ({"schema": "camera_hdr_merge_v1"}, {"method": "inverse_variance_raw"},
                   {"tie_break_stream": "automatic"}, {"saturation_margin_dn": 0},
                   {"gain_path_agreement_sigma": -1}, {"read_noise_guard_sigma": True},
                   {"reference_electrons": 100}):
        with pytest.raises(ValueError):
            validate_profile({**profile, **change})
