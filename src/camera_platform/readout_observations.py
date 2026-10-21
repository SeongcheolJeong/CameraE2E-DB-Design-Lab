"""Preserve supplied Poisson total-charge targets without inferring them from RAW.

One paired RAW row is one acquisition/site sample with two marginal codes.
Neither a target declaration nor its source label authenticates physical truth.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import numpy as np

from . import nonlinear_captures
from .readout_calibration_contracts import EVIDENCE_SCHEMA, validate_evidence
from .readout_fit_contracts import FIELDS, SCHEMA, validate_metadata
from .registry import IntegrityError, canonical, digest

MAX_METADATA_BYTES = 1024**2
MAX_EVIDENCE_BYTES = 2 * 1024**2
MAX_RAW_CODE_SAMPLES = 8192
MAX_GROUPS = 128
MAX_ANCESTORS = 64
MAX_DEPTH = 16
MAX_CLOSURE_BYTES = 64 * 1024**2
METADATA_NAME = "readout-fit-observation-metadata.json"
EVIDENCE_NAME = "readout-fit-observation-evidence.json"
NAMES = {METADATA_NAME, EVIDENCE_NAME}
SITES = ["R", "Gr", "Gb", "B"]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(content, limit, label):
    if type(content) is not bytes or not 0 < len(content) <= limit:
        raise ValueError(f"Original {label} requires bounded nonempty JSON bytes")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"Duplicate original {label} JSON key")
            value[key] = item
        return value

    def nonfinite(value):
        raise ValueError(f"Nonfinite original {label} JSON value: {value}")

    try:
        return json.loads(
            content.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite
        )
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError(f"Invalid bounded original {label} JSON") from exc


def _resources(data, path_count):
    height = data["roi"]["row_range"][1] - data["roi"]["row_range"][0]
    width = data["roi"]["column_range"][1] - data["roi"]["column_range"][0]
    sample_rows = len(data["frames"]) * height * width
    means = {mean for frame in data["frames"] for mean in frame["mean_total_charge_e"]}
    if sample_rows * path_count > MAX_RAW_CODE_SAMPLES or len(means) > MAX_GROUPS:
        raise ValueError("Observation exceeds 8192 raw code samples or 128 exact charge groups")
    return {
        "sample_rows": sample_rows,
        "raw_code_samples": sample_rows * path_count,
        "group_count": len(means),
    }


def _binding(data, capture):
    if [f["frame_id"] for f in data["frames"]] != [f["frame_id"] for f in capture["frames"]]:
        raise ValueError("Observation frame IDs must match every capture frame in original order")
    if any(fmt["cfa"] != "RGGB" for fmt in capture["raw_formats"]):
        raise ValueError("Charge fitting observations require native RGGB RAW")
    if data["physical_instance"] != capture["physical_instance"]:
        raise ValueError("Observation physical instance differs from its original capture")
    if (data["source_kind"] == "measured" or capture["source_kind"] == "measured") and data[
        "charge_reference"
    ]["kind"] == "authored_simulation":
        raise ValueError("Measured capture or observations cannot use an authored charge anchor")
    origin, shape = capture["geometry"]["crop_origin"], capture["raw_shape"][-2:]
    for values, start, size in zip(
        (data["roi"]["row_range"], data["roi"]["column_range"]), origin, shape, strict=True
    ):
        if not start <= values[0] < values[1] <= start + size:
            raise ValueError("Observation ROI must lie inside the retained full-native crop")
    path_count = 2 if capture["readout_mode"] == "shared_charge_paths" else 1
    resources = _resources(data, path_count)
    temperatures, analog, digital, common_gains, exposures = [], [], [], [], []
    for frame in capture["frames"]:
        if frame["settings_evidence"]["status"] != "recorded_applied":
            raise ValueError("Fitting observations require recorded applied capture settings")
        applied = frame["applied"]
        if any(applied[key] is None for key in ("exposure_ns", "temperature_c", "gain")):
            raise ValueError("Fitting observations require known exposure, temperature and gain")
        settings = applied["readout_settings"]
        if len(settings) != path_count or any(
            item[key] is None for item in settings for key in ("analog_gain", "digital_gain")
        ):
            raise ValueError("Fitting observations require every recorded path gain")
        temperatures.append(applied["temperature_c"])
        analog.append([item["analog_gain"] for item in settings])
        digital.append([item["digital_gain"] for item in settings])
        common_gains.append(applied["gain"])
        exposures.append(applied["exposure_ns"] / 1e9)
    if (
        any(value != temperatures[0] for value in temperatures)
        or any(value != analog[0] for value in analog)
        or any(value != digital[0] for value in digital)
        or any(value != common_gains[0] for value in common_gains)
    ):
        raise ValueError("Observation capture temperature and all gains must remain fixed")
    if path_count == 1 and common_gains[0] != analog[0][0]:
        raise ValueError("Sequential common gain differs from recorded analog gain")
    if path_count == 2 and common_gains[0] != 1:
        raise ValueError("Paired fitting uses fixed path gains and common gain 1")
    return {
        "sensor_model_id": capture["sensor_model_id"],
        "physical_instance": capture["physical_instance"],
        "readout_mode": capture["readout_mode"],
        "readout_ids": deepcopy(capture["readout_ids"]),
        "geometry": deepcopy(capture["geometry"]),
        "pixel_pitch_um": capture["pixel_pitch_um"],
        "raw_formats": deepcopy(capture["raw_formats"]),
        "roi": deepcopy(data["roi"]),
        "sites": list(SITES),
        "temperature_c": temperatures[0],
        "analog_gains": analog[0],
        "digital_gains": digital[0],
        "common_gain": common_gains[0],
        "exposure_s": exposures,
        "frame_count": len(data["frames"]),
        **resources,
        "sample_count_basis": "one_acquisition_site_row_with_one_or_two_marginal_codes",
    }


def _state():
    return {"observations": {}, "captures": {}, "visited": set(), "bytes": 0}


def _capture(service, identity, state, data):
    # Use only the registered mode to reject excessive declared work before RAW
    # array loading. This is not acceptance: fresh capture/NPY verification and
    # the exact mode/settings/native binding are mandatory immediately below.
    hint = service.registry.get("captures", identity)
    if hint.get("readout_mode") not in ("sequential_frames", "shared_charge_paths"):
        raise ValueError("Fitting observations require an original nonlinear capture")
    _resources(data, 2 if hint["readout_mode"] == "shared_charge_paths" else 1)
    capture = nonlinear_captures.get(service, identity)
    closure = nonlinear_captures.evidence_closure(service, capture)
    if (
        len(set(state["captures"]) | {c["id"] for c in closure})
        > nonlinear_captures.MAX_PARENT_NODES
    ):
        raise ValueError("Observation capture ancestry exceeds its node budget")
    for record in closure:
        existing = state["captures"].get(record["id"])
        if existing is not None and existing != record:
            raise IntegrityError("Capture changed within observation evidence closure")
        state["captures"][record["id"]] = record
    return capture, {record["id"]: digest(record) for record in closure}


def _compatible_parent(binding, parent):
    if any(
        binding[key] != parent["binding"][key]
        for key in ("sensor_model_id", "physical_instance", "readout_mode")
    ):
        raise ValueError("Observation ancestry changes sensor, physical instance or readout mode")


def _artifact_bytes(service, artifact):
    name = artifact.get("name")
    limit = MAX_METADATA_BYTES if name == METADATA_NAME else MAX_EVIDENCE_BYTES
    if (
        name not in NAMES
        or set(artifact) != {"id", "name", "sha256", "bytes", "media_type", "url"}
        or artifact.get("media_type") != "application/json"
        or type(artifact.get("bytes")) is not int
        or not 0 < artifact["bytes"] <= limit
        or artifact.get("id") != artifact.get("sha256")
        or artifact.get("url") != "/api/v1/artifacts/" + artifact.get("sha256", "")
    ):
        raise IntegrityError("Invalid original observation artifact descriptor")
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    if len(content) != artifact["bytes"] or _sha(content) != artifact["sha256"]:
        raise IntegrityError("Original observation artifact bytes differ")
    return content


def _derived(data, capture, closure, evidence_bytes, evidence, binding, parents):
    return {
        "capture_snapshot": capture,
        "capture_sha256": digest(capture),
        "capture_closure_sha256": closure,
        "binding": binding,
        "evidence_sha256": _sha(evidence_bytes),
        "evidence_normalized_sha256": digest(evidence),
        "parent_observation_sha256": {parent["id"]: digest(parent) for parent in parents},
        "physical_validation": "not_established",
        "approval_status": "draft",
        "authority": {"physical_validation": False, "release": False},
        "interpretation": "supplied_total_pre_full_well_poisson_charge_means_no_raw_inference",
        "assumptions": {
            "targets": "declared_total_charge_including_dark_not_independently_authenticated",
            "site_uniformity": "declared_uniform_mean_within_each_selected_native_CFA_site",
            "temporal_statistics": "declared_Poisson_pre_full_well_charge",
            "paired_paths": "same_acquisition_and_charge_target_not_independent_exposures",
            "total_physical_uncertainty": "unknown",
        },
    }


def _read(service, identity, state, active):
    if identity in active or len(active) > MAX_DEPTH:
        raise IntegrityError("Observation ancestry must be bounded and acyclic")
    if identity in state["observations"]:
        return state["observations"][identity]
    if len(state["visited"]) >= MAX_ANCESTORS:
        raise IntegrityError("Observation ancestry exceeds 64 revisions")
    state["visited"].add(identity)
    record = service.registry.get("readout_observations", identity)
    data = validate_metadata({key: record[key] for key in FIELDS})
    artifacts = record["artifacts"]
    if len(artifacts) != 2 or {a["name"] for a in artifacts} != NAMES:
        raise IntegrityError("Exactly two original observation JSON artifacts required")
    contents = {a["name"]: _artifact_bytes(service, a) for a in artifacts}
    state["bytes"] += sum(len(content) for content in contents.values())
    if state["bytes"] > MAX_CLOSURE_BYTES:
        raise IntegrityError("Observation evidence ancestry exceeds 64 MiB")
    if (
        validate_metadata(
            _json(contents[METADATA_NAME], MAX_METADATA_BYTES, "observation metadata")
        )
        != data
    ):
        raise IntegrityError("Original observation metadata differs from its immutable record")
    evidence = validate_evidence(
        _json(contents[EVIDENCE_NAME], MAX_EVIDENCE_BYTES, "charge evidence"), data
    )
    capture, closure = _capture(service, data["capture_id"], state, data)
    binding = _binding(data, capture)
    parents = [_read(service, parent, state, active | {identity}) for parent in data["parents"]]
    for parent in parents:
        _compatible_parent(binding, parent)
    expected = _derived(data, capture, closure, contents[EVIDENCE_NAME], evidence, binding, parents)
    if any(record.get(key) != value for key, value in expected.items()):
        raise IntegrityError("Observation capture, settings, targets or evidence binding differs")
    state["observations"][identity] = record
    return record


def _references(state):
    references = [("readout_observations", record) for record in state["observations"].values()]
    for capture in state["captures"].values():
        references.append(("captures", capture))
        references.extend(("models", model) for model in capture.get("model_snapshots", []))
        for field, kind in (("assembly_snapshot", "assemblies"), ("test_snapshot", "tests")):
            if field in capture:
                references.append((kind, capture[field]))
    return references


def import_observations(service, metadata, evidence_bytes, *, metadata_bytes=None):
    data = validate_metadata(metadata)
    original = canonical(metadata).encode() if metadata_bytes is None else metadata_bytes
    if validate_metadata(_json(original, MAX_METADATA_BYTES, "observation metadata")) != data:
        raise ValueError("Original observation metadata differs from supplied fields")
    evidence = validate_evidence(_json(evidence_bytes, MAX_EVIDENCE_BYTES, "charge evidence"), data)
    state = _state()
    capture, closure = _capture(service, data["capture_id"], state, data)
    binding = _binding(data, capture)
    parents = [_read(service, parent, state, set()) for parent in data["parents"]]
    if (
        len(state["observations"]) >= MAX_ANCESTORS
        or state["bytes"] + len(original) + len(evidence_bytes) > MAX_CLOSURE_BYTES
    ):
        raise ValueError("New observation exceeds its bounded ancestry budget")
    for parent in parents:
        _compatible_parent(binding, parent)
    artifacts = [
        service.registry.store_blob(original, METADATA_NAME, "application/json"),
        service.registry.store_blob(evidence_bytes, EVIDENCE_NAME, "application/json"),
    ]
    return service.registry.put_verified_record(
        "readout_observations",
        {
            **data,
            **_derived(data, capture, closure, evidence_bytes, evidence, binding, parents),
            "artifacts": artifacts,
        },
        _references(state),
    )


def get(service, identity):
    return deepcopy(_read(service, identity, _state(), set()))


def lineage(service, record):
    state = _state()
    fresh = _read(service, record["id"], state, set())
    if fresh != record:
        raise IntegrityError("Observation lineage request differs from its immutable revision")
    tokens = set()
    artifacts = {}
    for capture in state["captures"].values():
        # Fresh capture closure verification above includes legacy parent pixel identities.
        tokens.update(nonlinear_captures._own_tokens(capture))
    for observation in state["observations"].values():
        tokens.update("dataset-id:" + identity for identity in observation["dataset_ids"])
        tokens.add("readout-observation-id:" + observation["id"])
    for item in [*state["captures"].values(), *state["observations"].values()]:
        for artifact in item.get("artifacts", []):
            key = artifact["sha256"], artifact["name"]
            if key in artifacts and artifacts[key] != artifact:
                raise IntegrityError("Observation closure has conflicting artifact descriptors")
            artifacts[key] = artifact
    return {
        "observations": [
            deepcopy(state["observations"][key]) for key in sorted(state["observations"])
        ],
        "captures": [deepcopy(state["captures"][key]) for key in sorted(state["captures"])],
        "tokens": sorted(tokens),
        "artifacts": [deepcopy(artifacts[key]) for key in sorted(artifacts)],
    }


def groups(service, record):
    fresh = get(service, record["id"])
    if fresh != record:
        raise IntegrityError("Observation group request differs from its immutable revision")
    capture = fresh["capture_snapshot"]
    binding = _binding(fresh, capture)  # Check selected sample/group limits before grouping.
    raw = nonlinear_captures.load_raw(service, capture)
    row0, row1 = fresh["roi"]["row_range"]
    col0, col1 = fresh["roi"]["column_range"]
    origin = capture["geometry"]["crop_origin"]
    row0, row1, col0, col1 = row0 - origin[0], row1 - origin[0], col0 - origin[1], col1 - origin[1]
    paired = capture["readout_mode"] == "shared_charge_paths"
    pieces = {}
    for index, frame in enumerate(fresh["frames"]):
        for site, (dr, dc) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
            if paired:
                values = raw[index, :, row0 + dr : row1 : 2, col0 + dc : col1 : 2].reshape(2, -1).T
            else:
                values = raw[index, row0 + dr : row1 : 2, col0 + dc : col1 : 2].reshape(-1, 1)
            pieces.setdefault(frame["mean_total_charge_e"][site], []).append(values)
    return {
        **deepcopy(binding),
        "capture": capture,
        "groups": [
            {"mean_charge_e": float(mean), "raw_dn": np.concatenate(pieces[mean], axis=0)}
            for mean in sorted(pieces)
        ],
    }


def template(service, capture_id):
    capture = nonlinear_captures.get(service, capture_id)
    origin, shape = capture["geometry"]["crop_origin"], capture["raw_shape"][-2:]
    ranges = [
        [start + start % 2, start + size - (start + size) % 2]
        for start, size in zip(origin, shape, strict=True)
    ]
    if any(end <= start for start, end in ranges):
        raise ValueError("Retained crop has no complete native RGGB cell")
    # Keep even a large capture's starting template within the sample budget.
    return {
        "schema": SCHEMA,
        "name": "전하 fitting 관측 · 독립 전하 평균을 입력하세요",
        "revision": "1",
        "source_kind": "estimated",
        "source_uri": "authoring://readout-fit-observations",
        "license": "Declare original target evidence rights",
        "method_and_version": "declared-total-charge-targets:1",
        "physical_instance": capture["physical_instance"],
        "parents": [],
        "capture_id": capture_id,
        "dataset_role": "development",
        "dataset_ids": ["replace-with-original-target-dataset-id"],
        "roi": {
            "row_range": [ranges[0][0], ranges[0][0] + 2],
            "column_range": [ranges[1][0], ranges[1][0] + 2],
        },
        "charge_statistics": "poisson_total_pre_full_well",
        "charge_reference": {
            "kind": "authored_simulation",
            "reference_id": "replace-with-authored-or-external-reference",
            "source_uri": "authoring://readout-fit-targets",
            "method_and_version": "authoring-placeholder:1",
            "instrument": {
                "id": "not-a-measured-instrument",
                "calibration_revision": "authoring-placeholder",
            },
            "uncertainty": {
                "kind": "absolute_bound",
                "value": None,
                "unit": "electron",
                "description": (
                    "Supply a justified reference uncertainty; "
                    "this template is not measurement evidence."
                ),
            },
            "evidence_document_ids": ["replace-with-charge-reference-document"],
        },
        "frames": [
            {"frame_id": frame["frame_id"], "mean_total_charge_e": [None] * 4}
            for frame in capture["frames"]
        ],
    }


def evidence_template(metadata):
    reference = metadata["charge_reference"]
    if reference["kind"] != "authored_simulation":
        raise ValueError("External charge-reference evidence must be supplied, never generated")
    return {
        "schema": EVIDENCE_SCHEMA,
        "documents": [
            {
                "id": identity,
                "role": "charge_reference",
                "source_uri": reference["source_uri"],
                "method_and_version": reference["method_and_version"],
                "description": (
                    "Authoring guidance only: supply original independent total pre-full-well "
                    "charge means, including dark, with justified uncertainty. "
                    "No means are inferred from these RAW codes."
                ),
                "tables": [],
            }
            for identity in reference["evidence_document_ids"]
        ],
    }
