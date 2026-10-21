import json

import pytest
from fastapi.testclient import TestClient

from camera_platform import db_design_job as jobs
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService
from camera_platform.worker import execute


@pytest.mark.parametrize(
    "payload",
    [
        {"lens_keys": ["canon_p0248", "reference"]},
        {"sensor_keys": ["reference_imx462", "reference_imx462"]},
        {"sensor_keys": ["reference_imx462", "unreviewed"]},
        {"native_size": 32},
        {"schema": "old"},
        {"comparison_mode": "unknown"},
        {"lens_keys": ["reference", "canon_p0248_open"]},
        {"comparison_mode": "aperture_diffraction"},
    ],
)
def test_explicit_selected_candidates_and_fixed_native_contract(payload):
    with pytest.raises(ValueError):
        jobs.specification(payload)


def test_specification_does_not_mutate_request():
    payload = {"sensor_keys": ["reference_imx462", "imx307"]}
    result = jobs.specification(payload)
    result["sensor_keys"].append("imx662")
    assert payload == {"sensor_keys": ["reference_imx462", "imx307"]}


def test_aperture_recipe_pins_the_same_sensor_and_both_physical_irises():
    recipe = jobs.aperture_diffraction_recipe()
    assert jobs.specification(recipe) == recipe
    assert recipe["lens_keys"] == ["reference", "canon_p0248_open", "canon_p0248"]
    assert recipe["sensor_keys"] == ["reference_imx462"]
    with pytest.raises(ValueError, match="pinned"):
        jobs.specification({**recipe, "sensor_keys": ["reference_imx462", "imx662"]})
    assert jobs.specification({})["comparison_mode"] == "lens_sensor"


def test_aperture_sweep_binds_numeric_conditions_and_strict_selection_policy():
    recipe = jobs.aperture_sweep_recipe()
    assert jobs.specification(recipe) == recipe
    assert recipe["f_numbers"] == [2.75, 3.2, 4.12, 5.6, 8.0, 11.0]
    assert recipe["lens_keys"] == [
        "reference",
        "canon_p0248_open",
        "canon_p0248_f_3p2",
        "canon_p0248",
        "canon_p0248_f_5p6",
        "canon_p0248_f_8",
        "canon_p0248_f_11",
    ]
    custom = jobs.specification(
        {
            "comparison_mode": "aperture_sweep",
            "f_numbers": [3.1, 7.5],
            "selection_policy": {"min_snr_db": 22},
        }
    )
    assert custom["lens_keys"] == ["reference", "canon_p0248_f_3p1", "canon_p0248_f_7p5"]
    assert custom["selection_policy"] == {
        "min_snr_db": 22.0,
        "min_relative_signal": 0.1,
        "max_saturation_fraction": 0.01,
    }
    assert "selection_policy" not in jobs.specification({})


@pytest.mark.parametrize(
    "values",
    [
        [2.7, 4.12],
        [3.2, 12],
        [4.12, 2.75],
        [3.2, 3.2],
        [True, 4.12],
        [float("nan"), 4.12],
        [3.2, float("inf")],
        [3.2],
        "3.2,4.12",
    ],
)
def test_invalid_aperture_sweep_is_not_silently_reordered_or_clamped(values):
    with pytest.raises(ValueError):
        jobs.specification({"comparison_mode": "aperture_sweep", "f_numbers": values})


@pytest.mark.parametrize(
    "policy",
    [
        {"minimum_snr_db": 25},
        {"min_snr_db": True},
        {"min_snr_db": float("nan")},
        {"min_relative_signal": 1.1},
        {"max_saturation_fraction": -0.1},
        {"min_snr_db": 81},
    ],
)
def test_selection_policy_requires_finite_bounded_explicit_names(policy):
    with pytest.raises(ValueError):
        jobs.aperture_sweep_recipe(selection_policy=policy)


def test_sweep_rejects_sensor_substitution_and_stale_lens_keys():
    recipe = jobs.aperture_sweep_recipe()
    with pytest.raises(ValueError, match="lens_keys"):
        jobs.specification({**recipe, "f_numbers": [2.75, 4.12]})
    with pytest.raises(ValueError, match="IMX462"):
        jobs.specification({**recipe, "sensor_keys": ["imx662"]})
    with pytest.raises(ValueError, match="require aperture_sweep"):
        jobs.specification({"selection_policy": {"min_snr_db": 25}})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from collections import namedtuple

    service = PlatformService(tmp_path / "state", tmp_path)
    monkeypatch.setenv("CAMERA_PLATFORM_WORKSHOP_ROOT", str(tmp_path / "ssd"))
    monkeypatch.setattr(jobs, "snapshot", lambda *args: {"lenses": {}, "sensors": {}})
    monkeypatch.setattr(jobs, "fingerprint", lambda: {"engine": "frozen"})
    # Lifecycle fixtures write tiny arrays; isolate the declared demo estimate
    # from the developer's internal-disk free space (real runs use external SSD).
    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(jobs.storage.shutil, "disk_usage", lambda _: usage(2e12, 1e11, 1.9e12))
    return service


