"""A registered DCG receiver ROI keeps source halo, native timing, and two RAW paths."""

from copy import deepcopy

import numpy as np
import pytest
from test_charge_collection_models import authored_generation_model
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as workflow

from camera_platform import capture_programs, factory, prescription_psf, sequences
from camera_platform.contracts import AssemblyCreate
from camera_platform.service import PreflightError


def _static_scene(service, assembly, side):
    fixture = sequences.create_fixture(
        service,
        sequences.fixture_template(service, assembly["id"])
        | {"shape": [side, side], "knot_count": 2, "pattern": "static",
           "irradiance_scale": 0.0003},
    )
    # The DCG topology requires one exact static optical image at both knots.
    data = {
        key: deepcopy(value) for key, value in fixture.items()
        if key in sequences.SceneSequenceCreate.model_fields
    }
    data.update(name=f"DCG {side}-pixel static source", revision=f"static-{side}")
    data["payload"]["knots"][1]["spectral_image_id"] = (
        data["payload"]["knots"][0]["spectral_image_id"]
    )
    return sequences.create_sequence(service, "scene_sequences", data)


def _control(service, assembly, scene):
    data = sequences.control_template(service, assembly["id"], scene["id"])
    data["payload"]["frames"] = data["payload"]["frames"][:2]
    return sequences.create_sequence(service, "control_sequences", data)


def _request(assembly, scene, control, program, test, crop=None):
    return {
        "assembly_id": assembly["id"], "scene_sequence_id": scene["id"],
        "control_sequence_id": control["id"], "capture_program_id": program["id"],
        "test_id": test["id"], "plugin_id": "shared_charge_dcg_v1",
        "parameters": {"seed": 919, **({"capture_crop": crop} if crop is not None else {})},
    }


