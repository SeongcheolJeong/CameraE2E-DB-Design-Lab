"""Immutable authored time truth for retained-code ISP comparison.

The reference is bound to a verified original RAW path but never enters the
ISP processor. Authorship is declared, not independently authenticated.
"""

from __future__ import annotations

import io
import math
from copy import deepcopy

import numpy as np

from .registry import IntegrityError, canonical, digest
from .temporal_signal_contracts import TemporalReferenceImport

SCHEMA = "camera_temporal_signal_reference_v1"
OBSERVATION_ID = "camera_temporal_signal_observation_v1"
METRIC_ID = "isp.temporal_signal_change_error"
MAX_REFERENCE_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
AUTHORITY = {
    "independence": "declared_not_independently_authenticated",
    "physical_validation": "not_established",
    "physical_measurement_count": 0,
    "reference_claim_scope": "authored_synthetic_software_validation",
    "automatic_selection": False,
}


def _source(service, source_id, selector, isp_model_id):
    from .retained_isp import validate_inputs
    from .retained_isp_replay import prepare
    from .retained_isp_source import load_inputs

    base = prepare(service, {
        "schema": "camera_retained_code_isp_replay_v1",
        "name": "Temporal reference source verification",
        "source_kind": "run",
        "source_id": source_id,
        "selector": selector,
        "isp_model_id": isp_model_id,
    })
    source = base["source"]
    raw = load_inputs(service, source)
    parsed = validate_inputs(raw, source["raw_format"], base["profile"],
                             geometry=source["geometry"],
                             frame_records=source["frame_records"],
                             events=source["retained_charge_events"])
    original = source["source_snapshot"]
    test = original["test_snapshot"]
    if service.registry.get("tests", test["id"]) != test:
        raise IntegrityError("Temporal reference original frozen Test changed")
    fixed = {k: deepcopy(base["profile"][k]) for k in ("demosaic", "white_balance", "ccm")}
    binding = {
        "source_id": source_id,
        "selector": deepcopy(selector),
        "raw_sha256": source["raw_sha256"],
        "selected_array_sha256": source["selected_array_sha256"],
        "source_snapshot_sha256": digest(original),
        "source_test_id": test["id"],
        "source_test_sha256": digest(test),
        "geometry": deepcopy(source["geometry"]),
        "raw_shape": deepcopy(source["raw_shape"]),
        "raw_format": deepcopy(source["raw_format"]),
        "clock": deepcopy(source["clock"]),
        "frame_ids": [r["frame_id"] for r in source["frame_records"]],
        "frame_conditions_sha256": digest(source["frame_conditions"]),
        "frame_records_sha256": digest(source["frame_records"]),
        "timing_sha256": digest(source["timing"]),
        "event_schedule_sha256": source["retained_charge_events"]["schedule_sha256"],
        "source_provenance": deepcopy(source["source_provenance"]),
        "isp_model_id": isp_model_id,
        "isp_model_sha256": digest(base["isp_model_snapshot"]),
        "fixed_color_profile_sha256": digest(fixed),
    }
    return source, raw, parsed["schedule"]["times"], fixed, binding


def template(service, source_id, readout_id, isp_model_id):
    selector = {"schema": "camera_retained_raw_selection_v1", "readout_id": readout_id}
    source, _, times, fixed, binding = _source(service, source_id, selector, isp_model_id)
    return {
        "schema": SCHEMA,
        "name": "Authored temporal signal reference",
        "revision": "1",
        "derivation": "authored_independent",
        "source_kind": "simulated",
        "source_uri": "synthetic://replace-with-independent-generator-id",
        "method_and_version": "replace-with-independent-generator-version",
        "license": "replace-with-source-license",
        "description": "State how truth was generated independently of ISP candidate output.",
        "uncertainty": {"kind": "unknown", "reason": "Synthetic model discrepancy is not bounded"},
        "source_id": source_id,
        "selector": selector,
        "isp_model_id": isp_model_id,
        "source_binding": binding,
        "fixed_color_profile": fixed,
        "roi": {"row_start": 0, "row_end": source["raw_shape"][1],
                "column_start": 0, "column_end": source["raw_shape"][2]},
        "row_transfer_ns": times.tolist(),
        "step": None,
        "flicker_window": None,
        "minimum_reference_change": 0.01,
        "reference_stage": "ccm_code_rgb_frames",
        "unit": "normalized_code_rgb",
    }


