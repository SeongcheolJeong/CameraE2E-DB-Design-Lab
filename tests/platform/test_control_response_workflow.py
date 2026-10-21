"""Common Test -> actual camera RAW -> imported observed RAW -> validation evidence."""

import io
import json
from copy import deepcopy

import numpy as np
import pytest
import test_control_workflow as control_workflow
from fastapi.testclient import TestClient
from test_control_workflow import array, finish, register

from camera_platform import control_response_service as response
from camera_platform.api import create_app
from camera_platform.control_observation import METHOD
from camera_platform.observations import verify_capture_measurement
from camera_platform.registry import ConflictError

workflow = control_workflow.workflow


def response_run(workflow):
    service, _, _, _, _ = workflow
    program, _, request = register(workflow)
    draft = response.test_template(service, {"control_program_id": program["id"]})
    test = service.create_test(draft)
    run = finish(service, service.submit_run({**request, "test_id": test["id"]}))
    return service, test, run


def source(service, test, run):
    raw = array(service, run["result"], "raw_frames")
    buffer = io.BytesIO()
    np.save(buffer, raw, allow_pickle=False)
    metadata = {
        "name": "Synthetic observed RAW replay",
        "revision": "1",
        "source_kind": "simulated",
        "source_uri": "run://" + run["id"],
        "license": "MIT",
        "method_and_version": "exported_raw:1",
        "physical_instance": None,
        "parents": [],
        "test_id": test["id"],
        "assembly_id": run["assembly_id"],
        "dataset_ids": ["synthetic-export-1"],
        "instrument": {},
        "conditions": test["conditions"],
        "acquisition": deepcopy(run["result"]["response_observation"]["acquisition"]),
        "uncertainty": {
            "kind": "unknown",
            "unit": test["unit"],
            "reason": "No total observation uncertainty budget has been established",
        },
    }
    return metadata, buffer.getvalue()


def test_registered_worker_observes_actual_raw_and_replays_capture_analysis(workflow):
    service, test, run = response_run(workflow)
    assert run["status"] == "succeeded"
    prediction = run["result"]["prediction"]
    assert prediction["metric_id"] == test["metric_id"]
    assert prediction["observation_method"]["id"] == METHOD
    assert len(prediction["values"]) == 32
    metadata, raw_bytes = source(service, test, run)
    # Preserve verbatim metadata, including formatting, rather than regenerate it.
    original = json.dumps(metadata, indent=3)
    imported = response.import_capture(service, metadata, raw_bytes, original_text=original)
    measurement = imported["measurement"]
    assert measurement["values"] == prediction["values"]
    verify_capture_measurement(service, measurement)
    artifact = next(
        a for a in imported["capture"]["artifacts"] if a["name"] == "acquisition-metadata.json"
    )
    assert service.registry.blob_path(artifact["sha256"]).read_text() == original
    report = service.validate(
        {"run_id": run["id"], "test_id": test["id"], "measurement_id": measurement["id"]}
    )
    assert report["status"] == "INCONCLUSIVE"
    assert report["residuals"]["values"] == [0.0] * 32
    assert "closed_loop_comparison_operator_required" not in {i["code"] for i in report["issues"]}
    assert not report["physical_validation_pass"]
    changed = deepcopy(measurement)
    changed["response_observation"]["points"][0]["value"] += 1
    with pytest.raises(ValueError, match="preserved"):
        verify_capture_measurement(service, changed)


def test_generated_raw_cannot_be_relabelled_as_an_independent_physical_capture(workflow):
    service, test, run = response_run(workflow)
    metadata, raw_bytes = source(service, test, run)
    metadata.update(
        source_kind="measured",
        physical_instance="fake-serial",
        source_uri="fixture://not-a-real-instrument",
        instrument={
            "id": "fake-instrument",
            "calibration_revision": "1",
            "policy_implementation_evidence": "fixture://not-real-evidence",
        },
    )
    raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False).astype(np.uint32)
    buffer = io.BytesIO()
    np.save(buffer, raw, allow_pickle=False)
    with pytest.raises(ConflictError, match="generated pixels"):
        response.import_capture(service, metadata, buffer.getvalue())
    assert not service.registry.list("captures")
    assert not service.registry.list("measurements")


