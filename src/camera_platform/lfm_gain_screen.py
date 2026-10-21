"""Durable matched PWM gain screen over completed original-integer-RAW jobs.

Every candidate is an actual camera Worker acquisition. The screen re-reads
content-addressed RAW and reproduces the stored meter; no charge/trap oracle,
gain scaling, or unobserved exposure is used to rank sampled code outcomes.
"""

from __future__ import annotations

import hashlib
import io
import math
from copy import deepcopy

import numpy as np

from . import electrical_control
from .electrical_control_runtime import (
    PLUGIN_ID, TRAPPED_PROGRAM_SCHEMA, observation_path, validate_capture_program,
)
from .registry import ConflictError, IntegrityError, canonical, digest

REQUEST_SCHEMA = "camera_lfm_gain_screen_request_v1"
RESULT_SCHEMA = "camera_lfm_gain_screen_result_v1"
RECORD_SCHEMA = "camera_lfm_gain_screen_record_v1"
KIND = "lfm_gain_screens"
MAX_RUNS = 8
MAX_RAW_BYTES = 64 * 1024**2


def validate_specification(value):
    if not isinstance(value, dict) or set(value) != {"schema", "name", "run_ids"}:
        raise ValueError("LFM gain screen requires schema, name and run_ids")
    if value["schema"] != REQUEST_SCHEMA:
        raise ValueError("Unsupported LFM gain screen request")
    name, ids = value["name"], value["run_ids"]
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError("LFM gain screen name requires 1..200 characters")
    if (not isinstance(ids, list) or not 2 <= len(ids) <= MAX_RUNS
            or any(not isinstance(i, str) or not i.startswith("run-")
                   or len(i) > 100 for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("LFM gain screen requires 2..8 distinct completed run IDs")
    return deepcopy(value)


def _normalized_request(job):
    if job.get("plugin_id") != PLUGIN_ID or job.get("status") != "succeeded":
        raise ValueError("Gain screen requires completed electrical camera runs")
    request = deepcopy(job["request"])
    original = request["control_sequence"]
    if original != job["control_sequence_snapshot"]["payload"]:
        raise IntegrityError("Frozen control sequence differs from the run request")
    gains = [frame["applied"]["gain"] for frame in original["frames"]]
    gain = gains[0]
    if (type(gain) not in (int, float) or not math.isfinite(gain) or gain <= 0
            or any(value != gain for value in gains)):
        raise ValueError("Each gain candidate must hold one positive gain for every frame")
    for frame in request["control_sequence"]["frames"]:
        frame["applied"]["gain"] = "candidate_gain"
    return gain, request


def _raw(service, job, summary):
    artifacts = [a for a in job["artifacts"] if a["name"] == "raw_groups.npy"]
    if len(artifacts) != 1 or artifacts[0]["bytes"] > MAX_RAW_BYTES:
        raise IntegrityError("Gain screen requires one bounded original RAW artifact")
    artifact = artifacts[0]
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != artifact["sha256"] or len(content) != artifact["bytes"]:
        raise IntegrityError("Original RAW artifact failed its saved hash or byte count")
    try:
        array = np.load(io.BytesIO(content), allow_pickle=False)
    except (ValueError, EOFError, OSError) as exc:
        raise IntegrityError("Original RAW artifact is not a readable NPY array") from exc
    count, height, width = summary["frame_count"], *summary["output_size"]
    if (array.shape != (count, 1, height, width) or array.dtype.kind not in "iu"
            or artifact.get("shape") != list(array.shape)
            or artifact.get("dtype") != str(array.dtype)
            or artifact.get("unit") != "DN"
            or artifact.get("spatial_sampling") != "native_sensor"):
        raise IntegrityError("Original RAW shape/type differs from the native result")
    return artifact, array


def _candidate(service, job):
    gain, normalized = _normalized_request(job)
    summary = job["result"]["summary"]
    program = validate_capture_program(job["request"]["capture_program"])
    policy = electrical_control.validate_policy(program["electrical_control_policy"])
    index, readout_id, raw_format = observation_path(program)
    if (program["schema"] != TRAPPED_PROGRAM_SCHEMA
            or program["electrical_model"]["schema"] != "camera_electrical_readout_v1"
            or policy["schema"] != electrical_control.PERIOD_POLICY_SCHEMA
            or electrical_control.effective_exposure_grid(policy)["point_count"] != 1
            or readout_id != "single" or index != 0):
        raise ValueError("Screen requires single linear trapped AE and one whole-period exposure")
    trace = summary["electrical_control"]
    if (summary["capture_program"] != program
            or summary["control_sequence"] != job["request"]["control_sequence"]
            or summary["scene_sequence_id"] != job["scene_sequence_id"]
            or trace["policy"] != policy or trace["raw_format"] != raw_format
            or trace["readout_id"] != "single" or trace["source_truth_used"] is not False
            or summary["physical_validation"] != "not_established"):
        raise IntegrityError("Gain screen source/result authority differs from frozen acquisition")
    descriptor, raw = _raw(service, job, summary)
    if len(trace["frames"]) != raw.shape[0] or summary["raw_formats"][0] != raw_format:
        raise IntegrityError("Saved control trace differs from original RAW frame count/format")
    target = policy["target_black_relative_dn"]
    frames = []
    timing = []
    for position, (frame, original) in enumerate(zip(
        trace["frames"], job["request"]["control_sequence"]["frames"], strict=True
    )):
        actual = frame["applied"]
        if (frame["frame_id"] != original["frame_id"]
                or actual["gain"] != gain
                or actual["exposure_ns"] != original["applied"]["exposure_ns"]
                or actual["applied_at_ns"] != original["applied"]["applied_at_ns"]):
            raise ValueError("Matched gain screen requires unchanged actual exposure chronology")
        measured = electrical_control.raw_statistics(raw[position, 0], raw_format, policy)
        if canonical(measured) != canonical(frame["statistics"]):
            raise IntegrityError("Saved meter cannot be reproduced from original integer RAW")
        value = measured["value_dn"]
        distance = (target["minimum"] - value if value < target["minimum"] else
                    value - target["maximum"] if value > target["maximum"] else 0.0)
        frames.append({
            "frame_id": frame["frame_id"], "applied_exposure_ns": actual["exposure_ns"],
            "observed_dn": value, "in_target": distance == 0,
            "target_distance_dn": distance,
            "digital_high_rail_fraction": measured["digital_high_rail_fraction"],
            "digital_low_rail_fraction": measured["digital_low_rail_fraction"],
        })
        timing.append({
            "frame_id": frame["frame_id"], "frame_start_ns": original["frame_start_ns"],
            "applied_at_ns": actual["applied_at_ns"],
            "exposure_ns": actual["exposure_ns"],
            "statistics_ready_ns": frame["events"]["statistics_ready_ns"],
        })
    fingerprint = digest({
        "id": job["id"], "request": job["request"], "summary": summary,
        "raw_artifact": descriptor, "raw_sha256": descriptor["sha256"],
    })
    return {
        "run_id": job["id"], "gain": float(gain), "fingerprint": fingerprint,
        "raw_sha256": descriptor["sha256"], "frames": frames,
        "in_target_frame_count": sum(frame["in_target"] for frame in frames),
        "worst_target_distance_dn": max(frame["target_distance_dn"] for frame in frames),
        "maximum_digital_high_rail_fraction": max(
            frame["digital_high_rail_fraction"] for frame in frames
        ),
    }, normalized, timing, policy


def analyze(service, specification):
    spec = validate_specification(specification)
    jobs = [service.registry.get_run(identity) for identity in spec["run_ids"]]
    candidates, normalized, timelines, policies = [], [], [], []
    for job in jobs:
        candidate, request, timeline, policy = _candidate(service, job)
        candidates.append(candidate)
        normalized.append(request)
        timelines.append(timeline)
        policies.append(policy)
    if (any(canonical(item) != canonical(normalized[0]) for item in normalized[1:])
            or any(item != timelines[0] for item in timelines[1:])
            or any(item != policies[0] for item in policies[1:])
            or len({candidate["gain"] for candidate in candidates}) != len(candidates)):
        raise ValueError("Gain candidates must differ only in fixed gain and resulting original RAW")
    candidates.sort(key=lambda candidate: candidate["gain"])
    def dominates(left, right):
        better = (
            left["in_target_frame_count"] >= right["in_target_frame_count"]
            and left["worst_target_distance_dn"] <= right["worst_target_distance_dn"]
            and left["maximum_digital_high_rail_fraction"]
            <= right["maximum_digital_high_rail_fraction"]
        )
        strict = (
            left["in_target_frame_count"] > right["in_target_frame_count"]
            or left["worst_target_distance_dn"] < right["worst_target_distance_dn"]
            or left["maximum_digital_high_rail_fraction"]
            < right["maximum_digital_high_rail_fraction"]
        )
        return better and strict
    frontier = [candidate["run_id"] for candidate in candidates
                if not any(dominates(other, candidate) for other in candidates)]
    return {
        "schema": RESULT_SCHEMA,
        "scope": "matched_authored_pwm_gain_candidates_original_integer_raw_only",
        "name": spec["name"], "target_dn": deepcopy(policies[0]["target_black_relative_dn"]),
        "period_ns": policies[0]["period_lattice"]["period_ns"],
        "permitted_exposure_ns": electrical_control.effective_exposure_grid(policies[0])["minimum"],
        "frame_count": len(timelines[0]), "candidate_count": len(candidates),
        "candidates": candidates, "observed_pareto_run_ids": frontier,
        "pareto_objectives": ["maximize_in_target_frame_count",
                              "minimize_worst_target_distance_dn",
                              "minimize_maximum_digital_high_rail_fraction"],
        "automatic_selection": False,
        "charge_or_trap_truth_used": False,
        "well_saturation_observable_from_raw": False,
        "physical_validation": "not_established",
        "physical_lfm_performance": "not_established",
        "uncertainty": {"kind": "unknown"},
        "limitations": [
            "Only completed linear single-readout trapped AE runs with one permitted exposure.",
            "The same authored scene, PWM phase, sensor, trap model, seed and timing are required.",
            "Digital RAW rail counts cannot prove absence of hidden well saturation.",
            "A finite nominal Pareto frontier is not an automatic camera recommendation.",
            "Independent hardware measurement and phase/noise repetitions are absent.",
        ],
    }


def submit(service, specification):
    spec = validate_specification(specification)
    result = analyze(service, spec)
    identity = "lfm_gain_screen-" + digest({"specification": spec, "result": result})
    document = {"schema": RECORD_SCHEMA, "specification": spec, "analysis": result,
                "source_run_fingerprints": {c["run_id"]: c["fingerprint"]
                                            for c in result["candidates"]}}
    try:
        service.registry.put(KIND, document, identity=identity)
    except ConflictError:
        pass
    return get(service, identity)


def get(service, identity):
    record = service.registry.get(KIND, identity)
    if record.get("schema") != RECORD_SCHEMA:
        raise IntegrityError("Unknown LFM gain screen record schema")
    current = analyze(service, record["specification"])
    fingerprints = {c["run_id"]: c["fingerprint"] for c in current["candidates"]}
    if (canonical(current) != canonical(record["analysis"])
            or fingerprints != record["source_run_fingerprints"]):
        raise IntegrityError("LFM gain screen source RAW or saved analysis changed")
    return record


def list_records(service):
    return [get(service, record["id"]) for record in service.registry.list(KIND)]