def test_resource_failure_is_explicit_not_a_smaller_capture(setup, monkeypatch):
    monkeypatch.setattr(
        jobs.storage,
        "status",
        lambda _: {
            "remaining_bytes": 1,
            "volume_free_bytes": 1,
        },
    )
    with pytest.raises(ValueError, match="no sampling reduction"):
        jobs.submit(setup, {})
    assert setup.registry.list_runs() == []


def test_changed_source_is_rejected_before_execution(setup, monkeypatch):
    job = jobs.submit(setup, {})
    monkeypatch.setattr(jobs, "snapshot", lambda *args: {"changed": True})
    with pytest.raises(IntegrityError, match="sources changed"):
        jobs.execute(setup, job)


def test_common_lifecycle_and_bound_artifacts(setup, monkeypatch):
    from camera_platform import db_design_engine as engine

    def run(workspace, directory, request, snapshot, progress, check):
        progress("Real test adapter invocation")
        (directory / "array.npy").write_bytes(b"preserved fixture")
        return {"candidates": [], "raw_preserved": True, "physical_validation": "not_established"}

    monkeypatch.setattr(engine, "run", run)
    app = create_app(setup.registry.directory, setup.workspace, start_worker=False, seed_demo=False)
    with TestClient(app) as http:
        token = http.get("/api/v1/session").json()["token"]
        http.headers["X-Platform-Token"] = token
        client = object.__new__(PlatformClient)
        client.http = http
        job = client.submit_db_design_study()
        assert setup.registry.claim()["id"] == job["id"]
        execute(setup, job["id"])
        finished = client.wait(job["id"], timeout=2, poll_interval=0.01)
        assert finished["status"] == "succeeded"
        summary = finished["result"]["summary"]
        assert summary["raw_preserved"]
        assert http.get(summary["report_url"]).status_code == 200
        manifest = http.get(summary["manifest_url"]).json()
        assert "array.npy" in manifest["files"]
        path = jobs.workshop._directory(setup, job["id"]) / "array.npy"
        path.write_bytes(b"tampered")
        with pytest.raises(IntegrityError):
            jobs.workshop.verify_files(setup, jobs.workshop.get(setup, job["id"]))
        assert json.loads((path.parent / "manifest.json").read_text()) == manifest


def test_queued_study_cancel_uses_common_jobs(setup):
    job = jobs.submit(setup, {})
    setup.registry.cancel(job["id"])
    assert setup.registry.get_run(job["id"])["status"] == "cancelled"


def test_aperture_report_retains_policy_metrics_and_failed_candidate_reason(setup, monkeypatch):
    from camera_platform import db_design_engine as engine

    def run(workspace, directory, request, snapshot, progress, check):
        return {
            "candidates": [],
            "aperture_sweep": {
                "baseline_candidate_id": "candidate-01",
                "scope": "Fixed conditions only",
                "selection": {
                    "recommended_candidate_id": "candidate-02",
                    "policy": request["selection_policy"],
                    "decisions": [
                        {
                            "candidate_id": "candidate-01",
                            "eligible": False,
                            "reasons": ["outer-ne:snr_db_constraint"],
                        }
                    ],
                },
                "rows": [
                    {
                        "candidate_id": "candidate-01",
                        "f_number": 2.75,
                        "fields": [
                            {
                                "field_id": "outer-ne",
                                "processed_sfr50_cpp": 0.2,
                                "mean_signal_e": 100,
                                "relative_signal": 1,
                                "snr_db": 20,
                                "saturation_fraction": 0,
                            }
                        ],
                    }
                ],
            },
        }

    monkeypatch.setattr(engine, "run", run)
    job = jobs.submit(setup, jobs.aperture_sweep_recipe())
    assert job["default_worker_timeout_seconds"] == 14400
    setup.registry.claim()
    result = jobs.execute(setup, job)
    report = (jobs.workshop._directory(setup, job["id"]) / "report.html").read_text()
    assert "Temporal SNR (dB)" in report
    assert "outer-ne:snr_db_constraint" in report
    assert "min_snr_db" in report
    assert result["summary"]["result_url"].endswith(
        "&candidate-a=candidate-01&candidate-b=candidate-02"
    )
