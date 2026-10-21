"""Declared analytic wide-angle projection and direction-defined Lab charts."""

from __future__ import annotations

import math

import numpy as np


def load_geometry(sensor, specification):
    wide = specification["wide_angle"]
    rows, cols = map(int, wide["native_shape_row_col"])
    pitch = float(sensor.fields["pixel"]["size_m"][0]) * 1e3
    hfov = float(specification["field_model"].get("horizontal_fov_deg") or 120)
    radians = math.radians(hfov)
    focal = (
        cols
        * pitch
        / (2 * math.tan(radians / 2) if wide["projection"] == "rectilinear" else radians)
    )
    angle = (
        (lambda radius: 2 * math.atan(radius / focal))
        if wide["projection"] == "rectilinear"
        else (lambda radius: 2 * radius / focal)
    )
    return {
        "optical_model": "wide_parametric",
        "projection": wide["projection"],
        "native_shape": [rows, cols],
        "common_shape": [360, 640],
        "pixel_pitch_um": pitch * 1000,
        "focal_length_mm": focal,
        "horizontal_fov_deg": hfov,
        "source_scene_angular_extent_deg": [hfov, hfov * 9 / 16],
        "color_chart_angular_extent_deg": [hfov * 0.8, hfov * 0.42],
        "vertical_fov_deg": math.degrees(angle(rows * pitch / 2)),
        "diagonal_fov_deg": math.degrees(angle(math.hypot(rows, cols) * pitch / 2)),
        "common_horizontal_fov_deg": hfov,
        "native_horizontal_fov_deg": hfov,
        "field_model": specification["field_model"],
        "wide_angle": wide,
        "kind": "declared_parametric_wide_projection",
        "not_a_product_active_mode": True,
        "source_samples_per_sensor_pixel": wide["sampling"],
        "coordinate_convention": "sensor x right, y row-down; direction z forward",
    }


def sensor_to_direction(x_mm, y_mm, geometry):
    x, y = np.broadcast_arrays(np.asarray(x_mm, float), np.asarray(y_mm, float))
    focal = geometry["focal_length_mm"]
    if geometry["projection"] == "rectilinear":
        norm = np.sqrt(x * x + y * y + focal * focal)
        return np.stack([x / norm, y / norm, np.full_like(x, focal) / norm], axis=-1)
    radius = np.hypot(x, y)
    theta = radius / focal
    scale = np.divide(np.sin(theta), radius, out=np.full_like(radius, 1 / focal), where=radius > 0)
    return np.stack([x * scale, y * scale, np.cos(theta)], axis=-1)


def direction_to_sensor(directions, geometry):
    vector = np.asarray(directions, float)
    vector = vector / np.linalg.norm(vector, axis=-1, keepdims=True)
    if np.any(vector[..., 2] <= 0):
        raise ValueError("Wide Lab charts require forward-hemisphere directions")
    if geometry["projection"] == "rectilinear":
        return geometry["focal_length_mm"] * vector[..., :2] / vector[..., 2, None]
    radius = np.hypot(vector[..., 0], vector[..., 1])
    theta = np.arctan2(radius, vector[..., 2])
    scale = np.divide(
        geometry["focal_length_mm"] * theta, radius, out=np.zeros_like(radius), where=radius > 0
    )
    return vector[..., :2] * scale[..., None]


def pixel_directions(rows, cols, geometry):
    nr, nc = geometry["native_shape"]
    pitch = geometry["pixel_pitch_um"] * 0.001
    return sensor_to_direction(
        (np.asarray(cols) + 0.5 - nc / 2) * pitch,
        (np.asarray(rows) + 0.5 - nr / 2) * pitch,
        geometry,
    )


def direction_angles(directions):
    v = np.asarray(directions)
    return np.stack(
        [np.arctan2(v[..., 0], v[..., 2]), np.arctan2(v[..., 1], np.hypot(v[..., 0], v[..., 2]))],
        axis=-1,
    )


def _angles_to_native(yaw, elevation, geometry):
    v = np.stack(
        [np.sin(yaw) * np.cos(elevation), np.sin(elevation), np.cos(yaw) * np.cos(elevation)],
        axis=-1,
    )
    xy = direction_to_sensor(v, geometry) / (geometry["pixel_pitch_um"] * 0.001)
    return xy + np.array([geometry["native_shape"][1] / 2, geometry["native_shape"][0] / 2])


