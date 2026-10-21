"""Actual API/Worker, immutable calibration and retained-RAW v2 replay boundaries."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

import numpy as np
import pytest
from test_imaging_api import api as api
from test_imaging_api import checked, finish, spatial_run

from camera_platform.contracts import AssemblyCreate
from camera_platform.isp import default_profile, process_raw
from camera_platform.isp_replay_v2 import execute, prepare
from camera_platform.registry import IntegrityError
from camera_platform.sdk import PlatformClient


def advanced(api, *, calibrated=True):
    service, client, _ = api
    assembly, scene, original = spatial_run(api)
    sdk = PlatformClient.__new__(PlatformClient)
    sdk.http = client
    params = {"source_kind": "run", "source_id": original["id"]}
    calibration = None
    if calibrated:
        document = sdk.isp_calibration_template(**params)
        calibration = sdk.import_isp_calibration(document)
    document = sdk.advanced_isp_template(
        **params, calibration_id=calibration["id"] if calibration else None
    )
    document["payload"]["linear_sharpen"]["mode"] = "bounded_detail_v1"
    model = checked(client.post("/api/v1/models", json=document), 201)
    request = {**params, "isp_model_id": model["id"], "name": "same RAW · advanced ISP"}
    return assembly, original, calibration, model, request


def array(service, job, name):
    descriptor = next(a for a in job["artifacts"] if a["name"] == name + ".npy")
    content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == descriptor["sha256"]
    return np.load(io.BytesIO(content), allow_pickle=False)


def test_native_raw_advanced_replay_baseline_stages_masks_and_bundle(api):
    service, client, _ = api
    _, original, calibration, model, request = advanced(api)
    queued = checked(client.post("/api/v1/replays", json=request), 202)
    raw_sha = queued["source"]["raw_sha256"]
    original_bytes = service.registry.blob_path(raw_sha).read_bytes()
    result = finish(service, queued)
    assert result["plugin_id"] == "isp_replay_v2"
    summary = result["result"]["summary"]
    assert summary["calibration_snapshot"] == calibration
    assert summary["physical_validation"] == "not_established"
    assert summary["uncertainty"] == {"kind": "unknown"}
    assert summary["raw_sha256"] == raw_sha
    raw = np.load(io.BytesIO(original_bytes), allow_pickle=False)
    profile = default_profile()
    for key in ("white_balance", "ccm", "tone"):
        profile[key] = model["payload"][key]
    baseline = process_raw(
        raw, queued["source"]["raw_format"], profile, geometry=queued["source"]["geometry"]
    )
    np.testing.assert_array_equal(
        array(service, result, "baseline_rgb_frames"), baseline["arrays"]["rgb_frames"]
    )
    assert summary["stage_outputs"]["raw_denoise"]["applied"] is True
    assert array(service, result, "rgb_frames").shape == (2, 16, 20, 3)
    assert array(service, result, "rgb_valid_mask_frames").dtype.kind == "b"
    assert len(summary["stage_previews"]) >= 2 * 3
    assert service.registry.blob_path(raw_sha).read_bytes() == original_bytes
    assert service.registry.get_run(original["id"]) == original
    # Cache lookup reuses this replay after validating all original inputs.
    cached = checked(client.post("/api/v1/replays", json=request), 202)
    assert cached["id"] == result["id"]
    response = client.get(f"/api/v1/replays/{result['id']}/bundle")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["isp_calibration"] == calibration
        for artifact in result["artifacts"]:
            payload = bundle.read(f"artifacts/{artifact['sha256']}/{artifact['name']}")
            assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]


def test_v2_all_new_stages_bypass_equals_v1_and_needs_no_calibration(api):
    service, client, _ = api
    _, _, _, model, request = advanced(api, calibrated=False)
    edited = deepcopy(model)
    edited = {k: v for k, v in edited.items() if k in service.model_contract.model_fields}
    edited.update(revision="all-bypass", parents=[model["id"]])
    edited["payload"]["linear_sharpen"]["mode"] = "bypass"
    new = checked(client.post("/api/v1/models", json=edited), 201)
    request["isp_model_id"] = new["id"]
    result = finish(service, checked(client.post("/api/v1/replays", json=request), 202))
    np.testing.assert_array_equal(
        array(service, result, "rgb_frames"), array(service, result, "baseline_rgb_frames")
    )


@pytest.mark.parametrize("field", ["profile", "source", "calibration_snapshot", "input_artifacts"])
def test_worker_rebuilds_the_exact_frozen_request(api, field):
    service, _, _ = api
    *_, request = advanced(api)
    frozen = prepare(service, request)
    modified = deepcopy(frozen)
    if field == "profile":
        modified[field]["raw_denoise"]["strength"] = 0.123
    elif field == "source":
        modified[field]["geometry"]["crop_origin"][0] += 2
    elif field == "calibration_snapshot":
        modified[field]["name"] = "altered"
    else:
        modified[field] = []
    with pytest.raises(ValueError, match="Frozen advanced replay"):
        execute(service, modified)


def test_calibration_blob_damage_refused_at_submit_cache_and_worker(api):
    service, client, _ = api
    _, _, calibration, _, request = advanced(api)
    result = finish(service, checked(client.post("/api/v1/replays", json=request), 202))
    frozen = prepare(service, request)
    descriptor = calibration["artifacts"][0]
    path = service.registry.blob_path(descriptor["sha256"])
    saved = path.read_bytes()
    path.write_bytes(b"damaged calibration evidence")
    try:
        assert client.post("/api/v1/replays", json=request).status_code in {409, 422}
        with pytest.raises((ValueError, IntegrityError)):
            execute(service, frozen)
        assert client.get(f"/api/v1/replays/{result['id']}/bundle").status_code in {409, 422}
    finally:
        path.write_bytes(saved)


def test_v2_isp_is_explicitly_replay_only_in_camera_composer(api):
    _, client, _ = api
    assembly, _, _, model, _ = advanced(api)
    payload = {
        key: deepcopy(value)
        for key, value in assembly.items()
        if key in AssemblyCreate.model_fields
    }
    payload.update(name="unsupported acquisition ISP", revision="v2-unsupported")
    # Models are always one Lens/Sensor/ISP each; identify the old ISP by its slot.
    old = next(
        m
        for m in client.get("/api/v1/models").json()
        if m["id"] in assembly["model_ids"] and m["kind"] == "isp"
    )
    payload["model_ids"] = [model["id"] if i == old["id"] else i for i in assembly["model_ids"]]
    created = checked(client.post("/api/v1/assemblies", json=payload), 201)
    assert any(i["code"] == "isp_replay_only" for i in created["compatibility"]["issues"])


def test_damaged_cached_output_is_rejected_without_new_execution(api):
    service, client, _ = api
    *_, request = advanced(api)
    result = finish(service, checked(client.post("/api/v1/replays", json=request), 202))
    artifact = next(a for a in result["artifacts"] if a["name"] == "rgb_frames.npy")
    path = service.registry.blob_path(artifact["sha256"])
    saved = path.read_bytes()
    count = len(service.registry.list_runs())
    path.write_bytes(b"damaged cached RGB")
    try:
        assert client.post("/api/v1/replays", json=request).status_code in {409, 422}
        assert len(service.registry.list_runs()) == count
    finally:
        path.write_bytes(saved)


def test_calibration_reference_raw_and_parent_history_remain_in_reused_bundle(api):
    service, client, _ = api
    _, original, parent, _, _ = advanced(api)
    document = checked(
        client.get(
            "/api/v1/templates/isp-calibration",
            params={
                "source_kind": "run",
                "source_id": original["id"],
            },
        ),
        200,
    )
    document.update(revision="child", parents=[parent["id"]], evidence_refs=[])
    child = checked(client.post("/api/v1/isp-calibrations", json=document), 201)
    target = finish(
        service,
        checked(
            client.post(
                "/api/v1/runs",
                json={
                    "assembly_id": original["assembly_id"],
                    "test_id": original["test_id"],
                    "scene_id": original["scene_id"],
                    "plugin_id": "spatial_scene_v1",
                    "parameters": {**original["parameters"], "seed": 102},
                },
            ),
            202,
        ),
    )
    profile = checked(
        client.get(
            "/api/v1/templates/isp-profile",
            params={
                "version": 2,
                "source_kind": "run",
                "source_id": target["id"],
                "calibration_id": child["id"],
            },
        ),
        200,
    )
    model = checked(client.post("/api/v1/models", json=profile), 201)
    result = finish(
        service,
        checked(
            client.post(
                "/api/v1/replays",
                json={
                    "name": "same camera different RAW",
                    "source_kind": "run",
                    "source_id": target["id"],
                    "isp_model_id": model["id"],
                },
            ),
            202,
        ),
    )
    response = client.get(f"/api/v1/replays/{result['id']}/bundle")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert {r["id"] for r in manifest["isp_calibration_lineage"]} == {parent["id"], child["id"]}
        assert [r["id"] for r in manifest["isp_calibration_sources"]] == [original["id"]]
        for record in [original, parent, child]:
            for a in record["artifacts"]:
                data = bundle.read(f"artifacts/{a['sha256']}/{a['name']}")
                assert hashlib.sha256(data).hexdigest() == a["sha256"]


def test_calibration_and_profile_unknown_inputs_fail_and_v1_template_stays_v1(api):
    _, client, _ = api
    assert (
        client.get("/api/v1/templates/isp-profile").json()["payload"]["schema"]
        == "camera_isp_profile_v1"
    )
    assert client.get("/api/v1/templates/isp-profile", params={"version": 2}).status_code == 422
    assert client.get("/api/v1/templates/isp-profile", params={"version": 9}).status_code == 422
    assert client.post("/api/v1/isp-calibrations", json={}).status_code == 422
