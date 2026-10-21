"""Independent code-domain meter, integer actuator and causal queue counterexamples."""

from copy import deepcopy
from decimal import localcontext
from fractions import Fraction

import numpy as np
import pytest

from camera_platform import electrical_control as control
from camera_platform.timing import MAX_EXPOSURE_NS, MAX_TIME_NS

FORMAT = {
    "cfa": "RGGB",
    "bit_depth": 12,
    "black_level_dn": 16.0,
    "response_type": "linear",
    "packing": "unpacked",
    "layout": "bayer",
    "hdr_streams": 1,
}
GLOBAL = {
    "shutter": "global",
    "line_time_ns": 0,
    "hidden_top_rows": 0,
    "readout_basis": "full_sensor_rows",
}


def policy(shape=(4, 4)):
    result = control.default_policy(shape, FORMAT)
    result.update(
        exposure_grid_ns={"minimum": 100, "maximum": 1000, "step": 100},
        target_black_relative_dn={"minimum": 90, "maximum": 110},
    )
    return result


def observe(value, config=None, fmt=None):
    config, fmt = config or policy(), fmt or FORMAT
    shape = tuple(config["statistics_roi"]["shape"])
    return control.raw_statistics(
        np.full(shape, value + fmt["black_level_dn"], dtype=np.uint16), fmt, config
    )


def frame(index, start):
    return {"frame_id": f"frame-{index}", "frame_start_ns": start}


def command(index=0, start=0, config=None, value=20, exposure=100):
    config = config or policy()
    source = frame(index, start)
    proposal = control.propose_ae(observe(value, config), exposure, config)
    return control.make_command(
        proposal,
        source,
        index,
        control.events(source, exposure, GLOBAL, 4, config["latency"]),
        config,
    )


def apply(commands, index, at, *, exposure=100, prior=-1):
    return control.apply_pending(
        commands,
        frame_index=index,
        frame=frame(index, at),
        mode=GLOBAL,
        exposure_ns=exposure,
        last_applied_source_index=prior,
    )


def test_native_site_meter_preserves_signed_codes_and_odd_roi_phase():
    raw = np.tile(np.array([[10, 20], [30, 40]], dtype=np.uint16), (3, 3))
    config = policy((2, 4))
    config["statistics_roi"]["origin"] = [1, 1]
    config["meter"]["sites"] = ["R", "Gb"]
    stats = control.raw_statistics(raw, FORMAT, config)
    selected = [
        int(raw[r, c])
        for r in range(1, 3)
        for c in range(1, 5)
        if (r % 2, c % 2) in {(0, 0), (1, 0)}
    ]
    assert stats["value_dn"] == sum(selected) / len(selected) - 16 == 4
    assert stats["native_site_mean_dn"] == {"R": -6, "Gr": 4, "Gb": 14, "B": 24}
    assert stats["sum_codes_dn"] == sum(selected)
    assert stats["sample_count"] == len(selected)
    assert stats["excluded_sites"] == ["Gr", "B"]
    assert stats["charge_or_irradiance_inversion"] is False
    assert stats["dark_current_correction"] == "none"


def test_rails_are_included_not_rejected_or_interpreted_as_pd_saturation():
    raw = np.array([[16, 0], [4095, 16]], dtype=np.uint16)
    stats = control.raw_statistics(raw, FORMAT, policy((2, 2)))
    assert stats["value_dn"] == (4095 - 32) / 2
    assert stats["digital_low_rail_fraction"] == stats["digital_high_rail_fraction"] == 0.5
    assert not any("full_well" in key or "photocharge" in key for key in stats)


