"""Run a pinned local detector on one exact saved Lens × Sensor ISP RGB frame.

The detector runs in an offline Python 3.9 process because the platform's
Python 3.12 Torch installation is currently broken. The process receives only
bounded RGB bytes and a hash-pinned local weight path. Its output is treated as
untrusted until the parent validates the full protocol and image coordinates.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.task_perception import (
    annotations_to_bboxes,
    detection_metrics,
    task_perception_config,
)

from .lens_sensor_isp_perception import (
    ANNOTATION_SCHEMA,
    _annotation_independence,
    _document,
    _npz_array,
    lens_sensor_isp_image_binding,
)
from .saved_sensor_perception import _boxes, _finite_number, _identity, _label_set, _sha256

SCHEMA = "camerae2e_lens_sensor_isp_detector_result_v1"
PROFILE = "ultralytics_yolo11n_detection"
_WEIGHT_LIMIT = 16 * 1024 * 1024
_JSON_LIMIT = 2 * 1024 * 1024
_RESPONSE_LIMIT = 256 * 1024
_INTERPRETER = Path("/usr/bin/python3")
_WORKER = Path(__file__).with_name("_offline_yolo_worker.py")
_TRUST_ENV = "CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256"
_PREPROCESSING = (
    "round_float_srgb_to_rgb_uint8_then_rgb_to_bgr_numpy_"
    "then_ultralytics_internal_letterbox"
)


def local_weight_path() -> Path:
    """Fixed installed-cache location; API callers never supply a file path."""
    return Path.home() / ".cache/pyisetcam/task_perception/yolo/yolo11n.pt"


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact_id(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} requires a lowercase SHA-256.")
    return value


def _targets(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 64
        or any(not isinstance(name, str) or not 1 <= len(name.strip()) <= 128 for name in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("target_labels requires 1..64 unique model class names.")
    return list(value)


def _interpreter_probe() -> dict[str, str]:
    if not _INTERPRETER.is_file():
        raise ValueError("Offline YOLO Python interpreter is unavailable.")
    completed = subprocess.run(
        [
            str(_INTERPRETER), "-E", "-c",
            "import json,os,sys;print(json.dumps({'executable':os.path.realpath(sys.executable),"
            "'version':sys.version.split()[0]}))",
        ],
        capture_output=True, timeout=5, check=False,
    )
    if completed.returncode or len(completed.stdout) > 512:
        raise ValueError("Offline YOLO Python interpreter probe failed.")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Offline YOLO Python interpreter identity is invalid.") from exc
    if (
        not isinstance(result, dict)
        or set(result) != {"executable", "version"}
        or not isinstance(result["executable"], str)
        or not result["version"].startswith("3.9.")
        or not Path(result["executable"]).is_file()
    ):
        raise ValueError("Offline YOLO requires the supported local Python 3.9 runtime.")
    return result


def local_model_preflight(
    expected_sha256: str, *, weights_path: Path | None = None
) -> dict[str, Any]:
    """Require explicit operator trust, then pin local bytes without loading pickle."""
    expected = _artifact_id(expected_sha256, "model_weights_sha256")
    declared = os.environ.get(_TRUST_ENV, "")
    if not declared:
        raise ValueError(f"Set {_TRUST_ENV} before this development-only model run.")
    trusted = _artifact_id(declared, _TRUST_ENV)
    if trusted != expected:
        raise ValueError("Operator-trusted YOLO SHA differs from requested model weights.")
    try:
        path = (weights_path or local_weight_path()).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("Local YOLO11n weights are unavailable.") from exc
    if path.name != "yolo11n.pt":
        raise ValueError("Only the local YOLO11n weight file is supported.")
    actual = _sha256(path, _WEIGHT_LIMIT)
    if actual != expected:
        raise ValueError("Local YOLO11n weights differ from the expected SHA-256.")
    if not _WORKER.is_file():
        raise ValueError("Offline YOLO interpreter or worker script is unavailable.")
    interpreter = _interpreter_probe()
    return {
        "profile": PROFILE,
        "weights_sha256": actual,
        "weights_bytes": path.stat().st_size,
        "weights_location": "fixed_local_pyisetcam_cache",
        "weight_origin_verification": "not_established",
        "operator_trust": "explicit_environment_sha256_declaration",
        "operator_trust_env_var": _TRUST_ENV,
        "operator_trust_is_authenticity_verification": False,
        "runtime_boundary": "platform_python_to_offline_system_python_3_9",
        "runtime_executable": interpreter["executable"],
        "runtime_python": interpreter["version"],
        "worker_sha256": _sha256(_WORKER, _JSON_LIMIT),
    }


def _run_offline_yolo(request: dict[str, Any]) -> dict[str, Any]:
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "TORCH_HOME"}
    }
    environment["YOLO_OFFLINE"] = "true"
    environment["YOLO_AUTOINSTALL"] = "false"
    # Use a transient cwd so a repository module cannot shadow a runtime import.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="camera-offline-yolo-") as directory:
        environment["MPLCONFIGDIR"] = directory
        environment["YOLO_CONFIG_DIR"] = directory
        try:
            completed = subprocess.run(
                [str(_INTERPRETER), "-E", str(_WORKER)],
                input=json.dumps(request, sort_keys=True, separators=(",", ":")).encode(),
                capture_output=True,
                cwd=directory,
                env=environment,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Offline YOLO inference exceeded 120 seconds.") from exc
    if completed.returncode:
        error = completed.stderr.decode("utf-8", "replace")[-1200:].strip()
        raise RuntimeError(f"Offline YOLO inference failed: {error}")
    if len(completed.stdout) > _RESPONSE_LIMIT:
        raise ValueError("Offline YOLO output exceeds 256 KiB.")
    try:
        output = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Offline YOLO response is not valid JSON.") from exc
    if not isinstance(output, dict):
        raise ValueError("Offline YOLO response is not an object.")
    return output


def _validated_output(
    output: dict[str, Any], *, request: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    required = {
        "schema", "inference_performed", "weights_sha256", "rgb_uint8_sha256",
        "model_input_bgr_uint8_sha256", "rgb_shape", "target_labels",
        "model_class_ids", "detections", "runtime", "preprocessing",
    }
    if set(output) != required or output["schema"] != "camerae2e_offline_yolo_result_v1":
        raise ValueError("Offline YOLO result schema differs.")
    if (
        output["inference_performed"] is not True
        or output["weights_sha256"] != request["weights_sha256"]
        or output["rgb_uint8_sha256"] != request["rgb_uint8_sha256"]
        or output["rgb_shape"] != request["rgb_shape"]
        or output["target_labels"] != request["target_labels"]
        or output["preprocessing"] != _PREPROCESSING
    ):
        raise ValueError("Offline YOLO result lost the pinned image/model contract.")
    shape = tuple(request["rgb_shape"])
    pixels = base64.b64decode(request["rgb_uint8_base64"], validate=True)
    if len(pixels) != int(np.prod(shape)):
        raise ValueError("Parent detector request lacks the exact RGB frame bytes.")
    expected_bgr_sha = _sha256_bytes(
        np.ascontiguousarray(
            np.frombuffer(pixels, dtype=np.uint8).reshape(shape)[..., ::-1]
        ).tobytes(order="C")
    )
    if output["model_input_bgr_uint8_sha256"] != expected_bgr_sha:
        raise ValueError("Offline YOLO NumPy input differs from RGB-to-BGR conversion.")
    class_ids = output["model_class_ids"]
    if (
        not isinstance(class_ids, dict)
        or set(class_ids) != set(request["target_labels"])
        or any(type(value) is not int or not 0 <= value < 1000 for value in class_ids.values())
        or len(set(class_ids.values())) != len(class_ids)
    ):
        raise ValueError("Offline YOLO model class mapping is invalid.")
    raw = output["detections"]
    if not isinstance(raw, list) or len(raw) > 256:
        raise ValueError("Offline YOLO detections are not bounded.")
    detections = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"bbox", "label", "score", "class_id"}:
            raise ValueError(f"Offline YOLO detection {index} differs from the box contract.")
        if item["label"] not in class_ids or item["class_id"] != class_ids[item["label"]]:
            raise ValueError(f"Offline YOLO detection {index} has an inconsistent class ID.")
        detections.append({key: item[key] for key in ("bbox", "label", "score")})
    normalized = _boxes(
        detections, set(request["target_labels"]),
        tuple(request["rgb_shape"][:2]), predictions=True,
    )
    runtime = output["runtime"]
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {"python", "torch", "ultralytics", "numpy", "executable"}
        or any(not isinstance(value, str) or not 1 <= len(value) <= 128
               for value in runtime.values())
    ):
        raise ValueError("Offline YOLO runtime identity is incomplete.")
    if runtime["executable"] != _interpreter_probe()["executable"]:
        raise ValueError("Offline YOLO used an unexpected interpreter.")
    return normalized, runtime


def _metrics_for_annotations(
    annotation_path: Path, *, binding: dict[str, Any], detections: list[dict[str, Any]],
    target_labels: list[str], model_sha256: str, iou_threshold: float,
) -> dict[str, Any]:
    annotation_sha = _sha256(annotation_path, _JSON_LIMIT)
    document = _document(annotation_path)
    if document.get("schema") != ANNOTATION_SCHEMA or document.get("image_binding") != binding:
        raise ValueError("Annotations must bind to the exact saved Lens ISP RGB.")
    label_set = _label_set(document.get("label_set"))
    if label_set["labels"] != target_labels:
        raise ValueError("Annotation label_set must exactly match the detector target labels.")
    identity = document.get("annotation_set")
    if not isinstance(identity, dict) or set(identity) != {"id", "version"}:
        raise ValueError("Annotation set requires id and version.")
    identity = {
        "id": _identity(identity["id"], "annotation_set.id"),
        "version": _identity(identity["version"], "annotation_set.version"),
    }
    forbidden = {
        annotation_sha, model_sha256, binding["isp_report_sha256"],
        binding["isp_npz_sha256"], binding["array_sha256"],
        binding["source_binding"]["source_report_sha256"],
        binding["source_binding"]["source_raw_artifact_sha256"],
        binding["source_raw_content_sha256"].removeprefix("sha256:"),
    }
    source = _annotation_independence(document, binding=binding, forbidden_sha=forbidden)
    # An explicitly labeled negative frame is necessary to count false
    # detections.  The shared positive-label parser intentionally rejects an
    # empty object list, so permit it only in this detector replay path.
    objects = document.get("objects")
    truth = (
        [] if objects == [] else
        _boxes(objects, set(target_labels), tuple(binding["shape"][:2]),
               predictions=False)
    )
    cfg = task_perception_config(
        iou_threshold=iou_threshold, score_threshold=0.0, max_detections=256
    )
    image_size = tuple(binding["shape"][:2])
    measured = detection_metrics(
        annotations_to_bboxes(detections, bbox_format="xyxy", image_size=image_size),
        annotations_to_bboxes(truth, bbox_format="xyxy", image_size=image_size),
        cfg,
    )
    names = ("true_positive", "false_positive", "false_negative", "precision", "recall", "f1")
    return {
        "annotation_artifact_sha256": annotation_sha,
        "annotation_set": identity,
        "ground_truth_source": source,
        "independence_verification": (
            "semantic_ground_truth_absent_synthetic_contract_fixture"
            if source["source_kind"] == "synthetic_contract_fixture"
            else "supplier_declared_not_independently_verified"
        ),
        "single_frame_metrics": {
            "iou_threshold": measured["iou_threshold"],
            **{name: measured[name] for name in names},
            "mean_matched_iou": measured["mean_matched_iou"],
            "per_label": {
                label: {name: metrics[name] for name in names}
                for label, metrics in measured["per_label"].items()
            },
            "matches": measured["matches"],
            "dataset_map": {
                "available": False,
                "reason": "One simulated frame is not a held-out dataset.",
            },
        },
    }


def infer_lens_sensor_isp_detector(
    *, lens_raw_job: Mapping[str, Any], lens_raw_report_path: str | Path,
    lens_raw_path: str | Path, lens_isp_job: Mapping[str, Any],
    lens_isp_result_path: str | Path, lens_isp_report_path: str | Path,
    lens_isp_npz_path: str | Path, model_weights_sha256: str,
    target_labels: list[str], score_threshold: float,
    annotation_path: str | Path | None = None, iou_threshold: float = 0.5,
    weights_path: Path | None = None,
) -> dict[str, Any]:
    """Run actual YOLO inference; metrics require independent supplied labels."""
    target_labels = _targets(target_labels)
    score = _finite_number(score_threshold, "score_threshold")
    iou = _finite_number(iou_threshold, "iou_threshold")
    if not 0.01 <= score <= 0.95 or not 0.1 <= iou <= 0.95:
        raise ValueError("Detector score or IoU threshold is outside its bounded range.")
    binding = lens_sensor_isp_image_binding(
        lens_raw_job=lens_raw_job, lens_raw_report_path=lens_raw_report_path,
        lens_raw_path=lens_raw_path, lens_isp_job=lens_isp_job,
        lens_isp_result_path=lens_isp_result_path,
        lens_isp_report_path=lens_isp_report_path,
        lens_isp_npz_path=lens_isp_npz_path,
    )
    if binding["color_encoding"] != "srgb":
        raise ValueError("YOLO11n requires sRGB ISP output; linear RGB is unsupported.")
    preflight = local_model_preflight(model_weights_sha256, weights_path=weights_path)
    rgb = _npz_array(
        Path(lens_isp_npz_path), "candidate_rgb",
        tuple(binding["shape"]), binding["dtype"],
    )
    if _sha256_bytes(rgb.tobytes(order="C")) != binding["array_sha256"]:
        raise ValueError("Detector input pixels differ from the bound ISP RGB array.")
    if not np.all(np.isfinite(rgb)) or np.any(rgb < 0) or np.any(rgb > 1):
        raise ValueError("Detector input RGB samples are invalid.")
    pixels = np.asarray(np.round(rgb * 255.0), dtype=np.uint8).tobytes(order="C")
    request = {
        "schema": "camerae2e_offline_yolo_request_v1",
        "weights_path": str((weights_path or local_weight_path()).expanduser().resolve()),
        "weights_sha256": preflight["weights_sha256"],
        "rgb_shape": binding["shape"],
        "rgb_uint8_base64": base64.b64encode(pixels).decode("ascii"),
        "rgb_uint8_sha256": _sha256_bytes(pixels),
        "score_threshold": score,
        "target_labels": target_labels,
    }
    output = _run_offline_yolo(request)
    detections, runtime = _validated_output(output, request=request)
    if local_model_preflight(model_weights_sha256, weights_path=weights_path) != preflight:
        raise ValueError("Local detector weights or worker changed during inference.")
    evaluation = None
    if annotation_path is not None:
        evaluation = _metrics_for_annotations(
            Path(annotation_path), binding=binding, detections=detections,
            target_labels=target_labels, model_sha256=model_weights_sha256,
            iou_threshold=iou,
        )
    return {
        "schema": SCHEMA,
        "status": "completed",
        "image_binding": binding,
        "model_provenance": preflight,
        "runtime": runtime,
        "preprocessing": {
            "input_encoding": "srgb", "source_dtype": binding["dtype"],
            "source_shape": binding["shape"], "channel_order": "RGB",
            "quantization": "numpy_round_to_uint8",
            "numpy_model_input_channel_order": "BGR",
            "rgb_to_bgr_conversion": "reverse_last_channel_before_numpy_model_call",
            "internal_resize": "ultralytics_letterbox",
            "rgb_uint8_sha256": request["rgb_uint8_sha256"],
            "model_input_bgr_uint8_sha256": output["model_input_bgr_uint8_sha256"],
            "contract": _PREPROCESSING,
        },
        "target_labels": target_labels,
        "model_class_ids": output["model_class_ids"],
        "score_threshold": score,
        "detections": detections,
        "detection_count": len(detections),
        "annotation_evaluation": evaluation,
        "verification": {
            "source_raw_and_isp": "exact_hash_and_array_content_verified",
            "detector_inference": "performed_by_offline_ultralytics_yolo11n",
            "model_bytes": "local_sha256_verified_before_and_after_inference",
            "model_authenticity": "not_established",
            "model_execution_trust": "operator_declared_exact_local_sha256",
            "network_boundary": (
                "YOLO_OFFLINE=true, YOLO_AUTOINSTALL=false and Python socket connect "
                "guard; not an OS network sandbox"
            ),
            "ground_truth": (
                "not_supplied" if evaluation is None else
                "semantic_ground_truth_absent_synthetic_contract_fixture"
                if evaluation["ground_truth_source"]["source_kind"]
                == "synthetic_contract_fixture" else "supplier_declared_only"
            ),
            "physical_validation": "not_established",
            "product_perception_performance": "not_established",
        },
        "limitation": (
            "Single 8–32 px simulated ISP frame. An empty detection list is a real model output, "
            "not evidence of detector validity. Operator-pinned local weights are a "
            "development trust declaration, not vendor authentication. Annotation truth "
            "are unverified. Single-frame matching is not mAP or product performance."
        ),
    }
