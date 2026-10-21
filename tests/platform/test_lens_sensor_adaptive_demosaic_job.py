"""A pinned RAW job feeds a durable adaptive-demosaic job and downloads."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from camera_platform import lens_sensor_adaptive_demosaic_job as job
from camera_platform import lens_sensor_raw_job as raw_job
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from camera_platform.service import PlatformService

WORKSPACE = Path(__file__).resolve().parents[2]
ROOT = WORKSPACE / "camerae2e_db"
P0248_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"


def test_job_spec_requires_explicit_method_and_limit_acknowledgement():
    spec = job.default_spec()
    spec["source_lens_sensor_job_id"] = "source-run"
    with pytest.raises(ValueError, match="acknowledgement"):
        job.specification(spec)
    spec["acknowledge_simulation_limits"] = True
    assert job.specification(spec)["method"] == job.METHOD_ID
    with pytest.raises(ValueError, match="schema or method"):
        job.specification(dict(spec, method="unknown"))
    with pytest.raises(ValueError, match="exactly"):
        job.specification(dict(spec, extra=True))


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    if not (ROOT / "catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    service = PlatformService(tmp_path_factory.mktemp("adaptive-job-state"), WORKSPACE)
    sensor = next(
        row for row in sensor_revision_catalog(root=ROOT)
        if row["report_code"] == "DEF-2512-801" and row["model"] == "db_estimate_v1"
    )
    spec = raw_job.default_bounded_spec()
    spec.update(
        lens_revision_id=P0248_REVISION,
        sensor_revision={"identifier": "DEF-2512-801", "run_id": sensor["run_id"]},
        scene_kind="slanted_edge", sensor_size=[16, 16], noise_flag=2,
        accept_approximate_lens_radiometry=True,
    )
    queued = raw_job.submit(service, spec)
    assert service.registry.claim()["id"] == queued["id"]
    service.registry.finish(queued["id"], result=raw_job.execute(service, queued), error=None)
    saved = raw_job.get(service, queued["id"])
    assert saved["status"] == "succeeded"
    return service, saved


def test_real_source_preflight_and_job_artifacts_through_routes(source):
    service, saved = source
    app = FastAPI()
    job.install_routes(app, service)
    with TestClient(app) as transport:
        template = transport.get(
            "/api/v1/lens-sensor-adaptive-demosaic/template",
            params={"source_lens_sensor_job_id": saved["id"]},
        )
        assert template.status_code == 200
        body = template.json()
        assert body["specification"]["method"] == job.METHOD_ID
        assert body["selected_source_contract"]["native_cfa_contract"] == (
            "three_channel_rggb_2x2"
        )
        assert body["selected_source_contract"]["raw_shape"] == [16, 16]
        assert body["recommended_raw_size"] == [16, 16]
        spec = body["specification"]
        spec["acknowledge_simulation_limits"] = True
        queued_response = transport.post("/api/v1/lens-sensor-adaptive-demosaic", json=spec)
        assert queued_response.status_code == 202, queued_response.text
        queued = queued_response.json()
        assert queued["job_kind"] == job.JOB_KIND
        assert queued["source_binding"]["source_lens_sensor_job_id"] == saved["id"]
        assert service.registry.claim()["id"] == queued["id"]
        service.registry.finish(queued["id"], result=job.execute(service, queued), error=None)
        detail = transport.get(f"/api/v1/lens-sensor-adaptive-demosaic/{queued['id']}")
        assert detail.status_code == 200
        finished = detail.json()
        assert finished["status"] == "succeeded"
        summary = finished["result"]["summary"]
        assert summary["source_binding"] == queued["source_binding"]
        assert summary["method_binding"] == queued["method_binding"]
        assert summary["report"]["before_after"]["valid_pixels"] > 0
        assert summary["report"]["before_after"]["quality_improvement_claimed"] is False
        assert summary["report"]["calibration_provenance"]["raw_denoise"].startswith(
            "bypass_"
        )
        original = np.load(
            service.registry.blob_path(
                saved["result"]["summary"]["preview"]["raw_artifact"]["sha256"]
            ), allow_pickle=False,
        )
        npz = service.registry.blob_path(summary["evidence"]["npz"]["sha256"]).read_bytes()
        with np.load(io.BytesIO(npz), allow_pickle=False) as arrays:
            assert arrays["source_raw_dn_frame"].dtype == np.dtype("int32")
            assert arrays["source_raw_dn_frame"].tobytes(order="C") == original.tobytes(
                order="C"
            )
            assert arrays["adaptive_laplacian_sensor_rgb"].shape == (16, 16, 3)
        listing = transport.get("/api/v1/lens-sensor-adaptive-demosaic").json()
        assert any(item["id"] == queued["id"] for item in listing)
        exported = transport.get(
            f"/api/v1/lens-sensor-adaptive-demosaic/{queued['id']}/export"
        )
        assert exported.status_code == 200
        assert json.loads(exported.content)["result"]["summary"] == summary


def test_small_saved_raw_is_refused_at_prepare(source, monkeypatch):
    from camera_platform import retained_raw_isp_bridge

    service, saved = source
    original = retained_raw_isp_bridge.inspect_lens_sensor_isp_input

    def small_checked(*args, **kwargs):
        checked = original(*args, **kwargs)
        checked["raw"] = np.zeros((8, 8), dtype=np.int32)
        return checked

    # The lower-level adapter is also guarded; this checks the job preflight
    # independently of execution once a saved source is selected.
    monkeypatch.setattr(retained_raw_isp_bridge, "inspect_lens_sensor_isp_input", small_checked)
    spec = job.default_spec()
    spec.update(source_lens_sensor_job_id=saved["id"], acknowledge_simulation_limits=True)
    with pytest.raises(ValueError, match="more than 8 pixels"):
        job.prepare(service, spec)
