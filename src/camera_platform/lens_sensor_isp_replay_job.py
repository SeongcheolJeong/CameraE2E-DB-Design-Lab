"""Replay a pinned Lens DB x Sensor DB integer RAW through ISP v2.

This job accepts only the saved sensor's actual 2x2 Bayer CFA. It does not
reinterpret a Quad Bayer or monochrome capture as RGGB, and it never captures
a new RAW frame during ISP processing.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import isp_v2
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_isp_replay_study_v1"
JOB_KIND = "lens_sensor_isp_replay"
LIMITATION = (
    "One exact retained geometric Lens DB x saved Sensor DB RAW is processed by "
    "ISP v2. The saved native 2x2 Bayer CFA, ADC and black level must match; "
    "Quad Bayer and monochrome are refused. Lens absolute transmission and "
    "relative illumination remain unknown unity assumptions. LSC/denoise "
    "require a separate capture-domain calibration and are refused here. "
    "RGB differences are diagnostic, not measured quality or color accuracy."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 원본 RAW ISP v2 재처리",
        "source_lens_sensor_job_id": "",
        "profile": isp_v2.default_profile(),
        "acknowledge_simulation_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the Lens x Sensor ISP replay fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit Lens x Sensor ISP simulation-limit acknowledgement required.")
    name, identity = payload["name"], payload["source_lens_sensor_job_id"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("ISP replay name requires 1..160 characters.")
    if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 128:
        raise ValueError("Select one completed source_lens_sensor_job_id.")
    profile = isp_v2.validate_profile(payload["profile"])
    if profile["calibration_id"] is not None or any(
        profile[stage]["mode"] != "bypass" for stage in ("spatial_lsc", "raw_denoise")
    ):
        raise ValueError(
            "Lens x Sensor ISP replay requires bypass LSC/denoise and no calibration ID."
        )
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["source_lens_sensor_job_id"] = identity.strip()
    result["profile"] = profile
    return result


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current
    from .retained_raw_dataset_job import _lens_source
    from .retained_raw_isp_bridge import inspect_lens_sensor_isp_input

    spec = specification(payload)
    source = _lens_source(
        service,
        {"source_job_id": spec["source_lens_sensor_job_id"]},
    )
    checked = inspect_lens_sensor_isp_input(
        service.registry.blob_path(source["source_artifacts"]["json"]["sha256"]),
        service.registry.blob_path(source["source_artifacts"]["raw"]["sha256"]),
        source_lens_sensor_job_id=spec["source_lens_sensor_job_id"],
        sensor_db_root=Path(service.workspace) / "camerae2e_db",
        profile=spec["profile"],
    )
    if checked["source_binding"] != source["source_binding"]:
        raise IntegrityError("Lens x Sensor ISP input differs from the completed source job.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source_capture_result_sha256": source["source_capture_result_sha256"],
        "source_artifacts": source["source_artifacts"],
        "source_binding": checked["source_binding"],
        "native_cfa": checked["native_cfa"],
        "readout": checked["readout"],
        "raw_format": checked["raw_format"],
        "geometry": checked["geometry"],
        "raw_content_sha256": checked["raw_content_sha256"],
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .retained_raw_isp_bridge import replay_lens_sensor_raw

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens x Sensor ISP replay is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if canonical(frozen) != canonical({key: job.get(key) for key in frozen}):
        raise IntegrityError("Frozen Lens x Sensor RAW, executable or request changed.")
    spec = frozen["specification"]
    with tempfile.TemporaryDirectory(prefix="camera-lens-sensor-isp-") as temporary:
        report = replay_lens_sensor_raw(
            service.registry.blob_path(frozen["source_artifacts"]["json"]["sha256"]),
            service.registry.blob_path(frozen["source_artifacts"]["raw"]["sha256"]),
            source_lens_sensor_job_id=spec["source_lens_sensor_job_id"],
            sensor_db_root=Path(service.workspace) / "camerae2e_db",
            profile=spec["profile"],
            output_dir=Path(temporary) / "replay",
        )
        for field, frozen_field in (
            ("source_binding", "source_binding"),
            ("saved_native_cfa", "native_cfa"),
            ("raw_format", "raw_format"),
            ("geometry", "geometry"),
            ("source_readout", "readout"),
            ("raw_content_sha256", "raw_content_sha256"),
        ):
            if canonical(report[field]) != canonical(frozen[frozen_field]):
                raise IntegrityError("Lens x Sensor ISP output lost its frozen input contract.")
        if canonical(prepare(service, spec)) != canonical(frozen):
            raise IntegrityError("Lens x Sensor RAW, DB or executable changed during ISP replay.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Lens x Sensor ISP replay was cancelled during execution.")
        artifacts = []
        for kind, media in (("json", "application/json"), ("npz", "application/octet-stream")):
            content = Path(report["artifacts"][kind]).read_bytes()
            if hashlib.sha256(content).hexdigest() != report["artifact_sha256"][kind]:
                raise IntegrityError(f"Lens x Sensor ISP {kind} evidence bytes changed.")
            artifacts.append(service.registry.store_blob(
                content, f"lens-sensor-isp-replay.{kind}", media
            ))
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_capture_result_sha256": frozen["source_capture_result_sha256"],
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
    artifacts.append(service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-isp-replay-result.json", "application/json"
    ))
    return {"summary": summary, "artifacts": artifacts}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens x Sensor ISP replay.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        if (
            canonical(summary["specification"]) != canonical(job["specification"])
            or summary["source_capture_result_sha256"] != job["source_capture_result_sha256"]
            or canonical(summary["source_binding"]) != canonical(job["source_binding"])
        ):
            raise IntegrityError("Lens x Sensor ISP result differs from frozen request.")
        if job["status"] == "succeeded":
            for descriptor in job["artifacts"]:
                service.registry.blob_path(descriptor["sha256"])
            if len(job["artifacts"]) != 3 or any(
                summary["evidence"][kind] not in job["artifacts"] for kind in ("json", "npz")
            ):
                raise IntegrityError("Lens x Sensor ISP evidence link is missing.")
            source_json = json.loads(
                service.registry.blob_path(summary["evidence"]["json"]["sha256"]).read_text()
            )
            if source_json["artifacts"]["npz_sha256"] != summary["evidence"]["npz"]["sha256"]:
                raise IntegrityError("Lens x Sensor ISP NPZ differs from its report.")
            if canonical(source_json["source_binding"]) != canonical(summary["source_binding"]):
                raise IntegrityError("Lens x Sensor ISP evidence JSON source binding differs.")
            result = next(
                (item for item in job["artifacts"]
                 if item["name"] == "lens-sensor-isp-replay-result.json"), None,
            )
            if result is None or (
                service.registry.blob_path(result["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Lens x Sensor ISP result artifact differs.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-isp-replays/template", tags=["lens_sensor_isp_replay"])
    def template():
        sources = [
            {"id": item["id"], "name": item["name"], "status": item["status"]}
            for item in service.registry.list_runs()
            if item.get("job_kind") == "lens_sensor_raw" and item["status"] == "succeeded"
        ]
        return {"specification": default_spec(), "source_lens_sensor_jobs": sources,
                "limitation": LIMITATION}

    @app.post("/api/v1/lens-sensor-isp-replays", status_code=202, tags=["lens_sensor_isp_replay"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-isp-replays", tags=["lens_sensor_isp_replay"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-isp-replays/{identity}", tags=["lens_sensor_isp_replay"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-isp-replays/{identity}/cancel", tags=["lens_sensor_isp_replay"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-isp-replays/{identity}/export", tags=["lens_sensor_isp_replay"])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ValueError("Lens × Sensor ISP replay result is not ready.")
        return Response(
            canonical(job),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="lens-sensor-isp-replay.json"'},
        )
