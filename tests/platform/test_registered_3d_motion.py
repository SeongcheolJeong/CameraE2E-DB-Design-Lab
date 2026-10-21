"""Actual time-sampled 3D occlusion through registered HDR RAW and ISP."""

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
    scene_projection,
    scene_render,
    sequences,
)
from camera_platform.contracts import AssemblyCreate
from camera_platform.demo import PROVENANCE
from camera_platform.evidence import build_bundle
from camera_platform.scene_contracts import Instance, Pose, SceneAssetCreate, SceneRenderCreate
from camera_platform.scene_motion import resolve

CLOCK = {
    "clock_id": "sampled-3d-motion-clock", "unit": "ns", "reference": "relative",
    "origin": "Authored simulation zero at the base 3D poses; no hardware clock",
}
KNOTS_NS = (0, 40_000_000, 80_000_000)


def _asset(service, name, payload):
    return scene_render.create_asset(service, {**PROVENANCE, "name": name, "payload": payload})


def _array(service, run, name):
    artifact = next(a for a in run["artifacts"] if a["name"] == name + ".npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


def _scalar_linear_weights(start_ns, end_ns, knots_ns):
    """Independent integral of piecewise-linear sample basis over one row."""
    result = [0.0] * len(knots_ns)
    for index, (left, right) in enumerate(zip(knots_ns, knots_ns[1:], strict=False)):
        low, high = max(start_ns, left), min(end_ns, right)
        if high <= low:
            continue
        right_weight = ((high - left) ** 2 - (low - left) ** 2) / (
            2 * (right - left) * 1e9
        )
        result[index] += (high - low) * 1e-9 - right_weight
        result[index + 1] += right_weight
    return result


def _verify_bundle_hashes(service, kind, job):
    with zipfile.ZipFile(build_bundle(service, kind, job["id"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["run"]["id"] == job["id"]
        for artifact in job["artifacts"]:
            data = archive.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(data).hexdigest() == artifact["sha256"]


def _hdr_run(service, assembly, images, clock, name, *, timeline=None):
    if timeline is None:
        timeline = sequences.create_sequence(service, "scene_sequences", {
            **PROVENANCE,
            "name": name, "revision": "1",
            "source_uri": "scene-render://" + name,
            "method_and_version": "sampled_3d_scene_to_registered_irradiance_v1",
            "payload": {
                "schema": "timed_spectral_scene_v1", "clock": clock,
                "interpolation": "linear_irradiance",
                "knots": [
                    {"time_ns": time, "spectral_image_id": image["id"]}
                    for time, image in zip(KNOTS_NS, images, strict=True)
                ],
            },
        })
    control_data = sequences.control_template(service, assembly["id"], timeline["id"])
    for index, frame in enumerate(control_data["payload"]["frames"]):
        frame["applied"]["exposure_ns"] = (4 if index % 2 == 0 else 8) * 1_000_000
    control = sequences.create_sequence(service, "control_sequences", control_data)
    program_data = capture_programs.template(service, assembly["id"])
    program_data["payload"]["hdr_merge"] = None  # Moving geometry is no-fusion acquisition.
    program = capture_programs.create(service, program_data)
    test = service.create_test(sequences.test_template(
        service, assembly["id"], timeline["id"], control["id"], program["id"],
    ))
    queued = service.submit_run({
        "assembly_id": assembly["id"], "test_id": test["id"],
        "scene_sequence_id": timeline["id"], "control_sequence_id": control["id"],
        "capture_program_id": program["id"], "plugin_id": "hdr_bracket_v1",
        "parameters": {"seed": 419},
    })
    run = test_scene_render.finish(service, queued)
    assert run["status"] == "succeeded"
    return timeline, control, program, run


def test_motion_contract_requires_clock_time_and_bounded_translation():
    assert _scalar_linear_weights(0, 500_000_000, (0, 1_000_000_000)) == [0.375, 0.125]
    instance = {
        "geometry_id": "geometry", "material_id": "material",
        "motion": {"model": "linear_translation_v1", "velocity_m_s": [0.25, 0.0, 0.0]},
    }
    scene = {
        "kind": "scene", "coordinate_system": "right_handed_y_up_metres",
        "instances": [instance],
    }
    with pytest.raises(ValueError, match="explicit scene clock"):
        SceneAssetCreate.model_validate({**PROVENANCE, "name": "missing clock", "payload": scene})
    with pytest.raises(ValueError, match="bounded m/s velocity"):
        SceneAssetCreate.model_validate({**PROVENANCE, "name": "invalid motion", "payload": {
            **scene, "clock": CLOCK, "instances": [{
                **instance, "motion": {"model": "linear_translation_v1",
                                       "velocity_m_s": [1001.0, 0.0, 0.0]},
            }],
        }})
    render_spec = {
        "name": "incomplete time", "scene_asset_id": "scene", "assembly_id": "camera",
        "pose": test_scene_render.basic_request()["pose"],
        "projection": {
            **PROVENANCE, "projection_model": "paraxial_on_axis_projection_v1",
            "focal_length_mm": 4.0, "f_number": 8.0,
            "on_axis_transmittance": test_scene_render.spec([0.8, 0.8, 0.8]),
            "geometry_model": "pinhole_pixel_centre",
            "irradiance_model": "pi_L_T_over_4N2_before_relative_illumination",
            "approximation_uncertainty": "unknown",
        },
    }
    with pytest.raises(ValueError, match="both render_clock and render_time_ns"):
        SceneRenderCreate.model_validate(render_spec | {"render_time_ns": 0})
    for coerced_time in (1.0, "1", True):
        with pytest.raises(ValueError, match="render_time_ns"):
            SceneRenderCreate.model_validate(render_spec | {
                "render_clock": CLOCK, "render_time_ns": coerced_time,
            })
    with pytest.raises(ValueError):
        Pose.model_validate({
            **test_scene_render.basic_request()["pose"],
            "origin_m": [float("nan"), 0.0, 0.0],
        })
    with pytest.raises(ValueError):
        Instance.model_validate({
            "geometry_id": "geometry", "material_id": "material",
            "to_world": [
                [1.0, 0.0, 0.0, float("nan")], [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0],
            ],
        })
    camera = {
        "origin_m": [0.0, 0.0, 0.0], "target_m": [0.0, 0.0, 3.0],
        "up": [0.0, 1.0, 0.0],
        "motion": {"model": "linear_translation_v1", "velocity_m_s": [0.25, 0.0, 0.0]},
    }
    frozen = resolve(
        {"pose": camera, "render_clock": CLOCK, "render_time_ns": 40_000_000},
        {"payload": {
            "kind": "scene", "coordinate_system": "right_handed_y_up_metres",
            "instances": [], "environment_light_id": None, "point_lights": [],
        }},
    )
    assert frozen["pose"]["origin_m"] == [0.01, 0.0, 0.0]
    assert frozen["pose"]["target_m"] == [0.01, 0.0, 3.0]
    assert frozen["evidence"]["moving_camera"] is True


def test_moving_3d_occluder_registered_hdr_worker_and_static_control(tmp_path, monkeypatch):
    if not test_scene_render.PYTHON.exists():
        pytest.skip("Isolated Mitsuba environment is not installed")
    service, assembly, request = test_scene_render.scene_workflow.__wrapped__(
        tmp_path, monkeypatch
    )
    original_lens = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "lens"
    )
    lens = factory.publish(service, {
        "job_id": original_lens["psf_factory_job_id"],
        "base_lens_model_id": original_lens["parents"][0],
        "name": "Sampled 3D motion Lens", "revision": "motion-1",
        "conditions": assembly["conditions"], "condition_scope": "optical_response",
    })
    assembly_data = {
        key: copy.deepcopy(value) for key, value in assembly.items()
        if key in AssemblyCreate.model_fields
    }
    assembly_data.update(
        name="Sampled 3D motion HDR camera", revision="motion-1",
        model_ids=[lens["id"] if i == original_lens["id"] else i
                   for i in assembly["model_ids"]],
    )
    assembly = service.create_assembly(assembly_data)
    assert assembly["compatibility"]["status"] == "PASS"
    _, sensor, _, full = imaging._native(service, assembly["id"])
    origin = [(side - 8) // 2 for side in full]

    obstacle = _asset(service, "Moving opaque 3D rectangle", {
        "kind": "geometry", "unit": "m", "primitive": "triangle_mesh",
        "vertices": [
            [-0.01, -0.1, 1.0], [0.0, -0.1, 1.0],
            [0.0, 0.1, 1.0], [-0.01, 0.1, 1.0],
        ],
        "faces": [[0, 1, 2], [0, 2, 3]],
    })
    black = _asset(service, "Opaque black synthetic surface", {
        "kind": "material", "bsdf": "diffuse",
        "spectrum": test_scene_render.spec([0.0, 0.0, 0.0]),
        "surface_state": "synthetic opaque matte surface",
    })
    original_scene = service.registry.get("scene_assets", request["scene_asset_id"])
    assert "clock" not in original_scene["payload"]
    assert all("motion" not in item for item in original_scene["payload"]["instances"])
    scene_payload = copy.deepcopy(original_scene["payload"])
    obstacle_instance = {
        "geometry_id": obstacle["id"], "material_id": black["id"],
        "to_world": [
            [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0],
        ],
        "motion": {"model": "linear_translation_v1", "velocity_m_s": [0.25, 0.0, 0.0]},
    }
    scene_payload["instances"].append(obstacle_instance)
    scene_payload["clock"] = CLOCK
    moving_scene = _asset(service, "Spectral plane with translated opaque occluder", scene_payload)
    assert moving_scene["illumination_id"] == original_scene["illumination_id"]

    request.update(
        scene_asset_id=moving_scene["id"], assembly_id=assembly["id"],
        shape=[8, 8], crop_origin=origin, render_psf_halo=True,
        samples_per_pixel=2, render_clock=CLOCK,
    )
    with pytest.raises(ValueError, match="scene clock"):
        scene_render.submit(service, {k: v for k, v in request.items()
                                      if k != "render_clock"})
    with pytest.raises(ValueError, match="scene clock"):
        scene_render.submit(service, request | {
            "render_clock": {**CLOCK, "clock_id": "unrelated-clock"},
            "render_time_ns": 0,
        })
    with pytest.raises(ValueError, match="displacement exceeds"):
        scene_render.submit(service, request | {"render_time_ns": 8_000_000_000_000_000})

    images, render_jobs, occupied = [], [], []
    for time_ns in KNOTS_NS:
        rendered = test_scene_render.finish(service, scene_render.submit(
            service, request | {"render_time_ns": time_ns}
        ))
        render_jobs.append(rendered)
        assert rendered["request"]["scene"]["id"] == moving_scene["id"]
        motion = rendered["result"]["summary"]["scene_motion"]
        assert motion == rendered["request"]["scene_motion_evidence"]
        assert motion["render_time_ns"] == time_ns and motion["render_clock"] == CLOCK
        assert motion["between_knot_geometry"] == "not_evaluated_by_renderer"
        assert rendered["request"]["resolved_scene"]["payload"]["instances"][1][
            "to_world"
        ][0][3] == pytest.approx(0.25 * time_ns * 1e-9, abs=1e-15)
        obj = _array(service, rendered, "first-hit-object-index")
        occupied.append(obj == 1)
        images.append(scene_render.publish(service, {
            "job_id": rendered["id"], "name": f"3D occlusion at {time_ns} ns",
        }))
    assert not np.array_equal(occupied[0], occupied[1])
    assert not np.array_equal(occupied[1], occupied[2])
    assert occupied[0].sum() > 0 and occupied[1].sum() > 0
    assert np.where(occupied[0])[1].mean() < np.where(occupied[1])[1].mean()
    assert images[0]["sampling"] == images[1]["sampling"] == images[2]["sampling"]
    assert images[0]["shape"] == images[1]["shape"] == images[2]["shape"]
    camera_request = copy.deepcopy(request)
    camera_request["pose"]["motion"] = {
        "model": "linear_translation_v1", "velocity_m_s": [0.25, 0.0, 0.0],
    }
    camera_render = test_scene_render.finish(service, scene_render.submit(
        service, camera_request | {"render_time_ns": KNOTS_NS[1]}
    ))
    assert camera_render["request"]["scene_motion_evidence"]["moving_camera"] is True
    assert camera_render["request"]["resolved_pose"]["origin_m"] == [0.01, 0.0, 0.0]
    assert not np.array_equal(
        _array(service, camera_render, "first-hit-object-index"), occupied[1]
    )

    projection_draft = scene_projection.template(
        service, render_jobs[1]["id"], assembly["id"]
    )["request"]
    projection_draft["projection"] = copy.deepcopy(render_jobs[1]["request"]["projection"])
    projection_plan = scene_projection.preview(service, projection_draft)
    assert projection_plan["status"] == "ready", projection_plan["issues"]
    projection_job = test_scene_render.finish(service, scene_projection.submit(service, {
        "draft": projection_draft, "preview_sha256": projection_plan["preview_sha256"],
    }))
    projected = scene_projection.publish(service, {
        "job_id": projection_job["id"], "name": "Time-preserving stored radiance projection",
    })
    assert projected["source_render_snapshot"]["id"] == render_jobs[1]["id"]
    assert projected["source_render_snapshot"]["request"]["render_time_ns"] == KNOTS_NS[1]

    payload = {
        **PROVENANCE, "name": "Registered moving 3D occlusion", "revision": "motion-1",
        "payload": {
            "schema": "timed_spectral_scene_v1", "clock": CLOCK,
            "interpolation": "linear_irradiance",
            "knots": [{"time_ns": time, "spectral_image_id": image["id"]}
                      for time, image in zip(KNOTS_NS, images, strict=True)],
        },
    }
    with pytest.raises(ValueError, match="time/clock differs"):
        wrong_time = copy.deepcopy(payload)
        wrong_time["payload"]["knots"][1]["spectral_image_id"] = images[0]["id"]
        sequences.create_sequence(service, "scene_sequences", wrong_time)
    with pytest.raises(ValueError, match="time/clock differs"):
        wrong_clock = copy.deepcopy(payload)
        wrong_clock["payload"]["clock"] = {**CLOCK, "clock_id": "unrelated-clock"}
        sequences.create_sequence(service, "scene_sequences", wrong_clock)
    with pytest.raises(ValueError, match="time/clock differs"):
        projected_wrong_time = copy.deepcopy(payload)
        projected_wrong_time["payload"]["knots"][0]["spectral_image_id"] = projected["id"]
        sequences.create_sequence(service, "scene_sequences", projected_wrong_time)
    with pytest.raises(ValueError, match="time/clock differs"):
        projected_wrong_clock = copy.deepcopy(payload)
        projected_wrong_clock["payload"]["knots"][1]["spectral_image_id"] = projected["id"]
        projected_wrong_clock["payload"]["clock"] = {
            **CLOCK, "clock_id": "projection-unrelated-clock",
        }
        sequences.create_sequence(service, "scene_sequences", projected_wrong_clock)
    projected_correct_time = copy.deepcopy(payload)
    projected_correct_time["payload"]["knots"][1]["spectral_image_id"] = projected["id"]
    asserted = sequences.create_sequence(service, "scene_sequences", projected_correct_time)
    assert asserted["image_snapshots"][1]["id"] == projected["id"]

    timeline, control, program, run = _hdr_run(
        service, assembly, images, CLOCK, "moving-published-3d"
    )
    assert [item["id"] for item in timeline["image_snapshots"]] == [
        item["id"] for item in images
    ]
    assert run["result"]["summary"]["physical_validation"] == "not_established"
    raw, rgb = _array(service, run, "raw_frames"), _array(service, run, "rgb_frames")
    rates = _array(service, run, "channel_photoelectron_rate_scenes")
    weights = _array(service, run, "row_scene_integration_weights_s")
    electrons = _array(service, run, "mean_electrons")
    cfa = _array(service, run, "cfa_channel_index")
    starts = _array(service, run, "row_start_ns")
    ends = _array(service, run, "row_end_ns")
    assert raw.dtype.kind in "iu" and raw.shape[1:] == (8, 8)
    assert rgb.shape == (*raw.shape, 3) and np.isfinite(rgb).all()
    assert rates.shape[0] == 3 and not np.array_equal(rates[0], rates[1])
    mode = control["payload"]["mode"]
    for frame_index, frame in enumerate(control["payload"]["frames"]):
        for row in range(8):
            first = frame["frame_start_ns"] + (
                mode["hidden_top_rows"] + origin[0] + row
            ) * mode["line_time_ns"]
            last = first + frame["applied"]["exposure_ns"]
            assert starts[frame_index, row] == first
            assert ends[frame_index, row] == last
            scalar_weights = _scalar_linear_weights(first, last, KNOTS_NS)
            np.testing.assert_allclose(
                weights[frame_index, :, row], scalar_weights, rtol=1e-13, atol=1e-15
            )
            assert sum(scalar_weights) == pytest.approx(
                frame["applied"]["exposure_ns"] * 1e-9, rel=1e-13
            )
            for col in range(8):
                channel = int(cfa[row, col])
                expected = sum(scalar_weights[k] * rates[k, row, col, channel]
                               for k in range(3))
                assert electrons[frame_index, row, col] == pytest.approx(
                    expected, rel=1e-12, abs=1e-9
                )
    assert not np.array_equal(raw[0], raw[2])
    assert run["result"]["summary"]["scene_interpolation"] == "linear_irradiance"
    assert control["payload"]["clock"] == timeline["payload"]["clock"]
    assert program["payload"]["hdr_merge"] is None
    json.dumps(run["result"]["summary"], allow_nan=False)
    assert sensor["payload"]["readout"]["black_level_dn"] < raw.max()
    for rendered in render_jobs:
        _verify_bundle_hashes(service, "scene_renders", rendered)
    _verify_bundle_hashes(service, "scene_renders", camera_render)
    _verify_bundle_hashes(service, "scene_projections", projection_job)
    _verify_bundle_hashes(service, "runs", run)

    # Both endpoint renders alone miss the occluder's actual intermediate
    # positions. Render those positions again in 3D, then send the finer
    # irradiance series through the same registered rolling RAW/ISP Worker.
    planned = scene_motion_sampling.plan_times(0, 80_000_000, 20_000_000)
    assert planned == [0, 20_000_000, 40_000_000, 60_000_000, 80_000_000]
    middle_queues = scene_motion_sampling.submit_renders(
        service, request, [20_000_000, 60_000_000]
    )
    middle_jobs = [test_scene_render.finish(service, item) for item in middle_queues]
    by_time = dict(zip(KNOTS_NS, render_jobs, strict=True))
    by_time.update({20_000_000: middle_jobs[0], 60_000_000: middle_jobs[1]})
    with pytest.raises(ValueError, match="one frozen 3D scene"):
        scene_motion_sampling.publish_sequence(
            service, [render_jobs[0]["id"], camera_render["id"], render_jobs[2]["id"]],
            name="Different moving camera must not mix",
        )
    coarse = scene_motion_sampling.publish_sequence(
        service, [by_time[t]["id"] for t in (0, 80_000_000)],
        name="Endpoint-only moving 3D scene",
    )
    fine = scene_motion_sampling.publish_sequence(
        service, [by_time[t]["id"] for t in planned],
        name="Intermediate-render moving 3D scene",
    )
    diagnostic = scene_motion_sampling.compare_refinement(service, coarse["id"], fine["id"])
    assert diagnostic["relative_l1_change"] > 0
    assert diagnostic["integral_unit"] == "J/m^2/nm"
    assert "not an occlusion-error bound" in diagnostic["interpretation"]
    ten_ms = scene_motion_sampling.plan_times(0, 80_000_000, 10_000_000)
    extra_queues = scene_motion_sampling.submit_renders(
        service, request, [10_000_000, 30_000_000, 50_000_000, 70_000_000]
    )
    extra_jobs = [test_scene_render.finish(service, item) for item in extra_queues]
    by_time.update(dict(zip(
        [10_000_000, 30_000_000, 50_000_000, 70_000_000], extra_jobs, strict=True
    )))
    finer = scene_motion_sampling.publish_sequence(
        service, [by_time[t]["id"] for t in ten_ms],
        name="Ten-ms intermediate-render moving 3D scene",
    )
    refined = scene_motion_sampling.compare_refinement(service, fine["id"], finer["id"])
    assert refined["relative_l1_change"] < diagnostic["relative_l1_change"]
    _, _, _, coarse_run = _hdr_run(
        service, assembly, [], CLOCK, "endpoint-only-3d", timeline=coarse
    )
    _, _, _, fine_run = _hdr_run(
        service, assembly, [], CLOCK, "intermediate-3d", timeline=fine
    )
    coarse_electrons = _array(service, coarse_run, "mean_electrons")
    fine_electrons = _array(service, fine_run, "mean_electrons")
    assert np.max(np.abs(fine_electrons - coarse_electrons)) > 0
    starts = _array(service, fine_run, "row_start_ns")
    ends = _array(service, fine_run, "row_end_ns")
    exposure_windows = [(int(start), int(end)) for start, end in
                        zip(starts.ravel(), ends.ravel(), strict=True)]
    row_diagnostic = scene_motion_sampling.compare_refinement(
        service, coarse["id"], fine["id"], windows_ns=exposure_windows
    )
    assert row_diagnostic["exposure_window_count"] == starts.size
    assert row_diagnostic["maximum_window_pixel_change"] > 0
    assert not np.array_equal(_array(service, coarse_run, "raw_frames"),
                              _array(service, fine_run, "raw_frames"))
    assert fine_run["result"]["summary"]["scene_interpolation"] == "linear_irradiance"
    _verify_bundle_hashes(service, "runs", fine_run)

    # A static 3D revision at the same initial geometry is a negative control.
    static_payload = copy.deepcopy(scene_payload)
    static_payload["instances"][-1].pop("motion")
    static_payload.pop("clock")
    static_scene = _asset(service, "Identical initial 3D geometry held fixed", static_payload)
    static_request = {k: v for k, v in request.items()
                      if k not in {"render_clock", "render_time_ns"}}
    static_request["scene_asset_id"] = static_scene["id"]
    static_render = test_scene_render.finish(service, scene_render.submit(service, static_request))
    static_image = scene_render.publish(service, {
        "job_id": static_render["id"], "name": "Held initial geometry",
    })
    _, _, _, held = _hdr_run(
        service, assembly, [static_image] * 3, CLOCK, "held-3d-negative-control"
    )
    static_raw = _array(service, held, "raw_frames")
    static_rates = _array(service, held, "channel_photoelectron_rate_scenes")
    static_electrons = _array(service, held, "mean_electrons")
    assert static_rates.shape[0] == 1
    np.testing.assert_array_equal(static_rates[0], rates[0])
    assert not np.array_equal(electrons[2], static_electrons[2])
    assert not np.array_equal(raw[2], static_raw[2])
    assert held["result"]["summary"]["physical_validation"] == "not_established"
