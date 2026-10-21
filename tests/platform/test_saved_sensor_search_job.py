"""Pinned legacy optimizer use is exposed through the platform job lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.saved_sensor_search_job import default_spec, specification
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def test_search_spec_requires_saved_run_and_small_discrete_space():
    spec = default_spec()
    spec["sensor_revision"] = {"identifier": "sensor", "run_id": "run"}
    with pytest.raises(ValueError, match="acknowledgement"):
        specification(spec)
    spec["acknowledge_simulation_limits"] = True
    assert specification(spec)["exposure_s"] == [0.001, 0.004]
    with pytest.raises(ValueError, match="at most four"):
        specification(dict(spec, exposure_s=[0.001, 0.002, 0.003]))
    with pytest.raises(ValueError, match="distinct"):
        specification(dict(spec, analog_gain=[1.0, 1.0]))
    with pytest.raises(ValueError, match="sensor_size"):
        specification(dict(spec, sensor_size=[128, 128]))


def test_saved_search_real_run_via_api_sdk_worker(tmp_path):
    saved_db = WORKSPACE / "camerae2e_db/image_sensor/simulations/sensor_simulations.sqlite"
    if not saved_db.is_file():
        pytest.skip("Saved sensor DB is not installed")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        transport.headers["X-Platform-Token"] = transport.get("/api/v1/session").json()["token"]
        template = client.saved_sensor_search_template(query="DEP-2209-801")
        selected = next(
            (row for row in template["sensor_catalog"] if row["report_code"] == "DEP-2209-801"),
            None,
        )
        if selected is None:
            pytest.skip("Example saved sensor run is not installed")
        spec = template["specification"]
        spec.update(
            name="bounded saved-run exposure gain search",
            sensor_revision={"identifier": selected["report_code"], "run_id": selected["run_id"]},
            acknowledge_simulation_limits=True,
        )
        queued = client.submit_saved_sensor_search(spec)
        state = client.job_status(queued["id"], expected_kind="saved_sensor_search")
        assert state["detail_path"] == "saved-sensor-search/" + queued["id"]
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        completed = client.wait(queued["id"], timeout=5, expected_kind="saved_sensor_search")
        assert completed["status"] == "succeeded", completed.get("error")
        summary = completed["result"]["summary"]
        search = summary["search"]
        assert search["revision"]["run_id"] == selected["run_id"]
        assert search["case_count"] == 4
        assert search["feasible_count"] == 4
        assert search["raw_arrays_exported"] is False
        assert all(case["source_binding"]["run_id"] == selected["run_id"]
                   for case in search["cases"])
        assert len(completed["artifacts"]) == 1
        assert transport.get(completed["artifacts"][0]["url"]).status_code == 200
        assert client.saved_sensor_search(queued["id"])["result"]["summary"] == summary
