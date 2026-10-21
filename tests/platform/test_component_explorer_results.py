import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from camera_platform import component_explorer_results as results
from camera_platform.registry import IntegrityError, digest
from camera_platform.virtual_lab_job import sha256

RID = "run-" + "1" * 32
CHILD = "run-" + "2" * 32


@pytest.fixture
def sample(tmp_path, monkeypatch):
    db = tmp_path / "camerae2e_db/catalog.sqlite"
    db.parent.mkdir()
    with sqlite3.connect(db) as c:
        c.executescript("""CREATE TABLE components(component_id TEXT, family TEXT);
        CREATE TABLE sources(source_id TEXT,metadata_json TEXT,sha256 TEXT);
        CREATE TABLE model_revisions(revision_id TEXT, component_id TEXT, model_kind TEXT,
        content_sha256 TEXT,snapshot_json TEXT,source_id TEXT);""")
        snapshot = {"configuration": "intermediate"}
        c.execute("INSERT INTO components VALUES(?,?)", ("lens1", "lens"))
        c.execute(
            "INSERT INTO model_revisions VALUES(?,?,?,?,?,?)",
            (
                "revision1",
                "lens1",
                "patent_prescription",
                digest(snapshot),
                json.dumps(snapshot),
                "source1",
            ),
        )
    item = dict(
        id="lens:revision1",
        family="lens",
        component_id="lens1",
        revision_id="revision1",
        content_sha256=digest(snapshot),
        configuration_id="point-uuid",
        configuration_label="intermediate",
    )
    directory = tmp_path / "reference-workshop" / RID
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text("{}")
    record = dict(
        id=RID,
        status="succeeded",
        job_kind="reference_workshop",
        plugin_id="camera_reference_workshop_v1",
        storage_root=str(tmp_path),
        reference_snapshot={
            "lens": {
                "revision_id": "revision1",
                "revision_content_sha256": digest(snapshot),
                "source_database_sha256": "d" * 64,
                "reviewed_csv_sha256": "c" * 64,
                "reviewed_csv_relative_path": (
                    "reviewed_prescriptions/a/b/configured_surfaces/test.csv"
                ),
            }
        },
        result={"summary": {"manifest_sha256": sha256(directory / "manifest.json")}},
    )
    runs = {RID: record}
    service = SimpleNamespace(
        workspace=tmp_path,
        registry=SimpleNamespace(
            list_runs=lambda: list(runs.values()), get_run=lambda x: deepcopy(runs[x])
        ),
    )
    return service, item, record, runs


def test_only_exact_revision_and_hash_link(sample):
    service, item, _, _ = sample
    assert len(results.bindings(service, [item])[item["id"]]) == 1
    for key, value in [
        ("revision_id", "wrong"),
        ("content_sha256", "0" * 64),
        ("component_id", "wrong"),
        ("configuration_label", "wide"),
    ]:
        wrong = {**item, key: value}
        assert results.bindings(service, [wrong])[item["id"]] == []


def test_no_name_match_or_unpinned_source(sample):
    service, item, record, _ = sample
    record["reference_snapshot"]["lens"] = {"name": "same name"}
    item["name"] = "same name"
    assert results.bindings(service, [item])[item["id"]] == []


def test_source_missing_or_manifest_mismatch_never_substitutes(sample):
    service, item, record, runs = sample
    child = {
        **record,
        "id": CHILD,
        "source": {"run_id": RID, "manifest_sha256": "bad"},
        "reference_snapshot": None,
    }
    runs[CHILD] = child
    assert [r["id"] for r in results.bindings(service, [item])[item["id"]]] == [RID]
    del runs[RID]
    assert results.bindings(service, [item])[item["id"]] == []
    with pytest.raises(KeyError):
        results.validate_result(service, item, CHILD)


def test_missing_saved_storage_visible_unavailable(sample):
    service, item, record, _ = sample
    (Path(record["storage_root"]) / "reference-workshop" / RID / "manifest.json").unlink()
    result = results.bindings(service, [item])[item["id"]][0]
    assert result["status"] == "unavailable" and result["url"] is None


def test_selected_open_verifies_sources_and_every_artifact(sample, monkeypatch):
    service, item, record, _ = sample
    from camera_platform import (
        lens_catalog_bridge,
        lens_db_wavefront_bridge,
        reference_workshop_job,
    )

    calls = []
    monkeypatch.setattr(
        lens_catalog_bridge, "_revision_source", lambda *args: ({}, {}, Path("/db"), "d" * 64)
    )
    monkeypatch.setattr(
        lens_db_wavefront_bridge, "_reviewed_csv", lambda *args: calls.append("source_csv")
    )
    monkeypatch.setattr(reference_workshop_job, "get", lambda *args: record)
    monkeypatch.setattr(
        reference_workshop_job, "verify_files", lambda *args: calls.append("all_files")
    )
    opened = results.validate_result(service, item, RID)
    assert (
        opened["status"] == "verified_files"
        and opened["url"] == f"/reference-workshop.html?run={RID}"
    )
    assert calls == ["source_csv", "all_files"]

    def bad(*args):
        raise IntegrityError("Artifact changed")

    monkeypatch.setattr(reference_workshop_job, "verify_files", bad)
    with pytest.raises(IntegrityError, match="Artifact changed"):
        results.validate_result(service, item, RID)


