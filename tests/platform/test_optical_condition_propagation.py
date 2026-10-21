"""A declared aperture change reaches a registered 3D scene's RAW and ISP.

This is a deterministic software transfer check with an authored pupil and
scene. The paraxial transmittance is declared, not a measured Lens radiometry
result, and the test makes no physical accuracy claim.
"""

from __future__ import annotations

from copy import deepcopy

import numpy as np
import pytest
import test_scene_render

from camera_platform import factory, imaging, scene_render
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import PROVENANCE


@pytest.fixture
def scene_workflow(tmp_path, monkeypatch):
    return test_scene_render.scene_workflow.__wrapped__(tmp_path, monkeypatch)


def _array(service, job, name):
    artifact = next(a for a in job["artifacts"] if a["name"] == f"{name}.npy")
    return np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)


def _edge_scene(service, source_scene):
    """Put a real 3D black knife edge in front of the unchanged lit plane."""
    geometry = scene_render.create_asset(service, {
        **PROVENANCE, "name": "Aperture comparison knife edge",
        "payload": {
            "kind": "geometry", "unit": "m", "primitive": "triangle_mesh",
            "vertices": [[0, -1, 1], [1, -1, 1], [1, 1, 1], [0, 1, 1]],
            "faces": [[0, 1, 2], [0, 2, 3]],
        },
    })
    black = scene_render.create_asset(service, {
        **PROVENANCE, "name": "Aperture comparison black surface",
        "payload": {
            "kind": "material", "bsdf": "diffuse",
            "spectrum": test_scene_render.spec([0, 0, 0]),
            "surface_state": "authored opaque black",
        },
    })
    payload = deepcopy(source_scene["payload"])
    payload["instances"].append({
        "geometry_id": geometry["id"], "material_id": black["id"],
        "area_light_id": None,
        "to_world": [[1, 0, 0, 0], [0, 1, 0, 0],
                     [0, 0, 1, 0], [0, 0, 0, 1]],
    })
    return scene_render.create_asset(service, {
        **PROVENANCE, "name": "Shared 3D edge and spectral light", "payload": payload,
    })


def test_aperture_revision_changes_psf_throughput_charge_raw_and_isp(scene_workflow):
    service, original_assembly, original_request = scene_workflow
    source_scene = service.registry.get("scene_assets", original_request["scene_asset_id"])
    scene = _edge_scene(service, source_scene)
    source_lens = next(
        service.registry.get("models", identity)
        for identity in original_assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "lens"
    )
    base = service.registry.get("models", source_lens["parents"][0])
    sensor_id = next(
        identity for identity in original_assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    sensor = service.registry.get("models", sensor_id)
    _, native_shape = imaging.native_sensor_grid(sensor)
    receiver = {"crop_origin": [(n - 8) // 2 for n in native_shape], "shape": [8, 8]}
    results = {}

    for f_number in (8.0, 4.0):
        label = f"F{int(f_number)}"
        conditions = deepcopy(original_assembly["conditions"])
        conditions["f_number"] = {"value": f_number, "unit": "1"}
        conditions["light_id"] = {"value": scene["illumination_id"], "unit": "1"}

        # Match each independently declared pupil diameter to the same 4 mm
        # focal length; a new Factory job and Lens revision carry each stop.
        psf_request = factory.template(service, "prysm_psf_v1")
        psf_request["name"] = f"{label} independently declared pupil"
        psf_request["request"].update(
            f_number=f_number, pupil_diameter_mm=4.0 / f_number,
        )
        psf_job = test_scene_render.finish(service, factory.submit(service, psf_request))
        assert psf_job["result"]["summary"]["status"] == "PROPOSED"

        base_data = {
            key: deepcopy(value) for key, value in base.items()
            if key in ModelCreate.model_fields
        }
        base_data.update(name=f"{label} 4 mm base Lens", revision=label)
        base_data["properties"]["f_number"] = {"value": f_number, "unit": "1"}
        parent = service.create_model(base_data)
        lens = factory.publish(service, {
            "job_id": psf_job["id"], "base_lens_model_id": parent["id"],
            "name": f"{label} 4 mm PSF Lens", "revision": label,
            "conditions": conditions,
            "condition_scope": "optical_response",
        })
        assembly_data = {
            key: deepcopy(value) for key, value in original_assembly.items()
            if key in AssemblyCreate.model_fields
        }
        assembly_data.update(
            name=f"Shared scene {label} camera", revision=label,
            conditions=conditions,
            model_ids=[
                lens["id"] if identity == source_lens["id"] else identity
                for identity in original_assembly["model_ids"]
            ],
        )
        assembly = service.create_assembly(assembly_data)
        assert assembly["compatibility"]["status"] == "PASS"
        assert sensor_id in assembly["model_ids"]
        assert assembly["raw_format"] == original_assembly["raw_format"]

        render_request = deepcopy(original_request)
        render_request.update(
            name=f"Shared 3D edge {label}", scene_asset_id=scene["id"],
            assembly_id=assembly["id"], crop_origin=receiver["crop_origin"],
            shape=receiver["shape"], samples_per_pixel=2, render_psf_halo=True,
        )
        render_request["projection"]["f_number"] = f_number
        render_job = test_scene_render.finish(
            service, scene_render.submit(service, render_request)
        )
        image = scene_render.publish(service, {
            "job_id": render_job["id"], "name": f"Published {label} ideal image",
        })
        test = service.create_test(
            imaging.spatial_test_template(service, assembly["id"], image["id"])
        )
        run = test_scene_render.finish(service, service.submit_run({
            "assembly_id": assembly["id"], "test_id": test["id"],
            "plugin_id": "spatial_scene_v1", "scene_id": image["id"],
            "parameters": {"frames": 1, "seed": 927},
        }))
        assert run["status"] == "succeeded"
        assert run["parameters"]["capture_crop"] == receiver
        assert render_job["request"]["capture_crop"] == receiver
        assert image["projection_lens_model_id"] == lens["id"]
        assert image["render_job_snapshot"]["id"] == render_job["id"]
        assert run["assembly_snapshot"]["id"] == assembly["id"]
        assert run["scene_snapshot"]["id"] == image["id"]
        assert run["request"]["psf"] == lens["payload"]["psf"]
        assert {model["id"] for model in run["model_snapshots"]} == set(assembly["model_ids"])
        assert run["result"]["summary"]["physical_validation"] == "not_established"
        results[f_number] = {
            "lens": lens, "psf_job": psf_job, "assembly": assembly,
            "render_job": render_job, "image": image, "run": run,
            "radiance": _array(service, render_job, "spectral-radiance"),
            "irradiance": _array(service, render_job, "spectral-image"),
            "blurred": _array(service, run, "blurred_spectral_irradiance"),
            "charge": _array(service, run, "mean_electrons"),
            "raw": _array(service, run, "raw_frames"),
            "rgb": _array(service, run, "rgb_frames"),
        }

    slow, fast = results[8.0], results[4.0]
    assert slow["lens"]["psf_factory_job_id"] != fast["lens"]["psf_factory_job_id"]
    assert slow["lens"]["id"] != fast["lens"]["id"]
    assert slow["assembly"]["id"] != fast["assembly"]["id"]
    assert (slow["render_job"]["request"]["scene_asset_id"]
            == fast["render_job"]["request"]["scene_asset_id"] == scene["id"])
    np.testing.assert_array_equal(slow["radiance"], fast["radiance"])
    for f_number, record in results.items():
        np.testing.assert_allclose(
            record["irradiance"], record["radiance"] * (np.pi * 0.8 / (4 * f_number**2)),
            rtol=1e-14, atol=0,
        )
    np.testing.assert_allclose(fast["irradiance"], slow["irradiance"] * 4, rtol=0, atol=1e-15)

    slow_psf = np.asarray(slow["lens"]["payload"]["psf"]["values"])
    fast_psf = np.asarray(fast["lens"]["payload"]["psf"]["values"])
    assert slow_psf.shape == fast_psf.shape
    assert np.max(np.abs(slow_psf - fast_psf)) > 0.2
    yy, xx = np.indices(slow_psf.shape[-2:])
    radius_squared = (yy - yy.mean()) ** 2 + (xx - xx.mean()) ** 2
    slow_spread = np.mean(np.sum(slow_psf * radius_squared, axis=(-2, -1)))
    fast_spread = np.mean(np.sum(fast_psf * radius_squared, axis=(-2, -1)))
    assert slow_spread > 2 * fast_spread

    # The projected input scales exactly by four; the edge response does not,
    # because the new PSF redistributes light across the same receiver ROI.
    assert np.max(np.abs(fast["blurred"] / 4 - slow["blurred"])) > (
        0.01 * slow["blurred"].max()
    )
    assert np.max(np.abs(fast["charge"] / 4 - slow["charge"])) > (
        0.01 * slow["charge"].max()
    )
    assert fast["charge"].mean() > slow["charge"].mean()
    assert np.count_nonzero(fast["raw"] != slow["raw"]) > 32
    assert np.count_nonzero(fast["rgb"] != slow["rgb"]) > 32
    assert slow["raw"].shape == fast["raw"].shape == (1, 8, 8)
    assert slow["rgb"].shape == fast["rgb"].shape == (1, 8, 8, 3)

    # A saved F/8 ideal image may not be silently reused with the F/4 Lens.
    wrong_test = service.create_test(imaging.spatial_test_template(
        service, fast["assembly"]["id"], slow["image"]["id"]
    ))
    with pytest.raises(ValueError, match="different lens"):
        service.submit_run({
            "assembly_id": fast["assembly"]["id"], "test_id": wrong_test["id"],
            "plugin_id": "spatial_scene_v1", "scene_id": slow["image"]["id"],
            "parameters": {"frames": 1, "seed": 927},
        })
