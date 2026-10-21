"""Actual AE photons/RAW to causal code ISP; no truth restoration is asserted."""

from copy import deepcopy

import numpy as np
import pytest
from test_hdr_runtime import request_data as request_data
from test_retained_isp import inputs, temporal

from camera_platform import electrical_control, nonlinear_examples
from camera_platform import retained_isp as code
from camera_platform.electrical_control_runtime import default_capture_program, run
from camera_platform.electrical_isp_source import _replay_controller
from camera_platform.isp import ISPError
from camera_platform.registry import digest
from camera_platform.timing import MAX_EXPOSURE_NS, MAX_TIME_NS


def acquisition(request_data, *, noise=False, nonlinear=False):
    data = deepcopy(request_data)
    data["noise_enabled"] = noise
    first = deepcopy(data["control_sequence"]["frames"][0])
    frames = []
    for index in range(6):
        frame = deepcopy(first)
        frame.update(frame_id=f"observed-{index}", frame_start_ns=index * 10_000_000)
        frame["applied"].update(exposure_ns=2_000_000, applied_at_ns=index * 10_000_000)
        frames.append(frame)
    data["control_sequence"]["frames"] = frames
    data["scene_sequence"]["knots"][-1]["time_ns"] = 100_000_000
    policy = electrical_control.default_policy([8, 10], initial_exposure_ns=2_000_000)
    policy["exposure_grid_ns"]["maximum"] = 8_000_000
    policy["target_black_relative_dn"] = {"minimum": 1000, "maximum": 1200}
    for key in policy["latency"]:
        if key != "kind":
            policy["latency"][key] = 0
    program = default_capture_program(data["readout"], [f["frame_id"] for f in frames], policy)
    program["electrical_model"].update(dark_current_e_per_s=0, read_noise_e=0, post_gain_noise_v=0)
    if nonlinear:
        program["electrical_model"] = nonlinear_examples.single_model(data["readout"])
    program["retained_charge_model"]["transfer_efficiency"] = [0.5] * 4
    data["capture_program"] = program
    return data


def observed_inputs(output):
    summary = output["summary"]
    return {
        "raw_format": summary["raw_formats"][0],
        "geometry": summary["geometry"],
        "frame_records": summary["control_sequence"]["frames"],
        "events": summary["retained_charge"]["events"],
    }


@pytest.mark.parametrize("noise", [False, True], ids=["continuous", "stochastic"])
@pytest.mark.parametrize("nonlinear", [False, True], ids=["linear", "nonlinear"])
def test_actual_photo_ae_changes_reset_history_and_plateau_adds_code_persistence(
    request_data, noise, nonlinear
):
    data = acquisition(request_data, noise=noise, nonlinear=nonlinear)
    fixed = deepcopy(data)
    fixed["capture_program"]["electrical_control_policy"]["enabled"] = False
    active, baseline = run(data), run(fixed)
    raw = active["arrays"]["raw_groups"][:, 0]
    saved_raw = raw.tobytes()
    metadata = observed_inputs(active)
    filtered = code.process_raw(raw, profile=temporal(), **metadata)
    bypass = code.process_raw(raw, profile=code.default_profile(), **metadata)
    baseline_display = code.process_raw(
        baseline["arrays"]["raw_groups"][:, 0],
        profile=code.default_profile(),
        **observed_inputs(baseline),
    )
    np.testing.assert_array_equal(raw[0], baseline["arrays"]["raw_groups"][0, 0])
    assert not np.array_equal(raw[1:], baseline["arrays"]["raw_groups"][1:, 0])
    assert not np.array_equal(
        bypass["arrays"]["rgb_frames"][1:], baseline_display["arrays"]["rgb_frames"][1:]
    )
    assert not np.array_equal(
        active["arrays"]["mean_electrons"][1:], baseline["arrays"]["mean_electrons"][1:]
    )
    exposure = active["arrays"]["applied_exposure_ns"]
    assert exposure.tolist()[:5] == [2_000_000, 4_000_000, 8_000_000, 8_000_000, 8_000_000]
    assert 0 < raw.min() <= raw.max() < 4095  # No code-rail reset in this fixture.
    signal = raw.astype(float) - metadata["raw_format"]["black_level_dn"]
    expected = signal.copy()
    for index in range(1, len(raw)):
        if exposure[index] == exposure[index - 1]:
            expected[index] = (signal[index] + expected[index - 1]) / 2
        else:
            assert np.all(filtered["arrays"]["temporal_reset_reason_frames"][index] & 2)
    np.testing.assert_array_equal(filtered["arrays"]["temporal_dn_frames"], expected)
    assert np.any(expected[3] != signal[3])  # This is additional ISP history, not lag correction.
    assert filtered["summary"]["raw_format"] == metadata["raw_format"]
    assert filtered["summary"]["charge_inversion"] is False
    assert raw.tobytes() == saved_raw


def test_planned_initial_exposure_cannot_replace_realized_ae_controls(request_data):
    data = acquisition(request_data)
    active = run(data)
    metadata = observed_inputs(active)
    metadata["frame_records"] = data["control_sequence"]["frames"]
    with pytest.raises(ISPError, match="Row event exposure differs"):
        code.process_raw(active["arrays"]["raw_groups"][:, 0], profile=temporal(), **metadata)


