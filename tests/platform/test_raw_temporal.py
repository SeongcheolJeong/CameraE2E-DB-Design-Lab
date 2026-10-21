"""Numerical behavior, dynamic response, causality and known ambiguity."""

import numpy as np
import pytest

from camera_platform.raw_temporal import CausalRawPairFilter, PairFilterConfig, process_sequence


def process(raw, **kwargs):
    return process_sequence(raw, black_level_dn=64, white_level_dn=4095,
                            timestamps_ns=[i * 20_000_000 for i in range(len(raw))],
                            cadence_ns=20_000_000, settings_keys=["fixed"] * len(raw), **kwargs)


def alternating(count=16):
    return np.stack([np.full((4, 4), 64 + (580 if i % 2 == 0 else 604), np.uint16)
                     for i in range(count)])


def test_alternation_removed_preserves_mean_original_and_reports_temporal_age():
    raw = alternating()
    before = raw.copy()
    result = process(raw)
    np.testing.assert_array_equal(raw, before)
    np.testing.assert_array_equal(result["code_dn"][:3], raw[:3])
    assert np.all(result["code_dn"][3:] == 64 + 592)
    assert np.all(result["blend_mask"][3:])
    assert result["nominal_blended_sample_age_ns"] == 10_000_000


def test_light_step_is_not_delayed_and_recovers_after_phase_history():
    raw = alternating()
    raw[8:] *= 2
    result = process(raw)
    np.testing.assert_array_equal(result["code_dn"][8:11], raw[8:11])
    assert not result["blend_mask"][8:11].any()
    assert result["blend_mask"][11:].all()


def test_new_edge_bypasses_entire_cell_without_color_mixing():
    raw = alternating()
    raw[8:, 0, 0] += 400
    result = process(raw)
    np.testing.assert_array_equal(result["code_dn"][8:11, :2, :2], raw[8:11, :2, :2])
    assert result["blend_mask"][8, 2:, 2:].all()


def test_future_frames_cannot_change_a_completed_prefix():
    raw = alternating()
    full, prefix = process(raw), process(raw[:9])
    np.testing.assert_array_equal(full["code_dn"][:9], prefix["code_dn"])
    changed = raw.copy(); changed[9:] = 0
    np.testing.assert_array_equal(process(changed)["code_dn"][:9], prefix["code_dn"])


@pytest.mark.parametrize("cause", ["cadence", "settings", "geometry"])
def test_reset_on_acquisition_boundary(cause):
    filt = CausalRawPairFilter(black_level_dn=64, white_level_dn=4095, cadence_ns=20_000_000)
    for i, raw in enumerate(alternating(4)):
        filt.push(raw, timestamp_ns=i*20_000_000, settings_key="a")
    raw = alternating(1)[0] if cause != "geometry" else np.full((6, 4), 650, np.uint16)
    result = filt.push(raw, timestamp_ns=85_000_000 if cause == "cadence" else 80_000_000,
                       settings_key="b" if cause == "settings" else "a")
    np.testing.assert_array_equal(result["code_dn"], raw)
    assert not result["history_ready"]


def test_rails_invalid_input_and_nonmonotonic_time():
    raw = alternating(); raw[8, 0, 0] = 4095
    result = process(raw)
    assert not result["blend_mask"][8:12, :2, :2].any()
    filt = CausalRawPairFilter(black_level_dn=64, white_level_dn=4095, cadence_ns=20_000_000)
    with pytest.raises(ValueError): filt.push(raw[0].astype(float), timestamp_ns=0, settings_key="a")
    filt.push(raw[0], timestamp_ns=0, settings_key="a")
    with pytest.raises(ValueError): filt.push(raw[1], timestamp_ns=0, settings_key="a")
    with pytest.raises(ValueError): PairFilterConfig(noise_sigma_dn=float("nan"))


def test_real_period_two_scene_change_is_a_known_unresolvable_raw_only_case():
    # The very same codes may come from a real alternating light/scene. This
    # test records distortion rather than falsely declaring a universal cure.
    raw = alternating()
    result = process(raw)
    assert np.max(np.abs(result["code_dn"][3:] - raw[3:])) == 12


def test_noise_threshold_is_explicit_not_read_from_simulator_truth():
    raw = alternating()
    rng = np.random.default_rng(831)
    raw = np.rint(raw + rng.normal(0, 14, raw.shape)).astype(np.uint16)
    low, high = process(raw), process(raw, config=PairFilterConfig(noise_sigma_dn=14))
    assert high["blend_mask"].sum() > low["blend_mask"].sum()
