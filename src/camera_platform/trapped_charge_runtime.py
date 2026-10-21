"""Bounded native-row chronological optical inputs for authored trap kinetics.

This module does not replace optical transport or the trap solver. It preserves
PWM edges and scene knots before handing short interval photo means to the
explicit first-order charge model. No frame-average illumination is substituted.
"""

from __future__ import annotations

from bisect import bisect_right

import numpy as np

from . import electrical, trapped_charge
from .periodic_light import integrate_linear_basis
from .registry import digest


def _intervals(start, end, maximum_step, knots, waveform):
    """Make at most MAX_SEGMENTS intervals without expanding unbounded cycles."""
    result = []
    cursor = int(start)
    while cursor < end:
        if len(result) >= trapped_charge.MAX_SEGMENTS:
            raise ValueError("Trap chronology exceeds bounded segments per frame")
        stop = min(end, cursor + maximum_step)
        index = bisect_right(knots, cursor)
        if index < len(knots):
            stop = min(stop, knots[index])
        if (
            waveform is not None and waveform["kind"] == "pwm"
            and 0 < waveform["on_time_ns"] < waveform["period_ns"]
        ):
            phase = (cursor - waveform["phase_origin_ns"]) % waveform["period_ns"]
            delta = (
                waveform["on_time_ns"] - phase
                if phase < waveform["on_time_ns"] else waveform["period_ns"] - phase
            )
            stop = min(stop, cursor + delta)
        if stop <= cursor:
            raise ValueError("Trap chronology did not advance at an integer-ns boundary")
        result.append((cursor, stop))
        cursor = stop
    return result


