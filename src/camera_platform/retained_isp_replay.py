"""Immutable code-domain ISP replay of one original retained RAW readout path.

Source snapshots and charge ledgers are evidence only. The processor receives
selected integer codes, their actual format, a fixed profile and row timing.
"""

from __future__ import annotations

import hashlib
import io
from copy import deepcopy
from typing import Literal

import numpy as np
from pydantic import Field, field_validator

from .contracts import Contract, ModelCreate
from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_retained_code_isp_replay_v1"
PLUGIN_ID = "retained_isp_replay_v1"
PROFILE_SCHEMA = "camera_retained_code_isp_profile_v1"
MAX_ARCHIVE_BYTES = 256 * 1024**2
SUMMARY_FIXED_BYTES = 64 * 1024
SUMMARY_FRAME_BYTES = 16 * 1024
MAX_STAGE_PREVIEWS = 16


class ReplayCreate(Contract):
    schema: Literal["camera_retained_code_isp_replay_v1"]
    name: str = Field(min_length=1, max_length=200)
    source_kind: Literal["run"] = "run"
    source_id: str = Field(min_length=1, max_length=200)
    selector: dict
    isp_model_id: str = Field(min_length=1, max_length=200)
    force: bool = False

    @field_validator("selector")
    @classmethod
    def selection(cls, value):
        from .retained_isp_source import validate_selector

        return validate_selector(value)


def choices(service):
    """Discovery is metadata-only; selected bytes are verified during preparation."""
    result = []
    for record in service.list_simulation_runs():
        plugin = record.get("plugin_id")
        if (
            plugin not in ("retained_charge_v1", "electrical_control_v1")
            or record.get("status") != "succeeded"
        ):
            continue
        summary = (record.get("result") or {}).get("summary", {})
        entries = [a for a in record.get("artifacts", []) if a.get("name") == "raw_groups.npy"]
        if len(entries) != 1:
            continue
        artifact = entries[0]
        shape = artifact.get("shape", [])
        formats = summary.get("raw_formats", [])
        readouts = summary.get("readout_ids", [])
        if len(shape) != 4 or len(formats) != len(readouts) or shape[1] != len(readouts):
            continue
        electrical = plugin == "electrical_control_v1"
        if electrical and readouts not in (["single"], ["HCG", "LCG"]):
            continue
        observation = summary.get("electrical_control", {})
        explicit_observation = electrical and (
            len(readouts) == 2
            or summary.get("capture_program", {}).get("schema")
            == "camera_electrical_control_capture_program_v3"
        )
        if (
            explicit_observation
            and (
                observation.get("readout_id") not in readouts
                or type(observation.get("observation_readout_index")) is not int
                or observation["observation_readout_index"]
                != readouts.index(observation["readout_id"])
            )
        ):
            continue
        for index, readout in enumerate(readouts):
            if readout not in ("single", "HCG", "LCG"):
                continue
            choice = {
                "source_kind": "run",
                "source_id": record["id"],
                "name": record["name"] + " · " + readout,
                "selector": {
                    "schema": "camera_retained_raw_selection_v1",
                    "readout_id": readout,
                },
                "readout_id": readout,
                "readout_index": index,
                "raw_container_shape": shape,
                "raw_shape": [shape[0], *shape[-2:]] if len(shape) == 4 else [],
                "raw_sha256": artifact["sha256"],
                "raw_format": formats[index],
                "geometry": summary.get("geometry"),
                "clock": summary.get("clock"),
                "processing_domain": "delivered_signal_code_domain",
                "integrity": "verified_on_selection",
                "physical_validation": "not_established",
            }
            if electrical:
                # Discovery exposes saved metadata only. The source adapter checks
                # the actual RAW/controller/timing closure on selection.
                choice.update(
                    source_plugin_id=plugin,
                    acquisition_mode="closed_loop",
                    actual_frame_conditions=deepcopy(summary.get("frame_conditions", [])),
                )
                if explicit_observation:
                    choice.update(
                        observation_readout_id=observation["readout_id"],
                        observation_readout_index=observation["observation_readout_index"],
                    )
            result.append(choice)
    return result


