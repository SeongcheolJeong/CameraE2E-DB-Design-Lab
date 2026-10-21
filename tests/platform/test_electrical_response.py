"""Independent finite-sample response/causality oracles, with no simulated truth."""

import json
from copy import deepcopy
from decimal import localcontext
from fractions import Fraction

import numpy as np
import pytest

from camera_platform import electrical_response as response
from camera_platform.timing import MAX_TIME_NS

FORMAT = {
    "cfa": "RGGB",
    "bit_depth": 12,
    "black_level_dn": 16.0,
    "response_type": "linear",
    "packing": "unpacked",
    "layout": "bayer",
    "hdr_streams": 1,
}


def specification(count=4, shape=(4, 4)):
    return {
        "schema": response.SCHEMA,
        "roi": {"reference": "native_sensor", "origin": [0, 0], "shape": list(shape)},
        "sites": ["Gr", "Gb"],
        "target_black_relative_dn": {"minimum": 90, "maximum": 110},
        "clock": {
            "clock_id": "experiment",
            "unit": "ns",
            "reference": "relative",
            "origin": "authored start",
        },
        "window": {"start_ns": 0, "end_ns": 1000, "change_point_ns": 0},
        "frame_ids": [f"f{i}" for i in range(count)],
        "stimulus": {"kind": "constant_step", "declaration": "authored_simulation"},
        "hold": {"consecutive_observations": 2, "minimum_span_ns": 100},
        "crossing_exposure_policy": "exclude",
    }


def observed(count=4, height=4, *, origin=0, exposure=10, line=0):
    return [
        {
            "frame_id": f"f{i}",
            "start_ns": origin + i * 100,
            "applied_exposure_ns": exposure,
            "row_start_ns": [origin + i * 100 + r * line for r in range(height)],
            "row_end_ns": [origin + i * 100 + r * line + exposure for r in range(height)],
            "statistics_ready_ns": origin + i * 100 + (height - 1) * line + exposure + 5,
        }
        for i in range(count)
    ]


def run(values, spec=None, fmt=None, frames=None, shape=(4, 4)):
    spec = specification(len(values), shape) if spec is None else spec
    raw = np.asarray([np.full((1, *shape), value, dtype=np.uint16) for value in values])
    return response.evaluate(
        raw,
        FORMAT if fmt is None else fmt,
        observed(len(values), shape[0]) if frames is None else frames,
        spec,
    )


def test_confirmed_hold_distinguishes_entry_from_confirmation_and_breaks_on_excursion():
    result = run([116, 136, 116, 120])
    assert result["settling"] == {
        "status": "complete",
        "reason": "sampled_hold_confirmed",
        "entry_frame_id": "f2",
        "confirmation_frame_id": "f3",
        "entry_latency_ns": 215,
        "confirmation_latency_ns": 315,
        "observed_hold_span_ns": 100,
    }
    assert [f["in_target"] for f in result["frames"]] == [True, False, True, True]
    assert result["summary"]["peak_above_upper_dn"] == 10
    assert result["authority"]["continuous_hold"] == "not_established"
    assert result["authority"]["physical_validation"] == "not_established"


def test_hold_span_and_count_must_both_be_met_and_confirmation_is_first_qualifying_sample():
    spec = specification()
    spec["hold"] = {"consecutive_observations": 2, "minimum_span_ns": 200}
    result = run([116] * 4, spec)
    assert result["settling"]["entry_frame_id"] == "f0"
    assert result["settling"]["confirmation_frame_id"] == "f2"
    assert result["settling"]["observed_hold_span_ns"] == 200
    spec["hold"] = {"consecutive_observations": 4, "minimum_span_ns": 0}
    assert run([116] * 4, spec)["settling"]["confirmation_frame_id"] == "f3"


