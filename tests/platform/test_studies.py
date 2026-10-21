"""Design experiments preserve physical semantics, provenance and batch integrity."""

from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.contracts import TestCreate as SpecificationContract
from camera_platform.demo import seed
from camera_platform.registry import ConflictError, IntegrityError
from camera_platform.service import PlatformService
from camera_platform.studies import _analysis_rows, prepare


@pytest.fixture
def setup(tmp_path):
    service = PlatformService(tmp_path)
    seed(service)
    demo = service.bootstrap()["demo"]
    spec = {
        "name": "Exposure/read-noise design",
        "question": "What exposure/noise combinations meet our SNR target?",
        "assembly_ids": [demo["assembly_ids"][0]],
        "test_id": demo["test_id"],
        "axes": [
            {"parameter": "exposure_s", "unit": "s", "values": [0.002, 0.008]},
            {"parameter": "read_noise_e", "unit": "electron", "values": [1.0, 5.0]},
        ],
        "objectives": [
            {"metric": "min_theoretical_snr_db", "direction": "maximize"},
            {"metric": "exposure_ms", "direction": "minimize"},
        ],
        "constraints": [
            {
                "metric": "min_saturation_margin_e",
                "operator": ">=",
                "value": 0.0,
                "unit": "electron/pixel",
            }
        ],
        "frames": 2,
        "output_size": [8, 8],
    }
    return service, spec


def execute(service):
    while job := service.registry.claim():
        service.registry.finish(job["id"], result=service.execute_run(job))


def submit(service, spec):
    plan = service.plan_study(spec)
    return service.create_study({"specification": spec, "plan_hash": plan["plan_hash"]})


def test_whole_plan_is_read_only_and_submission_is_idempotent(setup):
    service, spec = setup
    before = service.bootstrap()["counts"]
    head = service.registry.audit()["head_hash"]
    plan = service.plan_study(spec)
    assert plan["run_count"] == 5
    assert service.bootstrap()["counts"] == before
    assert service.registry.audit()["head_hash"] == head
    study = submit(service, spec)
    after = service.bootstrap()["counts"]
    assert submit(service, spec) == study
    assert service.bootstrap()["counts"] == after
    assert after["runs"] == before["runs"] + 5
    assert after["studies"] == 1


def test_response_runs_show_independent_charge_snr_and_secant_scaling(setup):
    service, spec = setup
    study = submit(service, spec)
    execute(service)
    analysis = service.analyze_study(study["id"])
    assert analysis["coverage"]["succeeded"] == 5
    assert analysis["total_uncertainty"]["kind"] == "unknown"
    rows = analysis["rows"]
    baseline = next(r for r in rows if r["is_baseline"])
    noise_low = next(r for r in rows if r["changed"] == {"read_noise_e": 1.0})
    noise_high = next(r for r in rows if r["changed"] == {"read_noise_e": 5.0})
    assert (
        noise_low["metrics"]["mean_photoelectrons_e"]
        == noise_high["metrics"]["mean_photoelectrons_e"]
    )
    assert (
        noise_low["metrics"]["min_theoretical_snr_db"]
        > noise_high["metrics"]["min_theoretical_snr_db"]
    )
    for row in rows:
        if "exposure_s" in row["changed"]:
            ratio = row["coordinates"]["exposure_s"] / baseline["coordinates"]["exposure_s"]
            assert row["metrics"]["mean_photoelectrons_e"] == pytest.approx(
                baseline["metrics"]["mean_photoelectrons_e"] * ratio
            )
    for segment in analysis["sensitivities"]:
        slope = segment["metrics"]["mean_photoelectrons_e"]["slope"]
        expected = (
            0
            if segment["parameter"] == "read_noise_e"
            else baseline["metrics"]["mean_photoelectrons_e"]
            / baseline["coordinates"]["exposure_s"]
        )
        assert slope == pytest.approx(expected)
        assert segment["derivative_error"] == "unknown"
    assert noise_high["pareto_status"] == "dominated"
    assert noise_low["pareto_status"] == "non_dominated"
    assert service.registry.audit()["integrity"] == "verified"