def test_actual_clear_resets_plateau_and_future_raw_cannot_change_earlier_isp(request_data):
    data = acquisition(request_data)
    data["capture_program"]["pd_clear_before_exposure"][3]["clear"] = True
    active = run(data)
    metadata = observed_inputs(active)
    raw = active["arrays"]["raw_groups"][:, 0]
    result = code.process_raw(raw, profile=temporal(), **metadata)
    assert np.all(result["arrays"]["temporal_reset_reason_frames"][3] & 4)
    np.testing.assert_array_equal(
        result["arrays"]["temporal_dn_frames"][3],
        raw[3].astype(float) - metadata["raw_format"]["black_level_dn"],
    )
    future = raw.copy()
    future[-1] = 2000
    altered = code.process_raw(future, profile=temporal(), **metadata)
    for name in ("temporal_dn_frames", "pre_tone_code_rgb_frames", "rgb_frames"):
        np.testing.assert_array_equal(result["arrays"][name][:-1], altered["arrays"][name][:-1])


@pytest.mark.parametrize("offset", [-40_000_000, 2**50])
def test_actual_acquisition_and_isp_are_invariant_to_signed_relative_clock_translation(
    request_data, offset
):
    data = acquisition(request_data)
    original = run(data)
    for frame in data["control_sequence"]["frames"]:
        frame["frame_start_ns"] += offset
        frame["applied"]["applied_at_ns"] += offset
    for knot in data["scene_sequence"]["knots"]:
        knot["time_ns"] += offset
    shifted = run(data)
    actual, trace = _replay_controller(
        shifted["arrays"]["raw_groups"][:, 0], shifted["summary"]["raw_formats"][0],
        data["control_sequence"], data["capture_program"], shifted["summary"]["geometry"],
    )
    assert actual == shifted["summary"]["control_sequence"]
    assert trace == shifted["summary"]["electrical_control"]
    np.testing.assert_array_equal(original["arrays"]["raw_groups"], shifted["arrays"]["raw_groups"])
    left = code.process_raw(
        original["arrays"]["raw_groups"][:, 0], profile=temporal(), **observed_inputs(original)
    )
    right = code.process_raw(
        shifted["arrays"]["raw_groups"][:, 0], profile=temporal(), **observed_inputs(shifted)
    )
    for name in left["arrays"]:
        np.testing.assert_array_equal(left["arrays"][name], right["arrays"][name], err_msg=name)
    assert shifted["summary"]["control_sequence"]["frames"][0]["frame_start_ns"] == offset


@pytest.mark.parametrize("origin", [-MAX_TIME_NS, 0, MAX_TIME_NS - 34])
def test_exact_signed_boundaries_preserve_row_intervals_and_filter_state(origin):
    raw = np.broadcast_to(np.array([464, 264, 164])[:, None, None], (3, 2, 2)).astype(np.int32)
    metadata = inputs(
        raw, starts=[origin, origin + 11, origin + 22], exposure=[5] * 3,
        offsets=[[0, 7]] * 3,
    )
    result = code.process_raw(raw, profile=temporal(), **metadata)["arrays"]
    np.testing.assert_array_equal(result["temporal_dn_frames"][:, 0, 0], [400, 300, 200])
    np.testing.assert_array_equal(result["temporal_inter_transfer_interval_ns"][1:], 11)
    assert not result["temporal_reset_mask_frames"][1:].any()


@pytest.mark.parametrize(
    "kind",
    [
        "start_low", "row_high", "boolean_time", "float_time",
        "duration_high", "duration_zero", "span",
    ],
)
def test_unsupported_time_values_are_refused_before_int64_arithmetic(kind):
    raw = np.full((2, 2, 2), 200, dtype=np.int32)
    metadata = inputs(raw, starts=[0, 10], exposure=[5, 5])
    records, events = metadata["frame_records"], metadata["events"]
    if kind == "start_low":
        records[0]["frame_start_ns"] = -MAX_TIME_NS - 1
    elif kind == "row_high":
        events["frames"][0]["row_transfer_ns"][0] = 2**100
    elif kind == "boolean_time":
        events["frames"][0]["row_open_ns"][0] = False
    elif kind == "float_time":
        records[0]["frame_start_ns"] = 0.0
    elif kind == "duration_high":
        records[0]["applied"]["exposure_ns"] = MAX_EXPOSURE_NS + 1
    elif kind == "duration_zero":
        records[0]["applied"]["exposure_ns"] = 0
    else:
        # Both endpoint arrays fit int64 and the individual JSON time bounds;
        # the complete sequence still exceeds the shared exact-span contract.
        for record, event, start in zip(records, events["frames"], [-MAX_TIME_NS, 0], strict=True):
            record["frame_start_ns"] = start
            event["row_open_ns"] = [start] * 2
            event["row_transfer_ns"] = [start + 5] * 2
    events["schedule_sha256"] = digest({k: v for k, v in events.items() if k != "schedule_sha256"})
    with pytest.raises(ISPError):
        code.process_raw(raw, profile=temporal(), **metadata)