def resolution_sites(geometry):
    rows, cols = geometry["native_shape"]
    if geometry["projection"] != "rectilinear":
        reference = {
            **geometry,
            "projection": "rectilinear",
            "focal_length_mm": cols
            * geometry["pixel_pitch_um"]
            * 0.001
            / (2 * math.tan(math.radians(geometry["horizontal_fov_deg"]) / 2)),
        }
        sites = resolution_sites(reference)
        for site in sites.values():
            center = direction_angles(
                pixel_directions(
                    site["center_row_col"][0] - 0.5, site["center_row_col"][1] - 0.5, reference
                )
            )
            xy = _angles_to_native(center[0], center[1], geometry)
            site["center_row_col"] = [float(xy[1]), float(xy[0])]
            for label in ("radial", "tangential"):
                edge = site[label]
                source = np.asarray(edge["source_direction_angles_rad"])
                jac = np.linalg.inv(np.asarray(edge["angular_to_local_pixel"]))
                half = (edge["bounds_row_col"][1] - edge["bounds_row_col"][0]) / 2

                def points(x, y, jac=jac, source=source):
                    angle = np.stack(np.broadcast_arrays(x, y), axis=-1) @ jac.T + source
                    return _angles_to_native(angle[..., 0], angle[..., 1], geometry)

                axis = np.linspace(-half, half, 17)
                top = points(axis, -half)[:, 1].max()
                bottom = points(axis, half)[:, 1].min()
                left = points(-half, axis)[:, 0].max()
                right = points(half, axis)[:, 0].min()
                edge["bounds_row_col"] = [
                    math.ceil(top),
                    math.floor(bottom),
                    math.ceil(left),
                    math.floor(right),
                ]
                xy = _angles_to_native(source[0], source[1], geometry)
                cr, cc = xy[1], xy[0]
                actual_jac = np.column_stack(
                    [
                        direction_angles(pixel_directions(cr - 0.5, cc, geometry))
                        - direction_angles(pixel_directions(cr - 0.5, cc - 1, geometry)),
                        direction_angles(pixel_directions(cr, cc - 0.5, geometry))
                        - direction_angles(pixel_directions(cr - 1, cc - 0.5, geometry)),
                    ]
                )
                edge["source_normal_xy"] = edge["normal_xy"]
                normal = actual_jac.T @ np.linalg.solve(jac.T, np.asarray(edge["normal_xy"]))
                edge["normal_xy"] = (normal / np.linalg.norm(normal)).tolist()
        return sites
    scale = min(rows / 1080, cols / 1920)
    half = max(6, round(40 * scale))
    offset = max(8, round(52 * scale))
    sites = {}
    # Shared target fits the narrower vertical support of the equidistant
    # projection at the same horizontal FOV, including both edge windows.
    for ri, ry in enumerate([0.30, 0.5, 0.70]):
        for ci, cx in enumerate([0.20, 0.5, 0.80]):
            row, col = round(ry * rows), round(cx * cols)
            radial = np.array([col - cols / 2, row - rows / 2], float)
            radial = (
                radial / np.linalg.norm(radial)
                if np.linalg.norm(radial) > 0
                else np.array([1.0, 0.0])
            )
            tangential = np.array([-radial[1], radial[0]])
            site = {"center_row_col": [row, col]}
            for label, normal, delta in [
                ("radial", radial, -offset),
                ("tangential", tangential, offset),
            ]:
                # Slight fixed slant avoids an axis-aligned edge's phase ambiguity.
                a = math.radians(5)
                normal = (
                    np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]) @ normal
                )
                cr, cc = row + delta, col
                center = direction_angles(pixel_directions(cr - 0.5, cc - 0.5, geometry))
                jac = np.column_stack(
                    [
                        direction_angles(pixel_directions(cr - 0.5, cc, geometry))
                        - direction_angles(pixel_directions(cr - 0.5, cc - 1, geometry)),
                        direction_angles(pixel_directions(cr, cc - 0.5, geometry))
                        - direction_angles(pixel_directions(cr - 1, cc - 0.5, geometry)),
                    ]
                )
                site[label] = {
                    "bounds_row_col": [cr - half, cr + half, cc - half, cc + half],
                    "normal_xy": normal.tolist(),
                    "source_direction_angles_rad": center.tolist(),
                    "angular_to_local_pixel": np.linalg.inv(jac).tolist(),
                    "half_width_pixels": half + 8 * scale,
                }
            sites[f"row{ri + 1}_col{ci + 1}"] = site
    return sites


def chart_pattern(kind, rows, cols, geometry, sites=None):
    """Scene radiance pattern defined on rays, not on a resized output image."""
    directions = pixel_directions(rows, cols, geometry)
    angles = direction_angles(directions)
    u = 0.5 + angles[..., 0] / math.radians(geometry["horizontal_fov_deg"])
    v = 0.5 + angles[..., 1] / math.radians(geometry["source_scene_angular_extent_deg"][1])
    if kind == "uniform":
        return np.full(u.shape, 0.18)
    if kind == "color":
        u = 0.5 + angles[..., 0] / math.radians(geometry["color_chart_angular_extent_deg"][0])
        v = 0.5 + angles[..., 1] / math.radians(geometry["color_chart_angular_extent_deg"][1])
        return np.clip((v * 4).astype(int), 0, 3) * 6 + np.clip((u * 6).astype(int), 0, 5)
    if kind == "tone":
        return np.geomspace(0.002, 2, 16)[np.clip((u * 16).astype(int), 0, 15)]
    if kind == "grid":
        # Fixed unit-depth frontoparallel plane: straight lines remain straight
        # under rectilinear projection and curve under equidistant projection.
        plane = directions[..., :2] / directions[..., 2, None]
        lines = (abs((plane[..., 0] + 0.125) % 0.25 - 0.125) < 0.006) | (
            abs((plane[..., 1] + 0.125) % 0.25 - 0.125) < 0.006
        )
        return np.where(lines, 0.8, 0.10)
    if kind == "natural":
        sky = v < 0.47
        road = (v >= 0.47) & (abs(u - 0.5) < 0.08 + 0.65 * (v - 0.47))
        value = np.where(sky, 0.5, 0.2)
        value = np.where(road, 0.12, value)
        return np.where(road & (abs(u - 0.5) < 0.005) & (np.sin(v * 70) > 0), 0.8, value)
    value = np.full(u.shape, 0.20)
    for site in (sites or resolution_sites(geometry)).values():
        for label in ("radial", "tangential"):
            edge = site[label]
            delta = angles - np.asarray(edge["source_direction_angles_rad"])
            local = delta @ np.asarray(edge["angular_to_local_pixel"]).T
            mask = np.max(abs(local), axis=-1) < edge["half_width_pixels"]
            sign = local @ np.asarray(edge.get("source_normal_xy", edge["normal_xy"]))
            value = np.where(mask, np.where(sign > 0, 0.75, 0.08), value)
    return value


def angular_chart_rois(geometry, kind):
    """Conservative projected interiors; references retain angular chart IDs."""
    boxes = (
        [(r + 0.30, r + 0.70, c + 0.30, c + 0.70, 4, 6) for r in range(4) for c in range(6)]
        if kind == "color"
        else [(0.46, 0.54, c + 0.30, c + 0.70, 1, 16) for c in range(16)]
    )
    result = []
    extent = (
        geometry["color_chart_angular_extent_deg"]
        if kind == "color"
        else geometry["source_scene_angular_extent_deg"]
    )
    hfov, vfov = map(math.radians, extent)
    for r0, r1, c0, c1, nr, nc in boxes:
        yaw0, yaw1 = (c0 / nc - 0.5) * hfov, (c1 / nc - 0.5) * hfov
        el0, el1 = (r0 / nr - 0.5) * vfov, (r1 / nr - 0.5) * vfov
        top = _angles_to_native(np.linspace(yaw0, yaw1, 17), np.full(17, el0), geometry)[:, 1].max()
        bottom = _angles_to_native(np.linspace(yaw0, yaw1, 17), np.full(17, el1), geometry)[
            :, 1
        ].min()
        left = _angles_to_native(np.full(17, yaw0), np.linspace(el0, el1, 17), geometry)[:, 0].max()
        right = _angles_to_native(np.full(17, yaw1), np.linspace(el0, el1, 17), geometry)[
            :, 0
        ].min()
        bounds = [math.ceil(top), math.floor(bottom), math.ceil(left), math.floor(right)]
        if (
            bounds[0] < 0
            or bounds[1] > geometry["native_shape"][0]
            or bounds[2] < 0
            or bounds[3] > geometry["native_shape"][1]
            or min(bounds[1] - bounds[0], bounds[3] - bounds[2]) < 2
        ):
            result.append(None)
        else:
            result.append(bounds)
    return result
