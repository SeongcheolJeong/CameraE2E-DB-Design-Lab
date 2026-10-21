"""Durable quality study over four exact, previously completed Lens x Sensor RAWs."""

from __future__ import annotations

import io
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.dataset import _sha256_array

from . import isp_v2
from .lens_sensor_quality_study import (
    SCHEMA,
    SCHEMA_V2,
    SLOTS,
    _comparison,
    _condition,
    _matched_angular_edge,
    _require_crossed_identities,
    evaluate_matrix,
)
from .registry import IntegrityError, canonical, digest

JOB_KIND = "lens_sensor_quality_study"
_ARRAY_NAME = "lens-sensor-quality-study-arrays.npz"
_RESULT_NAME = "lens-sensor-quality-study-result.json"
LIMITATION = (
    "Uses four exact saved Lens x Sensor RAW frames in a 2 x 2 matrix and one "
    "fixed native-CFA ISP v2 profile. Each case may provide single-frame edge "
    "MTF50 as a simulated diagnostic or a reason it is unavailable. A single "
    "frame cannot yield temporal SNR. Native cycles/pixel values are not paired "
    "across different sampling. An optional matched object-angle edge diagnostic "
    "uses central angular overlap, dense source and bounded geometric "
    "distortion, but its native rectangles have different areas. The v2 "
    "common-grid diagnostic resamples the same angular area with antialiasing; "
    "it is still a simulated sensitivity result. Lens radiometry and "
    "PSF sampling are not validated; product ranking and physical accuracy "
    "are never claimed."
)

_DEV28_NATIVE_GRID_REASON = (
    "Distortion-aware point-center maps describe geometry only; a common "
    "PSF/pixel-integrated output grid, angular ROI resampling and comparable "
    "frequency metric are not established."
)
_CURRENT_NATIVE_GRID_REASON = (
    "Distortion-aware point-center maps describe geometry only; common "
    "PSF/pixel-integrated native RAW output footprints remain unestablished. "
    "This gate blocks native cycles/output-pixel pairwise deltas; separately "
    "evaluated angular-grid diagnostics have their own limits."
)
_DEV28_PAIR_REASON = "Full 2 x 2 scene and metric comparability gate did not pass."
_CURRENT_PAIR_REASON = (
    "Native cycles/output-pixel pairwise gate did not pass; separately "
    "evaluated angular-grid diagnostics have their own limits."
)
_CURRENT_BLOCKED_SCOPE = (
    "native cycles/output-pixel pairwise gate only; angular diagnostics "
    "are evaluated separately"
)


def _normalize_dev28_comparison_wording(comparison: dict[str, Any]) -> dict[str, Any]:
    """Accept only the exact superseded explanation on historical sealed jobs.

    Numeric fields, map evidence, gate booleans and the separately recomputed
    angular diagnostics remain byte-for-byte compared by ``get``.
    """
    normalized = deepcopy(comparison)
    if isinstance(normalized.get("reasons"), list):
        normalized["reasons"] = [
            _CURRENT_NATIVE_GRID_REASON if reason == _DEV28_NATIVE_GRID_REASON else reason
            for reason in normalized["reasons"]
        ]
    if normalized.get("scope") == "individual source diagnostics only":
        normalized["scope"] = _CURRENT_BLOCKED_SCOPE
    for pair in normalized.get("pairwise_diagnostic_deltas", []):
        if isinstance(pair, dict) and pair.get("reason") == _DEV28_PAIR_REASON:
            pair["reason"] = _CURRENT_PAIR_REASON
    return normalized


