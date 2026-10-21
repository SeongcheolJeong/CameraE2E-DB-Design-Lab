"""Declared, hash-bound LSC/noise ISP v2 on retained Lens × Sensor RAW.

The calibration document binds the target Lens/Sensor revisions, PSF field,
unknown-unity radiometry, native CFA/geometry, readout, acquisition and exact
RAW artifact. Its numeric payload must come from a separately declared source;
the platform verifies bytes and applicability, but cannot prove the supplier's
independence or physical accuracy. Existing saved-quality calibration documents
have a different target binding and are deliberately not accepted here.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.dataset import _sha256_array

from . import isp_v2
from .isp_calibration import validate_numeric_payload
from .retained_raw_isp_bridge import inspect_lens_sensor_isp_input
from .saved_sensor_calibrated_isp import (
    MAX_CALIBRATION_BYTES,
    _candidate_profile,
    _delta,
    _exact,
    _no_duplicate_pairs,
    _reject_nonfinite,
    _sha,
    _text,
)

SCHEMA = "camerae2e_lens_sensor_calibrated_isp_v1"
CALIBRATION_SCHEMA = "camerae2e_lens_sensor_isp_calibration_v1"
REUSABLE_CALIBRATION_SCHEMA = "camerae2e_lens_sensor_isp_calibration_v2"
_BINDING_KEYS = {
    "source_binding", "native_cfa", "readout", "raw_format", "geometry",
    "lens_model", "sensor_model", "scene", "capture",
}
_SOURCE_BINDING_KEYS = {
    "source_lens_sensor_job_id", "source_report_sha256", "source_raw_artifact_sha256",
    "source_raw_content_sha256", "lens_identity", "lens_scope",
    "lens_field_psf_selection", "saved_sensor_revision", "source_scene",
    "source_frame_seed", "lens_radiometry", "physical_validation",
}
_PROVENANCE_KEYS = {
    "source_kind", "source_uri", "method_and_version", "source_dataset_id",
    "source_dataset_sha256", "target_raw_used_for_fitting", "uncertainty",
}
_REUSABLE_BINDING_KEYS = {
    "lens_identity", "lens_scope", "lens_field_psf_selection",
    "saved_sensor_revision", "lens_radiometry", "physical_validation",
    "native_cfa", "readout", "raw_format", "geometry", "lens_model",
    "sensor_model", "scene_model", "operating_point",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def calibration_binding(
    source_report: dict[str, Any], checked: dict[str, Any]
) -> dict[str, Any]:
    """Give an author the exact target contract for a separate calibration."""
    preview = source_report.get("preview") or {}
    source = checked.get("source_binding") or {}
    _exact(source, _SOURCE_BINDING_KEYS, "Lens RAW source binding")
    if (
        source.get("lens_radiometry") != "assumed_unity_not_validated"
        or source.get("physical_validation") != "not_established"
        or preview.get("lens_identity") != source.get("lens_identity")
        or preview.get("sensor_revision") != source.get("saved_sensor_revision")
        or preview.get("lens_model", {}).get("absolute_radiometry")
        != "unknown_unity_transmission_assumption"
        or preview.get("lens_model", {}).get("relative_illumination")
        != "unknown_unity_assumption"
        or preview.get("lens_model", {}).get("field_psf_selection")
        != source.get("lens_field_psf_selection")
        or preview.get("capture", {}).get("seed") != source.get("source_frame_seed")
        or preview.get("capture", {}).get("sensor_size")
        != checked.get("geometry", {}).get("full_sensor_shape")
        or preview.get("sensor_model", {}).get("effective_readout")
        != checked.get("readout")
    ):
        raise ValueError("Lens RAW calibration source lost its PSF, radiometry or readout binding.")
    return deepcopy({
        "source_binding": source,
        "native_cfa": checked["native_cfa"],
        "readout": checked["readout"],
        "raw_format": checked["raw_format"],
        "geometry": checked["geometry"],
        "lens_model": preview["lens_model"],
        "sensor_model": preview["sensor_model"],
        "scene": preview["scene"],
        "capture": preview["capture"],
    })


def reusable_calibration_binding(
    source_report: dict[str, Any], checked: dict[str, Any]
) -> dict[str, Any]:
    """Pin a model and operating point without pinning one target scene or RAW.

    The simulated native geometry is deliberately exact: it is not a known
    product-die coordinate system, so a grid cannot be silently transferred to
    another output size or crop. Exposure and noise mode remain exact because
    the supplied noise model has no independently verified operating domain.
    """
    full = calibration_binding(source_report, checked)
    source, capture, scene = full["source_binding"], full["capture"], full["scene"]
    illuminant = (
        "D65" if scene.get("kind") in {"uniform_d65", "slanted_edge_d65"} else "unknown"
    )
    return deepcopy({
        "lens_identity": source["lens_identity"],
        "lens_scope": source["lens_scope"],
        "lens_field_psf_selection": source["lens_field_psf_selection"],
        "saved_sensor_revision": source["saved_sensor_revision"],
        "lens_radiometry": source["lens_radiometry"],
        "physical_validation": source["physical_validation"],
        "native_cfa": full["native_cfa"],
        "readout": full["readout"],
        "raw_format": full["raw_format"],
        "geometry": full["geometry"],
        "lens_model": full["lens_model"],
        "sensor_model": full["sensor_model"],
        "scene_model": {
            "illuminant_family": illuminant,
            "wavelength_nm": scene.get("wavelength_nm"),
            "horizontal_fov_deg": scene.get("horizontal_fov_deg"),
            "object_distance_m": scene.get("object_distance_m"),
            "angular_grid_contract": scene.get("angular_grid_contract"),
        },
        "operating_point": {
            "exposure_s": capture["exposure_s"],
            "noise_flag": capture["noise_flag"],
        },
    })


def _validate_calibration(
    record: dict[str, Any], *, expected_binding: dict[str, Any] | None = None,
    target_source_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _exact(record, {"schema", "provenance", "binding", "payload"}, "Lens ISP calibration")
    if record["schema"] not in {CALIBRATION_SCHEMA, REUSABLE_CALIBRATION_SCHEMA}:
        raise ValueError("Saved-quality or other calibration schema cannot bind Lens RAW.")
    provenance = _exact(record["provenance"], _PROVENANCE_KEYS, "calibration provenance")
    if provenance["source_kind"] not in {"estimated", "simulated", "vendor", "measured"}:
        raise ValueError("Unsupported Lens RAW calibration source kind.")
    for key in ("source_uri", "method_and_version", "source_dataset_id"):
        _text(provenance[key], f"calibration provenance {key}")
    dataset_sha = _sha(provenance["source_dataset_sha256"], "source_dataset_sha256")
    if provenance["target_raw_used_for_fitting"] is not False:
        raise ValueError("Calibration fitted to the target Lens RAW is unsupported.")
    uncertainty = _exact(provenance["uncertainty"], {"kind", "reason"}, "uncertainty")
    if uncertainty["kind"] != "unknown":
        raise ValueError("Lens RAW calibration uncertainty must remain unknown.")
    _text(uncertainty["reason"], "uncertainty reason")
    reusable = record["schema"] == REUSABLE_CALIBRATION_SCHEMA
    binding = _exact(
        record["binding"],
        _REUSABLE_BINDING_KEYS if reusable else _BINDING_KEYS,
        "Lens RAW calibration binding",
    )
    source = (
        binding if reusable
        else _exact(binding["source_binding"], _SOURCE_BINDING_KEYS, "source binding")
    )
    for key in (
        "native_cfa", "readout", "raw_format", "geometry", "lens_model", "sensor_model",
        *(("operating_point",) if reusable else ("scene", "capture")),
    ):
        if not isinstance(binding[key], dict):
            raise ValueError(f"Lens RAW calibration {key} must be an object.")
    if not isinstance(source["lens_identity"], dict) or not isinstance(
        source["saved_sensor_revision"], dict
    ):
        raise ValueError("Lens RAW source revisions must be explicit objects.")
    target = target_source_binding if reusable else source
    if target is not None:
        _exact(target, _SOURCE_BINDING_KEYS, "target source binding")
        report_sha = _sha(target["source_report_sha256"], "source_report_sha256")
        artifact_sha = _sha(target["source_raw_artifact_sha256"], "source_raw_artifact_sha256")
        raw_content = target["source_raw_content_sha256"]
        if not isinstance(raw_content, str) or not raw_content.startswith("sha256:"):
            raise ValueError("source_raw_content_sha256 requires sha256:<hex>.")
        raw_sha = _sha(raw_content.removeprefix("sha256:"), "source_raw_content_sha256")
        if dataset_sha in {report_sha, artifact_sha, raw_sha}:
            raise ValueError("Calibration dataset cannot be the target Lens RAW or its artifacts.")
        if provenance["source_dataset_id"] == target["source_lens_sensor_job_id"]:
            raise ValueError("Calibration dataset cannot be the target Lens RAW job.")
    if (
        source["lens_radiometry"] != "assumed_unity_not_validated"
        or source["physical_validation"] != "not_established"
        or binding["lens_model"].get("absolute_radiometry")
        != "unknown_unity_transmission_assumption"
        or binding["lens_model"].get("relative_illumination")
        != "unknown_unity_assumption"
        or binding["lens_model"].get("field_psf_selection")
        != source["lens_field_psf_selection"]
        or binding["sensor_model"].get("effective_readout") != binding["readout"]
    ):
        raise ValueError("Lens calibration lacks its approximate optics/readout contract.")
    if reusable:
        spectral = _exact(
            binding["scene_model"],
            {
                "illuminant_family", "wavelength_nm", "horizontal_fov_deg",
                "object_distance_m", "angular_grid_contract",
            },
            "scene_model",
        )
        wavelengths = spectral["wavelength_nm"]
        object_distance = spectral["object_distance_m"]
        operating = _exact(
            binding["operating_point"], {"exposure_s", "noise_flag"}, "operating_point"
        )
        exposure = operating["exposure_s"]
        if (
            isinstance(exposure, bool) or not isinstance(exposure, (int, float))
            or not np.isfinite(exposure) or exposure <= 0
            or type(operating["noise_flag"]) is not int
            or operating["noise_flag"] not in {0, 2}
            or binding["lens_model"].get("field_psf_selection")
            != binding["lens_field_psf_selection"]
            or spectral["illuminant_family"] != "D65"
            or not isinstance(wavelengths, list) or len(wavelengths) < 1
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not np.isfinite(value) or value <= 0
                for value in wavelengths
            )
            or any(left >= right for left, right in zip(wavelengths, wavelengths[1:], strict=False))
            or isinstance(spectral["horizontal_fov_deg"], bool)
            or not isinstance(spectral["horizontal_fov_deg"], (int, float))
            or not np.isfinite(spectral["horizontal_fov_deg"])
            or spectral["horizontal_fov_deg"] <= 0
            or not (
                object_distance == "infinity"
                or (
                    not isinstance(object_distance, bool)
                    and isinstance(object_distance, (int, float))
                    and np.isfinite(object_distance) and object_distance > 0
                )
            )
            or (
                spectral["angular_grid_contract"] is not None
                and not isinstance(spectral["angular_grid_contract"], dict)
            )
        ):
            raise ValueError("Reusable Lens calibration lacks an exact operating point or PSF.")
    if expected_binding is not None and binding != expected_binding:
        raise ValueError("Calibration binding differs from the exact Lens RAW source.")
    return {
        "provenance": deepcopy(provenance),
        "payload": validate_numeric_payload(record["payload"]),
    }


def validate_lens_calibration_bytes(
    content: bytes, expected_sha256: str
) -> dict[str, Any]:
    """Bound and hash-check an imported declaration; source fit is checked later."""
    expected = _sha(expected_sha256, "calibration_sha256")
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_CALIBRATION_BYTES:
        raise ValueError("Lens ISP calibration JSON exceeds the bounded file size.")
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("Calibration file differs from the submitted SHA-256.")
    try:
        record = json.loads(
            content, object_pairs_hook=_no_duplicate_pairs, parse_constant=_reject_nonfinite
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Calibration file is not valid UTF-8 JSON.") from exc
    if not isinstance(record, dict):
        raise ValueError("Calibration JSON must be an object.")
    _validate_calibration(record)
    return record


def _read_calibration(path: str | Path, expected_sha256: str) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not 0 < source.stat().st_size <= MAX_CALIBRATION_BYTES:
        raise ValueError("Lens ISP calibration JSON exceeds the bounded file size.")
    return validate_lens_calibration_bytes(source.read_bytes(), expected_sha256)


def evaluate_lens_sensor_calibrated_isp(
    source_report_path: str | Path,
    source_raw_path: str | Path,
    *,
    source_lens_sensor_job_id: str,
    sensor_db_root: str | Path,
    calibration_path: str | Path,
    calibration_sha256: str,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Apply supplied numeric LSC/noise to one pinned RAW, without files or capture."""
    calibration_sha256 = _sha(calibration_sha256, "calibration_sha256")
    candidate_profile = _candidate_profile(profile, calibration_sha256)
    checked = inspect_lens_sensor_isp_input(
        source_report_path, source_raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
        sensor_db_root=sensor_db_root, profile=isp_v2.default_profile(),
    )
    raw = checked["raw"]
    original_hash = checked["raw_content_sha256"]
    if original_hash != checked["source_binding"]["source_raw_content_sha256"]:
        raise ValueError("Retained Lens RAW content differs from its source binding.")
    report_path = Path(source_report_path).expanduser().resolve()
    raw_path = Path(source_raw_path).expanduser().resolve()
    source_report = json.loads(report_path.read_bytes())
    expected_binding = calibration_binding(source_report, checked)
    record = _read_calibration(calibration_path, calibration_sha256)
    applicability_binding = (
        reusable_calibration_binding(source_report, checked)
        if record["schema"] == REUSABLE_CALIBRATION_SCHEMA else expected_binding
    )
    calibration = _validate_calibration(
        record, expected_binding=applicability_binding,
        target_source_binding=checked["source_binding"],
    )
    if (
        candidate_profile["raw_denoise"]["mode"] != "bypass"
        and candidate_profile["raw_denoise"]["strength"] > 0
        and expected_binding["capture"]["noise_flag"] != 2
    ):
        raise ValueError("Active noise calibration needs a stochastic source RAW capture.")
    isp_v2.validate_inputs(
        raw, checked["raw_format"], candidate_profile,
        geometry=checked["geometry"], calibration=calibration["payload"],
    )
    raw.setflags(write=False)
    reference = isp_v2.process_raw(
        raw, checked["raw_format"], isp_v2.default_profile(), geometry=checked["geometry"]
    )
    candidate = isp_v2.process_raw(
        raw, checked["raw_format"], candidate_profile,
        geometry=checked["geometry"], calibration=calibration["payload"],
    )
    if (
        _sha256_array(raw) != original_hash
        or _sha256(report_path) != expected_binding["source_binding"]["source_report_sha256"]
        or _sha256(raw_path) != expected_binding["source_binding"]["source_raw_artifact_sha256"]
        or _sha256(Path(calibration_path).expanduser().resolve()) != calibration_sha256
    ):
        raise ValueError("Lens RAW, source report or calibration changed during ISP replay.")
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
        "native_cfa_contract": checked["native_cfa_contract"],
        "calibration": {
            "schema": record["schema"],
            "file_sha256": calibration_sha256,
            "numeric_sha256": candidate["summary"]["calibration_numeric_sha256"],
            "applicability_binding": applicability_binding,
            "provenance": calibration["provenance"],
            "provenance_verification": "declared_only_not_independently_verified",
            "applicability_binding_sha256": hashlib.sha256(
                json.dumps(applicability_binding, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode()
            ).hexdigest(),
        },
        "raw_content_sha256": original_hash,
        "raw_values_byte_equal": True,
        "geometry_interpretation": (
            "Full executed approximate Lens × Sensor mosaic at origin (0,0); "
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
                "Declared numeric ISP effects on one retained approximate Lens RAW, "
                "not measured improvement or calibrated uncertainty."
            ),
        },
        "limitations": [
            "Calibration source and independence are supplier declarations only.",
            "Lens transmission and relative illumination were assumed unity in the source RAW.",
            "LSC sensitivity does not establish correction of real lens shading.",
            "Noise coefficients are variance hints, not measured uncertainty bounds.",
            "No detector score, independent color accuracy or physical validation.",
        ],
    }
    json.dumps(summary, allow_nan=False)
    return {"summary": summary, "arrays": arrays}


