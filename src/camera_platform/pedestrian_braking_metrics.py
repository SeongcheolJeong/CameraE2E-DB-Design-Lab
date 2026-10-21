"""Causal tracks and conservative, event-level braking opportunity metrics.

Ground truth is consumed by the evaluator only. Interpolated annotations are
never presented as independent measurements or supplied to the tracker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment


def iou(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    extent = np.maximum(0, np.minimum(a[2:], b[2:]) - np.maximum(a[:2], b[:2]))
    overlap = float(np.prod(extent))
    union = float(np.prod(a[2:] - a[:2]) + np.prod(b[2:] - b[:2]) - overlap)
    return overlap / union if union > 0 else 0.0


def required_distance(speed_m_s, *, remaining_delay_s=0.3, deceleration_m_s2=6.0, buffer_m=2.0):
    values = (speed_m_s, remaining_delay_s, deceleration_m_s2, buffer_m)
    if (
        not all(math.isfinite(x) for x in values)
        or min(speed_m_s, remaining_delay_s, buffer_m) < 0
        or deceleration_m_s2 <= 0
    ):
        raise ValueError(
            "Finite, nonnegative braking conditions and positive deceleration required."
        )
    return speed_m_s * remaining_delay_s + speed_m_s**2 / (2 * deceleration_m_s2) + buffer_m


@dataclass
class CausalTracker:
    """Constant-velocity prediction + Hungarian IoU, with no GT input."""

    association_iou: float = 0.3
    max_missed: int = 2
    consecutive_required: int = 3
    maximum_frame_gap_s: float = 0.125
    expected_output_timestamps: list | None = None
    last_schedule_index: int | None = None
    tracks: dict = field(default_factory=dict)
    next_id: int = 1
    last_time: float | None = None

    def update(self, timestamp_s, detections):
        t = float(timestamp_s)
        if not math.isfinite(t) or (self.last_time is not None and t <= self.last_time):
            raise ValueError("Frame timestamps must increase strictly.")
        boxes = []
        for detection in detections:
            box = np.asarray(detection["bbox"], float)
            if box.shape != (4,) or not np.isfinite(box).all() or np.any(box[2:] <= box[:2]):
                raise ValueError("Detection must have a valid xyxy box.")
            boxes.append(box)
        missed_schedule = False
        if self.expected_output_timestamps is not None:
            schedule = np.asarray(self.expected_output_timestamps, float)
            if not np.isfinite(schedule).all() or np.any(np.diff(schedule) <= 0):
                raise ValueError("Expected output timestamps must increase strictly.")
            indices = np.flatnonzero(np.abs(schedule - t) < 1e-6)
            if len(indices) != 1:
                raise ValueError("Observation does not belong to the declared output schedule.")
            index = int(indices[0])
            missed_schedule = self.last_schedule_index is not None and index != self.last_schedule_index + 1
            self.last_schedule_index = index
        elif self.last_time is not None:
            missed_schedule = t - self.last_time > self.maximum_frame_gap_s
        if missed_schedule:
            # A missing observation can never count as continuous recognition.
            self.tracks.clear()
        self.last_time = t
        ids = list(self.tracks)
        predicted = [
            self.tracks[k]["box"] + self.tracks[k]["velocity"] * (t - self.tracks[k]["observed_s"])
            for k in ids
        ]
        matched = {}
        if ids and boxes:
            overlaps = np.array([[iou(a, b) for b in boxes] for a in predicted])
            # Gate BEFORE assignment; an inadmissible pair cannot steal a match.
            cost = np.where(overlaps >= self.association_iou, 1 - overlaps, 1e6)
            rr, cc = linear_sum_assignment(cost)
            matched = {ids[r]: int(c) for r, c in zip(rr, cc, strict=True) if cost[r, c] < 1e6}
        used, output = set(matched.values()), []
        for key in ids:
            tr = self.tracks[key]
            if key not in matched:
                tr["missed"] += 1
                tr["streak"] = 0
                if tr["missed"] >= self.max_missed:
                    del self.tracks[key]
                continue
            number = matched[key]
            box = boxes[number]
            tr["velocity"] = (box - tr["box"]) / (t - tr["observed_s"])
            tr.update(box=box, observed_s=t, missed=0, streak=tr["streak"] + 1)
            output.append(self._output(key, tr, detections[number]))
        for number, box in enumerate(boxes):
            if number in used:
                continue
            key = self.next_id
            self.next_id += 1
            tr = {"box": box, "velocity": np.zeros(4), "observed_s": t, "missed": 0, "streak": 1}
            self.tracks[key] = tr
            output.append(self._output(key, tr, detections[number]))
        return sorted(output, key=lambda x: x["track_id"])

    def _output(self, key, track, detection):
        return {
            "track_id": key,
            "bbox": track["box"].tolist(),
            "score": float(detection["score"]),
            "streak": track["streak"],
            "stable": track["streak"] >= self.consecutive_required,
        }


def match_truth(tracks, truth, threshold=0.5):
    """One-to-one scoring against visible person boxes at a measured anchor."""
    if not tracks or not truth:
        return {}
    overlap = np.array([[iou(tr["bbox"], gt["bbox"]) for gt in truth] for tr in tracks])
    cost = np.where(overlap >= threshold, 1 - overlap, 1e6)
    rows, cols = linear_sum_assignment(cost)
    return {
        tracks[r]["track_id"]: truth[c]["instance_id"]
        for r, c in zip(rows, cols, strict=True)
        if cost[r, c] < 1e6
    }


def evaluate_event(event, frames, *, processing_latency_s=0.1, braking=None):
    """Success is confirmed only over an entire bracketing GT interval.

    A stable track must match the same person at both measured bounding
    anchors. Occlusion/unannotated endpoints remain unresolved. Missed and
    uncertain events stay in the hazard denominator. Distance is already
    measured from the vehicle front boundary along the recorded path.
    """
    if processing_latency_s < 0 or not math.isfinite(processing_latency_s):
        raise ValueError("Latency is a finite nonnegative assumption.")
    braking = braking or {}
    required_distance(0.0, **braking)
    if not frames or any(
        b["timestamp_s"] <= a["timestamp_s"] for a, b in zip(frames[:-1], frames[1:], strict=True)
    ):
        raise ValueError("Evaluation requires ordered nonempty frames.")
    if any("result_available_ns" in f for f in frames):
        if not all("result_available_ns" in f for f in frames):
            raise ValueError("Every frame must declare result availability together.")
        times = [f["result_available_ns"] / 1e9 for f in frames]
        if any(t < f["timestamp_s"] for t, f in zip(times, frames, strict=True)) or any(b <= a for a,b in zip(times,times[1:])):
            raise ValueError("Availability must follow capture and increase strictly.")
    anchors = sorted(event["anchors"], key=lambda a: a["timestamp_s"])
    target = event.get("target_instance_id")
    by_token = {f["token"]: f for f in frames}
    anchor_matches = {}
    false_ids, uncertain_ids = set(), set()
    unreviewed_intervals = []
    for anchor in anchors:
        fr = by_token.get(anchor["frame_token"])
        if fr is not None:
            anchor_matches[anchor["frame_token"]] = match_truth(fr["tracks"], anchor["persons"])
    stable_frames = [fr for fr in frames if any(t["stable"] for t in fr["tracks"])]
    candidates = []
    for fr in stable_frames:
        for tr in fr["tracks"]:
            if not tr["stable"]:
                continue
            preceding = [a for a in anchors if a["timestamp_s"] <= fr["timestamp_s"]]
            following = [a for a in anchors if a["timestamp_s"] >= fr["timestamp_s"]]
            if not preceding or not following:
                uncertain_ids.add(tr["track_id"])
                unreviewed_intervals.append(
                    {
                        "track_id": tr["track_id"],
                        "timestamp_s": fr["timestamp_s"],
                        "reason": "outside_measured_annotation_anchors",
                    }
                )
                continue
            lo, hi = preceding[-1], following[0]
            if event.get("output_annotation_policy") == "bracketing_boxes":
                # Sparse HDR cadence need not include every annotation frame.
                # GT is evaluator-only; require overlap with BOTH measured boxes.
                m0, m1 = match_truth(fr["tracks"], lo["persons"]), match_truth(fr["tracks"], hi["persons"])
            else:
                m0 = anchor_matches.get(lo["frame_token"], {})
                m1 = anchor_matches.get(hi["frame_token"], {})
            instance = m0.get(tr["track_id"])
            if instance is None and tr["track_id"] not in m1 and lo is not hi:
                # Only fully annotated, two-anchor absence is confirmed FP.
                if lo.get("annotation_complete", False) and hi.get("annotation_complete", False):
                    false_ids.add(tr["track_id"])
                else:
                    uncertain_ids.add(tr["track_id"])
                continue
            if instance != m1.get(tr["track_id"]) or instance is None:
                uncertain_ids.add(tr["track_id"])
                continue
            if instance != target:
                continue
            if lo is hi:
                # Even exact anchors do not backdate the third observation.
                interval = [fr["timestamp_s"], fr["timestamp_s"]]
            else:
                interval = [lo["timestamp_s"], hi["timestamp_s"]]
            latency = (fr["result_available_ns"] / 1e9 - fr["timestamp_s"]
                       if "result_available_ns" in fr else processing_latency_s)
            interval = [x + latency for x in interval]
            interval[0] = max(interval[0], fr["timestamp_s"] + latency)
            candidates.append((interval, tr["track_id"], fr["timestamp_s"], latency))
    motion = event["motion"]
    mt = np.array([x["timestamp_s"] for x in motion])
    distances = np.array([x["remaining_distance_m"] for x in motion])
    speeds = np.array([x["speed_m_s"] for x in motion])
    if (
        len(mt) < 2
        or not np.isfinite(mt).all()
        or not np.isfinite(distances).all()
        or not np.isfinite(speeds).all()
        or np.any(np.diff(mt) <= 0)
        or np.any(speeds < 0)
    ):
        raise ValueError("Recorded motion requires ordered finite samples and nonnegative speed.")
    result = {
        "event_id": event["id"],
        "risk": event["risk"],
        "processing_latency_s": processing_latency_s,
        "availability_source": "per_frame" if "result_available_ns" in frames[0] else "legacy_constant",
        "confirmed_false_track_ids": sorted(false_ids),
        # A valid association earlier does not resolve an unannotated tail or
        # a later identity mismatch. Keep these possible false-track events.
        "uncertain_track_ids": sorted(uncertain_ids - false_ids),
        "unreviewed_track_intervals": unreviewed_intervals,
        "status": "missed" if event["risk"] else "nonrisk",
        "recognition_time_interval_s": None,
        "braking_margin_interval_m": None,
        "annotation_resolution_s": event["annotation_resolution_s"],
    }
    if candidates:
        interval, track_id, third_time, selected_latency = min(candidates, key=lambda x: x[0][1])
        valid_samples = np.array([bool(x.get("collision_corridor_valid", True)) for x in motion])
        # Include bracketing motion samples; do not interpolate a collision
        # distance through a corridor entry/exit or an unavailable GT region.
        corridor_slice = slice(max(0, int(np.searchsorted(mt, interval[0], side="right"))-1),
                               min(len(mt), int(np.searchsorted(mt, interval[1], side="left"))+1))
        if interval[0] < mt[0] or interval[1] > mt[-1]:
            result["status"] = "uncertain_outside_recorded_motion"
        elif not np.all(valid_samples[corridor_slice]):
            result["status"] = "uncertain_corridor_entry_exit"
        else:
            # Include internal samples: varying speed need not be monotonic.
            times = np.unique(np.r_[interval, mt[(mt >= interval[0]) & (mt <= interval[1])]])
            margin = [
                float(np.interp(t, mt, distances))
                - required_distance(float(np.interp(t, mt, speeds)), **braking)
                for t in times
            ]
            result.update(
                recognition_time_interval_s=interval,
                braking_margin_interval_m=[min(margin), max(margin)],
                recognition_distance_interval_m=[
                    float(np.interp(interval[1], mt, distances)),
                    float(np.interp(interval[0], mt, distances)),
                ],
                stable_track_id=track_id,
                third_observation_s=third_time,
                status="success"
                if min(margin) > 0
                else ("late" if max(margin) <= 0 else "uncertain"),
            )
            # Earliest subsequent braking-boundary crossing limits available
            # processing time. An unobserved boundary is right-censored.
            from scipy.optimize import brentq

            upper_without_latency = interval[1] - selected_latency
            remaining_times = np.r_[upper_without_latency, mt[mt > upper_without_latency]]

            def margin_at(t):
                return float(np.interp(t, mt, distances)) - required_distance(
                    float(np.interp(t, mt, speeds)), **braking
                )

            deadline, censored = upper_without_latency, False
            if margin_at(upper_without_latency) > 0:
                deadline, censored = mt[-1], True
                for left, right in zip(remaining_times[:-1], remaining_times[1:], strict=True):
                    if margin_at(right) <= 0:
                        deadline = brentq(margin_at, left, right)
                        censored = False
                        break
            result["maximum_processing_latency_s"] = max(0.0, deadline - upper_without_latency)
            result["processing_latency_limit_right_censored"] = censored
    elif event["risk"] and uncertain_ids:
        result["status"] = "uncertain"
    # Do not count predicted track persistence as observed recognition.
    hits = [
        any(t["stable"] and t["track_id"] == result.get("stable_track_id") for t in fr["tracks"])
        for fr in frames
    ]
    gaps, start = [], None
    for fr, hit in zip(frames, hits, strict=True):
        if not hit and start is None:
            start = fr["timestamp_s"]
        elif hit and start is not None:
            gaps.append(fr["timestamp_s"] - start)
            start = None
    if start is not None:
        gaps.append(frames[-1]["timestamp_s"] - start)
    result["longest_unrecognized_interval_s"] = max(gaps, default=0.0)
    result["unrecognized_intervals_s"] = gaps
    first_hit = next((i for i, hit in enumerate(hits) if hit), None)
    dropouts, beginning = [], None
    if first_hit is not None:
        for fr, hit in zip(frames[first_hit:], hits[first_hit:], strict=True):
            if not hit and beginning is None:
                beginning = fr["timestamp_s"]
            if hit and beginning is not None:
                dropouts.append(fr["timestamp_s"] - beginning)
                beginning = None
        if beginning is not None:
            dropouts.append(frames[-1]["timestamp_s"] - beginning)
    result["recognition_dropout_count"] = len(dropouts)
    result["longest_recognition_dropout_s"] = max(dropouts, default=0.0)
    return result


def compare_candidate(baseline, candidate):
    """Pair ALL hazard events and seeds; repeated frames never inflate N."""

    def key(r):
        return r["event_id"], r["seed"]

    b, c = {key(x): x for x in baseline}, {key(x): x for x in candidate}
    if len(b) != len(baseline) or len(c) != len(candidate) or b.keys() != c.keys():
        raise ValueError("Comparisons require exactly paired unique event/seed records.")
    regressions, improvements = [], []
    fp_increase = False
    for k in b:
        if b[k]["risk"] != c[k]["risk"]:
            raise ValueError("Risk classification changed between candidates.")
        if b[k]["risk"]:
            if b[k]["status"] == "success" and c[k]["status"] != "success":
                regressions.append(k)
            if b[k]["status"] != "success" and c[k]["status"] == "success":
                improvements.append(k)
        # Candidate upper FP bound must not exceed baseline confirmed count.
        fp_increase |= len(
            set(c[k]["confirmed_false_track_ids"]) | set(c[k]["uncertain_track_ids"])
        ) > len(b[k]["confirmed_false_track_ids"])
    ids = sorted({k[0] for k in b if b[k]["risk"]})
    improved_ids = [
        eid for eid in ids if all((eid, seed) in improvements for event, seed in b if event == eid)
    ]
    paired = []
    for k in b:
        if not b[k]["risk"]:
            continue
        before = b[k].get("recognition_time_interval_s")
        after = c[k].get("recognition_time_interval_s")
        # No survivor-only average: each miss/uncertain case remains explicit.
        paired.append(
            {
                "event_id": k[0],
                "seed": k[1],
                "baseline_status": b[k]["status"],
                "candidate_status": c[k]["status"],
                "earlier_recognition_interval_s": [before[0] - after[1], before[1] - after[0]]
                if before is not None and after is not None
                else None,
                "baseline_braking_margin_interval_m": b[k].get("braking_margin_interval_m"),
                "candidate_braking_margin_interval_m": c[k].get("braking_margin_interval_m"),
            }
        )
    return {
        "hazard_event_count": len(ids),
        "robustly_improved_event_ids": improved_ids,
        "regressions": regressions,
        "paired_hazard_records_including_misses": paired,
        "false_positive_nonincrease_confirmed": not fp_increase,
        "recommend_change": bool(improved_ids) and not regressions and not fp_increase,
        "baseline_confirmed_success_events": sum(
            all(b[k]["status"] == "success" for k in b if k[0] == eid) for eid in ids
        ),
        "candidate_confirmed_success_events": sum(
            all(c[k]["status"] == "success" for k in c if k[0] == eid) for eid in ids
        ),
    }