@pytest.fixture
def dcg_roi_workflow(workflow):
    service, sdk, original, *_ = workflow
    original_lens = next(
        service.registry.get("models", identity) for identity in original["model_ids"]
        if service.registry.get("models", identity)["kind"] == "lens"
    )
    base_sensor = next(
        service.registry.get("models", identity) for identity in original["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    prescription = prescription_psf.request_template(version=3)
    prescription["prescription"]["sampling"].update(polar_rings=20, azimuth_samples=20)
    ray_request = factory.template(service, "rayoptics_lens_v1", request_version=3)
    ray_request["request"] = prescription["prescription"]
    ray_job = finish(service, factory.submit(service, ray_request))
    psf_request = factory.template(service, "prescription_psf_v1", request_version=3)
    psf_request["request"] = prescription
    psf_job = finish(service, factory.submit(service, psf_request))
    conditions = deepcopy(original["conditions"])
    conditions["f_number"] = {
        "value": psf_job["result"]["summary"]["geometric_reference_f_number"], "unit": "1",
    }
    parent_lens = factory.publish(service, {
        "job_id": ray_job["id"], "name": "DCG joint carrier directions", "revision": "1",
        "conditions": {key: value for key, value in conditions.items()
                       if key not in {"exposure_s", "gain"}},
    })
    lens = factory.publish(service, {
        "job_id": psf_job["id"], "base_lens_model_id": parent_lens["id"],
        "name": "DCG joint carrier PSF", "revision": "1",
        "conditions": conditions, "condition_scope": "optical_response",
    })
    sensor_data = authored_generation_model(base_sensor)
    wave = [500, 600]
    pitch = 3
    sensor_data["spectral_range"] = {"min": 500, "max": 600, "unit": "nm"}
    sensor_data["properties"].update(
        active_height={"value": 0.156, "unit": "mm"},
        active_width={"value": 0.156, "unit": "mm"},
        accepted_angle_range={"min": 0, "max": 20, "unit": "deg"},
    )
    sensor_data["payload"].update(wavelength_nm=wave, pixel_pitch_um=pitch)
    generation = sensor_data["payload"]["native_pixel_response"]
    generation.update(
        wavelength_nm=wave, pixel_pitch_um=pitch, theta_deg=[0, 20],
        values=np.broadcast_to(
            [0.2, 0.35, 0.6, 0.8], (len(wave), 2, 4, 4)
        ).tolist(),
    )
    collection = sensor_data["payload"]["charge_collection"]
    collection.update(
        wavelength_nm=wave, pixel_pitch_um=pitch,
        destination_offsets_rc=[[0, 0], [2, -1]],
        probabilities=[[[0.7, 0.2] for _ in range(4)] for _ in wave],
    )
    sensor = service.create_model(sensor_data)
    fields = {
        key: deepcopy(value) for key, value in original.items()
        if key in AssemblyCreate.model_fields
    }
    fields.update(
        name="DCG registered source-halo camera", revision="dcg-roi-1",
        conditions=conditions, spectral_range=sensor_data["spectral_range"],
        model_ids=[
            sensor["id"] if identity == base_sensor["id"] else
            lens["id"] if identity == original_lens["id"] else identity
            for identity in original["model_ids"]
        ],
    )
    assembly = service.create_assembly(fields)
    assert assembly["compatibility"]["status"] == "PASS", assembly["compatibility"]["issues"]
    source, full = (_static_scene(service, assembly, side) for side in (50, 52))
    source_control, full_control = (
        _control(service, assembly, scene) for scene in (source, full)
    )
    source_origin = source["image_snapshots"][0]["sampling"]["crop_origin"]
    crop = {"crop_origin": [source_origin[0] + 22, source_origin[1] + 20],
            "shape": [8, 8]}
    assert crop["crop_origin"][0] % 2 == crop["crop_origin"][1] % 2 == 1
    program_data = capture_programs.template(service, assembly["id"], mode="shared_charge")
    program_data.update(name="DCG with receiver ROI", revision="dcg-roi-1")
    model = program_data["payload"]["electrical_model"]
    model["fixed_pattern"].update(prnu_sigma=0, dsnu_e_per_s=2, seed=73)
    program = capture_programs.create(service, program_data)
    roi_test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=source["id"],
        control_sequence_id=source_control["id"], capture_program_id=program["id"],
        capture_row=crop["crop_origin"][0], capture_col=crop["crop_origin"][1],
        capture_height=8, capture_width=8,
    ))
    full_test = service.create_test(sequences.test_template(
        service, assembly["id"], full["id"], full_control["id"], program["id"],
    ))
    return (service, sdk, assembly, source, source_control, full, full_control,
            program, roi_test, full_test, crop)


