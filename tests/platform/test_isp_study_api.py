"""HTTP/SDK study execution and case evidence bind the exact acquisition scope."""

import hashlib
import io
import zipfile
from copy import deepcopy

import pytest
from test_advanced_isp_api import advanced
from test_case_reviews import rows
from test_imaging_api import api as api
from test_imaging_api import checked, finish

from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient


def study_fixture(api):
    service, client, _ = api
    assembly, original, calibration, model, _ = advanced(api)
    source = finish(
        service,
        checked(
            client.post(
                "/api/v1/runs",
                json={
                    "assembly_id": assembly["id"],
                    "test_id": original["test_id"],
                    "scene_id": original["scene_id"],
                    "plugin_id": original["plugin_id"],
                    "parameters": {"frames": 4, "seed": 281},
                },
            ),
            202,
        ),
    )
    sdk = PlatformClient.__new__(PlatformClient)
    sdk.http = client
    spec = sdk.isp_study_template("run", source["id"], calibration["id"], model["id"])
    spec["name"] = "API fixed RAW study"
    spec["axes"] = [{"parameter": "raw_denoise.strength", "values": [0.0, 0.8]}]
    return sdk, source, spec


def completed(api):
    service, _, _ = api
    sdk, source, spec = study_fixture(api)
    plan = sdk.plan_isp_study(spec)
    study = sdk.submit_isp_study(plan)
    for cell in study["cells"]:
        finish(service, {"id": cell["job_id"]})
    return sdk, source, study, sdk.wait_isp_study(study["id"])


def case_payload(source, study, *, selection_id=None):
    return {
        "request_id": "api-isp-study-case-001",
        "name": "Same RAW noise trade-off",
        "question": "Which candidate warrants a separate acquisition?",
        "requirements": [
            {
                "key": "noise",
                "description": "Fixed acquisition residual",
                "test_id": source["test_id"],
            }
        ],
        "assembly_ids": [source["assembly_id"]],
        "evidence": [
            {
                "kind": "isp_studies",
                "id": study["id"],
                "requirement_keys": ["noise"],
                "dataset_requirements": {"flat": ["noise"]},
                "selection_id": selection_id,
            }
        ],
        "decision": {"status": "open", "rationale": "Physical camera accuracy is unverified"},
        "unresolved": ["Independent reference and hardware comparison remain"],
        "next_experiments": [],
    }


def test_read_only_plan_owned_cells_and_sdk_retry_then_exact_arrays_bundle(api):
    service, client, _ = api
    sdk, source, spec = study_fixture(api)
    before = rows(service.registry)
    plan = sdk.plan_isp_study(spec)
    assert rows(service.registry) == before
    assert plan["plan_hash"] == sdk.plan_isp_study(spec)["plan_hash"]
    study = sdk.submit_isp_study(plan)
    after = rows(service.registry)
    assert sdk.submit_isp_study(plan) == study
    assert rows(service.registry) == after
    assert 2 <= len(study["cells"]) <= 3
    for cell in study["cells"]:
        identity = cell["job_id"]
        for route in ("runs", "replays"):
            assert client.get(f"/api/v1/{route}/{identity}").status_code == 404
        result = finish(service, {"id": identity})
        assert result["status"] == "succeeded"
        assert (
            result["replay_request"]["source"]["raw_sha256"] == study["datasets"][0]["raw_sha256"]
        )
        assert checked(client.get("/api/v1/isp-study-cells/" + identity), 200) == result
    analysis = sdk.wait_isp_study(study["id"])
    assert analysis["status"] == "complete"
    assert analysis["physical_validation"] == "not_established"
    assert all(r["metrics"]["temporal_residual_std_dn"]["status"] == "ok" for r in analysis["rows"])
    assert all(r["metrics"]["mean_bias_dn"]["status"] == "unknown" for r in analysis["rows"])
    manifest = checked(client.get(f"/api/v1/isp-studies/{study['id']}/export"), 200)
    assert manifest == checked(client.get(f"/api/v1/isp-studies/{study['id']}/export"), 200)
    from camera_platform.evidence_package import read_bundle

    bundle = sdk.isp_study_bundle(study["id"])
    assert read_bundle(io.BytesIO(bundle)) == manifest
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        for name in archive.namelist():
            if name.startswith("artifacts/"):
                assert hashlib.sha256(archive.read(name)).hexdigest() == name.split("/")[1]
    assert service.registry.get_run(source["id"]) == source
    assert not list((service.registry.directory / "downloads").glob("*.zip"))


