"""Apply declared ISP v2 LSC/denoise to one exact saved-quality RAW frame.

The input is the retained int32 simulation mosaic, not a product-die crop. A
caller supplies a separate, SHA-256-pinned JSON calibration record with native
four-site LSC/noise numbers. Its provenance is declared by the supplier and is
not verified physical measurement. The record must bind the exact quality
artifacts, RAW content, saved sensor revision, CFA, readout, simulated mosaic
geometry and acquisition conditions. A claimed calibration derived from this
target RAW is refused, but independent derivation cannot be proven from a
self-declared record alone.

``evaluate_saved_sensor_calibrated_isp`` makes no files or new capture. The
optional ``run_saved_sensor_calibrated_isp`` writes a report and NPZ with the
original RAW and diagnostic ISP arrays. Neither function fits calibration,
estimates image-quality improvement, nor validates a physical camera.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.dataset import _sha256_array

from . import isp_v2
from .isp_calibration import validate_numeric_payload
from .saved_sensor_isp_replay import inspect_saved_quality_isp_input

SCHEMA = "camerae2e_saved_sensor_calibrated_isp_v1"
CALIBRATION_SCHEMA = "camerae2e_saved_sensor_isp_calibration_v1"
MAX_CALIBRATION_BYTES = 2 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError(f"{name} requires a lowercase SHA-256 hex digest.")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError(f"{name} requires nonempty bounded text.")
    return value


def _exact(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name} requires exactly {sorted(keys)}.")
    return value


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate calibration JSON key: {key}.")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"Calibration JSON contains a nonfinite number: {value}.")


def validate_calibration_bytes(content: bytes, expected_sha256: str) -> dict[str, Any]:
    """Validate bounded imported JSON bytes; applicability is checked at replay."""
    expected = _sha(expected_sha256, "calibration_sha256")
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_CALIBRATION_BYTES:
        raise ValueError("Calibration JSON exceeds the bounded file size.")
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("Calibration file differs from the submitted SHA-256.")
    try:
        result = json.loads(
            content,
            object_pairs_hook=_no_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Calibration file is not valid UTF-8 JSON.") from exc
    if not isinstance(result, dict):
        raise ValueError("Calibration JSON must be an object.")
    _validate_calibration(result)
    return result


def _read_calibration(path: str | Path, expected_sha256: str) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not 0 < source.stat().st_size <= MAX_CALIBRATION_BYTES:
        raise ValueError("Calibration JSON exceeds the bounded file size.")
    return validate_calibration_bytes(source.read_bytes(), expected_sha256)


def _binding(
    report: dict[str, Any], case: dict[str, Any], checked: dict[str, Any]
) -> dict[str, Any]:
    source = checked["source_binding"]
    controls = report["controls"]
    acquisition = {
        "scene": case["scene"],
        "luminance_cd_m2": case["luminance_cd_m2"],
        "exposure_s": case["exposure_s"],
        "frame_seed": source["source_frame_seed"],
        "quality_seed": controls["seed"],
        "saved_fixed_pattern_seed": controls["saved_fixed_pattern_seed"],
        "fnumber": controls["fnumber"],
        "focal_length_m": controls["focal_length_m"],
        "output_size": controls["output_size"],
        "readout_overrides": controls["readout_overrides"],
        "noise_flag": 2,
    }
    return {
        "source_quality_job_id": source["source_quality_job_id"],
        "source_report_sha256": source["source_report_sha256"],
        "source_arrays_sha256": source["source_arrays_sha256"],
        "source_case_index": source["source_case_index"],
        "source_raw_content_sha256": source["source_raw_content_sha256"],
        "saved_sensor_revision": source["saved_sensor_revision"],
        "native_cfa": checked["native_cfa"],
        "readout": checked["readout"],
        "raw_format": checked["raw_format"],
        "geometry": checked["geometry"],
        "acquisition": acquisition,
    }


def _validate_calibration(
    record: dict[str, Any], *, expected_binding: dict[str, Any] | None = None
) -> dict[str, Any]:
    _exact(record, {"schema", "provenance", "binding", "payload"}, "calibration")
    if record["schema"] != CALIBRATION_SCHEMA:
        raise ValueError("Unsupported saved sensor ISP calibration schema.")
    provenance = _exact(
        record["provenance"],
        {
            "source_kind",
            "source_uri",
            "method_and_version",
            "source_dataset_id",
            "source_dataset_sha256",
            "target_raw_used_for_fitting",
            "uncertainty",
        },
        "calibration provenance",
    )
    if provenance["source_kind"] not in {"estimated", "simulated", "vendor", "measured"}:
        raise ValueError("Unsupported calibration source kind.")
    for name in ("source_uri", "method_and_version", "source_dataset_id"):
        _text(provenance[name], f"calibration provenance {name}")
    dataset_sha = _sha(provenance["source_dataset_sha256"], "source_dataset_sha256")
    if provenance["target_raw_used_for_fitting"] is not False:
        raise ValueError("Calibration fitted to the target RAW is unsupported.")
    uncertainty = _exact(provenance["uncertainty"], {"kind", "reason"}, "uncertainty")
    if uncertainty["kind"] != "unknown":
        raise ValueError("Calibration uncertainty must remain unknown in this path.")
    _text(uncertainty["reason"], "uncertainty reason")
    binding = _exact(
        record["binding"],
        {
            "source_quality_job_id",
            "source_report_sha256",
            "source_arrays_sha256",
            "source_case_index",
            "source_raw_content_sha256",
            "saved_sensor_revision",
            "native_cfa",
            "readout",
            "raw_format",
            "geometry",
            "acquisition",
        },
        "calibration binding",
    )
    report_sha = _sha(binding["source_report_sha256"], "source_report_sha256")
    arrays_sha = _sha(binding["source_arrays_sha256"], "source_arrays_sha256")
    raw_sha = binding["source_raw_content_sha256"]
    if not isinstance(raw_sha, str) or not raw_sha.startswith("sha256:"):
        raise ValueError("source_raw_content_sha256 requires sha256:<hex>.")
    _sha(raw_sha.removeprefix("sha256:"), "source_raw_content_sha256")
    forbidden_hashes = {report_sha, arrays_sha, raw_sha.removeprefix("sha256:")}
    if dataset_sha in forbidden_hashes:
        raise ValueError("Calibration dataset cannot be the target quality artifact or RAW.")
    if expected_binding is not None and binding != expected_binding:
        raise ValueError("Calibration binding differs from the exact retained RAW source.")
    payload = validate_numeric_payload(record["payload"])
    return {"provenance": deepcopy(provenance), "payload": payload}


def _candidate_profile(profile: dict[str, Any], calibration_sha256: str) -> dict[str, Any]:
    candidate = isp_v2.validate_profile(profile)
    expected_id = f"sha256:{calibration_sha256}"
    if candidate["calibration_id"] != expected_id:
        raise ValueError("ISP profile calibration_id must pin the calibration file SHA-256.")
    if (
        candidate["spatial_lsc"]["mode"] == "bypass"
        and (
            candidate["raw_denoise"]["mode"] == "bypass"
            or candidate["raw_denoise"]["strength"] == 0
        )
    ):
        raise ValueError("Candidate must apply LSC or nonzero same-site denoise.")
    baseline = isp_v2.default_profile()
    for key in baseline:
        if key not in {"calibration_id", "spatial_lsc", "raw_denoise"}:
            if candidate[key] != baseline[key]:
                raise ValueError(f"Candidate changes unsupported common ISP stage: {key}.")
    return candidate


def _delta(reference: np.ndarray, candidate: np.ndarray, valid: np.ndarray) -> dict[str, Any]:
    if not np.any(valid):
        return {"available": False, "reason": "No common valid RGB pixels."}
    difference = np.abs(candidate - reference)[valid]
    return {
        "available": True,
        "mean_absolute_rgb_difference": float(np.mean(difference)),
        "changed_sample_fraction": float(np.mean(difference > 1e-12)),
        "comparison_domain": "common valid normalized RGB samples after fixed tone",
        "quality_improvement_claimed": False,
    }


def evaluate_saved_sensor_calibrated_isp(
    source_report_path: str | Path,
    source_arrays_path: str | Path,
    *,
    source_quality_job_id: str,
    case_index: int,
    sensor_db_root: str | Path,
    calibration_path: str | Path,
    calibration_sha256: str,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Return JSON summary and ndarray evidence; do not write or reacquire RAW."""
    calibration_sha256 = _sha(calibration_sha256, "calibration_sha256")
    selected = _candidate_profile(profile, calibration_sha256)
    checked = inspect_saved_quality_isp_input(
        source_report_path,
        source_arrays_path,
        source_quality_job_id=source_quality_job_id,
        case_index=case_index,
        sensor_db_root=sensor_db_root,
        profile=isp_v2.default_profile(),
    )
    raw = checked["raw"]
    if raw.dtype != np.dtype("int32") or raw.ndim != 2:
        raise ValueError("Saved quality RAW must remain an exact native int32 mosaic.")
    original_hash = _sha256_array(raw)
    if original_hash != checked["source_binding"]["source_raw_content_sha256"]:
        raise ValueError("Retained RAW content differs from its source binding.")
    report_path = Path(source_report_path).expanduser().resolve()
    arrays_path = Path(source_arrays_path).expanduser().resolve()
    report = json.loads(report_path.read_bytes())
    case = report["cases"][case_index]
    expected_binding = _binding(report, case, checked)
    calibration = _validate_calibration(
        _read_calibration(calibration_path, calibration_sha256),
        expected_binding=expected_binding,
    )
    # These are the exact numeric declarations supplied by the external record.
    isp_v2.validate_inputs(
        raw,
        checked["raw_format"],
        selected,
        geometry=checked["geometry"],
        calibration=calibration["payload"],
    )
    raw.setflags(write=False)
    reference = isp_v2.process_raw(
        raw, checked["raw_format"], isp_v2.default_profile(), geometry=checked["geometry"]
    )
    candidate = isp_v2.process_raw(
        raw,
        checked["raw_format"],
        selected,
        geometry=checked["geometry"],
        calibration=calibration["payload"],
    )
    if (
        _sha256_array(raw) != original_hash
        or _sha256(report_path) != expected_binding["source_report_sha256"]
        or _sha256(arrays_path) != expected_binding["source_arrays_sha256"]
        or _sha256(Path(calibration_path).expanduser().resolve()) != calibration_sha256
    ):
        raise ValueError("Source RAW, report, arrays or calibration changed during ISP replay.")
    ref_arrays, cand_arrays = reference["arrays"], candidate["arrays"]
    common_valid = (
        ref_arrays["rgb_valid_mask_frames"][0]
        & cand_arrays["rgb_valid_mask_frames"][0]
    )
    arrays = {
        "source_raw_dn_frame": raw.copy(),
        "reference_default_rgb": ref_arrays["rgb"].copy(),
        "candidate_rgb": cand_arrays["rgb"].copy(),
        "candidate_lsc_gain_map": cand_arrays["lsc_gain_map"].copy(),
        "candidate_lsc_valid_mask": cand_arrays["lsc_valid_mask_frames"][0].copy(),
        "candidate_denoised_normalized": cand_arrays["denoised_normalized_frames"][0].copy(),
        "candidate_noise_variance_hint_dn2": cand_arrays[
            "noise_variance_hint_dn2_frames"
        ][0].copy(),
        "candidate_denoise_valid_mask": cand_arrays["denoise_valid_mask_frames"][0].copy(),
        "common_valid_rgb_mask": common_valid.copy(),
    }
    summary = {
        "schema": SCHEMA,
        "status": "completed",
        "source_binding": expected_binding,
        "calibration": {
            "schema": CALIBRATION_SCHEMA,
            "file_sha256": calibration_sha256,
            "numeric_sha256": candidate["summary"]["calibration_numeric_sha256"],
            "provenance": calibration["provenance"],
            "provenance_verification": "declared_only_not_independently_verified",
        },
        "raw_content_sha256": original_hash,
        "raw_values_byte_equal": True,
        "geometry_interpretation": (
            "Full executed saved-quality simulation mosaic at origin (0,0); "
            "no physical product-die coordinates established."
        ),
        "reference_isp": reference["summary"],
        "candidate_isp": candidate["summary"],
        "before_after": {
            "default_vs_candidate": _delta(
                arrays["reference_default_rgb"], arrays["candidate_rgb"], common_valid
            ),
            "common_valid_pixel_fraction": float(np.mean(common_valid)),
            "lsc_valid_pixel_fraction": float(np.mean(arrays["candidate_lsc_valid_mask"])),
            "denoise_valid_pixel_fraction": float(
                np.mean(arrays["candidate_denoise_valid_mask"])
            ),
            "interpretation": (
                "Differences are modeled ISP effects on one retained RAW frame, "
                "not measured quality improvement or calibrated uncertainty."
            ),
        },
        "limitations": [
            "Calibration provenance and independence are supplier declarations only.",
            "Noise coefficients are variance hints, not measured uncertainty bounds.",
            "Simulated mosaic coordinates do not identify physical product-die coordinates.",
            "One retained RAW frame cannot establish image-quality improvement.",
        ],
    }
    json.dumps(summary, allow_nan=False)
    return {"summary": summary, "arrays": arrays}


