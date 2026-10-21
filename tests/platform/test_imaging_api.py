"""Real temporary-state HTTP/SDK spatial imaging and RAW replay integration."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.backup import backup_state, restore_state
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.isp import process_raw
from camera_platform.registry import SQLiteRegistry
from camera_platform.runtime import run_plugin
from camera_platform.sdk import PlatformClient
from camera_platform.spatial import request_template


@pytest.fixture
def api(tmp_path):
    app = create_app(tmp_path / "api", start_worker=False)
    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/bootstrap").json()
        client.headers["X-Platform-Token"] = bootstrap["token"]
        yield app.state.service, client, bootstrap


def checked(response, status):
    assert response.status_code == status, response.text
    return response.json()


def npy_bytes(values):
    stream = io.BytesIO()
    np.save(stream, values, allow_pickle=False)
    return stream.getvalue()


def finish(service, queued):
    """Exercise the real worker entry synchronously; this is not a process-supervision test."""
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    output = service.execute_run(job)
    return service.registry.finish(job["id"], result=output)


def composite_assembly(api):
    service, client, bootstrap = api
    original = bootstrap["assemblies"][0]
    models = [service.registry.get("models", identity) for identity in original["model_ids"]]
    lens = next(m for m in models if m["kind"] == "lens")
    sensor = next(m for m in models if m["kind"] == "sensor")
    payload = {key: deepcopy(value) for key, value in lens.items()
               if key in ModelCreate.model_fields}
    psf = request_template()["psf"]
    psf["sample_pitch_um"] = sensor["payload"]["pixel_pitch_um"]
    psf["wavelength_nm"] = sensor["payload"]["wavelength_nm"]
    payload.update(
        name="API assumed pixel-hit PSF lens", revision="api-psf-1", parents=[lens["id"]],
        source_uri="fixture://api-authored-psf-probabilities", source_kind="estimated",
    )
    payload["payload"]["psf"] = psf
    payload["included_effects"] += ["lens_psf", "pixel_aperture"]
    model = checked(client.post("/api/v1/models", json=payload), 201)
    assembled = {key: deepcopy(value) for key, value in original.items()
                 if key in AssemblyCreate.model_fields}
    assembled.update(name="API native spatial fixture camera", revision="api-spatial-1")
    assembled["model_ids"] = [model["id"] if i == lens["id"] else i for i in original["model_ids"]]
    return checked(client.post("/api/v1/assemblies", json=assembled), 201)


def spatial_run(api):
    service, client, _ = api
    assembly = composite_assembly(api)
    fixture = checked(client.get("/api/v1/templates/spatial-scene",
                                 params={"assembly_id": assembly["id"]}), 200)
    fixture.update(shape=[16, 20], pattern="slanted_edge")
    scene = checked(client.post("/api/v1/spectral_images/fixture", json=fixture), 201)
    test = checked(client.get("/api/v1/templates/spatial-test", params={
        "assembly_id": assembly["id"], "scene_id": scene["id"],
    }), 200)
    test = checked(client.post("/api/v1/tests", json=test), 201)
    sdk = PlatformClient.__new__(PlatformClient)
    sdk.http = client  # real handlers, using the in-process HTTP transport
    queued = sdk.submit(
        assembly["id"], test["id"], scene_id=scene["id"], plugin_id="spatial_scene_v1",
        parameters={"frames": 2, "seed": 9},
    )
    assert queued["scene_id"] == scene["id"]
    return assembly, scene, finish(service, queued)


def test_specific_templates_and_plugin_discovery_are_reachable(api):
    _, client, bootstrap = api
    assembly = bootstrap["assemblies"][0]["id"]
    for route in ("spectral-image", "spatial-scene", "isp-profile"):
        assert client.get("/api/v1/templates/" + route,
                          params={"assembly_id": assembly}).status_code == 200
    assert client.get("/api/v1/templates/spatial-test").status_code == 422
    assert client.get("/api/v1/templates/unknown-imaging-type").status_code == 404
    assert client.get("/api/v1/replay-sources").json() == []
    assert client.get("/api/v1/replays").json() == []
    assert client.get("/api/v1/spectral_images").json() == []
    ids = [p["plugin_id"] for p in bootstrap["plugins"]]
    assert ids.count("spatial_scene_v1") == 1
    schema = client.get("/openapi.json").json()
    assert "/api/v1/spectral_images/import" in schema["paths"]
    assert "/api/v1/replays/{identity}/bundle" in schema["paths"]


def test_multipart_spectral_import_preserves_verbatim_bytes_and_vendor_kind(api):
    service, client, bootstrap = api
    metadata = client.get("/api/v1/templates/spectral-image", params={
        "assembly_id": bootstrap["assemblies"][0]["id"],
    }).json()
    metadata.update(source_kind="vendor", source_uri="fixture://vendor-provenance-contract-test")
    values = np.full((16, 20, 5), 0.0001, dtype="<f4")
    content = npy_bytes(values)
    scene = checked(client.post("/api/v1/spectral_images/import",
                                data={"metadata": json.dumps(metadata)},
                                files={"image": ("explicit-spectrum.npy", content)}), 201)
    assert scene["source_kind"] == "vendor"
    assert scene["physical_validation"] == "not_established"
    artifact = next(a for a in scene["artifacts"] if a["name"] == "spectral-image.npy")
    assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
    assert client.get(artifact["url"]).content == content  # available before any camera run
    assert service.registry.blob_path(artifact["sha256"]).read_bytes() == content
    assert client.get("/api/v1/spectral_images/" + scene["id"]).json() == scene
    assert len(client.get("/api/v1/spectral_images").json()) == 1


@pytest.mark.parametrize("content", [
    b"not a NumPy file",
    npy_bytes(np.zeros((16, 20, 5), dtype=np.uint16)),
    npy_bytes(np.full((16, 20, 5), -1.0)),
    npy_bytes(np.full((16, 20, 5), float("nan"))),
    npy_bytes(np.zeros((16, 20, 3))),
])
def test_invalid_spectral_files_refused_without_registered_images(api, content):
    _, client, _ = api
    metadata = client.get("/api/v1/templates/spectral-image").json()
    response = client.post("/api/v1/spectral_images/import",
                           data={"metadata": json.dumps(metadata)},
                           files={"image": ("invalid.npy", content)})
    assert response.status_code == 422, response.text
    assert client.get("/api/v1/spectral_images").json() == []


def test_spectral_import_requires_token_multipart_and_bounded_metadata(api):
    _, client, _ = api
    response = client.post("/api/v1/spectral_images/import", json={})
    assert response.status_code == 415
    response = client.post("/api/v1/spectral_images/import",
                           files={"image": ("a.npy", b"anything")}, data={"metadata": "{}"},
                           headers={"X-Platform-Token": "stale"})
    assert response.status_code == 403
    response = client.post("/api/v1/spectral_images/import",
                           files={"image": ("a.npy", b"anything")},
                           data={"metadata": " " * (1_048_576 + 1)})
    # Starlette can enforce its own 1 MiB multipart-part cap before our
    # endpoint's size check. Both paths must reject without creating evidence.
    assert response.status_code in {400, 413}, response.text
    assert "size" in response.text.lower() or "mib" in response.text.lower()
    assert client.get("/api/v1/spectral_images").json() == []


def test_chunked_spectral_upload_obeys_stream_cap_without_content_length(api):
    _, client, _ = api

    def chunks():
        for _ in range(69):
            yield b"x" * (1024 * 1024)

    response = client.post("/api/v1/spectral_images/import", content=chunks(), headers={
        "Content-Type": "multipart/form-data; boundary=bounded-test",
        "Transfer-Encoding": "chunked",
    })
    assert response.status_code == 413
    assert client.get("/api/v1/spectral_images").json() == []


def test_actual_native_spatial_run_and_preserved_raw_replay_bundle(api):
    service, client, _ = api
    assembly, scene, source = spatial_run(api)
    assert source["status"] == "succeeded"
    assert source["result"]["prediction"]["metric_id"] == "raw.channel_mean_dn"
    assert source["result"]["prediction"]["uncertainty"] == {"kind": "unknown"}
    assert source["result"]["geometry"] == {
        key: value for key, value in scene["sampling"].items() if key != "pixel_pitch_um"
    }
    raw_artifact = next(a for a in source["artifacts"] if a["name"] == "raw_frames.npy")
    raw_bytes = client.get(raw_artifact["url"]).content
    raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
    assert raw.shape == (2, 16, 20) and np.issubdtype(raw.dtype, np.integer)
    assert raw.min() < raw.max() < 4095
    assert source["id"] in {r["source_id"] for r in client.get("/api/v1/replay-sources").json()}
    profile = client.get("/api/v1/templates/isp-profile",
                         params={"assembly_id": assembly["id"]}).json()
    profile["payload"]["white_balance"]["gains_rgb"] = [1.7, 1, 1]
    model = checked(client.post("/api/v1/models", json=profile), 201)
    queued = checked(client.post("/api/v1/replays", json={
        "name": "API preserved RAW tuning", "source_kind": "run", "source_id": source["id"],
        "isp_model_id": model["id"],
    }), 202)
    assert client.get("/api/v1/runs/" + queued["id"]).status_code == 404
    assert queued["id"] not in {r["id"] for r in client.get("/api/v1/runs").json()}
    replay = finish(service, queued)
    assert replay["status"] == "succeeded"
    assert replay["result"]["raw_sha256"] == raw_artifact["sha256"]
    assert client.get(raw_artifact["url"]).content == raw_bytes
    output_artifacts = {a["name"]: a for a in replay["artifacts"]}
    original_rgb = next(a for a in source["artifacts"] if a["name"] == "rgb.npy")
    assert output_artifacts["rgb.npy"]["sha256"] != original_rgb["sha256"]
    assert replay["id"] in {r["id"] for r in client.get("/api/v1/replays").json()}
    assert client.get("/api/v1/replays/" + replay["id"]).json()["status"] == "succeeded"
    manifest = checked(client.get(f"/api/v1/replays/{replay['id']}/export"), 200)
    assert manifest["type"] == "isp_replay_evidence_package"
    assert manifest["source"]["id"] == source["id"]
    response = client.get(f"/api/v1/replays/{replay['id']}/bundle")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert json.loads(archive.read("manifest.json")) == manifest
        raw_member = f"artifacts/{raw_artifact['sha256']}/{raw_artifact['name']}"
        assert archive.read(raw_member) == raw_bytes
    assert not list((service.registry.directory / "downloads").glob("*.zip"))


def test_replay_endpoints_refuse_camera_job_identity_and_cancel_only_replays(api):
    service, client, bootstrap = api
    queued = checked(client.post("/api/v1/runs", json={
        "assembly_id": bootstrap["assemblies"][0]["id"],
        "test_id": bootstrap["tests"][0]["id"], "plugin_id": "response_db",
        "parameters": {"frames": 1, "output_size": [8, 8]},
    }), 202)
    for suffix in ("", "/export", "/bundle"):
        assert client.get("/api/v1/replays/" + queued["id"] + suffix).status_code == 404
    assert client.post("/api/v1/replays/" + queued["id"] + "/cancel", json={}).status_code == 404
    source = finish(service, queued)
    isp_id = next(m["id"] for m in bootstrap["models"] if m["kind"] == "isp")
    replay = checked(client.post("/api/v1/replays", json={
        "name": "cancel API replay", "source_kind": "run", "source_id": source["id"],
        "isp_model_id": isp_id,
    }), 202)
    cancelled = checked(client.post("/api/v1/replays/" + replay["id"] + "/cancel", json={}), 200)
    assert cancelled["status"] == "cancelled"
    assert service.registry.get_run(source["id"])["status"] == "succeeded"


def test_sdk_uses_real_api_templates_upload_and_replay_routes(api, tmp_path):
    service, client, bootstrap = api
    # The in-process HTTP transport replaces socket construction only; the SDK
    # methods, HTTP handlers, registry, and camera/ISP execution are real.
    sdk = PlatformClient.__new__(PlatformClient)
    sdk.http = client
    metadata = sdk.template("spectral-image", assembly_id=bootstrap["assemblies"][0]["id"])
    path = tmp_path / "explicit-spectrum.npy"
    path.write_bytes(npy_bytes(np.full((8, 8, 5), 1e-4, dtype="<f8")))
    image = sdk.import_spectral_image(metadata, path)
    assert image["shape"] == [8, 8, 5]
    fixture = sdk.template("spatial-scene", assembly_id=bootstrap["assemblies"][0]["id"])
    fixture["shape"] = [8, 8]
    assert sdk.create_spectral_fixture(fixture)["shape"] == [8, 8, 5]
    source = finish(service, sdk.submit(
        bootstrap["assemblies"][0]["id"], bootstrap["tests"][0]["id"],
        parameters={"frames": 1, "output_size": [8, 8]},
    ))
    assert any(item["source_id"] == source["id"] for item in sdk.replay_sources())
    isp_id = next(m["id"] for m in bootstrap["models"] if m["kind"] == "isp")
    replay = finish(service, sdk.submit_replay("run", source["id"], isp_id))
    assert sdk.wait_replay(replay["id"], timeout=1)["status"] == "succeeded"
    assert sdk.export_replay(replay["id"])["source"]["id"] == source["id"]
    assert sdk.replay_bundle(replay["id"]).startswith(b"PK")


def test_registered_capture_dark_metadata_replay_bundle_and_state_restore(api, tmp_path):
    service, client, _ = api
    metadata = checked(client.get("/api/v1/templates/capture"), 200)
    metadata.update(
        name="Simulated RAW/Dark capture for ISP replay, not hardware evidence",
        source_kind="simulated", source_uri="fixture://api-capture-replay",
        license="MIT; authored synthetic implementation fixture",
        method_and_version="response_db_v1:synthetic-capture-replay-test",
        dataset_ids=["api-simulated-capture-replay"],
    )
    metadata["observation"]["source_kind"] = "simulated"
    metadata["observation"]["dark_conditions"] = deepcopy(metadata["observation"]["conditions"])
    source = finish(service, checked(client.post("/api/v1/runs", json={
        "assembly_id": metadata["assembly_id"], "test_id": metadata["test_id"],
        "parameters": {"frames": 4, "output_size": [64, 64], "seed": 815},
    }), 202))
    raw_artifact = next(a for a in source["artifacts"] if a["name"] == "raw_frames.npy")
    raw_bytes = client.get(raw_artifact["url"]).content
    raw = np.load(io.BytesIO(raw_bytes), allow_pickle=False)
    dark_request = deepcopy(source["request"])
    dark_request.update(seed=1815, spectral_irradiance_w_m2_nm=[0.0] * 5)
    dark = run_plugin("response_db", dark_request)["arrays"]["raw_frames"]
    dark_bytes = npy_bytes(dark)
    imported = checked(client.post("/api/v1/captures/import", data={
        "metadata": json.dumps(metadata),
    }, files={
        "raw": ("actual-simulated-raw.npy", raw_bytes, "application/octet-stream"),
        "dark": ("matched-simulated-dark.npy", dark_bytes, "application/octet-stream"),
    }), 201)
    capture, measurement, observation_test = (
        imported["capture"], imported["measurement"], imported["test"]
    )
    assert capture["source_kind"] == measurement["source_kind"] == "simulated"
    original_artifacts = {a["name"]: a for a in capture["artifacts"]}
    assert {"raw-capture.npy", "dark-capture.npy", "acquisition-metadata.json"} <= set(
        original_artifacts
    )
    original_bytes = {name: client.get(a["url"]).content for name, a in original_artifacts.items()}
    assert original_bytes["raw-capture.npy"] == raw_bytes
    assert original_bytes["dark-capture.npy"] == dark_bytes
    registered_metadata = json.loads(original_bytes["acquisition-metadata.json"])
    assert registered_metadata["observation"] == metadata["observation"]
    assert registered_metadata["source_kind"] == "simulated"

    replay_sources = checked(client.get("/api/v1/replay-sources"), 200)
    available = next(s for s in replay_sources if s["source_id"] == capture["id"])
    assert available["source_kind"] == "capture"
    assert available["source_provenance"]["source_kind"] == "simulated"
    assert available["raw_shape"] == list(raw.shape) == [4, 64, 64]
    assert available["conditions"] == metadata["observation"]["conditions"]
    profile = checked(client.get("/api/v1/templates/isp-profile", params={
        "assembly_id": metadata["assembly_id"],
    }), 200)
    profile.update(name="Captured RAW replay fixed ISP", revision="capture-replay-1")
    profile["payload"]["white_balance"]["gains_rgb"] = [1.6, 1.0, 1.2]
    model = checked(client.post("/api/v1/models", json=profile), 201)
    queued = checked(client.post("/api/v1/replays", json={
        "name": "Replay registered simulated capture", "source_kind": "capture",
        "source_id": capture["id"], "isp_model_id": model["id"],
    }), 202)
    replay = finish(service, queued)
    assert replay["status"] == "succeeded"
    assert replay["result"]["source_kind"] == "simulated"
    assert replay["result"]["source_id"] == capture["id"]
    assert replay["result"]["acquisition_conditions"] == metadata["observation"]["conditions"]
    assert replay["result"]["physical_validation"] == "not_established"
    assert replay["result"]["uncertainty"] == {"kind": "unknown"}
    assert replay["result"]["raw_sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    expected = process_raw(raw, queued["source"]["raw_format"], queued["profile"],
                           geometry=queued["source"]["geometry"])["arrays"]["rgb_frames"]
    rgb_artifact = next(a for a in replay["artifacts"] if a["name"] == "rgb_frames.npy")
    observed = np.load(io.BytesIO(client.get(rgb_artifact["url"]).content), allow_pickle=False)
    np.testing.assert_array_equal(observed, expected)
    assert observed.shape == (4, 64, 64, 3)
    assert not np.array_equal(observed[0], observed[1])
    np.testing.assert_array_equal(
        np.load(io.BytesIO(client.get(original_artifacts["raw-capture.npy"]["url"]).content),
                allow_pickle=False), raw,
    )

    manifest = checked(client.get(f"/api/v1/replays/{replay['id']}/export"), 200)
    assert manifest["source"]["id"] == capture["id"]
    bundle = client.get(f"/api/v1/replays/{replay['id']}/bundle")
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert json.loads(archive.read("manifest.json")) == manifest
        for name, artifact in original_artifacts.items():
            contents = archive.read(f"artifacts/{artifact['sha256']}/{name}")
            assert contents == original_bytes[name]
            assert hashlib.sha256(contents).hexdigest() == artifact["sha256"]
    assert not list((service.registry.directory / "downloads").glob("*.zip"))
    # An ISP replay cannot stand in for a camera physical prediction or be
    # attached as the camera run in a release, even with registered capture data.
    response = client.post("/api/v1/validations", json={
        "run_id": replay["id"], "measurement_id": measurement["id"],
        "test_id": observation_test["id"],
    })
    assert response.status_code == 422
    response = client.post("/api/v1/releases", json={
        "name": "Invalid replay camera release", "run_ids": [replay["id"]], "validation_ids": [],
    })
    assert response.status_code == 422
    assert client.get("/api/v1/validations").json() == []
    assert client.get("/api/v1/releases").json() == []

    fixture = checked(client.get("/api/v1/templates/spatial-scene", params={
        "assembly_id": metadata["assembly_id"],
    }), 200)
    fixture["shape"] = [8, 8]
    scene = checked(client.post("/api/v1/spectral_images/fixture", json=fixture), 201)
    snapshot = tmp_path / "capture-replay-and-spectral-state.zip"
    audit = service.registry.audit()
    saved = backup_state(service.registry.directory, snapshot)
    restored_path = tmp_path / "restored-capture-replay-and-spectral"
    restored_report = restore_state(snapshot, restored_path)
    assert restored_report["integrity"] == "verified"
    assert restored_report["workers_started"] is False
    assert restored_report["manifest_hash"] == saved["manifest_hash"]
    restored = SQLiteRegistry(restored_path)
    assert restored.audit() == service.registry.audit() == audit
    assert restored.get("captures", capture["id"]) == capture
    assert restored.get("measurements", measurement["id"]) == measurement
    assert restored.get("tests", observation_test["id"]) == observation_test
    assert restored.get("spectral_images", scene["id"]) == scene
    assert restored.get_run(replay["id"]) == replay
    for artifact in [*capture["artifacts"], *scene["artifacts"], *replay["artifacts"]]:
        assert restored.blob_path(artifact["sha256"]).read_bytes() == (
            service.registry.blob_path(artifact["sha256"]).read_bytes()
        )
