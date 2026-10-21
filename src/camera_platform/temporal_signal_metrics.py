"""Post-ISP, fixed-support comparison with independent time-resolved truth.

Reference bytes are integrity-checked during preflight. Their values never enter
`process_raw`; they are used only in evaluation. These sampled software metrics
do not infer photon charge, identify all pre-ADC clipping, or authenticate truth.
"""

from __future__ import annotations

import math
from copy import deepcopy

import numpy as np

from .registry import IntegrityError

METRIC_UNITS = {
    "truth_code_mae": "normalized_code_rgb",
    "signal_amplitude_retention": "1",
    "signal_amplitude_error": "1",
    "signal_change_rms_error": "1",
    "onset_delay_ns": "ns",
    "settling_delay_ns": "ns",
    "step_ghost_error": "1",
    "residual_period_two_amplitude": "normalized_code_rgb",
    "added_sample_age_ns": "ns",
}
OBJECTIVE_METRICS = {
    "truth_code_mae", "signal_amplitude_error", "signal_change_rms_error",
    "onset_delay_ns", "settling_delay_ns",
    "step_ghost_error", "residual_period_two_amplitude", "added_sample_age_ns",
}


def _metric(value, key, reason=None):
    if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
        raise ValueError("Temporal metric overflowed")
    return {"value": None if value is None else float(value),
            "unit": METRIC_UNITS[key], "status": "unknown" if value is None else "ok",
            "reason": reason if value is None else None}


def _all_unknown(reason):
    return {key: _metric(None, key, reason) for key in METRIC_UNITS}


def _age(gate, times, weight, rows, columns):
    cropped_gate = gate[:, rows, columns]
    cropped_times = times[:, rows]
    previous = np.zeros(cropped_gate.shape[1:], dtype=np.float64)
    maximum = 0.0
    for index in range(len(gate)):
        if index:
            interval = (cropped_times[index] - cropped_times[index - 1])[:, None]
            if np.any(interval <= 0):
                raise IntegrityError("Temporal reference row transfer clock is not monotone")
            previous = np.where(cropped_gate[index], weight * (interval + previous), 0.0)
            maximum = max(maximum, float(np.max(previous)))
        elif np.any(cropped_gate[0]):
            raise IntegrityError("First acquired frame cannot use previous ISP state")
    return maximum


