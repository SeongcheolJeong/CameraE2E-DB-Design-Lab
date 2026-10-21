"""Workshop publication, same-RAW lineage and bounded project storage."""

import copy
import io
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from camera_platform import design_workshop_storage as storage
from camera_platform import reference_workshop_job as job
from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient


def test_configuration_and_sweep_contract():
    spec = job.defaults()
    normalized = job.specification(spec)
    assert normalized["lens"]["optical_profile"] == "geometric_retrace"
    assert normalized["isp"]["display_white_level"] == 10
    assert (
        job.specification({"isp": {"display_white_level": 10}})["isp"]["display_white_level"] == 10
    )
    cells = job.expand_sweep(spec, [{"path": "pixel.cfa_thickness_scale", "values": [0.9, 1, 1.1]}])
    assert [x["pixel"]["cfa_thickness_scale"] for x in cells] == [0.9, 1, 1.1]
    assert spec["pixel"]["cfa_thickness_scale"] == 1
    for patch in (
        {"gain": float("nan")},
        {"frames": True},
        {"isp": {"display_white_level": 0}},
        {"pixel": {"cfa_thickness_scale": 0.95}},
    ):
        with pytest.raises(ValueError):
            job.specification({**spec, **patch})
    with pytest.raises(ValueError):
        job.expand_sweep(spec, [{"path": "lens.sensor_shift_um", "values": list(range(6))}])
    with pytest.raises(ValueError):
        job.expand_sweep(spec, [{"path": "pixel.read_noise_e", "values": [1, 2, 3]}])


def test_storage_project_budget_and_writer_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path))
    monkeypatch.setattr(storage, "BUDGET_BYTES", 10_000)
    (tmp_path / "other-result.bin").write_bytes(b"x" * 8000)
    with pytest.raises(ValueError, match="500 GB"):
        with storage.reserve(None, 5000):
            pass
    monkeypatch.setattr(storage, "BUDGET_BYTES", 100_000)
    with storage.reserve(None, 1000):
        with pytest.raises(ValueError, match="Another workshop"):
            with storage.reserve(None, 1000):
                pass


def test_common_lifecycle_revision_sweep_replay_integrity(tmp_path, monkeypatch):
    from camera_platform import reference_workshop_engine as engine

    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path / "external"))
    calls = []

    def run(workspace, directory, spec, cache, on_progress=None):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "raw.npy").write_bytes(b"preserved raw bytes")
        calls.append(("capture", spec["pixel"]["cfa_thickness_scale"]))
        on_progress({"stage": "RAW acquired"})
        return {"configuration": spec, "images": {}, "metrics": {}, "raw_preserved": True}

    def replay(source, directory, spec, on_progress=None):
        calls.append(("replay", source.name))
        (directory / "raw.npy").write_bytes((source / "raw.npy").read_bytes())
        return {"configuration": spec, "images": {}, "metrics": {}, "raw_preserved": True}

    monkeypatch.setattr(engine, "run", run)
    monkeypatch.setattr(engine, "replay", replay)
    app = create_app(
        tmp_path / "state", workspace=Path(__file__).parents[2], start_worker=False, seed_demo=False
    )
    service = app.state.service
    monkeypatch.setattr(job, "reference_snapshot", lambda *_: {"fixed_reference": "test"})
    from camera_platform import reference_workshop_optics

    monkeypatch.setattr(reference_workshop_optics, "field_layout", lambda *_: [{"id": "center"}])

    def finish(identity):
        claimed = service.registry.claim()
        assert claimed["id"] == identity
        service.registry.finish(identity, result=service.execute_run(claimed))

    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = http
        spec = sdk.reference_workshop_catalog()["specification"]
        rev = sdk.create_reference_revision(spec)
        updated = copy.deepcopy(spec)
        updated["pixel"]["cfa_thickness_scale"] = 0.9
        rev2 = sdk.create_reference_revision(updated, parent_id=rev["id"])
        assert rev2["parent_id"] == rev["id"]
        assert (
            service.registry.get("reference_design_revisions", rev["id"])["specification"] == spec
        )
        capture = sdk.submit_reference_workshop(spec)
        finish(capture["id"])
        record = sdk.wait(capture["id"], timeout=1, poll_interval=0)
        assert record["status"] == "succeeded"
        summary = record["result"]["summary"]
        assert http.get(summary["report_url"]).status_code == 200
        exported = http.get("/api/v1/reference-workshop/runs/" + capture["id"] + "/bundle")
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert archive.read("raw.npy") == b"preserved raw bytes"
            assert "manifest.json" in archive.namelist()
        assert http.get(summary["files_base"] + "raw.npy").content == b"preserved raw bytes"
        changed = {**spec["isp"], "sharpen": 1.2}
        replayed = sdk.replay_reference_workshop(capture["id"], changed)
        finish(replayed["id"])
        assert sdk.wait_reference_workshop(replayed["id"], timeout=1, poll_interval=0)["result"][
            "summary"
        ]["raw_preserved"]
        sweep = sdk.submit_reference_sweep(
            spec, [{"path": "pixel.cfa_thickness_scale", "values": [0.9, 1, 1.1]}]
        )
        finish(sweep["id"])
        replayed = sdk.replay_reference_workshop(
            sweep["id"], changed, source_candidate_id="candidate-00"
        )
        finish(replayed["id"])
        assert ("replay", "candidate-00") in calls
        cancelled = sdk.submit_reference_workshop({**spec, "name": "Cancelled"})
        assert sdk.cancel_reference_workshop(cancelled["id"])["status"] == "cancelled"
        path = job._directory(service, capture["id"]) / "raw.npy"
        path.write_bytes(b"changed")
        assert http.get(summary["files_base"] + "raw.npy").status_code >= 400
        with pytest.raises(httpx.HTTPStatusError):
            sdk.replay_reference_workshop(capture["id"], changed)
