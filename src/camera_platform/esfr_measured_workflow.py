"""Resumable measured E-SFR orchestration; immutable jobs own numerical evidence.

The journal is operational state only. Every transition re-enters the existing
verified capture/study/profile APIs. No synthetic input is relabelled measured.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import threading
import time
import uuid

from . import calibration_lab as lab, esfr_lab, esfr_bridge
from .registry import ConflictError, IntegrityError, canonical, digest

ROOT = esfr_lab.ROOT + "/measured"
KIND = "camera_esfr_measured_workflows"
THRESHOLDS = {"raw_sfr_rmse": .03, "isp_sfr_rmse": .05}
_threads = {}
_lock = threading.RLock()


def _directory(service, identity):
    if not isinstance(identity, str) or not __import__('re').fullmatch(r"measured-esfr-[0-9a-f]{32}", identity):
        raise ValueError("Invalid measured workflow identity")
    return service.registry.directory / "esfr-measured-workflows" / identity


def _read(service, identity):
    record = service.registry.get(KIND, identity)
    state = json.loads((_directory(service, identity) / "state.json").read_text())
    if state["input_sha256"] != record["content_hash"]:
        raise IntegrityError("Workflow input revision differs")
    return record, state


def _write(service, identity, state):
    directory = _directory(service, identity)
    directory.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    target = directory / "state.json"
    temporary = directory / ("state-" + uuid.uuid4().hex + ".tmp")
    temporary.write_text(canonical(state))
    temporary.replace(target)


def _current(service, record):
    if record["source_code"] != lab._source_code() or service.loaded_environment != record["source_code"]:
        raise ConflictError("Workflow executable changed; preserve this workflow and create a new one")


def review_contract(service, payload):
    from .esfr_measured import roi_review_digest
    flags = ("all_112_edges_reviewed", "registration_suitable", "fixed_camera_chart", "no_clipping_or_occlusion")
    if any(payload.get(key) is not True for key in flags):
        raise ValueError("Explicit per-capture native ROI review acknowledgements are required")
    operator = esfr_lab._id(payload.get("operator_id"), "operator_id")
    chart = esfr_lab._id(payload.get("physical_chart_id"), "physical_chart_id")
    identities = payload.get("capture_ids", [])
    if not identities or len(identities) != len(set(identities)) or len(identities) > 6:
        raise ValueError("Review exactly the selected measured captures")
    captures = [esfr_lab.capture(service, i) for i in identities]
    if any(c["metadata"]["source_kind"] != "measured" for c in captures):
        raise ValueError("Measured review cannot attest synthetic captures")
    rois = esfr_lab._rois(payload.get("rois"), captures[0]["raw_shape"])
    checksum = roi_review_digest(rois)
    return {"operator_id": operator, "physical_chart_id": chart,
            "chart_model": "imatest_extended_esfr_2023_16_9", "roi_sha256": checksum,
            "capture_reviews": [{"capture_id": c["id"], "raw_sha256": c["files"]["raw"]["sha256"],
                "roi_sha256": checksum, **{key: True for key in flags}} for c in captures],
            "evidence_scope": "operator_declared_not_authenticated"}


def create(service, payload, *, launch=True, source_kind="measured"):
    from .esfr_measured import validate_measured_study
    from . import esfr_iq_service as iq
    if source_kind not in {"measured", "synthetic"}:
        raise ValueError("Unsupported workflow source kind")
    allowed = {"source", "development_ids", "rois", "chart_profile", "measured_review", "roi_reviewed_capture_ids", "iq"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("Unexpected measured workflow field")
    ids = payload.get("development_ids", [])
    if len(ids) != 3 or len(set(ids)) != 3:
        raise ValueError("Select exactly three distinct development RAW/ISP pairs")
    captures = [esfr_lab.capture(service, i) for i in ids]
    if any(c["metadata"]["source_kind"] != source_kind or c["metadata"]["role"] != "development" for c in captures):
        raise ValueError("This workflow requires " + source_kind + " development captures")
    rois = esfr_lab._rois(payload.get("rois"), captures[0]["raw_shape"])
    chart = deepcopy(payload.get("chart_profile") or esfr_lab.template()["chart_profile"])
    review = payload.get("measured_review")
    if source_kind == "measured":
        validate_measured_study(captures, rois, chart, measured_review=review)
    else:
        from .esfr_chart import chart_rois
        expected = {r["id"] for r in chart_rois() if r["kind"] == "edge"}
        edges = [r for r in rois if r["kind"] == "edge"]
        if len(edges) != 112 or {r["id"] for r in edges} != expected:
            raise ValueError("Software verification also requires all 112 chart edges")
    if payload.get("roi_reviewed_capture_ids") is not None and set(payload["roi_reviewed_capture_ids"]) != set(ids):
        raise ValueError("Every development capture must be reviewed")
    source = payload.get("source")
    if not source:
        raise ValueError("Select the exact DB run/candidate/condition to link")
    from .calibration_db import source_selection
    _, context = source_selection(service, source)
    code = lab._source_code()
    if service.loaded_environment != code:
        raise ConflictError("Restart the current source before creating measured workflows")
    iq_config = iq.normalize_iq(service, payload.get("iq"), captures, rois)
    identity = "measured-esfr-" + uuid.uuid4().hex
    record = service.registry.put_verified_record(KIND, {
        "schema": "camera_esfr_measured_workflow_v1", "source": source,
        "source_context": context, "source_code": code, "development_ids": ids,
        "rois": rois, "chart_profile": chart, "measured_review": review,
        "thresholds": THRESHOLDS, "physical_validation": "not_established",
        "source_kind": source_kind, "iq": iq_config,
    }, [(esfr_lab.CAPTURES, c) for c in captures], identity=identity)
    _write(service, identity, {"id": identity, "input_sha256": record["content_hash"],
        "status": "running", "stage": "development_measurement", "receipts": {}, "current_job": None})
    if launch:
        _launch(service, identity)
    return get(service, identity)


def get(service, identity):
    record, state = _read(service, identity)
    current = state.get("current_job")
    if current:
        current = service.registry.get_run_state(current["id"], current["content_hash"])
    status = state["status"]
    with _lock:
        alive = identity in _threads and _threads[identity].is_alive()
    public = {key: value for key, value in state.items() if key not in {"receipts", "pending_operation"}}
    response = {**public, "source": record["source"], "development_ids": record["development_ids"],
        "rois": record["rois"], "chart_profile": record["chart_profile"],
        "measured_review": record["measured_review"], "thresholds": record["thresholds"],
        "source_kind": record.get("source_kind", "measured"), "iq": record.get("iq"),
        "current_job": current, "resume_required": status == "running" and not alive,
        "physical_validation": "not_established"}
    from .esfr_iq_exports import overlay_workflow
    return overlay_workflow(service, response)


def start_validation(service, identity, payload, *, launch=True):
    from .esfr_measured import validate_measured_study
    if set(payload) - {"validation_ids", "roi_reviewed_capture_ids", "measured_review", "iq_patch_roi_reviewed_capture_ids", "iq_noise_repeat_ids"}:
        raise ValueError("Unexpected validation field")
    with _lock:
        record, state = _read(service, identity)
        _current(service, record)
        if state["status"] != "awaiting_validation":
            raise ConflictError("Candidate contract must be ready before new validation")
        ids = payload.get("validation_ids", [])
        if len(ids) != 3 or len(set(ids)) != 3:
            raise ValueError("Select exactly three newly acquired validation pairs")
        captures = [esfr_lab.capture(service, i) for i in record["development_ids"] + ids]
        source_kind = record.get("source_kind", "measured")
        if any(c["metadata"].get("role") != "validation" or c["metadata"].get("source_kind") != source_kind for c in captures[3:]):
            raise ValueError("Validation requires immutable " + source_kind + " validation roles")
        review = deepcopy(payload.get("measured_review") or {})
        frozen_review = record["measured_review"] or {}
        if "development_review" in review and digest(review["development_review"]) != digest(frozen_review):
            raise ValueError("The original development review attribution must be preserved")
        rows = {r.get("capture_id"): r for r in review.get("capture_reviews", [])}
        if any(rows.get(r["capture_id"]) != r for r in frozen_review.get("capture_reviews", [])):
            raise ValueError("The frozen development capture reviews must be preserved unchanged")
        review["development_review"] = deepcopy(frozen_review)
        if source_kind == "measured":
            validate_measured_study(captures, record["rois"], record["chart_profile"], measured_review=review)
        if payload.get("roi_reviewed_capture_ids") is not None and set(payload["roi_reviewed_capture_ids"]) != set(ids):
            raise ValueError("Every validation capture must be reviewed")
        # Preflight acquisition checks; the completed source will be verified
        # independently by export_validation after numerical measurement.
        fresh = captures[3:]
        for capture in fresh:
            if source_kind == "measured":
                contract = esfr_bridge.verify_measured_validation_metadata(service, capture["metadata"])
                if contract["id"] != state["contract"]["id"]:
                    raise ValueError("Validation pair belongs to a different frozen candidate")
            else:
                from datetime import datetime
                contract = state["contract"]
                if capture["metadata"].get("validation_contract") != {"id": contract["id"], "content_hash": contract["content_hash"]}:
                    raise ValueError("Synthetic validation must bind the frozen acquisition contract")
                stamp = datetime.fromisoformat(capture["metadata"].get("generated_at", ""))
                if not datetime.fromisoformat(contract["created_at"]) <= stamp <= datetime.fromisoformat(capture["created_at"]):
                    raise ValueError("Synthetic validation must be generated after candidate freeze")
        lineage = set().union(*(esfr_lab._lineage(c) for c in fresh))
        allowed_fresh = set(ids)
        if record.get("iq", {}).get("enabled"):
            extras = payload.get("iq_noise_repeat_ids", [])
            if not isinstance(extras, list) or any(not isinstance(x, str) for x in extras):
                raise ValueError("IQ noise repeats must be a list of capture IDs")
            allowed_fresh.update(extras)
        elif payload.get("iq_noise_repeat_ids") or payload.get("iq_patch_roi_reviewed_capture_ids"):
            raise ValueError("IQ was not enabled before candidate freeze")
        for old in service.registry.list(esfr_lab.CAPTURES):
            if old["id"] not in allowed_fresh and lineage & esfr_lab._lineage(old):
                raise ConflictError("Validation reuses earlier capture pixels/session/acquisition")
        for prior in service.registry.list_runs(job_kinds={esfr_lab.KIND}):
            if set(ids) & set(prior.get("development_ids", []) + prior.get("validation_ids", [])):
                raise ConflictError("Validation captures have already been used in another study")
        state.update(status="running", stage="validation_measurement", validation_ids=ids,
                     validation_review=review, error=None)
        if record.get("iq", {}).get("enabled"):
            from . import esfr_iq_service as iq
            state["iq_validation"] = iq.validation_request(service, record, state, payload)
        _write(service, identity, state)
        if launch:
            _launch(service, identity)
    return get(service, identity)


def _launch(service, identity):
    with _lock:
        existing = _threads.get(identity)
        if existing and existing.is_alive():
            return
        thread = threading.Thread(target=_run, args=(service, identity), daemon=True,
                                  name="measured-esfr-" + identity[-8:])
        _threads[identity] = thread
        thread.start()


def resume(service, identity):
    with _lock:
        record, state = _read(service, identity)
        _current(service, record)
        if state["status"] != "running":
            raise ConflictError("Only interrupted running workflows can resume; failures stay preserved")
        _launch(service, identity)
    return get(service, identity)


def _run(service, identity):
    try:
        record, state = _read(service, identity)
        _current(service, record)

        def once(key, operation):
            if key in state["receipts"]:
                return state["receipts"][key]
            # An uncertain mutation is never automatically submitted twice.
            if state.get("pending_operation"):
                raise ConflictError("Interrupted mutation needs receipt reconciliation: " + state["pending_operation"])
            state["pending_operation"] = key
            _write(service, identity, state)
            value = operation()
            state["receipts"][key] = value
            state.pop("pending_operation", None)
            _write(service, identity, state)
            return value

        def job(key, operation, reader):
            def ticket():
                value = operation()
                return {"id": value["id"], "content_hash": value["content_hash"]}
            row = once(key, ticket)
            state.update(stage=key, current_job={"id": row["id"], "content_hash": row["content_hash"]})
            _write(service, identity, state)
            while True:
                status = service.registry.get_run_state(row["id"], row["content_hash"])
                if status["status"] not in {"queued", "running"}:
                    if status["status"] != "succeeded":
                        raise ValueError("Numerical stage " + key + " " + status["status"] + ": " + str(status.get("error")))
                    return reader(service, row["id"])
                time.sleep(2)

        spec = {"source": record["source"], "development_ids": record["development_ids"],
            "validation_ids": [], "rois": record["rois"], "chart_profile": record["chart_profile"],
            "thresholds": record["thresholds"], "fit": False, "baseline_sigma_native_px": 1.5,
            "measured_review": record["measured_review"], "name": identity + " development"}
        measured = job("development_measurement", lambda: esfr_lab.submit(service, spec), esfr_lab.get)
        exported = once("development_export", lambda: esfr_bridge.export(service, measured["id"], aggregate_repeats=True))
        request = exported.get("study_request")
        if not request:
            raise ValueError("Development data cannot fit: " + "; ".join(exported.get("study_blockers", [])))
        request = {**request, "name": identity + " RAW forward fit", "fit_method": "esfr_forward_observer_v1"}
        fitted = job("calibration_fit", lambda: lab.submit(service, request), lab.get)
        if record.get("iq", {}).get("enabled"):
            from . import esfr_iq_service as iq
            state["iq_frozen"] = once("iq_freeze", lambda: iq.freeze(service, record, identity, fitted))
        contract = once("contract", lambda: esfr_bridge.create_validation_contract(service, fitted["id"]))
        state.update(contract=contract, fit={"id": fitted["id"], "candidate_sha256": contract["candidate_sha256"]},
            campaign_id=exported["campaign_id"], current_job=None)
        if not state.get("validation_ids"):
            state.update(status="awaiting_validation", stage="candidate_frozen")
            _write(service, identity, state)
            return
        spec.update(validation_ids=state["validation_ids"], measured_review=state["validation_review"],
                    name=identity + " independent measurement")
        if state.get("iq_validation", {}).get("admission_id"):
            spec["iq_validation_admission_id"] = state["iq_validation"]["admission_id"]
        measured_val = job("validation_measurement", lambda: esfr_lab.submit(service, spec), esfr_lab.get)
        holdout = once("validation_export", lambda: esfr_bridge.export_validation(service, measured_val["id"],
            parent_study_id=fitted["id"], validation_contract_id=contract["id"]))
        validated = job("frozen_validation", lambda: lab.validate(service, fitted["id"],
            {"measurement_ids": holdout["validation_ids"]}), lab.get)
        image = validated["result"]["summary"]["esfr_image_validation"]
        passed = image["validation"]["status"] == "pass"
        applied = once("profile", lambda: lab.apply(service, fitted["id"],
            {"campaign_id": exported["campaign_id"], "activate": passed}))
        from .calibration_completion import create as completion
        report = once("report", lambda: completion(service, {"study_id": fitted["id"],
            "validation_id": validated["id"], "profile_id": applied["profile"]["id"]}))
        state.update(status="running", stage="report_finalization", current_job=None,
            validation={"id": validated["id"], "image_validation": image["validation"], "metrics": image["metrics"]},
            report=report, profile_id=applied["profile"]["id"], profile_activated=passed,
            links={"report": report["report_url"], "bundle": report["bundle_url"],
                "lab": exported["calibration_lab_url"] + "&study=" + fitted["id"] + "&validation=" + validated["id"],
                "image_report": image["report_url"]})
        if record.get("iq", {}).get("enabled"):
            state["stage"] = "iq_report"
            state["iq_result"] = once("iq_report", lambda: iq.validate(service, record, state))
            state["links"].update(iq_report=state["iq_result"]["report_url"],
                                  iq_bundle=state["iq_result"]["bundle_url"])
        # Publish completion once, after every requested report and link exists.
        # once() persists both before execution and after receipt storage.
        state.update(status="completed", stage="report_ready")
        _write(service, identity, state)
    except Exception as exc:
        try:
            _, state = _read(service, identity)
            state.update(status="failed", error=str(exc), current_job=None)
            _write(service, identity, state)
        except Exception:
            pass


def install_routes(app, service):
    @app.get(ROOT + "/template")
    def template():
        from .esfr_measured import measured_template
        from .esfr_iq_service import iq_template
        return {**measured_template(), "iq": iq_template()}

    @app.post(ROOT + "/review-contract")
    def review(payload: dict):
        return review_contract(service, payload)

    @app.post(ROOT + "/workflows", status_code=202)
    def start(payload: dict):
        return create(service, payload)

    @app.post(ROOT + "/software-workflows", status_code=202)
    def software_start(payload: dict):
        return create(service, payload, source_kind="synthetic")

    @app.get(ROOT + "/workflows/{identity}")
    def show(identity: str):
        return get(service, identity)

    @app.post(ROOT + "/workflows/{identity}/validate", status_code=202)
    def validate(identity: str, payload: dict):
        return start_validation(service, identity, payload)

    @app.post(ROOT + "/workflows/{identity}/resume", status_code=202)
    def continue_work(identity: str):
        return resume(service, identity)

    @app.get(ROOT + "/workflows/{identity}/iq/files/{relative:path}")
    def iq_file(identity: str, relative: str):
        from .esfr_iq_service import file_response
        return file_response(service, identity, relative)

    @app.post(ROOT + "/workflows/{identity}/iq/exports")
    def iq_export(identity: str, payload: dict):
        from .esfr_iq_exports import create
        return create(service, identity, payload)

    @app.get(ROOT + "/workflows/{identity}/iq/exports/{export_id}/files/{relative:path}")
    def iq_export_file(identity: str, export_id: str, relative: str):
        from .esfr_iq_exports import file_response
        return file_response(service, identity, export_id, relative)

    @app.post(ROOT + "/iq-comparisons")
    def iq_comparison(payload: dict):
        from .esfr_iq_service import compare
        return compare(service, payload)

    @app.get(ROOT + "/iq-latest")
    def iq_latest(source_run: str, candidate: str, condition: str | None = None):
        from .esfr_iq_service import latest, latest_comparison
        source = {"run_id": source_run, "candidate_id": candidate}
        if condition is not None:source["condition_id"] = condition
        result = {"report": latest(service, source)}
        comparison = latest_comparison(service, source)
        if comparison is not None:
            result["comparison"] = comparison
        return result

    @app.get(ROOT + "/iq-comparisons/latest")
    def iq_comparison_latest(source_run: str, candidate: str, condition: str | None = None):
        from .esfr_iq_service import latest_comparison
        source = {"run_id": source_run, "candidate_id": candidate}
        if condition is not None:source["condition_id"] = condition
        return {"comparison": latest_comparison(service, source)}

    @app.get(ROOT + "/iq-comparisons/{identity}/files/{relative:path}")
    def iq_comparison_file(identity: str, relative: str):
        from .esfr_iq_service import comparison_file_response
        return comparison_file_response(service, identity, relative)
