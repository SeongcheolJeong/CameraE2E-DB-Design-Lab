"""Public v2 program/Worker/artifact boundaries over isolated test states.

These TestClient cases execute the actual Worker handler; detached process/UI
verification remains a separate acceptance step. All inputs are authored.
"""

import hashlib
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import load_array
from test_hdr_workflow import workflow as workflow

from camera_platform import capture_programs, imaging
from camera_platform.contracts import MeasurementCreate
from camera_platform.evidence import build_bundle
from camera_platform.hdr_runtime import HDRBracketRuntime
from camera_platform.registry import IntegrityError
from camera_platform.validation import compare
from camera_platform.worker import execute


def nonlinear_workflow(base, mode="independent_bracket_nonlinear"):
    service, sdk, assembly, scene, control, legacy_program, _, request = base
    draft = sdk.template("capture-program", assembly_id=assembly["id"], mode=mode)
    program = sdk.create_capture_program(draft)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"]))
    request = {
        **request, "plugin_id": capture_programs.runtime_for_payload(program["payload"]),
        "capture_program_id": program["id"], "test_id": test["id"],
    }
    assert service.registry.get("capture_programs", legacy_program["id"]) == legacy_program
    return service, sdk, assembly, scene, control, program, test, request


def worker_finish(service, request):
    queued = service.submit_run(request)
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    execute(service, claimed["id"])
    result = service.registry.get_run(claimed["id"])
    assert result["status"] == "succeeded", result.get("error")
    return result


@pytest.mark.parametrize("mode", [
    "independent_bracket_nonlinear", "reference_guarded_nonlinear", "shared_charge_nonlinear",
])
def test_v2_template_public_registration_worker_retained_calibration_and_bundle(workflow, mode):
    service, sdk, _, _, _, program, spec, request = nonlinear_workflow(workflow, mode)
    assert program["source_kind"] == "estimated"
    assert "authored" in program["source_uri"]
    assert program["payload"]["schema"].endswith("_v2")
    capture_programs.verify_program(service, program, request["plugin_id"])
    run = worker_finish(service, request)
    summary = run["result"]["summary"]
    paired = mode == "shared_charge_nonlinear"
    raw = load_array(service, run, "raw_groups" if paired else "raw_frames")
    assert raw.dtype.kind in "iu" and raw.shape[-2:] == (16, 16)
    assert raw.ndim == (4 if paired else 3)
    assert summary["capture_program_hash"] == program["content_hash"]
    assert summary["prediction"]["sample_geometry"] == spec["stimulus"]["sample_contract"]
    assert summary["prediction"]["method_and_version"] == spec["stimulus"]["observation_method"]
    assert summary["prediction"]["dark_correction"] == "not_applied_to_raw_code_metric"
    assert np.isfinite(summary["prediction"]["values"]).all()
    assert summary["physical_validation"] == "not_established"
    assert "calibration_dark_e.npy" in {a["name"] for a in run["artifacts"]}
    json.dumps(summary, allow_nan=False)
    assert service.submit_run(request)["id"] == run["id"]
    assert sdk.get("runs/" + run["id"])["result"]["summary"] == summary
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["capture_program_snapshot"] == program
        for artifact in program["artifacts"]:
            content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    assert service.registry.audit()["integrity"] == "verified"


def test_v2_observation_can_use_public_measurement_contract_without_physical_promotion(workflow):
    service, _, _, _, _, _, spec, request = nonlinear_workflow(workflow)
    run = worker_finish(service, request)
    prediction = run["result"]["summary"]["prediction"]
    fields = {k: deepcopy(v) for k, v in prediction.items() if k in MeasurementCreate.model_fields}
    fields.update(name="Authored repeated numeric reference", revision="1",
                  source_kind="simulated", source_uri="fixture://nonlinear-code-reference",
                  license="MIT; authored test values", physical_instance=None,
                  dataset_ids=["test-nonlinear-code-reference"])
    measurement = service.create_measurement(fields)
    result = compare(prediction, measurement, spec)
    assert result["status"] != "INVALID_COMPARISON", result["issues"]
    assert not result["physical_validation_pass"]
    changed = deepcopy(measurement)
    changed["sample_geometry"]["raw_format"]["response_type"] = "linear"
    assert compare(prediction, changed, spec)["status"] == "INVALID_COMPARISON"