def _saved_shared_metric_matches(
    saved: dict[str, Any], recomputed: dict[str, Any],
) -> bool:
    """Read historical v2 results without weakening their existing metric fields.

    Absent later scope diagnostics stay absent in historical output. If saved,
    every diagnostic field must match its fresh reconstruction, just as metrics do.
    """
    if not isinstance(saved, dict):
        return False
    later_fields = {
        "model_550nm_point_cell_footprint",
        "physical_mtf_comparability",
        "model_input_support_diagnostic",
    }
    recomputed = deepcopy({
        key: value for key, value in recomputed.items()
        if key not in later_fields or key in saved
    })
    saved_support = saved.get("model_input_support_diagnostic")
    current_support = recomputed.get("model_input_support_diagnostic")
    if isinstance(saved_support, dict) and isinstance(current_support, dict):
        # These two source-halo details were added after some v2 jobs already
        # sealed their support diagnostic. Never synthesize them into a saved
        # report; when present, compare every value against the sealed source.
        if "all_recorded_global_psf_downstream_upper_bounds_fit_scene" not in saved_support:
            current_support.pop(
                "all_recorded_global_psf_downstream_upper_bounds_fit_scene", None
            )
        for slot in SLOTS:
            old_slot = saved_support.get("per_slot", {}).get(slot)
            new_slot = current_support.get("per_slot", {}).get(slot)
            if (
                isinstance(old_slot, dict) and isinstance(new_slot, dict)
                and "executed_psf_pixel_source_scene_halo" not in old_slot
            ):
                new_slot.pop("executed_psf_pixel_source_scene_halo", None)
    return canonical(saved) == canonical(recomputed)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA_V2,
        "name": "렌즈 × 센서 저장 RAW 2×2 화질 진단",
        "source_jobs": {slot: "" for slot in SLOTS},
        "profile": isp_v2.default_profile(),
        "acknowledge_simulation_limits": False,
    }


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the Lens x Sensor quality-study fields.")
    if (
        payload["schema"] not in {SCHEMA, SCHEMA_V2}
        or payload["acknowledge_simulation_limits"] is not True
    ):
        raise ValueError(
            "Explicit Lens x Sensor quality simulation-limit acknowledgement required."
        )
    name = payload["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Quality-study name requires 1..160 characters.")
    jobs = payload["source_jobs"]
    if not isinstance(jobs, dict) or set(jobs) != set(SLOTS):
        raise ValueError("Select exactly the four L1S1, L1S2, L2S1 and L2S2 source jobs.")
    ids = []
    for slot in SLOTS:
        identity = jobs[slot]
        if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 128:
            raise ValueError(f"{slot} requires one completed source job ID.")
        ids.append(identity.strip())
    if len(set(ids)) != 4:
        raise ValueError("The four Lens x Sensor source job IDs must be distinct.")
    profile = isp_v2.validate_profile(payload["profile"])
    if profile["calibration_id"] is not None or any(
        profile[stage]["mode"] != "bypass" for stage in ("spatial_lsc", "raw_denoise")
    ):
        raise ValueError("Quality study requires bypass LSC/denoise and no calibration ID.")
    result = deepcopy(payload)
    result["name"] = name.strip()
    result["source_jobs"] = dict(zip(SLOTS, ids, strict=True))
    result["profile"] = profile
    return result


