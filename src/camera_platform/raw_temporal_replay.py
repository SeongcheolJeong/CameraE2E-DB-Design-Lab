"""Explicit research replay of verified acquisition RAW, never a new capture.

Reuse retained/electrical source verification, including RAW-only AE replay.
Do not change registered ISP semantics or feed reservoir truth to the filter.
Discovery/listing read metadata only; selected execution/detail verifies bytes.
"""

from __future__ import annotations

import hashlib
import io
from copy import deepcopy
from dataclasses import asdict

import numpy as np

from .raw_temporal import CausalRawPairFilter, PairFilterConfig
from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_raw_temporal_replay_v1"
PLUGIN_ID = "raw_temporal_replay_v1"
JOB_KIND = "raw_temporal_replay"
LIMITATION = (
    "Genuine period-two scene changes can be suppressed. Research comparison only; "
    "not a replacement for original perception evidence or physical LFM qualification."
)
SOURCE_KEYS = (
    "source_id", "raw_sha256", "selected_array_sha256", "raw_format", "geometry",
    "readout_id", "clock", "frame_conditions", "frame_records", "retained_charge_events",
)


def specification(payload):
    required = {"schema", "name", "source_id", "readout_id", "cadence_ns", "config",
                "acknowledge_period_two_risk"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("Explicit RAW replay fields and risk acknowledgement are required")
    if payload["schema"] != SCHEMA or payload["acknowledge_period_two_risk"] is not True:
        raise ValueError("Explicit acknowledgement of genuine period-two signal loss is required")
    for key in ("name", "source_id"):
        if not isinstance(payload[key], str) or not 1 <= len(payload[key].strip()) <= 200:
            raise ValueError(f"{key} must contain 1..200 characters")
    if payload["readout_id"] not in ("single", "HCG", "LCG"):
        raise ValueError("Select one original acquired readout")
    if type(payload["cadence_ns"]) is not int or not 1 <= payload["cadence_ns"] < 2**62:
        raise ValueError("cadence_ns must be a positive bounded integer")
    config = payload["config"]
    if not isinstance(config, dict) or set(config) != set(asdict(PairFilterConfig())):
        raise ValueError("All filter thresholds must be explicitly declared")
    checked = PairFilterConfig(**config)
    return {**deepcopy(payload), "config": asdict(checked)}


def acquisition_context(source):
    return {key: deepcopy(source[key]) for key in SOURCE_KEYS}


def process_verified(source, raw, spec):
    """Pure adapter; the service boundary owns source verification.

    Use actual row-transfer times, not planned exposure or receive timestamps.
    A nonuniform row interval conservatively resets the whole frame. Each PD
    clear resets again, including consecutive clears. No truth arrays used.
    """
    from .retained_isp import default_profile, validate_inputs

    spec = specification(spec)
    if source["source_id"] != spec["source_id"] or source["readout_id"] != spec["readout_id"]:
        raise IntegrityError("Replay selector differs from the verified acquisition")
    parsed = validate_inputs(raw, source["raw_format"], default_profile(),
                             geometry=source["geometry"], frame_records=source["frame_records"],
                             events=source["retained_charge_events"])
    before = hashlib.sha256(raw.tobytes()).hexdigest()
    schedule = parsed["schedule"]
    times = schedule["times"]
    processor = CausalRawPairFilter(black_level_dn=parsed["black_level_dn"],
        white_level_dn=parsed["white_level_dn"], cadence_ns=spec["cadence_ns"],
        config=PairFilterConfig(**spec["config"]))
    codes, masks, ages, frames = [], [], [], []
    time_origin = int(times[0, 0])
    for index, (frame, condition) in enumerate(zip(source["frame_records"],
                                                 source["frame_conditions"], strict=True)):
        actual = frame["applied"]
        # Frame IDs, command IDs and timestamps are not acquisition settings.
        key = digest({"format": source["raw_format"], "geometry": source["geometry"],
            "readout": source["readout_id"],
            "applied": {k: v for k, v in actual.items() if k not in {"applied_at_ns", "command_id"}},
            "condition": {k: v for k, v in condition.items()
                          if k in {"exposure_ns", "gain", "temperature_c", "readout_analog_gain",
                                   "readout_digital_gain", "wb_gains_rgb"}},
            "row_transfer_offsets_ns": (times[index] - times[index, 0]).tolist()})
        reset = None
        if schedule["clear"][index]:
            reset = "pd_clear"
        elif index and np.any(schedule["intervals"][index] != spec["cadence_ns"]):
            reset = "row_cadence_change"
        if reset:
            processor.reset()
        result = processor.push(raw[index], timestamp_ns=int(times[index, 0]) - time_origin,
                                settings_key=key)
        mask = result["blend_mask"]
        age = np.zeros(raw[index].shape, dtype=np.float64)
        if index:
            age = np.where(mask, schedule["intervals"][index, :, None] / 2, 0.0)
        codes.append(result["code_dn"]); masks.append(mask); ages.append(age)
        delta = result["code_dn"] - raw[index]
        frames.append({"frame_id": frame["frame_id"], "frame_index": index,
            "first_row_transfer_ns": int(times[index, 0]),
            "last_row_transfer_ns": int(times[index, -1]),
            "reset_reason": reset or result["reset_reason"],
            "history_ready": result["history_ready"], "blend_fraction": float(mask.mean()),
            "maximum_added_sample_age_ns": float(age.max()),
            "mean_absolute_change_dn": float(np.abs(delta).mean()),
            "maximum_absolute_change_dn": float(np.abs(delta).max()),
            "original_mean_dn": float(raw[index].mean()),
            "processed_mean_dn": float(result["code_dn"].mean()),
            "applied": deepcopy(actual)})
    if hashlib.sha256(raw.tobytes()).hexdigest() != before:
        raise IntegrityError("Temporal processing changed its original RAW")
    processed = np.stack(codes)
    return {"arrays": {"raw_frames": raw.copy(), "processed_code_dn": processed,
                "blend_mask": np.stack(masks), "added_sample_age_ns": np.stack(ages),
                "processing_delta_dn": processed - raw, "row_transfer_ns": times.copy()},
            "summary": {"schema": SCHEMA, "plugin_id": PLUGIN_ID, "job_kind": JOB_KIND,
                "specification": spec, "acquisition": acquisition_context(source),
                "source_raw_bytes_sha256": before, "frame_count": len(raw), "frames": frames,
                "time_reference": "actual_per_row_transfer_ns", "relative_time_origin_ns": time_origin,
                "future_frames_used": 0, "source_truth_arrays_used": False,
                "processing_domain": "delivered_code_DN_not_new_sensor_RAW_or_charge_inverse",
                "output_may_contain_half_DN": True, "default_enabled": False,
                "physical_validation": "not_established", "limitation": LIMITATION,
                "array_units": {"raw_frames": "DN", "processed_code_dn": "DN",
                    "blend_mask": "boolean", "added_sample_age_ns": "ns",
                    "processing_delta_dn": "DN", "row_transfer_ns": "ns"}}}


def prepare(service, payload):
    from .imaging import _current
    from .retained_isp_source import _resolve
    from .retained_isp import default_profile, validate_inputs

    spec = specification(payload)
    source, raw = _resolve(service, spec["source_id"],
        {"schema": "camera_retained_raw_selection_v1", "readout_id": spec["readout_id"]})
    validate_inputs(raw, source["raw_format"], default_profile(), geometry=source["geometry"],
                    frame_records=source["frame_records"], events=source["retained_charge_events"])
    return {"name": spec["name"], "job_kind": JOB_KIND, "plugin_id": PLUGIN_ID,
            "specification": spec, "source": source, "environment": _current(service),
            "input_artifacts": deepcopy(source["input_artifacts"])}


def submit(service, payload):
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def _verify_source(service, job, *, current_environment=False):
    if job.get("job_kind") != JOB_KIND or job.get("plugin_id") != PLUGIN_ID:
        raise ValueError("Selected job is not a RAW temporal research replay")
    fresh = prepare(service, job["specification"])
    for key, value in fresh.items():
        if key == "environment" and not current_environment:
            continue
        if canonical(job.get(key)) != canonical(value):
            raise IntegrityError("Frozen RAW temporal replay differs: " + key)
    return fresh


def _previews(service, output):
    from PIL import Image

    summary, arrays = output["summary"], output["arrays"]
    fmt = summary["acquisition"]["raw_format"]
    black, white = fmt["black_level_dn"], 2**fmt["bit_depth"] - 1
    artifacts = []
    for index, frame in enumerate(summary["frames"]):
        frame["previews"] = {}
        for key in ("raw_frames", "processed_code_dn", "blend_mask"):
            values = arrays[key][index]
            pixels = values * 255 if key == "blend_mask" else np.clip((values-black)/(white-black), 0, 1)*255
            image = Image.fromarray(pixels.astype(np.uint8))
            image.thumbnail((512, 512))
            stream = io.BytesIO(); image.save(stream, format="PNG")
            artifact = service.registry.store_blob(stream.getvalue(), f"raw-pair-{key}-{index:03d}.png", "image/png")
            artifacts.append(artifact)
            frame["previews"][key] = artifact["url"]
    summary["preview_note"] = "Fixed black-to-white grayscale, no independent autoscale; mask white means blended. NPY keeps full resolution and half DN."
    return artifacts


def execute(service, job):
    from .imaging import store_output
    from .retained_isp_source import load_inputs

    fresh = _verify_source(service, job, current_environment=True)
    raw = load_inputs(service, fresh["source"])
    output = process_verified(fresh["source"], raw, fresh["specification"])
    artifacts = _previews(service, output)
    return store_output(service, output, source_kind="processed",
                        geometry=fresh["source"]["geometry"], input_artifacts=artifacts)


def get(service, identity):
    job = service.registry.get_run(identity)
    _verify_source(service, job)
    if job.get("status") == "succeeded":
        from .retained_isp_source import array_sha256

        summary = job["result"]["summary"]
        if (canonical(summary.get("specification")) != canonical(job["specification"])
                or canonical(summary.get("acquisition")) != canonical(acquisition_context(job["source"]))):
            raise IntegrityError("Saved replay source or settings differ")
        for artifact in job["artifacts"]:
            path = service.registry.blob_path(artifact["sha256"])
            if path.stat().st_size != artifact["bytes"]:
                raise IntegrityError("Replay artifact byte count differs")
            if artifact["name"] == "raw_frames.npy":
                with path.open("rb") as handle:
                    raw = np.load(handle, allow_pickle=False)
                if array_sha256(raw) != job["source"]["selected_array_sha256"]:
                    raise IntegrityError("Replay changed the original selected RAW")
            if artifact["name"] == "summary.json" and path.read_bytes() != canonical(summary).encode():
                raise IntegrityError("Saved replay summary bytes differ")
    return job


def list_records(service):
    """Metadata-only discovery. Never reread/reprocess every source RAW on list."""
    return [{key: deepcopy(job.get(key)) for key in ("id", "name", "status", "created_at", "error")}
            for job in service.registry.list_runs() if job.get("job_kind") == JOB_KIND]


def install_routes(app, service):
    @app.get("/api/v1/raw-temporal/sources", tags=["raw_temporal"])
    def sources():
        from .retained_isp_replay import choices
        return choices(service)

    @app.post("/api/v1/raw-temporal/replays", status_code=202, tags=["raw_temporal"])
    def create_replay(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/raw-temporal/replays", tags=["raw_temporal"])
    def replay_list():
        return list_records(service)

    @app.get("/api/v1/raw-temporal/replays/{identity}", tags=["raw_temporal"])
    def replay_get(identity: str):
        return get(service, identity)

    @app.get("/api/v1/raw-temporal/replays/{identity}/export", tags=["raw_temporal"])
    def replay_export(identity: str):
        from fastapi.responses import Response
        job = get(service, identity)
        return Response(canonical(job), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="raw-temporal-replay.json"'})
