"""Bounded motion studies use the existing immutable job lifecycle."""

import copy

import pytest

from camera_platform import db_design_job as jobs
from camera_platform.sdk import PlatformClient

SOURCE = "run-" + "a" * 32


def test_motion_axes_and_assumptions_roundtrip_without_mutating_input():
    payload = {"comparison_mode": "aperture_motion", "source_run_id": SOURCE}
    spec = jobs.specification(payload)
    assert payload == {"comparison_mode": "aperture_motion", "source_run_id": SOURCE}
    assert spec["object_speed_m_s"] == [0, 1, 4]
    assert spec["object_distance_m"] == 20
    assert spec["exposure_ms"] == [1, 4, 16]
    assert len(spec["luminance_cd_m2"]) * len(spec["object_speed_m_s"]) == 12
    assert spec["selection_policy"]["min_relative_signal"] == 0
    assert jobs.specification(copy.deepcopy(spec)) == spec


@pytest.mark.parametrize("change", [
    {"source_run_id": None}, {"object_speed_m_s": [-1]},
    {"object_speed_m_s": [float("nan")]}, {"object_speed_m_s": [True]},
    {"object_speed_m_s": [4, 1]}, {"object_speed_m_s": [0, 0]},
    {"object_speed_m_s": []}, {"object_speed_m_s": [51]},
    {"object_distance_m": None}, {"object_distance_m": 0},
    {"object_distance_m": True}, {"object_distance_m": float("inf")},
    {"object_distance_m": 1001}, {"motion_direction": "vertical"},
    {"object_speed_m_s": [0, 1, 2, 4]},
    {"luminance_cd_m2": list(range(1, 9)), "object_speed_m_s": [0, 1, 4, 5]},
])
def test_invalid_motion_contract_fails_without_lowering_accuracy(change):
    with pytest.raises(ValueError):
        jobs.specification({**jobs.aperture_motion_recipe(SOURCE), **change})


def test_static_contract_does_not_silently_add_motion():
    static = jobs.aperture_conditions_recipe(SOURCE)
    assert "object_speed_m_s" not in static
    with pytest.raises(ValueError):
        jobs.specification({**static, "object_speed_m_s": [0]})


def test_sdk_motion_submit_uses_shared_endpoint_and_explicit_parameters():
    client = object.__new__(PlatformClient)
    calls = []
    client.post = lambda path, payload: calls.append((path, payload)) or {"id": SOURCE}
    client.submit_aperture_motion(SOURCE, exposure_ms=[1, 4], object_speed_m_s=[0, 2],
                                  object_distance_m=30, selection_policy={"min_snr_db": 20})
    assert calls == [("db-design/studies", {
        "comparison_mode": "aperture_motion", "source_run_id": SOURCE,
        "exposure_ms": [1, 4], "object_speed_m_s": [0, 2], "object_distance_m": 30,
        "selection_policy": {"min_snr_db": 20},
    })]


def test_motion_storage_estimate_includes_every_exposure_and_speed(monkeypatch):
    from types import SimpleNamespace
    pinned = {"f_numbers": [2.75, 3.2, 4.12, 5.6, 8, 11]}
    monkeypatch.setattr(jobs, "_snapshot", lambda *args: pinned)
    monkeypatch.setattr(jobs, "fingerprint", lambda: {"test": "identity"})
    monkeypatch.setattr(jobs.storage, "status", lambda _: {
        "remaining_bytes": 500e9, "volume_free_bytes": 1e12, "path": "/test/ssd"})
    service = SimpleNamespace(registry=SimpleNamespace(submit=lambda doc, identity: doc))
    job = jobs.submit(service, jobs.aperture_motion_recipe(SOURCE))
    assert job["estimated_bytes"] == 216 * 512 * 1024**2
    assert job["default_worker_timeout_seconds"] == 14400
    pinned["f_numbers"] = list(range(8))
    with pytest.raises(ValueError, match="216"):
        jobs.submit(service, jobs.aperture_motion_recipe(SOURCE))
