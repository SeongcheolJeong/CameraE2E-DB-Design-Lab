"""Durable, exact-RGB evaluation of supplied Lens × Sensor ISP detections."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import Response

from .lens_sensor_isp_perception import (
    ANNOTATION_SCHEMA,
    MODEL_SCHEMA,
    PREDICTION_SCHEMA,
    evaluate_lens_sensor_isp_predictions,
    lens_sensor_isp_image_binding,
)
from .registry import ConflictError, IntegrityError, canonical, digest
from .saved_sensor_perception import _finite_number, _identity

SCHEMA = "camerae2e_lens_sensor_isp_prediction_study_v1"
JOB_KIND = "lens_sensor_isp_perception"
_MAX_IMPORT_BODY_BYTES = 512 * 1024
_MAX_IMPORT_JSON_BYTES = 256 * 1024
LIMITATION = (
    "One exact 8–32 pixel simulated Lens x Sensor ISP frame with user-supplied "
    "annotations and predictions. Independent ground truth and detector origin "
    "are supplier declarations, not independently verified. This job does not "
    "run the model, compute dataset mAP, or establish product perception performance."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 ISP RGB 제공 예측 평가",
        "source_lens_sensor_job_id": "",
        "source_lens_sensor_isp_job_id": "",
        "annotation_artifact_sha256": "",
        "prediction_artifact_sha256": "",
        "model_manifest_sha256": "",
        "model_artifact_sha256": "",
        "iou_threshold": 0.5,
        "acknowledge_external_predictions": False,
    }


def _artifact_id(value: Any, key: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(letter not in "0123456789abcdef" for letter in value)
    ):
        raise ValueError(f"{key} requires a lowercase SHA-256 artifact ID.")
    return value


def specification(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the Lens ISP prediction study fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_external_predictions"] is not True:
        raise ValueError("Explicit external-prediction limitation acknowledgement required.")
    result = deepcopy(payload)
    for key in ("name", "source_lens_sensor_job_id", "source_lens_sensor_isp_job_id"):
        result[key] = _identity(payload[key], key)
    for key in (
        "annotation_artifact_sha256", "prediction_artifact_sha256",
        "model_manifest_sha256",
    ):
        result[key] = _artifact_id(payload[key], key)
    result["model_artifact_sha256"] = _artifact_id(
        payload["model_artifact_sha256"], "model_artifact_sha256", optional=True
    )
    threshold = _finite_number(payload["iou_threshold"], "iou_threshold")
    if not 0.1 <= threshold <= 0.95:
        raise ValueError("iou_threshold must be within [0.1, 0.95].")
    result["iou_threshold"] = threshold
    return result


def _source_job(service: Any, spec: dict[str, Any]) -> tuple[dict, dict, dict]:
    from .lens_sensor_calibrated_isp_job import get as calibrated_get
    from .lens_sensor_isp_replay_job import get as isp_get
    from .lens_sensor_raw_job import get as raw_get

    raw_job = raw_get(service, spec["source_lens_sensor_job_id"])
    selected = service.registry.get_run(spec["source_lens_sensor_isp_job_id"])
    kind = selected.get("job_kind")
    if kind == "lens_sensor_isp_replay":
        isp_job = isp_get(service, selected["id"])
    elif kind == "lens_sensor_calibrated_isp":
        isp_job = calibrated_get(service, selected["id"])
    else:
        raise ValueError("Selected ISP job is not a supported Lens RGB replay.")
    if raw_job["status"] != "succeeded" or isp_job["status"] != "succeeded":
        raise ValueError("Lens RAW and ISP source jobs must both have succeeded.")
    raw_by_name = {row["name"]: row for row in raw_job["artifacts"]}
    isp_by_name = {row["name"]: row for row in isp_job["artifacts"]}
    prefix = (
        "lens-sensor-isp-replay" if kind == "lens_sensor_isp_replay"
        else "lens-sensor-calibrated-isp"
    )
    paths = {
        "lens_raw_report_path": service.registry.blob_path(
            raw_by_name["lens-sensor-raw-preview-result.json"]["sha256"]
        ),
        "lens_raw_path": service.registry.blob_path(
            raw_by_name["lens-sensor-original-raw.npy"]["sha256"]
        ),
        "lens_isp_result_path": service.registry.blob_path(
            isp_by_name[f"{prefix}-result.json"]["sha256"]
        ),
        "lens_isp_report_path": service.registry.blob_path(
            isp_by_name[f"{prefix}.json"]["sha256"]
        ),
        "lens_isp_npz_path": service.registry.blob_path(
            isp_by_name[f"{prefix}.npz"]["sha256"]
        ),
    }
    return raw_job, isp_job, paths


def _inputs(service: Any, spec: dict[str, Any]) -> dict[str, Path]:
    paths = {
        "annotation_path": service.registry.blob_path(spec["annotation_artifact_sha256"]),
        "prediction_path": service.registry.blob_path(spec["prediction_artifact_sha256"]),
        "model_manifest_path": service.registry.blob_path(spec["model_manifest_sha256"]),
    }
    if spec["model_artifact_sha256"]:
        paths["model_artifact_path"] = service.registry.blob_path(
            spec["model_artifact_sha256"]
        )
    return paths


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    raw_job, isp_job, paths = _source_job(service, spec)
    evidence_paths = _inputs(service, spec)
    evaluation = evaluate_lens_sensor_isp_predictions(
        lens_raw_job=raw_job, lens_isp_job=isp_job,
        **paths, **evidence_paths, iou_threshold=spec["iou_threshold"],
    )
    binding = evaluation["image_binding"]
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source_identities": {
            "lens_raw_job_id": raw_job["id"],
            "lens_raw_result_sha256": digest(raw_job["result"]),
            "lens_isp_job_id": isp_job["id"],
            "lens_isp_result_sha256": digest(isp_job["result"]),
            "image_binding": binding,
            "annotation_artifact_sha256": spec["annotation_artifact_sha256"],
            "prediction_artifact_sha256": spec["prediction_artifact_sha256"],
            "model_manifest_sha256": spec["model_manifest_sha256"],
            "model_artifact_sha256": evaluation["model_provenance"]["checkpoint_sha256"],
            "model_artifact_bytes_registered": bool(spec["model_artifact_sha256"]),
        },
        "evaluation_sha256": digest(evaluation),
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens ISP prediction study is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen Lens ISP RGB, supplied evidence or executable changed.")
    spec = frozen["specification"]
    raw_job, isp_job, paths = _source_job(service, spec)
    evaluation = evaluate_lens_sensor_isp_predictions(
        lens_raw_job=raw_job, lens_isp_job=isp_job,
        **paths, **_inputs(service, spec), iou_threshold=spec["iou_threshold"],
    )
    if (
        digest(evaluation) != frozen["evaluation_sha256"]
        or evaluation["image_binding"] != frozen["source_identities"]["image_binding"]
        or canonical(prepare(service, spec)) != canonical(frozen)
    ):
        raise IntegrityError("Lens ISP source or prediction changed during evaluation.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens ISP prediction study was cancelled during evaluation.")
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_identities": frozen["source_identities"],
        "evaluation_sha256": frozen["evaluation_sha256"],
        "evaluation": evaluation,
        "verification": {
            "implementation_verification": "runtime_completed",
            "detector_inference": "not_performed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitation": LIMITATION,
    }
    artifact = service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-isp-prediction-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens ISP prediction study.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        identities = job["source_identities"]
        raw_job, isp_job, _ = _source_job(service, job["specification"])
        if (
            summary.get("specification") != job["specification"]
            or summary.get("source_identities") != identities
            or summary.get("evaluation_sha256") != job["evaluation_sha256"]
            or digest(summary.get("evaluation")) != job["evaluation_sha256"]
            or digest(raw_job["result"]) != identities["lens_raw_result_sha256"]
            or digest(isp_job["result"]) != identities["lens_isp_result_sha256"]
            or summary["evaluation"].get("image_binding") != identities["image_binding"]
        ):
            raise IntegrityError("Lens ISP prediction result differs from frozen evidence.")
        if job["status"] == "succeeded":
            if len(job["artifacts"]) != 1:
                raise IntegrityError("Lens ISP prediction result artifact is incomplete.")
            artifact = job["artifacts"][0]
            if (
                artifact["name"] != "lens-sensor-isp-prediction-result.json"
                or service.registry.blob_path(artifact["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Lens ISP prediction result artifact differs.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    @app.post(
        "/api/v1/lens-sensor-isp-perception/artifacts", status_code=201,
        tags=[JOB_KIND],
    )
    async def import_artifact(request: Request):
        blocks: list[bytes] = []
        total = 0
        async for block in request.stream():
            total += len(block)
            if total > _MAX_IMPORT_BODY_BYTES:
                raise HTTPException(413, "Prediction artifact request exceeds 512 KiB.")
            blocks.append(block)
        try:
            payload = json.loads(b"".join(blocks))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise HTTPException(422, "Prediction artifact request must be JSON.") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "kind", "document", "expected_sha256"
        }:
            raise HTTPException(422, "Declare kind, document and expected_sha256 exactly.")
        kinds = {
            "annotation": (ANNOTATION_SCHEMA, "lens-isp-annotations.json"),
            "prediction": (PREDICTION_SCHEMA, "lens-isp-predictions.json"),
            "model_manifest": (MODEL_SCHEMA, "lens-isp-model-manifest.json"),
        }
        if not isinstance(payload["kind"], str) or payload["kind"] not in kinds:
            raise HTTPException(422, "Only annotation, prediction and model JSON are accepted.")
        document = payload["document"]
        schema, name = kinds[payload["kind"]]
        if not isinstance(document, dict) or document.get("schema") != schema:
            raise HTTPException(422, "Prediction artifact schema differs from its kind.")
        try:
            encoded = canonical(document).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise HTTPException(422, "Prediction artifact is not canonical JSON.") from exc
        if len(encoded) > _MAX_IMPORT_JSON_BYTES:
            raise HTTPException(413, "Canonical prediction artifact exceeds 256 KiB.")
        if hashlib.sha256(encoded).hexdigest() != payload["expected_sha256"]:
            raise HTTPException(422, "Prediction artifact SHA differs from canonical bytes.")
        artifact = service.registry.store_blob(encoded, name, "application/json")
        return {
            "kind": payload["kind"], "schema": schema, "artifact": artifact,
            "canonicalization": "UTF-8 JSON, sorted keys, compact separators, finite numbers",
            "detector_inference_performed": False,
            "model_weight_import_supported": False,
        }

    @app.get("/api/v1/lens-sensor-isp-perception/template", tags=[JOB_KIND])
    def template(source_lens_sensor_job_id: str = "", source_lens_sensor_isp_job_id: str = ""):
        candidates = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") in {"lens_sensor_isp_replay", "lens_sensor_calibrated_isp"}
            and item["status"] == "succeeded"
        ]
        response: dict[str, Any] = {
            "specification": default_spec(), "source_lens_isp_jobs": candidates,
            "limitation": LIMITATION,
        }
        if source_lens_sensor_job_id and source_lens_sensor_isp_job_id:
            spec = {
                "source_lens_sensor_job_id": source_lens_sensor_job_id,
                "source_lens_sensor_isp_job_id": source_lens_sensor_isp_job_id,
            }
            raw_job, isp_job, paths = _source_job(service, spec)
            response["image_binding"] = lens_sensor_isp_image_binding(
                lens_raw_job=raw_job, lens_isp_job=isp_job, **paths
            )
        return response

    @app.post("/api/v1/lens-sensor-isp-perception", status_code=202, tags=[JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-isp-perception", tags=[JOB_KIND])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-isp-perception/{identity}", tags=[JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-isp-perception/{identity}/cancel", tags=[JOB_KIND])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-isp-perception/{identity}/export", tags=[JOB_KIND])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ConflictError("Only a completed Lens ISP prediction job can be exported.")
        return Response(
            canonical(job), media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="lens-isp-prediction-result.json"'
            },
        )
