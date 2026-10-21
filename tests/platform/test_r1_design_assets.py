"""R1 registered examples execute without historical report or QA state folders."""

from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform.api import create_app
from camera_platform.project_mode import configure
from camera_platform.r1_design_assets import BUNDLE_ENV, catalog, load_sources
from camera_platform.response_chain import evaluate_response

ROOT = Path(__file__).resolve().parents[2]
PACKAGED = ROOT / "src/camera_platform/data/r1"
EXAMPLE = ROOT / "examples/r1"


def _copy_bundle(tmp_path, monkeypatch):
    destination = tmp_path / "portable-r1-bundle"
    shutil.copytree(PACKAGED, destination)
    monkeypatch.setenv(BUNDLE_ENV, str(destination))
    return destination


def test_packaged_example_matches_portable_source():
    files = sorted(p.relative_to(EXAMPLE) for p in EXAMPLE.rglob("*.json") if p.is_file())
    assert len(files) == 16
    assert all((PACKAGED / name).is_file() for name in files)
    for name in files:
        assert hashlib.sha256((EXAMPLE / name).read_bytes()).digest() == hashlib.sha256(
            (PACKAGED / name).read_bytes()).digest()
    registered = catalog(ROOT)
    assert registered["distribution_rights"]["license"] == "MIT"
    assert (registered["distribution_rights"]["status"]
            == "declared_mit_authored_synthetic_source_chain")
    assert len(registered["models"]) == 6
    assert len(registered["components"]["optics"]) == 3
    assert len(registered["components"]["pixels"]) == 2
    assert len(registered["components"]["electricals"]) == 2


def test_two_models_two_scenes_reach_integer_raw_without_qa(tmp_path, monkeypatch):
    _copy_bundle(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "reports").exists()
    assert not (tmp_path / "outputs/camera-platform-stop-v016-qa").exists()
    available = catalog(tmp_path)
    assert len(available["models"]) >= 2 and len(available["scenes"]) >= 2
    outputs = {}
    for scene in available["scenes"][:2]:
        sources = load_sources(tmp_path, [m["id"] for m in available["models"][:2]], scene["id"])
        for model_id, source in sources.items():
            result = evaluate_response(source.rate_request(), source.electrical_model,
                                       exposure_s=.002, analog_gain=1.0,
                                       noise_enabled=False, seed=2601)
            raw = result["arrays"]["raw_dn"]
            assert raw.shape == (16, 16) and raw.dtype.kind in "ui"
            assert source.identity()["model_id"] == model_id
            assert source.identity()["scene_id"] == scene["id"]
            assert "physical_validation" in source.identity()
            source.assert_unchanged()
            outputs[model_id, scene["id"]] = raw
    assert not np.array_equal(outputs["r1-stop-clear-v1", "r1-flat-v1"],
                              outputs["r1-stop-quarter-v1", "r1-flat-v1"])
    assert not np.array_equal(outputs["r1-stop-clear-v1", "r1-flat-v1"],
                              outputs["r1-stop-clear-v1", "r1-checker-v1"])


def test_evaluation_spectrum_is_changed_before_optical_transfer(tmp_path, monkeypatch):
    _copy_bundle(tmp_path, monkeypatch)
    source = load_sources(tmp_path, ["r1-stop-clear-v1"], "r1-checker-v1")["r1-stop-clear-v1"]
    base_radiance = np.asarray(source.scene["spectral_radiance_w_m2_sr_nm"])
    on_axis = np.asarray(source.model["request"]["lens"]["radiometry"][
        "irradiance_per_radiance_sr"])[:, 0]
    request = source.radiance_request(2.0, 1)
    expected = base_radiance[:, ::-1] * np.array([1.15, .85]) * 2.0 * on_axis
    np.testing.assert_array_equal(request["scene"]["spectral_irradiance"], expected)


