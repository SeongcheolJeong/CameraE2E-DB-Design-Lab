"""A saved DB comparison goes through the same API, worker and SDK boundary."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.db_comparisons import (
    default_spec,
    specification,
)
from camera_platform.db_comparisons import (
    execute as execute_db_comparison,
)
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute

WORKSPACE = Path(__file__).resolve().parents[2]


def test_specification_keeps_first_second_order_and_rejects_unacknowledged():
    spec = default_spec()
    spec["lens_revision_ids"] = ["lens-z", "lens-a"]
    spec["sensor_revisions"] = [
        {"identifier": "sensor-z", "run_id": "run-z"},
        {"identifier": "sensor-a", "run_id": "run-a"},
    ]
    with pytest.raises(ValueError, match="acknowledgement"):
        specification(spec)
    spec["acknowledge_simulation_limits"] = True
    normalized = specification(spec)
    assert normalized["lens_revision_ids"] == ["lens-z", "lens-a"]
    assert normalized["sensor_revisions"][0]["run_id"] == "run-z"


def test_saved_db_pair_via_api_sdk_worker(tmp_path):
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Saved component catalog is not installed")
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as transport:
        client = object.__new__(PlatformClient)
        client.http = transport
        token = transport.get("/api/v1/session").json()["token"]
        transport.headers["X-Platform-Token"] = token
        template = client.db_comparison_template()
        ready = [
            item
            for item in template["lens_catalog"]["revisions"]
            if item["preview_status"] == "ready"
        ]
        sensors = {item["report_code"]: item for item in template["sensor_catalog"]}
        if len(ready) < 2 or not {"DEF-1801-802", "DEP-2209-801"} <= sensors.keys():
            pytest.skip("Pinned example DB revisions are not installed")
        spec = template["specification"]
        spec.update(
            name="real saved revisions",
            lens_revision_ids=[ready[1]["revision_id"], ready[2]["revision_id"]],
            sensor_revisions=[
                {"identifier": code, "run_id": sensors[code]["run_id"]}
                for code in ("DEF-1801-802", "DEP-2209-801")
            ],
            acknowledge_simulation_limits=True,
        )
        queued = client.submit_db_comparison(spec)
        status = client.job_status(queued["id"], expected_kind="db_comparison")
        assert status["status"] == "queued"
        assert status["detail_path"] == "db-comparisons/" + queued["id"]
        assert app.state.service.registry.claim()["id"] == queued["id"]
        execute(app.state.service, queued["id"])
        finished = client.wait(queued["id"], timeout=5)
        assert finished["status"] == "succeeded", finished.get("error")
        summary = finished["result"]["summary"]
        assert len(summary["lens_geometry"]["lenses"]) == 2
        assert len(summary["sensor_raw"]["captures"]) == 2
        assert summary["lens_sensor_cross_product"]["status"] == "not_executed"
        assert len(finished["artifacts"]) == 3
        assert all(item["raw"]["dtype"] == "int32" for item in summary["sensor_raw"]["captures"])
        captures = summary["sensor_raw"]["captures"]
        change = summary["sensor_raw"]["differences"]["mean_raw_dn"]
        assert change["second_minus_first"] == pytest.approx(
            np.mean(captures[1]["raw"]["values"])
            - np.mean(captures[0]["raw"]["values"])
        )
        first = summary["sensor_raw"]["captures"][0]
        downloaded = transport.get(first["raw_artifact"]["url"])
        assert downloaded.status_code == 200
        original = np.load(io.BytesIO(downloaded.content), allow_pickle=False)
        assert original.dtype == np.dtype("int32")
        assert original.tolist() == first["raw"]["values"]
        assert client.db_comparison(queued["id"])["result"]["summary"] == summary
        blocked = next(
            item
            for item in template["lens_catalog"]["revisions"]
            if item["preview_status"] == "blocked"
        )
        refused = dict(spec, lens_revision_ids=[ready[0]["revision_id"], blocked["revision_id"]])
        response = transport.post("/api/v1/db-comparisons", json=refused)
        assert response.status_code == 422
        assert len(client.list_db_comparisons()) == 1
        cancelled = client.submit_db_comparison(dict(spec, name="cancel before calculate"))
        assert app.state.service.registry.claim()["id"] == cancelled["id"]
        app.state.service.registry.cancel(cancelled["id"])
        with pytest.raises(RuntimeError, match="cancelled"):
            execute_db_comparison(app.state.service, cancelled)
        assert app.state.service.registry.get_run(cancelled["id"])["artifacts"] == []
