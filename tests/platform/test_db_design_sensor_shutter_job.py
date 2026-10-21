"""The full-array clock is explicit and cannot silently reinterpret old scan jobs."""
from types import SimpleNamespace

import pytest

from camera_platform import db_design_job as jobs
from camera_platform.sdk import PlatformClient

SOURCE = "run-" + "a" * 32


def test_sensor_line_time_is_not_legacy_probe_readout():
    new = jobs.specification({"comparison_mode": "sensor_shutter", "source_run_id": SOURCE})
    assert new["line_time_us"] == [0, 10, 20]
    assert "readout_ms" not in new
    assert jobs.specification(new) == new
    old = jobs.specification({"comparison_mode": "shutter_motion", "source_run_id": SOURCE})
    assert old["readout_ms"] == [0, 5, 15, 30]
    assert "line_time_us" not in old


@pytest.mark.parametrize("extra", [
    {"readout_ms": [0, 10]}, {"sensor_rows": 2000}, {"line_time_us": [10, 20]},
    {"line_time_us": [0]}, {"line_time_us": [0, 10, 10]},
    {"line_time_us": [0, True]}, {"line_time_us": [0, float("nan")]},
    {"line_time_us": [0, -.1]}, {"line_time_us": [0, .01]}, {"line_time_us": [0, 101]},
    {"timing_provenance": "measured"}, {"f_numbers": [2.75, 3.2, 4.12, 5.6, 8]},
])
def test_reject_ambiguous_or_unsupported_timing(extra):
    with pytest.raises(ValueError):
        jobs.specification({**jobs.sensor_shutter_recipe(SOURCE), **extra})


def test_workload_accounts_for_all_lines_and_new_optics(monkeypatch):
    monkeypatch.setattr(jobs, "_snapshot", lambda *args: {
        "f_numbers": [2.75, 4.12, 5.6], "selected_f_numbers": [4.12]})
    monkeypatch.setattr(jobs, "fingerprint", lambda: {"test": "identity"})
    monkeypatch.setattr(jobs.storage, "status", lambda _: {
        "remaining_bytes": 500e9, "volume_free_bytes": 1e12, "path": "/test/ssd"})
    service = SimpleNamespace(registry=SimpleNamespace(submit=lambda doc, identity: doc))
    job = jobs.submit(service, jobs.sensor_shutter_recipe(SOURCE))
    assert job["estimated_bytes"] == 54 * 512 * 1024**2 + 2 * 1024**3
    assert job["default_worker_timeout_seconds"] == 14400


def test_sdk_keeps_units_and_line_axis():
    client = object.__new__(PlatformClient)
    calls = []
    client.post = lambda path, payload: calls.append((path, payload)) or {"id": SOURCE}
    client.submit_sensor_shutter(SOURCE, line_time_us=[0, 7.5, 15], exposure_ms=[1, 4],
                                object_speed_m_s=[0, 4], f_numbers=[4.12])
    assert calls == [("db-design/studies", {
        "comparison_mode": "sensor_shutter", "source_run_id": SOURCE,
        "line_time_us": [0, 7.5, 15], "exposure_ms": [1, 4],
        "object_speed_m_s": [0, 4], "f_numbers": [4.12]})]
