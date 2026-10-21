"""Public program, Worker, replay and evidence boundaries for reference RAW fusion."""

import hashlib
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as workflow

from camera_platform import capture_programs, imaging, reference_hdr, sequences
from camera_platform.contracts import CaptureProgramCreate
from camera_platform.evidence import build_bundle
from camera_platform.registry import IntegrityError
from camera_platform.service import PreflightError


@pytest.fixture
def reference_workflow(workflow):
    service, sdk, assembly, _, _, _, _, _ = workflow
    scene = sdk.create_temporal_fixture(
        sdk.template("temporal-fixture", assembly_id=assembly["id"])
        | {"shape": [16, 16], "knot_count": 3, "pattern": "moving_spot"}
    )
    control = sdk.create_control_sequence(sdk.template(
        "control-sequence", assembly_id=assembly["id"], scene_sequence_id=scene["id"]
    ))
    draft = sdk.template("capture-program", assembly_id=assembly["id"], mode="reference_guarded")
    program = sdk.create_capture_program(draft)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    ))
    request = {
        "assembly_id": assembly["id"], "test_id": test["id"],
        "scene_sequence_id": scene["id"], "control_sequence_id": control["id"],
        "capture_program_id": program["id"], "plugin_id": "reference_hdr_v1",
        "parameters": {"seed": 273},
    }
    return service, sdk, assembly, scene, control, program, test, request


def test_reference_program_worker_raw_replay_and_portable_evidence(reference_workflow):
    service, sdk, assembly, scene, control, program, test, request = reference_workflow
    assert test["stimulus"]["schema"] == "reference_hdr_test_v1"
    queued = sdk.submit_reference_hdr(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"], seed=273,
    )
    run = finish(service, queued)
    assert run["status"] == "succeeded"
    assert service.submit_run(request)["id"] == run["id"]
    summary = run["result"]["summary"]
    assert summary["physical_validation"] == "not_established"
    assert summary["prediction"]["included_effects"] == test["included_effects"]
    assert summary["hdr_merge"]["reference_frame_index"] == 0
    raw = load_array(service, run, "raw_frames")
    assert raw.shape == (4, 16, 16)
    assert not np.array_equal(raw[0], raw[-1])
    json.dumps(summary, allow_nan=False)
    replay_source = imaging.replay_source(service, "run", run["id"])
    assert replay_source["frame_conditions"] == summary["frame_conditions"]
    assert replay_source["capture_program_id"] == program["id"]
    model = service.create_model(imaging.isp_template(service, assembly["id"]))
    replay = finish(service, imaging.submit_replay(service, {
        "name": "Reference bracket original RAW replay", "source_kind": "run",
        "source_id": run["id"], "isp_model_id": model["id"],
    }))
    np.testing.assert_array_equal(load_array(service, replay, "raw_frames"), raw)
    assert replay["result"]["summary"]["frame_conditions"] == summary["frame_conditions"]
    bundle = build_bundle(service, "runs", run["id"])
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["capture_program_snapshot"] == program
        for artifact in program["artifacts"]:
            blob = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(blob).hexdigest() == artifact["sha256"]
    assert service.registry.audit()["integrity"] == "verified"
    assert not service.registry.list("measurements")


def test_reference_index_and_program_require_exact_test_revision(reference_workflow):
    service, _, assembly, scene, control, program, _, request = reference_workflow
    data = {k: deepcopy(program[k]) for k in CaptureProgramCreate.model_fields}
    data.update(revision="reference-1", parents=[program["id"]])
    data["payload"]["hdr_merge"]["reference_frame_index"] = 1
    changed = capture_programs.create(service, data)
    with pytest.raises(ValueError, match="exact sequence"):
        service.submit_run(request | {"capture_program_id": changed["id"]})
    test = service.create_test(sequences.test_template(
        service, assembly["id"], scene["id"], control["id"], changed["id"]
    ))
    first = finish(service, service.submit_run(request))
    second = finish(service, service.submit_run(request | {
        "capture_program_id": changed["id"], "test_id": test["id"],
    }))
    assert first["id"] != second["id"]
    np.testing.assert_array_equal(load_array(service, first, "raw_frames"),
                                  load_array(service, second, "raw_frames"))
    assert second["result"]["summary"]["hdr_merge"]["reference_frame_index"] == 1


