"""Observable-RAW HDR radiometry, censoring, temporal and native CFA boundaries."""

import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform.hdr import default_profile, merge_raw_bracket


def calibration(shape=(6, 8), **updates):
    return {
        "dn_per_e": 1.0, "black_level_dn": 10.0, "white_level_dn": 4095,
        "full_well_e": 4000.0, "dark_e": np.zeros(shape),
        "read_noise_variance_e2": 4.0, "quantization_variance_e2": 1 / 12,
        "linear_ceiling_dn": 4010.0, "linear_floor_dn": 0.0, **updates,
    }


def geometry(origin=(0, 0)):
    return {"kind": "native_sensor", "full_sensor_shape": [40, 40], "crop_origin": list(origin)}


def merge(raw, exposures, calibrations=None, *, origin=(0, 0), **tuning):
    profile = {**default_profile(), **tuning}
    return merge_raw_bracket(
        raw, exposures, calibrations or [calibration(raw.shape[1:]) for _ in raw],
        geometry=geometry(origin), profile=profile,
    )


def test_exposure_calibration_reconstructs_rate_and_inverse_variance_without_mutating_raw():
    raw = np.stack([np.full((6, 8), 110), np.full((6, 8), 410)]).astype(np.uint16)
    original = raw.copy()
    result = merge(raw, [0.5, 2])
    a, s = result["arrays"], result["summary"]
    np.testing.assert_array_equal(raw, original)
    np.testing.assert_allclose(a["exposure_rate"], 200)
    np.testing.assert_allclose(a["merged_rate"], 200)
    variances = np.array([(100 + 4 + 1 / 12) / 0.5**2, (400 + 4 + 1 / 12) / 2**2])
    expected_weights = (1 / variances) / (1 / variances).sum()
    np.testing.assert_allclose(a["weights"][:, 0, 0], expected_weights)
    np.testing.assert_allclose(a["estimated_variance"], 1 / (1 / variances).sum())
    assert a["temporal_consistency_mask"].all()
    assert not a["temporal_consistency_unknown_mask"].any()
    assert s["uncertainty"] == {"kind": "unknown"}
    json.dumps(s, allow_nan=False)


def test_shadow_and_highlight_recoverability_with_partial_bracket_saturation():
    raw = np.empty((2, 6, 8), dtype=np.uint16)
    raw[0, :, :4], raw[0, :, 4:] = 10, 60
    raw[1, :, :4], raw[1, :, 4:] = 15, 4010
    a = merge(raw, [0.01, 1], read_noise_guard_sigma=0)["arrays"]
    np.testing.assert_allclose(a["merged_rate"][:, :4], 5, atol=0.002)
    np.testing.assert_allclose(a["merged_rate"][:, 4:], 5000)
    assert a["valid_mask"].all()
    assert not a["exposure_valid_mask"][1, :, 4:].any()
    assert a["temporal_consistency_unknown_mask"][:, 4:].all()
    np.testing.assert_allclose(a["weights"][0, :, 4:], 1)


def test_identical_codes_with_different_gain_and_dark_calibration_recover_common_rate():
    raw = np.full((2, 6, 8), 120, dtype=np.uint16)
    first = calibration(dn_per_e=2, dark_e=np.full((6, 8), 5),
                        linear_ceiling_dn=4095, quantization_variance_e2=1 / 48)
    second = calibration(dn_per_e=0.5, dark_e=np.full((6, 8), 20),
                         linear_ceiling_dn=2010, quantization_variance_e2=1 / 3)
    a = merge(raw, [0.5, 2], [first, second])["arrays"]
    np.testing.assert_allclose(a["exposure_rate"], 100)
    np.testing.assert_allclose(a["merged_rate"], 100)
    assert a["temporal_consistency_mask"].all()


def test_gain_below_one_hidden_well_clipping_is_censored_below_adc_white():
    c = calibration(dn_per_e=0.5, full_well_e=1000, linear_ceiling_dn=510,
                    quantization_variance_e2=1 / 3)
    raw = np.stack([np.full((6, 8), 110), np.full((6, 8), 510)]).astype(np.uint16)
    a = merge(raw, [0.1, 1], [c, c])["arrays"]
    assert raw[1].max() < c["white_level_dn"]
    assert not a["exposure_valid_mask"][1].any()
    np.testing.assert_allclose(a["merged_rate"], 2000)
    assert a["temporal_consistency_unknown_mask"].all()


