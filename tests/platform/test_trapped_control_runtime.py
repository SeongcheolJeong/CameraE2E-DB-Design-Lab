"""Causal exposure changes over an actual two-reservoir chronological capture."""

from copy import deepcopy

import numpy as np
import pytest
from test_electrical_control_runtime import controlled as controlled
from test_hdr_runtime import request_data as request_data

from camera_platform import dcg_electrical, electrical_control, nonlinear_examples
from camera_platform import trapped_charge_runtime as trap_runtime
from camera_platform import trapped_control_runtime as control_traps
from camera_platform.electrical_control_runtime import (
    TRAPPED_PROGRAM_SCHEMA,
    ElectricalControlRuntime,
    _parse,
    default_trapped_capture_program,
    observation_path,
    retained_program,
    run,
    validate_capture_program,
)
from camera_platform.electrical_isp_source import _replay_controller
from camera_platform.retained_charge_runtime import RetainedChargeRuntime, event_schedule
from camera_platform.runtime import RuntimeRequestError
from camera_platform.temporal import TemporalSceneRuntime


@pytest.fixture
def trapped(controlled):
    data = deepcopy(controlled)
    original = data["capture_program"]
    program = default_trapped_capture_program(
        data["readout"],
        [f["frame_id"] for f in data["control_sequence"]["frames"]],
        original["electrical_control_policy"],
    )
    program["electrical_model"] = original["electrical_model"]
    data["capture_program"] = program
    return data


def select(data, family, path):
    program = data["capture_program"]
    if family == "nonlinear":
        program["electrical_model"] = nonlinear_examples.single_model(data["readout"])
    elif family == "shared":
        program["electrical_model"] = dcg_electrical.default_model(data["readout"])
    elif family == "shared_nonlinear":
        program["electrical_model"] = nonlinear_examples.shared_model(data["readout"])
    program["observation_readout_id"] = path
    return validate_capture_program(program)


FAMILIES = [
    ("linear", "single"),
    ("nonlinear", "single"),
    ("shared", "HCG"),
    ("shared", "LCG"),
    ("shared_nonlinear", "HCG"),
    ("shared_nonlinear", "LCG"),
]


def test_whole_pwm_period_policy_keeps_actual_trap_capture_on_lattice(trapped):
    data = deepcopy(trapped)
    control = data["control_sequence"]
    for i, frame in enumerate(control["frames"]):
        frame["frame_start_ns"] = i * 20_000_000
        frame["applied"].update(exposure_ns=8_000_000, applied_at_ns=i * 20_000_000)
    data["scene_sequence"]["knots"][-1]["time_ns"] = 120_000_000
    program = data["capture_program"]
    policy = program["electrical_control_policy"]
    policy.update(
        schema=electrical_control.PERIOD_POLICY_SCHEMA,
        exposure_grid_ns={"minimum": 2_000_000, "maximum": 12_000_000, "step": 2_000_000},
        target_black_relative_dn={"minimum": 1, "maximum": 2},
        period_lattice={"kind": "whole_pwm_period_v1", "period_ns": 8_000_000},
    )
    program["waveform"] = {
        "schema": "camera_light_waveform_v1",
        "kind": "pwm",
        "clock_id": control["clock"]["clock_id"],
        "period_ns": 8_000_000,
        "on_time_ns": 4_000_000,
        "phase_origin_ns": 0,
        "low": 0.0,
        "high": 2.0,
    }
    data["capture_program"] = validate_capture_program(program)
    result = run(data)
    actual = result["arrays"]["applied_exposure_ns"]
    np.testing.assert_array_equal(actual, np.full(len(actual), 8_000_000))
    trace = result["summary"]["electrical_control"]
    assert all(
        frame["proposal"]["status"] == "period_lattice_hold_outside_target"
        for frame in trace["frames"]
    )
    assert trace["target_outcome"]["in_target_frame_count"] == 0
    assert trace["target_outcome"]["period_lattice"]["point_count"] == 1
    assert trace["target_outcome"]["period_lattice"]["target_feasibility"] == (
        "not_established_by_finite_raw_sequence"
    )
    diagnosis = trace["target_outcome"]["exposure_space_diagnosis"]
    assert diagnosis["status"] == "only_permitted_exposure_missed_in_observed_frames"
    assert diagnosis["structural_exposure_only_alternative"] is False
    assert diagnosis["simulation_truth_used"] is False
    assert trace["source_truth_used"] is False
    assert result["summary"]["trap_control_work_envelope"]["grid_point_count"] == 1
    replayed, receipt = _replay_controller(
        result["arrays"]["raw_groups"][:, 0],
        result["summary"]["raw_formats"][0],
        control,
        program,
        result["summary"]["geometry"],
    )
    assert receipt == trace
    assert replayed == result["summary"]["control_sequence"]
    fixed = deepcopy(data)
    fixed["capture_program"]["electrical_control_policy"]["enabled"] = False
    other = run(fixed)
    for key in result["arrays"]:
        np.testing.assert_array_equal(result["arrays"][key], other["arrays"][key], err_msg=key)