def test_uint32_meter_sum_does_not_wrap_and_format_is_not_inverted():
    fmt = {**FORMAT, "bit_depth": 32, "response_type": "nonlinear_charge_voltage"}
    raw = np.full((512, 512), 2**32 - 1, dtype=np.uint32)
    config = policy((512, 512))
    stats = control.raw_statistics(raw, fmt, config)
    assert stats["sum_codes_dn"] == (512 * 512 // 2) * (2**32 - 1)
    assert stats["value_dn"] == 2**32 - 17
    assert stats["raw_format"] == fmt


def test_fractional_black_cancellation_uses_same_explicit_precision_for_observation_and_policy():
    fmt = {**FORMAT, "black_level_dn": 2 / 3}
    config = policy((2, 6))
    config["meter"]["sites"] = ["R"]
    raw = np.zeros((2, 6), dtype=np.uint16)
    raw[0, ::2] = [1, 1, 0]
    with localcontext() as context:
        context.prec = 8
        stats = control.raw_statistics(raw, fmt, config)
    with localcontext() as context:
        context.prec = 12
        result = control.propose_ae(stats, 100, config)
    expected = float(Fraction(2, 3) - Fraction(str(2 / 3)))
    assert stats["value_dn"] == result["observed_dn"] == expected
    assert stats["native_site_mean_dn"]["R"] == expected
    assert result["commanded"] == {"exposure_ns": 200}


@pytest.mark.parametrize("value", [90, 100, 110])
def test_target_interval_is_inclusive_and_holds(value):
    result = control.propose_ae(observe(value), 300, policy())
    assert result["status"] == "within_target" and result["commanded"] is None


@pytest.mark.parametrize("value", [-16, -1, 0])
def test_negative_black_relative_meter_probes_bounded_exposure_without_division(value):
    result = control.propose_ae(observe(value), 200, policy())
    assert result["status"] == "probe_dark"
    assert result["requested_ratio"] is None
    assert result["commanded"] == {"exposure_ns": 400}


def test_exact_half_grid_rounds_up_and_bound_is_integer():
    config = policy()
    config["target_black_relative_dn"] = {"minimum": 250, "maximum": 250}
    result = control.propose_ae(observe(200, config), 200, config)
    assert Fraction(250, 200) * 200 == Fraction(250)
    assert result["requested_exposure_ns"] == 250
    assert result["commanded"] == {"exposure_ns": 300}
    assert type(result["commanded"]["exposure_ns"]) is int


def test_quantized_command_cannot_exceed_the_declared_step_factor():
    config = policy()
    config["max_step_factor"] = 1.3
    config["target_black_relative_dn"] = {"minimum": 130, "maximum": 130}
    result = control.propose_ae(observe(100, config), 200, config)
    assert result["requested_exposure_ns"] == 260
    assert result["status"] == "actuator_grid_hold" and result["commanded"] is None
    assert result["step_limited"]


def test_exposure_bounds_and_disabled_hold_are_explicit():
    config = policy()
    bright = control.propose_ae(observe(1000), 100, config)
    dark = control.propose_ae(observe(0), 1000, config)
    assert bright["commanded"] is dark["commanded"] is None
    assert bright["clamped"] and dark["clamped"]
    config["enabled"] = False
    assert control.propose_ae(observe(0, config), 100, config)["status"] == "disabled"


def test_same_raw_has_same_code_error_for_different_applied_exposures_and_nonlinearity():
    config = policy()
    first = control.propose_ae(observe(20), 100, config)
    second = control.propose_ae(observe(20), 300, config)
    nonlinear = {**FORMAT, "response_type": "nonlinear_charge_voltage"}
    third = control.propose_ae(observe(20, fmt=nonlinear), 100, config)
    assert first["observed_dn"] == second["observed_dn"] == 20
    assert first["target_error_dn"] == second["target_error_dn"] == -80
    assert first["commanded"] == {"exposure_ns": 200}
    assert second["commanded"] == {"exposure_ns": 600}
    assert first == third
    assert set(first["commanded"]) == {"exposure_ns"}


@pytest.mark.parametrize("initial", [1, 13, 999_999, MAX_EXPOSURE_NS])
def test_default_initial_grid_is_bounded_and_contains_initial(initial):
    config = control.default_policy(shape=[4, 4], initial_exposure_ns=initial)
    grid = config["exposure_grid_ns"]
    assert 1 <= grid["minimum"] <= initial <= grid["maximum"] <= MAX_EXPOSURE_NS
    assert (initial - grid["minimum"]) % grid["step"] == 0


@pytest.mark.parametrize(
    "raw",
    [
        np.zeros((4, 4), dtype=float),
        np.zeros((4, 4), dtype=bool),
        np.zeros((2, 4, 4), dtype=np.uint16),
        np.full((4, 4), -1, dtype=np.int16),
        np.full((4, 4), 4096, dtype=np.uint16),
        np.zeros((3, 4), dtype=np.uint16),
    ],
)
def test_non_integer_paired_out_of_range_or_non_native_frames_refused(raw):
    with pytest.raises(ValueError):
        control.raw_statistics(raw, FORMAT, policy())


@pytest.mark.parametrize(
    "key,value",
    [
        ("hdr_streams", 2),
        ("hdr_streams", True),
        ("cfa", "BGGR"),
        ("black_level_dn", 10**1000),
        ("response_type", "inverted_charge"),
    ],
)
def test_unsupported_or_malformed_readout_metadata_refused(key, value):
    with pytest.raises(ValueError):
        control.raw_statistics(np.zeros((4, 4), dtype=np.uint16), {**FORMAT, key: value}, policy())


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": 1},
        {"minimum_delay_frames": 0},
        {"maximum_delay_frames": 0},
        {"max_step_factor": float("nan")},
        {"max_step_factor": True},
        {"exposure_grid_ns": {"minimum": 1, "maximum": 10**1000, "step": 1}},
        {"exposure_grid_ns": {"minimum": 1, "maximum": 100, "step": 2}},
        {"target_black_relative_dn": {"minimum": 1, "maximum": 4095}},
        {"meter": {"algorithm": "native_site_mean_black_relative_dn_v1", "sites": ["Gr", "Gr"]}},
    ],
)
def test_strict_policy_refuses_aliases_unbounded_targets_and_bad_grids(change):
    with pytest.raises(ValueError):
        control.validate_policy({**policy(), **change}, frame_shape=(4, 4), raw_format=FORMAT)


