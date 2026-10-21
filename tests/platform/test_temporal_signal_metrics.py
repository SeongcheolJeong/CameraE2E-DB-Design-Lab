"""Independent time truth cannot be replaced by same-RAW baseline fidelity."""

import io

import numpy as np
import pytest

from camera_platform import retained_isp_comparison as comparison
from camera_platform import temporal_signal_metrics as metrics
from camera_platform import temporal_signal_reference as reference


def inputs(values, actual=None):
    truth = np.broadcast_to(np.asarray(values, dtype=np.float64)[:, None, None, None],
                            (len(values), 2, 2, 3)).copy()
    candidate = (truth.copy() if actual is None else
                 np.broadcast_to(np.asarray(actual, dtype=np.float64)[:, None, None, None],
                                 truth.shape).copy())
    raw = np.full(truth.shape[:3], 100, dtype=np.int32)
    gate = np.zeros(raw.shape, dtype=bool)
    profile = {"temporal_filter": {"previous_weight": 0.5}}
    clock = [[index * 10, index * 10 + 1] for index in range(len(values))]
    spec = {
        "roi": {"row_start": 0, "row_end": 2, "column_start": 0, "column_end": 2},
        "step": None, "flicker_window": None, "minimum_reference_change": 0.01,
    }
    return candidate, truth, raw, gate, profile, clock, spec


def measure(args):
    candidate, truth, raw, gate, profile, clock, spec = args
    return metrics.measure(candidate, truth, raw, {"bit_depth": 12, "black_level_dn": 64},
                           gate, profile, clock, spec)


def test_period_two_true_scene_is_lost_by_flat_candidate_even_when_mean_bias_is_zero():
    actual = measure(inputs([0.25, 0.75, 0.25, 0.75], [0.5] * 4))
    values = actual["metrics"]
    assert values["truth_code_mae"]["value"] == pytest.approx(0.25)
    assert values["signal_amplitude_retention"]["value"] == pytest.approx(0)
    assert values["signal_amplitude_error"]["value"] == pytest.approx(1)
    assert values["signal_change_rms_error"]["value"] == pytest.approx(1)


def test_overshoot_is_worse_than_correct_retention_and_absolute_bias_is_visible():
    correct = measure(inputs([0.25, 0.75, 0.25, 0.75]))
    overshoot = measure(inputs([0.25, 0.75, 0.25, 0.75], [0, 1, 0, 1]))
    offset = measure(inputs([0.25, 0.75, 0.25, 0.75], [0.3, 0.8, 0.3, 0.8]))
    assert correct["metrics"]["signal_amplitude_error"]["value"] == pytest.approx(0)
    assert overshoot["metrics"]["signal_amplitude_retention"]["value"] == pytest.approx(2)
    assert overshoot["metrics"]["signal_amplitude_error"]["value"] == pytest.approx(1)
    assert offset["metrics"]["signal_change_rms_error"]["value"] == pytest.approx(0)
    assert offset["metrics"]["truth_code_mae"]["value"] == pytest.approx(0.05)


def test_post_detail_candidate_is_compared_to_fixed_color_truth():
    args = inputs([0.25, 0.75, 0.25, 0.75])
    ccm_candidate = args[1].copy()  # fixed-color stage would appear perfect
    post_detail_candidate = ccm_candidate + 0.05  # authored detail-stage counterexample
    args[0][:] = post_detail_candidate
    result = measure(args)
    assert result["metrics"]["truth_code_mae"]["value"] == pytest.approx(0.05)
    assert result["metrics"]["signal_change_rms_error"]["value"] == pytest.approx(0)


def test_candidate_pre_tone_clipping_makes_truth_metrics_unknown():
    args = inputs([0.25, 0.75, 0.25, 0.75])
    args[0][0, 0, 0, 0] = 1.1
    result = measure(args)
    assert result["support_reason"] == "clipped_selected_support"
    assert result["metrics"]["truth_code_mae"]["value"] is None
    assert result["metrics"]["added_sample_age_ns"]["value"] == 0