def template(service, *, source_id, readout_id):
    from .imaging import resolve_isp
    from .retained_isp import default_profile
    from .retained_isp_source import resolve

    source = resolve(
        service, source_id, {"schema": "camera_retained_raw_selection_v1", "readout_id": readout_id}
    )
    original = source["source_snapshot"]
    assembly = service.registry.get("assemblies", original["assembly_id"])
    parent = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "isp"
    )
    profile = default_profile(resolve_isp(parent))
    fields = {
        key: deepcopy(value) for key, value in parent.items() if key in ModelCreate.model_fields
    }
    fields.update(
        name="잔류 전하 RAW · 코드 영역 ISP",
        revision="retained-code-1",
        parents=[parent["id"]],
        payload=profile,
        input_raw_format=deepcopy(source["raw_format"]),
        source_kind="estimated",
        source_uri="authoring://retained-code-isp-profile",
        physical_instance=None,
        method_and_version=PROFILE_SCHEMA + ":declared_tuning",
    )
    return fields


def copied_summary_fields(job):
    source = job["source"]
    frames = deepcopy(source["frame_records"])
    for frame in frames:
        frame["replay_isp_model_id"] = job["isp_model_snapshot"]["id"]
    keys = (
        "selector",
        "raw_sha256",
        "raw_container_shape",
        "raw_shape",
        "selected_array_sha256",
        "selected_array_hash_encoding",
        "raw_format",
        "geometry",
        "pixel_pitch_um",
        "clock",
        "timing",
        "frame_conditions",
        "retained_charge_events",
        "readout_id",
        "readout_index",
        "acquisition_identity",
        "source_provenance",
        "processing_contract",
        "capture_program_id",
        "capture_program_hash",
        "capture_program_sha256",
        "source_plugin_id",
        "acquisition_mode",
        "planning_contract",
        "realized_acquisition",
        "control_trace",
        "control_trace_role",
    )
    return {
        **{key: deepcopy(source[key]) for key in keys if key in source},
        "plugin_id": PLUGIN_ID,
        "job_kind": "isp_replay",
        "source_id": source["source_id"],
        "source_kind": source["source_provenance"]["source_kind"],
        "conditions": deepcopy(source["conditions"]),
        "acquisition_conditions": deepcopy(source["conditions"]),
        "frame_count": len(frames),
        "frames": frames,
        "replay_isp_model_id": job["isp_model_snapshot"]["id"],
        "source_snapshot_role": "evidence_only_not_processing_inputs",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
    }


def summary_forecast(job):
    copied = len(canonical(copied_summary_fields(job)).encode()) + len(
        canonical(job["profile"]).encode()
    )
    return {
        "schema": "camera_retained_code_summary_budget_v1",
        "copied_summary_bytes": copied,
        "generated_summary_bytes_limit": SUMMARY_FIXED_BYTES
        + len(job["source"]["frame_records"]) * SUMMARY_FRAME_BYTES,
        "summary_artifact_bytes_limit": copied
        + SUMMARY_FIXED_BYTES
        + len(job["source"]["frame_records"]) * SUMMARY_FRAME_BYTES,
    }


def forecast(service, frozen, resources, raw_bytes):
    """Count original run/factory closure plus bounded outputs before queue publication."""
    manifest = service.export_run(frozen["source_id"])
    records = [manifest["run"], *manifest.get("factory_jobs", [])]
    entries = {
        (a["sha256"], a["name"]): a
        for record in records
        for a in [*record.get("artifacts", []), *record.get("input_artifacts", [])]
    }
    entries.update({(a["sha256"], a["name"]): a for a in frozen["input_artifacts"]})
    source_bytes = sum(a["bytes"] for a in entries.values())
    frames, height, width = frozen["source"]["raw_shape"]
    # PNG: RGB8 scanlines + zlib's bounded overhead + PNG chunks, conservatively.
    one_png = height * (width * 3 + 1) + height * width // 100 + 4096
    png_bytes = one_png * (frames * (MAX_STAGE_PREVIEWS + 1) + 1)
    summary = summary_forecast(frozen)
    arrays = resources["output_array_bytes_bound"] + raw_bytes
    # At most 64 processor arrays: NPY headers are independently bounded here.
    output_bytes = arrays + 64 * 4096 + png_bytes + summary["summary_artifact_bytes_limit"]
    # v1 replay export repeats source/model documents in its complete logical JSON.
    json_bytes = (
        len(canonical(manifest).encode())
        + len(canonical({k: v for k, v in frozen.items() if k != "environment"}).encode())
        + summary["summary_artifact_bytes_limit"] * 2
        + 1024 * 1024
    )
    total = source_bytes + output_bytes + json_bytes
    if (
        type(resources["output_array_bytes_bound"]) is not int
        or type(resources["working_array_bytes_bound"]) is not int
        or resources["output_array_bytes_bound"] > 128 * 1024**2
        or resources["working_array_bytes_bound"] > 1024**3
        or total > MAX_ARCHIVE_BYTES
    ):
        raise ValueError(
            "Retained ISP replay and original evidence exceed bounded memory/archive budget"
        )
    return {
        "schema": "camera_retained_code_replay_budget_v1",
        **resources,
        "source_artifact_bytes": source_bytes,
        "selected_raw_bytes": raw_bytes,
        "output_artifact_bytes_bound": output_bytes,
        "png_bytes_bound": png_bytes,
        "logical_manifest_bytes_bound": json_bytes,
        "archive_bytes_bound": total,
        "archive_bytes_limit": MAX_ARCHIVE_BYTES,
        "summary": summary,
        "max_processor_arrays": 64,
        "max_stage_previews": MAX_STAGE_PREVIEWS,
    }


