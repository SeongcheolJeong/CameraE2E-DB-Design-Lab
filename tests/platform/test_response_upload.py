"""Original browser/SDK metadata file upload preserves bytes and unknown observations."""

import json

import pytest
import test_control_response_workflow as response_workflow
from fastapi.testclient import TestClient
from test_control_response_workflow import response_run, source

from camera_platform.api import create_app
from camera_platform.control_response_service import acquisition_template
from camera_platform.sdk import PlatformClient

workflow = response_workflow.workflow


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_original_metadata_file_preserves_utf8_and_line_endings(workflow, tmp_path, newline):
    service, test, run = response_run(workflow)
    metadata, raw = source(service, test, run)
    metadata["name"] = "취득 원문 · 바이트 보존"
    original = (
        json.dumps(metadata, indent=3, ensure_ascii=False).replace("\n", newline) + newline
    ).encode()
    metadata_file, raw_file = tmp_path / "source.json", tmp_path / "raw.npy"
    metadata_file.write_bytes(original)
    raw_file.write_bytes(raw)
    with TestClient(
        create_app(service.registry.directory, start_worker=False, seed_demo=False)
    ) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = http
        result = sdk.import_control_response_file(metadata_file, raw_file)
        assert result["measurement"]["capture_analysis_id"] == result["analysis"]["id"]
        artifact = next(
            a for a in result["capture"]["artifacts"] if a["name"] == "acquisition-metadata.json"
        )
        assert http.get(artifact["url"]).content == original


@pytest.mark.parametrize("mode", ["both", "neither", "invalid_utf8", "too_large"])
def test_original_file_rejects_ambiguous_or_invalid_metadata_without_records(workflow, mode):
    service, test, run = response_run(workflow)
    metadata, raw = source(service, test, run)
    text = json.dumps(metadata)
    files = {"raw": ("raw.npy", raw, "application/octet-stream")}
    fields = {}
    if mode == "both":
        fields["metadata"] = text
        files["metadata_file"] = ("metadata.json", text.encode(), "application/json")
    elif mode == "invalid_utf8":
        files["metadata_file"] = ("metadata.json", b"\xff\xfe", "application/json")
    elif mode == "too_large":
        files["metadata_file"] = ("metadata.json", b" " * 1_048_577, "application/json")
    with TestClient(
        create_app(service.registry.directory, start_worker=False, seed_demo=False)
    ) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        reply = http.post("/api/v1/captures/control-response/import", files=files, data=fields)
        assert reply.status_code == (413 if mode == "too_large" else 422), reply.text
    for kind in ("captures", "capture_analyses", "measurements"):
        assert not service.registry.list(kind)


def test_acquisition_form_never_fabricates_control_or_time_records(workflow):
    service, test, run = response_run(workflow)
    template = acquisition_template(service, test["id"])
    assert template["source_kind"] == "estimated"
    assert template["dataset_ids"] == []
    assert template["acquisition"]["alignment_absolute_bound_ns"] is None
    assert template["acquisition"]["program_hash"] == test["stimulus"]["control_program_hash"]
    assert template["acquisition"]["execution_source_uri"] == ""
    assert all(
        f["raw_index"] is f["frame_start_ns"] is f["latch_at_ns"] is f["applied"] is None
        for f in template["acquisition"]["frames"]
    )
    assert template["uncertainty"]["kind"] == "unknown"
    with TestClient(
        create_app(service.registry.directory, start_worker=False, seed_demo=False)
    ) as http:
        reply = http.get(f"/api/v1/control_response_tests/{test['id']}/acquisition-template")
        assert reply.status_code == 200 and reply.json() == template
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        _, raw = source(service, test, run)
        reply = http.post(
            "/api/v1/captures/control-response/import",
            files={
                "raw": ("raw.npy", raw, "application/octet-stream"),
                "metadata_file": (
                    "metadata.json",
                    json.dumps(template).encode(),
                    "application/json",
                ),
            },
        )
        assert reply.status_code == 422
    assert not service.registry.list("captures")
