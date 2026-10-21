"""Ideal PWM phase sensitivity is exposed separately from authored-phase RAW."""

import numpy as np
import pytest
import test_hdr_runtime

from camera_platform.hdr_runtime import HDRBracketRuntime
from camera_platform.periodic_light import integrate_waveform_ns, phase_envelope_ns


def test_pwm_phase_envelope_matches_independent_exhaustive_integer_phase_sweep():
    spec = test_hdr_runtime.waveform(test_hdr_runtime.request_data.__wrapped__())
    spec.update(period_ns=11, on_time_ns=4, low=0.25, high=1.75)
    for duration in range(0, 38):
        expected = [
            integrate_waveform_ns({**spec, "phase_origin_ns": phase}, 0, duration)
            for phase in range(spec["period_ns"])
        ]
        assert phase_envelope_ns(spec, duration) == (min(expected), max(expected))
    assert phase_envelope_ns(spec, 22) == (17.5, 17.5)
    constant = {"schema": "camera_light_waveform_v1", "kind": "constant",
                "clock_id": spec["clock_id"], "level": 0.4}
    assert phase_envelope_ns(constant, 250) == (100.0, 100.0)
    with pytest.raises(ValueError):
        phase_envelope_ns(spec, -1)


def test_short_rolling_pwm_exposes_full_phase_range_without_changing_raw():
    request = test_hdr_runtime.request_data.__wrapped__()
    request["capture_program"]["hdr_merge"] = None
    request["capture_program"]["waveform"] = test_hdr_runtime.waveform(request)
    request["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=250_000)
    for frame in request["control_sequence"]["frames"]:
        frame["applied"]["exposure_ns"] = 250_000
    output = HDRBracketRuntime().execute(request)
    arrays, summary = output["arrays"], output["summary"]
    np.testing.assert_array_equal(
        arrays["waveform_row_all_phase_min_effective_s"], np.zeros((2, 8)))
    np.testing.assert_array_equal(
        arrays["waveform_row_all_phase_max_effective_s"], np.full((2, 8), 0.00025))
    assert np.all(arrays["waveform_row_effective_s"] >=
                  arrays["waveform_row_all_phase_min_effective_s"])
    assert np.all(arrays["waveform_row_effective_s"] <=
                  arrays["waveform_row_all_phase_max_effective_s"])
    assert summary["waveform"]["phase_invariant_effective_seconds"] is False
    assert "not_a_joint_row_extremum_or_driver_jitter_model" in (
        summary["waveform"]["all_phase_envelope_scope"])
    assert set(arrays) == set(summary["array_units"])
    assert summary["physical_validation"] == "not_established"


def test_whole_cycle_hdr_pwm_has_equal_phase_bounds_and_same_raw_for_phase_shift():
    request = test_hdr_runtime.request_data.__wrapped__()
    request["capture_program"]["waveform"] = test_hdr_runtime.waveform(request)
    request["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=100_000)
    first = HDRBracketRuntime().execute(request)
    request["capture_program"]["waveform"]["phase_origin_ns"] = 317_123
    shifted = HDRBracketRuntime().execute(request)
    arrays, summary = first["arrays"], first["summary"]
    np.testing.assert_array_equal(
        arrays["waveform_row_all_phase_min_effective_s"],
        arrays["waveform_row_all_phase_max_effective_s"])
    np.testing.assert_array_equal(
        arrays["waveform_row_effective_s"],
        arrays["waveform_row_all_phase_min_effective_s"])
    np.testing.assert_array_equal(arrays["raw_frames"], shifted["arrays"]["raw_frames"])
    assert summary["waveform"]["phase_invariant_effective_seconds"] is True
    assert summary["hdr_merge_applied"] is True
