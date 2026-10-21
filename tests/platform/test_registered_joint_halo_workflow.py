"""Published 3D optical/carrier halo through registered HDR RAW and ISP."""

from __future__ import annotations

import copy
import io

import numpy as np
import pytest
import test_scene_render
from test_charge_collection_models import authored_generation_model
from test_hdr_workflow import finish, load_array

from camera_platform import (
    capture_programs,
    factory,
    imaging,
    prescription_psf,
    scene_render,
    scene_templates,
    sequences,
)
from camera_platform.contracts import AssemblyCreate, SpectralImageImport
from camera_platform.demo import PROVENANCE, seed
from camera_platform.service import PlatformService, PreflightError


def _asset(service, name, payload):
    return scene_render.create_asset(service, {**PROVENANCE, "name": name, "payload": payload})


def _timeline(service, image, name):
    return sequences.create_sequence(service, "scene_sequences", {
        **PROVENANCE,
        "name": name,
        "revision": "1",
        "source_uri": "fixture://registered-joint-halo-" + name,
        "method_and_version": "authored_static_timeline_from_registered_image:1",
        "payload": {
            "schema": "timed_spectral_scene_v1",
            "clock": {
                "clock_id": "registered-joint-halo-clock", "unit": "ns", "reference": "relative",
                "origin": "Authored simulation time zero; no hardware acquisition clock",
            },
            "interpolation": "linear_irradiance",
            "knots": [
                {"time_ns": 0, "spectral_image_id": image["id"]},
                {"time_ns": 80_000_000, "spectral_image_id": image["id"]},
            ],
        },
    })


def _control(service, assembly, timeline):
    data = sequences.control_template(service, assembly["id"], timeline["id"])
    for index, frame in enumerate(data["payload"]["frames"]):
        frame["applied"]["exposure_ns"] = (4 if index % 2 == 0 else 8) * 1_000_000
    return sequences.create_sequence(service, "control_sequences", data)