def _format_comparison_key(raw):
    """Compare only the real-valued black level independent of JSON number spelling.

    Browser JSON writes 64.0 as 64. Keep every other field's canonical type and
    preserve the original model/source documents in frozen evidence.
    """
    from .imaging import _format

    formatted = _format(raw)
    black = formatted["black_level_dn"]
    if type(black) not in (int, float):
        raise ValueError("Retained code RAW black level requires a finite non-boolean number")
    try:
        black = float(black)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Retained code RAW black level exceeds finite numeric bounds") from exc
    if not np.isfinite(black):
        raise ValueError("Retained code RAW black level requires a finite number")
    formatted["black_level_dn"] = black
    return canonical(formatted)


def prepare(service, payload):
    from .imaging import _current
    from .retained_isp import validate_inputs, validate_profile
    from .retained_isp_source import load_inputs, resolve
    from .validation import check_domain

    data = ReplayCreate.model_validate(payload).model_dump()
    data.pop("force")
    source = resolve(service, data["source_id"], data["selector"])
    model = service.registry.get("models", data["isp_model_id"])
    if model["kind"] != "isp":
        raise ValueError("Retained code replay requires an explicit ISP model")
    profile = validate_profile(model["payload"])
    if _format_comparison_key(model["input_raw_format"]) != _format_comparison_key(
        source["raw_format"]
    ):
        raise ValueError("Retained code ISP format differs from the original selected readout")
    for frame in source["frame_conditions"]:
        conditions = {
            **source["conditions"],
            "exposure_s": {"value": frame["exposure_ns"] * 1e-9, "unit": "s"},
            "gain": {"value": frame["gain"], "unit": "1"},
        }
        if check_domain(conditions, model["valid_domain"]):
            raise ValueError("Code ISP applicability does not cover every original frame")
    raw = load_inputs(service, source)
    parsed = validate_inputs(
        raw,
        source["raw_format"],
        profile,
        geometry=source["geometry"],
        frame_records=source["frame_records"],
        events=source["retained_charge_events"],
    )
    artifacts = deepcopy(source["input_artifacts"])
    for artifact in artifacts:
        if artifact["name"] == "summary.json":
            artifact["name"] = "acquisition-summary.json"
    frozen = {
        **data,
        "job_kind": "isp_replay",
        "plugin_id": PLUGIN_ID,
        "source": source,
        "isp_model_snapshot": model,
        "profile": profile,
        "environment": _current(service),
        "input_artifacts": artifacts,
    }
    frozen["evidence_forecast"] = forecast(service, frozen, parsed["resources"], raw.nbytes)
    return frozen


def _verify_inputs(service, job, *, current_environment):
    payload = {key: job[key] for key in ReplayCreate.model_fields if key in job}
    fresh = prepare(service, payload)
    for key, value in fresh.items():
        if key == "environment" and not current_environment:
            continue
        if canonical(job.get(key)) != canonical(value):
            raise IntegrityError(
                "Frozen retained code replay differs from original source/profile: " + key
            )
    return fresh


