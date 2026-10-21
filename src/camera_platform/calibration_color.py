"""Explicit spectral-QE, untouched-RAW validation and ISP-CCM evidence workflow."""

from __future__ import annotations

import json
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np

from . import calibration_lab as lab
from .registry import ConflictError, IntegrityError, canonical, digest, now

COLLECTION = "calibration_color_tasks"
SCHEMA = "camera_spectral_color_record_v1"
ROUTE = lab.ROUTE + "/color-calibrations"


def decode_input_json(value):
    """Preserve numeric lexemes in profile-bearing input from browser editors."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > 32 * 1024**2:
        raise ValueError("input_json requires bounded JSON text")

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate calibration JSON key: " + key)
            result[key] = item
        return result

    def finite(value):
        raise ValueError("Nonfinite calibration JSON value: " + value)

    try:
        return json.loads(value, object_pairs_hook=unique, parse_constant=finite)
    except RecursionError as exc:
        raise ValueError("Invalid calibration JSON nesting") from exc


def _inputs(payload):
    required = {"schema", "name", "qe_scan"}
    optional = {
        "campaign_id",
        "baseline_qe",
        "raw_validation",
        "ccm_training",
        "ccm_validation",
        "raw_originals",
        "source_notes",
    }
    if (
        not isinstance(payload, dict)
        or not required <= set(payload)
        or set(payload) - required - optional
        or payload["schema"] != "camera_spectral_color_task_v1"
    ):
        raise ValueError("Expected a camera_spectral_color_task_v1 input document")
    if (
        not isinstance(payload["name"], str)
        or not 1 <= len(payload["name"]) <= 200
        or len(canonical(payload).encode()) > 32 * 1024**2
    ):
        raise ValueError("A bounded task name and input smaller than 32 MiB are required")
    if bool(payload.get("ccm_training")) != bool(payload.get("ccm_validation")):
        raise ValueError("CCM training and independent validation must be provided together")
    for key in (
        "qe_scan",
        "baseline_qe",
        "raw_validation",
        "ccm_training",
        "ccm_validation",
        "raw_originals",
        "source_notes",
    ):
        if key in payload and not isinstance(payload[key], dict):
            raise ValueError(f"{key} must be an object")
    return deepcopy(payload)


def _raw_inputs(payload):
    from .calibration_color_raw import observe_raw

    originals = payload.get("raw_originals", {})
    if not isinstance(originals, dict) or set(originals) - {"training", "validation"}:
        raise ValueError("RAW originals may only have training and validation acquisitions")
    evidence, files = {}, {}
    for role, source in originals.items():
        content, observed = observe_raw(source)
        if 2 ** source["bit_depth"] - 1 != payload["qe_scan"]["adc_white_dn"]:
            raise ValueError("RAW original bit depth differs from the fixed QE scan ADC")
        evidence[role] = observed
        files[f"raw-{role}.npy"] = (content, "application/octet-stream")
        if role == "validation" and payload.get("raw_validation"):
            raw = payload["raw_validation"]
            if raw.get("measured_raw_rgb_dn"):
                raise ValueError("RAW observations must be empty when an original is supplied")
            raw["measured_raw_rgb_dn"] = observed["rgb_dn"]
            raw["clipped_fraction"] = observed["clipped_fraction"]
            raw["provenance"]["source_sha256"] = observed["sha256"]
            if raw["adc_white_dn"] != 2 ** source["bit_depth"] - 1:
                raise ValueError("RAW bit depth differs from spectral comparison metadata")
        ccm = payload.get("ccm_" + role)
        if ccm:
            if ccm.get("camera_rgb"):
                raise ValueError("CCM camera RGB must be empty when an original is supplied")
            normalization = ccm.get("normalization_metadata")
            if not isinstance(normalization, dict):
                raise ValueError("CCM requires explicit RAW normalization metadata")
            black, gain, white = [
                normalization.get(k) for k in ("black_level_dn", "dn_per_e", "reference_white_e")
            ]
            if (
                any(type(v) not in (int, float) or not np.isfinite(v) for v in (black, gain, white))
                or black < 0
                or min(gain, white) <= 0
            ):
                raise ValueError("RAW to CCM normalization must use explicit finite constants")
            if ccm["normalization"] != "fixed_reference_electrons":
                raise ValueError("Native RAW CCM requires fixed_reference_electrons normalization")
            scan = payload["qe_scan"]
            if gain != scan["dn_per_e"]:
                raise ValueError("CCM normalization gain differs from fixed sensor scan gain")
            if role == "validation" and payload.get("raw_validation"):
                if not np.all(np.asarray(payload["raw_validation"]["black_level_dn"]) == black):
                    raise ValueError("CCM normalization black differs from RAW comparison")
            ccm["camera_rgb"] = ((np.asarray(observed["rgb_dn"]) - black) / gain / white).tolist()
            ccm["source_artifact_sha256"] = observed["sha256"]
            ccm["clipped_patch_indices"] = [
                i for i, values in enumerate(observed["clipped_fraction"]) if any(values)
            ]
    if set(evidence) == {"training", "validation"}:
        if set(evidence["training"]["frame_sha256"]) & set(evidence["validation"]["frame_sha256"]):
            raise ValueError("CCM training and validation share original RAW frames")
    payload.pop("raw_originals", None)
    return evidence, files


def create(service, payload):
    from .calibration_ccm import fit_ccm, validate_ccm
    from .calibration_color_report import render_color_report
    from .calibration_spectral import derive_qe, validate_raw

    original = _inputs(payload)
    environment = lab._source_code()
    if service.loaded_environment != environment:
        raise ConflictError(
            "Restart the server with the current calibration source before executing"
        )
    identity = "color-calibration-" + digest({"input": original, "environment": environment})[:32]
    with lab._lock(service):
        try:
            return get(service, identity)
        except KeyError:
            pass
        data = deepcopy(original)
        evidence, contents = _raw_inputs(data)
        qe = derive_qe(data["qe_scan"])
        source_kind = qe["source_kind"]
        references = []
        if data.get("campaign_id"):
            campaign = service.registry.get(lab.CAMPAIGNS, data["campaign_id"])
            if campaign["camera_id"] != qe["conditions"]["camera_id"]:
                raise ValueError("QE sensor identity differs from the selected campaign")
            references.append((lab.CAMPAIGNS, campaign))
        raw = {"candidate": {"status": "not_evaluated", "reason": "No RAW validation supplied"}}
        ccm = {
            "profile": None,
            "validation": {"status": "not_evaluated", "reason": "No CCM data supplied"},
        }
        for key in ("raw_validation", "ccm_training", "ccm_validation"):
            if data.get(key):
                if data[key]["source_kind"] != source_kind:
                    raise ValueError("Synthetic and measured evidence cannot be mixed")
                if data[key]["conditions"] != qe["conditions"]:
                    raise ValueError("QE, RAW and CCM must use the same frozen sensor conditions")
        if data.get("raw_validation"):
            scan_prov, raw_prov = qe["provenance"], data["raw_validation"]["provenance"]
            if any(scan_prov[k] == raw_prov[k] for k in ("session_id", "acquisition_id")):
                raise ValueError("QE scan and RAW validation must use independent acquisitions")
            if scan_prov.get("source_sha256") and (
                scan_prov["source_sha256"] == raw_prov.get("source_sha256")
            ):
                raise ValueError("QE scan and RAW validation share an original")
        if data.get("raw_validation") and qe["response_kind"] == "absolute_qe":
            raw["candidate"] = validate_raw(qe, data["raw_validation"])
            if data.get("baseline_qe"):
                raw["baseline"] = validate_raw(data["baseline_qe"], data["raw_validation"])
        elif data.get("raw_validation"):
            raw["candidate"]["reason"] = "Relative spectral response cannot predict absolute RAW DN"
        frozen_raw_sha256 = digest({"qe": qe, "raw": raw, "originals": evidence})
        if data.get("ccm_training"):
            fitted = fit_ccm(data["ccm_training"])
            ccm = {
                "profile": fitted["profile"],
                "training": fitted,
                "validation": validate_ccm(fitted["profile"], data["ccm_validation"]),
            }
        candidate_binding = digest({"qe": qe, "ccm": ccm["profile"]})
        if frozen_raw_sha256 != digest({"qe": qe, "raw": raw, "originals": evidence}):
            raise IntegrityError("CCM processing changed frozen QE or RAW observations")
        tokens = []
        for heldout in (data.get("raw_validation"), data.get("ccm_validation")):
            if not heldout:
                continue
            prov = heldout.get("provenance", heldout)
            tokens.extend(
                [
                    str(qe["conditions"]["camera_id"]) + ":" + k + ":" + str(prov[k])
                    for k in ("session_id", "acquisition_id")
                ]
            )
            source_hash = heldout.get("source_artifact_sha256") or prov.get("source_sha256")
            if source_hash:
                tokens.append("source:" + source_hash)
            observed = heldout.get("measured_raw_rgb_dn") or heldout.get("camera_rgb")
            if observed:
                tokens.append("observations:" + digest(sorted(observed)))
        tokens.extend(
            "frame:" + value for value in evidence.get("validation", {}).get("frame_sha256", [])
        )
        tokens = sorted(set(tokens))
        if tokens:
            for previous in service.registry.list(COLLECTION):
                if (
                    set(tokens) & set(previous.get("heldout_tokens", []))
                    and candidate_binding != previous["candidate_binding"]
                ):
                    raise ConflictError(
                        "This held-out acquisition already evaluated another candidate"
                    )
        statuses = [raw["candidate"]["status"], ccm["validation"]["status"]]
        record = {
            "schema": SCHEMA,
            "report_generated_at": now(),
            "name": data["name"],
            "campaign_id": data.get("campaign_id"),
            "source_kind": source_kind,
            "physical_validation": "not_established",
            "status": "pass"
            if all(s == "pass" for s in statuses)
            else "fail"
            if "fail" in statuses
            else "inconclusive",
            "qe": {"profile": qe, "baseline_profile": data.get("baseline_qe")},
            "raw_validation": raw,
            "ccm": ccm,
            "input_sha256": digest(original),
            "candidate_binding": candidate_binding,
            "heldout_tokens": tokens,
            "evidence": {
                "inputs": data,
                "raw_originals": evidence,
                "source_environment": environment,
            },
            "raw_unchanged_by_ccm": True,
            "raw_before_and_after_ccm_sha256": frozen_raw_sha256,
            "scope": "separate spectral patch forward model and fixed post-RAW ISP matrix; "
            "not an automatic rewrite of existing DB or chart campaign profiles",
            "report_url": f"{ROUTE}/{identity}/files/report.html",
            "bundle_url": f"{ROUTE}/{identity}/bundle",
        }
        report_data = {**record, "id": identity, "created_at": record["report_generated_at"]}
        contents.update(
            {
                "report.html": (render_color_report(report_data), "text/html"),
                "report.json": (canonical(report_data).encode(), "application/json"),
                "qe-profile.json": (canonical(qe).encode(), "application/json"),
                "ccm-profile.json": (canonical(ccm["profile"]).encode(), "application/json"),
                "inputs.json": (canonical(original).encode(), "application/json"),
            }
        )
        artifacts = [
            service.registry.store_blob(content, name, media)
            for name, (content, media) in contents.items()
        ]
        record.update(files={a["name"]: a["sha256"] for a in artifacts}, artifacts=artifacts)
        return service.registry.put_verified_record(
            COLLECTION, record, references, identity=identity
        )


def get(service, identity):
    record = service.registry.get(COLLECTION, identity)
    if record.get("schema") != SCHEMA:
        raise IntegrityError("Unexpected spectral color task record")
    return record


def file(service, identity, relative):
    record = get(service, identity)
    if relative not in record["files"]:
        raise KeyError("Unknown color calibration artifact")
    return service.registry.artifact_binding(
        record["files"][relative], owner_kind=COLLECTION, owner_id=identity
    )["path"]


def bundle(service, identity):
    record = get(service, identity)
    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    path = Path(handle.name)
    handle.close()
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("record.json", canonical(record))
            for name in record["files"]:
                archive.write(file(service, identity, name), name)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def install_routes(app, service):
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    @app.get(lab.ROUTE + "/color-template", tags=["calibration_lab"])
    def color_template():
        import secrets

        from .calibration_color_demo import template

        # Each imported example represents new training/validation acquisitions.
        return template(secrets.randbelow(2**31 - 102))

    @app.get(ROUTE, tags=["calibration_lab"])
    def list_tasks():
        return {
            "items": [
                {
                    k: row.get(k)
                    for k in (
                        "id",
                        "name",
                        "campaign_id",
                        "status",
                        "source_kind",
                        "created_at",
                        "report_url",
                        "bundle_url",
                    )
                }
                for row in service.registry.list(COLLECTION)
            ]
        }

    @app.post(ROUTE, status_code=201, tags=["calibration_lab"])
    def create_task(payload: dict):
        if set(payload) == {"input_json"}:
            payload = decode_input_json(payload["input_json"])
        return create(service, payload)

    @app.get(ROUTE + "/{identity}", tags=["calibration_lab"])
    def get_task(identity: str):
        return get(service, identity)

    @app.get(ROUTE + "/{identity}/files/{relative:path}", tags=["calibration_lab"])
    def get_file(identity: str, relative: str):
        import mimetypes

        return FileResponse(
            file(service, identity, relative),
            media_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
        )

    @app.get(ROUTE + "/{identity}/bundle", tags=["calibration_lab"])
    def get_bundle(identity: str):
        path = bundle(service, identity)
        return FileResponse(
            path,
            media_type="application/zip",
            filename=identity + ".zip",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )
