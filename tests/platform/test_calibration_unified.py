"""Single camera composition, provenance checks and completed-only CAS activation."""

import hashlib
import json
from copy import deepcopy

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import calibration_db as db
from camera_platform import calibration_db_color as color
from camera_platform import calibration_db_core as core
from camera_platform import calibration_lab as lab
from camera_platform import calibration_unified as unified
from camera_platform import calibration_unified_engine as engine
from camera_platform import calibration_unified_report as report
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError, canonical, digest

_REAL_PARENTS = unified.parents


@pytest.fixture
def api(tmp_path, monkeypatch):
    from camera_platform import calibration_db_color_engine as loader

    app = create_app(tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False)
    service = app.state.service
    monkeypatch.setattr(lab, "_source_code", lambda: {"source": "unified-test"})
    service.loaded_environment = lab._source_code()
    context = {
        "run_id": "run-" + "a" * 32,
        "candidate_id": "candidate-03",
        "condition_id": "condition-006",
        "manifest_sha256": digest("manifest"),
    }
    prepared = {
        "frozen_files": {"manifest.json": b"original manifest", "raw.npy": b"original raw"},
        "derived_files": {},
        "manifest_bindings": {"": context["manifest_sha256"]},
    }
    common = {
        "source_context": context,
        "source_kind": "synthetic",
        "physical_validation": "not_established",
    }
    parent_blob = service.registry.store_blob(b"parent", "parent.json", "application/json")
    cm = service.registry.put(
        color.MODELS,
        {
            **common,
            "name": "QE CCM",
            "schema": "camera_db_color_model_v1",
            "artifacts": [parent_blob],
            "source_files": {"parent.json": {"sha256": parent_blob["sha256"]}},
            "calibration_record_snapshot": {"evidence": {"inputs": {"source_notes": {"seed": 17}}}},
        },
        identity="db-color-model-" + "b" * 32,
    )
    km = service.registry.put(
        core.MODELS,
        {**common, "name": "E PSF", "run_id": "run-" + "c" * 32},
        identity="db-core-model-" + "d" * 32,
    )
    result = {"comparisons": [{"series": {"training": {"seeds": [117, 118]}}}]}
    monkeypatch.setattr(
        color, "_prepare", lambda *a: ({"id": context["run_id"]}, context, prepared)
    )
    monkeypatch.setattr(db, "source_selection", lambda *a: ({"id": context["run_id"]}, context))
    monkeypatch.setattr(unified, "parents", lambda *a: (cm, km, result, {}))
    profile = {
        "schema": "camera_unified_calibration_profile_v1",
        "camera_id": "synthetic-db-unified",
        "fields": {"center": {"test": "candidate"}},
        "electronics": {"dn_per_e": 0.46},
    }
    profile["profile_sha256"] = digest(profile)
    monkeypatch.setattr(
        engine, "compose", lambda *a: {k: deepcopy(profile) for k in ("before", "after", "truth")}
    )
    monkeypatch.setattr(loader, "prepare", lambda *a, **k: prepared)

    def compute(prepared, profiles, out, seed, excluded, progress):
        assert 117 in excluded and 118 in excluded
        progress("one camera RAW")
        p = profiles["after"]
        (out / "camera-profile.json").write_text(canonical(p))
        return {
            "camera_profile": p,
            "profile_sha256": p["profile_sha256"],
            "candidate_profiles": p["fields"],
            "validation": {"numerical_tolerance_pass": True},
            "protocol": {"roles": ("before", "observation", "after")},
        }

    monkeypatch.setattr(engine, "execute", compute)

    def publish(out, s):
        (out / "report.html").write_text("<html>" + s["model_id"] + "</html>")
        (out / "report-controls.js").write_text("window.print();")

    monkeypatch.setattr(report, "publish", publish)
    payload = {
        "source": {k: context[k] for k in ("run_id", "candidate_id", "condition_id")},
        "color_model_id": cm["id"],
        "core_model_id": km["id"],
        "seed": 51391,
    }
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()["token"]
        yield service, client, payload


def queue(api, **patch):
    _, client, payload = api
    response = client.post(unified.ROOT, json={**payload, **patch})
    assert response.status_code == 202, response.text
    return response.json()