def run_lens_sensor_calibrated_isp(
    source_report_path: str | Path,
    source_raw_path: str | Path,
    *,
    source_lens_sensor_job_id: str,
    sensor_db_root: str | Path,
    calibration_path: str | Path,
    calibration_sha256: str,
    profile: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Any]:
    """Persist an exact-RAW diagnostic report and numeric NPZ evidence."""
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Lens calibrated ISP output_dir is not empty: {destination}")
    evaluated = evaluate_lens_sensor_calibrated_isp(
        source_report_path, source_raw_path,
        source_lens_sensor_job_id=source_lens_sensor_job_id,
        sensor_db_root=sensor_db_root,
        calibration_path=calibration_path,
        calibration_sha256=calibration_sha256,
        profile=profile,
    )
    destination.mkdir(parents=True, exist_ok=True)
    array_path = destination / "lens_sensor_calibrated_isp_arrays.npz"
    with array_path.open("wb") as stream:
        np.savez_compressed(stream, **evaluated["arrays"])
    with np.load(array_path, allow_pickle=False) as saved:
        if _sha256_array(saved["source_raw_dn_frame"]) != evaluated["summary"][
            "raw_content_sha256"
        ]:
            raise ValueError("Calibrated ISP artifact does not preserve source Lens RAW.")
    report = deepcopy(evaluated["summary"])
    report["artifacts"] = {"npz_name": array_path.name, "npz_sha256": _sha256(array_path)}
    report_path = destination / "lens_sensor_calibrated_isp_report.json"
    report_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "artifacts": {"json": str(report_path), "npz": str(array_path)},
        "artifact_sha256": {"json": _sha256(report_path), "npz": _sha256(array_path)},
    }
