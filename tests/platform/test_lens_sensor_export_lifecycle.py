"""An attachment export is available only after its Lens study succeeds."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.registry import digest

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("job_kind", "path"),
    [
        ("lens_sensor_hdr", "lens-sensor-hdr"),
        ("lens_sensor_lfm", "lens-sensor-lfm"),
        ("lens_sensor_isp_replay", "lens-sensor-isp-replays"),
    ],
)
def test_lens_study_export_rejects_queued_running_and_failed_jobs(
    tmp_path, job_kind, path
):
    app = create_app(tmp_path / "state", WORKSPACE, start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()[
            "token"
        ]
        request = {"name": "lifecycle fixture", "job_kind": job_kind}
        queued = app.state.service.registry.submit(request, digest(request))
        endpoint = f"/api/v1/{path}/{queued['id']}/export"
        response = client.get(endpoint)
        assert response.status_code == 422
        assert "not ready" in response.json()["error"]

        assert app.state.service.registry.claim()["id"] == queued["id"]
        response = client.get(endpoint)
        assert response.status_code == 422
        assert "not ready" in response.json()["error"]

        app.state.service.registry.finish(queued["id"], error="fixture failure")
        response = client.get(endpoint)
        assert response.status_code == 422
        assert "not ready" in response.json()["error"]