def test_read_noise_guard_protects_upper_and_lower_censoring_without_using_black_as_zero():
    c = calibration(read_noise_variance_e2=100)
    raw = np.full((1, 6, 8), 100, dtype=np.int32)
    raw[0, 0, :4] = [29, 30, 3979, 3980]
    a = merge(raw, [1], [c], read_noise_guard_sigma=3)["arrays"]
    assert a["exposure_valid_mask"][0, 0, :4].tolist() == [False, False, True, False]


def test_all_invalid_radiometry_is_nan_with_masks_and_finite_black_preview_and_json():
    raw = np.stack([np.zeros((6, 8)), np.full((6, 8), 4095)]).astype(np.uint16)
    result = merge(raw, [0.1, 1])
    a = result["arrays"]
    assert not a["valid_mask"].any()
    assert not a["temporal_consistency_mask"].any()
    assert a["temporal_consistency_unknown_mask"].all()
    for key in ("merged_rate", "estimated_variance", "linear_rgb"):
        assert np.isnan(a[key]).all()
    assert not a["linear_rgb_valid_mask"].any()
    np.testing.assert_array_equal(a["rgb"], 0)
    np.testing.assert_array_equal(a["weights"], 0)
    json.dumps(result["summary"], allow_nan=False)


def test_positive_raw_adc_floor_after_small_digital_gain_is_censored():
    c = calibration(black_level_dn=64, dn_per_e=0.01, full_well_e=4000,
                    linear_floor_dn=63.36, linear_ceiling_dn=104,
                    read_noise_variance_e2=10000, quantization_variance_e2=833.333)
    raw = np.full((1, 6, 8), 70, dtype=np.int32)
    raw[0, 0, :3] = [63, 66, 67]
    a = merge(raw, [1], [c], read_noise_guard_sigma=3)["arrays"]
    assert a["exposure_valid_mask"][0, 0, :3].tolist() == [False, False, True]
    assert a["valid_mask"][0, 3]


def test_same_noisy_raw_can_flag_change_but_single_observation_cannot_establish_consistency():
    raw = np.stack([np.full((6, 8), 110), np.full((6, 8), 1010)]).astype(np.uint16)
    a = merge(raw, [1, 1], consistency_sigma=3)["arrays"]
    assert a["valid_mask"].all()
    assert not a["temporal_consistency_mask"].any()
    assert not a["temporal_consistency_unknown_mask"].any()
    single = merge(raw[:1], [1])["arrays"]
    assert single["temporal_consistency_unknown_mask"].all()
    assert not single["temporal_consistency_mask"].any()


