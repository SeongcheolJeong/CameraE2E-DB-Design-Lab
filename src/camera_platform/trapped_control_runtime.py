"""Admission bounds and actual row plans for causal trap-bearing exposure control.

The admission envelope covers every allowed grid point without enumerating the
controller's future observations. It is a conservative resource bound, never a
claim that the planned initial exposure is the realized acquisition.
"""

from __future__ import annotations

import numpy as np

from . import electrical_control, trapped_charge, trapped_charge_runtime
from .registry import digest


def admission(parsed):
    """Bound the entire exposure interval before photon arrays or sequence RNG.

    Splitting length L at K interior cuts yields at most ceil(L/dt)+K
    intervals: sum ceil(piece_i/dt) <= ceil(L/dt)+K. The existing greedy
    splitter resets its max-step grid at each cut, so this bound applies even
    when changing exposure moves the final partial interval. For each row,
    exposure opens are fixed; its preceding gap length depends on the prior
    realized exposure. Separate gap/exposure maxima cover their joint domain.
    """
    height, width = parsed["base"]["size"]
    pixels = height * width
    policy, control = parsed["policy"], parsed["control"]
    grid = electrical_control.effective_exposure_grid(policy)
    low, high = (
        (grid["minimum"], grid["maximum"])
        if policy["enabled"]
        else (parsed["initial_settings"]["exposure_ns"], parsed["initial_settings"]["exposure_ns"])
    )
    dt = trapped_charge.effective_max_step_ns(parsed["program"]["retained_charge_model"])
    # Coverage validation ensures no exposure crosses outside the first/last
    # knots. Counting ALL interior knots is conservative for every row/open.
    knot_cuts = max(0, len(parsed["timeline"]["knots"]) - 2)
    waveform = parsed["program"]["waveform"]
    pwm_cuts = 0
    if (
        waveform is not None
        and waveform["kind"] == "pwm"
        and 0 < waveform["on_time_ns"] < waveform["period_ns"]
    ):
        pwm_cuts = 2 * ((high + waveform["period_ns"] - 1) // waveform["period_ns"]) + 2
    exposure_count = (high + dt - 1) // dt + knot_cuts + pwm_cuts
    frames = []
    for index, frame in enumerate(control["frames"]):
        gap = (
            0
            if index == 0
            else frame["frame_start_ns"] - control["frames"][index - 1]["frame_start_ns"] - low
        )
        if gap < 0:
            raise ValueError("Reachable trap control creates overlapping same-row exposures")
        gap_count = (gap + dt - 1) // dt
        count = gap_count + exposure_count
        if count > trapped_charge.MAX_SEGMENTS or count > trapped_charge.MAX_SUBSTEPS:
            raise ValueError("Reachable trap control exceeds bounded segments per frame")
        if count * pixels > trapped_charge.MAX_PIXEL_SUBSTEPS:
            raise ValueError("Reachable trap control exceeds bounded pixel-substep work")
        frames.append(
            {
                "frame_id": frame["frame_id"],
                "maximum_gap_ns": gap,
                "gap_segments_bound": gap_count,
                "exposure_segments_bound": exposure_count,
                "segment_count_bound": count,
                "pixel_substeps_bound": count * pixels,
            }
        )
    maximum = max(f["segment_count_bound"] for f in frames)
    total = sum(f["segment_count_bound"] for f in frames)
    # Include all retained frame plans, while building the current native-row
    # lists, plus the input means, normalized copies and core working arrays.
    plan_storage = total * height * 256
    temporary = maximum * height * 512
    working = maximum * pixels * 8 * 3 + pixels * 80 * 8 + plan_storage + temporary
    if working > trapped_charge.MAX_WORKING_BYTES:
        raise ValueError("Reachable trap control exceeds bounded plan/solver memory")
    evidence = len(frames) * maximum * height * 24 + len(frames) * maximum * 8 + len(frames) * 8
    return {
        "schema": "camera_trap_control_work_envelope_v1",
        "scope": "all_grid_exposures_and_all_predecessor_exposures",
        "proof": "sum_ceil_partition_lengths_le_ceil_total_plus_interior_cuts",
        "grid_point_count": grid["point_count"],
        "policy_enabled": policy["enabled"],
        "minimum_exposure_ns": low,
        "maximum_exposure_ns": high,
        "effective_max_step_ns": dt,
        "exposure_internal_scene_cut_bound": knot_cuts,
        "exposure_pwm_edge_bound": pwm_cuts,
        "row_batching_bound": "max_gap_segments_plus_max_exposure_segments",
        "frames": frames,
        "resources": {
            "trap_segment_count": total,
            "trap_maximum_segments_per_frame": maximum,
            "trap_pixel_substeps": total * pixels,
            "trap_effective_max_step_ns": dt,
            "trap_segment_working_bytes_estimate": working,
            "trap_timing_evidence_bytes_estimate": evidence,
        },
    }


def actual_frame_plan(parsed, event, index):
    """Plan one realized frame and check the previously admitted work envelope."""
    plan = trapped_charge_runtime.plan(
        {**parsed, "events": {**parsed["events"], "frames": [event]}}
    )
    bound = parsed["trap_work_envelope"]["frames"][index]
    frame = plan["frames"][0]
    if (
        frame["frame_id"] != bound["frame_id"]
        or frame["segment_count"] > bound["segment_count_bound"]
    ):
        raise ArithmeticError("Actual trap frame exceeds the admitted complete-grid work bound")
    for phase, key in (("gap", "gap_segments_bound"), ("exposure", "exposure_segments_bound")):
        if sum(s["phase"] == phase for s in frame["segments"]) > bound[key]:
            raise ArithmeticError("Actual trap phase exceeds the admitted work bound")
    return plan


def complete_schedule(parsed, plans):
    """Combine realized plans with the exact fixed-runtime schedule identity."""
    frames = [plan["frames"][0] for plan in plans]
    height, width = parsed["base"]["size"]
    pixels = height * width
    counts = [frame["segment_count"] for frame in frames]
    maximum, total = max(counts), sum(counts)
    temporary = max(
        plan["resources"]["trap_segment_working_bytes_estimate"]
        - frame["segment_count"] * pixels * 8 * 3
        - pixels * 32 * 8
        - frame["segment_count"] * height * 256
        for plan, frame in zip(plans, frames, strict=True)
    )
    result = {
        key: plans[0][key]
        for key in (
            "schema",
            "clock",
            "integration_method",
            "effective_max_step_ns",
            "row_batching",
        )
    }
    result["frames"] = frames
    resources = {
        "trap_segment_count": total,
        "trap_maximum_segments_per_frame": maximum,
        "trap_pixel_substeps": total * pixels,
        "trap_effective_max_step_ns": result["effective_max_step_ns"],
        "trap_segment_working_bytes_estimate": maximum * pixels * 8 * 3
        + pixels * 32 * 8
        + total * height * 256
        + temporary,
        "trap_timing_evidence_bytes_estimate": len(frames) * maximum * height * 24
        + len(frames) * maximum * 8
        + len(frames) * 8,
    }
    admitted = parsed["trap_work_envelope"]["resources"]
    if any(resources[key] > admitted[key] for key in resources):
        raise ArithmeticError("Actual trap sequence exceeds the admitted whole-job bound")
    return {**result, "schedule_sha256": digest(result), "resources": resources}


def add_resources(parsed):
    """Replace initial-scaffold resource counts by the whole-grid upper bound."""
    envelope = admission(parsed)
    old = parsed["resources"]
    resources = envelope["resources"]
    evidence_delta = (
        resources["trap_timing_evidence_bytes_estimate"]
        - old["trap_timing_evidence_bytes_estimate"]
    )
    working_delta = (
        resources["trap_segment_working_bytes_estimate"]
        - old["trap_segment_working_bytes_estimate"]
    )
    if min(evidence_delta, working_delta) < 0:
        raise ArithmeticError("Trap admission bound underestimates the initial schedule")
    parsed["resources"] = {
        **old,
        **resources,
        "output_array_bytes_estimate": old["output_array_bytes_estimate"] + evidence_delta,
        "working_array_bytes_estimate": old["working_array_bytes_estimate"]
        + 2 * evidence_delta
        + working_delta,
    }
    parsed["trap_work_envelope"] = envelope
    parsed.pop("trap_schedule")  # Initial scaffold is never actual timing evidence.
    # Mean generation is bounded jointly across gap and exposure, not as two
    # unrelated admissible values which could exceed the core's total bound.
    dark = parsed["generation_patterns"][1]
    duration = (
        max(f["maximum_gap_ns"] for f in envelope["frames"]) + envelope["maximum_exposure_ns"]
    )
    with np.errstate(over="ignore", invalid="ignore"):
        mean = dark * (duration * 1e-9)
    if not np.isfinite(mean).all() or np.any(mean > trapped_charge.MAX_MEAN_E):
        raise ValueError("Reachable trap gap plus exposure dark generation exceeds charge bound")
    return parsed