def test_complete_roster_failure_differs_from_short_span_missing_and_excluded_censoring():
    assert run([16] * 4)["settling"]["status"] == "not_reached"
    spec = specification()
    spec["hold"]["minimum_span_ns"] = 500
    assert run([16] * 4, spec)["settling"] == {
        "status": "censored",
        "reason": "insufficient_observation_count_or_span",
        "entry_frame_id": None,
        "confirmation_frame_id": None,
        "entry_latency_ns": None,
        "confirmation_latency_ns": None,
        "observed_hold_span_ns": None,
    }
    spec = specification()
    result = run([16] * 3, spec)
    assert result["frames"][-1]["status"] == "missing"
    assert result["frames"][-1]["statistics"] is None
    assert result["settling"]["reason"] == "missing_declared_observations"
    spec["window"]["end_ns"] = 250
    result = run([16] * 4, spec)
    assert result["frames"][-1]["exclusion_reasons"] == [
        "statistics_ready_outside_window",
        "exposure_outside_window",
    ]
    assert result["settling"]["reason"] == "excluded_declared_observations"


def test_missing_middle_observation_breaks_hold_without_interpolation():
    spec = specification(3)
    frames = observed(3)
    result = run([116, 116], spec, frames=[frames[0], frames[2]])
    assert result["settling"]["status"] == "censored"
    assert result["summary"]["in_target_frame_count"] == 2


def test_hold_confirmation_is_historical_event_not_claim_of_terminal_stability():
    result = run([116, 116, 4095, 0])
    assert result["settling"]["status"] == "complete"
    assert result["settling"]["confirmation_frame_id"] == "f1"
    assert result["summary"]["in_target_frame_count"] == 2
    assert result["summary"]["peak_above_upper_dn"] == 3969


def test_clipped_constant_is_not_lfm_or_physical_stability_and_rails_are_retained():
    result = run([4095] * 4)
    assert result["settling"]["status"] == "not_reached"
    for entry in result["frames"]:
        assert entry["statistics"]["value_dn"] == 4079
        assert entry["statistics"]["digital_high_rail_fraction"] == 1
        assert entry["statistics"]["sample_count"] == 8
    spec = specification()
    spec["target_black_relative_dn"] = {"minimum": 4079, "maximum": 4079}
    result = run([4095] * 4, spec)
    assert result["settling"]["status"] == "complete"  # Authored code target only.
    assert result["authority"]["lfm_performance"] == "not_established"
    assert result["authority"]["physical_validation"] == "not_established"


@pytest.mark.parametrize("values", [[116] * 4, [0, 4095, 0, 4095]])
def test_pwm_has_no_constant_step_settling_even_when_target_or_constant_is_observed(values):
    spec = specification()
    spec["stimulus"]["kind"] = "pwm"
    result = run(values, spec)
    assert result["settling"]["status"] == "not_applicable"
    assert result["settling"]["reason"] == "pwm_is_not_a_constant_step"
    assert result["settling"]["entry_latency_ns"] is None
    assert result["summary"]["observed_frame_count"] == 4


def test_fractional_black_exact_integer_sum_signed_mean_and_independent_odd_roi_phase():
    spec = specification(1, (2, 6))
    spec["roi"]["origin"] = [1, 1]
    spec["sites"] = ["R"]
    spec["target_black_relative_dn"] = {"minimum": -0.5, "maximum": 0.5}
    fmt = {**FORMAT, "black_level_dn": 2 / 3, "response_type": "nonlinear_charge_voltage"}
    raw = np.zeros((1, 1, 4, 8), dtype=np.uint16)
    raw[0, 0, 2, [2, 4, 6]] = [1, 1, 0]
    expected = Fraction(2, 3) - Fraction(str(fmt["black_level_dn"]))
    with localcontext() as context:
        context.prec = 6
        result = response.evaluate(raw, fmt, observed(1), spec)
    stats = result["frames"][0]["statistics"]
    assert stats["sum_codes_dn"] == 2
    assert stats["sample_count"] == 3
    assert stats["value_dn"] == float(expected)
    assert stats["excluded_sites"] == ["Gr", "Gb", "B"]
    assert stats["digital_low_rail_count"] == 1
    assert stats["digital_low_rail_fraction"] == float(Fraction(1, 3))
    assert stats["native_site_mean_dn"]["Gr"] == -fmt["black_level_dn"]
    assert result["raw_format"]["response_type"] == "nonlinear_charge_voltage"
    assert result["authority"]["charge_or_irradiance_inversion"] is False