@pytest.mark.parametrize(
    "change",
    [
        {"latent_truth": 40},
        {"value_dn": 30},
        {"sample_count": 16},
        {"charge_or_irradiance_inversion": True},
    ],
)
def test_policy_rejects_augmented_or_inconsistent_observation(change):
    with pytest.raises(ValueError):
        control.propose_ae({**observe(20), **change}, 100, policy())


def test_exact_large_clock_rolling_full_sensor_event_chain():
    mode = {**GLOBAL, "shutter": "rolling", "line_time_ns": 13, "hidden_top_rows": 2}
    source = frame(0, MAX_TIME_NS - 1000)
    latency = policy()["latency"]
    for value, (key, _) in enumerate(control.LATENCY_EVENTS, 1):
        latency[key] = value
    result = control.events(source, 100, mode, 4, latency)
    assert control.latch(source, mode) == MAX_TIME_NS - 974
    expected = MAX_TIME_NS - 1000 + 2 * 13 + 3 * 13 + 100
    assert result["full_sensor_exposure_end_ns"] == expected
    for delay, (_, event) in enumerate(control.LATENCY_EVENTS, 1):
        expected += delay
        assert result[event] == expected and type(result[event]) is int


@pytest.mark.parametrize(
    "source,exposure,latency",
    [
        (frame(0, MAX_TIME_NS), 1, {}),
        (frame(0, 0), True, {}),
        (frame(0, -MAX_TIME_NS), 1, {"frame_transport_ns": MAX_TIME_NS}),
        (frame(0, 0), 1, {"controller_ns": True}),
    ],
)
def test_clock_overflow_span_and_boolean_aliases_refused(source, exposure, latency):
    with pytest.raises(ValueError):
        control.events(source, exposure, GLOBAL, 4, {**policy()["latency"], **latency})


def test_no_same_frame_feedback_and_exact_readiness_latch_boundary():
    queue = [command()]
    assert apply(queue, 0, 100)["exposure_ns"] == 100
    assert apply(queue, 1, 99)["exposure_ns"] == 100
    result = apply(queue, 1, 100)
    assert result["exposure_ns"] == 200
    assert result["commands"][0]["status"] == "applied"
    assert result["commands"][0]["source_to_applied_frames"] == 1
    assert queue[0]["status"] == "pending"