def test_whole_pwm_period_policy_rejects_wrong_waveform(trapped):
    program = deepcopy(trapped["capture_program"])
    program["electrical_control_policy"].update(
        schema=electrical_control.PERIOD_POLICY_SCHEMA,
        period_lattice={"kind": "whole_pwm_period_v1", "period_ns": 8_000_000},
    )
    with pytest.raises(ValueError, match="matching nonconstant PWM"):
        validate_capture_program(program)


@pytest.mark.parametrize("family,path", FAMILIES)
@pytest.mark.parametrize("noise", [False, True])
def test_disabled_trap_ae_exact_fixed_runtime_arrays_state_rng_and_timing(
    trapped, family, path, noise
):
    trapped["capture_program"] = select(trapped, family, path)
    trapped["capture_program"]["electrical_control_policy"]["enabled"] = False
    trapped["noise_enabled"] = noise
    old = RetainedChargeRuntime().execute(
        {**trapped, "capture_program": retained_program(trapped["capture_program"])}
    )
    result, retry = run(trapped), run(trapped)
    assert set(result["arrays"]) == set(old["arrays"]) | {"applied_exposure_ns"}
    for key in old["arrays"]:
        np.testing.assert_array_equal(result["arrays"][key], old["arrays"][key], err_msg=key)
        np.testing.assert_array_equal(retry["arrays"][key], result["arrays"][key], err_msg=key)
    assert result["summary"]["retained_charge"] == old["summary"]["retained_charge"]
    assert result["summary"]["schema_version"] == "camera_electrical_control_result_v2"
    assert (
        result["summary"]["retained_charge"]["events"]["schema"] == "camera_retained_row_events_v2"
    )
    assert not result["summary"]["electrical_control"]["commands"]
    assert result["summary"]["electrical_control"]["readout_id"] == path
    assert "observation_readout_id" in result["summary"]["electrical_control"]["fixed_controls"]


