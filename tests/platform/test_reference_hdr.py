"""Reference-constrained HDR checked against scalar rates and censoring boundaries."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import hdr, reference_hdr


def calibration(shape=(8, 12), **changes):
    return {
        "dn_per_e": 1.0, "black_level_dn": 10.0, "white_level_dn": 4095,
        "full_well_e": 4000.0, "dark_e": np.zeros(shape),
        "read_noise_variance_e2": 4.0, "quantization_variance_e2": 1 / 12,
        "linear_ceiling_dn": 4010.0, "linear_floor_dn": 0.0, **changes,
    }


def geometry(origin=(0, 0)):
    return {"kind": "native_sensor", "full_sensor_shape": [40, 40],
            "crop_origin": list(origin)}


def merge(raw, exposures, calibrations=None, *, index=0, origin=(0, 0), **tuning):
    profile = reference_hdr.default_profile()
    profile["reference_frame_index"] = index
    profile["base_profile"].update(tuning)
    return reference_hdr.merge_reference_raw_bracket(
        raw, exposures, calibrations or [calibration(raw.shape[1:]) for _ in raw],
        geometry=geometry(origin), profile=profile,
    )


def test_scalar_long_exposure_reduces_conditional_noise_on_a_constant_background():
    raw = np.stack([np.full((8, 12), 110), np.full((8, 12), 410)]).astype(np.uint16)
    before = raw.copy()
    out = merge(raw, [0.5, 2])
    a, s = out["arrays"], out["summary"]
    v0 = (100 + 4 + 1 / 12) / 0.5**2
    v1 = (400 + 4 + 1 / 12) / 2**2
    expected_weights = [v1 / (v0 + v1), v0 / (v0 + v1)]
    np.testing.assert_allclose(a["weights"][:, 2, 3], expected_weights)
    np.testing.assert_allclose(a["merged_rate"], 200)
    np.testing.assert_allclose(a["estimated_variance"], v0 * v1 / (v0 + v1))
    assert np.all(a["estimated_variance"] < v0)
    assert s["fused_pixels"] == 96 and s["reference_fallback_pixels"] == 0
    assert s["reference_invalid_pixels"] == 0 and s["reference_only_unknown_pixels"] == 0
    assert s["reference_clipping_recovery"] is False
    assert s["uncertainty"] == {"kind": "unknown"}
    assert "selection bias" in s["estimated_variance_scope"]
    np.testing.assert_array_equal(raw, before)


def test_changed_object_keeps_reference_rate_and_rgb_without_averaging_double_images():
    raw = np.full((2, 8, 12), 110, dtype=np.uint16)
    raw[1, 2:6, 5:8] = 1010
    out = merge(raw, [1, 1])
    a = out["arrays"]
    assert a["reference_fallback_mask"][1:7, 4:9].all()
    assert a["disagreement_mask"].sum() == 12
    assert a["expanded_disagreement_mask"].sum() == 30
    np.testing.assert_allclose(a["merged_rate"], 100)
    np.testing.assert_allclose(a["linear_rgb"], 100)
    np.testing.assert_allclose(a["weights"][:, 3, 6], [1, 0])
    np.testing.assert_allclose(a["weights"][:, 0, 0], [0.5, 0.5])
    naive = hdr.merge_raw_bracket(
        raw, [1, 1], [calibration() for _ in raw],
        geometry=geometry(), profile=hdr.default_profile(),
    )["arrays"]
    assert naive["merged_rate"][3, 6] > a["merged_rate"][3, 6]
    assert np.max(naive["linear_rgb"] - a["linear_rgb"]) > 1


def test_pairwise_disagreement_is_not_hidden_by_each_candidate_agreeing_with_reference():
    raw = np.stack([np.full((8, 12), v) for v in [1010, 810, 1210]]).astype(np.uint16)
    out = merge(raw, [1, 1, 1])["arrays"]
    # At sigma 5, each end is within the reference pair threshold (~213/235),
    # while the 400-electron separation of the two ends exceeds ~224.
    assert out["disagreement_mask"].all()
    np.testing.assert_allclose(out["weights"][0], 1)
    np.testing.assert_allclose(out["weights"][1:], 0)
    np.testing.assert_allclose(out["merged_rate"], 1000)


@pytest.mark.parametrize("reference_code", [0, 1, 4010, 4095])
def test_invalid_reference_is_never_filled_from_a_usable_other_frame(reference_code):
    raw = np.stack([np.full((8, 12), reference_code), np.full((8, 12), 110)]).astype(np.uint16)
    out = merge(raw, [1, 1])
    a, s = out["arrays"], out["summary"]
    assert a["exposure_valid_mask"][1].all()
    assert not a["valid_mask"].any()
    assert a["temporal_consistency_unknown_mask"].all()
    assert s["reference_invalid_pixels"] == 96
    for name in ("merged_rate", "estimated_variance", "linear_rgb"):
        assert np.isnan(a[name]).all()
    assert not a["linear_rgb_valid_mask"].any()
    np.testing.assert_array_equal(a["rgb"], 0)
    np.testing.assert_array_equal(a["weights"], 0)


def test_valid_reference_alone_is_preserved_with_unknown_consistency():
    raw = np.stack([np.full((8, 12), 110), np.full((8, 12), 4010)]).astype(np.uint16)
    out = merge(raw, [0.5, 2])
    a, s = out["arrays"], out["summary"]
    assert a["reference_only_unknown_mask"].all()
    assert a["temporal_consistency_unknown_mask"].all()
    assert not a["temporal_consistency_mask"].any()
    np.testing.assert_allclose(a["merged_rate"], 200)
    np.testing.assert_allclose(a["weights"][:, 0, 0], [1, 0])
    assert s["reference_only_unknown_pixels"] == 96 and s["fused_pixels"] == 0


def test_hidden_positive_raw_floor_is_checked_before_reference_admission():
    cal = calibration(black_level_dn=100, linear_floor_dn=95, linear_ceiling_dn=2100,
                      dn_per_e=0.5)
    raw = np.stack([np.full((8, 12), 96), np.full((8, 12), 150)]).astype(np.uint16)
    assert not merge(raw, [1, 1], [cal, cal])["arrays"]["valid_mask"].any()


def test_signed_below_black_values_survive_when_above_linear_floor_guard():
    raw = np.stack([np.full((8, 12), 80), np.full((8, 12), 60)]).astype(np.uint16)
    cal = calibration(black_level_dn=100, linear_ceiling_dn=4095)
    out = merge(raw, [0.5, 1], [cal, cal])["arrays"]
    np.testing.assert_allclose(out["merged_rate"], -40)
    np.testing.assert_allclose(out["linear_rgb"], -40)
    np.testing.assert_array_equal(out["rgb"], 0)
    assert out["valid_mask"].all()


@pytest.mark.parametrize("origin", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_native_cfa_crop_phase_preserves_distinct_constant_rgb_values(origin):
    values = np.array([[100, 200], [200, 300]])
    native = np.tile(values, (20, 20))
    y, x = origin
    charge = native[y:y + 8, x:x + 12]
    raw = np.stack([charge + 10, charge * 2 + 10]).astype(np.uint16)
    out = merge(raw, [1, 2], origin=origin)["arrays"]
    np.testing.assert_allclose(out["linear_rgb"], np.broadcast_to([100, 200, 300], (8, 12, 3)))


def test_reference_index_changes_fallback_target_but_does_not_invent_registration():
    raw = np.stack([np.full((8, 12), 110), np.full((8, 12), 1010)]).astype(np.uint16)
    first = merge(raw, [1, 1], index=0)
    second = merge(raw, [1, 1], index=1)
    np.testing.assert_allclose(first["arrays"]["merged_rate"], 100)
    np.testing.assert_allclose(second["arrays"]["merged_rate"], 1000)
    assert second["summary"]["motion_alignment"] is False


def test_border_disagreement_guard_uses_native_reflection_without_wrapping():
    raw = np.full((2, 8, 12), 110, dtype=np.uint16)
    raw[1, 0, 0] = 1010
    a = merge(raw, [1, 1])["arrays"]
    assert a["expanded_disagreement_mask"].sum() == 4
    assert a["reference_fallback_mask"][:2, :2].all()
    assert not a["reference_fallback_mask"][-1].any()
    assert not a["reference_fallback_mask"][:, -1].any()


@pytest.mark.parametrize("value", [None, True, -1, 16, 0.0, "0"])
def test_profile_rejects_missing_or_nonnative_reference_index(value):
    profile = reference_hdr.default_profile()
    profile["reference_frame_index"] = value
    with pytest.raises(ValueError, match="reference_frame_index"):
        reference_hdr.validate_profile(profile)


def test_profile_and_raw_contracts_reject_extra_truth_fields_and_missing_actual_reference():
    profile = reference_hdr.default_profile()
    with pytest.raises(ValueError):
        reference_hdr.validate_profile({**profile, "motion_truth": []})
    with pytest.raises(ValueError):
        reference_hdr.validate_profile({**profile, "base_profile": None})
    normalized = reference_hdr.validate_profile(profile)
    normalized["base_profile"]["white_balance_rgb"][0] = 2
    assert profile["base_profile"]["white_balance_rgb"][0] == 1
    raw = np.full((2, 8, 12), 110, dtype=np.uint16)
    with pytest.raises(ValueError, match="actual RAW bracket"):
        merge(raw, [1, 1], index=2)
    with pytest.raises(ValueError, match="2..16"):
        merge(raw[:1], [1])
    with pytest.raises(ValueError, match="integer"):
        merge(raw.astype(float), [1, 1])
    altered = deepcopy(calibration())
    altered["clipping_truth"] = np.zeros((8, 12), dtype=bool)
    with pytest.raises(ValueError, match="no truth arrays"):
        merge(raw, [1, 1], [altered, calibration()])
