"""Real 3D optical-halo publication through a registered partial-ROI HDR Worker.

This fixture's factorized Lens has no joint directional response, so the
registered PSF-plus-charge-collection halo still needs a separate test/model.
"""

from __future__ import annotations

import copy

import numpy as np
import pytest
import test_scene_render

from camera_platform import capture_programs, factory, imaging, scene_render, sequences
from camera_platform.contracts import AssemblyCreate
from camera_platform.demo import PROVENANCE


@pytest.fixture
def scene_workflow(tmp_path, monkeypatch):
    return test_scene_render.scene_workflow.__wrapped__(tmp_path, monkeypatch)


def test_published_3d_psf_halo_runs_registered_8x8_hdr_worker(scene_workflow):
    service, assembly, request = scene_workflow
    original_lens = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "lens"
    )
    lens = factory.publish(service, {
        "job_id": original_lens["psf_factory_job_id"],
        "base_lens_model_id": original_lens["parents"][0],
        "name": "HDR optical response for published 3D source",
        "revision": "optical-scope-1",
        "conditions": assembly["conditions"],
        "condition_scope": "optical_response",
    })
    assembly_data = {
        key: copy.deepcopy(value) for key, value in assembly.items()
        if key in AssemblyCreate.model_fields
    }
    assembly_data.update(
        name="Registered 3D halo HDR camera", revision="halo-hdr-1",
        model_ids=[
            lens["id"] if identity == original_lens["id"] else identity
            for identity in assembly["model_ids"]
        ],
    )
    assembly = service.create_assembly(assembly_data)
    assert assembly["compatibility"]["status"] == "PASS"
    _, sensor, _, full_sensor_shape = imaging._native(service, assembly["id"])
    capture_origin = [(native - 8) // 2 for native in full_sensor_shape]
    request.update(
        assembly_id=assembly["id"], shape=[8, 8], crop_origin=capture_origin,
        render_psf_halo=True,
        samples_per_pixel=2,
    )
    render_job = test_scene_render.finish(service, scene_render.submit(service, request))
    capture_crop = render_job["request"]["capture_crop"]
    source_sampling = render_job["request"]["sampling"]
    assert capture_crop == {"crop_origin": capture_origin, "shape": [8, 8]}
    assert render_job["request"]["shape"] != [8, 8]
    assert render_job["result"]["psf_source_support"]["shape"] == (
        render_job["request"]["shape"]
    )
    radiance_artifact = next(
        artifact for artifact in render_job["artifacts"]
        if artifact["name"] == "spectral-radiance.npy"
    )
    radiance = np.load(
        service.registry.blob_path(radiance_artifact["sha256"]), allow_pickle=False
    )
    assert radiance.max() > 0
    image = scene_render.publish(service, {
        "job_id": render_job["id"], "name": "Registered 3D halo source",
    })
    assert image["render_job_snapshot"]["id"] == render_job["id"]
    assert image["sampling"] == source_sampling
    assert image["shape"][:2] == render_job["request"]["shape"]

    timeline = sequences.create_sequence(service, "scene_sequences", {
        **PROVENANCE,
        "name": "One published 3D source at both static knots",
        "revision": "halo-1",
        "source_uri": "scene-render://registered-halo-static-timeline",
        "method_and_version": "published_scene_render_static_timeline:1",
        "payload": {
            "schema": "timed_spectral_scene_v1",
            "clock": {
                "clock_id": "registered-halo-clock", "unit": "ns", "reference": "relative",
                "origin": "Authored simulation time zero; no hardware acquisition clock",
            },
            "interpolation": "linear_irradiance",
            "knots": [
                {"time_ns": 0, "spectral_image_id": image["id"]},
                {"time_ns": 80_000_000, "spectral_image_id": image["id"]},
            ],
        },
    })
    assert [k["spectral_image_id"] for k in timeline["payload"]["knots"]] == [
        image["id"], image["id"],
    ]
    assert timeline["image_snapshots"][0]["render_job_snapshot"]["id"] == render_job["id"]

    control_data = sequences.control_template(service, assembly["id"], timeline["id"])
    for index, frame in enumerate(control_data["payload"]["frames"]):
        frame["applied"]["exposure_ns"] = (4 if index % 2 == 0 else 8) * 1_000_000
    control = sequences.create_sequence(service, "control_sequences", control_data)
    program = capture_programs.create(service, capture_programs.template(service, assembly["id"]))
    specification = sequences.test_template(
        service, assembly["id"], timeline["id"], control["id"], program["id"],
    )
    assert specification["stimulus"]["capture_crop"] == capture_crop
    assert specification["stimulus"]["source_support"] == {
        "kind": source_sampling["kind"],
        "full_sensor_shape": source_sampling["full_sensor_shape"],
        "crop_origin": source_sampling["crop_origin"],
        "shape": image["shape"][:2],
    }
    test = service.create_test(specification)
    submission = {
        "assembly_id": assembly["id"], "test_id": test["id"],
        "scene_sequence_id": timeline["id"], "control_sequence_id": control["id"],
        "capture_program_id": program["id"], "plugin_id": "hdr_bracket_v1",
        "parameters": {"seed": 927},
    }
    wrong_crop = copy.deepcopy(submission)
    wrong_crop["parameters"]["capture_crop"] = {"crop_origin": [0, 0], "shape": [8, 8]}
    with pytest.raises(ValueError, match="differs from the frozen halo render"):
        service.submit_run(wrong_crop)
    queued = service.submit_run(submission)
    assert queued["parameters"]["capture_crop"] == capture_crop
    assert queued["scene_sequence_snapshot"]["id"] == timeline["id"]
    assert queued["test_snapshot"]["stimulus"]["source_support"] == (
        test["stimulus"]["source_support"]
    )
    run = test_scene_render.finish(service, queued)
    assert run["status"] == "succeeded"
    assert run["result"]["summary"]["capture_program_hash"] == program["content_hash"]
    assert run["result"]["summary"]["physical_validation"] == "not_established"

    def array(name):
        artifact = next(a for a in run["artifacts"] if a["name"] == name + ".npy")
        return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)

    raw, rgb = array("raw_frames"), array("rgb_frames")
    assert array("channel_photoelectron_rate_scenes").max() > 0
    assert raw.shape == (len(control["payload"]["frames"]), 8, 8)
    assert rgb.shape == (*raw.shape, 3)
    assert raw.dtype.kind in "iu" and np.isfinite(rgb).all()
    assert raw.mean() > sensor["payload"]["readout"]["black_level_dn"]
