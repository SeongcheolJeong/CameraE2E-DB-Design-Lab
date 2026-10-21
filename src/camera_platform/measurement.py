"""Common, explicit RAW ROI observation for measured and simulated captures.

No RAW decoding, bit scaling, resizing, demosaic, or absolute-uncertainty
inference is performed here. Acquisition provenance is declared by the caller;
ADC-code checks cannot establish hardware linearity or detect hidden well clip.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any

import numpy as np

METHOD = "raw_roi_observation_v1"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_SPATIAL_PIXELS = 4_194_304
MAX_FRAMES = 256
MINIMUM_CFA_PIXELS = 16
CHANNELS = ["R", "G", "B"]


class ObservationError(ValueError):
    """A refusal retaining structured reasons instead of fabricating a metric."""

    def __init__(self, code: str, message: str, field: str = "observation", **details: Any):
        self.issues = [
            {"code": code, "message": message, "field": field, "severity": "error", **details}
        ]
        super().__init__(message)


def _refuse(message: str, field: str, code: str = "INVALID_OBSERVATION", **details: Any):
    raise ObservationError(code, message, field, **details)


def _integers(value: Any, count: int, field: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        _refuse(f"{field} requires {count} integers", field)
    if not all(
        isinstance(x, (int, np.integer)) and not isinstance(x, (bool, np.bool_)) for x in value
    ):
        _refuse(f"{field} requires integers without implicit rounding", field)
    return [int(x) for x in value]


def _number(value: Any, field: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        _refuse(f"{field} requires a finite number", field)
    result = float(value)
    if not np.isfinite(result) or not low <= result <= high:
        _refuse(f"{field} must be in [{low}, {high}]", field)
    return result


def _hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _shape(raw_shape: Any) -> list[int]:
    shape = list(raw_shape) if isinstance(raw_shape, (tuple, list)) else raw_shape
    if isinstance(shape, list) and len(shape) == 3:
        count, rows, cols = _integers(shape, 3, "raw_shape")
        if not 1 <= count <= MAX_FRAMES:
            _refuse("Frame count must be in [1,256]", "raw_shape")
    else:
        rows, cols = _integers(shape, 2, "raw_shape")
    if rows < 2 or cols < 2 or rows % 2 or cols % 2 or rows * cols > MAX_SPATIAL_PIXELS:
        _refuse("RAW shape requires even sides and at most 4194304 spatial pixels", "raw_shape")
    return [rows, cols]


def _layout(specification: Mapping[str, Any], raw_shape: Any) -> dict[str, Any]:
    if not isinstance(specification, Mapping):
        _refuse("Specification must be a mapping", "specification")
    rows, cols = _shape(raw_shape)
    raw = specification.get("raw_format")
    if not isinstance(raw, Mapping):
        _refuse("An explicit RAW format is required", "raw_format")
    allowed_raw_fields = {
        "cfa",
        "bit_depth",
        "black_level_dn",
        "response_type",
        "packing",
        "layout",
        "hdr_streams",
        "black_level",
        "white_level_dn",
    }
    if set(raw) - allowed_raw_fields:
        _refuse("Unsupported RAW preprocessing or format fields", "raw_format")
    if raw.get("cfa") != "RGGB" or raw.get("response_type") != "linear":
        _refuse("Only explicitly linear RGGB captures are supported", "raw_format")
    if raw.get("packing", "unpacked") != "unpacked" or raw.get("layout", "bayer") != "bayer":
        _refuse("Decode RAW packing explicitly before observation", "raw_format")
    if raw.get("hdr_streams", 1) != 1:
        _refuse("Only one linear RAW stream is supported", "raw_format.hdr_streams")
    bits = raw.get("bit_depth")
    if not isinstance(bits, int) or isinstance(bits, bool) or not 1 <= bits <= 24:
        _refuse("bit_depth must be an integer in [1,24]", "raw_format.bit_depth")
    white = 2**bits - 1
    black = _number(raw.get("black_level_dn"), "raw_format.black_level_dn", 0, white - 1)
    if "black_level" in raw and raw["black_level"] != black:
        _refuse("Conflicting black level declarations", "raw_format.black_level")
    if "white_level_dn" in raw and raw["white_level_dn"] != white:
        _refuse("white_level_dn must agree with the declared ADC bit depth", "raw_format")
    geometry = specification.get("geometry")
    if not isinstance(geometry, Mapping):
        _refuse("Explicit native crop or representative raster geometry is required", "geometry")
    kind = geometry.get("kind")
    if kind == "native_sensor":
        full_rows, full_cols = _integers(geometry.get("full_sensor_shape"), 2, "full_sensor_shape")
        origin_row, origin_col = _integers(geometry.get("crop_origin"), 2, "crop_origin")
        if not (
            2 <= full_rows <= 1_000_000
            and 2 <= full_cols <= 1_000_000
            and 0 <= origin_row <= full_rows - rows
            and 0 <= origin_col <= full_cols - cols
        ):
            _refuse("Native crop must lie inside full_sensor_shape without resizing", "geometry")
        normalized_geometry = {
            "kind": kind,
            "full_sensor_shape": [full_rows, full_cols],
            "crop_origin": [origin_row, origin_col],
        }
    elif kind == "representative_normalized_field_raster":
        if _integers(geometry.get("raster_shape"), 2, "raster_shape") != [rows, cols]:
            _refuse("Representative raster shape must exactly match RAW; no resize", "geometry")
        if geometry.get("mapping") != "center_to_corner_radius_v1":
            _refuse("Representative raster requires center_to_corner_radius_v1 mapping", "geometry")
        full_rows, full_cols, origin_row, origin_col = rows, cols, 0, 0
        normalized_geometry = {
            "kind": kind,
            "raster_shape": [rows, cols],
            "mapping": "center_to_corner_radius_v1",
        }
    else:
        _refuse("Unknown geometry kind; native and representative rasters are distinct", "geometry")
    if set(geometry) != set(normalized_geometry):
        _refuse("Unsupported geometry transform or undeclared geometry field", "geometry")
    rois = specification.get("rois")
    if not isinstance(rois, list) or not 1 <= len(rois) <= 32:
        _refuse("Declare 1..32 explicit ROI boxes", "rois")
    normalized_rois = []
    for index, roi in enumerate(rois):
        path = f"rois[{index}]"
        if not isinstance(roi, Mapping) or set(roi) != {"id", "field_radius", "box"}:
            _refuse("Each ROI requires id, field_radius, box only", path)
        identity = roi["id"]
        if not isinstance(identity, str) or not identity or len(identity) > 100:
            _refuse("ROI id must be a nonempty string of at most 100 characters", path)
        radius = _number(roi["field_radius"], path + ".field_radius", 0, 1)
        row, col, height, width = _integers(roi["box"], 4, path + ".box")
        if not (
            height > 0 and width > 0 and 0 <= row <= rows - height and 0 <= col <= cols - width
        ):
            _refuse("ROI [row,col,height,width] must lie inside the RAW image", path)
        normalized_rois.append(
            {"id": identity, "field_radius": radius, "box": [row, col, height, width]}
        )
    if len({roi["id"] for roi in normalized_rois}) != len(rois):
        _refuse("ROI identities must be unique", "rois")
    if any(
        left["field_radius"] >= right["field_radius"]
        for left, right in zip(normalized_rois, normalized_rois[1:], strict=False)
    ):
        _refuse("Nominal ROI field_radius labels must be unique and strictly increasing", "rois")
    return {
        "shape": [rows, cols],
        "raw_format": {
            "cfa": "RGGB",
            "bit_depth": bits,
            "black_level_dn": black,
            "response_type": "linear",
            "packing": "unpacked",
            "layout": "bayer",
            "hdr_streams": 1,
        },
        "white": white,
        "black": black,
        "geometry": normalized_geometry,
        "rois": normalized_rois,
        "full_shape": [full_rows, full_cols],
        "origin": [origin_row, origin_col],
    }


def _roi_geometry(layout: dict[str, Any], roi: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    row, col, height, width = roi["box"]
    full_rows, full_cols = layout["full_shape"]
    yy = np.arange(row + layout["origin"][0], row + layout["origin"][0] + height)
    xx = np.arange(col + layout["origin"][1], col + layout["origin"][1] + width)
    # Full-sensor RGGB phase is retained even when a native crop starts at odd coordinates.
    cfa = np.array([[0, 1], [1, 2]])[yy[:, None] % 2, xx[None, :] % 2]
    counts = np.bincount(cfa.ravel(), minlength=3)
    if np.any(counts < MINIMUM_CFA_PIXELS):
        _refuse(
            "Each ROI needs at least 16 pixels of every CFA channel",
            "rois",
            "INSUFFICIENT_CFA_SAMPLES",
            roi_id=roi["id"],
            channel_counts=counts.tolist(),
        )
    radius = np.hypot(yy[:, None] - (full_rows - 1) / 2, xx[None, :] - (full_cols - 1) / 2)
    radius /= np.hypot((full_rows - 1) / 2, (full_cols - 1) / 2)
    return cfa, radius


def observation_sampling_weights(
    specification: Mapping[str, Any], raw_shape: Any, field_radius: Any
) -> dict[str, Any]:
    """Average linear field-basis weights at the exact native CFA sample positions.

    No relative illumination or other physical response is applied. The returned
    weights can act on expected per-field channel signals (electrons or DN).
    """
    layout = _layout(specification, raw_shape)
    try:
        basis = np.asarray(field_radius, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ObservationError("INVALID_FIELD_BASIS", "Field basis must be numerical") from exc
    if (
        basis.ndim != 1
        or not 2 <= len(basis) <= 256
        or not np.isfinite(basis).all()
        or not np.all(np.diff(basis) > 0)
        or basis[0] < 0
        or basis[-1] > 1
    ):
        _refuse("Field basis requires 2..256 finite increasing samples in [0,1]", "field_radius")
    weights = []
    for roi in layout["rois"]:
        cfa, radius = _roi_geometry(layout, roi)
        if radius.min() < basis[0] or radius.max() > basis[-1]:
            _refuse(
                "ROI samples exceed the supplied field-response domain",
                "field_radius",
                "OUT_OF_DOMAIN",
                roi_id=roi["id"],
            )
        channel_weights = []
        for channel in range(3):
            sample = radius[cfa == channel]
            right = np.clip(np.searchsorted(basis, sample, side="right"), 1, len(basis) - 1)
            left = right - 1
            fraction = (sample - basis[left]) / (basis[right] - basis[left])
            values = (
                np.bincount(left, weights=1 - fraction, minlength=len(basis))
                + np.bincount(right, weights=fraction, minlength=len(basis))
            ) / sample.size
            channel_weights.append(values.tolist())
        weights.append(channel_weights)
    result = {
        "mode": "raw_roi_channel_mean_ratio",
        "field_radius": basis.tolist(),
        "roi_field_radius": [roi["field_radius"] for roi in layout["rois"]],
        "roi_ids": [roi["id"] for roi in layout["rois"]],
        "channel_field_weights": weights,
    }
    return {**result, "content_hash": _hash(result)}


def observe_field_response(
    field_channel_values: Any, sampling: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply the shared ROI operator to an expected [field,R/G/B] response.

    The caller must supply the same black/dark correction and clipping stage as
    the RAW observation. In particular, black-only observations retain mean dark.
    """
    try:
        response = np.asarray(field_channel_values, dtype=float)
        weights = np.asarray(sampling["channel_field_weights"], dtype=float)
    except (TypeError, ValueError, KeyError) as exc:
        raise ObservationError(
            "INVALID_FIELD_RESPONSE", "Invalid response or sampling weights"
        ) from exc
    if (
        sampling.get("mode") != "raw_roi_channel_mean_ratio"
        or response.ndim != 2
        or response.shape[1] != 3
        or weights.ndim != 3
        or weights.shape[1:] != (3, response.shape[0])
        or not np.isfinite(response).all()
        or not np.isfinite(weights).all()
        or np.any(response < 0)
        or np.any(weights < 0)
        or not np.allclose(weights.sum(axis=-1), 1, rtol=0, atol=1e-9)
    ):
        _refuse(
            "Expected nonnegative [field,3] response and normalized [ROI,3,field] weights",
            "field_channel_values",
        )
    means = np.einsum("rcf,fc->rc", weights, response)
    ratios = _ratios(means)
    return {
        "channel_means": means.tolist(),
        "values": ratios.ravel().tolist(),
        "axes": _axes(sampling["roi_field_radius"]),
    }


