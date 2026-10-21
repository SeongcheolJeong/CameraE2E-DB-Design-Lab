"""Registered source/receiver ROI remains pinned through Test, queue and Worker."""

from copy import deepcopy

import numpy as np
import pytest
from test_charge_collection_models import authored_generation_model
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as workflow

from camera_platform import capture_programs, factory, prescription_psf, sequences
from camera_platform.contracts import AssemblyCreate
from camera_platform.demo import seed
from camera_platform.service import PlatformService, PreflightError


def _static_scene(service, assembly, *, scale=0.0003, side=64):
    scene = sequences.create_fixture(
        service, sequences.fixture_template(service, assembly["id"])
        | {"shape": [side, side], "knot_count": 2, "pattern": "static",
           "irradiance_scale": scale},
    )
    source = scene["image_snapshots"][0]
    sampling = source["sampling"]
    start = (side - 8) // 2
    roi = {"crop_origin": [sampling["crop_origin"][0] + start,
                           sampling["crop_origin"][1] + start],
           "shape": [8, 8]}
    return scene, roi


def _control(service, assembly, scene):
    return sequences.create_sequence(
        service, "control_sequences",
        sequences.control_template(service, assembly["id"], scene["id"]),
    )


def _roi_params(crop):
    return dict(zip(
        ("capture_row", "capture_col", "capture_height", "capture_width"),
        (*crop["crop_origin"], *crop["shape"]), strict=True,
    ))


def test_registered_hdr_source_halo_matches_full_source_in_receiver_and_binds_worker(workflow):
    service, sdk, assembly, _, _, program, _, _ = workflow
    scene, roi = _static_scene(service, assembly)
    control = _control(service, assembly, scene)
    identifiers = dict(
        assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    )
    full_test = service.create_test(sdk.template("temporal-test", **identifiers))
    assert "capture_crop" not in full_test["stimulus"]
    malformed = sdk.http.get(
        "/api/v1/templates/temporal-test", params={**identifiers, "capture_row": 1}
    )
    assert malformed.status_code == 422
    roi_spec = sdk.template("temporal-test", **identifiers, **_roi_params(roi))
    assert roi_spec["stimulus"]["capture_crop"] == roi
    assert roi_spec["stimulus"]["source_support"] == {
        "kind": "native_sensor",
        "full_sensor_shape": scene["image_snapshots"][0]["sampling"]["full_sensor_shape"],
        "crop_origin": scene["image_snapshots"][0]["sampling"]["crop_origin"],
        "shape": [64, 64],
    }
    roi_test = service.create_test(roi_spec)
    assert roi_test["content_hash"] != full_test["content_hash"]
    full = finish(service, sdk.submit_hdr(
        assembly["id"], full_test["id"], scene["id"], control["id"], program["id"],
        seed=29,
    ))
    queued = sdk.submit_hdr(
        assembly["id"], roi_test["id"], scene["id"], control["id"], program["id"],
        seed=29, capture_crop=roi,
    )
    assert queued["parameters"]["capture_crop"] == roi
    assert queued["request"]["scene_sequence"] == scene["payload"]
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    changed = deepcopy(job)
    changed["parameters"]["capture_crop"]["crop_origin"][0] += 2
    with pytest.raises(ValueError, match="exact sequence"):
        service.execute_run(changed)
    changed = deepcopy(job)
    changed["request"]["control_sequence"]["frames"][0]["frame_start_ns"] += 1
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(changed)
    roi_run = service.registry.finish(job["id"], result=service.execute_run(job))
    assert roi_run["result"]["summary"]["geometry"]["crop_origin"] == roi["crop_origin"]
    assert roi_run["result"]["summary"]["output_size"] == roi["shape"]
    assert roi_run["result"]["summary"]["prediction"]["sample_geometry"] == {
        key: roi_test["stimulus"][key] for key in ("capture_crop", "source_support")
    }
    assert load_array(service, roi_run, "raw_frames").shape[1:] == (8, 8)
    source_origin = scene["image_snapshots"][0]["sampling"]["crop_origin"]
    y, x = (roi["crop_origin"][axis] - source_origin[axis] for axis in range(2))
    np.testing.assert_allclose(
        load_array(service, roi_run, "mean_electrons"),
        load_array(service, full, "mean_electrons")[:, y:y + 8, x:x + 8],
        rtol=2e-13, atol=1e-10,
    )
    with pytest.raises(ValueError, match="exact sequence"):
        service.submit_run({
            "assembly_id": assembly["id"], "test_id": full_test["id"],
            "scene_sequence_id": scene["id"], "control_sequence_id": control["id"],
            "capture_program_id": program["id"], "plugin_id": "hdr_bracket_v1",
            "parameters": {"seed": 29, "capture_crop": roi},
        })