@pytest.mark.parametrize(
    "parameter,unit,values",
    [
        ("gain", "1", [2.0]),
        ("full_well_e", "electron", [8000.0]),
        ("dark_current_e_per_s", "electron/s", [25.0]),
        ("qe_scale", "1", [0.5]),
        ("irradiance_scale", "1", [0.5]),
    ],
)
def test_physical_variants_pin_matching_contracts_and_change_real_output(
    setup, parameter, unit, values
):
    service, spec = setup
    if parameter == "gain":
        assembly = service.registry.get("assemblies", spec["assembly_ids"][0])
        ids = []
        for identity in assembly["model_ids"]:
            parent = service.registry.get("models", identity)
            data = {k: deepcopy(v) for k, v in parent.items() if k in ModelCreate.model_fields}
            data["parents"] = [identity]
            data["valid_domain"]["gain"] = {"min": 1.0, "max": 2.0, "unit": "1"}
            ids.append(service.create_model(data)["id"])
        data = {k: deepcopy(v) for k, v in assembly.items() if k in AssemblyCreate.model_fields}
        data["model_ids"] = ids
        spec["assembly_ids"] = [service.create_assembly(data)["id"]]
    spec["axes"] = [{"parameter": parameter, "unit": unit, "values": values}]
    study = submit(service, spec)
    original_test = service.registry.get("tests", spec["test_id"])
    execute(service)
    runs = [service.registry.get_run(i) for i in study["run_ids"]]
    original, variant = runs
    assert original["status"] == variant["status"] == "succeeded"
    before = {a["name"]: a["sha256"] for a in original["artifacts"]}
    after = {a["name"]: a["sha256"] for a in variant["artifacts"]}
    assert before["raw_frames.npy"] != after["raw_frames.npy"]
    assert service.registry.get("tests", spec["test_id"]) == original_test
    if parameter == "gain":
        assert (
            variant["request"]["readout"]["gain"]
            == variant["test_snapshot"]["conditions"]["gain"]["value"]
            == values[0]
        )
        assert variant["assembly_snapshot"]["conditions"] == variant["test_snapshot"]["conditions"]
    if parameter in ["qe_scale", "irradiance_scale"]:
        assert np.asarray(variant["result"]["summary"]["channel_electrons"]) == pytest.approx(
            np.asarray(original["result"]["summary"]["channel_electrons"]) * 0.5
        )


@pytest.mark.parametrize(
    "axis",
    [
        {"parameter": "temperature_c", "unit": "degC", "values": [50.0]},
        {"parameter": "exposure_s", "unit": "ms", "values": [8.0]},
        {"parameter": "qe_scale", "unit": "1", "values": [100.0]},
        {"parameter": "gain", "unit": "1", "values": [10000.0]},
        {"parameter": "read_noise_e", "unit": "electron", "values": [-1.0]},
        {"parameter": "read_noise_e", "unit": "electron", "values": [True]},
        {"parameter": "read_noise_e", "unit": "electron", "values": [1.0, 1.0]},
    ],
)
def test_invalid_candidate_is_rejected_before_any_write(setup, axis):
    service, spec = setup
    spec["axes"].append(axis) if axis["parameter"] == "qe_scale" else spec.update(axes=[axis])
    before = service.bootstrap()["counts"]
    audit = service.registry.audit()["head_hash"]
    with pytest.raises(ValueError):
        service.plan_study(spec)
    assert service.bootstrap()["counts"] == before
    assert service.registry.audit()["head_hash"] == audit


def test_preview_cannot_authorize_different_specification(setup):
    service, spec = setup
    plan = service.plan_study(spec)
    spec["axes"][0]["values"] = [0.003]
    with pytest.raises(ConflictError, match="changed"):
        service.create_study({"specification": spec, "plan_hash": plan["plan_hash"]})
    assert service.bootstrap()["counts"]["studies"] == 0
    assert service.registry.list_runs() == []