def test_reference_pwm_rejected_before_registration_and_index_before_queue(reference_workflow):
    service, sdk, assembly, scene, control, program, _, request = reference_workflow
    data = {k: deepcopy(program[k]) for k in CaptureProgramCreate.model_fields}
    data["payload"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": control["payload"]["clock"]["clock_id"], "period_ns": 1_000_000,
        "on_time_ns": 500_000, "phase_origin_ns": 0, "low": 0, "high": 1,
    }
    before = len(service.registry.list("capture_programs"))
    response = sdk.http.post("/api/v1/capture_programs", json=data)
    assert response.status_code == 422
    assert len(service.registry.list("capture_programs")) == before
    data["payload"]["waveform"] = None
    data["payload"]["hdr_merge"]["reference_frame_index"] = 15
    changed = capture_programs.create(service, data)
    test = service.create_test(sequences.test_template(
        service, assembly["id"], scene["id"], control["id"], changed["id"]
    ))
    count = len(service.registry.list_runs())
    with pytest.raises(PreflightError):
        service.submit_run(request | {"capture_program_id": changed["id"], "test_id": test["id"]})
    assert len(service.registry.list_runs()) == count


@pytest.mark.parametrize("target", ["program", "request"])
def test_reference_worker_rechecks_frozen_program_and_exact_request(reference_workflow, target):
    service, _, _, _, _, program, _, request = reference_workflow
    queued = service.submit_run(request)
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    if target == "program":
        artifact = program["artifacts"][0]
        service.registry.blob_path(artifact["sha256"]).write_bytes(b"changed")
        with pytest.raises(IntegrityError):
            service.execute_run(job)
    else:
        job["request"]["capture_program"]["hdr_merge"]["reference_frame_index"] = 1
        with pytest.raises(ValueError, match="fixed source evidence"):
            service.execute_run(job)


def test_global_translation_profile_runs_through_registered_test_worker_and_bundle(
    reference_workflow,
):
    service, sdk, assembly, scene, _, _, _, _ = reference_workflow
    control_draft = sdk.template(
        "control-sequence", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
    )
    control_draft["payload"]["mode"].update(shutter="global", line_time_ns=0)
    control_draft["revision"] = "global-translation-1"
    control = sdk.create_control_sequence(control_draft)
    program_draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode="reference_guarded_translation",
    )
    assert program_draft["payload"]["hdr_merge"] == reference_hdr.default_translation_profile()
    program_draft["revision"] = "raw-translation-1"
    program = sdk.create_capture_program(program_draft)
    test = service.create_test(sequences.test_template(
        service, assembly["id"], scene["id"], control["id"], program["id"],
    ))
    queued = sdk.submit_reference_hdr(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"], seed=284,
    )
    run = finish(service, queued)
    assert run["status"] == "succeeded"
    summary = run["result"]["summary"]
    assert summary["hdr_merge"]["schema"] == (
        "camera_reference_hdr_merge_result_global_translation_v1"
    )
    assert summary["capture_program"]["hdr_merge"]["schema"] == (
        reference_hdr.TRANSLATION_SCHEMA
    )
    assert summary["physical_validation"] == "not_established"
    assert summary["prediction"]["included_effects"] == test["included_effects"]
    raw = load_array(service, run, "raw_frames")
    aligned = load_array(service, run, "hdr_aligned_exposure_rate")
    assert raw.shape == aligned.shape
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["capture_program_snapshot"] == program
        assert any(name.endswith("hdr_aligned_exposure_rate.npy")
                   for name in archive.namelist())