def test_step_uses_actual_row_clock_and_nonsettling_is_unknown():
    args = inputs([0.2, 0.2, 0.8, 0.8], [0.2, 0.2, 0.5, 0.65])
    args[-1]["step"] = {
        "onset_frame": 2, "steady_start_frame": 2,
        "onset_fraction": 0.5, "settling_fraction": 0.2,
    }
    args[3][2:] = True
    result = measure(args)
    values = result["metrics"]
    assert values["onset_delay_ns"]["value"] == 0
    assert values["settling_delay_ns"]["status"] == "unknown"
    assert values["step_ghost_error"]["value"] == pytest.approx(0.5)
    assert values["added_sample_age_ns"]["value"] == pytest.approx(7.5)


def test_step_onset_is_absolute_timing_error_against_independent_truth():
    spec = {"onset_frame": 1, "steady_start_frame": 2,
            "onset_fraction": 0.5, "settling_fraction": 0.2}
    matching = inputs([0.2, 0.2, 0.8, 0.8])
    matching[-1]["step"] = spec
    expected = measure(matching)
    assert expected["metrics"]["onset_delay_ns"]["value"] == 0
    assert expected["onset_time_basis"] == (
        "absolute_candidate_vs_independent_truth_row_crossing")
    early = inputs([0.2, 0.2, 0.8, 0.8], [0.2, 0.8, 0.8, 0.8])
    early[-1]["step"] = spec
    assert measure(early)["metrics"]["onset_delay_ns"]["value"] == 10
    late = inputs([0.2, 0.2, 0.8, 0.8], [0.2, 0.2, 0.2, 0.8])
    late[-1]["step"] = spec
    assert measure(late)["metrics"]["onset_delay_ns"]["value"] == 10


def test_known_stationary_period_two_window_and_clipping_fail_closed():
    args = inputs([0.2, 0.2, 0.7, 0.7], [0.3, 0.1, 0.7, 0.7])
    args[-1]["flicker_window"] = {
        "kind": "known_period_two", "start_frame": 0, "end_frame": 2,
        "maximum_reference_variation": 0,
    }
    result = measure(args)
    assert result["metrics"]["residual_period_two_amplitude"]["value"] == pytest.approx(0.1)
    args[2][0, 0, 0] = 4095
    clipped = measure(args)
    assert clipped["support_status"] == "unknown"
    assert clipped["metrics"]["signal_change_rms_error"]["value"] is None
    assert clipped["metrics"]["added_sample_age_ns"]["value"] == 0
    args[2][0, 0, 0] = 64
    assert measure(args)["support_reason"] == "clipped_selected_support"


def test_v2_rank_excludes_unknown_truth_even_when_legacy_constraints_pass():
    base = np.full((4, 2, 2, 3), 0.5)
    legacy = comparison.measure(base, base, {"row_start": 0, "row_end": 2,
                                             "column_start": 0, "column_end": 2})
    temporal = measure(inputs([0.25, 0.75, 0.25, 0.75]))
    missing = {**temporal, "support_status": "unknown",
               "metrics": {**temporal["metrics"], "signal_change_rms_error": {
                   "value": None, "unit": "1", "status": "unknown", "reason": "missing"}}}
    spec = {
        "objectives": [{"metric": "mean_abs_baseline_delta", "direction": "minimize"},
                       {"metric": "spatial_tv", "direction": "minimize"}],
        "limits": {"max_frame_mean_abs_delta": 1,
                   "max_frame_temporal_change_distortion": 1},
        "temporal_objectives": [{"metric": "signal_change_rms_error", "direction": "minimize"}],
        "temporal_constraints": [
            {"metric": "truth_code_mae", "operator": "<=", "value": 0.5,
             "unit": "normalized_code_rgb"},
            {"metric": "signal_amplitude_error", "operator": "<=", "value": 0.5,
             "unit": "1"},
            {"metric": "signal_change_rms_error", "operator": "<=", "value": 0.5,
             "unit": "1"},
        ],
    }
    ranked = comparison.rank_with_temporal_truth([
        {"candidate_id": "truth", "metrics": legacy, "temporal_metrics": temporal},
        {"candidate_id": "missing", "metrics": legacy, "temporal_metrics": missing},
    ], spec)
    assert ranked["pareto_candidate_ids"] == ["truth"]
    assert ranked["excluded_candidate_ids"] == ["missing"]
    assert ranked["candidates"][1]["feasibility"] == "unknown"
    assert ranked["pareto_provisional"] is True
    assert ranked["selected_candidate_id"] is None


