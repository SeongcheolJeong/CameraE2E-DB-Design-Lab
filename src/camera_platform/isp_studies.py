"""Bounded immutable ISP studies over retained RAW; no acquisition or automatic winner.

A plan is read-only. Its hash binds semantic candidate inputs, source records and
execution environment; publication timestamps are separately bound by the stored
Study and each complete frozen cell request. Metrics consume stored pre-tone
arrays, never PNGs. Declaration of disjoint data is not physical independence.
"""

from __future__ import annotations

import copy
import hashlib
import io
import itertools
import json
import tempfile
import zipfile
from pathlib import Path

import numpy as np

from . import isp_study_metrics as metrics
from .contracts import ModelCreate
from .isp_study_contracts import (
    ISPReferenceImport,
    ISPSelectionCreate,
    ISPStudyPlan,
    ISPStudySubmit,
)
from .registry import ConflictError, IntegrityError, canonical, digest, now

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
SUMMARY_FORECAST_VERSION = "isp_study_inline_summary_v1"
# A hard contract for generated metadata, not a multiplier on source evidence.
# The current schema has at most 16 frames, 9 previews/frame, 8 stage descriptors,
# and fixed-size numeric diagnostics. Unbounded copied JSON is counted separately.
SUMMARY_GENERATED_BYTES_LIMIT = 64 * 1024
COMPLETION_METADATA_BYTES_PER_CELL = 512 * 1024
HALO = 4
TERMINAL = {"succeeded", "failed", "cancelled"}
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
}
AUTHORITY = {
    "physical_validation": "not_established",
    "release_authority": False,
    "selection_authority": False,
}


def catalog():
    return {
        **metrics.catalog(),
        "axes": ["raw_denoise.strength", "linear_sharpen.amount"],
        "max_jobs": 24,
        "frames": {"min": 4, "max": 16},
        "evaluation_halo_native_pixels": HALO,
        **AUTHORITY,
    }


def _artifacts(value):
    if isinstance(value, dict):
        if {"sha256", "name", "bytes"} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from _artifacts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _artifacts(child)


def _verify_artifacts(service, value):
    unique = {}
    for artifact in _artifacts(value):
        key = (artifact["sha256"], artifact["name"])
        if key in unique and unique[key]["bytes"] != artifact["bytes"]:
            raise IntegrityError("Conflicting artifact byte lengths")
        unique[key] = artifact
    for (checksum, _), artifact in unique.items():
        if service.registry.blob_path(checksum).stat().st_size != artifact["bytes"]:
            raise IntegrityError("ISP Study artifact size differs from its manifest")
    return [unique[key] for key in sorted(unique)]


def _npy(service, descriptor):
    _verify_artifacts(service, descriptor)
    return np.load(
        io.BytesIO(service.registry.blob_path(descriptor["sha256"]).read_bytes()),
        allow_pickle=False,
    )


def _array(service, result, name):
    matches = [a for a in result["artifacts"] if a["name"] == name + ".npy"]
    if len(matches) != 1:
        raise IntegrityError("Expected one stored metric input array: " + name)
    return _npy(service, matches[0]), matches[0]


def _store_array(service, array, name):
    stream = io.BytesIO()
    np.save(stream, array, allow_pickle=False)
    artifact = service.registry.store_blob(
        stream.getvalue(), name + ".npy", "application/octet-stream"
    )
    artifact.update(shape=list(array.shape), dtype=str(array.dtype))
    return artifact


def _semantic(record):
    return {k: v for k, v in record.items() if k not in {"id", "created_at", "content_hash"}}


def _json_bytes(value):
    """Canonical UTF-8 size without allocating a second full serialized document."""
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return sum(len(chunk.encode()) for chunk in encoder.iterencode(value))


def _summary_forecast(replay):
    """Bound the inline summary artifact, which graph deduplication cannot shrink.

    These are all input-dependent JSON fields copied by isp_replay_v2.execute
    and isp_v2.process_raw. Conditions occur twice; calibration occurs once per
    cell, regardless of sharing in the manifest graph. Frame preview identities
    have fixed SHA-256/URL lengths. Remaining generated fields are subject to an
    explicit 64 KiB contract, checked against actual output before cell success.
    This is not a physical-memory estimate or an estimate of compressed ZIP size.
    """
    from .isp_v2 import _common_profile

    source, profile = replay["source"], replay["profile"]
    copied = {
        "profile": profile,
        "baseline": {"profile": _common_profile(profile)},
        "raw_format": source["raw_format"],
        "geometry": source["geometry"],
        "source_kind": source["source_provenance"]["source_kind"],
        "acquisition_conditions": source["conditions"],
        "conditions": source["conditions"],
        "source_id": replay["source_id"],
        "source_provenance": source["source_provenance"],
        "replay_isp_model_id": replay["isp_model_snapshot"]["id"],
        "calibration_snapshot": replay["calibration_snapshot"],
    }
    if source.get("frame_records"):
        copied.update(
            frames=[
                {
                    **frame,
                    "replay_isp_model_id": replay["isp_model_snapshot"]["id"],
                    "preview_artifact_id": "0" * 64,
                    "preview_url": "/api/v1/artifacts/" + "0" * 64,
                }
                for frame in source["frame_records"]
            ],
            frame_conditions=source["frame_conditions"],
            clock=source["clock"],
        )
        copied.update(
            (key, source[key])
            for key in (
                "timing", "control_trace", "acquisition_mode", "control_program_id",
                "capture_program_id", "control_policy_id", "response_acquisition",
            )
            if key in source
        )
    copied_bytes = _json_bytes(copied)
    return {
        "version": SUMMARY_FORECAST_VERSION,
        "copied_summary_bytes": copied_bytes,
        "generated_summary_bytes_limit": SUMMARY_GENERATED_BYTES_LIMIT,
        "summary_artifact_bytes_limit": copied_bytes + SUMMARY_GENERATED_BYTES_LIMIT,
    }


def _verify_summary_forecast(job, result):
    # No reconstruction or new constraints on previously frozen v1/dev9 requests.
    forecast = job.get("evidence_forecast")
    if forecast is None:
        return
    if forecast.get("version") != SUMMARY_FORECAST_VERSION:
        raise IntegrityError("Unsupported frozen Study summary forecast version")
    summaries = [a for a in result["artifacts"] if a["name"] == "summary.json"]
    if len(summaries) != 1:
        raise IntegrityError("Expected one generated Study summary artifact")
    if summaries[0]["bytes"] > forecast["summary_artifact_bytes_limit"]:
        raise IntegrityError("Study summary artifact exceeds its declared preflight limit")