def test_uint32_sum_is_exact_and_negative_black_relative_rails_are_not_discarded():
    raw = np.full((1, 1, 512, 512), 2**32 - 1, dtype=np.uint32)
    spec = specification(1, (512, 512))
    fmt = {**FORMAT, "bit_depth": 32}
    result = response.evaluate(raw, fmt, observed(1, 512), spec)
    stats = result["frames"][0]["statistics"]
    assert stats["sum_codes_dn"] == (512**2 // 2) * (2**32 - 1)
    negative = run([0])
    assert negative["frames"][0]["statistics"]["value_dn"] == -16
    assert negative["frames"][0]["statistics"]["digital_low_rail_fraction"] == 1


@pytest.mark.parametrize("policy,eligible", [("exclude", False), ("include_flagged", True)])
def test_crossed_change_is_explicit_and_does_not_silently_drop_raw(policy, eligible):
    spec = specification(2)
    spec["window"]["change_point_ns"] = 5
    spec["crossing_exposure_policy"] = policy
    result = run([116, 116], spec)
    first = result["frames"][0]
    assert first["eligible"] is eligible
    assert first["exposure_flags"]["crosses_change_point"] is True
    assert first["statistics"]["value_dn"] == 100
    assert result["settling"]["status"] == ("complete" if eligible else "censored")


def test_exposure_crossing_uses_only_rows_of_selected_native_sites_and_boundary_ties():
    spec = specification(2)
    spec["sites"] = ["B"]
    spec["window"]["change_point_ns"] = 10
    frames = observed(2, line=10)
    result = run([116, 116], spec, frames=frames)
    first = result["frames"][0]
    assert first["timing"]["roi_exposure_start_ns"] == 10
    assert first["exposure_flags"]["crosses_change_point"] is False
    assert first["eligible"] is True
    spec["sites"] = ["R"]
    assert (
        run([116, 116], spec, frames=frames)["frames"][0]["exposure_flags"]["crosses_change_point"]
        is True
    )


def test_window_boundary_inclusion_and_old_completed_exposures_are_explicit():
    spec = specification(2)
    spec["window"] = {"start_ns": 10, "end_ns": 115, "change_point_ns": 10}
    result = run([116, 116], spec)
    assert result["frames"][0]["exposure_flags"]["fully_before_change_point"] is True
    assert result["frames"][1]["eligible"] is True  # readiness at end is included
    assert result["settling"]["status"] == "censored"


def test_large_exact_clock_translation_preserves_integer_latencies_and_forbids_unsafe_json_clock():
    spec = specification()
    offset = MAX_TIME_NS - 2000
    spec["window"] = {k: v + offset for k, v in spec["window"].items()}
    result = run([116] * 4, spec, frames=observed(origin=offset))
    assert result["settling"]["entry_latency_ns"] == 15
    assert result["settling"]["confirmation_latency_ns"] == 115
    spec["window"]["end_ns"] = 2**53
    with pytest.raises(ValueError, match="integer"):
        response.validate_evaluation(spec)
    spec = specification()
    frames = observed()
    frames[-1]["statistics_ready_ns"] = 2**53 + 1
    with pytest.raises(ValueError, match="integer"):
        run([116] * 4, spec, frames=frames)


def test_empty_observation_input_reports_missing_without_fabrication():
    result = response.evaluate(np.empty((0, 1, 4, 4), dtype=np.uint16), FORMAT, [], specification())
    assert result["summary"]["observed_frame_count"] == 0
    assert result["summary"]["peak_above_upper_dn"] is None
    assert result["settling"]["status"] == "censored"
    json.dumps(result, allow_nan=False)


def test_comparison_matches_frame_ids_and_keeps_actual_timing_differences():
    spec = specification()
    base = run([116, 96, 116, 116], spec)
    frames = observed(exposure=20)
    candidate = run([126, 106, 116, 116], spec, frames=frames)
    result = response.compare(base, candidate)
    assert [row["delta_value_dn"] for row in result["frames"]] == [10, 10, 0, 0]
    assert [row["delta_applied_exposure_ns"] for row in result["frames"]] == [10] * 4
    assert [row["delta_statistics_ready_ns"] for row in result["frames"]] == [10] * 4
    assert result["paired_frame_count"] == 4
    assert result["baseline_settling"]["entry_frame_id"] == "f2"
    assert result["candidate_settling"]["entry_frame_id"] == "f0"
    assert "winner" not in result and "lfm_score" not in result


def test_comparison_missing_and_excluded_frames_have_null_deltas_and_side_reasons():
    spec = specification()
    base = run([16] * 4, spec)
    frames = observed(3)
    frames[1]["statistics_ready_ns"] = 120
    candidate = run([116] * 3, spec, frames=frames)
    result = response.compare(base, candidate)
    assert result["frames"][-1]["status"] == "unavailable"
    assert result["frames"][-1]["missing_or_excluded_reasons"] == ["candidate:missing_observation"]
    assert result["frames"][-1]["delta_value_dn"] is None
    assert result["frames"][-1]["baseline_value_dn"] == 0
    assert result["frames"][-1]["candidate_value_dn"] is None


def test_evaluation_and_comparison_are_detached_and_do_not_mutate_inputs():
    spec = specification()
    original = deepcopy(spec)
    frames = observed()
    oldframes = deepcopy(frames)
    raw = np.full((4, 1, 4, 4), 116, dtype=np.uint16)
    before = raw.copy()
    result = response.evaluate(raw, FORMAT, frames, spec)
    compared = response.compare(result, result)
    compared["specification"]["sites"].append("B")
    result["specification"]["window"]["start_ns"] = 4
    assert spec == original and frames == oldframes
    np.testing.assert_array_equal(raw, before)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x.update(hidden_truth={"photocharge": 1}),
        lambda x: x["roi"].update(reference="crop"),
        lambda x: x["roi"].update(shape=[3, 4]),
        lambda x: x["roi"].update(origin=[511, 0]),
        lambda x: x.update(sites=["Gr", "Gr"]),
        lambda x: x.update(sites=[]),
        lambda x: x["target_black_relative_dn"].update(minimum=float("nan")),
        lambda x: x["target_black_relative_dn"].update(maximum=10**1000),
        lambda x: x["target_black_relative_dn"].update(minimum=True),
        lambda x: x["target_black_relative_dn"].update(minimum=111),
        lambda x: x["clock"].update(reference="unix"),
        lambda x: x["window"].update(start_ns=True),
        lambda x: x["window"].update(change_point_ns=1000),
        lambda x: x["window"].update(start_ns=-MAX_TIME_NS, end_ns=MAX_TIME_NS),
        lambda x: x.update(frame_ids=["f0", "f0"]),
        lambda x: x["stimulus"].update(declaration="inferred_from_raw"),
        lambda x: x["stimulus"].update(kind="unknown"),
        lambda x: x["hold"].update(consecutive_observations=0),
        lambda x: x["hold"].update(minimum_span_ns=1.0),
        lambda x: x.update(crossing_exposure_policy="silently_skip"),
    ],
)
def test_strict_spec_refuses_ambiguous_unbounded_or_truth_bearing_fields(mutation):
    spec = specification()
    mutation(spec)
    with pytest.raises(ValueError):
        response.validate_evaluation(spec)


