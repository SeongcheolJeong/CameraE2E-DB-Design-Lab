"""Independent math, split integrity and real camera-reference surrogate workflows."""

import itertools
import json
import shutil
import sqlite3
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient
from scipy.interpolate import RBFInterpolator

from camera_platform.api import create_app
from camera_platform.demo import seed
from camera_platform.registry import ConflictError
from camera_platform.service import PlatformService
from camera_platform.surrogate_math import domain_and_split, evaluate, fit


def drain(service):
    while job := service.registry.claim():
        service.registry.finish(job["id"], result=service.execute_run(job))


def study(service, demo, name, exposures, noise):
    spec = {
        "name": name,
        "question": "Numerical SNR and charge versus exposure/read noise",
        "assembly_ids": [demo["assembly_ids"][0]],
        "test_id": demo["test_id"],
        "design": "cartesian",
        "axes": [
            {"parameter": "exposure_s", "unit": "s", "values": exposures},
            {"parameter": "read_noise_e", "unit": "electron", "values": noise},
        ],
        "objectives": [{"metric": "min_theoretical_snr_db", "direction": "maximize"}],
        "frames": 2,
        "output_size": [8, 8],
    }
    plan = service.plan_study(spec)
    return service.create_study({"specification": spec, "plan_hash": plan["plan_hash"]})


@pytest.fixture(scope="module")
def reference_state(tmp_path_factory):
    path = tmp_path_factory.mktemp("surrogate-references")
    service = PlatformService(path)
    seed(service)
    demo = service.bootstrap()["demo"]
    training = study(service, demo, "Training", [0.002, 0.004, 0.006, 0.008], [1.0, 2.0, 3.0, 5.0])
    validation = study(service, demo, "Held out", [0.003, 0.005, 0.007], [1.5, 2.5, 4.0])
    drain(service)
    spec = {
        "name": "Exposure/noise RBF",
        "purpose": "Approximate a fixed numerical camera response",
        "training": [{"study_id": training["id"], "run_ids": training["run_ids"]}],
        "validation": [
            {
                "study_id": validation["id"],
                "run_ids": [
                    r
                    for r, c in zip(validation["run_ids"], validation["candidates"], strict=True)
                    if not c["is_baseline"]
                ],
            }
        ],
        "metrics": [
            {"metric": "min_theoretical_snr_db", "unit": "dB", "max_absolute_error": 0.3},
            {
                "metric": "mean_photoelectrons_e",
                "unit": "electron/pixel",
                "max_absolute_error": 1e-6,
            },
        ],
    }
    return path, spec


@pytest.fixture
def setup(reference_state, tmp_path):
    path, spec = reference_state
    state = tmp_path / "state"
    shutil.copytree(path, state)
    return PlatformService(state), deepcopy(spec)


def trained(service, spec):
    plan = service.plan_surrogate(spec)
    job = service.create_surrogate({"specification": spec, "plan_hash": plan["plan_hash"]})
    drain(service)
    return service.registry.get_run(job["id"])


def point(x=0.0055, noise=3.5):
    return {
        "points": [{"exposure_s": x, "read_noise_e": noise}],
        "units": {"exposure_s": "s", "read_noise_e": "electron"},
    }


@pytest.mark.parametrize("dimensions", [1, 2, 3, 6])
def test_rbf_matches_independent_scipy_and_json_roundtrip(dimensions):
    rng = np.random.default_rng(53)
    x = np.array(
        list(itertools.product([0.0, 1.0], repeat=dimensions))
        + rng.uniform(0.1, 0.9, (5, dimensions)).tolist()
    )
    v = rng.uniform(0.1, 0.9, (max(3, dimensions + 1), dimensions))
    y = np.column_stack([np.sin(x.sum(axis=1)), 20 + x @ np.arange(1, dimensions + 1)])
    model = fit(x, y, domain_and_split(x, v))
    restored = json.loads(json.dumps(model, allow_nan=False))
    np.testing.assert_allclose(
        evaluate(restored, v),
        RBFInterpolator(x, y, kernel="thin_plate_spline", degree=1)(v),
        rtol=1e-11,
        atol=1e-11,
    )
    np.testing.assert_allclose(evaluate(restored, x), y, rtol=1e-11, atol=1e-11)


def test_oat_cross_does_not_authorize_box_corners():
    with pytest.raises(ValueError, match="corner"):
        domain_and_split(
            [[0, 0.5], [0.5, 0], [0.5, 0.5], [1, 0.5], [0.5, 1]],
            [[0.2, 0.3], [0.4, 0.7], [0.8, 0.6]],
        )


