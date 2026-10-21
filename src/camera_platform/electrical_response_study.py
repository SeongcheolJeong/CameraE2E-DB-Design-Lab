"""Durable matched fixed-exposure/AE experiments over actual camera child jobs.

Planning stages immutable programs and Tests in memory. The registry publishes
all children and their dependent aggregate atomically; the worker executes the
ordinary camera jobs before this aggregate evaluates their retained integer RAW.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import tempfile
import zipfile
from copy import copy, deepcopy
from pathlib import Path

import numpy as np

from . import capture_programs, electrical_response, sequences
from .contracts import CaptureProgramCreate, RunCreate
from .imaging import _current
from .registry import ConflictError, IntegrityError, canonical, digest
from .retained_isp_comparison import _artifacts, _verify_artifacts
from .timing import MAX_TIME_NS

SCHEMA = "camera_electrical_response_study_v1"
JOB_KIND = "electrical_response_study"
PLUGIN_ID = "electrical_response_study_v1"
AXES = (
    "waveform.phase_ns",
    "electrical_control_policy.latency.actuator_ns",
    "electrical_control_policy.latency.controller_ns",
)
MAX_CELLS = 12
MAX_ARCHIVE_BYTES = 256 * 1024**2
MAX_SUMMARY_BYTES = 4 * 1024**2
JOB_KEYS = {
    "id",
    "status",
    "cache_key",
    "content_hash",
    "created_at",
    "updated_at",
    "result",
    "error",
    "artifacts",
    "cache_hit",
}
AUTHORITY = {
    "physical_validation": "not_established",
    "physical_measurement_count": 0,
    "uncertainty": {"kind": "unknown"},
    "automatic_selection": False,
    "model_promotion": False,
    "current_exposure_photocharge_estimation": False,
    "evaluation_input": "original_integer_raw_and_actual_timing_only",
}


def _keys(value, required, label, optional=()):
    if (
        not isinstance(value, dict)
        or not set(required) <= value.keys()
        or value.keys() - set(required) - set(optional)
    ):
        raise ValueError(label + " requires exactly its declared fields")


def _text(value, label):
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 200
    ):
        raise ValueError(label + " requires bounded nonblank text")
    return value


def validate_specification(payload):
    keys = {
        "schema",
        "name",
        "assembly_id",
        "scene_sequence_id",
        "control_sequence_id",
        "capture_program_id",
        "evaluation",
        "axes",
        "seeds",
        "noise_enabled",
    }
    _keys(payload, keys, "electrical response study", {"force"})
    if payload["schema"] != SCHEMA or type(payload.get("force", False)) is not bool:
        raise ValueError("Unsupported study schema or nonboolean force")
    result = {k: deepcopy(payload[k]) for k in keys}
    for key in keys - {"schema", "evaluation", "axes", "seeds", "noise_enabled"}:
        _text(result[key], key)
    if type(result["noise_enabled"]) is not bool:
        raise ValueError("noise_enabled must be an explicit boolean")
    seeds = result["seeds"]
    if (
        not isinstance(seeds, list)
        or not 1 <= len(seeds) <= MAX_CELLS
        or any(type(v) is not int or not 0 <= v <= 2**32 - 1 for v in seeds)
        or len(set(seeds)) != len(seeds)
    ):
        raise ValueError("Study requires unique explicit bounded integer seeds")
    axes = result["axes"]
    if not isinstance(axes, list) or len(axes) > 2:
        raise ValueError("Study permits zero to two finite axes")
    count, names = len(seeds), set()
    for axis in axes:
        _keys(axis, {"parameter", "values"}, "axis")
        name, values = axis["parameter"], axis["values"]
        if name not in AXES or name in names:
            raise ValueError("Study axes must be distinct declared phase or latency parameters")
        names.add(name)
        lower = -MAX_TIME_NS if name == "waveform.phase_ns" else 0
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= MAX_CELLS
            or any(type(v) is not int or not lower <= v <= MAX_TIME_NS for v in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError("Axis values must be unique finite integer nanoseconds")
        count *= len(values)
    if count > MAX_CELLS:
        raise ValueError("At most twelve condition/seed cells and twenty-four actual captures")
    result["evaluation"] = electrical_response.validate_evaluation(result["evaluation"])
    return result


class _MemoryBlob:
    def __init__(self, data):
        self.data = data

    def read_bytes(self):
        return self.data

    def read_text(self, encoding="utf-8"):
        return self.data.decode(encoding)

    def open(self, mode="rb"):
        if mode != "rb":
            raise ValueError("Planning blobs are read only")
        return io.BytesIO(self.data)


class _DraftRegistry:
    """Operation-local records/blobs; no files or registry rows are created by plan."""

    def __init__(self, base, stamp):
        self.base, self.stamp = base, stamp
        self.directory = base.directory
        self.records, self.blobs = {}, {}

    def __getattr__(self, key):
        if key.startswith(
            ("put", "store", "submit", "publish", "finish", "cancel", "claim", "transaction")
        ):
            raise ValueError("Planning cannot mutate registry state")
        return getattr(self.base, key)

    def get(self, kind, identity):
        if (kind, identity) in self.records:
            return deepcopy(self.records[kind, identity])
        return self.base.get(kind, identity)

    def put(self, kind, document):
        if kind not in {"capture_programs", "tests"}:
            raise ValueError("Only derived capture programs and Tests may be staged")
        identity = kind.rstrip("s") + "-electrical-response-" + digest(document)
        record = {
            **deepcopy(document),
            "id": identity,
            "created_at": self.stamp,
            "created_at_basis": "source_capture_program_time; deterministic_prospective_variant",
        }
        record["content_hash"] = digest(record)
        try:
            existing = self.base.get(kind, identity)
        except KeyError:
            existing = record
        if canonical(existing) != canonical(record):
            raise IntegrityError("Derived response-study record identity collision")
        self.records[kind, identity] = record
        return deepcopy(record)

    def store_blob(self, data, name, media_type):
        sha = hashlib.sha256(data).hexdigest()
        descriptor = {
            "id": sha,
            "sha256": sha,
            "bytes": len(data),
            "name": Path(name).name,
            "media_type": media_type,
            "url": f"/api/v1/artifacts/{sha}",
        }
        self.blobs[sha, descriptor["name"]] = (descriptor, bytes(data))
        return deepcopy(descriptor)

    def blob_path(self, sha):
        for (checksum, _), (_, data) in self.blobs.items():
            if checksum == sha:
                return _MemoryBlob(data)
        return self.base.blob_path(sha)


def _sources(service, spec):
    kinds = {
        "assembly_id": "assemblies",
        "scene_sequence_id": "scene_sequences",
        "control_sequence_id": "control_sequences",
        "capture_program_id": "capture_programs",
    }
    sources = {k: service.registry.get(kind, spec[k]) for k, kind in kinds.items()}
    program = sources["capture_program_id"]
    capture_programs.verify_program(service, program, "electrical_control_v1")
    if (
        program["assembly_id"] != spec["assembly_id"]
        or sources["control_sequence_id"]["assembly_id"] != spec["assembly_id"]
        or sources["control_sequence_id"]["scene_sequence_id"] != spec["scene_sequence_id"]
    ):
        raise ValueError("All study sources must belong to one fixed native acquisition")
    model_ids = set(sources["assembly_id"]["model_ids"])
    model_ids.update(
        f["applied"]["isp_model_id"] for f in sources["control_sequence_id"]["payload"]["frames"]
    )
    sources["models"] = [service.registry.get("models", i) for i in sorted(model_ids)]
    from .native_model_evidence import verify_models

    verify_models(service, sources["models"])
    scene = sources["scene_sequence_id"]
    if canonical(sequences._images(service, scene["payload"])) != canonical(
        scene["image_snapshots"]
    ):
        raise IntegrityError("Original scene image records differ from their frozen sequence")
    _verify_artifacts(service, _artifacts(sources))
    return sources


def template(service, assembly_id, scene_sequence_id, control_sequence_id, capture_program_id):
    spec = {
        "schema": SCHEMA,
        "name": "고정 노출 · 전기 AE 응답 비교",
        "assembly_id": assembly_id,
        "scene_sequence_id": scene_sequence_id,
        "control_sequence_id": control_sequence_id,
        "capture_program_id": capture_program_id,
        "axes": [],
        "seeds": [731],
        "noise_enabled": False,
    }
    sources = _sources(service, spec)
    scene, control, program = (
        sources[k] for k in ("scene_sequence_id", "control_sequence_id", "capture_program_id")
    )
    policy = program["payload"]["electrical_control_policy"]
    waveform = program["payload"]["waveform"]
    spec["evaluation"] = {
        "schema": electrical_response.SCHEMA,
        "roi": deepcopy(policy["statistics_roi"]),
        "sites": deepcopy(policy["meter"]["sites"]),
        "target_black_relative_dn": deepcopy(policy["target_black_relative_dn"]),
        "clock": deepcopy(control["payload"]["clock"]),
        "window": {
            "start_ns": scene["payload"]["knots"][0]["time_ns"],
            "end_ns": scene["payload"]["knots"][-1]["time_ns"],
            "change_point_ns": control["payload"]["frames"][0]["frame_start_ns"],
        },
        "frame_ids": [f["frame_id"] for f in control["payload"]["frames"]],
        "stimulus": {
            "kind": "pwm" if waveform and waveform["kind"] == "pwm" else "constant_step",
            "declaration": "authored_simulation",
        },
        "hold": {"consecutive_observations": 2, "minimum_span_ns": 0},
        "crossing_exposure_policy": "exclude",
    }
    spec = validate_specification(spec)
    _validate_stimulus(spec, sources)
    return spec


def _validate_stimulus(spec, sources):
    """Qualify an authored step against frozen inputs, never infer it from RAW."""
    evaluation = spec["evaluation"]
    waveform = sources["capture_program_id"]["payload"]["waveform"]
    kind = "pwm" if waveform is not None and waveform["kind"] == "pwm" else "constant_step"
    if evaluation["stimulus"]["kind"] != kind:
        raise ValueError("Evaluation stimulus declaration differs from capture waveform")
    if kind == "pwm":
        return {
            "kind": kind,
            "step_settling": "not_applicable",
            "scope": "authored_pwm_no_constant_step_claim",
        }
    scene = sources["scene_sequence_id"]
    knots = scene["payload"]["knots"]
    start, end = (evaluation["window"][key] for key in ("change_point_ns", "end_ns"))
    if start < knots[0]["time_ns"] or end > knots[-1]["time_ns"]:
        raise ValueError("Declared constant-step post-change window must be covered by the scene")

    def identity(image):
        arrays = [a for a in image["artifacts"] if a["name"] == "spectral-image.npy"]
        if len(arrays) != 1:
            raise IntegrityError("Constant-step source requires one original irradiance payload")
        return {
            "irradiance_sha256": arrays[0]["sha256"],
            **{
                key: image[key]
                for key in ("shape", "wavelength_nm", "sampling", "unit", "reference_plane")
            },
        }

    identities = [identity(image) for image in scene["image_snapshots"]]
    selected = []
    interpolation = scene["payload"]["interpolation"]
    for index, (left, right) in enumerate(zip(knots, knots[1:], strict=False)):
        if max(start, left["time_ns"]) >= min(end, right["time_ns"]):
            continue
        if interpolation == "linear_irradiance" and identities[index] != identities[index + 1]:
            raise ValueError(
                "Declared constant-step post-change irradiance varies within a linear segment"
            )
        if interpolation not in {"linear_irradiance", "zero_order_hold"}:
            raise ValueError(
                "Unknown interpolation cannot establish constant post-change irradiance"
            )
        selected.append(identities[index])
    if not selected or any(item != selected[0] for item in selected):
        raise ValueError("Declared constant-step post-change irradiance varies across the window")
    return {
        "kind": kind,
        "scope": "authored_static_irradiance_after_declared_change",
        "post_change_interval_ns": [start, end],
        "interval_convention": "half_open",
        "interpolation": interpolation,
        "irradiance_identity": deepcopy(selected[0]),
        "physical_step_timing": "not_established",
    }


def _prepare(service, payload):
    spec = validate_specification(payload)
    environment, sources = _current(service), _sources(service, spec)
    scene, control, original = (
        sources[k] for k in ("scene_sequence_id", "control_sequence_id", "capture_program_id")
    )
    shape = scene["image_snapshots"][0]["shape"][:2]
    frames = control["payload"]["frames"]
    evaluation = electrical_response.validate_evaluation(
        spec["evaluation"], frame_ids=[f["frame_id"] for f in frames], frame_shape=shape
    )
    if evaluation["clock"] != control["payload"]["clock"]:
        raise ValueError("Evaluation and original acquisition clocks differ")
    waveform = original["payload"]["waveform"]
    qualification = _validate_stimulus(spec, sources)
    basis = {
        "schema": SCHEMA,
        "stimulus_qualification": qualification,
        "specification": spec,
        "source_snapshots": sources,
        "environment": environment,
    }
    variant_key = digest(basis)
    draft = copy(service)
    draft.registry = _DraftRegistry(service.registry, original["created_at"])
    coordinates = [
        dict(zip([a["parameter"] for a in spec["axes"]], values, strict=True))
        for values in itertools.product(*(a["values"] for a in spec["axes"]))
    ]
    cells, children = [], []
    for coordinate in coordinates:
        for seed in spec["seeds"]:
            cell_id = f"cell-{len(cells):03d}"
            cell = {"cell_id": cell_id, "coordinates": coordinate, "seed": seed}
            for role in ("baseline", "candidate"):
                data = {
                    k: deepcopy(v)
                    for k, v in original.items()
                    if k in CaptureProgramCreate.model_fields
                }
                data.update(
                    name=spec["name"][:120] + " · " + cell_id + " · " + role,
                    revision="response-" + variant_key[:24] + "-" + cell_id + "-" + role,
                    parents=[original["id"]],
                    source_kind="estimated",
                    physical_instance=None,
                    source_uri="study://electrical-response/" + variant_key,
                    method_and_version="matched_fixed_vs_electrical_ae:1",
                )
                for parameter, value in coordinate.items():
                    if parameter == "waveform.phase_ns":
                        if waveform is None or waveform["kind"] != "pwm":
                            raise ValueError("PWM phase axis requires an explicit PWM waveform")
                        data["payload"]["waveform"]["phase_origin_ns"] = value
                    else:
                        data["payload"]["electrical_control_policy"]["latency"][
                            parameter.rsplit(".", 1)[1]
                        ] = value
                data["payload"]["electrical_control_policy"]["enabled"] = role == "candidate"
                program = capture_programs.create(draft, data)
                test_data = sequences.test_template(
                    draft,
                    spec["assembly_id"],
                    spec["scene_sequence_id"],
                    spec["control_sequence_id"],
                    program["id"],
                )
                test_data.update(name=data["name"], revision=data["revision"])
                test = draft.create_test(test_data)
                request = RunCreate.model_validate(
                    {
                        "assembly_id": spec["assembly_id"],
                        "scene_sequence_id": spec["scene_sequence_id"],
                        "control_sequence_id": spec["control_sequence_id"],
                        "capture_program_id": program["id"],
                        "test_id": test["id"],
                        "plugin_id": "electrical_control_v1",
                        "parameters": {"seed": seed, "noise_enabled": spec["noise_enabled"]},
                    }
                ).model_dump()
                frozen, _ = sequences.prepare_run(draft, request)
                children.append({"cell_id": cell_id, "role": role, "request": frozen})
                cell[role] = {"capture_program_id": program["id"], "test_id": test["id"]}
            cells.append(cell)
    # Every repeated source is stored only once in a ZIP; every actual child output
    # still has its own full retained ledger, readout and previews budgeted here.
    artifacts = _artifacts([sources, [c["request"]["input_artifacts"] for c in children]])
    source_bytes = sum(a["bytes"] for a in artifacts)
    array_bytes = sum(
        c["request"]["resources"]["output_array_bytes_estimate"] + 256 * 512 for c in children
    )
    png_bytes = len(children) * (len(frames) + 1) * (shape[0] * shape[1] * 3 + 4096)
    child_summary = sum(1024**2 + 2 * len(canonical(c["request"]).encode()) for c in children)
    manifest_bytes = (
        MAX_SUMMARY_BYTES + 6 * len(canonical([sources, children]).encode()) + 3 * child_summary
    )
    archive_bytes = (
        source_bytes
        + array_bytes
        + png_bytes
        + child_summary
        + manifest_bytes
        + MAX_SUMMARY_BYTES
        + len(children) * 1024**2
    )
    if archive_bytes > MAX_ARCHIVE_BYTES:
        raise ValueError("Complete paired capture/source/evaluation closure exceeds 256 MiB budget")
    cost = {
        "schema": "camera_electrical_response_study_budget_v1",
        "cell_count": len(cells),
        "capture_run_count": len(children),
        "source_artifact_bytes": source_bytes,
        "output_array_bytes_bound": array_bytes,
        "preview_bytes_bound": png_bytes,
        "child_summary_bytes_bound": child_summary,
        "summary_bytes_limit": MAX_SUMMARY_BYTES,
        "manifest_bytes_bound": manifest_bytes,
        "archive_bytes_bound": archive_bytes,
        "archive_bytes_limit": MAX_ARCHIVE_BYTES,
        "peak_child_working_array_bytes": max(
            c["request"]["resources"]["working_array_bytes_estimate"] for c in children
        ),
        "execution": "serial_camera_children_then_raw_only_aggregate",
    }
    basis["cost"] = cost
    document = {
        "schema": "camera_electrical_response_study_job_v1",
        "job_kind": JOB_KIND,
        "plugin_id": PLUGIN_ID,
        "name": spec["name"],
        "specification": spec,
        "plan_hash_basis": basis,
        "plan_hash": digest(basis),
        "environment": environment,
        "source_snapshots": sources,
        "cells": cells,
        "stimulus_qualification": qualification,
        "cost": cost,
        "input_artifacts": artifacts,
        "planned_requests": children,
        "planned_children": [
            {"cell_id": c["cell_id"], "role": c["role"], "request_sha256": digest(c["request"])}
            for c in children
        ],
        "materialized_records": [
            {"kind": kind, "record": record} for (kind, _), record in draft.registry.records.items()
        ],
    }
    return (
        document,
        [(kind, value) for (kind, _), value in draft.registry.records.items()],
        children,
        draft.registry.blobs,
    )


def plan(service, payload):
    document, _, _, _ = _prepare(service, payload)
    return {
        "plan_hash": document["plan_hash"],
        "frozen_request": document,
        "cost": document["cost"],
    }


def submit(service, payload, plan_hash):
    document, records, children, blobs = _prepare(service, payload)
    if document["plan_hash"] != plan_hash:
        raise ConflictError("Study source/specification/environment changed after planning")
    for descriptor, content in blobs.values():
        if (
            service.registry.store_blob(content, descriptor["name"], descriptor["media_type"])
            != descriptor
        ):
            raise IntegrityError("Published study blob differs from planned canonical bytes")
    job = service.registry.publish_electrical_response(
        document, records, children, force=payload.get("force", False)
    )
    verified = get(service, job["id"])
    return {**verified, **({"cache_hit": True} if job.get("cache_hit") else {})}


def _request(job):
    return {k: v for k, v in job.items() if k not in JOB_KEYS}


def _verify_sources(service, job):
    if job.get("job_kind") != JOB_KIND or job.get("plugin_id") != PLUGIN_ID:
        raise ValueError("Not an electrical response study")
    if (
        digest(job["plan_hash_basis"]) != job["plan_hash"]
        or any(
            canonical(job[key]) != canonical(job["plan_hash_basis"][key])
            for key in (
                "specification",
                "source_snapshots",
                "environment",
                "cost",
                "stimulus_qualification",
            )
        )
        or canonical(job["source_snapshots"]) != canonical(_sources(service, job["specification"]))
    ):
        raise IntegrityError("Study original sources or immutable plan binding changed")
    if (
        _validate_stimulus(job["specification"], job["source_snapshots"])
        != job["stimulus_qualification"]
    ):
        raise IntegrityError("Study authored stimulus qualification changed")
    for item in job["materialized_records"]:
        if canonical(service.registry.get(item["kind"], item["record"]["id"])) != canonical(
            item["record"]
        ):
            raise IntegrityError("Study materialized source record changed")
        if item["kind"] == "capture_programs":
            capture_programs.verify_program(service, item["record"], "electrical_control_v1")
    _verify_artifacts(service, job["input_artifacts"])


def _children(service, job, *, verify_blobs=True):
    if len(job["children"]) != len(job["planned_requests"]):
        raise IntegrityError("Study child manifest count differs")
    results = {}
    for actual, planned in zip(job["children"], job["planned_requests"], strict=True):
        key = (planned["cell_id"], planned["role"])
        owner = {"parent_id": job["id"], "cell_id": key[0], "role": key[1]}
        expected = {**planned["request"], "electrical_response_owner": owner}
        child = service.registry.get_run(actual["run_id"])
        if (
            key in results
            or any(actual[k] != planned[k] for k in ("cell_id", "role"))
            or digest(expected) != actual["request_sha256"]
            or canonical(_request(child)) != canonical(expected)
        ):
            raise IntegrityError("Study child ownership/exact frozen request changed")
        if verify_blobs:
            _verify_artifacts(service, _artifacts([child["input_artifacts"], child.get("result")]))
            if child.get("result") is not None:
                summaries = [a for a in child["artifacts"] if a["name"] == "summary.json"]
                if (
                    len(summaries) != 1
                    or service.registry.blob_path(summaries[0]["sha256"]).read_bytes()
                    != canonical(child["result"]["summary"]).encode()
                ):
                    raise IntegrityError("Child summary differs from its original saved bytes")
        results[key] = child
    if verify_blobs:
        totals = {"arrays": 0, "previews": 0, "summaries": 0}
        for child in results.values():
            inputs = {(a["sha256"], a["name"]) for a in child["input_artifacts"]}
            for artifact in child["artifacts"]:
                if (artifact["sha256"], artifact["name"]) in inputs:
                    continue
                category = (
                    "arrays"
                    if artifact["name"].endswith(".npy")
                    else "previews"
                    if artifact["media_type"] == "image/png"
                    else "summaries"
                    if artifact["name"] == "summary.json"
                    else None
                )
                if category:
                    totals[category] += artifact["bytes"]
        if (
            totals["arrays"] > job["cost"]["output_array_bytes_bound"]
            or totals["previews"] > job["cost"]["preview_bytes_bound"]
            or totals["summaries"] > job["cost"]["child_summary_bytes_bound"]
        ):
            raise IntegrityError("Actual child evidence exceeds the whole-study prequeue bound")
    return results


def _observation(service, child, evaluation):
    if child["status"] != "succeeded":
        return {
            "run_id": child["id"],
            "status": child["status"],
            "error": child["error"],
            "evaluation": None,
            "frames": [],
            "raw_artifact": None,
            "control_trace": None,
        }
    summary = child["result"]["summary"]
    artifacts = [a for a in child["artifacts"] if a["name"] == "raw_groups.npy"]
    if len(artifacts) != 1 or artifacts[0]["bytes"] > 64 * 1024**2:
        raise IntegrityError("Child requires one bounded original RAW container")
    artifact = artifacts[0]
    from .electrical_control_runtime import observation_path, validate_capture_program

    program = validate_capture_program(child["request"]["capture_program"])
    observation_index, observation_id, observation_format = observation_path(program)
    readout_ids = ["HCG", "LCG"] if observation_id != "single" else ["single"]
    expected_shape = (
        len(child["request"]["control_sequence"]["frames"]),
        len(readout_ids),
        *child["scene_sequence_snapshot"]["image_snapshots"][0]["shape"][:2],
    )
    if summary["frame_count"] != expected_shape[0] or summary["output_size"] != list(
        expected_shape[-2:]
    ):
        raise IntegrityError("Child output shape differs from its immutable source geometry")
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    stream = io.BytesIO(content)
    try:
        reader = {
            (1, 0): np.lib.format.read_array_header_1_0,
            (2, 0): np.lib.format.read_array_header_2_0,
        }[np.lib.format.read_magic(stream)]
        shape, fortran, dtype = reader(stream)
    except (KeyError, ValueError, EOFError, TypeError) as error:
        raise IntegrityError("Unsupported original RAW NPY header") from error
    if (
        shape != expected_shape
        or fortran
        or dtype.kind not in "iu"
        or dtype.itemsize > 4
        or dtype.hasobject
        or dtype.fields
        or np.prod(shape) > 1_048_576 * len(readout_ids)
        or int(np.prod(shape)) * dtype.itemsize + stream.tell() != len(content)
    ):
        raise IntegrityError("Original RAW NPY header/bytes exceed the fixed capture bounds")
    raw = np.load(io.BytesIO(content), allow_pickle=False)
    if (
        raw.shape != expected_shape
        or raw.dtype.kind not in "iu"
        or raw.size > 1_048_576 * len(readout_ids)
        or list(raw.shape) != artifact["shape"]
        or str(raw.dtype) != artifact["dtype"]
        or artifact.get("unit") != "DN"
        or artifact.get("spatial_sampling") != "native_sensor"
    ):
        raise IntegrityError(
            "Original child RAW shape/type differs from its declared native frames"
        )
    from . import electrical_control
    from .electrical_control_runtime import retained_program
    from .retained_charge_runtime import _raw_formats, event_schedule
    from .timing import validate_control_sequence

    program, scaffold = child["request"]["capture_program"], child["request"]["control_sequence"]
    formats = _raw_formats(program["electrical_model"])
    if len(readout_ids) == 2:
        if any(
            np.any(raw[:, index] < 0) or np.any(raw[:, index] > 2 ** fmt["bit_depth"] - 1)
            for index, fmt in enumerate(formats)
        ):
            raise IntegrityError("Original paired RAW exceeds a declared path ADC code range")
        if summary.get("preview_readout_id") != readout_ids[0]:
            raise IntegrityError("Paired child preview differs from its original HCG display path")
    actual = validate_control_sequence(
        summary["control_sequence"], geometry=summary["geometry"], frame_shape=expected_shape[-2:]
    )
    if (
        summary["raw_formats"] != formats
        or summary["readout_ids"] != readout_ids
        or summary["electrical_control"]["readout_id"] != observation_id
        or summary["electrical_control"]["raw_format"] != observation_format
        or (
            (observation_id != "single"
             or program["schema"] == "camera_electrical_control_capture_program_v3")
            and summary["electrical_control"].get("observation_readout_index") != observation_index
        )
        or (
            program["schema"] == "camera_electrical_control_capture_program_v3"
            and (
                summary.get("schema_version") != "camera_electrical_control_result_v2"
                or summary["retained_charge"]["model"] != program["retained_charge_model"]
            )
        )
        or canonical(summary["capture_program"]) != canonical(program)
        or actual["clock"] != scaffold["clock"]
        or actual["mode"] != scaffold["mode"]
        or summary["noise_enabled"] != child["parameters"]["noise_enabled"]
        or event_schedule(
            actual, retained_program(program), summary["geometry"], expected_shape[-2:]
        )
        != summary["retained_charge"]["events"]
    ):
        raise IntegrityError(
            "Child actual acquisition lost its original program/format/timing binding"
        )
    trace = summary["electrical_control"]["frames"]
    events = summary["retained_charge"]["events"]["frames"]
    frames = []
    for frame, control, event, planned in zip(
        summary["frames"], trace, events, scaffold["frames"], strict=True
    ):
        if not frame["frame_id"] == control["frame_id"] == event["frame_id"]:
            raise IntegrityError("Child actual frame/timing identities differ")
        if (
            frame["frame_id"] != planned["frame_id"]
            or frame["frame_start_ns"] != planned["frame_start_ns"]
            or any(
                frame["applied"].get(k) != planned["applied"].get(k)
                for k in ("gain", "isp_model_id", "wb_gains_rgb")
            )
            or control["events"]
            != electrical_control.events(
                frame,
                frame["applied"]["exposure_ns"],
                actual["mode"],
                expected_shape[-2],
                program["electrical_control_policy"]["latency"],
            )
        ):
            raise IntegrityError(
                "Child actual frame differs from its fixed scaffold or declared latency"
            )
        frames.append(
            {
                "frame_id": frame["frame_id"],
                "start_ns": frame["frame_start_ns"],
                "applied_exposure_ns": frame["applied"]["exposure_ns"],
                "row_start_ns": event["row_open_ns"],
                "row_end_ns": event["row_transfer_ns"],
                "statistics_ready_ns": control["events"]["statistics_ready_ns"],
            }
        )
    measured = electrical_response.evaluate(
        raw[:, observation_index:observation_index + 1], observation_format, frames, evaluation
    )
    previews = []
    for frame in summary["frames"]:
        preview_id = frame["preview_artifact_id"]
        if not any(
            a["sha256"] == preview_id and a["media_type"] == "image/png" for a in child["artifacts"]
        ):
            raise IntegrityError("Child preview is not bound to a verified artifact")
        previews.append(
            {
                "frame_id": frame["frame_id"],
                "frame_index": frame["frame_index"],
                "preview_artifact_id": preview_id,
                "preview_url": frame["preview_url"],
                "raw_artifact_id": artifact["sha256"],
            }
        )
    result = {
        "run_id": child["id"],
        "status": child["status"],
        "error": child["error"],
        "evaluation": measured,
        "frames": previews,
        "raw_artifact": artifact,
        "raw_format": observation_format,
        "control_trace": deepcopy(summary["electrical_control"]),
        "actual_timing": frames,
        "observation_sha256": digest(
            {
                "raw_sha256": artifact["sha256"],
                "frames": frames,
                "raw_format": observation_format,
                **({"observation_readout_id": observation_id} if len(readout_ids) == 2 else {}),
            }
        ),
    }
    if len(readout_ids) == 2:
        result.update(
            observation_readout_id=observation_id,
            observation_readout_index=observation_index,
            preview_readout_id=summary["preview_readout_id"],
        )
    return result


def _summary(service, job, children):
    cells = []
    for cell in job["cells"]:
        pair = {**deepcopy(cell)}
        for role in ("baseline", "candidate"):
            pair[role] = _observation(
                service, children[cell["cell_id"], role], job["specification"]["evaluation"]
            )
        available = all(pair[r]["evaluation"] is not None for r in ("baseline", "candidate"))
        pair["comparison"] = (
            electrical_response.compare(
                pair["baseline"]["evaluation"], pair["candidate"]["evaluation"]
            )
            if available
            else {
                "status": "missing",
                "reason": "matched_actual_capture_unavailable",
                "baseline_status": pair["baseline"]["status"],
                "candidate_status": pair["candidate"]["status"],
            }
        )
        cells.append(pair)
    complete = sum(
        all(c[r]["status"] == "succeeded" for r in ("baseline", "candidate")) for c in cells
    )
    return {
        "schema": "camera_electrical_response_study_result_v1",
        "plan_hash": job["plan_hash"],
        "specification": deepcopy(job["specification"]),
        "cells": cells,
        "status": "complete"
        if complete == len(cells)
        else "partial"
        if complete
        else "no_complete_pairs",
        "complete_pair_count": complete,
        "planned_pair_count": len(cells),
        "capture_status_counts": {
            s: sum(c["status"] == s for c in children.values())
            for s in ("queued", "running", "succeeded", "failed", "cancelled")
        },
        "cost": deepcopy(job["cost"]),
        **deepcopy(AUTHORITY),
    }


class NoCompletePairsError(ValueError):
    def __init__(self, result):
        super().__init__("No matched pair completed; original failures/cancellations retained")
        self.evidence_result = result


def execute(service, job):
    _verify_sources(service, job)
    if job["environment"] != _current(service):
        raise ConflictError("Aggregate worker differs from its frozen environment")
    if canonical(_request(service.registry.get_run(job["id"]))) != canonical(_request(job)):
        raise IntegrityError("Worker aggregate differs from its registered request")
    children = _children(service, job)
    if any(c["status"] not in {"succeeded", "failed", "cancelled"} for c in children.values()):
        raise ValueError("Aggregate cannot run before every owned child is terminal")
    summary = _summary(service, job, children)
    content = canonical(summary).encode()
    if len(content) > job["cost"]["summary_bytes_limit"]:
        raise IntegrityError("Study evaluation exceeds its planned summary bound")
    artifact = service.registry.store_blob(
        content, "electrical-response-summary.json", "application/json"
    )
    result = {
        "summary": summary,
        "artifacts": _artifacts([artifact, list(children.values())]),
        **deepcopy(AUTHORITY),
    }
    if summary["complete_pair_count"] == 0:
        raise NoCompletePairsError(result)
    return result


def get(service, identity):
    job = service.registry.get_run(identity)
    _verify_sources(service, job)
    children = _children(service, job)
    if job.get("result") is not None:
        expected = _summary(service, job, children)
        if canonical(expected) != canonical(job["result"]["summary"]):
            raise IntegrityError("Saved study evaluation differs from actual original RAW/timing")
        artifacts = job["artifacts"]
        _verify_artifacts(service, artifacts)
        own = [a for a in artifacts if a["name"] == "electrical-response-summary.json"]
        if (
            len(own) != 1
            or service.registry.blob_path(own[0]["sha256"]).read_bytes()
            != canonical(expected).encode()
            or canonical(_artifacts([own, list(children.values())])) != canonical(artifacts)
        ):
            raise IntegrityError("Study evaluation artifact differs from its saved summary")
    return job


def list_jobs(service):
    return [
        get(service, job["id"])
        for job in service.registry.list_runs()
        if job.get("job_kind") == JOB_KIND
    ]


def cancel(service, identity):
    # Ownership is sufficient to stop work even when a source blob is damaged.
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Not an electrical response study")
    _children(service, job, verify_blobs=False)
    return service.registry.cancel_electrical_response(identity)


def export_manifest(service, identity):
    job = get(service, identity)
    children = list(_children(service, job).values())
    artifacts = _artifacts([job, children])
    _verify_artifacts(service, artifacts)
    manifest = {
        "schema": "camera_electrical_response_study_export_v1",
        "study": job,
        "children": children,
        "artifacts": artifacts,
        "replay": {
            "specification": job["specification"],
            "plan_hash": job["plan_hash"],
            "instruction": "Replan original sources; each fixed/AE pair starts with zero PD state "
            "and its explicit shared seed.",
        },
        **deepcopy(AUTHORITY),
    }
    if len(canonical(manifest).encode()) > job["cost"]["manifest_bytes_bound"]:
        raise IntegrityError("Study manifest exceeds its prequeue byte bound")
    return manifest


def export_bundle(service, identity):
    manifest = export_manifest(service, identity)
    directory = service.registry.directory / "downloads"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="electrical-response-", suffix=".zip", dir=directory, delete=False
    ) as stream:
        path = Path(stream.name)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", canonical(manifest).encode())
            archive.writestr(
                "README.txt",
                "Matched authored electrical response evidence. No physical sensor, control "
                "stability, or LFM validation. Original RAW and diagnostics are preserved; "
                "only RAW/timing feed response evaluation.\n",
            )
            for artifact in manifest["artifacts"]:
                archive.write(
                    service.registry.blob_path(artifact["sha256"]),
                    f"artifacts/{artifact['sha256']}/{artifact['name']}",
                )
        if path.stat().st_size > manifest["study"]["cost"]["archive_bytes_bound"]:
            raise IntegrityError("Full study ZIP exceeds its prequeue byte bound")
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise
