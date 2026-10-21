"""Bounded consistency checks for declared HDR/LFM acquisition logs.

The three JSON logs are supplied by the submitter.  Their internal agreement
with the frozen simulation and uploaded arrays can be checked, but a matching
hash or self-consistent declaration does not authenticate an instrument or
establish independent physical ground truth.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from .registry import canonical

SCHEMA = "camerae2e_hdr_lfm_reference_packet_v1"
MODE_SCHEMA = "camerae2e_hdr_lfm_acquisition_mode_log_v1"
WAVEFORM_SCHEMA = "camerae2e_hdr_lfm_waveform_log_v1"
REFERENCE_SCHEMA = "camerae2e_hdr_lfm_reference_rate_log_v1"
MAX_LOG_BYTES = 2 * 1024 * 1024


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"Duplicate HDR/LFM log key: {key}")
        document[key] = value
    return document


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"Nonfinite HDR/LFM log value: {value}")


def _document(data: bytes, name: str) -> dict[str, Any]:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_LOG_BYTES:
        raise ValueError(f"{name} must be a bounded UTF-8 JSON document")
    try:
        result = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_pairs,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} must be valid UTF-8 JSON") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{name} must be a JSON object")
    return result


def _fields(value: Any, names: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != names:
        raise ValueError(f"{name} has missing or unsupported fields")
    return value


def _same(actual: Any, expected: Any, name: str) -> None:
    if canonical(actual) != canonical(expected):
        raise ValueError(f"{name} differs from the frozen HDR/LFM acquisition")


def _sha(value: Any, name: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError(f"{name} must be a nonblank bounded string")
    return value.strip()


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (positive and value <= 0)):
        raise ValueError(f"{name} must be a finite {'positive ' if positive else ''}number")
    return float(value)


def _array_sha(array: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(array).tobytes(order="C")).hexdigest()


def contract() -> dict[str, Any]:
    """Return a compact, discoverable description for the intake template."""
    return {
        "schema": SCHEMA,
        "formats": {
            "mode_log": MODE_SCHEMA,
            "waveform_log": WAVEFORM_SCHEMA,
            "reference_log": REFERENCE_SCHEMA,
        },
        "mode_log_fields": [
            "schema", "source_uri", "source_hdr_job_id", "source_hdr_report_sha256",
            "sensor_revision", "physical_instance", "instrument", "mode",
            "raw_acquisition", "raw_file_sha256", "native_cfa_pattern",
            "native_cfa_tile_origin_row_col", "frame_order",
        ],
        "frame_order_fields": [
            "array_index", "frame_id", "acquisition_order", "row_zero_timestamp_ns",
            "exposure_index", "phase_index", "exposure_s",
            "phase_fraction_of_period", "row_zero_phase_s", "raw_frame_sha256",
        ],
        "waveform_log_fields": [
            "schema", "source_uri", "source_hdr_job_id", "source_hdr_report_sha256",
            "mode_id", "timing_mode_sha256", "mode_log_sha256", "raw_file_sha256",
            "line_time_s", "phase_alignment", "stimulus",
        ],
        "reference_log_fields": [
            "schema", "source_uri", "source_hdr_job_id", "source_hdr_report_sha256",
            "mode_log_sha256", "waveform_log_sha256", "raw_file_sha256",
            "reference_rate_file_sha256", "reference_rate", "source_acquisition_id",
            "phase_order", "calibration",
        ],
        "reference_phase_fields": [
            "phase_index", "phase_fraction_of_period", "reference_phase_sha256",
        ],
        "calibration_fields": [
            "reference_instrument_id", "calibration_revision", "method_and_version",
            "calibration_document_sha256", "relative_standard_uncertainty",
            "applied_to_reference_array", "uses_uploaded_raw", "uses_model_output",
        ],
        "array_order": "C-order [2 exposure, 4 phase, row, column]; index=exposure*4+phase",
        "native_cfa_constraint": "2x2 RGGB virtual origin zero requires even-even native crop",
        "scope": "declared_packet_internal_consistency_only_not_source_authentication",
    }


def validate_packet(
    metadata: Mapping[str, Any], files: Mapping[str, bytes], *,
    raw: np.ndarray, reference_rate: np.ndarray,
    source_lfm_report: Mapping[str, Any], native_cfa: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify three supplied JSON logs against arrays and the saved model.

    This does not verify that any log was produced by the named instrument.
    The model window is unlocated on the physical die and no pixelwise metric
    is permitted, even when the declared crop/CFA phase is self-consistent.
    """
    mode_log = _document(files["mode_log"], "mode_log")
    waveform_log = _document(files["waveform_log"], "waveform_log")
    reference_log = _document(files["reference_log"], "reference_log")
    mode_sha = hashlib.sha256(files["mode_log"]).hexdigest()
    waveform_sha = hashlib.sha256(files["waveform_log"]).hexdigest()
    raw_sha = hashlib.sha256(files["measured_raw"]).hexdigest()
    rate_sha = hashlib.sha256(files["measured_reference_rate"]).hexdigest()
    source_id = metadata["source_hdr_job_id"]
    report_sha = metadata["source_hdr_report_sha256"]
    controls = source_lfm_report["controls"]
    exposures = controls["exposure_s"]
    stimulus = metadata["stimulus"]
    fractions = stimulus["phase_fractions_of_period"]
    origin = metadata["mode"]["native_crop_origin_row_col"]
    pattern = native_cfa["pattern"]
    if (pattern != [[1, 2], [2, 3]] or len(origin) != 2
            or origin[0] % 2 or origin[1] % 2):
        raise ValueError("Native crop CFA phase differs from virtual RGGB origin zero")
    if raw.shape != (2, 4, *metadata["raw_acquisition"]["shape"][2:]):
        raise ValueError("Measured RAW differs from the 2x4 packet frame contract")
    if reference_rate.shape != tuple(metadata["reference_rate"]["shape"]):
        raise ValueError("Reference rate differs from the packet phase grid")

    _fields(mode_log, set(contract()["mode_log_fields"]), "mode_log")
    _same(mode_log["schema"], MODE_SCHEMA, "mode_log.schema")
    _same(mode_log["source_uri"], metadata["sources"]["mode_log"]["uri"],
          "mode_log.source_uri")
    for key, expected in (
        ("source_hdr_job_id", source_id),
        ("source_hdr_report_sha256", report_sha),
        ("sensor_revision", metadata["sensor_revision"]),
        ("physical_instance", metadata["physical_instance"]),
        ("instrument", metadata["instrument"]),
        ("mode", metadata["mode"]),
        ("raw_acquisition", metadata["raw_acquisition"]),
        ("raw_file_sha256", raw_sha),
        ("native_cfa_pattern", pattern),
        ("native_cfa_tile_origin_row_col", [0, 0]),
    ):
        _same(mode_log[key], expected, f"mode_log.{key}")
    frames = mode_log["frame_order"]
    if not isinstance(frames, list) or len(frames) != 8:
        raise ValueError("mode_log.frame_order needs exactly 2x4 RAW labels")
    frame_ids: set[str] = set()
    capture_orders: set[int] = set()
    timestamps: dict[int, int] = {}
    for index, frame in enumerate(frames):
        _fields(frame, set(contract()["frame_order_fields"]),
                f"mode_log.frame_order[{index}]")
        exposure_index, phase_index = divmod(index, 4)
        if (type(frame["array_index"]) is not int or frame["array_index"] != index
                or type(frame["exposure_index"]) is not int
                or frame["exposure_index"] != exposure_index
                or type(frame["phase_index"]) is not int
                or frame["phase_index"] != phase_index):
            raise ValueError("RAW frame array order or exposure/phase labels differ")
        frame_id = _identity(frame["frame_id"], "frame_id")
        if frame_id in frame_ids:
            raise ValueError("RAW frame IDs must be unique")
        frame_ids.add(frame_id)
        order = frame["acquisition_order"]
        timestamp = frame["row_zero_timestamp_ns"]
        if (type(order) is not int or not 0 <= order < 8
                or order in capture_orders or type(timestamp) is not int
                or timestamp < 0 or timestamp >= 2**63):
            raise ValueError("RAW acquisition order or row-zero timestamp is invalid")
        capture_orders.add(order)
        timestamps[order] = timestamp
        _number(frame["exposure_s"], "frame.exposure_s", positive=True)
        _number(frame["phase_fraction_of_period"], "frame.phase_fraction_of_period")
        _number(frame["row_zero_phase_s"], "frame.row_zero_phase_s")
        _same(frame["exposure_s"], exposures[exposure_index], "frame.exposure_s")
        _same(frame["phase_fraction_of_period"], fractions[phase_index],
              "frame.phase_fraction_of_period")
        _same(frame["row_zero_phase_s"],
              fractions[phase_index] * stimulus["pwm_period_s"],
              "frame.row_zero_phase_s")
        _same(frame["raw_frame_sha256"], _array_sha(raw[exposure_index, phase_index]),
              "frame.raw_frame_sha256")
    if list(sorted(capture_orders)) != list(range(8)) or any(
        timestamps[index] >= timestamps[index + 1] for index in range(7)
    ):
        raise ValueError("RAW frame acquisition timestamps must increase in declared order")

    _fields(waveform_log, set(contract()["waveform_log_fields"]), "waveform_log")
    _same(waveform_log["schema"], WAVEFORM_SCHEMA, "waveform_log.schema")
    _same(waveform_log["source_uri"], metadata["sources"]["waveform_log"]["uri"],
          "waveform_log.source_uri")
    for key, expected in (
        ("source_hdr_job_id", source_id),
        ("source_hdr_report_sha256", report_sha),
        ("mode_id", metadata["mode"]["mode_id"]),
        ("timing_mode_sha256", metadata["mode"]["timing_mode_sha256"]),
        ("mode_log_sha256", mode_sha),
        ("raw_file_sha256", raw_sha),
        ("line_time_s", metadata["mode"]["line_time_s"]),
        ("phase_alignment", metadata["mode"]["phase_alignment"]),
        ("stimulus", stimulus),
    ):
        _same(waveform_log[key], expected, f"waveform_log.{key}")

    _fields(reference_log, set(contract()["reference_log_fields"]), "reference_log")
    _same(reference_log["schema"], REFERENCE_SCHEMA, "reference_log.schema")
    _same(reference_log["source_uri"], metadata["sources"]["reference_log"]["uri"],
          "reference_log.source_uri")
    for key, expected in (
        ("source_hdr_job_id", source_id),
        ("source_hdr_report_sha256", report_sha),
        ("mode_log_sha256", mode_sha),
        ("waveform_log_sha256", waveform_sha),
        ("raw_file_sha256", raw_sha),
        ("reference_rate_file_sha256", rate_sha),
        ("reference_rate", metadata["reference_rate"]),
    ):
        _same(reference_log[key], expected, f"reference_log.{key}")
    acquisition_id = _identity(reference_log["source_acquisition_id"],
                               "reference_log.source_acquisition_id")
    if acquisition_id in frame_ids or acquisition_id == metadata["physical_instance"]:
        raise ValueError("Reference source acquisition must differ from RAW frames")
    phase_order = reference_log["phase_order"]
    if not isinstance(phase_order, list) or len(phase_order) != 4:
        raise ValueError("Reference phase order must contain exactly four labels")
    for phase_index, phase in enumerate(phase_order):
        _fields(phase, set(contract()["reference_phase_fields"]),
                f"reference_log.phase_order[{phase_index}]")
        if type(phase["phase_index"]) is not int or phase["phase_index"] != phase_index:
            raise ValueError("Reference phase array order differs from frozen HDR phase order")
        _same(phase["phase_fraction_of_period"], fractions[phase_index],
              "reference.phase_fraction_of_period")
        _same(phase["reference_phase_sha256"], _array_sha(reference_rate[phase_index]),
              "reference.reference_phase_sha256")
    calibration = _fields(reference_log["calibration"],
                          set(contract()["calibration_fields"]),
                          "reference_log.calibration")
    reference_instrument = _identity(calibration["reference_instrument_id"],
                                     "reference_instrument_id")
    if reference_instrument == metadata["instrument"]["id"]:
        raise ValueError("Reference calibration instrument must differ from RAW instrument")
    _identity(calibration["calibration_revision"], "calibration_revision")
    _identity(calibration["method_and_version"], "method_and_version")
    calibration_sha = _sha(calibration["calibration_document_sha256"],
                           "calibration_document_sha256")
    if calibration_sha in {raw_sha, rate_sha, mode_sha, waveform_sha}:
        raise ValueError("Reference calibration document cannot reuse a packet artifact")
    uncertainty = _number(calibration["relative_standard_uncertainty"],
                          "relative_standard_uncertainty", positive=True)
    if uncertainty > 1:
        raise ValueError("Reference relative uncertainty must be at most 1")
    for key, expected in (
        ("applied_to_reference_array", True),
        ("uses_uploaded_raw", False),
        ("uses_model_output", False),
    ):
        if calibration[key] is not expected:
            raise ValueError(f"reference_log.calibration.{key} must be {expected}")
    return {
        "schema": SCHEMA,
        "mode_log_sha256": mode_sha,
        "waveform_log_sha256": waveform_sha,
        "reference_log_sha256": hashlib.sha256(files["reference_log"]).hexdigest(),
        "measured_raw_sha256": raw_sha,
        "measured_reference_rate_sha256": rate_sha,
        "frame_count": 8,
        "native_cfa_tile_phase_row_col": [0, 0],
        "declared_native_crop_origin_row_col": list(origin),
        "reference_calibration_document_sha256": calibration_sha,
        "relative_standard_uncertainty_declared": uncertainty,
        "internal_consistency": "verified_against_uploaded_arrays_and_frozen_job",
        "source_authenticity": "declared_not_independently_verified",
        "timing_value_verification": "self_consistent_not_independently_verified",
        "spatial_registration": "not_established",
        "physical_validation": "not_established",
        "product_hdr_lfm_decision": "inconclusive",
    }
