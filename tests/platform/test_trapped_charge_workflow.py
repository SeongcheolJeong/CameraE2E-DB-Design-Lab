"""Trap programs preserve original RAW, time contracts and code-only ISP replay."""

import hashlib
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow
from test_retained_isp_workflow import payload as replay_payload

from camera_platform import dcg_electrical, nonlinear_examples, retained_isp_source
from camera_platform.evidence import build_bundle
from camera_platform.service import PlatformService


def trapped_program(workflow, family="single"):
    service, sdk, assembly, scene, control, parent, _, request = workflow
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode="trapped_charge",
        control_sequence_id=control["id"],
    )
    draft.update(revision=family, parents=[parent["id"]])
    if family != "single":
        factory = {
            "nonlinear": nonlinear_examples.single_model,
            "paired": dcg_electrical.default_model,
            "paired_nonlinear": nonlinear_examples.shared_model,
        }[family]
        draft["payload"]["electrical_model"] = factory(
            parent["sensor_model_snapshot"]["payload"]["readout"]
        )
    program = sdk.create_capture_program(draft)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    ))
    return program, {**request, "capture_program_id": program["id"], "test_id": test["id"]}


@pytest.mark.parametrize("family", ["single", "nonlinear", "paired", "paired_nonlinear"])
def test_trap_program_delivered_raw_and_isp_all_readout_families(workflow, family):
    service, sdk, *_ = workflow
    program, request = trapped_program(workflow, family)
    queued = service.submit_run(request)
    run = finish(service, queued)
    summary = run["result"]["summary"]
    assert summary["schema_version"] == "camera_retained_charge_result_v2"
    assert summary["capture_program"] == program["payload"]
    assert sdk.get("capture_programs/" + program["id"]) == program
    assert service.submit_run(request)["id"] == run["id"]
    assert summary["physical_validation"] == "not_established"
    assert not summary["hdr_merge_applied"] and not summary["current_exposure_charge_estimation"]
    trapped = load_array(service, run, "retained_trapped_e")
    assert np.any(trapped > 0)
    for ledger in summary["retained_charge"]["frame_ledger"]:
        assert ledger["conservation_max_error_e"] <= ledger["conservation_tolerance_e"]
    source_raw = load_array(service, run, "raw_groups")
    readout = "LCG" if family.startswith("paired") else "single"
    selected = 1 if readout == "LCG" else 0
    response = sdk.http.post("/api/v1/replays", json=replay_payload(
        service, sdk, run, readout, enabled=True
    ))
    assert response.status_code == 202, response.text[:1000]
    replay = finish(service, response.json())
    np.testing.assert_array_equal(
        load_array(service, replay, "raw_frames"), source_raw[:, selected]
    )
    assert replay["result"]["summary"]["source_truth_arrays_used"] is False
    assert service.registry.get_run(run["id"]) == run


def test_trap_export_cold_readback_and_program_tamper(workflow):
    service, sdk, *_ = workflow
    program, request = trapped_program(workflow)
    run = finish(service, service.submit_run(request))
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        for artifact in run["artifacts"]:
            path = f"artifacts/{artifact['sha256']}/{artifact['name']}"
            assert hashlib.sha256(archive.read(path)).hexdigest() == artifact["sha256"]
    cold = PlatformService(service.registry.directory)
    selector = {"schema": "camera_retained_raw_selection_v1", "readout_id": "single"}
    resolved = retained_isp_source.resolve(cold, run["id"], selector)
    assert resolved["source_id"] == run["id"]
    altered = deepcopy(program)
    altered["payload"]["retained_charge_model"]["release_time_s"][0] *= 2
    from camera_platform.capture_programs import verify_program
    with pytest.raises(ValueError, match="frozen registry"):
        verify_program(service, altered, "retained_charge_v1")


def test_legacy_electrical_control_cannot_silently_accept_traps(workflow):
    from camera_platform.electrical_control_runtime import (
        ElectricalControlRuntime,
        validate_capture_program,
    )

    _, sdk, assembly, _, control, *_ = workflow
    trapped = sdk.template("capture-program", assembly_id=assembly["id"],
                           mode="trapped_charge", control_sequence_id=control["id"])
    ae = sdk.template("capture-program", assembly_id=assembly["id"],
                      mode="electrical_control", control_sequence_id=control["id"])
    ae["payload"]["retained_charge_model"] = trapped["payload"]["retained_charge_model"]
    with pytest.raises(ValueError):
        validate_capture_program(ae["payload"])
    capabilities = ElectricalControlRuntime().describe_capabilities()
    assert "charge_trap_capture_release" not in capabilities["unsupported"]
    assert "causal_trap_bearing_exposure_feedback" in capabilities["supports"]
