"""Chronological native optical inputs and resource bounds for the trap runtime."""

from copy import deepcopy

import numpy as np
import pytest
from test_hdr_runtime import request_data as request_data

from camera_platform import trapped_charge_runtime
from camera_platform.retained_charge_runtime import (
    EVENT_CONTRACT,
    PROGRAM_SCHEMA,
    TRAPPED_PROGRAM_SCHEMA,
    RetainedChargeRuntime,
    _parse,
    default_trapped_capture_program,
    validate_capture_program,
)
from camera_platform.runtime import RuntimeRequestError
from camera_platform.temporal import TemporalSceneRuntime


@pytest.fixture
def trapped(request_data):
    data = deepcopy(request_data)
    data["capture_program"] = default_trapped_capture_program(
        data["readout"], [f["frame_id"] for f in data["control_sequence"]["frames"]]
    )
    data["capture_program"]["electrical_model"].update(
        dark_current_e_per_s=0, read_noise_e=0, post_gain_noise_v=0
    )
    return data


def test_short_interval_illumination_and_saved_row_clock_are_bound_to_actual_generation(trapped):
    control, timeline = trapped["control_sequence"], trapped["scene_sequence"]
    control["mode"].update(shutter="rolling", line_time_ns=173_003)
    # A linearly changing scene and noncommensurate row/PWM boundaries require
    # subinterval transport; a single frame mean cannot stand in for chronology.
    timeline["knots"][-1]["scene"]["spectral_irradiance"] *= 3
    trapped["capture_program"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": control["clock"]["clock_id"], "period_ns": 700_003,
        "on_time_ns": 300_001, "phase_origin_ns": -234_567, "low": 0.2, "high": 1.,
    }
    output = RetainedChargeRuntime().execute(trapped)
    arrays, summary = output["arrays"], output["summary"]
    assert summary["schema_version"] == "camera_retained_charge_result_v2"
    events = summary["retained_charge"]["events"]
    assert events["schema"] == "camera_retained_row_events_v2"
    starts, ends = arrays["trap_segment_start_ns"], arrays["trap_segment_end_ns"]
    phase = arrays["trap_segment_phase"]
    counts = arrays["trap_segment_count"]
    waveform = trapped["capture_program"]["waveform"]
    for fi, event in enumerate(events["frames"]):
        for row in range(8):
            for code, begin, end in (
                (0, event["row_open_ns"][row] - event["row_gap_ns"][row],
                 event["row_open_ns"][row]),
                (1, event["row_open_ns"][row], event["row_transfer_ns"][row]),
            ):
                valid = [i for i in range(counts[fi]) if phase[fi, i] == code
                         and ends[fi, i, row] > starts[fi, i, row]]
                if not valid:
                    assert begin == end
                    continue
                assert starts[fi, valid[0], row] == begin
                assert ends[fi, valid[-1], row] == end
                np.testing.assert_array_equal(
                    ends[fi, valid[:-1], row], starts[fi, valid[1:], row]
                )
                assert max(ends[fi, valid, row] - starts[fi, valid, row]) <= 100_000
                if code == 1:
                    for i in valid:
                        a, b = int(starts[fi, i, row]), int(ends[fi, i, row])
                        # Independent integer enumeration of authored PWM edges.
                        first = (a - waveform["phase_origin_ns"]) // waveform["period_ns"] - 1
                        for cycle in range(first, first + 4):
                            origin = waveform["phase_origin_ns"] + cycle * waveform["period_ns"]
                            assert not a < origin < b
                            assert not a < origin + waveform["on_time_ns"] < b
    np.testing.assert_allclose(
        arrays["retained_expected_exposure_e"], arrays["mean_electrons"], rtol=1e-12, atol=1e-10
    )
    assert arrays["retained_captured_e"].sum() > 0
    assert arrays["retained_released_e"].sum() > 0
    assert arrays["raw_groups"].dtype.kind in "ui"
    forecast = RetainedChargeRuntime().estimate_resources(trapped)
    assert forecast["output_array_bytes_estimate"] >= sum(a.nbytes for a in arrays.values())
    assert forecast["working_array_bytes_estimate"] >= forecast["output_array_bytes_estimate"]
    assert set(summary["array_units"]) == set(arrays)


def test_stochastic_runtime_conserves_both_reservoirs_exactly(trapped):
    trapped["noise_enabled"] = True
    arrays = RetainedChargeRuntime().execute(trapped)["arrays"]
    for index in range(2):
        incoming = sum(arrays["retained_" + name][index] for name in (
            "initial_residual_e", "initial_trapped_e", "gap_generated_e", "exposure_generated_e"
        ))
        outgoing = sum(arrays["retained_" + name][index] for name in (
            "gap_overflow_e", "exposure_overflow_e", "clear_loss_e", "delivered_e",
            "residual_e", "trapped_e",
        ))
        assert incoming.dtype.kind in "iu"
        np.testing.assert_array_equal(incoming, outgoing)


def test_impractical_pwm_is_refused_before_optical_materialization(trapped, monkeypatch):
    trapped["capture_program"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": trapped["control_sequence"]["clock"]["clock_id"],
        "period_ns": 2, "on_time_ns": 1, "phase_origin_ns": 0, "low": 0, "high": 1,
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("Optical arrays must not be evaluated for an excessive time plan")

    monkeypatch.setattr(TemporalSceneRuntime, "_optical_rates", forbidden)
    with pytest.raises(RuntimeRequestError, match="bounded segments"):
        RetainedChargeRuntime().estimate_resources(trapped)


def test_large_native_step_forecast_refuses_before_expanding_any_row_list(trapped, monkeypatch):
    parsed = _parse(trapped)
    parsed["base"]["size"] = [5000, 5000]

    def forbidden(*args, **kwargs):
        raise AssertionError("The cheap work forecast must reject before row expansion")

    monkeypatch.setattr(trapped_charge_runtime, "_intervals", forbidden)
    with pytest.raises(ValueError, match="pixel-substep"):
        trapped_charge_runtime.plan(parsed)


@pytest.mark.parametrize("schema", [PROGRAM_SCHEMA, TRAPPED_PROGRAM_SCHEMA])
def test_program_schema_cannot_silently_swap_retained_and_trapped_models(trapped, schema):
    program = deepcopy(trapped["capture_program"])
    program["schema"] = schema
    if schema == PROGRAM_SCHEMA:
        program["event_contract"] = deepcopy(EVENT_CONTRACT)
    else:
        old = program["retained_charge_model"]
        program["retained_charge_model"] = {
            "schema": "camera_retained_photodiode_v1", "capacity_e": old["capacity_e"],
            "transfer_efficiency": old["transfer_efficiency"],
        }
    with pytest.raises(ValueError):
        validate_capture_program(program)
