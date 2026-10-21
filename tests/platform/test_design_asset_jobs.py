"""Queue, watch and SDK integration without touching source databases."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from camera_platform import design_asset_jobs as jobs
from camera_platform.api import create_app
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService
from camera_platform.worker import execute


@pytest.fixture
def service(tmp_path):
    return PlatformService(tmp_path / "state", tmp_path)


def test_watch_is_opt_in_coalesces_and_does_not_catch_up(service):
    assert not jobs.watch_status(service)["enabled"]
    assert jobs.watch_tick(service, timestamp=10**12) is None
    status = jobs.set_watch(service, {"enabled": True})
    first = status["last_job_id"]
    assert first
    assert jobs.submit(service, "refresh")["id"] == first
    assert jobs.set_watch(service, {"enabled": True})["last_job_id"] == first
    service.registry.cancel(first)
    last = datetime.fromisoformat(status["last_scan_at"].replace("Z", "+00:00")).timestamp()
    assert jobs.watch_tick(service, timestamp=last + 899) is None
    second = jobs.watch_tick(service, timestamp=last + 900 * 500)
    assert second["id"] != first
    assert jobs.watch_tick(service, timestamp=last + 900 * 500)["id"] == second["id"]
    jobs.set_watch(service, {"enabled": False})
    assert jobs.watch_tick(service, timestamp=10**12) is None
    assert len(service.registry.list_runs()) == 2


@pytest.mark.parametrize("payload", [{}, {"enabled": 1}, {"enabled": True, "interval": 1}])
def test_watch_rejects_implicit_enable_or_interval_changes(service, payload):
    with pytest.raises(ValueError):
        jobs.set_watch(service, payload)


def test_jobs_finish_with_actual_report_and_artifact(service, monkeypatch):
    from camera_platform import design_asset_readiness as readiness

    def scan(service, progress=None):
        progress({"stage": "Hashed sources", "completed": 1, "total": 2})
        return {"status": "unchanged", "changes": []}

    monkeypatch.setattr(readiness, "scan", scan)
    first = jobs.submit(service, "refresh")
    assert service.registry.claim()["id"] == first["id"]
    execute(service, first["id"])
    finished = jobs.get(service, first["id"])
    assert finished["status"] == "succeeded"
    assert finished["result"]["report"]["status"] == "unchanged"
    assert service.registry.blob_path(finished["artifacts"][0]["sha256"]).is_file()


def test_cancelled_job_cannot_publish_ready_report(service, monkeypatch):
    from camera_platform import design_asset_readiness as readiness

    def scan(service, progress=None):
        service.registry.cancel(first["id"])
        progress("Cancelled before completion")
        return {"status": "passed"}

    monkeypatch.setattr(readiness, "scan", scan)
    first = jobs.submit(service, "refresh")
    service.registry.claim()
    execute(service, first["id"])
    finished = jobs.get(service, first["id"])
    assert finished["status"] == "cancelled"
    assert finished["result"] is None


def test_common_sdk_wait_routes_to_asset_job_and_cancel(tmp_path, monkeypatch):
    from camera_platform import design_asset_readiness as readiness

    monkeypatch.setattr(readiness, "scan", lambda service, progress=None: {"status": "unchanged"})
    app = create_app(tmp_path / "state", tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app) as http:
        assert http.post(jobs.ROUTE + "/refreshes", json={}).status_code == 403
        client = object.__new__(PlatformClient)
        client.http = http
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        assert not client.design_asset_watch()["enabled"]
        job = client.submit_design_asset_refresh()
        service = app.state.service
        service.registry.claim()
        execute(service, job["id"])
        assert client.wait(job["id"], timeout=1)["result"]["report"]["status"] == "unchanged"
        next_job = client.submit_design_asset_refresh()
        assert client.cancel_design_asset_job(next_job["id"])["status"] == "cancelled"
        assert client.design_asset_job(next_job["id"])["status"] == "cancelled"
