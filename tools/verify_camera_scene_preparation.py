#!/usr/bin/env python3
"""Prepare and execute a camera from a previously verified, declared scene example.

This is an explicit creation tool: it may target the main registry only when the
caller supplies its exact state path and an existing scene proof for that state.
It preserves original records and never supplies missing optical properties.
The resulting synthetic calculation is not physical camera validation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from camera_platform.registry import digest
from camera_platform.scene_contracts import SceneRenderCreate
from camera_platform.sdk import PlatformClient

RENDER_ARTIFACTS = (
    "spectral-radiance.npy",
    "spectral-image.npy",
    "first-hit-depth-m.npy",
)
CAMERA_ARTIFACTS = ("raw_frames.npy", "rgb_frames.npy")


def bound_state(client, expected_state):
    assert client.get("bootstrap")["state_dir"] == str(expected_state.resolve())


def downloaded_arrays(client, record, names):
    arrays, hashes = {}, {}
    for name in names:
        artifact = next(a for a in record["artifacts"] if a["name"] == name)
        response = client.http.get("/api/v1/artifacts/" + artifact["id"])
        response.raise_for_status()
        hashes[name] = hashlib.sha256(response.content).hexdigest()
        assert hashes[name] == artifact["sha256"]
        arrays[name] = np.load(io.BytesIO(response.content), allow_pickle=False)
        assert np.all(np.isfinite(arrays[name]))
    return arrays, hashes


def verify(client, expected_state, scene_proof):
    """Save one prepared assembly and execute Mitsuba and Camera via public APIs."""
    bound_state(client, expected_state)
    assert scene_proof["status"] == "passed"
    assert scene_proof["state_dir"] == str(expected_state.resolve())
    ids = scene_proof["records"]
    pair = ids["pairs"][0]
    source = client.get("assemblies/" + ids["assembly"])
    scene = client.get("scene_assets/" + pair["scene"])
    original_render = client.get("scene_renders/" + pair["render"])
    original_run = client.get("runs/" + pair["run"])
    assert original_render["status"] == original_run["status"] == "succeeded"
    assert original_render["request"]["assembly_id"] == source["id"]
    assert original_render["request"]["scene_asset_id"] == scene["id"]
    assert original_run["assembly_snapshot"] == source
    models = {i: client.get("models/" + i) for i in source["model_ids"]}
    counts = client.get("workbench")["counts"]
    draft = client.get(
        "scene_authoring/camera-template?scene_id=" + scene["id"] + "&assembly_id=" + source["id"]
    )
    draft["assembly"]["name"] = "Prepared scene camera · " + source["name"][:140]
    draft["assembly"]["revision"] = "scene-preparation-1"
    preview = client.preview_scene_camera(draft)
    assert preview["status"] == "ready", preview["issues"]
    assert preview["physical_validation"] == "not_established"
    assert preview["source_assembly_sha256"] == source["content_hash"]
    assert preview["scene_sha256"] == scene["content_hash"]
    assert preview["model_hashes"] == {i: m["content_hash"] for i, m in models.items()}
    assert preview["preview_sha256"] == digest(
        {k: v for k, v in preview.items() if k != "preview_sha256"}
    )
    assert client.get("workbench")["counts"] == counts

    saved = client.save_scene_camera(preview)
    assembly = saved["assembly"]
    assert assembly["scene_preparation"] == preview
    saved_counts = client.get("workbench")["counts"]
    assert client.save_scene_camera(preview) == saved
    assert client.get("workbench")["counts"] == saved_counts
    assert client.get("assemblies/" + source["id"]) == source
    for model_id, model in models.items():
        assert client.get("models/" + model_id) == model
    template = saved["render"]["request"]
    assert template["assembly_id"] == assembly["id"]
    assert all(v is None for v in template["projection"]["on_axis_transmittance"]["values"])
    assert not saved["render"]["issues"]

    # Reuse an explicitly declared throughput from the frozen source experiment.
    # A successful preparation preview must not imply that unknown T(lambda) is 1.
    request = {
        k: copy.deepcopy(v)
        for k, v in original_render["request"].items()
        if k in SceneRenderCreate.model_fields
    }
    projection = request["projection"]
    assert projection["focal_length_mm"] == template["projection"]["focal_length_mm"]
    assert projection["f_number"] == template["projection"]["f_number"]
    assert projection["on_axis_transmittance"]["wavelength_nm"] == (
        template["projection"]["on_axis_transmittance"]["wavelength_nm"]
    )
    assert projection["source_uri"] and projection["method_and_version"]
    request.update(name=assembly["name"] + " · render", assembly_id=assembly["id"], force=False)
    rendered = client.wait_scene_render(client.render_scene(request)["id"])
    assert rendered["status"] == "succeeded", rendered.get("error")
    assert client.render_scene(request)["id"] == rendered["id"]
    assert preview["environment_sha256"] == digest(rendered["environment"])
    image = next(
        (
            i for i in client.get("spectral_images")
            if i.get("render_job_snapshot", {}).get("id") == rendered["id"]
        ),
        None,
    )
    if image is None:
        image = client.publish_scene_render(rendered["id"], assembly["name"] + " · irradiance")
    specification = client.get(
        "templates/spatial-test?assembly_id=" + assembly["id"] + "&scene_id=" + image["id"]
    )
    test = next(
        (t for t in client.get("tests") if all(t.get(k) == v for k, v in specification.items())),
        None,
    ) or client.post("tests", specification)
    parameters = {"seed": 931, "frames": 2}
    camera = client.wait(client.submit(
        assembly["id"], test["id"], plugin_id="spatial_scene_v1",
        scene_id=image["id"], parameters=parameters,
    )["id"])
    assert camera["status"] == "succeeded", camera.get("error")
    assert camera["environment"] == rendered["environment"]
    assert camera["assembly_snapshot"] == assembly
    assert camera["scene_snapshot"]["render_job_snapshot"]["id"] == rendered["id"]
    assert camera["result"]["prediction"]["uncertainty"] == {"kind": "unknown"}
    render_arrays, render_hashes = downloaded_arrays(client, rendered, RENDER_ARTIFACTS)
    camera_arrays, camera_hashes = downloaded_arrays(client, camera, CAMERA_ARTIFACTS)
    expected_shape = (2, *request["shape"])
    assert camera_arrays["raw_frames.npy"].shape == expected_shape
    assert camera_arrays["rgb_frames.npy"].shape == (*expected_shape, 3)
    np.testing.assert_allclose(
        render_arrays["spectral-image.npy"],
        render_arrays["spectral-radiance.npy"] * np.asarray(
            projection["on_axis_transmittance"]["values"]
        ) * np.pi / (4 * projection["f_number"] ** 2),
        rtol=1e-12,
    )
    # Preparation changes identity and provenance, not the unchanged camera response.
    assert render_hashes["spectral-radiance.npy"] == pair["radiance_sha256"]
    assert camera_hashes["raw_frames.npy"] == pair["raw_sha256"]
    assert camera_hashes["rgb_frames.npy"] == pair["rgb_sha256"]
    response = client.http.get("/api/v1/runs/" + camera["id"] + "/bundle")
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["run"]["assembly_snapshot"]["scene_preparation"] == preview
        for artifact in manifest["run"]["artifacts"]:
            content = bundle.read("artifacts/" + artifact["sha256"] + "/" + artifact["name"])
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    return {
        "schema": "camera_scene_preparation_verification_v1",
        "status": "passed",
        "state_dir": str(expected_state.resolve()),
        "verified_at": datetime.now(UTC).isoformat(),
        "physical_validation": "not_established",
        "source_kind": "simulated",
        "checks": [
            "Exact destination and fixed source camera, scene and model versions",
            "Preview is nonmutating and hashes all preparation evidence",
            "Idempotent save preserves source assembly and every selected model",
            "Unknown throughput stays empty until explicit source projection reuse",
            "Prepared camera executes real Mitsuba and Camera RAW/RGB",
            "Downloaded arrays preserve exact unchanged source response and aperture units",
            "Camera evidence ZIP retains preparation and verified original artifact bytes",
        ],
        "records": {
            "source_assembly": source["id"], "scene": scene["id"],
            "assembly": assembly["id"], "render": rendered["id"],
            "image": image["id"], "test": test["id"], "run": camera["id"],
        },
        "preview_sha256": preview["preview_sha256"],
        "assembly_sha256": assembly["content_hash"],
        "source_assembly_sha256": source["content_hash"],
        "scene_sha256": scene["content_hash"],
        "model_hashes": preview["model_hashes"],
        "artifact_sha256": {"render": render_hashes, "camera": camera_hashes},
        "environment": rendered["environment"], "backend": rendered["backend"],
        "parameters": parameters,
        "throughput_source_render_id": original_render["id"],
        "throughput_projection_sha256": digest(projection),
        "bundle_sha256": hashlib.sha256(response.content).hexdigest(),
    }


def repeat(client, expected_state, proof):
    """Force real reruns while proving the saved preparation has not changed."""
    bound_state(client, expected_state)
    assert proof["state_dir"] == str(expected_state.resolve())
    ids = proof["records"]
    assembly = client.get("assemblies/" + ids["assembly"])
    assert assembly["content_hash"] == proof["assembly_sha256"]
    preview = assembly["scene_preparation"]
    assert preview["preview_sha256"] == proof["preview_sha256"]
    assert client.preview_scene_camera(preview["draft"]) == preview
    assert client.save_scene_camera(preview)["assembly"] == assembly
    assert client.get("assemblies/" + ids["source_assembly"])["content_hash"] == (
        proof["source_assembly_sha256"]
    )
    assert client.get("scene_assets/" + ids["scene"])["content_hash"] == proof["scene_sha256"]
    for model_id, checksum in proof["model_hashes"].items():
        assert client.get("models/" + model_id)["content_hash"] == checksum
    original = client.get("scene_renders/" + ids["render"])
    request = {
        k: copy.deepcopy(v) for k, v in original["request"].items()
        if k in SceneRenderCreate.model_fields
    }
    request["force"] = True
    rendered = client.wait_scene_render(client.render_scene(request)["id"])
    assert rendered["status"] == "succeeded", rendered.get("error")
    assert rendered["id"] != ids["render"]
    assert rendered["environment"] == proof["environment"]
    assert rendered["backend"] == proof["backend"]
    _, render_hashes = downloaded_arrays(client, rendered, RENDER_ARTIFACTS)
    assert render_hashes == proof["artifact_sha256"]["render"]
    # The original Test pins its original image. The forced render above proves
    # that this input is numerically reproducible before rerunning the fixed Test.
    camera = client.wait(client.submit(
        ids["assembly"], ids["test"], plugin_id="spatial_scene_v1",
        scene_id=ids["image"], parameters=proof["parameters"], force=True,
    )["id"])
    assert camera["status"] == "succeeded", camera.get("error")
    assert camera["id"] != ids["run"]
    assert camera["environment"] == proof["environment"]
    assert camera["assembly_snapshot"] == assembly
    _, camera_hashes = downloaded_arrays(client, camera, CAMERA_ARTIFACTS)
    assert camera_hashes == proof["artifact_sha256"]["camera"]
    return {
        "status": "passed", "render": rendered["id"], "run": camera["id"],
        "preview_sha256": preview["preview_sha256"],
        "assembly_sha256": assembly["content_hash"],
        "artifact_sha256": {"render": render_hashes, "camera": camera_hashes},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--scene-proof", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with PlatformClient(args.base_url, timeout=120) as client:
        proof = verify(client, args.state_dir, json.loads(args.scene_proof.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": proof["status"], "checks": len(proof["checks"]),
                      "records": proof["records"], "report": str(args.output)}))


if __name__ == "__main__":
    main()