def test_registered_dcg_source_halo_reaches_both_native_raw_paths(dcg_roi_workflow):
    (service, sdk, assembly, source, control, full, full_control,
     program, roi_test, full_test, crop) = dcg_roi_workflow
    source_image = source["image_snapshots"][0]
    support = roi_test["stimulus"]["source_support"]
    assert support == {
        "kind": "native_sensor", "full_sensor_shape": [52, 52],
        "crop_origin": source_image["sampling"]["crop_origin"], "shape": [50, 50],
    }
    sample = roi_test["stimulus"]["sample_contract"]
    assert sample["sampling"]["crop_origin"] == crop["crop_origin"]
    assert sample["raster_shape"] == [8, 8]
    assert sample["readout_ids"] == ["HCG", "LCG"]
    assert roi_test["content_hash"] != full_test["content_hash"]
    # Both controls were registered against their immutable source shapes.
    assert source["image_snapshots"][0]["shape"][:2] == [50, 50]
    assert full["image_snapshots"][0]["shape"][:2] == [52, 52]

    queued = sdk.submit_dcg(
        assembly["id"], roi_test["id"], source["id"], control["id"], program["id"],
        seed=919, capture_crop=crop,
    )
    assert queued["parameters"]["capture_crop"] == crop
    assert queued["request"]["scene_sequence"] == source["payload"]
    assert queued["scene_sequence_snapshot"]["id"] == source["id"]
    assert queued["test_snapshot"]["stimulus"]["source_support"] == support
    artifacts = {artifact["sha256"] for artifact in queued["input_artifacts"]}
    assert {artifact["sha256"] for artifact in source_image["artifacts"]} <= artifacts
    sensor = next(model for model in queued["model_snapshots"] if model["kind"] == "sensor")
    assert sensor["response_artifacts"]["charge_collection"]["sha256"] in artifacts
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    changed = deepcopy(job)
    changed["parameters"]["capture_crop"]["crop_origin"][0] += 1
    with pytest.raises(ValueError, match="exact sequence"):
        service.execute_run(changed)
    changed = deepcopy(job)
    changed["request"]["seed"] += 1
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(changed)
    changed = deepcopy(job)
    changed["scene_sequence_snapshot"]["image_snapshots"][0]["sampling"][
        "crop_origin"
    ][0] += 1
    with pytest.raises(ValueError, match="fixed source evidence"):
        service.execute_run(changed)
    run = service.registry.finish(job["id"], result=service.execute_run(job))
    full_run = finish(service, service.submit_run(
        _request(assembly, full, full_control, program, full_test)))
    summary = run["result"]["summary"]
    assert summary["geometry"]["crop_origin"] == crop["crop_origin"]
    assert summary["output_size"] == [8, 8]
    assert summary["source_support"] == support
    assert summary["capture_crop"]["crop_origin"] == crop["crop_origin"]
    assert summary["array_geometry"]["collection_generated_rate_scenes"] == "source_support"
    assert summary["prediction"]["sample_geometry"] == sample
    assert summary["readout_ids"] == ["HCG", "LCG"]
    raw = load_array(service, run, "raw_groups")
    full_raw = load_array(service, full_run, "raw_groups")
    assert raw.shape == (2, 2, 8, 8) and raw.dtype == np.int32
    assert full_raw.shape == (2, 2, 52, 52) and full_raw.dtype == np.int32
    assert not np.array_equal(raw[:, 0], raw[:, 1])
    assert not np.array_equal(full_raw[:, 0], full_raw[:, 1])
    assert load_array(service, run, "rgb_groups").shape == (2, 2, 8, 8, 3)
    assert load_array(service, run, "collection_generated_rate_scenes").shape[1:] == (50, 50)
    row = crop["crop_origin"][0]
    line = control["payload"]["mode"]["line_time_ns"]
    start = control["payload"]["frames"][0]["frame_start_ns"]
    assert load_array(service, run, "row_start_ns")[0, 0] == start + row * line

    y, x = crop["crop_origin"]
    receiver = np.s_[..., y:y + 8, x:x + 8]
    for name in ("mean_electrons", "electrical_mean_total_charge_e",
                 "electrical_dark_current_e_per_s_map"):
        np.testing.assert_allclose(
            load_array(service, run, name), load_array(service, full_run, name)[receiver],
            rtol=2e-13, atol=1e-10,
        )
    # The paired integer codes are each real readouts. Poisson/read-noise RNG draws
    # follow local raster order, so the same seed does not imply full/ROI code equality.
    np.testing.assert_array_equal(
        load_array(service, run, "cfa_channel_index"),
        load_array(service, full_run, "cfa_channel_index")[y:y + 8, x:x + 8],
    )
    missing = {"crop_origin": [source["image_snapshots"][0]["sampling"][
        "crop_origin"][0] + 14, crop["crop_origin"][1]], "shape": [8, 8]}
    invalid_test = service.create_test(sequences.test_template(
        service, assembly["id"], source["id"], control["id"], program["id"],
        capture_crop=missing,
    ))
    with pytest.raises(PreflightError) as error:
        service.submit_run(_request(assembly, source, control, program, invalid_test, missing))
    assert any("combined PSF and carrier halo" in issue["message"]
               for issue in error.value.report["issues"])
    with pytest.raises(ValueError, match="exact sequence"):
        service.submit_run(_request(assembly, source, control, program, roi_test, missing))
