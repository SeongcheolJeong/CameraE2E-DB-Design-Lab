"""DB bridge immutability, exact condition selection and explicit replay admission."""

import hashlib
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import calibration_db as bridge_api
from camera_platform import calibration_db_engine as engine
from camera_platform import calibration_lab as lab
from camera_platform import reference_workshop_job
from camera_platform.api import create_app
from camera_platform.calibration_lab_contracts import template
from camera_platform.registry import IntegrityError, canonical, digest
from camera_platform.sdk import PlatformClient
from camera_platform.worker import execute


@pytest.fixture
def api(tmp_path, monkeypatch):
    app = create_app(tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False)
    service = app.state.service
    # Other agents edit independent staged modules while these contract tests run.
    monkeypatch.setattr(lab, "_source_code", lambda: {"test_adapter": "frozen-v1"})
    service.loaded_environment = lab._source_code()
    with TestClient(app, raise_server_exceptions=False) as http:
        http.headers["X-Platform-Token"] = http.get("/api/v1/session").json()["token"]
        yield service, http


def source(api, tmp_path, monkeypatch, *, conditional=False):
    service, _ = api
    root = tmp_path / "source"
    root.mkdir()
    candidate = {"id": "candidate-00", "status": "succeeded", "label": "frozen camera"}
    summary = {
        "schema": "camera_db_design_study_result_v1",
        "candidates": [candidate],
        "comparison_mode": "aperture_conditions" if conditional else "lens_sensor",
    }
    if conditional:
        summary["aperture_conditions"] = {
            "conditions": [
                {"id": "condition-000", "candidates": [{**candidate, "label": "first condition"}]},
                {"id": "condition-001", "candidates": [{**candidate, "label": "second condition"}]},
            ]
        }
    original = canonical(summary).encode()
    manifest = canonical(
        {
            "schema": "camera_reference_workshop_files_v1",
            "files": {
                "result.json": hashlib.sha256(original).hexdigest(),
                "sensor-input.bin": hashlib.sha256(b"photocharge-original").hexdigest(),
            },
        }
    ).encode()
    frozen = {
        "manifest.json": manifest,
        "result.json": original,
        "sensor-input.bin": b"photocharge-original",
    }
    for name, data in frozen.items():
        (root / name).write_bytes(data)
    summary["manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
    run = service.registry.submit(
        {"job_kind": "reference_workshop", "plugin_id": "camera_db_design_study_v1"}, "source-study"
    )
    assert service.registry.claim()["id"] == run["id"]
    artifact = service.registry.store_blob(
        canonical(summary).encode(), "source-result.json", "application/json"
    )
    run = service.registry.finish(run["id"], result={"summary": summary, "artifacts": [artifact]})
    monkeypatch.setattr(
        reference_workshop_job, "get", lambda _service, identity: service.registry.get_run(identity)
    )
    monkeypatch.setattr(reference_workshop_job, "_directory", lambda _service, _identity: root)
    projection = {
        "electronics": template()["campaign"]["baseline_profile"]["electronics"],
        "pixel_pitch_um": 2.9,
        "cfa": "RGGB",
        "probe_shape": [256, 256],
        "conditions": {"gain": 1.0, "temperature_c": 25.0},
        "applied_f_number": 4.12,
        "parameter_mapping": [],
        "unmapped": ["blur", "isp"],
        "scope": "electronics_only",
    }
    projection["electronics"]["dn_per_e"] = 0.4031

    def prepare(path, supplied, candidate_id, condition_id=None, expected_manifest_sha256=None):
        assert supplied is None or supplied == summary
        assert candidate_id == "candidate-00"
        assert condition_id == ("condition-001" if conditional else None)
        assert expected_manifest_sha256 == summary["manifest_sha256"]
        for name, data in frozen.items():
            if (path / name).read_bytes() != data:
                raise IntegrityError("source bytes changed")
        return {"frozen_files": frozen, "summary": supplied}

    monkeypatch.setattr(engine, "prepare_source", prepare)
    monkeypatch.setattr(engine, "baseline_projection", lambda _: deepcopy(projection))
    source_spec = {"run_id": run["id"], "candidate_id": "candidate-00"}
    if conditional:
        source_spec["condition_id"] = "condition-001"
    return source_spec, root, projection


def create(api, source_spec):
    _, http = api
    response = http.post(bridge_api.ROUTE + "/db-bridges", json={"source": source_spec})
    assert response.status_code == 201, response.text
    bridge = response.json()
    campaign = template()["campaign"]
    campaign["db_bridge_id"] = bridge["id"]
    campaign["source"] = source_spec
    campaign["native_shape"] = [1080, 1920]
    campaign["baseline_profile"]["native_shape"] = [1080, 1920]
    campaign["pixel_pitch_um"] = bridge["projection"]["pixel_pitch_um"]
    campaign["baseline_profile"]["pixel_pitch_um"] = campaign["pixel_pitch_um"]
    campaign["baseline_profile"]["electronics"] = bridge["projection"]["electronics"]
    response = http.post(lab.ROUTE + "/campaigns", json=campaign)
    assert response.status_code == 201, response.text
    return bridge, response.json(), campaign


def applied(service, campaign, *, full_well=None):
    candidate = deepcopy(campaign["baseline_profile"])
    candidate["electronics"]["black_level_dn"] += 8
    if full_well is not None:
        candidate["electronics"]["full_well_e"] = full_well
    candidate["blur"]["field_nodes"][0]["sigma_x_px"] = 1.5
    document = {
        "job_kind": lab.JOB_KIND,
        "name": "fixture fitting",
        "specification": {"campaign_id": campaign["id"]},
        "campaign_snapshot": campaign,
        "development_lineage": [],
        "input_artifacts": [],
    }
    study = service.registry.submit(document, digest(document), force=True)
    assert service.registry.claim()["id"] == study["id"]
    summary = {
        "candidate_profile": candidate,
        "candidate_sha256": digest(candidate),
        "fit": {"status": "fitted", "diagnostics": []},
    }
    artifact = service.registry.store_blob(
        canonical(summary).encode(), "fit-result.json", "application/json"
    )
    service.registry.finish(study["id"], result={"summary": summary, "artifacts": [artifact]})
    return lab.apply(service, study["id"], {})["profile"]


def test_bridge_exact_condition_and_template(api, tmp_path, monkeypatch):
    source_spec, _, _ = source(api, tmp_path, monkeypatch, conditional=True)
    _, http = api
    missing = {k: v for k, v in source_spec.items() if k != "condition_id"}
    response = http.post(lab.ROUTE + "/db-bridges", json={"source": missing})
    assert response.status_code == 422
    assert "condition" in response.text
    response = http.get(
        lab.ROUTE + "/template",
        params={
            "source_run": source_spec["run_id"],
            "candidate": "candidate-00",
            "condition": "condition-001",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["source_context"]["candidate_snapshot"]["label"] == "second condition"
    assert response.json()["campaign_template"]["source"] == source_spec
    bridge, campaign, _ = create(api, source_spec)
    assert bridge["source_context"]["condition_id"] == "condition-001"
    assert "source_summary" not in bridge
    assert campaign["native_shape"] == [1080, 1920]
    assert bridge["projection"]["probe_shape"] == [256, 256]
    assert campaign["db_bridge_sha256"] == bridge["content_hash"]


def test_bridge_artifacts_have_exact_typed_ownership_and_detect_tamper(api, tmp_path, monkeypatch):
    source_spec, _, _ = source(api, tmp_path, monkeypatch)
    service, _ = api
    bridge, _, _ = create(api, source_spec)
    for row in bridge["source_files"].values():
        bound = service.registry.artifact_binding(
            row["sha256"], owner_kind=bridge_api.BRIDGES, owner_id=bridge["id"]
        )
        assert bound["path"].stat().st_size == row["bytes"]
    descriptor = bridge["source_files"]["sensor-input.bin"]
    path = service.registry.blob_path(descriptor["sha256"])
    path.write_bytes(b"changed")
    with pytest.raises(IntegrityError):
        bridge_api.get_bridge(service, bridge["id"])


def test_campaign_rejects_conflicting_db_binding_and_electronics(api, tmp_path, monkeypatch):
    source_spec, _, _ = source(api, tmp_path, monkeypatch)
    _, http = api
    _, _, valid = create(api, source_spec)
    for mutation in ("electronics", "pitch", "gain", "source"):
        bad = deepcopy(valid)
        if mutation == "electronics":
            bad["baseline_profile"]["electronics"]["dn_per_e"] = 0.5
        elif mutation == "pitch":
            bad["pixel_pitch_um"] = bad["baseline_profile"]["pixel_pitch_um"] = 3.0
        elif mutation == "gain":
            bad["conditions"]["gain"] = bad["baseline_profile"]["conditions"]["gain"] = 2
        else:
            bad["source"]["candidate_id"] = "candidate-01"
        response = http.post(lab.ROUTE + "/campaigns", json=bad)
        assert response.status_code == 422, (mutation, response.text)


def test_replay_frozen_source_survives_original_removal_and_keeps_outputs_owned(
    api, tmp_path, monkeypatch
):
    source_spec, root, _ = source(api, tmp_path, monkeypatch)
    service, http = api
    bridge, campaign, _ = create(api, source_spec)
    revision = applied(service, campaign)
    for path in root.iterdir():
        path.unlink()
    root.rmdir()
    observed = []

    def render(payload, profile, directory, seed, on_progress=None):
        observed.append(deepcopy(profile))
        (directory / "baseline").mkdir()
        (directory / "calibrated").mkdir()
        np.save(directory / "baseline" / "raw.npy", np.array([[64, 65]], dtype=np.uint16))
        np.save(directory / "calibrated" / "raw.npy", np.array([[72, 73]], dtype=np.uint16))
        return {
            "schema": "camera_calibration_db_replay_v1",
            "images": [],
            "comparisons": [],
            "applied_parameters": list(bridge_api.TRANSFERRED),
            "not_transferred": ["blur", "isp"],
        }

    monkeypatch.setattr(engine, "render_replay", render)
    sdk = object.__new__(PlatformClient)
    sdk.http = http
    assert sdk.calibration_db_bridge(bridge["id"])["id"] == bridge["id"]
    run = sdk.submit_calibration_db_replay(campaign["id"], revision["id"], seed=9)
    assert service.registry.claim()["id"] == run["id"]
    execute(service, run["id"])
    done = sdk.wait_calibration_db_replay(run["id"], timeout=1, poll_interval=0.01)
    assert done["status"] == "succeeded", done.get("error")
    assert sdk.calibration_db_replay(run["id"])["id"] == done["id"]
    summary = done["result"]["summary"]
    assert summary["db_bridge_sha256"] == bridge["content_hash"]
    assert summary["physical_validation"] == "not_established"
    assert observed[0] == revision["profile"]
    for name in summary["files"]:
        assert http.get(summary["files_base"] + name).status_code == 200
    assert http.get(summary["files_base"] + "unknown.txt").status_code == 404
    detail = http.get(lab.ROUTE + "/campaigns/" + campaign["id"]).json()
    assert detail["db_bridge"]["id"] == bridge["id"]
    assert detail["db_replays"][0]["id"] == run["id"]


def test_replay_requires_saved_applied_compatible_profile_and_explicit_scope(
    api, tmp_path, monkeypatch
):
    source_spec, _, _ = source(api, tmp_path, monkeypatch)
    service, http = api
    _, campaign, _ = create(api, source_spec)
    revision = applied(service, campaign)
    valid = {
        "campaign_id": campaign["id"],
        "profile_id": revision["id"],
        "parameter_scope": "electronics_only",
    }
    for changes in (
        {"parameter_scope": "all"},
        {"seed": True},
        {"seed": -1},
        {"profile_id": "../bad"},
    ):
        response = http.post(lab.ROUTE + "/db-replays", json={**valid, **changes})
        assert response.status_code == 422, response.text
    other = lab.create_campaign(service, template()["campaign"])
    response = http.post(lab.ROUTE + "/db-replays", json={**valid, "campaign_id": other["id"]})
    assert response.status_code == 422
    service.loaded_environment = {"stale": True}
    assert http.post(lab.ROUTE + "/db-replays", json=valid).status_code == 409
    assert http.post(lab.ROUTE + "/db-bridges", json={"source": source_spec}).status_code == 409
    assert service.registry.claim() is None


def test_cancelled_db_replay_cannot_execute_or_download(api, tmp_path, monkeypatch):
    source_spec, _, _ = source(api, tmp_path, monkeypatch)
    service, http = api
    _, campaign, _ = create(api, source_spec)
    revision = applied(service, campaign)
    job = bridge_api.submit(
        service,
        {
            "campaign_id": campaign["id"],
            "profile_id": revision["id"],
            "parameter_scope": "electronics_only",
        },
    )
    response = http.post(lab.ROUTE + "/db-replays/" + job["id"] + "/cancel", json={})
    assert response.status_code == 200
    assert service.registry.claim() is None
    with pytest.raises(ValueError, match="claimed running"):
        bridge_api.execute(service, service.registry.get_run(job["id"]))
    assert (
        http.get(lab.ROUTE + "/db-replays/" + job["id"] + "/files/report.html").status_code == 422
    )


def test_replay_rejects_unapplied_revision_and_changed_physical_capacity(
    api, tmp_path, monkeypatch
):
    source_spec, _, _ = source(api, tmp_path, monkeypatch)
    service, http = api
    _, campaign, _ = create(api, source_spec)
    revision = applied(service, campaign)
    unapplied = service.registry.put(
        lab.PROFILES,
        {k: v for k, v in revision.items() if k not in {"id", "created_at", "content_hash"}},
    )
    bad_capacity = applied(service, campaign, full_well=20000)
    for profile, expected in ((unapplied, "previously applied"), (bad_capacity, "full_well_e")):
        response = http.post(
            lab.ROUTE + "/db-replays",
            json={
                "campaign_id": campaign["id"],
                "profile_id": profile["id"],
                "parameter_scope": "electronics_only",
            },
        )
        assert response.status_code == 422
        assert expected in response.text
    assert service.registry.claim() is None


def test_bridge_rejects_adapter_bytes_outside_pinned_manifest(api, tmp_path, monkeypatch):
    source_spec, _, _ = source(api, tmp_path, monkeypatch)
    _, http = api
    prepare = engine.prepare_source

    def unbound(*args, **kwargs):
        result = prepare(*args, **kwargs)
        result["frozen_files"] = {**result["frozen_files"], "unbound.bin": b"not in source"}
        return result

    monkeypatch.setattr(engine, "prepare_source", unbound)
    response = http.post(lab.ROUTE + "/db-bridges", json={"source": source_spec})
    assert response.status_code == 409, response.text
    assert "source manifest" in response.text
