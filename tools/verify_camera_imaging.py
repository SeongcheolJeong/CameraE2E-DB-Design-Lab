#!/usr/bin/env python3
"""Verify actual native spatial imaging and RAW-preserving ISP replay over durable HTTP."""

from __future__ import annotations

import hashlib
import io
import json
import socket
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np
from verify_camera_platform import WORKSPACE, start, stop

from camera_platform.contracts import AssemblyCreate
from camera_platform.sdk import PlatformClient
from camera_platform.service import environment_fingerprint


def main():
    report = {
        "schema": "camera_imaging_verification_v1",
        "environment": environment_fingerprint(),
        "physical_validation": "not_established",
        "source_kind": "simulated_or_estimated_fixtures",
        "checks": [],
    }

    def passed(name):
        report["checks"].append({"name": name, "status": "passed"})

    def download(client, record, name):
        artifact = next(a for a in record["artifacts"] if a["name"] == name)
        response = client.http.get(artifact["url"])
        response.raise_for_status()
        assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
        return response.content

    with tempfile.TemporaryDirectory(prefix="camera-imaging-http-") as directory:
        state = Path(directory).resolve()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        with (state / "http.log").open("ab") as log:
            process = start(state, port, log)
            try:
                with PlatformClient(f"http://127.0.0.1:{port}") as client:
                    boot = client.get("bootstrap")
                    original = boot["assemblies"][0]
                    models = {m["id"]: m for m in boot["models"]}
                    lens = next(
                        models[i] for i in original["model_ids"] if models[i]["kind"] == "lens"
                    )
                    job = client.wait_characterization(
                        client.characterize(client.template("factory", plugin_id="prysm_psf_v1"))[
                            "id"
                        ]
                    )
                    assert job["status"] == "succeeded", job
                    assert job["result"]["summary"]["status"] == "PROPOSED"
                    candidate = client.post(
                        "factory/models",
                        {
                            "job_id": job["id"],
                            "base_lens_model_id": lens["id"],
                            "conditions": original["conditions"],
                            "name": "HTTP native PSF",
                            "revision": "1",
                        },
                    )
                    assert candidate["psf_factory_job_id"] == job["id"]
                    passed("actual_prysm_propagation_refinements_and_parent_preserving_publication")
                    fields = {
                        k: deepcopy(v)
                        for k, v in original.items()
                        if k in AssemblyCreate.model_fields
                    }
                    fields.update(name="HTTP native camera", revision="native-1")
                    fields["model_ids"] = [
                        candidate["id"] if i == lens["id"] else i for i in fields["model_ids"]
                    ]
                    assembly = client.post("assemblies", fields)
                    scene = client.create_spectral_fixture(
                        client.template("spatial-scene", assembly_id=assembly["id"])
                    )
                    original_image = download(client, scene, "spectral-image.npy")
                    path = state / "explicit-spectral-image.npy"
                    path.write_bytes(original_image)
                    metadata = client.template("spectral-image", assembly_id=assembly["id"])
                    metadata.update(name="HTTP byte-preserving NPY import", revision="import-1")
                    imported = client.import_spectral_image(metadata, path)
                    assert download(client, imported, "spectral-image.npy") == original_image
                    passed("native_spectral_npy_upload_and_original_metadata_bytes_preserved")
                    specification = client.template(
                        "spatial-test", assembly_id=assembly["id"], scene_id=scene["id"]
                    )
                    test = client.post("tests", specification)
                    run = client.wait(
                        client.submit(
                            assembly["id"],
                            test["id"],
                            plugin_id="spatial_scene_v1",
                            scene_id=scene["id"],
                            parameters={"frames": 3, "seed": 783},
                        )["id"]
                    )
                    assert run["status"] == "succeeded", run
                    raw_bytes = download(client, run, "raw_frames.npy")
                    raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
                    rgb = np.load(
                        io.BytesIO(download(client, run, "rgb_frames.npy")), allow_pickle=False
                    )
                    assert raw.shape == (3, 64, 64) and np.std(raw) > 10 and raw.max() < 4095
                    assert not np.array_equal(raw[0], raw[1])
                    assert run["result"]["geometry"] == {
                        "kind": "native_sensor",
                        "full_sensor_shape": [1440, 1920],
                        "crop_origin": [688, 928],
                    }
                    passed("durable_worker_native_geometry_spectral_psf_to_nonuniform_raw_and_rgb")
                    profile = client.template("isp-profile", assembly_id=assembly["id"])
                    profile["payload"]["white_balance"]["gains_rgb"] = [1.4, 1.0, 0.7]
                    isp = client.post("models", profile)
                    replay = client.wait_replay(
                        client.submit_replay("run", run["id"], isp["id"])["id"]
                    )
                    assert replay["status"] == "succeeded", replay
                    assert download(client, replay, "raw_frames.npy") == raw_bytes
                    changed = np.load(
                        io.BytesIO(download(client, replay, "rgb_frames.npy")), allow_pickle=False
                    )
                    assert not np.array_equal(changed, rgb)
                    assert changed[..., 0].mean() > rgb[..., 0].mean()
                    assert changed[..., 2].mean() < rgb[..., 2].mean()
                    assert replay["result"]["acquisition_conditions"] == test["conditions"]
                    assert client.get("captures") == client.get("measurements") == []
                    passed(
                        "all_frame_isp_change_preserves_raw_hash_acquisition_conditions_and_provenance"
                    )
                    with zipfile.ZipFile(io.BytesIO(client.replay_bundle(replay["id"]))) as archive:
                        manifest = json.loads(archive.read("manifest.json"))
                        assert manifest["source"] == run
                        for name in archive.namelist():
                            if name.startswith("artifacts/"):
                                assert (
                                    hashlib.sha256(archive.read(name)).hexdigest()
                                    == name.split("/")[1]
                                )
                        for artifact in scene["artifacts"]:
                            assert (
                                f"artifacts/{artifact['sha256']}/{artifact['name']}"
                                in archive.namelist()
                            )
                    passed(
                        "portable_replay_bundle_retains_scene_input_aliases_models_factory_and_original_raw"
                    )
                    report.update(
                        psf_summary=job["result"]["summary"],
                        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
                        rgb_mean_before=rgb.mean(axis=(0, 1, 2)).tolist(),
                        rgb_mean_after=changed.mean(axis=(0, 1, 2)).tolist(),
                        spatial_summary=run["result"]["summary"],
                        replay_summary=replay["result"]["summary"],
                    )
                stop(process)
                process = start(state, port, log)
                with PlatformClient(f"http://127.0.0.1:{port}") as client:
                    assert client.get("replays/" + replay["id"]) == replay
                    assert (
                        download(client, client.get("runs/" + run["id"]), "raw_frames.npy")
                        == raw_bytes
                    )
                    assert client.get("audit")["integrity"] == "verified"
                    passed("restart_retains_jobs_and_verified_original_raw")
            finally:
                stop(process)
    target = WORKSPACE / "reports/camera_platform/imaging-http-verification.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": "passed", "checks": len(report["checks"]), "report": str(target)}))


if __name__ == "__main__":
    main()
