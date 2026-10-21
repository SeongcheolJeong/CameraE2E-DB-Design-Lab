"""Durable image-aware diagnostic over saved Lens × Sensor ISP prediction Jobs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi.responses import Response

from .lens_sensor_isp_perception_cohort import (
    assess_saved_lens_sensor_isp_perception_cohort,
)
from .registry import ConflictError, IntegrityError, canonical, digest
from .saved_sensor_perception import _finite_number, _identity

SCHEMA = "camerae2e_lens_sensor_isp_prediction_cohort_study_v1"
JOB_KIND = "lens_sensor_isp_perception_cohort"
RESULT_NAME = "lens-sensor-isp-perception-cohort-result.json"
LIMITATION = (
    "Image-aware mAP here is a diagnostic of supplied boxes on two distinct, "
    "tiny synthetic scene families. Annotation independence and external model "
    "execution are not independently verified. No representative or product "
    "perception accuracy is established."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 ISP 제공 예측 코호트 진단",
        "evaluation_job_ids": [],
        "iou_thresholds": [0.5, 0.75],
        "acknowledge_diagnostic_only": False,
    }


def specification(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the Lens ISP prediction cohort fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_diagnostic_only"] is not True:
        raise ValueError("Explicit diagnostic-only limitation acknowledgement required.")
    result = deepcopy(payload)
    result["name"] = _identity(payload["name"], "name")
    ids = payload["evaluation_job_ids"]
    if not isinstance(ids, list) or len(ids) != 2:
        raise ValueError("Choose exactly 2 distinct saved ISP prediction Job IDs.")
    result["evaluation_job_ids"] = [_identity(identity, "evaluation_job_id") for identity in ids]
    if len(set(result["evaluation_job_ids"])) != len(ids):
        raise ValueError("Cohort evaluation Job IDs must be distinct.")
    values = payload["iou_thresholds"]
    if not isinstance(values, list) or not 1 <= len(values) <= 10:
        raise ValueError("Declare 1..10 IoU thresholds.")
    thresholds = [_finite_number(value, "iou_threshold") for value in values]
    if (
        any(not 0.0 < value <= 1.0 for value in thresholds)
        or thresholds != sorted(set(thresholds))
        or len({int(round(value * 100.0)) for value in thresholds}) != len(thresholds)
    ):
        raise ValueError("IoU thresholds must be ascending, unique and have distinct percent keys.")
    result["iou_thresholds"] = thresholds
    return result


def _assessment(service: Any, spec: dict[str, Any]) -> dict[str, Any]:
    return assess_saved_lens_sensor_isp_perception_cohort(
        service, spec["evaluation_job_ids"],
        iou_thresholds=tuple(spec["iou_thresholds"]),
    )


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    assessment = _assessment(service, spec)
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source_cases": assessment["cases"],
        "assessment_sha256": digest(assessment),
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens ISP perception cohort is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen cohort sources, boxes, model or executable changed.")
    assessment = _assessment(service, frozen["specification"])
    from .imaging import _current

    if (
        digest(assessment) != frozen["assessment_sha256"]
        or assessment["cases"] != frozen["source_cases"]
        or _current(service) != frozen["environment"]
    ):
        raise IntegrityError("Cohort sources or executable changed during evaluation.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens ISP perception cohort was cancelled during evaluation.")
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": frozen["specification"],
        "source_cases": frozen["source_cases"],
        "assessment_sha256": frozen["assessment_sha256"],
        "assessment": assessment,
        "verification": {
            "implementation_verification": "runtime_completed",
            "source_evidence": "revalidated_at_submission_and_worker_execution",
            "annotation_independence": "supplier_declared_not_independently_verified",
            "model_execution": "not_performed_by_cohort_job",
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
        raise ValueError("Selected job is not a Lens ISP perception cohort.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        assessment = _assessment(service, job["specification"])
        if (
            summary.get("schema") != SCHEMA
            or summary.get("specification") != job["specification"]
            or summary.get("source_cases") != job["source_cases"]
            or summary.get("assessment_sha256") != job["assessment_sha256"]
            or summary.get("assessment") != assessment
            or digest(assessment) != job["assessment_sha256"]
            or assessment["cases"] != job["source_cases"]
        ):
            raise IntegrityError("Saved perception cohort differs from its frozen sources.")
        if job["status"] == "succeeded":
            if len(job["artifacts"]) != 1:
                raise IntegrityError("Perception cohort result artifact is incomplete.")
            artifact = job["artifacts"][0]
            if (
                artifact["name"] != RESULT_NAME
                or service.registry.blob_path(artifact["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Perception cohort result artifact differs.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    @app.get("/api/v1/lens-sensor-isp-perception-cohorts/template", tags=[JOB_KIND])
    def template():
        sources = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") == "lens_sensor_isp_perception"
            and item["status"] == "succeeded"
        ]
        return {"specification": default_spec(), "source_jobs": sources, "limitation": LIMITATION}

    @app.post("/api/v1/lens-sensor-isp-perception-cohorts", status_code=202, tags=[JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-isp-perception-cohorts", tags=[JOB_KIND])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-isp-perception-cohorts/{identity}", tags=[JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-isp-perception-cohorts/{identity}/cancel", tags=[JOB_KIND])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-isp-perception-cohorts/{identity}/export", tags=[JOB_KIND])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ConflictError("Only a completed perception cohort can be exported.")
        return Response(
            canonical(job), media_type="application/json",
            headers={
                "Content-Disposition": (
                    'attachment; filename="lens-isp-perception-cohort-result.json"'
                )
            },
        )