def test_explicit_r1_project_submit_compare_save_and_reopen(tmp_path, monkeypatch):
    _copy_bundle(tmp_path, monkeypatch)
    state = tmp_path / "my-camera-project"
    configure(state, project_id="r1-portable-smoke")
    app = create_app(state_dir=state, workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["x-platform-token"] = client.get("/api/v1/session").json()["token"]
        template = client.get("/api/v1/design-experiments/template")
        assert template.status_code == 200, template.text
        available = template.json()["catalog"]
        assert len(available["models"]) == 6 and len(available["scenes"]) == 2
        spec = deepcopy(template.json()["specification"])
        spec.update(model_ids=["r1-clear-lowgreen-v1", "r1-clear-8ke-v1"],
                    scene_id="r1-checker-v1", exposures_ms=[2.0], gains=[1.0],
                    training_scales=[1.0], evaluation_scales=[1.0],
                    evaluation_noise_repeats=0, acknowledge_simulation_limits=True)
        spec["limits"].update(minimum_green_dn=0, minimum_green_snr_p10=0,
                              maximum_well_fraction=1, maximum_white_fraction=1)
        queued = client.post("/api/v1/design-experiments", json=spec)
        assert queued.status_code == 202, queued.text
        service = app.state.service
        job = service.registry.claim()
        assert job["id"] == queued.json()["id"]
        result = service.execute_run(job)
        service.registry.finish(job["id"], result=result)
        saved = client.get("/api/v1/design-experiments/" + job["id"])
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["status"] == "succeeded"
        summary = body["result"]["summary"]
        assert set(summary["source_identities"]) == set(spec["model_ids"])
        assert {summary["source_identities"][model_id]["components"]["pixels"]["id"]
                for model_id in spec["model_ids"]} == {
                    "r1-pixel-base-v1", "r1-pixel-lowgreen-v1"}
        assert {summary["source_identities"][model_id]["components"]["electricals"]["id"]
                for model_id in spec["model_ids"]} == {
                    "r1-readout-base-v1", "r1-readout-8ke-v1"}
        assert {r["scene_id"] for r in summary["rows"]} == {spec["scene_id"]}
        assert len(summary["rows"]) >= 2
        for row in summary["rows"]:
            raw = np.load(service.registry.blob_path(row["raw_artifact"]["sha256"]),
                          allow_pickle=False)
            assert raw.dtype.kind in "ui" and raw.shape == (16, 16)
            rgb = np.load(service.registry.blob_path(row["rgb_artifact"]["sha256"]),
                          allow_pickle=False)
            assert rgb.dtype == np.float64 and rgb.shape == (16, 16, 3)
            assert row["isp_profile_sha256"] == job["isp_profile_sha256"]
            detail = client.get(f"/api/v1/design-experiments/{job['id']}/cases/{row['case_id']}")
            assert detail.status_code == 200 and "rgb" in detail.json()["images"]
        assert client.get("/api/v1/design-experiments").json()[0]["id"] == job["id"]
        reopened = client.get("/api/v1/design-experiments/" + job["id"]).json()
        assert reopened["result"] == body["result"]


def test_tampered_portable_input_is_rejected_before_catalog_or_submission(tmp_path, monkeypatch):
    bundle = _copy_bundle(tmp_path, monkeypatch)
    scene = bundle / "scenes/checker.json"
    scene.write_bytes(scene.read_bytes() + b" ")
    with pytest.raises(ValueError, match="manifest hash"):
        catalog(tmp_path)
    with pytest.raises(ValueError, match="manifest hash"):
        load_sources(tmp_path, ["r1-stop-clear-v1"], "r1-checker-v1")


def test_historical_development_state_is_readable_but_cannot_submit_r1(tmp_path):
    state = tmp_path / "outputs/camera-platform-design-experiments-existing"
    app = create_app(state_dir=state, workspace=tmp_path, start_worker=False, seed_demo=False)
    with TestClient(app) as client:
        client.headers["x-platform-token"] = client.get("/api/v1/session").json()["token"]
        assert client.get("/api/v1/design-experiments").status_code == 200
        assert client.get("/api/v1/design-experiments/template").status_code == 422
        assert client.post("/api/v1/design-experiments", json={}).status_code == 422


def _raw_result(source):
    return evaluate_response(source.rate_request(), source.electrical_model,
                             exposure_s=.002, analog_gain=1.0,
                             noise_enabled=False, seed=2601)


def test_legacy_models_preserve_integer_raw_after_component_registration(tmp_path, monkeypatch):
    _copy_bundle(tmp_path, monkeypatch)
    expected = {
        "r1-stop-clear-v1": "e4deeb8a5b0ceda99b29fbe2d7f4e7bd0beac58333d91b14d325eb83f174bfbf",
        "r1-stop-quarter-v1": "340f72c797a2a8249efd22d5aaa02b2be4fd447c6faab5c6a1241cf54fbf8e32",
        "r1-stop-radial-v1": "8f525ac2a88ae4c703abc18c24cf3440d295cd5f5a26d1538485f42712e2d77b",
    }
    for model_id, raw_hash in expected.items():
        source = load_sources(tmp_path, [model_id], "r1-flat-v1")[model_id]
        actual = hashlib.sha256(_raw_result(source)["arrays"]["raw_dn"].tobytes()).hexdigest()
        assert actual == raw_hash
        assert set(source.identity()["components"]) == {"optics", "pixels", "electricals"}
        for component in source.identity()["components"].values():
            assert component["id"] and component["revision"]
            assert len(component["sha256"]) == 64


def test_registered_pixel_and_electrical_substitution_changes_actual_raw(tmp_path, monkeypatch):
    _copy_bundle(tmp_path, monkeypatch)
    ids = ["r1-stop-clear-v1", "r1-clear-lowgreen-v1", "r1-clear-8ke-v1",
           "r1-clear-lowgreen-8ke-v1"]
    sources = load_sources(tmp_path, ids, "r1-flat-v1")
    results = {model_id: _raw_result(source) for model_id, source in sources.items()}
    arrays = {model_id: result["arrays"]["raw_dn"] for model_id, result in results.items()}
    identities = {model_id: result["summary"]["response_identity"]
                  for model_id, result in results.items()}
    for model_id in ids[1:]:
        assert arrays[model_id].dtype.kind in "ui"
        assert not np.array_equal(arrays[ids[0]], arrays[model_id])
        assert sources[ids[0]].identity() != sources[model_id].identity()
    baseline, pixel, electrical, combined = (identities[model_id] for model_id in ids)
    assert baseline["lens"] == pixel["lens"] == electrical["lens"] == combined["lens"]
    assert baseline["psf"] == pixel["psf"] == electrical["psf"] == combined["psf"]
    assert baseline["electrical_model"] == pixel["electrical_model"]
    assert electrical["electrical_model"] == combined["electrical_model"]
    assert baseline["electrical_model"] != electrical["electrical_model"]
    assert baseline["pixel_response"] == electrical["pixel_response"]
    assert pixel["pixel_response"] == combined["pixel_response"]
    assert baseline["pixel_response"] != pixel["pixel_response"]
    component_ids = {model_id: {group: item["id"] for group, item in
                                source.identity()["components"].items()}
                     for model_id, source in sources.items()}
    assert len({item["optics"] for item in component_ids.values()}) == 1
    assert len({item["pixels"] for item in component_ids.values()}) == 2
    assert len({item["electricals"] for item in component_ids.values()}) == 2


def _rewrite_registered_component(bundle, group, component_id, mutate):
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(item for item in manifest["components"][group]
                 if item["id"] == component_id)
    path = bundle / entry["file"]
    document = json.loads(path.read_text())
    mutate(document)
    content = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(content)
    entry["sha256"] = hashlib.sha256(content).hexdigest()
    entry["bytes"] = len(content)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":"))
                             + "\n")