def test_cartesian_has_interactions_and_only_baseline_slices_get_secants(setup):
    service, spec = setup
    spec["design"] = "cartesian"
    study = submit(service, spec)
    assert study["run_count"] == 5
    assert sum(len(c["changed"]) == 2 for c in study["candidates"]) == 4
    execute(service)
    assert service.analyze_study(study["id"])["sensitivities"] == []
    spec["axes"] = [
        {"parameter": "read_noise_e", "unit": "electron", "values": list(map(float, range(6)))},
        {
            "parameter": "dark_current_e_per_s",
            "unit": "electron/s",
            "values": list(map(float, range(6))),
        },
    ]
    with pytest.raises(ValueError, match="24"):
        service.plan_study(spec)


def test_insufficient_capacity_keeps_entire_plan_unpublished(setup):
    service, spec = setup
    plan = service.plan_study(spec)
    for i in range(61):
        service.registry.submit({"name": "queue fixture", "i": i}, str(i))
    before = service.bootstrap()["counts"]
    head = service.registry.audit()["head_hash"]
    with pytest.raises(ConflictError, match="capacity"):
        service.create_study({"specification": spec, "plan_hash": plan["plan_hash"]})
    assert service.bootstrap()["counts"] == before
    assert service.registry.audit()["head_hash"] == head


def test_staged_corruption_rolls_back_preceding_revisions(setup):
    service, spec = setup
    plan, records, requests = prepare(service, spec)
    records[-1][1]["content_hash"] = "0" * 64
    before = service.bootstrap()["counts"]
    head = service.registry.audit()["head_hash"]
    with pytest.raises(IntegrityError):
        service.registry.publish_study(plan, records, requests)
    assert service.bootstrap()["counts"] == before
    assert service.registry.audit()["head_hash"] == head


def test_changed_qe_becomes_estimated_unknown_preserving_original_bound(setup):
    service, spec = setup
    assembly = service.registry.get("assemblies", spec["assembly_ids"][0])
    original = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "sensor"
    )
    model = {k: deepcopy(v) for k, v in original.items() if k in ModelCreate.model_fields}
    bound = {
        "kind": "absolute_bound",
        "unit": "electron/photon",
        "value": 0.001,
        "source_kind": "estimated",
        "source_uri": "fixture://study-test",
        "method_and_version": "test/1",
    }
    model["parameter_uncertainty"] = {"qe": bound}
    bounded = service.create_model(model)
    data = {k: deepcopy(v) for k, v in assembly.items() if k in AssemblyCreate.model_fields}
    data["model_ids"] = [bounded["id"] if i == original["id"] else i for i in assembly["model_ids"]]
    spec["assembly_ids"] = [service.create_assembly(data)["id"]]
    spec["axes"] = [{"parameter": "qe_scale", "unit": "1", "values": [0.5]}]
    study = submit(service, spec)
    variant = service.registry.get_run(study["run_ids"][1])
    sensor = next(m for m in variant["model_snapshots"] if m["kind"] == "sensor")
    assert sensor["source_kind"] == "estimated" and sensor["physical_instance"] is None
    assert sensor["parents"] == [bounded["id"]]
    assert sensor["parameter_uncertainty"]["qe"]["kind"] == "unknown"
    assert service.registry.get("models", bounded["id"]) == bounded


