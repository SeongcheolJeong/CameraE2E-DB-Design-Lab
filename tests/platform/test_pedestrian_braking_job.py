"""Typed lifecycle/storage boundaries; mocked computations are not solver proof."""

import json

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.pedestrian_braking_inputs import SCHEMA, bound_specification
from camera_platform.pedestrian_braking_job import _package
from camera_platform.pedestrian_braking_study import _verified_record, _write_record, run_study
from camera_platform.road_design_inputs import write_json
from camera_platform.road_rgb_source import file_sha256, json_sha256
from camera_platform.sdk import PlatformClient


def frozen(package):
    package.mkdir(parents=True)
    spec = {
        "schema": SCHEMA,
        "baseline_id": "L1-S1",
        "seeds": [1, 2, 3, 4, 5],
        "events": [
            {
                "id": str(i),
                "log_token": str(i),
                "risk": i % 6 < 4,
                "split": "selection" if i < 6 else "evaluation",
            }
            for i in range(12)
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
    return spec, sha


def test_sdk_common_wait_artifact_binding_cancel_and_file_integrity(tmp_path, monkeypatch):
    package = tmp_path / "study"
    spec, sha = frozen(package)

    def computation(workspace, target, *, stage):
        assert stage == "full" and target == package
        (target / "report.html").write_text("protocol-only mocked report")
        write_json(
            target / "package-manifest.json",
            {"files": {"report.html": file_sha256(target / "report.html")}},
        )
        return {"status": "baseline_retained", "recommended_id": "L1-S1", "selected_id": "L1-S1"}

    monkeypatch.setattr("camera_platform.pedestrian_braking_job.run_study", computation)
    app = create_app(tmp_path / "registry", workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["x-platform-token"] = http.get("/api/v1/bootstrap").json()["token"]
        sdk = object.__new__(PlatformClient)
        sdk.http = http
        payload = sdk.pedestrian_braking_study_template()["specification"]
        payload.update(package_path="study", specification_sha256=sha)
        queued = sdk.submit_pedestrian_braking_study(payload)
        assert queued["status"] == "queued"
        claimed = app.state.service.registry.claim()
        result = app.state.service.execute_run(claimed)
        app.state.service.registry.finish(queued["id"], result=result)
        completed = sdk.wait_pedestrian_braking_study(queued["id"], timeout=1, poll_interval=0)
        assert completed["result"]["summary"]["physical_validation"] == "not_established"
        route = completed["result"]["summary"]["viewer_path"]
        assert http.get(route).status_code == 200
        assert http.get("/pedestrian-braking.html").status_code == 200
        assert http.get("/api/v1/runs/" + queued["id"]).status_code == 404
        (package / "report.html").write_text("changed")
        assert http.get(route).status_code == 409
        new = sdk.submit_pedestrian_braking_study({**payload, "stage": "first-sequence"})
        assert (
            http.post(
                "/api/v1/pedestrian-braking-studies/" + new["id"] + "/cancel", json={}
            ).json()["status"]
            == "cancelled"
        )
        assert app.state.service.registry.claim() is None


def test_storage_failure_precedes_any_physics_or_detector(tmp_path, monkeypatch):
    package = tmp_path / "study"
    frozen(package)
    monkeypatch.setattr(
        "camera_platform.pedestrian_braking_study.resource_plan",
        lambda *a, **k: {
            "status": "blocked_storage",
            "estimated_package_bytes": 9999,
            "observed_free_bytes": 1,
        },
    )
    monkeypatch.setattr(
        "camera_platform.pedestrian_braking_study.sampling_check",
        lambda *a, **k: pytest.fail("Must not compute on insufficient storage"),
    )
    with pytest.raises(ValueError, match="storage_blocked"):
        run_study(tmp_path, package)
    assert not (package / "decision.json").exists()


def test_external_package_root_requires_explicit_admin_binding(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external" / "study"
    _, sha = frozen(external)
    from types import SimpleNamespace

    service = SimpleNamespace(workspace=workspace)
    request = {"package_path": str(external), "specification_sha256": sha}
    monkeypatch.delenv("CAMERA_PLATFORM_PEDESTRIAN_PACKAGE_ROOTS", raising=False)
    with pytest.raises(ValueError):
        _package(service, request)
    monkeypatch.setenv("CAMERA_PLATFORM_PEDESTRIAN_PACKAGE_ROOTS", str(external.parent))
    assert _package(service, request) == external


def test_cached_metadata_and_original_files_are_both_verified(tmp_path):
    p = tmp_path / "capture.json"
    (tmp_path / "raw.npy").write_bytes(b"integer raw fixture")
    _write_record(
        p,
        {"specification_sha256": "fixed", "files": {"raw.npy": file_sha256(tmp_path / "raw.npy")}},
    )
    assert _verified_record(p, "fixed")
    (tmp_path / "raw.npy").write_bytes(b"corrupt raw")
    with pytest.raises(ValueError):
        _verified_record(p, "fixed")
    _write_record(p, {"specification_sha256": "fixed", "files": {}})
    p.write_text(p.read_text().replace("fixed", "other"))
    with pytest.raises(ValueError):
        _verified_record(p, "fixed")


def test_frozen_specification_rejects_changes(tmp_path):
    p = tmp_path / "study"
    frozen(p)
    assert bound_specification(p)
    spec = json.loads((p / "specification.json").read_text())
    spec["baseline_id"] = "L2-S2"
    write_json(p / "specification.json", spec)
    with pytest.raises(ValueError):
        bound_specification(p)


def test_adaptive_source_grid_uses_qualified_coarse_level_not_fixed_multiplier(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import numpy as np

    import camera_platform.pedestrian_braking_study as study

    package = tmp_path / "study"
    package.mkdir()
    (package / "specification.json").write_text("frozen sampling fixture")
    frames = [{"token": str(i), "timestamp_s": i / 12} for i in range(3)]
    spec = {
        "events": [{"id": "h", "risk": True, "frames": frames}],
        "candidates": [{"id": "C"}],
        "seeds": [1],
        "detector": {},
        "tracker": {},
        "observation": {},
        "spectral_proxy": {"wavelength_nm": [450, 550, 650]},
    }
    calls = []

    def mean(workspace, specification, frame, roi, candidate, directory, *, refinement):
        calls.append(refinement)
        directory.mkdir(parents=True)
        amplitude = {1: 0.8, 2: 1.0, 4: 1.005}[refinement]
        value = np.array([[0.2, 0.3], [0.4, 0.7]]) * amplitude
        np.save(directory / "mean_electrons.npy", value)
        return (
            value,
            {"spectral_proxy": {"primary_photons": [1]}, "native_roi_bounds": [0, 2, 0, 2]},
            None,
            None,
            None,
        )

    monkeypatch.setattr(study, "compute_mean", mean)
    monkeypatch.setattr(study, "fixed_profile", lambda *a: ({}, {}))
    monkeypatch.setattr(study, "roi_capture_sensor", lambda sensor, bounds, value: value)
    monkeypatch.setattr(
        "pyisetcam.sensor_capture.resample_sensor_capture",
        lambda value, **k: SimpleNamespace(data={"raw_dn": value}),
    )

    def rgb(value, *a):
        return np.repeat(value[..., None], 3, axis=-1)

    monkeypatch.setattr(study, "fixed_isp", lambda value, *a: (rgb(value), {}))
    monkeypatch.setattr(study, "reference_linear_isp", rgb)
    monkeypatch.setattr(study, "to_common_grid", lambda value, *a: value)
    monkeypatch.setattr(
        study,
        "infer_sequence",
        lambda config, tracker, frames, image: (
            [{**f, "detections": [], "tracks": []} for f in frames],
            {},
        ),
    )
    result = study.sampling_check(tmp_path, package, spec, lambda message: None)
    assert calls == [1] * 3 + [2] * 3 + [4] * 3
    assert result["qualified"] and result["selected_source_refinement"] == {"C": 2}
    assert not result["checks"][0]["qualified"] and result["checks"][1]["qualified"]
    assert not result["stored_psf_refined"]


def test_intake_diagnostic_requires_bound_manifest_and_never_marks_study_complete(tmp_path):
    root = tmp_path / "outputs/pedestrian-braking"
    connection = root / "full-window-first-frame"
    connection.mkdir(parents=True)
    (connection / "original.jpg").write_bytes(b"fixture image; protocol evidence only")
    write_json(
        connection / "manifest.json",
        {"files": {"original.jpg": file_sha256(connection / "original.jpg")}},
    )
    write_json(
        root / "implementation-status.json",
        {
            "study_complete": False,
            "connection_manifest_sha256": file_sha256(connection / "manifest.json"),
        },
    )
    app = create_app(tmp_path / "registry", workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app, raise_server_exceptions=False) as http:
        base = "/api/v1/pedestrian-braking-studies"
        assert http.get(base + "/intake").json()["study_complete"] is False
        assert http.get(base + "/connection-files/original.jpg").status_code == 200
        assert http.get(base + "/connection-files/unknown.jpg").status_code == 404
        (connection / "original.jpg").write_bytes(b"changed")
        assert http.get(base + "/connection-files/original.jpg").status_code == 409


def test_first_sequence_view_binds_its_own_outputs_before_full_decision(tmp_path, monkeypatch):
    package = tmp_path / "study"
    _, sha = frozen(package)

    def computation(workspace, target, *, stage):
        assert stage == "first-sequence"
        (target / "report.html").write_text("connection-only fixture")
        write_json(
            target / "first-sequence-manifest.json",
            {"files": {"report.html": file_sha256(target / "report.html")}},
        )
        return {"status": "first_sequence_calculated", "selected_id": "L2-S1"}

    monkeypatch.setattr("camera_platform.pedestrian_braking_job.run_study", computation)
    app = create_app(tmp_path / "registry", workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["x-platform-token"] = http.get("/api/v1/bootstrap").json()["token"]
        request = {
            "schema": "camera_pedestrian_braking_job_v1",
            "name": "connection",
            "package_path": "study",
            "specification_sha256": sha,
            "stage": "first-sequence",
        }
        queued = http.post("/api/v1/pedestrian-braking-studies", json=request).json()
        claimed = app.state.service.registry.claim()
        result = app.state.service.execute_run(claimed)
        app.state.service.registry.finish(queued["id"], result=result)
        summary = result["summary"]
        assert summary["recommended_id"] is None and summary["decision"] is None
        assert summary["review_manifest_name"] == "first-sequence-manifest.json"
        assert http.get(summary["viewer_path"]).status_code == 200
        (package / "report.html").write_text("different run")
        assert http.get(summary["viewer_path"]).status_code == 409
