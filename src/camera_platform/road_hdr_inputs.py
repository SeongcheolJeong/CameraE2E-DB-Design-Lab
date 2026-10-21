"""Frozen road HDR specifications, input provenance and resource admission."""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
from pathlib import Path

from .road_design_inputs import write_json
from .road_rgb_source import file_sha256, json_sha256

SCHEMA = "camera_road_hdr_study_v1"
TITLE = "RGB 기반 근사 도로 장면에서 HDR이 보행자 안정 인지 시점과 제동 판단 여유에 미치는 영향"


def default_specification(workspace):
    from .lens_sensor_isp_detector import local_weight_path

    base = (
        Path(workspace) / "reports/camera_platform/road-design-20261003-reviewed/specification.json"
    )
    source = json.loads(base.read_text())
    spec = copy.deepcopy(
        {k: source[k] for k in ("capture", "spectral_proxy", "geometry", "sampling", "resources")}
    )
    spec.update(
        schema=SCHEMA,
        title=TITLE,
        source_camera_specification_sha256=file_sha256(base),
        candidates=[next(c for c in source["candidates"] if c["id"] == "L1-S1")],
        single_exposures_s=[0.001, 0.004, 0.008],
        long_exposures_s=[0.004, 0.008],
        profiles=["global", "local"],
        seeds=[20261003, 20261004, 20261005, 20261006, 20261007],
        illumination=[
            {"id": "low", "multiplier": 0.25, "bright_multiplier": 1.0},
            {"id": "reference", "multiplier": 1.0, "bright_multiplier": 1.0},
            {"id": "high-contrast", "multiplier": 1.0, "bright_multiplier": 16.0},
        ],
        bright_mask={
            "source": "original_RGB_linear_luminance",
            "threshold": 0.8,
            "weights": [0.2126, 0.7152, 0.0722],
            "detector_or_ground_truth_used": False,
        },
        timing={
            "readout_s": 0.005,
            "common_processing_s": 0.100,
            "hdr_processing_s": 0.015,
            "hdr_sensitivity_s": [0, 0.005, 0.015, 0.030, 0.060],
            "source_frame_interpretation": (
                "piecewise-held RGB proxy; no recovered exposure-internal motion"
            ),
        },
        braking={"remaining_delay_s": 0.3, "deceleration_m_s2": 6.0, "buffer_m": 2.0},
        detector={
            "weights_sha256": file_sha256(local_weight_path()),
            "score_threshold": 0.25,
            "nms_iou": 0.7,
            "input_size": 640,
            "profile": "ultralytics_yolo11n_detection",
        },
        storage={"full_evaluation_requires_external_volume": True, "reserve_bytes": 5 * 1024**3},
        events=[],
        first_photo=None,
        license_scope="noncommercial internal research; commercial permission not established",
    )
    spec["geometry"]["horizontal_fov_deg"] = 32.0
    spec["resources"]["maximum_native_roi_cells"] = 100_000_000
    h = math.tan(math.radians(15))
    spec["observation"] = {
        "id": "fixed-front-window",
        "tangent_bounds_xyxy": [-h, -h * 9 / 16, h, h * 9 / 16],
        "common_shape_row_col": [360, 640],
    }
    return spec


def freeze_first(workspace, package):
    """Reuse the provenance-bound previously annotated road input, not its RAW."""
    local = Path(workspace) / ".cache/pedestrian-braking/nuscenes-mini"
    if local.is_dir():
        return freeze_pedestrian_first(workspace, local, package)
    package = Path(package)
    if package.exists():
        raise ValueError("Use a new immutable HDR package.")
    original = (
        Path(workspace)
        / "outputs/pedestrian-braking/full-window-first-frame/connection-specification.json"
    )
    connection = json.loads(original.read_text())
    spec = default_specification(workspace)
    frame = copy.deepcopy(connection["frame"])
    if file_sha256(frame["source_path"]) != frame["source_sha256"]:
        raise ValueError("Previously bound road photograph changed.")
    package.mkdir(parents=True)
    target = package / "inputs" / f"{frame['token']}.jpg"
    target.parent.mkdir()
    shutil.copyfile(frame["source_path"], target)
    frame["source_path"] = str(target.resolve())
    spec["first_photo"] = frame
    # Preserve the annotation source rather than asserting a new reviewed hazard event.
    spec["first_photo_annotation_provenance"] = {
        "source": "previous mini inventory input-only most-person anchor",
        "connection_specification_sha256": file_sha256(original),
        "braking_event_qualified": False,
    }
    write_json(package / "specification.json", spec)
    write_json(package / "specification-binding.json", {"sha256": json_sha256(spec)})
    return spec


def bound_specification(package):
    package = Path(package)
    spec = json.loads((package / "specification.json").read_text())
    binding = json.loads((package / "specification-binding.json").read_text())
    if spec["schema"] != SCHEMA or json_sha256(spec) != binding["sha256"]:
        raise ValueError("Frozen HDR specification binding differs.")
    if [c["id"] for c in spec["candidates"]] != ["L1-S1"] or spec["observation"][
        "common_shape_row_col"
    ] != [360, 640]:
        raise ValueError("Version 1 requires fixed L1-S1 and actual 640x360 observation.")
    for frame in ([spec["first_photo"]] if spec["first_photo"] else []) + [
        f for e in spec["events"] for f in e["frames"]
    ]:
        if file_sha256(frame["source_path"]) != frame["source_sha256"]:
            raise ValueError("Frozen RGB source changed or is absent.")
    return spec, binding


def resource_plan(workspace, package, spec, stage):
    from .road_design_compute import _roi_bounds, load_candidate, native_coordinates

    optics, sensor = load_candidate(Path(workspace), spec, spec["candidates"][0])
    bounds, _ = _roi_bounds(
        native_coordinates(optics, sensor, spec["observation"]), sensor.fields["size"]
    )
    cells = (bounds[1] - bounds[0]) * (bounds[3] - bounds[2])
    frames = 1 if stage == "first-photo" else sum(len(e["frames"]) for e in spec["events"])
    # Float64 mean; 3 independent native RAW/readout calibration + fusion arrays,
    # all conditions/seeds; includes native FPN maps, rates, diagnostic artifacts.
    required = (
        cells * (32 + 3 * len(spec["seeds"]) * len(spec["illumination"]) * 160) * max(1, frames)
    )
    if stage == "first-photo":
        required = cells * (16 + 9 * 43 + 18 * 27)
    package = Path(package).resolve()
    parent = package
    while not parent.exists():
        parent = parent.parent
    free = shutil.disk_usage(parent).free
    external = (
        package.is_relative_to(Path("/Volumes"))
        and os.stat(parent).st_dev != os.stat(Path(workspace)).st_dev
    )
    reasons = []
    if stage != "first-photo" and not external:
        reasons.append("external_storage_not_mounted")
    if stage != "first-photo":
        for split in ("selection", "evaluation"):
            events = [e for e in spec["events"] if e["split"] == split]
            if len(events) != 6 or sum(e["risk"] for e in events) != 4:
                reasons.append("dataset_shortfall_" + split)
        logs = [e["log_token"] for e in spec["events"]]
        if len(set(logs)) != 12:
            reasons.append("events_require_12_disjoint_logs")
    if free < required + spec["storage"]["reserve_bytes"]:
        reasons.append("insufficient_disk_capacity")
    return {
        "status": "ready" if not reasons else "blocked",
        "reasons": reasons,
        "native_bounds": list(bounds),
        "native_shape": [bounds[1] - bounds[0], bounds[3] - bounds[2]],
        "native_cells": cells,
        "estimated_storage_bytes": required,
        "free_bytes": free,
        "external_volume": external,
        "accuracy_or_pixel_pitch_reduced": False,
        "estimate_is_not_hardware_memory_limit": True,
    }


def freeze_sequences(workspace, dataset_root, package, *, version="v1.0-trainval"):
    from .pedestrian_braking_inputs import allocate_events, inventory

    package = Path(package)
    if package.exists():
        raise ValueError("Use a new immutable study directory.")
    audit = inventory(dataset_root, version, straight_projection=True, output_shape=(360, 640))
    events = allocate_events(audit)
    spec = default_specification(workspace)
    spec["events"] = events
    spec["dataset"] = {
        "version": version,
        "table_sha256": audit["table_sha256"],
        "license_scope": audit["license_scope"],
    }
    package.mkdir(parents=True)
    inputs = package / "inputs"
    inputs.mkdir()
    for event in events:
        event["output_annotation_policy"] = "bracketing_boxes"
        for frame in event["frames"]:
            sha = file_sha256(frame["source_path"])
            target = inputs / (frame["token"] + ".jpg")
            shutil.copyfile(frame["source_path"], target)
            frame.update(source_path=str(target.resolve()), source_sha256=sha)
    write_json(package / "input-inventory.json", {k: v for k, v in audit.items() if k != "events"})
    write_json(package / "specification.json", spec)
    write_json(package / "specification-binding.json", {"sha256": json_sha256(spec)})
    return spec


