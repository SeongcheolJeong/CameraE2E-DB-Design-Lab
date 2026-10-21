"""HTTP/SDK ownership, immutable plan dispatch and dedicated evidence routes.

Numerical and child lifecycle oracles live in test_electrical_response_study;
this file executes only one complete pair to qualify the public route wiring.
"""

import hashlib
import io
import json
import zipfile
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from test_electrical_control_workflow import workflow as workflow
from test_electrical_response_study import finish_all, snapshot
from test_hdr_workflow import workflow as _hdr_workflow
from test_retained_charge_workflow import workflow as _retained_workflow

from camera_platform.registry import canonical

hdr_workflow = _hdr_workflow
retained_workflow = _retained_workflow


def template(sdk, workflow):
    _, _, assembly, scene, control, program, *_ = workflow
    return sdk.electrical_response_template(
        assembly["id"], scene["id"], control["id"], program["id"]
    )


def test_http_template_plan_and_static_modules_are_read_only(workflow):
    service, sdk, *_ = workflow
    before = snapshot(service)
    spec = template(sdk, workflow)
    assert spec["schema"] == "camera_electrical_response_study_v1"
    assert spec["evaluation"]["schema"] == "camera_electrical_response_evaluation_v1"
    first = sdk.plan_electrical_response(spec)
    second = sdk.plan_electrical_response(deepcopy(spec))
    assert first == second
    assert first["frozen_request"]["specification"] == spec
    assert first["cost"]["capture_run_count"] == 2
    assert len(first["plan_hash"]) == 64
    assert sdk.get("electrical-response/studies") == []
    page = sdk.http.get("/")
    assert page.status_code == 200
    for filename in ("electrical-response.js", "electrical-response-controller.js"):
        assert f'src="/{filename}"' in page.text
        asset = sdk.http.get("/" + filename)
        assert asset.status_code == 200
        assert "javascript" in asset.headers["content-type"]
        assert asset.content == (Path("src/camera_platform/static") / filename).read_bytes()
    assert snapshot(service) == before


def test_strict_http_submit_and_sdk_plan_hash_refuse_without_registry_writes(workflow):
    service, sdk, *_ = workflow
    spec = template(sdk, workflow)
    proposal = sdk.plan_electrical_response(spec)
    before = snapshot(service)
    valid = {"specification": spec, "plan_hash": proposal["plan_hash"]}
    malformed = [
        {},
        {"specification": spec},
        {**valid, "force": True},
        {**valid, "unchecked_requests": []},
        {**valid, "specification": {**spec, "force": 1}},
        {**valid, "specification": {**spec, "noise_enabled": 0}},
    ]
    for body in malformed:
        result = sdk.http.post("/api/v1/electrical-response/studies", json=body)
        assert result.status_code == 422, result.text
        assert snapshot(service) == before
    denied = sdk.http.post(
        "/api/v1/electrical-response/studies",
        json=valid,
        headers={"X-Platform-Token": ""},
    )
    assert denied.status_code == 403
    stale = deepcopy(proposal)
    stale["frozen_request"]["specification"]["evaluation"]["hold"]["minimum_span_ns"] += 1
    with pytest.raises(httpx.HTTPStatusError) as error:
        sdk.submit_electrical_response(stale)
    assert error.value.response.status_code == 409
    wrong_hash = {**proposal, "plan_hash": "0" * 64}
    with pytest.raises(httpx.HTTPStatusError) as error:
        sdk.submit_electrical_response(wrong_hash)
    assert error.value.response.status_code == 409
    assert snapshot(service) == before


def test_sdk_sends_frozen_specification_hash_and_explicit_force_without_mutating_plan(
    workflow, monkeypatch
):
    service, sdk, *_ = workflow
    proposal = sdk.plan_electrical_response(template(sdk, workflow))
    original = deepcopy(proposal)
    post = sdk.http.post
    sent = []

    def capture(url, **kwargs):
        if url == "/api/v1/electrical-response/studies":
            sent.append(deepcopy(kwargs["json"]))
        return post(url, **kwargs)

    monkeypatch.setattr(sdk.http, "post", capture)
    first = sdk.submit_electrical_response(proposal)
    forced = sdk.submit_electrical_response(proposal, force=True)
    assert [item["plan_hash"] for item in sent] == [proposal["plan_hash"]] * 2
    assert [item["specification"].pop("force") for item in sent] == [False, True]
    assert all(
        item["specification"] == original["frozen_request"]["specification"] for item in sent
    )
    assert proposal == original
    assert first["id"] != forced["id"]
    assert first["plan_hash"] == forced["plan_hash"] == proposal["plan_hash"]
    assert not {row["run_id"] for row in first["children"]} & {
        row["run_id"] for row in forced["children"]
    }
    for parent in (first, forced):
        assert sdk.cancel_electrical_response(parent["id"])["status"] == "cancelled"
    assert service.registry.claim() is None


