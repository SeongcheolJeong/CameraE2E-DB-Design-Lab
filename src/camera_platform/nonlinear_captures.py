"""Immutable uploaded nonlinear RAW acquisitions; calibration is a separate interpretation."""

from __future__ import annotations

import hashlib
import io
import json
import math
import uuid
from copy import deepcopy

import numpy as np

from .nonlinear_capture_contracts import (
    MAX_CAPTURE_BYTES,
    MAX_METADATA_BYTES,
    MAX_SELECTED_PIXELS,
    SCHEMA,
    NonlinearCaptureImport,
    validate_timing,
)
from .registry import IntegrityError, canonical, digest, now

PIXEL_ENCODING = "canonical_shape_json_then_C_little_endian_uint32_v1"
MAX_PARENT_NODES = 128
MAX_PARENT_DEPTH = 16


def _metadata(content):
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_METADATA_BYTES:
        raise ValueError("Acquisition metadata requires at most 1 MiB of original JSON bytes")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Original acquisition JSON contains duplicate keys")
            result[key] = value
        return result

    try:
        return NonlinearCaptureImport.model_validate(
            json.loads(content, object_pairs_hook=unique)
        ).model_dump()
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid bounded original acquisition JSON") from exc


def pixel_hash(raw):
    result = hashlib.sha256(canonical(list(raw.shape)).encode())
    result.update(np.ascontiguousarray(raw, dtype="<u4").tobytes())
    return result.hexdigest()


def parse_raw(content, readout_mode):
    """Check actual NPY header, exact payload and native budgets before allocation."""
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_CAPTURE_BYTES:
        raise ValueError("Original RAW NPY must be nonempty bytes no larger than 64 MiB")
    stream = io.BytesIO(content)
    try:
        version = np.lib.format.read_magic(stream)
        reader = {
            (1, 0): np.lib.format.read_array_header_1_0,
            (2, 0): np.lib.format.read_array_header_2_0,
        }[version]
        shape, fortran, dtype = reader(stream)
    except (KeyError, ValueError, TypeError, EOFError) as exc:
        raise ValueError("Unsupported or invalid acquisition NPY header") from exc
    paired = readout_mode == "shared_charge_paths"
    if readout_mode not in ("sequential_frames", "shared_charge_paths"):
        raise ValueError("Explicit supported readout mode required")
    if (
        len(shape) != (4 if paired else 3)
        or any(type(n) is not int or n <= 0 for n in shape)
        or fortran
        or dtype.hasobject
        or dtype.fields
        or dtype.kind not in "iu"
        or dtype.itemsize > 4
        or math.prod(shape) * dtype.itemsize + stream.tell() != len(content)
    ):
        raise ValueError("RAW requires exact C-order integer NPY shape/dtype/payload")
    count, height, width = shape[0], *shape[-2:]
    if (
        not 1 <= count <= 64
        or (paired and shape[1] != 2)
        or min(height, width) < 2
        or max(height, width) > 4096
        or height % 2
        or width % 2
        or count * height * width > MAX_SELECTED_PIXELS
    ):
        raise ValueError("RAW exceeds native frame, path or selected-pixel bounds")
    raw = np.load(io.BytesIO(content), allow_pickle=False)
    if np.any(raw < 0):
        raise ValueError("Retained RAW codes cannot be negative")
    return raw


def _bytes(service, artifact):
    if (
        artifact.get("id") != artifact.get("sha256")
        or artifact.get("url") != "/api/v1/artifacts/" + artifact.get("sha256", "")
        or type(artifact.get("bytes")) is not int
        or not 0 <= artifact["bytes"] <= MAX_CAPTURE_BYTES
    ):
        raise IntegrityError("Invalid bounded capture artifact descriptor")
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    if (
        len(content) != artifact["bytes"]
        or hashlib.sha256(content).hexdigest() != artifact["sha256"]
    ):
        raise IntegrityError("Capture artifact differs from its retained bytes")
    return content


def _identities(raw, readout_mode):
    paths = raw if readout_mode == "shared_charge_paths" else raw[:, None]
    path_hashes = [[pixel_hash(path) for path in frame] for frame in paths]
    return {
        "raw_content_hash": pixel_hash(raw),
        "raw_acquisition_hashes": [pixel_hash(frame) for frame in raw],
        "raw_path_frame_hashes": path_hashes,
        "raw_frame_hashes": [value for frame in path_hashes for value in frame],
        "pixel_hash_encoding": PIXEL_ENCODING,
    }


