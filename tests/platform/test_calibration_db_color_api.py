"""Activation CAS, frozen queued revisions, original bytes and artifact readback."""

import io
import json
import zipfile
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from camera_platform import calibration_color
from camera_platform import calibration_db_color as bridge
from camera_platform import calibration_lab as lab
from camera_platform.api import create_app
from camera_platform.registry import ConflictError, IntegrityError, canonical, digest


@pytest.fixture
def api(tmp_path, monkeypatch):
    from camera_platform import calibration_db_color_contracts as contracts
    from camera_platform import calibration_db_color_engine as engine
    from camera_platform import calibration_db_color_report as report

    app = create_app(tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False)
    service = app.state.service
    monkeypatch.setattr(lab, "_source_code", lambda: {"source": "test-frozen"})
    service.loaded_environment = lab._source_code()
    names = ("report.html", "report.json", "qe-profile.json", "ccm-profile.json", "inputs.json")
    artifacts = [
        service.registry.store_blob(
            canonical({"test": "qe" if name.startswith("qe-") else "ccm"}).encode()
            if name in {"qe-profile.json", "ccm-profile.json"}
            else canonical({"test": "task"}).encode()
            if name == "inputs.json"
            else (name + " original").encode(),
            name,
            "application/json",
        )
        for name in names
    ]
    color = service.registry.put(
        calibration_color.COLLECTION,
        {
            "schema": calibration_color.SCHEMA,
            "name": "synthetic color fixture",
            "source_kind": "synthetic",
            "status": "pass",
            "input_sha256": digest({"test": "task"}),
            "files": {a["name"]: a["sha256"] for a in artifacts},
            "artifacts": artifacts,
            "qe": {"profile": {"test": "qe"}},
            "ccm": {"profile": {"test": "ccm"}},
        },
        identity="color-calibration-" + "a" * 32,
    )
    source = {
        "run_id": "run-" + "b" * 32,
        "candidate_id": "candidate-03",
        "condition_id": "condition-006",
    }
    context = {
        **source,
        "manifest_sha256": digest("manifest"),
        "candidate_sha256": digest("candidate"),
    }
    prepared = {
        "frozen_files": {
            "manifest.json": b"original manifest",
            "optics/transport.npz": b"original optical transport",
        },
        "derived_files": {"derived/spectra.json": b"pinned spectra"},
        "manifest_bindings": {"": context["manifest_sha256"]},
    }
    monkeypatch.setattr(
        bridge, "_prepare", lambda *_a, **_k: ({"id": source["run_id"]}, context, prepared)
    )
    monkeypatch.setattr(
        bridge.calibration_db, "source_selection", lambda *_a: ({"id": source["run_id"]}, context)
    )
    monkeypatch.setattr(contracts, "model_spec", deepcopy)
    resolved = {"qe": "replaced", "ccm": "imported"}
    resolved["profile_sha256"] = digest(resolved)
    monkeypatch.setattr(contracts, "freeze_model", lambda *a: deepcopy(resolved))
    monkeypatch.setattr(bridge, "_same_application", lambda *_a: None)
    monkeypatch.setattr(
        bridge,
        "_holdout_plan",
        lambda *_a: {"status": "not_evaluated", "reason": "Fixture without reference"},
    )
    monkeypatch.setattr(engine, "prepare", lambda *_a, **_k: prepared)
    monkeypatch.setattr(engine, "projection", lambda *_a: {"cfa": "RGGB", "gain": 1})
    monkeypatch.setattr(engine, "binding", lambda *_a: {"mode": "replace_qe_and_ccm"})

    def execute(prepared, color, directory, progress=None):
        progress("fresh photon transport")
        target = directory / "fields/center/raw.npy"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"fresh RAW bytes")
        return {
            "schema": "camera_db_color_replay_result_v1",
            "raw_unchanged_by_ccm": True,
            "independent_db_validation": "not_established",
            "fields": [],
        }

    monkeypatch.setattr(engine, "execute", execute)
    monkeypatch.setattr(
        report,
        "render_db_color_report",
        lambda summary, **_k: ("<html>" + summary["model_id"] + "</html>").encode(),
    )
    payload = {
        "name": "derived DB",
        "source": source,
        "color_calibration_id": color["id"],
        "binding": {
            "mode": "synthetic_model_transfer",
            "angular_assumption": "angle_independent",
            "green_sites": "shared_green",
            "target_camera_id": "synthetic-db-fixture",
        },
    }
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()["token"]
        yield service, client, payload


