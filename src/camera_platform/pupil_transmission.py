"""Authored passive radial stop power transmission, shared by rays and diffraction.

This neutral scalar mask adds no phase. It is not a coating, Fresnel, angular,
polarization, or general pupil-amplitude transport model.
"""

from __future__ import annotations

import hashlib
import json
import math

import numpy as np


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
        ).encode()
    ).hexdigest()


def validate_transmission(spec):
    """Return a normalized exact finite JSON contract; never infer spectral coverage."""
    try:
        data = json.loads(json.dumps(spec, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("Stop transmission requires finite JSON") from exc
    keys = {
        "schema",
        "stop_surface_id",
        "wavelength_nm",
        "normalized_radius",
        "power_transmittance",
        "interpolation",
        "source",
    }
    if not isinstance(data, dict) or set(data) != keys:
        raise ValueError("Declare the exact camera_stop_transmission_v1 fields")
    if data["schema"] != "camera_stop_transmission_v1" or data["interpolation"] != "linear_power":
        raise ValueError("Only camera_stop_transmission_v1 with linear_power is supported")
    if not isinstance(data["stop_surface_id"], str) or not 1 <= len(data["stop_surface_id"]) <= 100:
        raise ValueError("Stop transmission requires the explicit stop surface ID")
    source = data["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"kind", "description"}
        or source["kind"] not in ("measured", "simulated", "estimated")
        or not isinstance(source["description"], str)
        or not 1 <= len(source["description"].strip()) <= 4000
    ):
        raise ValueError("Stop transmission requires source kind and nonempty description")

    def axis(key, low, high, maximum):
        values = data[key]
        if (
            not isinstance(values, list)
            or not 2 <= len(values) <= maximum
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or not low <= v <= high
                for v in values
            )
            or any(b <= a for a, b in zip(values[:-1], values[1:], strict=True))
        ):
            raise ValueError(f"{key} requires finite increasing explicit samples")
        data[key] = [float(v) for v in values]

    axis("wavelength_nm", 350, 1100, 16)
    axis("normalized_radius", 0, 1, 129)
    if data["normalized_radius"][0] != 0 or data["normalized_radius"][-1] != 1:
        raise ValueError("Radial transmission must cover the full stop radius from 0 to 1")
    values = data["power_transmittance"]
    if not isinstance(values, list) or len(values) != len(data["wavelength_nm"]):
        raise ValueError("Power transmission requires one row per exact wavelength")
    for row in values:
        if (
            not isinstance(row, list)
            or len(row) != len(data["normalized_radius"])
            or any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in row
            )
        ):
            raise ValueError(
                "Passive power transmission requires a finite [wavelength,radius] LUT in [0,1]"
            )
    data["power_transmittance"] = [[float(v) for v in row] for row in values]
    return data


def bind_transmission(spec, prescription):
    """Bind a mask to the authored unpowered plane stop and exact wavelengths."""
    data = validate_transmission(spec)
    stops = [s for s in prescription["surfaces"] if s["is_stop"]]
    if len(stops) != 1 or data["stop_surface_id"] != stops[0]["id"]:
        raise ValueError("Transmission must reference the single explicit stop")
    stop = stops[0]
    index = prescription["surfaces"].index(stop)
    before = (
        [1.0] * len(prescription["wavelength_nm"])
        if index == 0
        else prescription["surfaces"][index - 1]["refractive_index_after"]
    )
    if (
        stop["profile"] != "plane"
        or stop["radius_mm"] is not None
        or stop["refractive_index_after"] != before
    ):
        raise ValueError("Transmission only supports an unpowered plane stop in unchanged medium")
    if data["wavelength_nm"] != prescription["wavelength_nm"]:
        raise ValueError("Transmission wavelengths must exactly match the prescription")
    return data


def sample_power(spec, wavelength_nm, normalized_radius):
    """Evaluate a validated mask at an actual stop-hit radius; no extrapolation."""
    if type(wavelength_nm) not in (int, float) or wavelength_nm not in spec["wavelength_nm"]:
        raise ValueError("Transmission has no exact requested wavelength")
    radius = np.asarray(normalized_radius)
    if (
        radius.dtype.kind not in "iuf"
        or not np.isfinite(radius).all()
        or np.any((radius < 0) | (radius > 1))
    ):
        raise ValueError("Transmission radius lies outside the declared physical stop")
    return np.interp(
        radius,
        spec["normalized_radius"],
        spec["power_transmittance"][spec["wavelength_nm"].index(wavelength_nm)],
    )


def power_at_stop(spec, wavelength_nm, point_mm, semi_diameter_mm):
    """Use local coordinates from a complete ray at the exact declared plane stop."""
    point = np.asarray(point_mm)
    if point.shape != (3,) or not np.isfinite(point).all() or abs(point[2]) > 1e-7:
        raise ValueError("Transmission requires a finite intersection on its physical plane stop")
    if not math.isfinite(semi_diameter_mm) or semi_diameter_mm <= 0:
        raise ValueError("Transmission requires a positive physical stop radius")
    radius = math.hypot(float(point[0]), float(point[1])) / semi_diameter_mm
    # No aperture widening or extrapolation. The ray tracer has already checked
    # the physical clear aperture with zero aperture fuzz.
    return float(sample_power(spec, wavelength_nm, radius))


def uniform_transmission(prescription, value=1.0):
    stop = next(s for s in prescription["surfaces"] if s["is_stop"])
    return bind_transmission(
        {
            "schema": "camera_stop_transmission_v1",
            "stop_surface_id": stop["id"],
            "wavelength_nm": prescription["wavelength_nm"],
            "normalized_radius": [0, 1],
            "power_transmittance": [[value, value] for _ in prescription["wavelength_nm"]],
            "interpolation": "linear_power",
            "source": {
                "kind": "simulated",
                "description": "Authored passive neutral stop mask; no physical validation",
            },
        },
        prescription,
    )
