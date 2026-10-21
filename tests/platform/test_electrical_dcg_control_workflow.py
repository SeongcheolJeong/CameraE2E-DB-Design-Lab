"""Authoring and frozen Test binding for fixed-path shared-charge electrical AE."""

import hashlib
import json
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as _hdr_workflow
from test_retained_charge_workflow import workflow as _retained_workflow

from camera_platform import capture_programs, electrical_control, sequences
from camera_platform.electrical_control_runtime import observation_path
from camera_platform.evidence import build_bundle

hdr_workflow = _hdr_workflow
retained_workflow = _retained_workflow


@pytest.mark.parametrize("readout_id", ["HCG", "LCG"])
@pytest.mark.parametrize("nonlinear", [False, True])
def test_paired_template_api_sdk_actual_raw_and_plan_binding(
    retained_workflow, readout_id, nonlinear,
):
    service, sdk, assembly, scene, control, _, _, _ = retained_workflow
    mode = f"electrical_control_{readout_id.lower()}" + ("_nonlinear" if nonlinear else "")
    before = [p["id"] for p in service.registry.list("capture_programs")]
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode=mode,
        control_sequence_id=control["id"],
    )
    assert [p["id"] for p in service.registry.list("capture_programs")] == before
    assert draft["source_kind"] == "estimated" and draft["physical_instance"] is None
    payload = draft["payload"]
    assert payload["schema"] == "camera_electrical_control_capture_program_v2"
    assert payload["observation_readout_id"] == readout_id
    assert payload["electrical_model"]["schema"] == (
        "camera_shared_charge_readout_v2" if nonlinear else "camera_shared_charge_readout_v1"
    )
    assert "hdr_merge" not in payload and "merge_profile" not in payload
    # Unequal authored black levels are valid and must remain path-specific.
    for path, black in zip(payload["electrical_model"]["paths"], [17.25, 91.5], strict=True):
        path["adc"]["black_level_dn"] = black
    payload["electrical_control_policy"]["target_black_relative_dn"] = {
        "minimum": 0.5, "maximum": 1.0,
    }
    program = sdk.create_capture_program(draft)
    assert sdk.get("capture_programs/" + program["id"]) == program
    index, selected_id, raw_format = observation_path(program["payload"])
    assert (index, selected_id) == ((0 if readout_id == "HCG" else 1), readout_id)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    ))
    plan = test["stimulus"]["sample_contract"]
    assert plan["schema"] == "camera_electrical_control_plan_v1"
    assert plan["observation_readout_id"] == readout_id
    assert plan["readout_index"] == index
    assert len(plan["raw_formats"]) == 2 and plan["raw_formats"][index] == raw_format
    assert test["frame_conditions"] is None
    queued = sdk.submit_electrical_control(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=591, noise_enabled=False,
    )
    run = finish(service, queued)
    summary = run["result"]["summary"]
    trace = summary["electrical_control"]
    assert trace["readout_id"] == readout_id and trace["raw_format"] == raw_format
    assert trace["observation_readout_index"] == index
    raw = load_array(service, run, "raw_groups")
    assert raw.shape == (4, 2, 16, 16) and raw.dtype.kind in "iu"
    assert not np.array_equal(raw[:, 0], raw[:, 1])
    for frame, observed in zip(raw, trace["frames"], strict=True):
        assert observed["statistics"] == electrical_control.raw_statistics(
            frame[index], raw_format, program["payload"]["electrical_control_policy"]
        )
    assert summary["prediction"]["sample_geometry"]["plan"] == plan
    assert summary["frame_conditions"] == sequences.frame_conditions(summary["control_sequence"])
    assert summary["current_exposure_charge_estimation"] is False
    assert summary["hdr_merge_applied"] is False
    assert sdk.http.get("/electrical-control.js").content == Path(
        "src/camera_platform/static/electrical-control.js"
    ).read_bytes()
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["capture_program_snapshot"]["payload"][
            "observation_readout_id"
        ] == readout_id
        for artifact in run["artifacts"]:
            data = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(data).hexdigest() == artifact["sha256"]


def test_selection_is_a_new_immutable_program_and_old_test_cannot_substitute(retained_workflow):
    service, sdk, assembly, scene, control, _, _, request = retained_workflow
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode="electrical_control_hcg",
        control_sequence_id=control["id"],
    )
    first = sdk.create_capture_program(draft)
    spec = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=first["id"],
    ))
    second_draft = deepcopy(draft)
    second_draft.update(revision="lcg-new", parents=[first["id"]])
    second_draft["payload"]["observation_readout_id"] = "LCG"
    second = sdk.create_capture_program(second_draft)
    assert first["id"] != second["id"] and first["content_hash"] != second["content_hash"]
    assert sdk.get("capture_programs/" + first["id"])["payload"]["observation_readout_id"] == "HCG"
    with pytest.raises(ValueError, match="program|Program|fixed|Test|test"):
        service._prepare_run({**request, "plugin_id": "electrical_control_v1",
                             "capture_program_id": second["id"], "test_id": spec["id"]})
    queued = service.submit_run({**request, "plugin_id": "electrical_control_v1",
                                 "capture_program_id": first["id"], "test_id": spec["id"]})
    forged = deepcopy(queued)
    forged["request"]["capture_program"]["observation_readout_id"] = "LCG"
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(forged)
    # The new schema must use the verified GET path, including its authored blob.
    source = first["artifacts"][0]
    service.registry.blob_path(source["sha256"]).write_bytes(b"corrupt source")
    response = sdk.http.get("/api/v1/capture_programs/" + first["id"])
    assert response.status_code == 409


def test_authoring_refuses_missing_path_and_both_adc_contract_mismatches(retained_workflow):
    service, sdk, assembly, _, control, _, _, _ = retained_workflow
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode="electrical_control_lcg",
        control_sequence_id=control["id"],
    )
    for invalid in [None, "single", "auto", True]:
        value = deepcopy(draft)
        value["payload"]["observation_readout_id"] = invalid
        with pytest.raises(ValueError, match="fixed|HCG|LCG"):
            capture_programs.create(service, value)
    for index in range(2):
        value = deepcopy(draft)
        value["payload"]["electrical_model"]["paths"][index]["adc"]["bit_depth"] -= 1
        with pytest.raises(ValueError, match="ADC|format|Target|target"):
            capture_programs.create(service, value)
    with pytest.raises(ValueError, match="control sequence"):
        capture_programs.template(service, assembly["id"], "electrical_control_hcg")


def test_original_single_template_and_plan_have_no_paired_contract_fields(retained_workflow):
    service, sdk, assembly, scene, control, _, _, _ = retained_workflow
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode="electrical_control",
        control_sequence_id=control["id"],
    )
    assert draft["payload"]["schema"] == "camera_electrical_control_capture_program_v1"
    assert "observation_readout_id" not in draft["payload"]
    assert observation_path(draft["payload"])[1] == "single"
    program = sdk.create_capture_program(draft)
    spec = sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    )
    plan = spec["stimulus"]["sample_contract"]
    assert "observation_readout_id" not in plan and "readout_index" not in plan
    assert len(plan["raw_formats"]) == 1
