"""Qualification for a registered, measured HDR/LFM comparison.

This inspects an already validated reference packet and its frozen model source.
The current packet has no independently evidenced coordinate transform, PWM
clock trace or measured readout calibration.  The output is an executable,
source-bound list of missing inputs, never a registration or product pass.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import ROUND_CEILING, Decimal
from typing import Any

from .hdr_lfm_reference_packet import SCHEMA as PACKET_SCHEMA
from .registry import digest

SCHEMA = "camerae2e_hdr_lfm_registration_qualification_v1"


def contract() -> dict[str, Any]:
    """Describe evidence needed before a pixelwise fusion comparison can run."""
    return {
        "schema": SCHEMA,
        "current_status": "blocked_pending_independent_registration_evidence",
        "required_inputs": [
            {
                "code": "virtual_window_to_native_mapping",
                "input": (
                    "A source-pinned transform from the frozen model's virtual pixel "
                    "centres to physical native sensor coordinates, including ROI, "
                    "orientation, CFA phase and uncertainty."
                ),
            },
            {
                "code": "reference_to_raw_spatial_registration",
                "input": (
                    "Reference-to-RAW coordinate transforms for every phase, with "
                    "source images or fiducials, overlap masks, residuals and uncertainty."
                ),
            },
            {
                "code": "raw_waveform_clock_registration",
                "input": (
                    "A recorded PWM waveform or trigger-edge trace in the RAW timestamp "
                    "clock, covering every row's exposure interval, with clock offset "
                    "and uncertainty."
                ),
            },
            {
                "code": "reference_phase_time_registration",
                "input": (
                    "Reference acquisition timestamps and integration windows in that "
                    "clock, linked to the four uploaded reference-rate planes."
                ),
            },
            {
                "code": "measured_readout_calibration",
                "input": (
                    "Source-pinned physical black level, DN-to-electron gain, saturation "
                    "and noise/FPN calibration for the selected mode, with uncertainty."
                ),
            },
            {
                "code": "independent_source_review",
                "input": (
                    "Original instrument and calibration records, a matching physical "
                    "scene/lens configuration, and independent review of their origin."
                ),
            },
        ],
        "current_metric_limit": "unregistered_global_means_only",
        "pixelwise_error_available": False,
        "measured_fusion_error_available": False,
        "product_hdr_lfm_decision": "inconclusive",
    }


def _ns(seconds: Any) -> int:
    """Conservatively round a validated nonnegative time to nanoseconds."""
    return int((Decimal(str(seconds)) * Decimal(1_000_000_000)).to_integral_value(
        rounding=ROUND_CEILING
    ))


def qualify(
    metadata: Mapping[str, Any], files: Mapping[str, bytes], *,
    packet_receipt: Mapping[str, Any] | None,
    hdr_report: Mapping[str, Any], source_lfm_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a bound qualification, after ``validate_packet`` has succeeded.

    Legacy opaque-log packets cannot supply the verified 2x4 frame labels and
    are explicitly not assessed.  This function does not estimate a transform
    from a same-shaped array or authenticate a submitter's declared clock.
    """
    requirements = contract()
    source = {
        "source_hdr_job_id": metadata["source_hdr_job_id"],
        "source_hdr_report_sha256": digest(hdr_report),
        "source_lfm_report_sha256": digest(source_lfm_report),
    }
    if packet_receipt is None:
        return {
            "schema": SCHEMA,
            "source": source,
            "status": "not_assessed_legacy_opaque_logs",
            "packet_consistency": "not_assessed",
            "requirements": requirements,
        }
    if (packet_receipt.get("schema") != PACKET_SCHEMA
            or packet_receipt.get("internal_consistency")
            != "verified_against_uploaded_arrays_and_frozen_job"):
        raise ValueError("Registration qualification requires a verified structured packet")
    mode_log = json.loads(files["mode_log"].decode("utf-8"))
    frames = mode_log["frame_order"]
    rows = metadata["raw_acquisition"]["shape"][2]
    line_time_s = metadata["mode"]["line_time_s"]
    windows = []
    for frame in frames:
        start = frame["row_zero_timestamp_ns"]
        duration = (
            Decimal(rows - 1) * Decimal(str(line_time_s))
            + Decimal(str(frame["exposure_s"]))
        )
        end = start + _ns(duration)
        windows.append({
            "array_index": frame["array_index"],
            "frame_id": frame["frame_id"],
            "exposure_index": frame["exposure_index"],
            "phase_index": frame["phase_index"],
            "declared_first_row_start_ns": start,
            "declared_last_row_exposure_end_ns": end,
        })
    geometry = source_lfm_report["geometry_provenance"]
    return {
        "schema": SCHEMA,
        "source": source,
        "status": "blocked_pending_independent_registration_evidence",
        "packet_consistency": packet_receipt["internal_consistency"],
        "verified_packet_bindings": {
            "frame_count": len(frames),
            "raw_shape": list(metadata["raw_acquisition"]["shape"]),
            "reference_rate_shape": list(metadata["reference_rate"]["shape"]),
            "native_crop_origin_row_col_declared": list(
                metadata["mode"]["native_crop_origin_row_col"]
            ),
            "cfa_tile_phase_row_col": packet_receipt["native_cfa_tile_phase_row_col"],
            "model_virtual_scope": geometry["configured_sensor_scope"],
            "model_native_crop_mapping": geometry["saved_native_array_crop_mapping"],
            "model_native_crop_origin": geometry["saved_native_array_crop_origin"],
        },
        "declared_timing_window": {
            "clock_scope": "submitter_declared_unverified_clock",
            "first_start_ns": min(item["declared_first_row_start_ns"] for item in windows),
            "last_end_ns": max(item["declared_last_row_exposure_end_ns"] for item in windows),
            "frames": windows,
            "waveform_trace_verified": False,
        },
        "requirements": requirements,
    }
