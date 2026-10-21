"""A source-bound relative row-timing declaration for the virtual LFM window.

This is not a vendor HDR/LFM mode decoder.  Source bytes can be retained and
hashed, but their asserted line time is not automatically extracted or proven.
The native die crop and its absolute PWM phase remain unknown.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from typing import Any

from .registry import digest

SCHEMA = "camerae2e_lens_sensor_timing_mode_v1"
CAPTURE_PROGRAM = "two_exposure_four_phase_repeated_static_scene_v1"
GEOMETRY_SCOPE = "virtual_window_origin_zero_unknown_native_crop"
PHASE_ORIGIN = "relative_phase_sweep_unknown_native_row0"
MAX_MODE_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label} requires exactly {sorted(keys)}.")
    return value


def _number(value: Any, label: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be finite and within [{low}, {high}].")
    return float(value)


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{label} requires a lowercase SHA-256 digest.")
    return value


def _text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str) or not value.strip()
        or value != value.strip() or len(value) > 512
    ):
        raise ValueError(f"{label} requires bounded nonblank text.")
    return value


def mode_binding(
    sensor_revision: dict[str, Any], saved_readout: dict[str, Any],
    readout_profile: dict[str, Any], sensor_size: list[int], exposure_s: list[float],
) -> dict[str, Any]:
    """Bind a declaration to one saved model and virtual capture contract."""
    if not isinstance(sensor_revision, dict) or not sensor_revision:
        raise ValueError("Timing mode requires a saved Sensor revision.")
    _exact(readout_profile, {"profile", "classification"}, "readout profile")
    if (
        not isinstance(sensor_size, list) or len(sensor_size) != 2
        or any(type(v) is not int or not 8 <= v <= 32 or v % 2 for v in sensor_size)
        or not isinstance(exposure_s, list) or len(exposure_s) != 2
    ):
        raise ValueError("Timing mode requires the bounded virtual window and exposure pair.")
    exposures = [_number(value, "exposure_s", 0.0005, 0.02) for value in exposure_s]
    if exposures[0] >= exposures[1]:
        raise ValueError("Timing mode exposures must be ascending.")
    return {
        "sensor_revision": deepcopy(sensor_revision),
        "saved_readout_sha256": digest(saved_readout),
        "readout_profile": _text(readout_profile["profile"], "readout_profile"),
        "readout_classification": _text(
            readout_profile["classification"], "readout_classification"
        ),
        "geometry_scope": GEOMETRY_SCOPE,
        "virtual_sensor_size": sensor_size.copy(),
        "exposure_s": exposures,
    }


def validate_timing_mode(
    record: Any, *, expected_binding: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Reject undocumented mode substitution and unsupported capture semantics."""
    mode = _exact(record, {"schema", "mode_id", "binding", "timing", "provenance"}, "mode")
    if mode["schema"] != SCHEMA:
        raise ValueError("Unsupported Lens Sensor timing-mode schema.")
    _text(mode["mode_id"], "mode_id")
    binding = _exact(
        mode["binding"],
        {
            "sensor_revision", "saved_readout_sha256", "readout_profile",
            "readout_classification", "geometry_scope", "virtual_sensor_size", "exposure_s",
        },
        "timing binding",
    )
    if not isinstance(binding["sensor_revision"], dict) or not binding["sensor_revision"]:
        raise ValueError("Timing mode lacks a Sensor revision.")
    _sha(binding["saved_readout_sha256"], "saved_readout_sha256")
    _text(binding["readout_profile"], "readout_profile")
    _text(binding["readout_classification"], "readout_classification")
    if binding["geometry_scope"] != GEOMETRY_SCOPE:
        raise ValueError("Timing mode cannot claim a located native sensor crop.")
    shape, exposures = binding["virtual_sensor_size"], binding["exposure_s"]
    if (
        not isinstance(shape, list) or len(shape) != 2
        or any(type(v) is not int or not 8 <= v <= 32 or v % 2 for v in shape)
        or not isinstance(exposures, list) or len(exposures) != 2
    ):
        raise ValueError("Timing mode requires the bounded virtual window and exposure pair.")
    parsed_exposures = [_number(value, "exposure_s", 0.0005, 0.02) for value in exposures]
    if parsed_exposures[0] >= parsed_exposures[1]:
        raise ValueError("Timing mode exposures must be ascending.")
    timing = _exact(
        mode["timing"],
        {"shutter", "line_time_s", "frame_start_s", "phase_origin", "capture_program"},
        "timing",
    )
    if (
        timing["shutter"] != "rolling"
        or type(timing["frame_start_s"]) not in (int, float)
        or timing["frame_start_s"] != 0.0
        or timing["phase_origin"] != PHASE_ORIGIN
        or timing["capture_program"] != CAPTURE_PROGRAM
    ):
        raise ValueError("Timing mode requires relative rolling rows and repeated brackets.")
    _number(timing["line_time_s"], "line_time_s", 1e-7, 1e-3)
    provenance = _exact(
        mode["provenance"],
        {
            "source_kind", "source_uri", "source_document_sha256",
            "method_and_version", "value_verification",
        },
        "timing provenance",
    )
    kind = provenance["source_kind"]
    if kind not in {"authored_assumption", "vendor_document_declared", "measurement_declared"}:
        raise ValueError("Timing source kind is unsupported.")
    uri = _text(provenance["source_uri"], "source_uri")
    _text(provenance["method_and_version"], "method_and_version")
    if provenance["value_verification"] != "declared_not_independently_verified":
        raise ValueError("Timing source cannot claim independent value verification.")
    evidence = provenance["source_document_sha256"]
    if kind == "authored_assumption":
        if evidence is not None or not uri.startswith("authored://"):
            raise ValueError("Authored timing cannot impersonate source-backed evidence.")
    elif uri.startswith("authored://"):
        raise ValueError("Source-backed timing requires an external source URI.")
    else:
        _sha(evidence, "source_document_sha256")
    if expected_binding is not None and binding != expected_binding:
        raise ValueError("Timing mode differs from the selected Sensor/readout/geometry/exposures.")
    return deepcopy(mode)


def timing_provenance(mode: dict[str, Any]) -> str:
    kind = mode["provenance"]["source_kind"]
    suffix = (
        "document_pinned_value_declared_not_verified"
        if kind == "authored_assumption"
        else "source_bytes_pinned_value_declared_not_verified"
    )
    return f"{kind}_{suffix}"


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate timing-mode JSON key: {key}.")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError(f"Timing-mode JSON contains nonfinite number {value}.")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Timing-mode JSON contains a nonfinite float.")
    return number


def validate_timing_mode_bytes(content: bytes, expected_sha256: str) -> dict[str, Any]:
    _sha(expected_sha256, "timing_mode_sha256")
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_MODE_BYTES:
        raise ValueError("Timing-mode JSON exceeds its bounded file size.")
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("Timing-mode JSON differs from its submitted SHA-256.")
    try:
        document = json.loads(
            content, object_pairs_hook=_unique_pairs, parse_constant=_nonfinite,
            parse_float=_finite_float,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Timing mode is not valid UTF-8 JSON.") from exc
    return validate_timing_mode(document)
