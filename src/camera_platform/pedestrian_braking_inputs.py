"""Input-only nuScenes event inventory and frozen sequence specifications."""

from __future__ import annotations

import copy
import json
import math
import shutil
from collections import Counter
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from .road_design_inputs import write_json
from .road_rgb_source import file_sha256, json_sha256

SCHEMA = "camera_pedestrian_braking_study_v1"
TABLES = (
    "scene",
    "sample",
    "sample_data",
    "sample_annotation",
    "instance",
    "category",
    "ego_pose",
    "calibrated_sensor",
    "sensor",
    "log",
)


def rotation(q):
    # nuScenes uses w,x,y,z, scipy uses x,y,z,w.
    q = np.asarray(q, float)
    if q.shape != (4,) or not np.isfinite(q).all() or abs(np.linalg.norm(q) - 1) > 1e-3:
        raise ValueError("Invalid nuScenes quaternion.")
    return Rotation.from_quat(q[[1, 2, 3, 0]]).as_matrix()


def collision_path_coordinate(poses, target, radius, *, half_width=1.0, front_offset=2.5):
    """First boundary of a stationary circular pedestrian vs rectangular front.

    Returns arc length along the actual recorded path, never Euclidean range.
    No extension past the recorded polyline is introduced.
    """
    poses, target = np.asarray(poses, float), np.asarray(target, float)
    if (
        poses.ndim != 2
        or poses.shape[1] != 2
        or len(poses) < 2
        or target.shape != (2,)
        or not np.isfinite(poses).all()
        or not np.isfinite(target).all()
        or not all(math.isfinite(x) and x >= 0 for x in (radius, half_width, front_offset))
    ):
        raise ValueError("Finite 2D recorded path and nonnegative collision dimensions required.")
    segments = np.diff(poses, axis=0)
    lengths = np.linalg.norm(segments, axis=1)
    positions = np.r_[0.0, np.cumsum(lengths)]
    for number, (segment, length) in enumerate(zip(segments, lengths, strict=True)):
        if length <= 1e-9:
            continue
        direction = segment / length
        relative = target - poses[number]
        along = float(relative @ direction)
        lateral = abs(float(relative @ np.array([-direction[1], direction[0]])))
        outside_width = max(0.0, lateral - half_width)
        if outside_width > radius:
            continue
        longitudinal_radius = math.sqrt(max(0.0, radius**2 - outside_width**2))
        entry = along - longitudinal_radius - front_offset
        exit_at = along + longitudinal_radius - front_offset
        if exit_at >= 0 and entry <= length:
            return float(positions[number] + max(0.0, entry))
    return None


def project_person(annotation, pose, calibration, tangent_bounds, shape):
    width, length, height = annotation["size"]
    corners = np.array(
        [
            [x, y, z]
            for x in (-length / 2, length / 2)
            for y in (-width / 2, width / 2)
            for z in (-height / 2, height / 2)
        ]
    )
    world = corners @ rotation(annotation["rotation"]).T + annotation["translation"]
    ego = (world - pose["translation"]) @ rotation(pose["rotation"])
    camera = (ego - calibration["translation"]) @ rotation(calibration["rotation"])
    if np.any(camera[:, 2] <= 0.1):
        return None
    x, y = camera[:, 0] / camera[:, 2], camera[:, 1] / camera[:, 2]
    x0, y0, x1, y1 = tangent_bounds
    rows, cols = shape
    bbox = [
        (x.min() - x0) / (x1 - x0) * (cols - 1),
        (y.min() - y0) / (y1 - y0) * (rows - 1),
        (x.max() - x0) / (x1 - x0) * (cols - 1),
        (y.max() - y0) / (y1 - y0) * (rows - 1),
    ]
    bbox = np.maximum(
        [0, 0, 0, 0], np.minimum(bbox, [cols - 1, rows - 1, cols - 1, rows - 1])
    ).tolist()
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return {
        "instance_id": annotation["instance_token"],
        "bbox": bbox,
        "annotation_token": annotation["token"],
        "visibility_token": annotation["visibility_token"],
        "box_source": "official_3d_box_projection_not_reviewed_2d_silhouette",
    }


def inventory(dataset_root, version="v1.0-mini", *, straight_projection=False, output_shape=(720, 1280)):
    root = Path(dataset_root).resolve()
    tables = {name: json.loads((root / version / f"{name}.json").read_text()) for name in TABLES}
    index = {name: {row["token"]: row for row in rows} for name, rows in tables.items()}
    annotations = {}
    for ann in tables["sample_annotation"]:
        cat = index["category"][index["instance"][ann["instance_token"]]["category_token"]]["name"]
        if cat.startswith("human.pedestrian."):
            annotations.setdefault(ann["sample_token"], []).append(ann)
    half = math.tan(math.radians(30) / 2)
    bounds, shape = [-half, -half * 9 / 16, half, half * 9 / 16], list(output_shape)
    events, excluded, hazard_rejections = [], [], Counter()
    front_by_scene = {}
    for sd in tables["sample_data"]:
        cal = index["calibrated_sensor"][sd["calibrated_sensor_token"]]
        if index["sensor"][cal["sensor_token"]]["channel"] == "CAM_FRONT":
            sid = index["sample"][sd["sample_token"]]["scene_token"]
            front_by_scene.setdefault(sid, []).append(sd)
    for scene in sorted(tables["scene"], key=lambda x: x["token"]):
        frames = []
        for sd in front_by_scene.get(scene["token"], []):
            cal = index["calibrated_sensor"][sd["calibrated_sensor_token"]]
            if index["sensor"][cal["sensor_token"]]["channel"] != "CAM_FRONT":
                continue
            pose = index["ego_pose"][sd["ego_pose_token"]]
            t = sd["timestamp"] / 1e6
            frames.append(
                {
                    "id": sd["token"],
                    "token": sd["token"],
                    "timestamp_s": t,
                    "source_timestamp_us": sd["timestamp"],
                    "is_key_frame": sd["is_key_frame"],
                    "source_path": str(root / sd["filename"]),
                    "source_filename": sd["filename"],
                    "original_shape_row_col": [sd["height"], sd["width"]],
                    "camera": {
                        **cal,
                        "camera_distortion": [0.0] * 5,
                        "projection_assumption": "nuScenes devkit pinhole; no extra rectification",
                    },
                    "pose": pose,
                    "sample_token": sd["sample_token"],
                }
            )
        frames.sort(key=lambda x: x["timestamp_s"])
        if len(frames) < 3:
            excluded.append({"scene": scene["token"], "reason": "front_sequence_missing"})
            continue
        # Nonoverlapping six-second windows; the allocator will use at most one per log.
        for offset in (0, 6, 12):
            start = frames[0]["timestamp_s"] + offset
            group = [f for f in frames if start <= f["timestamp_s"] <= start + 6]
            if len(group) < 60 or group[-1]["timestamp_s"] - group[0]["timestamp_s"] < 5.8:
                continue
            poses = np.array([f["pose"]["translation"][:2] for f in group])
            times = np.array([f["timestamp_s"] for f in group])
            yaw = np.unwrap(
                [
                    math.atan2(
                        rotation(f["pose"]["rotation"])[1, 0], rotation(f["pose"]["rotation"])[0, 0]
                    )
                    for f in group
                ]
            )
            if np.ptp(yaw) > math.radians(5):
                excluded.append(
                    {"scene": scene["token"], "offset_s": offset, "reason": "turn_exceeds_5deg"}
                )
                continue
            speed = np.linalg.norm(np.gradient(poses, times, axis=0), axis=1)
            path_s = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(poses, axis=0), axis=1))]
            anchors, instances = [], {}
            for f in group:
                if not f["is_key_frame"]:
                    continue
                anns = annotations.get(f["sample_token"], [])
                persons = []
                for ann in anns:
                    box = project_person(ann, f["pose"], f["camera"], bounds, shape)
                    if box is not None:
                        persons.append(box)
                        instances.setdefault(ann["instance_token"], []).append(
                            (f["timestamp_s"], ann)
                        )
                anchors.append(
                    {
                        "timestamp_s": f["timestamp_s"],
                        "frame_token": f["token"],
                        "persons": persons,
                        "annotation_complete": False,
                    }
                )
            targets = []
            for iid, records in instances.items():
                if len(records) < 4:
                    hazard_rejections["fewer_than_four_visible_anchors"] += 1
                    continue
                xyz = np.array([ann["translation"][:2] for _, ann in records])
                at = np.array([t for t, _ in records])
                radius = max(math.hypot(a["size"][0], a["size"][1]) / 2 for _, a in records)
                if straight_projection:
                    position = np.column_stack([np.interp(times, at, xyz[:, k]) for k in (0,1)])
                    delta = position - poses
                    direction = np.column_stack([np.cos(yaw), np.sin(yaw)])
                    along = np.sum(delta * direction, axis=1)
                    lateral = np.abs(delta[:,0] * -direction[:,1] + delta[:,1] * direction[:,0])
                    observed = (times >= at[0]) & (times <= at[-1])
                    in_path = (lateral <= 1.0 + radius) & (along > 0)
                    # First study distance is only meaningful while the target occupies
                    # the declared straight corridor. Crossing entry/exit is censored,
                    # never an extrapolated prediction of collision.
                    visible_in_path = in_path & observed
                    if np.count_nonzero(visible_in_path) < 4:
                        hazard_rejections["straight_corridor_insufficient_observed_support"] += 1
                        continue
                    longitudinal_radius = np.sqrt(np.maximum(0, radius**2 - np.maximum(0,lateral-1)**2))
                    remaining = along - longitudinal_radius - 2.5
                    distance0 = float(remaining[observed][0])
                else:
                    if max(np.linalg.norm(np.diff(xyz, axis=0), axis=1) / np.diff(at)) > 0.5:
                        hazard_rejections["pedestrian_motion_exceeds_0_5_m_s"] += 1
                        continue
                    target_position = xyz.mean(axis=0)
                    radius = max(math.hypot(a["size"][0], a["size"][1]) / 2 for _, a in records)
                    distance0 = collision_path_coordinate(poses, target_position, radius)
                    if distance0 is None:
                        hazard_rejections["no_collision_boundary_on_recorded_window_path"] += 1
                        continue
                    remaining = distance0 - path_s
                if (not straight_projection and (distance0 <= 0 or np.min(speed) < 2.0)) or (
                    straight_projection and not np.any(visible_in_path & (remaining > 0) & (speed >= 2.0))
                ):
                    hazard_rejections["no_positive_distance_or_ego_speed_below_2_m_s"] += 1
                    continue
                from .pedestrian_braking_metrics import required_distance

                margin = remaining - np.array([required_distance(float(v)) for v in speed])
                if not straight_projection and not (np.any(margin > 0) and np.any(margin <= 0)):
                    hazard_rejections["braking_boundary_not_bracketed"] += 1
                    # Observe the BRAKING boundary crossing, not an actual collision.
                    # Requiring the latter would incorrectly exclude avoided hazards.
                    continue
                targets.append((iid, remaining.tolist(), radius, {
                    "valid": visible_in_path.tolist() if straight_projection else [True] * len(group),
                    "observed_times": [float(at[0]), float(at[-1])],
                    "motion_bin": "moving" if np.max(np.linalg.norm(np.diff(xyz, axis=0), axis=1) / np.diff(at)) > .5 else "stationary",
                }))
            # One deterministic event per window, never detector-selected.
            risk = bool(targets)
            target = (
                sorted(targets, key=lambda x: x[0])[0]
                if risk
                else (None, [None] * len(group), None, {"valid": [True] * len(group), "observed_times": [float(times[0]),float(times[-1])], "motion_bin": "none"})
            )
            if not risk:
                # If a moving in-path pedestrian exists, it is not a negative control.
                moving_or_unknown = False
                for records in instances.values():
                    for t, ann in records:
                        nearest = int(np.argmin(abs(times - t)))
                        rel = (np.asarray(ann["translation"][:2]) - poses[nearest]) @ rotation(
                            group[nearest]["pose"]["rotation"]
                        )[:2, :2]
                        if rel[0] > 0 and abs(rel[1]) <= 1 + math.hypot(*ann["size"][:2]) / 2:
                            moving_or_unknown = True
                if moving_or_unknown:
                    excluded.append(
                        {
                            "scene": scene["token"],
                            "offset_s": offset,
                            "reason": "in_path_pedestrian_not_qualified_stationary_collision",
                        }
                    )
                    continue
            missing = [f["source_filename"] for f in group if not Path(f["source_path"]).is_file()]
            events.append(
                {
                    "id": scene["token"] + f"-{offset:02d}",
                    "scene_token": scene["token"],
                    "log_token": scene["log_token"],
                    "scene_description": scene["description"],
                    "risk": risk,
                    "target_instance_id": target[0],
                    "frames": group,
                    "anchors": anchors,
                    "annotation_resolution_s": 0.5,
                    "motion": [
                        {
                            "timestamp_s": f["timestamp_s"],
                            "speed_m_s": float(v),
                            "remaining_distance_m": d if d is not None else 0.0,
                            "collision_corridor_valid": bool(target[3]["valid"][number]),
                        }
                        for number, (f, v, d) in enumerate(zip(group, speed, target[1], strict=True))
                        if not straight_projection or not risk or
                        target[3]["observed_times"][0] <= f["timestamp_s"] <= target[3]["observed_times"][1]
                    ],
                    "geometry_assumptions": {
                        "vehicle_width_m": 2.0,
                        "vehicle_front_from_pose_m": 2.5,
                        "target_radius_m": target[2],
                        "distance_contract": "instantaneous_straight_front_collision_projection" if straight_projection else "recorded_polyline_arc_to_front_collision_boundary",
                        "crossing_entry_exit_is_censored": straight_projection,
                        "motion_bin": target[3]["motion_bin"],
                        "observed_braking_boundary_crossing_required": not straight_projection,
                    },
                    "missing_source_files": missing,
                }
            )
    return {
        "schema": "camera_nuscenes_event_inventory_v1",
        "dataset_root": str(root),
        "version": version,
        "table_sha256": {name: file_sha256(root / version / f"{name}.json") for name in TABLES},
        "events": events,
        "excluded": excluded,
        "risk_events": sum(e["risk"] for e in events),
        "negative_events": sum(not e["risk"] for e in events),
        "hazard_instance_window_rejection_counts": dict(hazard_rejections),
        "license_scope": "noncommercial internal research; commercial permission not established",
    }


