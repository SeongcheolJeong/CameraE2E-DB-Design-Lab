"""Real characterization reused through the camera runtime and preserved in evidence."""

import copy
import hashlib
import json
import subprocess
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import factory
from camera_platform.api import create_app
from camera_platform.backup import backup_state, restore_state
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.contracts import TestCreate as SpecificationContract
from camera_platform.demo import seed
from camera_platform.evidence import build_bundle
from camera_platform.registry import ConflictError, IntegrityError
from camera_platform.service import PlatformService
from camera_platform.worker import execute as execute_worker_job


@pytest.fixture
def service(tmp_path):
    result = PlatformService(tmp_path / "factory")
    seed(result)
    return result


def finish(service, queued):
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    return service.registry.finish(job["id"], result=service.execute_run(job))


def payload(record, contract):
    return {k: copy.deepcopy(v) for k, v in record.items() if k in contract.model_fields}


@pytest.fixture
def characterized(service):
    pytest.importorskip("rayoptics")
    return finish(service, factory.submit(service, factory.template(service, "rayoptics_lens_v1")))


def publication(service, job):
    return {
        "job_id": job["id"],
        "name": "Reusable spectral lens",
        "revision": "factory-1",
        "conditions": service.registry.list("assemblies")[0]["conditions"],
    }


def test_actual_lens_characterization_reused_in_camera_and_offline_evidence(
    service, characterized, tmp_path
):
    job = characterized
    assert job["status"] == "succeeded"
    assert job["result"]["summary"]["status"] == "PROPOSED"
    assert service.list_simulation_runs() == []
    assert len(service.list_factory_jobs()) == 1
    model = factory.publish(service, publication(service, job))
    assert model["factory_job_id"] == job["id"]
    assert model["uncertainty"] == {"kind": "unknown"}
    assert model["payload"] == job["result"]["model_candidate"]
    assert model["factory_application_conditions"] == publication(service, job)["conditions"]
    original = service.registry.list("assemblies")[0]
    assembly = payload(original, AssemblyCreate)
    assembly.update(name="Characterized lens camera", revision="factory-camera-1")
    assembly["model_ids"] = [
        model["id"] if service.registry.get("models", i)["kind"] == "lens" else i
        for i in assembly["model_ids"]
    ]
    assembly = service.create_assembly(assembly)
    run = finish(
        service,
        service.submit_run(
            {
                "assembly_id": assembly["id"],
                "test_id": service.registry.list("tests")[0]["id"],
                "parameters": {"output_size": [16, 16], "frames": 2, "seed": 42},
            }
        ),
    )
    assert run["status"] == "succeeded"
    assert {"raw_frames.npy", "rgb.npy"} <= {a["name"] for a in run["artifacts"]}
    for name in ("raw_frames.npy", "rgb.npy"):
        artifact = next(a for a in run["artifacts"] if a["name"] == name)
        assert np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False).max() > 0
    release = service.create_release(
        {"name": "Factory lineage", "run_ids": [run["id"]], "validation_ids": []}
    )
    assert release["status"] == "blocked"
    bundle = build_bundle(service, "releases", release["id"])
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["factory_jobs"][0]["id"] == job["id"]
        for artifact in job["artifacts"]:
            content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    backup = tmp_path / "factory-backup.zip"
    backup_state(service.registry.directory, backup)
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    reopened = PlatformService(restored)
    assert reopened.registry.get_run(job["id"]) == job
    assert reopened.registry.audit()["integrity"] == "verified"
    assert not reopened.registry.list("measurements")
    different_light = payload(assembly, AssemblyCreate)
    different_light.update(name="Changed fixed light", revision="wrong-light")
    different_light["conditions"]["light_id"]["value"] = "another-light"
    different_light = service.create_assembly(different_light)
    checked = service.check(different_light["id"])
    assert any(i["code"] == "factory_application_condition_mismatch" for i in checked["issues"])


def test_characterization_cache_and_forced_replay_keep_same_frozen_physics(service):
    pytest.importorskip("rayoptics")
    request = factory.template(service, "rayoptics_lens_v1")
    first = factory.submit(service, request)
    repeated = factory.submit(service, copy.deepcopy(request))
    replay = factory.submit(service, {**request, "force": True})
    assert repeated["id"] == first["id"] and repeated["cache_hit"]
    assert replay["id"] != first["id"] and replay["cache_key"] == first["cache_key"]
    request["request"]["surfaces"][0]["thickness_mm"] *= 2
    assert first["request"] != request["request"]
    assert service.registry.cancel(first["id"])["status"] == "cancelled"
    with pytest.raises(ConflictError, match="no reusable"):
        factory.publish(service, publication(service, first))