def test_frame_delay_is_independent_of_zero_wall_clock_latency():
    config = policy()
    config["minimum_delay_frames"] = 3
    queue = [command(config=config)]
    assert apply(queue, 2, 1000)["commands"][0]["status"] == "pending"
    assert apply(queue, 3, 1000)["commands"][0]["status"] == "applied"


def test_latest_eligible_command_supersedes_older_without_mutating_callers():
    queue = [command(0, 0), command(1, 100, exposure=200)]
    saved = deepcopy(queue)
    result = apply(queue, 2, 300)
    assert [c["status"] for c in result["commands"]] == ["superseded", "applied"]
    assert result["exposure_ns"] == 400 and result["last_applied_source_index"] == 1
    assert queue == saved
    result["commands"][1]["proposal"]["commanded"]["exposure_ns"] = 900
    assert queue == saved


def test_late_older_source_never_replaces_a_newer_applied_source():
    delayed = policy()
    delayed["latency"]["command_transport_ns"] = 300
    queue = [command(0, 0, config=delayed), command(1, 100, exposure=200)]
    first = apply(queue, 2, 300)
    assert first["exposure_ns"] == 400
    second = apply(
        first["commands"],
        3,
        400,
        exposure=first["exposure_ns"],
        prior=first["last_applied_source_index"],
    )
    assert second["commands"][0]["status"] == "stale"
    assert second["exposure_ns"] == 400 and second["applied_command_id"] is None


def test_expiration_is_inclusive_at_limit_and_never_resolved_before_arrival():
    config = policy()
    config["maximum_delay_frames"] = 1
    config["latency"]["command_transport_ns"] = 300
    queue = [command(config=config)]
    early = apply(queue, 2, 200)
    assert early["commands"][0]["status"] == "pending"
    expired = apply(early["commands"], 3, 400)
    assert expired["commands"][0]["status"] == "expired"
    assert expired["exposure_ns"] == 100
    assert expired["commands"][0]["resolved_at_ns"] >= queue[0]["command_arrival_ns"]
    config["latency"]["command_transport_ns"] = 0
    assert apply([command(config=config)], 1, 100)["commands"][0]["status"] == "applied"


def test_final_commands_preserve_independent_delay_arrival_and_actuator_reasons():
    config = policy()
    config["latency"]["command_transport_ns"] = 1000
    queue = [command(15, 1500, config=config)]
    result = control.finalize_pending(queue, last_frame_index=15, last_latch_ns=1500)
    assert result[0]["unapplied_reasons"] == [
        "minimum_frame_delay_after_sequence",
        "command_not_arrived_by_final_latch",
    ]
    assert queue[0]["status"] == "pending"
    config["latency"].update(command_transport_ns=0, actuator_ns=300)
    result = control.finalize_pending(
        [command(config=config)], last_frame_index=2, last_latch_ns=300
    )
    assert result[0]["unapplied_reasons"] == ["actuator_not_ready_by_final_latch"]


def test_scheduler_refuses_backward_time_boolean_ready_and_gain_commands():
    queue = [command()]
    applied = apply(queue, 1, 100)["commands"]
    with pytest.raises(ValueError, match="backwards"):
        apply(applied, 2, 99)
    with pytest.raises(ValueError, match="precede"):
        control.finalize_pending(applied, last_frame_index=2, last_latch_ns=99)
    queue[0]["actuator_ready_ns"] = True
    with pytest.raises(ValueError):
        apply(queue, 1, 100)
    queue = [command()]
    queue[0]["proposal"]["commanded"]["gain"] = 2
    with pytest.raises(ValueError, match="exactly"):
        apply(queue, 1, 100)


def test_command_cannot_forge_statistics_controller_transport_chain():
    config = policy()
    source = frame(0, 0)
    times = control.events(source, 100, GLOBAL, 4, config["latency"])
    times["statistics_ready_ns"] = 99
    with pytest.raises(ValueError, match="causal"):
        control.make_command(control.propose_ae(observe(20), 100, config), source, 0, times, config)
