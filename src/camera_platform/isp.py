"""Deterministic, bounded RAW replay through an explicit floating-point ISP.

This processes acquired pixel codes only. It neither resamples acquisition nor
establishes color accuracy. Signed linear intermediates are retained; clipping
occurs once, at the declared input to the output transfer function.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA = "camera_isp_profile_v1"
IMPLEMENTATION = "camera_platform_float_isp_v1"
GRAPH = ["black_level", "demosaic", "white_balance", "ccm", "tone"]
MAX_FRAMES = 64
MAX_FRAME_PIXELS = 1_048_576
MAX_SIDE = 4096


class ISPError(ValueError):
    """A rejected format, profile, geometry, or bounded execution request."""

    def __init__(self, message: str, field: str = "isp", code: str = "INVALID_ISP_REQUEST"):
        self.issues = [{"code": code, "field": field, "message": message, "severity": "error"}]
        super().__init__(message)


def _refuse(message: str, field: str, code: str = "INVALID_ISP_REQUEST"):
    raise ISPError(message, field, code)


def _exact(value: Any, keys: set[str], field: str) -> Mapping:
    if not isinstance(value, Mapping) or set(value) != keys:
        _refuse(f"{field} requires exactly {sorted(keys)}", field)
    return value


def _number(value: Any, low: float, high: float, field: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)):
        _refuse("A finite real number is required", field)
    if np.iscomplexobj(value):
        _refuse("Complex tuning values are unsupported", field)
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ISPError("A representable finite real number is required", field) from exc
    if not np.isfinite(number) or not low <= number <= high:
        _refuse(f"Value must be finite and in [{low}, {high}]", field)
    return number


def _integers(value: Any, count: int, field: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        _refuse(f"{field} requires {count} integers", field)
    if not all(
        isinstance(v, (int, np.integer)) and not isinstance(v, (bool, np.bool_)) for v in value
    ):
        _refuse("Geometry requires integers without implicit rounding", field)
    return [int(v) for v in value]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def default_profile() -> dict[str, Any]:
    """Legacy-compatible fixed sensor-RGB visualization, with every stage explicit."""
    return {
        "schema": SCHEMA,
        "graph": GRAPH.copy(),
        "precision": "float64",
        "black_level": {"source": "raw_format", "negative_policy": "preserve"},
        "demosaic": {"method": "bilinear", "boundary": "reflect"},
        "white_balance": {"mode": "fixed", "gains_rgb": [1.0, 1.0, 1.0]},
        "ccm": {
            "matrix_rgb": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "orientation": "output_rows_input_columns",
        },
        "tone": {"curve": "srgb", "clip_input": [0.0, 1.0]},
        "disabled_effects": ["auto_white_balance", "lens_shading_correction", "hdr_merge"],
    }


def validate_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a complete versioned graph; no implied stages or unknown knobs."""
    expected = default_profile()
    _exact(profile, set(expected), "profile")
    for key in ("schema", "graph", "precision", "disabled_effects"):
        if profile[key] != expected[key]:
            _refuse(f"Unsupported or reordered {key}", "profile." + key)
    for key in ("black_level", "demosaic"):
        if not isinstance(profile[key], Mapping) or dict(profile[key]) != expected[key]:
            _refuse(f"Unsupported {key} policy", "profile." + key)
    wb = _exact(profile["white_balance"], {"mode", "gains_rgb"}, "profile.white_balance")
    if wb["mode"] != "fixed" or not isinstance(wb["gains_rgb"], (list, tuple)):
        _refuse("White balance requires explicit fixed RGB gains", "profile.white_balance")
    if len(wb["gains_rgb"]) != 3:
        _refuse("White balance requires exactly three RGB gains", "profile.white_balance")
    gains = [
        _number(value, 1 / 16, 16, f"profile.white_balance.gains_rgb[{i}]")
        for i, value in enumerate(wb["gains_rgb"])
    ]
    ccm = _exact(profile["ccm"], {"matrix_rgb", "orientation"}, "profile.ccm")
    if ccm["orientation"] != "output_rows_input_columns":
        _refuse("CCM orientation must be output_rows_input_columns", "profile.ccm")
    matrix = ccm["matrix_rgb"]
    if not isinstance(matrix, (list, tuple)) or len(matrix) != 3:
        _refuse("CCM requires a 3 by 3 matrix", "profile.ccm.matrix_rgb")
    normalized_matrix = []
    for i, row in enumerate(matrix):
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            _refuse("CCM requires a 3 by 3 matrix", "profile.ccm.matrix_rgb")
        normalized_row = [
            _number(value, -8, 8, f"profile.ccm.matrix_rgb[{i}][{j}]")
            for j, value in enumerate(row)
        ]
        if sum(abs(v) for v in normalized_row) > 16:
            _refuse("Each CCM row's absolute coefficient sum must be <=16", "profile.ccm")
        normalized_matrix.append(normalized_row)
    tone = _exact(profile["tone"], {"curve", "clip_input"}, "profile.tone")
    clip_input = tone["clip_input"]
    if (
        tone["curve"] not in ("linear", "srgb")
        or not isinstance(clip_input, list)
        or len(clip_input) != 2
        or [_number(v, 0, 1, "profile.tone.clip_input") for v in clip_input] != [0.0, 1.0]
    ):
        _refuse("Tone requires linear or srgb with explicit input clip [0,1]", "profile.tone")
    # Return JSON-native normalized values, detached from the caller's mutable graph.
    result = copy.deepcopy(expected)
    result["white_balance"]["gains_rgb"] = gains
    result["ccm"]["matrix_rgb"] = normalized_matrix
    result["tone"]["curve"] = tone["curve"]
    return result