def test_pareto_matches_hand_computed_front_and_keeps_missing_data_unknown():
    # Max SNR / min exposure: A and B tie on the front, C loses; D lacks SNR.
    candidates = [{"index": i, "is_baseline": i == 0} for i in range(4)]
    plan = {
        "candidates": candidates,
        "specification": {
            "objectives": [
                {"metric": "min_theoretical_snr_db", "direction": "maximize"},
                {"metric": "exposure_ms", "direction": "minimize"},
            ],
            "constraints": [
                {
                    "metric": "min_saturation_margin_e",
                    "operator": ">=",
                    "value": 0.0,
                    "unit": "electron/pixel",
                }
            ],
        },
    }
    runs = [
        {
            "id": str(i),
            "content_hash": str(i),
            "status": "succeeded",
            "test_snapshot": {"conditions": {"exposure_s": {"value": x / 1000}}},
            "result": {
                "summary": {"theoretical_snr_db": [[snr]], "saturation_margin_e": [[margin]]}
            },
        }
        for i, (x, snr, margin) in enumerate([(2, 20, 1), (4, 30, 1), (4, 10, 1), (1, None, -1)])
    ]
    rows = _analysis_rows(plan, runs)
    assert [r["pareto_status"] for r in rows] == [
        "non_dominated",
        "non_dominated",
        "dominated",
        "excluded",
    ]
    assert rows[-1]["feasibility"] == "infeasible"
    runs[1]["status"] = "failed"
    runs[1]["error"] = "solver error"
    rows = _analysis_rows(plan, runs)
    assert rows[1]["pareto_status"] == "excluded" and rows[1]["feasibility"] == "unknown"
    assert rows[1]["metrics"] == {}


def test_http_plan_submit_analysis_and_openapi_contract(setup):
    service, spec = setup
    app = create_app(service.registry.directory, start_worker=False)
    with TestClient(app) as client:
        info = client.get("/api/v1/bootstrap").json()
        headers = {"x-platform-token": info["token"]}
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/api/v1/studies/catalog").json()["max_runs"] == 24
        response = client.post("/api/v1/studies/plan", json=spec, headers=headers)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert client.get("/api/v1/studies").json() == []
        response = client.post(
            "/api/v1/studies",
            json={"specification": spec, "plan_hash": plan["plan_hash"]},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        identity = response.json()["id"]
        analysis = client.get(f"/api/v1/studies/{identity}/analysis").json()
        assert analysis["status"] == "running" and analysis["coverage"]["queued"] == 5
        assert all(r["pareto_status"] == "excluded" for r in analysis["rows"])
        assert client.get("/api/v1/bootstrap").json()["counts"]["studies"] == 1


def test_quick_study_uses_matching_test_and_does_not_invent_raw(setup):
    service, spec = setup
    original = service.registry.get("tests", spec["test_id"])
    test = {k: deepcopy(v) for k, v in original.items() if k in SpecificationContract.model_fields}
    test["included_effects"] = ["sensor_effective_qe", "mean_dark_charge", "readout_clipping"]
    test["parent_test_id"] = original["id"]
    spec.update(test_id=service.create_test(test)["id"], plugin_id="quick_estimate_v1")
    study = submit(service, spec)
    execute(service)
    analysis = service.analyze_study(study["id"])
    assert analysis["coverage"]["succeeded"] == 5
    assert all(r["metrics"]["max_relative_color_shading"] is None for r in analysis["rows"])
    for identity in study["run_ids"]:
        run = service.registry.get_run(identity)
        assert not any(a["name"].startswith("raw") for a in run["artifacts"])
    spec["objectives"][0]["metric"] = "max_relative_color_shading"
    with pytest.raises(ValueError, match="Response-DB"):
        service.plan_study(spec)


def test_multiple_assemblies_keep_distinct_baselines_and_all_plan_sources(setup):
    service, spec = setup
    spec["assembly_ids"] = service.bootstrap()["demo"]["assembly_ids"]
    study = submit(service, spec)
    assert study["run_count"] == 10
    execute(service)
    analysis = service.analyze_study(study["id"])
    assert len(analysis["sensitivities"]) == 8
    baselines = [r for r in analysis["rows"] if r["is_baseline"]]
    assert {r["base_assembly_id"] for r in baselines} == set(spec["assembly_ids"])
    assert (
        baselines[0]["metrics"]["max_relative_color_shading"]
        != baselines[1]["metrics"]["max_relative_color_shading"]
    )
    assert all(r["result_hash"] for r in analysis["rows"])
    assert analysis["source_hashes"] == study["source_hashes"]
    assert not analysis["pareto_provisional"]
