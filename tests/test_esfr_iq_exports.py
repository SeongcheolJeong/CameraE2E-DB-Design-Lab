"""Presentation-only revisions over real synthetic IQ arithmetic and artifacts.

The MTF job/contract are explicit test doubles; no physical validation is claimed.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from camera_platform import esfr_iq_exports as exports, esfr_iq_service as iq
from camera_platform import esfr_iq_report as renderer, esfr_iq_engine as engine
from camera_platform import esfr_iq_replay as replay, esfr_measured_workflow as flow
from camera_platform.registry import IntegrityError, ConflictError, digest
from test_esfr_iq_service import context


@pytest.fixture
def completed(tmp_path, monkeypatch):
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch)
    monkeypatch.setattr(replay, "execute", lambda *args: {"status": "explicit_test_double", "numerical_MTF_verified": False})
    original = iq.validate(service, record, state)
    state.update(status="completed", iq_result=original)
    monkeypatch.setattr(flow, "_read", lambda s, identity: (deepcopy(record), deepcopy(state)))
    def no_numerical_work(*args, **kwargs):
        pytest.fail("Presentation export must not rerun numerical/image work")
    monkeypatch.setattr(engine, "run", no_numerical_work)
    monkeypatch.setattr(engine, "fit_profile", no_numerical_work)
    monkeypatch.setattr(replay, "execute", no_numerical_work)
    monkeypatch.setattr(renderer, "render_chart_previews", no_numerical_work)
    monkeypatch.setattr(flow, "_write", no_numerical_work)
    # A new renderer release may export old evidence without changing its source.
    service.loaded_environment = {"test": "new-report-export-release"}
    return service, record, state, original


def test_export_revision_idempotent_preserves_original_and_owns_every_zip_byte(completed):
    service, record, state, original = completed
    old_state, old_record, old_report = deepcopy(state), deepcopy(record), deepcopy(original)
    result = exports.create(service, record["id"], {})
    assert exports.create(service, record["id"], {}) == result
    assert len(service.registry.list(exports.KIND)) == 1
    assert service.registry.get(iq.REPORTS, original["id"]) == old_report
    assert service.registry.get(iq.WORKFLOWS, record["id"]) == old_record
    assert state == old_state
    assert result["numerical_source_code"] == record["source_code"] != result["render_source_code"]
    assert result["numerical_rerun"] is result["refit_performed"] is result["db_active_model_updated"] is False
    assert result["source_kind"] == "synthetic" and result["physical_validation"] == "not_established"
    assert result["source_report_sha256"] == original["content_hash"]
    response = exports.file_response(service, record["id"], result["id"], "bundle.zip")
    with zipfile.ZipFile(response.path) as archive:
        names = set(archive.namelist())
        assert set(renderer.CSV_FILES) <= names
        manifest = json.loads(archive.read("bundle-manifest.json"))
        assert {r["path"] for r in manifest["files"]} == names - {"bundle-manifest.json"}
        for name in names:
            checksum = hashlib.sha256(archive.read(name)).hexdigest()
            assert checksum == result["files"][name]
            service.registry.artifact_binding(checksum, owner_kind=exports.KIND, owner_id=result["id"])
        current = json.loads(archive.read("report.json"))
        assert current.pop("export_provenance") == result["export_provenance"]
        assert current == json.loads(archive.read("source-report.json"))
        assert current["profile"] == state["iq_frozen"]["profile"]
        assert current["provenance"]["source_code"] == record["source_code"]
        for name, checksum in result["export_provenance"]["preserved_files"].items():
            assert result["files"][name] == original["files"][name] == checksum
    assert iq.file_response(service, record["id"], "report.html").path != exports.file_response(service, record["id"], result["id"], "report.html").path
    latest = iq.latest(service, record["source"])
    assert latest["id"] == original["id"] and latest["iq_export"] == result
    assert latest["report_url"] == result["report_url"]
    public = {"status": "completed", "iq_result": original, "links": {"iq_report": original["report_url"], "other": "unchanged"}}
    overlay = exports.overlay_workflow(service, public)
    assert overlay["iq_result"] == original and overlay["links"]["other"] == "unchanged"
    assert overlay["links"]["iq_report"] == result["report_url"]
    assert public["links"]["iq_report"] == original["report_url"]
    with pytest.raises(KeyError):
        exports.file_response(service, "another-workflow", result["id"], "report.html")
    with pytest.raises(KeyError):
        exports.file_response(service, record["id"], result["id"], "../report.json")


@pytest.mark.parametrize("relative", ["report.json", "images/capture-01-raw.png", "bundle.zip", "frozen-iq.json"])
def test_corrupt_original_fails_even_after_successful_export(completed, relative):
    service, record, state, original = completed
    result = exports.create(service, record["id"])
    binding = service.registry.artifact_binding(original["files"][relative], owner_kind=iq.REPORTS, owner_id=original["id"])
    Path(binding["path"]).write_bytes(b"corrupted original")
    with pytest.raises(IntegrityError):
        exports.create(service, record["id"])
    assert len(service.registry.list(exports.KIND)) == 1


def test_failure_does_not_publish_or_mutate_workflow(completed, monkeypatch):
    service, record, state, original = completed
    before = deepcopy(state)
    monkeypatch.setattr(renderer, "publish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("renderer failed")))
    with pytest.raises(RuntimeError, match="renderer failed"):
        exports.create(service, record["id"])
    assert service.registry.list(exports.KIND) == []
    assert state == before
    assert service.registry.get(iq.REPORTS, original["id"]) == original


def test_changed_completed_receipt_and_new_numerical_options_rejected(completed):
    service, record, state, original = completed
    with pytest.raises(ValueError, match="cannot change"):
        exports.create(service, record["id"], {"refit": True})
    state["status"] = "running"
    with pytest.raises(ConflictError, match="completed"):
        exports.create(service, record["id"])
    state["status"] = "completed"
    state["iq_result"] = {**original, "source_kind": "measured"}
    with pytest.raises(IntegrityError, match="differ"):
        exports.create(service, record["id"])


def test_new_exporter_release_creates_new_revision_not_replacement(completed):
    service, record, state, original = completed
    first = exports.create(service, record["id"])
    service.loaded_environment = {"test": "another-report-release"}
    second = exports.create(service, record["id"])
    assert first["id"] != second["id"]
    assert first["source_report_sha256"] == second["source_report_sha256"]
    assert first["numerical_source_code"] == second["numerical_source_code"]
    assert service.registry.get(exports.KIND, first["id"]) == first
    assert iq.latest(service, record["source"])["iq_export"]["id"] == second["id"]
