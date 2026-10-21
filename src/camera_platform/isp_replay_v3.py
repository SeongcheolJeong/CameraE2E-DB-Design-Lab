"""Explicit charge-domain replay of retained nonlinear RAW, with immutable selection."""

from __future__ import annotations

import hashlib
import io
from copy import deepcopy
from typing import Literal

from pydantic import Field, field_validator

from . import isp_charge_calibration as calibration_records
from . import isp_charge_source as sources
from .contracts import Contract, ModelCreate
from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_isp_replay_v3"
SUMMARY_FORECAST_VERSION = "isp_charge_replay_summary_v1"
SUMMARY_GENERATED_BYTES_LIMIT = 64 * 1024
SUMMARY_GENERATED_BYTES_PER_FRAME = 16 * 1024


class ReplayCreateV3(Contract):
    schema: Literal["camera_isp_replay_v3"]
    name: str = Field(min_length=1, max_length=200)
    source_kind: Literal["run", "capture"]
    source_id: str = Field(min_length=1)
    selector: dict
    isp_model_id: str = Field(min_length=1)
    force: bool = False

    @field_validator("selector")
    @classmethod
    def selection(cls, value):
        return sources.validate_selector(value)


def template(service, *, source_kind, source_id, selector, calibration_id=None):
    from .imaging import resolve_isp
    from .isp_v3 import default_profile

    source = sources.resolve(service, source_kind, source_id, selector)
    original = source["source_snapshot"]
    assembly = service.registry.get("assemblies", original["assembly_id"])
    model = next(
        service.registry.get("models", i)
        for i in assembly["model_ids"]
        if service.registry.get("models", i)["kind"] == "isp"
    )
    profile = default_profile(resolve_isp(model), calibration_id=calibration_id)
    if calibration_id:
        calibration = calibration_records.resolve_for_source(service, calibration_id, source)
        if calibration["payload"]["lsc"] is not None:
            profile["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
    result = {k: deepcopy(v) for k, v in model.items() if k in ModelCreate.model_fields}
    result.update(
        name="보존 비선형 RAW · 보정 전하 ISP",
        revision="charge-1",
        parents=[model["id"]],
        payload=profile,
        source_kind="estimated",
        source_uri="authoring://charge-isp-profile",
        physical_instance=None,
        method_and_version="camera_isp_profile_v3:declared_tuning",
        input_raw_format=deepcopy(source["raw_format"]),
    )
    return result


def prepare(service, payload):
    from .imaging import _current, _format
    from .isp_v3 import validate_inputs, validate_profile
    from .validation import check_domain

    data = ReplayCreateV3.model_validate(payload).model_dump()
    data.pop("force")
    source = sources.resolve(service, data["source_kind"], data["source_id"], data["selector"])
    model = service.registry.get("models", data["isp_model_id"])
    if model["kind"] != "isp":
        raise ValueError("Charge replay requires an ISP model")
    profile = validate_profile(model["payload"])
    if _format(model["input_raw_format"]) != source["raw_format"]:
        raise ValueError("Charge ISP model format differs from original selected nonlinear RAW")
    if any(
        check_domain(c, model["valid_domain"]) for c in calibration_records.condition_rows(source)
    ):
        raise ValueError("Charge ISP applicability does not cover original acquisition")
    identity = profile["calibration_id"]
    calibration = (
        calibration_records.resolve_for_source(service, identity, source)
        if identity is not None
        else None
    )
    raw, calibrations = sources.load_inputs(service, source)
    validate_inputs(
        raw,
        calibrations,
        profile,
        geometry=source["geometry"],
        normalization_e=source["normalization_e"],
        calibration=calibration["payload"] if calibration else None,
    )
    artifacts = deepcopy(source["input_artifacts"])
    if calibration:
        artifacts.extend(deepcopy(calibration["artifacts"]))
        artifacts.extend(deepcopy(calibration["evidence_artifacts"]))
    return {
        **data,
        "job_kind": "isp_replay",
        "plugin_id": "isp_replay_v3",
        "source": source,
        "isp_model_snapshot": model,
        "profile": profile,
        "calibration_snapshot": calibration,
        "environment": _current(service),
        "input_artifacts": artifacts,
    }


def _verify_inputs(service, job, *, current_environment):
    submitted = {key: job[key] for key in ReplayCreateV3.model_fields if key in job}
    fresh = prepare(service, submitted)
    for key, value in fresh.items():
        if key == "environment" and not current_environment:
            continue
        if canonical(job.get(key)) != canonical(value):
            raise IntegrityError(
                "Frozen charge replay differs from its source/model/calibration: " + key
            )
    return fresh


def verify(service, job):
    """Fresh evidence check for historical GET/cache/export; no environment migration."""
    _verify_inputs(service, job, current_environment=False)
    for artifact in (job.get("result") or {}).get("artifacts", []):
        content = service.registry.blob_path(artifact["sha256"]).read_bytes()
        if (
            len(content) != artifact["bytes"]
            or hashlib.sha256(content).hexdigest() != artifact["sha256"]
        ):
            raise IntegrityError("Saved charge replay artifact bytes differ")
    return job


def submit(service, payload):
    frozen = prepare(service, payload)
    job = service.registry.submit(frozen, digest(frozen), force=payload.get("force", False))
    return verify(service, job)


def copied_summary_fields(replay):
    """Exact input-dependent output JSON, shared with Study evidence forecasting."""
    source = replay["source"]
    frames = deepcopy(source["frame_records"])
    for frame in frames:
        frame["replay_isp_model_id"] = replay["isp_model_snapshot"]["id"]
    binding = (
        {
            "decoder_binding": source["decoder_binding"],
            "response_calibration_snapshot": source["response_calibration_snapshot"],
            "response_calibration_lineage": source["response_calibration_lineage"],
            "acquisition_lineage": source["acquisition_lineage"],
        }
        if source["source_kind"] == "capture"
        else {
            "capture_program_id": source["capture_program_id"],
            "capture_program_sha256": source["capture_program_sha256"],
        }
    )
    return {
        "plugin_id": "isp_replay_v3",
        "job_kind": "isp_replay",
        "source_kind": source["source_provenance"]["source_kind"],
        "acquisition_conditions": source["conditions"],
        "conditions": source["conditions"],
        "source_id": source["source_id"],
        "selector": source["selector"],
        "raw_sha256": source["raw_sha256"],
        "raw_container_shape": source["raw_container_shape"],
        "raw_shape": source["raw_shape"],
        "selected_array_sha256": source["selected_array_sha256"],
        "selected_array_hash_encoding": source["selected_array_hash_encoding"],
        "source_provenance": source["source_provenance"],
        "replay_isp_model_id": replay["isp_model_snapshot"]["id"],
        "calibration_snapshot": replay["calibration_snapshot"],
        "raw_format": source["raw_format"],
        "geometry": source["geometry"],
        "normalization_e": source["normalization_e"],
        "frame_count": len(frames),
        "frames": frames,
        "frame_conditions": source["frame_conditions"],
        "clock": source["clock"],
        "timing": source["timing"],
        **binding,
        "calibration_summaries": source["calibration_summaries"],
        "original_acquisition_records": source["original_acquisition_records"],
        "acquisition_identity": source["acquisition_identity"],
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
    }


def summary_forecast(replay):
    from .isp_studies import _json_bytes

    copied = copied_summary_fields(replay)
    copied_bytes = _json_bytes(copied) + _json_bytes(replay["profile"])
    # Decoder scalar diagnostics and nine stage previews grow with frame count.
    # This explicit hard bound is enforced on the actual generated summary.
    generated = (
        SUMMARY_GENERATED_BYTES_LIMIT
        + len(replay["source"]["frame_records"]) * SUMMARY_GENERATED_BYTES_PER_FRAME
    )
    return {
        "version": SUMMARY_FORECAST_VERSION,
        "copied_summary_bytes": copied_bytes,
        "generated_summary_bytes_limit": generated,
        "summary_artifact_bytes_limit": copied_bytes + generated,
    }


def _stage_previews(service, output):
    import numpy as np
    from PIL import Image

    artifacts, previews = [], []
    masks = {
        stage["array"]: stage["validity"] for stage in output["summary"]["stage_outputs"].values()
    }
    masks["baseline_rgb_frames"] = "baseline_rgb_valid_mask_frames"
    for name in sorted(masks):
        for index, values in enumerate(output["arrays"][name]):
            valid = output["arrays"][masks[name]][index]
            if values.ndim == 3:
                valid = valid[..., None]
            pixels = np.where(valid & np.isfinite(values), values, 0.0)
            if pixels.ndim == 2:
                pixels = np.repeat(pixels[..., None], 3, axis=-1)
            stream = io.BytesIO()
            Image.fromarray((np.clip(pixels, 0, 1) * 255).astype(np.uint8)).save(stream, "PNG")
            artifact = service.registry.store_blob(
                stream.getvalue(), f"isp-{name}-{index:03d}.png", "image/png"
            )
            artifact.update(stage_array=name, frame_index=index)
            artifacts.append(artifact)
            previews.append(
                {
                    "array": name,
                    "frame_index": index,
                    "preview_artifact_id": artifact["id"],
                    "preview_url": artifact["url"],
                    "display_range": [0, 1],
                    "display_transfer": "clamp_normalized_values_no_autoscale",
                    "invalid_display": "black; physical arrays and validity remain preserved",
                }
            )
    output["summary"]["stage_previews"] = previews
    return artifacts


def execute(service, job):
    from .imaging import store_frame_previews, store_output
    from .isp_v3 import process_raw

    fresh = _verify_inputs(service, job, current_environment=True)
    source = fresh["source"]
    raw, calibrations = sources.load_inputs(service, source)
    calibration = fresh["calibration_snapshot"]
    output = process_raw(
        raw,
        calibrations,
        fresh["profile"],
        geometry=source["geometry"],
        normalization_e=source["normalization_e"],
        calibration=calibration["payload"] if calibration else None,
    )
    output["summary"].update(deepcopy(copied_summary_fields(fresh)))
    artifacts = deepcopy(fresh["input_artifacts"])
    artifacts.extend(store_frame_previews(service, output))
    artifacts.extend(_stage_previews(service, output))
    bound = summary_forecast(fresh)["summary_artifact_bytes_limit"]
    if len(canonical(output["summary"]).encode()) > bound:
        raise IntegrityError("Charge replay summary exceeds its declared metadata budget")
    return store_output(
        service,
        output,
        source_kind=source["source_provenance"]["source_kind"],
        geometry=source["geometry"],
        input_artifacts=artifacts,
    )
