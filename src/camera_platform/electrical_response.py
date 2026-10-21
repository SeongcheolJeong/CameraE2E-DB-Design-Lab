"""Pure sampled, delivered-RAW response evaluation; no latent simulation inputs.

The authored frame roster defines the finite observation experiment. A confirmed
hold describes sampled codes only, never a continuously settled physical signal.
PWM observations intentionally have no constant-step settling interpretation.
"""

import math
from collections.abc import Mapping
from copy import deepcopy
from fractions import Fraction

import numpy as np

from .electrical_control import SITES, _format
from .timing import MAX_EXPOSURE_NS, MAX_FRAMES, MAX_TIME_NS, _clock, _integer, _span

SCHEMA = "camera_electrical_response_evaluation_v1"
RESULT_SCHEMA = "camera_electrical_response_result_v1"
COMPARISON_SCHEMA = "camera_electrical_response_comparison_v1"
AUTHORITY = {
    "input_quantity": "signed_black_relative_delivered_total_code_dn",
    "observation_scope": "authored_finite_frame_roster",
    "physical_validation": "not_established",
    "continuous_hold": "not_established",
    "lfm_performance": "not_established",
    "charge_or_irradiance_inversion": False,
    "simulation_truth_used": False,
    "automatic_selection": False,
}
FRAME_KEYS = {
    "frame_id",
    "start_ns",
    "applied_exposure_ns",
    "row_start_ns",
    "row_end_ns",
    "statistics_ready_ns",
}
SETTLING_KEYS = {
    "status",
    "reason",
    "entry_frame_id",
    "confirmation_frame_id",
    "entry_latency_ns",
    "confirmation_latency_ns",
    "observed_hold_span_ns",
}


def _keys(value, required, label):
    if not isinstance(value, Mapping) or set(value) != set(required):
        raise ValueError(f"{label} requires exactly {sorted(required)}")


def _number(value, label, low=-(2**32 - 1), high=2**32 - 1):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{label} requires a finite number in [{low},{high}]")
    return value


