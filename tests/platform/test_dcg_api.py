"""DCG API authority, paired artifacts, cache integrity and Worker revalidation.

The optical fixture uses a real PSF Factory job; all sensor/readout inputs are
authored assumptions. TestClient execution is not detached-server evidence.
"""

import hashlib
import io
import json
import sqlite3
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as workflow

from camera_platform import capture_programs, imaging, sequences
from camera_platform.dcg_runtime import SharedChargeDCGRuntime
from camera_platform.registry import canonical
from camera_platform.worker import execute


@pytest.fixture
def dcg_workflow(workflow):
    service, sdk, assembly, scene, control, _, _, _ = workflow
    draft = sdk.template("capture-program", assembly_id=assembly["id"], mode="shared_charge")
    draft.update(name="API shared-charge fixture", revision="dcg-api-1")
    model = draft["payload"]["electrical_model"]
    model["shared_charge_capacity_e"] = 1000
    model["dark_current_e_per_s"] = 0
    model["paths"][0]["input_linear_ceiling_e"] = 200
    model["read_noise"]["covariance_e2"] = [[4, 1], [1, 16]]
    program = sdk.create_capture_program(draft)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"]))
    request = {"assembly_id": assembly["id"], "scene_sequence_id": scene["id"],
               "control_sequence_id": control["id"], "capture_program_id": program["id"],
               "test_id": test["id"], "plugin_id": "shared_charge_dcg_v1",
               "parameters": {"seed": 919}}
    return service, sdk, assembly, scene, control, program, test, request


def test_dcg_api_keeps_acquisition_readout_axes_cache_and_portable_bytes(dcg_workflow):
    service, sdk, assembly, scene, control, program, test, request = dcg_workflow
    queued = sdk.submit_dcg(assembly["id"], test["id"], scene["id"], control["id"],
                            program["id"], seed=919)
    run = finish(service, queued)
    summary = run["result"]["summary"]
    raw = load_array(service, run, "raw_groups")
    count = len(control["payload"]["frames"])
    assert raw.shape == (count, 2, 16, 16) and raw.dtype == np.int32
    assert summary["raw_axes"] == ["acquisition", "readout", "row", "column"]
    assert summary["acquisition_count"] == count and summary["readout_count"] == count * 2
    assert summary["readout_ids"] == ["HCG", "LCG"]
    assert "raw_frames.npy" not in {a["name"] for a in run["artifacts"]}
    assert "frames" not in summary and "frame_count" not in summary
    assert not np.array_equal(raw[:, 0], raw[:, 1])
    assert run["capture_program_snapshot"] == program
    assert summary["prediction"]["sample_geometry"] == test["stimulus"]["sample_contract"]
    assert summary["physical_validation"] == "not_established"
    assert summary["simulation_oracle"]["used_by_merge"] is False
    assert summary["prediction"]["axes"][1]["values"] == ["HCG", "LCG"]
    json.dumps(summary, allow_nan=False)
    original = sdk.get("runs/" + run["id"])
    before_count = len(service.registry.list_runs())
    cached = sdk.post("runs", request)
    assert cached["id"] == run["id"] and cached["cache_hit"] is True
    assert len(service.registry.list_runs()) == before_count
    assert sdk.get("runs/" + run["id"]) == original
    for acquisition in summary["acquisitions"]:
        assert [r["stream_id"] for r in acquisition["readouts"]] == ["HCG", "LCG"]
        for readout in acquisition["readouts"]:
            image = sdk.http.get(readout["preview_url"])
            assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
        assert sdk.http.get(acquisition["merged_preview_url"]).status_code == 200
    response = sdk.http.get("/api/v1/runs/" + run["id"] + "/bundle")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["capture_program_snapshot"] == program
        for artifact in run["artifacts"]:
            content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    assert service.registry.audit()["integrity"] == "verified"
    assert service.registry.list("measurements") == []


def test_dcg_saved_raw_cannot_be_flattened_by_legacy_replay(dcg_workflow):
    service, sdk, assembly, *_, request = dcg_workflow
    run = finish(service, service.submit_run(request))
    isp = service.create_model(imaging.isp_template(service, assembly["id"]))
    before = len(service.registry.list_runs())
    response = sdk.http.post("/api/v1/replays", json={
        "name": "Refused implicit readout flattening", "source_kind": "run",
        "source_id": run["id"], "isp_model_id": isp["id"],
    })
    assert response.status_code == 422 and "paired readouts" in response.text
    assert len(service.registry.list_runs()) == before
    assert service.registry.get_run(run["id"]) == run


