"""Presentation-only immutable exports of completed IQ numerical evidence.

Never rerun a fitter, an observer, a preview renderer or a camera simulation.
The original workflow, report and numerical-source fingerprint remain immutable.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
import zipfile

from . import esfr_iq_service as iq, calibration_lab as lab, esfr_lab, esfr_bridge
from .calibration_completion import _bindings
from .registry import ConflictError, IntegrityError, digest

KIND = "camera_esfr_iq_exports"
SCHEMA = "camera_esfr_iq_export_v1"
FORMAT_VERSION = "iq_csv_tables_v1"
_lock = threading.RLock()
_RECORD_FIELDS = {"id", "created_at", "content_hash", "files", "artifacts",
                  "report_url", "bundle_url", "db_active_model_updated"}
_REGENERATED = {"bundle.zip", "bundle-manifest.json", "report.html", "report.json", "iq-result.json",
                "color-patches.csv", "gray-tone.csv", "noise-patches.csv", "provenance.csv"}


def _hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _record_hash(record):
    if digest({k: v for k, v in record.items() if k != "content_hash"}) != record.get("content_hash"):
        raise IntegrityError("IQ immutable record content hash differs")


def _bundle(record, bindings):
    """Every packaged byte must be the same byte owned by this exact record."""
    try:
        with zipfile.ZipFile(bindings[record["files"]["bundle.zip"]]["path"]) as archive:
            names = archive.namelist()
            expected = set(record["files"]) - {"bundle.zip"}
            if len(names) != len(set(names)) or set(names) != expected:
                raise IntegrityError("IQ ZIP entries differ from the typed artifact owner")
            manifest = json.loads(archive.read("bundle-manifest.json"))
            entries = {entry["path"]: entry for entry in manifest["files"]}
            if len(entries) != len(manifest["files"]) or set(entries) != expected - {"bundle-manifest.json"}:
                raise IntegrityError("IQ ZIP manifest omits or duplicates owned files")
            for name in names:
                with archive.open(name) as stream:
                    checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                if checksum != record["files"][name]:
                    raise IntegrityError("IQ ZIP file differs from its typed artifact owner")
                if name in entries and (entries[name]["sha256"] != checksum or entries[name]["bytes"] != archive.getinfo(name).file_size):
                    raise IntegrityError("IQ ZIP manifest byte binding differs")
    except (KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise IntegrityError("IQ ZIP or manifest is invalid") from exc


def _source(service, workflow_id):
    from . import esfr_measured_workflow as flow
    record, state = flow._read(service, workflow_id)
    _record_hash(record)
    if state.get("status") != "completed" or not state.get("iq_result"):
        raise ConflictError("Only a completed IQ workflow may publish a report export")
    report = iq._report_for_workflow(service, workflow_id)
    _record_hash(report)
    if state["iq_result"] != report or report["source"] != record["source"]:
        raise IntegrityError("Completed workflow and original IQ report differ")
    bindings = _bindings(service, report["files"].values(), iq.REPORTS, report["id"])
    _bundle(report, bindings)
    payload = json.loads(Path(bindings[report["files"]["report.json"]]["path"]).read_text())
    if payload != {k: v for k, v in report.items() if k not in _RECORD_FIELDS}:
        raise IntegrityError("Original IQ report JSON differs from its immutable numerical record")
    frozen, contract = iq._contract(service, record, state)
    for relative, expected in (("frozen-iq.json", frozen), ("acquisition-contract.json", contract),
                               ("composite-candidate.json", payload["composite_candidate"]),
                               ("iq-result.json", payload)):
        if json.loads(Path(bindings[report["files"][relative]]["path"]).read_text()) != expected:
            raise IntegrityError("Original IQ artifact differs from its bound evidence: " + relative)
    provenance = payload["provenance"]
    if (payload["profile"] != frozen["profile"] or payload["reference"] != frozen["reference"]
            or provenance["source_code"] != record["source_code"]
            or provenance["frozen_id"] != frozen["id"] or provenance["frozen_sha256"] != frozen["content_hash"]
            or provenance["contract_id"] != contract["id"] or provenance["contract_sha256"] != contract["content_hash"]
            or provenance["profile_sha256"] != frozen["profile_sha256"]):
        raise IntegrityError("Original numerical source, candidate or acquisition contract differs")
    composite = payload["composite_candidate"]
    if (digest(composite) != payload["composite_candidate_sha256"]
            or composite["reference_isp"]["profile"] != frozen["profile"]
            or composite["reference_isp"]["profile_sha256"] != frozen["profile_sha256"]
            or composite["mtf"]["profile_sha256"] != frozen["mtf_candidate_sha256"]
            or digest(composite["mtf"]["profile"]) != frozen["mtf_candidate_sha256"]):
        raise IntegrityError("Original composite candidate changed")
    captures = []
    for evidence in provenance["development_captures"] + provenance["validation_captures"]:
        capture = esfr_lab.capture(service, evidence["id"])
        if iq._capture_evidence([capture])[0] != evidence:
            raise IntegrityError("Original IQ capture evidence changed")
        captures.append(capture)
    iq._inputs(service, captures)  # verify original RAW/ISP bytes without observing them
    validation = lab.get(service, payload["mtf_evidence"]["validation_id"])
    if (validation["status"] != "succeeded" or validation["parent_study_id"] != frozen["fit_id"]
            or validation["candidate_sha256"] != frozen["mtf_candidate_sha256"]
            or digest(validation["result"]) != payload["mtf_evidence"]["validation_result_sha256"]):
        raise IntegrityError("Original MTF validation result differs")
    combined = payload.get("combined_replay", {})
    native_checksums = []
    for row in combined.get("source_bindings", []):
        files = validation["result"]["summary"]["files"]
        if (row["source_run_id"] != validation["id"]
                or files[row["source_isp_file"]] != row["source_isp_sha256"]
                or files[row["source_raw_file"]] != row["raw_before_ccm_sha256"]
                or row["raw_after_ccm_sha256"] != row["raw_before_ccm_sha256"]
                or row["raw_changed_by_ccm"] is not False
                or row["output_file"] not in report["files"]):
            raise IntegrityError("Original native combined replay binding differs")
        native_checksums.extend((row["source_isp_sha256"], row["raw_before_ccm_sha256"]))
    if native_checksums:
        _bindings(service, native_checksums, "runs", validation["id"])
    refs = [(iq.WORKFLOWS, record), (iq.REPORTS, report), (iq.FROZEN, frozen),
            (esfr_bridge.VALIDATION_CONTRACTS, contract), *[(esfr_lab.CAPTURES, c) for c in captures]]
    return report, payload, bindings, refs


def _verify_export(service, row, report, *, full=False):
    _record_hash(row)
    if (row.get("schema") != SCHEMA or row["source_report_id"] != report["id"]
            or row["source_report_sha256"] != report["content_hash"]
            or row["workflow_id"] != report["workflow_id"] or row["source"] != report["source"]
            or row["composite_candidate_sha256"] != report["composite_candidate_sha256"]
            or row["numerical_source_code"] != report["provenance"]["source_code"]
            or row.get("numerical_rerun") is not False or row.get("refit_performed") is not False):
        raise IntegrityError("IQ export revision belongs to different numerical evidence")
    if full:
        bindings = _bindings(service, row["files"].values(), KIND, row["id"])
        _bundle(row, bindings)
    return row


def latest(service, report):
    rows = [r for r in service.registry.list(KIND) if r.get("source_report_id") == report["id"]]
    if not rows:
        return None
    return _verify_export(service, max(rows, key=lambda r: (r["created_at"], r["id"])), report)


def overlay_workflow(service, response):
    if response.get("status") != "completed" or not response.get("iq_result"):
        return response
    revision = latest(service, response["iq_result"])
    if not revision:
        return response
    return {**response, "iq_export": revision,
        "links": {**response.get("links", {}), "iq_report": revision["report_url"], "iq_bundle": revision["bundle_url"]}}


def create(service, workflow_id, payload=None):
    if payload not in (None, {}):
        raise ValueError("A report export cannot change numerical inputs or options")
    from . import esfr_iq_report as renderer
    render_code = lab._source_code()
    if service.loaded_environment != render_code:
        raise ConflictError("Restart the current export source before publication")
    renderer_sha256 = _hash(Path(renderer.__file__))
    # Serialize same-process retries; registry identity is the cross-process CAS.
    with _lock:
        report, original, bindings, refs = _source(service, workflow_id)
        key = {"source_report_sha256": report["content_hash"], "schema": SCHEMA,
               "format_version": FORMAT_VERSION, "renderer_sha256": renderer_sha256,
               "render_source_code_sha256": digest(render_code)}
        identity = "esfr-iq-export-" + digest(key)[:32]
        try:
            existing = service.registry.get(KIND, identity)
        except KeyError:
            existing = None
        if existing is not None:
            return _verify_export(service, existing, report, full=True)
        provenance = {"schema": SCHEMA, "format_version": FORMAT_VERSION,
            "source_report_id": report["id"], "source_report_sha256": report["content_hash"],
            "source_payload_sha256": digest(original), "source_report_json_sha256": report["files"]["report.json"],
            "numerical_source_code": original["provenance"]["source_code"],
            "render_source_code": render_code, "renderer_sha256": renderer_sha256,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "numerical_rerun": False, "refit_performed": False,
            "preserved_files": {p: sha for p, sha in report["files"].items() if p not in _REGENERATED}}
        augmented = {**deepcopy(original), "export_provenance": provenance}
        base = iq.ROOT + "/workflows/" + workflow_id + "/iq/exports/" + identity + "/files/"
        with tempfile.TemporaryDirectory(prefix="esfr-iq-export-") as temporary:
            directory = Path(temporary)
            for relative, checksum in provenance["preserved_files"].items():
                destination = directory / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bindings[checksum]["path"], destination)
            previews = directory / "preview-manifest.json"
            images = json.loads(previews.read_text()) if previews.exists() else []
            renderer.publish(directory, augmented, images=images)
            iq._write_json(directory / "report.json", augmented)
            iq._write_json(directory / "source-report.json", original)
            iq._write_json(directory / "export-provenance.json", provenance)
            for relative, checksum in provenance["preserved_files"].items():
                if _hash(directory / relative) != checksum:
                    raise IntegrityError("Presentation export changed an original numerical artifact")
            iq._zip(directory)
            if lab._source_code() != render_code or _hash(Path(renderer.__file__)) != renderer_sha256:
                raise ConflictError("Export source changed during report rendering")
            document = {"schema": SCHEMA, "workflow_id": workflow_id, "source": report["source"],
                "source_kind": report["source_kind"], "source_report_id": report["id"],
                "source_report_sha256": report["content_hash"], "source_payload_sha256": digest(original),
                "composite_candidate_sha256": report["composite_candidate_sha256"],
                "profile_sha256": original["provenance"]["profile_sha256"],
                "numerical_source_code": provenance["numerical_source_code"], "render_source_code": render_code,
                "renderer_sha256": renderer_sha256, "export_provenance": provenance,
                "numerical_rerun": False, "refit_performed": False, "db_active_model_updated": False,
                "physical_validation": report["physical_validation"], "physical_identity_verified": False,
                "report_url": base + "report.html", "bundle_url": base + "bundle.zip"}
            try:
                published = iq._publish(service, KIND, identity, document, directory, refs)
            except ConflictError:
                # Another identical request may have won the immutable insert.
                try:
                    published = service.registry.get(KIND, identity)
                except KeyError:
                    raise
            return _verify_export(service, published, report, full=True)


def file_response(service, workflow_id, identity, relative):
    row = service.registry.get(KIND, identity)
    if row["workflow_id"] != workflow_id:
        raise KeyError("IQ export belongs to another workflow")
    report = service.registry.get(iq.REPORTS, row["source_report_id"])
    _verify_export(service, row, report)
    return iq._response(service, KIND, row, relative)