def _axes(fields: list[float]) -> list[dict[str, Any]]:
    return [
        {"name": "field_radius", "unit": "1", "values": fields},
        {"name": "channel_ratio", "unit": "1", "values": ["R/G", "B/G"]},
    ]


def observation_method(
    specification: Mapping[str, Any], raw_shape: Any, *, dark_present: bool
) -> dict[str, Any]:
    """Normalize/check the shared operator without reading capture pixel values.

    This does not verify conditions, provenance, clipping, or supplied bounds;
    analyze_raw_sequence performs those additional acquisition checks.
    """
    if not isinstance(dark_present, bool):
        _refuse("dark_present must be boolean", "dark_present")
    layout = _layout(specification, raw_shape)
    for roi in layout["rois"]:
        _roi_geometry(layout, roi)
    policy = {
        "minimum_cfa_pixels": MINIMUM_CFA_PIXELS,
        "dark_correction": "mean_dark_frame" if dark_present else "black_level_only",
        "saturation_policy": "reject_adc_endpoints_in_rois",
    }
    for key, value in policy.items():
        if key in specification and specification[key] != value:
            _refuse("Declared observation policy disagrees with the actual operator", key)
    frozen = {
        "raw_format": layout["raw_format"],
        "geometry": layout["geometry"],
        "rois": layout["rois"],
        **policy,
    }
    return {
        "id": METHOD,
        "specification": frozen,
        "sampling_hash": _hash(frozen),
        "dark_correction": policy["dark_correction"],
    }


