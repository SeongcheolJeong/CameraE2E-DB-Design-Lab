"""Explicit native-probe scan contracts and source-bound workload accounting."""

import copy
from types import SimpleNamespace

import pytest

from camera_platform import db_design_job as jobs
from camera_platform.sdk import PlatformClient

SOURCE = "run-" + "a" * 32


def test_shutter_contract_roundtrip_and_native_scan_defaults():
    payload = {"comparison_mode": "shutter_motion", "source_run_id": SOURCE}
    spec = jobs.specification(payload)
    assert len(payload) == 2
    assert spec["readout_ms"] == [0, 5, 15, 30]
    assert spec["f_numbers"] == [4.12]
    assert spec["luminance_cd_m2"] == [100, 1000]
    assert spec["object_speed_m_s"] == [0, 1, 4]
    assert spec["selection_policy"]["min_relative_signal"] == 0
    assert jobs.specification(copy.deepcopy(spec)) == spec


@pytest.mark.parametrize("change", [
    {"readout_ms": [5, 15]}, {"readout_ms": [0]}, {"readout_ms": []},
    {"readout_ms": [0, True]}, {"readout_ms": [0, float("nan")]},
    {"readout_ms": [0, 101]}, {"readout_ms": [0, 5, 5]},
    {"f_numbers": []}, {"f_numbers": [True]}, {"f_numbers": [2]},
    {"f_numbers": [4.12, 2.75]}, {"f_numbers": [float("inf")]},
    {"readout_direction": "bottom_to_top"}, {"frame_rows": 2400},
    {"source_run_id": None}, {"f_numbers": [2.75, 3.2, 4.12, 5.6]},
])
def test_invalid_scan_and_size_not_silently_coerced(change):
    with pytest.raises(ValueError):
        jobs.specification({**jobs.shutter_motion_recipe(SOURCE), **change})


def test_old_motion_does_not_accept_or_invent_scan():
    old = jobs.aperture_motion_recipe(SOURCE)
    assert "readout_ms" not in old
    with pytest.raises(ValueError):
        jobs.specification({**old, "readout_ms": [0, 5]})


def test_shutter_estimate_counts_only_selected_apertures_and_all_scans(monkeypatch):
    pinned = {"f_numbers": [2.75, 3.2, 4.12, 5.6, 8, 11], "selected_f_numbers": [4.12]}
    monkeypatch.setattr(jobs, "_snapshot", lambda *a: pinned)
    monkeypatch.setattr(jobs, "fingerprint", lambda: {"test": "identity"})
    monkeypatch.setattr(jobs.storage, "status", lambda _: {
        "remaining_bytes": 500e9, "volume_free_bytes": 1e12, "path": "/test/ssd"})
    service = SimpleNamespace(registry=SimpleNamespace(submit=lambda doc, identity: doc))
    job = jobs.submit(service, jobs.shutter_motion_recipe(SOURCE))
    assert job["estimated_bytes"] == 72 * 512 * 1024**2
    assert job["default_worker_timeout_seconds"] == 14400
    monkeypatch.setattr(jobs.storage, "status", lambda _: {
        "remaining_bytes": 1024, "volume_free_bytes": 1e12, "path": "/test/ssd"})
    with pytest.raises(ValueError, match="no sampling reduction"):
        jobs.submit(service, jobs.shutter_motion_recipe(SOURCE))


def test_source_rejects_missing_aperture_before_execution(monkeypatch):
    summary = {"comparison_mode": "aperture_sweep", "aperture_sweep": {"rows": []},
               "raw_preserved": True, "manifest_sha256": "b" * 64,
               "configuration": {"f_numbers": [2.75, 4.12]}}
    record = {"id": SOURCE, "status": "succeeded", "db_design_study": {"schema": jobs.SCHEMA},
              "source_code": {"version": "old"}, "result": {"summary": summary}}
    monkeypatch.setattr(jobs.workshop, "get", lambda *a: record)
    monkeypatch.setattr(jobs.workshop, "_directory", lambda *a: "/source")
    _, _, pinned = jobs.condition_source(None, jobs.shutter_motion_recipe(SOURCE))
    assert pinned["selected_f_numbers"] == [4.12]
    assert pinned["f_numbers"] == [2.75, 4.12]
    with pytest.raises(ValueError, match="present in the preserved source"):
        jobs.condition_source(None, jobs.shutter_motion_recipe(SOURCE, f_numbers=[5.6]))


def test_sdk_shutter_submit_keeps_explicit_scan_axis():
    client = object.__new__(PlatformClient)
    calls = []
    client.post = lambda path, payload: calls.append((path, payload)) or {"id": SOURCE}
    client.submit_shutter_motion(SOURCE, readout_ms=[0, 10], exposure_ms=[1, 8],
                                 object_speed_m_s=[0, 4], f_numbers=[4.12])
    assert calls == [("db-design/studies", {
        "comparison_mode": "shutter_motion", "source_run_id": SOURCE,
        "readout_ms": [0, 10], "exposure_ms": [1, 8], "object_speed_m_s": [0, 4],
        "f_numbers": [4.12]})]