def test_plan_checks_selected_frame_order_and_native_roi_geometry():
    spec = specification(2)
    response.validate_evaluation(spec, frame_ids=["f0", "f1", "f2"], frame_shape=(4, 4))
    for ids in (["f1", "f0"], ["f0"]):
        with pytest.raises(ValueError, match="ordered subset"):
            response.validate_evaluation(spec, frame_ids=ids)
    with pytest.raises(ValueError, match="exceeds"):
        response.validate_evaluation(spec, frame_shape=(2, 2))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda f: f[0].update(retained_charge_e=[1]),
        lambda f: f[0].update(statistics_ready_ns=9),
        lambda f: f[0].update(applied_exposure_ns=11),
        lambda f: f[0].update(row_start_ns=[0, 0]),
        lambda f: f[0].update(row_start_ns=[0, 0, True, 0]),
        lambda f: f[0].update(row_start_ns=[0, 3, 1, 0]),
        lambda f: f[1].update(frame_id="f0"),
        lambda f: f[1].update(start_ns=0),
        lambda f: f[1].update(statistics_ready_ns=15),
        lambda f: f[1].update(
            start_ns=5, row_start_ns=[5] * 4, row_end_ns=[15] * 4, statistics_ready_ns=20
        ),
    ],
)
def test_only_complete_causal_observed_frame_contract_is_admitted(mutation):
    frames = observed()
    mutation(frames)
    with pytest.raises(ValueError):
        run([116] * 4, frames=frames)


