#!/usr/bin/env python3
"""Verify authored feedback acquisition and replay through a live installed API.

Creates explicitly synthetic scene/policy/program/Test records using an existing
camera's declared gain=1 domain; never widens model validity or changes old records.
"""

import argparse
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from camera_platform.sdk import PlatformClient


def verify(client, assembly_id, suffix):
    checks = []

    def passed(name):
        checks.append({"name": name, "status": "passed"})

    fixture = client.template("temporal-fixture", assembly_id=assembly_id)
    fixture.update(
        name="AE·AWB 합성 광량 시험",
        revision=suffix,
        shape=[16, 24],
        knot_count=3,
        pattern="static",
        duration_ns=320_000_000,
        irradiance_scale=0.002,
    )
    scene = client.create_temporal_fixture(fixture)
    policy_document = client.template("control-policy", scene_sequence_id=scene["id"])
    policy_document.update(name="AE·AWB · Gain 1 범위의 합성 시험 정책", revision=suffix)
    policy_document["payload"]["ae"]["readout_gain"]["maximum"] = 1.0
    policy = client.create_control_policy(policy_document)
    program_document = client.template(
        "control-program",
        assembly_id=assembly_id,
        scene_sequence_id=scene["id"],
        control_policy_id=policy["id"],
        frame_count=32,
    )
    program_document.update(name="AE·AWB · 32프레임 합성 촬영", revision=suffix)
    check = client.check_control_program(program_document)
    assert check["compatibility"]["status"] == "PASS"
    assert check["compatibility"]["control_bounds"]["gain"] == {"min": 1, "max": 1, "unit": "1"}
    passed("entire_control_range_respects_existing_gain_one_models")
    original = " \r\n" + json.dumps(program_document, ensure_ascii=False, indent=3) + "\r\n"
    program = client.post("control_programs/import", {"document_text": original})
    assert program["expanded_program_sha256"] == check["expanded_program_sha256"]
    source = next(a for a in program["artifacts"] if a["name"] == "control-program-source.json")
    response = client.http.get(source["url"])
    response.raise_for_status()
    assert response.content == original.encode()
    passed("registered_program_preserves_original_bytes_and_expanded_hash")
    test = client.post(
        "tests", client.template("closed-loop-test", control_program_id=program["id"])
    )
    assert test["frame_conditions"] is None
    queued = client.submit_closed_loop(assembly_id, test["id"], program["id"], seed=993)
    run = client.wait(queued["id"], timeout=180)
    assert run["status"] == "succeeded", run.get("error")
    summary = run["result"]["summary"]
    frames, trace = summary["frames"], summary["control_trace"]
    assert len(frames) == len(summary["frame_conditions"]) == 32
    assert len({f["applied"]["exposure_ns"] for f in frames}) > 1
    assert any(f["applied"]["wb_gains_rgb"] != frames[0]["applied"]["wb_gains_rgb"] for f in frames)
    assert trace["commands"] and all(
        c["applied_frame_index"] > c["source_frame_index"]
        for c in trace["commands"]
        if c["status"] == "applied"
    )
    assert summary["control_program_sha256"] == program["expanded_program_sha256"]
    assert run["control_program_snapshot"] == program and run["control_policy_snapshot"] == policy
    passed("separate_worker_changes_actual_exposure_wb_and_retains_command_application")

    def artifact(record, name):
        entry = next(a for a in record["artifacts"] if a["name"] == name)
        response = client.http.get(entry["url"])
        response.raise_for_status()
        assert hashlib.sha256(response.content).hexdigest() == entry["sha256"]
        return response.content, entry["sha256"]

    raw_bytes, raw_hash = artifact(run, "raw_frames.npy")
    raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
    assert raw.shape == (32, 16, 24) and not np.array_equal(raw[0], raw[-1])
    _, rgb_hash = artifact(run, "rgb_frames.npy")
    repeated = client.submit_closed_loop(assembly_id, test["id"], program["id"], seed=993)
    assert repeated["id"] == run["id"]
    passed("real_raw_rgb_hashes_and_same_input_cache_verified")
    profile = client.template("isp-profile", assembly_id=assembly_id)
    profile.update(name="AE·AWB RAW · 고정 WB 재처리", revision=suffix)
    profile["payload"]["white_balance"]["gains_rgb"] = [1.25, 1.0, 0.75]
    isp = client.post("models", profile)
    replay = client.wait_replay(
        client.post(
            "replays",
            {
                "name": "AE·AWB RAW · 취득 이력 보존 재처리",
                "source_kind": "run",
                "source_id": run["id"],
                "isp_model_id": isp["id"],
            },
        )["id"],
        timeout=180,
    )
    assert replay["status"] == "succeeded", replay.get("error")
    assert artifact(replay, "raw_frames.npy")[1] == raw_hash
    assert artifact(replay, "rgb_frames.npy")[1] != rgb_hash
    assert replay["result"]["summary"]["control_trace"] == trace
    assert replay["result"]["summary"]["frame_conditions"] == summary["frame_conditions"]
    passed("static_isp_replay_preserves_raw_and_original_control_trace")
    return {
        "schema": "camera_control_delivery_verification_v1",
        "status": "passed",
        "verified_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "environment": run["environment"],
        "physical_validation": "not_established",
        "input_kind": "authored_synthetic_fixture",
        "state_dir": client.get("health")["state_dir"],
        "records": {
            "assembly": assembly_id,
            "scene": scene["id"],
            "policy": policy["id"],
            "program": program["id"],
            "test": test["id"],
            "run": run["id"],
            "isp": isp["id"],
            "replay": replay["id"],
        },
        "raw_sha256": raw_hash,
        "rgb_sha256": rgb_hash,
        "final_observation": frames[-1]["statistics"],
        "final_control_state": frames[-1]["control_proposals"],
        "expanded_program_sha256": program["expanded_program_sha256"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--assembly-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with PlatformClient(args.base_url, timeout=120) as client:
        report = verify(client, args.assembly_id, datetime.now(UTC).strftime("v09-%Y%m%dT%H%M%SZ"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "checks": len(report["checks"]),
                "records": report["records"],
            },
            ensure_ascii=False,
        )
    )