def test_dcg_no_fusion_api_worker_keeps_only_paired_readout_previews(dcg_workflow):
    service, sdk, assembly, scene, control, original_program, _, _ = dcg_workflow
    draft = sdk.template("capture-program", assembly_id=assembly["id"], mode="shared_charge")
    draft.update(name="Readout diagnostics without fusion", revision="raw-only",
                 parents=[original_program["id"]], payload=deepcopy(original_program["payload"]))
    draft["payload"]["merge_profile"] = None
    program = sdk.create_capture_program(draft)
    test = sdk.post("tests", sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"]))
    queued = sdk.submit_dcg(assembly["id"], test["id"], scene["id"], control["id"],
                            program["id"], seed=929)
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    execute(service, claimed["id"])
    run = sdk.get("runs/" + queued["id"])
    assert run["status"] == "succeeded", run.get("error")
    summary = run["result"]["summary"]
    assert run["capture_program_snapshot"] == program
    assert summary["prediction"]["sample_geometry"] == test["stimulus"]["sample_contract"]
    assert summary["merge_applied"] is False
    assert summary["dcg_merge"] is None and summary["merged_preview"] is None
    assert summary["main_preview"] == "rgb_groups[0,0]"
    assert "frames" not in summary and "frame_count" not in summary
    count = len(control["payload"]["frames"])
    assert summary["acquisition_count"] == count and summary["readout_count"] == count * 2
    assert load_array(service, run, "raw_groups").shape == (count, 2, 16, 16)
    groups = load_array(service, run, "rgb_groups")
    assert groups.shape == (count, 2, 16, 16, 3)
    np.testing.assert_array_equal(load_array(service, run, "rgb"), groups[0, 0])
    names = {a["name"] for a in run["artifacts"]}
    assert not any(name.startswith("merged_") or name.endswith("-merged.png") for name in names)
    assert not names.intersection({"raw_frames.npy", "rgb_frames.npy", "weights.npy",
                                   "valid_mask.npy", "estimated_variance.npy"})
    preview_artifacts = [a for a in run["artifacts"] if a["name"].startswith("dcg-")]
    assert len(preview_artifacts) == count * 2
    for index, acquisition in enumerate(summary["acquisitions"]):
        assert "merged_preview_url" not in acquisition
        assert "merged_preview_artifact_id" not in acquisition
        assert [r["stream_id"] for r in acquisition["readouts"]] == ["HCG", "LCG"]
        for readout in acquisition["readouts"]:
            artifact = next(a for a in preview_artifacts
                            if a["acquisition_index"] == index
                            and a["stream_id"] == readout["stream_id"])
            assert artifact["acquisition_id"] == acquisition["acquisition_id"]
            assert artifact["id"] == readout["preview_artifact_id"]
            response = sdk.http.get(readout["preview_url"])
            assert response.status_code == 200 and response.content.startswith(b"\x89PNG")
            assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]


@pytest.mark.parametrize("plugin", ["hdr_bracket_v1", "temporal_scene_v1", "response_db_v1"])
def test_shared_program_cannot_be_reinterpreted_as_independent_exposures(dcg_workflow, plugin):
    service, sdk, *_, request = dcg_workflow
    before = len(service.registry.list_runs())
    response = sdk.http.post("/api/v1/runs", json=request | {"plugin_id": plugin})
    assert response.status_code == 422, response.text
    assert len(service.registry.list_runs()) == before


def test_changed_read_covariance_requires_new_program_test_and_cache_key(dcg_workflow):
    service, sdk, assembly, scene, control, program, _, request = dcg_workflow
    original = finish(service, service.submit_run(request))
    draft = capture_programs.template(service, assembly["id"], mode="shared_charge")
    draft.update(payload=deepcopy(program["payload"]), parents=[program["id"]], revision="negative")
    draft["payload"]["electrical_model"]["read_noise"]["covariance_e2"] = [[4, -1], [-1, 16]]
    changed = sdk.create_capture_program(draft)
    response = sdk.http.post("/api/v1/runs", json=request | {"capture_program_id": changed["id"]})
    assert response.status_code == 422 and "exact sequence" in response.text
    test = service.create_test(sequences.test_template(service, assembly["id"], scene["id"],
                                                       control["id"], changed["id"]))
    altered = finish(service, service.submit_run(request | {
        "capture_program_id": changed["id"], "test_id": test["id"]}))
    assert altered["id"] != original["id"] and altered["cache_key"] != original["cache_key"]
    np.testing.assert_array_equal(load_array(service, original, "mean_electrons"),
                                  load_array(service, altered, "mean_electrons"))
    assert not np.array_equal(load_array(service, original, "raw_groups"),
                              load_array(service, altered, "raw_groups"))


