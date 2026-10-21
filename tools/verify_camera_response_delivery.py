"""Reusable installed HTTP/SDK/Worker proof; authors explicitly synthetic evidence."""

import json
import tempfile
from pathlib import Path


def verify(client, program_id, suffix):
    checks = []
    program = client.get("control_programs/" + program_id)
    draft = client.response_test_template(program_id, observable="green_mean_normalized")
    draft["name"] = "합성 RAW 응답 비교 · " + suffix
    test = client.post("tests", draft)
    run = client.wait(
        client.submit(
            program["assembly_id"],
            test["id"],
            plugin_id="closed_loop_scene_v1",
            control_program_id=program_id,
            parameters={"seed": 123},
        )["id"],
        timeout=120,
        poll_interval=0.1,
    )
    assert run["status"] == "succeeded", run.get("error")
    observation = run["result"]["response_observation"]
    checks.append({"name": "installed_worker_actual_raw_common_observation", "status": "passed"})
    raw_artifact = next(a for a in run["artifacts"] if a["name"] == "raw_frames.npy")
    raw_response = client.http.get(raw_artifact["url"])
    raw_response.raise_for_status()
    metadata = {
        "name": "합성 RAW 원본 · " + suffix,
        "revision": "1",
        "source_kind": "simulated",
        "source_uri": "run://" + run["id"],
        "license": "MIT",
        "method_and_version": "qa_raw_export:1",
        "physical_instance": None,
        "parents": [],
        "test_id": test["id"],
        "assembly_id": program["assembly_id"],
        "dataset_ids": ["synthetic-response-" + run["id"]],
        "instrument": {},
        "conditions": test["conditions"],
        "acquisition": observation["acquisition"],
        "uncertainty": {
            "kind": "unknown",
            "unit": test["unit"],
            "reason": "총 관측 불확실도 미확립",
        },
    }
    original = (
        json.dumps(metadata, ensure_ascii=False, indent=3).replace("\n", "\r\n") + "\r\n"
    ).encode()
    with tempfile.TemporaryDirectory(prefix="camera-response-delivery-") as directory:
        path = Path(directory)
        (path / "raw.npy").write_bytes(raw_response.content)
        (path / "metadata.json").write_bytes(original)
        imported = client.import_control_response_file(path / "metadata.json", path / "raw.npy")
    capture = imported["capture"]
    artifact = next(a for a in capture["artifacts"] if a["name"] == "acquisition-metadata.json")
    assert client.http.get(artifact["url"]).content == original
    assert imported["measurement"]["values"] == run["result"]["prediction"]["values"]
    checks.append({"name": "original_utf8_crlf_file_and_pixels_preserved", "status": "passed"})
    compared = client.compare(run["id"], imported["measurement"]["id"], test["id"])
    assert compared["status"] == "INCONCLUSIVE" and not compared["physical_validation_pass"]
    assert all(v == 0 for v in compared["residuals"]["values"])
    checks.append(
        {"name": "same_operator_nominal_residuals_unknown_physical_accuracy", "status": "passed"}
    )
    other_test = client.post(
        "tests", client.response_test_template(program_id, observable="exposure_ns")
    )
    request = {
        "capture_id": capture["id"],
        "test_id": other_test["id"],
        "name": "같은 RAW의 노출 응답 분석",
        "revision": "2",
        "parent_analysis_id": imported["analysis"]["id"],
        "uncertainty": {
            "kind": "unknown",
            "unit": "ns",
            "reason": "Exposure response bound unestablished",
        },
    }
    second = client.analyze_capture(**request)
    assert client.analyze_capture(**request) == second
    assert second["capture"] == capture
    assert second["measurement"]["dataset_ids"] == imported["measurement"]["dataset_ids"]
    assert second["measurement"]["source_kind"] == "simulated"
    checks.append(
        {
            "name": "one_acquisition_multiple_metric_revisions_and_idempotent_retry",
            "status": "passed",
        }
    )
    return {
        "status": "passed",
        "checks": checks,
        "environment": run["environment"],
        "physical_validation": "not_established",
        "source_kind": "simulated",
        "state_dir": client.get("health")["state_dir"],
        "capture_hash": capture["content_hash"],
        "records": {
            "assembly": program["assembly_id"],
            "program": program_id,
            "test": test["id"],
            "run": run["id"],
            "capture": capture["id"],
            "analysis": imported["analysis"]["id"],
            "measurement": imported["measurement"]["id"],
            "validation": compared["id"],
            "alternate_test": other_test["id"],
            "alternate_analysis": second["analysis"]["id"],
            "alternate_measurement": second["measurement"]["id"],
        },
    }
