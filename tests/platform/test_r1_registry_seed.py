"""Installed R1 temporal/HDR/DCG pilot uses only its packaged synthetic sources."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from camera_platform import imaging, isp
from camera_platform.api import create_app
from camera_platform.demo import RAW_FORMAT
from camera_platform.project_mode import configure
from camera_platform.r1_design_assets import load_sources
from camera_platform.r1_registry_seed import ensure
from camera_platform.registry import canonical
from camera_platform.response_chain import evaluate_response
from camera_platform.service import PlatformService


WORKSPACE = Path(__file__).resolve().parents[2]


def _array(service, run, name):
    artifact = next(item for item in run["artifacts"] if item["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


def _finish(service, request):
    queued = service.submit_run(request)
    claimed = service.registry.claim()
    assert claimed["id"] == queued["id"]
    run = service.registry.finish(claimed["id"], result=service.execute_run(claimed))
    assert run["status"] == "succeeded", run.get("error")
    assert service.registry.get_run(run["id"]) == run
    return run


def _project(tmp_path):
    state = tmp_path / "r1-project"
    configure(state, project_id="r1-registry-seed-test")
    return PlatformService(state, WORKSPACE)


def test_r1_registry_seed_is_explicit_idempotent_and_bound_to_packaged_sources(tmp_path):
    unmarked = PlatformService(tmp_path / "unmarked", WORKSPACE)
    assert ensure(unmarked) is None
    assert unmarked.registry.list("assemblies") == []

    service = _project(tmp_path)
    first = ensure(service)
    counts = {kind: len(service.registry.list(kind)) for kind in (
        "assets", "models", "assemblies", "spectral_images", "scene_sequences",
        "control_sequences", "capture_programs", "tests",
    )}
    assert counts == {"assets": 5, "models": 3, "assemblies": 1,
                      "spectral_images": 2, "scene_sequences": 2,
                      "control_sequences": 2, "capture_programs": 2, "tests": 3}
    assert ensure(service) == first
    assert {kind: len(service.registry.list(kind)) for kind in counts} == counts
    assert service.check(first["assembly_id"])["status"] == "PASS"
    assert first["physical_validation"] == "not_established"

    source = load_sources(WORKSPACE, ["r1-stop-clear-v1"], "r1-flat-v1")[
        "r1-stop-clear-v1"
    ]
    original = source.rate_request()
    identity = source.identity()
    assert first["bundle_manifest_sha256"] == identity["bundle_manifest_sha256"]
    assert first["source_identity"]["components"] == identity["components"]
    assert set(identity["components"]) == {"optics", "pixels", "electricals"}
    assembly = service.registry.get("assemblies", first["assembly_id"])
    models = {service.registry.get("models", key)["kind"]: service.registry.get("models", key)
              for key in assembly["model_ids"]}
    lens, sensor = models["lens"], models["sensor"]
    assert "radiometry" not in lens["payload"]
    assert lens["payload"]["psf"] == original["psf"]
    assert {key: value for key, value in lens["payload"].items() if key != "psf"} == {
        key: value for key, value in original["lens"].items() if key != "radiometry"
    }
    assert sensor["payload"]["qe"] == original["qe"]
    assert sensor["payload"]["sensor_angular_response"] == original["sensor_angular_response"]
    assert sensor["payload"]["readout"] == original["readout"]
    assert sensor["raw_format"] == assembly["raw_format"] == RAW_FORMAT
    assert lens["properties"]["on_axis_throughput_location"] == (
        "frozen_ideal_spectral_image_once"
    )
    assert lens["properties"]["image_circle_diameter"]["value"] == (
        2 * original["lens"]["radiometry"]["image_height_mm"][-1]
    )
    assert lens["properties"]["incident_angle_range"]["max"] == (
        original["lens"]["theta_deg"][-1]
    )
    assert all(model["properties"]["r1_source_identity"]["components"] == identity["components"]
               for model in models.values())
    assert all(model["source_uri"].startswith("r1-bundle://") for model in models.values())
    assert all("qa" not in model["source_uri"].lower() for model in models.values())

    flat = service.registry.get("spectral_images", first["spectral_image_ids"]["r1-flat-v1"])
    image = next(item for item in flat["artifacts"] if item["name"] == "spectral-image.npy")
    actual = np.load(io.BytesIO(service.registry.blob_path(image["sha256"]).read_bytes()),
                     allow_pickle=False)
    radiance = np.asarray(source.scene["spectral_radiance_w_m2_sr_nm"], dtype=float)
    on_axis = np.asarray(original["lens"]["radiometry"]["irradiance_per_radiance_sr"],
                         dtype=float)[:, 0]
    np.testing.assert_array_equal(actual, radiance * on_axis)
    np.testing.assert_array_equal(actual, original["scene"]["spectral_irradiance"])
    assert flat["included_effects"] == ["on_axis_lens_throughput"]
    assert flat["sampling"] == original["scene"]["sampling"]

    tests = {key: service.registry.get("tests", value) for key, value in first["test_ids"].items()}
    for key, test in tests.items():
        assert test["validation_kind"] == "implementation_verification"
        selected = "changing" if key == "temporal" else "static"
        assert test["stimulus"]["scene_sequence_id"] == first["scene_sequence_ids"][selected]
        assert test["stimulus"]["control_sequence_id"] == first["control_sequence_ids"][selected]
        if key != "temporal":
            assert test["stimulus"]["capture_program_id"] == first["capture_program_ids"][key]
    for key, identity in first["capture_program_ids"].items():
        program = service.registry.get("capture_programs", identity)
        assert program["payload"].get("waveform") is None
        assert program["assembly_id"] == first["assembly_id"]
        if key == "hdr":
            assert program["payload"]["hdr_merge"] is not None
        else:
            assert program["payload"]["schema"] == "camera_dcg_capture_program_v1"
    assert service.registry.audit()["integrity"] == "verified"


def test_r1_registry_seed_rejects_external_bundle_override(tmp_path, monkeypatch):
    service = _project(tmp_path)
    monkeypatch.setenv("CAMERAE2E_R1_ASSET_BUNDLE", str(WORKSPACE / "examples/r1"))
    try:
        ensure(service)
    except ValueError as error:
        assert "installed package bundle" in str(error)
    else:
        raise AssertionError("External R1 example override was accepted by installed pilot")
    assert service.registry.list("assets") == []


def test_r1_registry_spatial_bridge_keeps_identical_integer_raw(tmp_path):
    service = _project(tmp_path)
    pilot = ensure(service)
    source = load_sources(WORKSPACE, ["r1-stop-clear-v1"], "r1-flat-v1")[
        "r1-stop-clear-v1"
    ]
    image_id = pilot["spectral_image_ids"]["r1-flat-v1"]
    test = service.create_test(imaging.spatial_test_template(
        service, pilot["assembly_id"], image_id
    ))
    run = _finish(service, {
        "assembly_id": pilot["assembly_id"], "test_id": test["id"],
        "plugin_id": "spatial_scene_v1", "scene_id": image_id,
        "parameters": {"seed": 471, "frames": 1, "exposure_ms": 4},
    })
    original = evaluate_response(
        source.rate_request(), source.electrical_model, exposure_s=0.004,
        analog_gain=1.0, noise_enabled=True, seed=471, isp_profile=isp.default_profile(),
    )
    np.testing.assert_array_equal(_array(service, run, "raw_frames")[0],
                                  original["arrays"]["raw_dn"])
    assert canonical(run["scene_snapshot"]["sampling"]) == canonical(
        source.rate_request()["scene"]["sampling"]
    )
    assert run["result"]["summary"]["physical_validation"] == "not_established"


def test_r1_registry_temporal_static_hdr_and_shared_charge_dcg_execute(tmp_path):
    service = _project(tmp_path)
    pilot = ensure(service)
    cases = (
        ("temporal", "temporal_scene_v1", "changing", None, "raw_frames", (4, 16, 16)),
        ("hdr", "hdr_bracket_v1", "static", "hdr", "raw_frames", (4, 16, 16)),
        ("dcg", "shared_charge_dcg_v1", "static", "dcg", "raw_groups", (4, 2, 16, 16)),
    )
    for key, plugin, scene, program, raw_name, shape in cases:
        request = {
            "assembly_id": pilot["assembly_id"],
            "test_id": pilot["test_ids"][key], "plugin_id": plugin,
            "scene_sequence_id": pilot["scene_sequence_ids"][scene],
            "control_sequence_id": pilot["control_sequence_ids"][scene],
            "parameters": {"seed": 471},
        }
        if program:
            request["capture_program_id"] = pilot["capture_program_ids"][program]
        run = _finish(service, request)
        raw = _array(service, run, raw_name)
        assert raw.shape == shape and raw.dtype.kind in "iu"
        summary = run["result"]["summary"]
        assert summary["physical_validation"] == "not_established"
        if key == "temporal":
            assert summary["frame_count"] == 4
            assert _array(service, run, "rgb_frames").shape == (4, 16, 16, 3)
        elif key == "hdr":
            assert summary["frame_count"] == 4 and summary["hdr_merge_applied"] is True
            assert _array(service, run, "rgb_frames").shape == (4, 16, 16, 3)
        else:
            assert summary["acquisition_count"] == 4 and summary["merge_applied"] is True
            assert summary["raw_axes"] == ["acquisition", "readout", "row", "column"]
            assert _array(service, run, "rgb_groups").shape == (4, 2, 16, 16, 3)
            assert "raw_frames.npy" not in {artifact["name"] for artifact in run["artifacts"]}
    assert service.registry.audit()["integrity"] == "verified"


def test_r1_seeded_modes_are_selectable_from_same_origin_workbench(tmp_path):
    service = _project(tmp_path)
    pilot = ensure(service)
    app = create_app(service.registry.directory, WORKSPACE, start_worker=False,
                     seed_demo=False)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200 and "Engineering Workbench" in page.text
        data = client.get("/api/v1/workbench").json()
        assert pilot["assembly_id"] in {record["id"] for record in data["assemblies"]}
        assert set(pilot["scene_sequence_ids"].values()) <= {
            record["id"] for record in data["scene_sequences"]
        }
        assert set(pilot["control_sequence_ids"].values()) <= {
            record["id"] for record in data["control_sequences"]
        }
        assert set(pilot["capture_program_ids"].values()) <= {
            record["id"] for record in data["capture_programs"]
        }
        assert set(pilot["test_ids"].values()) <= {
            record["id"] for record in data["tests"]
        }
        assert {"temporal_scene_v1", "hdr_bracket_v1", "shared_charge_dcg_v1"} <= {
            record["plugin_id"] for record in data["plugins"] if record["available"]
        }


def test_seed_r1_modes_cli_requires_correct_project_and_is_idempotent(tmp_path):
    state = tmp_path / "cli-state"
    env = {**os.environ, "PYTHONPATH": str(WORKSPACE / "src")}
    common = [sys.executable, "-B", "-m", "camera_platform", "--workspace", str(WORKSPACE),
              "--state-dir", str(state), "--r1-project-id", "r1-cli-test"]
    init = subprocess.run(common[:4] + ["init-r1"] + common[4:], cwd=WORKSPACE,
                          env=env, capture_output=True, text=True, check=True)
    assert json.loads(init.stdout)["project_id"] == "r1-cli-test"
    command = common[:4] + ["seed-r1-modes"] + common[4:]
    first = json.loads(subprocess.run(command, cwd=WORKSPACE, env=env,
                                      capture_output=True, text=True, check=True).stdout)
    second = json.loads(subprocess.run(command, cwd=WORKSPACE, env=env,
                                       capture_output=True, text=True, check=True).stdout)
    assert first == second and first["schema"] == "r1-registry-pilot-v1"
    wrong = subprocess.run(command[:-1] + ["wrong-project"], cwd=WORKSPACE, env=env,
                           capture_output=True, text=True)
    assert wrong.returncode != 0
    assert "another mode or ID" in wrong.stderr
