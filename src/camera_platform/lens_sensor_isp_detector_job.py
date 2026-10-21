"""Durable offline YOLO inference on a saved Lens × Sensor ISP RGB artifact."""

from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from typing import Annotated, Any

from fastapi import HTTPException, Query, Response

from .lens_sensor_isp_detector import (
    _TRUST_ENV,
    PROFILE,
    _artifact_id,
    _metrics_for_annotations,
    _targets,
    infer_lens_sensor_isp_detector,
    local_model_preflight,
)
from .lens_sensor_isp_perception import lens_sensor_isp_image_binding
from .lens_sensor_isp_perception_job import _source_job
from .registry import ConflictError, IntegrityError, canonical, digest
from .saved_sensor_perception import _finite_number, _identity

SCHEMA = "camerae2e_lens_sensor_isp_detector_study_v1"
JOB_KIND = "lens_sensor_isp_detector"
RESULT_NAME = "lens-sensor-isp-detector-result.json"
LIMITATION = (
    "Runs locally cached YOLO11n weights on one exact 8–32 px simulated Lens x Sensor "
    "ISP sRGB frame. Operator trust in the exact local weight SHA must be declared "
    "before submission; the weight publishing origin is not verified. "
    "Ultralytics may internally resize/letterbox. Empty detections are real inference output, "
    "not a validity result. Metrics require separately supplied annotations and remain "
    "single-frame diagnostics, never mAP or product performance."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 ISP RGB 로컬 검출기 실행",
        "source_lens_sensor_job_id": "",
        "source_lens_sensor_isp_job_id": "",
        "model_profile": PROFILE,
        "model_weights_sha256": "",
        "target_labels": ["person"],
        "score_threshold": 0.25,
        "annotation_artifact_sha256": "",
        "iou_threshold": 0.5,
        "acknowledge_simulation_limits": False,
    }


