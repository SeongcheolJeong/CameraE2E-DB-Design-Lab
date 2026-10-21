"""Bounded PWM exposure-space diagnosis from actual applied settings and RAW codes.

This describes the discrete control space and sampled outcomes. It deliberately
does not extrapolate delivered RAW through retained/trapped charge, analog gain,
readout clipping, or an unobserved PWM phase.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from . import electrical_control

SCHEMA = "camera_pwm_exposure_space_diagnosis_v1"


def _observation(frame, effective, target):
    if not isinstance(frame, Mapping) or not isinstance(frame.get("frame_id"), str):
        raise ValueError("PWM diagnosis requires actual frame IDs")
    applied, statistics = frame.get("applied"), frame.get("statistics")
    if not isinstance(applied, Mapping) or not isinstance(statistics, Mapping):
        raise ValueError("PWM diagnosis requires actual applied settings and RAW statistics")
    exposure = applied.get("exposure_ns")
    if (type(exposure) is not int or exposure < effective["minimum"]
            or exposure > effective["maximum"]
            or (exposure - effective["minimum"]) % effective["step"]):
        raise ValueError("Observed exposure is outside the effective PWM lattice")
    value = statistics.get("value_dn")
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("PWM diagnosis requires a finite original RAW meter")
    for key in ("digital_low_rail_fraction", "digital_high_rail_fraction"):
        fraction = statistics.get(key)
        if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError("PWM diagnosis requires bounded original RAW rail fractions")
    return {
        "frame_id": frame["frame_id"],
        "applied_exposure_ns": exposure,
        "observed_dn": float(value),
        "in_target": target["minimum"] <= value <= target["maximum"],
        "signed_target_distance_dn": (
            float(value - target["minimum"]) if value < target["minimum"] else
            float(value - target["maximum"]) if value > target["maximum"] else 0.0
        ),
        "digital_low_rail_fraction": float(statistics["digital_low_rail_fraction"]),
        "digital_high_rail_fraction": float(statistics["digital_high_rail_fraction"]),
    }


def analyze(policy, frames):
    """Distinguish a structural exposure constraint from unproved target feasibility."""
    policy = electrical_control.validate_policy(policy)
    if policy["schema"] != electrical_control.PERIOD_POLICY_SCHEMA:
        raise ValueError("PWM exposure diagnosis requires the whole-period policy")
    if not isinstance(frames, list) or not frames:
        raise ValueError("PWM exposure diagnosis requires at least one actual RAW frame")
    effective = electrical_control.effective_exposure_grid(policy)
    target = policy["target_black_relative_dn"]
    observations = [_observation(frame, effective, target) for frame in frames]
    if len({item["frame_id"] for item in observations}) != len(observations):
        raise ValueError("PWM exposure diagnosis requires unique actual frame IDs")
    sampled = sorted({item["applied_exposure_ns"] for item in observations})
    any_target = any(item["in_target"] for item in observations)
    single = effective["point_count"] == 1
    if any_target:
        status = "target_observed_finite_samples"
        next_test = "repeat_matched_phase_seed_and_independent_measurement"
    elif single:
        status = "only_permitted_exposure_missed_in_observed_frames"
        next_test = "compare_declared_gain_or_hdr_paths_at_same_pwm_period"
    else:
        status = "target_unobserved_with_exposure_alternatives"
        next_test = "run_matched_fixed_exposure_lattice_screen"
    return {
        "schema": SCHEMA,
        "scope": "authored_policy_and_finite_original_raw_observations",
        "target_dn": dict(target),
        "effective_exposure_grid_ns": effective,
        "observations": observations,
        "sampled_exposure_ns": sampled,
        "sampled_setting_count": len(sampled),
        "unobserved_setting_count": effective["point_count"] - len(sampled),
        "in_target_frame_count": sum(item["in_target"] for item in observations),
        "status": status,
        "structural_exposure_only_alternative": not single,
        "structural_claim": (
            "No different exposure is permitted by this frozen whole-period policy."
            if single else "Other exposures are permitted; their outcomes are not inferred."
        ),
        "next_discriminating_experiment": {
            "kind": next_test,
            "hold_fixed": [
                "versioned_assembly", "scene_and_illumination", "pwm_waveform_and_phase",
                "native_raw_meter", "frame_timing_and_clear", "temperature",
            ],
            "record": [
                "actual_applied_settings", "original_integer_raw", "digital_and_well_rails",
                "read_noise_and_retained_state_evidence", "phase_and_seed_coverage",
            ],
            "automatic_selection": False,
        },
        "target_feasibility": "not_established_by_finite_raw_sequence",
        "physical_lfm_performance": "not_established",
        "simulation_truth_used": False,
        "gain_or_photocharge_extrapolation": False,
    }