class _DraftRegistry:
    """Read-through immutable records; model publication is staged in memory only."""

    def __init__(self, registry):
        self.base = registry
        self.records = []
        self.directory = registry.directory

    def __getattr__(self, name):
        if name.startswith(("store", "submit", "publish", "cancel", "finish")):
            raise ValueError("Planning cannot write registry data")
        return getattr(self.base, name)

    def get(self, kind, identity):
        for k, record in self.records:
            if k == kind and record["id"] == identity:
                return copy.deepcopy(record)
        return self.base.get(kind, identity)

    def put(self, kind, document, **kwargs):
        if kind != "models" or kwargs:
            raise ValueError("Study planning only stages new immutable ISP models")
        identity = "model-isp-study-" + digest(document)
        try:
            previous = self.get(kind, identity)
        except KeyError:
            previous = None
        if previous is not None:
            if _semantic(previous) != document:
                raise IntegrityError("Candidate model identity collision")
            return previous
        record = {**document, "id": identity, "created_at": now()}
        record["content_hash"] = digest(record)
        self.records.append((kind, record))
        return copy.deepcopy(record)


def _implementation():
    names = (
        "isp.py",
        "isp_v2.py",
        "isp_calibration.py",
        "isp_replay_v2.py",
        "isp_study_metrics.py",
        "isp_studies.py",
        "isp_study_contracts.py",
        "isp_study_registry.py",
        "evidence_reads.py",
        "evidence_objects.py",
        "evidence_storage.py",
        "evidence_package.py",
    )
    return {
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in names
    }


def _mask(shape):
    result = np.zeros(shape, dtype=bool)
    result[HALO:-HALO, HALO:-HALO] = True
    if not result.any():
        raise ValueError("Study RAW must have an interior after the fixed four-pixel halo")
    return result


def _fixed_acquisition(service, source):
    record = source["source_snapshot"]
    if record.get("job_kind") in {"isp_replay", "isp_study_cell"}:
        raise ValueError("Study data must be original RAW acquisition, not candidate output")
    rows = source.get("frame_conditions", [])
    if rows:
        if len(rows) != source["raw_shape"][0]:
            raise ValueError("Every RAW frame needs its fixed actual acquisition conditions")
        conditions = [
            {
                k: v
                for k, v in row.items()
                if k not in {"frame_id", "frame_start_ns", "applied_at_ns"}
            }
            for row in rows
        ]
        if any(c != conditions[0] for c in conditions[1:]):
            raise ValueError(
                "Repeated Study RAW requires fixed exposure, gain and applied settings"
            )
    if source["source_kind"] == "run":
        if record.get("plugin_id") not in {
            "spatial_scene_v1",
            "temporal_scene_v1",
            "hdr_bracket_v1",
            "reference_hdr_v1",
            "closed_loop_scene_v1",
        }:
            raise ValueError("Study supports explicit native scene acquisition only")
        sequence = record.get("scene_sequence_snapshot")
        if sequence:
            images = [
                service.registry.get("spectral_images", k["spectral_image_id"])
                for k in sequence["payload"]["knots"]
            ]
            image_hashes = {
                next(a["sha256"] for a in image["artifacts"] if a["name"] == "spectral-image.npy")
                for image in images
            }
            if len(image_hashes) != 1:
                raise ValueError("Repeated noise metrics refuse a changing optical scene")
        program = record.get("capture_program_snapshot", {}).get("payload", {})
        waveform = program.get("waveform")
        if waveform and waveform.get("kind") != "constant":
            raise ValueError("Study fixed acquisition refuses periodic illumination")
    elif not rows and source["source_snapshot"].get("observation", {}).get("frames"):
        raise ValueError(
            "Unsupported capture per-frame settings require an explicit fixed contract"
        )


def _lineage(service, source):
    record = source["source_snapshot"]
    values = {source["source_id"]}
    for key in ("scene_id", "scene_sequence_id", "capture_id"):
        if record.get(key):
            values.add(record[key])
    values.update(record.get("dataset_ids", []))
    sequence = record.get("scene_sequence_snapshot")
    if sequence:
        values.add(sequence["id"])
        values.update(k["spectral_image_id"] for k in sequence["payload"]["knots"])
    return sorted(values)


def _provenance_closure(service, sources, baseline, calibration):
    """Original model/Factory and calibration fitting lineage, all read-only."""
    from .imaging import replay_source
    from .isp_calibration import get_calibration

    calibrations, calibration_sources = {}, {}
    pending_calibrations = [calibration]
    while pending_calibrations:
        value = pending_calibrations.pop()
        if value["id"] in calibrations:
            continue
        calibrations[value["id"]] = value
        ref = value["reference_source"]
        calibration_sources[(ref["kind"], ref["id"])] = replay_source(
            service, ref["kind"], ref["id"]
        )
        pending_calibrations.extend(get_calibration(service, i) for i in value["parents"])
    all_sources = [*sources, *calibration_sources.values()]
    pending = [baseline["id"]]
    for source in all_sources:
        assembly = service.registry.get("assemblies", source["source_snapshot"]["assembly_id"])
        pending.extend(assembly["model_ids"])
    models = {}
    while pending:
        identity = pending.pop()
        if identity in models:
            continue
        model = service.registry.get("models", identity)
        models[identity] = model
        pending.extend(model["parents"])
    factory_ids = {
        m[key]
        for m in models.values()
        for key in ("factory_job_id", "psf_factory_job_id")
        if m.get(key)
    }
    closure = {
        "model_lineage": [models[k] for k in sorted(models)],
        "factory_jobs": [service.registry.get_run(k) for k in sorted(factory_ids)],
        "calibration_lineage": [calibrations[k] for k in sorted(calibrations)],
        "calibration_sources": [calibration_sources[k] for k in sorted(calibration_sources)],
    }
    _verify_artifacts(service, closure)
    return closure


def _fit_tokens(service, calibration_records):
    """Expand declared fitting dataset/capture membership, not just user labels."""
    tokens = {i for record in calibration_records for i in record["dataset_ids"]}
    # A canonical calibration may retain evidence that is explicitly a fitting input.
    tokens.update(
        e["sha256"]
        for record in calibration_records
        for e in record["evidence_refs"]
        if "fit" in e["role"].lower()
    )
    # Dataset aliases are a graph. Expand each linked record once so transitive
    # fitting membership is independent of registry ordering.
    from collections import defaultdict, deque

    groups = []
    for measurement in service.registry.list("measurements"):
        group = {measurement["id"], *measurement.get("dataset_ids", [])}
        if measurement.get("raw_capture_id"):
            group.add(measurement["raw_capture_id"])
        groups.append(group)
    for capture in service.registry.list("captures"):
        group = {capture["id"], *capture.get("dataset_ids", [])}
        group.update("raw-frame-sha256:" + h for h in capture.get("raw_frame_hashes", []))
        group.update(a["sha256"] for a in capture.get("artifacts", []))
        groups.append(group)
    adjacency = defaultdict(list)
    for index, group in enumerate(groups):
        for token in group:
            adjacency[token].append(index)
    pending, visited = deque(tokens), set()
    while pending:
        for index in adjacency[pending.popleft()]:
            if index in visited:
                continue
            visited.add(index)
            added = groups[index] - tokens
            tokens.update(added)
            pending.extend(added)

    return tokens


def _dataset(service, spec, calibration_id):
    from .imaging import replay_source
    from .isp_calibration import resolve_for_source
    from .observations import load_raw_npy

    source = replay_source(service, spec["source_kind"], spec["source_id"])
    _verify_artifacts(service, source)
    if len(source["raw_shape"]) != 3 or not 4 <= source["raw_shape"][0] <= 16:
        raise ValueError("Repeated Study data requires 4 through 16 retained RAW frames")
    if source["geometry"].get("kind") != "native_sensor":
        raise ValueError("Study data requires fixed native geometry")
    _fixed_acquisition(service, source)
    calibration = resolve_for_source(service, calibration_id, source)
    metric_spec = metrics.validate_dataset(
        spec["metric_spec"], shape=tuple(source["raw_shape"][-2:])
    )
    if not metric_spec["stationary"]:
        raise ValueError("Study metrics require a declared stationary acquisition")
    spec = {**spec, "metric_spec": metric_spec}
    mask = _mask(tuple(source["raw_shape"][-2:]))
    roi = metric_spec["roi"]
    if not mask[roi["row_start"] : roi["row_end"], roi["column_start"] : roi["column_end"]].any():
        raise ValueError("The fixed evaluation ROI is empty after the declared halo")
    raw = load_raw_npy(service.registry.blob_path(source["raw_sha256"]).read_bytes())
    frames = []
    for frame in raw:
        frame_hash = hashlib.sha256(canonical(list(frame.shape)).encode())
        frame_hash.update(np.ascontiguousarray(frame, dtype="<u4").tobytes())
        frames.append(frame_hash.hexdigest())
    reference = get_reference(service, spec["reference_id"]) if spec["reference_id"] else None
    if reference:
        scale = 2 ** source["raw_format"]["bit_depth"] - 1 - source["raw_format"]["black_level_dn"]
        if (
            reference["geometry"] != source["geometry"]
            or reference["pixel_pitch_um"] != calibration["binding"]["pixel_pitch_um"]
            or reference["shape"] != [*source["raw_shape"][-2:], 3]
            or reference["normalization_dn"] != scale
        ):
            raise ValueError("Reference geometry, shape or DN normalization differs from fixed RAW")
    record = source["source_snapshot"]
    if not record.get("assembly_id") or not record.get("test_id"):
        raise ValueError("Study requires a fixed original acquisition assembly and test")
    tokens = set(_lineage(service, source)) | {spec["acquisition_group_id"], source["raw_sha256"]}
    tokens |= {"raw-frame-sha256:" + h for h in frames}
    if tokens & set(calibration["dataset_ids"]):
        raise ValueError("Study data overlaps declared calibration fitting data")
    return {
        "key": spec["key"],
        "specification": spec,
        "source": source,
        "source_sha256": digest(source),
        "raw_sha256": source["raw_sha256"],
        "raw_frame_sha256": frames,
        "assembly_id": record["assembly_id"],
        "test_id": record["test_id"],
        "acquisition_lineage_ids": _lineage(service, source),
        "reference_snapshot": reference,
        "evaluation_mask": {
            "shape": list(mask.shape),
            "roi": metric_spec["roi"],
            "halo": HALO,
            "sha256": hashlib.sha256(mask.tobytes()).hexdigest(),
        },
    }


def _overlap(left, right):
    return (
        left["raw_sha256"] == right["raw_sha256"]
        or bool(set(left["raw_frame_sha256"]) & set(right["raw_frame_sha256"]))
        or bool(set(left["acquisition_lineage_ids"]) & set(right["acquisition_lineage_ids"]))
        or left["specification"]["acquisition_group_id"]
        == right["specification"]["acquisition_group_id"]
    )


def _reducers(spec):
    definitions, objectives, constraints = {}, [], []
    catalog_values = metrics.catalog()["metrics"]
    for key, destination in (("objectives", objectives), ("constraints", constraints)):
        for target in spec[key]:
            metric = target["metric"]
            if metric not in catalog_values:
                raise ValueError("Unknown Study metric: " + metric)
            if key == "constraints" and target["unit"] != catalog_values[metric]["unit"]:
                raise ValueError("Study constraint unit differs from metric definition")
            reduction = {
                "metric": metric,
                "dataset_ids": target["dataset_keys"],
                "reducer": target["reducer"],
            }
            identity = "reduction-" + digest(reduction)[:16]
            definitions[identity] = {"id": identity, **reduction}
            destination.append(
                {
                    "metric": identity,
                    **{
                        k: target[k]
                        for k in (
                            ["direction"] if key == "objectives" else ["operator", "value", "unit"]
                        )
                    },
                }
            )
    if len({o["metric"] for o in objectives}) != len(objectives):
        raise ValueError("Study objectives cannot duplicate the same reduction")
    return list(definitions.values()), objectives, constraints


def template(service, source_kind, source_id, calibration_id, baseline_isp_model_id):
    from .imaging import replay_source, resolve_isp

    base = service.registry.get("models", baseline_isp_model_id)
    profile = resolve_isp(base, allow_v2=True)
    if (
        profile.get("schema") != "camera_isp_profile_v2"
        or profile["calibration_id"] != calibration_id
    ):
        raise ValueError("Study fixes one v2 baseline profile and its exact calibration")
    axes = [
        {"parameter": parameter, "values": values}
        for parameter, values in (
            ("raw_denoise.strength", [0.0, 0.4, 0.8]),
            ("linear_sharpen.amount", [0.0, 0.4]),
        )
        if profile[parameter.split(".")[0]]["mode"] != "bypass"
    ]
    if not axes:
        raise ValueError(
            "Both raw denoise and linear sharpen are bypassed; configure an enabled "
            "stage in the baseline ISP before creating a tuning template"
        )
    source = replay_source(service, source_kind, source_id)
    h, w = source["raw_shape"][-2:]
    return ISPStudyPlan.model_validate(
        {
            "name": "동일 RAW · 잡음과 선명도 비교",
            "question": "고정 RAW에서 허용 가능한 잡음·질감·halo trade-off는?",
            "calibration_id": calibration_id,
            "baseline_isp_model_id": baseline_isp_model_id,
            "axes": axes,
            "datasets": [
                {
                    "key": "flat",
                    "source_kind": source_kind,
                    "source_id": source_id,
                    "purpose": "tuning",
                    "acquisition_group_id": "acquisition:" + source_id,
                    "stationarity": {
                        "source_kind": "estimated",
                        "description": "사용자가 정지 반복 취득 여부를 확인해야 합니다.",
                    },
                    "metric_spec": {
                        "kind": "flat",
                        "stationary": True,
                        "roi": {
                            "row_start": HALO,
                            "row_end": h - HALO,
                            "column_start": HALO,
                            "column_end": w - HALO,
                        },
                    },
                }
            ],
            "objectives": [
                {
                    "metric": "temporal_residual_std_dn",
                    "dataset_keys": ["flat"],
                    "reducer": "max",
                    "direction": "minimize",
                }
            ],
            "constraints": [],
        }
    ).model_dump()


