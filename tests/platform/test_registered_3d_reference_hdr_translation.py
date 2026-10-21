"""A registered moving 3D scene reaches reference HDR through native RAW."""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile

import numpy as np
import pytest
import test_scene_render

from camera_platform import (
    capture_programs,
    factory,
    imaging,
    scene_motion_sampling,
    scene_render,
    sequences,
)
from camera_platform.contracts import AssemblyCreate
from camera_platform.demo import PROVENANCE
from camera_platform.evidence import build_bundle
from camera_platform.sdk import PlatformClient

CLOCK = {
    "clock_id": "reference-hdr-moving-3d-clock",
    "unit": "ns",
    "reference": "relative",
    "origin": "Authored simulation zero; no measured or hardware clock",
}
TIMES_NS = [0, 40_000_000, 80_000_000]


def _array(service, run, name):
    artifact = next(item for item in run["artifacts"] if item["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


def test_moving_3d_occluder_reaches_registered_reference_hdr_with_native_roi(
    tmp_path, monkeypatch,
):
    if not test_scene_render.PYTHON.exists():
        pytest.skip("Isolated Mitsuba environment is not installed")
    service, assembly, request = test_scene_render.scene_workflow.__wrapped__(
        tmp_path, monkeypatch,
    )
    original_lens = next(
        service.registry.get("models", identity) for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "lens"
    )
    lens = factory.publish(service, {
        "job_id": original_lens["psf_factory_job_id"],
        "base_lens_model_id": original_lens["parents"][0],
        "name": "Moving 3D reference HDR optical Lens",
        "revision": "1",
        "conditions": assembly["conditions"],
        "condition_scope": "optical_response",
    })
    assembly_data = {
        key: copy.deepcopy(value) for key, value in assembly.items()
        if key in AssemblyCreate.model_fields
    }
    assembly_data.update(
        name="Moving 3D reference HDR camera", revision="1",
        model_ids=[lens["id"] if identity == original_lens["id"] else identity
                   for identity in assembly["model_ids"]],
    )
    assembly = service.create_assembly(assembly_data)
    assert assembly["compatibility"]["status"] == "PASS"
    _, _, _, full = imaging._native(service, assembly["id"])
    receiver_origin = [(side - 12) // 2 for side in full]
    receiver = {"crop_origin": receiver_origin, "shape": [12, 12]}

    obstacle = scene_render.create_asset(service, {
        **PROVENANCE, "name": "Moving opaque reference HDR occluder",
        "payload": {
            "kind": "geometry", "unit": "m", "primitive": "triangle_mesh",
            "vertices": [
                [-0.01, -0.1, 1.0], [0.0, -0.1, 1.0],
                [0.0, 0.1, 1.0], [-0.01, 0.1, 1.0],
            ],
            "faces": [[0, 1, 2], [0, 2, 3]],
        },
    })
    black = scene_render.create_asset(service, {
        **PROVENANCE, "name": "Opaque zero-radiance surface",
        "payload": {
            "kind": "material", "bsdf": "diffuse",
            "spectrum": test_scene_render.spec([0.0, 0.0, 0.0]),
            "surface_state": "authored synthetic matte surface",
        },
    })
    source_scene = service.registry.get("scene_assets", request["scene_asset_id"])
    scene_payload = copy.deepcopy(source_scene["payload"])
    scene_payload["clock"] = CLOCK
    scene_payload["instances"].append({
        "geometry_id": obstacle["id"], "material_id": black["id"],
        "to_world": [
            [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0],
        ],
        "motion": {"model": "linear_translation_v1", "velocity_m_s": [0.25, 0.0, 0.0]},
    })
    moving_scene = scene_render.create_asset(service, {
        **PROVENANCE, "name": "Reference HDR moving occluder scene",
        "payload": scene_payload,
    })
    request.update(
        assembly_id=assembly["id"], scene_asset_id=moving_scene["id"],
        shape=[12, 12], crop_origin=receiver_origin, render_psf_halo=True,
        samples_per_pixel=2, render_clock=CLOCK,
    )
    queued_renders = scene_motion_sampling.submit_renders(service, request, TIMES_NS)
    render_jobs = [test_scene_render.finish(service, item) for item in queued_renders]
    first_hits = [_array(service, job, "first-hit-object-index") for job in render_jobs]
    assert all(job["status"] == "succeeded" for job in render_jobs)
    assert not np.array_equal(first_hits[0], first_hits[1])
    assert not np.array_equal(first_hits[1], first_hits[2])
    for time_ns, job in zip(TIMES_NS, render_jobs, strict=True):
        assert job["request"]["scene_motion_evidence"]["render_time_ns"] == time_ns
        assert job["result"]["summary"]["scene_motion"] == (
            job["request"]["scene_motion_evidence"]
        )
    scene = scene_motion_sampling.publish_sequence(
        service, [job["id"] for job in render_jobs], name="Moving 3D reference HDR knots",
    )
    control_data = sequences.control_template(service, assembly["id"], scene["id"])
    control_data["payload"]["mode"].update(
        shutter="global", line_time_ns=0, hidden_top_rows=0,
    )
    for index, frame in enumerate(control_data["payload"]["frames"]):
        frame["applied"]["exposure_ns"] = (4 if index % 2 == 0 else 8) * 1_000_000
    control = sequences.create_sequence(service, "control_sequences", control_data)
    program_data = capture_programs.template(
        service, assembly["id"], mode="reference_guarded_translation",
    )
    registration = program_data["payload"]["hdr_merge"]["registration"]
    registration.update(max_shift_px=2, minimum_common_pixels=32)
    program = capture_programs.create(service, program_data)
    test = service.create_test(sequences.test_template(
        service, assembly["id"], scene["id"], control["id"], program["id"],
        capture_crop=receiver,
    ))
    sdk = PlatformClient.__new__(PlatformClient)

    def submit_local(path, payload):
        assert path == "runs"
        return service.submit_run(payload)

    sdk.post = submit_local
    queued = sdk.submit_reference_hdr(
        assembly["id"], test["id"], scene["id"], control["id"], program["id"],
        seed=341, capture_crop=receiver,
    )
    assert queued["parameters"]["capture_crop"] == receiver
    run = test_scene_render.finish(service, queued)
    assert run["status"] == "succeeded"
    summary = run["result"]["summary"]
    assert summary["physical_validation"] == "not_established"
    assert summary["scene_interpolation"] == "linear_irradiance"
    assert summary["geometry"]["crop_origin"] == receiver_origin
    assert summary["prediction"]["sample_geometry"] == {
        key: test["stimulus"][key] for key in ("capture_crop", "source_support")
    }
    assert summary["hdr_merge"]["schema"] == (
        "camera_reference_hdr_merge_result_global_translation_v1"
    )
    raw = _array(service, run, "raw_frames")
    rates = _array(service, run, "channel_photoelectron_rate_scenes")
    assert raw.dtype.kind in "iu" and raw.shape[1:] == (12, 12)
    assert rates.shape[0] == len(TIMES_NS)
    assert not np.array_equal(rates[0], rates[1])
    assert not np.array_equal(raw[0], raw[-1])
    reference_valid = _array(service, run, "hdr_reference_valid_mask")
    weights = _array(service, run, "hdr_weights")
    merged = _array(service, run, "hdr_merged_rate")
    exposure_rates = _array(service, run, "hdr_exposure_rate")
    fallback = _array(service, run, "hdr_reference_fallback_mask")
    unknown = _array(service, run, "hdr_reference_only_unknown_mask")
    contributions = _array(service, run, "hdr_registration_contribution_mask")
    index = summary["hdr_merge"]["reference_frame_index"]
    np.testing.assert_allclose(weights.sum(axis=0)[reference_valid], 1.0)
    np.testing.assert_allclose(weights.sum(axis=0)[~reference_valid], 0.0)
    np.testing.assert_allclose(
        merged[fallback | unknown], exposure_rates[index][fallback | unknown],
    )
    assert np.isnan(merged[~reference_valid]).all()
    np.testing.assert_array_equal(contributions[index], reference_valid)
    for shift in summary["hdr_merge"]["registration"]["shifts"]:
        if shift["status"] not in {"reference", "identity_best", "translated"}:
            assert not contributions[shift["frame_index"]].any()
        if shift["status"] == "translated":
            assert all(abs(offset) <= 2 and offset % 2 == 0
                       for offset in shift["source_offset_yx_px"])
    json.dumps(summary, allow_nan=False)
    with zipfile.ZipFile(build_bundle(service, "runs", run["id"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["scene_sequence_snapshot"]["id"] == scene["id"]
        assert manifest["run"]["capture_program_snapshot"]["id"] == program["id"]
        artifact = next(item for item in run["artifacts"]
                        if item["name"] == "hdr_merged_rate.npy")
        content = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
        assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
