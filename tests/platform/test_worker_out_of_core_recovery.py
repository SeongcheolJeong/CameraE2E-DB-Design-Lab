"""A restarted Worker diagnoses disk-RAW scratch without deleting an old child’s files."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from camera_platform import worker
from camera_platform.registry import digest
from camera_platform.service import PlatformService


def _submit(service: PlatformService, kind: str, label: str) -> dict:
    request = {"job_kind": kind, "name": label}
    return service.registry.submit(request, digest(request))


def _scratch(service: PlatformService, identity: str) -> Path:
    root = service.registry.directory / "out_of_core_work" / identity
    root.mkdir(parents=True)
    (root / "partial.npy").write_bytes(b"unfinished")
    return root


def _saved_report(service: PlatformService) -> dict:
    return json.loads(
        (service.registry.directory / worker.OUT_OF_CORE_DIAGNOSTIC_NAME).read_text()
    )


def test_diagnostic_preserves_terminal_running_unknown_and_other_kind(tmp_path: Path):
    service = PlatformService(tmp_path / "state")
    terminal = _submit(service, "lens_sensor_out_of_core_raw", "terminal")
    assert service.registry.claim()["id"] == terminal["id"]
    service.registry.finish(terminal["id"], error="fixture failure")
    roi_terminal = _submit(service, "lens_sensor_registered_roi_disk_raw", "roi terminal")
    assert service.registry.claim()["id"] == roi_terminal["id"]
    service.registry.finish(roi_terminal["id"], error="fixture failure")
    running = _submit(service, "lens_sensor_out_of_core_raw", "running")
    assert service.registry.claim()["id"] == running["id"]
    other = _submit(service, "characterization", "other kind")
    # The queued other-kind record has a valid Registry identity, but it never
    # authorizes touching a directory under the disk-RAW scratch parent.
    unknown_id = "run-" + "f" * 32
    roots = {
        "terminal": _scratch(service, terminal["id"]),
        "roi_terminal": _scratch(service, roi_terminal["id"]),
        "running": _scratch(service, running["id"]),
        "other": _scratch(service, other["id"]),
        "unknown": _scratch(service, unknown_id),
    }

    report = worker._record_out_of_core_recovery_diagnostic(service)

    assert report == _saved_report(service)
    assert report["safe_to_delete"] is False
    entries = {entry["name"]: entry for entry in report["entries"]}
    assert entries[terminal["id"]]["status"] == "failed"
    assert entries[terminal["id"]]["terminal"] is True
    assert "old child process" in entries[terminal["id"]]["reason"]
    assert entries[roi_terminal["id"]]["job_kind"] == "lens_sensor_registered_roi_disk_raw"
    assert "old child process" in entries[roi_terminal["id"]]["reason"]
    assert entries[running["id"]]["status"] == "running"
    assert entries[running["id"]]["terminal"] is False
    assert entries[other["id"]]["job_kind"] == "characterization"
    assert entries[unknown_id]["registry_state"] == "unknown"
    assert all(entry["safe_to_delete"] is False for entry in entries.values())
    assert all((root / "partial.npy").read_bytes() == b"unfinished" for root in roots.values())


def test_restart_recovery_keeps_scratch_while_old_child_is_alive(tmp_path: Path):
    service = PlatformService(tmp_path / "state")
    job = _submit(service, "lens_sensor_out_of_core_raw", "interrupted")
    assert service.registry.claim()["id"] == job["id"]
    root = _scratch(service, job["id"])
    live_marker = root / "old_child_alive.txt"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('alive'); "
            "time.sleep(30)",
            str(live_marker),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not live_marker.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(0.01)
        assert live_marker.read_text() == "alive"

        worker.work(service, once=True)

        assert child.poll() is None
        assert service.registry.get_run(job["id"])["status"] == "failed"
        assert (root / "partial.npy").read_bytes() == b"unfinished"
        assert live_marker.read_text() == "alive"
        report = _saved_report(service)
        entry = next(item for item in report["entries"] if item["name"] == job["id"])
        assert entry["status"] == "failed"
        assert entry["safe_to_delete"] is False
        assert "old child process" in entry["reason"]
    finally:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)


def test_symlink_scratch_is_reported_without_following_or_deletion(tmp_path: Path):
    service = PlatformService(tmp_path / "state")
    job = _submit(service, "lens_sensor_out_of_core_raw", "symlink")
    assert service.registry.claim()["id"] == job["id"]
    service.registry.finish(job["id"], error="fixture failure")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    parent = service.registry.directory / "out_of_core_work"
    parent.mkdir()
    (parent / job["id"]).symlink_to(outside, target_is_directory=True)

    worker.work(service, once=True)

    entry = next(item for item in _saved_report(service)["entries"] if item["name"] == job["id"])
    assert entry["entry_type"] == "symlink"
    assert entry["safe_to_delete"] is False
    assert "never follow" in entry["reason"]
    assert (outside / "keep.txt").read_text() == "keep"
    assert (parent / job["id"]).is_symlink()


def test_symlink_parent_is_not_traversed(tmp_path: Path):
    service = PlatformService(tmp_path / "state")
    outside = tmp_path / "outside"
    (outside / ("run-" + "a" * 32)).mkdir(parents=True)
    (outside / ("run-" + "a" * 32) / "keep.txt").write_text("keep")
    (service.registry.directory / "out_of_core_work").symlink_to(
        outside, target_is_directory=True
    )

    worker.work(service, once=True)

    report = _saved_report(service)
    assert report["parent_state"] == "symlink"
    assert report["entries"] == []
    assert report["safe_to_delete"] is False
    assert (outside / ("run-" + "a" * 32) / "keep.txt").read_text() == "keep"