def run_saved_sensor_calibrated_isp(
    source_report_path: str | Path,
    source_arrays_path: str | Path,
    *,
    source_quality_job_id: str,
    case_index: int,
    sensor_db_root: str | Path,
    calibration_path: str | Path,
    calibration_sha256: str,
    profile: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Any]:
    """Persist a pure evaluation as a source-bound JSON report and numeric NPZ."""
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Saved calibrated ISP output_dir is not empty: {destination}")
    evaluated = evaluate_saved_sensor_calibrated_isp(
        source_report_path,
        source_arrays_path,
        source_quality_job_id=source_quality_job_id,
        case_index=case_index,
        sensor_db_root=sensor_db_root,
        calibration_path=calibration_path,
        calibration_sha256=calibration_sha256,
        profile=profile,
    )
    destination.mkdir(parents=True, exist_ok=True)
    array_path = destination / "saved_sensor_calibrated_isp_arrays.npz"
    with array_path.open("wb") as stream:
        np.savez_compressed(stream, **evaluated["arrays"])
    with np.load(array_path, allow_pickle=False) as saved:
        stored_raw = saved["source_raw_dn_frame"]
        if _sha256_array(stored_raw) != evaluated["summary"]["raw_content_sha256"]:
            raise ValueError("Calibrated ISP artifact does not preserve the source RAW.")
    report = deepcopy(evaluated["summary"])
    report["artifacts"] = {"npz_name": array_path.name, "npz_sha256": _sha256(array_path)}
    report_path = destination / "saved_sensor_calibrated_isp_report.json"
    report_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "artifacts": {"json": str(report_path), "npz": str(array_path)},
        "artifact_sha256": {"json": _sha256(report_path), "npz": _sha256(array_path)},
    }