def _own_tokens(capture):
    return {
        "capture-id:" + capture["id"],
        *("dataset-id:" + value for value in capture.get("dataset_ids", [])),
        *("raw-frame-sha256:" + value for value in capture.get("raw_frame_hashes", [])),
        *("raw-acquisition-sha256:" + value for value in capture.get("raw_acquisition_hashes", [])),
        *(
            ["acquisition-group-id:" + capture["acquisition_group_id"]]
            if capture.get("acquisition_group_id")
            else []
        ),
    }


def _snapshots(service, data):
    from .imaging import native_sensor_grid
    from .native_model_evidence import verify_models

    assembly = service.registry.get("assemblies", data["assembly_id"])
    test = service.registry.get("tests", data["test_id"])
    models = [service.registry.get("models", identity) for identity in assembly["model_ids"]]
    sensors = [model for model in models if model["kind"] == "sensor"]
    if len(sensors) != 1:
        raise ValueError("Acquisition assembly requires one native sensor")
    pitch, full = native_sensor_grid(sensors[0])
    if data["geometry"]["full_sensor_shape"] != full or data["pixel_pitch_um"] != pitch:
        raise ValueError("Acquisition geometry/pitch differs from registered native sensor")
    if any(fmt["cfa"] != sensors[0]["raw_format"]["cfa"] for fmt in data["raw_formats"]):
        raise ValueError(
            "Acquisition CFA differs from registered sensor; crop origin stays explicit"
        )
    verify_models(service, models)
    from .factory import verify_model_link, verify_psf_model_link

    for model in models:
        if model.get("psf_factory_job_id"):
            verify_psf_model_link(service, model)
        elif model.get("factory_job_id"):
            verify_model_link(service, model)
    return {
        "assembly_snapshot": assembly,
        "test_snapshot": test,
        "model_snapshots": models,
        "sensor_model_id": sensors[0]["id"],
    }


def _validate_raw(data, raw):
    if len(data["frames"]) != raw.shape[0]:
        raise ValueError("Every original RAW acquisition requires exactly one ordered frame record")
    validate_timing(data, raw.shape[-2:])
    for index, fmt in enumerate(data["raw_formats"]):
        values = raw[:, index] if data["readout_mode"] == "shared_charge_paths" else raw
        if np.any(values > 2 ** fmt["bit_depth"] - 1):
            raise ValueError("RAW codes exceed the declared readout ADC domain")


def _parent(service, identity, *, memo, active, depth):
    if identity in active or depth > MAX_PARENT_DEPTH:
        raise ValueError("Capture parent graph must be bounded and acyclic")
    if identity in memo:
        return memo[identity]
    if len(memo) + len(active) >= MAX_PARENT_NODES:
        raise ValueError("Capture ancestry exceeds its bounded node budget")
    record = service.registry.get("captures", identity)
    if record.get("schema") == SCHEMA:
        return _get(service, identity, memo=memo, active=active, depth=depth)
    # Old captures keep their original contract and pixel identity. Reading them
    # as ancestry does not rerun or reinterpret their observation operator.
    from .observations import load_raw_npy

    artifacts = [a for a in record.get("artifacts", []) if a.get("name") == "raw-capture.npy"]
    if len(artifacts) != 1:
        raise IntegrityError("Capture parent lacks one original RAW artifact")
    contents = {a["sha256"]: _bytes(service, a) for a in record.get("artifacts", [])}
    raw = load_raw_npy(contents[artifacts[0]["sha256"]])
    if pixel_hash(raw) != record.get("raw_content_hash") or [
        pixel_hash(frame) for frame in raw
    ] != record.get("raw_frame_hashes"):
        raise IntegrityError("Legacy capture parent RAW identity differs")
    active.add(identity)
    tokens = _own_tokens(record)
    for parent in record.get("parents", []):
        tokens.update(_parent(service, parent, memo=memo, active=active, depth=depth + 1)[1])
    active.remove(identity)
    memo[identity] = (record, tokens)
    return memo[identity]


def _parent_tokens(service, parents, *, memo, active, depth):
    tokens, snapshots = set(), []
    for identity in parents:
        record, values = _parent(service, identity, memo=memo, active=active, depth=depth + 1)
        tokens.update(values)
        snapshots.append({"id": identity, "content_hash": record["content_hash"]})
    return tokens, snapshots


