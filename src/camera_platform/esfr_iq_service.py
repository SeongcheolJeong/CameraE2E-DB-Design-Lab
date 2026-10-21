"""Immutable, condition-bound E-SFR IQ orchestration.

The numerical engine never receives validation inputs during development. A
CCM here belongs to a declared reference ISP and nominal chart reference; it
does not estimate spectral QE, sensor gain, illuminant SPD or a vendor ISP.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
from pathlib import Path
import tempfile
import zipfile

from . import calibration_lab as lab, esfr_bridge, esfr_lab
from .registry import ConflictError, IntegrityError, canonical, digest

FROZEN = "camera_esfr_iq_frozen"
REPORTS = "camera_esfr_iq_reports"
COMPARISONS = "camera_esfr_iq_comparisons"
ADMISSIONS = "camera_esfr_iq_admissions"
WORKFLOWS = "camera_esfr_measured_workflows"
REFERENCE_ID = "imatest_esfr_2023_nominal_v1"
CONFIG_SCHEMA = "camera_esfr_iq_config_v1"
ROOT = esfr_lab.ROOT + "/measured"
MAX_REPEAT_CAPTURES = 8
MAX_ARTIFACT_BYTES = 512 * 1024**2
MAX_ARTIFACT_FILES = 2048
CONTEXT = ("source_kind", "camera_id", "conditions", "raw_format", "geometry",
           "pixel_pitch_um", "isp", "sampling", "raw_processing")


def iq_template():
    return {"schema": CONFIG_SCHEMA, "enabled": False, "reference_id": REFERENCE_ID,
        "actual_isp_color_space": "unknown", "patch_roi_reviewed_capture_ids": [],
        "noise_repeat_ids": [], "required_patch_count": {"color": 18, "gray": 20},
        "reference_status": "nominal_design_reference_not_measured_print",
        "noise_repeat_protocol": "Core three captures form the repeat set; optionally select 0–5 EXTRA same-condition captures for 3–8 total frames. Review every patch ROI. Freeze the extra count before acquiring equal-count validation repeats.",
        "capabilities": {"conditional_reference_isp_ccm": True,
            "qe_fit": False, "sensor_gain_fit": False, "illuminant_spd_fit": False,
            "physical_certification": False, "camera_dynamic_range_measurement": False}}


def _ids(value, name, *, required=None, maximum=MAX_REPEAT_CAPTURES):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError(name + " must be an explicit list of capture IDs")
    if len(value) != len(set(value)) or len(value) > maximum:
        raise ValueError(name + " must contain unique bounded capture IDs")
    if required is not None and len(value) != required:
        raise ValueError(name + " requires " + str(required) + " captures")
    return list(value)


def _patches(rois):
    from .esfr_chart import chart_manifest
    from .esfr_iq_engine import MAX_PATCH_PIXELS, MAX_TOTAL_PATCH_PIXELS
    expected = {p["id"]: kind for kind, key in (("color", "color_patches"), ("gray", "gray_patches"))
                for p in chart_manifest()[key]}
    rows = [{"id": r["id"], "kind": r["kind"], "box": list(r["box"])}
            for r in rois if r.get("kind") in {"color", "gray"}]
    if len(rows) != 38 or {r["id"]: r["kind"] for r in rows} != expected:
        raise ValueError("IQ requires every named 18 color and 20 gray patch exactly once")
    if len({tuple(r["box"]) for r in rows}) != 38:
        raise ValueError("Each IQ patch requires its own native ROI")
    for index, row in enumerate(rows):
        if len(row["box"]) != 4 or any(type(v) is not int for v in row["box"]) or min(row["box"][:2]) < 0 or min(row["box"][2:]) < 4:
            raise ValueError("Patch ROIs require bounded native integer boxes at least 4 by 4")
        y, x, h, w = row["box"]
        if h * w > MAX_PATCH_PIXELS:
            raise ValueError("IQ patch exceeds the native ROI resource budget")
        for prior in rows[:index]:
            yy, xx, hh, ww = prior["box"]
            if min(y + h, yy + hh) > max(y, yy) and min(x + w, xx + ww) > max(x, xx):
                raise ValueError("IQ patch native boxes must not overlap")
    if sum(r["box"][2] * r["box"][3] for r in rows) > MAX_TOTAL_PATCH_PIXELS:
        raise ValueError("IQ aggregate patch size exceeds the native ROI resource budget")
    return sorted(rows, key=lambda r: r["id"])


def _context(capture):
    return {k: deepcopy(capture["metadata"].get(k)) for k in CONTEXT}


def _captures(service, identities, role, context=None):
    rows = [esfr_lab.capture(service, identity) for identity in identities]
    if not rows:
        return rows
    context = context or _context(rows[0])
    if context.get("source_kind") not in {"measured", "synthetic"}:
        raise ValueError("IQ source kind must remain explicitly measured or synthetic")
    for capture in rows:
        if capture["metadata"].get("role") != role or _context(capture) != context:
            raise ValueError("IQ captures must have the declared role and identical fixed sensor/ISP/conditions")
    for field in ("native_pixel_sha256", "isp_pixel_sha256"):
        values = [r.get(field) for r in rows]
        if None in values or len(set(values)) != len(values):
            raise ValueError("IQ captures must contain distinct native RAW and ISP pixels")
    acquisitions = [(r["metadata"].get("camera_id"), r["metadata"].get("acquisition_id")) for r in rows]
    if any(a[1] is None for a in acquisitions) or len(set(acquisitions)) != len(acquisitions):
        raise ValueError("IQ captures require distinct acquisition identities")
    return rows


def _repeat_ids(value):
    return _ids(value, "noise_repeat_ids", maximum=MAX_REPEAT_CAPTURES - 3)


def _review(value, capture_ids):
    ids = _ids(value, "patch_roi_reviewed_capture_ids", maximum=11)
    if set(ids) != set(capture_ids):
        raise ValueError("Review all 18 color and 20 gray native ROIs in every selected capture, including noise repeats")
    return ids


def normalize_iq(service, iq, captures, rois):
    # Omission supports existing clients; an explicit version is never guessed.
    if isinstance(iq, dict) and "schema" in iq and iq["schema"] != CONFIG_SCHEMA:
        raise ValueError("Unsupported IQ configuration schema")
    if iq is None or iq == {"enabled": False}:
        return {"enabled": False}
    allowed = {"schema", "enabled", "reference_id", "actual_isp_color_space", "patch_roi_reviewed_capture_ids", "noise_repeat_ids"}
    if not isinstance(iq, dict) or set(iq) - allowed or type(iq.get("enabled")) is not bool:
        raise ValueError("IQ requires an explicit supported configuration")
    if not iq["enabled"]:
        return {"enabled": False}
    if iq.get("reference_id") != REFERENCE_ID:
        raise ValueError("Only the explicit published nominal E-SFR reference is supported")
    if iq.get("actual_isp_color_space") not in {"srgb", "unknown"}:
        raise ValueError("Declare actual ISP color space as srgb or unknown")
    core = _ids([c["id"] for c in captures], "development_ids", required=3)
    rows = _captures(service, core, "development")
    if rows != captures:
        raise IntegrityError("Development capture snapshots changed")
    repeats = _repeat_ids(iq.get("noise_repeat_ids", []))
    if set(repeats) & set(core):
        raise ValueError("noise_repeat_ids are EXTRA captures, disjoint from the core three")
    ids = list(dict.fromkeys(core + repeats))
    all_rows = _captures(service, ids, "development", _context(rows[0]))
    patches = _patches(rois)
    for capture in all_rows:
        for roi in patches:
            r, c, h, w = roi["box"]
            if r + h > capture["raw_shape"][0] or c + w > capture["raw_shape"][1]:
                raise ValueError("IQ patch ROI is outside the native capture")
    return {"schema": CONFIG_SCHEMA, "enabled": True, "reference_id": REFERENCE_ID,
        "actual_isp_color_space": iq["actual_isp_color_space"],
        "patch_roi_reviewed_capture_ids": _review(iq.get("patch_roi_reviewed_capture_ids"), ids),
        "noise_repeat_ids": repeats, "patch_rois": patches, "patch_roi_sha256": digest(patches),
        "noise_frame_count": len(ids),
        "capture_context": _context(rows[0]), "capture_context_sha256": digest(_context(rows[0])),
        "capture_snapshots": all_rows,
        "review_evidence": "operator_declared_native_patch_registration_not_authenticated"}


def _inputs(service, captures):
    return [{"record": c, "raw_path": esfr_lab._binding(service, c, "raw"),
             "isp_path": esfr_lab._binding(service, c, "isp")} for c in captures]


def _capture_evidence(captures):
    return [{"id": c["id"], "content_hash": c["content_hash"],
        "role": c["metadata"]["role"], "source_kind": c["metadata"]["source_kind"],
        "raw_sha256": c["files"]["raw"]["sha256"], "isp_sha256": c["files"]["isp"]["sha256"],
        "native_pixel_sha256": c["native_pixel_sha256"], "isp_pixel_sha256": c["isp_pixel_sha256"]}
        for c in captures]


def _current(service, record):
    if service.registry.get(WORKFLOWS, record["id"]) != record:
        raise IntegrityError("IQ workflow input revision changed")
    if record["source_code"] != lab._source_code() or service.loaded_environment != record["source_code"]:
        raise ConflictError("IQ execution source differs from the frozen workflow")


def _write_json(path, value):
    path.write_text(canonical(value), encoding="utf-8")


def _publish(service, kind, identity, document, directory, references):
    paths = sorted(p for p in Path(directory).rglob("*") if p.is_file())
    if not paths or len(paths) > MAX_ARTIFACT_FILES or sum(p.stat().st_size for p in paths) > MAX_ARTIFACT_BYTES:
        raise ValueError("IQ report exceeds bounded artifact resources")
    artifacts, files = [], {}
    for path in paths:
        if path.is_symlink():
            raise IntegrityError("IQ output may not be a symbolic link")
        relative = path.relative_to(directory).as_posix()
        descriptor = service.registry.store_file(path, relative, mimetypes.guess_type(relative)[0] or "application/octet-stream")
        artifacts.append(descriptor)
        files[relative] = descriptor["sha256"]
    return service.registry.put_verified_record(kind, {**document, "files": files, "artifacts": artifacts}, references, identity=identity)


def freeze(service, record, workflow_id, fit):
    from . import esfr_iq_engine as engine
    _current(service, record)
    if record["id"] != workflow_id or not record.get("iq", {}).get("enabled"):
        raise ValueError("IQ freeze requires its enabled immutable workflow")
    fit = lab.get(service, fit["id"])
    if fit["status"] != "succeeded":
        raise ValueError("IQ freeze follows a completed development MTF candidate")
    summary = fit["result"]["summary"]
    if digest(summary["candidate_profile"]) != summary["candidate_sha256"]:
        raise IntegrityError("MTF candidate hash differs")
    source_ids = {m.get("provenance", {}).get("esfr_run_id") for m in fit.get("measurement_snapshots", [])}
    if None in source_ids or len(source_ids) != 1:
        raise IntegrityError("IQ requires one exact MTF development source")
    source = esfr_lab.get(service, next(iter(source_ids)))
    if (source["development_ids"] != record["development_ids"] or source.get("validation_ids")
            or source["specification"].get("source") != record["source"]
            or source["specification"]["rois"] != record["rois"]):
        raise IntegrityError("IQ and MTF development source, captures or native ROIs differ")
    identity = "esfr-iq-frozen-" + digest({"workflow_id": workflow_id, "fit_id": fit["id"], "candidate_sha256": summary["candidate_sha256"]})[:32]
    try:
        existing = service.registry.get(FROZEN, identity)
    except KeyError:
        existing = None
    if existing is not None:
        _verify_frozen(service, record, existing)
        return existing
    config = record["iq"]
    captures = _captures(service, record["development_ids"], "development", config["capture_context"])
    all_rows = _captures(service, [c["id"] for c in config["capture_snapshots"]], "development", config["capture_context"])
    if all_rows != config["capture_snapshots"]:
        raise IntegrityError("Frozen development or noise-repeat snapshot changed")
    if config["patch_roi_sha256"] != digest(_patches(record["rois"])):
        raise IntegrityError("Frozen native patch ROI mapping differs")
    numerical = engine.run(_inputs(service, captures), config["patch_rois"], actual_isp_color_space=config["actual_isp_color_space"])
    if numerical["reference"].get("id") != REFERENCE_ID:
        raise IntegrityError("Numerical engine changed the requested nominal reference")
    document = {"schema": "camera_esfr_iq_frozen_v1", "workflow_id": workflow_id,
        "workflow_sha256": record["content_hash"], "source": record["source"], "source_context": record["source_context"],
        "source_code": record["source_code"], "source_kind": captures[0]["metadata"]["source_kind"],
        "fit_id": fit["id"], "mtf_candidate_sha256": summary["candidate_sha256"], "fit_result_sha256": digest(fit["result"]),
        "profile": numerical["profile"], "profile_sha256": digest(numerical["profile"]),
        "reference": numerical["reference"], "reference_sha256": digest(numerical["reference"]),
        "patch_roi_sha256": config["patch_roi_sha256"], "capture_context_sha256": config["capture_context_sha256"],
        "development_capture_evidence": _capture_evidence(all_rows), "numerical": numerical,
        "physical_validation": "not_established", "physical_identity_verified": False,
        "fit_data_role": "development_only", "db_activation": False}
    with tempfile.TemporaryDirectory(prefix="esfr-iq-freeze-") as temporary:
        directory = Path(temporary)
        _write_json(directory / "iq-profile.json", document["profile"])
        _write_json(directory / "development.json", numerical)
        _write_json(directory / "frozen.json", document)
        return _publish(service, FROZEN, identity, document, directory,
                        [(WORKFLOWS, record), *[(esfr_lab.CAPTURES, c) for c in all_rows]])


def _verify_frozen(service, record, frozen):
    if service.registry.get(FROZEN, frozen["id"]) != frozen:
        raise IntegrityError("Frozen IQ receipt differs from immutable record")
    if frozen["workflow_id"] != record["id"] or frozen["workflow_sha256"] != record["content_hash"]:
        raise IntegrityError("Frozen IQ belongs to a different workflow")
    if frozen["source"] != record["source"] or frozen["source_context"] != record["source_context"] or frozen["source_code"] != record["source_code"]:
        raise IntegrityError("Frozen IQ DB source or executable differs")
    if digest(frozen["profile"]) != frozen["profile_sha256"] or digest(frozen["reference"]) != frozen["reference_sha256"]:
        raise IntegrityError("Frozen IQ profile or reference changed")
    if frozen["patch_roi_sha256"] != digest(_patches(record["rois"])) or frozen["capture_context_sha256"] != digest(record["iq"]["capture_context"]):
        raise IntegrityError("Frozen IQ ROI or acquisition context changed")
    for evidence in frozen["development_capture_evidence"]:
        current = esfr_lab.capture(service, evidence["id"])
        if _capture_evidence([current])[0] != evidence:
            raise IntegrityError("Frozen IQ development capture changed")
    for checksum in frozen["files"].values():
        service.registry.artifact_binding(checksum, owner_kind=FROZEN, owner_id=frozen["id"])
    return frozen


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("IQ acquisition needs an explicit timezone-aware timestamp") from exc
    if result.tzinfo is None:
        raise ValueError("IQ acquisition time must have a timezone")
    return result


def _contract(service, record, state):
    frozen = _verify_frozen(service, record, state["iq_frozen"])
    contract, parent = esfr_bridge._verify_validation_contract(service, state["contract"]["id"], frozen["fit_id"])
    if contract != state["contract"] or contract["source"] != frozen["source"]:
        raise IntegrityError("IQ fresh acquisition contract changed source or revision")
    if contract["candidate_sha256"] != frozen["mtf_candidate_sha256"] or digest(parent["result"]) != frozen["fit_result_sha256"]:
        raise IntegrityError("IQ and MTF candidates were not frozen together")
    if _time(contract["created_at"]) < _time(frozen["created_at"]):
        raise IntegrityError("New acquisition contract must be issued after IQ candidate freeze")
    return frozen, contract


def _fresh(service, rows, contract, context, allowed_ids):
    for capture in rows:
        metadata = capture["metadata"]
        if metadata["source_kind"] == "measured":
            esfr_bridge._verify_measured_metadata(metadata, contract, imported_at=capture["created_at"])
        else:
            if metadata.get("validation_contract") != {"id": contract["id"], "content_hash": contract["content_hash"]}:
                raise IntegrityError("Synthetic IQ validation must bind the frozen acquisition contract")
            if not _time(contract["created_at"]) <= _time(metadata.get("generated_at")) <= _time(capture["created_at"]):
                raise IntegrityError("Synthetic IQ validation must be newly generated after candidate freeze")
        if _context(capture) != context:
            raise IntegrityError("IQ validation changed the fixed capture conditions")
    lineage = set().union(*(esfr_lab._lineage(c) for c in rows)) if rows else set()
    for prior in service.registry.list(esfr_lab.CAPTURES):
        if prior["id"] not in allowed_ids and lineage & esfr_lab._lineage(prior):
            raise ConflictError("IQ validation reuses earlier native pixels or acquisition lineage")
    return rows


def validation_request(service, record, state, payload):
    frozen, contract = _contract(service, record, state)
    core = _ids(state["validation_ids"], "validation_ids", required=3)
    repeats = _repeat_ids(payload.get("iq_noise_repeat_ids", []))
    if set(repeats) & set(core):
        raise ValueError("Validation noise_repeat_ids must be EXTRA captures, disjoint from core three")
    if len(repeats) != len(record["iq"]["noise_repeat_ids"]):
        raise ValueError("Validation noise repeat count must equal the frozen development protocol")
    ids = list(dict.fromkeys(core + repeats))
    rows = _captures(service, ids, "validation", record["iq"]["capture_context"])
    if set(ids) & set(record["development_ids"] + record["iq"]["noise_repeat_ids"]):
        raise ConflictError("IQ development and validation captures must be disjoint")
    review = _review(payload.get("iq_patch_roi_reviewed_capture_ids"), ids)
    _fresh(service, rows, contract, record["iq"]["capture_context"], set(ids))
    _inputs(service, rows)
    allowed_jobs = {v["id"] for v in state.get("receipts", {}).values()
                    if isinstance(v, dict) and isinstance(v.get("id"), str)}
    for prior in service.registry.list_runs(job_kinds={esfr_lab.KIND}):
        if prior["id"] not in allowed_jobs and set(ids) & set(prior.get("development_ids", []) + prior.get("validation_ids", [])):
            raise ConflictError("IQ validation captures were already exposed to another chart study")
    for prior in service.registry.list(REPORTS):
        used = {c["id"] for c in prior["provenance"]["validation_captures"]}
        if prior["workflow_id"] != record["id"] and set(ids) & used:
            raise ConflictError("IQ validation captures were already exposed to another IQ candidate")
    request = {"validation_ids": core, "noise_repeat_ids": repeats,
        "patch_roi_reviewed_capture_ids": review, "patch_roi_sha256": frozen["patch_roi_sha256"],
        "frozen_profile_sha256": frozen["profile_sha256"], "validation_contract_id": contract["id"],
        "validation_contract_sha256": contract["content_hash"], "capture_snapshots": rows,
        "review_evidence": "operator_declared_native_patch_registration_not_authenticated"}
    document = {"schema": "camera_esfr_iq_validation_admission_v1", "workflow_id": record["id"],
        "workflow_sha256": record["content_hash"], "frozen_id": frozen["id"], "frozen_sha256": frozen["content_hash"], **request}
    identity = "esfr-iq-admission-" + digest(document)[:32]
    try:
        admission = service.registry.get(ADMISSIONS, identity)
        if any(admission.get(k) != v for k, v in document.items()):
            raise IntegrityError("Existing IQ admission differs from immutable inputs")
    except KeyError:
        admission = service.registry.put_verified_record(ADMISSIONS, document,
            [(WORKFLOWS, record), (FROZEN, frozen), (esfr_bridge.VALIDATION_CONTRACTS, contract),
             *[(esfr_lab.CAPTURES, c) for c in rows]], identity=identity)
    return {**request, "admission_id": admission["id"], "admission_sha256": admission["content_hash"]}


def mtf_noise_companions(service, admission_id, contract, core_ids):
    """Permit only independently verified patch-only repeats in this acquisition.

    They remain outside the MTF core-three fit/replay but may share their session.
    No general session or lineage exemption is granted.
    """
    admission = service.registry.get(ADMISSIONS, admission_id)
    if admission.get("schema") != "camera_esfr_iq_validation_admission_v1" or admission["validation_ids"] != core_ids:
        raise IntegrityError("IQ noise admission does not bind these exact MTF core captures")
    if (admission["validation_contract_id"] != contract["id"]
            or admission["validation_contract_sha256"] != contract["content_hash"]
            or service.registry.get(esfr_bridge.VALIDATION_CONTRACTS, contract["id"]) != contract):
        raise IntegrityError("IQ noise admission belongs to another acquisition contract")
    record = service.registry.get(WORKFLOWS, admission["workflow_id"])
    frozen = service.registry.get(FROZEN, admission["frozen_id"])
    if record["content_hash"] != admission["workflow_sha256"] or frozen["content_hash"] != admission["frozen_sha256"]:
        raise IntegrityError("IQ noise admission source revision changed")
    _contract(service, record, {"iq_frozen": frozen, "contract": contract})
    repeats = _repeat_ids(admission["noise_repeat_ids"])
    if len(repeats) != len(record["iq"]["noise_repeat_ids"]) or set(repeats) & set(core_ids):
        raise IntegrityError("IQ noise admission changed its frozen repeated-frame protocol")
    ids = core_ids + repeats
    rows = _captures(service, ids, "validation", record["iq"]["capture_context"])
    if rows != admission["capture_snapshots"]:
        raise IntegrityError("IQ noise admission capture snapshots changed")
    _fresh(service, rows, contract, record["iq"]["capture_context"], set(ids))
    _inputs(service, rows)  # Every exact capture owner and original byte hash.
    for prior in service.registry.list_runs(job_kinds={esfr_lab.KIND}):
        if prior.get("specification", {}).get("iq_validation_admission_id") != admission_id and set(ids) & set(prior.get("development_ids", []) + prior.get("validation_ids", [])):
            raise ConflictError("IQ companions were exposed by a different chart study")
    for prior in service.registry.list(REPORTS):
        if prior["workflow_id"] != record["id"] and set(ids) & {c["id"] for c in prior["provenance"]["validation_captures"]}:
            raise ConflictError("IQ companions were exposed to a different IQ candidate")
    return set(repeats)


def _noise(service, identities, config, role):
    if not identities:
        return {"status": "unavailable", "reason": "No explicitly declared repeat set", "sensor_gain_fit": False, "ptc_fit": False}
    from . import esfr_iq_engine as engine
    captures = _captures(service, identities, role, config["capture_context"])
    return engine.analyze_repeats(_inputs(service, captures), config["patch_rois"],
                                  actual_isp_color_space=config["actual_isp_color_space"])


def _zip(directory):
    paths = sorted(p for p in directory.rglob("*") if p.is_file())
    if len(paths) + 2 > MAX_ARTIFACT_FILES or sum(p.stat().st_size for p in paths) > MAX_ARTIFACT_BYTES // 2:
        raise ValueError("IQ report and ZIP exceed bounded artifact resources")
    manifest = []
    for path in paths:
        checksum = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        manifest.append({"path": path.relative_to(directory).as_posix(), "sha256": checksum.hexdigest(), "bytes": path.stat().st_size})
    _write_json(directory / "bundle-manifest.json", {"schema": "camera_esfr_iq_bundle_v1", "files": manifest})
    with zipfile.ZipFile(directory / "bundle.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in [*paths, directory / "bundle-manifest.json"]:
            bundle.write(path, path.relative_to(directory).as_posix())


def validate(service, record, state):
    from . import esfr_iq_engine as engine, esfr_iq_report as renderer
    _current(service, record)
    frozen, contract = _contract(service, record, state)
    request = state["iq_validation"]
    recomputed = validation_request(service, record, state, {
        "iq_patch_roi_reviewed_capture_ids": request["patch_roi_reviewed_capture_ids"], "iq_noise_repeat_ids": request["noise_repeat_ids"]})
    if recomputed != request:
        raise IntegrityError("IQ validation inputs changed after admission")
    identity = "esfr-iq-report-" + digest({"workflow_id": record["id"], "frozen_sha256": frozen["content_hash"], "validation": request})[:32]
    try:
        existing = service.registry.get(REPORTS, identity)
    except KeyError:
        existing = None
    if existing is not None:
        _verify_files(service, REPORTS, existing)
        return existing
    rows = _captures(service, state["validation_ids"], "validation", record["iq"]["capture_context"])
    inputs = _inputs(service, rows)
    numerical = engine.run(inputs, record["iq"]["patch_rois"], frozen_profile=deepcopy(frozen["profile"]),
        actual_isp_color_space=record["iq"]["actual_isp_color_space"])
    if digest(numerical["profile"]) != frozen["profile_sha256"] or digest(numerical["reference"]) != frozen["reference_sha256"]:
        raise IntegrityError("Validation attempted to alter the frozen IQ profile or reference")
    validation = lab.get(service, state["validation"]["id"])
    if validation["status"] != "succeeded" or validation.get("parent_study_id") != frozen["fit_id"] or validation.get("candidate_sha256") != frozen["mtf_candidate_sha256"]:
        raise IntegrityError("Linked MTF validation does not belong to the frozen candidate")
    mtf_profile = service.registry.get(lab.PROFILES, state["profile_id"])
    if mtf_profile["profile_sha256"] != frozen["mtf_candidate_sha256"]:
        raise IntegrityError("Linked MTF profile differs from frozen candidate")
    composite = {"schema": "camera_esfr_condition_bound_composite_v1", "source": record["source"],
        "source_context_sha256": digest(record["source_context"]), "source_kind": frozen["source_kind"],
        "mtf": {"profile_id": mtf_profile["id"], "profile_sha256": mtf_profile["profile_sha256"], "profile": mtf_profile["profile"]},
        "reference_isp": {"profile_sha256": frozen["profile_sha256"], "profile": frozen["profile"]},
        "fixed_conditions": record["iq"]["capture_context"], "patch_roi_sha256": frozen["patch_roi_sha256"],
        "reference_sha256": frozen["reference_sha256"], "workflow_id": record["id"],
        "scope": "effective_MTF_and_nominal_chart_reference_ISP_CCM_only",
        "db_design_use": "condition_bound_linked_candidate", "db_active_model_updated": False,
        "qe_updated": False, "sensor_gain_updated": False, "physical_validation": "not_established"}
    composite["id"] = "esfr-iq-composite-" + digest(composite)[:32]
    base = ROOT + "/workflows/" + record["id"] + "/iq/files/"
    payload = {"schema": "camera_esfr_iq_report_v1", "workflow_id": record["id"],
        "source": record["source"], "source_context": record["source_context"], "source_kind": frozen["source_kind"],
        "physical_validation": "not_established", "physical_identity_verified": False,
        "chart_profile": record["chart_profile"],
        "reference": frozen["reference"], "profile": frozen["profile"], "numerical": numerical,
        "development": frozen["numerical"].get("development", frozen["numerical"]), "validation": numerical["validation"],
        "composite_candidate": composite, "composite_candidate_sha256": digest(composite),
        "noise_repeat_analysis": {"development": _noise(service, record["development_ids"] + record["iq"]["noise_repeat_ids"], record["iq"], "development"),
                                  "validation": _noise(service, request["validation_ids"] + request["noise_repeat_ids"], record["iq"], "validation"),
                                  "protocol": {"core_frames": 3, "additional_frames": len(request["noise_repeat_ids"]),
                                    "total_frames": 3 + len(request["noise_repeat_ids"]), "minimum_mode": len(request["noise_repeat_ids"]) == 0,
                                    "recommended_total_frames": 8, "additional_frames_used_for_ccm_fit": False}},
        "mtf_evidence": {"validation_id": validation["id"], "validation_result_sha256": digest(validation["result"]),
            "image_validation": state["validation"]["image_validation"], "report_url": state["links"].get("image_report"),
            "completion_report_url": state["report"]["report_url"]},
        "provenance": {"frozen_id": frozen["id"], "frozen_sha256": frozen["content_hash"], "profile_sha256": frozen["profile_sha256"],
            "frozen_at": frozen["created_at"], "contract_id": contract["id"], "contract_sha256": contract["content_hash"],
            "admission_id": request["admission_id"], "admission_sha256": request["admission_sha256"],
            "contract_created_at": contract["created_at"], "source_code": record["source_code"],
            "patch_roi_sha256": frozen["patch_roi_sha256"], "reference_sha256": frozen["reference_sha256"],
            "development_captures": frozen["development_capture_evidence"], "validation_captures": _capture_evidence(request["capture_snapshots"]),
            "validation_fit_performed": False, "same_raw_before_after": True,
            "acquisition_time_authenticated": False, "physical_identity_verified": False},
        "limitations": ["Nominal chart design targets are not measured print reflectance or spectral QE.",
            "Reference ISP CCM is condition-bound; actual camera ISP is observed separately and is not changed.",
            "Chart-range OECF, clipping and temporal noise do not measure full camera dynamic range or sensor gain.",
            "Synthetic evidence remains synthetic; measured identity and acquisition timestamps are operator-declared."]}
    with tempfile.TemporaryDirectory(prefix="esfr-iq-report-") as temporary:
        directory = Path(temporary)
        from . import esfr_iq_replay
        payload["combined_replay"] = esfr_iq_replay.execute(service, validation, frozen["profile"], directory)
        images = renderer.render_chart_previews(inputs, frozen["profile"], directory)
        renderer.publish(directory, payload, images=images)
        _write_json(directory / "report.json", payload)
        _write_json(directory / "composite-candidate.json", composite)
        _write_json(directory / "frozen-iq.json", frozen)
        _write_json(directory / "acquisition-contract.json", contract)
        _zip(directory)
        document = {**payload, "report_url": base + "report.html", "bundle_url": base + "bundle.zip",
            "db_active_model_updated": False}
        return _publish(service, REPORTS, identity, document, directory,
            [(WORKFLOWS, record), (FROZEN, frozen), (esfr_bridge.VALIDATION_CONTRACTS, contract),
             (lab.PROFILES, mtf_profile), *[(esfr_lab.CAPTURES, c) for c in request["capture_snapshots"]]])


def _verify_files(service, kind, record):
    from .calibration_completion import _bindings
    _bindings(service, list(record["files"].values()), kind, record["id"])
    return record


def _report_for_workflow(service, workflow_id):
    rows = [r for r in service.registry.list(REPORTS) if r["workflow_id"] == workflow_id]
    if len(rows) != 1:
        raise KeyError("IQ report is not complete for this exact workflow")
    return rows[0]


def _response(service, kind, row, relative):
    from fastapi.responses import FileResponse
    if relative not in row["files"]:
        raise KeyError("Unknown immutable IQ artifact")
    binding = service.registry.artifact_binding(row["files"][relative], owner_kind=kind, owner_id=row["id"])
    return FileResponse(binding["path"], media_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
                        filename=Path(relative).name if relative.endswith(".zip") else None)


def file_response(service, workflow_id, relative):
    return _response(service, REPORTS, _report_for_workflow(service, workflow_id), relative)


def latest(service, source):
    rows = [r for r in service.registry.list(REPORTS) if r["source"] == source]
    if not rows:
        return None
    row = max(rows, key=lambda r: (r["created_at"], r["id"]))
    result = {k: row[k] for k in ("id", "workflow_id", "source", "source_kind", "report_url", "bundle_url",
                                 "composite_candidate_sha256", "physical_validation", "db_active_model_updated")}
    from .esfr_iq_exports import latest as latest_export
    revision = latest_export(service, row)
    if revision:
        result.update(iq_export=revision, report_url=revision["report_url"], bundle_url=revision["bundle_url"])
    return result


def _comparison_replay_snapshot(report):
    original = report.get("combined_replay", {})
    if "curves" not in original:
        return deepcopy(original)
    relative = "combined-replay/result.json" if "combined-replay/result.json" in report.get("files", {}) else "iq-result.json"
    if relative not in report.get("files", {}):
        raise IntegrityError("A/B retained curve source artifact is unavailable")
    value = deepcopy({k:v for k,v in original.items() if k != "curves"})
    value["retained_curves"] = {"owner_kind": REPORTS, "owner_id": report["id"],
        "source_report_sha256": report["content_hash"], "source_file": relative,
        "source_file_sha256": report["files"][relative], "curves_payload_sha256": digest(original["curves"]),
        "url": report["report_url"].rsplit("/", 1)[0] + "/" + relative,
        "scope": "original native replay curves retained without duplicate inline arrays"}
    return value


def compare(service, payload):
    if not isinstance(payload, dict) or set(payload) != {"workflow_a_id", "workflow_b_id"} or payload["workflow_a_id"] == payload["workflow_b_id"]:
        raise ValueError("HDR comparison requires two distinct completed IQ workflows")
    a, b = (_report_for_workflow(service, payload[k]) for k in ("workflow_a_id", "workflow_b_id"))
    _verify_files(service, REPORTS, a)
    _verify_files(service, REPORTS, b)
    if a["source_kind"] != b["source_kind"] or a["reference"] != b["reference"]:
        raise ValueError("HDR comparison needs the same source kind and nominal reference")
    if a["chart_profile"] != b["chart_profile"] or a["provenance"]["patch_roi_sha256"] != b["provenance"]["patch_roi_sha256"]:
        raise ValueError("Chart-range comparison must preserve the same chart revision and native patch ROIs")
    ca, cb = a["composite_candidate"]["fixed_conditions"], b["composite_candidate"]["fixed_conditions"]
    required = ("camera_id", "geometry", "pixel_pitch_um")
    if any(ca.get(k) != cb.get(k) for k in required):
        raise ValueError("Chart-range comparison requires the same declared camera and native geometry")
    conditions_a, conditions_b = ca["conditions"], cb["conditions"]
    allowed_changes = {"mode_id", "gain", "exposure_s"}
    difference = {k: {"a": conditions_a.get(k), "b": conditions_b.get(k)}
                  for k in set(conditions_a) | set(conditions_b) if conditions_a.get(k) != conditions_b.get(k)}
    if set(difference) - allowed_changes:
        raise ValueError("Chart-range comparison must preserve chart lighting and optics conditions")
    from . import esfr_iq_ab as ab
    comparison_revision = ab.REVISION
    comparison_code = lab._source_code()
    if service.loaded_environment != comparison_code:
        raise ConflictError("Restart current comparison source before publication")
    identity = "esfr-iq-comparison-" + digest({"a": a["content_hash"], "b": b["content_hash"],
        "comparison_revision": comparison_revision, "comparison_source_code": comparison_code})[:32]
    try:
        existing = service.registry.get(COMPARISONS, identity)
    except KeyError:
        existing = None
    if existing is not None:
        return _verify_files(service, COMPARISONS, existing)
    from . import esfr_iq_report as renderer
    document = {"schema": "camera_esfr_iq_comparison_v1", "comparison_id": identity,
        "comparison_revision": comparison_revision, "judgement": "pending_criteria",
        "scope": "same_chart_range_mode_comparison_only", "camera_dynamic_range_measured": False,
        "source_kind": a["source_kind"], "physical_validation": "not_established",
        "workflow_a_id": a["workflow_id"], "workflow_b_id": b["workflow_id"],
        "report_a_sha256": a["content_hash"], "report_b_sha256": b["content_hash"],
        "mode_a": {"source": a["source"], "conditions": conditions_a, "validation": a["validation"], "report_url": a["report_url"],
            "mtf_evidence": deepcopy(a.get("mtf_evidence", {})),
            "combined_replay": _comparison_replay_snapshot(a)},
        "mode_b": {"source": b["source"], "conditions": conditions_b, "validation": b["validation"], "report_url": b["report_url"],
            "mtf_evidence": deepcopy(b.get("mtf_evidence", {})),
            "combined_replay": _comparison_replay_snapshot(b)},
        "reference": a["reference"], "noise_a": a["noise_repeat_analysis"], "noise_b": b["noise_repeat_analysis"],
        "condition_differences": difference, "comparison_source_code": comparison_code,
        "ab_comparison": ab.assemble(a, b, ab.native_mtf(service, a), ab.native_mtf(service, b)),
        "limitations": ["This fixed chart spans only its own nominal reflectance range; it cannot establish sensor or camera dynamic range.",
            "No HDR fusion, temporal motion, flare or exposure sweep validation is inferred.",
            "Each condition has its own frozen CCM; differences are not attributable to sensor hardware alone."]}
    base = ROOT + "/iq-comparisons/" + identity + "/files/"
    with tempfile.TemporaryDirectory(prefix="esfr-iq-compare-") as temporary:
        directory = Path(temporary)
        document["comparison_previews"] = ab.copy_previews(service, a, b, directory)
        ab.publish_tables(directory, document)
        renderer.publish_comparison(directory, document)
        _write_json(directory / "report.json", document)
        _zip(directory)
        return _publish(service, COMPARISONS, identity, {**document, "report_url": base + "report.html", "bundle_url": base + "bundle.zip"}, directory,
                        [(REPORTS, a), (REPORTS, b)])


def comparison_file_response(service, identity, relative):
    return _response(service, COMPARISONS, service.registry.get(COMPARISONS, identity), relative)


def latest_comparison(service, source):
    rows = [r for r in service.registry.list(COMPARISONS)
            if all(r.get(mode, {}).get("source") == source for mode in ("mode_a", "mode_b"))]
    if not rows:
        return None
    row = max(rows, key=lambda r: (r["created_at"], r["id"]))
    result = {k: row[k] for k in ("id", "comparison_id", "workflow_a_id", "workflow_b_id",
        "report_url", "bundle_url", "source_kind", "physical_validation", "comparison_revision")}
    result["mtf_status"] = {}
    for mode, label in (("mode_a", "a"), ("mode_b", "b")):
        gate = row.get(mode, {}).get("mtf_evidence", {}).get("image_validation", {})
        checks = gate.get("checks")
        result["mtf_status"][label] = {"status": gate.get("status", "unavailable"),
            "fail_count": sum(c.get("status") == "fail" for c in checks) if isinstance(checks, list) else None,
            "decision_checks": sum(c.get("status") != "diagnostic" for c in checks) if isinstance(checks, list) else None,
            "diagnostic_checks": sum(c.get("status") == "diagnostic" for c in checks) if isinstance(checks, list) else None}
    return result
