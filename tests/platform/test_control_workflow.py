"""Registry-to-RAW feedback execution, complete domain admission and preserved evidence."""

import copy
import json
import time
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import control_service, factory, imaging, sequences
from camera_platform.api import create_app
from camera_platform.backup import backup_state, restore_state
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import seed
from camera_platform.evidence import build_bundle
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService, PreflightError
from camera_platform.validation import compare


def finish(service, queued):
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    return service.registry.finish(job["id"], result=service.execute_run(job))


def array(service, record, name):
    artifact = next(a for a in record["artifacts"] if a["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


@pytest.fixture
def workflow(tmp_path):
    service = PlatformService(tmp_path / "control-platform")
    seed(service)
    original = service.registry.list("assemblies")[0]
    # New explicitly authored behavioral-model fixtures, not widened production records.
    models = {}
    for identity in original["model_ids"]:
        model = service.registry.get("models", identity)
        fields = {k: copy.deepcopy(v) for k, v in model.items() if k in ModelCreate.model_fields}
        fields.update(
            name="Authored feedback " + model["kind"],
            revision="control-1",
            parents=[model["id"]],
            source_uri="fixture://declared-readout-gain-family",
            method_and_version="authored_behavioral_gain_domain:1",
        )
        fields["valid_domain"]["gain"] = {"min": 1.0, "max": 8.0, "unit": "1"}
        models[model["kind"]] = service.create_model(fields)
    psf_job = finish(service, factory.submit(service, factory.template(service, "prysm_psf_v1")))
    models["lens"] = factory.publish(
        service,
        {
            "job_id": psf_job["id"],
            "base_lens_model_id": models["lens"]["id"],
            "name": "Feedback lens with PSF",
            "revision": "psf-1",
            "conditions": original["conditions"],
            "condition_scope": "optical_response",
        },
    )
    fields = {k: copy.deepcopy(v) for k, v in original.items() if k in AssemblyCreate.model_fields}
    fields.update(
        name="Feedback test camera",
        revision="control-1",
        model_ids=[models[k]["id"] for k in ["lens", "sensor", "isp"]],
    )
    fields["conditions"]["exposure_s"] = {"value": 0.001, "unit": "s"}
    assembly = service.create_assembly(fields)
    scene = sequences.create_fixture(
        service,
        {
            **sequences.fixture_template(service, assembly["id"]),
            "shape": [16, 16],
            "knot_count": 3,
            "pattern": "static",
            "duration_ns": 320_000_000,
            "irradiance_scale": 2e-5,
        },
    )
    policy = control_service.create_policy(
        service, control_service.policy_template(service, scene["id"])
    )
    draft = control_service.program_template(service, assembly["id"], scene["id"], policy["id"], 32)
    return service, assembly, scene, policy, draft


def register(workflow):
    service, assembly, _, _, draft = workflow
    program = control_service.create_program(service, draft)
    test = service.create_test(control_service.test_template(service, program["id"]))
    request = {
        "assembly_id": assembly["id"],
        "test_id": test["id"],
        "plugin_id": "closed_loop_scene_v1",
        "control_program_id": program["id"],
        "parameters": {"seed": 71},
    }
    return program, test, request


def test_full_program_run_replay_and_bundle_preserve_generated_controls(workflow):
    service, assembly, scene, policy, _ = workflow
    program, test, request = register(workflow)
    result = finish(service, service.submit_run(request))
    summary = result["result"]["summary"]
    assert test["frame_conditions"] is None
    assert summary["frame_count"] == 32 and len(summary["frame_conditions"]) == 32
    assert summary["control_program_sha256"] == program["expanded_program_sha256"]
    assert result["request"]["scene_sequence"] == scene["payload"]
    assert result["control_policy_snapshot"] == policy
    assert result["compatibility"]["domain_proof"] == "axis_aligned_interval_containment"
    assert result["compatibility"]["control_bounds"]["gain"]["max"] == 8
    assert array(service, result, "raw_frames").shape == (32, 16, 16)
    assert not np.array_equal(
        array(service, result, "raw_frames")[0], array(service, result, "raw_frames")[-1]
    )
    assert summary["frame_conditions"][0]["exposure_ns"] == 1_000_000
    assert any(f["exposure_ns"] != 1_000_000 for f in summary["frame_conditions"][1:])
    assert all(f["preview_url"] for f in summary["frames"])
    assert summary["prediction"]["acquisition_mode"] == "closed_loop"
    assert summary["prediction"]["uncertainty"]["kind"] == "unknown"
    assert service.submit_run(request)["id"] == result["id"]

    isp_fields = imaging.isp_template(service, assembly["id"])
    isp_fields["payload"]["white_balance"]["gains_rgb"] = [1.5, 1.0, 0.5]
    isp = service.create_model(isp_fields)
    replay = finish(
        service,
        imaging.submit_replay(
            service,
            {
                "source_kind": "run",
                "source_id": result["id"],
                "isp_model_id": isp["id"],
                "name": "Reprocess closed-loop RAW",
            },
        ),
    )
    replay_summary = replay["result"]["summary"]
    assert replay_summary["control_trace"] == summary["control_trace"]
    assert replay_summary["frame_conditions"] == summary["frame_conditions"]
    assert replay_summary["control_program_id"] == program["id"]
    assert "timing" not in replay_summary
    assert replay["source"]["raw_sha256"] == next(
        a["sha256"] for a in result["artifacts"] if a["name"] == "raw_frames.npy"
    )
    assert not np.array_equal(
        array(service, replay, "rgb_frames"), array(service, result, "rgb_frames")
    )
    bundle = build_bundle(service, "runs", result["id"])
    with zipfile.ZipFile(bundle) as z:
        manifest = json.loads(z.read("manifest.json"))["run"]
        assert manifest["control_program_snapshot"] == program
        for source in [program, policy]:
            for artifact in source["artifacts"]:
                assert (
                    z.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
                    == service.registry.blob_path(artifact["sha256"]).read_bytes()
                )


def test_check_is_read_only_and_import_keeps_original_bytes(workflow):
    service, _, _, _, draft = workflow
    before = service.registry.audit()["head_hash"]
    checked = control_service.check_program(service, draft)
    assert checked["compatibility"]["status"] == "PASS"
    assert service.registry.audit()["head_hash"] == before
    original = " \r\n" + json.dumps(draft, ensure_ascii=False, indent=3) + "\r\n"
    program = control_service.import_record(service, "control_programs", original)
    artifact = next(a for a in program["artifacts"] if a["name"] == "control-program-source.json")
    assert service.registry.blob_path(artifact["sha256"]).read_bytes() == original.encode()
    assert checked["expanded_program_sha256"] == program["expanded_program_sha256"]


@pytest.mark.parametrize(
    "role,axis,bounds",
    [
        ("sensor", "gain", {"min": 1.0, "max": 2.0, "unit": "1"}),
        ("lens", "gain", {"min": 1.0, "max": 2.0, "unit": "1"}),
        ("isp", "gain", {"min": 1.0, "max": 2.0, "unit": "1"}),
        ("sensor", "exposure_s", {"min": 0.0005, "max": 0.002, "unit": "s"}),
        ("isp", "gain", None),
        ("sensor", "gain", {"min": 1.0, "max": 8.0, "unit": "dB"}),
    ],
)
def test_initial_setting_pass_is_not_full_domain_coverage(workflow, role, axis, bounds):
    service, assembly, _, policy, draft = workflow
    models = [service.registry.get("models", i) for i in assembly["model_ids"]]
    affected = next(m for m in models if m["kind"] == role)
    if bounds is None:
        affected["valid_domain"].pop(axis)
    else:
        affected["valid_domain"][axis] = bounds
    p = control_service.expand_program(draft["payload"], policy["payload"])
    with pytest.raises(PreflightError) as error:
        control_service.check_control_domain(assembly, models, p)
    assert error.value.report["status"] == "FAIL"
    assert error.value.report["control_bounds"]["gain"]["max"] == 8


def test_fixed_factory_conditions_cannot_be_widened_by_endpoint_bounds(workflow):
    service, assembly, _, policy, draft = workflow
    models = [service.registry.get("models", i) for i in assembly["model_ids"]]
    lens = next(m for m in models if m["kind"] == "lens")
    lens["factory_condition_scope"] = "all_declared"
    p = control_service.expand_program(draft["payload"], policy["payload"])
    with pytest.raises(PreflightError) as error:
        control_service.check_control_domain(assembly, models, p)
    assert any(
        i["code"] == "factory_application_condition_mismatch" for i in error.value.report["issues"]
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["payload"]["initial"].update(exposure_ns=1_000_001),
        lambda p: p["payload"]["frames"][-1].update(frame_start_ns=319_000_000),
        lambda p: p["payload"]["initial"].update(isp_model_id="not-the-pinned-isp"),
        lambda p: p["payload"]["clock"].update(clock_id="wrong-clock"),
    ],
)
def test_bad_program_creates_no_records_or_jobs(workflow, mutate):
    service, _, _, _, draft = workflow
    before = service.registry.audit()["head_hash"]
    mutate(draft)
    with pytest.raises(ValueError):
        control_service.create_program(service, draft)
    assert service.registry.audit()["head_hash"] == before


def test_seed_identity_and_test_program_mismatch_refuse_before_queue(workflow):
    service, _, _, _, _ = workflow
    _, test, request = register(workflow)
    before = service.registry.audit()["head_hash"]
    for override in [
        {"parameters": {"seed": True}},
        {"parameters": {"gain": 2}},
        {"control_sequence_id": "recorded"},
        {"scene_sequence_id": "another"},
        {"plugin_id": "temporal_scene_v1"},
    ]:
        with pytest.raises(ValueError):
            service.submit_run({**request, **override})
    assert service.registry.audit()["head_hash"] == before
    fields = {
        k: copy.deepcopy(v)
        for k, v in test.items()
        if k not in {"id", "created_at", "content_hash"}
    }
    from camera_platform.contracts import TestCreate

    fields = {k: v for k, v in fields.items() if k in TestCreate.model_fields}
    fields["frame_conditions"] = [{"exposure_ns": 1_000_000}]
    changed = service.create_test(fields)
    with pytest.raises(ValueError, match="Test must pin"):
        service.submit_run({**request, "test_id": changed["id"]})


def test_closed_loop_raw_comparison_cannot_claim_pass_even_with_identical_values(workflow):
    service, _, _, _, _ = workflow
    _, test, request = register(workflow)
    output = finish(service, service.submit_run(request))
    prediction = output["result"]["prediction"]
    measurement = {**copy.deepcopy(prediction), "source_kind": "measured"}
    prediction["uncertainty"] = measurement["uncertainty"] = {
        "kind": "absolute_bound",
        "value": 0,
        "unit": "DN",
    }
    compared = compare(prediction, measurement, {**test, "validation_kind": "physical_validation"})
    assert compared["status"] == "INCONCLUSIVE" and compared["residuals"] is None
    assert not compared["physical_validation_pass"] and not compared["comparison_valid"]
    assert compared["issues"][0]["code"] == "closed_loop_comparison_operator_required"


def test_actual_worker_via_http_and_source_artifact_routes(workflow):
    service, _, scene, policy, draft = workflow
    app = create_app(service.registry.directory, start_worker=True, seed_demo=False)
    with TestClient(app) as client:
        boot = client.get("/api/v1/bootstrap").json()
        client.headers["X-Platform-Token"] = boot["token"]
        assert policy["id"] in {r["id"] for r in client.get("/api/v1/control_policies").json()}
        assert (
            client.get(
                "/api/v1/templates/control-policy", params={"scene_sequence_id": scene["id"]}
            ).status_code
            == 200
        )
        checked = client.post("/api/v1/control_programs/check", json=draft)
        assert checked.status_code == 200, checked.text
        response = client.post("/api/v1/control_programs", json=draft)
        assert response.status_code == 201, response.text
        program = response.json()
        for artifact in program["artifacts"]:
            assert (
                client.get(artifact["url"]).content
                == service.registry.blob_path(artifact["sha256"]).read_bytes()
            )
        test_data = client.get(
            "/api/v1/templates/closed-loop-test", params={"control_program_id": program["id"]}
        ).json()
        test = client.post("/api/v1/tests", json=test_data).json()
        queued = client.post(
            "/api/v1/runs",
            json={
                "assembly_id": draft["assembly_id"],
                "test_id": test["id"],
                "plugin_id": "closed_loop_scene_v1",
                "control_program_id": program["id"],
            },
        )
        assert queued.status_code == 202, queued.text
        identity = queued.json()["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = client.get("/api/v1/runs/" + identity).json()
            if result["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert result["status"] == "succeeded", result.get("error")
        assert result["result"]["frame_count"] == 32
        assert result["result"]["control_trace"]["commands"]
        assert client.get("/api/v1/health").json()["worker"] == "running"
        indexed = client.get("/api/v1/workbench").json()
        projected = next(r for r in indexed["runs"] if r["id"] == identity)
        assert projected["control_program_id"] == program["id"]
        projected_program = next(r for r in indexed["control_programs"] if r["id"] == program["id"])
        assert "assembly_snapshot" not in projected_program
        assert projected_program["display_summary"]["frame_count"] == 32
        original = client.get("/api/v1" + projected_program["_projection"]["detail_path"]).json()
        assert original["record"] == program
        assert original["record_sha256"] == projected_program["_projection"]["record_sha256"]
        detail = client.get("/api/v1/workbench/runs/" + identity).json()
        assert detail["record"] == result
        blob = next(a for a in result["artifacts"] if a["name"] == "raw_frames.npy")
        assert (
            client.get(blob["url"]).content
            == service.registry.blob_path(blob["sha256"]).read_bytes()
        )


def test_backup_restore_preserves_control_sources_and_acquisition(workflow, tmp_path):
    service, _, _, policy, _ = workflow
    program, _, request = register(workflow)
    result = finish(service, service.submit_run(request))
    archive, target = tmp_path / "controls.zip", tmp_path / "restored-controls"
    original_audit = service.registry.audit()
    backup_state(service.registry.directory, archive)
    report = restore_state(archive, target)
    assert report["integrity"] == "verified" and report["workers_started"] is False
    restored = PlatformService(target)
    assert restored.registry.audit() == original_audit
    assert restored.registry.get("control_policies", policy["id"]) == policy
    assert restored.registry.get("control_programs", program["id"]) == program
    assert restored.registry.get_run(result["id"]) == result
    assert np.array_equal(
        array(restored, result, "raw_frames"), array(service, result, "raw_frames")
    )
    for record in (program, policy):
        for artifact in record["artifacts"]:
            assert (
                restored.registry.blob_path(artifact["sha256"]).read_bytes()
                == service.registry.blob_path(artifact["sha256"]).read_bytes()
            )


def test_sdk_register_check_submit_through_public_api(workflow):
    service, assembly, scene, _, draft = workflow
    app = create_app(service.registry.directory, start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()["token"]
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = client
        policy = sdk.create_control_policy(control_service.policy_template(service, scene["id"]))
        draft = {**draft, "control_policy_id": policy["id"], "revision": "sdk-1"}
        check = sdk.check_control_program(draft)
        program = sdk.create_control_program(draft)
        assert program["expanded_program_sha256"] == check["expanded_program_sha256"]
        test = sdk.post("tests", control_service.test_template(service, program["id"]))
        job = sdk.submit_closed_loop(assembly["id"], test["id"], program["id"], seed=27)
        assert job["control_program_snapshot"] == program and job["request"]["seed"] == 27
        assert job["status"] == "queued"
