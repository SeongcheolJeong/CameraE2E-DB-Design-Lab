"""Durable adaptive-demosaic diagnostic on one pinned Lens x Sensor RAW."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.responses import Response

from pyisetcam import ip as legacy_ip
from pyisetcam.dataset import _sha256_array

from .lens_sensor_adaptive_demosaic import (
    METHOD,
    _file_sha256,
    run_lens_sensor_adaptive_demosaic,
)
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_adaptive_demosaic_study_v1"
JOB_KIND = "lens_sensor_adaptive_demosaic"
METHOD_ID = "adaptive_laplacian_v1"
LIMITATION = (
    "Reprocesses one exact saved 2x2 RGGB Lens x Sensor integer RAW through "
    "pyisetcam's Adaptive Laplacian demosaic. The saved readout supplies black level; "
    "LSC, denoise, white balance and CCM calibration are not inferred. A four-pixel "
    "border and invalid-code neighborhoods are excluded from comparison. Lens "
    "radiometry and readout accuracy remain unvalidated; output differences are "
    "diagnostic, not measured image-quality improvement or product ranking."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 저장 RAW Adaptive Laplacian demosaic",
        "source_lens_sensor_job_id": "",
        "method": METHOD_ID,
        "acknowledge_simulation_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the Lens x Sensor adaptive-demosaic fields.")
    if payload["schema"] != SCHEMA or payload["method"] != METHOD_ID:
        raise ValueError("Unsupported Lens x Sensor adaptive-demosaic schema or method.")
    if payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit adaptive-demosaic simulation-limit acknowledgement required.")
    name, identity = payload["name"], payload["source_lens_sensor_job_id"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Adaptive-demosaic name requires 1..160 characters.")
    if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 128:
        raise ValueError("Select one completed source_lens_sensor_job_id.")
    selected = deepcopy(payload)
    selected["name"] = name.strip()
    selected["source_lens_sensor_job_id"] = identity.strip()
    return selected


def _source_input(service: Any, identity: str) -> tuple[dict, dict]:
    from .isp_v2 import default_profile
    from .retained_raw_dataset_job import _lens_source
    from .retained_raw_isp_bridge import inspect_lens_sensor_isp_input

    source = _lens_source(service, {"source_job_id": identity})
    report_path = service.registry.blob_path(source["source_artifacts"]["json"]["sha256"])
    raw_path = service.registry.blob_path(source["source_artifacts"]["raw"]["sha256"])
    checked = inspect_lens_sensor_isp_input(
        report_path,
        raw_path,
        source_lens_sensor_job_id=identity,
        sensor_db_root=Path(service.workspace) / "camerae2e_db",
        profile=default_profile(),
    )
    if checked["source_binding"] != source["source_binding"]:
        raise IntegrityError("Adaptive ISP source differs from the completed Lens RAW job.")
    if checked["native_cfa_contract"] != "three_channel_rggb_2x2":
        raise ValueError("Adaptive Laplacian requires saved native three-channel 2x2 RGGB.")
    rows, columns = checked["raw"].shape
    if rows <= 8 or columns <= 8:
        raise ValueError("Adaptive comparison requires more than 8 pixels on each RAW axis.")
    return source, checked


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    source, checked = _source_input(service, spec["source_lens_sensor_job_id"])
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        **source,
        "native_cfa_contract": checked["native_cfa_contract"],
        "native_cfa": checked["native_cfa"],
        "readout": checked["readout"],
        "raw_content_sha256": checked["raw_content_sha256"],
        "method_binding": {
            "id": METHOD,
            "source_sha256": _file_sha256(legacy_ip.__file__),
        },
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Adaptive-demosaic job is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen Lens RAW, CFA, readout or demosaic executable changed.")
    spec = frozen["specification"]
    with tempfile.TemporaryDirectory(prefix="camera-lens-adaptive-demosaic-") as temp:
        report = run_lens_sensor_adaptive_demosaic(
            service.registry.blob_path(frozen["source_artifacts"]["json"]["sha256"]),
            service.registry.blob_path(frozen["source_artifacts"]["raw"]["sha256"]),
            source_lens_sensor_job_id=spec["source_lens_sensor_job_id"],
            sensor_db_root=Path(service.workspace) / "camerae2e_db",
            output_dir=Path(temp) / "replay",
        )
        if (
            report["source_binding"] != frozen["source_binding"]
            or report["native_cfa_contract"] != frozen["native_cfa_contract"]
            or report["native_cfa"] != frozen["native_cfa"]
            or report["readout"] != frozen["readout"]
            or report["raw_content_sha256"] != frozen["raw_content_sha256"]
            or report["method"]["id"] != frozen["method_binding"]["id"]
            or report["method"]["pyisetcam_ip_source_sha256"]
            != frozen["method_binding"]["source_sha256"]
            or report["raw_values_byte_equal"] is not True
        ):
            raise IntegrityError("Adaptive-demosaic output lost its frozen source contract.")
        if canonical(prepare(service, spec)) != canonical(frozen):
            raise IntegrityError("Lens RAW source or demosaic executable changed during replay.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Adaptive-demosaic job was cancelled during execution.")
        paths = report["artifacts"]
        bytes_by_kind = {kind: Path(paths[kind]).read_bytes() for kind in ("json", "npz")}
        if any(
            hashlib.sha256(content).hexdigest() != report["artifact_sha256"][kind]
            for kind, content in bytes_by_kind.items()
        ):
            raise IntegrityError("Adaptive-demosaic evidence bytes changed before storage.")
        stored_report = json.loads(bytes_by_kind["json"])
        if (
            stored_report["artifacts"]["npz_sha256"]
            != report["artifact_sha256"]["npz"]
            or stored_report["source_binding"] != frozen["source_binding"]
        ):
            raise IntegrityError("Adaptive-demosaic JSON differs from numeric evidence.")
        evidence = {
            kind: service.registry.store_blob(
                bytes_by_kind[kind], f"lens-sensor-adaptive-demosaic.{kind}",
                "application/json" if kind == "json" else "application/octet-stream",
            )
            for kind in ("json", "npz")
        }
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_capture_result_sha256": frozen["source_capture_result_sha256"],
        "source_binding": frozen["source_binding"],
        "source_artifacts": frozen["source_artifacts"],
        "native_cfa_contract": frozen["native_cfa_contract"],
        "native_cfa": frozen["native_cfa"],
        "readout": frozen["readout"],
        "raw_content_sha256": frozen["raw_content_sha256"],
        "method_binding": frozen["method_binding"],
        "report": stored_report,
        "evidence": evidence,
        "limitation": LIMITATION,
    }
    result_artifact = service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-adaptive-demosaic-result.json",
        "application/json",
    )
    return {"summary": summary, "artifacts": [*evidence.values(), result_artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    from .retained_raw_dataset_job import _lens_source

    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens x Sensor adaptive-demosaic study.")
    summary = (job.get("result") or {}).get("summary")
    if summary is None:
        return job
    source = _lens_source(
        service, {"source_job_id": job["specification"]["source_lens_sensor_job_id"]}
    )
    for key in (
        "specification", "source_capture_result_sha256", "source_binding",
        "source_artifacts", "native_cfa_contract", "native_cfa", "readout",
        "raw_content_sha256", "method_binding",
    ):
        if canonical(summary[key]) != canonical(job[key]):
            raise IntegrityError(f"Adaptive-demosaic result differs from frozen {key}.")
    for key in ("source_capture_result_sha256", "source_binding", "source_artifacts"):
        if canonical(source[key]) != canonical(job[key]):
            raise IntegrityError(f"Adaptive-demosaic source {key} changed.")
    report = summary["report"]
    if (
        summary["schema"] != SCHEMA
        or report["schema"] != "camerae2e_lens_sensor_adaptive_demosaic_v1"
        or report["source_binding"] != job["source_binding"]
        or report["native_cfa_contract"] != job["native_cfa_contract"]
        or report["native_cfa"] != job["native_cfa"]
        or report["readout"] != job["readout"]
        or report["raw_content_sha256"] != job["raw_content_sha256"]
        or report["method"]["id"] != job["method_binding"]["id"]
        or report["method"]["pyisetcam_ip_source_sha256"]
        != job["method_binding"]["source_sha256"]
        or report["raw_values_byte_equal"] is not True
    ):
        raise IntegrityError("Saved adaptive-demosaic method or source evidence is missing.")
    if job["status"] != "succeeded":
        return job
    if len(job["artifacts"]) != 3 or any(
        summary["evidence"][kind] not in job["artifacts"] for kind in ("json", "npz")
    ):
        raise IntegrityError("Saved adaptive-demosaic evidence links are missing.")
    result = [item for item in job["artifacts"] if item["name"] == (
        "lens-sensor-adaptive-demosaic-result.json"
    )]
    if len(result) != 1:
        raise IntegrityError("Adaptive-demosaic result artifact is missing.")
    if service.registry.blob_path(result[0]["sha256"]).read_bytes() != canonical(summary).encode():
        raise IntegrityError("Adaptive-demosaic result artifact differs.")
    json_bytes = service.registry.blob_path(summary["evidence"]["json"]["sha256"]).read_bytes()
    npz_bytes = service.registry.blob_path(summary["evidence"]["npz"]["sha256"]).read_bytes()
    if (
        json.loads(json_bytes) != report
        or report["artifacts"]["npz_sha256"] != summary["evidence"]["npz"]["sha256"]
    ):
        raise IntegrityError("Adaptive-demosaic report differs from saved NPZ.")
    source_path = service.registry.blob_path(source["source_artifacts"]["raw"]["sha256"])
    with source_path.open("rb") as stream:
        original = np.load(stream, allow_pickle=False)
    with np.load(io.BytesIO(npz_bytes), allow_pickle=False) as arrays:
        retained = np.asarray(arrays["source_raw_dn_frame"])
        adaptive = np.asarray(arrays["adaptive_laplacian_sensor_rgb"])
        reference = np.asarray(arrays["reference_bilinear_sensor_rgb"])
        valid = np.asarray(arrays["comparison_valid_mask"])
    if (
        original.dtype != np.dtype("int32")
        or retained.dtype != original.dtype
        or retained.shape != original.shape
        or retained.tobytes(order="C") != original.tobytes(order="C")
        or _sha256_array(retained) != job["raw_content_sha256"]
        or adaptive.shape != (*retained.shape, 3)
        or reference.shape != adaptive.shape
        or valid.shape != retained.shape
        or valid.dtype != np.dtype("bool")
        or not np.isfinite(adaptive).all()
        or not np.isfinite(reference).all()
    ):
        raise IntegrityError("Adaptive-demosaic NPZ lost its exact RAW or RGB evidence.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    @app.get("/api/v1/lens-sensor-adaptive-demosaic/template", tags=[JOB_KIND])
    def template(source_lens_sensor_job_id: str = ""):
        sources = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") == "lens_sensor_raw" and item["status"] == "succeeded"
        ]
        spec = default_spec()
        source_contract = None
        if source_lens_sensor_job_id:
            spec["source_lens_sensor_job_id"] = source_lens_sensor_job_id
            source, checked = _source_input(service, source_lens_sensor_job_id)
            source_contract = {
                "source_binding": source["source_binding"],
                "native_cfa_contract": checked["native_cfa_contract"],
                "raw_shape": source["source_raw_shape"],
                "readout": checked["readout"],
                "method_binding": {
                    "id": METHOD, "source_sha256": _file_sha256(legacy_ip.__file__),
                },
            }
        return {
            "specification": spec,
            "source_lens_sensor_jobs": sources,
            "selected_source_contract": source_contract,
            "minimum_raw_size": [10, 10],
            "recommended_raw_size": [16, 16],
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/lens-sensor-adaptive-demosaic", status_code=202, tags=[JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-adaptive-demosaic", tags=[JOB_KIND])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-adaptive-demosaic/{identity}", tags=[JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-adaptive-demosaic/{identity}/cancel", tags=[JOB_KIND])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-adaptive-demosaic/{identity}/export", tags=[JOB_KIND])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ValueError("Adaptive-demosaic result is not ready.")
        return Response(
            canonical(job), media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="lens-sensor-adaptive-demosaic.json"'
            },
        )
