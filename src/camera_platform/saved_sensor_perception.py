"""Evaluate supplied detections against a pinned saved-sensor RGB frame.

The saved quality experiment has no object ground truth.  This bridge requires
user-provided annotations, predictions, a versioned label set and a hash-pinned
model identity.  It evaluates those *supplied predictions* with pyisetcam's
task-perception box matcher; it does not invoke a detector or claim dataset mAP.
"""

from __future__ import annotations

import hashlib
import json
import math
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import Request

from pyisetcam.task_perception import (
    annotations_to_bboxes,
    detection_metrics,
    task_model_config,
    task_perception_config,
)

QUALITY_SCHEMA = "camerae2e_saved_sensor_quality_v1"
ANNOTATION_SCHEMA = "camerae2e_detection_annotations_v1"
PREDICTION_SCHEMA = "camerae2e_detection_predictions_v1"
MODEL_SCHEMA = "camerae2e_detection_model_manifest_v1"
RESULT_SCHEMA = "camerae2e_saved_sensor_prediction_evaluation_v1"
COORDINATES = "quality_rgb_output_xyxy_half_open"
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_NPZ_BYTES = 64 * 1024 * 1024
_MAX_MODEL_BYTES = 128 * 1024 * 1024
_MAX_IMPORT_BODY_BYTES = 512 * 1024
_MAX_IMPORT_JSON_BYTES = 256 * 1024
JOB_KIND = "saved_sensor_perception"
JOB_SCHEMA = "camerae2e_saved_sensor_prediction_study_v1"
LIMITATION = (
    "This job evaluates user-supplied boxes against a pinned saved-quality RGB frame. "
    "It verifies supplied JSON artifact identities and checks model bytes only when "
    "separately registered. It does not execute "
    "the named detector, establish annotation correctness, compute dataset mAP, "
    "or establish physical-camera perception performance."
)


def _sha256(path: Path, limit: int) -> str:
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError(f"Evidence file is missing, empty or exceeds {limit} bytes: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    _sha256(path, _MAX_JSON_BYTES)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Evidence JSON is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Evidence JSON must be an object: {path}")
    return payload


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128:
        raise ValueError(f"{name} must be a nonempty versioned identifier.")
    return value.strip()