def create(api, **changes):
    _, client, payload = api
    response = client.post(bridge.ROUTE + "/color-models", json={**payload, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def apply(api, model, expected=None):
    _, client, _ = api
    return client.post(
        bridge.ROUTE + f"/color-models/{model['id']}/activate",
        json={"expected_active_model_id": expected},
    )


def complete(service, identity):
    job = service.registry.claim()
    assert job["id"] == identity
    result = service.execute_run(job)
    service.registry.finish(identity, result=result)


def test_one_action_saved_calibration_applies_and_submits(api):
    service, client, payload = api
    config = {k: payload[k] for k in ("name", "source", "binding")}
    response = client.post(
        "/api/v1/calibration-lab/color-db-workflows",
        json={"model": config, "color_calibration_id": payload["color_calibration_id"]},
    )
    assert response.status_code == 202, response.text
    flow = response.json()
    assert flow["status"] == "submitted"
    assert flow["activation"]["model_id"] == flow["model"]["id"]
    assert flow["run"]["model_snapshot"]["id"] == flow["model"]["id"]
    assert len(service.registry.list(bridge.ACTIVATIONS)) == 1


def test_workflow_accepts_lossless_editor_json(api, monkeypatch):
    service, client, payload = api
    config = {k: payload[k] for k in ("name", "source", "binding")}
    saved = calibration_color.get(service, payload["color_calibration_id"])
    captured = []

    def create(_service, task):
        captured.append(task)
        return saved

    monkeypatch.setattr(calibration_color, "create", create)
    flow = client.post(
        bridge.ROUTE + "/color-workflows",
        json={"model": config, "calibration_json": '{"wavelength":380.0}'},
    ).json()
    assert flow["status"] == "submitted"
    assert type(captured[0]["wavelength"]) is float


def test_workflow_does_not_overwrite_concurrent_activation(api, monkeypatch):
    service, client, payload = api
    config = {k: payload[k] for k in ("name", "source", "binding")}
    original = bridge.create_model

    def concurrent(service, spec):
        model = original(service, spec)
        bridge.activate(service, model["id"], {"expected_active_model_id": None})
        return model

    monkeypatch.setattr(bridge, "create_model", concurrent)
    flow = client.post(
        bridge.ROUTE + "/color-workflows",
        json={"model": config, "color_calibration_id": payload["color_calibration_id"]},
    ).json()
    assert flow["status"] == "blocked"
    assert flow["run"] is None
    assert flow["model"] is not None
    assert len(service.registry.list(bridge.ACTIVATIONS)) == 1


def test_automatic_holdout_is_owned_and_downloadable(api, monkeypatch):
    from camera_platform import calibration_db_holdout

    service, client, _ = api
    monkeypatch.setattr(
        bridge,
        "_holdout_plan",
        lambda *_a: {"status": "planned", "seed": 147, "task_sha256": digest({"test": "task"})},
    )

    def execute(prepared, model, task, output, seed, run_id, progress):
        output.mkdir()
        (output / "report.html").write_bytes(b"automatic independent report")
        (output / "report.json").write_bytes(b"{}")
        (output / "report-controls.js").write_bytes(b"'use strict';")
        (output / "evidence.zip").write_bytes(b"owned child ZIP")
        return {
            "status": "synthetic_holdout_completed",
            "source": {"replay_id": run_id, "model_id": model["id"]},
            "aggregate": {"raw": {"before": {"rmse": 10}, "after": {"rmse": 0.2}}},
        }

    monkeypatch.setattr(calibration_db_holdout, "execute", execute)
    model = create(api)
    apply(api, model)
    run = client.post(bridge.ROUTE + "/color-replays", json={"model_id": model["id"]}).json()
    complete(service, run["id"])
    path = bridge.ROUTE + "/color-replays/" + run["id"]
    row = client.get(path).json()
    assert row["result"]["summary"]["holdout"]["source"]["replay_id"] == run["id"]
    assert (
        client.get(path + "/files/holdout/report.html").content == b"automatic independent report"
    )
    assert client.get(path + "/files/holdout/evidence.zip").content == b"owned child ZIP"
    controls = client.get(path + "/files/holdout/report-controls.js")
    assert controls.status_code == 200
    assert "javascript" in controls.headers["content-type"]
    assert "script-src 'self'" in controls.headers["content-security-policy"]
    rows = client.get(
        bridge.ROUTE + "/color-replays",
        params={"color_calibration_id": model["color_calibration_id"]},
    ).json()["items"]
    assert rows[0]["validation"]["report_url"] == path + "/files/holdout/report.html"
    assert (
        client.get(
            bridge.ROUTE + "/color-replays",
            params={"color_calibration_id": "color-calibration-" + "f" * 32},
        ).json()["items"]
        == []
    )


def test_create_idempotent_does_not_activate_and_activation_is_explicit(api):
    service, client, _ = api
    model = create(api)
    assert create(api) == model
    assert client.get(bridge.ROUTE + "/active-color-models").json() == {"items": []}
    response = client.post(bridge.ROUTE + "/color-replays", json={"model_id": model["id"]})
    assert response.status_code == 409
    assert apply(api, model).status_code == 201
    assert client.get(bridge.ROUTE + "/color-models").json()["items"][0]["active"] is True
    assert apply(api, model).status_code == 409
    assert apply(api, model, model["id"]).status_code == 201
    assert len(service.registry.list(bridge.ACTIVATIONS)) == 1


def test_activation_cas_and_queued_revision_stays_frozen(api):
    service, client, _ = api
    first = create(api)
    apply(api, first)
    run = client.post(bridge.ROUTE + "/color-replays", json={"model_id": first["id"]}).json()
    second = create(api, name="new model revision")
    assert first["target_key"] == second["target_key"]
    assert apply(api, second).status_code == 409
    assert apply(api, second, first["id"]).status_code == 201
    assert (
        client.post(bridge.ROUTE + "/color-replays", json={"model_id": first["id"]}).status_code
        == 409
    )
    complete(service, run["id"])
    saved = client.get(bridge.ROUTE + "/color-replays/" + run["id"]).json()
    assert saved["result"]["summary"]["model_id"] == first["id"]
    assert bridge.active_models(service)[first["target_key"]]["model_id"] == second["id"]


def test_replay_files_bundle_and_source_preservation(api, monkeypatch):
    service, client, _ = api
    model = create(api)
    apply(api, model)
    response = client.post(bridge.ROUTE + "/color-replays", json={"model_id": model["id"]})
    assert response.status_code == 202
    run = response.json()
    complete(service, run["id"])
    path = bridge.ROUTE + "/color-replays/" + run["id"]
    saved = client.get(path).json()
    assert saved["status"] == "succeeded"
    summary = saved["result"]["summary"]
    assert summary["fresh_simulation"] is True
    assert summary["independent_db_validation"] == "not_established"
    assert client.get(path + "/files/fields/center/raw.npy").content == b"fresh RAW bytes"
    assert client.get(path + "/files/unregistered.npy").status_code == 404
    original_get_run = bridge.get_run
    summary_reads = []

    def counted_get_run(service, identity):
        summary_reads.append(identity)
        return original_get_run(service, identity)

    monkeypatch.setattr(bridge, "get_run", counted_get_run)
    package = client.get(path + "/bundle")
    assert summary_reads == [run["id"]]
    assert package.status_code == 200, package.text
    with zipfile.ZipFile(io.BytesIO(package.content)) as z:
        assert z.testzip() is None
        assert json.loads(z.read("record.json")) == saved
        assert z.read("fields/center/raw.npy") == b"fresh RAW bytes"
        assert json.loads(z.read("model.json")) == model
    assert service.registry.get(bridge.MODELS, model["id"]) == model
    raw = service.registry.artifact_binding(
        summary["files"]["fields/center/raw.npy"], owner_kind="runs", owner_id=run["id"]
    )["path"]
    raw.write_bytes(b"tampered RAW bytes")
    assert client.get(path + "/bundle").status_code == 409


def test_modified_frozen_optical_bytes_are_rejected(api):
    service, client, _ = api
    model = create(api)
    apply(api, model)
    run = client.post(bridge.ROUTE + "/color-replays", json={"model_id": model["id"]}).json()
    row = model["source_files"]["optics/transport.npz"]
    path = service.registry.artifact_binding(
        row["sha256"], owner_kind=bridge.MODELS, owner_id=model["id"]
    )["path"]
    path.write_bytes(b"modified optical input")
    job = service.registry.claim()
    assert job["id"] == run["id"]
    with pytest.raises(IntegrityError):
        service.execute_run(job)


def test_source_revision_change_blocks_submission(api, monkeypatch):
    service, _, _ = api
    model = create(api)
    apply(api, model)
    monkeypatch.setattr(lab, "_source_code", lambda: {"source": "changed"})
    with pytest.raises(ConflictError):
        bridge.submit(service, {"model_id": model["id"]})


def test_unrelated_run_cannot_be_read_as_color_replay(api):
    service, client, _ = api
    run = service.registry.submit(
        {"job_kind": "unrelated", "name": "unrelated"}, digest("unrelated")
    )
    assert client.get(bridge.ROUTE + "/color-replays/" + run["id"]).status_code == 422


def test_corrupted_calibration_profile_artifact_prevents_new_model(api):
    service, _, payload = api
    color = calibration_color.get(service, payload["color_calibration_id"])
    path = calibration_color.file(service, color["id"], "qe-profile.json")
    path.write_text('{"test":"changed"}')
    with pytest.raises(IntegrityError):
        bridge.create_model(service, payload)


def test_execution_operator_must_equal_the_resolved_model():
    resolved = {
        "applied_response": {
            k: [1]
            for k in (
                "wavelength_nm",
                "qe_rgb",
                "qe_rggb",
                "angular_policy",
                "green_policy",
                "application",
            )
        },
        "isp_adapter": {
            k: [1]
            for k in (
                "reference_white_e",
                "white_balance",
                "exposure_scale",
                "baseline_matrix",
                "matrix",
                "electron_to_output_matrix",
                "baseline_electron_to_output_matrix",
                "matrix_convention",
                "target_space",
            )
        },
    }
    bridge._same_application(resolved, deepcopy(resolved))
    incorrect = deepcopy(resolved)
    incorrect["isp_adapter"]["electron_to_output_matrix"] = [2]
    with pytest.raises(IntegrityError, match="electron_to_output_matrix"):
        bridge._same_application(resolved, incorrect)
