"""Saved sensor quality evidence crosses API, SDK, Worker and blob boundaries."""

from __future__ import annotations

import copy
import io
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import pyisetcam.sensor_simulation_db as saved_db
from camera_platform.api import create_app
from camera_platform.saved_sensor_quality_job import default_spec, prepare, specification
from camera_platform.sdk import PlatformClient
from camera_platform.sensor_db_bridge import sensor_revision_catalog
from camera_platform.service import PlatformService
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def test_saved_quality_spec_requires_pinned_run_and_bounded_work():
    spec = default_spec()
    spec["sensor_revisions"] = [{"identifier": "sensor-a", "run_id": "run-a"}]
    with pytest.raises(ValueError, match="acknowledgement"):
        specification(spec)
    spec["acknowledge_simulation_limits"] = True
    assert specification(spec)["sensor_revisions"] == spec["sensor_revisions"]
    with pytest.raises(ValueError, match="Output size"):
        specification(dict(spec, output_size=[64, 64]))
    with pytest.raises(ValueError, match="distinct"):
        specification(dict(spec, sensor_revisions=spec["sensor_revisions"] * 2))
    with pytest.raises(ValueError, match="luminance_cd_m2"):
        specification(dict(spec, luminance_cd_m2=10**1000))


def test_saved_quality_one_real_revision_via_api_sdk_worker(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Saved sensor DB is not installed")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        token = transport.get("/api/v1/session").json()["token"]
        transport.headers["X-Platform-Token"] = token
        template = client.saved_sensor_quality_template(query="DEF-1801-802")
        choices = {item["report_code"]: item for item in template["sensor_catalog"]}
        if "DEF-1801-802" not in choices:
            pytest.skip("Example saved sensor run is not installed")
        spec = template["specification"]
        spec.update(
            name="saved run with actual RAW",
            sensor_revisions=[{
                "identifier": "DEF-1801-802",
                "run_id": choices["DEF-1801-802"]["run_id"],
            }],
            scenes=["uniform"],
            output_size=[16, 16],
            frames=2,
            acknowledge_simulation_limits=True,
        )
        queued = client.submit_saved_sensor_quality(spec)
        status = client.job_status(queued["id"], expected_kind="saved_sensor_quality")
        assert status["detail_path"] == "saved-sensor-quality/" + queued["id"]
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], timeout=5, expected_kind="saved_sensor_quality")
        assert finished["status"] == "succeeded", finished.get("error")
        summary = finished["result"]["summary"]
        assert summary["source_identities"][0]["run_id"] == spec["sensor_revisions"][0]["run_id"]
        assert len(summary["sensor_reports"]) == 1
        report = summary["sensor_reports"][0]
        assert (
            report["revision"]["result_sha256"]
            == summary["source_identities"][0]["result_sha256"]
        )
        assert report["cases"][0]["scene"] == "uniform"
        assert report["cases"][0]["metrics"]["native_raw_snr_db"]
        response = transport.get(report["artifacts"]["npz"]["url"])
        assert response.status_code == 200
        with np.load(io.BytesIO(response.content), allow_pickle=False) as arrays:
            raw = arrays[report["cases"][0]["array_prefix"] + "_raw_dn_frame"]
            assert raw.dtype == np.int32
            assert raw.shape == tuple(report["cases"][0]["native_size"])
        assert client.saved_sensor_quality(queued["id"])["result"]["summary"] == summary


def test_saved_quality_rejects_incompatible_saved_optics_before_queue(tmp_path, monkeypatch):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Saved sensor DB is not installed")
    rows = sensor_revision_catalog(root=WORKSPACE / "camerae2e_db", query="DEF-1801-802")
    selected = next((row for row in rows if row["report_code"] == "DEF-1801-802"), None)
    if selected is None:
        pytest.skip("Example saved sensor run is not installed")
    service = PlatformService(tmp_path / "state", WORKSPACE)
    spec = default_spec()
    spec["sensor_revisions"] = [{
        "identifier": selected["report_code"], "run_id": selected["run_id"]
    }]
    spec["acknowledge_simulation_limits"] = True
    original = saved_db.sensor_simulation_db_get

    def incompatible(*args, **kwargs):
        result = copy.deepcopy(original(*args, **kwargs))
        result["metadata"]["simulation_settings"]["fnumber"] = 2.0
        return result

    monkeypatch.setattr(saved_db, "sensor_simulation_db_get", incompatible)
    with pytest.raises(ValueError, match="saved F/4"):
        prepare(service, spec)
    assert service.registry.list_runs() == []
