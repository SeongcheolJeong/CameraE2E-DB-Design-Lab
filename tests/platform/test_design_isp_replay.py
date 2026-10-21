"""R1 saved-design-case ISP replay: one original RAW, two immutable RGB versions."""

from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_design_experiments import AuthoredCase, complete, spec

from camera_platform import design_experiments as design
from camera_platform import design_isp_replay as replay
from camera_platform import isp
from camera_platform.api import create_app
from camera_platform.project_mode import configure
from camera_platform.registry import IntegrityError, canonical


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    case = AuthoredCase()
    monkeypatch.setattr(design, "load_sources", lambda root, model_ids, scene_id: {
        model_id: case for model_id in model_ids})
    state = tmp_path / "r1-replay"
    configure(state, project_id="test-design-isp-replay")
    app = create_app(state_dir=state, workspace=tmp_path,
                     start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["x-platform-token"] = client.get("/api/v1/session").json()["token"]
        yield app.state.service, client, case


def request(experiment, case, *, gain=1.4):
    profile = deepcopy(experiment["isp_profile"])
    profile["white_balance"]["gains_rgb"] = [gain, 1.0, 1.0]
    return {"schema": replay.SCHEMA, "name": "R1 preserved RAW · warm WB",
            "experiment_id": experiment["id"], "case_id": case["case_id"],
            "profile": profile}


def finish(service, client, payload):
    response = client.post("/api/v1/design-isp-replays", json=payload)
    assert response.status_code == 202, response.text
    queued = response.json()
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    service.registry.finish(job["id"], result=service.execute_run(job))
    response = client.get("/api/v1/design-isp-replays/" + job["id"])
    assert response.status_code == 200, response.text
    return response.json()


def test_replay_changes_only_rgb_and_reopens_with_source_and_profile_binding(workbench):
    service, client, _ = workbench
    experiment = complete(service, client, spec())
    case = experiment["result"]["summary"]["rows"][0]
    raw_before = service.registry.blob_path(case["raw_artifact"]["sha256"]).read_bytes()
    rgb_before = service.registry.blob_path(case["rgb_artifact"]["sha256"]).read_bytes()
    selection_before = deepcopy(experiment["result"]["summary"]["selection"])
    job = finish(service, client, request(experiment, case))
    assert job["status"] == "succeeded"
    summary = job["result"]["summary"]
    assert summary["new_captures"] == 0 and summary["input_unchanged"] is True
    assert summary["source_raw_sha256"] == case["raw_artifact"]["sha256"]
    assert summary["source_original_rgb_sha256"] == case["rgb_artifact"]["sha256"]
    assert summary["profile_sha256"] == job["profile_sha256"]
    assert summary["comparison"]["max_abs_rgb_delta"] > 0
    assert summary["comparison"]["mean_abs_rgb_delta"] > 0
    artifacts = {item["name"]: item for item in job["artifacts"]}
    before = np.load(service.registry.blob_path(case["rgb_artifact"]["sha256"]), allow_pickle=False)
    after = np.load(service.registry.blob_path(artifacts["replay-rgb.npy"]["sha256"]),
                    allow_pickle=False)
    assert before.shape == after.shape and before.dtype == after.dtype == np.float64
    assert np.any(before != after)
    assert service.registry.blob_path(case["raw_artifact"]["sha256"]).read_bytes() == raw_before
    assert service.registry.blob_path(case["rgb_artifact"]["sha256"]).read_bytes() == rgb_before
    reopened = client.get("/api/v1/design-experiments/" + experiment["id"]).json()
    assert reopened["result"]["summary"]["selection"] == selection_before
    assert client.get("/api/v1/design-isp-replays", params={
        "experiment_id": experiment["id"], "case_id": case["case_id"]}).json()[0]["id"] == job["id"]
    exported = client.get("/api/v1/design-isp-replays/" + job["id"] + "/export")
    assert exported.status_code == 200 and exported.json()["id"] == job["id"]
    repeated = client.post("/api/v1/design-isp-replays", json=request(experiment, case))
    assert repeated.json()["id"] == job["id"]


def test_replay_rejects_partial_old_format_and_corrupt_original_artifact(workbench):
    service, client, _ = workbench
    experiment = complete(service, client, spec())
    case = experiment["result"]["summary"]["rows"][0]
    payload = request(experiment, case)
    blob = service.registry.blob_path(case["raw_artifact"]["sha256"])
    original = blob.read_bytes()
    blob.write_bytes(b"corrupt")
    try:
        response = client.post("/api/v1/design-isp-replays", json=payload)
        assert response.status_code == 409, response.text
        assert "checksum" in response.text.lower() or "bytes differ" in response.text
    finally:
        blob.write_bytes(original)
    bad = deepcopy(case)
    bad["raw_format"] = {"cfa": "RGGB", "black_level_dn": 64, "white_level_dn": 4095}
    with pytest.raises(isp.ISPError, match="explicit complete"):
        isp._input(np.zeros((8, 8), dtype=np.int32), bad["raw_format"], bad["geometry"])


def test_replay_refuses_profile_and_source_drift_before_processing(workbench, monkeypatch):
    service, client, _ = workbench
    experiment = complete(service, client, spec())
    case = experiment["result"]["summary"]["rows"][0]
    response = client.post("/api/v1/design-isp-replays", json=request(experiment, case))
    assert response.status_code == 202, response.text
    queued = service.registry.claim()
    changed = deepcopy(queued)
    changed["profile"]["white_balance"]["gains_rgb"][0] = 1.7
    with pytest.raises(IntegrityError, match="profile"):
        replay.execute(service, changed)
    original_source = replay.source_case

    def drifted(*args):
        snapshot, raw, before = original_source(*args)
        snapshot["row"]["candidate_id"] = "changed"
        return snapshot, raw, before

    monkeypatch.setattr(replay, "source_case", drifted)
    with pytest.raises(IntegrityError, match="source"):
        replay.execute(service, queued)


def test_replay_bad_input_does_not_queue_or_recapture(workbench):
    service, client, _ = workbench
    experiment = complete(service, client, spec())
    case = experiment["result"]["summary"]["rows"][0]
    count = len(service.registry.list_runs())
    payload = request(experiment, case)
    payload["profile"]["graph"] = ["demosaic", "tone"]
    response = client.post("/api/v1/design-isp-replays", json=payload)
    assert response.status_code == 422, response.text
    assert len(service.registry.list_runs()) == count
    original = client.get("/api/v1/design-experiments/" + experiment["id"]).json()
    assert canonical(original) == canonical(experiment)


def test_older_partial_case_contract_fails_closed(workbench, monkeypatch):
    service, client, _ = workbench
    experiment = complete(service, client, spec())
    case = experiment["result"]["summary"]["rows"][0]
    original_get = design.get

    def partial(service_, identity):
        record = deepcopy(original_get(service_, identity))
        record["result"]["summary"]["rows"][0].pop("geometry")
        return record

    monkeypatch.setattr(design, "get", partial)
    with pytest.raises(ValueError, match="full RAW/ISP replay contract"):
        replay.source_case(service, experiment["id"], case["case_id"])


def test_replay_storage_failure_preserves_original_case(workbench, monkeypatch):
    service, client, _ = workbench
    experiment = complete(service, client, spec())
    case = experiment["result"]["summary"]["rows"][0]
    original = client.get("/api/v1/design-experiments/" + experiment["id"]).json()
    queued = client.post("/api/v1/design-isp-replays", json=request(experiment, case))
    assert queued.status_code == 202, queued.text
    job = service.registry.claim()
    store = service.registry.store_blob

    def fail_summary(content, name, media_type):
        if name == "replay-summary.json":
            raise OSError("Injected replay summary failure")
        return store(content, name, media_type)

    monkeypatch.setattr(service.registry, "store_blob", fail_summary)
    with pytest.raises(replay.ReplayExecutionError) as caught:
        service.execute_run(job)
    evidence = caught.value.evidence_result
    assert evidence["summary"]["status"] == "failed"
    assert evidence["summary"]["new_captures"] == 0
    service.registry.finish(job["id"], result=evidence, error=str(caught.value))
    saved = client.get("/api/v1/design-isp-replays/" + job["id"])
    assert saved.status_code == 200 and saved.json()["status"] == "failed"
    reopened = client.get("/api/v1/design-experiments/" + experiment["id"]).json()
    assert canonical(reopened) == canonical(original)


def test_cancelled_replay_does_not_touch_original_case(workbench):
    service, client, _ = workbench
    experiment = complete(service, client, spec())
    case = experiment["result"]["summary"]["rows"][0]
    queued = client.post("/api/v1/design-isp-replays", json=request(experiment, case))
    identity = queued.json()["id"]
    cancelled = client.post("/api/v1/design-isp-replays/" + identity + "/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert service.registry.claim() is None
    assert service.registry.get_run(experiment["id"])["status"] == "succeeded"


def test_registered_second_model_and_scene_replay_without_qa(tmp_path):
    state = tmp_path / "registered-project"
    configure(state, project_id="registered-design-isp-replay")
    app = create_app(state_dir=state, workspace=tmp_path,
                     start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["x-platform-token"] = client.get("/api/v1/session").json()["token"]
        template = client.get("/api/v1/design-experiments/template").json()["specification"]
        template.update(model_ids=["r1-clear-lowgreen-v1"], scene_id="r1-checker-v1",
                        exposures_ms=[2.0], gains=[1.0], training_scales=[1.0],
                        evaluation_scales=[1.0], evaluation_noise_repeats=0,
                        acknowledge_simulation_limits=True)
        template["limits"].update(minimum_green_dn=0, minimum_green_snr_p10=0,
                                  maximum_well_fraction=1, maximum_white_fraction=1)
        service = app.state.service
        experiment = complete(service, client, template)
        case = experiment["result"]["summary"]["rows"][0]
        assert case["raw_format"]["response_type"] == "linear"
        assert case["geometry"]["kind"] == "native_sensor"
        replayed = finish(service, client, request(experiment, case, gain=1.6))
        assert replayed["result"]["summary"]["new_captures"] == 0
        assert replayed["result"]["summary"]["comparison"]["max_abs_rgb_delta"] > 0
        assert service.registry.list("captures") == []