def describe_capabilities() -> dict[str, Any]:
    """Describe this bounded software path without acquiring or decoding RAW."""
    return {
        "implementation_id": IMPLEMENTATION,
        "profile_schema": SCHEMA,
        "graph": GRAPH.copy(),
        "supports": [
            "linear_unpacked_integer_rggb",
            "native_crop_cfa_phase",
            "signed_linear_intermediates",
            "fixed_rgb_white_balance",
            "explicit_3x3_ccm",
            "linear_or_srgb_transfer",
            "all_frames",
        ],
        "unsupported": [
            "packed_raw",
            "other_full_sensor_cfa",
            "automatic_controls",
            "lens_shading_correction",
            "hdr",
            "noise_resampling",
            "fixed_point_hardware_equivalence",
            "physical_color_accuracy",
        ],
        "limits": {
            "max_frames": MAX_FRAMES,
            "max_side": MAX_SIDE,
            "max_frame_pixels": MAX_FRAME_PIXELS,
        },
        "precision": "float64",
        "physical_validation": "not_established",
    }


def _input(raw: Any, raw_format: Any, geometry: Any) -> tuple[np.ndarray, dict, dict, np.ndarray]:
    if (
        not isinstance(raw, np.ndarray)
        or raw.dtype.fields
        or raw.dtype.hasobject
        or raw.dtype.kind not in {"u", "i"}
        or raw.dtype.itemsize > 4
        or raw.ndim not in (2, 3)
    ):
        _refuse("RAW must be an unpacked integer ndarray [F,H,W] or [H,W], <=32 bits", "raw")
    frames = raw[None, ...] if raw.ndim == 2 else raw
    count, rows, cols = frames.shape
    if not (
        1 <= count <= MAX_FRAMES
        and 2 <= rows <= MAX_SIDE
        and 2 <= cols <= MAX_SIDE
        and rows % 2 == 0
        and cols % 2 == 0
        and count * rows * cols <= MAX_FRAME_PIXELS
    ):
        _refuse(
            "RAW requires 1..64 frames, even sides in [2,4096], and <=1048576 frame pixels",
            "raw.shape",
            "RESOURCE_LIMIT",
        )
    required = {
        "cfa",
        "bit_depth",
        "black_level_dn",
        "response_type",
        "packing",
        "layout",
        "hdr_streams",
    }
    if not isinstance(raw_format, Mapping) or not required <= set(raw_format):
        _refuse("An explicit complete linear unpacked RGGB format is required", "raw_format")
    if set(raw_format) - required - {"white_level_dn", "black_level"}:
        _refuse("Unknown RAW format or acquisition transform", "raw_format")
    for key, expected in {
        "cfa": "RGGB",
        "response_type": "linear",
        "packing": "unpacked",
        "layout": "bayer",
    }.items():
        if raw_format[key] != expected:
            _refuse(f"RAW {key} must be {expected}", "raw_format." + key)
    streams, bits = raw_format["hdr_streams"], raw_format["bit_depth"]
    if type(streams) is not int or streams != 1 or type(bits) is not int or not 1 <= bits <= 24:
        _refuse("Only one RAW stream with integer ADC depth in [1,24] is supported", "raw_format")
    white = 2**bits - 1
    black = _number(raw_format["black_level_dn"], 0, white - 1, "raw_format.black_level_dn")
    for alias, expected in (("white_level_dn", white), ("black_level", black)):
        if alias in raw_format and _number(raw_format[alias], 0, white, alias) != expected:
            _refuse("Conflicting black or white level declarations", "raw_format." + alias)
    if np.min(frames) < 0 or np.max(frames) > white:
        _refuse("RAW pixel codes exceed the declared ADC domain; no hidden clipping", "raw")
    if not isinstance(geometry, Mapping):
        _refuse("Explicit native crop or representative geometry is required", "geometry")
    kind = geometry.get("kind")
    if kind == "native_sensor":
        _exact(geometry, {"kind", "full_sensor_shape", "crop_origin"}, "geometry")
        full = _integers(geometry["full_sensor_shape"], 2, "geometry.full_sensor_shape")
        origin = _integers(geometry["crop_origin"], 2, "geometry.crop_origin")
        if not all(
            2 <= total <= 1_000_000 and 0 <= start <= total - length
            for total, start, length in zip(full, origin, [rows, cols], strict=True)
        ):
            _refuse("Native crop must fit the full sensor without resizing", "geometry")
        normalized_geometry = {"kind": kind, "full_sensor_shape": full, "crop_origin": origin}
    elif kind == "representative_normalized_field_raster":
        _exact(geometry, {"kind", "raster_shape", "mapping"}, "geometry")
        shape = _integers(geometry["raster_shape"], 2, "geometry.raster_shape")
        if shape != [rows, cols] or geometry["mapping"] != "center_to_corner_radius_v1":
            _refuse("Representative geometry must match RAW exactly", "geometry")
        origin = [0, 0]
        normalized_geometry = {"kind": kind, "raster_shape": shape, "mapping": geometry["mapping"]}
    else:
        _refuse("Unsupported geometry kind", "geometry")
    pattern = np.array([[1, 2], [2, 3]])
    pattern = np.roll(pattern, (-origin[0] % 2, -origin[1] % 2), axis=(0, 1))
    normalized_format = {k: copy.deepcopy(raw_format[k]) for k in sorted(required)}
    normalized_format.update(black_level_dn=black, white_level_dn=white)
    return frames, normalized_format, normalized_geometry, pattern