@pytest.mark.parametrize("name", ["capture-program-source.json", "capture-program-normalized.json"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
@pytest.mark.parametrize("stage", ["submit", "cache", "worker"])
def test_dcg_program_source_bytes_required_at_submit_cache_and_worker(
    dcg_workflow, name, damage, stage, monkeypatch
):
    service, sdk, _, _, _, program, _, request = dcg_workflow
    original = finish(service, service.submit_run(request)) if stage == "cache" else None
    queued = service.submit_run(request) if stage == "worker" else None
    blob = next(a for a in program["artifacts"] if a["name"] == name)
    path = service.registry.blob_path(blob["sha256"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"damaged owned DCG source fixture")
    monkeypatch.setattr(SharedChargeDCGRuntime, "execute",
                        lambda *args: pytest.fail("Damaged program reached acquisition"))
    before = len(service.registry.list_runs())
    if queued:
        claimed = service.registry.claim()
        assert claimed["id"] == queued["id"]
        execute(service, claimed["id"])
        failed = service.registry.get_run(claimed["id"])
        assert failed["status"] == "failed" and failed["result"] is None
        assert "Artifact" in failed["error"]
    else:
        response = sdk.http.post("/api/v1/runs", json=request)
        assert response.status_code == (404 if damage == "missing" else 409), response.text
    assert len(service.registry.list_runs()) == before
    if original:
        assert service.registry.get_run(original["id"]) == original


@pytest.mark.parametrize("change", ["gain", "read_covariance", "snapshot", "sample_contract",
                                    "source_descriptor", "geometry", "unknown_calibration"])
def test_worker_reconstructs_fixed_inputs_before_any_acquisition(dcg_workflow, change, monkeypatch):
    service, _, _, _, _, _, _, request = dcg_workflow
    job = deepcopy(service.submit_run(request))
    if change == "gain":
        job["request"]["capture_program"]["electrical_model"]["paths"][0]["analog_gain"] *= 2
    elif change == "read_covariance":
        job["request"]["capture_program"]["electrical_model"]["read_noise"]["covariance_e2"] = (
            [[0, 0], [0, 0]])
    elif change == "snapshot":
        model = job["capture_program_snapshot"]["payload"]["electrical_model"]
        model["paths"][0]["digital_gain"] = 2
    elif change == "sample_contract":
        job["test_snapshot"]["stimulus"]["sample_contract"]["readout_ids"].reverse()
    elif change == "source_descriptor":
        job["input_artifacts"][0]["bytes"] += 1
    elif change == "geometry":
        job["scene_sequence_snapshot"]["image_snapshots"][0]["sampling"]["crop_origin"][0] += 1
    else:
        job["request"]["calibration"] = {"dn_per_e": 0.1}
    monkeypatch.setattr(SharedChargeDCGRuntime, "execute",
                        lambda *args: pytest.fail("Changed frozen request reached acquisition"))
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(job)


def test_saved_calibration_edit_is_blocked_and_original_cache_remains_intact(dcg_workflow):
    service, sdk, *_, request = dcg_workflow
    run = finish(service, service.submit_run(request))
    bad = deepcopy(run["result"])
    bad["summary"]["calibrations"][0]["paths"][0]["dn_per_e"] *= 2
    with pytest.raises(sqlite3.IntegrityError, match="completed runs are immutable"):
        with service.registry.connection() as connection:
            connection.execute("UPDATE jobs SET result=? WHERE id=?", (canonical(bad), run["id"]))
    response = sdk.http.get("/api/v1/runs/" + run["id"])
    assert response.status_code == 200 and response.json() == run
    assert sdk.post("runs", request)["id"] == run["id"]


def test_program_tokens_unknown_fields_adc_and_unsupported_waveform_refused(dcg_workflow):
    service, sdk, assembly, *_ = dcg_workflow
    draft = capture_programs.template(service, assembly["id"], mode="shared_charge")
    before = len(service.registry.list("capture_programs"))
    invalid = deepcopy(draft)
    invalid["payload"]["waveform"] = None
    assert sdk.http.post("/api/v1/capture_programs", json=invalid).status_code == 422
    invalid = deepcopy(draft)
    invalid["payload"]["electrical_model"]["paths"][0]["adc"]["bit_depth"] -= 1
    assert sdk.http.post("/api/v1/capture_programs", json=invalid).status_code == 422
    assert sdk.http.post("/api/v1/capture_programs", json=draft,
                         headers={"X-Platform-Token": "wrong"}).status_code == 403
    assert len(service.registry.list("capture_programs")) == before
