"""Scoped core workflow: immutable sources, completed activation and RAW observation bounds."""

import hashlib
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from camera_platform import calibration_db as db
from camera_platform import calibration_db_color as source
from camera_platform import calibration_db_core as core
from camera_platform import calibration_db_core_engine as engine
from camera_platform import calibration_db_core_report as report
from camera_platform import calibration_lab as lab
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError, canonical, digest


@pytest.fixture
def api(tmp_path, monkeypatch):
    from camera_platform import calibration_db_color_engine as loader

    app = create_app(tmp_path / "state", workspace=tmp_path, start_worker=False, seed_demo=False)
    service = app.state.service
    monkeypatch.setattr(lab, "_source_code", lambda: {"source": "frozen-core-test"})
    service.loaded_environment = lab._source_code()
    context = dict(
        run_id="run-" + "b" * 32,
        candidate_id="candidate-03",
        condition_id="condition-006",
        manifest_sha256=digest("manifest"),
    )
    prepared = {
        "frozen_files": {"manifest.json": b"original manifest", "raw/center.npy": b"original raw"},
        "derived_files": {},
        "manifest_bindings": {"": context["manifest_sha256"]},
    }
    monkeypatch.setattr(
        source, "_prepare", lambda *a: ({"id": context["run_id"]}, context, prepared)
    )
    monkeypatch.setattr(db, "source_selection", lambda *a: ({"id": context["run_id"]}, context))
    monkeypatch.setattr(engine, "profiles", lambda p: ({}, {}))
    monkeypatch.setattr(loader, "prepare", lambda *a, **k: prepared)

    def compute(p, out, seed, modules, progress):
        progress("RAW-observed fit")
        (out / "candidate-profiles.json").write_text(
            canonical({"center": {"test": "fitted"}, "outer-ne": {"test": "fitted"}})
        )
        profiles = json.loads((out / "candidate-profiles.json").read_text())
        return {
            "protocol": {"roles": ("training", "observation", "after")},
            "candidate_profiles": profiles,
            "candidate_sha256": digest(profiles),
            "validation": {"numerical_tolerance_pass": True},
            "modules": modules,
        }

    monkeypatch.setattr(engine, "execute", compute)

    def publish(out, summary):
        (out / "report.html").write_text("<html>" + summary["model_id"] + "</html>")
        (out / "report-controls.js").write_text("window.print();")
        (out / "report.json").write_text(canonical(summary))

    monkeypatch.setattr(report, "publish", publish)
    with TestClient(app) as client:
        client.headers["X-Platform-Token"] = client.get("/api/v1/session").json()["token"]
        yield (
            service,
            client,
            dict(source=context | {}, modules=["electronics", "optics"], seed=71391),
        )


def queue(api, **patch):
    service, client, payload = api
    response = client.post(core.ROOT, json={**payload, **patch})
    assert response.status_code == 202, response.text
    return response.json()


def test_completed_owner_and_active_model_binding(api):
    service, client, _ = api
    run = queue(api)
    assert client.get("/api/v1/db-design/active-core-models").json() == {"items": []}
    job = service.registry.claim()
    result = service.execute_run(job)
    # Pending activation stays invisible while artifacts have not finished publishing.
    assert core.active(service) == {}
    assert len(core.active(service, include_pending=True)) == 1
    service.registry.finish(run["id"], result=result)
    row = client.get(core.ROOT + "/" + run["id"]).json()
    assert row["status"] == "succeeded"
    summary = row["result"]["summary"]
    assert summary["protocol"]["roles"] == ["training", "observation", "after"]
    active = client.get("/api/v1/db-design/active-core-models").json()["items"]
    assert active[0]["id"] == summary["model_id"]
    assert active[0]["profile_sha256"] == digest(summary["candidate_profiles"])
    for name, h in summary["files"].items():
        response = client.get(core.ROOT + "/" + run["id"] + "/files/" + name)
        assert response.status_code == 200, response.text
        assert hashlib.sha256(response.content).hexdigest() == h
    assert client.get(core.ROOT + "/" + run["id"] + "/files/missing.npy").status_code == 404
    assert (
        client.get(core.ROOT + "/" + run["id"] + "/files/report-controls.js")
        .headers["content-type"]
        .startswith(("text/javascript", "application/javascript"))
    )
    assert (
        client.get(core.ROOT + "/" + run["id"] + "/files/source/raw/center.npy").content
        == b"original raw"
    )


