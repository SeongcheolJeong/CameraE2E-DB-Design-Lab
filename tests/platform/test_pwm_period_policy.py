"""Whole-period PWM policy stays causal and reports a missed code target."""

import numpy as np
import pytest

from camera_platform import electrical_control

FORMAT = {
    "cfa": "RGGB",
    "bit_depth": 12,
    "black_level_dn": 16.0,
    "response_type": "linear",
    "packing": "unpacked",
    "layout": "bayer",
    "hdr_streams": 1,
}


def period_policy():
    value = electrical_control.default_policy([4, 4], FORMAT)
    value.update(
        schema=electrical_control.PERIOD_POLICY_SCHEMA,
        exposure_grid_ns={"minimum": 2_000_000, "maximum": 24_000_000, "step": 2_000_000},
        target_black_relative_dn={"minimum": 200, "maximum": 300},
        period_lattice={"kind": "whole_pwm_period_v1", "period_ns": 8_000_000},
    )
    return value


def raw(value, policy):
    frame = np.full((4, 4), value + 16, dtype=np.uint16)
    return electrical_control.raw_statistics(frame, FORMAT, policy)


def test_integer_grid_intersection_and_raw_only_proposals():
    policy = period_policy()
    assert electrical_control.effective_exposure_grid(policy) == {
        "minimum": 8_000_000,
        "maximum": 24_000_000,
        "step": 8_000_000,
        "point_count": 3,
    }
    bright = electrical_control.propose_ae(raw(500, policy), 8_000_000, policy)
    assert bright["status"] == "period_lattice_hold_outside_target"
    assert bright["commanded"] is None
    assert bright["period_lattice"]["feasibility"] == (
        "target_reachability_unknown_from_one_raw_observation"
    )
    dark = electrical_control.propose_ae(raw(100, policy), 8_000_000, policy)
    assert dark["status"] == "update"
    assert dark["commanded"] == {"exposure_ns": 16_000_000}
    assert dark["interpretation"] == "delivered_code_feedback_not_photocharge_estimation"


def test_strict_period_and_initial_exposure_validation():
    policy = period_policy()
    with pytest.raises(ValueError, match="whole number"):
        electrical_control.propose_ae(raw(500, policy), 4_000_000, policy)
    policy["exposure_grid_ns"] = {"minimum": 3_000_000, "maximum": 7_000_000, "step": 2_000_000}
    with pytest.raises(ValueError, match="no whole-PWM-period"):
        electrical_control.validate_policy(policy)


def test_non_aligned_integer_grid_intersection_matches_small_exhaustive_cases():
    for minimum in range(1, 9):
        for step in range(1, 7):
            for period in range(1, 12):
                maximum = minimum + 5 * step
                values = [x for x in range(minimum, maximum + 1, step) if x % period == 0]
                policy = period_policy()
                policy["exposure_grid_ns"] = {
                    "minimum": minimum, "maximum": maximum, "step": step,
                }
                policy["period_lattice"]["period_ns"] = period
                if not values:
                    with pytest.raises(ValueError, match="no whole-PWM-period"):
                        electrical_control.validate_policy(policy)
                    continue
                effective = electrical_control.effective_exposure_grid(policy)
                assert list(range(
                    effective["minimum"], effective["maximum"] + 1, effective["step"]
                )) == values
