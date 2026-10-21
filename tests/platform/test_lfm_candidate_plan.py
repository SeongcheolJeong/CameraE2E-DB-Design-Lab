"""Frozen model domains and PWM policy jointly screen LFM candidate points."""

from copy import deepcopy

import pytest
from test_hdr_workflow import finish
from test_hdr_workflow import workflow as hdr_workflow
from test_lfm_gain_screen import _candidate_runs
from test_retained_charge_workflow import workflow
from test_trapped_control_workflow import create_program

from camera_platform.contracts import ControlSequenceCreate
from camera_platform.lfm_candidate_plan import REQUEST_SCHEMA, analyze, analyze_job, validate_request
from camera_platform.registry import IntegrityError
from camera_platform.service import PreflightError


def specification(run_id, points):
    return {"schema": REQUEST_SCHEMA, "source_run_id": run_id,
            "candidates": [{"exposure_ns": exposure, "gain": gain}
                           for exposure, gain in points]}


def test_saved_synthetic_gain_range_is_read_only_and_pwm_lattice_still_blocks(workflow):
    service, sdk, runs = _candidate_runs(workflow, declare_gain_roles=False)
    baseline = runs[0]
    before = service.registry.audit()
    spec = specification(baseline["id"], [
        (4_000_000, 1.0), (8_000_000, 0.3), (8_000_000, 0.4), (8_000_000, 1.0),
    ])
    response = sdk.http.post("/api/v1/lfm/candidate-plans", json=spec)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["effective_exposure_grid_ns"]["point_count"] == 1
    assert [candidate["status"] for candidate in result["candidates"]] == [
        "blocked", "blocked", "unknown",
        "eligible_for_new_frozen_run_preflight",
    ]
    assert result["candidates"][0]["reasons"] == ["outside_effective_pwm_exposure_lattice"]
    assert "model_gain_domain_blocked" in result["candidates"][1]["reasons"]
    assert result["candidates"][2]["reasons"] == ["analog_gain_model_roles_undeclared"]
    assert len(result["candidates"][2]["unresolved_gain_role_model_ids"]) == 3
    assert result["candidate_gain_application"] == "electrical_readout_analog_gain"
    assert result["frozen_electrical_digital_gain"] == 1
    assert result["model_gain_intersection"] == {"min": 0.4, "max": 1.0, "unit": "1"}
    assert result["automatic_submission"] is False
    assert result["target_feasibility"] == "not_established"
    assert result["execution_feasibility"] == "requires_new_control_test_and_full_worker_preflight"
    assert service.registry.audit() == before
    tampered = deepcopy(baseline)
    tampered["request"]["capture_program"]["waveform"]["on_time_ns"] += 1
    with pytest.raises(IntegrityError, match="snapshots"):
        analyze_job(tampered, spec)
    artifact = baseline["capture_program_snapshot"]["artifacts"][0]
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"corrupt program source")
    with pytest.raises(IntegrityError, match="checksum"):
        analyze(service, spec)


def test_authored_roles_allow_only_declared_analog_gain_candidate(workflow):
    service, sdk, runs = _candidate_runs(workflow, declare_gain_roles=True)
    run = runs[0]
    result = sdk.http.post("/api/v1/lfm/candidate-plans", json=specification(run["id"], [
        (8_000_000, 0.5), (8_000_000, 0.3),
    ]))
    assert result.status_code == 200, result.text
    report = result.json()
    assert {item["role"] for item in report["model_gain_role_declarations"]} == {
        "optical_response_independent", "sensor_model_readout_condition",
        "raw_input_processing_condition",
    }
    assert report["candidates"][0]["status"] == "eligible_for_new_frozen_run_preflight"
    assert report["candidates"][0]["unresolved_gain_role_model_ids"] == []
    assert report["candidates"][1]["status"] == "blocked"
    assert "model_gain_domain_blocked" in report["candidates"][1]["reasons"]


def test_changed_analog_gain_run_refused_without_model_roles(workflow):
    service, sdk, runs = _candidate_runs(workflow, declare_gain_roles=False)
    baseline = runs[0]
    original = service.registry.get("control_sequences", baseline["control_sequence_id"])
    fields = {key: deepcopy(value) for key, value in original.items()
              if key in ControlSequenceCreate.model_fields}
    fields.update(name="Unscoped analog gain change", revision="unscoped-0.5")
    for frame in fields["payload"]["frames"]:
        frame["applied"]["gain"] = 0.5
    control = sdk.create_control_sequence(fields)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=baseline["assembly_id"],
        scene_sequence_id=baseline["scene_sequence_id"],
        control_sequence_id=control["id"],
        capture_program_id=baseline["capture_program_id"],
    ))
    before = service.registry.audit()
    request = {key: baseline[key] for key in ("assembly_id", "scene_sequence_id",
                                               "capture_program_id", "plugin_id")}
    request.update(control_sequence_id=control["id"], test_id=test["id"],
                   parameters={"seed": 713, "noise_enabled": False})
    with pytest.raises(PreflightError) as error:
        service.submit_run(request)
    assert error.value.report["status"] == "INCONCLUSIVE"
    assert [issue["code"] for issue in error.value.report["issues"]] == [
        "analog_gain_model_role_undeclared"] * 3
    assert service.registry.audit() == before