def finish(service):
    job = service.registry.claim()
    result = service.execute_run(job)
    service.registry.finish(job["id"], result=result)
    return result["summary"]


def test_one_profile_completed_publication_and_typed_files(api):
    service, client, _ = api
    r = queue(api)
    job = service.registry.claim()
    result = service.execute_run(job)
    assert unified.active(service) == {}
    assert len(unified.active(service, include_pending=True)) == 1
    service.registry.finish(job["id"], result=result)
    s = result["summary"]
    assert s["protocol"]["roles"] == ["before", "observation", "after"]
    model = unified.get_model(service, s["model_id"])
    assert model["camera_profile"]["electronics"]["dn_per_e"] == 0.46
    assert len(model["parent_pins"]) == 2
    assert (
        client.get("/api/v1/db-design/active-unified-models").json()["items"][0]["id"]
        == model["id"]
    )
    for name, sha in s["files"].items():
        response = client.get(unified.ROOT + "/" + r["id"] + "/files/" + name)
        assert response.status_code == 200, response.text
        assert hashlib.sha256(response.content).hexdigest() == sha
    assert (
        client.get(unified.ROOT + "/" + r["id"] + "/files/source/raw.npy").content
        == b"original raw"
    )
    assert (
        client.get(unified.ROOT + "/" + r["id"] + "/files/not-in-manifest.npy").status_code == 404
    )
    assert (
        client.get(unified.ROOT).json()["items"][0]["parents"]["core_model_id"]
        == api[2]["core_model_id"]
    )


def test_parallel_same_prior_only_one_active_profile(api):
    service, _, _ = api
    queue(api)
    queue(api)
    a, b = finish(service), finish(service)
    assert a["activation"]["status"] == "applied"
    assert b["activation"]["status"] == "blocked"
    assert next(iter(unified.active(service).values()))["model_id"] == a["model_id"]
    assert unified.file(service, b["run_id"], "report.html").exists()


def test_failed_publication_never_changes_visible_active(api, monkeypatch):
    service, _, _ = api
    queue(api)
    s = finish(service)
    previous = deepcopy(unified.active(service))
    queue(api)
    job = service.registry.claim()
    monkeypatch.setattr(
        report, "publish", lambda *a: (_ for _ in ()).throw(ValueError("report failed"))
    )
    with pytest.raises(ValueError, match="report failed"):
        service.execute_run(job)
    service.registry.finish(job["id"], error="report failed")
    assert unified.active(service) == previous
    assert unified.active(service, include_pending=True) == previous
    assert unified.get_model(service, s["model_id"])["id"] == s["model_id"]


def test_frozen_combined_profile_tampering_fails_before_capture(api):
    service, _, _ = api
    queue(api)
    job = service.registry.claim()
    path = service.registry.artifact_binding(
        job["frozen_profiles_sha256"], owner_kind="runs", owner_id=job["id"]
    )["path"]
    path.write_bytes(b"changed")
    with pytest.raises(IntegrityError):
        service.execute_run(job)


@pytest.mark.parametrize("apply", [True, False])
def test_numerical_failure_or_inspection_keeps_report_without_activation(api, monkeypatch, apply):
    service, _, _ = api
    calculate = engine.execute

    def reject(*args):
        s = calculate(*args)
        s["validation"]["numerical_tolerance_pass"] = False
        return s

    monkeypatch.setattr(engine, "execute", reject)
    queue(api, apply=apply)
    s = finish(service)
    assert s["activation"]["status"] == ("blocked" if apply else "not_requested")
    assert unified.active(service) == {}
    assert unified.file(service, s["run_id"], "report.html").exists()


def test_parent_must_match_full_db_condition_before_any_composition(api, monkeypatch):
    service, _, payload = api
    # Use the real compatibility gate with controlled immutable parent lookups.
    cm = service.registry.get(color.MODELS, payload["color_model_id"])
    km = service.registry.get(core.MODELS, payload["core_model_id"])
    monkeypatch.setattr(color, "get_model", lambda *a: cm)
    monkeypatch.setattr(color, "_verify_color", lambda *a: None)
    monkeypatch.setattr(core, "get_model", lambda *a: km)
    context = {**cm["source_context"], "condition_id": "condition-007"}
    with pytest.raises(ValueError, match="exact DB"):
        _REAL_PARENTS(service, payload, context)