@pytest.mark.parametrize(
    "raw",
    [
        np.ones((4, 4)),
        np.ones((4, 1, 4, 4)),
        np.ones((4, 2, 4, 4), dtype=np.uint16),
        np.ones((17, 1, 4, 4), dtype=np.uint16),
        np.ones((4, 1, 3, 4), dtype=np.uint16),
        np.full((4, 1, 4, 4), -1, dtype=np.int16),
        np.full((4, 1, 4, 4), 4096, dtype=np.uint16),
    ],
)
def test_original_single_integer_native_raw_is_required(raw):
    with pytest.raises(ValueError):
        response.evaluate(raw, FORMAT, observed(), specification())


def test_comparison_refuses_different_evaluation_format_and_forged_integer_statistics():
    base = run([116] * 4)
    different = deepcopy(base)
    different["specification"]["sites"] = ["B"]
    with pytest.raises(ValueError):
        response.compare(base, different)
    different = deepcopy(base)
    different["raw_format"]["response_type"] = "nonlinear_charge_voltage"
    with pytest.raises(ValueError, match="identical"):
        response.compare(base, different)
    different = deepcopy(base)
    different["frames"][0]["statistics"]["sum_codes_dn"] += 1
    with pytest.raises(ValueError, match="integer RAW"):
        response.compare(base, different)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x["frames"][0].update(in_target=False),
        lambda x: x["frames"][0].update(above_target_dn=1),
        lambda x: x["frames"][0].update(eligible=False),
        lambda x: x["frames"][0]["statistics"].update(digital_high_rail_fraction=0.5),
        lambda x: x["frames"][0]["statistics"].update(digital_high_rail_count=1),
        lambda x: x["frames"][0]["statistics"].update(sample_count=1),
        lambda x: x["frames"][0]["statistics"].update(selected_sites=["R"]),
        lambda x: x["frames"][0]["statistics"].update(native_site_mean_dn={"R": 0}),
        lambda x: x["frames"][0]["timing"].update(statistics_ready_ns=True),
        lambda x: x["frames"][0]["timing"].update(applied_exposure_ns=0),
        lambda x: x["frames"][0]["timing"].update(roi_exposure_end_ns=500),
        lambda x: x["frames"][0].update(exposure_flags={}),
        lambda x: x["frames"][0].update(exclusion_reasons=["made_up"]),
        lambda x: x["summary"].update(in_target_frame_count=0),
        lambda x: x["settling"].update(entry_latency_ns=999),
        lambda x: x["authority"].update(simulation_truth_used=0),
        lambda x: x["frames"].append(None),
    ],
)
def test_compare_refuses_internally_inconsistent_evaluation_documents(mutation):
    base = run([116] * 4)
    candidate = deepcopy(base)
    mutation(candidate)
    with pytest.raises(ValueError):
        response.compare(base, candidate)


