"""Offline, persistent JSONL YOLO worker. Compatible with system Python 3.9."""

import base64
import hashlib
import json
import math
import os
import socket
import sys
from pathlib import Path


def sha(content):
    return hashlib.sha256(content).hexdigest()


def offline(*args, **kwargs):
    raise RuntimeError("Network is disabled for sequence inference.")


def main():
    socket.create_connection = offline
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    stdout = sys.stdout
    sys.stdout = sys.stderr
    line = sys.stdin.readline(16385)
    config = json.loads(line)
    if (
        set(config)
        != {"schema", "weights_path", "weights_sha256", "score_threshold", "nms_iou", "input_size"}
        or config["schema"] not in {"camera_offline_yolo_sequence_v1", "camera_offline_yolo_sequence_v2"}
    ):
        raise ValueError("Unsupported sequence configuration.")
    weight = Path(config["weights_path"])
    if (
        not weight.is_absolute()
        or weight.name != "yolo11n.pt"
        or weight.stat().st_size > 16 * 1024**2
        or sha(weight.read_bytes()) != config["weights_sha256"]
        or os.environ.get("CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256") != config["weights_sha256"]
        or os.environ.get("YOLO_OFFLINE") != "true"
        or os.environ.get("YOLO_AUTOINSTALL") != "false"
    ):
        raise ValueError("Offline trusted model binding missing or changed.")
    if (
        config["score_threshold"] != 0.25
        or config["nms_iou"] != 0.7
        or config["input_size"] != (640 if config["schema"] == "camera_offline_yolo_sequence_v2" else 1280)
    ):
        raise ValueError("Sequence detector configuration differs from the frozen profile.")
    import numpy as np
    import torch
    import ultralytics

    model = ultralytics.YOLO(str(weight))
    person = [key for key, value in model.names.items() if value == "person"]
    if len(person) != 1:
        raise ValueError("Person class mapping missing.")

    def send(obj):
        stdout.write(json.dumps(obj, allow_nan=False) + "\n")
        stdout.flush()

    send(
        {
            "status": "ready",
            "weights_sha256": config["weights_sha256"],
            "runtime": {
                "python": sys.version.split()[0],
                "torch": torch.__version__,
                "ultralytics": ultralytics.__version__,
            },
            "model_loaded_once": True,
        }
    )
    rows, cols = (360, 640) if config["schema"] == "camera_offline_yolo_sequence_v2" else (720, 1280)
    last_time = None
    while True:
        line = sys.stdin.readline(8 * 1024**2 + 1)
        if not line:
            break
        if len(line) > 8 * 1024**2:
            raise ValueError("Frame protocol exceeds limit.")
        request = json.loads(line)
        if request == {"command": "close"}:
            break
        if request == {"command": "reset_sequence"}:
            last_time = None
            send({"status": "sequence_reset"})
            continue
        if set(request) != {"token", "timestamp_s", "rgb_shape", "rgb_base64", "rgb_sha256"}:
            raise ValueError("Frame protocol differs.")
        t = request["timestamp_s"]
        if not math.isfinite(t) or last_time is not None and t <= last_time:
            raise ValueError("Strictly increasing sequence timestamps required.")
        last_time = t
        if request["rgb_shape"] != [rows, cols, 3]:
            raise ValueError("Requires actual full observation RGB 1280x720.")
        content = base64.b64decode(request["rgb_base64"], validate=True)
        if len(content) != rows * cols * 3 or sha(content) != request["rgb_sha256"]:
            raise ValueError("RGB frame hash/size differs.")
        rgb = np.frombuffer(content, dtype=np.uint8).reshape(rows, cols, 3)
        bgr = np.ascontiguousarray(rgb[..., ::-1])
        result = model(
            bgr,
            conf=config["score_threshold"],
            iou=config["nms_iou"],
            imgsz=config["input_size"],
            device="cpu",
            classes=person,
            verbose=False,
        )[0]
        if tuple(result.orig_shape) != (rows, cols):
            raise ValueError("Inference coordinates changed.")
        detections = []
        if result.boxes is not None:
            for box, confidence, cls in zip(  # noqa: B905 -- system Python 3.9
                result.boxes.xyxy.cpu().numpy(),
                result.boxes.conf.cpu().numpy(),
                result.boxes.cls.cpu().numpy(),
            ):
                detections.append(
                    {
                        "bbox": box.tolist(),
                        "score": float(confidence),
                        "label": "person",
                        "class_id": int(cls),
                    }
                )
        if len(detections) > 1000:
            raise ValueError("Detection result exceeds bound.")
        send(
            {
                "token": request["token"],
                "timestamp_s": t,
                "rgb_sha256": request["rgb_sha256"],
                "model_input_bgr_sha256": sha(bgr.tobytes()),
                "weights_sha256": config["weights_sha256"],
                "detections": detections,
            }
        )
    if sha(weight.read_bytes()) != config["weights_sha256"]:
        raise ValueError("Model changed during inference.")


if __name__ == "__main__":
    main()
