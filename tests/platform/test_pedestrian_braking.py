"""Causal recognition, timing, missing events and fixed-cohort decision tests."""

import copy

import numpy as np
import pytest

from camera_platform.pedestrian_braking_inputs import (
    allocate_events,
    collision_path_coordinate,
    rotation,
)
from camera_platform.pedestrian_braking_metrics import (
    CausalTracker,
    compare_candidate,
    evaluate_event,
    required_distance,
)


def detection():
    return {"bbox": [10, 10, 25, 50], "score": 0.8}


def sequence():
    tracker = CausalTracker()
    return [
        {"token": str(i), "timestamp_s": i / 12, "tracks": tracker.update(i / 12, [detection()])}
        for i in range(13)
    ]


def event(distance=80):
    return {
        "id": "hazard",
        "risk": True,
        "target_instance_id": "person-1",
        "annotation_resolution_s": 0.5,
        "anchors": [
            {
                "frame_token": str(i),
                "timestamp_s": i / 12,
                "annotation_complete": False,
                "persons": [{"instance_id": "person-1", "bbox": detection()["bbox"]}],
            }
            for i in (0, 6, 12)
        ],
        "motion": [
            {"timestamp_s": i / 12, "remaining_distance_m": distance - i, "speed_m_s": 10}
            for i in range(13)
        ],
    }


def test_stability_is_third_observation_not_first_or_prediction():
    frames = sequence()
    assert [f["tracks"][0]["stable"] for f in frames[:3]] == [False, False, True]
    result = evaluate_event(event(), frames)
    assert result["status"] == "success"
    assert result["third_observation_s"] == pytest.approx(2 / 12)
    assert result["recognition_time_interval_s"] == pytest.approx([2 / 12 + 0.1, 0.5 + 0.1])


def test_tracking_frame_gap_and_two_misses_reset_stability():
    tracker = CausalTracker()
    old_id = tracker.update(0, [detection()])[0]["track_id"]
    tracker.update(0.08, [])
    assert tracker.update(0.16, [detection()])[0]["streak"] == 1
    assert tracker.update(0.4, [detection()])[0]["track_id"] != old_id
    tracker.update(0.48, [])
    tracker.update(0.56, [])
    assert not tracker.tracks


@pytest.mark.parametrize("speed", [-1, float("nan"), float("inf")])
def test_invalid_braking_speed_rejected(speed):
    with pytest.raises(ValueError):
        required_distance(speed)


def test_declared_50kph_example_and_no_latency_double_count():
    assert required_distance(50 / 3.6) == pytest.approx(22.2417695)
    r = evaluate_event(event(), sequence(), processing_latency_s=0.2)
    assert r["recognition_time_interval_s"][0] == pytest.approx(2 / 12 + 0.2)


def test_late_missed_and_annotation_boundary_remain_in_denominator():
    assert evaluate_event(event(5), sequence())["status"] == "late"
    missed = [{"token": str(i), "timestamp_s": i / 12, "tracks": []} for i in range(13)]
    assert evaluate_event(event(), missed)["status"] == "missed"
    # With incomplete annotation, a stable unmatched track is uncertain, not TP/FP.
    e = event()
    for a in e["anchors"]:
        a["persons"] = []
    r = evaluate_event(e, sequence())
    assert r["status"] == "uncertain"
    assert not r["confirmed_false_track_ids"] and r["uncertain_track_ids"]


def test_future_truth_cannot_enter_the_tracker():
    tracker = CausalTracker()
    with pytest.raises(TypeError):
        tracker.update(0, [detection()], ground_truth=[detection()])
    tracker.update(1, [])
    with pytest.raises(ValueError):
        tracker.update(0.5, [])


def records(status):
    return [
        {
            "event_id": "hazard",
            "seed": seed,
            "risk": True,
            "status": status,
            "confirmed_false_track_ids": [],
            "uncertain_track_ids": [],
        }
        for seed in range(5)
    ]