def _inputs(service: Any, spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    from .retained_raw_dataset_job import _lens_source
    from .retained_raw_isp_bridge import inspect_lens_sensor_isp_input

    result: dict[str, dict[str, Any]] = {}
    for slot in SLOTS:
        identity = spec["source_jobs"][slot]
        source = _lens_source(service, {"source_job_id": identity})
        json_path = service.registry.blob_path(source["source_artifacts"]["json"]["sha256"])
        raw_path = service.registry.blob_path(source["source_artifacts"]["raw"]["sha256"])
        checked = inspect_lens_sensor_isp_input(
            json_path,
            raw_path,
            source_lens_sensor_job_id=identity,
            sensor_db_root=Path(service.workspace) / "camerae2e_db",
            profile=spec["profile"],
        )
        if checked["source_binding"] != source["source_binding"]:
            raise IntegrityError("Quality study source differs from its completed RAW job.")
        from .retained_raw_dataset import load_retained_lens_sensor_raw

        verified_report, raw, binding = load_retained_lens_sensor_raw(
            json_path, raw_path, source_lens_sensor_job_id=identity
        )
        if binding != source["source_binding"] or not np.array_equal(raw, checked["raw"]):
            raise IntegrityError("Quality study loader returned a different original RAW.")
        result[slot] = {
            "report": verified_report,
            "raw": raw,
            "checked": checked,
            "binding": binding,
            "source": source,
        }
    _require_crossed_identities(result)
    return result


def _entries(inputs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "slot": slot,
            "source_job_id": inputs[slot]["binding"]["source_lens_sensor_job_id"],
            "source_capture_result_sha256": inputs[slot]["source"]["source_capture_result_sha256"],
            "source_artifacts": inputs[slot]["source"]["source_artifacts"],
            "source_binding": inputs[slot]["binding"],
            "conditions": _condition(inputs[slot]["report"], inputs[slot]["checked"]),
            "raw_format": inputs[slot]["checked"]["raw_format"],
            "geometry": inputs[slot]["checked"]["geometry"],
        }
        for slot in SLOTS
    ]


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current

    spec = specification(payload)
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": spec["schema"],
        "specification": spec,
        "source_entries": _entries(_inputs(service, spec)),
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens x Sensor quality study is not running or was cancelled.")
    fresh = prepare(service, job["specification"])
    if canonical(fresh) != canonical({key: job.get(key) for key in fresh}):
        raise IntegrityError("Frozen Lens x Sensor sources, ISP profile or executable changed.")
    inputs = _inputs(service, fresh["specification"])
    report, arrays = evaluate_matrix(
        inputs, profile=fresh["specification"]["profile"],
        schema=fresh["specification"]["schema"],
    )
    if any(
        canonical(report["cases"][entry["slot"]]["source_binding"])
        != canonical(entry["source_binding"])
        or canonical(report["cases"][entry["slot"]]["conditions"])
        != canonical(entry["conditions"])
        for entry in fresh["source_entries"]
    ):
        raise IntegrityError("Quality metric inputs differ from frozen source conditions.")
    stream = io.BytesIO()
    np.savez_compressed(stream, **arrays)
    array_bytes = stream.getvalue()
    with np.load(io.BytesIO(array_bytes), allow_pickle=False) as restored:
        for slot in SLOTS:
            raw = np.asarray(restored[f"{slot}_raw_dn"])
            if (
                raw.dtype != np.dtype("int32")
                or _sha256_array(raw) != report["cases"][slot]["raw_content_sha256"]
                or not np.array_equal(raw, arrays[f"{slot}_raw_dn"])
            ):
                raise IntegrityError("Quality study NPZ lost an exact original RAW.")
    if canonical(prepare(service, fresh["specification"])) != canonical(fresh):
        raise IntegrityError("Quality study source or executable changed during evaluation.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Lens x Sensor quality study was cancelled during execution.")
    array_descriptor = service.registry.store_blob(
        array_bytes, _ARRAY_NAME, "application/octet-stream"
    )
    summary = {
        **report,
        "specification": fresh["specification"],
        "source_entries": fresh["source_entries"],
        "evidence_arrays": array_descriptor,
        "limitation": LIMITATION,
    }
    result_descriptor = service.registry.store_blob(
        canonical(summary).encode(), _RESULT_NAME, "application/json"
    )
    return {"summary": summary, "artifacts": [array_descriptor, result_descriptor]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Lens x Sensor quality study.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        if (
            canonical(summary.get("specification")) != canonical(job.get("specification"))
            or canonical(summary.get("source_entries")) != canonical(job.get("source_entries"))
            or canonical(summary.get("isp_profile"))
            != canonical(job.get("specification", {}).get("profile"))
            or summary.get("schema") != job.get("plugin_id")
            or summary.get("schema") not in {SCHEMA, SCHEMA_V2}
            or summary.get("comparison", {}).get("product_ranking_allowed") is not False
            or canonical(_normalize_dev28_comparison_wording({
                key: value for key, value in (summary.get("comparison") or {}).items()
                if key not in {
                    "matched_object_angle_edge_mtf50", "common_grid_object_angle_edge_mtf50",
                }
            })) != canonical(_comparison(summary["cases"]))
        ):
            raise IntegrityError("Saved quality study differs from its frozen request.")
        for entry in job["source_entries"]:
            case = summary["cases"][entry["slot"]]
            if (
                canonical(case["source_binding"]) != canonical(entry["source_binding"])
                or canonical(case["conditions"]) != canonical(entry["conditions"])
                or case["source_job_id"] != entry["source_job_id"]
            ):
                raise IntegrityError("Quality case lost its frozen source or conditions.")
        shared_diagnostic = summary["comparison"].get("common_grid_object_angle_edge_mtf50")
        if isinstance(shared_diagnostic, dict) and "model_input_support_diagnostic" in (
            shared_diagnostic
        ):
            sharpen = summary["isp_profile"]["linear_sharpen"]
            expected_halo = 1 + int(
                sharpen["mode"] != "bypass"
                and sharpen["amount"] > 0
                and sharpen["max_delta"] > 0
            )
            if any(
                summary["cases"][slot].get("isp_implementation") != isp_v2.IMPLEMENTATION
                or summary["cases"][slot].get(
                    "isp_crop_full_frame_halo_native_pixels"
                ) != expected_halo
                for slot in SLOTS
            ):
                raise IntegrityError("Quality case ISP identity or halo differs from profile.")
        if job["status"] == "succeeded":
            artifacts = job["artifacts"]
            if (
                len(artifacts) != 2
                or summary["evidence_arrays"] not in artifacts
                or {item["name"] for item in artifacts} != {_ARRAY_NAME, _RESULT_NAME}
            ):
                raise IntegrityError("Quality study lacks its exact evidence pair.")
            report_item = next(item for item in artifacts if item["name"] == _RESULT_NAME)
            if (
                service.registry.blob_path(report_item["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Quality study result bytes differ from its summary.")
            npz = service.registry.blob_path(summary["evidence_arrays"]["sha256"])
            with np.load(npz, allow_pickle=False) as restored:
                if set(restored.files) != {
                    f"{slot}_{suffix}"
                    for slot in SLOTS
                    for suffix in ("raw_dn", "linear_isp_rgb", "valid_rgb_mask")
                }:
                    raise IntegrityError("Quality study arrays have an unexpected set of keys.")
                for slot in SLOTS:
                    raw = np.asarray(restored[f"{slot}_raw_dn"])
                    case = summary["cases"][slot]
                    if (
                        raw.dtype != np.dtype("int32")
                        or list(raw.shape) != case["conditions"]["sensor_size"]
                        or _sha256_array(raw) != case["raw_content_sha256"]
                        or np.asarray(restored[f"{slot}_linear_isp_rgb"]).shape
                        != (*raw.shape, 3)
                        or np.asarray(restored[f"{slot}_valid_rgb_mask"]).shape
                        != raw.shape
                    ):
                        raise IntegrityError("Quality study evidence lost an original RAW.")
                angular = summary["comparison"].get("matched_object_angle_edge_mtf50")
                if summary["schema"] == SCHEMA_V2 and angular is None:
                    raise IntegrityError("V2 quality study lacks its native angular diagnostic.")
                if angular is not None:
                    expected_angular = _matched_angular_edge(
                        summary["cases"],
                        {slot: np.asarray(restored[f"{slot}_linear_isp_rgb"])
                         for slot in SLOTS},
                        {slot: np.asarray(restored[f"{slot}_valid_rgb_mask"])
                         for slot in SLOTS},
                        _comparison(summary["cases"]),
                    )
                    if canonical(angular) != canonical(expected_angular) or any(
                        canonical(summary["cases"][slot].get("matched_object_angle_edge_mtf50"))
                        != canonical(expected_angular["per_slot"].get(
                            slot, {"available": False,
                                   "reason": "; ".join(expected_angular["reasons"])}
                        ))
                        for slot in SLOTS
                    ):
                        raise IntegrityError(
                            "Saved object-angle edge metric differs from sealed ISP arrays."
                        )
                if summary["schema"] == SCHEMA_V2:
                    from .lens_sensor_common_angular_mtf import evaluate_common_grid_edge

                    shared = summary["comparison"].get("common_grid_object_angle_edge_mtf50")
                    if not _saved_shared_metric_matches(
                        shared,
                        evaluate_common_grid_edge(
                            summary["cases"],
                            {slot: np.asarray(restored[f"{slot}_linear_isp_rgb"])
                             for slot in SLOTS},
                            {slot: np.asarray(restored[f"{slot}_valid_rgb_mask"])
                             for slot in SLOTS},
                            _comparison(summary["cases"]),
                            expected_angular,
                            isp_profile=summary["isp_profile"],
                        ),
                    ):
                        raise IntegrityError(
                            "Saved common-grid edge metric differs from sealed ISP arrays."
                        )
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-quality-studies/template", tags=[JOB_KIND])
    def template():
        sources = []
        for item in service.registry.list_runs():
            if item.get("job_kind") != "lens_sensor_raw" or item.get("status") != "succeeded":
                continue
            summary = (item.get("result") or {}).get("summary") or {}
            spec = summary.get("specification") or {}
            preview = summary.get("preview") or {}
            sources.append({
                "id": item["id"], "name": item["name"], "status": item["status"],
                "lens_revision_id": spec.get("lens_revision_id"),
                "sensor_run_id": (spec.get("sensor_revision") or {}).get("run_id"),
                "scene_kind": spec.get("scene_kind"),
                "horizontal_fov_deg": (preview.get("scene") or {}).get("horizontal_fov_deg"),
                "sensor_size": (preview.get("capture") or {}).get("sensor_size"),
            })
        return {"specification": default_spec(), "source_jobs": sources,
                "limitation": LIMITATION}

    @app.post("/api/v1/lens-sensor-quality-studies", status_code=202, tags=[JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-quality-studies", tags=[JOB_KIND])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-quality-studies/{identity}", tags=[JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-quality-studies/{identity}/cancel", tags=[JOB_KIND])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-quality-studies/{identity}/export", tags=[JOB_KIND])
    def export(identity: str):
        job = get(service, identity)
        if job["status"] != "succeeded":
            raise ValueError("Lens x Sensor quality study result is not ready.")
        return Response(
            canonical(job), media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="lens-sensor-quality-study.json"'
            },
        )
