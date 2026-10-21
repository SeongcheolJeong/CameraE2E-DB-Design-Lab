"""Immutable advanced ISP replay over retained native RAW and calibration evidence."""

from __future__ import annotations

import copy
import io

import numpy as np

from .contracts import ModelCreate, ReplayCreate
from .registry import canonical, digest


def template(service, *, source_kind, source_id, calibration_id=None):
    from .imaging import replay_source
    from .isp_calibration import resolve_for_source
    from .isp_v2 import default_profile

    source = replay_source(service, source_kind, source_id)
    if source["geometry"].get("kind") != "native_sensor":
        raise ValueError("Advanced ISP needs explicit native RAW geometry")
    original = source["source_snapshot"]
    assembly = service.registry.get("assemblies", original["assembly_id"])
    model = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "isp"
    )
    profile = default_profile(calibration_id)
    calibration = resolve_for_source(service, calibration_id, source) if calibration_id else None
    if calibration:
        if calibration["payload"]["lsc"] is not None:
            profile["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
        if calibration["payload"]["noise"] is not None:
            profile["raw_denoise"]["mode"] = "calibrated_same_site_filter_v1"
    result = {k: copy.deepcopy(v) for k, v in model.items() if k in ModelCreate.model_fields}
    result.update(
        name="보존 Native RAW · LSC·잡음·선명도 프로파일",
        revision="advanced-1",
        parents=[model["id"]],
        payload=profile,
        source_kind="estimated",
        source_uri="authoring://advanced-isp-profile",
        physical_instance=None,
        method_and_version="camera_isp_profile_v2:declared_tuning",
        input_raw_format=copy.deepcopy(source["raw_format"]),
    )
    return result


def prepare(service, payload):
    from .imaging import _current, _format, replay_source, resolve_isp
    from .isp_calibration import resolve_for_source
    from .isp_v2 import validate_inputs
    from .observations import load_raw_npy
    from .validation import check_domain

    data = ReplayCreate.model_validate(payload).model_dump()
    data.pop("force")
    source = replay_source(service, data["source_kind"], data["source_id"])
    model = service.registry.get("models", data["isp_model_id"])
    profile = resolve_isp(model, allow_v2=True)
    if profile["schema"] != "camera_isp_profile_v2":
        raise ValueError("Advanced replay requires an explicit v2 ISP profile")
    if _format(model["input_raw_format"]) != source["raw_format"]:
        raise ValueError("ISP input RAW format differs from preserved acquisition")
    conditions = [source["conditions"]]
    if source.get("frame_conditions"):
        if any(
            f.get("exposure_ns") is None or f.get("gain") is None
            for f in source["frame_conditions"]
        ):
            raise ValueError("Every preserved frame needs its actual exposure and gain")
        conditions = [
            {
                **source["conditions"],
                "exposure_s": {"value": f["exposure_ns"] * 1e-9, "unit": "s"},
                "gain": {"value": f["gain"], "unit": "1"},
            }
            for f in source["frame_conditions"]
        ]
    if any(check_domain(c, model["valid_domain"]) for c in conditions):
        raise ValueError("ISP applicability does not cover the original acquisition")
    calibration = (
        resolve_for_source(service, profile["calibration_id"], source)
        if profile["calibration_id"] is not None
        else None
    )
    raw = load_raw_npy(service.registry.blob_path(source["raw_sha256"]).read_bytes())
    validate_inputs(
        raw,
        source["raw_format"],
        profile,
        geometry=source["geometry"],
        calibration=calibration["payload"] if calibration else None,
    )
    artifacts = [source["raw_artifact"]]
    if calibration:
        artifacts.extend(calibration["artifacts"])
        artifacts.extend(calibration.get("evidence_artifacts", []))
    return {
        **data,
        "job_kind": "isp_replay",
        "plugin_id": "isp_replay_v2",
        "source": source,
        "isp_model_snapshot": model,
        "profile": profile,
        "calibration_snapshot": calibration,
        "environment": _current(service),
        "input_artifacts": artifacts,
    }


def submit(service, payload):
    frozen = prepare(service, payload)
    job = service.registry.submit(frozen, digest(frozen), force=payload.get("force", False))
    if job["status"] == "succeeded":
        for artifact in job["result"]["artifacts"]:
            path = service.registry.blob_path(artifact["sha256"])
            if path.stat().st_size != artifact["bytes"]:
                raise ValueError("Cached advanced ISP artifact size differs from its evidence")
    return job


def _stage_previews(service, output):
    """Fixed normalized display range, separate from retained floating arrays."""
    from PIL import Image

    artifacts, previews = [], []
    names = {stage["array"] for stage in output["summary"]["stage_outputs"].values()}
    names.add("baseline_rgb_frames")
    for name in sorted(names):
        values = output["arrays"][name]
        for index, pixels in enumerate(values):
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
                }
            )
    output["summary"]["stage_previews"] = previews
    return artifacts


def execute(service, job):
    from .imaging import _current, store_frame_previews, store_output
    from .isp_v2 import process_raw
    from .observations import load_raw_npy

    if job["environment"] != _current(service):
        raise ValueError("Worker differs from frozen advanced ISP environment")
    submitted = {key: job[key] for key in ReplayCreate.model_fields if key in job}
    fresh = prepare(service, submitted)
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise ValueError(
            "Frozen advanced replay differs from its source/model/calibration evidence"
        )
    source, calibration = fresh["source"], fresh["calibration_snapshot"]
    raw = load_raw_npy(service.registry.blob_path(source["raw_sha256"]).read_bytes())
    output = process_raw(
        raw,
        source["raw_format"],
        fresh["profile"],
        geometry=source["geometry"],
        calibration=calibration["payload"] if calibration else None,
    )
    summary = output["summary"]
    summary.update(
        plugin_id="isp_replay_v2",
        job_kind="isp_replay",
        source_kind=source["source_provenance"]["source_kind"],
        acquisition_conditions=source["conditions"],
        conditions=source["conditions"],
        source_id=job["source_id"],
        raw_sha256=source["raw_sha256"],
        source_provenance=copy.deepcopy(source["source_provenance"]),
        replay_isp_model_id=job["isp_model_snapshot"]["id"],
        calibration_snapshot=copy.deepcopy(calibration),
        physical_validation="not_established",
        uncertainty={"kind": "unknown"},
    )
    artifacts = list(fresh["input_artifacts"])
    if source.get("frame_records"):
        summary.update(
            frame_count=len(source["frame_records"]),
            frames=copy.deepcopy(source["frame_records"]),
            frame_conditions=copy.deepcopy(source["frame_conditions"]),
            clock=copy.deepcopy(source["clock"]),
            **{
                key: copy.deepcopy(source[key])
                for key in (
                    "timing",
                    "control_trace",
                    "acquisition_mode",
                    "control_program_id",
                    "capture_program_id",
                    "control_policy_id",
                    "response_acquisition",
                )
                if key in source
            },
        )
        for frame in summary["frames"]:
            frame["replay_isp_model_id"] = job["isp_model_snapshot"]["id"]
        artifacts.extend(store_frame_previews(service, output))
    artifacts.extend(_stage_previews(service, output))
    return store_output(
        service,
        output,
        source_kind=source["source_provenance"]["source_kind"],
        geometry=source["geometry"],
        input_artifacts=artifacts,
    )
