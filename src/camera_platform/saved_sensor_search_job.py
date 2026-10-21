"""Durable, bounded optimization of exposure and gain on one saved sensor run."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_saved_sensor_search_study_v1"
JOB_KIND = "saved_sensor_search"
LIMITATION = (
    "A bounded discrete search of exposure and analog gain for one hash-pinned "
    "saved sensor run on a uniform equal-energy scene and fixed F/4, 4 mm ideal "
    "optics. The declared objective maximizes mean photoelectrons subject to "
    "well/ADC-white clipping ceilings. This does not optimize SNR, spatial "
    "quality, HDR/LFM, or product performance. Candidate reports contain "
    "numeric diagnostics only; original candidate RAW arrays are not exported. "
    "Physical validation is not established."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "저장 센서 노출·gain 탐색",
        "sensor_revision": {"identifier": "", "run_id": ""},
        "exposure_s": [0.001, 0.004],
        "analog_gain": [1.0, 2.0],
        "luminance_cd_m2": 1000.0,
        "sensor_size": [8, 8],
        "max_saturation_fraction": 0.0,
        "seed": 7,
        "acknowledge_simulation_limits": False,
    }


def _values(values: Any, name: str, low: float, high: float) -> list[float]:
    if not isinstance(values, list) or not 1 <= len(values) <= 4:
        raise ValueError(f"{name} requires one to four declared candidates.")
    result = []
    for value in values:
        if type(value) not in (int, float):
            raise ValueError(f"{name} values must be finite and within [{low}, {high}].")
        try:
            number = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"{name} values must be finite and within [{low}, {high}].") from exc
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"{name} values must be finite and within [{low}, {high}].")
        result.append(number)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} candidate values must be distinct.")
    return result


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the saved sensor search fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit saved-search simulation-limit acknowledgement required.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Search name requires 1..160 characters.")
    selected = payload["sensor_revision"]
    if (
        not isinstance(selected, dict)
        or set(selected) != {"identifier", "run_id"}
        or any(not isinstance(selected[key], str) or not 1 <= len(selected[key].strip()) <= 128
               for key in ("identifier", "run_id"))
    ):
        raise ValueError("Select one explicit saved sensor report and run ID.")
    exposures = _values(payload["exposure_s"], "exposure_s", 1e-6, 1.0)
    gains = _values(payload["analog_gain"], "analog_gain", 1.0, 16.0)
    if len(exposures) * len(gains) > 4:
        raise ValueError("Search may evaluate at most four exposure/gain candidates.")
    size = payload["sensor_size"]
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(type(item) is not int or not 8 <= item <= 32 for item in size)
    ):
        raise ValueError("sensor_size must contain two integer dimensions from 8 to 32.")
    luminance = _values([payload["luminance_cd_m2"]], "luminance_cd_m2", 0.01, 10000.0)[0]
    clip = _values([payload["max_saturation_fraction"]], "max_saturation_fraction", 0.0, 1.0)[0]
    if type(payload["seed"]) is not int or not 0 <= payload["seed"] < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["exposure_s"] = exposures
    result["analog_gain"] = gains
    result["luminance_cd_m2"] = luminance
    result["max_saturation_fraction"] = clip
    return result


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
        raise ValueError("Saved search supports pinned DB/physical estimate snapshots only.")
    saved = sensor_simulation_db_get(
        revision["report_code"], root=_db_root(service), run_id=revision["run_id"]
    )
    settings = saved["metadata"].get("simulation_settings") or {}
    scenario = sensor_simulation_db_config(
        revision["report_code"], root=_db_root(service), run_id=revision["run_id"]
    )["scenario"]
    if settings.get("fnumber") != 4.0 or settings.get("focal_length_m") != 0.004:
        raise ValueError("Saved search requires the saved F/4, 4 mm optics contract.")
    if scenario.get("sensor_output_mode") != "digital_raw":
        raise ValueError("Saved search requires a digital_raw saved output contract.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source_identity": revision,
        "upstream_assets": offline_asset_identity(workspace=service.workspace),
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .saved_sensor_search import search_saved_sensor_capture

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Saved sensor search is not running or was cancelled.")
    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen search request, source, assets or executable changed.")
    spec = fresh["specification"]
    report = search_saved_sensor_capture(
        spec["sensor_revision"]["identifier"],
        run_id=spec["sensor_revision"]["run_id"],
        root=_db_root(service),
        workspace=service.workspace,
        exposure_s=spec["exposure_s"],
        analog_gain=spec["analog_gain"],
        luminance_cd_m2=spec["luminance_cd_m2"],
        sensor_size=spec["sensor_size"],
        max_saturation_fraction=spec["max_saturation_fraction"],
        seed=spec["seed"],
    )
    if any(report["revision"][key] != fresh["source_identity"][key]
           for key in ("run_id", "result_sha256", "config_sha256")):
        raise IntegrityError("Search result differs from pinned saved run.")
    if report["upstream_assets"] != fresh["upstream_assets"]:
        raise IntegrityError("Pinned upstream assets changed during search.")
    if canonical(prepare(service, spec)) != canonical(fresh):
        raise IntegrityError("Saved run, assets or executable changed during search.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Saved sensor search was cancelled during execution.")
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_identity": fresh["source_identity"],
        "upstream_assets": fresh["upstream_assets"],
        "search": report,
        "limitation": LIMITATION,
    }
    artifact = service.registry.store_blob(
        canonical(summary).encode(), "saved-sensor-search-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a saved sensor search.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        for field in ("specification", "source_identity", "upstream_assets"):
            if canonical(summary[field]) != canonical(job[field]):
                raise IntegrityError(
                    f"Saved sensor search {field} differs from the frozen request."
                )
        if job["status"] == "succeeded":
            artifacts = [item for item in job["artifacts"]
                         if item["name"] == "saved-sensor-search-result.json"]
            if len(artifacts) != 1 or (
                service.registry.blob_path(artifacts[0]["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Saved sensor search result differs from its artifact.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/saved-sensor-search/template", tags=["saved_sensor_search"])
    def template(q: str = ""):
        from .sensor_db_bridge import sensor_revision_catalog

        return {
            "specification": default_spec(),
            "sensor_catalog": sensor_revision_catalog(root=_db_root(service), query=q or None),
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/saved-sensor-search", status_code=202, tags=["saved_sensor_search"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/saved-sensor-search", tags=["saved_sensor_search"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/saved-sensor-search/{identity}", tags=["saved_sensor_search"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/saved-sensor-search/{identity}/cancel", tags=["saved_sensor_search"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/saved-sensor-search/{identity}/export", tags=["saved_sensor_search"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="saved-sensor-search.json"'},
        )
