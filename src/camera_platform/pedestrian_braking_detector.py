"""Pinned persistent inference with bounded messages, timeout and causal tracks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import selectors
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from .lens_sensor_isp_detector import _INTERPRETER, local_model_preflight, local_weight_path
from .pedestrian_braking_metrics import CausalTracker
from .road_rgb_source import file_sha256


class SequenceDetector:
    def __init__(self, config):
        self.config = dict(config)
        self.preflight = local_model_preflight(config["weights_sha256"])
        self.temporary = tempfile.TemporaryDirectory(prefix="camera-sequence-yolo-")
        environment = {
            k: v
            for k, v in os.environ.items()
            if k not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "TORCH_HOME"}
        }
        environment.update(
            YOLO_OFFLINE="true",
            YOLO_AUTOINSTALL="false",
            MPLCONFIGDIR=self.temporary.name,
            YOLO_CONFIG_DIR=self.temporary.name,
        )
        worker = Path(__file__).with_name("_offline_yolo_sequence_worker.py")
        self.worker_sha256 = file_sha256(worker)
        self.log = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [str(_INTERPRETER), "-E", str(worker)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            cwd=self.temporary.name,
            env=environment,
        )
        self.pending = bytearray()
        try:
            self._send(
                {
                    "schema": "camera_offline_yolo_sequence_v2" if config["input_size"] == 640 else "camera_offline_yolo_sequence_v1",
                    "weights_path": str(local_weight_path()),
                    **{
                        k: config[k]
                        for k in ("weights_sha256", "score_threshold", "nms_iou", "input_size")
                    },
                }
            )
            self.identity = self._receive()
            if (
                self.identity.get("status") != "ready"
                or self.identity.get("weights_sha256") != config["weights_sha256"]
            ):
                raise ValueError("Sequence worker identity differs.")
        except BaseException:
            self.close()
            raise

    def _send(self, payload):
        self.process.stdin.write((json.dumps(payload, allow_nan=False) + "\n").encode())
        self.process.stdin.flush()

    def _receive(self):
        import time

        deadline = time.monotonic() + 120
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.pending:
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise TimeoutError("Sequence inference exceeded 120 seconds.")
                block = os.read(self.process.stdout.fileno(), 65536)
                if not block:
                    self.log.seek(0)
                    raise RuntimeError(
                        "Sequence inference ended: "
                        + self.log.read()[-1200:].decode(errors="replace")
                    )
                self.pending.extend(block)
                if len(self.pending) > 256 * 1024:
                    raise ValueError("Sequence output exceeds bound.")
            line, _, remaining = self.pending.partition(b"\n")
            self.pending = bytearray(remaining)
            return json.loads(line)

    def reset_sequence(self):
        """Start an independent arm without fabricating later capture timestamps."""
        self._send({"command": "reset_sequence"})
        if self._receive() != {"status": "sequence_reset"}:
            raise ValueError("Detector did not reset its independent sequence.")

    def detect(self, rgb, token, timestamp_s):
        pixels = np.asarray(rgb)
        if pixels.dtype != np.uint8 or pixels.shape != ((360, 640, 3) if self.config["input_size"] == 640 else (720, 1280, 3)):
            raise ValueError("Sequence detector requires the full uint8 observation window.")
        content = pixels.tobytes()
        expected = hashlib.sha256(content).hexdigest()
        self._send(
            {
                "token": token,
                "timestamp_s": timestamp_s,
                "rgb_shape": list(pixels.shape),
                "rgb_base64": base64.b64encode(content).decode(),
                "rgb_sha256": expected,
            }
        )
        result = self._receive()
        bgr_sha = hashlib.sha256(np.ascontiguousarray(pixels[..., ::-1]).tobytes()).hexdigest()
        if (
            result.get("token") != token
            or result.get("timestamp_s") != timestamp_s
            or result.get("rgb_sha256") != expected
            or result.get("weights_sha256") != self.config["weights_sha256"]
            or result.get("model_input_bgr_sha256") != bgr_sha
        ):
            raise ValueError("Sequence inference lost its exact frame/model binding.")
        for detection in result["detections"]:
            box = np.asarray(detection["bbox"], float)
            score = detection["score"]
            if (
                detection["label"] != "person"
                or box.shape != (4,)
                or not np.isfinite(box).all()
                or np.any(box < 0)
                or np.any(box[2:] <= box[:2])
                or box[2] > pixels.shape[1]
                or box[3] > pixels.shape[0]
                or not np.isfinite(score)
                or not 0.25 <= score <= 1
            ):
                raise ValueError("Worker detection is outside the pinned profile.")
        return result

    def close(self):
        process = getattr(self, "process", None)
        if process is not None:
            if process.poll() is None:
                try:
                    self._send({"command": "close"})
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
        if hasattr(self, "log"):
            self.log.close()
        self.temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def infer_sequence(config, tracker_config, frames, read_rgb):
    tracker = CausalTracker(**tracker_config)
    results = []
    with SequenceDetector(config) as detector:
        for frame in frames:
            result = detector.detect(read_rgb(frame), frame["token"], frame["timestamp_s"])
            result["tracks"] = tracker.update(frame["timestamp_s"], result["detections"])
            results.append(result)
        identity = {
            **detector.identity,
            "worker_sha256": detector.worker_sha256,
            "wall_clock_latency_used_for_braking": False,
        }
    return results, identity
