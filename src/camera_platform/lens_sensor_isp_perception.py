"""Evaluate supplied boxes on one exact Lens × Sensor ISP RGB artifact.

This module never runs a detector or creates ground truth.  Annotation source
independence and model-to-prediction lineage are declarations, not verified
facts.  Matching on one tiny simulated frame is a diagnostic only.
"""

from __future__ import annotations

import hashlib
import json
import math
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.dataset import _sha256_array
from pyisetcam.task_perception import (
    annotations_to_bboxes,
    detection_metrics,
    task_model_config,
    task_perception_config,
)

from .lens_sensor_perception import _source as _lens_raw_source
from .registry import canonical, digest
from .saved_sensor_perception import (
    ANNOTATION_SCHEMA,
    MODEL_SCHEMA,
    PREDICTION_SCHEMA,
    _boxes,
    _extension,
    _finite_number,
    _identity,
    _label_set,
    _sha256,
)

SCHEMA = "camerae2e_lens_sensor_isp_prediction_evaluation_v1"
COORDINATES = "lens_sensor_isp_candidate_rgb_xyxy_half_open"
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_NPZ_BYTES = 64 * 1024 * 1024
_MAX_MODEL_BYTES = 128 * 1024 * 1024
_RAW_SHAPE_LIMIT = 32
_ISP_KINDS = {
    "lens_sensor_isp_replay": "camerae2e_lens_sensor_isp_replay_v1",
    "lens_sensor_calibrated_isp": "camerae2e_lens_sensor_calibrated_isp_v1",
}
_RGB_DTYPE_BY_KIND = {
    "lens_sensor_isp_replay": "float32",
    "lens_sensor_calibrated_isp": "float64",
}
_RESULT_NAMES = {
    "lens_sensor_isp_replay": "lens-sensor-isp-replay-result.json",
    "lens_sensor_calibrated_isp": "lens-sensor-calibrated-isp-result.json",
}
_ANNOTATION_SOURCE_KINDS = {
    "human_annotation", "independent_reference", "synthetic_contract_fixture",
}
_SYNTHETIC_FIXTURE_VERIFICATION = "semantic_ground_truth_absent_synthetic_contract_fixture"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate evidence JSON key: {key}.")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError(f"Evidence JSON contains nonfinite numeric data: {value}.")


def _document(path: Path) -> dict[str, Any]:
    _sha256(path, _MAX_JSON_BYTES)
    try:
        result = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_nonfinite,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Evidence is not valid UTF-8 JSON.") from exc
    if not isinstance(result, dict):
        raise ValueError("Evidence JSON must be an object.")
    return result


def _npz_array(path: Path, key: str, shape: tuple[int, ...], dtype: str) -> np.ndarray:
    _sha256(path, _MAX_NPZ_BYTES)
    member_name = f"{key}.npy"
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("ISP NPZ contains duplicate array members.")
            member = archive.getinfo(member_name)
            if member.file_size > math.prod(shape) * np.dtype(dtype).itemsize + 4096:
                raise ValueError("ISP array exceeds its bounded shape.")
            with archive.open(member) as stream:
                if np.lib.format.read_magic(stream) != (1, 0):
                    raise ValueError("ISP array has an unsupported NPY version.")
                stored_shape, fortran, stored_dtype = np.lib.format.read_array_header_1_0(
                    stream
                )
                if stored_shape != shape or fortran or stored_dtype != np.dtype(dtype):
                    raise ValueError("ISP array header differs from its frame contract.")
        with np.load(path, allow_pickle=False) as arrays:
            result = np.asarray(arrays[key]).copy()
    except (KeyError, zipfile.BadZipFile, EOFError) as exc:
        raise ValueError("ISP NPZ lacks a valid bounded frame array.") from exc
    if result.shape != shape or result.dtype != np.dtype(dtype):
        raise ValueError("ISP frame differs from its declared shape or dtype.")
    return result