@pytest.mark.parametrize(
    "validation",
    [
        [[0.0, 0.0], [0.4, 0.7], [0.8, 0.6]],
        [[1e-10, 1e-10], [0.4, 0.7], [0.8, 0.6]],
        [[-0.01, 0.3], [0.4, 0.7], [0.8, 0.6]],
        [[0.2, 0.3], [0.2, 0.7], [0.2, 0.6]],
    ],
)
def test_invalid_holdout_geometry_is_refused(validation):
    with pytest.raises(ValueError):
        domain_and_split([[0, 0], [0, 1], [1, 0], [1, 1]], validation)


def test_plan_has_no_writes_and_job_is_idempotent(setup):
    service, spec = setup
    before = service.bootstrap()["counts"]
    head = service.registry.audit()["head_hash"]
    plan = service.plan_surrogate(spec)
    assert service.bootstrap()["counts"] == before
    assert service.registry.audit()["head_hash"] == head
    payload = {"specification": spec, "plan_hash": plan["plan_hash"]}
    job = service.create_surrogate(payload)
    assert service.create_surrogate(payload)["id"] == job["id"]
    assert job["job_kind"] == "surrogate_training"
    assert job["id"] not in [r["id"] for r in service.list_simulation_runs()]


def test_real_reference_fit_predict_and_preserved_evidence(setup, monkeypatch):
    service, spec = setup
    before = service.bootstrap()["counts"]
    job = trained(service, spec)
    model = job["result"]
    assert model["prediction_eligible"], model["numerical_validation"]
    assert model["numerical_validation"]["status"] == "PASS"
    assert model["approval_status"] == "candidate"
    assert model["physical_validation"] == "not_established"
    assert len(model["datasets"]["training"]) == 16
    assert len(model["datasets"]["validation"]) == 9
    for rows in model["datasets"].values():
        for row in rows:
            assert len(row["run_hash"]) == len(row["result_hash"]) == 64
    monkeypatch.setattr(
        "camera_platform.surrogates.fit", lambda *args: pytest.fail("Unexpected re-fit")
    )
    reopened = PlatformService(service.registry.directory)
    prediction = reopened.predict_surrogate(job["id"], point())
    assert prediction["total_uncertainty"] == {"kind": "unknown"}
    assert prediction["rows"][0]["outputs"]["mean_photoelectrons_e"] > 0
    assert reopened.registry.get("surrogate_predictions", prediction["id"]) == prediction
    assert reopened.registry.audit()["integrity"] == "verified"
    after = reopened.bootstrap()["counts"]
    for key in ("runs", "measurements", "captures", "validations", "models", "studies"):
        assert after[key] == before[key]


def test_failed_tolerance_is_preserved_and_blocks_prediction(setup):
    service, spec = setup
    spec["metrics"][0]["max_absolute_error"] = 1e-12
    job = trained(service, spec)
    assert job["status"] == "succeeded"
    assert job["result"]["numerical_validation"]["status"] == "FAIL"
    assert not job["result"]["prediction_eligible"]
    with pytest.raises(ValueError, match="holdout"):
        service.predict_surrogate(job["id"], point())
    assert service.registry.list("surrogate_predictions") == []


def test_validation_duplicate_baseline_from_different_run_is_rejected(setup):
    service, spec = setup
    validation = service.registry.get("studies", spec["validation"][0]["study_id"])
    assert validation["run_ids"][0] not in spec["training"][0]["run_ids"]
    spec["validation"][0]["run_ids"].append(validation["run_ids"][0])
    with pytest.raises(ValueError, match="coordinates"):
        service.plan_surrogate(spec)


@pytest.mark.parametrize("change", ["tolerance", "purpose"])
def test_changed_plan_is_refused_without_job(setup, change):
    service, spec = setup
    plan = service.plan_surrogate(spec)
    if change == "tolerance":
        spec["metrics"][0]["max_absolute_error"] *= 2
    else:
        spec["purpose"] += " revised"
    with pytest.raises(ConflictError):
        service.create_surrogate({"specification": spec, "plan_hash": plan["plan_hash"]})
    assert service.bootstrap()["counts"]["surrogates"] == 0