def sensor_item(service, record):
    from pyisetcam._catalog_sensor import _semantic

    pdf = service.workspace / "licensed/reports/p.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"source report")
    raw = {
        "metadata": {"code": "DEF-1"},
        "derived_specs": {"pixel_pitch_um": 2.9},
        "source_files": {"pdf": [str(pdf)]},
    }
    snap = {
        "derived_specs": raw["derived_specs"],
        "derived_evidence": {},
        "table_evidence": [],
        "metadata": raw["metadata"],
        "origin_kind": "extracted",
    }
    with sqlite3.connect(service.workspace / "camerae2e_db/catalog.sqlite") as c:
        c.execute("INSERT INTO components VALUES(?,?)", ("sensor1", "sensor"))
        c.execute(
            "INSERT INTO sources VALUES(?,?,?)",
            (
                "source-s",
                json.dumps({"source_record": raw, "report_code": "DEF-1"}),
                digest(_semantic(raw)),
            ),
        )
        c.execute(
            "INSERT INTO model_revisions VALUES(?,?,?,?,?,?)",
            (
                "sensor-rev",
                "sensor1",
                "sensor_report_extraction",
                digest(snap),
                json.dumps(snap),
                "source-s",
            ),
        )
    optical = service.workspace / "optical.json"
    optical.write_text("{}")
    stack = service.workspace / "stack.json"
    stack.write_text("{}")
    evidence = {
        "available": True,
        "report_code": "DEF-1",
        "manifest_sha256": "e" * 64,
        "sources": {"DEF-1": {"relative_path": "reports/p.pdf", "sha256": sha256(pdf)}},
    }
    record["reference_snapshot"]["pixel"] = {
        "source_report": "DEF-1",
        "provenance": {
            "structural_evidence": evidence,
            "source_files": {"optical_model": str(optical), "generated_stack": str(stack)},
            "source_optical_model_sha256": sha256(optical),
            "source_generated_stack_sha256": sha256(stack),
        },
    }
    item = {
        "id": "sensor:rev",
        "family": "sensor",
        "report_code": "DEF-1",
        "revision_id": "sensor-rev",
        "component_id": "sensor1",
        "content_sha256": digest(snap),
    }
    return item, pdf


def test_sensor_requires_report_evidence_and_explicit_derivative_label(sample):
    service, _, record, _ = sample
    item, pdf = sensor_item(service, record)
    b = results.bindings(service, [item])[item["id"]][0]
    assert b["binding"]["relation"] == "reviewed_derivative_of_source_report"
    assert b["binding"]["legacy_extraction_executed"] is False
    assert "derivative" in b["label"]
    assert str(service.workspace) not in json.dumps(b)
    record["reference_snapshot"]["pixel"]["provenance"]["structural_evidence"]["sources"][
        "DEF-1"
    ].pop("sha256")
    assert results.bindings(service, [item])[item["id"]] == []


def test_sensor_pdf_and_models_verified_at_open(sample, monkeypatch):
    service, _, record, _ = sample
    item, pdf = sensor_item(service, record)
    from camera_platform import reference_workshop_job
    from pyisetcam import sensor_techinsights

    monkeypatch.setattr(sensor_techinsights, "_manifest", lambda: ({}, "e" * 64))
    monkeypatch.setattr(reference_workshop_job, "get", lambda *args: record)
    monkeypatch.setattr(reference_workshop_job, "verify_files", lambda *args: None)
    assert results.validate_result(service, item, RID)["status"] == "verified_files"
    pdf.write_bytes(b"changed report")
    with pytest.raises(IntegrityError, match="PDF changed"):
        results.validate_result(service, item, RID)
    pdf.unlink()
    assert results.bindings(service, [item])[item["id"]] == []


def test_failed_jobs_never_offer_completed_results(sample):
    service, item, record, _ = sample
    record["status"] = "failed"
    assert results.bindings(service, [item])[item["id"]] == []
    with pytest.raises(IntegrityError, match="not a completed"):
        results.validate_result(service, item, RID)


