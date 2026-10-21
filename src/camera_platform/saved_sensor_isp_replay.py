"""Replay a bounded ISP v2 graph on an exact saved sensor-quality RAW frame."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.dataset import _sha256_array
from pyisetcam.sensor_simulation_db import sensor_simulation_db_get

from . import isp_v2
from .retained_raw_dataset import load_retained_quality_frame
from .retained_raw_isp_bridge import validate_native_isp_source

SCHEMA = "camerae2e_saved_sensor_isp_replay_v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _available_delta(reference: np.ndarray, candidate: np.ndarray, valid: np.ndarray) -> dict:
    if not np.any(valid):
        return {"available": False, "reason": "No valid RGB pixels in this RAW capture."}
    values = np.abs(candidate - reference)[valid]
    return {
        "available": True,
        "mean_absolute_rgb_difference": float(np.mean(values)),
        "changed_sample_fraction": float(np.mean(values > 1e-12)),
        "comparison_domain": "valid pixels only, normalized output RGB after tone",
        "quality_improvement_claimed": False,
    }


def inspect_saved_quality_isp_input(
    source_report_path: str | Path,
    source_arrays_path: str | Path,
    *,
    source_quality_job_id: str,
    case_index: int,
    sensor_db_root: str | Path,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Preflight exact RAW, native CFA, saved readout and allowed ISP profile."""
    source_report, case, raw, binding = load_retained_quality_frame(
        source_report_path,
        source_arrays_path,
        source_quality_job_id=source_quality_job_id,
        case_index=case_index,
    )
    revision = source_report["revision"]
    saved = sensor_simulation_db_get(
        revision["report_code"], root=sensor_db_root, run_id=revision["run_id"]
    )
    row, metadata = saved["db_entry"], saved["metadata"]
    if (
        row["run_id"] != revision["run_id"]
        or row["result_sha256"] != revision["result_sha256"]
        or row["config_sha256"] != revision["config_sha256"]
    ):
        raise ValueError("Saved sensor DB revision differs from the retained quality source.")
    if source_report.get("source_boundary", {}).get("saved_sensor_simulation_run_used") is not True:
        raise ValueError("Source quality report lacks a saved sensor simulation binding.")
    if source_report.get("controls", {}).get("readout_overrides") != {}:
        raise ValueError("Source quality report declares readout overrides.")
    checked = validate_native_isp_source(
        raw,
        native_cfa=metadata.get("native_cfa") or {},
        model=metadata["model_snapshot"],
        captured_readout=case.get("readout") or {},
        profile=profile,
    )
    return {
        "source_binding": binding,
        **checked,
    }


def replay_saved_quality_raw(
    source_report_path: str | Path,
    source_arrays_path: str | Path,
    *,
    source_quality_job_id: str,
    case_index: int,
    sensor_db_root: str | Path,
    profile: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Any]:
    """Apply v2 ISP to one retained frame, without sensor or optics recompute.

    The full executed simulation mosaic is treated as RGGB with origin (0,0).
    This is not a claim about physical product die coordinates.
    """
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Saved sensor ISP replay output_dir is not empty: {destination}")
    checked = inspect_saved_quality_isp_input(
        source_report_path,
        source_arrays_path,
        source_quality_job_id=source_quality_job_id,
        case_index=case_index,
        sensor_db_root=sensor_db_root,
        profile=profile,
    )
    raw, binding, cfa, readout, raw_format, geometry, selected = (
        checked[key]
        for key in (
            "raw",
            "source_binding",
            "native_cfa",
            "readout",
            "raw_format",
            "geometry",
            "profile",
        )
    )
    original_raw_hash = _sha256_array(raw)
    default = isp_v2.default_profile()
    reference = isp_v2.process_raw(raw, raw_format, default, geometry=geometry)
    candidate = isp_v2.process_raw(raw, raw_format, selected, geometry=geometry)
    if _sha256_array(raw) != original_raw_hash:
        raise ValueError("Retained source RAW changed during ISP replay.")
    ref_rgb = np.asarray(reference["arrays"]["rgb"], dtype=np.float32)
    candidate_rgb = np.asarray(candidate["arrays"]["rgb"], dtype=np.float32)
    same_profile_baseline = np.asarray(candidate["arrays"]["baseline_rgb"], dtype=np.float32)
    valid = np.asarray(candidate["arrays"]["rgb_valid_mask_frames"][0], dtype=bool)
    output_arrays = {
        "source_raw_dn_frame": raw,
        "reference_default_rgb": ref_rgb,
        "candidate_rgb": candidate_rgb,
        "same_profile_v1_baseline_rgb": same_profile_baseline,
        "candidate_valid_rgb_mask": valid,
        "candidate_sharpen_delta": np.asarray(
            candidate["arrays"]["sharpen_delta_frames"][0], dtype=np.float32
        ),
    }
    before_after = {
        "default_vs_candidate": _available_delta(ref_rgb, candidate_rgb, valid),
        "same_profile_v1_baseline_vs_v2": _available_delta(
            same_profile_baseline, candidate_rgb, valid
        ),
        "valid_pixel_fraction": float(np.mean(valid)),
        "reference_mean_rgb": [float(v) for v in np.mean(ref_rgb, axis=(0, 1))],
        "candidate_mean_rgb": [float(v) for v in np.mean(candidate_rgb, axis=(0, 1))],
        "reference_endpoint_sample_fraction": float(np.mean((ref_rgb <= 0) | (ref_rgb >= 1))),
        "candidate_endpoint_sample_fraction": float(
            np.mean((candidate_rgb <= 0) | (candidate_rgb >= 1))
        ),
        "interpretation": (
            "Image differences are diagnostic effects of the declared ISP profile "
            "on one retained RAW frame, not quality scores or color accuracy."
        ),
    }
    destination.mkdir(parents=True, exist_ok=True)
    array_path = destination / "saved_sensor_isp_replay_arrays.npz"
    with array_path.open("wb") as stream:
        np.savez_compressed(stream, **output_arrays)
    with np.load(array_path, allow_pickle=False) as stored:
        stored_raw = np.asarray(stored["source_raw_dn_frame"])
        if (
            stored_raw.dtype != raw.dtype
            or stored_raw.shape != raw.shape
            or stored_raw.tobytes(order="C") != raw.tobytes(order="C")
        ):
            raise ValueError("ISP replay evidence no longer contains the exact source RAW.")
    report = {
        "schema": SCHEMA,
        "status": "completed",
        "source_binding": binding,
        "saved_native_cfa": cfa,
        "native_cfa_contract": checked["native_cfa_contract"],
        "raw_format": raw_format,
        "geometry": geometry,
        "geometry_interpretation": (
            "Full executed quality-simulation mosaic, origin (0,0); "
            "not a measured product die coordinate system."
        ),
        "source_readout": readout,
        "raw_content_sha256": original_raw_hash,
        "raw_values_byte_equal": True,
        "candidate_rgb_consumer": {
            "array_artifact": "saved_sensor_isp_replay_arrays.npz",
            "array_key": "candidate_rgb",
            "array_dtype": "float32",
            "channel_order": "RGB",
            "tone_curve": selected["tone"]["curve"],
            "value_range": [0.0, 1.0],
            "source_binding": binding,
            "ground_truth_available": False,
            "detector_evaluation_performed": False,
        },
        "reference_isp": reference["summary"],
        "candidate_isp": candidate["summary"],
        "before_after": before_after,
        "limitations": [
            "One retained noisy RAW frame; no sensor or optical recomputation.",
            "No LSC or denoise without a hash-bound numeric calibration in the capture domain.",
            "No HDR fusion, detector score, ground truth, or physical color validation.",
        ],
    }
    report["artifacts"] = {
        "npz_name": array_path.name,
        "npz_sha256": _sha256(array_path),
    }
    json_path = destination / "saved_sensor_isp_replay_report.json"
    json_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "artifacts": {"json": str(json_path), "npz": str(array_path)},
        "artifact_sha256": {"json": _sha256(json_path), "npz": _sha256(array_path)},
    }