def _reference_array(content, expected_shape):
    if not isinstance(content, bytes) or len(content) > MAX_REFERENCE_BYTES:
        raise ValueError("Temporal reference NPY exceeds the bounded byte limit")
    stream = io.BytesIO(content)
    try:
        version = np.lib.format.read_magic(stream)
        reader = {
            (1, 0): np.lib.format.read_array_header_1_0,
            (2, 0): np.lib.format.read_array_header_2_0,
        }[version]
        shape, fortran, dtype = reader(stream)
        if (len(shape) != 4 or not 2 <= shape[0] <= 16
                or any(type(n) is not int or not 1 <= n <= 4096 for n in shape)
                or list(shape) != expected_shape or fortran or dtype != np.dtype("float64")
                or math.prod(shape) * dtype.itemsize + stream.tell() != len(content)):
            raise ValueError("Temporal truth needs exact C-order float64 F,H,W,3 NPY")
        array = np.load(io.BytesIO(content), allow_pickle=False)
    except (KeyError, EOFError, TypeError, OverflowError, MemoryError) as exc:
        raise ValueError("Invalid temporal truth NPY") from exc
    if not np.isfinite(array).all():
        raise ValueError("Temporal truth contains missing or nonfinite samples")
    return array


def _support(data, truth, raw, times):
    roi = data["roi"]
    if (set(roi) != {"row_start", "row_end", "column_start", "column_end"}
            or any(type(v) is not int for v in roi.values())):
        raise ValueError("Temporal truth needs an exact native receiver ROI")
    r0, r1, c0, c1 = (roi[k] for k in ("row_start", "row_end", "column_start", "column_end"))
    if not (0 <= r0 < r1 <= raw.shape[1] and 0 <= c0 < c1 <= raw.shape[2]
            and r1 - r0 >= 2 and c1 - c0 >= 2):
        raise ValueError("Temporal reference ROI exceeds the native receiver")
    if data["row_transfer_ns"] != times.tolist():
        raise ValueError("Temporal truth row-transfer clock differs from the original source")
    selected = truth[:, r0:r1, c0:c1]
    if np.any((selected < 0) | (selected > 1)):
        raise ValueError("Temporal reference has clipped or invalid selected RGB samples")
    if (truth.shape[0] < 2 or np.sqrt(np.mean(np.diff(selected, axis=0) ** 2))
            < data["minimum_reference_change"]):
        raise ValueError("Temporal reference has no resolved genuine time signal")
    step = data["step"]
    if step:
        onset, steady = step["onset_frame"], step["steady_start_frame"]
        if steady >= len(truth):
            raise ValueError("Step steady window exceeds the recorded acquisition")
        if (float(np.max(np.ptp(selected[:onset], axis=0)))
                > data["minimum_reference_change"]
                or float(np.max(np.ptp(selected[steady:], axis=0)))
                > data["minimum_reference_change"]):
            raise ValueError("Declared step has no stable independent pre/steady support")
        amplitude = selected[steady:].mean(axis=0) - selected[:onset].mean(axis=0)
        if np.sqrt(np.mean(amplitude ** 2)) < data["minimum_reference_change"]:
            raise ValueError("Declared step lacks a resolved independent truth amplitude")
        active = np.abs(amplitude) >= data["minimum_reference_change"]
        onset_change = selected[onset:steady + 1] - selected[:onset].mean(axis=0)
        reached = onset_change * np.sign(amplitude) >= (
            step["onset_fraction"] * np.abs(amplitude)
            - 16 * np.finfo(np.float64).eps
        )
        if not np.any(active) or not np.all(np.any(reached[:, active], axis=0)):
            raise ValueError("Declared step response misses its truth onset/steady window")
    flicker = data["flicker_window"]
    if flicker:
        start, end = flicker["start_frame"], flicker["end_frame"]
        if end > len(truth):
            raise ValueError("Period-two window exceeds recorded frames")
        stable = selected[start:end]
        if float(np.max(np.ptp(stable, axis=0))) > flicker["maximum_reference_variation"]:
            raise ValueError("Declared flicker support is not stationary in independent truth")
    return (slice(r0, r1), slice(c0, c1))


def import_reference(service, metadata, npy_bytes):
    data = TemporalReferenceImport.model_validate(metadata).model_dump()
    source, raw, times, fixed, binding = _source(
        service, data["source_id"], data["selector"], data["isp_model_id"]
    )
    if data["source_binding"] != binding or data["fixed_color_profile"] != fixed:
        raise ValueError("Temporal truth source, Test, clock or fixed ISP profile differs")
    truth = _reference_array(npy_bytes, [*raw.shape, 3])
    _support(data, truth, raw, times)
    if len(canonical(data).encode()) > MAX_METADATA_BYTES:
        raise ValueError("Temporal truth metadata exceeds the bounded byte limit")
    artifact = service.registry.store_blob(
        npy_bytes, "temporal-signal-reference.npy", "application/octet-stream"
    )
    artifact.update(shape=list(truth.shape), dtype=str(truth.dtype), unit=data["unit"])
    meta = service.registry.store_blob(
        canonical(data).encode(), "temporal-signal-reference.json", "application/json"
    )
    return service.registry.put("temporal_signal_references", {
        **data,
        "array_artifact": artifact,
        "metadata_artifact": meta,
        "artifacts": [artifact, meta],
        "shape": list(truth.shape),
        "source_test_snapshot": deepcopy(source["source_snapshot"]["test_snapshot"]),
        **AUTHORITY,
    })


