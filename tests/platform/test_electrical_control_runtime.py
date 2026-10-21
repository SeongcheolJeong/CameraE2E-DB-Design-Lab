"""Independent native charge/time counterexamples for causal RAW exposure feedback."""

import json
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_runtime import request_data as request_data
from test_native_temporal import native_request as native_request
from test_retained_charge_pwm_independent import (
    offset_clock,
)
from test_retained_charge_pwm_independent import (
    pwm_request as pwm_request,
)
from test_retained_charge_pwm_independent import (
    reference as pwm_reference,
)

from camera_platform import dcg_electrical, electrical_control, nonlinear_examples
from camera_platform.electrical_control_runtime import (
    ElectricalControlRuntime,
    _target_outcome,
    default_capture_program,
    retained_program,
    run,
)
from camera_platform.retained_charge_runtime import RetainedChargeRuntime
from camera_platform.runtime import RuntimeRequestError
from camera_platform.temporal import TemporalSceneRuntime


@pytest.fixture
def controlled(request_data):
    data = deepcopy(request_data)
    control = data["control_sequence"]
    first = deepcopy(control["frames"][0])
    control["frames"] = []
    for i in range(6):
        frame = deepcopy(first)
        frame.update(frame_id=f"frame-{i}", frame_start_ns=i * 10_000_000)
        frame["applied"].update(exposure_ns=2_000_000, applied_at_ns=i * 10_000_000)
        control["frames"].append(frame)
    data["scene_sequence"]["knots"][-1]["time_ns"] = 100_000_000
    policy = electrical_control.default_policy([8, 10])
    policy["target_black_relative_dn"] = {"minimum": 1000, "maximum": 1200}
    policy["latency"] = {
        key: value if key == "kind" else 0 for key, value in policy["latency"].items()
    }
    data["capture_program"] = default_capture_program(
        data["readout"], [f["frame_id"] for f in control["frames"]], policy
    )
    data["capture_program"]["electrical_model"].update(
        dark_current_e_per_s=0, read_noise_e=0, post_gain_noise_v=0
    )
    data["capture_program"]["retained_charge_model"]["transfer_efficiency"] = [0.5] * 4
    return data


def policy(data):
    return data["capture_program"]["electrical_control_policy"]


def disable(data):
    value = deepcopy(data)
    policy(value)["enabled"] = False
    return value


def test_unresolvable_v1_raw_span_is_rejected_before_control_optics(controlled, monkeypatch):
    model = controlled["capture_program"]["electrical_model"]
    model.update(conversion_gain_v_per_e=1e-15, digital_gain=1e-6)
    model["adc"]["full_scale_v"] = 1e6
    monkeypatch.setattr(
        TemporalSceneRuntime, "_optical_rates",
        lambda *args, **kwargs: pytest.fail("optics ran before electrical span preflight"),
    )
    issues = ElectricalControlRuntime().validate_request(controlled)
    assert len(issues) == 1
    assert "Electron-to-RAW linear span" in issues[0]["message"]


@pytest.mark.parametrize("noise", [False, True])
@pytest.mark.parametrize("nonlinear", [False, True])
def test_disabled_control_is_exact_existing_stateful_runtime_and_retry(
    controlled, noise, nonlinear
):
    data = disable(controlled)
    data["noise_enabled"] = noise
    if nonlinear:
        data["capture_program"]["electrical_model"] = nonlinear_examples.single_model(
            data["readout"]
        )
    old_request = {**data, "capture_program": retained_program(data["capture_program"])}
    old, new, retry = RetainedChargeRuntime().execute(old_request), run(data), run(data)
    assert set(new["arrays"]) == set(old["arrays"]) | {"applied_exposure_ns"}
    for name, value in old["arrays"].items():
        np.testing.assert_array_equal(new["arrays"][name], value, err_msg=name)
        np.testing.assert_array_equal(retry["arrays"][name], value, err_msg=name)
    assert new["summary"]["retained_charge"] == old["summary"]["retained_charge"]
    assert not new["summary"]["electrical_control"]["commands"]
    assert new["summary"]["raw_formats"] == old["summary"]["raw_formats"]