@pytest.mark.parametrize("origin", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_native_crop_cfa_phase_preserves_constant_sensor_rgb_including_reflected_edges(origin):
    phase = np.array([[0, 1], [1, 2]])
    yy, xx = np.indices((6, 8))
    channels = phase[(yy + origin[0]) % 2, (xx + origin[1]) % 2]
    rate = np.array([100, 200, 300])[channels]
    raw = np.stack([10 + rate, 10 + 2 * rate]).astype(np.uint16)
    a = merge(raw, [1, 2], origin=origin)["arrays"]
    assert a["linear_rgb_valid_mask"].all()
    np.testing.assert_allclose(a["linear_rgb"], np.broadcast_to([100, 200, 300], (6, 8, 3)))


def test_signed_dark_corrected_rates_survive_wb_ccm_and_only_preview_clips_negative():
    c = calibration(dark_e=np.ones((6, 8)))
    raw = np.full((2, 6, 8), 8, dtype=np.uint16)
    a = merge(raw, [1, 1], [c, c], read_noise_guard_sigma=0,
              white_balance_rgb=[2, 1, 1])["arrays"]
    np.testing.assert_allclose(a["merged_rate"], -3)
    np.testing.assert_allclose(a["linear_rgb"], np.broadcast_to([-6, -3, -3], (6, 8, 3)))
    np.testing.assert_array_equal(a["rgb"], 0)


def test_invalid_bayer_hole_invalidates_conservative_rgb_stencil_without_spreading_finite_fill():
    raw = np.full((2, 6, 8), 110, dtype=np.uint16)
    raw[:, 2, 3] = 0
    a = merge(raw, [1, 1])["arrays"]
    assert not a["valid_mask"][2, 3]
    assert a["valid_mask"].sum() == 47
    expected = np.ones((6, 8), dtype=bool)
    expected[1:4, 2:5] = False
    np.testing.assert_array_equal(a["linear_rgb_valid_mask"], expected)
    assert np.isnan(a["linear_rgb"][~expected]).all()
    assert np.isfinite(a["linear_rgb"][expected]).all()
    np.testing.assert_array_equal(a["rgb"][~expected], 0)


def test_display_tuning_changes_only_preview_and_preserves_hdr_rate_above_ldr_range():
    raw = np.full((2, 6, 8), 110, dtype=np.uint16)
    low = merge(raw, [0.001, 0.001])
    high = merge(raw, [0.001, 0.001], tone={
        "curve": "reinhard", "white_point": 2, "exposure_scale": 0.01})
    np.testing.assert_array_equal(low["arrays"]["merged_rate"], high["arrays"]["merged_rate"])
    np.testing.assert_array_equal(low["arrays"]["linear_rgb"], high["arrays"]["linear_rgb"])
    np.testing.assert_allclose(high["arrays"]["merged_rate"], 100000)
    assert (high["arrays"]["rgb"] > low["arrays"]["rgb"]).all()


@pytest.mark.parametrize("truth", ["mean_electrons", "reference_dn", "well_saturation_mask"])
def test_truth_arrays_cannot_enter_calibration_or_override_observed_raw(truth):
    raw = np.full((2, 6, 8), 110, dtype=np.uint16)
    c = calibration(**{truth: np.full((6, 8), 999)})
    with pytest.raises(ValueError, match="calibration.*no truth"):
        merge(raw, [1, 1], [c, calibration()])


def test_undefined_zero_variance_is_reported_without_infinite_weights():
    raw = np.full((1, 6, 8), 10, dtype=np.uint16)
    c = calibration(read_noise_variance_e2=0, quantization_variance_e2=0)
    result = merge(raw, [1], [c])
    assert result["summary"]["frame_diagnostics"][0]["undefined_variance_pixels"] == 48
    assert not result["arrays"]["valid_mask"].any()
    assert np.isfinite(result["arrays"]["weights"]).all()


@pytest.mark.parametrize("change", [
    lambda raw, e, c, g, p: c[0].update(linear_ceiling_dn=4095),
    lambda raw, e, c, g, p: c[0].update(dn_per_e=0),
    lambda raw, e, c, g, p: c[0].update(linear_floor_dn=11),
    lambda raw, e, c, g, p: c[0].update(dark_e=np.zeros((2, 2))),
    lambda raw, e, c, g, p: c[0].update(read_noise_variance_e2=float("nan")),
    lambda raw, e, c, g, p: e.__setitem__(0, 0),
    lambda raw, e, c, g, p: g.update(crop_origin=[0.5, 0]),
    lambda raw, e, c, g, p: p.update(saturation_margin_dn=0),
    lambda raw, e, c, g, p: raw.__setitem__((0, 0, 0), 4096),
])
def test_invalid_calibration_geometry_or_codes_are_refused_without_mutation(change):
    raw = np.full((2, 6, 8), 110, dtype=np.int32)
    exposures, calibrations = [1, 1], [calibration(), calibration()]
    geom, profile = geometry(), default_profile()
    change(raw, exposures, calibrations, geom, profile)
    before = raw.copy()
    with pytest.raises(ValueError):
        merge_raw_bracket(raw, exposures, calibrations, geometry=geom, profile=profile)
    np.testing.assert_array_equal(raw, before)


def test_caller_calibration_and_profile_are_not_modified():
    raw = np.full((2, 6, 8), 110, dtype=np.uint16)
    c, p = [calibration(), calibration()], default_profile()
    original = deepcopy(c)
    p_before = deepcopy(p)
    merge_raw_bracket(raw, [1, 1], c, geometry=geometry(), profile=p)
    assert p == p_before
    for old, new in zip(original, c, strict=True):
        np.testing.assert_array_equal(old.pop("dark_e"), new["dark_e"])
        assert old == {k: v for k, v in new.items() if k != "dark_e"}
