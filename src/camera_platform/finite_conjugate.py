"""Explicit finite planar object coordinates shared by ray and scalar PSF factories.

Distance is measured from the first physical surface vertex toward the object.
The signed chart magnification is authored, wavelength-independent geometry;
it neither focuses the lens nor recenters its chromatic/aberrated ray bundles.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np

SCHEMA = "camera_finite_planar_object_v1"
DECLARATIONS = {
    "schema": SCHEMA,
    "reference": "first_surface_vertex",
    "orientation": "normal_toward_sensor",
    "support": "unbounded_uniform_lambertian_plane",
}
MIN_CLEARANCE_MM = 1e-5
MIN_OBJECT_DIRECTION_COSINE = 1e-9
MAX_RAY_DISTANCE_MM = 1e9
MAX_COORDINATE_MM = 1e9


def _number(value, label, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{label} requires a finite number in [{low}, {high}]")
    return float(value)


def validate_conjugate(value, surfaces=None):
    """Normalize exact authored fields; optionally certify first-surface clearance."""
    if not isinstance(value, Mapping) or set(value) != set(DECLARATIONS) | {
        "distance_mm",
        "reference_magnification",
    }:
        raise ValueError("Declare exactly the camera_finite_planar_object_v1 fields")
    if any(value[key] != expected for key, expected in DECLARATIONS.items()):
        raise ValueError("Finite object requires the declared first-vertex planar radiance frame")
    distance = _number(value["distance_mm"], "object distance_mm", 1e-3, 1e6)
    magnification = _number(
        value["reference_magnification"], "object reference_magnification", -10, -1e-6
    )
    if surfaces is not None:
        from .optics_surfaces import surface_bounds

        if not isinstance(surfaces, list) or not surfaces:
            raise ValueError("Object-plane clearance requires physical surfaces")
        front = surface_bounds(surfaces[0])
        clearance = math.nextafter(distance + front["sag_min_mm"], -math.inf)
        if clearance <= MIN_CLEARANCE_MM:
            raise ValueError("Object plane lacks certified clearance before the full front surface")
    return {**DECLARATIONS, "distance_mm": distance, "reference_magnification": magnification}


def application_conditions(value):
    """Exact conditions that must follow the finite prescription into its models."""
    data = validate_conjugate(value)
    return {
        "object_distance_mm": {"value": data["distance_mm"], "unit": "mm"},
        "object_reference_magnification": {
            "value": data["reference_magnification"],
            "unit": "1",
        },
    }


def _coordinates(value, shape, label):
    array = np.asarray(value)
    if (
        array.shape != shape
        or array.dtype.kind not in "fiu"
        or not np.isfinite(array).all()
        or np.any(array < -MAX_COORDINATE_MM)
        or np.any(array > MAX_COORDINATE_MM)
    ):
        raise ValueError(f"{label} requires finite bounded coordinates with shape {shape}")
    return array.astype(float, copy=False)


def object_xy_mm(sensor_xy, conjugate):
    """Map ideal sensor-chart coordinates to the one authored object-plane chart."""
    data = validate_conjugate(conjugate)
    array = np.asarray(sensor_xy)
    if array.ndim < 1 or array.shape[-1] != 2 or array.size > 2_000_000:
        raise ValueError("Sensor chart requires bounded (...,2) coordinates")
    array = _coordinates(array, array.shape, "Sensor chart")
    result = array / data["reference_magnification"]
    if not np.isfinite(result).all() or np.any(np.abs(result) > MAX_COORDINATE_MM):
        raise ValueError("Object chart leaves the bounded finite coordinate domain")
    return result


def intersect_object_plane(position_xyz_mm, direction_xyz, conjugate):
    """Project one reciprocal ray from first-surface coordinates to z=-distance.

    A grazing, backward, nonunit or unbounded ray is an unsupported numerical
    candidate, not a zero-throughput pupil sample. Callers must refuse its model.
    """
    data = validate_conjugate(conjugate)
    point = _coordinates(position_xyz_mm, (3,), "Object exit position")
    direction = _coordinates(direction_xyz, (3,), "Object exit direction")
    if abs(float(np.linalg.norm(direction)) - 1) > 1e-10:
        raise ValueError("Object exit direction must have unit length")
    if direction[2] >= -MIN_OBJECT_DIRECTION_COSINE:
        raise ValueError("Object ray must propagate toward the plane with non-grazing negative z")
    distance = (-data["distance_mm"] - float(point[2])) / float(direction[2])
    if not math.isfinite(distance) or not 0 < distance <= MAX_RAY_DISTANCE_MM:
        raise ValueError("Object-plane ray travel must be finite, positive and bounded")
    hit = point + distance * direction
    if not np.isfinite(hit).all() or np.any(np.abs(hit) > MAX_COORDINATE_MM):
        raise ValueError("Object-plane intersection leaves the bounded coordinate domain")
    tolerance = 128 * np.finfo(float).eps * max(1.0, abs(float(point[2])), distance)
    if abs(float(hit[2]) + data["distance_mm"]) > tolerance:
        raise ValueError("Object-plane intersection failed its axial residual check")
    hit[2] = -data["distance_mm"]
    return hit, distance