def test_published_joint_halo_registered_hdr_worker_and_missing_support(tmp_path, monkeypatch):
    if not test_scene_render.PYTHON.exists():
        pytest.skip("Isolated Mitsuba environment is not installed")
    monkeypatch.setenv("CAMERA_PLATFORM_MITSUBA_PYTHON", str(test_scene_render.PYTHON))
    service = PlatformService(tmp_path / "joint-halo")
    seed(service)
    original = service.registry.list("assemblies")[0]
    base_sensor = next(
        service.registry.get("models", identity)
        for identity in original["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    wavelengths = [500.0, 600.0]
    geometry = _asset(service, "Visible plane geometry", {
        "kind": "geometry", "unit": "m", "primitive": "rectangle",
    })
    material = _asset(service, "Synthetic diffuse surface", {
        "kind": "material", "bsdf": "diffuse",
        "spectrum": {"wavelength_nm": wavelengths, "values": [0.5, 0.5]},
        "surface_state": "synthetic matte plane",
    })
    light = _asset(service, "Synthetic spectral environment", {
        "kind": "light", "distribution": "constant_environment",
        "spectrum": {"wavelength_nm": wavelengths, "values": [0.002, 0.003]},
        "unit": "W/m^2/sr/nm", "time_model": "static",
    })
    scene = _asset(service, "Visible 3D plane", {
        "kind": "scene", "coordinate_system": "right_handed_y_up_metres",
        "instances": [{
            "geometry_id": geometry["id"], "material_id": material["id"],
            "area_light_id": None, "to_world": test_scene_render.plane(3.0),
        }],
        "environment_light_id": light["id"], "point_lights": [],
    })

    prescription = prescription_psf.request_template(version=3)
    prescription["prescription"]["sampling"].update(polar_rings=20, azimuth_samples=20)
    ray_input = factory.template(service, "rayoptics_lens_v1", request_version=3)
    ray_input["request"] = prescription["prescription"]
    ray_job = finish(service, factory.submit(service, ray_input))
    psf_input = factory.template(service, "prescription_psf_v1", request_version=3)
    psf_input["request"] = prescription
    psf_job = finish(service, factory.submit(service, psf_input))
    conditions = copy.deepcopy(original["conditions"])
    conditions.update(
        light_id={"value": scene["illumination_id"], "unit": "1"},
        f_number={
            "value": psf_job["result"]["summary"]["geometric_reference_f_number"],
            "unit": "1",
        },
    )
    parent = factory.publish(service, {
        "job_id": ray_job["id"], "name": "Joint directional Lens", "revision": "1",
        "conditions": {k: v for k, v in conditions.items()
                       if k not in {"exposure_s", "gain"}},
    })
    lens = factory.publish(service, {
        "job_id": psf_job["id"], "base_lens_model_id": parent["id"],
        "name": "Matched joint PSF", "revision": "1", "conditions": conditions,
        "condition_scope": "optical_response",
    })
    draft = authored_generation_model(base_sensor)
    for key in ("active_height", "active_width"):
        draft["properties"][key] = {"value": 0.384, "unit": "mm"}
    draft["payload"].update(wavelength_nm=wavelengths, pixel_pitch_um=3)
    generation = draft["payload"]["native_pixel_response"]
    generation.update(wavelength_nm=wavelengths, pixel_pitch_um=3)
    generation["values"] = np.broadcast_to(
        [0.2, 0.35, 0.6, 0.8], (len(wavelengths), 2, 4, 4)
    ).tolist()
    collection = draft["payload"]["charge_collection"]
    collection.update(
        wavelength_nm=wavelengths, pixel_pitch_um=3,
        destination_offsets_rc=[[0, 0], [2, -1]],
        probabilities=[[[0.7, 0.2] for _ in range(4)] for _ in wavelengths],
    )
    sensor = service.create_model(draft)
    assembly_data = {
        key: copy.deepcopy(value) for key, value in original.items()
        if key in AssemblyCreate.model_fields
    }
    assembly_data.update(
        name="Registered joint halo camera", revision="1", conditions=conditions,
        spectral_range={"min": 500, "max": 600, "unit": "nm"},
        model_ids=[lens["id"], sensor["id"], original["model_ids"][2]],
    )
    assembly = service.create_assembly(assembly_data)
    assert assembly["compatibility"]["status"] == "PASS"

    template = scene_templates.render_template(service, scene["id"], assembly["id"])
    assert template["issues"] == []
    render_request = template["request"]
    receiver = {"crop_origin": [60, 60], "shape": [8, 8]}
    render_request.update(
        shape=receiver["shape"], crop_origin=receiver["crop_origin"],
        samples_per_pixel=2, render_charge_collection_halo=True,
    )
    render_job = finish(service, scene_render.submit(service, render_request))
    frozen = render_job["request"]
    assert frozen["capture_crop"] == receiver
    assert frozen["carrier_halo"]["sensor_model_id"] == sensor["id"]
    assert frozen["carrier_halo"]["charge_collection_sha256"] == (
        sensor["response_artifacts"]["charge_collection"]["sha256"]
    )
    assert frozen["shape"] != receiver["shape"]
    assert render_job["result"]["psf_source_support"]["carrier_halo"] == (
        frozen["carrier_halo"]
    )
    image = scene_render.publish(service, {
        "job_id": render_job["id"], "name": "Published joint optical/carrier halo",
    })
    assert image["render_job_snapshot"]["id"] == render_job["id"]
    assert image["sampling"] == frozen["sampling"]

    timeline = _timeline(service, image, "published-joint-halo")
    assert timeline["image_snapshots"][0]["render_job_snapshot"]["id"] == render_job["id"]
    control = _control(service, assembly, timeline)
    program = capture_programs.create(service, capture_programs.template(service, assembly["id"]))
    specification = sequences.test_template(
        service, assembly["id"], timeline["id"], control["id"], program["id"],
    )
    assert specification["stimulus"]["capture_crop"] == receiver
    assert specification["stimulus"]["source_support"]["shape"] == frozen["shape"]
    test = service.create_test(specification)
    request = {
        "assembly_id": assembly["id"], "test_id": test["id"],
        "scene_sequence_id": timeline["id"], "control_sequence_id": control["id"],
        "capture_program_id": program["id"], "plugin_id": "hdr_bracket_v1",
        "parameters": {"seed": 37},
    }
    queued = service.submit_run(request)
    assert queued["parameters"]["capture_crop"] == receiver
    assert queued["scene_sequence_snapshot"]["image_snapshots"][0][
        "render_job_snapshot"
    ]["id"] == render_job["id"]
    run = finish(service, queued)
    summary = run["result"]["summary"]
    assert summary["output_size"] == [8, 8]
    assert {key: summary["capture_crop"][key] for key in receiver} == receiver
    assert summary["source_support"]["shape"] == frozen["shape"]
    assert summary["charge_collection"]["crop_support"] == (
        "partial_source_combined_halo_then_receiver_crop"
    )
    assert summary["array_geometry"]["collection_generated_rate_scenes"] == "source_support"
    raw, rgb = load_array(service, run, "raw_frames"), load_array(service, run, "rgb_frames")
    generated = load_array(service, run, "collection_generated_rate_scenes")
    collected = load_array(
        service, run, "source_support_collected_photoelectron_rate_scenes"
    )
    intrinsic_loss = load_array(service, run, "collection_intrinsic_loss_rate_scenes")
    off_sensor_loss = load_array(service, run, "collection_off_sensor_loss_rate_scenes")
    outside_support = load_array(
        service, run, "collection_outside_support_collected_rate_scenes"
    )
    assert raw.shape[1:] == (8, 8) and rgb.shape[1:] == (8, 8, 3)
    assert raw.dtype.kind in "iu" and np.isfinite(rgb).all()
    assert generated.shape[1:3] == collected.shape[1:3] == tuple(frozen["shape"])
    assert generated.sum() > 0 and collected.sum() > 0
    np.testing.assert_allclose(
        generated.sum(),
        collected.sum() + intrinsic_loss.sum() + off_sensor_loss.sum()
        + outside_support.sum(),
        rtol=1e-12, atol=1e-12,
    )
    assert raw.mean() > sensor["payload"]["readout"]["black_level_dn"]

    # Truncate the very same published source down to the receiver. This
    # negative control has valid image metadata but lacks both pinned halos.
    source_artifact = next(a for a in image["artifacts"] if a["name"] == "spectral-image.npy")
    source_array = np.load(service.registry.blob_path(source_artifact["sha256"]),
                           allow_pickle=False)
    start = [receiver["crop_origin"][axis] - image["sampling"]["crop_origin"][axis]
             for axis in range(2)]
    clipped = source_array[start[0]:start[0] + 8, start[1]:start[1] + 8].copy()
    stream = io.BytesIO()
    np.save(stream, clipped, allow_pickle=False)
    metadata = {key: copy.deepcopy(image[key]) for key in SpectralImageImport.model_fields}
    metadata.update(
        name="Intentionally missing both halos", asset_id=None, source_kind="estimated",
        source_uri="fixture://truncated-published-halo-negative-control",
        method_and_version="authored_truncated_halo_negative_control:1",
        parents=[image["asset_id"]],
        sampling={**image["sampling"], "crop_origin": receiver["crop_origin"]},
    )
    truncated = imaging.import_spectral(service, metadata, stream.getvalue())
    bad_timeline = _timeline(service, truncated, "truncated-joint-halo")
    bad_control = _control(service, assembly, bad_timeline)
    bad_test = service.create_test(sequences.test_template(
        service, assembly["id"], bad_timeline["id"], bad_control["id"],
        program["id"], capture_crop=receiver,
    ))
    with pytest.raises(PreflightError) as error:
        service.submit_run({
            **request, "test_id": bad_test["id"],
            "scene_sequence_id": bad_timeline["id"],
            "control_sequence_id": bad_control["id"],
            "parameters": {"seed": 37, "capture_crop": receiver},
        })
    assert any("combined PSF and carrier halo" in issue["message"]
               for issue in error.value.report["issues"])
