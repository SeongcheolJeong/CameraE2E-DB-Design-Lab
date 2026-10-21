"""Electrical/HDR runs preserve program authority, RAW, timing and portable evidence."""

import copy
import hashlib
import json
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import capture_programs, factory, imaging, sequences
from camera_platform.api import create_app
from camera_platform.contracts import AssemblyCreate
from camera_platform.evidence import build_bundle
from camera_platform.sdk import PlatformClient
from camera_platform.service import PreflightError


def finish(service, queued):
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    return service.registry.finish(job["id"], result=service.execute_run(job))


def load_array(service, run, name):
    artifact = next(a for a in run["artifacts"] if a["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


@pytest.fixture
def workflow(tmp_path):
    app = create_app(tmp_path / "hdr", start_worker=False)
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()["token"]
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = client
        service = app.state.service
        original = service.registry.list("assemblies")[0]
        base = next(service.registry.get("models", i) for i in original["model_ids"]
                    if service.registry.get("models", i)["kind"] == "lens")
        job = finish(service, factory.submit(service, factory.template(service, "prysm_psf_v1")))
        lens = factory.publish(service, {
            "job_id": job["id"], "base_lens_model_id": base["id"],
            "name": "HDR optical response", "revision": "1",
            "conditions": original["conditions"], "condition_scope": "optical_response",
        })
        fields = {k: copy.deepcopy(v) for k, v in original.items()
                  if k in AssemblyCreate.model_fields}
        fields.update(name="HDR camera", revision="hdr-1")
        fields["model_ids"] = [lens["id"] if i == base["id"] else i for i in fields["model_ids"]]
        assembly = service.create_assembly(fields)
        scene = sequences.create_fixture(
            service, sequences.fixture_template(service, assembly["id"])
            | {"shape": [16, 16], "knot_count": 2, "pattern": "static"})
        # Reuse exactly one registered spectral image: no inferred scene alignment.
        scene_data = {k: copy.deepcopy(v) for k, v in scene.items()
                      if k in sequences.SceneSequenceCreate.model_fields}
        scene_data.update(name="Static HDR target", revision="static-hdr")
        scene_data["payload"]["knots"][1]["spectral_image_id"] = (
            scene_data["payload"]["knots"][0]["spectral_image_id"])
        scene = sequences.create_sequence(service, "scene_sequences", scene_data)
        controls = sequences.control_template(service, assembly["id"], scene["id"])
        for index, frame in enumerate(controls["payload"]["frames"]):
            frame["applied"]["exposure_ns"] = (4 if index % 2 == 0 else 8) * 1_000_000
        control = sdk.create_control_sequence(controls)
        draft = sdk.template("capture-program", assembly_id=assembly["id"])
        program = sdk.create_capture_program(draft)
        spec = sdk.template("temporal-test", assembly_id=assembly["id"],
                            scene_sequence_id=scene["id"], control_sequence_id=control["id"],
                            capture_program_id=program["id"])
        test = service.create_test(spec)
        request = {
            "assembly_id": assembly["id"], "test_id": test["id"],
            "scene_sequence_id": scene["id"], "control_sequence_id": control["id"],
            "capture_program_id": program["id"], "plugin_id": "hdr_bracket_v1",
            "parameters": {"seed": 927},
        }
        yield service, sdk, assembly, scene, control, program, test, request


def test_hdr_sdk_execution_frozen_program_raw_replay_and_bundle(workflow):
    service, sdk, assembly, scene, control, program, test, request = workflow
    queued = sdk.submit_hdr(assembly["id"], test["id"], scene["id"], control["id"],
                            program["id"], seed=927)
    run = finish(service, queued)
    assert service.submit_run(request)["id"] == run["id"]
    assert run["capture_program_snapshot"] == program
    summary = run["result"]["summary"]
    assert summary["capture_program_hash"] == program["content_hash"]
    assert summary["frame_conditions"] == test["frame_conditions"]
    assert summary["prediction"]["included_effects"] == test["included_effects"]
    raw = load_array(service, run, "raw_frames")
    assert raw.shape == (4, 16, 16) and raw.dtype.kind in "iu"
    assert not np.array_equal(raw[0], raw[1])
    json.dumps(summary, allow_nan=False)
    assert summary["physical_validation"] == "not_established"
    assert sdk.get("workbench")["capture_programs"][0]["id"] == program["id"]
    assert sdk.get("runs/" + run["id"])["capture_program_id"] == program["id"]
    for frame in summary["frames"]:
        png = service.registry.blob_path(frame["preview_artifact_id"]).read_bytes()
        assert png[:4] == b"\x89PNG"
    isp = service.create_model(imaging.isp_template(service, assembly["id"]))
    replay = finish(service, imaging.submit_replay(service, {
        "name": "HDR bracket RAW per-frame reprocessing", "source_kind": "run",
        "source_id": run["id"], "isp_model_id": isp["id"],
    }))
    np.testing.assert_array_equal(load_array(service, replay, "raw_frames"), raw)
    assert replay["result"]["summary"]["frame_conditions"] == summary["frame_conditions"]
    assert replay["result"]["summary"]["capture_program_id"] == program["id"]
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        for artifact in program["artifacts"]:
            content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["capture_program_snapshot"] == program
    assert service.registry.audit()["integrity"] == "verified"
    assert service.registry.list("measurements") == []


def test_program_revision_requires_new_fixed_test_and_cache_identity(workflow):
    service, _, assembly, scene, control, program, _, request = workflow
    data = capture_programs.template(service, assembly["id"])
    data.update(revision="digital2", parents=[program["id"]])
    data["payload"]["electrical_model"]["digital_gain"] = 2
    changed = capture_programs.create(service, data)
    with pytest.raises(ValueError, match="exact sequence"):
        service.submit_run(request | {"capture_program_id": changed["id"]})
    test = service.create_test(sequences.test_template(service, assembly["id"], scene["id"],
        control["id"], changed["id"]) | {"revision": "digital2"})
    old = finish(service, service.submit_run(request))
    new = finish(service, service.submit_run(request | {
        "capture_program_id": changed["id"], "test_id": test["id"],
    }))
    assert old["id"] != new["id"]
    np.testing.assert_array_equal(load_array(service, old, "mean_electrons"),
                                  load_array(service, new, "mean_electrons"))
    assert not np.array_equal(load_array(service, old, "raw_frames"),
                              load_array(service, new, "raw_frames"))


@pytest.mark.parametrize("field,value", [("bit_depth", 10), ("black_level_dn", 65)])
def test_program_cannot_silently_change_native_raw_format(workflow, field, value):
    service, sdk, assembly, *_ = workflow
    data = capture_programs.template(service, assembly["id"])
    data["payload"]["electrical_model"]["adc"][field] = value
    before = len(service.registry.list("capture_programs"))
    response = sdk.http.post("/api/v1/capture_programs", json=data)
    assert response.status_code == 422 and "ADC" in response.text
    assert len(service.registry.list("capture_programs")) == before


def test_missing_or_wrong_plugin_program_is_refused(workflow):
    service, _, _, _, _, _, _, request = workflow
    with pytest.raises(ValueError, match="fixed capture program"):
        service.submit_run(request | {"capture_program_id": None})
    for plugin in ("temporal_scene_v1", "closed_loop_scene_v1", "response_db_v1"):
        with pytest.raises(ValueError, match="capture program requires"):
            service.submit_run(request | {"plugin_id": plugin})


def test_pwm_partial_cycles_refuse_hdr_but_run_explicit_lfm_diagnostics(workflow):
    service, _, assembly, scene, control, _, _, request = workflow
    data = capture_programs.template(service, assembly["id"])
    data.update(name="LED phase investigation", revision="pwm")
    data["payload"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": control["payload"]["clock"]["clock_id"],
        "period_ns": 3_000_000, "on_time_ns": 600_000, "phase_origin_ns": 0,
        "low": 0.0, "high": 1.0,
    }
    hdr = capture_programs.create(service, data)
    test = service.create_test(sequences.test_template(service, assembly["id"], scene["id"],
        control["id"], hdr["id"]) | {"revision": "pwm-hdr"})
    with pytest.raises(PreflightError):
        service.submit_run(request | {"capture_program_id": hdr["id"], "test_id": test["id"]})
    data["payload"]["hdr_merge"] = None
    data["revision"] = "lfm-only"
    lfm = capture_programs.create(service, data)
    test = service.create_test(sequences.test_template(service, assembly["id"], scene["id"],
        control["id"], lfm["id"]) | {"revision": "lfm"})
    assert "raw_hdr_bracket_merge" not in test["included_effects"]
    run = finish(service, service.submit_run(request | {
        "capture_program_id": lfm["id"], "test_id": test["id"],
    }))
    assert run["status"] == "succeeded"
    raw = load_array(service, run, "raw_frames")
    assert not np.array_equal(raw[0], raw[2])


def test_new_program_routes_protect_token_and_exact_contract(workflow):
    service, sdk, assembly, *_ = workflow
    data = capture_programs.template(service, assembly["id"])
    for mutation in ({"unknown": True}, {"payload": {**data["payload"], "hidden_oracle": True}}):
        response = sdk.http.post("/api/v1/capture_programs", json=data | mutation)
        assert response.status_code == 422
    assert sdk.http.post("/api/v1/capture_programs", json=data,
        headers={"X-Platform-Token": "wrong"}).status_code == 403
    artifact = service.registry.list("capture_programs")[0]["artifacts"][0]
    assert sdk.http.get(artifact["url"]).status_code == 200
