"""Durable RAW perception handoff over one existing Lens retained dataset bundle.

This job never exports or copies RAW. The retained_raw_dataset job owns the one
portable dataset ZIP; this job verifies it and stores only a small relative RAW
index and a provenance report. No annotations, detector, or score are inferred.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .lens_sensor_perception import inspect_retained_lens_sensor_bundle
from .registry import IntegrityError, canonical, digest
from .retained_raw_dataset_job import LENS_SCHEMA

SCHEMA = "camerae2e_lens_sensor_perception_handoff_study_v1"
JOB_KIND = "lens_sensor_perception"
LIMITATION = (
    "References one previously exported Lens × Sensor retained RAW dataset bundle. "
    "Only a portable relative-path RAW index and provenance report are stored; "
    "the original RAW is neither recaptured nor exported again. Annotations and "
    "independent ground truth are absent, so no detector or perception metric is "
    "available. Registry-local duplicate RAW is checked at execution, but this "
    "does not establish a global train/test holdout or physical-camera validity."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 RAW perception handoff",
        "source_dataset_job_id": "",
        "acknowledge_simulation_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the Lens × Sensor perception handoff fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit perception simulation-limit acknowledgement required.")
    name = payload["name"]
    identity = payload["source_dataset_job_id"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Perception handoff name requires 1..160 characters.")
    if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 128:
        raise ValueError("Select one completed Lens retained dataset job ID.")
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["source_dataset_job_id"] = identity.strip()
    return result


def _source(service: Any, source_dataset_job_id: str) -> tuple[dict, dict, dict]:
    from .lens_sensor_raw_job import get as lens_get
    from .retained_raw_dataset_job import get as dataset_get

    dataset = dataset_get(service, source_dataset_job_id)
    if dataset["status"] != "succeeded" or dataset["specification"].get("schema") != LENS_SCHEMA:
        raise ValueError("Select one succeeded Lens × Sensor retained dataset job.")
    binding = dataset["result"]["summary"]["source_binding"]
    lens = lens_get(service, binding["source_lens_sensor_job_id"])
    if lens["status"] != "succeeded":
        raise ValueError("The original Lens × Sensor RAW job is not complete.")
    bundle = dataset["result"]["summary"]["bundle"]
    handoff = inspect_retained_lens_sensor_bundle(
        dataset,
        lens,
        bundle_path=service.registry.blob_path(bundle["sha256"]),
    )
    return dataset, lens, handoff


def _registry_cohort(service: Any, *, raw_content_sha256: str, selected_id: str) -> dict:
    """Check current registry's completed retained datasets for content reuse."""
    from .retained_raw_dataset_job import get as dataset_get

    matching = []
    for row in service.registry.list_runs():
        if row.get("job_kind") != "retained_raw_dataset" or row["status"] != "succeeded":
            continue
        summary = row["result"]["summary"]
        if summary.get("raw_content_sha256") != raw_content_sha256:
            continue
        verified = dataset_get(service, row["id"])
        if verified["result"]["summary"]["raw_content_sha256"] != raw_content_sha256:
            raise IntegrityError("Registry cohort RAW hash changed during verification.")
        matching.append(row["id"])
    if matching != [selected_id]:
        raise ValueError(
            "The exact RAW occurs in another completed retained dataset job; "
            "a perception cohort cannot reuse it across exports or splits."
        )
    return {
        "scope": "this_registry_at_prepare_and_execution",
        "matching_dataset_job_ids": matching,
        "duplicate_raw_in_registry_at_execution": False,
        "global_cross_export_exclusivity_enforced": False,
        "holdout_independence_claimed": False,
    }


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    dataset, lens, handoff = _source(service, spec["source_dataset_job_id"])
    cohort = _registry_cohort(
        service,
        raw_content_sha256=handoff["raw_content_sha256"],
        selected_id=dataset["id"],
    )
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source_dataset_result_sha256": digest(dataset["result"]),
        "source_capture_result_sha256": digest(lens["result"]),
        "source_lens_sensor_job_id": lens["id"],
        "source_handoff": handoff,
        "split_isolation": cohort,
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens perception handoff job is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen Lens RAW dataset, cohort or executable changed.")
    handoff = frozen["source_handoff"]
    index_bytes = (canonical(handoff["portable_raw_index"]) + "\n").encode()
    index_descriptor = service.registry.store_blob(
        index_bytes, "lens-sensor-portable-raw-index.jsonl", "application/x-ndjson"
    )
    if canonical(prepare(service, job["specification"])) != canonical(frozen):
        raise IntegrityError("Lens RAW dataset or registry cohort changed during handoff.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens perception handoff job was cancelled during execution.")
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": frozen["specification"],
        "source_dataset_result_sha256": frozen["source_dataset_result_sha256"],
        "source_capture_result_sha256": frozen["source_capture_result_sha256"],
        "source_lens_sensor_job_id": frozen["source_lens_sensor_job_id"],
        "source_binding": handoff["source_binding"],
        "dataset_bundle": handoff["dataset_bundle"],
        "dataset_manifest_sha256": handoff["dataset_manifest_sha256"],
        "raw_content_sha256": handoff["raw_content_sha256"],
        "raw_shape": handoff["raw_shape"],
        "split": handoff["split"],
        "split_isolation": frozen["split_isolation"],
        "portable_raw_index": {
            "artifact": index_descriptor,
            "row_count": 1,
            "path_semantics": "relative_to_extracted_dataset_bundle_root",
            "pyisetcam_native_index": False,
        },
        "native_index_reconstruction": {
            "function": "pyisetcam.dataset.camerae2e_dataset_export_perception_index",
            "manifest": "manifest.json",
            "formats": ["raw_manifest"],
            "requires_bundle_extraction": True,
        },
        "annotation_provenance": handoff["annotation_provenance"],
        "perception_metric": handoff["perception_metric"],
        "detector_evaluation_performed": False,
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitation": LIMITATION,
    }
    result_descriptor = service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-perception-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [index_descriptor, result_descriptor]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens × Sensor perception handoff.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        if (
            summary.get("specification") != job["specification"]
            or summary.get("source_dataset_result_sha256") != job["source_dataset_result_sha256"]
            or summary.get("source_capture_result_sha256") != job["source_capture_result_sha256"]
            or summary.get("source_lens_sensor_job_id") != job["source_lens_sensor_job_id"]
            or summary.get("source_binding") != job["source_handoff"]["source_binding"]
            or summary.get("dataset_bundle") != job["source_handoff"]["dataset_bundle"]
            or summary.get("split_isolation") != job["split_isolation"]
            or summary.get("annotation_provenance")
            != job["source_handoff"]["annotation_provenance"]
            or summary.get("perception_metric", {}).get("available") is not False
        ):
            raise IntegrityError(
                "Lens perception result differs from frozen source or truth boundary."
            )
        if job["status"] == "succeeded":
            if len(job["artifacts"]) != 2:
                raise IntegrityError("Lens perception handoff artifacts are incomplete.")
            index_descriptor = summary["portable_raw_index"]["artifact"]
            if index_descriptor not in job["artifacts"] or (
                service.registry.blob_path(index_descriptor["sha256"]).read_bytes()
                != (canonical(job["source_handoff"]["portable_raw_index"]) + "\n").encode()
            ):
                raise IntegrityError("Portable RAW index differs from the frozen handoff.")
            result_descriptor = next(
                (
                    item
                    for item in job["artifacts"]
                    if item["name"] == "lens-sensor-perception-result.json"
                ),
                None,
            )
            if result_descriptor is None or (
                service.registry.blob_path(result_descriptor["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Lens perception result artifact differs from its summary.")
            service.registry.blob_path(summary["dataset_bundle"]["sha256"])
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-perception/template", tags=[JOB_KIND])
    def template():
        sources = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") == "retained_raw_dataset"
            and item["status"] == "succeeded"
            and item.get("specification", {}).get("schema") == LENS_SCHEMA
        ]
        return {
            "specification": default_spec(),
            "source_dataset_jobs": sources,
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/lens-sensor-perception", status_code=202, tags=[JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-perception", tags=[JOB_KIND])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-perception/{identity}", tags=[JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-perception/{identity}/cancel", tags=[JOB_KIND])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-perception/{identity}/export", tags=[JOB_KIND])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ValueError("Lens perception handoff is not ready.")
        return Response(
            canonical(job["result"]["summary"]),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="lens-sensor-perception.json"'},
        )