def test_rehashed_baseline_component_mismatch_fails_closed(tmp_path, monkeypatch):
    bundle = _copy_bundle(tmp_path, monkeypatch)
    _rewrite_registered_component(
        bundle, "pixels", "r1-pixel-base-v1",
        lambda pixel: pixel["qe"][1].__setitem__(0, 0.49),
    )
    with pytest.raises(ValueError, match="legacy model differs"):
        catalog(tmp_path)


@pytest.mark.parametrize("group,component_id,field,value", [
    ("pixels", "r1-pixel-lowgreen-v1", "cfa", "BGGR"),
    ("pixels", "r1-pixel-lowgreen-v1", "wavelength_nm", [500, 610]),
    ("pixels", "r1-pixel-lowgreen-v1", "pixel_pitch_um", 2.9),
    ("electricals", "r1-readout-8ke-v1", "cfa", "BGGR"),
])
def test_incompatible_registered_component_fails_closed(
    tmp_path, monkeypatch, group, component_id, field, value
):
    bundle = _copy_bundle(tmp_path, monkeypatch)
    _rewrite_registered_component(
        bundle, group, component_id, lambda document: document.__setitem__(field, value),
    )
    with pytest.raises(ValueError, match="component contracts differ"):
        catalog(tmp_path)


def test_rehashed_readout_electrical_mismatch_fails_closed(tmp_path, monkeypatch):
    bundle = _copy_bundle(tmp_path, monkeypatch)
    _rewrite_registered_component(
        bundle, "electricals", "r1-readout-8ke-v1",
        lambda document: document["readout"].__setitem__("full_well_e", 7000.0),
    )
    with pytest.raises(ValueError, match="electrical model differs"):
        catalog(tmp_path)