def test_same_prior_cas_preserves_first_model_and_second_report(api):
    service, _, _ = api
    first, second = queue(api), queue(api)
    for r in (first, second):
        job = service.registry.claim()
        assert job["id"] == r["id"]
        result = service.execute_run(job)
        service.registry.finish(job["id"], result=result)
    a, b = [core.get(service, r["id"])["result"]["summary"] for r in (first, second)]
    assert a["activation"]["status"] == "applied"
    assert b["activation"]["status"] == "blocked"
    assert list(core.active(service).values())[0]["model_id"] == a["model_id"]
    assert core.file(service, second["id"], "report.html").is_file()


def test_failed_publication_does_not_replace_previous_active(api, monkeypatch):
    service, _, _ = api
    r = queue(api)
    job = service.registry.claim()
    result = service.execute_run(job)
    service.registry.finish(r["id"], result=result)
    previous = deepcopy(core.active(service))
    queue(api)
    job = service.registry.claim()
    monkeypatch.setattr(
        report, "publish", lambda *a: (_ for _ in ()).throw(ValueError("report write failed"))
    )
    with pytest.raises(ValueError, match="report write failed"):
        service.execute_run(job)
    service.registry.finish(job["id"], error="report write failed")
    assert core.active(service) == previous
    assert core.active(service, include_pending=True) == previous


def test_queued_source_tampering_fails_before_fit(api):
    service, _, _ = api
    queue(api)
    job = service.registry.claim()
    sha = job["source_files"]["raw/center.npy"]["sha256"]
    path = service.registry.artifact_binding(sha, owner_kind="runs", owner_id=job["id"])["path"]
    path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        service.execute_run(job)


def test_inspect_only_never_activates(api):
    service, _, _ = api
    queue(api, apply=False)
    job = service.registry.claim()
    result = service.execute_run(job)
    service.registry.finish(job["id"], result=result)
    assert result["summary"]["activation"]["status"] == "not_requested"
    assert core.active(service) == {}


def test_all_acquisition_streams_disjoint_and_groups_explicit():
    streams = []
    for s in engine.specs(["electronics", "optics"]):
        levels, repeats = (1, 16) if s["chart"] == "mtf" else (7, 32)
        streams += [
            engine.acquisition_seed(71391, r, s, level, n)
            for r in engine.ROLES
            for level in range(levels)
            for n in range(repeats)
        ]
    assert len(streams) == len(set(streams)) == 2560
    assert {s["chart"] for s in engine.specs(["electronics"])} == {"dark", "ptc"}
    assert {s["chart"] for s in engine.specs(["optics"])} == {"mtf"}


def test_sfr_only_uses_common_observed_band():
    curves = {
        r: {"metrics": {"raw_sfr": {"x": [0, 0.1, 0.3], "y": [1, 0.8, 0.2]}}}
        for r in ("before", "after", "observation", "replicate")
    }
    v = engine.sfr_comparison(curves, "raw_sfr")
    assert v["x"][0] == 0 and v["x"][-1] == 0.25
    assert v["after"]["rmse"] == 0
    curves["before"]["metrics"]["raw_sfr"]["x"] = [0.3, 0.4, 0.5]
    with pytest.raises(ValueError, match="shared"):
        engine.sfr_comparison(curves, "raw_sfr")


@pytest.mark.parametrize(
    "patch",
    [
        {"modules": []},
        {"modules": [{}]},
        {"modules": ["optics", "optics"]},
        {"seed": True},
        {"seed": 2**31},
        {"apply": "true"},
    ],
)
def test_config_rejects_ambiguous_or_invalid_values(patch):
    with pytest.raises(ValueError):
        core.spec(patch)


def test_report_cannot_republish_into_existing_evidence(tmp_path):
    (tmp_path / "report.html").write_bytes(b"preserved report")
    with pytest.raises(ValueError, match="immutable"):
        report.publish(tmp_path, {})
    assert (tmp_path / "report.html").read_bytes() == b"preserved report"


def test_numerical_failure_still_reports_but_never_activates(api, monkeypatch):
    service, _, _ = api
    calculate = engine.execute

    def failing_quality(*args):
        summary = calculate(*args)
        summary["validation"]["numerical_tolerance_pass"] = False
        return summary

    monkeypatch.setattr(engine, "execute", failing_quality)
    queue(api)
    job = service.registry.claim()
    result = service.execute_run(job)
    service.registry.finish(job["id"], result=result)
    assert result["summary"]["activation"]["status"] == "blocked"
    assert core.active(service) == {}
    assert core.file(service, job["id"], "report.html").is_file()
