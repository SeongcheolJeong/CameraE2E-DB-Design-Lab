"""Typed Worker lifecycle for a bounded pinned-sensor PWM phase sweep."""

from __future__ import annotations

import hashlib
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_saved_sensor_lfm_study_v1"
JOB_KIND = "saved_sensor_lfm"
LIMITATION = (
    "Four-phase sweep of an authored ideal square-wave light using the pinned "
    "sensor spectral model and native readout/FPN. Phase-dependent RAW variation "
    "is a bounded simulation sensitivity, not measured LFM, all-phase immunity, "
    "closed-loop AE, sensor HDR mode or physical validation."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "저장 센서 PWM 위상·노출 진단",
        "sensor_revision": {"identifier": "", "run_id": ""},
        "exposure_s": [0.001, 0.004],
        "pwm_period_s": 0.002,
        "duty_cycle": 0.5,
        "off_to_on_ratio": 0.02,
        "on_luminance_cd_m2": 1000.0,
        "seed": 7,
        "acknowledge_simulation_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    from .saved_sensor_hdr import _number
    from .saved_sensor_lfm import MAX_PWM_CYCLES_PER_EXPOSURE

    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the saved sensor LFM study fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit PWM simulation-limit acknowledgement required.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Study name requires 1..160 characters.")
    revision = payload["sensor_revision"]
    if (
        not isinstance(revision, dict)
        or set(revision) != {"identifier", "run_id"}
        or any(not isinstance(revision[key], str) or not 1 <= len(revision[key].strip()) <= 128
               for key in ("identifier", "run_id"))
    ):
        raise ValueError("Select one explicit saved sensor run and identifier.")
    if not isinstance(payload["exposure_s"], list) or len(payload["exposure_s"]) != 2:
        raise ValueError("Declare exactly two exposure_s values.")
    exposures = [_number(v, "exposure_s", 0.0005, 0.02) for v in payload["exposure_s"]]
    if not exposures[0] < exposures[1]:
        raise ValueError("PWM exposure_s values must be distinct and ascending.")
    period = _number(payload["pwm_period_s"], "pwm_period_s", 0.0005, 0.02)
    duty = _number(payload["duty_cycle"], "duty_cycle", 0.05, 0.95)
    off = _number(payload["off_to_on_ratio"], "off_to_on_ratio", 0.0, 0.5)
    luma = _number(payload["on_luminance_cd_m2"], "on_luminance_cd_m2", 0.01, 10000.0)
    if exposures[-1] / period > MAX_PWM_CYCLES_PER_EXPOSURE:
        raise ValueError("PWM exposure exceeds the bounded 32-cycle integration domain.")
    if type(payload["seed"]) is not int or not 0 <= payload["seed"] < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    selected = deepcopy(payload)
    selected.update(
        name=name.strip(), exposure_s=exposures, pwm_period_s=period,
        duty_cycle=duty, off_to_on_ratio=off, on_luminance_cd_m2=luma,
    )
    return selected


def _db_root(service: Any) -> Path:
    return Path(service.workspace) / "camerae2e_db"


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from pyisetcam.sensor_simulation_db import (
        sensor_simulation_db_config,
        sensor_simulation_db_get,
    )

    from .imaging import _current
    from .offline_assets import offline_asset_identity
    from .sensor_db_bridge import inspect_sensor_revision

    spec = specification(payload)
    selected = spec["sensor_revision"]
    source = inspect_sensor_revision(
        selected["identifier"], root=_db_root(service), run_id=selected["run_id"]
    )
    if not source["executable"]:
        raise ValueError(f"Saved sensor run cannot execute: {source['reason']}")
    revision = source["revision"]
    if revision["model"] not in {"db_estimate_v1", "physical_estimate_v1"}:
        raise ValueError("Saved LFM requires a pinned DB/physical estimate snapshot.")
    saved = sensor_simulation_db_get(
        revision["report_code"], root=_db_root(service), run_id=revision["run_id"]
    )
    settings = saved["metadata"].get("simulation_settings") or {}
    if settings.get("fnumber") != 4.0 or settings.get("focal_length_m") != 0.004:
        raise ValueError("Saved LFM requires saved F/4, 4 mm ideal optics.")
    scenario = sensor_simulation_db_config(
        revision["report_code"], root=_db_root(service), run_id=revision["run_id"]
    )["scenario"]
    if scenario.get("sensor_output_mode") != "digital_raw":
        raise ValueError("Saved LFM requires digital_raw output.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source_identity": source["revision"],
        "upstream_assets": offline_asset_identity(workspace=service.workspace),
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .saved_sensor_lfm import evaluate_saved_sensor_pwm_lfm

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Saved sensor LFM study is not running or was cancelled.")
    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen saved LFM source, assets or request changed.")
    spec = fresh["specification"]
    selected = spec["sensor_revision"]
    with tempfile.TemporaryDirectory(prefix="camera-saved-lfm-") as temporary:
        report = evaluate_saved_sensor_pwm_lfm(
            selected["identifier"],
            run_id=selected["run_id"],
            root=_db_root(service),
            workspace=service.workspace,
            output_dir=Path(temporary) / "evidence",
            exposure_s=spec["exposure_s"],
            pwm_period_s=spec["pwm_period_s"],
            duty_cycle=spec["duty_cycle"],
            off_to_on_ratio=spec["off_to_on_ratio"],
            on_luminance_cd_m2=spec["on_luminance_cd_m2"],
            seed=spec["seed"],
        )
        if any(report["revision"][key] != fresh["source_identity"][key]
               for key in ("run_id", "result_sha256", "config_sha256")):
            raise IntegrityError("Saved LFM report differs from its frozen source.")
        if report["upstream_assets"] != fresh["upstream_assets"]:
            raise IntegrityError("Saved LFM upstream assets changed.")
        artifacts = []
        saved_artifacts = {}
        for kind, media in (("json", "application/json"), ("npz", "application/octet-stream")):
            content = Path(report["artifacts"][kind]).read_bytes()
            if hashlib.sha256(content).hexdigest() != report["artifact_sha256"][kind]:
                raise IntegrityError("Saved LFM artifact hash differs from its result.")
            descriptor = service.registry.store_blob(
                content, f"saved-sensor-lfm-evidence.{kind}", media
            )
            artifacts.append(descriptor)
            saved_artifacts[kind] = descriptor
        report = {key: value for key, value in report.items()
                  if key not in {"artifacts", "artifact_sha256"}}
        report["artifacts"] = saved_artifacts
        if canonical(prepare(service, spec)) != canonical(fresh):
            raise IntegrityError("Saved LFM source, assets or executable changed during study.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Saved LFM study was cancelled during execution.")
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_identity": fresh["source_identity"],
        "upstream_assets": fresh["upstream_assets"],
        "report": report,
        "verification": report["verification"],
        "limitation": LIMITATION,
    }
    artifacts.append(service.registry.store_blob(
        canonical(summary).encode(), "saved-sensor-lfm-result.json", "application/json"
    ))
    return {"summary": summary, "artifacts": artifacts}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a saved sensor LFM study.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        for key in ("specification", "source_identity", "upstream_assets"):
            if canonical(summary[key]) != canonical(job[key]):
                raise IntegrityError("Saved LFM result differs from its frozen request.")
        if job["status"] == "succeeded":
            for descriptor in summary["report"]["artifacts"].values():
                if descriptor not in job["artifacts"]:
                    raise IntegrityError("Saved LFM evidence link is missing.")
            for descriptor in job["artifacts"]:
                content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != descriptor["sha256"]:
                    raise IntegrityError("Saved LFM artifact bytes differ.")
            result = [item for item in job["artifacts"]
                      if item["name"] == "saved-sensor-lfm-result.json"]
            if len(result) != 1 or service.registry.blob_path(
                result[0]["sha256"]
            ).read_bytes() != canonical(summary).encode():
                raise IntegrityError("Saved LFM result differs from its artifact.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/saved-sensor-lfm/template", tags=["saved_sensor_lfm"])
    def template(q: str = ""):
        from .sensor_db_bridge import sensor_revision_catalog

        return {
            "specification": default_spec(),
            "sensor_catalog": sensor_revision_catalog(root=_db_root(service), query=q or None),
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/saved-sensor-lfm", status_code=202, tags=["saved_sensor_lfm"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/saved-sensor-lfm", tags=["saved_sensor_lfm"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/saved-sensor-lfm/{identity}", tags=["saved_sensor_lfm"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/saved-sensor-lfm/{identity}/cancel", tags=["saved_sensor_lfm"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/saved-sensor-lfm/{identity}/export", tags=["saved_sensor_lfm"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="saved-sensor-lfm.json"'},
        )