def lens_sensor_isp_image_binding(
    *,
    lens_raw_job: Mapping[str, Any],
    lens_raw_report_path: str | Path,
    lens_raw_path: str | Path,
    lens_isp_job: Mapping[str, Any],
    lens_isp_result_path: str | Path,
    lens_isp_report_path: str | Path,
    lens_isp_npz_path: str | Path,
) -> dict[str, Any]:
    """Pin candidate RGB pixels to a completed Lens RAW and ISP job pair."""
    raw_report_path = Path(lens_raw_report_path).expanduser().resolve()
    raw_path = Path(lens_raw_path).expanduser().resolve()
    isp_result_path = Path(lens_isp_result_path).expanduser().resolve()
    isp_report_path = Path(lens_isp_report_path).expanduser().resolve()
    isp_npz_path = Path(lens_isp_npz_path).expanduser().resolve()
    source, _ = _lens_raw_source(lens_raw_job, raw_report_path, raw_path)
    raw, raw_binding = source["raw"], source["binding"]
    if (
        not isinstance(lens_isp_job, Mapping)
        or lens_isp_job.get("job_kind") not in _ISP_KINDS
        or lens_isp_job.get("status") != "succeeded"
        or not isinstance(lens_isp_job.get("id"), str)
        or not lens_isp_job["id"].strip()
    ):
        raise ValueError("Select one succeeded Lens × Sensor ISP job.")
    kind = lens_isp_job["job_kind"]
    result = lens_isp_job.get("result") or {}
    summary = result.get("summary") if isinstance(result, Mapping) else None
    descriptors = lens_isp_job.get("artifacts")
    if (
        not isinstance(summary, dict)
        or not isinstance(descriptors, list)
        or len(descriptors) != 3
        or result.get("artifacts") != descriptors
        or not isinstance(summary.get("evidence"), dict)
    ):
        raise ValueError("Completed Lens ISP job lacks its three evidence artifacts.")
    evidence = summary["evidence"]
    if (
        not all(evidence.get(key) in descriptors for key in ("json", "npz"))
        or len({item.get("sha256") for item in descriptors}) != 3
    ):
        raise ValueError("Lens ISP job evidence descriptors are incomplete.")
    result_descriptor = next(
        (item for item in descriptors if item.get("name") == _RESULT_NAMES[kind]), None
    )
    if result_descriptor is None:
        raise ValueError("Lens ISP job has no matching result artifact.")
    isp_report_sha = _sha256(isp_report_path, _MAX_JSON_BYTES)
    isp_npz_sha = _sha256(isp_npz_path, _MAX_NPZ_BYTES)
    isp_result_sha = _sha256(isp_result_path, _MAX_JSON_BYTES)
    result_bytes = canonical(summary).encode()
    if (
        evidence["json"].get("sha256") != isp_report_sha
        or evidence["json"].get("bytes") != isp_report_path.stat().st_size
        or evidence["npz"].get("sha256") != isp_npz_sha
        or evidence["npz"].get("bytes") != isp_npz_path.stat().st_size
        or result_descriptor.get("sha256") != hashlib.sha256(result_bytes).hexdigest()
        or result_descriptor.get("sha256") != isp_result_sha
        or result_descriptor.get("bytes") != len(result_bytes)
        or isp_result_path.read_bytes() != result_bytes
    ):
        raise ValueError("Lens ISP job artifact hashes differ from supplied files.")
    source_artifacts = lens_isp_job.get("source_artifacts") or {}
    job_binding = lens_isp_job.get("source_binding") or {}
    selected_binding = (
        job_binding.get("source_binding") if kind == "lens_sensor_calibrated_isp"
        else job_binding
    )
    if (
        selected_binding != raw_binding
        or source_artifacts.get("json", {}).get("sha256")
        != raw_binding["source_report_sha256"]
        or source_artifacts.get("raw", {}).get("sha256")
        != raw_binding["source_raw_artifact_sha256"]
        or lens_isp_job.get("source_capture_result_sha256") != digest(lens_raw_job["result"])
        or summary.get("source_capture_result_sha256")
        != lens_isp_job["source_capture_result_sha256"]
        or summary.get("source_binding") != job_binding
        or summary.get("raw_content_sha256") != raw_binding["source_raw_content_sha256"]
        or summary.get("raw_values_byte_equal") is not True
        or summary.get("specification") != lens_isp_job.get("specification")
    ):
        raise ValueError("Lens RAW and ISP jobs do not describe the same exact frame.")
    report = _document(isp_report_path)
    if (
        report.get("schema") != _ISP_KINDS[kind]
        or report.get("status") != "completed"
        or report.get("artifacts", {}).get("npz_sha256") != isp_npz_sha
        or report.get("source_binding") != job_binding
        or report.get("raw_content_sha256") != raw_binding["source_raw_content_sha256"]
        or report.get("raw_values_byte_equal") is not True
        or report.get("candidate_isp") != summary.get("candidate_isp")
        or report.get("reference_isp") != summary.get("reference_isp")
    ):
        raise ValueError("Lens ISP report differs from the completed job summary.")
    if kind == "lens_sensor_calibrated_isp" and (
        report.get("calibration") != summary.get("calibration")
        or report.get("calibration", {}).get("file_sha256")
        != lens_isp_job.get("specification", {}).get("calibration_sha256")
    ):
        raise ValueError("Calibrated Lens ISP report differs from its pinned calibration.")
    tone_curve = report.get("candidate_isp", {}).get("profile", {}).get(
        "tone", {}
    ).get("curve")
    if tone_curve not in {"srgb", "linear"}:
        raise ValueError("Lens ISP candidate RGB has an unsupported tone encoding.")
    shape = tuple(raw.shape)
    if (
        len(shape) != 2
        or any(length < 8 or length > _RAW_SHAPE_LIMIT or length % 2 for length in shape)
    ):
        raise ValueError("Lens ISP output shape is outside the bounded sensor frame.")
    retained = _npz_array(isp_npz_path, "source_raw_dn_frame", shape, "int32")
    if (
        retained.tobytes(order="C") != raw.tobytes(order="C")
        or _sha256_array(retained) != raw_binding["source_raw_content_sha256"]
    ):
        raise ValueError("Lens ISP NPZ does not retain the exact source RAW.")
    rgb_dtype = _RGB_DTYPE_BY_KIND[kind]
    rgb = _npz_array(isp_npz_path, "candidate_rgb", (*shape, 3), rgb_dtype)
    if not np.all(np.isfinite(rgb)) or np.any(rgb < 0) or np.any(rgb > 1):
        raise ValueError("Lens ISP candidate RGB has nonfinite or out-of-range samples.")
    if kind == "lens_sensor_isp_replay":
        consumer = report.get("candidate_rgb_consumer") or {}
        if (
            consumer.get("array_key") != "candidate_rgb"
            or consumer.get("array_dtype") != "float32"
            or consumer.get("channel_order") != "RGB"
            or consumer.get("source_binding") != raw_binding
            or consumer.get("detector_evaluation_performed") is not False
            or summary.get("candidate_rgb_consumer") != consumer
        ):
            raise ValueError("Lens ISP candidate RGB consumer contract differs.")
    return {
        "lens_raw_job_id": _identity(lens_raw_job["id"], "lens_raw_job_id"),
        "lens_isp_job_id": _identity(lens_isp_job["id"], "lens_isp_job_id"),
        "lens_isp_job_kind": kind,
        "lens_raw_result_sha256": digest(lens_raw_job["result"]),
        "lens_isp_result_sha256": digest(result),
        "source_raw_content_sha256": raw_binding["source_raw_content_sha256"],
        "source_binding": raw_binding,
        "isp_report_sha256": isp_report_sha,
        "isp_npz_sha256": isp_npz_sha,
        "array_key": "candidate_rgb",
        "array_sha256": hashlib.sha256(rgb.tobytes(order="C")).hexdigest(),
        "shape": list(rgb.shape),
        "dtype": rgb_dtype,
        "channel_order": "RGB",
        "tone_curve": tone_curve,
        "color_encoding": "srgb" if tone_curve == "srgb" else "linear_rgb",
        "origin_row_column": [0, 0],
        "coordinate_system": COORDINATES,
        "image_meaning": "single_simulated_isp_output_not_detector_input_validated",
    }