def _ratios(means: np.ndarray) -> np.ndarray:
    if not np.isfinite(means).all() or np.any(means <= 0):
        _refuse(
            "Every corrected ROI channel mean must be positive; no clipping to fabricate ratios",
            "channel_means_dn",
            "NONPOSITIVE_CHANNEL_SIGNAL",
            channel_means=means.tolist(),
        )
    return means[:, [0, 2]] / means[:, 1:2]


def _conditions(specification: Mapping[str, Any], dark: bool) -> dict[str, Any]:
    conditions = specification.get("conditions")
    if not isinstance(conditions, Mapping):
        _refuse("Actual capture conditions must be declared", "conditions")
    expected = {
        "exposure_s": ("s", 1e-12, 3600),
        "temperature_c": ("degC", -273.15, 1000),
        "gain": ("1", 1e-12, 1e6),
    }
    for key, (unit, low, high) in expected.items():
        value = conditions.get(key)
        if not isinstance(value, Mapping) or value.get("unit") != unit:
            _refuse(f"Actual {key} must use {unit}", "conditions." + key)
        _number(value.get("value"), "conditions." + key, low, high)
    light = conditions.get("light_id")
    if (
        not isinstance(light, Mapping)
        or light.get("unit") != "1"
        or not isinstance(light.get("value"), str)
        or not light["value"]
    ):
        _refuse("Actual illumination identity must be declared as light_id", "conditions.light_id")
    if dark:
        dark_conditions = specification.get("dark_conditions")
        if not isinstance(dark_conditions, Mapping):
            _refuse(
                "Dark frames require explicit matched exposure, gain and temperature",
                "dark_conditions",
            )
        for key in expected:
            if dark_conditions.get(key) != conditions[key]:
                _refuse(
                    f"Dark {key} must exactly match light capture; no implicit scaling",
                    "dark_conditions." + key,
                    "DARK_CONDITION_MISMATCH",
                )
    return copy.deepcopy(dict(conditions))


