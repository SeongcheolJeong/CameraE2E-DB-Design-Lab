"""A pinned Lens × Sensor RAW crosses the Job/API/SDK ISP v2 boundary unchanged."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.lens_sensor_isp_replay_job import default_spec, specification
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def _client(transport):
    client = object.__new__(PlatformClient)
    client.http = transport
    transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
    return client


def _capture_source(app, client, report_code):
    template = client.lens_sensor_raw_template()
    lens = next(
        (row for row in template["lens_catalog"]["revisions"]
         if row["preview_status"] == "ready"),
        None,
    )
    sensor = next(
        (row for row in template["sensor_catalog"] if row["report_code"] == report_code),
        None,
    )
    if lens is None or sensor is None:
        pytest.skip("Required pinned Lens and Sensor DB revisions are unavailable")
    spec = template["specification"]
    spec.update(
        lens_revision_id=lens["revision_id"],
        sensor_revision={"identifier": report_code, "run_id": sensor["run_id"]},
        scene_kind="uniform",
        sensor_size=[8, 8],
        noise_flag=2,
        accept_approximate_lens_radiometry=True,
    )
    queued = client.submit_lens_sensor_raw(spec)
    assert app.state.service.registry.claim()["id"] == queued["id"]
    execute(app.state.service, queued["id"])
    source = client.wait(queued["id"], timeout=5, expected_kind="lens_sensor_raw")
    assert source["status"] == "succeeded", source.get("error")
    return source


def test_lens_sensor_isp_requires_explicit_source_and_uncalibrated_profile():
    spec = default_spec()
    with pytest.raises(ValueError, match="acknowledgement"):
        specification(spec)
    spec.update(source_lens_sensor_job_id="capture-1", acknowledge_simulation_limits=True)
    assert specification(spec)["source_lens_sensor_job_id"] == "capture-1"
    spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
    spec["profile"]["calibration_id"] = "not-bound"
    with pytest.raises(ValueError, match="bypass LSC/denoise"):
        specification(spec)


def test_real_2x2_bayer_lens_raw_replays_through_worker_api_and_sdk(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        source = _capture_source(app, client, "DEF-2512-801")
        source_preview = source["result"]["summary"]["preview"]
        original_bytes = transport.get(source_preview["raw_artifact"]["url"]).content
        original = np.load(io.BytesIO(original_bytes), allow_pickle=False)
        assert original.shape == (8, 8) and original.dtype == np.int32

        template = client.lens_sensor_isp_replay_template()
        assert any(item["id"] == source["id"] for item in template["source_lens_sensor_jobs"])
        spec = template["specification"]
        spec.update(source_lens_sensor_job_id=source["id"], acknowledge_simulation_limits=True)
        spec["profile"]["white_balance"]["gains_rgb"] = [1.2, 1.0, 0.8]
        queued = client.submit_lens_sensor_isp_replay(spec)
        assert client.job_status(
            queued["id"], expected_kind="lens_sensor_isp_replay"
        )["detail_path"] == f"lens-sensor-isp-replays/{queued['id']}"
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], timeout=5, expected_kind="lens_sensor_isp_replay")
        assert finished["status"] == "succeeded", finished.get("error")
        summary = finished["result"]["summary"]
        assert summary["source_binding"]["source_lens_sensor_job_id"] == source["id"]
        assert summary["source_binding"]["saved_sensor_revision"] == (
            source["result"]["summary"]["source_identities"]["sensor"]
        )
        assert summary["source_binding"]["lens_radiometry"] == "assumed_unity_not_validated"
        assert summary["saved_native_cfa"]["pattern"] == [[1, 2], [2, 3]]
        assert summary["raw_format"]["cfa"] == "RGGB"
        assert summary["raw_values_byte_equal"] is True
        assert summary["before_after"]["default_vs_candidate"]["available"]
        assert summary["before_after"]["default_vs_candidate"][
            "mean_absolute_rgb_difference"
        ] > 0
        assert summary["candidate_rgb_consumer"]["detector_evaluation_performed"] is False
        replay_bytes = transport.get(summary["evidence"]["npz"]["url"]).content
        with np.load(io.BytesIO(replay_bytes), allow_pickle=False) as arrays:
            assert arrays["source_raw_dn_frame"].dtype == np.int32
            assert arrays["source_raw_dn_frame"].tobytes(order="C") == original.tobytes(order="C")
            assert arrays["candidate_rgb"].shape == (8, 8, 3)
        assert client.lens_sensor_isp_replay(queued["id"])["result"]["summary"] == summary
        assert len(client.list_lens_sensor_isp_replays()) == 1
        export = transport.get(f"/api/v1/lens-sensor-isp-replays/{queued['id']}/export")
        assert export.status_code == 200


def test_real_quad_bayer_lens_raw_refused_before_isp_job_submission(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned Lens DB and Sensor DB are unavailable")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = _client(transport)
        source = _capture_source(app, client, "DEP-2209-801")
        assert source["result"]["summary"]["preview"]["sensor_model"]["kind"] == "db_estimate_v1"
        spec = client.lens_sensor_isp_replay_template()["specification"]
        spec.update(source_lens_sensor_job_id=source["id"], acknowledge_simulation_limits=True)
        before = len(app.state.service.registry.list_runs())
        response = transport.post("/api/v1/lens-sensor-isp-replays", json=spec)
        assert response.status_code == 422
        assert "native CFA" in response.text
        assert len(app.state.service.registry.list_runs()) == before