def _annotation_independence(
    annotations: dict[str, Any], *, binding: dict[str, Any], forbidden_sha: set[str]
) -> dict[str, Any]:
    provenance = annotations.get("ground_truth_provenance")
    keys = {
        "source_kind", "source_uri", "source_dataset_id", "source_dataset_sha256",
        "method_and_version", "predictions_used_for_annotation", "uncertainty",
    }
    if not isinstance(provenance, dict) or set(provenance) != keys:
        raise ValueError("Annotation ground_truth_provenance is required and exact.")
    if provenance["source_kind"] not in _ANNOTATION_SOURCE_KINDS:
        raise ValueError("Annotation source_kind is unsupported.")
    for key in ("source_uri", "source_dataset_id", "method_and_version"):
        _identity(provenance[key], f"ground_truth_provenance.{key}")
    dataset_sha = provenance["source_dataset_sha256"]
    if (
        not isinstance(dataset_sha, str)
        or len(dataset_sha) != 64
        or any(letter not in "0123456789abcdef" for letter in dataset_sha)
        or dataset_sha in forbidden_sha
    ):
        raise ValueError("Annotation source dataset SHA must differ from target artifacts.")
    if provenance["source_dataset_id"] in {
        binding["lens_raw_job_id"], binding["lens_isp_job_id"]
    }:
        raise ValueError("Annotation source dataset cannot be the target job.")
    if provenance["predictions_used_for_annotation"] is not False:
        raise ValueError("Annotations derived from supplied predictions are unsupported.")
    uncertainty = provenance["uncertainty"]
    if (
        not isinstance(uncertainty, dict)
        or set(uncertainty) != {"kind", "reason"}
        or uncertainty["kind"] != "unknown"
    ):
        raise ValueError("Annotation uncertainty must be explicitly unknown.")
    _identity(uncertainty["reason"], "ground_truth_provenance.uncertainty.reason")
    return provenance