def test_registered_two_component_roi_preserves_both_sources_and_rejects_missing_halo(workflow):
    service, sdk, assembly, _, _, _, _, _ = workflow
    scene, roi = _static_scene(service, assembly)
    unmodulated, _ = _static_scene(service, assembly, scale=0.0001)
    control = _control(service, assembly, scene)
    program_data = capture_programs.template(service, assembly["id"])
    program_data.update(name="Two-component ROI", revision="roi-components")
    program_data["payload"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": control["payload"]["clock"]["clock_id"],
        "period_ns": 1_000_000, "on_time_ns": 500_000,
        "phase_origin_ns": 0, "low": 0, "high": 1,
    }
    program = capture_programs.create(service, program_data)
    identifiers = dict(
        assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
        unmodulated_scene_sequence_id=unmodulated["id"],
    )
    test = service.create_test(sdk.template(
        "temporal-test", **identifiers, **_roi_params(roi),
    ))
    assert test["stimulus"]["unmodulated_scene_sequence_hash"] == unmodulated["content_hash"]
    queued = sdk.submit_hdr(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=31, unmodulated_scene_sequence_id=unmodulated["id"], capture_crop=roi,
    )
    hashes = {artifact["sha256"] for artifact in queued["input_artifacts"]}
    assert {artifact["sha256"] for artifact in unmodulated["artifacts"]} <= hashes
    assert {artifact["sha256"] for artifact in scene["artifacts"]} <= hashes
    result = finish(service, queued)
    assert result["result"]["summary"]["unmodulated_scene_sequence_id"] == unmodulated["id"]
    assert result["result"]["summary"]["prediction"]["sample_geometry"] == {
        key: test["stimulus"][key] for key in ("capture_crop", "source_support")
    }
    unmodulated_rate = load_array(
        service, result, "unmodulated_channel_photoelectron_rate_scenes"
    )
    assert unmodulated_rate.shape[1:3] == (8, 8)
    missing_halo = {"crop_origin": [
        scene["image_snapshots"][0]["sampling"]["crop_origin"][0] + 2,
        scene["image_snapshots"][0]["sampling"]["crop_origin"][1] + 2,
    ], "shape": [8, 8]}
    invalid_test = service.create_test(sdk.template(
        "temporal-test", **identifiers, **_roi_params(missing_halo),
    ))
    with pytest.raises(PreflightError) as error:
        service.submit_run({
            "assembly_id": assembly["id"], "test_id": invalid_test["id"],
            "scene_sequence_id": scene["id"], "control_sequence_id": control["id"],
            "capture_program_id": program["id"], "plugin_id": "hdr_bracket_v1",
            "parameters": {"seed": 31, "unmodulated_scene_sequence_id": unmodulated["id"],
                           "capture_crop": missing_halo},
        })
    assert any("halo" in issue["message"] for issue in error.value.report["issues"])


def test_registered_temporal_roi_binds_test_and_worker_without_hdr_program(workflow):
    service, sdk, assembly, _, _, _, _, _ = workflow
    scene, roi = _static_scene(service, assembly)
    control = _control(service, assembly, scene)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], **_roi_params(roi),
    ))
    queued = sdk.submit_temporal(
        assembly["id"], test["id"], scene["id"], control["id"],
        seed=43, capture_crop=roi,
    )
    job = service.registry.claim()
    assert queued["id"] == job["id"]
    changed = deepcopy(job)
    changed["request"]["seed"] += 1
    with pytest.raises(ValueError, match="Temporal queued request"):
        service.execute_run(changed)
    changed = deepcopy(job)
    changed["parameters"].pop("capture_crop")
    with pytest.raises(ValueError, match="exact sequence"):
        service.execute_run(changed)
    result = service.registry.finish(job["id"], result=service.execute_run(job))
    assert result["result"]["summary"]["output_size"] == [8, 8]
    assert result["result"]["summary"]["geometry"]["crop_origin"] == roi["crop_origin"]
    assert load_array(service, result, "raw_frames").shape[1:] == (8, 8)