def _uncertainty(specification: Mapping[str, Any], count: int) -> dict[str, Any]:
    value = specification.get("uncertainty", {"kind": "unknown"})
    if not isinstance(value, Mapping):
        _refuse("uncertainty must be a declared object", "uncertainty")
    if value.get("kind") == "unknown":
        return {"kind": "unknown"}
    if value.get("kind") != "absolute_bound" or value.get("unit") != "1":
        _refuse(
            "Only an externally established absolute_bound in unit 1 may be supplied", "uncertainty"
        )
    for key in ("method_and_version", "source_uri"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            _refuse("Known bounds require an external method/version and source URI", "uncertainty")
    values = value.get("values")
    if not isinstance(values, list) or len(values) != count:
        _refuse(
            "Uncertainty bounds must align with every flattened ROI ratio", "uncertainty.values"
        )
    bounds = [_number(x, "uncertainty.values", 0, 1e12) for x in values]
    return {**copy.deepcopy(dict(value)), "values": bounds}


def _raw_arrays(raw_frames: Any, dark_frames: Any) -> tuple[np.ndarray, np.ndarray | None]:
    arrays = [raw_frames] + ([] if dark_frames is None else [dark_frames])
    total_bytes = 0
    for index, array in enumerate(arrays):
        field = "raw_frames" if index == 0 else "dark_frames"
        if not isinstance(array, np.ndarray) or array.ndim != 3:
            _refuse("RAW arrays require [frames,rows,columns] NumPy arrays", field)
        if not np.issubdtype(array.dtype, np.integer):
            _refuse(
                "RAW arrays must have integer dtype; float casts/scaling are not implicit", field
            )
        _shape(array.shape)
        total_bytes += array.nbytes
    if total_bytes > MAX_INPUT_BYTES:
        _refuse("Combined RAW and dark input exceeds 64 MiB", "raw_frames", "RESOURCE_LIMIT")
    if dark_frames is not None and raw_frames.shape[1:] != dark_frames.shape[1:]:
        _refuse(
            "Dark frames must have exactly the same crop shape",
            "dark_frames",
            "DARK_SHAPE_MISMATCH",
        )
    return raw_frames, dark_frames


def _code_stats(array: np.ndarray, white: int) -> dict[str, int]:
    return {
        "zero_adc_samples": int(np.count_nonzero(array == 0)),
        "white_adc_samples": int(np.count_nonzero(array == white)),
        "total_samples": int(array.size),
        "minimum_dn": int(array.min()),
        "maximum_dn": int(array.max()),
    }


def analyze_raw_sequence(
    raw_frames: np.ndarray, dark_frames: np.ndarray | None, specification: Mapping[str, Any]
) -> dict[str, Any]:
    """Measure ratio-of-ROI-channel-means without inventing measurement accuracy.

    A matched dark stack removes its temporal mean (including the black pedestal).
    Otherwise only the declared black level is removed; dark signal remains.
    ADC endpoint codes in observed ROIs are refused. At least three frames per
    available stack permit an empirical delta-method standard-error diagnostic,
    which is never promoted to an absolute interval or physical validation.
    """
    raw, dark = _raw_arrays(raw_frames, dark_frames)
    layout = _layout(specification, raw.shape)
    method = observation_method(specification, raw.shape, dark_present=dark is not None)
    frozen = method["specification"]
    conditions = _conditions(specification, dark is not None)
    source = specification.get("source_kind")
    if not isinstance(source, str) or source not in {
        "measured",
        "simulated",
        "estimated",
        "vendor",
    }:
        _refuse("source_kind must explicitly identify the capture provenance", "source_kind")
    for field, array in (("raw_frames", raw), ("dark_frames", dark)):
        if array is not None and (array.min() < 0 or array.max() > layout["white"]):
            _refuse("RAW codes exceed declared bit depth; no implicit rescaling", field)
    means, roi_records, standard_errors = [], [], []
    raw_count = dark_count = 0
    for roi in layout["rois"]:
        row, col, height, width = roi["box"]
        cfa, radius = _roi_geometry(layout, roi)
        light_roi = raw[:, row : row + height, col : col + width]
        dark_roi = None if dark is None else dark[:, row : row + height, col : col + width]
        light_stats = _code_stats(light_roi, layout["white"])
        dark_stats = None if dark_roi is None else _code_stats(dark_roi, layout["white"])
        for name, stats in (("raw", light_stats), ("dark", dark_stats)):
            if stats and (stats["zero_adc_samples"] or stats["white_adc_samples"]):
                _refuse(
                    "Observed ROI includes ADC-clipped samples; linear ratio is not accepted",
                    "rois",
                    "ADC_CLIPPING",
                    roi_id=roi["id"],
                    capture=name,
                    **stats,
                )
        light_channels = np.stack(
            [light_roi[:, cfa == channel].mean(axis=1, dtype=float) for channel in range(3)], axis=1
        )
        dark_channels = (
            None
            if dark_roi is None
            else np.stack(
                [dark_roi[:, cfa == channel].mean(axis=1, dtype=float) for channel in range(3)],
                axis=1,
            )
        )
        dark_mean = (
            np.full(3, layout["black"]) if dark_channels is None else dark_channels.mean(axis=0)
        )
        mean = light_channels.mean(axis=0) - dark_mean
        _ratios(mean[None, :])
        means.append(mean)
        counts = np.bincount(cfa.ravel(), minlength=3).tolist()
        roi_records.append(
            {
                **roi,
                "channel_counts": dict(zip(CHANNELS, counts, strict=True)),
                "sampled_radius": {
                    "min": float(radius.min()),
                    "mean": float(radius.mean()),
                    "max": float(radius.max()),
                },
                "raw_code_statistics": light_stats,
                "dark_code_statistics": dark_stats,
                "light_channel_means_dn": light_channels.mean(axis=0).tolist(),
                "subtracted_channel_means_dn": dark_mean.tolist(),
            }
        )
        raw_count += light_stats["total_samples"]
        dark_count += 0 if dark_stats is None else dark_stats["total_samples"]
        if len(raw) >= 3 and (dark is None or len(dark) >= 3):
            covariance = np.cov(light_channels, rowvar=False, ddof=1) / len(raw)
            if dark_channels is not None:
                covariance += np.cov(dark_channels, rowvar=False, ddof=1) / len(dark)
            red, green, blue = mean
            gradients = np.array(
                [[1 / green, -red / green**2, 0], [0, -blue / green**2, 1 / green]]
            )
            variance = np.einsum("ic,cd,id->i", gradients, covariance, gradients)
            standard_errors.extend(np.sqrt(np.maximum(variance, 0)).tolist())
        else:
            standard_errors.extend([None, None])
    channel_means = np.asarray(means)
    values = _ratios(channel_means).ravel().tolist()
    effects = specification.get("included_effects")
    if (
        not isinstance(effects, list)
        or not effects
        or not all(isinstance(effect, str) and effect for effect in effects)
        or len(effects) != len(set(effects))
    ):
        _refuse("Declare unique physical included_effects for the observation", "included_effects")
    measurement = {
        "metric_id": "raw.channel_ratio",
        "unit": "1",
        "values": values,
        "axes": _axes([roi["field_radius"] for roi in layout["rois"]]),
        "reference_plane": "sensor_raw",
        "source_kind": source,
        "conditions": conditions,
        "included_effects": copy.deepcopy(effects),
        "method_and_version": METHOD,
        "observation_method": method,
        "sample_geometry": {
            "shape": layout["shape"],
            "geometry": layout["geometry"],
            "rois": roi_records,
            "dark_correction": frozen["dark_correction"],
        },
        "channel_means_dn": channel_means.tolist(),
        "uncertainty": _uncertainty(specification, len(values)),
    }
    for key in (
        "source_uri",
        "physical_instance",
        "instrument",
        "dataset_ids",
        "calibration_dataset_ids",
        "license",
        "parents",
    ):
        if key in specification:
            measurement[key] = copy.deepcopy(specification[key])
    diagnostics = {
        "source": METHOD,
        "raw_frames": len(raw),
        "dark_frames": 0 if dark is None else len(dark),
        "input_bytes": raw.nbytes + (0 if dark is None else dark.nbytes),
        "saturation_diagnostics": {
            "source": METHOD,
            "linear_unsaturated": True,
            "saturated_samples": 0,
            "zero_adc_samples": 0,
            "total_samples": raw_count + dark_count,
            "raw_roi_samples": raw_count,
            "dark_roi_samples": dark_count,
            "scope": "selected_ROI_ADC_codes; overlapping_ROIs_count_samples_again",
            "physical_well_clipping_verified": False,
        },
        "full_input_raw_code_statistics": _code_stats(raw, layout["white"]),
        "full_input_dark_code_statistics": None
        if dark is None
        else _code_stats(dark, layout["white"]),
        "statistical_uncertainty": {
            "kind": "empirical_delta_method_standard_error",
            "unit": "1",
            "values": standard_errors,
            "minimum_frames_per_stack": 3,
            "is_absolute_bound": False,
            "assumptions": [
                "Independent stationary light and dark captures",
                "First-order ratio linearization around positive channel means",
                "Temporal sampling only; no calibration, drift, model or systematic error",
            ],
        },
        "limitations": [
            "Linearity is a declared acquisition contract, not established from these arrays.",
            "ADC-code checks cannot detect physical well clipping below the ADC ceiling.",
            "Nominal field labels do not turn ROI averages into point-response samples.",
            "CFA Gr/Gb samples are pooled as G; unequal green-site responses are not separated.",
            "Empirical standard errors are not confidence intervals or absolute bounds.",
            "Black-only correction leaves mean dark signal; match this observation in simulation.",
        ],
    }
    return {"measurement": measurement, "diagnostics": diagnostics}