def verify(service, job):
    _verify_inputs(service, job, current_environment=False)
    result = job.get("result")
    if result is not None:
        for artifact in result.get("artifacts", []):
            content = service.registry.blob_path(artifact["sha256"]).read_bytes()
            if (
                len(content) != artifact["bytes"]
                or hashlib.sha256(content).hexdigest() != artifact["sha256"]
            ):
                raise IntegrityError("Saved retained ISP artifact bytes differ")
        summary = result["summary"]
        if (
            canonical(summary.get("profile")) != canonical(job["profile"])
            or summary.get("profile_sha256") != digest(job["profile"])
            or summary.get("source_truth_arrays_used") is not False
            or summary.get("charge_inversion") is not False
            or summary.get("dark_subtraction") is not False
            or canonical(summary.get("evidence_forecast")) != canonical(job["evidence_forecast"])
        ):
            raise IntegrityError("Retained replay profile or processing contract differs")
        raw_entries = [a for a in result["artifacts"] if a["name"] == "raw_frames.npy"]
        if len(raw_entries) != 1:
            raise IntegrityError("Retained replay must preserve one selected RAW array")
        raw = np.load(service.registry.blob_path(raw_entries[0]["sha256"]), allow_pickle=False)
        from .retained_isp_source import array_sha256

        if (
            list(raw.shape) != job["source"]["raw_shape"]
            or array_sha256(raw) != job["source"]["selected_array_sha256"]
            or summary.get("input_array_bytes_sha256")
            != hashlib.sha256(raw.tobytes(order="C")).hexdigest()
        ):
            raise IntegrityError("Retained replay RAW differs from its original selected codes")
        for key, value in copied_summary_fields(job).items():
            # Stored frame previews add immutable PNG identities after processing.
            if key == "frames":
                for expected, saved in zip(value, summary.get("frames", []), strict=True):
                    if any(canonical(saved.get(k)) != canonical(v) for k, v in expected.items()):
                        raise IntegrityError("Retained replay frame identity differs")
            elif canonical(summary.get(key)) != canonical(value):
                raise IntegrityError("Retained replay source summary binding differs: " + key)
        entries = [a for a in result["artifacts"] if a["name"] == "summary.json"]
        if (
            len(entries) != 1
            or service.registry.blob_path(entries[0]["sha256"]).read_bytes()
            != canonical(summary).encode()
        ):
            raise IntegrityError("Retained replay summary differs from its immutable artifact")
    return job


def submit(service, payload):
    frozen = prepare(service, payload)
    job = service.registry.submit(frozen, digest(frozen), force=payload.get("force", False))
    return verify(service, job)


def _stage_previews(service, output):
    from PIL import Image

    stages = output["summary"].get("stage_outputs", {})
    names = sorted({stage["array"] for stage in stages.values()})
    if len(names) > MAX_STAGE_PREVIEWS:
        raise IntegrityError("Retained ISP stage preview count exceeds its forecast")
    artifacts, previews = [], []
    for name in names:
        for index, array in enumerate(output["arrays"][name]):
            # Code-stage display is fixed ADC black-to-white normalization;
            # floating arrays retain original DN and are never auto-scaled.
            pixels = array.astype(float)
            if pixels.ndim == 2:
                fmt = output["summary"]["raw_format"]
                pixels = pixels / (2 ** fmt["bit_depth"] - 1 - fmt["black_level_dn"])
                pixels = np.repeat(pixels[..., None], 3, axis=-1)
            stream = io.BytesIO()
            Image.fromarray((np.clip(pixels, 0, 1) * 255).astype(np.uint8)).save(stream, "PNG")
            artifact = service.registry.store_blob(
                stream.getvalue(), f"retained-isp-{name}-{index:03d}.png", "image/png"
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
                    "display_transfer": "fixed_code_range_no_autoscale",
                }
            )
    output["summary"]["stage_previews"] = previews
    return artifacts


def execute(service, job):
    from .imaging import store_frame_previews, store_output
    from .retained_isp import process_raw
    from .retained_isp_source import load_inputs

    fresh = _verify_inputs(service, job, current_environment=True)
    source = fresh["source"]
    raw = load_inputs(service, source)
    original_hash = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    output = process_raw(
        raw,
        source["raw_format"],
        fresh["profile"],
        geometry=source["geometry"],
        frame_records=source["frame_records"],
        events=source["retained_charge_events"],
    )
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != original_hash:
        raise IntegrityError("Code ISP changed its original selected RAW input")
    output["arrays"]["raw_frames"] = raw.copy()
    output["summary"].setdefault("array_units", {})["raw_frames"] = "DN"
    output["summary"].update(copied_summary_fields(fresh))
    output["summary"]["evidence_forecast"] = deepcopy(fresh["evidence_forecast"])
    if (
        len(output["arrays"]) > 64
        or sum(a.nbytes for a in output["arrays"].values())
        > fresh["evidence_forecast"]["output_array_bytes_bound"] + raw.nbytes
    ):
        raise IntegrityError("Retained ISP array output exceeds its prequeue forecast")
    artifacts = deepcopy(fresh["input_artifacts"])
    artifacts.extend(store_frame_previews(service, output))
    artifacts.extend(_stage_previews(service, output))
    if (
        len(canonical(output["summary"]).encode())
        > summary_forecast(fresh)["summary_artifact_bytes_limit"]
    ):
        raise IntegrityError("Retained ISP summary exceeds its prequeue forecast")
    return store_output(
        service,
        output,
        source_kind=source["source_provenance"]["source_kind"],
        geometry=source["geometry"],
        input_artifacts=artifacts,
    )