def _prepare(service, payload):
    from .evidence_reads import operation_service

    service = operation_service(service)
    from .imaging import _current, resolve_isp
    from .isp_calibration import get_calibration
    from .isp_replay_v2 import prepare as prepare_replay
    from .isp_v2 import validate_inputs
    from .observations import load_raw_npy

    spec = ISPStudyPlan.model_validate(payload).model_dump()
    base = service.registry.get("models", spec["baseline_isp_model_id"])
    profile = resolve_isp(base, allow_v2=True)
    if (
        profile.get("schema") != "camera_isp_profile_v2"
        or profile["calibration_id"] != spec["calibration_id"]
    ):
        raise ValueError("Study fixes one v2 baseline profile and its exact calibration")
    calibration = get_calibration(service, spec["calibration_id"])
    reducers, objectives, constraints = _reducers(spec)
    datasets = [_dataset(service, d, spec["calibration_id"]) for d in spec["datasets"]]
    common = {k: profile[k] for k in ("black_level", "demosaic", "white_balance", "ccm")}
    for dataset in datasets:
        reference = dataset["reference_snapshot"]
        if reference and reference["common_linear_profile"] != common:
            raise ValueError("Reference fixed BLC/demosaic/WB/CCM differs from the Study baseline")
    spec["datasets"] = [d["specification"] for d in datasets]
    closure = _provenance_closure(service, [d["source"] for d in datasets], base, calibration)
    fitting = _fit_tokens(service, closure["calibration_lineage"])
    for dataset in datasets:
        tokens = set(dataset["acquisition_lineage_ids"]) | {
            dataset["raw_sha256"],
            dataset["specification"]["acquisition_group_id"],
        }
        tokens.update("raw-frame-sha256:" + h for h in dataset["raw_frame_sha256"])
        if tokens & fitting:
            raise ValueError("Study RAW overlaps calibration fitting ancestry or original capture")
    for index, dataset in enumerate(datasets):
        if any(_overlap(dataset, prior) for prior in datasets[:index]):
            raise ValueError("Study datasets cannot relabel overlapping RAW acquisition")
    selection = None
    profiles = [(profile, {})]
    if spec["phase"] == "tuning":
        for axis in spec["axes"]:
            stage = axis["parameter"].split(".")[0]
            if profile[stage]["mode"] == "bypass":
                raise ValueError("A tuning axis cannot be hidden by a bypassed stage")
        for values in itertools.product(*(a["values"] for a in spec["axes"])):
            candidate = copy.deepcopy(profile)
            coordinates = {}
            for axis, value in zip(spec["axes"], values, strict=True):
                stage, parameter = axis["parameter"].split(".")
                candidate[stage][parameter] = value
                coordinates[axis["parameter"]] = value
            profiles.append((candidate, coordinates))
    else:
        selection = get_selection(service, spec["selection_id"])
        if (
            selection["calibration_id"] != spec["calibration_id"]
            or selection["baseline_isp_model_id"] != base["id"]
            or spec["parent_study_id"] != selection["study_id"]
        ):
            raise ValueError(
                "Independent validation must retain selected Study calibration and baseline"
            )
        parent = get_study(service, selection["study_id"])
        if any(_overlap(d, prior) for d in datasets for prior in parent["datasets"]):
            raise ValueError("Independent validation overlaps tuning acquisition or RAW frames")
        profiles.append(
            (selection["profile"], {"selected_candidate_id": selection["candidate_id"]})
        )
    if spec["parent_study_id"] and spec["phase"] == "tuning":
        parent = get_study(service, spec["parent_study_id"])
        if spec["attempt"] <= parent["specification"]["attempt"]:
            raise ValueError("A retry must increment the preserved parent attempt")
    unique = {}
    for p, coordinates in profiles:
        unique.setdefault(digest(p), (p, coordinates))
    if len(unique) * len(datasets) > 24:
        raise ValueError("Study candidate × dataset count exceeds 24 cells")
    environment = _current(service)
    basis = {
        "evidence_format": "camera_evidence_bundle_v2",
        "evidence_forecast_version": SUMMARY_FORECAST_VERSION,
        "specification": spec,
        "environment": environment,
        "calibration_sha256": digest(calibration),
        "provenance_closure": closure,
        "baseline_sha256": digest(base),
        "datasets": datasets,
        "profiles": list(unique),
        "implementation": _implementation(),
        "selection_snapshot": selection,
        "reducers": reducers,
        "objectives": objectives,
        "constraints": constraints,
    }
    plan_hash = digest(basis)
    identity = "isp_study-" + plan_hash
    if spec["phase"] == "independent_validation":
        for previous in service.registry.list("isp_studies"):
            if previous["id"] == identity:
                continue
            if any(_overlap(d, prior) for d in datasets for prior in previous["datasets"]):
                raise ValueError("Validation acquisition was already used by a preserved Study")
    draft = copy.copy(service)
    draft.registry = _DraftRegistry(service.registry)
    candidates, requests, cells = [], [], []
    output_bytes, summary_bytes, working_max = 0, 0, 0
    for profile_hash, (p, coordinates) in unique.items():
        is_baseline = p == profile
        model = base
        if not is_baseline:
            document = {
                k: copy.deepcopy(v) for k, v in base.items() if k in ModelCreate.model_fields
            }
            document.update(
                name=spec["name"] + " · " + profile_hash[:8],
                revision="isp-study-" + plan_hash[:16],
                parents=[base["id"]],
                source_kind="estimated",
                physical_instance=None,
                source_uri="isp-study://" + plan_hash + "/" + profile_hash,
                method_and_version="camera_isp_study_v1:authored_tuning",
                payload=p,
            )
            model = draft.create_model(document)
        candidate = {
            "candidate_id": "candidate-" + profile_hash,
            "is_baseline": is_baseline,
            "profile": p,
            "profile_sha256": profile_hash,
            "coordinates": coordinates,
            "isp_model_id": model["id"],
            "model_snapshot": model,
        }
        candidates.append(candidate)
        for dataset in datasets:
            source = dataset["source"]
            replay = prepare_replay(
                draft,
                {
                    "name": spec["name"] + " · " + dataset["key"],
                    "source_kind": source["source_kind"],
                    "source_id": source["source_id"],
                    "isp_model_id": model["id"],
                },
            )
            raw = load_raw_npy(draft.registry.blob_path(source["raw_sha256"]).read_bytes())
            parsed = validate_inputs(
                raw,
                source["raw_format"],
                p,
                geometry=source["geometry"],
                calibration=calibration["payload"],
            )
            working_max = max(working_max, parsed["resources"]["working_array_bytes_estimate"])
            # Conservative uncompressed stage arrays, metric baseline, previews, headers.
            output_bytes += raw.size * 720 + raw.shape[-2] * raw.shape[-1] * 128 + 64 * 1024
            forecast = _summary_forecast(replay)
            summary_bytes += forecast["summary_artifact_bytes_limit"]
            if output_bytes + summary_bytes > MAX_ARCHIVE_BYTES:
                raise ValueError(
                    "Study estimated uncompressed evidence exceeds 256 MiB archive budget"
                )
            wrapper = {
                "name": replay["name"],
                "job_kind": "isp_study_cell",
                "plugin_id": "isp_study_v1",
                "study_id": identity,
                "candidate_id": candidate["candidate_id"],
                "dataset_key": dataset["key"],
                "dataset_snapshot": dataset,
                "replay_request": replay,
                "environment": environment,
                "metric_version": metrics.VERSION,
                "implementation": basis["implementation"],
                "evidence_forecast": forecast,
            }
            requests.append(wrapper)
            cells.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "dataset_key": dataset["key"],
                    "request_sha256": digest(wrapper),
                }
            )
    existing_bytes = sum(
        a["bytes"] for a in _verify_artifacts(service, [datasets, calibration, base, closure])
    )
    from .evidence_package import graph, graph_size

    # The same frozen source/model/calibration appears in every request. Count
    # their exact content-addressed object union, not repeated logical JSON.
    # Reserve output arrays separately and room for completion/metric metadata.
    packed = graph({"basis": basis, "requests": requests, "candidates": candidates,
                    "calibration": calibration, "baseline": base})
    graph_bytes = graph_size(packed)
    completion_bytes = len(cells) * COMPLETION_METADATA_BYTES_PER_CELL
    estimated = output_bytes + summary_bytes + existing_bytes + graph_bytes + completion_bytes
    if estimated > MAX_ARCHIVE_BYTES:
        raise ValueError("Study estimated uncompressed evidence exceeds 256 MiB archive budget")
    result = {
        "schema": "camera_isp_study_plan_v1",
        "evidence_format": "camera_evidence_bundle_v2",
        "specification": spec,
        "plan_hash": plan_hash,
        "plan_hash_basis": basis,
        "environment": environment,
        "calibration_snapshot": calibration,
        "baseline_isp_snapshot": base,
        "selection_snapshot": selection,
        "candidates": candidates,
        "datasets": datasets,
        "cells": cells,
        "job_count": len(cells),
        "resources": {
            "estimated_output_bytes": output_bytes,
            "estimated_summary_artifact_bytes": summary_bytes,
            "completion_metadata_reserved_bytes": completion_bytes,
            "estimated_bundle_bytes": estimated,
            "unique_evidence_object_bytes": graph_bytes,
            "logical_evidence_bytes": packed["root"]["expanded_bytes"],
            "estimated_working_bytes_max": working_max,
            "queued_jobs": len(cells),
        },
        "independence": {
            "status": "declared_disjoint_data_not_physical_independence",
            "calibration_fitting_dataset_ids": calibration["dataset_ids"],
            "reference_authorship": "declared_not_independently_authenticated",
        },
        **AUTHORITY,
    }
    return result, draft.registry.records, requests


def plan(service, payload):
    if payload.get("schema") == "camera_isp_study_v2":
        from .isp_studies_v2 import plan as charge_plan
        return charge_plan(service, payload)
    return _prepare(service, payload)[0]


def submit(service, payload):
    if payload.get("specification", {}).get("schema") == "camera_isp_study_v2":
        from .isp_studies_v2 import submit as charge_submit
        return charge_submit(service, payload)
    from .isp_study_registry import publish

    data = ISPStudySubmit.model_validate(payload).model_dump()
    prepared, records, requests = _prepare(service, data["specification"])
    if prepared["plan_hash"] != data["plan_hash"]:
        raise ConflictError("ISP Study plan changed; review a fresh plan before submission")
    result = publish(service.registry, prepared, records, requests)
    return get_study(service, result["id"])