def plan(parsed):
    """Read-only exact-clock forecast before optical materialization or RNG."""
    size = tuple(parsed["base"]["size"])
    pixels = int(np.prod(size))
    maximum_step = trapped_charge.effective_max_step_ns(
        parsed["program"]["retained_charge_model"]
    )
    knots = [knot["time_ns"] for knot in parsed["timeline"]["knots"]]
    # A second light source may change its linear/ZOH basis at a different
    # instant. A trap step must never smear that boundary across one interval.
    if "unmodulated" in parsed:
        knots = sorted(set(knots) | {
            knot["time_ns"] for knot in parsed["unmodulated"]["timeline"]["knots"]
        })
    waveform = parsed["program"]["waveform"]
    frames = []
    maximum_segments = 0
    total_pixel_substeps = 0
    plan_storage = 0
    temporary_plan_peak = 0
    for event in parsed["events"]["frames"]:
        segments = []
        for phase in ("gap", "exposure"):
            starts = (
                [a - b for a, b in zip(event["row_open_ns"], event["row_gap_ns"], strict=True)]
                if phase == "gap" else event["row_open_ns"]
            )
            ends = event["row_open_ns"] if phase == "gap" else event["row_transfer_ns"]
            minimum_count = max(
                ((b - a + maximum_step - 1) // maximum_step
                 for a, b in zip(starts, ends, strict=True)), default=0
            )
            if (len(segments) + minimum_count) * pixels > trapped_charge.MAX_PIXEL_SUBSTEPS:
                raise ValueError("Trap frame exceeds bounded pixel-substep work")
            if not minimum_count:
                continue
            per_row = []
            interval_count = 0
            for a, b in zip(starts, ends, strict=True):
                row = _intervals(
                    a, b, maximum_step, knots if phase == "exposure" else [],
                    waveform if phase == "exposure" else None,
                )
                interval_count += len(row)
                if (len(segments) + len(row)) * pixels > trapped_charge.MAX_PIXEL_SUBSTEPS:
                    raise ValueError("Trap frame exceeds bounded pixel-substep work")
                # Conservative Python plan storage, guarded while constructing,
                # before all native-row lists or photon arrays are allocated.
                if plan_storage + interval_count * 512 > trapped_charge.MAX_WORKING_BYTES:
                    raise ValueError("Trap interval plan exceeds bounded working memory")
                per_row.append(row)
            # Rows have independent reservoirs. Batch each row's nth interval;
            # padding advances no time and admits no photocharge for that row.
            count = max((len(row) for row in per_row), default=0)
            if len(segments) + count > trapped_charge.MAX_SEGMENTS:
                raise ValueError("Trap gap plus exposure exceeds bounded segments per frame")
            temporary_plan_peak = max(temporary_plan_peak, interval_count * 512)
            plan_storage += count * size[0] * 256
            if plan_storage + temporary_plan_peak > trapped_charge.MAX_WORKING_BYTES:
                raise ValueError("Trap complete interval plan exceeds bounded working memory")
            for index in range(count):
                pairs = [
                    row[index] if index < len(row) else (end, end)
                    for row, end in zip(per_row, ends, strict=True)
                ]
                segments.append({
                    "phase": phase,
                    "start_ns": [int(pair[0]) for pair in pairs],
                    "end_ns": [int(pair[1]) for pair in pairs],
                })
        count = len(segments)
        if not count or count > trapped_charge.MAX_SUBSTEPS:
            raise ValueError("Trap frame requires bounded nonempty chronological integration")
        work = count * pixels
        if work > trapped_charge.MAX_PIXEL_SUBSTEPS:
            raise ValueError("Trap frame exceeds bounded pixel-substep work")
        maximum_segments = max(maximum_segments, count)
        total_pixel_substeps += work
        frames.append({"frame_id": event["frame_id"], "segments": segments,
                       "segment_count": count, "pixel_substeps": work})
    # Generated means plus normalized/core copies are alive for one frame.
    working = (maximum_segments * pixels * 8 * 3 + pixels * 32 * 8
               + plan_storage + temporary_plan_peak)
    if working > trapped_charge.MAX_WORKING_BYTES:
        raise ValueError("Trap segment inputs exceed bounded working memory")
    result = {
        "schema": "camera_trap_segment_schedule_v1",
        "clock": parsed["control"]["clock"],
        "integration_method": trapped_charge.METHOD,
        "effective_max_step_ns": maximum_step,
        "row_batching": "independent_row_chronology_zero_duration_padding",
        "frames": frames,
    }
    return {
        **result,
        "schedule_sha256": digest(result),
        "resources": {
            "trap_segment_count": sum(f["segment_count"] for f in frames),
            "trap_maximum_segments_per_frame": maximum_segments,
            "trap_pixel_substeps": total_pixel_substeps,
            "trap_effective_max_step_ns": maximum_step,
            "trap_segment_working_bytes_estimate": working,
            "trap_timing_evidence_bytes_estimate": len(frames) * maximum_segments * size[0] * 24
            + len(frames) * maximum_segments * 8 + len(frames) * 8,
        },
    }


def native_rates(rates, cfa):
    return np.take_along_axis(rates, cfa[None, ..., None], axis=-1)[..., 0]