def get_reference(service, identity, *, source=None):
    record = service.registry.get("temporal_signal_references", identity)
    data = TemporalReferenceImport.model_validate({
        key: record[key] for key in TemporalReferenceImport.model_fields
    }).model_dump()
    checked_source, raw, times, fixed, binding = _source(
        service, data["source_id"], data["selector"], data["isp_model_id"]
    )
    if (data["source_binding"] != binding or data["fixed_color_profile"] != fixed
            or record["source_test_snapshot"] != checked_source["source_snapshot"]["test_snapshot"]
            or (source is not None and source != checked_source)):
        raise IntegrityError("Temporal reference source or immutable Test lineage differs")
    from .retained_isp_comparison import _verify_artifacts

    _verify_artifacts(service, record["artifacts"])
    if (record["metadata_artifact"] not in record["artifacts"]
            or record["array_artifact"] not in record["artifacts"]
            or service.registry.blob_path(record["metadata_artifact"]["sha256"]).read_bytes()
            != canonical(data).encode()):
        raise IntegrityError("Temporal reference metadata differs from immutable bytes")
    content = service.registry.blob_path(record["array_artifact"]["sha256"]).read_bytes()
    truth = _reference_array(content, [*raw.shape, 3])
    _support(data, truth, raw, times)
    if (record["shape"] != list(truth.shape)
            or record["array_artifact"].get("shape") != list(truth.shape)
            or record["array_artifact"].get("dtype") != str(truth.dtype)
            or record["array_artifact"].get("unit") != data["unit"]
            or record["independence"] != AUTHORITY["independence"]
            or record["physical_validation"] != "not_established"):
        raise IntegrityError("Temporal reference descriptor or authority differs")
    return record


def load_truth(service, reference):
    checked = get_reference(service, reference["id"])
    if checked != reference:
        raise IntegrityError("Frozen temporal reference snapshot differs")
    return _reference_array(
        service.registry.blob_path(checked["array_artifact"]["sha256"]).read_bytes(),
        checked["shape"],
    )


def test_template(service, reference_id):
    reference = get_reference(service, reference_id)
    source_test = reference["source_test_snapshot"]
    binding = reference["source_binding"]
    return {
        "name": "Independent time signal · " + reference["name"],
        "revision": "temporal-reference-1",
        "validation_kind": "implementation_verification",
        "metric_id": METRIC_ID,
        "unit": "1",
        "reference_plane": "isp_fixed_color_code_rgb",
        "included_effects": ["original_retained_raw", "fixed_color_code_isp",
                             "declared_independent_time_reference"],
        "conditions": deepcopy(source_test["conditions"]),
        "required_conditions": deepcopy(source_test["required_conditions"]),
        "absolute_tolerance": {"value": 0.2, "unit": "1"},
        "uncertainty_policy": "bounded_interval",
        "calibration_dataset_ids": [],
        "stimulus": {"schema": "camera_temporal_signal_test_v1",
                     "reference_id": reference_id,
                     "reference_content_hash": reference["content_hash"],
                     "source_binding_sha256": digest(binding)},
        "parameter_uncertainty": {},
        "parent_test_id": binding["source_test_id"],
        "observation_method": {"id": OBSERVATION_ID,
                               "stage": "ccm_code_rgb_frames",
                               "truth_role": "evaluation_only"},
        "observation_specification": {
            "schema": "camera_temporal_signal_observation_spec_v1",
            "reference_id": reference_id,
            "reference_sha256": reference["array_artifact"]["sha256"],
            "source_binding": deepcopy(binding),
            "roi": deepcopy(reference["roi"]),
            "row_transfer_sha256": digest(reference["row_transfer_ns"]),
            "fixed_color_profile_sha256": digest(reference["fixed_color_profile"]),
            "step": deepcopy(reference["step"]),
            "flicker_window": deepcopy(reference["flicker_window"]),
            "minimum_reference_change": reference["minimum_reference_change"],
        },
        "observation_shape": deepcopy(reference["shape"]),
        "clock": deepcopy(binding["clock"]),
        "frame_conditions": deepcopy(_source(
            service, reference["source_id"], reference["selector"],
            reference["isp_model_id"]
        )[0]["frame_conditions"]),
    }


def validate_registered_test(service, test):
    if (test.get("observation_method") or {}).get("id") != OBSERVATION_ID:
        raise ValueError("Temporal Test needs its versioned observation method")
    spec = test.get("observation_specification") or {}
    reference_id = spec.get("reference_id")
    if not isinstance(reference_id, str):
        raise ValueError("Temporal Test needs an immutable reference ID")
    expected = test_template(service, reference_id)
    for key in expected:
        if key in {"name", "revision", "absolute_tolerance"}:
            continue
        if test.get(key) != expected[key]:
            raise ValueError("Temporal Test source, clock, ROI or reference differs: " + key)
    if test.get("validation_kind") != "implementation_verification":
        raise ValueError("Authored temporal truth cannot claim physical validation")
    return get_reference(service, reference_id)
