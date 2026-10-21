"""Unified crop calibration must use the bounded calibration timeout, not 120s."""
import signal
from types import SimpleNamespace
import pytest

from camera_platform import worker
from camera_platform.registry import digest
from camera_platform.service import PlatformService


@pytest.mark.parametrize("kind", ["calibration_unified_workflow", "sensor_structure_camera_demo", "sensor_structure_spectral_camera"])
def test_unified_uses_calibration_deadline_in_worker_loop(tmp_path, monkeypatch, kind):
    service = PlatformService(tmp_path / "state")
    doc = {"job_kind": kind, "default_worker_timeout_seconds": 1800}
    job = service.registry.submit(doc, digest(doc))
    calls, signals = [], []

    def poll():
        calls.append(1)
        if len(calls) < 3:
            return None
        service.registry.finish(job["id"], result={"qualified": True})
        return 0

    child = SimpleNamespace(pid=99999999, returncode=0, poll=poll, wait=lambda timeout: 0)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(worker, "signal_private_group", lambda pid, sig: signals.append(sig))
    # A missing job-kind route deterministically times out on the first loop.
    monkeypatch.setattr(worker, "DEFAULT_JOB_TIMEOUT_S", 0)
    worker.work(service, once=True)
    assert signal.SIGTERM not in signals
    assert service.registry.get_run(job["id"])["status"] == "succeeded"
