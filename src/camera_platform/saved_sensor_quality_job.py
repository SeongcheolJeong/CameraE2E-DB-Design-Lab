"""Worker/API lifecycle for quality diagnostics of pinned saved sensor runs."""

from __future__ import annotations

import hashlib
import math
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_saved_sensor_quality_study_v1"
JOB_KIND = "saved_sensor_quality"
SCENES = frozenset({"uniform", "slanted_edge", "macbeth_d65", "hdr_chart_d65"})
LIMITATION = (
    "Saved sensor simulations are evaluated with their pinned native response and "
    "readout under a fixed ideal optical configuration. Edge MTF is a system-model "
    "diagnostic, not intrinsic sensor MTF; Macbeth residuals use the same patches "
    "for calibration and evaluation. Pixel grids are not matched physical fields "
    "of view across sensors. Results are model sensitivities, not product rankings, "
    "measured accuracy, or physical validation."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "저장 센서 run 화질 진단",
        "sensor_revisions": [],
        "scenes": ["uniform", "slanted_edge"],
        "luminance_cd_m2": 1000.0,
        "exposure_s": 0.004,
        "frames": 2,
        "output_size": [32, 32],
        "seed": 7,
        "acknowledge_simulation_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the saved sensor quality study fields.")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit saved-run simulation-limit acknowledgement required.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Study name requires 1..160 characters.")
    revisions = payload["sensor_revisions"]
    if (
        not isinstance(revisions, list)
        or not 1 <= len(revisions) <= 2
        or any(
            not isinstance(item, dict)
            or set(item) != {"identifier", "run_id"}
            or any(not isinstance(item[key], str) or not 1 <= len(item[key].strip()) <= 128
                   for key in ("identifier", "run_id"))
            for item in revisions
        )
        or len({item["run_id"] for item in revisions}) != len(revisions)
    ):
        raise ValueError("Select one or two distinct saved sensor run IDs.")
    scenes = payload["scenes"]
    if (
        not isinstance(scenes, list)
        or not 1 <= len(scenes) <= 3
        or any(not isinstance(item, str) for item in scenes)
        or len(set(scenes)) != len(scenes)
        or set(scenes) - SCENES
    ):
        raise ValueError(f"Scenes must be distinct values from {sorted(SCENES)}.")
    size = payload["output_size"]
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(type(item) is not int or not 16 <= item <= 32 for item in size)
    ):
        raise ValueError("Output size must be two integer dimensions from 16 to 32.")
    if "hdr_chart_d65" in scenes and min(size) < 32:
        raise ValueError("HDR chart strip diagnostics require at least 32x32 output.")
    if type(payload["frames"]) is not int or not 2 <= payload["frames"] <= 4:
        raise ValueError("Frames must be an integer from 2 to 4.")
    if type(payload["seed"]) is not int or not 0 <= payload["seed"] < 2**32:
        raise ValueError("Seed must be a uint32 integer.")
    for key, low, high in (
        ("luminance_cd_m2", 0.01, 10000.0),
        ("exposure_s", 1e-6, 1.0),
    ):
        value = payload[key]
        if type(value) not in (int, float):
            raise ValueError(f"{key} must be finite and within [{low}, {high}].")
        try:
            number = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"{key} must be finite and within [{low}, {high}].") from exc
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"{key} must be finite and within [{low}, {high}].")
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["luminance_cd_m2"] = float(result["luminance_cd_m2"])
    result["exposure_s"] = float(result["exposure_s"])
    return result