def evaluate_lens_sensor_isp_predictions(
    *,
    lens_raw_job: Mapping[str, Any],
    lens_raw_report_path: str | Path,
    lens_raw_path: str | Path,
    lens_isp_job: Mapping[str, Any],
    lens_isp_result_path: str | Path,
    lens_isp_report_path: str | Path,
    lens_isp_npz_path: str | Path,
    annotation_path: str | Path,
    prediction_path: str | Path,
    model_manifest_path: str | Path,
    model_artifact_path: str | Path | None = None,
    iou_threshold: float = 0.5,
) -> dict[str, Any]:
    """Match provided boxes only; no detector, mAP, or product validation."""
    iou_threshold = _finite_number(iou_threshold, "iou_threshold")
    if not 0.1 <= iou_threshold <= 0.95:
        raise ValueError("iou_threshold must be finite in [0.1, 0.95].")
    binding = lens_sensor_isp_image_binding(
        lens_raw_job=lens_raw_job,
        lens_raw_report_path=lens_raw_report_path,
        lens_raw_path=lens_raw_path,
        lens_isp_job=lens_isp_job,
        lens_isp_result_path=lens_isp_result_path,
        lens_isp_report_path=lens_isp_report_path,
        lens_isp_npz_path=lens_isp_npz_path,
    )
    annotation_path, prediction_path, manifest_path = (
        Path(annotation_path), Path(prediction_path), Path(model_manifest_path)
    )
    model_path = None if model_artifact_path is None else Path(model_artifact_path)
    annotation_sha = _sha256(annotation_path, _MAX_JSON_BYTES)
    prediction_sha = _sha256(prediction_path, _MAX_JSON_BYTES)
    manifest_sha = _sha256(manifest_path, _MAX_JSON_BYTES)
    model_sha_verified = None if model_path is None else _sha256(model_path, _MAX_MODEL_BYTES)
    annotations, predictions, manifest = (
        _document(annotation_path), _document(prediction_path), _document(manifest_path)
    )
    if (
        annotations.get("schema") != ANNOTATION_SCHEMA
        or predictions.get("schema") != PREDICTION_SCHEMA
        or manifest.get("schema") != MODEL_SCHEMA
    ):
        raise ValueError("Detection annotation, prediction or model schema is unsupported.")
    if annotations.get("image_binding") != binding or predictions.get("image_binding") != binding:
        raise ValueError("Detection boxes do not bind to the exact Lens ISP RGB frame.")
    model_sha = manifest.get("checkpoint_sha256")
    if (
        not isinstance(model_sha, str)
        or len(model_sha) != 64
        or any(letter not in "0123456789abcdef" for letter in model_sha)
    ):
        raise ValueError("Model manifest requires a lowercase checkpoint SHA-256.")
    if model_sha_verified is not None and model_sha_verified != model_sha:
        raise ValueError("Model artifact differs from its manifest checksum.")
    if (
        predictions.get("model_manifest_sha256") != manifest_sha
        or predictions.get("model_artifact_sha256") != model_sha
    ):
        raise ValueError("Prediction artifact has a different model manifest or checkpoint.")
    ground_truth = _annotation_independence(
        annotations, binding=binding,
        forbidden_sha={
            prediction_sha, manifest_sha, model_sha, annotation_sha,
            binding["isp_report_sha256"], binding["isp_npz_sha256"],
            binding["array_sha256"],
            binding["source_binding"]["source_report_sha256"],
            binding["source_binding"]["source_raw_artifact_sha256"],
            binding["source_raw_content_sha256"].removeprefix("sha256:"),
        },
    )
    label_set = _label_set(annotations.get("label_set"))
    if _label_set(manifest.get("label_set")) != label_set:
        raise ValueError("Model and annotation label sets differ.")
    if predictions.get("label_set") != label_set:
        raise ValueError("Prediction and annotation label sets differ.")
    annotation_set = annotations.get("annotation_set")
    if not isinstance(annotation_set, dict) or set(annotation_set) != {"id", "version"}:
        raise ValueError("Annotation set requires id and version.")
    annotation_identity = {
        "id": _identity(annotation_set["id"], "annotation_set.id"),
        "version": _identity(annotation_set["version"], "annotation_set.version"),
    }
    backend = _identity(manifest.get("backend"), "model.backend")
    if backend not in {
        "external_predictions", "callable", "ultralytics_yolo",
        "torchvision_detection", "transformers_object_detection",
    }:
        raise ValueError("Model backend is not a supported detection extension point.")
    model_identity = {
        "id": _identity(manifest.get("id"), "model.id"),
        "version": _identity(manifest.get("version"), "model.version"),
        "backend": backend,
        "checkpoint_sha256": model_sha,
        "manifest_sha256": manifest_sha,
    }
    if backend != "external_predictions":
        task_model_config({
            "name": model_identity["id"], "backend": backend, "task": "detection",
            "model_id": str(model_path) if model_path is not None else model_identity["id"],
        })
    preprocessing = manifest.get("preprocessing")
    if not isinstance(preprocessing, dict) or any(
        preprocessing.get(key) != value for key, value in {
            "input_color_encoding": binding["color_encoding"],
            "input_shape": binding["shape"],
            "input_dtype": binding["dtype"],
            "channel_order": "RGB",
            "resize": "none",
        }.items()
    ):
        raise ValueError("Model preprocessing must bind exact ISP RGB encoding and grid.")
    inference_run_id = _identity(predictions.get("inference_run_id"), "inference_run_id")
    shape = (binding["shape"][0], binding["shape"][1])
    labels = set(label_set["labels"])
    truth = _boxes(annotations.get("objects"), labels, shape, predictions=False)
    estimated = _boxes(predictions.get("detections"), labels, shape, predictions=True)
    cfg = task_perception_config(
        iou_threshold=iou_threshold, score_threshold=0.0, max_detections=256
    )
    measured = detection_metrics(
        annotations_to_bboxes(estimated, bbox_format="xyxy", image_size=shape),
        annotations_to_bboxes(truth, bbox_format="xyxy", image_size=shape),
        cfg,
    )
    metric_names = (
        "true_positive", "false_positive", "false_negative", "precision", "recall", "f1"
    )
    per_label = {
        label: {name: metrics[name] for name in metric_names}
        for label, metrics in measured["per_label"].items()
    }
    fixture = ground_truth["source_kind"] == "synthetic_contract_fixture"
    independence = (
        _SYNTHETIC_FIXTURE_VERIFICATION if fixture
        else "supplier_declared_not_independently_verified"
    )
    limitations = [
        "The 8–32 pixel Lens simulation scene has no generated object ground truth.",
        "Annotation correctness and independence cannot be proven from supplied JSON.",
        "Model bytes do not prove the model generated these supplied predictions.",
        "Boxes use the ISP output RGB grid, not full product-die coordinates.",
        "Single-image box matching is not dataset mAP or product perception performance.",
    ]
    if fixture:
        limitations.append(
            "Synthetic contract fixture boxes have no verified semantic object truth."
        )
    return {
        "schema": SCHEMA,
        "status": "completed",
        "evaluation_kind": "supplied_prediction_artifact_single_simulated_image",
        "image_binding": binding,
        "annotation_provenance": {
            **annotation_identity,
            "artifact_sha256": annotation_sha,
            "ground_truth_source": ground_truth,
            "independence_verification": independence,
        },
        "prediction_provenance": {
            "inference_run_id": inference_run_id,
            "artifact_sha256": prediction_sha,
            "model_execution_verified": False,
        },
        "model_provenance": {
            **model_identity,
            "checkpoint_bytes_verified": model_sha_verified is not None,
            "label_set": label_set,
            "preprocessing_declared": preprocessing,
            "runtime_extension": _extension(backend),
        },
        "metrics": {
            "iou_threshold": measured["iou_threshold"],
            **{name: measured[name] for name in metric_names},
            "mean_matched_iou": measured["mean_matched_iou"],
            "per_label": per_label,
            "matches": measured["matches"],
            "match_index_basis": "pyisetcam predictions sorted by descending score",
            "dataset_map": {
                "available": False,
                "reason": "One supplied prediction artifact; no held-out dataset or detector run.",
            },
        },
        "verification": {
            "lens_raw_and_isp_artifacts": "sha256_and_array_content_verified",
            "annotation_and_prediction_artifacts": "sha256_pinned",
            "detector_inference": "not_performed",
            "ground_truth_independence": independence,
            "physical_validation": "not_established",
        },
        "limitations": limitations,
    }
