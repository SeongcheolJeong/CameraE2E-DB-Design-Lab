import copy
import json

import pytest

from camera_platform import db_design_job as jobs
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService

SOURCE = "run-" + "a" * 32


def test_condition_recipe_is_source_bound_and_does_not_exclude_narrow_irises():
    spec = jobs.specification({"comparison_mode": "aperture_conditions", "source_run_id": SOURCE})
    assert spec["luminance_cd_m2"] == [10, 100, 1000, 10000]
    assert spec["exposure_ms"] == [1, 4, 16]
    assert spec["selection_policy"]["min_relative_signal"] == 0
    assert jobs.aperture_sweep_recipe()["selection_policy"]["min_relative_signal"] == .1
    assert jobs.specification(spec) == spec
    modified = jobs.specification({**spec, "selection_policy": {"min_relative_signal": .25}})
    assert modified["selection_policy"]["min_relative_signal"] == .25


@pytest.mark.parametrize("change", [
    {"source_run_id": None}, {"source_run_id": "../../source"},
    {"luminance_cd_m2": [0]}, {"luminance_cd_m2": [float("inf")]},
    {"luminance_cd_m2": [True]}, {"luminance_cd_m2": [100, 10]},
    {"exposure_ms": [1, 1]}, {"exposure_ms": [101]}, {"exposure_ms": []},
    {"exposure_ms": "1,4"}, {"sensor_keys": ["imx662"]}, {"f_numbers": [2.75, 8]},
    {"luminance_cd_m2": list(range(1, 9)), "exposure_ms": [1, 2, 3, 4]},
    {"schema": "other"}, {"name": ""}, {"selection_policy": {"min_snr_db": float("nan")}},
])
def test_invalid_grid_rejected_without_clamping_or_reduction(change):
    with pytest.raises(ValueError):
        jobs.specification({**jobs.aperture_conditions_recipe(SOURCE), **change})


def test_sdk_condition_submit_uses_shared_lifecycle_endpoint():
    client = object.__new__(PlatformClient)
    calls = []
    client.post = lambda path, payload: calls.append((path, payload)) or {"id": SOURCE}
    assert client.submit_aperture_conditions(SOURCE, exposure_ms=[4])["id"] == SOURCE
    assert calls == [("db-design/studies", {"comparison_mode": "aperture_conditions",
                                           "source_run_id": SOURCE, "exposure_ms": [4]})]


@pytest.fixture
def service(tmp_path, monkeypatch):
    from collections import namedtuple

    svc = PlatformService(tmp_path / "state", tmp_path)
    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path / "ssd"))
    monkeypatch.setattr(jobs, "fingerprint", lambda: {"engine": "frozen"})
    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(jobs.storage.shutil, "disk_usage", lambda _: usage(2e12, 1e11, 1.9e12))
    return svc


def test_source_requires_completed_preserved_aperture_sweep(service, monkeypatch):
    record = {"id": SOURCE, "status": "succeeded", "db_design_study": {},
              "source_code": {"version": "old"}, "result": {"summary": {}}}
    monkeypatch.setattr(jobs.workshop, "get", lambda *a: record)
    spec = jobs.aperture_conditions_recipe(SOURCE)
    with pytest.raises(ValueError, match="completed"):
        jobs.condition_source(service, spec)
    record["db_design_study"] = {"schema": jobs.SCHEMA}
    with pytest.raises(ValueError, match="preserved"):
        jobs.condition_source(service, spec)
    record["result"]["summary"] = {
        "comparison_mode": "aperture_sweep", "aperture_sweep": {"rows": []},
        "raw_preserved": True, "manifest_sha256": "b" * 64,
        "configuration": {"f_numbers": [2.75, 4.12]},
    }
    _, summary, frozen = jobs.condition_source(service, spec)
    assert frozen["source_manifest_sha256"] == "b" * 64
    summary["calibration"] = "tampered"
    assert jobs.condition_source(service, spec)[2] != frozen


def test_condition_execute_rejects_changed_source_before_calculation(service, monkeypatch):
    pin = {"f_numbers": [2.75, 4.12], "manifest": "old"}
    monkeypatch.setattr(jobs, "condition_source", lambda *a: (None, {}, copy.deepcopy(pin)))
    job = jobs.submit(service, jobs.aperture_conditions_recipe(SOURCE, [100], [4]))
    assert job["estimated_bytes"] == 1024**3
    pin["manifest"] = "changed"
    with pytest.raises(IntegrityError, match="sources changed"):
        jobs.execute(service, job)


@pytest.mark.parametrize("motion", [False, True])
def test_condition_saved_report_and_query_bind_actual_condition(service, monkeypatch, motion):
    if motion:
        from camera_platform import db_design_motion as adapter
    else:
        from camera_platform import db_design_conditions as adapter

    monkeypatch.setattr(jobs, "condition_source", lambda *a: (
        service.workspace, {}, {"f_numbers": [2.75, 4.12], "manifest": "frozen"}
    ))
    sweep = {
        "baseline_candidate_id": "candidate-01", "rows": [],
        "selection": {"recommended_candidate_id": None, "decisions": []},
    }

    def run(workspace, directory, request, source_root, source_summary, progress, check, cancel_check):
        progress("condition capture fixture")
        (directory / "raw.npy").write_bytes(b"new independent raw fixture")
        return {"candidates": [], "raw_preserved": True, "aperture_conditions": {
            "conditions": [{"id": "condition-000", "luminance_cd_m2": 100,
                            "exposure_ms": 4, "object_speed_m_s": 1,
                            "candidates": [], "aperture_sweep": sweep}]
        }}

    monkeypatch.setattr(adapter, "run", run)
    recipe = jobs.aperture_motion_recipe if motion else jobs.aperture_conditions_recipe
    job = jobs.submit(service, recipe(SOURCE, [100], [4]))
    service.registry.claim()
    result = jobs.execute(service, job)
    summary = result["summary"]
    assert "condition=condition-000" in summary["result_url"]
    assert summary["result_url"].endswith("candidate-a=candidate-01&candidate-b=candidate-01")
    folder = jobs.workshop._directory(service, job["id"])
    assert "no feasible candidate" in (folder / "report.html").read_text()
    if motion:
        assert "1 m/s at 20 m" in (folder / "report.html").read_text()
        assert "longer exposures do not model motion blur" not in (folder / "report.html").read_text()
    assert "raw.npy" in json.loads((folder / "manifest.json").read_text())["files"]