def profile():
    p = engine.core.lab.default_profile()
    p["native_shape"] = p["render_shape"] = [128, 128]
    return {
        "fields": {fid: deepcopy(p) for fid in engine.FIELDS},
        "electronics": p["electronics"],
        "spectral_response": {fid: np.full((21, 4), 0.4).tolist() for fid in engine.FIELDS},
        "spectral_dc_gain": {fid: np.ones(21).tolist() for fid in engine.FIELDS},
    }


def test_qe_scan_uses_known_photons_and_bypasses_psf_and_lens():
    p = profile()
    p["spectral_dc_gain"]["center"] = [12345.0] * 21
    task = next(t for t in engine.tasks() if t["chart"] == "qe_scan")
    _, charge, _, _, _ = engine.doses(p, {"spec": {"exposure_ms": 1.0}}, task)
    assert np.allclose(charge[0], 1200 * 0.4)
    p["spectral_response"]["center"] = np.full((21, 4), 0.2).tolist()
    _, changed, _, _, _ = engine.doses(p, {"spec": {"exposure_ms": 1.0}}, task)
    assert np.allclose(changed[0], charge[0] / 2)


def test_dark_ptc_ignore_spectral_optical_and_isp_parameters():
    p = profile()
    prepared = {"spec": {"exposure_ms": 1.0}}
    for task in [t for t in engine.tasks() if t["chart"] in ("dark", "ptc")]:
        expected = engine.doses(p, prepared, task)
        changed = deepcopy(p)
        changed["spectral_response"] = {}
        changed["fields"] = {}
        changed["spectral_dc_gain"] = {}
        actual = engine.doses(changed, prepared, task)
        assert all(np.array_equal(a, b) for a, b in zip(expected[1], actual[1], strict=True))


def test_unified_stream_plan_is_disjoint_and_collision_rejected_before_acquisition(tmp_path):
    values = []
    for r in engine.ROLES:
        for t in engine.tasks():
            levels = 21 if t["chart"] == "qe_scan" else 7 if t["chart"] in ("dark", "ptc") else 1
            repeats = 32 if t["chart"] in ("dark", "ptc") else 16
            values.extend(
                engine.seed_for(51391, r, t, level, n)
                for level in range(levels)
                for n in range(repeats)
            )
    assert len(values) == len(set(values)) == 3520
    with pytest.raises(ValueError, match="overlap"):
        engine.execute({}, {"after": {}}, tmp_path, 51391, [values[0]])


def test_unified_report_cannot_overwrite_preserved_evidence(tmp_path):
    (tmp_path / "evidence.zip").write_bytes(b"preserved")
    with pytest.raises(ValueError, match="immutable"):
        report.publish(tmp_path, {})


@pytest.mark.parametrize(
    "patch", [{"seed": True}, {"seed": 2**31}, {"apply": "true"}, {"core_model_id": "wrong"}]
)
def test_invalid_unified_requests_refused_before_source_load(api, patch):
    _, client, payload = api
    assert client.post(unified.ROOT, json={**payload, **patch}).status_code == 422


def test_source_drift_after_calculation_stops_before_model_report_or_activation(api, monkeypatch):
    service, _, _ = api
    queue(api)
    job = service.registry.claim()
    calculate = engine.execute

    def drift(*args):
        result = calculate(*args)
        monkeypatch.setattr(lab, "_source_code", lambda: {"source": "new-other-feature"})
        return result

    monkeypatch.setattr(engine, "execute", drift)
    monkeypatch.setattr(
        report, "publish", lambda *a: pytest.fail("Stale source must never publish a report")
    )
    with pytest.raises(IntegrityError, match="before publication"):
        service.execute_run(job)
    output = unified.directory(service, job["id"])
    evidence = json.loads((output / "source-invalidated-numerical-summary.json").read_text())
    assert evidence["status"] == "not_published"
    assert not (output / "report.html").exists()
    assert unified.active(service, include_pending=True) == {}
    assert service.registry.list(unified.MODELS) == []
