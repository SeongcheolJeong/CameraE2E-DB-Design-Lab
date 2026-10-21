"""Public original-byte import, Worker, publication and replay integration."""

import json
from copy import deepcopy

import pytest
from test_hdr_workflow import workflow as workflow
from test_isp_capture_studies import selector
from test_readout_fits import build_fixture

from camera_platform import isp_capture_source, readout_observations
from camera_platform.evidence_package import read_bundle
from camera_platform.readout_fit_contracts import FIELDS
from camera_platform.registry import digest
from camera_platform.worker import execute


@pytest.mark.parametrize("shared", [False, True])
def test_public_sdk_worker_publication_replay_and_original_evidence(workflow, tmp_path, shared):
    service, sdk, assembly, *_, test, _ = workflow
    _, base, original_records, spec = build_fixture((service, assembly, test), shared=shared)
    imported = []
    for i, observation in enumerate(original_records):
        metadata = {k: deepcopy(observation[k]) for k in FIELDS}
        # Text formatting remains original evidence, including CRLF and Unicode.
        metadata["name"] = f"공개 API 교정 관측 {i}"
        content = (json.dumps(metadata, ensure_ascii=False, indent=3).replace("\n", "\r\n")
                   + "\r\n").encode()
        evidence = (json.dumps(readout_observations.evidence_template(metadata), indent=2)
                    + "\n").encode()
        metadata_path = tmp_path / f"observation-{i}.json"
        evidence_path = tmp_path / f"anchor-{i}.json"
        metadata_path.write_bytes(content)
        evidence_path.write_bytes(evidence)
        record = sdk.import_readout_observations(metadata_path, evidence_path)
        imported.append(record)
        assert sdk.get("readout-observations/" + record["id"]) == record
        assert sdk.get("readout_observations/" + record["id"]) == record
        originals = {"readout-fit-observation-metadata.json": content,
                     "readout-fit-observation-evidence.json": evidence}
        for artifact in record["artifacts"]:
            reply = sdk.http.get(artifact["url"])
            assert reply.status_code == 200
            assert reply.content == originals[artifact["name"]]
    spec.update(training_observation_ids=[imported[0]["id"]],
                validation_observation_ids=[imported[1]["id"]])
    before = len(service.registry.list_runs())
    plan = sdk.plan_readout_fit(spec)
    assert len(service.registry.list_runs()) == before
    job = sdk.submit_readout_fit(spec, plan["plan_hash"])
    assert sdk.submit_readout_fit(spec, plan["plan_hash"])["id"] == job["id"]
    claimed = service.registry.claim()
    assert claimed["id"] == job["id"]
    execute(service, job["id"])
    job = sdk.get("readout-fits/" + job["id"])
    assert job["status"] == "succeeded", job["error"]
    assert service.registry.list("readout_calibrations") == [base]
    evidence = sdk.get("readout-fits/" + job["id"] + "/export")
    binding = evidence["publication_binding"]
    assert binding == {"request_sha256": job["content_hash"],
                       "result_sha256": digest(job["result"])}
    wrong = sdk.http.post("/api/v1/readout-fits/" + job["id"] + "/publish", json={
        "name": "wrong result", "revision": "1", **binding, "result_sha256": "0" * 64,
    })
    assert wrong.status_code == 409
    fitted = sdk.publish_readout_fit(job["id"], name="Public fitted response", revision="1",
                                    **binding)
    audit_count = len(service.registry.audit()["events"])
    assert sdk.publish_readout_fit(job["id"], name="Public fitted response", revision="1",
                                   **binding) == fitted
    assert len(service.registry.audit()["events"]) == audit_count
    assert sdk.get("readout-calibrations/" + fitted["id"]) == fitted
    assert not fitted["authority"]["physical_validation"]
    assert sdk.http.get("/api/v1/runs/" + job["id"]).status_code == 404
    assert sdk.http.get("/api/v1/runs/" + job["id"] + "/export").status_code == 422
    bundle = sdk.http.get("/api/v1/readout-fits/" + job["id"] + "/bundle")
    assert bundle.status_code == 200, bundle.text
    target = tmp_path / "fit-evidence.zip"
    target.write_bytes(bundle.content)
    assert read_bundle(target, expected_manifest_sha256=digest(evidence)) == evidence
    workbench = sdk.get("workbench")
    assert any(r["id"] == job["id"] for r in workbench["readout_fits"])
    assert not any(r["id"] == job["id"] for r in workbench["runs"])
    for collection, record in (("readout_fits", job), ("readout_observations", imported[0]),
                                ("readout_calibrations", fitted)):
        assert sdk.get(f"workbench/{collection}/" + record["id"])["record"] == record
    capture = imported[1]["capture_snapshot"]
    selected = selector(fitted, "hcg" if shared else None)
    source, _, _ = isp_capture_source.resolve_inputs(service, capture["id"], selected)
    lineage = source["response_calibration_lineage"]
    assert {r["id"] for r in lineage["readout_observations"]} == {r["id"] for r in imported}
    assert [r["id"] for r in lineage["readout_fit_jobs"]] == [job["id"]]
    assert ("capture-id:" + capture["id"]
            in source["acquisition_lineage"]["decoder_evaluation_tokens"])
    profile = sdk.template("isp-profile", version=3, source_kind="capture", source_id=capture["id"],
                           selector=json.dumps(selected))
    # This fixture explicitly declares its new estimated ISP at the applied gain.
    # The template correctly retains the parent ISP domain until the author edits it.
    gain = capture["frames"][0]["applied"]["gain"]
    profile["valid_domain"]["gain"] = {"min": gain, "max": gain, "unit": "1"}
    model = sdk.post("models", profile)
    response = sdk.http.post("/api/v1/replays", json={
        "schema": "camera_isp_replay_v3", "name": "Fitted response replay",
        "source_kind": "capture", "source_id": capture["id"], "selector": selected,
        "isp_model_id": model["id"],
    })
    assert response.status_code == 202, response.text
    replay = response.json()
    assert service.registry.claim()["id"] == replay["id"]
    execute(service, replay["id"])
    replay = sdk.get("replays/" + replay["id"])
    assert replay["status"] == "succeeded", replay["error"]
    exported = sdk.get("replays/" + replay["id"] + "/export")
    assert {r["id"] for r in exported["readout_observations"]} == {r["id"] for r in imported}
    assert [r["id"] for r in exported["readout_fit_jobs"]] == [job["id"]]


def test_observation_multipart_requires_exactly_one_original_metadata(workflow):
    _, sdk, *_ = workflow
    response = sdk.http.post("/api/v1/readout-observations/import", data={"metadata": "{}"},
                            files={"metadata_file": ("metadata.json", b"{}"),
                                   "evidence": ("evidence.json", b"{}")})
    assert response.status_code == 422 and "exactly one" in response.text
    response = sdk.http.post("/api/v1/readout-observations/import", data={"metadata": "{}"},
                            files={"evidence": ("evidence.json", b"x" * (2 * 1024**2 + 1))})
    assert response.status_code == 413


def test_fitting_submission_needs_a_checked_plan(workflow):
    service, sdk, *_ = workflow
    before = len(service.registry.list_runs())
    response = sdk.http.post("/api/v1/readout-fits", json={"specification": {}})
    assert response.status_code == 422
    assert len(service.registry.list_runs()) == before
    html = sdk.http.get("/").text
    assert '/readout-fitting.js' in html
    assert sdk.http.get("/readout-fitting.js").status_code == 200