def test_case_freezes_selection_terminal_metrics_and_workbench_detail(api):
    service, client, _ = api
    sdk, source, study, analysis = completed(api)
    candidate = study["candidates"][-1]["candidate_id"]
    selection = sdk.select_isp_candidate(
        study["id"], candidate, "Compare on a separate acquisition", analysis["analysis_sha256"]
    )
    assert (
        sdk.select_isp_candidate(
            study["id"], candidate, "Compare on a separate acquisition", analysis["analysis_sha256"]
        )
        == selection
    )
    document = case_payload(source, study, selection_id=selection["id"])
    saved = checked(client.post("/api/v1/case_reviews", json=document), 201)
    evidence = saved["snapshots"]["evidence"][0]
    assert evidence["dataset_requirements"] == {"flat": ["noise"]}
    assert evidence["selection_id"] == selection["id"]
    assert evidence["record_sha256"] == digest(study)
    linked = {entry["kind"]: entry for entry in evidence["linked_records"]}
    assert linked["isp_selections"]["snapshot"] == selection
    assert saved["authority"]["physical_validation"] is False
    before = rows(service.registry)
    assert checked(client.get("/api/v1/case_reviews/" + saved["id"]), 200) == saved
    assert rows(service.registry) == before
    workbench = checked(client.get("/api/v1/workbench"), 200)
    assert study["id"] in {r["id"] for r in workbench["isp_studies"]}
    detail = checked(client.get("/api/v1/workbench/isp_studies/" + study["id"]), 200)
    assert detail["record"] == study and detail["record_sha256"] == digest(study)


@pytest.mark.parametrize("damage", ["dataset", "test", "assembly", "union"])
def test_case_rejects_misleading_acquisition_scope_without_publishing(api, damage):
    service, client, bootstrap = api
    _, source, study, _ = completed(api)
    document = case_payload(source, study)
    if damage == "dataset":
        document["evidence"][0]["dataset_requirements"] = {"invented": ["noise"]}
    elif damage == "test":
        document["requirements"][0]["test_id"] = bootstrap["tests"][0]["id"]
    elif damage == "assembly":
        document["assembly_ids"] = [bootstrap["assemblies"][0]["id"]]
    else:
        document["evidence"][0]["dataset_requirements"] = {"flat": ["unlisted"]}
    before = rows(service.registry)
    assert client.post("/api/v1/case_reviews", json=document).status_code == 422
    assert rows(service.registry) == before


def test_incomplete_case_and_stale_selection_are_rejected_then_cancel_is_terminal(api):
    service, client, _ = api
    sdk, source, spec = study_fixture(api)
    study = sdk.submit_isp_study(sdk.plan_isp_study(spec))
    assert client.post("/api/v1/case_reviews", json=case_payload(source, study)).status_code == 409
    assert client.post(
        f"/api/v1/isp-studies/{study['id']}/selection",
        json={
            "candidate_id": study["candidates"][0]["candidate_id"],
            "rationale": "Premature",
            "analysis_sha256": "0" * 64,
        },
    ).status_code in {409, 422}
    sdk.cancel_isp_study(study["id"])
    analysis = sdk.wait_isp_study(study["id"])
    assert all(row["status"] == "cancelled" for row in analysis["rows"])
    assert analysis["aggregate"]["pareto_provisional"] is True
    saved = checked(client.post("/api/v1/case_reviews", json=case_payload(source, study)), 201)
    assert saved["authority"]["physical_validation"] is False


def test_changed_submission_and_wrong_route_kind_never_create_extra_jobs(api):
    service, client, _ = api
    sdk, source, spec = study_fixture(api)
    plan = sdk.plan_isp_study(spec)
    modified = deepcopy(plan["specification"])
    modified["question"] += " changed"
    before = rows(service.registry)
    assert (
        client.post(
            "/api/v1/isp-studies",
            json={
                "specification": modified,
                "plan_hash": plan["plan_hash"],
            },
        ).status_code
        == 409
    )
    assert rows(service.registry) == before
    assert client.get("/api/v1/isp-study-cells/" + source["id"]).status_code == 404
    assert client.get("/api/v1/templates/isp-study").status_code == 422
    assert client.post("/api/v1/isp-references/import", json={}).status_code == 415


def test_study_cells_cannot_bypass_evidence_or_camera_validation_boundaries(api):
    service, _, _ = api
    sdk, source, spec = study_fixture(api)
    study = sdk.submit_isp_study(sdk.plan_isp_study(spec))
    identity = study["cells"][0]["job_id"]
    before = rows(service.registry)
    with pytest.raises(ValueError, match="owning ISP Study"):
        service.export_run(identity)
    with pytest.raises(ValueError, match="camera measurement prediction"):
        service.validate({
            "run_id": identity, "measurement_id": "unread-measurement",
            "test_id": source["test_id"],
        })
    assert rows(service.registry) == before
