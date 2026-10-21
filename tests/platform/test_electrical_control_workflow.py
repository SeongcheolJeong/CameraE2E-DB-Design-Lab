"""Fixed policy/source evidence and actual electrical-feedback acquisition wiring."""

import hashlib
import json
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as hdr_workflow
from test_retained_charge_workflow import workflow as retained_workflow

from camera_platform import imaging, nonlinear_examples, sequences
from camera_platform.evidence import build_bundle
from camera_platform.validation import compare


@pytest.fixture
def workflow(retained_workflow):
    service, sdk, assembly, scene, control, _, _, request = retained_workflow
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode="electrical_control",
        control_sequence_id=control["id"],
    )
    draft["payload"]["electrical_control_policy"]["target_black_relative_dn"] = {
        "minimum": 0.5, "maximum": 1.0,
    }
    program = sdk.create_capture_program(draft)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    ))
    request = {
        **request, "plugin_id": "electrical_control_v1", "capture_program_id": program["id"],
        "test_id": test["id"],
    }
    return service, sdk, assembly, scene, control, program, test, request


def test_sdk_acquires_realized_exposure_and_exports_all_original_bytes(workflow):
    service, sdk, assembly, scene, control, program, test, request = workflow
    page = sdk.http.get("/")
    assert 'src="/electrical-control.js"' in page.text
    script = sdk.http.get("/electrical-control.js")
    assert script.status_code == 200
    assert script.content == Path("src/camera_platform/static/electrical-control.js").read_bytes()
    queued = sdk.submit_electrical_control(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=591, noise_enabled=False,
    )
    assert test["frame_conditions"] is None
    assert "retained_charge_events" not in queued["request"]
    run = finish(service, queued)
    summary = run["result"]["summary"]
    assert sdk.get("capture_programs/" + program["id"]) == program
    assert service.submit_run(request)["id"] == run["id"]
    realized = summary["control_sequence"]
    exposures = [f["applied"]["exposure_ns"] for f in realized["frames"]]
    assert len(set(exposures)) > 1
    assert [f["applied"]["exposure_ns"] for f in summary["frames"]] == exposures
    assert summary["frame_conditions"] == sequences.frame_conditions(realized)
    assert summary["prediction"]["frame_conditions"] == summary["frame_conditions"]
    actual = summary["prediction"]["sample_geometry"]
    assert actual["control_sequence"] == realized
    assert actual["events"] == summary["retained_charge"]["events"]
    assert actual["plan"] == test["stimulus"]["sample_contract"]
    raw = load_array(service, run, "raw_groups")
    assert raw.shape == (4, 1, 16, 16)
    assert not np.array_equal(raw[0], raw[-1])
    assert all(f.get("preview_artifact_id") for f in summary["frames"])
    assert summary["prediction"]["acquisition_mode"] == "closed_loop"
    assert not service.registry.list("measurements")
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["request"]["capture_program"] == program["payload"]
        for artifact in run["artifacts"]:
            content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]


def test_fresh_zero_state_retry_is_exact_and_policy_changes_identity(workflow):
    service, _, _, _, _, program, _, request = workflow
    first = finish(service, service.submit_run(request))
    second = finish(service, service.submit_run({**request, "force": True}))
    assert first["id"] != second["id"]
    np.testing.assert_array_equal(
        load_array(service, first, "raw_groups"), load_array(service, second, "raw_groups")
    )
    assert first["result"]["summary"]["control_sequence"] == second["result"]["summary"]["control_sequence"]
    assert first["request"]["capture_program"]["electrical_control_policy"] == program["payload"]["electrical_control_policy"]


@pytest.mark.parametrize("field", ["policy", "noise", "initial_exposure", "policy_bool"])
def test_worker_rebuild_refuses_frozen_request_tampering(workflow, field):
    service, _, _, _, _, _, _, request = workflow
    job = deepcopy(service.submit_run(request))
    if field == "policy":
        job["request"]["capture_program"]["electrical_control_policy"]["latency"]["actuator_ns"] += 1
    elif field == "policy_bool":
        job["request"]["capture_program"]["electrical_control_policy"]["enabled"] = 1
    elif field == "noise":
        job["request"]["noise_enabled"] = True
    else:
        job["request"]["control_sequence"]["frames"][0]["applied"]["exposure_ns"] += 1
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(job)


def test_generic_validation_and_current_exposure_inverse_cannot_pass(workflow):
    service, sdk, _, _, _, _, test, request = workflow
    run = finish(service, service.submit_run(request))
    prediction = run["result"]["summary"]["prediction"]
    fake_measurement = deepcopy(prediction)
    report = compare(prediction, fake_measurement, test)
    assert not report["comparison_valid"] and not report["physical_validation_pass"]
    assert "closed_loop_comparison_operator_required" in {x["code"] for x in report["issues"]}
    forged_prediction = {k: v for k, v in prediction.items() if k != "acquisition_mode"}
    assert not compare(forged_prediction, fake_measurement, test)["comparison_valid"]
    with pytest.raises(ValueError, match="Retained|delivered"):
        imaging.replay_source(service, "run", run["id"])
    from camera_platform.isp_charge_source import resolve

    with pytest.raises(ValueError, match="Retained|delivered"):
        resolve(service, "run", run["id"], {
            "schema": "camera_nonlinear_raw_selection_v1", "mode": "sequential_frames",
        })
    assert not any(x["source_id"] == run["id"] for x in sdk.get("replay-sources"))


def test_nonlinear_program_keeps_actual_raw_format_and_distinct_policy(workflow):
    service, sdk, assembly, scene, control, parent, _, request = workflow
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode="electrical_control",
        control_sequence_id=control["id"],
    )
    draft.update(revision="nonlinear", parents=[parent["id"]])
    draft["payload"]["electrical_model"] = nonlinear_examples.single_model(
        parent["sensor_model_snapshot"]["payload"]["readout"]
    )
    program = sdk.create_capture_program(draft)
    spec = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    ))
    run = finish(service, service.submit_run({
        **request, "test_id": spec["id"], "capture_program_id": program["id"],
    }))
    summary = run["result"]["summary"]
    assert summary["raw_formats"][0]["response_type"] == "nonlinear_charge_voltage"
    assert summary["electrical_control"]["source_truth_used"] is False
    assert summary["current_exposure_charge_estimation"] is False
    assert summary["prediction"]["valid_domain"]["temperature_c"]["min"] == 25


def test_program_source_corruption_blocks_cache_and_worker(workflow):
    service, _, _, _, _, program, _, request = workflow
    run = finish(service, service.submit_run(request))
    artifact = program["artifacts"][0]
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"corrupt authored fixture")
    from camera_platform.registry import IntegrityError

    with pytest.raises(IntegrityError):
        service.submit_run(request)
    with pytest.raises(IntegrityError):
        service.execute_run(run)