def test_registered_carrier_halo_uses_pinned_sensor_and_source_ledgers(tmp_path):
    service = PlatformService(tmp_path / "carrier")
    seed(service)
    original = service.registry.list("assemblies")[0]
    base_sensor = next(
        service.registry.get("models", identity) for identity in original["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    prescription = prescription_psf.request_template(version=3)
    prescription["prescription"]["sampling"].update(
        polar_rings=20, azimuth_samples=20,
    )
    ray_input = factory.template(service, "rayoptics_lens_v1", request_version=3)
    ray_input["request"] = prescription["prescription"]
    ray_job = finish(service, factory.submit(service, ray_input))
    psf_input = factory.template(service, "prescription_psf_v1", request_version=3)
    psf_input["request"] = prescription
    psf_job = finish(service, factory.submit(service, psf_input))
    conditions = deepcopy(original["conditions"])
    conditions["f_number"] = {
        "value": psf_job["result"]["summary"]["geometric_reference_f_number"],
        "unit": "1",
    }
    parent = factory.publish(service, {
        "job_id": ray_job["id"], "name": "Joint carrier directions", "revision": "1",
        "conditions": {key: value for key, value in conditions.items()
                       if key not in {"exposure_s", "gain"}},
    })
    lens = factory.publish(service, {
        "job_id": psf_job["id"], "base_lens_model_id": parent["id"],
        "name": "Joint carrier PSF", "revision": "1", "conditions": conditions,
        "condition_scope": "optical_response",
    })
    draft = authored_generation_model(base_sensor)
    wavelength = [500, 600]
    pitch = 3
    draft["spectral_range"] = {"min": 500, "max": 600, "unit": "nm"}
    for key in ("active_height", "active_width"):
        draft["properties"][key] = {"value": 0.384, "unit": "mm"}
    draft["payload"]["wavelength_nm"] = wavelength
    draft["payload"]["pixel_pitch_um"] = pitch
    generation = draft["payload"]["native_pixel_response"]
    generation["wavelength_nm"] = wavelength
    generation["pixel_pitch_um"] = pitch
    generation["values"] = np.broadcast_to(
        [0.2, 0.35, 0.6, 0.8], (len(wavelength), 2, 4, 4)
    ).tolist()
    collection = draft["payload"]["charge_collection"]
    collection["wavelength_nm"] = wavelength
    collection["pixel_pitch_um"] = pitch
    collection["destination_offsets_rc"] = [[0, 0], [2, -1]]
    collection["probabilities"] = [
        [[0.7, 0.2] for _ in range(4)] for _ in wavelength
    ]
    sensor = service.create_model(draft)
    fields = {key: deepcopy(value) for key, value in original.items()
              if key in AssemblyCreate.model_fields}
    fields.update(
        name="Registered carrier ROI camera", revision="carrier-roi-1",
        conditions=conditions, spectral_range=draft["spectral_range"],
        model_ids=[lens["id"], sensor["id"], original["model_ids"][2]],
    )
    assembly = service.create_assembly(fields)
    assert assembly["compatibility"]["status"] == "PASS", assembly["compatibility"]["issues"]
    scene, roi = _static_scene(service, assembly, side=50)
    source_origin = scene["image_snapshots"][0]["sampling"]["crop_origin"]
    # Asymmetric +2/-1 carrier offsets require asymmetric optical source margins.
    roi["crop_origin"] = [source_origin[0] + 22, source_origin[1] + 20]
    control = _control(service, assembly, scene)
    program = capture_programs.create(service, capture_programs.template(service, assembly["id"]))
    test = service.create_test(sequences.test_template(
        service, assembly["id"], scene["id"], control["id"], program["id"],
        capture_crop=roi,
    ))
    request = {
        "assembly_id": assembly["id"], "test_id": test["id"],
        "scene_sequence_id": scene["id"], "control_sequence_id": control["id"],
        "capture_program_id": program["id"], "plugin_id": "hdr_bracket_v1",
        "parameters": {"seed": 37, "capture_crop": roi},
    }
    queued = service.submit_run(request)
    assert queued["request"]["scene_sequence"] == scene["payload"]
    result = finish(service, queued)
    summary = result["result"]["summary"]
    assert summary["source_support"]["shape"] == [50, 50]
    assert summary["capture_crop"]["crop_origin"] == roi["crop_origin"]
    assert summary["array_geometry"]["collection_generated_rate_scenes"] == "source_support"
    assert load_array(service, result, "raw_frames").shape[1:] == (8, 8)
    generated = load_array(service, result, "collection_generated_rate_scenes")
    collected = load_array(service, result, "source_support_collected_photoelectron_rate_scenes")
    assert generated.shape[1:3] == collected.shape[1:3] == (50, 50)
    assert generated.sum() > 0 and collected.sum() > 0
    missing_carrier_halo = {"crop_origin": [source_origin[0] + 20,
                                            source_origin[1] + 20], "shape": [8, 8]}
    invalid_test = service.create_test(sequences.test_template(
        service, assembly["id"], scene["id"], control["id"], program["id"],
        capture_crop=missing_carrier_halo,
    ))
    with pytest.raises(PreflightError) as error:
        service.submit_run({
            **request, "test_id": invalid_test["id"],
            "parameters": {"seed": 37, "capture_crop": missing_carrier_halo},
        })
    assert any("combined PSF and carrier halo" in issue["message"]
               for issue in error.value.report["issues"])