def test_comparison_excluded_observation_retains_signed_value_but_has_no_delta():
    spec = specification(2)
    spec["window"]["change_point_ns"] = 10
    spec["hold"] = {"consecutive_observations": 1, "minimum_span_ns": 0}
    base_frames = observed(2, exposure=10)
    candidate_frames = observed(2, exposure=20)
    base = run([0, 116], spec, frames=base_frames)
    candidate = run([16, 116], spec, frames=candidate_frames)
    pair = response.compare(base, candidate)["frames"][0]
    assert pair["baseline_value_dn"] == -16
    assert pair["candidate_value_dn"] == 0
    assert pair["delta_value_dn"] is None
    assert pair["missing_or_excluded_reasons"] == [
        "baseline:exposure_before_change_point",
        "candidate:crosses_change_point",
    ]


def test_comparison_fractional_dn_delta_uses_original_integer_sums_not_rounded_means():
    fmt = {**FORMAT, "black_level_dn": 2 / 3}
    spec = specification(2, (2, 6))
    spec["sites"] = ["R"]
    spec["target_black_relative_dn"] = {"minimum": -0.5, "maximum": 10}
    raw_a = np.zeros((2, 1, 2, 6), dtype=np.uint16)
    raw_b = raw_a.copy()
    raw_a[:, 0, 0, [0, 2, 4]] = [1, 1, 0]
    raw_b[:, 0, 0, [0, 2, 4]] = [1, 1, 1]
    frames = observed(2, 2)
    a = response.evaluate(raw_a, fmt, frames, spec)
    b = response.evaluate(raw_b, fmt, frames, spec)
    result = response.compare(a, b)
    assert result["frames"][0]["delta_value_dn"] == float(Fraction(1, 3))


def test_negative_safe_clock_comparison_does_not_treat_exposure_duration_as_timestamp():
    spec = specification()
    offset = -MAX_TIME_NS
    spec["window"] = {key: value + offset for key, value in spec["window"].items()}
    result = run([116] * 4, spec, frames=observed(origin=offset))
    comparison = response.compare(result, result)
    assert comparison["paired_frame_count"] == 4
    assert result["settling"]["entry_latency_ns"] == 15


def test_changing_future_raw_does_not_change_earlier_observation_or_confirmed_hold():
    a = run([116] * 4)
    b = run([116, 116, 0, 4095])
    assert a["frames"][:2] == b["frames"][:2]
    assert a["settling"] == b["settling"]


@pytest.mark.parametrize(
    "format_patch",
    [
        {"black_level_dn": True},
        {"black_level_dn": float("inf")},
        {"black_level_dn": 4095},
        {"hdr_streams": 2},
        {"hdr_streams": True},
        {"cfa": "BGGR"},
        {"response_type": "estimated_linear"},
        {"oracle": {}},
    ],
)
def test_evaluator_never_silently_changes_original_readout_format(format_patch):
    with pytest.raises(ValueError):
        run([116] * 4, fmt={**FORMAT, **format_patch})


def test_target_must_lie_in_actual_signed_black_relative_code_range():
    spec = specification()
    spec["target_black_relative_dn"]["minimum"] = -17
    with pytest.raises(ValueError, match="actual black-relative"):
        run([116] * 4, spec)
    spec["target_black_relative_dn"] = {"minimum": 0, "maximum": 4095}
    with pytest.raises(ValueError, match="actual black-relative"):
        run([116] * 4, spec)