def test_factory_candidate_cannot_be_silently_rewritten_or_laundered(service, characterized):
    model = factory.publish(service, publication(service, characterized))
    changed = payload(model, ModelCreate)
    changed.update(name="Changed candidate", revision="forged")
    changed["payload"]["relative_illumination"][0][-1] *= 0.9
    with pytest.raises(ValueError, match="differs from the preserved"):
        service.create_model(changed)
    unlinked = payload(model, ModelCreate)
    unlinked.update(name="Unlinked claim", revision="unlinked", factory_job_id=None)
    with pytest.raises(ValueError, match="Factory provenance requires"):
        service.create_model(unlinked)
    wider = payload(model, ModelCreate)
    wider.update(name="Wider domain", revision="wide")
    wider["valid_domain"]["temperature_c"]["max"] += 5
    with pytest.raises(ValueError, match="fixed application conditions"):
        service.create_model(wider)
    with pytest.raises(ValueError, match="camera release"):
        service.create_release(
            {
                "name": "Optical result as camera proof",
                "run_ids": [characterized["id"]],
                "validation_ids": [],
            }
        )
    artifact = characterized["artifacts"][0]
    service.registry.blob_path(artifact["sha256"]).write_bytes(b"changed source input")
    with pytest.raises(IntegrityError, match="checksum"):
        factory.publish(service, {**publication(service, characterized), "revision": "tampered"})


@pytest.mark.parametrize(
    "change", ["metric_id", "unit", "reference_plane", "included_effects", "quick"]
)
def test_runtime_refuses_unimplemented_measurement_contract_before_queue(service, change):
    original = service.registry.list("tests")[0]
    test = payload(original, SpecificationContract)
    test.update(name="Unsupported output contract", revision=change)
    changes = {
        "metric_id": "lens.mtf",
        "unit": "%",
        "reference_plane": "sensor_surface",
        "included_effects": ["psf"],
    }
    if change != "quick":
        test[change] = changes[change]
    if change == "unit":
        test["absolute_tolerance"]["unit"] = "%"
    test = service.create_test(test)
    with pytest.raises(ValueError, match="computes raw.channel_ratio"):
        service.submit_run(
            {
                "assembly_id": service.registry.list("assemblies")[0]["id"],
                "test_id": test["id"],
                "plugin_id": "quick_estimate" if change == "quick" else "response_db",
            }
        )
    assert not service.registry.list_runs()


def test_factory_template_routes_and_unavailable_solver_fail_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("CAMERA_PLATFORM_MEEP_PYTHON", raising=False)
    app = create_app(tmp_path / "http", start_worker=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = {"x-platform-token": client.get("/api/v1/bootstrap").json()["token"]}
        for plugin in factory.PLUGINS:
            response = client.get("/api/v1/templates/factory", params={"plugin_id": plugin})
            assert response.status_code == 200, response.text
            assert response.json()["plugin_id"] == plugin
        request = client.get("/api/v1/templates/factory?plugin_id=meep_planar_stack_v1").json()
        response = client.post("/api/v1/factory/jobs", json=request, headers=headers)
        assert response.status_code == 422 and "CAMERA_PLATFORM_MEEP_PYTHON" in response.text
        assert client.get("/api/v1/factory/jobs").json() == []


def test_failed_external_solver_preserves_input_and_diagnostics(service, monkeypatch):
    monkeypatch.setattr(factory, "backend_identity", lambda plugin: {"executable": "test-only"})
    request = factory.template(service, "meep_planar_stack_v1")
    queued = factory.submit(service, request)

    def failed_subprocess(command, *, stdout, **kwargs):
        stdout.write(b"explicit simulated backend failure evidence\n")
        stdout.flush()
        raise subprocess.CalledProcessError(2, command)

    monkeypatch.setattr(factory.subprocess, "run", failed_subprocess)
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    execute_worker_job(service, claimed["id"])
    job = factory.get_job(service, claimed["id"])
    assert job["status"] == "failed"
    assert job["result"]["model_candidate"] is None
    assert {a["name"] for a in job["artifacts"]} == {"factory-input.json", "solver.log"}
    log = next(a for a in job["artifacts"] if a["name"] == "solver.log")
    assert service.registry.blob_path(log["sha256"]).read_bytes().startswith(b"explicit simulated")
    assert service.registry.audit()["integrity"] == "verified"
