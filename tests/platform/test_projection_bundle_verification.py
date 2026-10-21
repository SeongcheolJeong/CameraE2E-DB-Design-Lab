"""The HTTP verifier must reject incomplete or rehashed Camera evidence ZIPs."""

import copy
import hashlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.registry import digest
from camera_platform.sdk import PlatformClient


@pytest.fixture(scope="module")
def verifier():
    path = Path(__file__).resolve().parents[2] / "tools/verify_camera_scene_projection.py"
    spec = importlib.util.spec_from_file_location("projection_bundle_verifier", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def evidence(tmp_path):
    # Registry/export fixtures exercise real HTTP and original-byte packaging.
    # They are not simulated optical results or physical-camera evidence.
    app = create_app(tmp_path / "state", start_worker=False, seed_demo=False)
    registry = app.state.service.registry

    def blob(content, name):
        return registry.store_blob(content, name, "application/octet-stream")

    def finished(document, artifacts):
        queued = registry.submit(document, digest(document))
        claimed = registry.claim()
        assert claimed["id"] == queued["id"]
        return registry.finish(queued["id"], result={"artifacts": artifacts})

    ri = finished({
        "job_kind": "characterization", "plugin_id": "rayoptics_lens_v1",
        "input_artifacts": [blob(b'{"fixture":"ri"}', "factory-request.json")],
    }, [blob(b"fixture RI response", "relative-illumination.npy")])
    # Equal bytes with different meanings must retain both archive paths.
    psf = finished({
        "job_kind": "characterization", "plugin_id": "prescription_psf_v1",
        "input_artifacts": [blob(b'{"fixture":"psf"}', "factory-request.json")],
    }, [blob(b"fixture array", "fine_opd_nm.npy"), blob(b"fixture array", "psf_kernel.npy")])
    ancestor = registry.put("models", {"name": "Original design", "parents": []})
    parent = registry.put("models", {
        "name": "Radiometry parent", "parents": [ancestor["id"]], "factory_job_id": ri["id"],
    })
    lens = registry.put("models", {
        "name": "Composite field Lens", "parents": [parent["id"]],
        "psf_factory_job_id": psf["id"],
    })
    sensor = registry.put("models", {"name": "Sensor fixture", "parents": []})
    originals = [blob(b"original irradiance", "spectral-image.npy"),
                 blob(b"projected irradiance", "spectral-image.npy")]
    camera = finished({
        "plugin_id": "spatial_scene_v1", "model_snapshots": [lens, sensor],
    }, [blob(b"fixture RAW", "raw_frames.npy"), *originals])
    projection_input = blob(b"frozen render request", "scene-request.json")
    projection = finished({
        "job_kind": "spectral_scene_projection", "input_artifacts": [projection_input],
        "plugin_id": "stored_spectral_radiance_projection_v1",
    }, originals)
    render = finished({
        "job_kind": "spectral_scene_render", "input_artifacts": [projection_input],
    }, originals[:1])
    with TestClient(app) as http:
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = http
        response = http.get("/api/v1/runs/" + camera["id"] + "/bundle")
        assert response.status_code == 200, response.text
        yield SimpleNamespace(
            client=sdk, camera=camera, projection=projection, render=render,
            originals=originals, parent=parent, ancestor=ancestor, lens=lens,
            factories=[ri, psf], archive=response.content,
        )


def archive_parts(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        entries = {n: archive.read(n) for n in archive.namelist() if n != "manifest.json"}
    return manifest, entries


def packed(manifest, entries, *, duplicate=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, content in entries.items():
            archive.writestr(name, content)
        if duplicate is not None:
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr(duplicate, entries[duplicate])
    return output.getvalue()


def served(evidence, content):
    def get(path):
        assert path.endswith("/bundle")
        return httpx.Response(
            200, content=content, request=httpx.Request("GET", "http://test" + path),
        )

    # Model/Factory reads still use the real immutable registry; only ZIP bytes change.
    return SimpleNamespace(get=evidence.client.get, http=SimpleNamespace(get=get))


def rehash_camera(manifest):
    manifest["package_hash"] = digest({
        key: manifest[key] for key in ("run", "model_lineage", "factory_jobs")
    })


def test_actual_camera_export_binds_full_recursive_lineage_and_factory_originals(
    evidence, verifier,
):
    result = verifier.bundle_proof(evidence.client, "runs", evidence.camera, evidence.originals)
    manifest, entries = archive_parts(evidence.archive)
    assert result["package_hash"] == manifest["package_hash"] != digest(evidence.camera)
    assert result["models_verified"] == 4
    assert result["factory_jobs_verified"] == 2
    assert result["artifacts_verified"] == len([n for n in entries if n.startswith("artifacts/")])
    assert result["artifacts_verified"] == 8


@pytest.mark.parametrize("record,route", [
    ("projection", "scene_projections"), ("projection", "runs"),
    ("render", "scene_renders"), ("render", "runs"),
])
def test_projection_and_render_keep_job_hash_including_generic_route_aliases(
    evidence, verifier, record, route,
):
    job = getattr(evidence, record)
    result = verifier.bundle_proof(evidence.client, route, job, job["artifacts"])
    assert result["package_hash"] == digest(job)
    assert result["models_verified"] == result["factory_jobs_verified"] == 0
    assert result["artifacts_verified"] == len(job["artifacts"]) + len(job["input_artifacts"])


@pytest.mark.parametrize("change", [
    "missing_parent", "missing_ancestor", "changed_parent", "duplicate_parent",
    "missing_factory", "changed_factory", "factory_input_omitted", "root_snapshot_changed",
])
def test_rehashed_partial_or_modified_lineage_is_rejected(evidence, verifier, change):
    manifest, entries = archive_parts(evidence.archive)
    if change.startswith("missing_") and change != "missing_factory":
        identity = evidence.parent["id"] if change == "missing_parent" else evidence.ancestor["id"]
        manifest["model_lineage"] = [m for m in manifest["model_lineage"] if m["id"] != identity]
    elif change == "changed_parent":
        next(m for m in manifest["model_lineage"] if m["id"] == evidence.parent["id"])["name"] = "X"
    elif change == "duplicate_parent":
        manifest["model_lineage"].append(copy.deepcopy(evidence.parent))
    elif change == "missing_factory":
        manifest["factory_jobs"].pop()
    elif change == "changed_factory":
        manifest["factory_jobs"][0]["plugin_id"] = "different_solver"
    elif change == "factory_input_omitted":
        manifest["factory_jobs"][0]["input_artifacts"] = []
    else:
        next(m for m in manifest["model_lineage"] if m["id"] == evidence.lens["id"])["parents"] = []
    rehash_camera(manifest)
    with pytest.raises(RuntimeError, match="lineage changed"):
        verifier.bundle_proof(served(evidence, packed(manifest, entries)), "runs",
                              evidence.camera, evidence.originals)


@pytest.mark.parametrize("field", ["model_lineage", "factory_jobs"])
def test_half_manifest_cannot_fall_back_to_legacy_job_hash(evidence, verifier, field):
    manifest, entries = archive_parts(evidence.archive)
    del manifest[field]
    manifest["package_hash"] = digest(evidence.camera)
    with pytest.raises(RuntimeError, match="lineage changed"):
        verifier.bundle_proof(served(evidence, packed(manifest, entries)), "runs",
                              evidence.camera, evidence.originals)


def test_camera_with_complete_lineage_still_rejects_old_job_only_hash(evidence, verifier):
    manifest, entries = archive_parts(evidence.archive)
    manifest["package_hash"] = digest(evidence.camera)
    with pytest.raises(RuntimeError, match="manifest hash changed"):
        verifier.bundle_proof(served(evidence, packed(manifest, entries)), "runs",
                              evidence.camera, evidence.originals)


@pytest.mark.parametrize("category", ["input_artifacts", "artifacts"])
@pytest.mark.parametrize("change", ["missing", "same_size_corruption"])
def test_parent_factory_originals_must_be_present_and_unchanged(
    evidence, verifier, category, change,
):
    manifest, entries = archive_parts(evidence.archive)
    original = evidence.factories[0][category][0]
    path = "artifacts/" + original["sha256"] + "/" + original["name"]
    if change == "missing":
        del entries[path]
    else:
        entries[path] = b"x" * original["bytes"]
        assert hashlib.sha256(entries[path]).hexdigest() != original["sha256"]
    with pytest.raises(RuntimeError, match="original (artifact paths|bytes) changed"):
        verifier.bundle_proof(served(evidence, packed(manifest, entries)), "runs",
                              evidence.camera, evidence.originals)


def test_equal_hash_semantic_alias_and_duplicate_zip_paths_are_checked(evidence, verifier):
    manifest, entries = archive_parts(evidence.archive)
    original = evidence.factories[1]["artifacts"][0]
    path = "artifacts/" + original["sha256"] + "/" + original["name"]
    duplicate = packed(manifest, entries, duplicate=path)
    with pytest.raises(RuntimeError, match="duplicate semantic paths"):
        verifier.bundle_proof(
            served(evidence, duplicate), "runs", evidence.camera, evidence.originals,
        )
    del entries[path]
    with pytest.raises(RuntimeError, match="original artifact paths changed"):
        verifier.bundle_proof(served(evidence, packed(manifest, entries)), "runs",
                              evidence.camera, evidence.originals)


def test_modified_job_and_wrong_evidence_type_are_rejected(evidence, verifier):
    for change in ("run", "type"):
        manifest, entries = archive_parts(evidence.archive)
        if change == "run":
            manifest["run"]["plugin_id"] = "different_runtime"
            rehash_camera(manifest)
        else:
            manifest["type"] = "scene_projection_evidence_package"
        with pytest.raises(RuntimeError, match="completed job|evidence type changed"):
            verifier.bundle_proof(served(evidence, packed(manifest, entries)), "runs",
                                  evidence.camera, evidence.originals)
