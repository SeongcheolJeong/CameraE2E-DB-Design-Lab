"""Two real 3D emitter passes, exact geometry oracle and frozen timeline binding."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile

import numpy as np
import pytest
import test_scene_render
from test_scene_render import scene_workflow as scene_workflow  # noqa: F401

from camera_platform import factory, scene_motion_sampling, scene_render
from camera_platform.contracts import AssemblyCreate
from camera_platform.demo import PROVENANCE
from camera_platform.evidence import build_bundle
from camera_platform.service import PlatformService

CLOCK = {
    "clock_id": "dual-3d-light-clock", "unit": "ns", "reference": "relative",
    "origin": "Authored zero of a synthetic moving occluder, not a camera clock",
}
TIMES_NS = [0, 40_000_000]
SPECTRA = ([0.005, 0.007, 0.009], [0.010, 0.006, 0.004])


def _asset(service, name, payload):
    return scene_render.create_asset(service, {**PROVENANCE, "name": name, "payload": payload})


def _array(service, job, name):
    matching = [a for a in job["artifacts"] if a["name"] == name + ".npy"]
    assert len(matching) == 1
    return np.load(io.BytesIO(service.registry.blob_path(
        matching[0]["sha256"]).read_bytes()), allow_pickle=False)


def _setup(service, assembly, request):
    rectangle = _asset(service, "Shared dual-light emitter rectangle", {
        "kind": "geometry", "unit": "m", "primitive": "rectangle",
    })
    matte = _asset(service, "Perfectly black synthetic emitter substrate", {
        "kind": "material", "bsdf": "diffuse",
        "spectrum": test_scene_render.spec([0., 0., 0.]),
        "surface_state": "synthetic zero-reflectance path-isolation oracle",
    })
    lights = [_asset(service, f"3D component {index}", {
        "kind": "light", "distribution": "area",
        "spectrum": test_scene_render.spec(list(values)),
        "unit": "W/m^2/sr/nm", "time_model": "static",
    }) for index, values in enumerate(SPECTRA)]
    blocker = _asset(service, "Moving black foreground mesh", {
        "kind": "geometry", "unit": "m", "primitive": "triangle_mesh",
        "vertices": [[-0.004, -0.1, 1.], [0., -0.1, 1.],
                     [0., 0.1, 1.], [-0.004, 0.1, 1.]],
        "faces": [[0, 1, 2], [0, 2, 3]],
    })
    scene = _asset(service, "Frozen moving two-area-light scene", {
        "kind": "scene", "coordinate_system": "right_handed_y_up_metres",
        "clock": CLOCK,
        "instances": [
            {"geometry_id": rectangle["id"], "material_id": matte["id"],
             "area_light_id": light["id"],
             "to_world": [[-0.015, 0., 0., x], [0., 0.03, 0., 0.],
                          [0., 0., -1., 3.], [0., 0., 0., 1.]]}
            for light, x in zip(lights, (-0.015, 0.015), strict=True)
        ] + [
            {"geometry_id": blocker["id"], "material_id": matte["id"],
             "to_world": [[1., 0., 0., 0.], [0., 1., 0., 0.],
                          [0., 0., 1., 0.], [0., 0., 0., 1.]],
             "motion": {"model": "linear_translation_v1",
                        "velocity_m_s": [0.25, 0., 0.]}},
        ],
        "environment_light_id": None, "point_lights": [],
    })
    original_lens = next(
        service.registry.get("models", identity) for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "lens"
    )
    conditions = copy.deepcopy(assembly["conditions"])
    conditions["light_id"] = {"value": scene["illumination_id"], "unit": "1"}
    lens = factory.publish(service, {
        "job_id": original_lens["psf_factory_job_id"],
        "base_lens_model_id": original_lens["parents"][0],
        "name": "Dual-light bound declared pupil", "revision": "dual-1",
        "conditions": conditions,
    })
    fields = {key: copy.deepcopy(value) for key, value in assembly.items()
              if key in AssemblyCreate.model_fields}
    fields.update(
        name="Dual-light bound synthetic camera", revision="dual-1",
        conditions=conditions,
        model_ids=[lens["id"] if identity == original_lens["id"] else identity
                   for identity in assembly["model_ids"]],
    )
    bound = service.create_assembly(fields)
    assert bound["compatibility"]["status"] == "PASS"
    base = {**request, "scene_asset_id": scene["id"], "assembly_id": bound["id"],
            "render_clock": CLOCK, "samples_per_pixel": 2}
    return scene, lights, base


def test_real_dual_3d_area_emitters_preserve_occlusion_sum_and_frozen_pair(
    scene_workflow,
):
    service, assembly, request = scene_workflow
    scene, lights, base = _setup(service, assembly, request)
    light_ids = [light["id"] for light in lights]
    assert scene_render.placed_light_ids(scene["payload"]) == set(light_ids)
    assert scene["illumination_id"] == service.registry.get(
        "scene_assets", scene["id"]
    )["illumination_id"]
    with pytest.raises(ValueError, match="Selected Light must be placed"):
        scene_render.submit(service, {**base, "render_time_ns": 0,
                                      "light_component_id": "unplaced-light"})
    jobs = {identity: [] for identity in [*light_ids, "all"]}
    first_hits = []
    for time_ns in TIMES_NS:
        rendered = {}
        for identity in [*light_ids, "all"]:
            chosen = {} if identity == "all" else {"light_component_id": identity}
            queued = scene_render.submit(service, {
                **base, **chosen, "render_time_ns": time_ns,
                "name": f"{identity} at {time_ns} ns",
            })
            job = test_scene_render.finish(service, queued)
            jobs[identity].append(job)
            rendered[identity] = _array(service, job, "spectral-radiance")
            if identity == "all":
                assert "light_component_id" not in job["request"]
                assert "light_component" not in job["result"]["summary"]
            else:
                assert job["request"]["light_component_id"] == identity
                assert job["result"]["summary"]["light_component"] == (
                    scene_render._light_component_evidence(job["request"])
                )
            assert job["result"]["summary"]["physical_validation"] == "not_established"
        indexed = _array(service, jobs["all"][-1], "first-hit-object-index")
        first_hits.append(indexed)
        assert set(np.unique(indexed)) <= {0, 1, 2}
        for index, identity in enumerate(light_ids):
            value = rendered[identity]
            expected = np.zeros_like(value)
            expected[indexed == index] = np.interp(
                jobs[identity][-1]["request"]["wavelength_nm"],
                test_scene_render.WAVE, SPECTRA[index],
            )
            # Mitsuba scalar_spectral carries its spectral value in float32;
            # geometric support is exact, and the radiance oracle allows its
            # conversion rounding only.
            np.testing.assert_allclose(value, expected, rtol=1e-6, atol=1e-12)
        np.testing.assert_array_equal(
            rendered["all"], rendered[light_ids[0]] + rendered[light_ids[1]],
        )
        pair = scene_render.verify_light_component_pair(
            service, jobs[light_ids[0]][-1]["id"], jobs[light_ids[1]][-1]["id"],
            all_light_job_id=jobs["all"][-1]["id"],
        )
        assert pair["render_time_ns"] == time_ns
        assert pair["whole_scene_illumination_id"] == scene["illumination_id"]
        altered = copy.deepcopy(jobs[light_ids[0]][-1])
        altered["request"]["light_component_id"] = light_ids[1]
        with pytest.raises(ValueError, match="Renderer Light component|Rendered Light component"):
            scene_render._verify_light_component_job(service, altered)
        with pytest.raises(ValueError, match="exactly two"):
            scene_render.verify_light_component_pair(
                service, jobs[light_ids[0]][-1]["id"], jobs["all"][-1]["id"],
            )
    assert set(np.unique(np.stack(first_hits))) == {0, 1, 2}
    assert not np.array_equal(first_hits[0], first_hits[1])
    sequences = [scene_motion_sampling.publish_sequence(
        service, [job["id"] for job in jobs[identity]],
        name=f"Rendered component {identity}",
    ) for identity in light_ids]
    evidence = scene_render.verify_light_component_sequences(
        service, sequences[0]["id"], sequences[1]["id"],
    )
    assert evidence["render_times_ns"] == TIMES_NS
    assert evidence["light_asset_ids"] == light_ids
    assert len(evidence["pair_sha256"]) == 2
    cold = PlatformService(service.registry.directory)
    assert scene_render.verify_light_component_sequences(
        cold, sequences[0]["id"], sequences[1]["id"],
    ) == evidence
    with pytest.raises(ValueError, match="one frozen scene, camera and time"):
        scene_render.verify_light_component_pair(
            cold, jobs[light_ids[0]][0]["id"], jobs[light_ids[1]][1]["id"],
        )
    with zipfile.ZipFile(build_bundle(service, "scene_renders", jobs[light_ids[0]][0]["id"])) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["run"]["request"]["light_component_id"] == light_ids[0]


def test_isolated_point_and_environment_selection_retain_lambertian_oracles(tmp_path):
    if not test_scene_render.PYTHON.exists():
        pytest.skip("Isolated Mitsuba environment is not installed")
    request = test_scene_render.basic_request()
    request["scene"]["payload"]["instances"][0]["area_light_id"] = None
    request["scene"]["payload"]["environment_light_id"] = "environment"
    request["scene"]["payload"]["point_lights"] = [
        {"light_id": "point", "position_m": [0., 0., 0.]}
    ]
    request["scene"]["illumination_id"] = "synthetic-direct-adapter-fixture"
    request["assets"] = [item for item in request["assets"] if item["id"] != "light"]
    for identity, distribution, value, unit in (
        ("environment", "constant_environment", [0.02, 0.03, 0.04], "W/m^2/sr/nm"),
        ("point", "point", [1., 2., 3.], "W/sr/nm"),
    ):
        request["assets"].append({
            "id": identity, "content_hash": hashlib.sha256(identity.encode()).hexdigest(),
            "payload": {"kind": "light", "distribution": distribution,
                        "spectrum": test_scene_render.spec(value), "unit": unit,
                        "time_model": "static"},
        })
    request["samples_per_pixel"] = 128
    point = test_scene_render.render(tmp_path / "point", {
        **request, "light_component_id": "point",
    })["spectral-radiance"]
    environment = test_scene_render.render(tmp_path / "environment", {
        **request, "light_component_id": "environment",
    })["spectral-radiance"]
    yy, xx = np.indices((8, 8))
    xy2 = ((xx + 0.5 - 4) ** 2 + (4 - yy - 0.5) ** 2) * (0.01 / 4 * 3) ** 2
    distance = np.sqrt(9 + xy2)
    expected_point = (0.5 / np.pi * 3 / distance**3)[..., None] * [1., 2., 3.]
    np.testing.assert_allclose(point, expected_point, rtol=2e-5)
    np.testing.assert_allclose(
        environment.mean(axis=(0, 1)), [0.01, 0.015, 0.02], rtol=0.05,
    )
    assert np.all(environment > 0) and np.all(point > 0)
    for identity in ("point", "environment"):
        summary = json.loads((tmp_path / identity / "renderer-summary.json").read_text())
        assert summary["light_component"]["light_asset_id"] == identity
        assert summary["light_component"]["whole_scene_illumination_id"] == (
            "synthetic-direct-adapter-fixture"
        )


def test_absent_light_selection_keeps_legacy_renderer_bytes(tmp_path):
    request = test_scene_render.basic_request()
    original = test_scene_render.render(tmp_path / "legacy", request)
    none = test_scene_render.render(tmp_path / "none", {
        **request, "light_component_id": None,
    })
    for name in original:
        np.testing.assert_array_equal(original[name], none[name])
        assert (tmp_path / "legacy" / (name + ".npy")).read_bytes() == (
            tmp_path / "none" / (name + ".npy")
        ).read_bytes()
    assert (tmp_path / "legacy" / "renderer-summary.json").read_bytes() == (
        tmp_path / "none" / "renderer-summary.json"
    ).read_bytes()