def test_response_test_refuses_operator_geometry_or_policy_drift(workflow):
    service, _, _, _, _ = workflow
    program, _, _ = register(workflow)
    draft = response.test_template(service, {"control_program_id": program["id"]})
    draft["observation_specification"]["geometry"]["crop_origin"][0] += 1
    from camera_platform.control_observation import method

    draft["observation_method"] = method(draft["observation_specification"])
    with pytest.raises(ValueError, match="pin the actual"):
        service.create_test(draft)


def test_http_response_import_uses_token_and_preserves_original_metadata(workflow):
    service, test, run = response_run(workflow)
    app = create_app(state_dir=service.registry.directory, start_worker=False, seed_demo=False)
    metadata, raw_bytes = source(service, test, run)
    original = json.dumps(metadata, indent=2)
    with TestClient(app) as client:
        files = {"raw": ("capture.npy", raw_bytes, "application/octet-stream")}
        assert (
            client.post(
                "/api/v1/captures/control-response/import", files=files, data={"metadata": original}
            ).status_code
            == 403
        )
        token = client.get("/api/v1/session").json()["token"]
        client.headers["X-Platform-Token"] = token
        prepared = client.post(
            "/api/v1/control_response_tests/template",
            json={
                "control_program_id": run["control_program_id"],
                "observable": "wb_adjusted_rg_ratio",
            },
        )
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["unit"] == "1"
        result = client.post(
            "/api/v1/captures/control-response/import", files=files, data={"metadata": original}
        )
        assert result.status_code == 201, result.text
        assert result.json()["measurement"]["values"] == run["result"]["prediction"]["values"]


def test_response_capture_replay_keeps_raw_and_observed_acquisition(workflow):
    from camera_platform import imaging

    service, test, run = response_run(workflow)
    metadata, raw_bytes = source(service, test, run)
    imported = response.import_capture(service, metadata, raw_bytes)
    capture = imported["capture"]
    isp_id = metadata["acquisition"]["frames"][0]["applied"]["isp_model_id"]
    assert capture["id"] in {s["source_id"] for s in imaging.replay_sources(service)}
    replay = finish(
        service,
        imaging.submit_replay(
            service,
            {
                "name": "Observed RAW replay",
                "source_kind": "capture",
                "source_id": capture["id"],
                "isp_model_id": isp_id,
            },
        ),
    )
    assert np.array_equal(
        array(service, replay["result"], "raw-capture"),
        np.load(io.BytesIO(raw_bytes), allow_pickle=False),
    )
    assert replay["result"]["response_acquisition"] == metadata["acquisition"]
    assert replay["result"]["clock"] == metadata["acquisition"]["clock"]


def test_sdk_actual_worker_response_import_and_comparison(workflow, tmp_path):
    from camera_platform.sdk import PlatformClient

    service, assembly, _, _, _ = workflow
    program, _, _ = register(workflow)
    app = create_app(service.registry.directory, start_worker=True, seed_demo=False)
    with TestClient(app) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = http
        draft = sdk.response_test_template(program["id"], observable="wb_adjusted_rg_ratio")
        test = sdk.post("tests", draft)
        queued = sdk.submit(
            assembly["id"],
            test["id"],
            plugin_id="closed_loop_scene_v1",
            control_program_id=program["id"],
            parameters={"seed": 123},
        )
        run = sdk.wait(queued["id"], timeout=30, poll_interval=0.05)
        assert run["status"] == "succeeded", run.get("error")
        metadata, raw_bytes = source(service, test, run)
        raw_path = tmp_path / "observed-raw.npy"
        raw_path.write_bytes(raw_bytes)
        imported = sdk.import_control_response(metadata, raw_path)
        report = sdk.compare(run["id"], imported["measurement"]["id"], test["id"])
        assert report["residuals"]["values"] == [0.0] * len(run["result"]["prediction"]["values"])
        assert report["response_comparison"]["prediction"]["coverage"]["planned_frames"] == 32
        assert report["response_comparison"][
            "missing_samples"
        ]  # Too few usable cells in early RAW.
        assert report["status"] == "INCONCLUSIVE"
        assert sdk.get("health")["worker"] == "running"
        assert (
            sdk.get("workbench")["measurements"][0]["response_observation"]["method"]["id"]
            == METHOD
        )