def _label_set(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"id", "version", "labels"}:
        raise ValueError("label_set requires id, version and labels.")
    names = value["labels"]
    if (
        not isinstance(names, list)
        or not 1 <= len(names) <= 64
        or any(not isinstance(name, str) or not 1 <= len(name.strip()) <= 128 for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("label_set.labels requires 1..64 unique nonempty names.")
    return {
        "id": _identity(value["id"], "label_set.id"),
        "version": _identity(value["version"], "label_set.version"),
        "labels": names,
    }


def _finite_number(value: Any, name: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be finite numeric data.")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be finite numeric data.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite numeric data.")
    return number


def _boxes(
    value: Any, labels: set[str], shape: tuple[int, int], *, predictions: bool
) -> list[dict[str, Any]]:
    limit = 256 if predictions else 128
    if not isinstance(value, list) or len(value) > limit or (not predictions and not value):
        raise ValueError(f"{'detections' if predictions else 'objects'} requires bounded boxes.")
    height, width = shape
    result = []
    for index, item in enumerate(value):
        required = {"bbox", "label", "score"} if predictions else {"bbox", "label"}
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError(f"Box {index} requires exactly {sorted(required)}.")
        if not isinstance(item["label"], str) or item["label"] not in labels:
            raise ValueError(f"Box {index} uses a label outside the declared label_set.")
        coords = item["bbox"]
        if not isinstance(coords, list) or len(coords) != 4:
            raise ValueError(f"Box {index} requires finite xyxy coordinates.")
        try:
            x1, y1, x2, y2 = (_finite_number(value, f"Box {index} coordinate") for value in coords)
        except ValueError as exc:
            raise ValueError(f"Box {index} requires finite xyxy coordinates.") from exc
        if not 0 <= x1 < x2 <= width or not 0 <= y1 < y2 <= height:
            raise ValueError(f"Box {index} exceeds the saved output image or is degenerate.")
        normalized: dict[str, Any] = {"bbox": [x1, y1, x2, y2], "label": item["label"]}
        if predictions:
            score = _finite_number(item["score"], f"Detection {index} score")
            if not 0 <= score <= 1:
                raise ValueError(f"Detection {index} requires a score in [0, 1].")
            normalized["score"] = float(score)
        result.append(normalized)
    return result


def saved_quality_image_binding(
    *,
    quality_report_path: str | Path,
    quality_npz_path: str | Path,
    quality_job_id: str,
    sensor_run_id: str,
    scene: str,
) -> dict[str, Any]:
    """Resolve one actual saved RGB frame and its output-pixel coordinate frame."""
    report_path, npz_path = Path(quality_report_path), Path(quality_npz_path)
    report_sha = _sha256(report_path, _MAX_JSON_BYTES)
    npz_sha = _sha256(npz_path, _MAX_NPZ_BYTES)
    report = _json(report_path)
    if report.get("schema") != QUALITY_SCHEMA or report.get("status") != "completed":
        raise ValueError("Source is not a completed saved sensor quality report.")
    if report.get("revision", {}).get("run_id") != sensor_run_id:
        raise ValueError("Saved quality report has a different sensor run ID.")
    if report.get("artifacts", {}).get("npz_sha256") != npz_sha:
        raise ValueError("Saved quality NPZ differs from its report binding.")
    cases = [case for case in report["cases"] if case.get("scene") == scene]
    if len(cases) != 1:
        raise ValueError("Select exactly one saved quality scene containing an RGB frame.")
    case = cases[0]
    array_key = f"{case['array_prefix']}_rgb_frame"
    shape = report["controls"]["output_size"]
    if (
        not isinstance(shape, list)
        or len(shape) != 2
        or any(type(value) is not int or not 16 <= value <= 64 for value in shape)
    ):
        raise ValueError("Saved quality output geometry is unsupported.")
    try:
        with zipfile.ZipFile(npz_path) as archive:
            member = archive.getinfo(f"{array_key}.npy")
            if member.file_size > 64 * 64 * 3 * 4 + 4096:
                raise ValueError("Saved quality RGB array exceeds bounded output storage.")
            with archive.open(member) as stream:
                if np.lib.format.read_magic(stream) != (1, 0):
                    raise ValueError("Saved quality RGB array has an unsupported NPY version.")
                stored_shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream)
                if (
                    stored_shape != (shape[0], shape[1], 3)
                    or fortran
                    or dtype != np.dtype("float32")
                ):
                    raise ValueError("Saved quality RGB array header differs from output geometry.")
    except (KeyError, zipfile.BadZipFile, EOFError) as exc:
        raise ValueError("Saved quality NPZ does not contain a valid RGB frame.") from exc
    with np.load(npz_path, allow_pickle=False) as arrays:
        if array_key not in arrays:
            raise ValueError("Saved quality scene has no fixed linear RGB frame.")
        image = arrays[array_key]
    if (
        image.dtype != np.dtype("float32")
        or image.shape != (shape[0], shape[1], 3)
        or not np.all(np.isfinite(image))
        or np.any(image < 0)
    ):
        raise ValueError("Saved quality RGB frame has invalid dtype, shape or values.")
    return {
        "quality_job_id": _identity(quality_job_id, "quality_job_id"),
        "sensor_run_id": _identity(sensor_run_id, "sensor_run_id"),
        "scene": scene,
        "source_report_sha256": report_sha,
        "source_npz_sha256": npz_sha,
        "array_key": array_key,
        "array_sha256": hashlib.sha256(image.tobytes(order="C")).hexdigest(),
        "shape": list(image.shape),
        "dtype": "float32",
        "coordinate_system": COORDINATES,
        "image_meaning": "first_sampled_fixed_linear_rgb_frame_not_display_normalized",
    }


def _extension(backend: str) -> dict[str, Any]:
    """State what the existing task-model adapter can safely do in a later job."""
    if backend == "ultralytics_yolo":
        return {
            "existing_adapter": "pyisetcam.task_perception.task_model_from_config",
            "local_checkpoint_path_possible": True,
            "this_job_inference_performed": False,
            "next_gate": (
                "Install optional backend and bind this exact checkpoint and preprocessing offline."
            ),
        }
    if backend == "callable":
        return {
            "existing_adapter": "pyisetcam.task_perception.task_model_from_config",
            "local_checkpoint_path_possible": False,
            "this_job_inference_performed": False,
            "next_gate": (
                "Register and verify a callable implementation against the model artifact."
            ),
        }
    if backend == "external_predictions":
        return {
            "existing_adapter": None,
            "local_checkpoint_path_possible": False,
            "this_job_inference_performed": False,
            "next_gate": "Select and validate a concrete offline model adapter before inference.",
        }
    return {
        "existing_adapter": "pyisetcam.task_perception.task_model_from_config",
        "local_checkpoint_path_possible": False,
        "this_job_inference_performed": False,
        "next_gate": (
            "Existing backend does not prove that this pinned checkpoint is loaded offline; "
            "supply a verified local loader before enabling inference."
        ),
    }


def evaluate_saved_sensor_predictions(
    *,
    quality_report_path: str | Path,
    quality_npz_path: str | Path,
    annotation_path: str | Path,
    prediction_path: str | Path,
    model_manifest_path: str | Path,
    model_artifact_path: str | Path | None = None,
    quality_job_id: str,
    sensor_run_id: str,
    scene: str,
    iou_threshold: float = 0.5,
) -> dict[str, Any]:
    """Evaluate explicit predictions; never run the named model or report mAP."""
    iou_threshold = _finite_number(iou_threshold, "iou_threshold")
    if not 0.1 <= iou_threshold <= 0.95:
        raise ValueError("iou_threshold must be a finite value in [0.1, 0.95].")
    binding = saved_quality_image_binding(
        quality_report_path=quality_report_path,
        quality_npz_path=quality_npz_path,
        quality_job_id=quality_job_id,
        sensor_run_id=sensor_run_id,
        scene=scene,
    )
    annotation_path = Path(annotation_path)
    prediction_path = Path(prediction_path)
    manifest_path = Path(model_manifest_path)
    model_path = None if model_artifact_path is None else Path(model_artifact_path)
    annotation_sha = _sha256(annotation_path, _MAX_JSON_BYTES)
    prediction_sha = _sha256(prediction_path, _MAX_JSON_BYTES)
    manifest_sha = _sha256(manifest_path, _MAX_JSON_BYTES)
    verified_model_sha = None if model_path is None else _sha256(model_path, _MAX_MODEL_BYTES)
    annotations, predictions, manifest = (
        _json(annotation_path),
        _json(prediction_path),
        _json(manifest_path),
    )
    if (
        annotations.get("schema") != ANNOTATION_SCHEMA
        or predictions.get("schema") != PREDICTION_SCHEMA
    ):
        raise ValueError("Annotation or prediction artifact schema is unsupported.")
    if manifest.get("schema") != MODEL_SCHEMA:
        raise ValueError("Model manifest schema is unsupported.")
    if annotations.get("image_binding") != binding or predictions.get("image_binding") != binding:
        raise ValueError("Annotation/prediction coordinates are not bound to this saved RGB frame.")
    model_sha = manifest.get("checkpoint_sha256")
    if (
        not isinstance(model_sha, str)
        or len(model_sha) != 64
        or any(letter not in "0123456789abcdef" for letter in model_sha)
    ):
        raise ValueError("Model manifest needs a declared lowercase checkpoint SHA-256.")
    if verified_model_sha is not None and model_sha != verified_model_sha:
        raise ValueError("Model artifact differs from its versioned manifest.")
    if predictions.get("model_manifest_sha256") != manifest_sha:
        raise ValueError("Prediction artifact does not name this model manifest.")
    if predictions.get("model_artifact_sha256") != model_sha:
        raise ValueError("Prediction artifact does not name this model artifact.")
    label_set = _label_set(annotations.get("label_set"))
    if _label_set(manifest.get("label_set")) != label_set:
        raise ValueError("Model and annotation label-set identities differ.")
    if predictions.get("label_set") != label_set:
        raise ValueError("Prediction label-set identity differs from annotation labels.")
    annotation_set = annotations.get("annotation_set")
    if not isinstance(annotation_set, dict) or set(annotation_set) != {"id", "version"}:
        raise ValueError("Annotation set requires id and version.")
    annotation_identity = {
        "id": _identity(annotation_set["id"], "annotation_set.id"),
        "version": _identity(annotation_set["version"], "annotation_set.version"),
    }
    model_identity = {
        "id": _identity(manifest.get("id"), "model.id"),
        "version": _identity(manifest.get("version"), "model.version"),
        "backend": _identity(manifest.get("backend"), "model.backend"),
        "checkpoint_sha256": model_sha,
        "manifest_sha256": manifest_sha,
    }
    if model_identity["backend"] not in {
        "external_predictions",
        "callable",
        "ultralytics_yolo",
        "torchvision_detection",
        "transformers_object_detection",
    }:
        raise ValueError("Model backend is not a supported detection extension point.")
    if model_identity["backend"] != "external_predictions":
        task_model_config(
            {
                "name": model_identity["id"],
                "backend": model_identity["backend"],
                "task": "detection",
                "model_id": str(model_path) if model_path is not None else model_identity["id"],
            }
        )
    _identity(predictions.get("inference_run_id"), "inference_run_id")
    if not isinstance(manifest.get("preprocessing"), dict) or not manifest["preprocessing"]:
        raise ValueError("Model manifest must declare nonempty preprocessing assumptions.")
    labels = set(label_set["labels"])
    shape = (binding["shape"][0], binding["shape"][1])
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
        "true_positive",
        "false_positive",
        "false_negative",
        "precision",
        "recall",
        "f1",
    )
    per_label = {
        label: {name: metrics[name] for name in metric_names}
        for label, metrics in measured["per_label"].items()
    }
    return {
        "schema": RESULT_SCHEMA,
        "status": "completed",
        "evaluation_kind": "supplied_prediction_artifact_single_image",
        "image_binding": binding,
        "annotation_provenance": {**annotation_identity, "artifact_sha256": annotation_sha},
        "prediction_provenance": {
            "inference_run_id": predictions["inference_run_id"],
            "artifact_sha256": prediction_sha,
            "model_execution_verified": False,
        },
        "model_provenance": {
            **model_identity,
            "checkpoint_bytes_verified": verified_model_sha is not None,
            "label_set": label_set,
            "preprocessing_declared": manifest["preprocessing"],
            "runtime_extension": _extension(model_identity["backend"]),
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
                "reason": (
                    "This job evaluates one user-annotated image with externally supplied "
                    "predictions; no held-out dataset or verified model inference was run."
                ),
            },
        },
        "limitations": [
            (
                "The model artifact hash proves file identity, not that it produced predictions."
                if verified_model_sha is not None
                else "Model checkpoint SHA-256 is user-declared; no checkpoint bytes were supplied."
            ),
            "Ground-truth annotation correctness is user-supplied and not independently verified.",
            "Boxes use the saved output RGB pixel grid; native RAW coordinates are unsupported.",
            "This is not detector inference, dataset mAP or physical-camera task validation.",
        ],
    }


def default_spec() -> dict[str, Any]:
    """Bounded job template; JSON evidence can be imported through this module's API."""
    return {
        "schema": JOB_SCHEMA,
        "name": "저장 센서 RGB 예측 평가",
        "quality_job_id": "",
        "sensor_run_id": "",
        "scene": "",
        "annotation_artifact_sha256": "",
        "prediction_artifact_sha256": "",
        "model_manifest_sha256": "",
        "model_artifact_sha256": "",
        "iou_threshold": 0.5,
        "acknowledge_external_predictions": False,
    }


def specification(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the saved sensor prediction study fields.")
    if payload["schema"] != JOB_SCHEMA or payload["acknowledge_external_predictions"] is not True:
        raise ValueError("Explicit external-prediction limitation acknowledgement required.")
    result = deepcopy(payload)
    result["name"] = _identity(payload["name"], "name")
    for key in ("quality_job_id", "sensor_run_id", "scene"):
        result[key] = _identity(payload[key], key)
    for key in (
        "annotation_artifact_sha256",
        "prediction_artifact_sha256",
        "model_manifest_sha256",
    ):
        value = payload[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(letter not in "0123456789abcdef" for letter in value)
        ):
            raise ValueError(f"{key} must be an existing lowercase SHA-256 artifact ID.")
    optional_model = payload["model_artifact_sha256"]
    if optional_model != "" and (
        not isinstance(optional_model, str)
        or len(optional_model) != 64
        or any(letter not in "0123456789abcdef" for letter in optional_model)
    ):
        raise ValueError("model_artifact_sha256 must be empty or a lowercase SHA-256 artifact ID.")
    threshold = _finite_number(payload["iou_threshold"], "iou_threshold")
    if not 0.1 <= threshold <= 0.95:
        raise ValueError("iou_threshold must be within [0.1, 0.95].")
    result["iou_threshold"] = threshold
    return result


def _source(service: Any, spec: dict[str, Any], *, with_inputs: bool = True) -> dict[str, Any]:
    from .saved_sensor_quality_job import get as get_quality

    job = get_quality(service, spec["quality_job_id"])
    if job["status"] != "succeeded":
        raise ValueError("Source saved sensor quality job must have succeeded.")
    reports = [
        report
        for report in job["result"]["summary"]["sensor_reports"]
        if report["revision"]["run_id"] == spec["sensor_run_id"]
    ]
    if len(reports) != 1:
        raise ValueError("Select exactly one sensor run from the saved quality job.")
    report = reports[0]
    if len([case for case in report["cases"] if case["scene"] == spec["scene"]]) != 1:
        raise ValueError("Selected scene is absent or repeated in the saved quality job.")
    paths = {
        "quality_report": service.registry.blob_path(report["artifacts"]["json"]["sha256"]),
        "quality_npz": service.registry.blob_path(report["artifacts"]["npz"]["sha256"]),
    }
    if with_inputs:
        paths.update(
            {
                "annotation": service.registry.blob_path(spec["annotation_artifact_sha256"]),
                "prediction": service.registry.blob_path(spec["prediction_artifact_sha256"]),
                "model_manifest": service.registry.blob_path(spec["model_manifest_sha256"]),
            }
        )
        if spec["model_artifact_sha256"]:
            paths["model_artifact"] = service.registry.blob_path(spec["model_artifact_sha256"])
    return {"quality_job": job, "report": report, "paths": paths}


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Pin the source RGB frame and all user-provided evidence before enqueueing."""
    from .imaging import _current
    from .registry import digest

    spec = specification(payload)
    environment = _current(service)
    source = _source(service, spec)
    paths = source["paths"]
    binding = saved_quality_image_binding(
        quality_report_path=paths["quality_report"],
        quality_npz_path=paths["quality_npz"],
        quality_job_id=spec["quality_job_id"],
        sensor_run_id=spec["sensor_run_id"],
        scene=spec["scene"],
    )
    annotations, predictions, manifest = (
        _json(paths["annotation"]),
        _json(paths["prediction"]),
        _json(paths["model_manifest"]),
    )
    if annotations.get("image_binding") != binding or predictions.get("image_binding") != binding:
        raise ValueError("Provided labels/predictions do not bind to the source RGB frame.")
    if spec["model_artifact_sha256"] and (
        manifest.get("checkpoint_sha256") != spec["model_artifact_sha256"]
    ):
        raise ValueError("Versioned model manifest names another artifact.")
    declared = manifest.get("checkpoint_sha256")
    if (
        not isinstance(declared, str)
        or len(declared) != 64
        or any(letter not in "0123456789abcdef" for letter in declared)
    ):
        raise ValueError("Versioned model manifest needs a checkpoint SHA-256 identity.")
    if predictions.get("model_manifest_sha256") != spec["model_manifest_sha256"]:
        raise ValueError("Prediction artifact names another model manifest.")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": JOB_SCHEMA,
        "specification": spec,
        "source_identities": {
            "quality_job_id": spec["quality_job_id"],
            "quality_result_sha256": digest(source["quality_job"]["result"]["summary"]),
            "image_binding": binding,
            "annotation_artifact_sha256": spec["annotation_artifact_sha256"],
            "prediction_artifact_sha256": spec["prediction_artifact_sha256"],
            "model_manifest_sha256": spec["model_manifest_sha256"],
            "model_artifact_sha256": declared,
            "model_artifact_bytes_registered": bool(spec["model_artifact_sha256"]),
        },
        "environment": environment,
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .registry import digest

    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .registry import IntegrityError, canonical

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Saved sensor prediction study is not running or was cancelled.")
    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen quality image, labels, model or executable changed.")
    spec = fresh["specification"]
    paths = _source(service, spec)["paths"]
    report = evaluate_saved_sensor_predictions(
        quality_report_path=paths["quality_report"],
        quality_npz_path=paths["quality_npz"],
        annotation_path=paths["annotation"],
        prediction_path=paths["prediction"],
        model_manifest_path=paths["model_manifest"],
        model_artifact_path=paths.get("model_artifact"),
        quality_job_id=spec["quality_job_id"],
        sensor_run_id=spec["sensor_run_id"],
        scene=spec["scene"],
        iou_threshold=spec["iou_threshold"],
    )
    after = prepare(service, spec)
    if canonical(after) != canonical(fresh):
        raise IntegrityError("Perception evidence changed during evaluation.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Saved sensor prediction study was cancelled during execution.")
    summary = {
        "schema": JOB_SCHEMA,
        "status": "completed",
        "specification": spec,
        "source_identities": fresh["source_identities"],
        "evaluation": report,
        "limitation": LIMITATION,
    }
    artifact = service.registry.store_blob(
        canonical(summary).encode(), "saved-sensor-prediction-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [artifact]}


def get(service: Any, identity: str) -> dict[str, Any]:
    from .registry import IntegrityError, canonical

    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a saved sensor prediction study.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        if canonical(summary["specification"]) != canonical(job["specification"]) or canonical(
            summary["source_identities"]
        ) != canonical(job["source_identities"]):
            raise IntegrityError("Saved prediction result differs from frozen evidence.")
        if job["status"] == "succeeded":
            matching = [
                item
                for item in job["artifacts"]
                if item["name"] == "saved-sensor-prediction-result.json"
            ]
            if len(matching) != 1 or (
                service.registry.blob_path(matching[0]["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Saved prediction result artifact differs.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs()
        if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi import HTTPException
    from fastapi.responses import Response

    from .registry import canonical

    @app.post(
        "/api/v1/saved-sensor-perception/artifacts",
        status_code=201,
        tags=["saved_sensor_perception"],
    )
    async def import_artifact(request: Request):
        """Import only small, hash-declared JSON evidence; never detector weights."""
        blocks: list[bytes] = []
        total = 0
        async for block in request.stream():
            total += len(block)
            if total > _MAX_IMPORT_BODY_BYTES:
                raise HTTPException(413, "Perception artifact request exceeds 512 KiB.")
            blocks.append(block)
        try:
            payload = json.loads(b"".join(blocks))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise HTTPException(422, "Perception artifact request must be JSON.") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "kind",
            "document",
            "expected_sha256",
        }:
            raise HTTPException(422, "Declare kind, document and expected_sha256 exactly.")
        kinds = {
            "annotation": (ANNOTATION_SCHEMA, "saved-detection-annotations.json"),
            "prediction": (PREDICTION_SCHEMA, "saved-detection-predictions.json"),
            "model_manifest": (MODEL_SCHEMA, "saved-detection-model-manifest.json"),
        }
        if not isinstance(payload["kind"], str) or payload["kind"] not in kinds:
            raise HTTPException(
                422, "Only annotation, prediction and model_manifest JSON is accepted."
            )
        document = payload["document"]
        schema, name = kinds[payload["kind"]]
        if not isinstance(document, dict) or document.get("schema") != schema:
            raise HTTPException(422, "Artifact document schema differs from its kind.")
        try:
            data = canonical(document).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise HTTPException(422, "Artifact document is not canonical JSON.") from exc
        if len(data) > _MAX_IMPORT_JSON_BYTES:
            raise HTTPException(413, "Canonical perception artifact exceeds 256 KiB.")
        checksum = hashlib.sha256(data).hexdigest()
        if payload["expected_sha256"] != checksum:
            raise HTTPException(422, "Artifact SHA-256 differs from canonical JSON bytes.")
        artifact = service.registry.store_blob(data, name, "application/json")
        return {
            "kind": payload["kind"],
            "schema": schema,
            "artifact": artifact,
            "canonicalization": "UTF-8 JSON, sorted keys, compact separators, finite numbers",
            "model_inference_performed": False,
            "model_weight_import_supported": False,
        }

    @app.get("/api/v1/saved-sensor-perception/template", tags=["saved_sensor_perception"])
    def template(quality_job_id: str = "", sensor_run_id: str = "", scene: str = ""):
        payload: dict[str, Any] = {"specification": default_spec(), "limitation": LIMITATION}
        if quality_job_id and sensor_run_id and scene:
            source = _source(
                service,
                {
                    "quality_job_id": quality_job_id,
                    "sensor_run_id": sensor_run_id,
                    "scene": scene,
                    "annotation_artifact_sha256": "",
                    "prediction_artifact_sha256": "",
                    "model_manifest_sha256": "",
                    "model_artifact_sha256": "",
                },
                with_inputs=False,
            )
            paths = source["paths"]
            payload["image_binding"] = saved_quality_image_binding(
                quality_report_path=paths["quality_report"],
                quality_npz_path=paths["quality_npz"],
                quality_job_id=quality_job_id,
                sensor_run_id=sensor_run_id,
                scene=scene,
            )
        return payload

    @app.post("/api/v1/saved-sensor-perception", status_code=202, tags=["saved_sensor_perception"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/saved-sensor-perception", tags=["saved_sensor_perception"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/saved-sensor-perception/{identity}", tags=["saved_sensor_perception"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/saved-sensor-perception/{identity}/cancel", tags=["saved_sensor_perception"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/saved-sensor-perception/{identity}/export", tags=["saved_sensor_perception"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="saved-sensor-perception.json"'},
        )