def measure(actual, truth, raw, raw_format, gate, profile, row_transfer_ns, reference,
            *, pre_tone=None):
    """Return every temporal metric on one immutable native ROI.

    `actual` is the processor's final post-detail pre-tone RGB. The independently
    authored truth is in the fixed-color pre-detail coordinate. This stage
    mapping is explicit so detail candidates cannot escape the comparison.
    """
    actual, truth, raw, gate = map(np.asarray, (actual, truth, raw, gate))
    if (actual.shape != truth.shape or actual.ndim != 4 or actual.shape[-1] != 3
            or raw.shape != actual.shape[:3] or gate.shape != raw.shape
            or actual.dtype.kind != "f" or truth.dtype != np.dtype("float64")
            or gate.dtype.kind != "b"):
        raise IntegrityError("Temporal evaluation arrays differ from frozen native shape/type")
    times = np.asarray(row_transfer_ns)
    if times.shape != raw.shape[:2] or times.dtype.kind not in "iu":
        raise IntegrityError("Temporal evaluation row transfer clock differs")
    roi = reference["roi"]
    rows = slice(roi["row_start"], roi["row_end"])
    columns = slice(roi["column_start"], roi["column_end"])
    pre_tone = np.asarray(pre_tone) if pre_tone is not None else actual
    if (pre_tone.shape != actual.shape or not np.isfinite(pre_tone).all()
            or not np.isfinite(actual).all() or not np.isfinite(truth).all()
            or actual[:, rows, columns].size == 0):
        metrics = _all_unknown("Missing, nonfinite or empty fixed native support")
        metrics["added_sample_age_ns"] = _metric(_age(
            gate, times, profile["temporal_filter"]["previous_weight"], rows, columns
        ), "added_sample_age_ns")
        return {"schema": "camera_temporal_signal_metrics_v1", "metrics": metrics,
            "roi": deepcopy(roi),
            "support_status": "unknown", "support_reason": "invalid_selected_support",
            "physical_validation": "not_established"}
    selected_actual = actual[:, rows, columns]
    selected_truth = truth[:, rows, columns]
    selected_raw = raw[:, rows, columns]
    white = 2 ** raw_format["bit_depth"] - 1
    black = raw_format["black_level_dn"]
    if (np.any((selected_raw <= black) | (selected_raw == white))
            or np.any((selected_actual < 0) | (selected_actual > 1))
            or np.any((pre_tone[:, rows, columns] < 0)
                      | (pre_tone[:, rows, columns] > 1))
            or np.any((selected_truth < 0) | (selected_truth > 1))):
        metrics = _all_unknown(
            "Selected native ROI contains a code rail or clipped fixed-color sample")
        metrics["added_sample_age_ns"] = _metric(_age(
            gate, times, profile["temporal_filter"]["previous_weight"], rows, columns
        ), "added_sample_age_ns")
        return {"schema": "camera_temporal_signal_metrics_v1", "metrics": metrics,
            "roi": deepcopy(roi), "support_status": "unknown",
            "support_reason": "clipped_selected_support",
            "physical_validation": "not_established"}
    change_truth = np.diff(selected_truth, axis=0)
    change_actual = np.diff(selected_actual, axis=0)
    reference_rms = float(np.sqrt(np.mean(np.square(change_truth))))
    metrics = {}
    metrics["truth_code_mae"] = _metric(
        float(np.mean(np.abs(selected_actual - selected_truth))), "truth_code_mae"
    )
    if reference_rms < reference["minimum_reference_change"]:
        metrics["signal_amplitude_retention"] = _metric(
            None, "signal_amplitude_retention", "Independent truth time change is unresolved")
        metrics["signal_amplitude_error"] = _metric(
            None, "signal_amplitude_error", "Independent truth time change is unresolved")
        metrics["signal_change_rms_error"] = _metric(
            None, "signal_change_rms_error", "Independent truth time change is unresolved")
    else:
        denominator = float(np.sum(np.square(change_truth)))
        retention = float(np.sum(change_actual * change_truth) / denominator)
        error = float(np.sqrt(np.mean(np.square(change_actual - change_truth))) / reference_rms)
        metrics["signal_amplitude_retention"] = _metric(retention, "signal_amplitude_retention")
        metrics["signal_amplitude_error"] = _metric(abs(1 - retention), "signal_amplitude_error")
        metrics["signal_change_rms_error"] = _metric(error, "signal_change_rms_error")
    step = reference["step"]
    if step is None:
        for key in ("onset_delay_ns", "settling_delay_ns", "step_ghost_error"):
            metrics[key] = _metric(None, key, "No step event is declared")
        active_count = 0
    else:
        onset, steady = step["onset_frame"], step["steady_start_frame"]
        true_pre = np.mean(selected_truth[:onset], axis=0)
        actual_pre = np.mean(selected_actual[:onset], axis=0)
        true_delta = np.mean(selected_truth[steady:], axis=0) - true_pre
        active = np.abs(true_delta) >= reference["minimum_reference_change"]
        active_count = int(np.count_nonzero(active))
        if not active_count:
            for key in ("onset_delay_ns", "settling_delay_ns", "step_ghost_error"):
                metrics[key] = _metric(None, key, "Step has no resolved selected signal samples")
        else:
            relative = selected_actual - actual_pre
            true_relative = selected_truth - true_pre
            threshold = np.maximum(
                step["onset_fraction"] * np.abs(true_delta)
                - 16 * np.finfo(np.float64).eps, 0.0
            )
            truth_reached = (
                true_relative[onset:] * np.sign(true_delta) >= threshold
            )[:, active]
            candidate_reached = (
                relative[onset:] * np.sign(true_delta) >= threshold
            )[:, active]
            if not np.all(np.any(truth_reached, axis=0)):
                metrics["onset_delay_ns"] = _metric(
                    None, "onset_delay_ns",
                    "Independent truth never reaches the sampled step threshold")
            elif not np.all(np.any(candidate_reached, axis=0)):
                metrics["onset_delay_ns"] = _metric(
                    None, "onset_delay_ns", "Candidate never reaches the sampled step threshold")
            else:
                truth_first = onset + np.argmax(truth_reached, axis=0)
                candidate_first = onset + np.argmax(candidate_reached, axis=0)
                native_rows = np.broadcast_to(
                    np.arange(rows.start, rows.stop)[:, None, None], active.shape
                )[active]
                timing_error = np.abs(
                    times[candidate_first, native_rows] - times[truth_first, native_rows]
                )
                metrics["onset_delay_ns"] = _metric(
                    float(np.max(timing_error)), "onset_delay_ns")
            tolerance = step["settling_fraction"] * np.abs(true_delta)
            settled_now = np.abs(relative[onset:] - true_relative[onset:]) <= tolerance
            settled_suffix = np.logical_and.accumulate(settled_now[::-1], axis=0)[::-1]
            settled_suffix = settled_suffix[:, active]
            if not np.all(np.any(settled_suffix, axis=0)):
                metrics["settling_delay_ns"] = _metric(
                    None, "settling_delay_ns", "Candidate does not settle within recorded frames")
            else:
                first = onset + np.argmax(settled_suffix, axis=0)
                native_rows = np.broadcast_to(
                    np.arange(rows.start, rows.stop)[:, None, None], active.shape
                )[active]
                delay = times[first, native_rows] - times[onset, native_rows]
                metrics["settling_delay_ns"] = _metric(float(np.max(delay)), "settling_delay_ns")
            ghost = np.abs(relative[onset:] - true_relative[onset:])[:, active]
            scale = np.abs(true_delta[active])
            metrics["step_ghost_error"] = _metric(
                float(np.max(ghost / scale)), "step_ghost_error"
            )
    flicker = reference["flicker_window"]
    if flicker is None:
        metrics["residual_period_two_amplitude"] = _metric(
            None, "residual_period_two_amplitude", "No known stationary period-two window")
    else:
        start, end = flicker["start_frame"], flicker["end_frame"]
        expected = selected_truth[start:end]
        if float(np.max(np.ptp(expected, axis=0))) > flicker["maximum_reference_variation"]:
            metrics["residual_period_two_amplitude"] = _metric(
                None, "residual_period_two_amplitude", "Independent truth is not stationary")
        else:
            residual = selected_actual[start:end] - expected
            even, odd = residual[::2], residual[1::2]
            if not len(even) or not len(odd):
                metrics["residual_period_two_amplitude"] = _metric(
                    None, "residual_period_two_amplitude", "No sampled phase pair")
            else:
                amplitude = np.abs(even.mean(axis=0) - odd.mean(axis=0)) / 2
                metrics["residual_period_two_amplitude"] = _metric(
                    float(np.max(amplitude)), "residual_period_two_amplitude"
                )
    weight = profile["temporal_filter"]["previous_weight"]
    metrics["added_sample_age_ns"] = _metric(
        _age(gate, times, weight, rows, columns), "added_sample_age_ns"
    )
    return {
        "schema": "camera_temporal_signal_metrics_v1",
        "metrics": metrics,
        "roi": deepcopy(roi),
        "support_status": "ok",
        "support_reason": None,
        "truth_change_rms": reference_rms,
        "step_active_rgb_samples": active_count,
        "time_reference": "actual_per_row_transfer_ns",
        "onset_time_basis": "absolute_candidate_vs_independent_truth_row_crossing",
        "added_age_meaning": "effective_age_of_causal_native_RAW_site_recursion",
        "reference_independence": "declared_not_independently_authenticated",
        "physical_validation": "not_established",
    }
