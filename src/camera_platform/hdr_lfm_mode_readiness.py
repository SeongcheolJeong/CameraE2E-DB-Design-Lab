"""Read-only, job-bound HDR/LFM mode and reference-evidence readiness.

The output names executable next API steps without promoting a synthetic or
geometric-PSF result to a physical product claim.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

import numpy as np

from .registry import IntegrityError, digest

SCHEMA = "camerae2e_hdr_lfm_mode_readiness_v1"
SUPPORTED = {
    "r1_timed_acquisition", "lens_sensor_hdr", "lens_sensor_lfm",
    "lens_sensor_pwm_hdr",
}


def _block(code: str, field: str, next_action: str) -> dict[str, str]:
    return {"code": code, "missing_evidence": field, "next_action": next_action}


def assess(service: Any, identity: str) -> dict[str, Any]:
    from . import (
        lens_sensor_hdr_job,
        lens_sensor_lfm_job,
        lens_sensor_pwm_hdr_job,
        r1_timed_acquisition,
    )

    raw_job = service.registry.get_run(identity)
    kind = raw_job.get("job_kind")
    if kind not in SUPPORTED:
        raise ValueError("Select an R1 or saved Lens x Sensor HDR/LFM RAW Job")
    getter = {
        "r1_timed_acquisition": r1_timed_acquisition.get,
        "lens_sensor_hdr": lens_sensor_hdr_job.get,
        "lens_sensor_lfm": lens_sensor_lfm_job.get,
        "lens_sensor_pwm_hdr": lens_sensor_pwm_hdr_job.get,
    }[kind]
    job = getter(service, identity)
    base = {
        "schema": SCHEMA,
        "job": {"id": identity, "kind": kind, "status": job["status"],
                "request_content_hash": job["content_hash"]},
        "model_execution": {"status": "not_completed"},
        "sensor_operating_point": None,
        "mode_execution": {"status": "not_established", "blockers": []},
        "reference_comparison": {"status": "not_available", "blockers": []},
        "product_hdr_lfm_decision": {"status": "not_established"},
        "next_workflows": [],
    }
    if job["status"] != "succeeded":
        base["mode_execution"]["blockers"].append(_block(
            "source_job_not_succeeded", "Completed source RAW and result artifacts",
            f"GET /api/v1/jobs/{identity}",
        ))
        return base

    summary = job["result"]["summary"]
    base["model_execution"] = {
        "status": "source_artifacts_hash_reverified",
        "result_summary_sha256": digest(summary),
    }
    if kind == "r1_timed_acquisition":
        base["model_execution"].update(
            raw_artifact_sha256=summary["raw_artifact"]["sha256"],
            raw_shape=summary["raw_artifact"]["shape"],
            hdr_fusion_applied=bool(summary["runtime"]["hdr_merge_applied"]),
            scope="synthetic_registered_r1_16x16_two_wavelength",
        )
        base["mode_execution"]["blockers"] += [
            _block(
                "r1_has_no_saved_sensor_operating_point",
                "Pinned real Sensor DB revision, operating point and executed mode",
                "GET /api/v1/lens-sensor-lfm/template?scope=central_bounded_timed",
            ),
            _block(
                "authored_r1_timing_and_waveform",
                "Independently sourced line/frame timing and measured waveform",
                "POST /api/v1/lens-sensor-lfm/timing-modes/template",
            ),
        ]
        base["reference_comparison"]["blockers"].append(_block(
            "r1_cannot_be_relabelled_physical_reference",
            "Independent physical RAW and ground-truth reference from an actual Sensor mode",
            "Select a saved Lens x Sensor timed PWM RAW/HDR Job first",
        ))
        base["next_workflows"].append({
            "kind": "select_saved_sensor_mode",
            "endpoint": "/api/v1/lens-sensor-lfm/template?scope=central_bounded_timed",
            "automatically_reuses_r1_as_product_evidence": False,
        })
        return base

    from pyisetcam.sensor_simulation_db import sensor_simulation_db_get

    from .sensor_db_bridge import inspect_sensor_revision

    revision = job["source_identities"]["sensor"]
    root = service.workspace / "camerae2e_db"
    sensor = inspect_sensor_revision(
        revision["report_code"], root=root, run_id=revision["run_id"]
    )
    if sensor["revision"] != revision:
        raise IntegrityError("Saved Sensor operating point differs from Job source")
    stored = sensor_simulation_db_get(
        revision["report_code"], root=root, run_id=revision["run_id"]
    )
    readout = stored["metadata"]["readout_profile"]
    provenance = readout["provenance"]
    conditions = provenance.get("conditions") or {}
    base["sensor_operating_point"] = {
        "revision": revision,
        "profile": readout["profile"],
        "operating_point": readout["operating_point"],
        "classification": provenance["classification"],
        "source_conditions": {
            "exposure_s": conditions.get("exposure_s"),
            "readout_rate_fps": conditions.get("readout_rate_fps"),
            "temperature_c": conditions.get("temperature_c"),
            "conversion_mode": conditions.get("conversion_mode"),
        },
        "result_sha256": revision["result_sha256"],
    }
    if kind == "lens_sensor_hdr":
        report = summary["report"]
        base["model_execution"].update(
            raw_artifact_sha256=summary["evidence"]["npz"]["sha256"],
            raw_array_sha256=report["fusion"]["input_array_bytes_sha256"],
            raw_shape=report["arrays"]["original_raw_dn"]["shape"],
            hdr_fusion_applied=True,
            scope="static_sequential_independent_reset_no_timestamps",
        )
        base["mode_execution"]["blockers"].extend([
            _block("no_mode_timestamps", "Mode-specific frame/line timing and phase origin",
                   "POST /api/v1/lens-sensor-lfm/timing-modes/template"),
            _block("not_vendor_hdr_program", "Executed vendor exposure/readout/fusion schedule",
                   "Supply a separate executable mode contract"),
        ])
        base["reference_comparison"]["blockers"].append(_block(
            "no_timed_pwm_source", "Timed PWM RAW and matching independent reference",
            "GET /api/v1/lens-sensor-lfm/template?scope=central_bounded_timed",
        ))
        base["next_workflows"].append({
            "kind": "timed_pwm_raw_from_same_lens_sensor",
            "endpoint": "/api/v1/lens-sensor-lfm/template?scope=central_bounded_timed",
            "preselected_source": {
                "lens_revision_id": job["specification"]["lens_revision_id"],
                "sensor_revision": deepcopy(job["specification"]["sensor_revision"]),
                "exposure_s": deepcopy(job["specification"]["exposure_s"]),
            },
        })
    else:
        if kind == "lens_sensor_pwm_hdr":
            source = lens_sensor_lfm_job.get(service, job["source_lfm_job_id"])
            report = summary["report"]
            with np.load(service.registry.blob_path(
                summary["evidence"]["npz"]["sha256"]
            ), allow_pickle=False) as arrays:
                raw = np.asarray(arrays["original_pwm_raw_dn"]).copy()
            from .hdr_lfm_evidence_readiness import assess_hdr_lfm_evidence

            coverage = assess_hdr_lfm_evidence(
                report, original_raw_dn=raw,
                source_lfm_summary=source["result"]["summary"],
            )
            base["model_execution"].update(
                raw_artifact_sha256=summary["evidence"]["npz"]["sha256"],
                raw_array_sha256=report["original_pwm_raw_sha256"],
                raw_shape=report["original_pwm_raw_shape"],
                hdr_fusion_applied=True,
                coverage=coverage["model_internal_coverage"],
                scope="model_internal_four_phase_pwm_hdr_reconstruction",
            )
            base["mode_execution"]["blockers"].extend(
                coverage["vendor_mode_simulation"]["blockers"]
            )
        else:
            source = job
            report = summary["report"]
            with np.load(service.registry.blob_path(
                report["artifacts"]["npz"]["sha256"]
            ), allow_pickle=False) as arrays:
                raw = np.asarray(arrays["original_pwm_raw_dn"]).copy()
                if raw.dtype != np.dtype("int32") or raw.shape != (
                    2, 4, *job["specification"]["sensor_size"]
                ):
                    raise IntegrityError("Saved PWM RAW has the wrong array contract")
            if len(report["cases"]) != 2 or any(
                len(case.get("phases", [])) != 4 for case in report["cases"]
            ):
                raise IntegrityError("Saved PWM RAW phase case count changed")
            for exposure in range(2):
                for phase in range(4):
                    observed = hashlib.sha256(
                        np.ascontiguousarray(raw[exposure, phase]).tobytes()
                    ).hexdigest()
                    if observed != report["cases"][exposure]["phases"][phase]["raw_sha256"]:
                        raise IntegrityError("Saved PWM RAW frame differs from its report")
            base["model_execution"].update(
                raw_artifact_sha256=report["artifacts"]["npz"]["sha256"],
                raw_shape=list(raw.shape), hdr_fusion_applied=False,
                scope="authored_or_source_declared_pwm_phase_sensitivity",
            )
            base["mode_execution"]["blockers"].extend([
                _block("vendor_capture_program_not_executed",
                       "Vendor exposure/readout/fusion schedule",
                       "Supply an executable sensor-mode program"),
                _block("absolute_phase_and_native_crop_unknown",
                       "Native crop mapping and absolute PWM phase",
                       "Provide an independent acquisition/reference packet"),
            ])
            base["next_workflows"].append({
                "kind": "reconstruct_original_pwm_raw",
                "endpoint": "/api/v1/lens-sensor-pwm-hdr",
                "specification": {
                    "schema": lens_sensor_pwm_hdr_job.SCHEMA,
                    "name": "저장 PWM RAW HDR 복원",
                    "source_lfm_job_id": job["id"],
                    "acknowledge_simulation_limits": False,
                },
            })
        timing = source.get("timing_mode")
        if timing is None:
            base["mode_execution"]["blockers"].append(_block(
                "authored_line_time", "Source-bound timing-mode JSON and source bytes",
                "POST /api/v1/lens-sensor-lfm/timing-modes/import",
            ))
            timed_spec = deepcopy(source["specification"])
            timed_spec["schema"] = lens_sensor_lfm_job.TIMED_BOUNDED_SCHEMA
            timed_spec["timing_mode_sha256"] = ""
            base["next_workflows"].append({
                "kind": "source_bound_timed_pwm_raw",
                "mode_template_endpoint": "/api/v1/lens-sensor-lfm/timing-modes/template",
                "mode_template_payload": {
                    "sensor_revision": deepcopy(source["specification"]["sensor_revision"]),
                    "sensor_size": deepcopy(source["specification"]["sensor_size"]),
                    "exposure_s": deepcopy(source["specification"]["exposure_s"]),
                },
                "mode_import_endpoint": "/api/v1/lens-sensor-lfm/timing-modes/import",
                "job_endpoint": "/api/v1/lens-sensor-lfm",
                "specification_after_import": timed_spec,
            })
        else:
            base["model_execution"]["timing_mode_sha256"] = source["timing_mode_sha256"]
            base["mode_execution"]["source_bound_timing"] = {
                "mode_id": timing["mode_id"],
                "source_kind": timing["provenance"]["source_kind"],
                "source_document_sha256": timing["provenance"]["source_document_sha256"],
                "value_verification": timing["provenance"]["value_verification"],
            }
            if not any(
                block["code"] == "timing_value_not_independently_verified"
                for block in base["mode_execution"]["blockers"]
            ):
                base["mode_execution"]["blockers"].append(_block(
                    "timing_value_not_independently_verified",
                    "Independently checked line/frame timing for the selected operating point",
                    "Provide mode source and measured timing log to the reference comparison",
                ))
        if kind == "lens_sensor_pwm_hdr":
            if timing is not None and timing["provenance"]["source_kind"] != "authored_assumption":
                base["reference_comparison"]["status"] = "intake_available"
                base["reference_comparison"]["template_endpoint"] = (
                    f"/api/v1/hdr-lfm-reference-comparisons/template/{identity}"
                )
                base["reference_comparison"]["submit_endpoint"] = (
                    "/api/v1/hdr-lfm-reference-comparisons"
                )
            else:
                base["reference_comparison"]["blockers"].append(_block(
                    "no_source_bound_operating_mode",
                    "Source-hashed operating-mode timing linked to this RAW job",
                    "POST /api/v1/lens-sensor-lfm/timing-modes/import",
                ))
        else:
            base["reference_comparison"]["blockers"].append(_block(
                "hdr_reconstruction_not_run", "Saved HDR reconstruction of this exact PWM RAW",
                "POST /api/v1/lens-sensor-pwm-hdr",
            ))

    if conditions.get("exposure_s") is None or conditions.get("readout_rate_fps") is None:
        base["mode_execution"]["blockers"].append(_block(
            "operating_point_conditions_incomplete",
            "Sensor-mode exposure range and readout FPS with source conditions",
            "Register a mode-specific Sensor revision; do not infer from a generic/proxy profile",
        ))
    if provenance["classification"] in {
        "unmeasured_assumption", "camera_proxy_with_assumptions"
    }:
        base["mode_execution"]["blockers"].append(_block(
            "readout_is_assumed_or_camera_proxy",
            "Mode-specific calibrated bare-sensor readout and FPN uncertainty",
            "Import an independently calibrated Sensor revision",
        ))
    base["reference_comparison"]["required_independent_evidence"] = [
        "original undemosaiced int32 RAW with full frame/exposure/phase labels",
        "independent float64 electron-rate reference on the declared phase grid",
        "physical Sensor instance, instrument calibration and native crop origin",
        "source-hashed mode, waveform and reference acquisition logs",
    ]
    return base


def install_routes(app, service) -> None:
    @app.get("/api/v1/hdr-lfm-mode-readiness/{identity}", tags=["hdr_lfm"])
    def detail(identity: str):
        return assess(service, identity)