def _backend() -> dict[str, Any]:
    import scipy

    import pyisetcam.ip
    import pyisetcam.utils

    try:
        version = importlib.metadata.version("pyisetcam")
    except importlib.metadata.PackageNotFoundError:
        version = "unpackaged_checkout"
    return {
        "id": IMPLEMENTATION,
        "precision": "IEEE754_float64",
        "numpy_version": np.__version__,
        "scipy_version": scipy.__version__,
        "pyisetcam_version": version,
        "demosaic": "pyisetcam.ip._ie_bilinear",
        "srgb_transfer": "pyisetcam.utils.linear_to_srgb",
        "source_sha256": {
            name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for name, path in {
                "camera_platform/isp.py": __file__,
                "pyisetcam/ip.py": pyisetcam.ip.__file__,
                "pyisetcam/utils.py": pyisetcam.utils.__file__,
            }.items()
        },
    }


def process_raw(raw, raw_format, profile, *, geometry) -> dict[str, Any]:
    """Process existing RAW codes without modifying them or acquiring new pixels.

    Arrays are always frame-first, except ``rgb`` which copies frame zero for
    existing preview consumers. Per-frame acquisition conditions belong to the
    caller's immutable capture wrapper and are never modified by this function.
    """
    from pyisetcam.ip import _ie_bilinear
    from pyisetcam.utils import linear_to_srgb

    selected = validate_profile(profile)
    frames, fmt, geom, pattern = _input(raw, raw_format, geometry)
    before_hash = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    count, rows, cols = frames.shape
    black = fmt["black_level_dn"]
    white = fmt["white_level_dn"]
    blc = (frames.astype(np.float64) - black) / (white - black)
    demosaiced = np.empty((count, rows, cols, 3), dtype=np.float64)
    mask = np.tile(pattern, (rows // 2, cols // 2))
    for index in range(count):
        planes = np.zeros((rows, cols, 3), dtype=np.float64)
        for channel in range(3):
            selected_pixels = mask == channel + 1
            planes[..., channel][selected_pixels] = blc[index][selected_pixels]
        demosaiced[index] = _ie_bilinear(planes, pattern)
    balanced = demosaiced * np.asarray(selected["white_balance"]["gains_rgb"])
    matrix = np.asarray(selected["ccm"]["matrix_rgb"])
    corrected = balanced @ matrix.T
    clipped = np.clip(corrected, 0.0, 1.0)
    output = linear_to_srgb(clipped) if selected["tone"]["curve"] == "srgb" else clipped
    arrays = {
        "black_subtracted_normalized_frames": blc,
        "demosaiced_rgb_frames": demosaiced,
        "white_balanced_rgb_frames": balanced,
        "ccm_rgb_frames": corrected,
        "rgb_frames": output,
        "rgb": output[0].copy(),
    }
    if not all(np.isfinite(value).all() for value in arrays.values()):
        _refuse("ISP produced a nonfinite intermediate", "result", "NUMERICAL_FAILURE")
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before_hash:
        _refuse("RAW input changed during processing", "raw", "INTEGRITY_FAILURE")
    units = {name: "1" for name in arrays}
    summary = {
        "schema_version": "camera_isp_replay_result_v1",
        "implementation": _backend(),
        "profile": selected,
        "profile_sha256": _digest(selected),
        "raw_format": fmt,
        "geometry": geom,
        "input_shape": list(raw.shape),
        "input_dtype": raw.dtype.str,
        "input_array_bytes_sha256": before_hash,
        "input_hash_scope": "C-order array data bytes; original container bytes retained by caller",
        "local_cfa_rgb_indices": (pattern - 1).tolist(),
        "frames": count,
        "output_size": [rows, cols],
        "array_units": units,
        "normalization": "(raw_dn - black_level_dn) / (2**bit_depth - 1 - black_level_dn)",
        "black_subtractions": 1,
        "frame_max_normalization": False,
        "linear_negative_policy": "preserved through BLC, demosaic, fixed WB and CCM",
        "tone_input_clipping": [0.0, 1.0],
        "frame_diagnostics": [
            {
                "frame_index": index,
                "raw_zero_code_count": int(np.count_nonzero(frames[index] == 0)),
                "raw_white_code_count": int(np.count_nonzero(frames[index] == white)),
                "below_black_sample_count": int(np.count_nonzero(blc[index] < 0)),
                "tone_low_clipped_channel_samples": int(np.count_nonzero(corrected[index] < 0)),
                "tone_high_clipped_channel_samples": int(np.count_nonzero(corrected[index] > 1)),
            }
            for index in range(count)
        ],
        "rgb_meaning": "fixed matrix-transformed sensor RGB; colorimetric accuracy not established",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "limitations": [
            "Software float64 reference, not a fixed-point or commercial hardware ISP equivalent.",
            "sRGB denotes only the transfer curve; no calibrated primaries or illuminant "
            "are inferred.",
            "Bilinear demosaic reflects the local crop boundary; a crop can differ from "
            "full-frame processing.",
            "No automatic exposure/WB, LSC, denoising, sharpening, HDR, or temporal state "
            "is applied.",
            "Reprocessing preserves acquired pixels; changing tuning is not sensor or "
            "exposure recapture.",
            "ADC endpoint counts cannot establish hardware linearity or rule out hidden "
            "full-well clipping.",
        ],
    }
    return {"summary": summary, "arrays": arrays}
