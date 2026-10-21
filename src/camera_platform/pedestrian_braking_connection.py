"""Actual full-window camera connection on a mini frame, never a hazard study."""

from __future__ import annotations

import copy
import json
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from .pedestrian_braking_detector import infer_sequence
from .pedestrian_braking_inputs import inventory
from .road_design_compute import (
    _roi_bounds,
    compute_mean,
    fixed_isp,
    fixed_profile,
    load_candidate,
    native_coordinates,
    roi_capture_sensor,
    to_common_grid,
    well_saturation_mask,
)
from .road_design_inputs import write_json
from .road_rgb_source import file_sha256, json_sha256


def run_connection(workspace, dataset_root, output, on_progress=print):
    from pyisetcam.sensor_capture import resample_sensor_capture
    from pyisetcam.utils import linear_to_srgb

    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Use a new connection output; diagnostic snapshots are immutable.")
    audit = inventory(dataset_root)
    event = max(audit["events"], key=lambda e: sum(len(a["persons"]) for a in e["anchors"]))
    # Input-only choice: most annotated persons; no detector or camera scores.
    anchor = max(event["anchors"], key=lambda a: len(a["persons"]))
    frame = next(f for f in event["frames"] if f["token"] == anchor["frame_token"])
    base = workspace / "reports/camera_platform/road-design-20261003-reviewed/specification.json"
    spec = copy.deepcopy(json.loads(base.read_text()))
    spec["geometry"]["horizontal_fov_deg"] = 32.0
    spec["resources"]["maximum_native_roi_cells"] = 100_000_000
    h = math.tan(math.radians(15))
    window = {
        "id": "fixed-front-window",
        "tangent_bounds_xyxy": [-h, -h * 9 / 16, h, h * 9 / 16],
        "common_shape_row_col": [720, 1280],
    }
    candidates = [c for c in spec["candidates"] if c["id"] in {"L1-S1", "L2-S1"}]
    required = 0
    sizes = []
    for c in candidates:
        optics, sensor = load_candidate(workspace, spec, c)
        bounds, _ = _roi_bounds(native_coordinates(optics, sensor, window), sensor.fields["size"])
        cells = (bounds[1] - bounds[0]) * (bounds[3] - bounds[2])
        sizes.append(
            {"id": c["id"], "native_cells": cells, "sensor_shape": list(sensor.fields["size"])}
        )
        required += cells * (8 + 5 * 4) + 720 * 1280 * 16 * 5
    if shutil.disk_usage(output.parent).free < required * 1.1 + 5 * 1024**3:
        raise ValueError(f"First-frame diagnostic requires {required} bytes plus 5GiB reserve.")
    output.mkdir(parents=True)
    snapshot = output / "original.jpg"
    shutil.copyfile(frame["source_path"], snapshot)
    frame = {**frame, "source_path": str(snapshot), "source_sha256": file_sha256(snapshot)}
    config = {
        "profile": "ultralytics_yolo11n_detection",
        "weights_sha256": file_sha256(
            Path.home() / ".cache/pyisetcam/task_perception/yolo/yolo11n.pt"
        ),
        "score_threshold": 0.25,
        "nms_iou": 0.7,
        "input_size": 1280,
    }
    tracker = {
        "association_iou": 0.3,
        "max_missed": 2,
        "consecutive_required": 3,
        "maximum_frame_gap_s": 0.125,
    }
    write_json(
        output / "connection-specification.json",
        {
            "source_road_specification_sha256": file_sha256(base),
            "frame": frame,
            "camera_specification": spec,
            "window": window,
            "detector": config,
            "hazard_cohort_qualified": False,
            "source_sampling_qualified": False,
            "purpose": "one actual camera frame connection, not temporal/braking assessment",
        },
    )
    results = []
    for candidate in candidates:
        on_progress(f"Computing full-window mean: {candidate['id']}")
        directory = output / candidate["id"]
        mean, report, optics, sensor, coords = compute_mean(
            workspace, spec, frame, window, candidate, directory
        )
        bounds = tuple(report["native_roi_bounds"])
        profile, calibration = fixed_profile(
            sensor,
            np.asarray(report["spectral_proxy"]["primary_photons"]),
            spec["spectral_proxy"]["wavelength_nm"],
        )
        capture_sensor = roi_capture_sensor(sensor, bounds, mean * 0.25)
        captures = []
        for seed in (20261003, 20261004, 20261005, 20261006, 20261007):
            actual_seed = int(json_sha256([event["id"], frame["token"], seed])[:15], 16)
            capture = resample_sensor_capture(capture_sensor, seed=actual_seed)
            raw = capture.data["raw_dn"]
            well = well_saturation_mask(capture_sensor, actual_seed)
            if (
                abs(
                    float(well.mean())
                    - capture.metadata["sensor_capture"]["saturation"]["well_fraction"]
                )
                > 1e-14
            ):
                raise ValueError("Well mask differs from native readout.")
            linear, metadata = fixed_isp(raw, sensor, bounds, profile)
            common = to_common_grid(linear, coords, bounds)
            rgb = np.rint(linear_to_srgb(np.clip(common, 0, 1)) * 255).astype(np.uint8)
            np.save(directory / f"raw-{seed}.npy", raw)
            np.save(directory / f"linear-rgb-{seed}.npy", common.astype(np.float32))
            Image.fromarray(rgb).save(directory / f"rgb-{seed}.png")
            frames, identity = infer_sequence(config, tracker, [frame], lambda f, rgb=rgb: rgb)
            captures.append(
                {
                    "seed": seed,
                    "actual_seed": actual_seed,
                    "frames": frames,
                    "identity": identity,
                    "well_fraction": float(well.mean()),
                    "fixed_isp": metadata,
                }
            )
            on_progress(f"{candidate['id']} seed {seed}: {len(frames[0]['detections'])} detections")
        results.append(
            {
                "candidate_id": candidate["id"],
                "captures": captures,
                "fixed_color_calibration": calibration,
            }
        )
    result = {
        "status": "actual_first_frame_camera_connection_only",
        "frame_token": frame["token"],
        "camera_simulation_performed": True,
        "temporal_recognition_evaluated": False,
        "braking_opportunity_evaluated": False,
        "source_sampling_qualified": False,
        "native_geometry": sizes,
        "results": results,
    }
    write_json(output / "connection-result.json", result)
    write_json(
        output / "manifest.json",
        {
            "files": {
                str(p.relative_to(output)): file_sha256(p)
                for p in output.rglob("*")
                if p.is_file() and p.name != "manifest.json"
            }
        },
    )
    return result
