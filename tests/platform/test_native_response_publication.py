"""Source-bundle publication and fixture-only native Lens/Sensor contract path."""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import factory, imaging, prescription_psf
from camera_platform.api import create_app
from camera_platform.composer import check_assembly
from camera_platform.contracts import AssemblyCreate, ModelCreate
from camera_platform.demo import PROVENANCE, seed
from camera_platform.native_model_evidence import verify_models
from camera_platform.native_response_publication import (
    extract_native_response_bundle,
    publish_native_response_bundle,
)
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient
from camera_platform.service import PlatformService


def _finish(service, queued):
    job = service.registry.claim()
    assert job["id"] == queued["id"]
    return service.registry.finish(job["id"], result=service.execute_run(job))


@pytest.fixture(scope="module", name="native_workflow")
def _native_workflow(tmp_path_factory):
    pytest.importorskip("rayoptics")
    pytest.importorskip("prysm")
    service = PlatformService(tmp_path_factory.mktemp("native-source-publication"))
    seed(service)
    original = service.registry.list("assemblies")[0]
    baseline = next(
        service.registry.get("models", identity) for identity in original["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    prescription = prescription_psf.request_template(version=3)
    prescription["prescription"]["sampling"].update(polar_rings=20, azimuth_samples=20)
    ray_input = factory.template(service, "rayoptics_lens_v1", request_version=3)
    ray_input["request"] = prescription["prescription"]
    ray = _finish(service, factory.submit(service, ray_input))
    psf_input = factory.template(service, "prescription_psf_v1", request_version=3)
    psf_input["request"] = prescription
    psf = _finish(service, factory.submit(service, psf_input))
    assert ray["result"]["summary"]["status"] == "PROPOSED"
    assert psf["result"]["summary"]["status"] == "PROPOSED"
    conditions = copy.deepcopy(original["conditions"])
    conditions["f_number"] = {
        "value": psf["result"]["summary"]["geometric_reference_f_number"],
        "unit": "1",
    }
    parent = factory.publish(service, {
        "job_id": ray["id"], "name": "Fixture source-bound radiometry", "revision": "1",
        "conditions": {key: value for key, value in conditions.items()
                       if key not in {"exposure_s", "gain"}},
    })
    lens = factory.publish(service, {
        "job_id": psf["id"], "base_lens_model_id": parent["id"],
        "name": "Fixture source-bound joint PSF", "revision": "1",
        "conditions": conditions, "condition_scope": "optical_response",
    })
    yield {"service": service, "lens": lens, "sensor": baseline,
           "assembly": {**original, "conditions": conditions}}


def _write(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(root: Path, lens: dict, revision: str) -> str:
    wave = lens["payload"]["wavelength_nm"]
    pitch = lens["payload"]["psf"]["sample_pitch_um"]
    response = {
        "schema": "camera_native_pixel_response_v1",
        "site_names": ["R", "Gr", "Gb", "B"],
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": pitch,
        "reference_plane": "sensor_surface_before_pixel_optics",
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "polarization": "unpolarized",
        "response_basis": "independent_site_collected_electrons_per_incident_pixel_photon",
        "wavelength_nm": wave,
        "theta_deg": [0, 30],
        "phi_deg": [0, 90, 180, 270],
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
        "values": [
            [
                [[0.20, 0.40, 0.45, 0.30] for _ in range(4)],
                [[0.18, 0.35, 0.39, 0.27] for _ in range(4)],
            ]
            for _ in wave
        ],
        "source": {
            "kind": "simulated",
            "description": "Fixture-only independent-site solver table; no physical validation",
        },
    }
    response_sha = _write(root / "response.json", response)
    source_files = []
    for role in ("solver_output", "geometry", "materials", "collection"):
        original = root / "original" / f"{role}.txt"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text(f"fixture {role} for {revision}\n")
        source_files.append({
            "path": original.relative_to(root).as_posix(),
            "role": role,
            "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        })
    qualification = {
        "schema": "camera_native_response_qualification_v1",
        "kind": "solver", "status": "passed",
        "response_sha256": response_sha,
        "source_files_sha256": {
            entry["path"]: entry["sha256"] for entry in source_files
        },
        "checks": {key: True for key in (
            "absolute_photon_normalization", "site_coordinate_mapping",
            "wavelength_and_angle_support", "independent_site_basis",
            "collection_accounted_once", "uncertainty_or_numerical_convergence",
        )},
        "limitations": ["Fixture declarations have no physical product accuracy"],
    }
    qualification_sha = _write(root / "qualification.json", qualification)
    manifest = {
        "schema": "camera_native_response_bundle_v1",
        "sensor": {
            "component_id": "fixture-sensor", "revision_id": revision,
            "pixel_pitch_um": pitch,
            "site_index_tile": [[0, 1], [2, 3]],
        },
        "response": {"path": "response.json", "sha256": response_sha},
        "source_files": source_files,
        "qualification": {
            "path": "qualification.json", "sha256": qualification_sha,
        },
    }
    return _write(root / "manifest.json", manifest)


def ready_publication_fixture(tmp_path: Path, native_workflow: dict,
                              *, revision: str = "r1") -> tuple:
    """A real Factory Lens plus explicitly identified, synthetic readout source."""
    service = native_workflow["service"]
    lens = native_workflow["lens"]
    baseline = native_workflow["sensor"]
    source_asset = service.create_asset({
        **PROVENANCE,
        "name": "Fixture matching readout " + revision,
        "kind": "sensor",
        "source_kind": "simulated",
        "source_uri": "fixture://readout/" + revision,
        "method_and_version": "fixture_readout:1",
        "properties": {"component_id": "fixture-sensor", "revision_id": revision},
    })
    base_data = {
        key: copy.deepcopy(baseline[key]) for key in ModelCreate.model_fields
    }
    base_data.update(
        asset_id=source_asset["id"],
        name="Fixture matching readout " + revision,
        revision=revision,
        parents=[],
        source_kind="simulated",
        source_uri="fixture://readout/" + revision,
        method_and_version="fixture_readout:1",
    )
    base_data["properties"].update(
        active_height={"value": 0.024, "unit": "mm"},
        active_width={"value": 0.024, "unit": "mm"},
    )
    readout = service.create_model(base_data)
    bundle = tmp_path / ("bundle-" + revision)
    bundle.mkdir()
    manifest_sha = _bundle(bundle, lens, revision)
    return service, bundle, lens, readout, manifest_sha


def _publish(case):
    service, bundle, lens, readout, manifest_sha = case
    return publish_native_response_bundle(
        service, bundle,
        expected_manifest_sha256=manifest_sha,
        lens_model_id=lens["id"], readout_model_id=readout["id"],
        name="Fixture collected-EQE composite", revision="1",
        license="Fixture test only; no commercial rights",
    )


def test_source_qualified_draft_is_retrievable_and_bound_after_restart(
    tmp_path, native_workflow,
):
    case = ready_publication_fixture(tmp_path, native_workflow)
    service, bundle, lens, readout, manifest_sha = case
    published = _publish(case)
    assert _publish(case)["model_id"] == published["model_id"]
    assert published["approval_status"] == "draft"
    assert published["physical_validation"] == "not_established"
    assert published["product_ranking_allowed"] is False
    model = service.registry.get("models", published["model_id"])
    assert model["payload"]["native_pixel_response"]["values"][0][0][0][1:3] == [
        0.40, 0.45
    ]
    asset = service.registry.get("assets", published["asset_id"])
    assert asset["properties"]["native_response_bundle"]["manifest_sha256"] == manifest_sha
    for descriptor in asset["native_response_source_artifacts"]:
        bound = service.registry.artifact_binding(
            descriptor["sha256"], owner_kind="assets", owner_id=asset["id"]
        )
        assert bound["path"].read_bytes() == (bundle / (
            "manifest.json" if descriptor["name"] == "manifest.json" else
            "response.json" if descriptor["name"] == "response.json" else
            "qualification.json" if descriptor["name"] == "qualification.json" else
            "original/" + [
                "solver_output", "geometry", "materials", "collection"
            ][int(descriptor["name"].split("-")[1])] + ".txt"
        )).read_bytes()
    restarted = PlatformService(service.registry.directory, workspace=service.workspace)
    verify_models(restarted, [lens, model])
    assert restarted.registry.get("models", model["id"]) == model


def test_partial_asset_publication_retries_without_duplicate(monkeypatch, tmp_path,
                                                              native_workflow):
    case = ready_publication_fixture(tmp_path, native_workflow, revision="retry-r2")
    service = case[0]
    original = service.create_model
    calls = 0

    def fail_once(payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fixture one-time model write failure")
        return original(payload)

    monkeypatch.setattr(service, "create_model", fail_once)
    with pytest.raises(RuntimeError, match="one-time"):
        _publish(case)
    source_uri = "native-response-bundle://sha256/" + case[-1]
    assert len([a for a in service.registry.list("assets")
                if a.get("source_uri") == source_uri]) == 1
    result = _publish(case)
    assert calls == 2
    assert len([a for a in service.registry.list("assets")
                if a.get("source_uri") == source_uri]) == 1
    assert len([m for m in service.registry.list("models")
                if m.get("asset_id") == result["asset_id"]]) == 1


def test_readout_revision_mismatch_refuses_before_asset_write(tmp_path, native_workflow):
    case = ready_publication_fixture(tmp_path, native_workflow, revision="source-r3")
    wrong = native_workflow["sensor"]
    with pytest.raises(ValueError, match="same declared component/revision"):
        publish_native_response_bundle(
            case[0], case[1], expected_manifest_sha256=case[-1],
            lens_model_id=case[2]["id"], readout_model_id=wrong["id"],
            name="Should refuse", revision="1", license="Fixture only",
        )
    assert not any(a["source_uri"].endswith(case[-1])
                   for a in case[0].registry.list("assets"))


def test_zip_extraction_rejects_traversal_duplicate_and_unlisted(tmp_path):
    bundle = tmp_path / "bundle-zip-r4"
    bundle.mkdir()
    _bundle(bundle, {"payload": {"wavelength_nm": [500, 600],
                                "psf": {"sample_pitch_um": 3.0}}}, "zip-r4")
    source_paths = [p for p in bundle.rglob("*") if p.is_file()]
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as writer:
        for path in source_paths:
            writer.write(path, path.relative_to(bundle).as_posix())
    extracted = extract_native_response_bundle(archive, tmp_path / "extracted")
    assert (extracted / "manifest.json").read_bytes() == (bundle / "manifest.json").read_bytes()
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as writer:
        writer.writestr("../escape", "bad")
        writer.writestr("manifest.json", "{}")
    with pytest.raises(ValueError, match="escapes"):
        extract_native_response_bundle(bad, tmp_path / "bad-out")
    with zipfile.ZipFile(bad, "w") as writer:
        writer.writestr("manifest.json", "{}")
        writer.writestr("manifest.json", "{}")
    with pytest.raises(ValueError, match="Duplicate"):
        extract_native_response_bundle(bad, tmp_path / "duplicate-out")
    malformed_manifest = json.loads((bundle / "manifest.json").read_bytes())
    malformed_manifest["source_files"] = 5
    with zipfile.ZipFile(bad, "w") as writer:
        writer.writestr("manifest.json", json.dumps(malformed_manifest))
        for path in source_paths:
            if path.name != "manifest.json":
                writer.write(path, path.relative_to(bundle).as_posix())
    with pytest.raises(ValueError, match="source_files"):
        extract_native_response_bundle(bad, tmp_path / "malformed-out")


def test_corrupt_source_blob_refuses_model_use(tmp_path, native_workflow):
    case = ready_publication_fixture(tmp_path, native_workflow, revision="corrupt-r5")
    published = _publish(case)
    service = case[0]
    model = service.registry.get("models", published["model_id"])
    asset = service.registry.get("assets", published["asset_id"])
    descriptor = asset["native_response_source_artifacts"][3]
    path = service.registry.blob_path(descriptor["sha256"])
    saved = path.read_bytes()
    try:
        path.write_bytes(b"tampered fixture source")
        with pytest.raises(IntegrityError, match="checksum"):
            verify_models(service, [case[2], model])
    finally:
        path.write_bytes(saved)


def test_same_source_bytes_have_distinct_typed_asset_owners(tmp_path, native_workflow):
    first = ready_publication_fixture(tmp_path, native_workflow, revision="owner-r6")
    second = ready_publication_fixture(tmp_path, native_workflow, revision="owner-r7")
    # Original response bytes are identical, while manifests have distinct revisions.
    one, two = _publish(first), _publish(second)
    service = first[0]
    a = service.registry.get("assets", one["asset_id"])
    b = service.registry.get("assets", two["asset_id"])
    descriptor = a["native_response_source_artifacts"][1]
    assert descriptor["sha256"] == b["native_response_source_artifacts"][1]["sha256"]
    for asset in (a, b):
        binding = service.registry.artifact_binding(
            descriptor["sha256"], owner_kind="assets", owner_id=asset["id"]
        )
        assert binding["owner"]["id"] == asset["id"]


def test_duplicate_original_source_bytes_keep_both_paths_and_one_binding(
    tmp_path, native_workflow,
):
    case = ready_publication_fixture(tmp_path, native_workflow, revision="duplicate-r10")
    bundle = case[1]
    (bundle / "original/materials.txt").write_bytes(
        (bundle / "original/geometry.txt").read_bytes()
    )
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    source_hashes = {}
    for item in manifest["source_files"]:
        item["sha256"] = hashlib.sha256((bundle / item["path"]).read_bytes()).hexdigest()
        source_hashes[item["path"]] = item["sha256"]
    qualification = json.loads((bundle / "qualification.json").read_bytes())
    qualification["source_files_sha256"] = source_hashes
    manifest["qualification"]["sha256"] = _write(
        bundle / "qualification.json", qualification
    )
    manifest_sha = _write(bundle / "manifest.json", manifest)
    case = (*case[:-1], manifest_sha)
    published = _publish(case)
    asset = case[0].registry.get("assets", published["asset_id"])
    files = asset["properties"]["native_response_bundle"]["source_files"]
    assert files[1]["path"] == "original/geometry.txt"
    assert files[2]["path"] == "original/materials.txt"
    assert files[1]["sha256"] == files[2]["sha256"]
    assert files[1]["artifact_index"] == files[2]["artifact_index"]
    assert len(asset["native_response_source_artifacts"]) == 6


def test_different_lens_refused_at_assembly_preflight(tmp_path, native_workflow):
    case = ready_publication_fixture(tmp_path, native_workflow, revision="other-lens-r9")
    published = _publish(case)
    service = case[0]
    sensor = service.registry.get("models", published["model_id"])
    original = native_workflow["assembly"]
    isp = next(service.registry.get("models", identity)
               for identity in original["model_ids"]
               if service.registry.get("models", identity)["kind"] == "isp")
    other = copy.deepcopy(case[2])
    other["id"] = "different-factory-lens"
    assembly = {
        "model_ids": [other["id"], sensor["id"], isp["id"]],
        "conditions": original["conditions"],
        "spectral_range": sensor["spectral_range"],
        "raw_format": sensor["raw_format"],
    }
    result = check_assembly(assembly, [other, sensor, isp])
    assert result["status"] == "FAIL"
    assert any(issue["code"] == "native_response_mismatch"
               and "another exact Factory Lens" in issue["message"]
               for issue in result["issues"])


def test_fixture_only_registered_native_raw_uses_published_source_bundle(
    tmp_path, native_workflow,
):
    case = ready_publication_fixture(tmp_path, native_workflow, revision="raw-r11")
    published = _publish(case)
    service, _, lens, _, _ = case
    sensor = service.registry.get("models", published["model_id"])
    original = native_workflow["assembly"]
    isp = next(service.registry.get("models", identity)
               for identity in original["model_ids"]
               if service.registry.get("models", identity)["kind"] == "isp")
    assembly_data = {
        key: copy.deepcopy(original[key]) for key in AssemblyCreate.model_fields
    }
    assembly_data.update(
        name="Fixture-only native source camera",
        revision="1",
        model_ids=[lens["id"], sensor["id"], isp["id"]],
        spectral_range=sensor["spectral_range"],
    )
    assembly = service.create_assembly(assembly_data)
    assert assembly["compatibility"]["status"] == "PASS", assembly["compatibility"]
    scene_request = imaging.fixture_template(service, assembly["id"])
    scene_request.update(shape=[8, 8], pattern="spectral_chart")
    scene = imaging.create_fixture(service, scene_request)
    test = service.create_test(
        imaging.spatial_test_template(service, assembly["id"], scene["id"])
    )
    queued = service.submit_run({
        "assembly_id": assembly["id"], "test_id": test["id"],
        "scene_id": scene["id"], "plugin_id": "spatial_scene_v1",
        "parameters": {"seed": 19, "frames": 1},
    })
    run = _finish(service, queued)
    assert run["status"] == "succeeded"
    artifact = next(item for item in run["artifacts"]
                    if item["name"] == "raw_frames.npy")
    raw = np.load(service.registry.blob_path(artifact["sha256"]), allow_pickle=False)
    assert raw.shape == (1, 8, 8)
    assert raw.dtype.kind in "iu"
    assert run["result"]["summary"]["physical_validation"] == "not_established"


def test_multipart_sdk_import_uses_existing_factory_lens_and_downloads_originals(
    tmp_path, native_workflow,
):
    case = ready_publication_fixture(tmp_path, native_workflow, revision="sdk-r8")
    service, bundle, lens, readout, manifest_sha = case
    archive = tmp_path / "native-response.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as writer:
        for path in bundle.rglob("*"):
            if path.is_file():
                writer.write(path, path.relative_to(bundle).as_posix())
    app = create_app(service.registry.directory, workspace=service.workspace,
                     start_worker=False, seed_demo=False)
    with TestClient(app) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        sdk = PlatformClient.__new__(PlatformClient)
        sdk.http = http
        result = sdk.publish_native_response_bundle(
            archive, expected_manifest_sha256=manifest_sha,
            lens_model_id=lens["id"], readout_model_id=readout["id"],
            name="Fixture SDK native Sensor", revision="1",
            license="Fixture test only; no commercial rights",
        )
        assert result["physical_validation"] == "not_established"
        assert result["product_ranking_allowed"] is False
        asset = app.state.service.registry.get("assets", result["asset_id"])
        for descriptor in asset["native_response_source_artifacts"]:
            received = http.get(descriptor["url"])
            assert received.status_code == 200
            assert hashlib.sha256(received.content).hexdigest() == descriptor["sha256"]
        assert sdk.publish_native_response_bundle(
            archive, expected_manifest_sha256=manifest_sha,
            lens_model_id=lens["id"], readout_model_id=readout["id"],
            name="Fixture SDK native Sensor", revision="1",
            license="Fixture test only; no commercial rights",
        )["model_id"] == result["model_id"]
