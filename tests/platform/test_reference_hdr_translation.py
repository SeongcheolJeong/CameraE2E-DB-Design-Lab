"""RAW-only global integer registration, conservatively tied to one reference."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import reference_hdr

HEIGHT, WIDTH = 32, 40
GEOMETRY = {"kind": "native_sensor", "full_sensor_shape": [HEIGHT, WIDTH],
            "crop_origin": [0, 0]}


def _calibration():
    return {
        "dn_per_e": 1.0, "black_level_dn": 10.0, "white_level_dn": 4095,
        "full_well_e": 4000.0, "dark_e": np.zeros((HEIGHT, WIDTH)),
        "read_noise_variance_e2": 4.0, "quantization_variance_e2": 1 / 12,
        "linear_ceiling_dn": 4010.0, "linear_floor_dn": 0.0,
    }


def _bracket(*, occlusion=False):
    reference = np.random.default_rng(7).integers(
        120, 700, size=(HEIGHT, WIDTH), dtype=np.uint16
    )
    candidate = np.full((HEIGHT, WIDTH), 300, dtype=np.uint16)
    # candidate[y+2,x-2] == reference[y,x] where the two frames overlap.
    candidate[2:, :-2] = reference[:-2, 2:]
    if occlusion:
        candidate[12:16, 15:19] += 900
    return np.stack([reference + 10, candidate * 2 + 10]), reference


def _merge(raw, profile=None):
    return reference_hdr.merge_reference_raw_bracket(
        raw, [1.0, 2.0], [_calibration(), _calibration()],
        geometry=GEOMETRY,
        profile=profile or reference_hdr.default_translation_profile(),
    )


def test_raw_integer_shift_recovers_global_translation_without_warping_cfa():
    raw, reference = _bracket()
    before = raw.copy()
    translated = _merge(raw)
    legacy = _merge(raw, reference_hdr.default_profile())
    arrays, summary = translated["arrays"], translated["summary"]
    shift = summary["registration"]["shifts"][1]
    assert shift["status"] == "translated"
    assert shift["source_offset_yx_px"] == [2, -2]
    assert shift["mean_clipped_chi2"] == 0
    assert shift["runner_up_mean_clipped_chi2"] > 3
    assert summary["fused_pixels"] > legacy["summary"]["fused_pixels"] + 900
    overlap = np.s_[4:HEIGHT - 4, 4:WIDTH - 4]
    np.testing.assert_allclose(arrays["merged_rate"][overlap], reference[overlap])
    # Independent scalar noise calculation at one registered site.
    y, x = 10, 12
    signal = float(reference[y, x])
    v0 = signal + 4 + 1 / 12
    v1 = (2 * signal + 4 + 1 / 12) / 4
    np.testing.assert_allclose(arrays["weights"][:, y, x], [v1 / (v0 + v1),
                                                            v0 / (v0 + v1)])
    np.testing.assert_allclose(arrays["estimated_variance"][y, x],
                               v0 * v1 / (v0 + v1))
    np.testing.assert_array_equal(raw, before)
    assert summary["physical_validation"] == "not_established"


def test_occlusion_and_uncovered_edges_fall_back_to_reference_only():
    raw, reference = _bracket(occlusion=True)
    arrays = _merge(raw)["arrays"]
    assert arrays["reference_fallback_mask"][9:15, 16:22].all()
    np.testing.assert_allclose(
        arrays["merged_rate"][arrays["reference_fallback_mask"]],
        reference[arrays["reference_fallback_mask"]],
    )
    assert arrays["reference_only_unknown_mask"][:, :2].all()
    assert not arrays["registration_contribution_mask"][1, :, :2].any()
    assert arrays["fused_mask"][4:8, 4:8].all()


def test_ambiguous_nonzero_shift_drops_candidate_instead_of_ghosting():
    raw, reference = _bracket()
    profile = reference_hdr.default_translation_profile()
    profile["registration"]["minimum_score_gap"] = 25.0
    result = _merge(raw, profile)
    assert result["summary"]["registration"]["shifts"][1]["status"] == "rejected_ambiguous"
    arrays = result["arrays"]
    assert not arrays["registration_contribution_mask"][1].any()
    assert arrays["reference_only_unknown_mask"].all()
    np.testing.assert_allclose(arrays["merged_rate"], reference)
    np.testing.assert_array_equal(arrays["weights"][0], 1)
    np.testing.assert_array_equal(arrays["weights"][1], 0)


def test_large_photometric_change_rejects_even_the_identity_hypothesis():
    raw, reference = _bracket()
    raw[1] = (reference.astype(np.uint16) + 400) * 2 + 10
    result = _merge(raw)
    assert result["summary"]["registration"]["shifts"][1]["status"] == "rejected_ambiguous"
    assert result["summary"]["temporal_consistency_unknown_pixels"] == HEIGHT * WIDTH
    np.testing.assert_array_equal(result["arrays"]["weights"][1], 0)
    np.testing.assert_allclose(result["arrays"]["merged_rate"], reference)


def test_static_identity_is_accepted_and_clipped_reference_remains_unknown():
    raw, reference = _bracket()
    raw[1] = reference * 2 + 10
    raw[0, 3, 5] = 4095
    result = _merge(raw)
    arrays = result["arrays"]
    assert result["summary"]["registration"]["shifts"][1]["status"] == "identity_best"
    assert not arrays["valid_mask"][3, 5]
    assert np.isnan(arrays["merged_rate"][3, 5])
    assert arrays["weights"][:, 3, 5].sum() == 0
    assert arrays["fused_mask"].sum() > 1000


@pytest.mark.parametrize("field,value", [
    ("max_shift_px", 3), ("max_shift_px", True),
    ("minimum_common_pixels", 31), ("minimum_common_pixels", 3.0),
    ("maximum_mean_clipped_chi2", float("nan")),
    ("minimum_score_gap", 26.0),
])
def test_translation_profile_rejects_invalid_registration_contract(field, value):
    profile = reference_hdr.default_translation_profile()
    profile["registration"][field] = value
    with pytest.raises(ValueError):
        reference_hdr.validate_profile(profile)


def test_translation_profile_is_separate_from_legacy_schema():
    profile = reference_hdr.default_profile()
    profile["registration"] = deepcopy(reference_hdr.default_translation_profile()["registration"])
    with pytest.raises(ValueError, match="fields differ"):
        reference_hdr.validate_profile(profile)
