"""Durable, exact-RAW Lens × Sensor ISP v2 calibration sensitivity job."""

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
from .lens_sensor_calibrated_isp import (
    CALIBRATION_SCHEMA,
    MAX_CALIBRATION_BYTES,
    REUSABLE_CALIBRATION_SCHEMA,
    _candidate_profile,
    _read_calibration,
    _sha,
    _validate_calibration,
    calibration_binding,
    reusable_calibration_binding,
    run_lens_sensor_calibrated_isp,
    validate_lens_calibration_bytes,
)
from .registry import ConflictError, IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_calibrated_isp_study_v1"
JOB_KIND = "lens_sensor_calibrated_isp"
LIMITATION = (
    "Applies separately supplied, hash-pinned numeric LSC/noise hints to one exact "
    "geometric Lens DB x saved Sensor DB RAW. Lens transmission and relative "
    "illumination are unknown unity assumptions, and the calibration source and "
    "independence are supplier declarations, not independently verified. LSC "
    "effects are sensitivities, not proven real-lens shading correction or "
    "measured image-quality improvement. Physical validation is not established."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 RAW · 선언 보정 ISP",
        "source_lens_sensor_job_id": "",
        "calibration_sha256": "",
        "profile": isp_v2.default_profile(),
        "acknowledge_simulation_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the Lens x Sensor calibrated ISP fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit Lens x Sensor calibration-limit acknowledgement required.")
    name, identity = payload["name"], payload["source_lens_sensor_job_id"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Calibrated ISP name requires 1..160 characters.")
    if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 128:
        raise ValueError("Select one completed source_lens_sensor_job_id.")
    sha = _sha(payload["calibration_sha256"], "calibration_sha256")
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["source_lens_sensor_job_id"] = identity.strip()
    result["profile"] = _candidate_profile(payload["profile"], sha)
    return result


def _source_input(service: Any, spec: dict[str, Any]) -> tuple[dict, dict, dict, dict]:
    from .retained_raw_dataset_job import _lens_source
    from .retained_raw_isp_bridge import inspect_lens_sensor_isp_input

    source = _lens_source(service, {"source_job_id": spec["source_lens_sensor_job_id"]})
    report_path = service.registry.blob_path(source["source_artifacts"]["json"]["sha256"])
    raw_path = service.registry.blob_path(source["source_artifacts"]["raw"]["sha256"])
    checked = inspect_lens_sensor_isp_input(
        report_path, raw_path,
        source_lens_sensor_job_id=spec["source_lens_sensor_job_id"],
        sensor_db_root=Path(service.workspace) / "camerae2e_db",
        profile=isp_v2.default_profile(),
    )
    if checked["source_binding"] != source["source_binding"]:
        raise IntegrityError("Calibrated ISP source differs from the completed Lens RAW job.")
    report = json.loads(report_path.read_bytes())
    binding = calibration_binding(report, checked)
    reusable = reusable_calibration_binding(report, checked)
    return source, checked, binding, reusable


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    source, checked, binding, reusable = _source_input(service, spec)
    sha = spec["calibration_sha256"]
    calibration_path = service.registry.blob_path(sha)
    record = _read_calibration(calibration_path, sha)
    calibration_binding_for_schema = (
        reusable if record["schema"] == REUSABLE_CALIBRATION_SCHEMA else binding
    )
    validated = _validate_calibration(
        record, expected_binding=calibration_binding_for_schema,
        target_source_binding=checked["source_binding"],
    )
    if (
        spec["profile"]["raw_denoise"]["mode"] != "bypass"
        and spec["profile"]["raw_denoise"]["strength"] > 0
        and binding["capture"]["noise_flag"] != 2
    ):
        raise ValueError("Active noise calibration needs a stochastic source RAW capture.")
    isp_v2.validate_inputs(
        checked["raw"], checked["raw_format"], spec["profile"],
        geometry=checked["geometry"], calibration=validated["payload"],
    )
    frozen = {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        **source,
        "source_binding": binding,
        "native_cfa_contract": checked["native_cfa_contract"],
        "raw_content_sha256": checked["raw_content_sha256"],
        "calibration_artifact": {
            "id": sha,
            "name": "lens-sensor-isp-calibration.json",
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
    if record["schema"] == REUSABLE_CALIBRATION_SCHEMA:
        frozen["calibration_binding"] = reusable
        frozen["calibration_schema"] = REUSABLE_CALIBRATION_SCHEMA
    return frozen


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens calibrated ISP job is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen Lens RAW, calibration, ISP graph or source changed.")
    spec = frozen["specification"]
    with tempfile.TemporaryDirectory(prefix="camera-lens-calibrated-isp-") as temporary:
        report = run_lens_sensor_calibrated_isp(
            service.registry.blob_path(frozen["source_artifacts"]["json"]["sha256"]),
            service.registry.blob_path(frozen["source_artifacts"]["raw"]["sha256"]),
            source_lens_sensor_job_id=spec["source_lens_sensor_job_id"],
            sensor_db_root=Path(service.workspace) / "camerae2e_db",
            calibration_path=service.registry.blob_path(spec["calibration_sha256"]),
            calibration_sha256=spec["calibration_sha256"],
            profile=spec["profile"], output_dir=Path(temporary) / "replay",
        )
        if (
            canonical(report["source_binding"]) != canonical(frozen["source_binding"])
            or report["native_cfa_contract"] != frozen["native_cfa_contract"]
            or report["raw_content_sha256"] != frozen["raw_content_sha256"]
            or report["calibration"]["file_sha256"] != spec["calibration_sha256"]
            or report["calibration"]["numeric_sha256"]
            != frozen["calibration_numeric_sha256"]
            or report["calibration"]["schema"]
            != frozen.get("calibration_schema", CALIBRATION_SCHEMA)
            or report["calibration"]["applicability_binding"]
            != frozen.get("calibration_binding", frozen["source_binding"])
        ):
            raise IntegrityError("Calibrated Lens ISP output lost its frozen input contract.")
        if canonical(prepare(service, spec)) != canonical(frozen):
            raise IntegrityError("Lens source or calibration changed during ISP replay.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Lens calibrated ISP job was cancelled during execution.")
        artifacts = []
        for kind, media in (("json", "application/json"), ("npz", "application/octet-stream")):
            content = Path(report["artifacts"][kind]).read_bytes()
            if hashlib.sha256(content).hexdigest() != report["artifact_sha256"][kind]:
                raise IntegrityError(f"Lens calibrated ISP {kind} evidence changed.")
            artifacts.append(service.registry.store_blob(
                content, f"lens-sensor-calibrated-isp.{kind}", media
            ))
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_capture_result_sha256": frozen["source_capture_result_sha256"],
        "source_binding": frozen["source_binding"],
        "native_cfa_contract": frozen["native_cfa_contract"],
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
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitation": LIMITATION,
    }
    if "calibration_binding" in frozen:
        summary["calibration_binding"] = frozen["calibration_binding"]
    artifacts.append(service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-calibrated-isp-result.json", "application/json"
    ))
    return {"summary": summary, "artifacts": artifacts}


def get(service: Any, identity: str) -> dict[str, Any]:
    from .retained_raw_dataset_job import _lens_source

    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens calibrated ISP replay.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        spec = job["specification"]
        source = _lens_source(service, {"source_job_id": spec["source_lens_sensor_job_id"]})
        for key in ("source_capture_result_sha256", "source_artifacts"):
            if canonical(source[key]) != canonical(job[key]):
                raise IntegrityError(f"Calibrated Lens ISP frozen {key} changed.")
        binding = job["source_binding"]
        source_binding = binding["source_binding"]
        if (
            source_binding != source["source_binding"]
            or source_binding["source_report_sha256"]
            != source["source_artifacts"]["json"]["sha256"]
            or source_binding["source_raw_artifact_sha256"]
            != source["source_artifacts"]["raw"]["sha256"]
        ):
            raise IntegrityError("Calibrated Lens ISP source differs from frozen artifacts.")
        calibration_sha = spec["calibration_sha256"]
        calibration_path = service.registry.blob_path(calibration_sha)
        record = validate_lens_calibration_bytes(calibration_path.read_bytes(), calibration_sha)
        expected_calibration_binding = (
            job["calibration_binding"]
            if record["schema"] == REUSABLE_CALIBRATION_SCHEMA else binding
        )
        if record["schema"] == REUSABLE_CALIBRATION_SCHEMA:
            _, checked, current_binding, current_reusable = _source_input(service, spec)
            if current_binding != binding or current_reusable != expected_calibration_binding:
                raise IntegrityError("Reusable Lens calibration applicability changed.")
            _validate_calibration(
                record, expected_binding=current_reusable,
                target_source_binding=checked["source_binding"],
            )
        if (
            record["binding"] != expected_calibration_binding
            or record["schema"] != job.get("calibration_schema", CALIBRATION_SCHEMA)
            or record["provenance"] != job["calibration_provenance"]
            or digest(record["payload"]) != job["calibration_payload_sha256"]
            or job["calibration_artifact"]["sha256"] != calibration_sha
            or job["calibration_artifact"]["bytes"] != calibration_path.stat().st_size
            or spec["profile"]["calibration_id"] != f"sha256:{calibration_sha}"
        ):
            raise IntegrityError("Calibrated Lens ISP frozen calibration differs.")
        if (
            canonical(summary["specification"]) != canonical(spec)
            or canonical(summary["source_binding"]) != canonical(binding)
            or summary["source_capture_result_sha256"] != job["source_capture_result_sha256"]
            or summary["raw_content_sha256"] != job["raw_content_sha256"]
            or summary["native_cfa_contract"] != job["native_cfa_contract"]
            or summary["calibration_artifact"] != job["calibration_artifact"]
            or summary["calibration_payload_sha256"] != job["calibration_payload_sha256"]
            or summary["calibration"]["numeric_sha256"] != job["calibration_numeric_sha256"]
            or summary["calibration"]["schema"]
            != job.get("calibration_schema", CALIBRATION_SCHEMA)
            or summary["calibration"].get("applicability_binding", binding)
            != expected_calibration_binding
            or (
                record["schema"] == REUSABLE_CALIBRATION_SCHEMA
                and summary.get("calibration_binding") != expected_calibration_binding
            )
        ):
            raise IntegrityError("Calibrated Lens ISP result differs from frozen request.")
        if job["status"] == "succeeded":
            if len(job["artifacts"]) != 3 or any(
                summary["evidence"][kind] not in job["artifacts"] for kind in ("json", "npz")
            ):
                raise IntegrityError("Calibrated Lens ISP evidence links are missing.")
            for descriptor in job["artifacts"]:
                service.registry.blob_path(descriptor["sha256"])
            source_json = json.loads(service.registry.blob_path(
                summary["evidence"]["json"]["sha256"]
            ).read_text())
            if (
                source_json["artifacts"]["npz_sha256"] != summary["evidence"]["npz"]["sha256"]
                or canonical(source_json["source_binding"]) != canonical(binding)
                or source_json["calibration"]["file_sha256"] != calibration_sha
                or source_json["raw_content_sha256"] != job["raw_content_sha256"]
                or source_json["raw_values_byte_equal"] is not True
                or any(canonical(source_json[key]) != canonical(summary[key]) for key in (
                    "calibration", "before_after", "reference_isp", "candidate_isp"
                ))
            ):
                raise IntegrityError("Calibrated Lens ISP evidence JSON differs.")
            source_raw = service.registry.blob_path(source["source_artifacts"]["raw"]["sha256"])
            result_npz = service.registry.blob_path(summary["evidence"]["npz"]["sha256"])
            with source_raw.open("rb") as stream:
                original = np.load(stream, allow_pickle=False)
            with np.load(result_npz, allow_pickle=False) as output:
                retained = np.asarray(output["source_raw_dn_frame"])
            if (
                original.dtype != retained.dtype or original.shape != retained.shape
                or original.tobytes(order="C") != retained.tobytes(order="C")
                or _sha256_array(original) != job["raw_content_sha256"]
            ):
                raise IntegrityError("Calibrated Lens ISP NPZ lost exact source RAW.")
            result = next((item for item in job["artifacts"] if item["name"] == (
                "lens-sensor-calibrated-isp-result.json"
            )), None)
            if result is None or service.registry.blob_path(result["sha256"]).read_bytes() != (
                canonical(summary).encode()
            ):
                raise IntegrityError("Calibrated Lens ISP result artifact differs.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def import_calibration(service: Any, content: bytes, expected_sha256: str) -> dict[str, Any]:
    document = validate_lens_calibration_bytes(content, expected_sha256)
    descriptor = service.registry.store_blob(
        content, "lens-sensor-isp-calibration.json", "application/json"
    )
    if descriptor["sha256"] != expected_sha256:
        raise IntegrityError("Stored Lens calibration differs from submitted hash.")
    return {
        "schema": document["schema"],
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
    @app.get("/api/v1/lens-sensor-calibrated-isp/template", tags=[JOB_KIND])
    def template(source_lens_sensor_job_id: str = ""):
        sources = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") == "lens_sensor_raw" and item["status"] == "succeeded"
        ]
        spec = default_spec()
        expected_binding = None
        if source_lens_sensor_job_id:
            spec["source_lens_sensor_job_id"] = source_lens_sensor_job_id
            _, _, expected_binding, reusable_binding = _source_input(service, spec)
        else:
            reusable_binding = None
        return {
            "specification": spec,
            "source_lens_sensor_jobs": sources,
            "expected_calibration_binding": expected_binding,
            "expected_reusable_calibration_binding": reusable_binding,
            "calibration_document_template": (
                None if expected_binding is None else {
                    "schema": CALIBRATION_SCHEMA, "provenance": None,
                    "binding": expected_binding, "payload": None,
                }
            ),
            "reusable_calibration_document_template": (
                None if reusable_binding is None else {
                    "schema": REUSABLE_CALIBRATION_SCHEMA, "provenance": None,
                    "binding": reusable_binding, "payload": None,
                }
            ),
            "calibration_import": {
                "path": "/api/v1/lens-sensor-calibrated-isp/calibrations/import",
                "media_type": "multipart/form-data",
                "fields": ["calibration", "sha256"],
                "max_json_bytes": MAX_CALIBRATION_BYTES,
            },
            "limitation": LIMITATION,
        }

    @app.post(
        "/api/v1/lens-sensor-calibrated-isp/calibrations/import",
        status_code=201, tags=[JOB_KIND],
    )
    async def import_json(
        calibration: Annotated[UploadFile, File()], sha256: Annotated[str, Form()]
    ):
        if calibration.content_type not in {"application/json", "application/octet-stream"}:
            raise ValueError("Calibration upload must be JSON bytes.")
        return import_calibration(
            service, await calibration.read(MAX_CALIBRATION_BYTES + 1), sha256
        )

    @app.post("/api/v1/lens-sensor-calibrated-isp", status_code=202, tags=[JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-calibrated-isp", tags=[JOB_KIND])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-calibrated-isp/{identity}", tags=[JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-calibrated-isp/{identity}/cancel", tags=[JOB_KIND])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-calibrated-isp/{identity}/export", tags=[JOB_KIND])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ConflictError("Only a completed Lens calibrated ISP job can be exported.")
        return Response(
            canonical(job), media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="lens-sensor-calibrated-isp.json"'
            },
        )