def specification(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the Lens ISP detector study fields.")
    if payload["schema"] != SCHEMA or payload["model_profile"] != PROFILE:
        raise ValueError("Only the pinned local YOLO11n detection profile is supported.")
    if payload["acknowledge_simulation_limits"] is not True:
        raise ValueError(
            "Explicit tiny simulated-frame detector limitation acknowledgement required."
        )
    spec = deepcopy(payload)
    for key in ("name", "source_lens_sensor_job_id", "source_lens_sensor_isp_job_id"):
        spec[key] = _identity(payload[key], key)
    spec["model_weights_sha256"] = _artifact_id(
        payload["model_weights_sha256"], "model_weights_sha256"
    )
    annotation = payload["annotation_artifact_sha256"]
    spec["annotation_artifact_sha256"] = (
        "" if annotation == "" else _artifact_id(annotation, "annotation_artifact_sha256")
    )
    spec["target_labels"] = _targets(payload["target_labels"])
    score = _finite_number(payload["score_threshold"], "score_threshold")
    iou = _finite_number(payload["iou_threshold"], "iou_threshold")
    if not 0.01 <= score <= 0.95 or not 0.1 <= iou <= 0.95:
        raise ValueError("Detector score or IoU threshold is outside its bounded range.")
    spec["score_threshold"], spec["iou_threshold"] = score, iou
    return spec


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    raw_job, isp_job, paths = _source_job(service, spec)
    binding = lens_sensor_isp_image_binding(
        lens_raw_job=raw_job, lens_isp_job=isp_job, **paths
    )
    if binding["color_encoding"] != "srgb":
        raise ValueError("YOLO11n requires sRGB ISP output; linear RGB is unsupported.")
    model = local_model_preflight(spec["model_weights_sha256"])
    if spec["annotation_artifact_sha256"]:
        path = service.registry.blob_path(spec["annotation_artifact_sha256"])
        _metrics_for_annotations(
            path, binding=binding, detections=[],
            target_labels=spec["target_labels"],
            model_sha256=spec["model_weights_sha256"],
            iou_threshold=spec["iou_threshold"],
        )
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
            "model_weights_sha256": model["weights_sha256"],
            "offline_worker_sha256": model["worker_sha256"],
        },
        "model_preflight": model,
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens ISP detector study is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen Lens ISP frame, model weights or executable changed.")
    spec = frozen["specification"]
    raw_job, isp_job, paths = _source_job(service, spec)
    annotation_path = (
        service.registry.blob_path(spec["annotation_artifact_sha256"])
        if spec["annotation_artifact_sha256"] else None
    )
    inference = infer_lens_sensor_isp_detector(
        lens_raw_job=raw_job, lens_isp_job=isp_job, **paths,
        model_weights_sha256=spec["model_weights_sha256"],
        target_labels=spec["target_labels"],
        score_threshold=spec["score_threshold"],
        annotation_path=annotation_path, iou_threshold=spec["iou_threshold"],
    )
    if (
        inference["image_binding"] != frozen["source_identities"]["image_binding"]
        or inference["model_provenance"] != frozen["model_preflight"]
        or canonical(prepare(service, spec)) != canonical(frozen)
    ):
        raise IntegrityError(
            "Lens ISP detector source, weights or executable changed during inference."
        )
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens ISP detector study was cancelled during inference.")
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_identities": frozen["source_identities"],
        "inference": inference,
        "verification": {
            "implementation_verification": "runtime_completed",
            "detector_inference": "performed_by_offline_ultralytics_yolo11n",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
            "dataset_map": "not_established",
            "product_perception_performance": "not_established",
        },
        "limitation": LIMITATION,
    }
    artifact = service.registry.store_blob(
        canonical(summary).encode(), RESULT_NAME, "application/json"
    )
    return {"summary": summary, "artifacts": [artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens ISP detector study.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        identities = job["source_identities"]
        raw_job, isp_job, paths = _source_job(service, job["specification"])
        binding = lens_sensor_isp_image_binding(
            lens_raw_job=raw_job, lens_isp_job=isp_job, **paths
        )
        if (
            summary.get("specification") != job["specification"]
            or summary.get("source_identities") != identities
            or digest(raw_job["result"]) != identities["lens_raw_result_sha256"]
            or digest(isp_job["result"]) != identities["lens_isp_result_sha256"]
            or binding != identities["image_binding"]
            or summary.get("inference", {}).get("image_binding") != binding
            or summary.get("inference", {}).get("model_provenance") != job["model_preflight"]
        ):
            raise IntegrityError("Lens ISP detector result differs from frozen evidence.")
        if job["status"] == "succeeded":
            if len(job["artifacts"]) != 1:
                raise IntegrityError("Lens ISP detector result artifact is incomplete.")
            artifact = job["artifacts"][0]
            expected_bytes = canonical(summary).encode()
            if (
                artifact["name"] != RESULT_NAME
                or artifact["sha256"] != hashlib.sha256(expected_bytes).hexdigest()
                or service.registry.blob_path(artifact["sha256"]).read_bytes() != expected_bytes
            ):
                raise IntegrityError("Lens ISP detector result artifact differs.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    @app.get("/api/v1/lens-sensor-isp-detectors/template", tags=[JOB_KIND])
    def template(source_lens_sensor_job_id: str = "", source_lens_sensor_isp_job_id: str = ""):
        spec = default_spec()
        trusted = os.environ.get(_TRUST_ENV, "")
        availability: dict[str, str] = {"status": "operator_trust_required"}
        if trusted:
            try:
                model = local_model_preflight(trusted)
                spec["model_weights_sha256"] = model["weights_sha256"]
                availability = {"status": "ready_for_development_inference"}
            except ValueError as exc:
                availability = {
                    "status": "operator_trusted_runtime_unavailable",
                    "reason": str(exc),
                }
        candidates = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") in {"lens_sensor_isp_replay", "lens_sensor_calibrated_isp"}
            and item["status"] == "succeeded"
        ]
        response: dict[str, Any] = {
            "specification": spec, "source_lens_isp_jobs": candidates,
            "limitation": LIMITATION,
            "availability": availability,
            "required_operator_trust_env_var": _TRUST_ENV,
            "annotation_import_endpoint": "/api/v1/lens-sensor-isp-perception/artifacts",
            "annotation_schema": "camerae2e_detection_annotations_v1",
        }
        if source_lens_sensor_job_id and source_lens_sensor_isp_job_id:
            selected = {
                "source_lens_sensor_job_id": source_lens_sensor_job_id,
                "source_lens_sensor_isp_job_id": source_lens_sensor_isp_job_id,
            }
            raw_job, isp_job, paths = _source_job(service, selected)
            response["image_binding"] = lens_sensor_isp_image_binding(
                lens_raw_job=raw_job, lens_isp_job=isp_job, **paths
            )
        return response

    @app.post("/api/v1/lens-sensor-isp-detectors", status_code=202, tags=[JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get(
        "/api/v1/lens-sensor-isp-detectors/replay-qualifications/template",
        tags=[JOB_KIND],
    )
    def replay_qualification_template(
        detector_job_id: Annotated[list[str] | None, Query()] = None,
    ):
        from .lens_sensor_isp_detector_qualification import detector_replay_intake_template

        return detector_replay_intake_template(service, detector_job_id or [])

    @app.post(
        "/api/v1/lens-sensor-isp-detectors/replay-qualifications",
        status_code=201, tags=[JOB_KIND],
    )
    def qualify_replay(payload: dict):
        from .lens_sensor_isp_detector_qualification import qualify_saved_detector_replay

        try:
            encoded = canonical(payload).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise HTTPException(422, "Detector replay intake must be canonical JSON.") from exc
        if len(encoded) > 256 * 1024:
            raise HTTPException(413, "Detector replay intake exceeds 256 KiB.")
        report = qualify_saved_detector_replay(service, payload)
        artifact = service.registry.store_blob(
            canonical(report).encode(),
            "lens-sensor-isp-detector-replay-qualification.json",
            "application/json",
        )
        return {"report": report, "artifact": artifact}

    @app.get("/api/v1/lens-sensor-isp-detectors", tags=[JOB_KIND])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-isp-detectors/{identity}", tags=[JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-isp-detectors/{identity}/cancel", tags=[JOB_KIND])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-isp-detectors/{identity}/export", tags=[JOB_KIND])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ConflictError("Only a completed Lens ISP detector job can be exported.")
        return Response(
            canonical(job), media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="lens-isp-detector-result.json"'},
        )