def test_v2_rank_definite_failure_precedes_unknown_and_does_not_provisionalize():
    base = np.full((4, 2, 2, 3), 0.5)
    legacy = comparison.measure(base, base, {"row_start": 0, "row_end": 2,
                                             "column_start": 0, "column_end": 2})
    unknown = measure(inputs([0.25, 0.75, 0.25, 0.75]))
    unknown["support_status"] = "unknown"
    unknown["metrics"]["signal_change_rms_error"] = {
        "value": None, "unit": "1", "status": "unknown", "reason": "missing"}
    unknown["metrics"]["truth_code_mae"]["value"] = 0.25
    spec = {
        "objectives": [{"metric": "mean_abs_baseline_delta", "direction": "minimize"}],
        "limits": {"max_frame_mean_abs_delta": 1,
                   "max_frame_temporal_change_distortion": 1},
        "temporal_objectives": [{"metric": "signal_change_rms_error",
                                 "direction": "minimize"}],
        "temporal_constraints": [
            {"metric": "truth_code_mae", "operator": "<=", "value": 0.1,
             "unit": "normalized_code_rgb"}],
    }
    ranked = comparison.rank_with_temporal_truth([
        {"candidate_id": "failed", "metrics": legacy, "temporal_metrics": unknown},
    ], spec)
    assert ranked["candidates"][0]["feasibility"] == "infeasible"
    assert ranked["candidates"][0]["feasible"] is False
    assert ranked["excluded_candidate_ids"] == ["failed"]
    assert ranked["pareto_provisional"] is False


def test_reference_array_rejects_huge_untrusted_header_before_allocation():
    stream = io.BytesIO()
    np.lib.format.write_array_header_1_0(stream, {
        "descr": "<f8", "fortran_order": False, "shape": (2**30, 2**30, 2**30, 3),
    })
    with pytest.raises(ValueError, match="exact C-order"):
        reference._reference_array(stream.getvalue(), [4, 2, 2, 3])


def test_reference_support_accepts_real_period_two_and_refuses_false_step_onset():
    _, truth, raw, _, _, times, spec = inputs([0.25, 0.75, 0.25, 0.75])
    binding = {**spec, "row_transfer_ns": times}
    reference._support(binding, truth, raw, np.asarray(times))
    wrong = {**binding, "step": {"onset_frame": 3, "steady_start_frame": 3,
                                 "onset_fraction": 0.5, "settling_fraction": 0.2}}
    with pytest.raises(ValueError, match="stable independent pre"):
        reference._support(wrong, truth, raw, np.asarray(times))


def test_reference_support_accepts_stimulus_to_truth_latency_but_rejects_false_window():
    _, truth, raw, _, _, times, spec = inputs([0.2, 0.2, 0.8, 0.8])
    valid = {**spec, "row_transfer_ns": times,
             "step": {"onset_frame": 1, "steady_start_frame": 2,
                      "onset_fraction": 0.5, "settling_fraction": 0.2}}
    reference._support(valid, truth, raw, np.asarray(times))
    premature = {**valid, "step": {**valid["step"], "onset_frame": 3,
                                    "steady_start_frame": 3}}
    with pytest.raises(ValueError, match="stable independent pre"):
        reference._support(premature, truth, raw, np.asarray(times))
    unsteady = {**valid, "step": {**valid["step"], "steady_start_frame": 1}}
    with pytest.raises(ValueError, match="stable independent pre"):
        reference._support(unsteady, truth, raw, np.asarray(times))
