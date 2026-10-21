"""Fail-closed intake for a saved Lens × Sensor detector replay cohort.

This checks actual saved detector outputs and exact RGB/RAW bindings.  A
declared split or supplied annotation cannot establish semantic correctness,
model provenance, or representative product performance by itself.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.task_perception import (
    dataset_mean_average_precision,
    task_perception_config,
)

from .lens_sensor_isp_detector import _WORKER, _metrics_for_annotations
from .lens_sensor_isp_detector_job import _source_job
from .lens_sensor_isp_detector_job import get as get_detector
from .lens_sensor_isp_perception import _document, _npz_array, lens_sensor_isp_image_binding
from .registry import IntegrityError, canonical, digest
from .saved_sensor_perception import _boxes, _identity, _label_set, _sha256

SCHEMA = "camerae2e_lens_sensor_detector_replay_qualification_v1"
SPLIT_SCHEMA = "camerae2e_lens_sensor_detector_scene_splits_v1"
REPORT_SCHEMA = "camerae2e_lens_sensor_detector_replay_qualification_report_v1"
_SPLITS = frozenset({"train", "validation", "test"})
_MAX_CASES = 64
_MAX_JSON_BYTES = 2 * 1024 * 1024


def _scene_identity(service: Any, raw_job: Mapping[str, Any]) -> dict[str, Any]:
    scene = raw_job["result"]["summary"]["preview"]["scene"]
    kind = scene.get("kind") if isinstance(scene, dict) else None
    if kind not in {"uniform_d65", "slanted_edge_d65", "registered_object_radiance"}:
        raise IntegrityError("Saved RAW has no supported scene family for detector replay.")
    asset = scene.get("source_asset")
    if kind == "registered_object_radiance":
        from .lens_sensor_radiance_scene import load_radiance_scene, source_identity

        if not isinstance(asset, dict) or not isinstance(asset.get("id"), str):
            raise IntegrityError("Registered scene lost its pinned source asset.")
        record, _ = load_radiance_scene(service, asset["id"])
        if source_identity(record) != asset:
            raise IntegrityError("Saved RAW scene differs from its registered radiance asset.")
        group_id = "source-sha256:" + asset["source_sha256"]
        return {
            "scene_kind": kind,
            "source_asset": asset,
            "scene_group_id": group_id,
            "source_frame_id": asset["id"],
        }
    if asset is not None:
        raise IntegrityError("Built-in scene unexpectedly claims a registered source asset.")
    return {
        "scene_kind": kind,
        "source_asset": None,
        "scene_group_id": "builtin:" + kind,
        "source_frame_id": "builtin:" + kind,
    }


def detector_replay_case_template(service: Any, detector_job_id: str) -> dict[str, Any]:
    """Make exact intake pins from one completed, sealed detector Job."""
    job = get_detector(service, _identity(detector_job_id, "detector_job_id"))
    if job.get("status") != "succeeded":
        raise ValueError("Choose a completed detector Job.")
    raw_job, isp_job, paths = _source_job(service, job["specification"])
    binding = lens_sensor_isp_image_binding(
        lens_raw_job=raw_job, lens_isp_job=isp_job, **paths
    )
    inference = job["result"]["summary"]["inference"]
    return {
        "detector_job_id": job["id"],
        "detector_result_sha256": digest(job["result"]),
        "inference_sha256": digest(inference),
        "image_binding_sha256": digest(binding),
        "scene_identity": _scene_identity(service, raw_job),
        "annotation_artifact_sha256": job["specification"]["annotation_artifact_sha256"],
        "split": "test",
    }


def detector_replay_intake_template(
    service: Any, detector_job_ids: list[str]
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "dataset_id": "declare-dataset-identity",
        "dataset_revision": "1",
        "split_assignment_artifact_sha256": "",
        "cases": [detector_replay_case_template(service, identity)
                  for identity in detector_job_ids],
    }


def _split_artifact(
    service: Any, checksum: str, *, dataset_id: str, dataset_revision: str,
    cases: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if checksum == "":
        return None
    if (
        not isinstance(checksum, str) or len(checksum) != 64
        or any(char not in "0123456789abcdef" for char in checksum)
    ):
        raise ValueError("split_assignment_artifact_sha256 requires lowercase SHA-256.")
    path = service.registry.blob_path(checksum)
    if _sha256(path, _MAX_JSON_BYTES) != checksum:
        raise IntegrityError("Scene split assignment artifact changed.")
    document = _document(path)
    if (
        set(document) != {
            "schema", "dataset_id", "dataset_revision", "source_uri",
            "method_and_version", "assignments",
        }
        or document["schema"] != SPLIT_SCHEMA
        or document["dataset_id"] != dataset_id
        or document["dataset_revision"] != dataset_revision
    ):
        raise ValueError("Scene split assignment schema or dataset identity differs.")
    _identity(document["source_uri"], "split.source_uri")
    _identity(document["method_and_version"], "split.method_and_version")
    rows = document["assignments"]
    if (
        not isinstance(rows, list) or len(rows) != len(cases)
        or any(not isinstance(row, dict) or set(row) != {"scene_group_id", "split"}
               for row in rows)
        or any(not isinstance(row["split"], str) or row["split"] not in _SPLITS
               for row in rows)
    ):
        raise ValueError("Scene split assignments require one exact row per case.")
    expected = sorted(
        (case["scene_identity"]["scene_group_id"], case["split"]) for case in cases
    )
    actual = sorted(
        (_identity(row["scene_group_id"], "scene_group_id"), row["split"])
        for row in rows
    )
    if actual != expected:
        raise ValueError("Scene split assignment differs from pinned detector cases.")
    return {"sha256": checksum, "source_uri": document["source_uri"],
            "method_and_version": document["method_and_version"]}


def _verified_replay(service: Any, case: dict[str, Any]) -> dict[str, Any]:
    job = get_detector(service, case["detector_job_id"])
    if job.get("status") != "succeeded":
        raise ValueError("Replay intake requires completed detector Jobs.")
    raw_job, isp_job, paths = _source_job(service, job["specification"])
    binding = lens_sensor_isp_image_binding(
        lens_raw_job=raw_job, lens_isp_job=isp_job, **paths
    )
    inference = job["result"]["summary"]["inference"]
    expected = detector_replay_case_template(service, job["id"])
    for key in expected:
        if key != "split" and case[key] != expected[key]:
            raise IntegrityError(f"Detector replay {key} differs from its saved source.")
    spec = job["specification"]
    detections_raw = inference.get("detections")
    inference_verification = inference.get("verification")
    if (
        inference.get("status") != "completed"
        or inference.get("image_binding") != binding
        or inference.get("model_provenance") != job["model_preflight"]
        or not isinstance(inference_verification, dict)
        or inference_verification.get("detector_inference")
        != "performed_by_offline_ultralytics_yolo11n"
        or inference.get("target_labels") != spec["target_labels"]
        or inference.get("score_threshold") != spec["score_threshold"]
        or not isinstance(detections_raw, list)
        or inference.get("detection_count") != len(detections_raw)
    ):
        raise IntegrityError("Saved detector inference protocol differs from frozen Job.")
    labels = spec["target_labels"]
    shape = tuple(binding["shape"][:2])
    detections = _boxes(detections_raw, set(labels), shape, predictions=True)
    class_ids = inference.get("model_class_ids")
    if (
        not isinstance(class_ids, dict) or set(class_ids) != set(labels)
        or any(type(value) is not int or not 0 <= value < 1000
               for value in class_ids.values())
        or len(set(class_ids.values())) != len(class_ids)
    ):
        raise IntegrityError("Saved detector class mapping differs from target labels.")
    rgb = _npz_array(
        paths["lens_isp_npz_path"], "candidate_rgb", tuple(binding["shape"]),
        binding["dtype"],
    )
    if hashlib.sha256(rgb.tobytes(order="C")).hexdigest() != binding["array_sha256"]:
        raise IntegrityError("Saved detector RGB array differs from its image binding.")
    rgb_uint8 = np.asarray(np.round(rgb * 255.0), dtype=np.uint8)
    preprocessing = inference.get("preprocessing")
    if (
        not isinstance(preprocessing, dict)
        or preprocessing.get("rgb_uint8_sha256")
        != hashlib.sha256(rgb_uint8.tobytes(order="C")).hexdigest()
        or preprocessing.get("model_input_bgr_uint8_sha256")
        != hashlib.sha256(np.ascontiguousarray(rgb_uint8[..., ::-1]).tobytes()).hexdigest()
        or preprocessing.get("source_shape") != binding["shape"]
        or preprocessing.get("source_dtype") != binding["dtype"]
        or preprocessing.get("input_encoding") != "srgb"
    ):
        raise IntegrityError("Saved detector output does not bind to exact ISP input bytes.")
    annotation_sha = spec["annotation_artifact_sha256"]
    if annotation_sha:
        path = service.registry.blob_path(annotation_sha)
        evaluated = _metrics_for_annotations(
            path, binding=binding, detections=detections, target_labels=labels,
            model_sha256=spec["model_weights_sha256"],
            iou_threshold=spec["iou_threshold"],
        )
        if inference.get("annotation_evaluation") != evaluated:
            raise IntegrityError("Saved detector annotation evaluation differs from labels.")
        annotation = _document(path)
        label_set = _label_set(annotation["label_set"])
        if label_set["labels"] != labels:
            raise IntegrityError("Saved annotation label set differs from detector classes.")
        truth = (
            [] if annotation["objects"] == [] else
            _boxes(annotation["objects"], set(labels), shape, predictions=False)
        )
        source = evaluated["ground_truth_source"]
        annotation_set = evaluated["annotation_set"]
    else:
        if inference.get("annotation_evaluation") is not None:
            raise IntegrityError("Unlabeled detector Job unexpectedly has evaluation metrics.")
        truth, source, label_set, annotation_set = [], None, None, None
    weight_path = Path.home() / ".cache/pyisetcam/task_perception/yolo/yolo11n.pt"
    weights_present = (
        weight_path.is_file() and weight_path.stat().st_size <= 16 * 1024 * 1024
        and hashlib.sha256(weight_path.read_bytes()).hexdigest()
        == spec["model_weights_sha256"]
    )
    worker_present = (
        _WORKER.is_file()
        and hashlib.sha256(_WORKER.read_bytes()).hexdigest()
        == job["model_preflight"]["worker_sha256"]
    )
    return {
        "job": job,
        "binding": binding,
        "scene": case["scene_identity"],
        "truth": truth,
        "detections": detections,
        "annotation_source": source,
        "annotation_set": annotation_set,
        "label_set": label_set,
        "model_weights_present_with_saved_sha256": weights_present,
        "worker_present_with_saved_sha256": worker_present,
    }


def _test_annotation_cohort_issue(test_items: list[dict[str, Any]]) -> str | None:
    """Require one named label and annotation cohort before diagnostic AP."""
    if len({canonical(item["label_set"]) for item in test_items}) != 1:
        return "Test annotations use different label sets."
    if len({canonical(item["annotation_set"]) for item in test_items}) != 1:
        return "Test annotations use different annotation sets."
    if len({item["annotation_source"]["source_dataset_id"]
            for item in test_items}) != 1:
        return "Test annotations come from different source datasets."
    return None


def qualify_saved_detector_replay(service: Any, intake: dict[str, Any]) -> dict[str, Any]:
    """Audit exact Jobs and scene splits; never release product mAP from declarations."""
    if not isinstance(intake, dict) or set(intake) != {
        "schema", "dataset_id", "dataset_revision", "split_assignment_artifact_sha256",
        "cases",
    } or intake["schema"] != SCHEMA:
        raise ValueError("Detector replay qualification intake schema differs.")
    dataset_id = _identity(intake["dataset_id"], "dataset_id")
    revision = _identity(intake["dataset_revision"], "dataset_revision")
    cases = intake["cases"]
    if (
        not isinstance(cases, list) or not 1 <= len(cases) <= _MAX_CASES
        or any(not isinstance(case, dict) or set(case) != {
            "detector_job_id", "detector_result_sha256", "inference_sha256",
            "image_binding_sha256", "scene_identity",
            "annotation_artifact_sha256", "split",
        } for case in cases)
    ):
        raise ValueError("Declare 1..64 exact detector replay cases.")
    if any(not isinstance(case["split"], str) or case["split"] not in _SPLITS
           for case in cases):
        raise ValueError("Replay split must be train, validation or test.")
    if any(not isinstance(case["detector_job_id"], str)
           or not case["detector_job_id"].strip() for case in cases):
        raise ValueError("Replay case detector Job ID is missing.")
    if len({case["detector_job_id"] for case in cases}) != len(cases):
        raise ValueError("Detector replay Job IDs must be unique.")
    verified = [_verified_replay(service, case) for case in cases]
    split_evidence = _split_artifact(
        service, intake["split_assignment_artifact_sha256"],
        dataset_id=dataset_id, dataset_revision=revision, cases=cases,
    )
    model_hashes = {
        item["job"]["specification"]["model_weights_sha256"] for item in verified
    }
    if len(model_hashes) != 1:
        raise ValueError("Replay cohort must use one exact detector model weights SHA.")
    label_names = {tuple(item["job"]["specification"]["target_labels"])
                   for item in verified}
    if len(label_names) != 1:
        raise ValueError("Replay cohort must use one exact detector class list.")
    groups: dict[str, set[str]] = {}
    for case in cases:
        groups.setdefault(case["scene_identity"]["scene_group_id"], set()).add(
            case["split"]
        )
    if any(len(splits) > 1 for splits in groups.values()):
        raise ValueError("One scene source appears in multiple train/validation/test splits.")
    if len({item["binding"]["array_sha256"] for item in verified}) != len(verified):
        raise ValueError("Replay cohort duplicates an exact ISP image.")
    if len({item["binding"]["source_raw_content_sha256"] for item in verified}) != len(
        verified
    ):
        raise ValueError("Replay cohort duplicates an exact RAW image.")
    reasons = [
        "annotation_correctness_not_independently_reviewed",
        "model_weight_origin_not_authenticated",
        "model_training_split_lineage_not_verified",
        "dataset_representativeness_not_established",
        "physical_camera_validation_not_established",
    ]
    if dataset_id == "declare-dataset-identity":
        reasons.append("dataset_identity_placeholder")
    if split_evidence is None:
        reasons.append("split_assignment_artifact_missing")
    if any(item["scene"]["source_asset"] is None for item in verified):
        reasons.append("independent_scene_asset_missing")
    elif any(item["scene"]["source_asset"] is not None for item in verified):
        reasons.append("scene_source_semantics_not_independently_verified")
    if any(item["annotation_source"] is None for item in verified):
        reasons.append("annotation_missing")
    if any(item["annotation_source"] is not None and item[
        "annotation_source"
    ]["source_kind"] == "synthetic_contract_fixture" for item in verified):
        reasons.append("semantic_object_ground_truth_missing")
    if any(not item["model_weights_present_with_saved_sha256"] for item in verified):
        reasons.append("saved_model_weights_not_present_now")
    if any(not item["worker_present_with_saved_sha256"] for item in verified):
        reasons.append("saved_detector_worker_not_present_now")
    test_items = [item for item, case in zip(verified, cases, strict=True)
                  if case["split"] == "test"]
    diagnostic: dict[str, Any] = {"available": False, "reason": ""}
    if len(test_items) < 2:
        diagnostic["reason"] = "At least two distinct test scenes are required."
    elif len({item["scene"]["scene_group_id"] for item in test_items}) != len(test_items):
        diagnostic["reason"] = "Repeated scene sources cannot count as independent images."
    elif any(item["annotation_source"] is None for item in test_items):
        diagnostic["reason"] = "Every test image needs a pinned annotation artifact."
    elif (cohort_issue := _test_annotation_cohort_issue(test_items)) is not None:
        diagnostic["reason"] = cohort_issue
    else:
        image_ids = [item["job"]["id"] for item in test_items]
        predictions = {identity: item["detections"]
                       for identity, item in zip(image_ids, test_items, strict=True)}
        truth = {identity: item["truth"]
                 for identity, item in zip(image_ids, test_items, strict=True)}
        try:
            value = dataset_mean_average_precision(
                predictions, truth,
                task_perception_config(score_threshold=0.0, max_detections=256),
                label_set=list(next(iter(label_names))),
                iou_thresholds=(0.5, 0.75),
            )
        except ValueError as exc:
            diagnostic["reason"] = str(exc)
        else:
            diagnostic = {
                "available": True, "value": value,
                "meaning": "saved_real_detector_output_against_supplied_labels_only",
            }
    public_cases = []
    for item, case in zip(verified, cases, strict=True):
        source = item["annotation_source"]
        public_cases.append({
            **case,
            "lens_raw_job_id": item["binding"]["lens_raw_job_id"],
            "lens_isp_job_id": item["binding"]["lens_isp_job_id"],
            "raw_content_sha256": item["binding"]["source_raw_content_sha256"],
            "rgb_array_sha256": item["binding"]["array_sha256"],
            "model_weights_sha256": item["job"]["specification"]["model_weights_sha256"],
            "model_weights_present_with_saved_sha256": item[
                "model_weights_present_with_saved_sha256"
            ],
            "worker_present_with_saved_sha256": item["worker_present_with_saved_sha256"],
            "detector_output_sha256": digest(item["job"]["result"]["summary"]["inference"]),
            "annotation_source_kind": None if source is None else source["source_kind"],
            "annotation_provenance": source,
            "annotation_set": item["annotation_set"],
            "ground_truth_box_count": len(item["truth"]),
            "prediction_box_count": len(item["detections"]),
        })
    return {
        "schema": REPORT_SCHEMA,
        "status": "audited",
        "dataset_id": dataset_id,
        "dataset_revision": revision,
        "intake_sha256": digest(intake),
        "split_assignment_artifact": split_evidence,
        "model_weights_sha256": next(iter(model_hashes)),
        "target_labels": list(next(iter(label_names))),
        "cases": public_cases,
        "diagnostic_map": diagnostic,
        "product_map": {"available": False, "blockers": reasons},
        "verification": {
            "raw_and_isp": "revalidated_by_source_jobs_and_array_bytes",
            "detector_output": "sealed_job_and_recomputed_rgb_input_hashes",
            "scene_group_split_overlap": "checked",
            "annotation": "sha256_pinned_and_single_frame_metrics_recomputed_when_supplied",
            "product_ground_truth": "not_established",
        },
    }
