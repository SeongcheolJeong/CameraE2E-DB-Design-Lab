"""Durable, source-bound HDR reconstruction of a saved Lens x Sensor PWM study.

This replays the original 2 x 4 phase RAW bracket. It performs no optical
solve or new sensor capture and makes no product LFM or physical claim.
"""

from __future__ import annotations

import hashlib
import io
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from .lens_sensor_pwm_hdr import fuse_lens_sensor_pwm_hdr
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_pwm_hdr_study_v1"
JOB_KIND = "lens_sensor_pwm_hdr"
LIMITATION = (
    "A saved Lens x Sensor PWM/LFM 2-exposure x 4-phase integer RAW artifact is "
    "inverted using declared rectangular PWM row timing and model-generated "
    "virtual-window FPN maps. Pixels reconstructed from one bracket are labeled "
    "fallback, separate from two-bracket fusion. The scene is static and D65; "
    "the native CFA tile is anchored at virtual origin zero but the die crop is "
    "unknown. This is model-internal HDR reconstruction, not measured HDR/LFM, "
    "moving-scene fusion, physical validation or product approval."
)
SOURCE_CAPTURE_MODULES = (
    "pyisetcam/sensor.py",
    "pyisetcam/sensor_capture.py",
    "pyisetcam/sensor_estimate.py",
    "pyisetcam/sensor_simulation_db.py",
    "pyisetcam/sensor_readout_profiles.py",
    "pyisetcam/system_faca.py",
    "camera_platform/lens_sensor_hdr_bridge.py",
    "camera_platform/lens_sensor_lfm.py",
    "camera_platform/lens_sensor_lfm_job.py",
    "camera_platform/saved_sensor_lfm.py",
)
TIMED_SOURCE_MODULES = SOURCE_CAPTURE_MODULES + (
    "camera_platform/lens_sensor_timing_mode.py",
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "렌즈 × 센서 저장 PWM RAW HDR 복원",
        "source_lfm_job_id": "",
        "acknowledge_simulation_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the saved PWM HDR study fields.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported saved PWM HDR study schema.")
    if payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Acknowledge authored timing and model-only HDR limits explicitly.")
    name = payload["name"]
    identity = payload["source_lfm_job_id"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("PWM HDR study name requires 1..160 characters.")
    if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 128:
        raise ValueError("Choose one completed saved Lens x Sensor PWM/LFM Job ID.")
    selected = deepcopy(payload)
    selected["name"] = name.strip()
    selected["source_lfm_job_id"] = identity.strip()
    return selected


def _db_root(service: Any) -> Path:
    return Path(service.workspace) / "camerae2e_db"


def _source_contract(service: Any, identity: str) -> dict[str, Any]:
    from pyisetcam.sensor import pixel_create
    from pyisetcam.sensor_simulation_db import (
        _digest,
        sensor_simulation_db_config,
        sensor_simulation_db_get,
    )

    from . import lens_sensor_lfm_job

    source = lens_sensor_lfm_job.get(service, identity)
    if source["status"] != "succeeded":
        raise ValueError("PWM HDR requires a succeeded saved Lens x Sensor PWM/LFM Job.")
    if source.get("plugin_id") not in {
        lens_sensor_lfm_job.SCHEMA, lens_sensor_lfm_job.BOUNDED_SCHEMA,
        lens_sensor_lfm_job.TIMED_BOUNDED_SCHEMA,
    }:
        raise IntegrityError("Saved PWM/LFM Job has an unsupported capture schema.")
    result = source["result"]["summary"]
    report = result["report"]
    expected_report_schema = (
        "camerae2e_lens_sensor_pwm_lfm_v3"
        if source["plugin_id"] == lens_sensor_lfm_job.TIMED_BOUNDED_SCHEMA else
        "camerae2e_lens_sensor_pwm_lfm_v2"
        if source["plugin_id"] == lens_sensor_lfm_job.BOUNDED_SCHEMA
        else "camerae2e_lens_sensor_pwm_lfm_v1"
    )
    if (
        report.get("schema") != expected_report_schema
        or result.get("schema") != source["plugin_id"]
        or report["lens_identity"] != source["source_identities"]["lens"]
        or report["sensor_revision"] != source["source_identities"]["sensor"]
        or report["upstream_assets"] != source["upstream_assets"]
        or report["geometry_provenance"]["saved_native_cfa"] != source["native_cfa"]
    ):
        raise IntegrityError("Saved PWM/LFM report lost its frozen Lens/Sensor binding.")
    refreshed = lens_sensor_lfm_job.prepare(service, source["specification"])
    for key in ("source_identities", "native_cfa", "upstream_assets"):
        if canonical(refreshed[key]) != canonical(source[key]):
            raise IntegrityError(f"Saved PWM/LFM {key} changed since capture.")
    original_hashes = source["environment"]["source_hashes"]
    current_hashes = refreshed["environment"]["source_hashes"]
    capture_modules = (
        TIMED_SOURCE_MODULES
        if source["plugin_id"] == lens_sensor_lfm_job.TIMED_BOUNDED_SCHEMA
        else SOURCE_CAPTURE_MODULES
    )
    if any(
        original_hashes.get(name) is None or original_hashes[name] != current_hashes.get(name)
        for name in capture_modules
    ):
        raise IntegrityError(
            "Saved PWM/LFM capture implementation changed; exact FPN replay is unavailable."
        )
    capture_hashes = {name: original_hashes[name] for name in capture_modules}
    revision = source["source_identities"]["sensor"]
    root = _db_root(service)
    stored = sensor_simulation_db_get(revision["report_code"], root=root, run_id=revision["run_id"])
    metadata = stored["metadata"]
    if _digest(metadata["config"]) != revision["config_sha256"]:
        raise IntegrityError("Saved Sensor config differs from its pinned revision.")
    readout = metadata["model_snapshot"]["readout"]
    profile = metadata["readout_profile"]
    fpn = profile["fixed_pattern"]
    if (
        readout != report["controls"]["saved_readout"]
        or profile["readout"] != readout
        or fpn != report["controls"]["saved_fpn_parameter_assumptions"]
        or metadata["native_cfa"] != source["native_cfa"]
    ):
        raise IntegrityError("Saved Sensor readout, FPN or native CFA differs from source RAW.")
    scenario = sensor_simulation_db_config(
        revision["report_code"], root=root, run_id=revision["run_id"]
    )["scenario"]
    if (
        scenario.get("sensor_output_mode") != "digital_raw"
        or scenario.get("sensor_illumination") is not None
    ):
        raise ValueError("PWM HDR requires the original non-angular digital RAW Sensor model.")
    overrides = scenario.get("sensor", {})
    if not isinstance(overrides, dict) or set(overrides) - {
        "integration_time",
        "noise_flag",
        "fixed_pattern_seed",
    }:
        raise ValueError("PWM HDR cannot prove conversion gain for this saved Sensor override set.")
    for bucket in ("parameters", "camera_parameters"):
        values = scenario.get(bucket, {})
        if isinstance(values, dict) and any(
            str(key).replace("_", " ").lower().startswith("sensor ") for key in values
        ):
            raise ValueError(
                "PWM HDR cannot prove conversion gain under Sensor parameter overrides."
            )
    voltage_swing = float(pixel_create("default")["voltage_swing"])
    full_well = float(readout["full_well_e"])
    conversion = voltage_swing / full_well
    if not math.isfinite(conversion) or conversion <= 0:
        raise ValueError("PWM HDR could not establish model conversion gain.")
    evidence = report["artifacts"]
    if set(evidence) != {"json", "npz"} or any(
        descriptor not in source["artifacts"] for descriptor in evidence.values()
    ):
        raise IntegrityError("Saved PWM/LFM evidence is incomplete.")
    result_descriptor = [
        item for item in source["artifacts"] if item["name"] == "lens-sensor-lfm-result.json"
    ]
    if len(result_descriptor) != 1:
        raise IntegrityError("Saved PWM/LFM result artifact is missing.")
    return {
        "source_lfm_job_id": identity,
        "source_lfm_content_hash": source["content_hash"],
        "source_lfm_result_sha256": result_descriptor[0]["sha256"],
        "source_lfm_report_sha256": digest(report),
        "source_capture_implementation_sha256": capture_hashes,
        "source_lfm_evidence": evidence,
        "source_identities": source["source_identities"],
        "native_cfa": source["native_cfa"],
        "saved_readout": readout,
        "saved_fpn_parameters": fpn,
        "upstream_assets": source["upstream_assets"],
        "conversion_gain_v_per_e": conversion,
        "conversion_gain_provenance": {
            "kind": "model_default_voltage_swing_divided_by_saved_full_well",
            "model_default_voltage_swing_v": voltage_swing,
            "saved_full_well_e": full_well,
            "saved_sensor_overrides": overrides,
            "physical_validation": "not_established",
        },
    }


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    source = _source_contract(service, spec["source_lfm_job_id"])
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        **source,
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from . import lens_sensor_lfm_job

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Saved PWM HDR Job is not running or was cancelled.")
    frozen = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in frozen.items()):
        raise IntegrityError("Frozen PWM HDR source, calibration or executable changed.")
    source = lens_sensor_lfm_job.get(service, frozen["source_lfm_job_id"])
    source_report = source["result"]["summary"]["report"]
    source_npz = service.registry.blob_path(frozen["source_lfm_evidence"]["npz"]["sha256"])
    with np.load(source_npz, allow_pickle=False) as archive:
        if set(archive.files) != {
            "original_pwm_raw_dn",
            "original_constant_raw_dn",
            "integrated_mean_electrons",
        }:
            raise IntegrityError("Saved PWM/LFM array artifact has unexpected content.")
        original_raw = np.asarray(archive["original_pwm_raw_dn"]).copy()
    result = fuse_lens_sensor_pwm_hdr(
        original_raw,
        source_report,
        conversion_gain_v_per_e=frozen["conversion_gain_v_per_e"],
    )
    report = deepcopy(result["report"])
    arrays = result["arrays"]
    if (
        report["lens_identity"] != frozen["source_identities"]["lens"]
        or report["sensor_revision"] != frozen["source_identities"]["sensor"]
        or report["geometry_provenance"]["saved_native_cfa"] != frozen["native_cfa"]
        or report["upstream_assets"] != frozen["upstream_assets"]
    ):
        raise IntegrityError("PWM HDR reconstruction lost its frozen Lens/Sensor source.")
    report["source_lfm_job_id"] = frozen["source_lfm_job_id"]
    report["source_lfm_result_sha256"] = frozen["source_lfm_result_sha256"]
    report["source_lfm_report_sha256"] = frozen["source_lfm_report_sha256"]
    report["source_capture_implementation_sha256"] = frozen["source_capture_implementation_sha256"]
    report["source_lfm_evidence"] = frozen["source_lfm_evidence"]
    report["conversion_gain_provenance"] = frozen["conversion_gain_provenance"]
    if canonical(prepare(service, job["specification"])) != canonical(frozen):
        raise IntegrityError("Frozen PWM HDR source or executable changed during replay.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Saved PWM HDR Job was cancelled during replay.")
    stream = io.BytesIO()
    np.savez_compressed(stream, **arrays)
    npz = service.registry.store_blob(
        stream.getvalue(), "lens-sensor-pwm-hdr-evidence.npz", "application/octet-stream"
    )
    report["evidence_npz"] = npz
    json_artifact = service.registry.store_blob(
        canonical(report).encode(), "lens-sensor-pwm-hdr-evidence.json", "application/json"
    )
    summary = {
        "schema": SCHEMA,
        "status": report["status"],
        "completion_scope": report["completion_scope"],
        "fused_hdr_pixels": report["fused_hdr_pixels"],
        "single_bracket_fallback_pixels": report["single_bracket_fallback_pixels"],
        "invalid_pixels": report["invalid_pixels"],
        "specification": frozen["specification"],
        "source_lfm_job_id": frozen["source_lfm_job_id"],
        "source_lfm_result_sha256": frozen["source_lfm_result_sha256"],
        "source_lfm_report_sha256": frozen["source_lfm_report_sha256"],
        "source_capture_implementation_sha256": frozen["source_capture_implementation_sha256"],
        "source_lfm_evidence": frozen["source_lfm_evidence"],
        "source_identities": frozen["source_identities"],
        "native_cfa": frozen["native_cfa"],
        "saved_readout": frozen["saved_readout"],
        "saved_fpn_parameters": frozen["saved_fpn_parameters"],
        "upstream_assets": frozen["upstream_assets"],
        "conversion_gain_provenance": frozen["conversion_gain_provenance"],
        "report": report,
        "evidence": {"json": json_artifact, "npz": npz},
        "limitation": LIMITATION,
    }
    result_artifact = service.registry.store_blob(
        canonical(summary).encode(), "lens-sensor-pwm-hdr-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [npz, json_artifact, result_artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    from . import lens_sensor_lfm_job

    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected Job is not a saved Lens x Sensor PWM HDR study.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        if any(
            summary[key] != summary["report"][key]
            for key in (
                "status",
                "completion_scope",
                "fused_hdr_pixels",
                "single_bracket_fallback_pixels",
                "invalid_pixels",
            )
        ):
            raise IntegrityError("Saved PWM HDR summary differs from reconstruction coverage.")
        for field in (
            "specification",
            "source_lfm_job_id",
            "source_lfm_result_sha256",
            "source_lfm_report_sha256",
            "source_lfm_evidence",
            "source_identities",
            "source_capture_implementation_sha256",
            "native_cfa",
            "saved_readout",
            "saved_fpn_parameters",
            "upstream_assets",
            "conversion_gain_provenance",
        ):
            if canonical(summary[field]) != canonical(job[field]):
                raise IntegrityError(f"Saved PWM HDR result differs from frozen {field}.")
        if job["status"] == "succeeded":
            source = lens_sensor_lfm_job.get(service, job["source_lfm_job_id"])
            if (
                source["status"] != "succeeded"
                or source["content_hash"] != job["source_lfm_content_hash"]
                or digest(source["result"]["summary"]["report"]) != job["source_lfm_report_sha256"]
                or source["result"]["summary"]["report"]["artifacts"] != job["source_lfm_evidence"]
            ):
                raise IntegrityError(
                    "Saved PWM HDR source LFM Job is no longer its frozen artifact."
                )
            descriptors = summary["evidence"]
            if set(descriptors) != {"json", "npz"} or any(
                descriptor not in job["artifacts"] for descriptor in descriptors.values()
            ):
                raise IntegrityError("Saved PWM HDR evidence link is missing.")
            by_name = {item["name"]: item for item in job["artifacts"]}
            result_artifact = by_name.get("lens-sensor-pwm-hdr-result.json")
            if result_artifact is None:
                raise IntegrityError("Saved PWM HDR result artifact is missing.")
            for descriptor in job["artifacts"]:
                content = service.registry.blob_path(descriptor["sha256"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != descriptor["sha256"]:
                    raise IntegrityError("Saved PWM HDR evidence bytes changed.")
            if (
                service.registry.blob_path(result_artifact["sha256"]).read_bytes()
                != canonical(summary).encode()
                or service.registry.blob_path(descriptors["json"]["sha256"]).read_bytes()
                != canonical(summary["report"]).encode()
            ):
                raise IntegrityError("Saved PWM HDR report differs from its artifact.")
            with np.load(
                service.registry.blob_path(descriptors["npz"]["sha256"]), allow_pickle=False
            ) as archive:
                raw = np.asarray(archive["original_pwm_raw_dn"])
                checks = summary["report"]["array_sha256"]
                if (
                    raw.dtype != np.dtype("int32")
                    or hashlib.sha256(raw.tobytes(order="C")).hexdigest()
                    != summary["report"]["original_pwm_raw_sha256"]
                ):
                    raise IntegrityError("Saved original PWM RAW differs from HDR fusion input.")
                for name, checksum in checks.items():
                    array = np.asarray(archive[name])
                    if (
                        hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()
                        != checksum
                    ):
                        raise IntegrityError(f"Saved PWM HDR {name} differs from its report.")
                with np.load(
                    service.registry.blob_path(job["source_lfm_evidence"]["npz"]["sha256"]),
                    allow_pickle=False,
                ) as source_archive:
                    if not np.array_equal(raw, source_archive["original_pwm_raw_dn"]):
                        raise IntegrityError(
                            "Saved PWM HDR original RAW differs from the source LFM artifact."
                        )
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-pwm-hdr/template", tags=["lens_sensor_pwm_hdr"])
    def template():
        from .lens_sensor_lfm_job import list_records as list_lfm

        return {
            "specification": default_spec(),
            "source_lfm_jobs": list_lfm(service),
            "limitation": LIMITATION,
        }

    @app.post("/api/v1/lens-sensor-pwm-hdr", status_code=202, tags=["lens_sensor_pwm_hdr"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-pwm-hdr", tags=["lens_sensor_pwm_hdr"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-pwm-hdr/{identity}", tags=["lens_sensor_pwm_hdr"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-pwm-hdr/{identity}/cancel", tags=["lens_sensor_pwm_hdr"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-pwm-hdr/{identity}/export", tags=["lens_sensor_pwm_hdr"])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ValueError("Saved Lens x Sensor PWM HDR result is not ready.")
        return Response(
            canonical(job),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="lens-sensor-pwm-hdr.json"'},
        )
