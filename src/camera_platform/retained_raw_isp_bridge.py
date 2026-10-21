"""Exact retained RAW sources for the bounded ISP v2 replay graph.

The only accepted mosaic layouts are the saved sensor DB's native two-by-two
RGB Bayer tiles.  A four-by-four Quad Bayer tile must not be relabelled RGGB
merely because its first four samples resemble a Bayer corner.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.dataset import _sha256_array
from pyisetcam.sensor_simulation_db import sensor_simulation_db_get

from . import isp_v2
from .retained_raw_dataset import load_retained_lens_sensor_raw

SCHEMA = "camerae2e_lens_sensor_isp_replay_v1"
_THREE_SITE = (["red", "green", "blue"], [[1, 2], [2, 3]])
_FOUR_SITE = (["R", "Gr", "Gb", "B"], [[1, 2], [3, 4]])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_native_isp_source(
    raw: np.ndarray,
    *,
    native_cfa: Mapping[str, Any],
    model: Mapping[str, Any],
    captured_readout: Mapping[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Bind an actual saved CFA/readout to the single-stream ISP v2 contract.

    ``RGGB`` describes the color layout, not an arbitrary sensor called Bayer.
    Only the two native DB tiles below have the correct two-by-two phase.  The
    physical four-site response is retained as four sites, including Gr/Gb;
    this ISP's demosaic treats both green sites as G in its RGB result.
    """
    selected = isp_v2.validate_profile(profile)
    if selected["calibration_id"] is not None or any(
        selected[stage]["mode"] != "bypass" for stage in ("spatial_lsc", "raw_denoise")
    ):
        raise ValueError("Retained RAW ISP requires bypass LSC/denoise and no calibration ID.")
    if not isinstance(native_cfa, Mapping) or not isinstance(model, Mapping):
        raise ValueError("Saved sensor native CFA and model snapshot are required.")
    names, pattern = native_cfa.get("filter_names"), native_cfa.get("pattern")
    if (names, pattern) not in (_THREE_SITE, _FOUR_SITE):
        raise ValueError("Saved sensor native CFA is not an explicit 2x2 RGGB tile.")
    if (
        native_cfa.get("pattern_index_base") != 1
        or model.get("filter_names") != names
        or native_cfa.get("pixel_pitch_um") != model.get("pixel_pitch_um")
        or (
            names == _THREE_SITE[0]
            and (model.get("kind") != "db_estimate_v1"
                 or model.get("cfa_pattern") != "rggb_bayer")
        )
        or (names == _FOUR_SITE[0] and model.get("kind") != "physical_estimate_v1")
    ):
        raise ValueError("Saved sensor native CFA differs from its model snapshot.")
    nominal = native_cfa.get("native_size")
    if (
        not isinstance(nominal, list) or len(nominal) != 2
        or any(type(v) is not int or v <= 0 or v % 2 for v in nominal)
    ):
        raise ValueError("Saved native CFA lacks an even, two-dimensional nominal size.")
    if (
        not isinstance(raw, np.ndarray)
        or raw.ndim != 2
        or raw.dtype != np.dtype("int32")
        or any(length <= 0 or length % 2 for length in raw.shape)
    ):
        raise ValueError("Retained native RAW must be an even, two-dimensional int32 mosaic.")
    readout = model.get("readout")
    if not isinstance(readout, dict) or captured_readout != readout:
        raise ValueError("Retained RAW readout differs from the saved sensor snapshot.")
    bits, black = readout.get("bit_depth"), readout.get("black_level_dn")
    if (
        type(bits) is not int or not 1 <= bits <= 32
        or isinstance(black, bool) or not isinstance(black, (int, float))
        or not math.isfinite(black)
    ):
        raise ValueError("Saved readout lacks a valid ADC bit depth and black level.")
    raw_format = {
        "cfa": "RGGB",
        "bit_depth": bits,
        "black_level_dn": black,
        "response_type": "linear",
        "packing": "unpacked",
        "layout": "bayer",
        "hdr_streams": 1,
    }
    geometry = {
        "kind": "native_sensor",
        "full_sensor_shape": list(raw.shape),
        "crop_origin": [0, 0],
    }
    isp_v2.validate_inputs(raw, raw_format, selected, geometry=geometry)
    return {
        "raw": raw,
        "raw_content_sha256": _sha256_array(raw),
        "native_cfa": dict(native_cfa),
        "readout": dict(readout),
        "raw_format": raw_format,
        "geometry": geometry,
        "profile": selected,
        "native_cfa_contract": (
            "three_channel_rggb_2x2" if names == _THREE_SITE[0]
            else "four_site_r_gr_gb_b_2x2"
        ),
    }


