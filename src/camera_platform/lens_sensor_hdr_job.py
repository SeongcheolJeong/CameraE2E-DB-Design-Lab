"""Durable static Lens DB × saved Sensor DB RAW HDR bracket job."""

from __future__ import annotations

import hashlib
import io
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from .lens_sensor_hdr_bridge import BOUNDED_LIMITATION, LIMITATION
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_static_hdr_study_v1"
BOUNDED_SCHEMA = "camerae2e_lens_sensor_static_hdr_study_v2"
JOB_KIND = "lens_sensor_hdr"


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 저장 센서 정적 RAW HDR 브래킷",
        "lens_revision_id": "",
        "sensor_revision": {"identifier": "", "run_id": ""},
        "exposure_s": [0.0005, 0.008],
        "scene_kind": "slanted_edge",
        "sensor_size": [16, 16],
        "luminance_cd_m2": 1000.0,
        "seed": 7,
        "accept_approximate_lens_radiometry": False,
    }


def default_bounded_spec() -> dict[str, Any]:
    return {
        **default_spec(),
        "schema": BOUNDED_SCHEMA,
        "name": "렌즈 × 저장 센서 정적 RAW HDR bounded strict PSF",
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    from .lens_sensor_raw_bridge import _number
    from .saved_sensor_hdr import _exposures

    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the Lens × Sensor static HDR study fields.")
    if payload["schema"] not in {SCHEMA, BOUNDED_SCHEMA}:
        raise ValueError("Unsupported Lens × Sensor static HDR schema.")
    if payload["accept_approximate_lens_radiometry"] is not True:
        raise ValueError("Explicit approximate lens-radiometry acceptance is required.")
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("HDR study name requires 1..160 characters.")
    lens_id = payload["lens_revision_id"]
    if not isinstance(lens_id, str) or not 1 <= len(lens_id.strip()) <= 128:
        raise ValueError("Choose one explicit Lens DB revision ID.")
    sensor = payload["sensor_revision"]
    if (
        not isinstance(sensor, dict)
        or set(sensor) != {"identifier", "run_id"}
        or any(
            not isinstance(sensor[key], str) or not 1 <= len(sensor[key].strip()) <= 128
            for key in ("identifier", "run_id")
        )
    ):
        raise ValueError("Choose one explicit saved sensor identifier and run ID.")
    exposures = _exposures(payload["exposure_s"])
    if len(exposures) != 2:
        raise ValueError("Lens × Sensor static HDR requires exactly two exposures.")
    if payload["scene_kind"] not in {"uniform", "slanted_edge"}:
        raise ValueError("scene_kind must be uniform or slanted_edge.")
    size = payload["sensor_size"]
    if (
        not isinstance(size, list) or len(size) != 2
        or any(type(value) is not int or value < 8 or value > 32 or value % 2 for value in size)
    ):
        raise ValueError("sensor_size needs two even integer dimensions from 8 to 32.")
    if type(payload["seed"]) is not int or not 0 <= payload["seed"] < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    selected = deepcopy(payload)
    selected["name"] = name.strip()
    selected["lens_revision_id"] = lens_id.strip()
    selected["sensor_revision"] = {key: value.strip() for key, value in sensor.items()}
    selected["exposure_s"] = exposures
    selected["luminance_cd_m2"] = _number(
        payload["luminance_cd_m2"], "luminance_cd_m2", 0.01, 10000.0
    )
    return selected


def _db_root(service: Any) -> Path:
    return Path(service.workspace) / "camerae2e_db"


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from pyisetcam.sensor_simulation_db import (
        _digest,
        sensor_simulation_db_config,
        sensor_simulation_db_get,
    )

    from .imaging import _current
    from .lens_catalog_bridge import geometric_lens_preview
    from .lens_sensor_raw_bridge import (
        bounded_psf_resource_preflight,
        require_bounded_psf_budget,
    )
    from .offline_assets import offline_asset_identity
    from .saved_sensor_hdr import _FPN_FIELDS
    from .sensor_db_bridge import inspect_sensor_revision

    spec = specification(payload)
    root = _db_root(service)
    lens = geometric_lens_preview(spec["lens_revision_id"], catalog_root=root)
    chosen = spec["sensor_revision"]
    source = inspect_sensor_revision(
        chosen["identifier"], root=root, run_id=chosen["run_id"]
    )
    if not source["executable"]:
        raise ValueError(f"Saved sensor run cannot execute: {source['reason']}")
    if source["native_response"]["kind"] != "db_estimate_v1":
        raise ValueError(
            "Joint HDR supports db_estimate_v1 only; physical/FDTD sensors need "
            "a matched lens pupil and CRA contract."
        )
    resource_preflight = None
    if spec["schema"] == BOUNDED_SCHEMA:
        resource_preflight = bounded_psf_resource_preflight(
            lens["model"], source["native_response"]["pixel_pitch_um"],
            spec["sensor_size"],
        )
        require_bounded_psf_budget(resource_preflight)
    revision = source["revision"]
    saved = sensor_simulation_db_get(
        revision["report_code"], root=root, run_id=revision["run_id"]
    )
    metadata = saved["metadata"]
    if _digest(metadata["config"]) != revision["config_sha256"]:
        raise ValueError("Saved sensor config differs from its pinned run.")
    cfa = metadata.get("native_cfa") or {}
    model = metadata["model_snapshot"]
    native_size = cfa.get("native_size")
    if (
        cfa.get("pattern_index_base") != 1
        or cfa.get("filter_names") != model.get("filter_names")
        or cfa.get("pixel_pitch_um") != model.get("pixel_pitch_um")
        or not isinstance(native_size, list)
        or len(native_size) != 2
        or any(type(value) is not int or value <= 0 or value % 2 for value in native_size)
    ):
        raise ValueError("Saved sensor native CFA differs from its model snapshot.")
    if (
        cfa.get("pattern") != [[1, 2], [2, 3]]
        or cfa.get("filter_names") != ["red", "green", "blue"]
        or model.get("cfa_pattern") != "rggb_bayer"
    ):
        raise ValueError(
            "Joint HDR requires an exact native 2x2 RGGB saved CFA; "
            "Quad Bayer and other mosaics need a CFA-aware HDR merger."
        )
    fpn = (metadata.get("readout_profile") or {}).get("fixed_pattern") or {}
    if set(fpn) != set(_FPN_FIELDS) or any(
        type(value) not in (int, float) or not math.isfinite(float(value))
        for value in fpn.values()
    ):
        raise ValueError("Saved HDR needs explicit finite four-field FPN provenance.")
    scenario = sensor_simulation_db_config(
        revision["report_code"], root=root, run_id=revision["run_id"]
    )["scenario"]
    if scenario.get("sensor_output_mode") != "digital_raw":
        raise ValueError("Saved sensor needs a digital_raw output contract.")
    if scenario.get("sensor_illumination") is not None:
        raise ValueError("Saved angular illumination cannot be reused with a new lens.")
    frozen = {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": spec["schema"],
        "specification": spec,
        "source_identities": {"lens": lens["identity"], "sensor": revision},
        "saved_readout": metadata["model_snapshot"]["readout"],
        "native_cfa": cfa,
        "upstream_assets": offline_asset_identity(workspace=service.workspace, source=root),
        "environment": _current(service),
        "limitation": BOUNDED_LIMITATION if spec["schema"] == BOUNDED_SCHEMA else LIMITATION,
    }
    if spec["schema"] == BOUNDED_SCHEMA:
        frozen["resource_preflight"] = resource_preflight
    return frozen


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .lens_sensor_hdr_bridge import evaluate_lens_sensor_hdr_bracket
    from .offline_assets import offline_asset_identity

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens × Sensor HDR job is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in frozen.items()):
        raise IntegrityError("Frozen lens, sensor, assets or executable changed.")
    spec = frozen["specification"]
    sensor = spec["sensor_revision"]
    result = evaluate_lens_sensor_hdr_bracket(
        spec["lens_revision_id"], sensor["identifier"],
        sensor_run_id=sensor["run_id"], root=_db_root(service),
        workspace=service.workspace, exposure_s=spec["exposure_s"],
        scene_kind=spec["scene_kind"], sensor_size=spec["sensor_size"],
        luminance_cd_m2=spec["luminance_cd_m2"], seed=spec["seed"],
        accept_approximate_lens_radiometry=True,
        psf_compute_mode=(
            "bounded_streaming" if spec["schema"] == BOUNDED_SCHEMA else "precomputed"
        ),
    )
    report, arrays = result["report"], result["arrays"]
    if (
        report["lens_identity"] != frozen["source_identities"]["lens"]
        or report["sensor_revision"] != frozen["source_identities"]["sensor"]
        or report["controls"]["readout"] != frozen["saved_readout"]
        or report["upstream_assets"] != frozen["upstream_assets"]
    ):
        raise IntegrityError("Computed HDR bracket lost its pinned source/readout binding.")
    if spec["schema"] == BOUNDED_SCHEMA and (
        report.get("schema") != "camerae2e_lens_sensor_static_hdr_v2"
        or canonical(report.get("resource_preflight"))
        != canonical(frozen["resource_preflight"])
        or report.get("psf_compute_mode") != "bounded_streaming"
        or not _valid_bounded_execution(report.get("psf_execution"))
    ):
        raise IntegrityError("Computed HDR bounded PSF execution lost its frozen contract.")
    if offline_asset_identity(workspace=service.workspace, source=_db_root(service)) != (
        frozen["upstream_assets"]
    ) or canonical(prepare(service, spec)) != canonical(frozen):
        raise IntegrityError("Pinned lens, sensor or executable changed during HDR capture.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens × Sensor HDR job was cancelled during execution.")
    original_raw = np.asarray(arrays["original_raw_dn"])
    if original_raw.dtype != np.dtype("int32") or original_raw.shape != (
        2, *spec["sensor_size"]
    ) or hashlib.sha256(original_raw.tobytes(order="C")).hexdigest() != (
        report["fusion"]["input_array_bytes_sha256"]
    ):
        raise IntegrityError("Original RAW array differs from the fused input.")
    stream = io.BytesIO()
    np.savez_compressed(stream, **arrays)
    npz = service.registry.store_blob(
        stream.getvalue(), "lens-sensor-hdr-evidence.npz", "application/octet-stream"
    )
    report = deepcopy(report)
    report["evidence_npz"] = npz
    json_artifact = service.registry.store_blob(
        canonical(report).encode(), "lens-sensor-hdr-evidence.json", "application/json"
    )
    summary = {
        "schema": spec["schema"],
        "status": report["status"],
        "specification": spec,
        "source_identities": frozen["source_identities"],
        "saved_readout": frozen["saved_readout"],
        "native_cfa": frozen["native_cfa"],
        "upstream_assets": frozen["upstream_assets"],
        "report": report,
        "evidence": {"json": json_artifact, "npz": npz},
        "limitation": frozen["limitation"],
    }
    if spec["schema"] == BOUNDED_SCHEMA:
        summary["resource_preflight"] = frozen["resource_preflight"]
    result_artifact = service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-hdr-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [npz, json_artifact, result_artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens × Sensor static HDR bracket.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        for key in (
            "specification", "source_identities", "saved_readout",
            "native_cfa", "upstream_assets",
        ):
            if canonical(summary[key]) != canonical(job[key]):
                raise IntegrityError(
                    f"Saved Lens × Sensor HDR {key} differs from its frozen request."
                )
        if summary["schema"] == BOUNDED_SCHEMA and (
            summary.get("resource_preflight") != job.get("resource_preflight")
            or summary.get("resource_preflight")
            != summary["report"].get("resource_preflight")
            or summary["report"].get("schema") != "camerae2e_lens_sensor_static_hdr_v2"
            or summary["report"].get("psf_compute_mode") != "bounded_streaming"
            or not _valid_bounded_execution(summary["report"].get("psf_execution"))
        ):
            raise IntegrityError("Saved HDR bounded PSF execution evidence is missing.")
        if job["status"] == "succeeded":
            descriptors = summary["evidence"]
            if len(job["artifacts"]) != 3 or any(
                descriptor not in job["artifacts"] for descriptor in descriptors.values()
            ):
                raise IntegrityError("Saved Lens × Sensor HDR evidence link is missing.")
            by_name = {item["name"]: item for item in job["artifacts"]}
            result_artifact = by_name.get("lens-sensor-hdr-result.json")
            if result_artifact is None:
                raise IntegrityError("Saved Lens × Sensor HDR result artifact is missing.")
            for descriptor in job["artifacts"]:
                content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != descriptor["sha256"]:
                    raise IntegrityError("Saved Lens × Sensor HDR evidence bytes changed.")
            if service.registry.blob_path(result_artifact["sha256"]).read_bytes() != (
                canonical(summary).encode()
            ) or service.registry.blob_path(descriptors["json"]["sha256"]).read_bytes() != (
                canonical(summary["report"]).encode()
            ):
                raise IntegrityError("Saved Lens × Sensor HDR report differs from its artifact.")
            with np.load(
                service.registry.blob_path(descriptors["npz"]["sha256"]), allow_pickle=False
            ) as arrays:
                raw = np.asarray(arrays["original_raw_dn"])
            if raw.dtype != np.dtype("int32") or hashlib.sha256(
                raw.tobytes(order="C")
            ).hexdigest() != summary["report"]["fusion"]["input_array_bytes_sha256"]:
                raise IntegrityError("Saved original RAW differs from the HDR fusion input.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def _valid_bounded_execution(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("mode") == "strict_bounded_streaming"
        and value.get("complete_precomputed_stack") is False
        and type(value.get("peak_cached_kernels")) is int
        and 0 <= value["peak_cached_kernels"] <= 4
    )


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-hdr/template", tags=["lens_sensor_hdr"])
    def template(q: str = "", scope: str = "central"):
        from .lens_catalog_bridge import lens_revision_catalog
        from .sensor_db_bridge import sensor_revision_catalog

        if scope not in {"central", "central_bounded"}:
            raise ValueError("scope must be central or central_bounded.")
        return {
            "specification": (
                default_bounded_spec() if scope == "central_bounded" else default_spec()
            ),
            "lens_catalog": lens_revision_catalog(catalog_root=_db_root(service), query=q or None),
            "sensor_catalog": sensor_revision_catalog(root=_db_root(service), query=q or None),
            "limitation": BOUNDED_LIMITATION if scope == "central_bounded" else LIMITATION,
            "resource_contract": {
                "central_bounded_uses_heuristic_working_budget": True,
                "execution_guaranteed": False,
            },
        }

    @app.post("/api/v1/lens-sensor-hdr", status_code=202, tags=["lens_sensor_hdr"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-hdr", tags=["lens_sensor_hdr"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-hdr/{identity}", tags=["lens_sensor_hdr"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-hdr/{identity}/cancel", tags=["lens_sensor_hdr"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-hdr/{identity}/export", tags=["lens_sensor_hdr"])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ValueError("Lens × Sensor HDR result is not ready.")
        return Response(
            canonical(job),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="lens-sensor-hdr.json"'},
        )