def test_invalid_gain_role_is_rejected_at_model_registration(workflow):
    service, _, assembly, *_ = workflow
    original = next(service.registry.get("models", identity)
                    for identity in assembly["model_ids"]
                    if service.registry.get("models", identity)["kind"] == "sensor")
    from camera_platform.contracts import ModelCreate

    fields = {key: deepcopy(value) for key, value in original.items()
              if key in ModelCreate.model_fields}
    fields["properties"]["analog_gain_applicability"] = {
        "schema": "camera_analog_gain_applicability_v1",
        "role": "optical_response_independent",
        "basis": "authored_model_scope",
    }
    with pytest.raises(ValueError, match="exact model role"):
        service.create_model(fields)


def test_original_fixed_gain_model_blocks_gain_candidate_before_new_run(workflow):
    service, sdk, *_ = workflow
    _, _, request = create_program(workflow, "electrical_control_trapped_pwm_period")
    run = finish(service, service.submit_run(request))
    exposure = run["request"]["control_sequence"]["frames"][0]["applied"]["exposure_ns"]
    result = sdk.http.post("/api/v1/lfm/candidate-plans", json=specification(run["id"], [
        (exposure, 0.5), (exposure, 1.0), (exposure + 1, 1.0),
    ]))
    assert result.status_code == 200, result.text
    points = result.json()["candidates"]
    assert "model_gain_domain_blocked" in points[0]["reasons"]
    assert points[1]["status"] == "eligible_for_new_frozen_run_preflight"
    assert "outside_effective_pwm_exposure_lattice" in points[2]["reasons"]


@pytest.mark.parametrize("points", [[(8_000_000, True)], [(8_000_000, float("inf"))],
                                          [(8_000_000, 1.0), (8_000_000, 1.0)]])
def test_bad_candidate_input_rejected(points):
    with pytest.raises(ValueError):
        validate_request(specification("run-example", points))


def test_candidate_plan_accepts_real_ae_exposure_changes_and_checks_raw(workflow):
    """A realized AE sequence is not its initial scaffold; verify RAW causality."""
    service, sdk, assembly, scene, control, _, _, request = workflow
    initial_ns = control["payload"]["frames"][0]["applied"]["exposure_ns"]
    period_ns = initial_ns // 2
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"],
        mode="electrical_control_trapped_pwm_period", control_sequence_id=control["id"],
    )
    payload = draft["payload"]
    payload["waveform"].update(period_ns=period_ns, on_time_ns=period_ns // 2)
    policy = payload["electrical_control_policy"]
    policy["period_lattice"]["period_ns"] = period_ns
    policy["exposure_grid_ns"] = {
        "minimum": period_ns, "maximum": initial_ns, "step": period_ns,
    }
    policy["target_black_relative_dn"] = {"minimum": 0.5, "maximum": 1.0}
    program = sdk.create_capture_program(draft)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    ))
    run = finish(service, service.submit_run({
        **request, "plugin_id": "electrical_control_v1",
        "capture_program_id": program["id"], "test_id": test["id"],
    }))
    actual = run["result"]["summary"]["control_sequence"]
    exposures = [frame["applied"]["exposure_ns"] for frame in actual["frames"]]
    assert exposures[0] == initial_ns and period_ns in exposures[1:]
    assert actual != control["payload"]
    spec = specification(run["id"], [(period_ns, 1.0), (initial_ns, 1.0)])
    before = service.registry.audit()
    response = sdk.http.post("/api/v1/lfm/candidate-plans", json=spec)
    assert response.status_code == 200, response.text
    assert all(point["status"] == "eligible_for_new_frozen_run_preflight"
               for point in response.json()["candidates"])
    assert service.registry.audit() == before
    assert service.registry.get_run(run["id"])["result"]["summary"]["control_sequence"] == actual
    raw_artifact = next(a for a in run["artifacts"] if a["name"] == "raw_groups.npy")
    service.registry.blob_path(raw_artifact["sha256"]).write_bytes(b"damaged RAW in isolated test")
    with pytest.raises(IntegrityError):
        analyze(service, spec)
