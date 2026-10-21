"""Hash-pinned Job/API lifecycle for declared calibration on saved-quality RAW."""

from __future__ import annotations

import hashlib
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any

import numpy as np
from fastapi import File, Form, UploadFile
from fastapi.responses import Response

from pyisetcam.dataset import _sha256_array

from . import isp_v2
from .registry import IntegrityError, canonical, digest
from .retained_raw_dataset_job import _source
from .saved_sensor_calibrated_isp import (
    CALIBRATION_SCHEMA,
    MAX_CALIBRATION_BYTES,
    _binding,
    _candidate_profile,
    _read_calibration,
    _sha,
    _validate_calibration,
    run_saved_sensor_calibrated_isp,
    validate_calibration_bytes,
)
from .saved_sensor_isp_replay import inspect_saved_quality_isp_input

SCHEMA = "camerae2e_saved_sensor_calibrated_isp_study_v1"
JOB_KIND = "saved_sensor_calibrated_isp"
LIMITATION = (
    "Applies a separately supplied, hash-pinned numeric LSC/noise calibration "
    "to one exact saved-quality simulation RAW frame. Calibration provenance "
    "and independence are declarations, not verified measurements. The mosaic "
    "has simulation coordinates, not product-die coordinates. Before/after "
    "differences are diagnostic and do not establish image-quality improvement."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "저장 센서 RAW · 선언 보정 ISP",
        "source_quality_job_id": "",
        "sensor_report_index": 0,
        "case_index": 0,
        "calibration_sha256": "",
        "profile": isp_v2.default_profile(),
        "acknowledge_simulation_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the saved sensor calibrated ISP fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit saved sensor ISP simulation-limit acknowledgement required.")
    name, identity = payload["name"], payload["source_quality_job_id"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Calibrated ISP name requires 1..160 characters.")
    if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 128:
        raise ValueError("Select one completed source_quality_job_id.")
    for key in ("sensor_report_index", "case_index"):
        if type(payload[key]) is not int or not 0 <= payload[key] <= 2:
            raise ValueError(f"{key} must be an integer index from 0 to 2.")
    sha = _sha(payload["calibration_sha256"], "calibration_sha256")
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["source_quality_job_id"] = identity.strip()
    result["profile"] = _candidate_profile(payload["profile"], sha)
    return result


def _source_input(service: Any, spec: dict[str, Any]) -> tuple[dict, dict, dict]:
    source = _source(service, spec)
    report_path = service.registry.blob_path(source["source_artifacts"]["json"]["sha256"])
    arrays_path = service.registry.blob_path(source["source_artifacts"]["npz"]["sha256"])
    checked = inspect_saved_quality_isp_input(
        report_path,
        arrays_path,
        source_quality_job_id=spec["source_quality_job_id"],
        case_index=spec["case_index"],
        sensor_db_root=Path(service.workspace) / "camerae2e_db",
        profile=isp_v2.default_profile(),
    )
    if (
        checked["source_binding"]["saved_sensor_revision"] != source["source_revision"]
        or checked["source_binding"]["source_report_sha256"]
        != source["source_artifacts"]["json"]["sha256"]
        or checked["source_binding"]["source_arrays_sha256"]
        != source["source_artifacts"]["npz"]["sha256"]
    ):
        raise IntegrityError("Calibrated ISP source differs from the completed quality job.")
    report = json.loads(report_path.read_bytes())
    binding = _binding(report, report["cases"][spec["case_index"]], checked)
    return source, checked, binding


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    source, checked, binding = _source_input(service, spec)
    sha = spec["calibration_sha256"]
    calibration_path = service.registry.blob_path(sha)
    record = _read_calibration(calibration_path, sha)
    validated = _validate_calibration(record, expected_binding=binding)
    isp_v2.validate_inputs(
        checked["raw"],
        checked["raw_format"],
        spec["profile"],
        geometry=checked["geometry"],
        calibration=validated["payload"],
    )
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        **source,
        "source_binding": binding,
        "raw_content_sha256": checked["raw_content_sha256"],
        "calibration_artifact": {
            "id": sha,
            "name": "saved-sensor-isp-calibration.json",
            "sha256": sha,
            "bytes": calibration_path.stat().st_size,
            "media_type": "application/json",
            "url": f"/api/v1/artifacts/{sha}",
        },
        "calibration_provenance": validated["provenance"],
        "calibration_payload_sha256": digest(record["payload"]),
        "calibration_numeric_sha256": digest(validated["payload"]),
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Saved sensor calibrated ISP job is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen RAW, calibration, ISP graph or source changed.")
    spec = frozen["specification"]
    with tempfile.TemporaryDirectory(prefix="camera-saved-calibrated-isp-") as temporary:
        report = run_saved_sensor_calibrated_isp(
            service.registry.blob_path(frozen["source_artifacts"]["json"]["sha256"]),
            service.registry.blob_path(frozen["source_artifacts"]["npz"]["sha256"]),
            source_quality_job_id=spec["source_quality_job_id"],
            case_index=spec["case_index"],
            sensor_db_root=Path(service.workspace) / "camerae2e_db",
            calibration_path=service.registry.blob_path(spec["calibration_sha256"]),
            calibration_sha256=spec["calibration_sha256"],
            profile=spec["profile"],
            output_dir=Path(temporary) / "replay",
        )
        if (
            canonical(report["source_binding"]) != canonical(frozen["source_binding"])
            or report["raw_content_sha256"] != frozen["raw_content_sha256"]
            or report["calibration"]["file_sha256"] != spec["calibration_sha256"]
            or report["calibration"]["numeric_sha256"]
            != frozen["calibration_numeric_sha256"]
        ):
            raise IntegrityError("Calibrated ISP output lost its frozen input contract.")
        if canonical(prepare(service, spec)) != canonical(frozen):
            raise IntegrityError("Saved source or calibration changed during ISP replay.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Saved sensor calibrated ISP job was cancelled during execution.")
        artifacts = []
        for kind, media in (("json", "application/json"), ("npz", "application/octet-stream")):
            content = Path(report["artifacts"][kind]).read_bytes()
            if hashlib.sha256(content).hexdigest() != report["artifact_sha256"][kind]:
                raise IntegrityError(f"Calibrated ISP {kind} artifact changed.")
            artifacts.append(
                service.registry.store_blob(content, f"saved-sensor-calibrated-isp.{kind}", media)
            )
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_quality_result_sha256": frozen["source_quality_result_sha256"],
        "source_binding": frozen["source_binding"],
        "raw_content_sha256": frozen["raw_content_sha256"],
        "raw_values_byte_equal": report["raw_values_byte_equal"],
        "calibration_artifact": frozen["calibration_artifact"],
        "calibration_payload_sha256": frozen["calibration_payload_sha256"],
        "calibration": report["calibration"],
        "before_after": report["before_after"],
        "reference_isp": report["reference_isp"],
        "candidate_isp": report["candidate_isp"],
        "evidence": {"json": artifacts[0], "npz": artifacts[1]},
        "verification": {
            "implementation_verification": "runtime_completed",
            "calibration_file_bytes": "sha256_verified",
            "calibration_provenance": "supplier_declared_not_independently_verified",
            "physical_validation": "not_established",
        },
        "limitation": LIMITATION,
    }
    artifacts.append(
        service.registry.store_blob(
            canonical(summary).encode(),
            "saved-sensor-calibrated-isp-result.json",
            "application/json",
        )
    )
    return {"summary": summary, "artifacts": artifacts}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a saved sensor calibrated ISP replay.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        spec = job["specification"]
        # Historical reads verify frozen artifacts. They do not require the
        # current saved DB revision or executable to remain installed.
        source = _source(service, spec)
        for key in (
            "source_quality_result_sha256",
            "source_revision",
            "source_artifacts",
            "source_case",
        ):
            if canonical(source[key]) != canonical(job[key]):
                raise IntegrityError(f"Calibrated ISP frozen {key} changed.")
        binding = job["source_binding"]
        if (
            binding["source_report_sha256"] != source["source_artifacts"]["json"]["sha256"]
            or binding["source_arrays_sha256"] != source["source_artifacts"]["npz"]["sha256"]
            or binding["saved_sensor_revision"] != source["source_revision"]
            or binding["source_quality_job_id"] != spec["source_quality_job_id"]
            or binding["source_case_index"] != spec["case_index"]
        ):
            raise IntegrityError("Calibrated ISP source binding differs from frozen artifacts.")
        calibration_sha = spec["calibration_sha256"]
        calibration_path = service.registry.blob_path(calibration_sha)
        calibration = json.loads(calibration_path.read_bytes())
        if (
            calibration["binding"] != job["source_binding"]
            or calibration["provenance"] != job["calibration_provenance"]
            or digest(calibration["payload"]) != job["calibration_payload_sha256"]
            or job["calibration_artifact"]["sha256"] != calibration_sha
            or job["calibration_artifact"]["bytes"] != calibration_path.stat().st_size
            or spec["profile"]["calibration_id"] != f"sha256:{calibration_sha}"
        ):
            raise IntegrityError("Calibrated ISP frozen calibration bytes or binding changed.")
        if (
            canonical(summary["specification"]) != canonical(spec)
            or canonical(summary["source_binding"]) != canonical(job["source_binding"])
            or summary["source_quality_result_sha256"] != job["source_quality_result_sha256"]
            or summary["raw_content_sha256"] != job["raw_content_sha256"]
            or summary["calibration_artifact"] != job["calibration_artifact"]
            or summary["calibration_payload_sha256"] != job["calibration_payload_sha256"]
            or summary["calibration"]["file_sha256"] != spec["calibration_sha256"]
            or summary["calibration"]["numeric_sha256"] != job["calibration_numeric_sha256"]
        ):
            raise IntegrityError("Calibrated ISP result differs from frozen request.")
        if job["status"] == "succeeded":
            if len(job["artifacts"]) != 3 or any(
                summary["evidence"][kind] not in job["artifacts"] for kind in ("json", "npz")
            ):
                raise IntegrityError("Calibrated ISP evidence links are missing.")
            for descriptor in job["artifacts"]:
                service.registry.blob_path(descriptor["sha256"])
            source_json = json.loads(
                service.registry.blob_path(summary["evidence"]["json"]["sha256"]).read_text()
            )
            if (
                source_json["artifacts"]["npz_sha256"]
                != summary["evidence"]["npz"]["sha256"]
                or canonical(source_json["source_binding"])
                != canonical(summary["source_binding"])
                or source_json["calibration"]["file_sha256"]
                != spec["calibration_sha256"]
                or source_json["raw_content_sha256"] != job["raw_content_sha256"]
                or source_json["raw_values_byte_equal"] is not True
                or any(
                    canonical(source_json[key]) != canonical(summary[key])
                    for key in ("calibration", "before_after", "reference_isp", "candidate_isp")
                )
            ):
                raise IntegrityError("Calibrated ISP report differs from frozen source.")
            source_arrays = service.registry.blob_path(
                source["source_artifacts"]["npz"]["sha256"]
            )
            result_arrays = service.registry.blob_path(summary["evidence"]["npz"]["sha256"])
            source_key = source["source_case"]["array_prefix"] + "_raw_dn_frame"
            with np.load(source_arrays, allow_pickle=False) as original:
                with np.load(result_arrays, allow_pickle=False) as processed:
                    raw = np.asarray(original[source_key])
                    retained = np.asarray(processed["source_raw_dn_frame"])
                    if (
                        raw.dtype != retained.dtype
                        or raw.shape != retained.shape
                        or raw.tobytes(order="C") != retained.tobytes(order="C")
                        or _sha256_array(raw) != job["raw_content_sha256"]
                    ):
                        raise IntegrityError("Calibrated ISP NPZ lost the exact source RAW.")
            result_descriptor = next(
                (
                    item
                    for item in job["artifacts"]
                    if item["name"] == "saved-sensor-calibrated-isp-result.json"
                ),
                None,
            )
            if result_descriptor is None or (
                service.registry.blob_path(result_descriptor["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Calibrated ISP result artifact differs.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def import_calibration(service: Any, content: bytes, expected_sha256: str) -> dict[str, Any]:
    document = validate_calibration_bytes(content, expected_sha256)
    descriptor = service.registry.store_blob(
        content, "saved-sensor-isp-calibration.json", "application/json"
    )
    if descriptor["sha256"] != expected_sha256:
        raise IntegrityError("Stored calibration bytes differ from submitted hash.")
    return {
        "schema": CALIBRATION_SCHEMA,
        "artifact": descriptor,
        "calibration_sha256": expected_sha256,
        "provenance": document["provenance"],
        "verification": {
            "file_bytes": "sha256_verified",
            "provenance": "supplier_declared_not_independently_verified",
            "source_applicability": "checked_only_when_job_is_submitted",
        },
    }


def install_routes(app: Any, service: Any) -> None:
    @app.get("/api/v1/saved-sensor-calibrated-isp/template", tags=[JOB_KIND])
    def template(
        source_quality_job_id: str = "",
        sensor_report_index: int = 0,
        case_index: int = 0,
    ):
        sources = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") == "saved_sensor_quality" and item["status"] == "succeeded"
        ]
        spec = default_spec()
        expected_binding = None
        if source_quality_job_id:
            spec.update(
                source_quality_job_id=source_quality_job_id,
                sensor_report_index=sensor_report_index,
                case_index=case_index,
            )
            _, _, expected_binding = _source_input(service, spec)
        return {
            "specification": spec,
            "source_quality_jobs": sources,
            "expected_calibration_binding": expected_binding,
            "calibration_document_template": (
                None
                if expected_binding is None
                else {
                    "schema": CALIBRATION_SCHEMA,
                    "provenance": None,
                    "binding": expected_binding,
                    "payload": None,
                }
            ),
            "calibration_import": {
                "path": "/api/v1/saved-sensor-calibrated-isp/calibrations/import",
                "media_type": "multipart/form-data",
                "fields": ["calibration", "sha256"],
                "max_json_bytes": MAX_CALIBRATION_BYTES,
            },
            "limitation": LIMITATION,
        }

    @app.post(
        "/api/v1/saved-sensor-calibrated-isp/calibrations/import",
        status_code=201,
        tags=[JOB_KIND],
    )
    async def import_json(
        calibration: Annotated[UploadFile, File()], sha256: Annotated[str, Form()]
    ):
        if calibration.content_type not in {"application/json", "application/octet-stream"}:
            raise ValueError("Calibration upload must be JSON bytes.")
        content = await calibration.read(MAX_CALIBRATION_BYTES + 1)
        return import_calibration(service, content, sha256)

    @app.post("/api/v1/saved-sensor-calibrated-isp", status_code=202, tags=[JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/saved-sensor-calibrated-isp", tags=[JOB_KIND])
    def listing():
        return list_records(service)

    @app.get("/api/v1/saved-sensor-calibrated-isp/{identity}", tags=[JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/saved-sensor-calibrated-isp/{identity}/cancel", tags=[JOB_KIND])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/saved-sensor-calibrated-isp/{identity}/export", tags=[JOB_KIND])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="saved-sensor-calibrated-isp.json"'
            },
        )
