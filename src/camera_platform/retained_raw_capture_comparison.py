"""Descriptive original-code comparison of a retained Run and imported Capture.

This is deliberately outside the temporal ISP, HDR and optimizer workflows.
It does not infer that two declared scenes, devices or clocks were physically
the same, and it never turns a capture's provenance declaration into a pass.
"""

from __future__ import annotations

import math
from copy import deepcopy

import numpy as np

from . import nonlinear_captures, retained_isp_source
from .registry import IntegrityError, digest

SCHEMA = "camera_retained_raw_capture_comparison_v1"
RESULT_SCHEMA = "camera_retained_raw_capture_comparison_result_v1"
MAX_ROI_SELECTED_PIXELS = 1_000_000
READOUTS = {"single": 0, "HCG": 0, "LCG": 1}
ROI_KEYS = {"row_start", "row_end", "column_start", "column_end"}


def _request(value):
    if not isinstance(value, dict) or set(value) != {
        "schema", "run_id", "capture_id", "readout_id", "roi",
    } or value["schema"] != SCHEMA:
        raise ValueError("RAW comparison requires its exact v1 request")
    for key in ("run_id", "capture_id"):
        item = value[key]
        if (not isinstance(item, str) or not item or item != item.strip()
                or len(item) > 200):
            raise ValueError("RAW comparison requires bounded source IDs")
    if (not isinstance(value["readout_id"], str)
            or value["readout_id"] not in READOUTS):
        raise ValueError("RAW comparison needs single, HCG or LCG readout")
    roi = value["roi"]
    if not isinstance(roi, dict) or set(roi) != ROI_KEYS or any(
        type(v) is not int for v in roi.values()
    ):
        raise ValueError("RAW comparison requires an exact integer receiver ROI")
    return deepcopy(value)


def _selected_capture(raw, capture, readout_id):
    mode = capture["readout_mode"]
    if (readout_id == "single" and mode != "sequential_frames"
            or readout_id != "single" and mode != "shared_charge_paths"
            or capture["readout_ids"] != (
                ["single"] if mode == "sequential_frames" else ["HCG", "LCG"]
            )):
        raise ValueError("Imported RAW readout path differs from requested retained path")
    index = READOUTS[readout_id]
    if (mode == "sequential_frames" and raw.ndim != 3
            or mode == "shared_charge_paths" and (raw.ndim != 4 or raw.shape[1] != 2)):
        raise IntegrityError("Imported RAW container differs from original readout mode")
    return (raw if mode == "sequential_frames" else raw[:, index]), index


def _roi(value, shape):
    frames, rows, columns = shape
    r0, r1, c0, c1 = (value[key] for key in (
        "row_start", "row_end", "column_start", "column_end",
    ))
    if (not 0 <= r0 < r1 <= rows or not 0 <= c0 < c1 <= columns
            or r1 - r0 < 2 or c1 - c0 < 2
            or frames * (r1 - r0) * (c1 - c0) > MAX_ROI_SELECTED_PIXELS):
        raise ValueError("RAW comparison ROI exceeds fixed native/pixel support")
    return slice(r0, r1), slice(c0, c1)


def _applied_issues(source, capture, index):
    issues = []
    for frame_index, (simulated, acquired) in enumerate(zip(
            source["frame_conditions"], capture["frames"], strict=True)):
        applied = acquired["applied"]
        if acquired["settings_evidence"]["status"] != "recorded_applied":
            issues.append({"frame_index": frame_index,
                           "reason": "capture_applied_settings_not_recorded"})
            continue
        readout = applied["readout_settings"][index]
        pairs = (
            ("exposure_ns", simulated["exposure_ns"], applied["exposure_ns"]),
            ("gain", simulated["gain"], applied["gain"]),
            ("temperature_c", simulated["temperature_c"], applied["temperature_c"]),
            ("readout_analog_gain", simulated["readout_analog_gain"],
             readout["analog_gain"]),
            ("readout_digital_gain", simulated["readout_digital_gain"],
             readout["digital_gain"]),
        )
        for field, expected, actual in pairs:
            if (type(expected) not in (int, float) or type(actual) not in (int, float)
                    or not math.isfinite(expected) or not math.isfinite(actual)
                    or expected != actual):
                issues.append({"frame_index": frame_index,
                               "reason": "declared_applied_setting_differs", "field": field})
    return issues