def test_parent_uses_dedicated_read_export_routes_and_cancel_stops_only_owned_children(workflow):
    service, sdk, *_, request = workflow
    unrelated = service.submit_run(request)
    proposal = sdk.plan_electrical_response(template(sdk, workflow))
    parent = sdk.submit_electrical_response(proposal)
    identity = parent["id"]
    assert sdk.electrical_response(identity)["job_kind"] == "electrical_response_study"
    assert [item["id"] for item in sdk.get("electrical-response/studies")] == [identity]
    assert identity not in {item["id"] for item in sdk.get("runs")}
    assert {row["run_id"] for row in parent["children"]} <= {item["id"] for item in sdk.get("runs")}
    before = snapshot(service)
    for suffix, status in (("", 404), ("/export", 422), ("/bundle", 422)):
        rejected = sdk.http.get(f"/api/v1/runs/{identity}{suffix}")
        assert rejected.status_code == status, rejected.text
    child_identity = parent["children"][0]["run_id"]
    rejected = sdk.http.get("/api/v1/electrical-response/studies/" + child_identity)
    assert rejected.status_code == 422
    assert snapshot(service) == before
    cancelled = sdk.cancel_electrical_response(identity)
    assert cancelled["status"] == "cancelled"
    assert all(
        service.registry.get_run(row["run_id"])["status"] == "cancelled"
        for row in parent["children"]
    )
    assert service.registry.get_run(unrelated["id"])["status"] == "queued"
    after = snapshot(service)
    assert sdk.cancel_electrical_response(identity)["status"] == "cancelled"
    assert snapshot(service) == after
    exported = sdk.export_electrical_response(identity)
    assert exported["study"]["status"] == "cancelled"
    assert all(child["result"] is None for child in exported["children"])


def test_generic_cancel_delegates_to_owned_study_cancellation_without_orphaned_captures(workflow):
    service, sdk, *_ = workflow
    proposal = sdk.plan_electrical_response(template(sdk, workflow))
    parent = sdk.submit_electrical_response(proposal)
    # The global cancel route intentionally delegates to the same ownership-aware
    # registry transaction; unlike generic reads/exports this is supported.
    cancelled = sdk.post("runs/" + parent["id"] + "/cancel", {})
    assert cancelled["status"] == "cancelled"
    assert all(
        service.registry.get_run(child["run_id"])["status"] == "cancelled"
        for child in parent["children"]
    )
    assert service.registry.claim() is None


def test_sdk_completed_pair_detail_manifest_zip_and_original_raw_routes(workflow):
    service, sdk, *_ = workflow
    proposal = sdk.plan_electrical_response(template(sdk, workflow))
    parent = sdk.submit_electrical_response(proposal)
    finished = finish_all(service)
    assert len(finished) == 3 and finished[-1]["id"] == parent["id"]
    identity = parent["id"]
    before = snapshot(service)
    detail = sdk.electrical_response(identity)
    assert detail["status"] == "succeeded"
    assert detail["result"]["summary"]["complete_pair_count"] == 1
    assert canonical(sdk.get("electrical-response/studies")[0]) == canonical(detail)
    manifest = sdk.export_electrical_response(identity)
    assert manifest["schema"] == "camera_electrical_response_study_export_v1"
    assert canonical(manifest["study"]) == canonical(detail)
    assert manifest["replay"]["plan_hash"] == proposal["plan_hash"]
    assert manifest["physical_validation"] == "not_established"
    downloads = service.registry.directory / "downloads"
    prior_downloads = set(downloads.glob("*")) if downloads.exists() else set()
    body = sdk.electrical_response_bundle(identity)
    assert body[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert json.loads(archive.read("manifest.json")) == manifest
        for child in manifest["children"]:
            raw = next(a for a in child["artifacts"] if a["name"] == "raw_groups.npy")
            content = archive.read(f"artifacts/{raw['sha256']}/{raw['name']}")
            assert len(content) == raw["bytes"]
            assert hashlib.sha256(content).hexdigest() == raw["sha256"]
            assert sdk.http.get(raw["url"]).content == content
            assert sdk.get("runs/" + child["id"])["status"] == "succeeded"
    assert set(downloads.glob("*")) == prior_downloads  # FileResponse cleans its temporary ZIP.
    assert snapshot(service) == before
    cached = sdk.submit_electrical_response(proposal)
    assert cached["id"] == identity and cached["cache_hit"] is True


def test_unknown_identity_and_wrong_template_source_are_errors_without_writes(workflow):
    service, sdk, assembly, scene, control, program, *_ = workflow
    before = snapshot(service)
    for suffix in ("", "/export", "/bundle"):
        result = sdk.http.get("/api/v1/electrical-response/studies/run-unknown" + suffix)
        assert result.status_code == 404
    with pytest.raises(httpx.HTTPStatusError) as error:
        sdk.electrical_response_template(assembly["id"], scene["id"], control["id"], "missing")
    assert error.value.response.status_code == 404
    with pytest.raises(httpx.HTTPStatusError) as error:
        sdk.electrical_response_template("missing", scene["id"], control["id"], program["id"])
    assert error.value.response.status_code == 404
    assert snapshot(service) == before