def test_all_seeds_improvement_and_no_success_regressions():
    assert compare_candidate(records("missed"), records("success"))["recommend_change"]
    candidate = records("success")
    candidate[-1]["status"] = "uncertain"
    assert not compare_candidate(records("missed"), candidate)["recommend_change"]
    assert not compare_candidate(records("success"), records("late"))["recommend_change"]


def test_false_positive_uncertainty_prevents_recommendation():
    candidate = records("success")
    candidate[0]["uncertain_track_ids"] = [999]
    assert not compare_candidate(records("missed"), candidate)["recommend_change"]


def test_paired_cohort_and_no_seed_inflation():
    result = compare_candidate(records("missed"), records("success"))
    assert result["hazard_event_count"] == 1
    with pytest.raises(ValueError):
        compare_candidate(records("missed"), records("success")[:-1])
    with pytest.raises(ValueError):
        compare_candidate(records("missed"), records("success") + records("success")[:1])


def test_cohort_logs_are_independent_and_no_shortfall_relabeling():
    events = [
        {"id": str(i), "risk": i < 8, "log_token": str(i), "missing_source_files": []}
        for i in range(12)
    ]
    selected = allocate_events({"events": events})
    assert len(selected) == len({e["log_token"] for e in selected}) == 12
    assert sum(e["risk"] for e in selected if e["split"] == "evaluation") == 4
    duplicate = copy.deepcopy(events)
    duplicate[-1]["log_token"] = duplicate[-2]["log_token"]
    with pytest.raises(ValueError, match="dataset_shortfall"):
        allocate_events({"events": duplicate})


def test_quaternion_order():
    assert np.allclose(rotation([1, 0, 0, 0]), np.eye(3))
    assert np.allclose(
        rotation([np.cos(np.pi / 4), 0, 0, np.sin(np.pi / 4)]) @ [1, 0, 0], [0, 1, 0]
    )


def test_collision_distance_is_recorded_arc_not_euclidean_and_no_extrapolation():
    path = [[0, 0], [10, 0], [10, 10]]
    assert collision_path_coordinate(path, [10, 8], 0.5) == pytest.approx(15.0)
    assert collision_path_coordinate(path, [10, 30], 0.5) is None
    assert collision_path_coordinate([[0, 0], [10, 0]], [8, 5], 0.5) is None
    with pytest.raises(ValueError):
        collision_path_coordinate(path, [10, 8], -1)


def test_unreviewed_tail_is_not_resolved_by_earlier_true_person_association():
    frames = sequence()
    e = event()
    e["anchors"] = e["anchors"][:2]
    result = evaluate_event(e, frames)
    assert result["status"] == "success"
    assert result["uncertain_track_ids"] == [frames[-1]["tracks"][0]["track_id"]]
    assert result["unreviewed_track_intervals"]


def test_nonfinite_motion_and_nonmonotonic_timestamps_rejected():
    e = event()
    e["motion"][2]["speed_m_s"] = float("nan")
    with pytest.raises(ValueError, match="Recorded motion"):
        evaluate_event(e, sequence())
    e = event()
    e["motion"][2]["timestamp_s"] = e["motion"][1]["timestamp_s"]
    with pytest.raises(ValueError, match="Recorded motion"):
        evaluate_event(e, sequence())


def test_hazard_logs_reserved_before_nonrisk_controls():
    hazard = [
        {"id": "h" + str(i), "risk": True, "log_token": str(i), "missing_source_files": []}
        for i in range(8)
    ]
    nonrisk = [
        {"id": "n" + str(i), "risk": False, "log_token": str(i), "missing_source_files": []}
        for i in range(12)
    ]
    chosen = allocate_events({"events": nonrisk + hazard})
    assert len({e["log_token"] for e in chosen}) == 12
    assert {e["log_token"] for e in chosen if not e["risk"]} == {"8", "9", "10", "11"}
