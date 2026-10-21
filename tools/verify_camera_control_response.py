#!/usr/bin/env python3
"""Exercise common response observation on an explicitly isolated development API.

Authors synthetic evidence only. The imported reference deliberately omits one RAW
frame while retaining its known control log. Never points to the installed main state.
"""

import argparse
import io
import json
import tempfile
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from camera_platform.sdk import PlatformClient


def verify(base_url, inputs_path, output_path):
    inputs = json.loads(Path(inputs_path).read_text())
    report = {
        "schema": "camera_control_response_http_verification_v1",
        "checks": [],
        "verified_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "scope": "Synthetic development API/Worker/SDK evidence; not physical validation",
    }

    def passed(name):
        report["checks"].append({"name": name, "status": "passed"})

    with PlatformClient(base_url) as client:
        health = client.get("health")
        expected = Path(inputs["state_dir"]).resolve()
        if Path(health["state_dir"]).resolve() != expected or expected.name == "camera-platform":
            raise ValueError("Verifier requires the explicitly prepared isolated QA state")
        if client.get("captures"):
            raise ValueError("Use fresh QA inputs; existing observations are preserved")
        report["state_dir"] = str(expected)
        draft = client.template("control-policy", scene_sequence_id=inputs["scene_sequence_id"])
        policy = client.create_control_policy(draft)
        draft = client.template(
            "control-program",
            assembly_id=inputs["assembly_id"],
            scene_sequence_id=inputs["scene_sequence_id"],
            control_policy_id=policy["id"],
            frame_count=32,
        )
        program = client.create_control_program(draft)
        draft = client.response_test_template(program["id"], observable="wb_adjusted_rg_ratio")
        test = client.post("tests", draft)
        queued = client.submit(
            inputs["assembly_id"],
            test["id"],
            plugin_id="closed_loop_scene_v1",
            control_program_id=program["id"],
            parameters={"seed": 123},
        )
        run = client.wait(queued["id"], timeout=60)
        assert run["status"] == "succeeded", run.get("error")
        report["environment"] = run["environment"]
        passed("common_test_and_separate_worker_observe_actual_32_frame_raw")
        observation = run["result"]["response_observation"]
        assert observation["coverage"]["planned_frames"] == 32
        assert observation["acquisition"]["commands"][0]["commanded"]
        artifact = next(a for a in run["result"]["artifacts"] if a["name"] == "raw_frames.npy")
        raw_response = client.http.get(artifact["url"])
        raw_response.raise_for_status()
        raw = np.load(io.BytesIO(raw_response.content), allow_pickle=False)
        acquisition = deepcopy(observation["acquisition"])
        drop = 5
        acquisition["frames"][drop]["raw_index"] = None
        for index in range(drop + 1, 32):
            acquisition["frames"][index]["raw_index"] -= 1
        metadata = {
            "name": "합성 응답 관측 · RAW 1프레임 누락",
            "revision": "1",
            "source_kind": "simulated",
            "source_uri": "run://" + run["id"],
            "license": "MIT; authored synthetic fixture",
            "method_and_version": "synthetic_raw_export_with_explicit_missing_frame:1",
            "physical_instance": None,
            "parents": [],
            "test_id": test["id"],
            "assembly_id": inputs["assembly_id"],
            "dataset_ids": ["synthetic-response-export-1"],
            "instrument": {},
            "conditions": test["conditions"],
            "acquisition": acquisition,
            "uncertainty": {
                "kind": "unknown",
                "unit": "1",
                "reason": "No total observation budget",
            },
        }
        with tempfile.TemporaryDirectory(prefix="camera-response-upload-") as temporary:
            path = Path(temporary) / "raw.npy"
            np.save(path, np.delete(raw, drop, axis=0), allow_pickle=False)
            imported = client.import_control_response(metadata, path)
        passed("original_raw_and_metadata_preserved_with_missing_frame_and_known_applied_settings")
        validation = client.compare(run["id"], imported["measurement"]["id"], test["id"])
        assert validation["status"] == "INCONCLUSIVE"
        assert validation["residuals"]["values"] and all(
            v == 0 for v in validation["residuals"]["values"]
        )
        assert any(
            m["measurement"] == "missing_raw"
            for m in validation["response_comparison"]["missing_samples"]
        )
        assert not validation["physical_validation_pass"]
        passed("matched_residuals_and_missing_coverage_do_not_become_a_physical_pass")
        release = client.post(
            "releases",
            {
                "name": "응답 관측 개발 근거 · 상용 승인 아님",
                "run_ids": [run["id"]],
                "validation_ids": [validation["id"]],
            },
        )
        bundle_response = client.http.get("/api/v1/releases/" + release["id"] + "/bundle")
        bundle_response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(bundle_response.content)) as archive:
            assert any(n.endswith("raw-capture.npy") for n in archive.namelist())
            assert any(n.endswith("acquisition-metadata.json") for n in archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["captures"][0]["id"] == imported["capture"]["id"]
        passed("evidence_zip_contains_original_capture_and_common_test_lineage")
        assert client.get("health")["worker"] == "running"
        report.update(
            status="passed",
            records={
                "program": program["id"],
                "test": test["id"],
                "run": run["id"],
                "capture": imported["capture"]["id"],
                "measurement": imported["measurement"]["id"],
                "validation": validation["id"],
                "release": release["id"],
            },
            coverage={
                "prediction": observation["coverage"],
                "measurement": imported["measurement"]["response_observation"]["coverage"],
            },
            paired_samples=len(validation["residuals"]["values"]),
            missing_samples=validation["response_comparison"]["missing_samples"],
        )
    Path(output_path).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = verify(args.base_url, args.inputs, args.output)
    print(
        json.dumps(
            {k: result[k] for k in ("status", "checks", "records", "coverage")}, ensure_ascii=False
        )
    )
