"""New explicit experiments: actual service execution, decisions and isolated writes."""
from copy import deepcopy
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import design_experiments as design
from camera_platform.api import create_app
from camera_platform.electrical import default_electrical_model
from camera_platform.project_mode import configure
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.spatial import request_template


def spec():
    data = design.default_spec()
    data.update(model_ids=["r1-stop-clear-v1"], exposures_ms=[1.0, 2.0], gains=[1.0],
                training_scales=[1.0, 2.0], evaluation_scales=[1.5],
                evaluation_noise_repeats=1, acknowledge_simulation_limits=True)
    data["limits"].update(minimum_green_dn=1, minimum_green_snr_p10=1)
    return data


class AuthoredCase:
    def __init__(self):
        self.request = request_template()
        self.request["scene"]["spectral_irradiance"] = np.full((8, 8, 5), 1e-4)
        self.request["scene"]["sampling"]["full_sensor_shape"] = [8, 8]
        self.request.pop("exposure_s"); self.request.pop("frames")
        self.electrical_model = default_electrical_model(self.request["readout"])
        self.revision = "test-only-authored-input"

    def rate_request(self):
        return deepcopy(self.request)

    def identity(self):
        return {"revision": self.revision, "physical_validation": "not_established"}

    def assert_unchanged(self):
        pass


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    case = AuthoredCase()
    monkeypatch.setattr(design, "load_sources", lambda root, model_ids, scene_id: {
        model_id: case for model_id in model_ids})
    state = tmp_path / "r1-experiments"
    configure(state, project_id="test-design-experiments")
    app = create_app(state_dir=state,
                     workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["x-platform-token"] = client.get("/api/v1/session").json()["token"]
        yield app.state.service, client, case


def complete(service, client, payload):
    response = client.post("/api/v1/design-experiments", json=payload)
    assert response.status_code == 202, response.text
    queued = response.json()
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    result = service.execute_run(job)
    service.registry.finish(job["id"], result=result)
    response = client.get("/api/v1/design-experiments/" + job["id"])
    assert response.status_code == 200, response.text
    return response.json()


def test_sdk_submit_then_wait_uses_design_detail_even_with_fresh_client(workbench):
    service, http, _ = workbench
    sdk = object.__new__(PlatformClient)
    sdk.http = http
    queued = sdk.submit_design_experiment(spec())
    assert http.get(f"/api/v1/runs/{queued['id']}").status_code == 404
    lifecycle = sdk.job_status(queued["id"], expected_kind="design_experiment")
    assert lifecycle["schema"] == "camera_job_status_v1"
    assert lifecycle["status"] == "queued"
    assert "result" not in lifecycle and "artifacts" not in lifecycle

    job = service.registry.claim()
    assert job["id"] == queued["id"]
    service.registry.finish(job["id"], result=service.execute_run(job))

    fresh_sdk = object.__new__(PlatformClient)
    fresh_sdk.http = http
    completed = fresh_sdk.wait(queued["id"], timeout=1, poll_interval=0)
    assert completed["id"] == queued["id"]
    assert completed["job_kind"] == design.JOB_KIND
    assert completed["status"] == "succeeded"
    assert completed["result"]["summary"]["selection_frozen_before_evaluation"]
    assert "result" not in fresh_sdk.job_status(queued["id"])


def test_common_job_wait_preserves_unknown_and_wrong_kind_errors(workbench):
    service, http, _ = workbench
    sdk = object.__new__(PlatformClient)
    sdk.http = http

    with pytest.raises(httpx.HTTPStatusError) as unknown:
        sdk.wait("run-missing", timeout=1, poll_interval=0)
    assert unknown.value.response.status_code == 404
    assert "Unknown run" in unknown.value.response.text

    other = service.registry.submit(
        {"name": "Unrelated specialized job", "job_kind": "surrogate_training"},
        "unrelated-specialized-job",
    )
    assert http.get(f"/api/v1/runs/{other['id']}").status_code == 404
    with pytest.raises(httpx.HTTPStatusError) as wrong_kind:
        sdk.wait(other["id"], timeout=1, poll_interval=0,
                 expected_kind="design_experiment")
    assert wrong_kind.value.response.status_code == 422
    assert "job kind does not match" in wrong_kind.value.response.text

    service.registry.cancel(other["id"])
    finished = sdk.wait(other["id"], timeout=1, poll_interval=0)
    assert finished["job_kind"] == "surrogate_training"
    assert finished["status"] == "cancelled"
    assert "result" not in finished and "artifacts" not in finished


def test_explicit_submission_saved_real_RAW_and_frozen_selection(workbench):
    service, client, _ = workbench
    assert client.get("/api/v1/design-experiments/template").status_code == 200
    assert client.get("/api/v1/design-experiments").json() == []
    assert service.registry.list_runs() == []
    job = complete(service, client, spec())
    s = job["result"]["summary"]
    assert job["status"] == "succeeded"
    assert s["new_captures"] == 6 and s["selection_frozen_before_evaluation"]
    assert s["selection"]["selected_candidate_id"] == "r1-stop-clear-v1-e2-g1"
    assert len(s["rows"]) == 6 and s["evaluation"]["count"] == 2
    assert s["evaluation"]["reselection"] is False
    # Recorded original RAW is a real ndarray, not metadata-only diagnostics.
    row = s["rows"][0]
    raw = np.load(service.registry.blob_path(row["raw_artifact"]["sha256"]), allow_pickle=False)
    assert raw.shape == (8, 8) and raw.dtype.kind in "ui" and raw.max() > 64
    rgb = np.load(service.registry.blob_path(row["rgb_artifact"]["sha256"]), allow_pickle=False)
    assert rgb.shape == (8, 8, 3) and rgb.dtype == np.float64
    assert row["isp_profile_sha256"] and row["isp_input_raw_sha256"]
    preview = client.get(f"/api/v1/design-experiments/{job['id']}/cases/{row['case_id']}")
    assert preview.status_code == 200 and preview.json()["images"]["rgb"].startswith("data:image/png;base64,")
    count = len(service.registry.list_runs())
    assert client.get(f"/api/v1/design-experiments/{job['id']}/export").status_code == 200
    assert client.get("/api/v1/design-experiments").json()[0]["choice"] == "r1-stop-clear-v1-e2-g1"
    assert len(service.registry.list_runs()) == count
    assert client.post("/api/v1/design-experiments", json=spec()).json()["id"] == job["id"]


def test_no_feasible_is_preserved_and_evaluation_skipped(workbench):
    service, client, _ = workbench
    data = spec(); data["limits"]["minimum_green_snr_p10"] = 1e6
    result = complete(service, client, data)["result"]["summary"]
    assert result["selection"]["selected_candidate_id"] is None
    assert result["new_captures"] == 4
    assert result["evaluation"]["skipped_reason"] == "no_feasible_candidate"


def test_failed_evaluation_does_not_reselect(workbench):
    service, client, _ = workbench
    data = spec(); data["evaluation_scales"] = [30]
    result = complete(service, client, data)["result"]["summary"]
    assert result["selection"]["selected_candidate_id"] == "r1-stop-clear-v1-e2-g1"
    assert result["evaluation"]["status"] == "failed_samples"
    assert result["evaluation"]["reselection"] is False


def test_missing_opt_in_no_queue(workbench):
    service, client, _ = workbench
    data = spec(); data["acknowledge_simulation_limits"] = False
    assert client.post("/api/v1/design-experiments", json=data).status_code == 422
    assert service.registry.list_runs() == []


def test_source_drift_refused_before_capture(workbench):
    service, client, case = workbench
    assert client.post("/api/v1/design-experiments", json=spec()).status_code == 202
    job=service.registry.claim();case.revision="changed-after-queue"
    with pytest.raises(IntegrityError, match="changed"):
        service.execute_run(job)


def test_primary_state_rejected_before_source_or_submit(workbench):
    service, _, _ = workbench
    original = service.registry.directory
    service.registry.directory = Path(service.workspace)/"outputs/camera-platform"
    try:
        with pytest.raises(ValueError, match="explicit R1"):
            design.submit(service, spec())
    finally:
        service.registry.directory=original


@pytest.mark.parametrize("change", [
    {"gains": [1, float("nan")]}, {"exposures_ms": [1, 1]}, {"model_ids": ["../outside"]},
    {"seed": True}, {"evaluation_noise_repeats": 5},
    {"model_ids": ["model-clear", "model-radial", "model-quarter"], "exposures_ms": [1, 2, 3, 4],
     "gains": [1, 2, 3, 4]},
])
def test_invalid_specs_fail_without_execution(change):
    payload=spec();payload.update(change)
    with pytest.raises(ValueError):
        design.specification(payload)


def test_input_transform_preserves_source_and_throughput():
    case=AuthoredCase(); before=case.rate_request()["scene"]["spectral_irradiance"]
    changed=design.input_request(case, 2, evaluation_index=0)
    np.testing.assert_array_equal(changed["scene"]["spectral_irradiance"], before[::-1]*2)
    np.testing.assert_array_equal(case.rate_request()["scene"]["spectral_irradiance"], before)


def test_distinct_fractional_settings_never_share_a_candidate_identity():
    data = spec()
    data.update(exposures_ms=[1.0000001, 1.0000002], gains=[1.0000001, 1.0000002])
    candidates = design.candidate_grid(design.specification(data))
    assert len({c["candidate_id"] for c in candidates}) == 4
    assert [c["candidate_id"] for c in design.candidate_grid(design.specification(spec()))] == [
        "r1-stop-clear-v1-e1-g1", "r1-stop-clear-v1-e2-g1"]


@pytest.mark.parametrize("failed_name", ["metrics.csv", "experiment-result.json"])
def test_late_storage_failure_keeps_completed_RAW_and_choice(workbench, monkeypatch, failed_name):
    service, client, _ = workbench
    response = client.post("/api/v1/design-experiments", json=spec())
    assert response.status_code == 202, response.text
    job = service.registry.claim()
    original_store = service.registry.store_blob

    def failing_store(content, name, media_type):
        if name in {failed_name, "partial-experiment.json"}:
            raise OSError("Injected result-storage failure: " + name)
        return original_store(content, name, media_type)

    monkeypatch.setattr(service.registry, "store_blob", failing_store)
    with pytest.raises(design.ExperimentFailure) as caught:
        service.execute_run(job)
    evidence = caught.value.evidence_result
    assert evidence["summary"]["status"] == "failed"
    assert evidence["summary"]["new_captures"] == 6
    assert len(evidence["summary"]["rows"]) == 6
    assert evidence["summary"]["selection"]["selected_candidate_id"] == "r1-stop-clear-v1-e2-g1"
    assert "partial_evidence_store_error" in evidence["summary"]
    assert any(a["name"] == "selection-before-evaluation.json" for a in evidence["artifacts"])
    assert len([a for a in evidence["artifacts"] if a["name"].endswith("-raw.npy")]) == 6
    # Publication may still succeed even when the artifact store is unavailable.
    # Preserve completed work instead of silently resampling the experiment.
    service.registry.finish(job["id"], result=evidence, error=str(caught.value))
    saved = client.get("/api/v1/design-experiments/" + job["id"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "failed"
    assert len(saved.json()["result"]["summary"]["rows"]) == 6
