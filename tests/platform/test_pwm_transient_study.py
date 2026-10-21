"""Time-preserving study construction and independently specified window metrics."""

import sys
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from run_camera_pwm_transient import cadence_of, extend_static, make_case, relative_span


@pytest.fixture
def original():
    scene = {"spectral_irradiance": np.ones((8, 8, 5)),
             "unit": "W/m2/nm", "wavelength_nm": [450, 500, 550, 600, 650]}
    frames = [{"frame_id": f"frame-{i:04d}", "frame_start_ns": i*20_000_000,
               "applied": {"exposure_ns": 8_000_000, "gain": 1.0,
                           "applied_at_ns": i*20_000_000, "command_id": None}}
              for i in range(4)]
    return {"control_sequence": {"mode": {"shutter": "rolling", "line_time_ns": 50_000},
                                  "frames": frames},
            "scene_sequence": {"knots": [{"time_ns": 0, "scene": deepcopy(scene)},
                                           {"time_ns": 80_000_000, "scene": deepcopy(scene)}]},
            "capture_program": {
                "waveform": {"schema": "camera_light_waveform_v1", "kind": "pwm",
                             "period_ns": 8_000_000, "on_time_ns": 4_000_000,
                             "phase_origin_ns": 0, "clock_id": "test-clock", "low": 0.0, "high": 2.0},
                "pd_clear_before_exposure": [{"frame_id": f["frame_id"], "clear": False} for f in frames],
                "electrical_control_policy": {
                    "schema": "camera_electrical_exposure_policy_v2", "enabled": True,
                    "period_lattice": {"kind": "whole_pwm_period_v1", "period_ns": 8_000_000},
                    "exposure_grid_ns": {"minimum": 2_000_000, "maximum": 12_000_000, "step": 2_000_000}},
                "retained_charge_model": {"capture_rate_s": [10.0]*4, "release_time_s": [0.01]*4,
                                          "transfer_efficiency": [1.0]*4,
                                          "integration": {"max_step_ns": 100_000}}}}


def test_extension_preserves_initial_events_and_continuous_frame_clock(original):
    before = deepcopy(original["control_sequence"])
    extended = extend_static(original)
    frames = extended["control_sequence"]["frames"]
    assert frames[:4] == before["frames"]
    assert len(frames) == 16 and len({f["frame_id"] for f in frames}) == 16
    assert [f["frame_start_ns"] for f in frames] == [i*20_000_000 for i in range(16)]
    assert not any(c["clear"] for c in extended["capture_program"]["pd_clear_before_exposure"])
    assert original["control_sequence"] == before
    assert original["scene_sequence"]["knots"][-1]["time_ns"] == 80_000_000
    assert extended["scene_sequence"]["knots"][-1]["time_ns"] >= 308_400_000


def test_timing_candidate_has_same_pwm_phase_and_explicit_cadence_cost(original):
    extended = make_case(original, "period_locked", 2_000_000)
    assert cadence_of(extended) == 24_000_000
    assert cadence_of(original) == 20_000_000
    frames = extended["control_sequence"]["frames"]
    assert len({f["frame_start_ns"] % 8_000_000 for f in frames}) == 1
    assert all(f["applied"]["gain"] == 1 and f["applied"]["exposure_ns"] == 8_000_000 for f in frames)
    assert extended["capture_program"]["retained_charge_model"] == original["capture_program"]["retained_charge_model"]
    assert extended["capture_program"]["waveform"]["on_time_ns"] == 4_000_000


def test_no_capture_and_pd_clear_are_distinct_interventions(original):
    no_capture = make_case(original, "capture_off")["capture_program"]
    cleared = make_case(original, "clear_each_frame")["capture_program"]
    assert no_capture["retained_charge_model"]["capture_rate_s"] == [0.0]*4
    assert not any(c["clear"] for c in no_capture["pd_clear_before_exposure"])
    assert cleared["retained_charge_model"] == original["capture_program"]["retained_charge_model"]
    assert [c["clear"] for c in cleared["pd_clear_before_exposure"]] == [False] + [True]*15
    assert original["capture_program"]["retained_charge_model"]["capture_rate_s"] == [10.0]*4


def test_mean_light_equal_window_energy_is_not_the_same_temporal_input(original):
    constant = make_case(original, "mean_light")["capture_program"]
    # high=2 for 4 ms and low=0 for 4 ms integrates to 0.008 level-seconds.
    assert constant["waveform"]["level"] * 0.008 == 2.0 * 0.004
    assert constant["waveform"]["kind"] == "constant"
    assert constant["electrical_control_policy"]["enabled"] is False
    assert "period_lattice" not in constant["electrical_control_policy"]
    assert constant["retained_charge_model"] == original["capture_program"]["retained_charge_model"]


def test_half_step_changes_numerics_not_trap_parameters_or_acquisition(original):
    base = make_case(original, "original")
    half = make_case(original, "original", refine=True)
    assert half["capture_program"]["retained_charge_model"]["integration"]["max_step_ns"] == 50_000
    half["capture_program"]["retained_charge_model"]["integration"]["max_step_ns"] = 100_000
    assert half["control_sequence"] == base["control_sequence"]
    assert half["capture_program"] == base["capture_program"]


def test_relative_variation_cannot_be_improved_by_scalar_gain():
    values = np.array([580.0, 604.0] * 4)
    assert relative_span(values) == pytest.approx(24/592)
    assert relative_span(0.4*values) == pytest.approx(relative_span(values))
    assert relative_span([592.0]*8) == 0


def test_extension_refuses_unsupported_long_or_changed_scene(original):
    with pytest.raises(ValueError, match="4..16"):
        extend_static(original, frames=32)
    original["scene_sequence"]["knots"][-1]["scene"]["spectral_irradiance"][0, 0, 0] = 2
    with pytest.raises(ValueError, match="non-static"):
        extend_static(original)