def test_nonlinear_raw_is_not_relabelled_as_linear_replay(workflow):
    service, sdk, assembly, _, _, _, _, request = nonlinear_workflow(workflow)
    run = worker_finish(service, request)
    model = service.create_model(imaging.isp_template(service, assembly["id"]))
    before = service.registry.get_run(run["id"])
    response = sdk.http.post("/api/v1/replays", json={
        "name": "Unsupported direct nonlinear replay", "source_kind": "run",
        "source_id": run["id"], "isp_model_id": model["id"],
    })
    assert response.status_code == 422
    assert "direct linear ISP replay is unsupported" in response.text
    assert service.registry.get_run(run["id"]) == before


@pytest.mark.parametrize("artifact_name", [
    "capture-program-source.json", "capture-program-normalized.json",
])
@pytest.mark.parametrize("stage", ["submission", "queued_worker"])
def test_fresh_v2_program_blob_damage_is_rejected_before_capture(
    workflow, monkeypatch, artifact_name, stage,
):
    service, _, _, _, _, program, _, request = nonlinear_workflow(workflow)
    if stage == "queued_worker":
        queued = service.submit_run(request)
    artifact = next(a for a in program["artifacts"] if a["name"] == artifact_name)
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"owned test corruption")
    monkeypatch.setattr(HDRBracketRuntime, "execute",
                        lambda *a, **kw: pytest.fail("Corrupt nonlinear evidence reached capture"))
    if stage == "submission":
        with pytest.raises(IntegrityError, match="checksum"):
            service.submit_run(request)
    else:
        claimed = service.registry.claim()
        assert claimed["id"] == queued["id"]
        execute(service, claimed["id"])
        failed = service.registry.get_run(claimed["id"])
        assert failed["status"] == "failed" and "checksum" in failed["error"]
        assert failed["result"] is None


def test_v2_worker_binds_exact_frozen_model_and_gain_to_program(workflow, monkeypatch):
    service, _, _, _, _, _, _, request = nonlinear_workflow(workflow)
    queued = service.submit_run(request)
    forged = deepcopy(queued)
    forged["request"]["capture_program"]["electrical_model"]["digital_gain"] *= 2
    monkeypatch.setattr(HDRBracketRuntime, "execute",
                        lambda *a, **kw: pytest.fail("Substituted model reached capture"))
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(forged)
    assert service.registry.get_run(queued["id"])["status"] == "queued"


def test_changed_v2_curve_requires_new_program_and_fixed_test(workflow):
    service, sdk, assembly, _, _, program, _, request = nonlinear_workflow(workflow)
    draft = sdk.template("capture-program", assembly_id=assembly["id"],
                         mode="independent_bracket_nonlinear")
    draft["payload"]["electrical_model"]["charge_voltage_response"]["signal_voltage_v"][-1] *= 1.05
    draft.update(revision="changed-curve", parents=[program["id"]])
    changed = sdk.create_capture_program(draft)
    with pytest.raises(ValueError, match="exact sequence"):
        service.submit_run(request | {"capture_program_id": changed["id"]})


def test_registration_rejects_v2_curve_capacity_before_publication(workflow):
    service, sdk, assembly, *_ = workflow
    draft = sdk.template("capture-program", assembly_id=assembly["id"],
                         mode="independent_bracket_nonlinear")
    m = draft["payload"]["electrical_model"]
    m["full_well_e"] = 2 * m["charge_voltage_response"]["charge_e"][-1]
    before = len(service.registry.list("capture_programs"))
    response = sdk.http.post("/api/v1/capture_programs", json=draft)
    assert response.status_code == 422 and "capacity" in response.text
    assert len(service.registry.list("capture_programs")) == before


@pytest.mark.parametrize("artifact_name", [
    "capture-program-source.json", "capture-program-normalized.json",
])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_v2_program_get_freshly_checks_source_artifact(workflow, artifact_name, damage):
    service, sdk, _, _, _, program, _, _ = nonlinear_workflow(workflow)
    route = "/api/v1/capture_programs/" + program["id"]
    assert sdk.http.get(route).status_code == 200
    artifact = next(a for a in program["artifacts"] if a["name"] == artifact_name)
    path = service.registry.blob_path(artifact["sha256"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"owned nonlinear source corruption")
    response = sdk.http.get(route)
    assert response.status_code == (404 if damage == "missing" else 409)
    assert ("Artifact not found" if damage == "missing" else "checksum") in response.text
