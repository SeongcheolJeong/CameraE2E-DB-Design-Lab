"""Worker/API lifecycle for exact saved sensor RAW -> bounded ISP v2 replay."""

from __future__ import annotations

import hashlib
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import isp_v2
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_saved_sensor_isp_replay_study_v1"
JOB_KIND = "saved_sensor_isp_replay"
LIMITATION = (
    "Replays one exact retained RAW frame from a completed saved sensor-quality "
    "job through the existing ISP v2 graph. Native RGGB, saved ADC bit depth, "
    "black level and full executed mosaic origin are verified. Fixed white "
    "balance, CCM, tone and bounded sharpening are supported. LSC and denoise "
    "require a capture-domain calibration asset and are refused here. Differences "
    "are diagnostic, not measured quality, HDR fusion or perception scores."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "보존 RAW ISP v2 재처리",
        "source_quality_job_id": "",
        "sensor_report_index": 0,
        "case_index": 0,
        "profile": isp_v2.default_profile(),
        "acknowledge_simulation_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the saved sensor ISP replay fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit saved sensor ISP simulation-limit acknowledgement required.")
    name, identity = payload["name"], payload["source_quality_job_id"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("ISP replay name requires 1..160 characters.")
    if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 128:
        raise ValueError("Select one completed source_quality_job_id.")
    for key in ("sensor_report_index", "case_index"):
        if type(payload[key]) is not int or not 0 <= payload[key] <= 2:
            raise ValueError(f"{key} must be an integer index from 0 to 2.")
    profile = isp_v2.validate_profile(payload["profile"])
    if profile["calibration_id"] is not None or any(
        profile[stage]["mode"] != "bypass" for stage in ("spatial_lsc", "raw_denoise")
    ):
        raise ValueError(
            "Saved sensor ISP replay requires bypass LSC/denoise and no calibration ID."
        )
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["source_quality_job_id"] = identity.strip()
    result["profile"] = profile
    return result


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current
    from .retained_raw_dataset_job import _source
    from .saved_sensor_isp_replay import inspect_saved_quality_isp_input

    spec = specification(payload)
    source = _source(service, spec)
    checked = inspect_saved_quality_isp_input(
        service.registry.blob_path(source["source_artifacts"]["json"]["sha256"]),
        service.registry.blob_path(source["source_artifacts"]["npz"]["sha256"]),
        source_quality_job_id=spec["source_quality_job_id"],
        case_index=spec["case_index"],
        sensor_db_root=Path(service.workspace) / "camerae2e_db",
        profile=spec["profile"],
    )
    if (
        checked["source_binding"]["saved_sensor_revision"] != source["source_revision"]
        or checked["source_binding"]["source_report_sha256"]
        != source["source_artifacts"]["json"]["sha256"]
        or checked["source_binding"]["source_arrays_sha256"]
        != source["source_artifacts"]["npz"]["sha256"]
    ):
        raise IntegrityError("ISP replay source differs from completed quality job.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        **source,
        "source_binding": checked["source_binding"],
        "raw_content_sha256": checked["raw_content_sha256"],
        "native_cfa": checked["native_cfa"],
        "raw_format": checked["raw_format"],
        "geometry": checked["geometry"],
        "readout": checked["readout"],
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .saved_sensor_isp_replay import replay_saved_quality_raw

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Saved sensor ISP replay job is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen saved RAW, DB, ISP graph or request changed.")
    spec = frozen["specification"]
    with tempfile.TemporaryDirectory(prefix="camera-saved-isp-") as temporary:
        report = replay_saved_quality_raw(
            service.registry.blob_path(frozen["source_artifacts"]["json"]["sha256"]),
            service.registry.blob_path(frozen["source_artifacts"]["npz"]["sha256"]),
            source_quality_job_id=spec["source_quality_job_id"],
            case_index=spec["case_index"],
            sensor_db_root=Path(service.workspace) / "camerae2e_db",
            profile=spec["profile"],
            output_dir=Path(temporary) / "replay",
        )
        if any(
            canonical(report[field]) != canonical(frozen[frozen_field])
            for field, frozen_field in (
                ("source_binding", "source_binding"),
                ("saved_native_cfa", "native_cfa"),
                ("raw_format", "raw_format"),
                ("geometry", "geometry"),
                ("source_readout", "readout"),
                ("raw_content_sha256", "raw_content_sha256"),
            )
        ):
            raise IntegrityError("Saved sensor ISP replay output lost its frozen input contract.")
        if canonical(prepare(service, spec)) != canonical(frozen):
            raise IntegrityError("Saved RAW, DB or executable changed during ISP replay.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Saved sensor ISP replay job was cancelled during execution.")
        artifacts = []
        for kind, media in (("json", "application/json"), ("npz", "application/octet-stream")):
            content = Path(report["artifacts"][kind]).read_bytes()
            if hashlib.sha256(content).hexdigest() != report["artifact_sha256"][kind]:
                raise IntegrityError(f"Saved sensor ISP {kind} artifact bytes changed.")
            artifacts.append(
                service.registry.store_blob(content, f"saved-sensor-isp-replay.{kind}", media)
            )
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_quality_result_sha256": frozen["source_quality_result_sha256"],
        "source_binding": frozen["source_binding"],
        "saved_native_cfa": frozen["native_cfa"],
        "raw_format": frozen["raw_format"],
        "geometry": frozen["geometry"],
        "readout": frozen["readout"],
        "raw_content_sha256": report["raw_content_sha256"],
        "raw_values_byte_equal": report["raw_values_byte_equal"],
        "candidate_rgb_consumer": report["candidate_rgb_consumer"],
        "before_after": report["before_after"],
        "reference_isp": report["reference_isp"],
        "candidate_isp": report["candidate_isp"],
        "evidence": {"json": artifacts[0], "npz": artifacts[1]},
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitation": LIMITATION,
    }
    artifacts.append(
        service.registry.store_blob(
            canonical(summary).encode(), "saved-sensor-isp-replay-result.json", "application/json"
        )
    )
    return {"summary": summary, "artifacts": artifacts}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a saved sensor ISP replay.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        if (
            canonical(summary["specification"]) != canonical(job["specification"])
            or summary["source_quality_result_sha256"] != job["source_quality_result_sha256"]
            or canonical(summary["source_binding"]) != canonical(job["source_binding"])
        ):
            raise IntegrityError("Saved sensor ISP replay result differs from frozen request.")
        if job["status"] == "succeeded":
            for descriptor in job["artifacts"]:
                service.registry.blob_path(descriptor["sha256"])
            if len(job["artifacts"]) != 3 or any(
                summary["evidence"][kind] not in job["artifacts"] for kind in ("json", "npz")
            ):
                raise IntegrityError("Saved sensor ISP replay evidence link is missing.")
            source_json = json.loads(
                service.registry.blob_path(summary["evidence"]["json"]["sha256"]).read_text()
            )
            if source_json["artifacts"]["npz_sha256"] != summary["evidence"]["npz"]["sha256"]:
                raise IntegrityError("Saved sensor ISP replay NPZ differs from its report.")
            if canonical(source_json["source_binding"]) != canonical(summary["source_binding"]):
                raise IntegrityError("Saved sensor ISP replay JSON source binding differs.")
            result_descriptor = next(
                (
                    item
                    for item in job["artifacts"]
                    if item["name"] == "saved-sensor-isp-replay-result.json"
                ),
                None,
            )
            if result_descriptor is None or (
                service.registry.blob_path(result_descriptor["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Saved sensor ISP replay result artifact differs.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/saved-sensor-isp-replays/template", tags=["saved_sensor_isp_replay"])
    def template():
        sources = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") == "saved_sensor_quality" and item["status"] == "succeeded"
        ]
        return {
            "specification": default_spec(),
            "source_quality_jobs": sources,
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/saved-sensor-isp-replays", status_code=202, tags=["saved_sensor_isp_replay"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/saved-sensor-isp-replays", tags=["saved_sensor_isp_replay"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/saved-sensor-isp-replays/{identity}", tags=["saved_sensor_isp_replay"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(
        "/api/v1/saved-sensor-isp-replays/{identity}/cancel", tags=["saved_sensor_isp_replay"]
    )
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/saved-sensor-isp-replays/{identity}/export", tags=["saved_sensor_isp_replay"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="saved-sensor-isp-replay.json"'},
        )
