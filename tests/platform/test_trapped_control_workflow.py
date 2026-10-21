"""Trap AE authoring, immutable acquisition and matched response studies."""

import hashlib
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_electrical_response_study import finish_all
from test_hdr_workflow import finish, load_array
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow

from camera_platform import capture_programs, electrical_control, electrical_response_study
from camera_platform.evidence import build_bundle
from camera_platform.service import PlatformService

MODES = [
    ("electrical_control_trapped", "single", "camera_electrical_readout_v1"),
    ("electrical_control_trapped_nonlinear", "single", "camera_electrical_readout_v2"),
    ("electrical_control_trapped_hcg", "HCG", "camera_shared_charge_readout_v1"),
    ("electrical_control_trapped_lcg", "LCG", "camera_shared_charge_readout_v1"),
    ("electrical_control_trapped_hcg_nonlinear", "HCG", "camera_shared_charge_readout_v2"),
    ("electrical_control_trapped_lcg_nonlinear", "LCG", "camera_shared_charge_readout_v2"),
]


def test_pwm_period_template_registers_and_runs_with_explicit_unknown_feasibility(workflow):
    service, sdk, assembly, _, control, *_ = workflow
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"],
        mode="electrical_control_trapped_pwm_period", control_sequence_id=control["id"],
    )
    payload = draft["payload"]
    assert payload["electrical_control_policy"]["schema"] == (
        electrical_control.PERIOD_POLICY_SCHEMA
    )
    period = payload["waveform"]["period_ns"]
    assert payload["electrical_control_policy"]["period_lattice"]["period_ns"] == period
    assert control["payload"]["frames"][0]["applied"]["exposure_ns"] == period
    program, _, request = create_program(workflow, "electrical_control_trapped_pwm_period")
    run = finish(service, service.submit_run(request))
    summary = run["result"]["summary"]
    assert summary["electrical_control"]["policy"]["schema"] == (
        electrical_control.PERIOD_POLICY_SCHEMA
    )
    assert all(
        frame["applied"]["exposure_ns"] % period == 0
        for frame in summary["electrical_control"]["frames"]
    )
    assert summary["electrical_control"]["target_outcome"]["period_lattice"][
        "target_feasibility"
    ] == "not_established_by_finite_raw_sequence"


def create_program(workflow, mode="electrical_control_trapped"):
    service, sdk, assembly, scene, control, _, _, request = workflow
    draft = sdk.template("capture-program", assembly_id=assembly["id"], mode=mode,
                         control_sequence_id=control["id"])
    draft["payload"]["electrical_control_policy"]["target_black_relative_dn"] = {
        "minimum": 0.5, "maximum": 1.0,
    }
    program = sdk.create_capture_program(draft)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    ))
    return program, test, {**request, "plugin_id": "electrical_control_v1",
                           "capture_program_id": program["id"], "test_id": test["id"]}


@pytest.mark.parametrize("mode,readout_id,model_schema", MODES)
def test_all_trap_ae_templates_actual_raw_realized_events_and_plan(workflow, mode,
                                                                readout_id, model_schema):
    service, sdk, *_ = workflow
    program, test, request = create_program(workflow, mode)
    payload = program["payload"]
    assert payload["schema"] == "camera_electrical_control_capture_program_v3"
    assert payload["observation_readout_id"] == readout_id
    assert payload["electrical_model"]["schema"] == model_schema
    assert payload["retained_charge_model"]["schema"] == "camera_trapped_photodiode_v1"
    assert sdk.get("capture_programs/" + program["id"]) == program
    plan = test["stimulus"]["sample_contract"]
    assert plan["observation_readout_id"] == readout_id
    assert test["frame_conditions"] is None
    run = finish(service, service.submit_run(request))
    summary = run["result"]["summary"]
    assert summary["schema_version"] == "camera_electrical_control_result_v2"
    assert summary["retained_charge"]["events"]["schema"] == "camera_retained_row_events_v2"
    exposures = [f["applied"]["exposure_ns"] for f in summary["control_sequence"]["frames"]]
    assert len(set(exposures)) > 1
    assert summary["prediction"]["sample_geometry"]["plan"] == plan
    assert (
        summary["prediction"]["sample_geometry"]["events"] == summary["retained_charge"]["events"]
    )
    raw = load_array(service, run, "raw_groups")
    index = 1 if readout_id == "LCG" else 0
    assert raw.shape == (4, 2 if readout_id != "single" else 1, 16, 16)
    assert raw.dtype.kind in "iu"
    for values, frame in zip(raw, summary["electrical_control"]["frames"], strict=True):
        assert frame["statistics"] == electrical_control.raw_statistics(
            values[index], summary["raw_formats"][index], payload["electrical_control_policy"]
        )
    assert np.any(load_array(service, run, "retained_trapped_e") > 0)
    assert load_array(service, run, "trap_segment_count").shape == (4,)
    for frame in summary["retained_charge"]["frame_ledger"]:
        assert frame["conservation_max_error_e"] <= frame["conservation_tolerance_e"]
    assert not summary["hdr_merge_applied"] and not summary["current_exposure_charge_estimation"]
    assert service.submit_run(request)["id"] == run["id"]