def _metrics(simulated, acquired, *, black, white, origin, roi):
    rows, columns = roi
    a = simulated[:, rows, columns].astype(np.float64)
    b = acquired[:, rows, columns].astype(np.float64)
    delta = b - a
    site = np.empty(a.shape[1:], dtype=np.uint8)
    native_rows = np.arange(rows.start, rows.stop) + origin[0]
    native_columns = np.arange(columns.start, columns.stop) + origin[1]
    site[:] = 2 * (native_rows[:, None] % 2) + native_columns[None, :] % 2
    names = ("R", "Gr", "Gb", "B")
    frames = []
    for index in range(len(a)):
        frame = {
            "frame_index": index,
            "simulated_mean_dn": float(np.mean(a[index])),
            "capture_mean_dn": float(np.mean(b[index])),
            "capture_minus_simulated_mean_dn": float(np.mean(delta[index])),
            "pixelwise_mae_dn": float(np.mean(np.abs(delta[index]))),
            "pixelwise_rmse_dn": float(np.sqrt(np.mean(delta[index] ** 2))),
            "simulated_code_rail_fraction": float(np.mean(
                (a[index] <= black) | (a[index] >= white),
            )),
            "capture_code_rail_fraction": float(np.mean(
                (b[index] <= black) | (b[index] >= white),
            )),
            "cfa_site_mean_difference_dn": {
                name: float(np.mean(delta[index][site == position]))
                for position, name in enumerate(names)
            },
        }
        frames.append(frame)
    return frames


def compare(service, request):
    """Cold-resolve original bytes and return bounded DN diagnostics only."""
    request = _request(request)
    readout_id = request["readout_id"]
    source = retained_isp_source.resolve(
        service, request["run_id"],
        {"schema": retained_isp_source.SELECTION_SCHEMA, "readout_id": readout_id},
    )
    simulated = retained_isp_source.load_inputs(service, source)
    capture = nonlinear_captures.get(service, request["capture_id"])
    container = nonlinear_captures.load_raw(service, capture)
    acquired, path_index = _selected_capture(container, capture, readout_id)
    if (source["source_snapshot"]["assembly_id"] != capture["assembly_id"]
            or source["source_snapshot"]["assembly_snapshot"]
            != capture["assembly_snapshot"]):
        raise ValueError("RAW comparison assembly differs")
    if (source["raw_format"] != capture["raw_formats"][path_index]
            or source["geometry"] != capture["geometry"]
            or source["pixel_pitch_um"] != capture["pixel_pitch_um"]
            or simulated.shape != acquired.shape
            or len(source["frame_conditions"]) != len(capture["frames"])):
        raise ValueError("RAW comparison format, native crop or frame support differs")
    if (source["source_snapshot"]["test_id"] != capture["test_id"]
            or source["conditions"] != capture["conditions"]):
        raise ValueError("RAW comparison declared Test identity or conditions differ")
    roi = _roi(request["roi"], simulated.shape)
    simulated_roi = simulated[:, roi[0], roi[1]]
    acquired_roi = acquired[:, roi[0], roi[1]]
    if (nonlinear_captures.pixel_hash(simulated)
            == nonlinear_captures.pixel_hash(acquired)
            or nonlinear_captures.pixel_hash(simulated_roi)
            == nonlinear_captures.pixel_hash(acquired_roi)):
        raise ValueError("Imported RAW aliases the simulated selected pixel sequence or ROI")
    issues = _applied_issues(source, capture, path_index)
    report = {
        "schema": RESULT_SCHEMA,
        "request": request,
        "provenance": {
            "run_id": source["source_id"],
            "run_source_sha256": digest(source),
            "run_selected_array_sha256": source["selected_array_sha256"],
            "run_selected_pixel_sha256": nonlinear_captures.pixel_hash(simulated),
            "capture_id": capture["id"],
            "capture_content_hash": capture["content_hash"],
            "capture_raw_artifact_sha256": capture["raw_sha256"],
            "capture_selected_pixel_sha256": nonlinear_captures.pixel_hash(acquired),
            "run_selected_roi_pixel_sha256": nonlinear_captures.pixel_hash(simulated_roi),
            "capture_selected_roi_pixel_sha256": nonlinear_captures.pixel_hash(acquired_roi),
            "capture_source_kind": capture["source_kind"],
            "capture_physical_instance": capture["physical_instance"],
            "run_test_id": source["source_snapshot"]["test_id"],
            "capture_test_id": capture["test_id"],
            "capture_exposure_tokens_sha256": digest(capture["exposure_tokens"]),
        },
        "comparison_status": "INCONCLUSIVE" if issues else "DESCRIPTIVE_ONLY",
        "issues": issues,
        "frame_metrics": None if issues else _metrics(
            simulated, acquired,
            black=source["raw_format"]["black_level_dn"],
            white=2 ** source["raw_format"]["bit_depth"] - 1,
            origin=source["geometry"]["crop_origin"], roi=roi,
        ),
        "metric_unit": "DN",
        "metric_interpretation": "descriptive_same-index_original_RAW_not_accuracy",
        "optical_scene_equivalence": "unknown",
        "model_device_binding": "unknown",
        "timestamp_accuracy": "unknown",
        "temporal_validation": "not_established",
        "hdr_lfm_validation": "not_established",
        "optimizer_validation": "not_established",
        "blinding_enforced": False,
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
    }
    return report
