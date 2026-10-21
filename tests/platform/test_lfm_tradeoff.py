"""Finite PWM RAW diagnosis must not promote a missed target to physical infeasibility."""

from copy import deepcopy

import pytest
from test_pwm_period_policy import period_policy

from camera_platform import lfm_tradeoff


def observed(frame_id, exposure_ns, value_dn, high_rail=0.0):
    return {
        "frame_id": frame_id,
        "applied": {"exposure_ns": exposure_ns},
        "statistics": {"value_dn": value_dn, "digital_low_rail_fraction": 0.0,
                       "digital_high_rail_fraction": high_rail},
    }


def test_single_permitted_setting_miss_identifies_structural_block_without_extrapolation():
    policy = period_policy()
    policy["exposure_grid_ns"] = {"minimum": 2_000_000, "maximum": 12_000_000,
                                  "step": 2_000_000}
    frames = [observed("f0", 8_000_000, 571.6875),
              observed("f1", 8_000_000, 606.75)]
    original = deepcopy(frames)
    result = lfm_tradeoff.analyze(policy, frames)
    assert frames == original
    assert result["status"] == "only_permitted_exposure_missed_in_observed_frames"
    assert result["effective_exposure_grid_ns"]["point_count"] == 1
    assert result["structural_exposure_only_alternative"] is False
    assert result["next_discriminating_experiment"]["kind"] == (
        "compare_declared_gain_or_hdr_paths_at_same_pwm_period"
    )
    assert result["target_feasibility"] == "not_established_by_finite_raw_sequence"
    assert result["physical_lfm_performance"] == "not_established"
    assert result["gain_or_photocharge_extrapolation"] is False
    assert result["simulation_truth_used"] is False
    assert result["observations"][0]["signed_target_distance_dn"] == 271.6875


def test_multiple_settings_need_a_matched_exposure_screen_even_if_all_seen():
    policy = period_policy()
    frames = [observed("f0", 8_000_000, 500), observed("f1", 16_000_000, 600),
              observed("f2", 24_000_000, 700)]
    result = lfm_tradeoff.analyze(policy, frames)
    assert result["unobserved_setting_count"] == 0
    assert result["status"] == "target_unobserved_with_exposure_alternatives"
    assert result["next_discriminating_experiment"]["kind"] == (
        "run_matched_fixed_exposure_lattice_screen"
    )
    assert result["target_feasibility"] == "not_established_by_finite_raw_sequence"
    frames[1]["statistics"]["value_dn"] = 250
    met = lfm_tradeoff.analyze(policy, frames)
    assert met["status"] == "target_observed_finite_samples"
    assert met["in_target_frame_count"] == 1
    assert met["physical_lfm_performance"] == "not_established"


@pytest.mark.parametrize("mutate", [
    lambda f: f[0]["applied"].update(exposure_ns=4_000_000),
    lambda f: f[0]["statistics"].update(value_dn=float("nan")),
    lambda f: f[0]["statistics"].update(digital_high_rail_fraction=2),
    lambda f: f.append(deepcopy(f[0])),
])
def test_invalid_or_forged_actual_observations_are_refused(mutate):
    frames = [observed("f0", 8_000_000, 500)]
    mutate(frames)
    with pytest.raises(ValueError):
        lfm_tradeoff.analyze(period_policy(), frames)