def _inputs(service, study):
    if study["specification"].get("schema") == "camera_isp_study_v2":
        from .isp_studies_v2 import _inputs as charge_inputs
        return charge_inputs(service, study)
    from .imaging import replay_source
    from .isp_calibration import get_calibration, resolve_for_source

    if digest(study["plan_hash_basis"]) != study["plan_hash"]:
        raise IntegrityError("ISP Study semantic plan hash differs")
    if (
        get_calibration(service, study["specification"]["calibration_id"])
        != study["calibration_snapshot"]
    ):
        raise IntegrityError("ISP Study calibration differs from frozen evidence")
    for candidate in study["candidates"]:
        if (
            service.registry.get("models", candidate["isp_model_id"]) != candidate["model_snapshot"]
            or digest(candidate["profile"]) != candidate["profile_sha256"]
            or candidate["model_snapshot"]["payload"] != candidate["profile"]
        ):
            raise IntegrityError("ISP candidate differs from its immutable profile")
    for dataset in study["datasets"]:
        source = dataset["source"]
        fresh = replay_source(service, source["source_kind"], source["source_id"])
        if fresh != source or digest(source) != dataset["source_sha256"]:
            raise IntegrityError("ISP Study RAW acquisition differs from its frozen snapshot")
        resolve_for_source(service, study["specification"]["calibration_id"], fresh)
        reference = dataset["reference_snapshot"]
        if reference and get_reference(service, reference["id"]) != reference:
            raise IntegrityError("ISP Study independent reference differs")
    fresh_closure = _provenance_closure(
        service,
        [d["source"] for d in study["datasets"]],
        study["baseline_isp_snapshot"],
        study["calibration_snapshot"],
    )
    if fresh_closure != study["plan_hash_basis"]["provenance_closure"]:
        raise IntegrityError("Original model/Factory/calibration ancestry changed")
    _verify_artifacts(service, study)


def _cell(service, study, cell, *, check_artifacts=True):
    job = service.registry.get_run(cell["job_id"])
    request = {k: v for k, v in job.items() if k not in JOB_KEYS}
    if (
        digest(request) != cell["request_sha256"]
        or request.get("study_id") != study["id"]
        or request.get("candidate_id") != cell["candidate_id"]
        or request.get("dataset_key") != cell["dataset_key"]
    ):
        raise IntegrityError("ISP Study cell ownership or exact frozen request differs")
    if check_artifacts:
        _verify_artifacts(service, job)
    if check_artifacts and job["status"] == "succeeded":
        result = job["result"]
        metric = result["metric_result"]
        artifact = next(a for a in result["artifacts"] if a["name"] == "isp-study-metrics.json")
        stored = json.loads(service.registry.blob_path(artifact["sha256"]).read_text())
        if stored != metric or result["raw_sha256"] != request["dataset_snapshot"]["raw_sha256"]:
            raise IntegrityError("Stored Study metric or RAW identity differs from completion")
        if request.get("plugin_id") == "isp_study_v2":
            from .isp_studies_v2 import _verify_cell_result
            _verify_cell_result(job)
    return job


class _EvidenceReader:
    """One operation's verified evidence, never a cache on the shared service.

    Composite reads reuse the exact checked job snapshots for analysis, selection
    and export. A new public operation always checks sources, completions and
    blob bytes again. Callers must neither retain this reader nor mutate its
    records, and mutations must not be performed inside its lifetime.
    """

    def __init__(self, service):
        from .evidence_reads import operation_service

        self.service = operation_service(service)
        self._studies = {}
        self._analyses = {}
        self._selections = {}
        self._exports = {}

    def study(self, identity):
        if identity not in self._studies:
            study = self.service.registry.get("isp_studies", identity)
            _inputs(self.service, study)
            cells = [_cell(self.service, study, cell) for cell in study["cells"]]
            self._studies[identity] = (study, cells)
        return self._studies[identity]

    def analysis(self, identity):
        if identity not in self._analyses:
            self._analyses[identity] = _analyze(*self.study(identity))
        return self._analyses[identity]

    def selection(self, identity):
        if identity not in self._selections:
            record = self.service.registry.get("isp_selections", identity)
            analysis = self.analysis(record["study_id"])
            if (
                analysis != record["analysis_snapshot"]
                or analysis["analysis_sha256"] != record["analysis_sha256"]
            ):
                raise IntegrityError("Selection differs from its exact completed tuning analysis")
            study, _ = self.study(record["study_id"])
            candidate = next(
                c for c in study["candidates"] if c["candidate_id"] == record["candidate_id"]
            )
            if (
                candidate["profile"] != record["profile"]
                or digest(record["profile"]) != record["profile_sha256"]
            ):
                raise IntegrityError(
                    "Selection profile differs from the chosen immutable candidate"
                )
            self._selections[identity] = record
        return self._selections[identity]

    def export(self, identity):
        if identity not in self._exports:
            study, cells = self.study(identity)
            manifest = {
                "schema": (
                    "camera_isp_study_evidence_v2"
                    if study["specification"].get("schema") == "camera_isp_study_v2"
                    else "camera_isp_study_evidence_v1"
                ),
                "study": study,
                "analysis": self.analysis(identity),
                "cells": cells,
                **study["plan_hash_basis"]["provenance_closure"],
                **AUTHORITY,
            }
            manifest["artifacts"] = _verify_artifacts(self.service, manifest)
            self._exports[identity] = manifest
        return self._exports[identity]


def get_study(service, identity):
    return _EvidenceReader(service).study(identity)[0]


def get_cell(service, identity):
    job = service.registry.get_run(identity)
    if job.get("job_kind") != "isp_study_cell":
        raise KeyError("Not an ISP Study cell")
    study = service.registry.get("isp_studies", job["study_id"])
    _inputs(service, study)
    matches = [c for c in study["cells"] if c["job_id"] == identity]
    if len(matches) != 1:
        raise IntegrityError("Cell is not owned by its claimed Study")
    return _cell(service, study, matches[0])


