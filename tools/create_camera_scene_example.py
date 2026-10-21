#!/usr/bin/env python3
"""Create a declared synthetic 3D material-comparison example via the public SDK.

Requires the exact destination state path. Preserves every original model and asset.
The example is a design demonstration, not measured camera validation.
"""

import argparse
import copy
import hashlib
import io
import json
import zipfile
from pathlib import Path

import numpy as np

from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import PROVENANCE
from camera_platform.scene_contracts import SceneAssetCreate
from camera_platform.sdk import PlatformClient


def create_example(client, expected_state):
    bootstrap = client.get("bootstrap")
    assert bootstrap["state_dir"] == str(expected_state.resolve())
    assert client.get("scene_renders/catalog")["available"]
    models = {m["id"]: m for m in bootstrap["models"]}
    # A prior temporal verification may also have no declared aperture but already
    # pins a Factory PSF. Its optical properties cannot be rewritten as a new lens.
    original = next(
        a
        for a in bootstrap["assemblies"]
        if "f_number" not in a["conditions"]
        and any(
            models[i]["kind"] == "lens"
            and models[i]["source_kind"] == "estimated"
            and not models[i]["calibration_dataset_ids"]
            and "focal_length" not in models[i]["properties"]
            and "f_number" not in models[i]["properties"]
            and not models[i].get("factory_job_id")
            and not models[i].get("psf_factory_job_id")
            and "psf" not in models[i]["payload"]
            for i in a["model_ids"]
        )
    )
    sensor = next(models[i] for i in original["model_ids"] if models[i]["kind"] == "sensor")
    base = next(models[i] for i in original["model_ids"] if models[i]["kind"] == "lens")
    wave = sensor["payload"]["wavelength_nm"]

    def spectrum(values):
        return {"wavelength_nm": wave, "values": list(values)}

    def once(collection, payload):
        for record in client.get(collection):
            if all(record.get(k) == v for k, v in payload.items()):
                return record
        return client.post(collection, payload)

    def asset(name, payload):
        data = SceneAssetCreate.model_validate(
            {**PROVENANCE, "name": name, "payload": payload}
        ).model_dump()
        return once("scene_assets", data)

    geometry = asset(
        "Reusable 3D rectangle", {"kind": "geometry", "unit": "m", "primitive": "rectangle"}
    )
    sphere = asset(
        "Reusable sphere geometry", {"kind": "geometry", "unit": "m", "primitive": "sphere"}
    )
    material_a = asset(
        "Declared red reflectance",
        {
            "kind": "material",
            "bsdf": "diffuse",
            "surface_state": "synthetic dry diffuse",
            "spectrum": spectrum(0.05 + 0.7 * np.exp(-(((np.asarray(wave) - 620) / 45) ** 2))),
        },
    )
    material_b = asset(
        "Declared blue reflectance",
        {
            "kind": "material",
            "bsdf": "diffuse",
            "surface_state": "synthetic dry diffuse",
            "spectrum": spectrum(0.05 + 0.7 * np.exp(-(((np.asarray(wave) - 470) / 45) ** 2))),
        },
    )
    neutral = asset(
        "Neutral background",
        {
            "kind": "material",
            "bsdf": "diffuse",
            "surface_state": "synthetic dry diffuse",
            "spectrum": spectrum([0.6] * len(wave)),
        },
    )
    light = asset(
        "Declared environment spectral radiance",
        {
            "kind": "light",
            "distribution": "constant_environment",
            "unit": "W/m^2/sr/nm",
            "time_model": "static",
            "spectrum": spectrum([0.002] * len(wave)),
        },
    )

    def scene(material, name):
        return asset(
            name,
            {
                "kind": "scene",
                "coordinate_system": "right_handed_y_up_metres",
                "environment_light_id": light["id"],
                "instances": [
                    {
                        "geometry_id": geometry["id"],
                        "material_id": neutral["id"],
                        "to_world": [
                            [-0.1, 0.0, 0.0, 0.0],
                            [0.0, 0.1, 0.0, 0.0],
                            [0.0, 0.0, -0.1, 3.0],
                            [0.0, 0.0, 0.0, 1.0],
                        ],
                    },
                    {
                        "geometry_id": sphere["id"],
                        "material_id": material["id"],
                        "to_world": [
                            [0.015, 0.0, 0.0, -0.008],
                            [0.0, 0.015, 0.0, 0.0],
                            [0.0, 0.0, 0.015, 2.7],
                            [0.0, 0.0, 0.0, 1.0],
                        ],
                    },
                ],
            },
        )

    scene_a, scene_b = (
        scene(material_a, "Same geometry · red material"),
        scene(material_b, "Same geometry · blue material"),
    )
    assert scene_a["illumination_id"] == scene_b["illumination_id"]
    conditions = {
        **original["conditions"],
        "f_number": {"value": 8.0, "unit": "1"},
        "light_id": {"value": scene_a["illumination_id"], "unit": "1"},
    }
    fields = {k: copy.deepcopy(v) for k, v in base.items() if k in ModelCreate.model_fields}
    fields.update(
        name="Declared 4 mm F/8 design assumption",
        revision="scene-optics-assumption-1",
        parents=[base["id"]],
        source_kind="estimated",
        source_uri="assumption://scene-example/f4mm-fnumber8/" + base["id"],
        method_and_version="independently_declared_projection_with_inherited_response:1",
        physical_instance=None,
        uncertainty={"kind": "unknown"},
    )
    fields["properties"].update(
        focal_length={"value": 4.0, "unit": "mm"}, f_number={"value": 8.0, "unit": "1"}
    )
    declared_lens = once("models", fields)
    factory = client.post("factory/jobs", client.get("templates/factory?plugin_id=prysm_psf_v1"))
    factory = client.wait_characterization(factory["id"])
    assert factory["status"] == "succeeded", factory.get("error")
    saved_lens = next(
        (
            m
            for m in client.get("models")
            if m.get("psf_factory_job_id") == factory["id"]
            and declared_lens["id"] in m.get("parents", [])
            and m.get("factory_application_conditions") == conditions
            and m["name"] == "Declared projection plus Prysm PSF"
        ),
        None,
    )
    lens = saved_lens or client.post(
        "factory/models",
        {
            "job_id": factory["id"],
            "base_lens_model_id": declared_lens["id"],
            "name": "Declared projection plus Prysm PSF",
            "revision": "scene-1",
            "conditions": conditions,
        },
    )
    fields = {k: copy.deepcopy(v) for k, v in original.items() if k in AssemblyCreate.model_fields}
    fields.update(
        name="3D material comparison camera",
        conditions=conditions,
        model_ids=[lens["id"] if models[i]["kind"] == "lens" else i for i in fields["model_ids"]],
    )
    assembly = once("assemblies", fields)
    projection = {
        **PROVENANCE,
        "projection_model": "paraxial_on_axis_projection_v1",
        "focal_length_mm": 4.0,
        "f_number": 8.0,
        "on_axis_transmittance": spectrum([0.8] * len(wave)),
        "geometry_model": "pinhole_pixel_centre",
        "irradiance_model": "pi_L_T_over_4N2_before_relative_illumination",
        "approximation_uncertainty": "unknown",
    }

    def fetch_array(record, name):
        artifact = next(a for a in record["artifacts"] if a["name"] == name)
        response = client.http.get("/api/v1/artifacts/" + artifact["id"])
        response.raise_for_status()
        assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
        return np.load(io.BytesIO(response.content), allow_pickle=False), artifact["sha256"]

    results = []
    for source in [scene_a, scene_b]:
        request = {
            "name": source["name"],
            "scene_asset_id": source["id"],
            "assembly_id": assembly["id"],
            "shape": [32, 32],
            "samples_per_pixel": 16,
            "max_depth": 6,
            "seed": 7351,
            "pose": {"origin_m": [0.0, 0.0, 0.0], "target_m": [0.0, 0.0, 3.0]},
            "projection": projection,
        }
        job = client.wait_scene_render(client.render_scene(request)["id"])
        assert job["status"] == "succeeded", job.get("error")
        assert client.render_scene(request)["id"] == job["id"]
        image = next(
            (
                i
                for i in client.get("spectral_images")
                if i.get("render_job_snapshot", {}).get("id") == job["id"]
            ),
            None,
        )
        if image is None:
            image = client.publish_scene_render(
                job["id"], source["name"] + " · projected irradiance"
            )
        test = once(
            "tests",
            client.get(
                "templates/spatial-test?assembly_id=" + assembly["id"] + "&scene_id=" + image["id"]
            ),
        )
        run = client.wait(
            client.submit(
                assembly["id"],
                test["id"],
                plugin_id="spatial_scene_v1",
                scene_id=image["id"],
                parameters={"seed": 931, "frames": 2},
            )["id"]
        )
        assert run["status"] == "succeeded", run.get("error")
        radiance, radiance_hash = fetch_array(job, "spectral-radiance.npy")
        irradiance, _ = fetch_array(job, "spectral-image.npy")
        raw, raw_hash = fetch_array(run, "raw_frames.npy")
        rgb, rgb_hash = fetch_array(run, "rgb_frames.npy")
        depth, _ = fetch_array(job, "first-hit-depth-m.npy")
        objects, _ = fetch_array(job, "first-hit-object-index.npy")
        assert set(np.unique(objects)) == {0, 1}
        assert np.min(depth) < 2.8 and np.max(depth) > 3.0
        np.testing.assert_allclose(irradiance, radiance * np.pi * 0.8 / 256, rtol=1e-12)
        assert raw.shape == (2, 32, 32) and rgb.shape == (2, 32, 32, 3)
        assert run["result"]["prediction"]["uncertainty"] == {"kind": "unknown"}
        results.append(
            {
                "scene": source["id"],
                "render": job["id"],
                "image": image["id"],
                "test": test["id"],
                "run": run["id"],
                "radiance_sha256": radiance_hash,
                "raw_sha256": raw_hash,
                "rgb_sha256": rgb_hash,
            }
        )
    assert results[0]["radiance_sha256"] != results[1]["radiance_sha256"]
    assert results[0]["raw_sha256"] != results[1]["raw_sha256"]
    assert results[0]["rgb_sha256"] != results[1]["rgb_sha256"]
    bundle = client.http.get("/api/v1/runs/" + results[1]["run"] + "/bundle")
    bundle.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert (
            manifest["run"]["scene_snapshot"]["render_job_snapshot"]["id"] == results[1]["render"]
        )
        for a in manifest["run"]["artifacts"]:
            assert (
                hashlib.sha256(z.read("artifacts/" + a["sha256"] + "/" + a["name"])).hexdigest()
                == a["sha256"]
            )
    return {
        "schema": "camera_scene_http_verification_v1",
        "status": "passed",
        "state_dir": str(expected_state.resolve()),
        "source_kind": "simulated",
        "physical_validation": "not_established",
        "checks": [
            "HTTP and separate Worker execute Mitsuba and Prysm",
            "Scene light conditions and reusable geometry preserved across material swap",
            "Fixed-wavelength radiance and explicit aperture projection retain units",
            "3D foreground and background produce actual depth and occlusion",
            "Material change affects spectral radiance and actual RAW/RGB",
            "Exact request cache reuses immutable rendering job",
            "Original geometry/material/light/render evidence included in camera ZIP",
        ],
        "records": {
            "assembly": assembly["id"],
            "geometry": geometry["id"],
            "sphere": sphere["id"],
            "materials": [material_a["id"], material_b["id"]],
            "pairs": results,
        },
        "environment": job["environment"],
        "backend": job["backend"],
        "bundle_sha256": hashlib.sha256(bundle.content).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with PlatformClient(args.base_url) as client:
        result = create_example(client, args.state_dir)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": result["status"],
                "checks": len(result["checks"]),
                "records": result["records"],
            }
        )
    )