@pytest.fixture
def db_sample(sample, monkeypatch):
    from camera_platform import db_design_job, reference_workshop_job

    service, item, record, runs = sample
    lens = {
        "key": "reference",
        "revision_id": item["revision_id"],
        "source": {
            "revision_id": item["revision_id"],
            "revision_content_sha256": item["content_sha256"],
        },
    }
    sensor = {
        "key": "imx662",
        "asset_id": "sensor-662",
        "revision_id": "sensor-revision",
        "content_sha256": "s" * 64,
    }
    pinned = {"lenses": {"reference": lens}, "sensors": {"imx662": sensor}}
    record.pop("reference_snapshot")
    record.update(
        plugin_id=db_design_job.SCHEMA,
        specification={"fixture": True},
        db_design_study={"schema": db_design_job.SCHEMA, "snapshot": pinned},
    )
    record["result"]["summary"].update(
        baseline_candidate_id="candidate-00",
        candidates=[
            {
                "id": "candidate-00",
                "status": "succeeded",
                "label": "Pinned pair",
                "lens_key": "reference",
                "sensor_key": "imx662",
                "source_identities": deepcopy({"lens": lens, "sensor": sensor}),
            }
        ],
    )
    monkeypatch.setattr(db_design_job, "snapshot", lambda *args: deepcopy(pinned))
    monkeypatch.setattr(reference_workshop_job, "get", lambda *args: record)
    monkeypatch.setattr(reference_workshop_job, "verify_files", lambda *args: None)
    return service, item, record, runs


def test_db_candidate_links_without_fabricated_reference_snapshot(db_sample):
    service, item, record, _ = db_sample
    assert "reference_snapshot" not in record
    indexed = results.bindings(service, [item])[item["id"]]
    assert len(indexed) == 1
    assert indexed[0]["kind"] == "db_design_study"
    assert indexed[0]["url"] == f"/db-design.html?run={RID}&a=candidate-00&b=candidate-00"
    assert indexed[0]["binding"]["relation"] == "exact_db_design_candidate"
    assert results.validate_result(service, item, RID)["status"] == "verified_files"


@pytest.mark.parametrize(
    "changed", ["result_identity", "catalog_hash", "candidate_status", "cancelled"]
)
def test_db_candidate_binding_is_fail_closed(db_sample, changed):
    service, item, record, _ = db_sample
    candidate = record["result"]["summary"]["candidates"][0]
    if changed == "result_identity":
        candidate["source_identities"]["lens"]["source"]["revision_content_sha256"] = "bad"
    elif changed == "catalog_hash":
        item["content_sha256"] = "bad"
    elif changed == "candidate_status":
        candidate["status"] = "running"
    else:
        record["status"] = "cancelled"
    assert results.bindings(service, [item])[item["id"]] == []
    with pytest.raises(IntegrityError):
        results.validate_result(service, item, RID)


def test_db_current_snapshot_and_artifacts_checked_on_open(db_sample, monkeypatch):
    from camera_platform import db_design_job, reference_workshop_job

    service, item, record, _ = db_sample
    pinned = deepcopy(record["db_design_study"]["snapshot"])
    monkeypatch.setattr(db_design_job, "snapshot", lambda *args: {**pinned, "changed": True})
    with pytest.raises(IntegrityError, match="source snapshot changed"):
        results.validate_result(service, item, RID)
    monkeypatch.setattr(db_design_job, "snapshot", lambda *args: pinned)

    def corrupted(*args):
        raise IntegrityError("Changed native RAW")

    monkeypatch.setattr(reference_workshop_job, "verify_files", corrupted)
    with pytest.raises(IntegrityError, match="native RAW"):
        results.validate_result(service, item, RID)


def test_lineage_cycle_is_refused(sample):
    service, item, record, _ = sample
    record["source"] = {
        "run_id": RID,
        "manifest_sha256": record["result"]["summary"]["manifest_sha256"],
    }
    assert results.bindings(service, [item])[item["id"]] == []
    with pytest.raises(IntegrityError, match="cyclic"):
        results.validate_result(service, item, RID)


def test_packed_requests_and_registry_change_invalidate_cache(tmp_path):
    from camera_platform.evidence_storage import PREFIX
    from camera_platform.registry import SQLiteRegistry

    registry = SQLiteRegistry(tmp_path / "state")
    service = SimpleNamespace(registry=registry)
    document = {
        "job_kind": "reference_workshop",
        "plugin_id": "camera_reference_workshop_v1",
        "padding": "x" * 300000,
    }
    first = registry.submit(document, "first")
    with registry.connection() as conn:
        assert (
            conn.execute("SELECT document FROM jobs WHERE id=?", (first["id"],))
            .fetchone()[0]
            .startswith(PREFIX)
        )
    assert first["id"] in results._runs(service)
    second = registry.submit({**document, "padding": "y" * 300000}, "second")
    assert second["id"] in results._runs(service)
    copy = results._runs(service)
    copy[first["id"]]["status"] = "fake"
    assert results._runs(service)[first["id"]]["status"] == "queued"