def _ids(value, label, *, empty=False):
    if (
        not isinstance(value, list)
        or not (0 if empty else 1) <= len(value) <= MAX_FRAMES
        or any(type(v) is not str or not v.strip() or len(v) > 200 for v in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label} requires unique ordered frame IDs, at most {MAX_FRAMES}")
    return value


def _shape(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} requires two even native dimensions")
    result = [_integer(v, label, 2, 512) for v in value]
    if any(v % 2 for v in result):
        raise ValueError(f"{label} requires two even native dimensions")
    return result


def validate_evaluation(payload, *, frame_ids=None, frame_shape=None):
    """Validate authored observation settings, never infer a stimulus from RAW."""
    _keys(
        payload,
        {
            "schema",
            "roi",
            "sites",
            "target_black_relative_dn",
            "clock",
            "window",
            "frame_ids",
            "stimulus",
            "hold",
            "crossing_exposure_policy",
        },
        "evaluation",
    )
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported electrical response evaluation schema")
    roi = payload["roi"]
    _keys(roi, {"reference", "origin", "shape"}, "roi")
    if roi["reference"] != "native_sensor":
        raise ValueError("Evaluation ROI requires the full native sensor reference")
    shape = _shape(roi["shape"], "roi.shape")
    if not isinstance(roi["origin"], list) or len(roi["origin"]) != 2:
        raise ValueError("Evaluation ROI origin requires two nonnegative integers")
    origin = [_integer(v, "roi.origin", 0, 510) for v in roi["origin"]]
    full = _shape(frame_shape, "frame_shape") if frame_shape is not None else [512, 512]
    if any(origin[i] + shape[i] > full[i] for i in range(2)):
        raise ValueError("Evaluation ROI exceeds the full native sensor")
    sites = payload["sites"]
    if (
        not isinstance(sites, list)
        or not 1 <= len(sites) <= 4
        or any(type(v) is not str or v not in SITES for v in sites)
        or len(set(sites)) != len(sites)
    ):
        raise ValueError("Evaluation sites require unique explicit R/Gr/Gb/B entries")
    target = payload["target_black_relative_dn"]
    _keys(target, {"minimum", "maximum"}, "target_black_relative_dn")
    for key, value in target.items():
        _number(value, "target_black_relative_dn." + key)
    if target["minimum"] > target["maximum"]:
        raise ValueError("Target minimum must not exceed maximum")
    _clock(payload["clock"], "evaluation.clock")
    window = payload["window"]
    _keys(window, {"start_ns", "end_ns", "change_point_ns"}, "window")
    for key, value in window.items():
        _integer(value, "window." + key)
    if not window["start_ns"] <= window["change_point_ns"] < window["end_ns"]:
        raise ValueError("Window requires start <= change point < end")
    _span(list(window.values()), "evaluation.window")
    selected = _ids(payload["frame_ids"], "evaluation.frame_ids")
    if frame_ids is not None:
        available = _ids(frame_ids, "available frame_ids")
        if (
            any(v not in available for v in selected)
            or [v for v in available if v in selected] != selected
        ):
            raise ValueError("Evaluation frame IDs must be an ordered subset of the acquisition")
    stimulus = payload["stimulus"]
    _keys(stimulus, {"kind", "declaration"}, "stimulus")
    if (
        stimulus["kind"] not in ("constant_step", "pwm")
        or stimulus["declaration"] != "authored_simulation"
    ):
        raise ValueError("Stimulus must explicitly declare authored constant_step or pwm")
    hold = payload["hold"]
    _keys(hold, {"consecutive_observations", "minimum_span_ns"}, "hold")
    _integer(hold["consecutive_observations"], "hold.consecutive_observations", 1, MAX_FRAMES)
    _integer(hold["minimum_span_ns"], "hold.minimum_span_ns", 0, MAX_TIME_NS)
    if payload["crossing_exposure_policy"] not in ("exclude", "include_flagged"):
        raise ValueError("Crossing exposure policy must be exclude or include_flagged")
    result = deepcopy(dict(payload))
    result["roi"]["shape"] = shape
    return result


def _observed_frames(frames, count, height, specification):
    if not isinstance(frames, list) or len(frames) != count:
        raise ValueError("Observed frame records must match the RAW frame count")
    for frame in frames:
        _keys(frame, FRAME_KEYS, "observed frame")
    ids = _ids([f["frame_id"] for f in frames], "observed frame IDs", empty=True)
    selected = specification["frame_ids"]
    if [v for v in ids if v in selected] != [v for v in selected if v in ids]:
        raise ValueError("Observed frames disagree with the declared frame order")
    times = list(specification["window"].values())
    previous = None
    for frame in frames:
        start = _integer(frame["start_ns"], "frame.start_ns")
        exposure = _integer(
            frame["applied_exposure_ns"], "frame.applied_exposure_ns", 1, MAX_EXPOSURE_NS
        )
        ready = _integer(frame["statistics_ready_ns"], "frame.statistics_ready_ns")
        for key in ("row_start_ns", "row_end_ns"):
            rows = frame[key]
            if not isinstance(rows, list) or len(rows) != height:
                raise ValueError("Observed row times must cover every full-native sensor row")
            for value in rows:
                _integer(value, "frame." + key)
            if rows != sorted(rows):
                raise ValueError("Full-native row times must be nondecreasing")
            times.extend(rows)
        starts, ends = frame["row_start_ns"], frame["row_end_ns"]
        if start > starts[0] or any(b - a != exposure for a, b in zip(starts, ends, strict=True)):
            raise ValueError("Observed row durations must equal the actual applied exposure")
        if ready < ends[-1]:
            raise ValueError("RAW statistics cannot be ready before all native rows end")
        if previous is not None and (
            start <= previous["start_ns"]
            or ready <= previous["statistics_ready_ns"]
            or any(a < b for a, b in zip(starts, previous["row_end_ns"], strict=True))
        ):
            raise ValueError(
                "Observed frames require ordered clocks and nonoverlapping same-row exposures"
            )
        times.extend((start, ready))
        previous = frame
    _span(times, "observation clock span")
    return {frame["frame_id"]: (index, frame) for index, frame in enumerate(frames)}


def _statistics(raw, specification, black, white):
    row, col = specification["roi"]["origin"]
    height, width = specification["roi"]["shape"]
    codes = raw[row : row + height, col : col + width]
    yy, xx = np.indices(codes.shape)
    indices = 2 * ((yy + row) % 2) + ((xx + col) % 2)
    mask = np.isin(indices, [SITES.index(site) for site in specification["sites"]])
    selected = codes[mask]
    count, total = int(selected.size), int(selected.astype(np.uint64).sum())
    exact = Fraction(total, count) - Fraction(str(black))
    return {
        "sample_count": count,
        "sum_codes_dn": total,
        "value_dn": float(exact),
        "native_site_mean_dn": {
            site: float(
                Fraction(
                    int(codes[indices == i].astype(np.uint64).sum()), int((indices == i).sum())
                )
                - Fraction(str(black))
            )
            for i, site in enumerate(SITES)
        },
        "selected_sites": list(specification["sites"]),
        "excluded_sites": [site for site in SITES if site not in specification["sites"]],
        "digital_low_rail_count": int((selected == 0).sum()),
        "digital_high_rail_count": int((selected == white).sum()),
        "digital_low_rail_fraction": float((selected == 0).mean()),
        "digital_high_rail_fraction": float((selected == white).mean()),
        "rail_policy": "include_all_selected_codes_no_rejection",
    }, exact


def _eligibility(first, last, ready, specification):
    window = specification["window"]
    change = window["change_point_ns"]
    flags = {
        "crosses_window_start": first < window["start_ns"] < last,
        "crosses_change_point": first < change < last,
        "crosses_window_end": first < window["end_ns"] < last,
        "fully_before_change_point": last <= change,
        "fully_before_window": last <= window["start_ns"],
        "fully_after_window": first >= window["end_ns"],
    }
    reasons = []
    if not window["start_ns"] <= ready <= window["end_ns"]:
        reasons.append("statistics_ready_outside_window")
    if flags["fully_before_change_point"]:
        reasons.append("exposure_before_change_point")
    if flags["fully_before_window"] or flags["fully_after_window"]:
        reasons.append("exposure_outside_window")
    if specification["crossing_exposure_policy"] == "exclude":
        reasons.extend(
            name
            for name in ("crosses_window_start", "crosses_change_point", "crosses_window_end")
            if flags[name]
        )
    return flags, reasons


def _target(exact, specification):
    target = specification["target_black_relative_dn"]
    low, high = Fraction(str(target["minimum"])), Fraction(str(target["maximum"]))
    return {
        "in_target": low <= exact <= high,
        "above_target_dn": float(max(Fraction(0), exact - high)),
        "below_target_dn": float(max(Fraction(0), low - exact)),
    }


def _entry(raw, frame, specification, black, white):
    statistics, exact = _statistics(raw, specification, black, white)
    row, _ = specification["roi"]["origin"]
    height, _ = specification["roi"]["shape"]
    # Only rows containing selected CFA sites belong to this observation.
    selected_row_parities = {SITES.index(site) // 2 for site in specification["sites"]}
    rows = [r for r in range(row, row + height) if r % 2 in selected_row_parities]
    starts = [frame["row_start_ns"][r] for r in rows]
    ends = [frame["row_end_ns"][r] for r in rows]
    first, last = min(starts), max(ends)
    flags, reasons = _eligibility(first, last, frame["statistics_ready_ns"], specification)
    return {
        "frame_id": frame["frame_id"],
        "status": "observed",
        "eligible": not reasons,
        "exclusion_reasons": reasons,
        "statistics": statistics,
        **_target(exact, specification),
        "timing": {
            key: frame[key] for key in ("start_ns", "applied_exposure_ns", "statistics_ready_ns")
        }
        | {"roi_exposure_start_ns": first, "roi_exposure_end_ns": last},
        "exposure_flags": flags,
    }


def _missing(frame_id):
    return {
        "frame_id": frame_id,
        "status": "missing",
        "eligible": False,
        "exclusion_reasons": ["missing_observation"],
        "statistics": None,
        "in_target": None,
        "above_target_dn": None,
        "below_target_dn": None,
        "timing": None,
        "exposure_flags": None,
    }


def _settling(entries, specification):
    result = {key: None for key in SETTLING_KEYS}
    if specification["stimulus"]["kind"] == "pwm":
        return result | {"status": "not_applicable", "reason": "pwm_is_not_a_constant_step"}
    hold = specification["hold"]
    consecutive = []
    for entry in entries:
        if not entry["eligible"] or not entry["in_target"]:
            consecutive = []
            continue
        consecutive.append(entry)
        first, last = consecutive[0], consecutive[-1]
        span = last["timing"]["statistics_ready_ns"] - first["timing"]["statistics_ready_ns"]
        if len(consecutive) >= hold["consecutive_observations"] and span >= hold["minimum_span_ns"]:
            change = specification["window"]["change_point_ns"]
            return {
                "status": "complete",
                "reason": "sampled_hold_confirmed",
                "entry_frame_id": first["frame_id"],
                "confirmation_frame_id": last["frame_id"],
                "entry_latency_ns": first["timing"]["statistics_ready_ns"] - change,
                "confirmation_latency_ns": last["timing"]["statistics_ready_ns"] - change,
                "observed_hold_span_ns": span,
            }
    eligible = [entry for entry in entries if entry["eligible"]]
    if any(entry["status"] == "missing" for entry in entries):
        reason = "missing_declared_observations"
    elif len(eligible) != len(entries):
        reason = "excluded_declared_observations"
    elif len(eligible) < hold["consecutive_observations"] or (
        eligible[-1]["timing"]["statistics_ready_ns"] - eligible[0]["timing"]["statistics_ready_ns"]
        < hold["minimum_span_ns"]
    ):
        reason = "insufficient_observation_count_or_span"
    else:
        return result | {"status": "not_reached", "reason": "no_hold_in_complete_declared_roster"}
    return result | {"status": "censored", "reason": reason}


def _summary(entries):
    eligible = [entry for entry in entries if entry["eligible"]]
    return {
        "declared_frame_count": len(entries),
        "observed_frame_count": sum(e["status"] == "observed" for e in entries),
        "missing_frame_count": sum(e["status"] == "missing" for e in entries),
        "eligible_frame_count": len(eligible),
        "excluded_observed_frame_count": sum(
            e["status"] == "observed" and not e["eligible"] for e in entries
        ),
        "in_target_frame_count": sum(e["in_target"] for e in eligible),
        "peak_above_upper_dn": max((e["above_target_dn"] for e in eligible), default=None),
        "peak_below_lower_dn": max((e["below_target_dn"] for e in eligible), default=None),
        "hold_time_basis": "statistics_ready_ns_first_to_confirmation_observation",
        "frame_roster_completeness": "complete"
        if all(e["status"] == "observed" for e in entries)
        else "incomplete",
    }


def evaluate(raw_groups, raw_format, frames, specification):
    """Evaluate only selected original codes and explicit observed acquisition timing.

    Missing declared frames are represented explicitly. Supplied RAW and frame
    records must correspond one-to-one; partial or invented per-row metadata is
    refused. The caller binds their clock identity to specification.clock.
    """
    raw = np.asarray(raw_groups)
    if raw.ndim != 4 or raw.shape[1] != 1 or not 0 <= raw.shape[0] <= MAX_FRAMES:
        raise ValueError(
            "Response evaluation requires RAW [frames,1,height,width], at most 16 frames"
        )
    if raw.dtype.kind not in "ui":
        raise ValueError("Response evaluation requires original integer RAW codes")
    full = _shape(raw.shape[2:], "RAW shape")
    specification = validate_evaluation(specification, frame_shape=full)
    fmt, black, white = _format(raw_format)
    target = specification["target_black_relative_dn"]
    if target["minimum"] < -black or target["maximum"] > white - black:
        raise ValueError("Evaluation target exceeds the actual black-relative digital range")
    if raw.size and (raw.min() < 0 or raw.max() > white):
        raise ValueError("RAW codes exceed their declared digital range")
    observed = _observed_frames(frames, raw.shape[0], full[0], specification)
    entries = [
        _entry(raw[observed[key][0], 0], observed[key][1], specification, black, white)
        if key in observed
        else _missing(key)
        for key in specification["frame_ids"]
    ]
    return {
        "schema": RESULT_SCHEMA,
        "specification": specification,
        "raw_format": fmt,
        "frames": entries,
        "summary": _summary(entries),
        "settling": _settling(entries, specification),
        "authority": deepcopy(AUTHORITY),
    }


def _checked_result(result):
    _keys(
        result,
        {"schema", "specification", "raw_format", "frames", "summary", "settling", "authority"},
        "evaluation result",
    )
    _keys(result["authority"], set(AUTHORITY), "evaluation authority")
    if result["schema"] != RESULT_SCHEMA or any(
        type(result["authority"][key]) is not type(value) or result["authority"][key] != value
        for key, value in AUTHORITY.items()
    ):
        raise ValueError("Comparison requires explicit sampled-code evaluation authority")
    specification = validate_evaluation(result["specification"])
    fmt, black, white = _format(result["raw_format"])
    entries = result["frames"]
    if (
        not isinstance(entries, list)
        or any(not isinstance(entry, Mapping) for entry in entries)
        or [entry.get("frame_id") for entry in entries] != specification["frame_ids"]
    ):
        raise ValueError("Evaluation results must retain the exact authored frame roster")
    previous_timing = None
    times = list(specification["window"].values())
    for entry in entries:
        _keys(entry, set(_missing(entry["frame_id"])), "evaluation frame")
        if entry["status"] == "missing":
            if entry != _missing(entry["frame_id"]):
                raise ValueError("Missing observations cannot contain invented metrics")
            continue
        if (
            entry["status"] != "observed"
            or type(entry["eligible"]) is not bool
            or type(entry["in_target"]) is not bool
        ):
            raise ValueError("Malformed observed evaluation status")
        stats = entry["statistics"]
        _keys(
            stats,
            {
                "sample_count",
                "sum_codes_dn",
                "value_dn",
                "native_site_mean_dn",
                "selected_sites",
                "excluded_sites",
                "digital_low_rail_count",
                "digital_high_rail_count",
                "digital_low_rail_fraction",
                "digital_high_rail_fraction",
                "rail_policy",
            },
            "observed RAW statistics",
        )
        count = _integer(stats.get("sample_count"), "sample_count", 1, 512**2)
        height, width = specification["roi"]["shape"]
        if count != height * width // 4 * len(specification["sites"]):
            raise ValueError("Evaluation sample count differs from the ROI/sites")
        total = _integer(stats.get("sum_codes_dn"), "sum_codes_dn", 0, count * white)
        value = _number(stats.get("value_dn"), "value_dn")
        exact = Fraction(total, count) - Fraction(str(black))
        if value != float(exact):
            raise ValueError("Evaluation mean differs from its integer RAW sum/count")
        if (
            stats["selected_sites"] != specification["sites"]
            or stats["excluded_sites"] != [s for s in SITES if s not in specification["sites"]]
            or stats["rail_policy"] != "include_all_selected_codes_no_rejection"
        ):
            raise ValueError("Evaluation RAW site/rail policy differs from its specification")
        _keys(stats["native_site_mean_dn"], set(SITES), "native_site_mean_dn")
        for mean in stats["native_site_mean_dn"].values():
            _number(mean, "native_site_mean_dn", -black, white - black)
        rail_counts = []
        for key in ("digital_low_rail_fraction", "digital_high_rail_fraction"):
            _number(stats.get(key), key, 0, 1)
            rail_count = _integer(stats[key.replace("fraction", "count")], key, 0, count)
            if stats[key] != rail_count / count:
                raise ValueError("Evaluation rail fraction differs from its sample count")
            rail_counts.append(rail_count)
        if (
            sum(rail_counts) > count
            or not rail_counts[1] * white <= total <= (count - rail_counts[0]) * white
        ):
            raise ValueError("Evaluation rail counts contradict its RAW sum")
        for key in ("above_target_dn", "below_target_dn"):
            _number(entry[key], key, 0, 2**33 - 2)
        if any(entry[key] != value for key, value in _target(exact, specification).items()):
            raise ValueError("Evaluation target metrics differ from its RAW mean")
        timing = entry["timing"]
        _keys(
            timing,
            {
                "start_ns",
                "applied_exposure_ns",
                "statistics_ready_ns",
                "roi_exposure_start_ns",
                "roi_exposure_end_ns",
            },
            "evaluation timing",
        )
        for key, value in timing.items():
            _integer(value, "evaluation timing." + key)
        _integer(timing["applied_exposure_ns"], "applied_exposure_ns", 1, MAX_EXPOSURE_NS)
        first, last = timing["roi_exposure_start_ns"], timing["roi_exposure_end_ns"]
        if not timing["start_ns"] <= first < last <= timing["statistics_ready_ns"]:
            raise ValueError("Evaluation acquisition/ready times are inconsistent")
        times.extend(value for key, value in timing.items() if key != "applied_exposure_ns")
        if previous_timing is not None and (
            timing["start_ns"] <= previous_timing["start_ns"]
            or timing["statistics_ready_ns"] <= previous_timing["statistics_ready_ns"]
        ):
            raise ValueError("Evaluation frame clocks are not ordered")
        previous_timing = timing
        flags, reasons = _eligibility(first, last, timing["statistics_ready_ns"], specification)
        if (
            entry["exposure_flags"] != flags
            or entry["exclusion_reasons"] != reasons
            or entry["eligible"] != (not reasons)
        ):
            raise ValueError("Evaluation exclusions differ from observed exposure timing")
    _span(times, "evaluation time span")
    if result["summary"] != _summary(entries):
        raise ValueError("Evaluation summary differs from its sampled observations")
    if result["settling"] != _settling(entries, specification):
        raise ValueError("Evaluation settling differs from its sampled observations")
    return specification, fmt


def compare(baseline_evaluation, candidate_evaluation):
    """Report candidate-minus-baseline code deltas by frame ID, without a winner.

    This pure comparison is not evidence authentication; service callers must
    verify both original RAW/source closures before supplying their evaluations.
    """
    spec, fmt = _checked_result(baseline_evaluation)
    other, other_fmt = _checked_result(candidate_evaluation)
    if spec != other or fmt != other_fmt:
        raise ValueError("Comparison requires identical evaluation specification and RAW format")
    rows = []
    for baseline, candidate in zip(
        baseline_evaluation["frames"], candidate_evaluation["frames"], strict=True
    ):
        reasons = [
            side + ":" + reason
            for side, entry in (("baseline", baseline), ("candidate", candidate))
            for reason in entry["exclusion_reasons"]
        ]
        both = baseline["status"] == candidate["status"] == "observed"
        comparable = both and baseline["eligible"] and candidate["eligible"]
        row = {
            "frame_id": baseline["frame_id"],
            "status": "paired" if comparable else "unavailable",
            "missing_or_excluded_reasons": reasons,
            "baseline_value_dn": baseline["statistics"]["value_dn"]
            if baseline["status"] == "observed"
            else None,
            "candidate_value_dn": candidate["statistics"]["value_dn"]
            if candidate["status"] == "observed"
            else None,
            "delta_value_dn": None,
            "delta_digital_low_rail_fraction": None,
            "delta_digital_high_rail_fraction": None,
            "delta_applied_exposure_ns": None,
            "delta_statistics_ready_ns": None,
        }
        if comparable:
            # The same black code cancels; subtract exact rational code means.
            a, b = baseline["statistics"], candidate["statistics"]
            row["delta_value_dn"] = float(
                Fraction(b["sum_codes_dn"], b["sample_count"])
                - Fraction(a["sum_codes_dn"], a["sample_count"])
            )
            for key in ("digital_low_rail_fraction", "digital_high_rail_fraction"):
                row["delta_" + key] = b[key] - a[key]
            for key in ("applied_exposure_ns", "statistics_ready_ns"):
                delta = candidate["timing"][key] - baseline["timing"][key]
                _integer(delta, "paired timing delta")
                row["delta_" + key] = delta
        rows.append(row)
    return {
        "schema": COMPARISON_SCHEMA,
        "specification": deepcopy(spec),
        "raw_format": deepcopy(fmt),
        "delta_convention": "candidate_minus_baseline",
        "frames": rows,
        "baseline_settling": deepcopy(baseline_evaluation["settling"]),
        "candidate_settling": deepcopy(candidate_evaluation["settling"]),
        "paired_frame_count": sum(row["status"] == "paired" for row in rows),
        "authority": deepcopy(AUTHORITY),
    }
