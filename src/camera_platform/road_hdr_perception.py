"""Pinned single-frame detector diagnostic; never stable recognition/AEB evidence."""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .pedestrian_braking_detector import SequenceDetector
from .road_design_inputs import write_json


def diagnose_first_photo(output, spec, records):
    output = Path(output)
    detector = None
    results = []
    try:
        detector = SequenceDetector(spec["detector"])
        for record in records:
            directory = output / record["path"]
            rgb = np.asarray(Image.open(directory / "rgb.png"))
            detector.reset_sequence()
            result = detector.detect(rgb, record["frame_token"], record["timestamp_s"])
            result["stable_recognition_evaluated"] = False
            result["braking_opportunity_evaluated"] = False
            write_json(directory / "detections.json", result)
            preview = Image.fromarray(rgb.copy())
            draw = ImageDraw.Draw(preview)
            for detection in result["detections"]:
                draw.rectangle(detection["bbox"], outline="cyan", width=2)
                draw.text(detection["bbox"][:2], f"person {detection['score']:.2f}", fill="cyan")
            preview.save(directory / "detections.png")
            record["person_detections"] = len(result["detections"])
            results.append(
                {
                    "arm": record["arm"],
                    "condition": record["condition"],
                    "person_detections": len(result["detections"]),
                    "rgb_sha256": result["rgb_sha256"],
                }
            )
        return {
            "status": "actual_offline_inference",
            "weights_sha256": spec["detector"]["weights_sha256"],
            "worker_sha256": detector.worker_sha256,
            "records": results,
            "independent_risk_events": 0,
            "diagnostic_only": True,
            "stable_recognition_evaluated": False,
        }
    except (ValueError, RuntimeError, TimeoutError, OSError) as error:
        return {
            "status": "unavailable",
            "reason": str(error),
            "diagnostic_only": True,
            "stable_recognition_evaluated": False,
        }
    finally:
        if detector is not None:
            detector.close()
