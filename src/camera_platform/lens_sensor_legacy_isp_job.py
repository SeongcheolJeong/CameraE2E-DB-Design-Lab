"""Durable, source-bound CameraE2E spectral ISP study on retained Lens RAW."""

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

from pyisetcam.dataset import _sha256_array

from .lens_sensor_legacy_isp import (
    METHODS,
    preflight_lens_sensor_legacy_isp,
    run_lens_sensor_legacy_isp,
)
from .lens_sensor_legacy_isp import (
    SCHEMA as REPORT_SCHEMA,
)
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_legacy_isp_study_v1"
JOB_KIND = "lens_sensor_legacy_isp"
LIMITATION = (
    "Reprocesses the exact saved 2x2 RGGB Lens x Sensor RAW with CameraE2E's "
    "public ip_compute, saved sensor EQE, D65 Macbeth XYZ fit and display model. "
    "Only saved D65 scenes and explicit bilinear, Laplacian or Adaptive Laplacian "
    "demosaic are accepted. "
    "Saved lens PSF is geometric without diffraction or validated radiometry/CRA-pixel "
    "coupling; sensor EQE and readout are model estimates/assumptions. This is a "
    "model-derived display diagnostic, not measured color accuracy, camera ranking, "
    "general advanced ISP, HDR or a validated product result."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 저장 RAW CameraE2E 색 ISP 진단",
        "source_lens_sensor_job_id": "",
        "demosaic_method": "bilinear",
        "acknowledge_model_derived_color_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the Lens x Sensor CameraE2E ISP fields.")
    if (
        payload["schema"] != SCHEMA
        or not isinstance(payload["demosaic_method"], str)
        or payload["demosaic_method"] not in METHODS
    ):
        raise ValueError("Unsupported Lens x Sensor CameraE2E ISP schema or demosaic method.")
    if payload["acknowledge_model_derived_color_limits"] is not True:
        raise ValueError("Explicit model-derived color-limit acknowledgement required.")
    name, identity = payload["name"], payload["source_lens_sensor_job_id"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("CameraE2E ISP name requires 1..160 characters.")
    if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 128:
        raise ValueError("Select one completed source_lens_sensor_job_id.")
    selected = deepcopy(payload)
    selected["name"] = name.strip()
    selected["source_lens_sensor_job_id"] = identity.strip()
    return selected


def _source_input(service: Any, spec: dict[str, Any]) -> tuple[dict, dict]:
    from .retained_raw_dataset_job import _lens_source

    source = _lens_source(service, {"source_job_id": spec["source_lens_sensor_job_id"]})
    report_path = service.registry.blob_path(source["source_artifacts"]["json"]["sha256"])
    raw_path = service.registry.blob_path(source["source_artifacts"]["raw"]["sha256"])
    ready = preflight_lens_sensor_legacy_isp(
        report_path, raw_path,
        source_lens_sensor_job_id=spec["source_lens_sensor_job_id"],
        sensor_db_root=Path(service.workspace) / "camerae2e_db",
        workspace=service.workspace,
        demosaic_method=spec["demosaic_method"],
    )
    if ready["checked"]["source_binding"] != source["source_binding"]:
        raise IntegrityError("CameraE2E ISP source differs from completed Lens RAW job.")
    return source, ready


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    source, ready = _source_input(service, spec)
    checked = ready["checked"]
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
        "source_scene": ready["scene"],
        "sensor_db_revision": ready["sensor_db_entry"],
        "sensor_model_snapshot_sha256": digest(ready["model"]),
        "offline_assets": ready["offline_assets"],
        "method_binding": ready["method_binding"],
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("CameraE2E ISP job is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen Lens RAW, sensor snapshot or CameraE2E ISP changed.")
    spec = frozen["specification"]
    with tempfile.TemporaryDirectory(prefix="camera-lens-legacy-isp-") as temp:
        report = run_lens_sensor_legacy_isp(
            service.registry.blob_path(frozen["source_artifacts"]["json"]["sha256"]),
            service.registry.blob_path(frozen["source_artifacts"]["raw"]["sha256"]),
            source_lens_sensor_job_id=spec["source_lens_sensor_job_id"],
            sensor_db_root=Path(service.workspace) / "camerae2e_db",
            workspace=service.workspace,
            demosaic_method=spec["demosaic_method"],
            output_dir=Path(temp) / "replay",
        )
        if (
            report["source_binding"] != frozen["source_binding"]
            or report["native_cfa_contract"] != frozen["native_cfa_contract"]
            or report["native_cfa"] != frozen["native_cfa"]
            or report["readout"] != frozen["readout"]
            or report["raw_content_sha256"] != frozen["raw_content_sha256"]
            or report["scene"] != frozen["source_scene"]
            or report["sensor_model_snapshot_sha256"]
            != frozen["sensor_model_snapshot_sha256"]
            or report["sensor_db_revision"] != frozen["sensor_db_revision"]
            or report["offline_assets"] != frozen["offline_assets"]
            or report["method_binding"] != frozen["method_binding"]
            or report["raw_values_byte_equal"] is not True
        ):
            raise IntegrityError("CameraE2E ISP output lost its frozen source contract.")
        if canonical(prepare(service, spec)) != canonical(frozen):
            raise IntegrityError(
                "Lens RAW, sensor model, ISP code or assets changed during replay."
            )
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("CameraE2E ISP job was cancelled during execution.")
        paths = report["artifacts"]
        bytes_by_kind = {kind: Path(paths[kind]).read_bytes() for kind in ("json", "npz")}
        if any(
            hashlib.sha256(content).hexdigest() != report["artifact_sha256"][kind]
            for kind, content in bytes_by_kind.items()
        ):
            raise IntegrityError("CameraE2E ISP evidence bytes changed before storage.")
        stored_report = json.loads(bytes_by_kind["json"])
        if (
            stored_report["artifacts"]["npz_sha256"] != report["artifact_sha256"]["npz"]
            or stored_report["source_binding"] != frozen["source_binding"]
        ):
            raise IntegrityError("CameraE2E ISP JSON differs from numeric evidence.")
        evidence = {
            kind: service.registry.store_blob(
                bytes_by_kind[kind], f"lens-sensor-legacy-isp.{kind}",
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
        "source_scene": frozen["source_scene"],
        "sensor_db_revision": frozen["sensor_db_revision"],
        "sensor_model_snapshot_sha256": frozen["sensor_model_snapshot_sha256"],
        "offline_assets": frozen["offline_assets"],
        "method_binding": frozen["method_binding"],
        "report": stored_report,
        "evidence": evidence,
        "limitation": LIMITATION,
    }
    result_artifact = service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-legacy-isp-result.json", "application/json",
    )
    return {"summary": summary, "artifacts": [*evidence.values(), result_artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    from .retained_raw_dataset_job import _lens_source

    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens x Sensor CameraE2E ISP study.")
    summary = (job.get("result") or {}).get("summary")
    if summary is None:
        return job
    source = _lens_source(
        service, {"source_job_id": job["specification"]["source_lens_sensor_job_id"]}
    )
    for key in (
        "specification", "source_capture_result_sha256", "source_binding",
        "source_artifacts", "native_cfa_contract", "native_cfa", "readout",
        "raw_content_sha256", "source_scene", "sensor_db_revision",
        "sensor_model_snapshot_sha256",
        "offline_assets", "method_binding",
    ):
        if canonical(summary[key]) != canonical(job[key]):
            raise IntegrityError(f"CameraE2E ISP result differs from frozen {key}.")
    for key in ("source_capture_result_sha256", "source_binding", "source_artifacts"):
        if canonical(source[key]) != canonical(job[key]):
            raise IntegrityError(f"CameraE2E ISP source {key} changed.")
    report = summary["report"]
    if (
        summary["schema"] != SCHEMA
        or report["schema"] != REPORT_SCHEMA
        or report["source_binding"] != job["source_binding"]
        or report["native_cfa_contract"] != job["native_cfa_contract"]
        or report["native_cfa"] != job["native_cfa"]
        or report["readout"] != job["readout"]
        or report["raw_content_sha256"] != job["raw_content_sha256"]
        or report["scene"] != job["source_scene"]
        or report["sensor_model_snapshot_sha256"]
        != job["sensor_model_snapshot_sha256"]
        or report["sensor_db_revision"] != job["sensor_db_revision"]
        or report["offline_assets"] != job["offline_assets"]
        or report["method_binding"] != job["method_binding"]
        or report["raw_values_byte_equal"] is not True
        or report["color_contract"]["measured_color_accuracy"] is not False
    ):
        raise IntegrityError("Saved CameraE2E ISP method or source evidence is missing.")
    if job["status"] != "succeeded":
        return job
    if len(job["artifacts"]) != 3 or any(
        summary["evidence"][kind] not in job["artifacts"] for kind in ("json", "npz")
    ):
        raise IntegrityError("Saved CameraE2E ISP evidence links are missing.")
    result = [item for item in job["artifacts"] if item["name"] == (
        "lens-sensor-legacy-isp-result.json"
    )]
    if len(result) != 1:
        raise IntegrityError("CameraE2E ISP result artifact is missing.")
    if service.registry.blob_path(result[0]["sha256"]).read_bytes() != canonical(summary).encode():
        raise IntegrityError("CameraE2E ISP result artifact differs.")
    json_bytes = service.registry.blob_path(summary["evidence"]["json"]["sha256"]).read_bytes()
    npz_bytes = service.registry.blob_path(summary["evidence"]["npz"]["sha256"]).read_bytes()
    if (
        json.loads(json_bytes) != report
        or report["artifacts"]["npz_sha256"] != summary["evidence"]["npz"]["sha256"]
    ):
        raise IntegrityError("CameraE2E ISP report differs from saved NPZ.")
    source_path = service.registry.blob_path(source["source_artifacts"]["raw"]["sha256"])
    with source_path.open("rb") as stream:
        original = np.load(stream, allow_pickle=False)
    with np.load(io.BytesIO(npz_bytes), allow_pickle=False) as arrays:
        expected_names = {
            "source_raw_dn_frame", "normalized_sensor_dn", "sensor_rgb",
            "model_derived_xyz", "display_rgb", "srgb",
        }
        if set(arrays.files) != expected_names or set(report["arrays"]) != expected_names:
            raise IntegrityError("CameraE2E ISP NPZ stage set changed.")
        retained = np.asarray(arrays["source_raw_dn_frame"])
        if (
            original.dtype != np.dtype("int32")
            or retained.dtype != original.dtype or retained.shape != original.shape
            or retained.tobytes(order="C") != original.tobytes(order="C")
            or _sha256_array(retained) != job["raw_content_sha256"]
        ):
            raise IntegrityError("CameraE2E ISP NPZ lost its exact RAW evidence.")
        for key in ("sensor_rgb", "model_derived_xyz", "display_rgb", "srgb"):
            value = np.asarray(arrays[key])
            if (
                value.dtype != np.dtype("float64")
                or value.shape != (*retained.shape, 3)
                or not np.isfinite(value).all()
            ):
                raise IntegrityError(f"CameraE2E ISP NPZ invalid {key} stage.")
        normalized = np.asarray(arrays["normalized_sensor_dn"])
        expected = (
            retained.astype(np.float64) - job["readout"]["black_level_dn"]
        ) / (2 ** job["readout"]["bit_depth"] - 1 - job["readout"]["black_level_dn"])
        if normalized.dtype != np.dtype("float64") or not np.array_equal(
            normalized, expected
        ):
            raise IntegrityError("CameraE2E ISP NPZ did not normalize source RAW once.")
        for key in expected_names:
            value = np.asarray(arrays[key])
            if report["arrays"][key] != {
                "shape": list(value.shape), "dtype": str(value.dtype)
            }:
                raise IntegrityError(f"CameraE2E ISP NPZ {key} descriptor changed.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    @app.get("/api/v1/lens-sensor-legacy-isp/template", tags=[JOB_KIND])
    def template(source_lens_sensor_job_id: str = ""):
        sources = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") == "lens_sensor_raw" and item["status"] == "succeeded"
        ]
        spec = default_spec()
        contract = None
        if source_lens_sensor_job_id:
            spec["source_lens_sensor_job_id"] = source_lens_sensor_job_id
            source, ready = _source_input(service, spec)
            contract = {
                "source_binding": source["source_binding"],
                "native_cfa_contract": ready["checked"]["native_cfa_contract"],
                "raw_shape": source["source_raw_shape"],
                "source_scene": ready["scene"],
                "readout": ready["checked"]["readout"],
                "method_binding": ready["method_binding"],
            }
        return {
            "specification": spec,
            "source_lens_sensor_jobs": sources,
            "selected_source_contract": contract,
            "demosaic_methods": list(METHODS),
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/lens-sensor-legacy-isp", status_code=202, tags=[JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-legacy-isp", tags=[JOB_KIND])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-legacy-isp/{identity}", tags=[JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-legacy-isp/{identity}/cancel", tags=[JOB_KIND])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-legacy-isp/{identity}/export", tags=[JOB_KIND])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ValueError("CameraE2E ISP result is not ready.")
        return Response(
            canonical(job), media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="lens-sensor-legacy-isp.json"'
            },
        )