def execute(service, job):
    if job.get("plugin_id") == "isp_study_v2":
        from .isp_studies_v2 import execute as charge_execute
        return charge_execute(service, job)
    from .imaging import _current
    from .isp import default_profile, process_raw
    from .isp_replay_v2 import execute as execute_replay
    from .observations import load_raw_npy

    exact = get_cell(service, job["id"])
    if job != exact:
        raise IntegrityError("Worker cell differs from the registered exact request")
    if job["environment"] != _current(service) or job["implementation"] != _implementation():
        raise ValueError("Worker differs from the frozen ISP Study implementation/environment")
    result = execute_replay(service, job["replay_request"])
    _verify_summary_forecast(job, result)
    dataset = job["dataset_snapshot"]
    source = dataset["source"]
    rgb, rgb_artifact = _array(service, result, "sharpened_rgb_frames")
    valid, valid_artifact = _array(service, result, "rgb_valid_mask_frames")
    raw = load_raw_npy(service.registry.blob_path(source["raw_sha256"]).read_bytes())
    baseline_profile = default_profile()
    for key in ("white_balance", "ccm", "tone"):
        baseline_profile[key] = copy.deepcopy(job["replay_request"]["profile"][key])
    baseline = process_raw(
        raw, source["raw_format"], baseline_profile, geometry=source["geometry"]
    )["arrays"]["ccm_rgb_frames"]
    reference = dataset["reference_snapshot"]
    reference_rgb = _npy(service, reference["array_artifact"]) if reference else None
    mask = _mask(raw.shape[-2:])
    if hashlib.sha256(mask.tobytes()).hexdigest() != dataset["evaluation_mask"]["sha256"]:
        raise IntegrityError("Study evaluation mask differs from its predeclared mask")
    scale = 2 ** source["raw_format"]["bit_depth"] - 1 - source["raw_format"]["black_level_dn"]
    measured = metrics.measure(
        rgb,
        normalization_dn=scale,
        dataset=dataset["specification"]["metric_spec"],
        evaluation_mask=mask,
        valid_mask_frames=valid,
        baseline_rgb_frames=baseline,
        reference_rgb=reference_rgb,
    )
    baseline_artifact = _store_array(service, baseline, "isp-study-baseline-linear-rgb")
    mask_artifact = _store_array(service, mask, "isp-study-evaluation-mask")
    measured["evidence"] = {
        "raw_sha256": source["raw_sha256"],
        "rgb_artifact": rgb_artifact,
        "valid_mask_artifact": valid_artifact,
        "evaluation_mask_artifact": mask_artifact,
        "baseline_artifact": baseline_artifact,
        "reference_artifact": reference["array_artifact"] if reference else None,
        "normalization_dn": scale,
        "implementation": job["implementation"],
        "environment": job["environment"],
    }
    metric_artifact = service.registry.store_blob(
        canonical(measured).encode(), "isp-study-metrics.json", "application/json"
    )
    result["artifacts"].extend([baseline_artifact, mask_artifact, metric_artifact])
    if reference:
        result["artifacts"].extend(reference["artifacts"])
    result.update(
        metric_result=measured,
        study_id=job["study_id"],
        candidate_id=job["candidate_id"],
        dataset_key=job["dataset_key"],
        **AUTHORITY,
    )
    return result


def analyze(service, identity):
    return _EvidenceReader(service).analysis(identity)


def _analyze(study, cells):
    if study["specification"].get("schema") == "camera_isp_study_v2":
        from .isp_studies_v2 import _analyze as charge_analyze
        return charge_analyze(study, cells)
    """Derive metrics from the same verified jobs used by the containing read."""
    rows = []
    for cell, job in zip(study["cells"], cells, strict=True):
        result = job["result"] or {}
        rows.append(
            {
                **cell,
                "status": job["status"],
                "error": job["error"],
                "job_sha256": digest(job),
                "artifacts": job["artifacts"],
                "metrics": result.get("metric_result", {}).get("metrics"),
                "metric_result": result.get("metric_result"),
                "summary": {
                    k: result.get("summary", {}).get(k)
                    for k in ("stage_previews", "raw_sha256", "stage_outputs")
                },
            }
        )
    reducers, objectives, constraints = _reducers(study["specification"])
    aggregate_rows = []
    for candidate in study["candidates"]:
        own = [r for r in rows if r["candidate_id"] == candidate["candidate_id"]]
        status = next(
            (
                s
                for s in ("running", "queued", "failed", "cancelled")
                if any(r["status"] == s for r in own)
            ),
            "succeeded",
        )
        reduced = metrics.reduce_datasets(
            {r["dataset_key"]: r["metric_result"] for r in own if r["metric_result"] is not None},
            reducers,
        )
        aggregate_rows.append(
            {
                "candidate_id": candidate["candidate_id"],
                "status": status,
                "metrics": reduced["metrics"],
            }
        )
    aggregate = metrics.aggregate(
        aggregate_rows,
        objectives,
        constraints,
        expected_candidate_ids=[c["candidate_id"] for c in study["candidates"]],
    )
    if study["specification"]["phase"] == "independent_validation":
        for row in aggregate["rows"]:
            row["pareto_status"] = "not_ranked_validation"
        aggregate["pareto_scope"] = "fixed_baseline_and_explicit_selection_no_validation_ranking"
        aggregate["pareto_provisional"] = True
    analysis = {
        "schema": "camera_isp_study_analysis_v1",
        "study_id": study["id"],
        "plan_hash": study["plan_hash"],
        "phase": study["specification"]["phase"],
        "status": "complete" if all(r["status"] in TERMINAL for r in rows) else "incomplete",
        "rows": rows,
        "coverage": {
            s: sum(r["status"] == s for r in rows)
            for s in ("queued", "running", "succeeded", "failed", "cancelled")
        },
        "aggregate": aggregate,
        "reduction_definitions": reducers,
        **AUTHORITY,
    }
    analysis["analysis_sha256"] = digest(analysis)
    return analysis


def cancel(service, identity):
    # Cancellation depends on exact ownership, not on continued availability of
    # input/output files. A damaged input must not strand the owner's queue.
    study = service.registry.get("isp_studies", identity)
    for cell in study["cells"]:
        job = _cell(service, study, cell, check_artifacts=False)
        if job["status"] not in TERMINAL:
            service.registry.cancel(job["id"])
    try:
        return analyze(service, identity)
    except (KeyError, ValueError) as exc:
        jobs = [_cell(service, study, c, check_artifacts=False) for c in study["cells"]]
        return {
            "schema": "camera_isp_study_cancellation_v1",
            "study_id": identity,
            "status": "cancellation_recorded_evidence_unavailable",
            "rows": [
                {
                    "job_id": j["id"],
                    "candidate_id": j["candidate_id"],
                    "dataset_key": j["dataset_key"],
                    "status": j["status"],
                }
                for j in jobs
            ],
            "evidence_error": str(exc),
            **AUTHORITY,
        }


