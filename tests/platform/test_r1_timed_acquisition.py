"""Installed R1 examples reach timed RAW and conservative HDR through the Worker."""

from __future__ import annotations

import io
import shutil
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.project_mode import configure
from camera_platform.r1_design_assets import BUNDLE_ENV, bundle_root
from camera_platform.r1_timed_acquisition import get
from camera_platform.registry import IntegrityError
from camera_platform.worker import execute as worker_execute
from pyisetcam.assets import AssetStore


@pytest.fixture
def workbench(tmp_path):
    state = tmp_path / "r1-state"
    configure(state, project_id="r1-timed-test")
    app = create_app(state_dir=state, workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["x-platform-token"] = client.get("/api/v1/session").json()["token"]
        yield app, client


def _template(client):
    response = client.get("/api/v1/r1-timed-acquisitions/template")
    assert response.status_code == 200, response.text
    spec = deepcopy(response.json()["specification"])
    spec["acknowledge_simulation_limits"] = True
    return spec


def _submit_and_work(app, client, spec):
    queued = client.post("/api/v1/r1-timed-acquisitions", json=spec)
    assert queued.status_code == 202, queued.text
    job = app.state.service.registry.claim()
    assert job["id"] == queued.json()["id"]
    worker_execute(app.state.service, job["id"])
    saved = client.get("/api/v1/r1-timed-acquisitions/" + job["id"])
    assert saved.status_code == 200, saved.text
    return job, saved.json()


def test_registered_dynamic_scene_runs_timed_raw_and_reopens(workbench, monkeypatch):
    app, client = workbench
    # A fresh R1 install has no upstream ISETCam snapshot. Its RAW path must
    # never fall back to AssetStore.default() and a network download.
    def forbidden_default(cls):
        raise AssertionError("R1 timed RAW requested an external ISETCam asset store")

    monkeypatch.setattr(AssetStore, "default", classmethod(forbidden_default))
    spec = _template(client)
    spec["model_id"] = "r1-clear-lowgreen-8ke-v1"
    spec["scene_knots"][1]["scene_id"] = "r1-checker-v1"
    job, saved = _submit_and_work(app, client, spec)
    assert saved["status"] == "succeeded", saved.get("error")
    summary = saved["result"]["summary"]
    assert summary["runtime_plugin_id"] == "hdr_bracket_v1"
    assert summary["runtime"]["hdr_merge_applied"] is False
    assert set(summary["source_identities"]) == {"r1-flat-v1", "r1-checker-v1"}
    for identity in summary["source_identities"].values():
        assert identity["components"]["electricals"]["id"] == "r1-readout-8ke-v1"
        assert identity["model_id"] == spec["model_id"]
    assert summary["runtime"]["scene_knots"][0]["scene_content_hash"] != (
        summary["runtime"]["scene_knots"][1]["scene_content_hash"])
    assert "hdr_rgb_artifact" not in summary
    raw = np.load(app.state.service.registry.blob_path(summary["raw_artifact"]["sha256"]),
                  allow_pickle=False)
    rgb = np.load(app.state.service.registry.blob_path(
        summary["rgb_frames_artifact"]["sha256"]), allow_pickle=False)
    assert raw.dtype == np.int32 and raw.shape == (2, 16, 16)
    assert rgb.dtype == np.float64 and rgb.shape == (2, 16, 16, 3)
    downloaded = client.get(summary["raw_artifact"]["url"])
    assert downloaded.status_code == 200
    np.testing.assert_array_equal(np.load(io.BytesIO(downloaded.content), allow_pickle=False), raw)
    assert client.get("/api/v1/jobs/" + job["id"]).json()["detail_path"] == (
        "r1-timed-acquisitions/" + job["id"])
    assert client.get("/api/v1/r1-timed-acquisitions").json()[0]["id"] == job["id"]
    assert (
        client.get("/api/v1/r1-timed-acquisitions/" + job["id"]).json()["result"]
        == saved["result"]
    )


def test_static_registered_bracket_preserves_original_raw_separate_from_hdr(workbench):
    app, client = workbench
    spec = _template(client)
    spec["merge_hdr"] = True
    job, saved = _submit_and_work(app, client, spec)
    assert saved["status"] == "succeeded", saved.get("error")
    summary = saved["result"]["summary"]
    assert summary["runtime_plugin_id"] == "hdr_bracket_v1"
    assert summary["runtime"]["hdr_merge_applied"] is True
    assert summary["runtime"]["physical_validation"] == "not_established"
    raw = np.load(app.state.service.registry.blob_path(summary["raw_artifact"]["sha256"]),
                  allow_pickle=False)
    hdr = np.load(app.state.service.registry.blob_path(summary["hdr_rgb_artifact"]["sha256"]),
                  allow_pickle=False)
    mask = np.load(app.state.service.registry.blob_path(
        summary["hdr_valid_mask_artifact"]["sha256"]), allow_pickle=False)
    assert raw.dtype == np.int32 and raw.shape == (2, 16, 16)
    assert hdr.dtype == np.float64 and hdr.shape == (16, 16, 3)
    assert mask.shape == (16, 16) and mask.dtype == np.bool_
    assert summary["raw_artifact"]["sha256"] != summary["hdr_rgb_artifact"]["sha256"]
    assert get(app.state.service, job["id"])["result"] == saved["result"]
    readiness = client.get("/api/v1/hdr-lfm-mode-readiness/" + job["id"])
    assert readiness.status_code == 200, readiness.text
    assessed = readiness.json()
    assert assessed["model_execution"]["hdr_fusion_applied"] is True
    assert assessed["sensor_operating_point"] is None
    assert assessed["reference_comparison"]["status"] == "not_available"
    assert assessed["product_hdr_lfm_decision"]["status"] == "not_established"
    assert client.get(
        "/api/v1/hdr-lfm-reference-comparisons/template/" + job["id"]
    ).status_code == 422


def test_dynamic_hdr_and_partial_cycle_pwm_fusion_fail_before_queue(workbench):
    app, client = workbench
    dynamic = _template(client)
    dynamic["merge_hdr"] = True
    dynamic["scene_knots"][1]["scene_id"] = "r1-checker-v1"
    denied = client.post("/api/v1/r1-timed-acquisitions", json=dynamic)
    assert denied.status_code == 422 and "static scene" in denied.text

    pwm = _template(client)
    pwm["merge_hdr"] = True
    pwm["waveform"] = {"schema": "camera_light_waveform_v1", "kind": "pwm",
                       "clock_id": pwm["control_sequence"]["clock"]["clock_id"],
                       "period_ns": 3_000_000, "on_time_ns": 1_000_000,
                       "phase_origin_ns": 0, "low": 0.1, "high": 1.0}
    denied = client.post("/api/v1/r1-timed-acquisitions", json=pwm)
    assert denied.status_code == 422 and "whole-period" in denied.text
    assert app.state.service.registry.list_runs() == []

    pwm["merge_hdr"] = False
    _, saved = _submit_and_work(app, client, pwm)
    assert saved["status"] == "succeeded", saved.get("error")
    summary = saved["result"]["summary"]
    assert summary["runtime_plugin_id"] == "hdr_bracket_v1"
    assert summary["runtime"]["hdr_merge_applied"] is False
    assert "hdr_rgb_artifact" not in summary
    ratios = np.load(app.state.service.registry.blob_path(
        summary["waveform_row_exposure_ratio_artifact"]["sha256"]), allow_pickle=False)
    assert ratios.shape == (2, 16) and np.all((ratios > 0) & (ratios < 1))


def test_static_whole_cycle_pwm_bracket_can_fuse_without_overwriting_raw(workbench):
    app, client = workbench
    spec = _template(client)
    spec["merge_hdr"] = True
    spec["waveform"] = {"schema": "camera_light_waveform_v1", "kind": "pwm",
                        "clock_id": spec["control_sequence"]["clock"]["clock_id"],
                        "period_ns": 1_000_000, "on_time_ns": 500_000,
                        "phase_origin_ns": 0, "low": 0.1, "high": 1.0}
    _, saved = _submit_and_work(app, client, spec)
    assert saved["status"] == "succeeded", saved.get("error")
    summary = saved["result"]["summary"]
    assert summary["runtime"]["hdr_merge_applied"] is True
    raw = np.load(app.state.service.registry.blob_path(summary["raw_artifact"]["sha256"]),
                  allow_pickle=False)
    ratio = np.load(app.state.service.registry.blob_path(
        summary["waveform_row_exposure_ratio_artifact"]["sha256"]), allow_pickle=False)
    assert raw.shape == (2, 16, 16) and raw.dtype == np.int32
    np.testing.assert_allclose(ratio, .55)
    assert summary["raw_artifact"]["sha256"] != summary["hdr_rgb_artifact"]["sha256"]


def test_source_drift_fails_worker_before_raw_and_saved_raw_tamper_fails_readback(
    workbench, tmp_path, monkeypatch
):
    app, client = workbench
    bundle = tmp_path / "bundle"
    shutil.copytree(bundle_root(tmp_path), bundle)
    monkeypatch.setenv(BUNDLE_ENV, str(bundle))
    spec = _template(client)
    queued = client.post("/api/v1/r1-timed-acquisitions", json=spec)
    assert queued.status_code == 202, queued.text
    scene = bundle / "scenes/flat.json"
    scene.write_bytes(scene.read_bytes() + b" ")
    claimed = app.state.service.registry.claim()
    worker_execute(app.state.service, claimed["id"])
    failed = get(app.state.service, claimed["id"])
    assert failed["status"] == "failed" and "manifest hash" in failed["error"]
    assert failed["artifacts"] == []

    monkeypatch.delenv(BUNDLE_ENV)
    _, saved = _submit_and_work(app, client, spec)
    raw = saved["result"]["summary"]["raw_artifact"]
    path = app.state.service.registry.blobs / raw["sha256"]
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(IntegrityError, match="checksum"):
        get(app.state.service, saved["id"])


def test_primary_state_cannot_open_or_submit_r1_timed(tmp_path):
    app = create_app(state_dir=tmp_path / "primary", workspace=tmp_path,
                     start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["x-platform-token"] = client.get("/api/v1/session").json()["token"]
        assert client.get("/api/v1/r1-timed-acquisitions/template").status_code == 422
        assert client.post("/api/v1/r1-timed-acquisitions", json={}).status_code == 422
