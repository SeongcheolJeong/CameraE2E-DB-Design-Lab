"""Code-domain spatial/causal ISP for history-bearing RAW.

This DSP state is not photodiode state. No charge inversion, noise model,
exposure division, truth correction or physical color interpretation is used.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy

import numpy as np

from . import isp
from .isp_v2 import _neighbors
from .registry import digest

SCHEMA = "camera_retained_code_isp_profile_v1"
IMPLEMENTATION = "camera_platform_retained_code_isp_v1"
GRAPH = [
    "black_relative_dn",
    "same_site_spatial_filter",
    "causal_temporal_filter",
    "demosaic",
    "fixed_color",
    "bounded_code_detail",
    "display_transfer",
]
MAX_FRAMES = 16
MAX_FRAME_PIXELS = 1_048_576
MAX_OUTPUT_ARRAY_BYTES = 128 * 1024**2
MAX_WORKING_ARRAY_BYTES = 1024 * 1024**2
RESET_REASONS = {
    "first": 1,
    "settings": 2,
    "pd_clear": 4,
    "interval": 8,
    "current_code_rail": 16,
    "previous_code_rail": 32,
}


def default_profile(common=None):
    base = isp.validate_profile(common) if common is not None else isp.default_profile()
    return {
        "schema": SCHEMA,
        "graph": GRAPH.copy(),
        "precision": "float64",
        "coordinate": "signed_final_DN_minus_declared_black",
        "normalization": "declared_white_minus_black",
        "spatial_filter": {"mode": "bypass", "strength": 0.5, "range_sigma_dn": 16.0},
        "temporal_filter": {
            "mode": "bypass",
            "previous_weight": 0.5,
            "threshold_dn": 32.0,
            "max_inter_transfer_interval_ns": 100_000_000,
            "reset_on_pd_clear": True,
            "reset_on_settings_change": True,
            "reset_on_code_rail": True,
        },
        "demosaic": deepcopy(base["demosaic"]),
        "white_balance": deepcopy(base["white_balance"]),
        "ccm": deepcopy(base["ccm"]),
        "detail": {"mode": "bypass", "amount": 0.25, "threshold": 0.01, "max_delta": 0.1},
        "tone": deepcopy(base["tone"]),
        "unsupported_effects": [
            "photocharge_inversion",
            "dark_subtraction",
            "exposure_division",
            "lag_correction",
            "hdr_merge",
            "auto_controls",
            "physical_color",
        ],
    }


def validate_profile(profile):
    expected = default_profile()
    isp._exact(profile, set(expected), "profile")
    for key in (
        "schema",
        "graph",
        "precision",
        "coordinate",
        "normalization",
        "unsupported_effects",
    ):
        if profile[key] != expected[key]:
            isp._refuse("Unsupported code ISP contract: " + key, "profile." + key)
    common = isp.default_profile()
    for key in ("demosaic", "white_balance", "ccm", "tone"):
        common[key] = deepcopy(profile[key])
    common = isp.validate_profile(common)
    result = deepcopy(expected)
    for key in ("demosaic", "white_balance", "ccm", "tone"):
        result[key] = common[key]
    for stage, mode, bounds in (
        (
            "spatial_filter",
            "same_site_bilateral_code_v1",
            {"strength": (0, 1), "range_sigma_dn": (1e-6, 2**24)},
        ),
        (
            "temporal_filter",
            "causal_difference_gate_v1",
            {"previous_weight": (0, 0.95), "threshold_dn": (0, 2**24)},
        ),
        (
            "detail",
            "bounded_code_detail_v1",
            {"amount": (0, 1), "threshold": (0, 16), "max_delta": (0, 1)},
        ),
    ):
        config = isp._exact(profile[stage], set(expected[stage]), "profile." + stage)
        if config["mode"] not in ("bypass", mode):
            isp._refuse("Unsupported code filter mode", "profile." + stage)
        result[stage]["mode"] = config["mode"]
        for key, bound in bounds.items():
            result[stage][key] = isp._number(config[key], *bound, "profile." + stage + "." + key)
    temporal = profile["temporal_filter"]
    gap = temporal["max_inter_transfer_interval_ns"]
    if type(gap) is not int or not 1 <= gap <= 2**62 - 1:
        isp._refuse(
            "Inter-transfer interval must be a bounded positive integer ns", "temporal_filter"
        )
    result["temporal_filter"]["max_inter_transfer_interval_ns"] = gap
    for key in ("reset_on_pd_clear", "reset_on_settings_change", "reset_on_code_rail"):
        if temporal[key] is not True:
            isp._refuse(
                "The initial code filter requires explicit reset policy", "temporal_filter." + key
            )
    return result


def bypass_profile(profile):
    result = validate_profile(profile)
    for key in ("spatial_filter", "temporal_filter", "detail"):
        result[key]["mode"] = "bypass"
    return result


def describe_capabilities():
    return {
        "implementation_id": IMPLEMENTATION,
        "profile_schema": SCHEMA,
        "graph": GRAPH.copy(),
        "precision": "float64",
        "supports": [
            "original_selected_readout_codes",
            "all_original_frames",
            "independent_RGGB_site_spatial_filter",
            "causal_row_timed_code_filter",
            "explicit_algorithm_reset_diagnostics",
            "signed_code_intermediates",
            "fixed_color_display_transform",
            "bounded_code_detail",
            "fixed_native_receiver_roi_with_origin_aware_CFA",
        ],
        "unsupported": default_profile()["unsupported_effects"]
        + [
            "physical_noise_reduction_claim",
            "physical_full_well_mask_from_final_codes",
            "fixed_point_hardware_equivalence",
            "changing_receiver_roi_within_one_sequence",
        ],
        "limits": {
            "max_frames": MAX_FRAMES,
            "max_frame_pixels": MAX_FRAME_PIXELS,
            "max_output_array_bytes": MAX_OUTPUT_ARRAY_BYTES,
            "max_working_array_bytes": MAX_WORKING_ARRAY_BYTES,
        },
        "physical_validation": "not_established",
    }


def _frame_events(events, records, shape, geometry):
    from .retained_charge_runtime import EVENT_CONTRACT, TRAP_EVENT_CONTRACT
    from .timing import MAX_EXPOSURE_NS, MAX_TIME_NS

    count, rows, cols = shape
    contracts = {
        "camera_retained_row_events_v1": EVENT_CONTRACT,
        "camera_retained_row_events_v2": TRAP_EVENT_CONTRACT,
        "camera_retained_row_events_v3": EVENT_CONTRACT,
        "camera_retained_row_events_v4": TRAP_EVENT_CONTRACT,
    }
    if (
        not isinstance(events, Mapping)
        or not isinstance(events.get("schema"), str)
        or events["schema"] not in contracts
    ):
        isp._refuse("Original retained row event schedule is required", "events")
    full, origin = geometry["full_sensor_shape"], geometry["crop_origin"]
    cropped = origin != [0, 0] or full != [rows, cols]
    expected_crop = {"crop_origin": origin, "shape": [rows, cols]} if cropped else None
    if (events.get("event_contract") != contracts[events["schema"]]
            or events.get("full_sensor_shape") != full
            or events.get("capture_crop") != expected_crop
            or ((events["schema"] in {"camera_retained_row_events_v3",
                                         "camera_retained_row_events_v4"}) != cropped)):
        isp._refuse("Row schedule must bind the original native receiver geometry", "events")
    if digest({k: v for k, v in events.items() if k != "schedule_sha256"}) != events.get(
        "schedule_sha256"
    ):
        isp._refuse("Row schedule digest differs", "events")
    if (
        not isinstance(records, list)
        or len(records) != count
        or len(events.get("frames", [])) != count
    ):
        isp._refuse("Every original frame and row event must be supplied in order", "frames")
    ids, transfers, signatures, clear = [], [], [], []
    earliest, latest = MAX_TIME_NS, -MAX_TIME_NS
    for record, entry in zip(records, events["frames"], strict=True):
        identity = record.get("frame_id")
        if (
            not isinstance(identity, str)
            or not identity
            or identity in ids
            or identity != entry.get("frame_id")
        ):
            isp._refuse("Frame IDs must match the unique ordered original schedule", "frames")
        ids.append(identity)
        applied = record.get("applied", {})
        duration = applied.get("exposure_ns")
        start = record.get("frame_start_ns")
        if (
            type(duration) is not int
            or not 1 <= duration <= MAX_EXPOSURE_NS
            or type(start) is not int
            or not -MAX_TIME_NS <= start <= MAX_TIME_NS
        ):
            isp._refuse("Recorded exposure and frame start require integer nanoseconds", "frames")
        gain = isp._number(applied.get("gain"), 1e-12, 1e12, "frames.gain")
        values = []
        for key in ("row_open_ns", "row_transfer_ns"):
            value = entry.get(key)
            if (
                not isinstance(value, list)
                or len(value) != rows
                or any(type(v) is not int or not -MAX_TIME_NS <= v <= MAX_TIME_NS for v in value)
            ):
                isp._refuse(
                    "Every row event must have an exact bounded integer timestamp", "events." + key
                )
            earliest, latest = min(earliest, start, *value), max(latest, start, *value)
            if latest - earliest > MAX_TIME_NS:
                isp._refuse("Relative time span exceeds exact supported integer range", "events")
            # The signed endpoint bounds make every subtraction safe in int64;
            # the additional sequence span bound preserves exact JSON durations.
            values.append(np.asarray(value, dtype=np.int64))
        opened, ended = values
        if np.any(ended - opened != duration) or np.any(opened < start):
            isp._refuse("Row event exposure differs from recorded control", "events")
        if transfers and np.any(ended <= transfers[-1]):
            isp._refuse("Row transfers must be strictly monotone across frames", "events")
        if type(entry.get("clear_before_exposure")) is not bool:
            isp._refuse("PD clear must be explicit", "events")
        signatures.append(
            (
                duration,
                gain,
                record.get("temperature_c"),
                tuple((opened - start).tolist()),
                tuple((ended - opened).tolist()),
            )
        )
        transfers.append(ended)
        clear.append(entry["clear_before_exposure"])
    times = np.stack(transfers)
    intervals = np.zeros(times.shape, dtype=np.int64)
    intervals[1:] = times[1:] - times[:-1]
    return {
        "ids": ids,
        "times": times,
        "intervals": intervals,
        "signatures": signatures,
        "clear": clear,
    }


def validate_inputs(raw, raw_format, profile, *, geometry, frame_records, events):
    selected = validate_profile(profile)
    if (
        not isinstance(raw, np.ndarray)
        or raw.ndim != 3
        or raw.dtype.kind not in "iu"
        or raw.dtype.itemsize > 4
    ):
        isp._refuse(
            "Selected code input requires original unpacked integer [frame,row,column] RAW", "raw"
        )
    count, rows, cols = raw.shape
    if (
        not 1 <= count <= MAX_FRAMES
        or min(rows, cols) < 2
        or rows % 2
        or cols % 2
        or max(rows, cols) > 4096
        or raw.size > MAX_FRAME_PIXELS
    ):
        isp._refuse("Selected code input exceeds frame/native pixel limits", "raw")
    expected_keys = {
        "cfa",
        "bit_depth",
        "black_level_dn",
        "response_type",
        "packing",
        "layout",
        "hdr_streams",
    }
    if not isinstance(raw_format, Mapping) or set(raw_format) != expected_keys:
        isp._refuse("The unmodified selected readout format is required", "raw_format")
    for key, value in (("cfa", "RGGB"), ("packing", "unpacked"), ("layout", "bayer")):
        if raw_format[key] != value:
            isp._refuse("Unsupported retained readout format", "raw_format." + key)
    if raw_format["response_type"] not in ("linear", "nonlinear_charge_voltage"):
        isp._refuse("Unknown code acquisition response", "raw_format")
    bits = raw_format["bit_depth"]
    if (
        type(bits) is not int
        or not 1 <= bits <= 24
        or type(raw_format["hdr_streams"]) is not int
        or raw_format["hdr_streams"] != 1
    ):
        isp._refuse(
            "One explicit selected readout with 1..24 bit integer codes is required", "raw_format"
        )
    white = 2**bits - 1
    black = isp._number(raw_format["black_level_dn"], 0, white - 1, "raw_format.black_level_dn")
    if raw.min() < 0 or raw.max() > white:
        isp._refuse("Stored RAW exceeds its declared code range", "raw")
    if not isinstance(geometry, Mapping) or set(geometry) != {
        "kind",
        "full_sensor_shape",
        "crop_origin",
    }:
        isp._refuse("Explicit full native geometry is required", "geometry")
    full = isp._integers(geometry["full_sensor_shape"], 2, "geometry.full_sensor_shape")
    origin = isp._integers(geometry["crop_origin"], 2, "geometry.crop_origin")
    if (geometry["kind"] != "native_sensor" or any(side < 2 for side in full)
            or any(start < 0 or start + side > maximum
                   for start, side, maximum in zip(origin, (rows, cols), full, strict=True))
            or full[0] * full[1] > 16_777_216):
        isp._refuse("Retained code ISP requires bounded native receiver geometry", "geometry")
    schedule = _frame_events(events, frame_records, raw.shape, {"full_sensor_shape": full,
                                                                  "crop_origin": origin})
    resources = {
        "output_array_bytes_bound": 256 * raw.size + 64 * rows * cols + 32 * count * rows,
        "working_array_bytes_bound": 1024 * raw.size + 512 * rows * cols + 64 * count * rows,
    }
    if (
        resources["output_array_bytes_bound"] > MAX_OUTPUT_ARRAY_BYTES
        or resources["working_array_bytes_bound"] > MAX_WORKING_ARRAY_BYTES
    ):
        isp._refuse("Retained code ISP array forecast exceeds memory/evidence bounds", "resources")
    return {
        "frames": raw,
        "format": deepcopy(dict(raw_format)),
        "geometry": deepcopy(dict(geometry)),
        "profile": selected,
        "resources": resources,
        "frame_count": count,
        "white_level_dn": white,
        "black_level_dn": black,
        "schedule": schedule,
    }


def _spatial(signal, rails, config):
    result = signal.copy()
    if config["mode"] == "bypass" or config["strength"] == 0:
        return result
    for row in range(2):
        for column in range(2):
            center = signal[:, row::2, column::2]
            allowed = ~rails[:, row::2, column::2]
            numerator, denominator = np.zeros(center.shape), np.zeros(center.shape)
            for (neighbor, spatial), (neighbor_allowed, _) in zip(
                _neighbors(center), _neighbors(allowed), strict=True
            ):
                weight = (
                    spatial
                    * np.exp(-0.5 * np.square((neighbor - center) / config["range_sigma_dn"]))
                    * neighbor_allowed
                )
                numerator += weight * neighbor
                denominator += weight
            average = np.divide(numerator, denominator, out=center.copy(), where=denominator > 0)
            result[:, row::2, column::2] = np.where(
                allowed, center + config["strength"] * (average - center), center
            )
    return result


def _temporal(signal, rails, schedule, config):
    output = signal.copy()
    previous = np.zeros(signal.shape)
    gate = np.zeros(signal.shape, dtype=bool)
    reason = np.zeros(signal.shape, dtype=np.uint8)
    for i in range(len(signal)):
        if i == 0:
            reason[i] |= RESET_REASONS["first"]
        else:
            previous[i] = output[i - 1]
            if schedule["signatures"][i] != schedule["signatures"][i - 1]:
                reason[i] |= RESET_REASONS["settings"]
            reason[i][schedule["intervals"][i] > config["max_inter_transfer_interval_ns"]] |= (
                RESET_REASONS["interval"]
            )
            reason[i][rails[i - 1]] |= RESET_REASONS["previous_code_rail"]
        if schedule["clear"][i]:
            reason[i] |= RESET_REASONS["pd_clear"]
        reason[i][rails[i]] |= RESET_REASONS["current_code_rail"]
        if config["mode"] != "bypass" and config["previous_weight"] > 0:
            gate[i] = (reason[i] == 0) & (np.abs(signal[i] - previous[i]) <= config["threshold_dn"])
            weight = config["previous_weight"]
            output[i] = np.where(
                gate[i], (1 - weight) * signal[i] + weight * previous[i], signal[i]
            )
    return output, previous, gate, reason


def process_raw(raw, raw_format, profile, *, geometry, frame_records, events):
    from pyisetcam.ip import _ie_bilinear
    from pyisetcam.utils import linear_to_srgb

    parsed = validate_inputs(
        raw, raw_format, profile, geometry=geometry, frame_records=frame_records, events=events
    )
    selected = parsed["profile"]
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    count, rows, cols = raw.shape
    white, black = parsed["white_level_dn"], parsed["black_level_dn"]
    signal = raw.astype(np.float64) - black
    rails = (raw == 0) | (raw == white)
    spatial = _spatial(signal, rails, selected["spatial_filter"])
    temporal, previous, gate, reasons = _temporal(
        spatial, rails, parsed["schedule"], selected["temporal_filter"]
    )
    origin = parsed["geometry"]["crop_origin"]
    native_sites = np.array([[0, 1], [2, 3]], dtype=np.uint8)
    native_sites = native_sites[np.ix_(
        (np.arange(2) + origin[0]) % 2, (np.arange(2) + origin[1]) % 2
    )]
    pattern = np.array([1, 2, 2, 3])[native_sites]
    rgb_index = np.tile(pattern - 1, (rows // 2, cols // 2))
    normalized = temporal / (white - black)
    demosaiced = np.empty((count, rows, cols, 3))
    for i in range(count):
        planes = np.zeros((rows, cols, 3))
        for channel in range(3):
            mask = rgb_index == channel
            planes[..., channel][mask] = normalized[i][mask]
        demosaiced[i] = _ie_bilinear(planes, pattern)
    balanced = demosaiced * np.asarray(selected["white_balance"]["gains_rgb"])
    transformed = balanced @ np.asarray(selected["ccm"]["matrix_rgb"]).T
    detail = selected["detail"]
    delta = np.zeros((count, rows, cols))
    if detail["mode"] != "bypass" and detail["amount"] > 0 and detail["max_delta"] > 0:
        guide = transformed.mean(axis=-1)
        blur = sum(value * weight / 16 for value, weight in _neighbors(guide))
        residual = guide - blur
        delta = np.sign(residual) * np.minimum(
            detail["max_delta"],
            detail["amount"] * np.maximum(np.abs(residual) - detail["threshold"], 0),
        )
    pre_tone = transformed + delta[..., None]
    clipped = np.clip(pre_tone, 0, 1)
    rgb = linear_to_srgb(clipped) if selected["tone"]["curve"] == "srgb" else clipped
    arrays = {
        "black_relative_dn_frames": signal,
        "spatial_dn_frames": spatial,
        "temporal_input_dn_frames": spatial,
        "temporal_dn_frames": temporal,
        "temporal_previous_state_dn_frames": previous,
        "temporal_gate_mask_frames": gate,
        "temporal_reset_mask_frames": reasons != 0,
        "temporal_reset_reason_frames": reasons,
        "temporal_inter_transfer_interval_ns": parsed["schedule"]["intervals"],
        "code_rail_mask_frames": rails,
        "demosaiced_code_rgb_frames": demosaiced,
        "white_balanced_code_rgb_frames": balanced,
        "ccm_code_rgb_frames": transformed,
        "pre_tone_code_rgb_frames": pre_tone,
        "detail_delta_frames": delta,
        "rgb_frames": rgb,
        "rgb": rgb[0].copy(),
        "native_site_index": np.tile(native_sites, (rows // 2, cols // 2)),
    }
    if (
        any(not np.isfinite(a).all() for a in arrays.values())
        or sum(a.nbytes for a in arrays.values()) > parsed["resources"]["output_array_bytes_bound"]
    ):
        isp._refuse("Code ISP exceeded its numerical/evidence forecast", "result")
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        isp._refuse("Code ISP changed the acquired RAW", "raw")
    units = {key: "normalized_code_rgb" for key in arrays}
    for key, value in arrays.items():
        if "_dn_" in key:
            units[key] = "DN"
        elif value.dtype.kind == "b":
            units[key] = "boolean"
        elif value.dtype.kind in "iu":
            units[key] = "ns" if key.endswith("interval_ns") else "1"
    stages = {
        "black_relative_dn": "black_relative_dn_frames",
        "same_site_spatial_filter": "spatial_dn_frames",
        "causal_temporal_filter": "temporal_dn_frames",
        "demosaic": "demosaiced_code_rgb_frames",
        "fixed_color": "ccm_code_rgb_frames",
        "bounded_code_detail": "pre_tone_code_rgb_frames",
        "display_transfer": "rgb_frames",
    }
    return {
        "arrays": arrays,
        "summary": {
            "schema_version": "camera_retained_code_isp_result_v1",
            "implementation": IMPLEMENTATION,
            "profile": selected,
            "profile_sha256": digest(selected),
            "raw_format": parsed["format"],
            "geometry": parsed["geometry"],
            "input_shape": list(raw.shape),
            "input_dtype": str(raw.dtype),
            "input_array_bytes_sha256": before,
            "normalization_dn": white - black,
            "acquisition_response_type_preserved": raw_format["response_type"],
            "processing_domain": "code_coordinates_not_photocharge_or_scene_linear_rgb",
            "temporal_state_role": "causal_algorithm_state_not_photodiode_charge",
            "temporal_time_basis": "per_row_original_transfer_events",
            "temporal_reset_reason_bits": RESET_REASONS.copy(),
            "events_sha256": events["schedule_sha256"],
            "source_truth_arrays_used": False,
            "charge_inversion": False,
            "dark_subtraction": False,
            "stage_outputs": {
                key: {"array": value, "unit": units[value]} for key, value in stages.items()
            },
            "array_units": units,
            "resources": parsed["resources"],
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "limitations": [
                "Code-space filters can add persistence; they do not remove PD history.",
                "Final code rails cannot identify every earlier ADC or full-well clipping event.",
                "Fixed gains/matrix and display transfer do not establish physical color accuracy.",
                "Temporal code differences are not stationary noise or SNR measurements.",
            ],
        },
    }