def template(service, assembly_id, test_id, readout_mode="sequential_frames"):
    from .imaging import native_sensor_grid

    assembly = service.registry.get("assemblies", assembly_id)
    test = service.registry.get("tests", test_id)
    sensor = next(
        service.registry.get("models", identity)
        for identity in assembly["model_ids"]
        if service.registry.get("models", identity)["kind"] == "sensor"
    )
    pitch, full = native_sensor_grid(sensor)
    if readout_mode not in ("sequential_frames", "shared_charge_paths"):
        raise ValueError("Choose sequential_frames or shared_charge_paths")
    paths = 2 if readout_mode == "shared_charge_paths" else 1
    raw = sensor["raw_format"]
    raw_format = {
        "cfa": raw["cfa"],
        "bit_depth": raw["bit_depth"],
        "black_level_dn": raw.get("black_level_dn", raw.get("black_level")),
        "response_type": "nonlinear_charge_voltage",
        "packing": "unpacked",
        "layout": "bayer",
        "hdr_streams": 1,
    }
    data = {
        "schema": SCHEMA,
        "name": "비선형 RAW 획득 · 실제 메타데이터로 교체하세요",
        "revision": "1",
        "source_kind": "estimated",
        "source_uri": "authoring://nonlinear-capture",
        "license": "Declare original acquisition rights",
        "method_and_version": "declared-acquisition:1",
        "physical_instance": None,
        "parents": [],
        "assembly_id": assembly_id,
        "test_id": test["id"],
        "dataset_ids": ["replace-with-original-dataset-id"],
        "acquisition_group_id": "replace-with-original-acquisition-group",
        "instrument": {},
        "geometry": {"kind": "native_sensor", "full_sensor_shape": full, "crop_origin": [0, 0]},
        "pixel_pitch_um": pitch,
        "readout_mode": readout_mode,
        "raw_formats": [deepcopy(raw_format) for _ in range(paths)],
        "clock": {"status": "unknown", "reason": "Original capture clock must be supplied"},
        "timing_mode": {"shutter": "unknown", "reason": "Original shutter timing must be supplied"},
        "frames": [
            {
                "frame_id": "frame-0",
                "raw_index": 0,
                "frame_start_ns": None,
                "applied": {
                    "exposure_ns": None,
                    "gain": None,
                    "temperature_c": None,
                    "applied_at_ns": None,
                    "readout_settings": [
                        {"analog_gain": None, "digital_gain": None} for _ in range(paths)
                    ],
                },
                "settings_evidence": {
                    "status": "unknown",
                    "source_uri": "authoring://unknown",
                    "method_and_version": "unknown",
                    "description": "Supply original applied settings evidence",
                },
            }
        ],
    }
    return NonlinearCaptureImport.model_validate(data).model_dump()


def import_capture(service, metadata, raw_bytes, *, metadata_bytes=None):
    from .imaging import _current

    data = NonlinearCaptureImport.model_validate(metadata).model_dump()
    original = metadata_bytes if metadata_bytes is not None else canonical(metadata).encode()
    if not isinstance(original, bytes) or not 0 < len(original) <= MAX_METADATA_BYTES:
        raise ValueError("Acquisition metadata exceeds 1 MiB")
    if _metadata(original) != data:
        raise ValueError("Original metadata differs from normalized acquisition")
    raw = parse_raw(raw_bytes, data["readout_mode"])
    _validate_raw(data, raw)
    snapshots = _snapshots(service, data)
    tokens, parents = _parent_tokens(service, data["parents"], memo={}, active=set(), depth=0)
    identity, stamp = "capture-" + uuid.uuid4().hex, now()
    raw_artifact = service.registry.store_blob(
        raw_bytes, "raw-capture.npy", "application/octet-stream"
    )
    raw_artifact.update(
        shape=list(raw.shape), dtype=str(raw.dtype), unit="DN", spatial_sampling="native_sensor"
    )
    capture = {
        **data,
        **snapshots,
        **_identities(raw, data["readout_mode"]),
        "id": identity,
        "created_at": stamp,
        "capture_schema": SCHEMA,
        "capture_kind": "nonlinear_raw_acquisition_v1",
        "raw_shape": list(raw.shape),
        "raw_sha256": raw_artifact["sha256"],
        "readout_ids": ["HCG", "LCG"]
        if data["readout_mode"] == "shared_charge_paths"
        else ["single"],
        "conditions": deepcopy(snapshots["test_snapshot"]["conditions"]),
        "parent_capture_bindings": parents,
        "artifacts": [
            raw_artifact,
            service.registry.store_blob(original, "acquisition-metadata.json", "application/json"),
            service.registry.store_blob(
                canonical(data).encode(), "acquisition-metadata-normalized.json", "application/json"
            ),
        ],
        "registration_environment": _current(service),
        "approval_status": "unreviewed",
        "physical_validation": "not_established",
    }
    capture["exposure_tokens"] = sorted(tokens | _own_tokens(capture))
    capture["content_hash"] = digest(capture)
    return service.registry.publish_nonlinear_capture(capture)