def _db_root(service: Any) -> Path:
    return Path(service.workspace) / "camerae2e_db"


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Pin the *saved run*, not a newly generated catalog estimate."""
    from pyisetcam.sensor_simulation_db import (
        sensor_simulation_db_config,
        sensor_simulation_db_get,
    )

    from .imaging import _current
    from .offline_assets import offline_asset_identity
    from .sensor_db_bridge import inspect_sensor_revision

    spec = specification(payload)
    sources = [
        inspect_sensor_revision(item["identifier"], root=_db_root(service), run_id=item["run_id"])
        for item in spec["sensor_revisions"]
    ]
    for source in sources:
        if not source["executable"]:
            raise ValueError(f"Saved sensor run cannot execute: {source['reason']}")
        revision = source["revision"]
        saved = sensor_simulation_db_get(
            revision["report_code"], root=_db_root(service), run_id=revision["run_id"]
        )
        settings = saved["metadata"].get("simulation_settings") or {}
        scenario = sensor_simulation_db_config(
            revision["report_code"], root=_db_root(service), run_id=revision["run_id"]
        )["scenario"]
        if settings.get("fnumber") != 4.0 or settings.get("focal_length_m") != 0.004:
            raise ValueError("Saved quality requires a saved F/4, 4 mm optics contract.")
        if scenario.get("sensor_output_mode") != "digital_raw":
            raise ValueError("Saved quality requires a saved digital_raw output contract.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source_identities": [source["revision"] for source in sources],
        "upstream_assets": offline_asset_identity(workspace=service.workspace),
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .saved_sensor_quality import evaluate_saved_sensor_quality

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Saved sensor quality study is not running or was cancelled.")
    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen saved run, assets, request or executable changed.")
    spec = fresh["specification"]
    cases = []
    artifacts = []
    with tempfile.TemporaryDirectory(prefix="camera-saved-quality-") as temporary:
        for index, selected in enumerate(spec["sensor_revisions"], start=1):
            output = Path(temporary) / f"sensor-{index}"
            report = evaluate_saved_sensor_quality(
                selected["identifier"],
                run_id=selected["run_id"],
                root=_db_root(service),
                workspace=service.workspace,
                output_dir=output,
                scenes=spec["scenes"],
                luminance_cd_m2=spec["luminance_cd_m2"],
                exposure_s=spec["exposure_s"],
                frames=spec["frames"],
                output_size=tuple(spec["output_size"]),
                seed=spec["seed"],
            )
            expected = fresh["source_identities"][index - 1]
            if any(report["revision"][key] != expected[key]
                   for key in ("run_id", "result_sha256", "config_sha256")):
                raise IntegrityError("Quality report differs from pinned saved run.")
            if report["upstream_assets"] != fresh["upstream_assets"]:
                raise IntegrityError("Pinned upstream assets changed during quality study.")
            saved = {}
            for kind, media in (("json", "application/json"), ("npz", "application/octet-stream")):
                content = Path(report["artifacts"][kind]).read_bytes()
                if hashlib.sha256(content).hexdigest() != report["artifact_sha256"][kind]:
                    raise IntegrityError(f"Computed saved sensor {kind} artifact differs.")
                descriptor = service.registry.store_blob(
                    content, f"sensor-{index}-saved-quality.{kind}", media
                )
                artifacts.append(descriptor)
                saved[kind] = descriptor
            report = {key: value for key, value in report.items()
                      if key not in {"artifacts", "artifact_sha256"}}
            report["artifacts"] = saved
            cases.append(report)
        after = prepare(service, spec)
        if canonical(after) != canonical(fresh):
            raise IntegrityError("Saved run, assets or executable changed during quality study.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Saved sensor quality study was cancelled during execution.")
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_identities": fresh["source_identities"],
        "upstream_assets": fresh["upstream_assets"],
        "sensor_reports": cases,
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitation": LIMITATION,
    }
    artifacts.append(service.registry.store_blob(
        canonical(summary).encode(), "saved-sensor-quality-result.json", "application/json"
    ))
    return {"summary": summary, "artifacts": artifacts}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a saved sensor quality study.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        if (canonical(summary["specification"]) != canonical(job["specification"])
                or canonical(summary["source_identities"]) != canonical(job["source_identities"])
                or canonical(summary["upstream_assets"]) != canonical(job["upstream_assets"])):
            raise IntegrityError("Saved sensor quality result differs from its frozen request.")
        if job["status"] == "succeeded":
            for report in summary["sensor_reports"]:
                for descriptor in report["artifacts"].values():
                    if descriptor not in job["artifacts"]:
                        raise IntegrityError("Saved quality evidence link is missing.")
            for descriptor in job["artifacts"]:
                content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != descriptor["sha256"]:
                    raise IntegrityError("Saved quality artifact bytes differ.")
            result = [item for item in job["artifacts"]
                      if item["name"] == "saved-sensor-quality-result.json"]
            if len(result) != 1 or (
                service.registry.blob_path(result[0]["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Saved quality result differs from its artifact.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/saved-sensor-quality/template", tags=["saved_sensor_quality"])
    def template(q: str = ""):
        from .sensor_db_bridge import sensor_revision_catalog

        return {
            "specification": default_spec(),
            "sensor_catalog": sensor_revision_catalog(root=_db_root(service), query=q or None),
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/saved-sensor-quality", status_code=202, tags=["saved_sensor_quality"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/saved-sensor-quality", tags=["saved_sensor_quality"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/saved-sensor-quality/{identity}", tags=["saved_sensor_quality"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/saved-sensor-quality/{identity}/cancel", tags=["saved_sensor_quality"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/saved-sensor-quality/{identity}/export", tags=["saved_sensor_quality"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="saved-sensor-quality.json"'},
        )