def allocate_events(audit):
    # Reserve scarce hazard logs before assigning negative controls.
    chosen, used_logs = {True: [], False: []}, set()
    for risk, count in ((True, 8), (False, 4)):
        for event in audit["events"]:
            if (
                event["risk"] != risk
                or event["missing_source_files"]
                or event["log_token"] in used_logs
            ):
                continue
            chosen[risk].append(event)
            used_logs.add(event["log_token"])
            if len(chosen[risk]) == count:
                break
        if len(chosen[risk]) != count:
            raise ValueError(
                f"dataset_shortfall: require {count} {'hazard' if risk else 'negative'} "
                f"disjoint-log events, found {len(chosen[risk])}. "
                "Do not relabel sidewalk/moving pedestrians."
            )
    selected = []
    for split, offset in (("selection", 0), ("evaluation", 1)):
        selected.extend({**e, "split": split} for e in chosen[True][offset * 4 : offset * 4 + 4])
        selected.extend({**e, "split": split} for e in chosen[False][offset * 2 : offset * 2 + 2])
    return selected


def freeze_inputs(workspace, dataset_root, package, *, version="v1.0-mini"):
    workspace, package = Path(workspace).resolve(), Path(package).resolve()
    if package.exists():
        raise ValueError("Use a new package; frozen inputs are immutable.")
    audit = inventory(dataset_root, version)
    events = allocate_events(audit)
    previous = (
        workspace / "reports/camera_platform/road-design-20261003-reviewed/specification.json"
    )
    source = json.loads(previous.read_text())
    spec = copy.deepcopy(
        {
            k: source[k]
            for k in (
                "candidates",
                "baseline_id",
                "capture",
                "spectral_proxy",
                "geometry",
                "sampling",
                "resources",
                "isp",
            )
        }
    )
    spec.update(
        schema=SCHEMA,
        title="카메라 설계 변경이 저광량 전방 보행자의 안정 인지 시점과 제동 여유에 미치는 영향",
        events=events,
        dataset={
            "version": version,
            "table_sha256": audit["table_sha256"],
            "license_scope": audit["license_scope"],
        },
        observation={
            "tangent_bounds_xyxy": [
                -math.tan(math.radians(15)),
                -math.tan(math.radians(15)) * 9 / 16,
                math.tan(math.radians(15)),
                math.tan(math.radians(15)) * 9 / 16,
            ],
            "common_shape_row_col": [720, 1280],
        },
        detector={
            "profile": "ultralytics_yolo11n_detection",
            "score_threshold": 0.25,
            "nms_iou": 0.7,
            "input_size": 1280,
            "label": "person",
        },
        tracker={
            "association_iou": 0.3,
            "max_missed": 2,
            "consecutive_required": 3,
            "maximum_frame_gap_s": 0.125,
        },
        timing={
            "processing_latency_s": 0.1,
            "latency_sensitivity_s": [0.05, 0.1, 0.2],
            "timestamp_reference": (
                "source camera timestamp; 100ms includes "
                "acquisition/readout/ISP/inference/tracking assumption"
            ),
        },
        braking={"remaining_delay_s": 0.3, "deceleration_m_s2": 6.0, "buffer_m": 2.0},
        seeds=[20261003, 20261004, 20261005, 20261006, 20261007],
        selection_rule=(
            "confirmed successes then worst margin; "
            "no successful-event regressions or FP increase; ties baseline"
        ),
        acceptance_rule=(
            "at least one all-seed hazard success conversion, no successful-event regression, "
            "FP upper bound <= baseline lower bound"
        ),
        model_scope=(
            "virtual sensor size; RGB spectral proxy; geometric PSF; "
            "no AEB/closed-loop/physical/product qualification"
        ),
    )
    from .lens_sensor_isp_detector import local_weight_path

    spec["detector"]["weights_sha256"] = file_sha256(local_weight_path())
    spec["capture"]["brightness_multipliers"] = [0.25]
    # Full observation requires real input/ISP support around its boundary.
    # This declared support extent does not enlarge the detector observation.
    spec["geometry"]["horizontal_fov_deg"] = 32.0
    spec["geometry"]["observation_horizontal_fov_deg"] = 30.0
    spec["geometry"]["support_extent_reason"] = "PSF/pixel/ISP halo outside fixed 30deg observation"
    spec["resources"]["maximum_native_roi_cells"] = 100_000_000
    package.mkdir(parents=True)
    for event in spec["events"]:
        for frame in event["frames"]:
            path = package / "inputs" / event["id"] / (frame["id"] + ".jpg")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(frame["source_path"], path)
            frame["source_path"] = str(path)
            frame["source_sha256"] = file_sha256(path)
    write_json(package / "specification.json", spec)
    write_json(
        package / "specification-binding.json",
        {
            "sha256": file_sha256(package / "specification.json"),
            "semantic_sha256": json_sha256(spec),
        },
    )
    write_json(package / "input-audit.json", audit)
    return spec


def bound_specification(package):
    package = Path(package)
    spec = json.loads((package / "specification.json").read_text())
    binding = json.loads((package / "specification-binding.json").read_text())
    if (
        spec.get("schema") != SCHEMA
        or binding["sha256"] != file_sha256(package / "specification.json")
        or binding["semantic_sha256"] != json_sha256(spec)
    ):
        raise ValueError("Frozen pedestrian specification changed.")
    return spec, binding