def first_pedestrian_frame(dataset_root, *, version="v1.0-mini"):
    """Input-only maximum visible pedestrian angular size, never detector scores."""
    from .pedestrian_braking_inputs import project_person

    root = Path(dataset_root).resolve()
    names = [
        "sample",
        "sample_data",
        "sample_annotation",
        "instance",
        "category",
        "ego_pose",
        "calibrated_sensor",
        "sensor",
    ]
    tables = {n: json.loads((root / version / (n + ".json")).read_text()) for n in names}
    indexes = {n: {r["token"]: r for r in rows} for n, rows in tables.items()}
    annotations = {}
    for ann in tables["sample_annotation"]:
        category = indexes["category"][
            indexes["instance"][ann["instance_token"]]["category_token"]
        ]["name"]
        if category.startswith("human.pedestrian.") and int(ann["visibility_token"]) >= 3:
            annotations.setdefault(ann["sample_token"], []).append(ann)
    h = math.tan(math.radians(15))
    bounds = [-h, -h * 9 / 16, h, h * 9 / 16]
    choices = []
    for sd in tables["sample_data"]:
        cal = indexes["calibrated_sensor"][sd["calibrated_sensor_token"]]
        if (
            not sd["is_key_frame"]
            or indexes["sensor"][cal["sensor_token"]]["channel"] != "CAM_FRONT"
        ):
            continue
        source = root / sd["filename"]
        if not source.is_file():
            continue
        pose = indexes["ego_pose"][sd["ego_pose_token"]]
        persons = [
            b
            for ann in annotations.get(sd["sample_token"], [])
            if (b := project_person(ann, pose, cal, bounds, (360, 640))) is not None
        ]
        useful = [
            b
            for b in persons
            if b["bbox"][3] - b["bbox"][1] >= 24
            and b["bbox"][2] - b["bbox"][0] >= 8
            and b["bbox"][0] > 1
            and b["bbox"][2] < 638
        ]
        if not useful:
            continue
        rank = max(b["bbox"][3] - b["bbox"][1] for b in useful)
        frame = {
            "id": sd["token"],
            "token": sd["token"],
            "timestamp_s": sd["timestamp"] / 1e6,
            "source_timestamp_us": sd["timestamp"],
            "source_path": str(source),
            "source_sha256": file_sha256(source),
            "source_filename": sd["filename"],
            "original_shape_row_col": [sd["height"], sd["width"]],
            "camera": {**cal, "camera_distortion": [0.0] * 5},
            "pose": pose,
            "persons": persons,
        }
        choices.append((rank, sd["token"], frame))
    if not choices:
        raise ValueError(
            "No annotated >=24px pedestrian in fixed 30 degree window; "
            "do not substitute an enlarged crop."
        )
    return sorted(choices, key=lambda x: (-x[0], x[1]))[0][2]


def freeze_pedestrian_first(workspace, dataset_root, package):
    package = Path(package)
    if package.exists():
        raise ValueError("Use a new immutable input package.")
    frame = first_pedestrian_frame(dataset_root)
    spec = default_specification(workspace)
    package.mkdir(parents=True)
    inputs = package / "inputs"
    inputs.mkdir()
    target = inputs / (frame["token"] + ".jpg")
    shutil.copyfile(frame["source_path"], target)
    frame["source_path"] = str(target.resolve())
    spec["first_photo"] = frame
    spec["first_photo_annotation_provenance"] = {
        "source": "official 3D projected box; visibility >=3",
        "selection": (
            "largest >=24px pedestrian height in fixed window before simulated/detector results"
        ),
        "braking_event_qualified": False,
    }
    write_json(package / "specification.json", spec)
    write_json(package / "specification-binding.json", {"sha256": json_sha256(spec)})
    return spec
