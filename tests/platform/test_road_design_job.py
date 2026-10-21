"""Typed job lifecycle, snapshot binding and package isolation for road studies."""

import fcntl
import json

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.road_design_inputs import SCHEMA as STUDY_SCHEMA
from camera_platform.road_design_inputs import write_json
from camera_platform.road_design_study import run_study
from camera_platform.road_rgb_source import file_sha256, json_sha256
from camera_platform.sdk import PlatformClient


@pytest.fixture
def road_api(tmp_path, monkeypatch):
    package = tmp_path / "study"
    package.mkdir()
    spec = {
        "schema": STUDY_SCHEMA,
        "baseline_id": "L1-S1",
        "images": [
            {"id": str(i), "log_token": str(i), "split": split}
            for split, start in (("selection", 0), ("evaluation", 6))
            for i in range(start, start + 6)
        ],
        "candidates": [
            {"id": cid, "lens": {"identity": {"id": cid}}, "sensor": {"revision": cid}}
            for cid in ("L1-S1", "L1-S2", "L2-S1", "L2-S2")
        ],
    }
    write_json(package / "specification.json", spec)
    sha = file_sha256(package / "specification.json")
    write_json(
        package / "specification-binding.json",
        {"sha256": sha, "semantic_sha256": json_sha256(spec)},
    )
    calls = []

    def computation(workspace, target, *, stage):
        assert workspace == tmp_path and target == package
        calls.append(stage)
        (target / "report.html").write_text("<html>Fixed road decision</html>")
        (target / "raw.npy").write_bytes(b"original raw bytes")
        write_json(
            target / "package-manifest.json",
            {"files": {name: file_sha256(target / name) for name in ("report.html", "raw.npy")}},
        )
        return {"status": "baseline_retained", "recommended_id": "L1-S1"}

    monkeypatch.setattr("camera_platform.road_design_job.run_study", computation)
    app = create_app(tmp_path / "registry", workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["x-platform-token"] = http.get("/api/v1/bootstrap").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = http
        payload = {
            "schema": "camera_road_design_job_v1",
            "name": "Pinned road study",
            "package_path": "study",
            "specification_sha256": sha,
            "stage": "full",
        }
        yield app, http, sdk, payload, package, calls


def test_road_submit_common_wait_cancel_and_verified_files(road_api):
    app, http, sdk, payload, package, calls = road_api
    assert sdk.road_design_study_template()["specification"]["stage"] == "full"
    queued = sdk.submit_road_design_study(payload)
    assert queued["status"] == "queued"
    claimed = app.state.service.registry.claim()
    result = app.state.service.execute_run(claimed)
    app.state.service.registry.finish(queued["id"], result=result)
    completed = sdk.wait(
        queued["id"], timeout=1, poll_interval=0, expected_kind="road_design_study"
    )
    assert completed["result"]["summary"]["recommended_id"] == "L1-S1"
    assert calls == ["full"]
    url = completed["result"]["summary"]["viewer_path"]
    assert http.get(url).status_code == 200
    assert http.get(url.replace("report.html", "raw.npy")).content == b"original raw bytes"
    (package / "raw.npy").write_bytes(b"changed raw bytes")
    assert http.get(url.replace("report.html", "raw.npy")).status_code == 409
    assert http.get(url.replace("report.html", "specification-binding.json")).status_code == 404
    assert http.get(url.replace("report.html", "..%2f..%2fsecret")).status_code == 404
    assert http.get("/api/v1/runs/" + queued["id"]).status_code == 404
    pending = sdk.submit_road_design_study({**payload, "stage": "first-photo"})
    cancelled = http.post("/api/v1/road-design-studies/" + pending["id"] + "/cancel", json={})
    assert cancelled.json()["status"] == "cancelled"
    assert app.state.service.registry.claim() is None
    manifest = package / "package-manifest.json"
    modified = json.loads(manifest.read_text())
    modified["files"]["raw.npy"] = file_sha256(package / "raw.npy")
    write_json(manifest, modified)
    assert http.get("/api/v1/road-design-studies/" + queued["id"]).status_code == 409


def test_completed_full_study_can_return_first_photo_without_a_separate_first_job(
    tmp_path, monkeypatch
):
    spec = {"images": [{"id": "first"}], "candidates": [{"id": "A"}, {"id": "B"}]}
    monkeypatch.setattr(
        "camera_platform.road_design_study.bound_specification",
        lambda *a: (spec, {"sha256": "pinned"}),
    )
    monkeypatch.setattr("camera_platform.road_design_study.verify_package", lambda *a: {})
    write_json(tmp_path / "decision.json", {"status": "baseline_retained"})
    write_json(tmp_path / "package-manifest.json", {})
    write_json(
        tmp_path / "selection-records.json",
        [
            {"candidate_id": "A", "image_id": "first"},
            {"candidate_id": "B", "image_id": "first"},
            {"candidate_id": "A", "image_id": "other"},
        ],
    )
    result = run_study(tmp_path, tmp_path, stage="first-photo")
    assert result["source"] == "verified complete study"
    assert len(result["records"]) == 2


def test_road_cannot_execute_changed_specification_or_outside_package(road_api):
    _, http, _, payload, package, _ = road_api
    for changed in (
        {"package_path": "../study"},
        {"package_path": str(package)},
        {"specification_sha256": "0" * 64},
        {"stage": "arbitrary"},
    ):
        assert http.post(
            "/api/v1/road-design-studies", json={**payload, **changed}
        ).status_code in {409, 422}
    (package / "specification.json").write_text("{}")
    assert http.post("/api/v1/road-design-studies", json=payload).status_code == 422


def test_concurrent_package_execution_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("camera_platform.road_design_study._run_study", lambda *a, **kw: {})
    with (tmp_path / ".execution.lock").open("a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="already running"):
            run_study(tmp_path, tmp_path)
    assert run_study(tmp_path, tmp_path) == {}


def test_interrupted_execution_preserves_inputs_and_can_resume(tmp_path, monkeypatch):
    (tmp_path / "immutable.npy").write_bytes(b"original")

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr("camera_platform.road_design_study._run_study", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_study(tmp_path, tmp_path)
    assert json.loads((tmp_path / "progress.json").read_text())["status"] == "interrupted"
    assert (tmp_path / "immutable.npy").read_bytes() == b"original"
    monkeypatch.setattr("camera_platform.road_design_study._run_study", lambda *a, **kw: {})
    assert run_study(tmp_path, tmp_path) == {}