@pytest.mark.parametrize("family,path", FAMILIES)
def test_active_raw_feedback_realized_gaps_and_trap_state_equal_actual_fixed_acquisition(
    trapped, family, path
):
    trapped["capture_program"] = select(trapped, family, path)
    program = trapped["capture_program"]
    program["electrical_control_policy"]["target_black_relative_dn"] = {
        "minimum": 3000,
        "maximum": 3500,
    }
    program["waveform"] = {
        "schema": "camera_light_waveform_v1",
        "kind": "pwm",
        "clock_id": trapped["control_sequence"]["clock"]["clock_id"],
        "period_ns": 700_003,
        "on_time_ns": 300_001,
        "phase_origin_ns": -234_567,
        "low": 0.2,
        "high": 1.0,
    }
    trapped["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=173_003)
    result = run(trapped)
    a, s = result["arrays"], result["summary"]
    assert np.unique(a["applied_exposure_ns"]).size > 1
    expected_gap = 10_000_000 - a["applied_exposure_ns"][:-1]
    np.testing.assert_array_equal(a["row_gap_ns"][1:], np.repeat(expected_gap[:, None], 8, axis=1))
    assert np.any(a["retained_trapped_e"] > 0) and np.any(a["retained_released_e"] > 0)
    np.testing.assert_array_equal(a["retained_initial_trapped_e"][1:], a["retained_trapped_e"][:-1])
    fixed = RetainedChargeRuntime().execute(
        {
            **trapped,
            "capture_program": retained_program(program),
            "control_sequence": s["control_sequence"],
        }
    )
    for key in fixed["arrays"]:
        np.testing.assert_array_equal(a[key], fixed["arrays"][key], err_msg=key)
    assert s["retained_charge"] == fixed["summary"]["retained_charge"]
    index, _, fmt = observation_path(program)
    for codes, frame in zip(a["raw_groups"], s["electrical_control"]["frames"], strict=True):
        assert frame["statistics"] == electrical_control.raw_statistics(
            codes[index], fmt, program["electrical_control_policy"]
        )
    estimate = ElectricalControlRuntime().estimate_resources(trapped)
    assert sum(x.nbytes for x in a.values()) <= estimate["output_array_bytes_estimate"]
    assert set(s["array_units"]) == set(a)
    assert s["simulation_oracle"]["used_by_controller"] is False
    actual_resources = s["retained_charge"]["trap_integration"]["resources"]
    for key, value in actual_resources.items():
        assert value <= s["trap_control_work_envelope"]["resources"][key]


def test_full_clear_preserves_traps_while_only_readonly_selected_integer_raw_reaches_policy(
    trapped, monkeypatch
):
    trapped["capture_program"]["pd_clear_before_exposure"][2]["clear"] = True
    original = electrical_control.raw_statistics
    seen = []

    def meter(raw, fmt, policy):
        assert raw.ndim == 2 and raw.dtype.kind in "ui" and not raw.flags.writeable
        seen.append(raw.copy())
        return original(raw, fmt, policy)

    monkeypatch.setattr(electrical_control, "raw_statistics", meter)
    result = run(trapped)
    a = result["arrays"]
    np.testing.assert_array_equal(seen, a["raw_groups"][:, 0])
    assert np.all(a["retained_clear_loss_e"][2] > 0)
    assert np.all(a["retained_initial_trapped_e"][2] > 0)
    assert np.all(a["retained_trapped_e"][2] > 0)


def test_late_ready_command_does_not_change_prior_generation_and_cancellation_retries_empty(
    trapped,
):
    latency = trapped["capture_program"]["electrical_control_policy"]["latency"]
    latency["statistics_ns"] = 15_000_000
    result = run(trapped)
    np.testing.assert_array_equal(result["arrays"]["applied_exposure_ns"][:2], 2_000_000)
    assert result["summary"]["electrical_control"]["commands"][0]["applied_frame_index"] == 2
    progress = []
    with pytest.raises(InterruptedError):
        run(trapped, cancelled=lambda: bool(progress), progress=progress.append)
    retry = run(trapped)
    np.testing.assert_array_equal(retry["arrays"]["retained_initial_trapped_e"][0], 0)
    for key in result["arrays"]:
        np.testing.assert_array_equal(retry["arrays"][key], result["arrays"][key])


def test_unseen_grid_work_is_rejected_before_optics_or_sequence_rng(trapped, monkeypatch):
    # The uniform initial2ms schedule has400 intervals and is admissible; a
    # permissible predecessor/current pair can need716, which must refuse early.
    trapped["capture_program"]["retained_charge_model"]["integration"]["max_step_ns"] = 25_000
    fixed = {**trapped, "capture_program": retained_program(trapped["capture_program"])}
    RetainedChargeRuntime().estimate_resources(fixed)

    def forbidden(*args, **kwargs):
        pytest.fail("unadmitted work reached optics or sequence RNG")

    monkeypatch.setattr(TemporalSceneRuntime, "_optical_rates", forbidden)
    seed_sequence = np.random.SeedSequence
    fixed_seed = trapped["capture_program"]["electrical_model"]["fixed_pattern"]["seed"]

    def seed_guard(seed, *args, **kwargs):
        # Authoring derives repeatable FPN maps in preflight; these are separate
        # from the capture charge/readout noise streams and remain permitted.
        if seed != fixed_seed:
            forbidden()
        return seed_sequence(seed, *args, **kwargs)

    monkeypatch.setattr(np.random, "SeedSequence", seed_guard)
    with pytest.raises(RuntimeRequestError, match="Reachable trap control.*segments"):
        run(trapped)


def test_interval_bound_includes_every_small_grid_previous_current_pair(trapped):
    p = trapped["capture_program"]["electrical_control_policy"]
    p["exposure_grid_ns"] = {"minimum": 1_000_000, "maximum": 4_000_000, "step": 1_000_000}
    trapped["capture_program"]["waveform"] = {
        "schema": "camera_light_waveform_v1",
        "kind": "pwm",
        "clock_id": trapped["control_sequence"]["clock"]["clock_id"],
        "period_ns": 713_009,
        "on_time_ns": 230_011,
        "phase_origin_ns": 113_003,
        "low": 0,
        "high": 1,
    }
    parsed = _parse(trapped)
    # Exhaust every previous/current grid combination, including interior grid
    # points and partial last intervals. No policy-output sampling is substituted.
    for previous in range(1_000_000, 4_000_001, 1_000_000):
        for current in range(1_000_000, 4_000_001, 1_000_000):
            control = deepcopy(parsed["control"])
            control["frames"][0]["applied"]["exposure_ns"] = previous
            control["frames"][1]["applied"]["exposure_ns"] = current
            event = event_schedule(
                control,
                retained_program(parsed["program"]),
                parsed["base"]["geometry"],
                parsed["base"]["size"],
            )["frames"][1]
            actual = control_traps.actual_frame_plan(parsed, event, 1)
            assert (
                actual["frames"][0]["segment_count"]
                <= parsed["trap_work_envelope"]["frames"][1]["segment_count_bound"]
            )


@pytest.mark.parametrize(
    "bad", ["missing", "paired_selector", "single_selector", "retained_model", "event_contract"]
)
def test_v3_requires_model_events_and_fixed_selector_to_match_family(trapped, bad):
    program = trapped["capture_program"]
    if bad == "missing":
        program.pop("observation_readout_id")
    elif bad == "paired_selector":
        program["observation_readout_id"] = "LCG"
    elif bad == "single_selector":
        program["electrical_model"] = dcg_electrical.default_model(trapped["readout"])
    elif bad == "retained_model":
        old = program["retained_charge_model"]
        program["retained_charge_model"] = {
            k: old[k] for k in ("capacity_e", "transfer_efficiency")
        }
        program["retained_charge_model"]["schema"] = "camera_retained_photodiode_v1"
    else:
        program["event_contract"]["initial_state"] = "zero_at_each_row_first_exposure_start"
    with pytest.raises(ValueError):
        validate_capture_program(program)


def test_capabilities_expose_real_trap_feedback_and_plan_matches_full_schedule(trapped):
    capabilities = ElectricalControlRuntime().describe_capabilities()
    assert "causal_trap_bearing_exposure_feedback" in capabilities["supports"]
    assert "charge_trap_capture_release" not in capabilities["unsupported"]
    parsed = _parse(trapped)
    plans = [
        control_traps.actual_frame_plan(parsed, event, i)
        for i, event in enumerate(parsed["events"]["frames"])
    ]
    combined = control_traps.complete_schedule(parsed, plans)
    full = trap_runtime.plan(parsed)
    assert combined == full
    assert trapped["capture_program"]["schema"] == TRAPPED_PROGRAM_SCHEMA


def test_combined_reachable_photo_and_gap_dark_refused_before_sequence_rng(trapped, monkeypatch):
    # Each maximum exposure alone is below1e13e, but adding the possible gap
    # crosses the core's per-pixel frame bound. Do not discover that after an
    # earlier frame has sampled its state.
    trapped["capture_program"]["electrical_model"]["dark_current_e_per_s"] = 6e11
    original = TemporalSceneRuntime._optical_rates

    def bright_rates(parsed):
        rates, *rest = original(parsed)
        rates = np.full_like(rates, 1.24875e15)
        return (rates, *rest)

    monkeypatch.setattr(TemporalSceneRuntime, "_optical_rates", bright_rates)
    seed_sequence = np.random.SeedSequence
    fixed_seed = trapped["capture_program"]["electrical_model"]["fixed_pattern"]["seed"]

    def seed_guard(seed, *args, **kwargs):
        if seed != fixed_seed:
            pytest.fail("invalid combined reachable mean advanced capture RNG")
        return seed_sequence(seed, *args, **kwargs)

    monkeypatch.setattr(np.random, "SeedSequence", seed_guard)
    assert 1.24875e15 * 0.008 + 6e11 * 0.008 < 1e13
    assert 1.24875e15 * 0.008 + 6e11 * (0.008 + 0.0099) > 1e13
    with pytest.raises(RuntimeRequestError, match="Reachable optical generation"):
        run(trapped)