def test_frozen_trap_policy_and_source_tampering_refused(workflow):
    service, sdk, *_ = workflow
    program, _, request = create_program(workflow)
    queued = service.submit_run(request)
    for field in ["capture_rate_s", "release_time_s"]:
        forged = deepcopy(queued)
        forged["request"]["capture_program"]["retained_charge_model"][field][0] *= 2
        with pytest.raises(ValueError, match="fixed source evidence"):
            service.execute_run(forged)
    artifact = program["artifacts"][0]
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"corrupt trap program")
    assert sdk.http.get("/api/v1/capture_programs/" + program["id"]).status_code == 409


def test_selector_and_adc_contracts_refuse_incompatible_authoring(workflow):
    service, sdk, assembly, _, control, *_ = workflow
    single = sdk.template("capture-program", assembly_id=assembly["id"],
                          mode="electrical_control_trapped", control_sequence_id=control["id"])
    for readout in [None, "HCG", "auto", True]:
        invalid = deepcopy(single)
        invalid["payload"]["observation_readout_id"] = readout
        with pytest.raises(ValueError):
            capture_programs.create(service, invalid)
    paired = sdk.template("capture-program", assembly_id=assembly["id"],
                          mode="electrical_control_trapped_hcg", control_sequence_id=control["id"])
    for index in [0, 1]:
        invalid = deepcopy(paired)
        invalid["payload"]["electrical_model"]["paths"][index]["adc"]["bit_depth"] -= 1
        with pytest.raises(ValueError, match="ADC|format|target|Target"):
            capture_programs.create(service, invalid)


def test_original_artifacts_export_and_cold_readback(workflow):
    service, sdk, *_ = workflow
    program, _, request = create_program(workflow)
    run = finish(service, service.submit_run(request))
    before = deepcopy(run)
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        for artifact in run["artifacts"]:
            content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    cold = PlatformService(service.registry.directory)
    assert cold.registry.get_run(run["id"]) == before
    assert cold.registry.get("capture_programs", program["id"]) == program
    assert sdk.get("runs/" + run["id"]) == before


def test_matched_fixed_and_trap_ae_study_preserves_shared_start_and_history(workflow):
    service, _, assembly, scene, control, *_ = workflow
    program, _, _ = create_program(workflow)
    spec = electrical_response_study.template(
        service, assembly["id"], scene["id"], control["id"], program["id"]
    )
    proposal = electrical_response_study.plan(service, spec)
    parent = electrical_response_study.submit(service, spec, proposal["plan_hash"])
    jobs = finish_all(service)
    assert jobs[-1]["id"] == parent["id"] and len(jobs) == 3
    result = electrical_response_study.get(service, parent["id"])["result"]["summary"]
    assert result["status"] == "complete" and result["complete_pair_count"] == 1
    cell = result["cells"][0]
    baseline, candidate = [service.registry.get_run(cell[role]["run_id"])
                           for role in ("baseline", "candidate")]
    first, second = [load_array(service, run, "raw_groups") for run in (baseline, candidate)]
    np.testing.assert_array_equal(first[0], second[0])
    assert not np.array_equal(first[1:], second[1:])
    assert not cell["baseline"]["control_trace"]["commands"]
    assert cell["candidate"]["control_trace"]["commands"]
    assert all(run["result"]["summary"]["retained_charge"]["events"]["schema"]
               == "camera_retained_row_events_v2" for run in (baseline, candidate))