def select_candidate(service, identity, payload):
    stored = service.registry.get("isp_studies", identity)
    if stored["specification"].get("schema") == "camera_isp_study_v2":
        from .isp_studies_v2 import select_candidate as charge_select
        return charge_select(service, identity, payload)
    data = ISPSelectionCreate.model_validate(payload).model_dump()
    reader = _EvidenceReader(service)
    study, _ = reader.study(identity)
    analysis = reader.analysis(identity)
    if study["specification"]["phase"] != "tuning" or analysis["status"] != "complete":
        raise ValueError("Explicit selection requires a terminal tuning Study")
    if data["analysis_sha256"] != analysis["analysis_sha256"]:
        raise ConflictError("Selection analysis changed; review the current exact result")
    candidate = next(
        (c for c in study["candidates"] if c["candidate_id"] == data["candidate_id"]), None
    )
    if candidate is None or any(
        r["status"] != "succeeded"
        for r in analysis["rows"]
        if r["candidate_id"] == data["candidate_id"]
    ):
        raise ValueError("Selection needs a registered candidate with every dataset completed")
    document = {
        "schema": "camera_isp_selection_v1",
        "study_id": identity,
        "plan_hash": study["plan_hash"],
        **data,
        "profile": candidate["profile"],
        "profile_sha256": candidate["profile_sha256"],
        "analysis_snapshot": analysis,
        "calibration_id": study["specification"]["calibration_id"],
        "baseline_isp_model_id": study["specification"]["baseline_isp_model_id"],
        "decision_kind": "explicit_engineering_choice_not_approval",
        **AUTHORITY,
    }
    selection_id = "isp_selection-" + digest(document)
    try:
        return get_selection(service, selection_id)
    except KeyError:
        try:
            return service.registry.put("isp_selections", document, identity=selection_id)
        except ConflictError:
            existing = get_selection(service, selection_id)
            if _semantic(existing) != document:
                raise IntegrityError("Selection idempotency identity collision") from None
            return existing


def get_selection(service, identity):
    return _EvidenceReader(service).selection(identity)


def _reference_geometry(value, shape):
    if (
        not isinstance(value, dict)
        or value.get("kind") != "native_sensor"
        or set(value) != {"kind", "full_sensor_shape", "crop_origin"}
    ):
        raise ValueError("Reference requires explicit native sensor geometry")
    for key in ("full_sensor_shape", "crop_origin"):
        if (
            not isinstance(value.get(key), list)
            or len(value[key]) != 2
            or any(
                type(n) is not int or n < (2 if key == "full_sensor_shape" else 0)
                for n in value[key]
            )
        ):
            raise ValueError("Reference native geometry requires exact integer pairs")
    if any(
        o + n > f
        for o, n, f in zip(value["crop_origin"], shape[:2], value["full_sensor_shape"], strict=True)
    ):
        raise ValueError("Reference crop extends beyond native sensor")


def import_reference(service, metadata, npy_bytes):
    if metadata.get("schema") == "camera_isp_reference_v2":
        from .isp_studies_v2 import import_reference as charge_import
        return charge_import(service, metadata, npy_bytes)
    data = ISPReferenceImport.model_validate(metadata).model_dump()
    if not isinstance(npy_bytes, bytes) or len(npy_bytes) > 8 * 1024 * 1024:
        raise ValueError("Reference NPY exceeds the bounded upload contract")
    stream = io.BytesIO(npy_bytes)
    try:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2, 0):
            shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError("Reference requires NPY v1/v2")
        if (
            len(shape) != 3
            or shape[-1] != 3
            or any(not 1 <= n <= 512 for n in shape[:2])
            or dtype.kind != "f"
        ):
            raise ValueError("Reference requires bounded floating H×W×3 pre-tone RGB")
        if np.prod(shape) * dtype.itemsize + stream.tell() != len(npy_bytes):
            raise ValueError("Reference NPY has missing or trailing payload")
        values = np.load(io.BytesIO(npy_bytes), allow_pickle=False)
    except (TypeError, EOFError, OverflowError) as exc:
        raise ValueError("Invalid independent reference NPY") from exc
    if not np.isfinite(values).all():
        raise ValueError("Reference RGB must be finite signed pre-tone values")
    _reference_geometry(data["geometry"], shape)
    parents = [get_reference(service, p) for p in data["parents"]]
    for parent in parents:
        if parent["geometry"] != data["geometry"]:
            raise ValueError("Reference revision must retain its geometry")
    artifact = service.registry.store_blob(
        npy_bytes, "isp-reference-linear-rgb.npy", "application/octet-stream"
    )
    artifact.update(shape=list(shape), dtype=str(values.dtype), unit=data["unit"])
    meta = service.registry.store_blob(
        canonical(data).encode(), "isp-reference-metadata.json", "application/json"
    )
    return service.registry.put(
        "isp_references",
        {
            **data,
            "shape": list(shape),
            "array_artifact": artifact,
            "artifacts": [artifact, meta],
            "parent_snapshots": parents,
            "approval_status": "draft",
            "independence": "declared_not_independently_authenticated",
            **AUTHORITY,
        },
    )


def get_reference(service, identity):
    if service.registry.get("isp_references", identity).get("schema") == "camera_isp_reference_v2":
        from .isp_studies_v2 import get_reference as charge_reference
        return charge_reference(service, identity)
    record = service.registry.get("isp_references", identity)
    normalized = ISPReferenceImport.model_validate(
        {k: record[k] for k in ISPReferenceImport.model_fields}
    ).model_dump()
    artifact = next(a for a in record["artifacts"] if a["name"] == "isp-reference-metadata.json")
    _verify_artifacts(service, record)
    if (
        service.registry.blob_path(artifact["sha256"]).read_bytes()
        != canonical(normalized).encode()
    ):
        raise IntegrityError("Reference metadata differs from its canonical original blob")
    for parent in record["parent_snapshots"]:
        if get_reference(service, parent["id"]) != parent:
            raise IntegrityError("Reference parent differs from frozen lineage")
    return record


def export_manifest(service, identity):
    return _EvidenceReader(service).export(identity)


def export_bundle(service, identity):
    manifest = export_manifest(service, identity)
    entries = manifest["artifacts"]
    if manifest["study"].get("evidence_format") == "camera_evidence_bundle_v2":
        from .evidence_package import build_graph_bundle

        return build_graph_bundle(service, manifest, entries)
    if sum(a["bytes"] for a in entries) + len(canonical(manifest).encode()) > MAX_ARCHIVE_BYTES:
        raise ValueError("ISP Study evidence exceeds the 256 MiB archive limit")
    directory = service.registry.directory / "downloads"
    directory.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".zip", delete=False) as temporary:
        target = Path(temporary.name)
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", canonical(manifest))
            archive.writestr(
                "README.txt",
                "Retained RAW ISP Study. Verify all artifact SHA-256 values. "
                "Explicit engineering choice; no physical validation or release authority.\n",
            )
            for artifact in entries:
                archive.write(
                    service.registry.blob_path(artifact["sha256"]),
                    "artifacts/" + artifact["sha256"] + "/" + artifact["name"],
                )
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise
