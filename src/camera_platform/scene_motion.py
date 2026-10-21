"""Resolve declared 3D translation at one exact simulation-clock sample.

The result is an instantaneous scene. Exposure-time radiance between separately
rendered knots is an explicit ideal-image interpolation approximation downstream.
"""

from __future__ import annotations

import copy

import numpy as np

from .scene_contracts import Instance, Pose, Scene


def resolve(request, scene):
    """Return validated scene and camera poses at a frozen relative-ns timestamp."""
    source = scene["payload"]
    source_clock = source.get("clock")
    clock = request.get("render_clock")
    time_ns = request.get("render_time_ns")
    if (clock is None) != (time_ns is None):
        raise ValueError("Render clock and nanosecond time must be supplied together")
    if source_clock is not None and clock != source_clock:
        raise ValueError("Render clock differs from the immutable 3D scene clock")
    moving_instances = [i for i, item in enumerate(source["instances"])
                        if item.get("motion") is not None]
    moving_camera = request["pose"].get("motion") is not None
    if (source_clock is not None or moving_instances or moving_camera) and time_ns is None:
        raise ValueError("Moving 3D geometry or camera requires an explicit render time")
    if time_ns is None:
        return None
    if type(time_ns) is not int or not -(2**53 - 1) <= time_ns <= 2**53 - 1:
        raise ValueError("Render time must be an exact relative nanosecond integer")
    seconds = time_ns * 1e-9
    resolved_scene = copy.deepcopy(scene)
    initial_positions = [copy.deepcopy(item["to_world"])
                         for item in source["instances"]]
    positions = []
    for item in resolved_scene["payload"]["instances"]:
        motion = item.pop("motion", None)
        if motion is not None:
            matrix = np.asarray(item["to_world"], dtype=np.float64)
            delta = np.asarray(motion["velocity_m_s"]) * seconds
            if not np.isfinite(delta).all() or np.max(np.abs(delta)) > 1e6:
                raise ValueError("Moving 3D object displacement exceeds 1e6 m")
            matrix[:3, 3] += delta
            item["to_world"] = matrix.tolist()
        validated = Instance.model_validate(item)
        positions.append(validated.to_world)
    Scene.model_validate(resolved_scene["payload"])
    pose = copy.deepcopy(request["pose"])
    initial_pose = copy.deepcopy(pose)
    camera_motion = pose.pop("motion", None)
    if camera_motion is not None:
        delta = np.asarray(camera_motion["velocity_m_s"]) * seconds
        if not np.isfinite(delta).all() or np.max(np.abs(delta)) > 1e6:
            raise ValueError("Moving camera displacement exceeds 1e6 m")
        pose["origin_m"] = (np.asarray(pose["origin_m"]) + delta).tolist()
        pose["target_m"] = (np.asarray(pose["target_m"]) + delta).tolist()
    Pose.model_validate(pose)
    return {
        "scene": resolved_scene,
        "pose": pose,
        "evidence": {
            "schema": "sampled_3d_linear_translation_v1",
            "render_clock": copy.deepcopy(clock),
            "render_time_ns": time_ns,
            "motion_model": "world_frame_linear_translation_from_clock_zero",
            "moving_instance_indices": moving_instances,
            "moving_camera": moving_camera,
            "time_zero_instance_to_world": initial_positions,
            "time_zero_camera_pose": initial_pose,
            "resolved_instance_to_world": positions,
            "resolved_camera_pose": copy.deepcopy(pose),
            "radiance_sample": "instantaneous_3d_scene_at_render_time",
            "between_knot_geometry": "not_evaluated_by_renderer",
            "physical_validation": "not_established",
        },
    }