def test_enabled_exposure_changes_physical_integration_only_after_command_application(controlled):
    fixed, active = run(disable(controlled)), run(controlled)
    a, summary = active["arrays"], active["summary"]
    assert a["applied_exposure_ns"].tolist()[:3] == [2_000_000, 4_000_000, 8_000_000]
    for name in ("raw_groups", "retained_delivered_e", "row_scene_integration_weights_s"):
        np.testing.assert_array_equal(a[name][0], fixed["arrays"][name][0])
        assert not np.array_equal(a[name][1:], fixed["arrays"][name][1:])
    assert not np.array_equal(a["rgb_frames"][1:], fixed["arrays"]["rgb_frames"][1:])
    rates = a["mean_electrons"][0] / 0.002
    residual = np.zeros((8, 10))
    for i, ns in enumerate(a["applied_exposure_ns"]):
        generated = rates * (int(ns) / 1e9)
        available = np.minimum(residual + generated, 30_000)
        delivered = available * 0.5
        np.testing.assert_allclose(a["mean_electrons"][i], generated, rtol=1e-14)
        np.testing.assert_allclose(a["retained_delivered_e"][i], delivered, rtol=1e-14)
        residual = available - delivered
    for i, record in enumerate(summary["electrical_control"]["frames"]):
        raw = a["raw_groups"][i, 0].astype(float)
        black = summary["raw_formats"][0]["black_level_dn"]
        expected = np.concatenate((raw[0::2, 1::2].ravel(), raw[1::2, 0::2].ravel())).mean() - black
        assert record["statistics"]["value_dn"] == expected
        assert record["events"]["full_sensor_exposure_end_ns"] == i * 10_000_000 + int(
            a["applied_exposure_ns"][i]
        )
    assert summary["control_sequence"]["frames"][1]["applied"]["command_id"] == "electrical-ae-0"
    assert summary["prediction"]["acquisition_mode"] == "closed_loop"
    estimate = ElectricalControlRuntime().estimate_resources(controlled)
    assert sum(v.nbytes for v in a.values()) <= estimate["output_array_bytes_estimate"]
    assert set(a) == set(summary["array_units"])
    json.dumps(summary, allow_nan=False)


def test_rolling_last_row_readiness_defers_and_gap_dark_uses_previous_actual_exposure(controlled):
    control = controlled["control_sequence"]
    control["mode"].update(shutter="rolling", line_time_ns=2_000_000)
    controlled["capture_program"]["electrical_model"]["dark_current_e_per_s"] = 10_000
    out = run(controlled)
    a, trace = out["arrays"], out["summary"]["electrical_control"]
    assert trace["frames"][0]["events"]["full_sensor_exposure_end_ns"] == 16_000_000
    assert a["applied_exposure_ns"].tolist()[:3] == [2_000_000, 2_000_000, 4_000_000]
    assert trace["commands"][0]["applied_frame_index"] == 2
    expected = 10_000_000 - a["applied_exposure_ns"][:-1]
    np.testing.assert_array_equal(a["row_gap_ns"][1:], np.repeat(expected[:, None], 8, axis=1))
    np.testing.assert_allclose(
        a["retained_gap_generated_e"][1:],
        np.broadcast_to(expected[:, None, None] * 1e-5, (5, 8, 10)),
    )


def test_controller_receives_only_readonly_original_integer_raw_and_no_truth(
    controlled, monkeypatch
):
    controlled["capture_program"]["electrical_model"] = nonlinear_examples.single_model(
        controlled["readout"]
    )
    actual_statistics = electrical_control.raw_statistics
    seen = []

    def observe(raw, raw_format, selected_policy):
        assert raw.shape == (8, 10) and raw.dtype.kind in "iu" and not raw.flags.writeable
        assert raw_format["response_type"] == "nonlinear_charge_voltage"
        assert set(selected_policy) == set(policy(controlled))
        seen.append(raw.copy())
        return actual_statistics(raw, raw_format, selected_policy)

    monkeypatch.setattr(electrical_control, "raw_statistics", observe)
    output = run(controlled)
    np.testing.assert_array_equal(seen, output["arrays"]["raw_groups"][:, 0])
    assert output["summary"]["simulation_oracle"]["used_by_controller"] is False


def test_cancelled_sequence_returns_nothing_and_retry_starts_with_zero_state(controlled):
    progress = []
    with pytest.raises(InterruptedError):
        run(controlled, cancelled=lambda: bool(progress), progress=progress.append)
    assert progress == [{"stage": "capture", "completed_frames": 1, "total_frames": 6}]
    left, right = run(controlled), run(controlled)
    assert not np.any(left["arrays"]["retained_initial_residual_e"][0])
    for key in left["arrays"]:
        np.testing.assert_array_equal(left["arrays"][key], right["arrays"][key])