def _get(service, identity, *, memo, active, depth):
    if identity in active or depth > MAX_PARENT_DEPTH:
        raise ValueError("Capture parent graph must be bounded and acyclic")
    if identity in memo:
        return memo[identity]
    if len(memo) + len(active) >= MAX_PARENT_NODES:
        raise ValueError("Capture ancestry exceeds its bounded node budget")
    capture = service.registry.get("captures", identity)
    if capture.get("schema") != SCHEMA or capture.get("capture_schema") != SCHEMA:
        raise ValueError("Select an explicit nonlinear acquisition revision")
    data = NonlinearCaptureImport.model_validate(
        {k: capture[k] for k in NonlinearCaptureImport.model_fields}
    ).model_dump()
    artifacts = capture["artifacts"]
    if len(artifacts) != 3 or {a["name"] for a in artifacts} != {
        "raw-capture.npy",
        "acquisition-metadata.json",
        "acquisition-metadata-normalized.json",
    }:
        raise IntegrityError("Capture requires exact original RAW and metadata artifacts")
    contents = {a["name"]: _bytes(service, a) for a in artifacts}
    if (
        len(contents["acquisition-metadata.json"]) > MAX_METADATA_BYTES
        or _metadata(contents["acquisition-metadata.json"]) != data
        or contents["acquisition-metadata-normalized.json"] != canonical(data).encode()
    ):
        raise IntegrityError("Original acquisition metadata differs from its immutable record")
    raw = parse_raw(contents["raw-capture.npy"], data["readout_mode"])
    _validate_raw(data, raw)
    raw_artifact = next(a for a in artifacts if a["name"] == "raw-capture.npy")
    expected = {
        "shape": list(raw.shape),
        "dtype": str(raw.dtype),
        "unit": "DN",
        "spatial_sampling": "native_sensor",
    }
    if any(raw_artifact.get(k) != v for k, v in expected.items()):
        raise IntegrityError("Original RAW descriptor differs from its bytes")
    snapshots = _snapshots(service, data)
    if any(capture.get(k) != value for k, value in snapshots.items()):
        raise IntegrityError("Capture model, assembly or Test snapshot differs")
    derived = {
        **_identities(raw, data["readout_mode"]),
        "raw_shape": list(raw.shape),
        "raw_sha256": raw_artifact["sha256"],
        "readout_ids": ["HCG", "LCG"]
        if data["readout_mode"] == "shared_charge_paths"
        else ["single"],
        "conditions": snapshots["test_snapshot"]["conditions"],
        "capture_kind": "nonlinear_raw_acquisition_v1",
    }
    if any(capture.get(k) != value for k, value in derived.items()):
        raise IntegrityError("Capture content/path/frame identity differs from original RAW")
    active.add(identity)
    tokens, parents = _parent_tokens(
        service, data["parents"], memo=memo, active=active, depth=depth
    )
    active.remove(identity)
    tokens.update(_own_tokens(capture))
    if capture.get("parent_capture_bindings") != parents or capture.get(
        "exposure_tokens"
    ) != sorted(tokens):
        raise IntegrityError("Capture exposure ancestry differs from its original parents")
    memo[identity] = capture, tokens
    return memo[identity]


def get(service, identity):
    return deepcopy(_get(service, identity, memo={}, active=set(), depth=0)[0])


def lineage(service, capture):
    fresh = get(service, capture["id"])
    if fresh != capture:
        raise IntegrityError("Capture lineage request differs from its frozen acquisition")
    return deepcopy(fresh["exposure_tokens"])


def evidence_closure(service, capture):
    memo = {}
    fresh, _ = _get(service, capture["id"], memo=memo, active=set(), depth=0)
    if fresh != capture:
        raise IntegrityError("Capture evidence request differs from its frozen acquisition")
    return [deepcopy(memo[identity][0]) for identity in sorted(memo)]


def load_raw(service, capture):
    fresh = get(service, capture["id"])
    if fresh != capture:
        raise IntegrityError("Capture differs from its frozen acquisition revision")
    artifact = next(a for a in fresh["artifacts"] if a["name"] == "raw-capture.npy")
    raw = parse_raw(_bytes(service, artifact), fresh["readout_mode"])
    if _identities(raw, fresh["readout_mode"])["raw_content_hash"] != fresh["raw_content_hash"]:
        raise IntegrityError("Capture RAW changed during decoder input load")
    return raw
