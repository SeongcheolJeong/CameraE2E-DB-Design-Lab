"""Offline YOLO subprocess protocol for the platform's separate Python 3.9 runtime.

This file deliberately imports no camera_platform or pyisetcam modules: the
available Ultralytics runtime is Python 3.9 while the platform needs 3.11+.
The parent validates every returned field before retaining any result.
"""

import base64
import hashlib
import json
import os
import socket
import sys
from pathlib import Path


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _offline_socket(*_args, **_kwargs):
    raise RuntimeError("Network access is disabled for offline detector inference.")


def main():
    # The model is an already-pinned local file. Keep Ultralytics from fetching
    # a missing model or performing a background connectivity check.
    socket.create_connection = _offline_socket
    socket.socket.connect = _offline_socket
    socket.socket.connect_ex = _offline_socket
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict) or set(payload) != {
        "schema", "weights_path", "weights_sha256", "rgb_shape", "rgb_uint8_base64",
        "rgb_uint8_sha256", "score_threshold", "target_labels",
    } or payload["schema"] != "camerae2e_offline_yolo_request_v1":
        raise ValueError("Offline detector request contract differs.")
    trusted_sha = os.environ.get("CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256", "")
    if (len(trusted_sha) != 64
            or any(char not in "0123456789abcdef" for char in trusted_sha)
            or trusted_sha != payload["weights_sha256"]):
        raise ValueError("Operator trust declaration for these YOLO weights is required.")
    if (os.environ.get("YOLO_OFFLINE") != "true"
            or os.environ.get("YOLO_AUTOINSTALL") != "false"):
        raise ValueError("Offline YOLO environment controls are required.")
    weights = Path(payload["weights_path"])
    if not weights.is_absolute() or not weights.is_file() or weights.name != "yolo11n.pt":
        raise ValueError("YOLO11n weights must be a present absolute local file.")
    if weights.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("YOLO11n weights exceed the local model limit.")
    if _sha256(weights.read_bytes()) != payload["weights_sha256"]:
        raise ValueError("YOLO11n weights differ from the pinned hash.")
    shape = payload["rgb_shape"]
    if (not isinstance(shape, list) or len(shape) != 3 or shape[2] != 3
            or not all(type(n) is int for n in shape)
            or not 8 <= shape[0] <= 32 or not 8 <= shape[1] <= 32):
        raise ValueError("Offline detector frame shape is unsupported.")
    encoded = payload["rgb_uint8_base64"]
    if not isinstance(encoded, str) or len(encoded) > 8192:
        raise ValueError("Offline detector RGB payload is too large.")
    pixels = base64.b64decode(encoded, validate=True)
    if len(pixels) != shape[0] * shape[1] * 3 or _sha256(pixels) != payload["rgb_uint8_sha256"]:
        raise ValueError("Offline detector RGB bytes differ from the pinned frame.")
    labels = payload["target_labels"]
    if (not isinstance(labels, list) or not 1 <= len(labels) <= 64
            or any(not isinstance(label, str) or not label for label in labels)
            or len(set(labels)) != len(labels)):
        raise ValueError("Offline detector target labels are invalid.")
    threshold = payload["score_threshold"]
    if type(threshold) not in (int, float) or not 0.01 <= threshold <= 0.95:
        raise ValueError("Offline detector score threshold is invalid.")

    # Ultralytics may print first-run settings to stdout even with verbose=False.
    # Reserve stdout exclusively for the one machine-readable response.
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    import numpy as np
    import torch
    import ultralytics

    # An absolute present file never enters Ultralytics' model-name downloader.
    model = ultralytics.YOLO(str(weights))
    names = model.names
    if not isinstance(names, dict) or not names:
        raise ValueError("YOLO model lacks its class name mapping.")
    mapping = {str(value): int(key) for key, value in names.items()}
    if len(mapping) != len(names) or any(label not in mapping for label in labels):
        raise ValueError("YOLO model does not support the requested target labels.")
    # The platform sends RGB bytes. Ultralytics treats HWC NumPy input as BGR
    # and reverses it internally, so convert at this exact model-call boundary.
    rgb_image = np.frombuffer(pixels, dtype=np.uint8).reshape(shape)
    image = np.ascontiguousarray(rgb_image[..., ::-1])
    bgr_sha256 = _sha256(image.tobytes(order="C"))
    result = model(
        image, conf=float(threshold), device="cpu",
        classes=[mapping[label] for label in labels], verbose=False,
    )[0]
    if tuple(result.orig_shape) != tuple(shape[:2]):
        raise ValueError("YOLO returned a different source image shape.")
    boxes = result.boxes
    detections = []
    if boxes is not None:
        coords = boxes.xyxy.detach().cpu().numpy()
        scores = boxes.conf.detach().cpu().numpy()
        classes = boxes.cls.detach().cpu().numpy()
        if not (len(coords) == len(scores) == len(classes)) or len(coords) > 256:
            raise ValueError("YOLO returned an unsupported number of detections.")
        # Length equality was checked above; Python 3.9 has no zip(strict=).
        for xyxy, score, class_id in zip(coords, scores, classes):  # noqa: B905
            numeric_id = int(class_id)
            if numeric_id not in names:
                raise ValueError("YOLO returned an unknown class ID.")
            detections.append({
                "bbox": [float(v) for v in xyxy],
                "label": str(names[numeric_id]),
                "score": float(score),
                "class_id": numeric_id,
            })
    if _sha256(weights.read_bytes()) != payload["weights_sha256"]:
        raise ValueError("YOLO11n weights changed during inference.")
    output = {
        "schema": "camerae2e_offline_yolo_result_v1",
        "inference_performed": True,
        "weights_sha256": payload["weights_sha256"],
        "rgb_uint8_sha256": payload["rgb_uint8_sha256"],
        "model_input_bgr_uint8_sha256": bgr_sha256,
        "rgb_shape": shape,
        "target_labels": labels,
        "model_class_ids": {label: mapping[label] for label in labels},
        "detections": detections,
        "runtime": {
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "ultralytics": ultralytics.__version__,
            "numpy": np.__version__,
            "executable": os.path.realpath(sys.executable),
        },
        "preprocessing": (
            "round_float_srgb_to_rgb_uint8_then_rgb_to_bgr_numpy_"
            "then_ultralytics_internal_letterbox"
        ),
    }
    protocol_stdout.write(
        json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )


if __name__ == "__main__":
    main()