def inspect_lens_sensor_isp_input(
    source_report_path: str | Path,
    source_raw_path: str | Path,
    *,
    source_lens_sensor_job_id: str,
    sensor_db_root: str | Path,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Preflight a hash-pinned Lens × Sensor RAW without re-running capture."""
    report, raw, binding = load_retained_lens_sensor_raw(
        source_report_path,
        source_raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
    )
    preview = report["preview"]
    selected = report.get("source_identities") or {}
    revision = selected.get("sensor")
    if (
        not isinstance(revision, dict)
        or revision != binding["saved_sensor_revision"]
        or selected.get("lens") != binding["lens_identity"]
        or preview.get("sensor_model", {}).get("kind") != "db_estimate_v1"
        or preview.get("sensor_model", {}).get("saved_snapshot_used") is not True
        or preview.get("lens_model", {}).get("absolute_radiometry")
        != "unknown_unity_transmission_assumption"
        or preview.get("lens_model", {}).get("relative_illumination")
        != "unknown_unity_assumption"
        or preview.get("capture", {}).get("sensor_size") != list(raw.shape)
    ):
        raise ValueError("Lens × Sensor source identity or approximation contract differs.")
    saved = sensor_simulation_db_get(
        revision["report_code"], root=sensor_db_root, run_id=revision["run_id"]
    )
    row, metadata = saved["db_entry"], saved["metadata"]
    if any(row.get(key) != revision.get(key) for key in (
        "sensor_id", "report_code", "run_id", "result_sha256", "config_sha256"
    )):
        raise ValueError("Saved sensor DB revision differs from the retained Lens × Sensor RAW.")
    checked = validate_native_isp_source(
        raw,
        native_cfa=metadata.get("native_cfa") or {},
        model=metadata["model_snapshot"],
        captured_readout=preview["sensor_model"].get("effective_readout") or {},
        profile=profile,
    )
    checked["source_binding"] = binding
    return checked


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


def replay_lens_sensor_raw(
    source_report_path: str | Path,
    source_raw_path: str | Path,
    *,
    source_lens_sensor_job_id: str,
    sensor_db_root: str | Path,
    profile: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Any]:
    """Apply ISP v2 to the exact approximate Lens × Sensor integer RAW."""
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Lens × Sensor ISP replay output_dir is not empty: {destination}")
    checked = inspect_lens_sensor_isp_input(
        source_report_path,
        source_raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
        sensor_db_root=sensor_db_root,
        profile=profile,
    )
    raw = checked["raw"]
    original_raw_hash = checked["raw_content_sha256"]
    default = isp_v2.default_profile()
    reference = isp_v2.process_raw(
        raw, checked["raw_format"], default, geometry=checked["geometry"]
    )
    candidate = isp_v2.process_raw(
        raw, checked["raw_format"], checked["profile"], geometry=checked["geometry"]
    )
    if _sha256_array(raw) != original_raw_hash:
        raise ValueError("Retained Lens × Sensor RAW changed during ISP replay.")
    reference_rgb = np.asarray(reference["arrays"]["rgb"], dtype=np.float32)
    candidate_rgb = np.asarray(candidate["arrays"]["rgb"], dtype=np.float32)
    same_profile_baseline = np.asarray(candidate["arrays"]["baseline_rgb"], dtype=np.float32)
    valid = np.asarray(candidate["arrays"]["rgb_valid_mask_frames"][0], dtype=bool)
    arrays = {
        "source_raw_dn_frame": raw,
        "reference_default_rgb": reference_rgb,
        "candidate_rgb": candidate_rgb,
        "same_profile_v1_baseline_rgb": same_profile_baseline,
        "candidate_valid_rgb_mask": valid,
        "candidate_sharpen_delta": np.asarray(
            candidate["arrays"]["sharpen_delta_frames"][0], dtype=np.float32
        ),
    }
    before_after = {
        "default_vs_candidate": _available_delta(reference_rgb, candidate_rgb, valid),
        "same_profile_v1_baseline_vs_v2": _available_delta(
            same_profile_baseline, candidate_rgb, valid
        ),
        "valid_pixel_fraction": float(np.mean(valid)),
        "reference_mean_rgb": [float(v) for v in np.mean(reference_rgb, axis=(0, 1))],
        "candidate_mean_rgb": [float(v) for v in np.mean(candidate_rgb, axis=(0, 1))],
        "reference_endpoint_sample_fraction": float(
            np.mean((reference_rgb <= 0) | (reference_rgb >= 1))
        ),
        "candidate_endpoint_sample_fraction": float(
            np.mean((candidate_rgb <= 0) | (candidate_rgb >= 1))
        ),
        "interpretation": (
            "Image differences are diagnostic ISP effects on one retained RAW frame, "
            "not quality scores or color accuracy."
        ),
    }
    destination.mkdir(parents=True, exist_ok=True)
    array_path = destination / "lens_sensor_isp_replay_arrays.npz"
    with array_path.open("wb") as stream:
        np.savez_compressed(stream, **arrays)
    with np.load(array_path, allow_pickle=False) as stored:
        restored = np.asarray(stored["source_raw_dn_frame"])
        if (
            restored.dtype != raw.dtype or restored.shape != raw.shape
            or restored.tobytes(order="C") != raw.tobytes(order="C")
        ):
            raise ValueError("ISP replay evidence no longer contains the exact source RAW.")
    binding = checked["source_binding"]
    report = {
        "schema": SCHEMA,
        "status": "completed",
        "source_kind": "lens_sensor_raw",
        "source_binding": binding,
        "saved_native_cfa": checked["native_cfa"],
        "native_cfa_contract": checked["native_cfa_contract"],
        "raw_format": checked["raw_format"],
        "geometry": checked["geometry"],
        "geometry_interpretation": (
            "Full executed Lens × Sensor mosaic, origin (0,0); "
            "not a measured product die coordinate system."
        ),
        "source_readout": checked["readout"],
        "raw_content_sha256": original_raw_hash,
        "raw_values_byte_equal": True,
        "candidate_rgb_consumer": {
            "array_artifact": array_path.name,
            "array_key": "candidate_rgb",
            "array_dtype": "float32",
            "channel_order": "RGB",
            "tone_curve": checked["profile"]["tone"]["curve"],
            "value_range": [0.0, 1.0],
            "source_binding": binding,
            "ground_truth_available": False,
            "detector_evaluation_performed": False,
        },
        "reference_isp": reference["summary"],
        "candidate_isp": candidate["summary"],
        "before_after": before_after,
        "limitations": [
            "One retained approximate geometric Lens DB × saved Sensor DB RAW; "
            "no capture recompute.",
            "Lens transmission and relative illumination were assumed unity, not validated.",
            "No LSC or denoise without hash-bound capture-domain calibration.",
            "No measured color accuracy, detector score, HDR fusion or physical validation.",
        ],
    }
    report["artifacts"] = {"npz_name": array_path.name, "npz_sha256": _sha256(array_path)}
    json_path = destination / "lens_sensor_isp_replay_report.json"
    json_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "artifacts": {"json": str(json_path), "npz": str(array_path)},
        "artifact_sha256": {"json": _sha256(json_path), "npz": _sha256(array_path)},
    }