@pytest.mark.parametrize(
    "change",
    ["gain", "wb", "exposure", "command", "paired", "events", "grid", "overlap", "coverage"],
)
def test_preflight_rejects_uncontrolled_settings_and_entire_reachable_domain(
    controlled, change, monkeypatch
):
    frame = controlled["control_sequence"]["frames"][1]
    if change == "gain":
        frame["applied"]["gain"] = 2
    elif change == "wb":
        frame["applied"]["wb_gains_rgb"] = [1, 2, 1]
    elif change == "exposure":
        frame["applied"]["exposure_ns"] = 3_000_000
    elif change == "command":
        frame["commanded"] = {**frame["applied"], "issued_at_ns": 0}
    elif change == "paired":
        controlled["capture_program"]["electrical_model"] = dcg_electrical.default_model(
            controlled["readout"]
        )
    elif change == "events":
        controlled["retained_charge_events"] = {}
    elif change == "grid":
        policy(controlled)["exposure_grid_ns"]["minimum"] += 1
    elif change == "overlap":
        policy(controlled)["exposure_grid_ns"]["maximum"] = 12_000_000
    else:
        controlled["scene_sequence"]["knots"][-1]["time_ns"] = 53_000_000
    monkeypatch.setattr(
        TemporalSceneRuntime, "_optical_rates", lambda *a: pytest.fail("optics ran")
    )
    assert ElectricalControlRuntime().validate_request(controlled)
    with pytest.raises(RuntimeRequestError):
        run(controlled)


@pytest.mark.parametrize("clock_offset", [0, 2**50])
def test_native_four_site_pwm_integrates_actual_commanded_windows_and_history(
    pwm_request, clock_offset
):
    data = pwm_request
    data["capture_program"]["schema"] = "camera_electrical_control_capture_program_v1"
    p = electrical_control.default_policy([8, 10], initial_exposure_ns=730_000)
    p["target_black_relative_dn"] = {"minimum": 3000, "maximum": 3500}
    p["latency"] = {key: value if key == "kind" else 0 for key, value in p["latency"].items()}
    data["capture_program"]["electrical_control_policy"] = p
    for frame in data["control_sequence"]["frames"]:
        frame["applied"].update(
            exposure_ns=730_000, applied_at_ns=frame["frame_start_ns"] + 274_000
        )
    offset_clock(data, clock_offset)
    first = run(data)
    shifted = deepcopy(data)
    shifted["capture_program"]["waveform"]["phase_origin_ns"] += 390_000
    second = run(shifted)
    for request, output in ((data, first), (shifted, second)):
        actual_request = {**request, "control_sequence": output["summary"]["control_sequence"]}
        expected, _, _ = pwm_reference(actual_request)
        a = output["arrays"]
        assert np.unique(a["applied_exposure_ns"]).size > 1
        for name, key in (
            ("row_start_ns", "opens"),
            ("row_end_ns", "ends"),
            ("row_gap_ns", "gaps"),
        ):
            np.testing.assert_array_equal(a[name], expected[key])
        np.testing.assert_allclose(
            a["waveform_row_effective_s"], expected["on_ns"] * 1e-9, rtol=0, atol=1e-18
        )
        np.testing.assert_allclose(a["mean_electrons"], expected["photo"], rtol=5e-14, atol=1e-12)
        for name in (
            "initial_residual",
            "gap_generated",
            "gap_overflow",
            "exposure_generated",
            "exposure_overflow",
            "pretransfer",
            "delivered",
            "residual",
        ):
            np.testing.assert_allclose(
                a[f"retained_{name}_e"], expected[name], rtol=5e-14, atol=1e-12
            )
        np.testing.assert_array_equal(a["raw_groups"][:, 0], expected["raw"])
    assert not np.array_equal(first["arrays"]["raw_groups"], second["arrays"]["raw_groups"])
    assert not np.array_equal(first["arrays"]["mean_electrons"], second["arrays"]["mean_electrons"])


@pytest.mark.parametrize("offset", [0, 2**50])
@pytest.mark.parametrize("edge", [-1, 0, 1])
def test_command_readiness_uses_exact_integer_latch_boundary(controlled, offset, edge):
    policy(controlled)["latency"]["actuator_ns"] = 8_000_000 + edge
    for frame in controlled["control_sequence"]["frames"]:
        frame["frame_start_ns"] += offset
        frame["applied"]["applied_at_ns"] += offset
    for knot in controlled["scene_sequence"]["knots"]:
        knot["time_ns"] += offset
    output = run(controlled)
    first = output["summary"]["electrical_control"]["commands"][0]
    assert first["actuator_ready_ns"] == offset + 10_000_000 + edge
    assert first["applied_frame_index"] == (1 if edge <= 0 else 2)
    assert output["arrays"]["applied_exposure_ns"][1] == (4_000_000 if edge <= 0 else 2_000_000)