def frame_segments(parsed, frame_plan, rates, unmodulated_rates=None):
    """Actual short-interval means; both sources feed one trap population."""
    timeline, control = parsed["timeline"], parsed["control"]
    knots = [knot["time_ns"] for knot in timeline["knots"]]
    ambient = parsed.get("unmodulated")
    if (ambient is None) != (unmodulated_rates is None):
        raise ValueError("Trap segment illumination requires both ambient timeline and rates")
    ambient_knots = ([knot["time_ns"] for knot in ambient["timeline"]["knots"]]
                     if ambient is not None else None)
    waveform = parsed["program"]["waveform"] or {
        "schema": "camera_light_waveform_v1", "kind": "constant", "level": 1,
        "clock_id": control["clock"]["clock_id"],
    }
    ambient_waveform = {
        "schema": "camera_light_waveform_v1", "kind": "constant", "level": 1,
        "clock_id": control["clock"]["clock_id"],
    }
    shape = tuple(parsed["base"]["size"])
    segments = []
    component_sums = ({"modulated": np.zeros(shape), "unmodulated": np.zeros(shape)}
                      if ambient is not None else None)
    for segment in frame_plan["segments"]:
        starts, ends = segment["start_ns"], segment["end_ns"]
        duration = np.asarray(ends, dtype=np.int64) - np.asarray(starts, dtype=np.int64)
        photo = np.zeros(shape)
        modulated_photo = np.zeros(shape) if ambient is not None else None
        unmodulated_photo = np.zeros(shape) if ambient is not None else None
        if segment["phase"] == "exposure":
            for row, (start, end) in enumerate(zip(starts, ends, strict=True)):
                if start == end:
                    continue
                left_index = bisect_right(knots, start) - 1
                if left_index < 0 or left_index + 1 >= len(knots) or end > knots[left_index + 1]:
                    raise ValueError("Trap interval leaves its declared optical scene support")
                a, b = integrate_linear_basis(
                    waveform, start, end, knots[left_index], knots[left_index + 1],
                    timeline["interpolation"],
                )
                led = (
                    a * rates[parsed["scene_indices"][left_index], row]
                    + b * rates[parsed["scene_indices"][left_index + 1], row]
                )
                if ambient is not None:
                    ambient_index = bisect_right(ambient_knots, start) - 1
                    if (ambient_index < 0 or ambient_index + 1 >= len(ambient_knots)
                            or end > ambient_knots[ambient_index + 1]):
                        raise ValueError(
                            "Trap interval leaves unmodulated optical scene support"
                        )
                    aa, bb = integrate_linear_basis(
                        ambient_waveform,
                        start, end, ambient_knots[ambient_index],
                        ambient_knots[ambient_index + 1],
                        ambient["timeline"]["interpolation"],
                    )
                    ambient_e = (
                        aa * unmodulated_rates[ambient["scene_indices"][ambient_index], row]
                        + bb * unmodulated_rates[ambient["scene_indices"][ambient_index + 1], row]
                    )
                    modulated_photo[row] = led
                    unmodulated_photo[row] = ambient_e
                    photo[row] = led + ambient_e
                else:
                    photo[row] = led
        if (
            not np.isfinite(photo).all() or np.any(photo < 0)
            or np.any(photo > electrical.MAX_CHARGE_E)
        ):
            raise ValueError("Trap segment generation exceeds the finite charge domain")
        segments.append({"phase": segment["phase"], "duration_ns": duration,
                         "expected_photo_e": photo})
        if ambient is not None:
            # The trap core accepts only total expected_photo_e. These detached
            # optical ledgers are diagnostics for the independent closure check.
            component_sums["modulated"] += modulated_photo
            component_sums["unmodulated"] += unmodulated_photo
    if ambient is not None:
        return segments, component_sums
    return segments


def evidence(schedule, height):
    frames = schedule["frames"]
    count = schedule["resources"]["trap_maximum_segments_per_frame"]
    starts = np.zeros((len(frames), count, height), dtype=np.int64)
    ends = np.zeros_like(starts)
    phase = np.full((len(frames), count), -1, dtype=np.int64)
    counts = np.asarray([f["segment_count"] for f in frames], dtype=np.int64)
    for fi, frame in enumerate(frames):
        for si, segment in enumerate(frame["segments"]):
            starts[fi, si] = segment["start_ns"]
            ends[fi, si] = segment["end_ns"]
            phase[fi, si] = 0 if segment["phase"] == "gap" else 1
    return {
        "trap_segment_start_ns": starts,
        "trap_segment_end_ns": ends,
        "trap_segment_duration_ns": ends - starts,
        "trap_segment_phase": phase,
        "trap_segment_count": counts,
    }


def summary(schedule):
    return {
        key: value for key, value in schedule.items() if key != "frames"
    } | {
        "phase_codes": {"padding": -1, "gap": 0, "exposure": 1},
        "frame_ids": [frame["frame_id"] for frame in schedule["frames"]],
        "segment_counts": [frame["segment_count"] for frame in schedule["frames"]],
        "time_approximation": "interval_mean_generation_with_bounded_first_order_split",
        "illumination_boundaries": "exact_integer_ns_scene_knots_and_pwm_edges",
        "exact_ctmc": False,
    }