@pytest.mark.parametrize(
    "bad",
    [
        {
            "points": [{"exposure_s": 0.008000000000000002, "read_noise_e": 3.5}],
            "units": {"exposure_s": "s", "read_noise_e": "electron"},
        },
        {
            "points": [{"exposure_s": True, "read_noise_e": 3.5}],
            "units": {"exposure_s": "s", "read_noise_e": "electron"},
        },
        {
            "points": [{"exposure_s": 0.005, "read_noise_e": 3.5}],
            "units": {"exposure_s": "ms", "read_noise_e": "electron"},
        },
        {
            "points": [{"exposure_s": 0.005, "read_noise_e": 3.5, "gain": 1.0}],
            "units": {"exposure_s": "s", "read_noise_e": "electron"},
        },
    ],
)
def test_invalid_prediction_has_no_persisted_result(setup, bad):
    service, spec = setup
    job = trained(service, spec)
    with pytest.raises(ValueError):
        service.predict_surrogate(job["id"], bad)
    assert service.registry.list("surrogate_predictions") == []


def test_source_membership_and_units_are_checked(setup):
    service, spec = setup
    spec["validation"][0]["study_id"] = spec["training"][0]["study_id"]
    with pytest.raises(ValueError, match="member"):
        service.plan_surrogate(spec)
    spec["metrics"][0]["unit"] = "1"
    with pytest.raises(ValueError, match="unit"):
        service.plan_surrogate(spec)


def test_api_plan_train_get_predict_and_openapi(setup):
    service, spec = setup
    with TestClient(create_app(service.registry.directory, start_worker=False)) as http:
        b = http.get("/api/v1/bootstrap").json()
        http.headers["X-Platform-Token"] = b["token"]
        assert http.get("/api/v1/surrogates/catalog").status_code == 200
        p = http.post("/api/v1/surrogates/plan", json=spec)
        assert p.status_code == 200, p.text
        submitted = http.post(
            "/api/v1/surrogates", json={"specification": spec, "plan_hash": p.json()["plan_hash"]}
        )
        assert submitted.status_code == 201, submitted.text
        identity = submitted.json()["id"]
        drain(service)
        assert http.get("/api/v1/runs/" + identity).status_code == 404
        assert http.get("/api/v1/surrogates/" + identity).json()["result"]["prediction_eligible"]
        predicted = http.post("/api/v1/surrogates/" + identity + "/predict", json=point())
        assert predicted.status_code == 201, predicted.text
        assert (
            http.get("/api/v1/surrogate_predictions/" + predicted.json()["id"]).json()
            == predicted.json()
        )
        operation = http.get("/openapi.json").json()["paths"]["/api/v1/surrogates/plan"]["post"]
        assert "requestBody" in operation


def test_completed_model_cannot_be_overwritten(setup):
    service, spec = setup
    job = trained(service, spec)
    altered = deepcopy(job["result"])
    altered["coefficients"]["output_offset"][0] += 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with service.registry.transaction() as db:
            db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(altered), job["id"]))
    assert service.registry.get_run(job["id"])["result"] == job["result"]


def test_reference_settings_cannot_be_mixed(setup):
    service, spec = setup
    source = service.registry.get("studies", spec["validation"][0]["study_id"])
    changed = deepcopy(source["specification"])
    changed["seed"] = 17
    plan = service.plan_study(changed)
    submitted = service.create_study({"specification": changed, "plan_hash": plan["plan_hash"]})
    drain(service)
    spec["validation"] = [
        {
            "study_id": submitted["id"],
            "run_ids": [
                r
                for r, c in zip(submitted["run_ids"], submitted["candidates"], strict=True)
                if not c["is_baseline"]
            ],
        }
    ]
    with pytest.raises(ValueError, match="environment/settings"):
        service.plan_surrogate(spec)


def test_worker_refuses_frozen_environment_drift(setup, monkeypatch):
    service, spec = setup
    plan = service.plan_surrogate(spec)
    service.create_surrogate({"specification": spec, "plan_hash": plan["plan_hash"]})
    job = service.registry.claim()
    monkeypatch.setattr(
        "camera_platform.service.environment_fingerprint", lambda: {"changed": True}
    )
    with pytest.raises(ConflictError, match="Worker environment"):
        service.execute_run(job)


def test_reused_holdout_is_recorded_and_not_presented_as_blind_validation(setup):
    service, spec = setup
    first = trained(service, spec)
    spec["name"] += " repeated"
    second = trained(service, spec)
    validation = second["result"]["numerical_validation"]
    assert validation["previous_validation_uses"] == [
        {
            "model_id": first["id"],
            "validation_run_ids": sorted(spec["validation"][0]["run_ids"]),
        }
    ]
    assert "no claim of blinded" in validation["independence_scope"]
    assert first["result"]["coefficients"] == second["result"]["coefficients"]
    with pytest.raises(ValueError, match="surrogate endpoint"):
        service.export_run(second["id"])