def test_bright_to_dark_retains_observed_signal_without_current_exposure_inverse(controlled):
    timeline = controlled["scene_sequence"]
    timeline["interpolation"] = "zero_order_hold"
    bright = deepcopy(timeline["knots"][0]["scene"])
    dark = deepcopy(bright)
    dark["spectral_irradiance"] = np.zeros_like(bright["spectral_irradiance"])
    timeline["knots"] = [
        {"time_ns": 0, "scene": bright},
        {"time_ns": 2_000_000, "scene": dark},
        {"time_ns": 100_000_000, "scene": dark},
    ]
    half = run(controlled)
    a = half["arrays"]
    assert not np.any(a["mean_electrons"][1:])
    assert np.all(a["retained_delivered_e"][1:] > 0)
    for i in range(1, 6):
        np.testing.assert_allclose(
            a["retained_delivered_e"][i], a["retained_delivered_e"][i - 1] * 0.5
        )
    assert half["summary"]["electrical_control"]["frames"][1]["statistics"]["value_dn"] > 0
    controlled["capture_program"]["retained_charge_model"]["transfer_efficiency"] = [1] * 4
    full = run(controlled)
    assert not np.any(full["arrays"]["retained_delivered_e"][1:])
    assert full["summary"]["electrical_control"]["frames"][1]["statistics"]["value_dn"] == 0


def test_minimum_reachable_exposure_checks_largest_dark_gap_before_queue(controlled, monkeypatch):
    for i, frame in enumerate(controlled["control_sequence"]["frames"]):
        frame["frame_start_ns"] = i * 15_000_000_000
        frame["applied"].update(exposure_ns=6_000_000_000, applied_at_ns=frame["frame_start_ns"])
    controlled["scene_sequence"]["knots"][-1]["time_ns"] = 100_000_000_000
    policy(controlled)["exposure_grid_ns"] = {
        "minimum": 1_000_000_000,
        "maximum": 6_000_000_000,
        "step": 1_000_000_000,
    }
    controlled["capture_program"]["electrical_model"]["dark_current_e_per_s"] = 1e12
    assert not ElectricalControlRuntime().validate_request(disable(controlled))
    monkeypatch.setattr(
        TemporalSceneRuntime, "_optical_rates", lambda *a: pytest.fail("optics ran")
    )
    assert ElectricalControlRuntime().validate_request(controlled)
    with pytest.raises(RuntimeRequestError, match="Reachable electrical exposure/gap dark"):
        run(controlled)


def test_commands_not_ready_by_end_are_preserved_without_affecting_raw(controlled):
    policy(controlled)["latency"]["command_transport_ns"] = 100_000_000
    active, fixed = run(controlled), run(disable(controlled))
    np.testing.assert_array_equal(active["arrays"]["raw_groups"], fixed["arrays"]["raw_groups"])
    commands = active["summary"]["electrical_control"]["commands"]
    assert len(commands) == 6
    assert all(c["status"] == "pending_after_sequence" for c in commands)
    assert all("command_not_arrived_by_final_latch" in c["unapplied_reasons"] for c in commands)


@pytest.mark.parametrize(
    ("values", "count", "terminal", "span", "above", "below"),
    [([2, 4, 3, 8], 2, 0, 0, 4, 1), ([8, 2, 3, 4], 2, 2, 7, 4, 1), ([8, 2, 1, 3], 1, 1, 0, 4, 2)],
)
def test_target_outcome_reports_only_observed_code_hold_not_settling(
    values, count, terminal, span, above, below
):
    frames = [
        {"statistics": {"value_dn": value}, "events": {"statistics_ready_ns": time}}
        for value, time in zip(values, [0, 5, 9, 16], strict=True)
    ]
    outcome = _target_outcome(frames, {"target_black_relative_dn": {"minimum": 3, "maximum": 4}})
    assert outcome["in_target_frame_count"] == count
    assert outcome["terminal_consecutive_in_target_frames"] == terminal
    assert outcome["terminal_in_target_statistics_span_ns"] == span
    assert outcome["peak_above_upper_dn"] == above
    assert outcome["peak_below_lower_dn"] == below
    assert outcome["scope"] == "observed_sequence_only"
    assert outcome["settling"] == "undefined_no_declared_step_window"
    assert outcome["physical_stability"] == outcome["lfm_performance"] == "not_established"
