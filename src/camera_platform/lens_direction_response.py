"""Joint image-space ray directions and conditional transmitted power.

Off-axis samples keep their original direction/power pairing. At the axis a
traced ray represents a complete annulus, so the declared finite azimuth
quadrature rotates that ray and divides its power equally. This is justified
only by the centered prescription and radial stop, not by independent theta
and phi marginals. No extra solid-angle, stop-loss, or sine factor applies.
"""

from __future__ import annotations

import json
import math
import re

import numpy as np

from .finite_conjugate import validate_conjugate
from .pupil_transmission import digest

MAX_BASE_SAMPLES = 8192
MAX_WEIGHTED_BASE_SAMPLES = 52_428
MAX_EXPANDED_SAMPLES = 1_000_000
MAX_CHUNK_SAMPLES = 131_072
_DECLARATIONS = {
    "schema": "camera_lens_directional_response_v1",
    "direction_convention": "propagation_into_sensor_x_column_y_row",
    "reference_plane": "sensor_surface_before_pixel_optics",
    "field_reference": "positive_x_column",
    "symmetry": "centered_spherical_or_plane_prescription_radial_stop",
    "normalization": "conditional_transmitted_irradiance_fraction",
}
_ASPHERE_DECLARATIONS = {
    **_DECLARATIONS,
    "schema": "camera_lens_directional_response_v2",
    "symmetry": "centered_even_asphere_or_spherical_or_plane_prescription_radial_stop",
}
_FINITE_DECLARATIONS = {
    **_ASPHERE_DECLARATIONS,
    "schema": "camera_lens_directional_response_v3",
}


def _number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} requires a finite number in [{low}, {high}]")
    return float(value)


def _axis(value, name, low, high, maximum):
    if not isinstance(value, list) or not 2 <= len(value) <= maximum:
        raise ValueError(f"{name} requires 2..{maximum} explicit nodes")
    axis = [_number(v, name, low, high) for v in value]
    if any(b <= a for a, b in zip(axis[:-1], axis[1:], strict=True)):
        raise ValueError(f"{name} must increase strictly")
    return axis


def _array(value, shape, name, low, high):
    def visit(part, remaining):
        if not remaining:
            return _number(part, name, low, high)
        if not isinstance(part, list) or len(part) != remaining[0]:
            raise ValueError(f"{name} requires shape {shape}")
        return [visit(item, remaining[1:]) for item in part]

    return np.asarray(visit(value, shape), dtype=float)


def validate_directional_response(response):
    """Validate and copy the finite JSON response, without authorizing its source.

    Publication must additionally bind both source hashes and these exact
    samples to the original Factory evidence. A valid authored structure alone
    is not evidence of ray tracing or physical accuracy.
    """
    keys = set(_DECLARATIONS) | {
        "wavelength_nm",
        "field_radius",
        "image_height_mm",
        "field_reference_radius_mm",
        "prescription_sha256",
        "stop_transmission_sha256",
        "directions_xyz",
        "conditional_weights",
        "on_axis_rotation_phi_deg",
    }
    finite = isinstance(response, dict) and response.get("schema") == (
        "camera_lens_directional_response_v3"
    )
    if finite:
        keys.add("object_conjugate")
    if not isinstance(response, dict) or set(response) != keys:
        raise ValueError("Declare exactly the camera_lens_directional_response_v1/v2/v3 fields")
    declarations = (
        _FINITE_DECLARATIONS
        if finite
        else _ASPHERE_DECLARATIONS
        if response.get("schema") == "camera_lens_directional_response_v2"
        else _DECLARATIONS
    )
    if any(response[key] != value for key, value in declarations.items()):
        raise ValueError("Unsupported joint direction convention, reference plane, or symmetry")
    for key in ("prescription_sha256", "stop_transmission_sha256"):
        if not isinstance(response[key], str) or not re.fullmatch(r"[0-9a-f]{64}", response[key]):
            raise ValueError(f"{key} requires the complete normalized source SHA-256")
    wave = _axis(response["wavelength_nm"], "wavelength_nm", 350, 1100, 16)
    fields = _axis(response["field_radius"], "field_radius", 0, 1, 17)
    heights = _axis(response["image_height_mm"], "image_height_mm", 0, 500, 17)
    reference = _number(response["field_reference_radius_mm"], "field_reference_radius_mm", 0, 500)
    if (
        len(fields) != len(heights)
        or fields[0] != 0
        or fields[-1] != 1
        or heights[0] != 0
        or reference != heights[-1]
        or not np.allclose(fields, np.asarray(heights) / reference, atol=1e-12, rtol=0)
    ):
        raise ValueError(
            "Field radii must cover center/edge and match exact physical image heights"
        )
    rotations = response["on_axis_rotation_phi_deg"]
    if not isinstance(rotations, list) or not 8 <= len(rotations) <= 128 or len(rotations) % 4:
        raise ValueError("On-axis azimuth quadrature requires 8..128 nodes, a multiple of four")
    rotations = np.array([_number(v, "on_axis_rotation_phi_deg", 0, 360) for v in rotations])
    expected = np.arange(len(rotations)) * (360.0 / len(rotations))
    if not np.allclose(rotations, expected, rtol=0, atol=1e-12):
        raise ValueError("On-axis rotation nodes must be uniform, start at zero, and exclude 360")
    directions = response["directions_xyz"]
    if not isinstance(directions, list) or not len(directions) == len(fields):
        raise ValueError("directions_xyz requires one direction sample array per field")
    if not isinstance(directions[0], list) or not 1 <= len(directions[0]) <= MAX_BASE_SAMPLES:
        raise ValueError(f"directions_xyz requires 1..{MAX_BASE_SAMPLES} base samples per field")
    count = len(directions[0])
    if len(wave) * len(fields) * count > MAX_WEIGHTED_BASE_SAMPLES:
        raise ValueError("Joint direction payload exceeds the Factory weighted base sample limit")
    if len(wave) * count * (len(rotations) + len(fields) - 1) > MAX_EXPANDED_SAMPLES:
        raise ValueError("Joint direction payload exceeds one million expanded quadrature samples")
    xyz = _array(directions, (len(fields), count, 3), "directions_xyz", -1, 1)
    if not np.allclose(np.linalg.norm(xyz, axis=-1), 1, atol=1e-12, rtol=0):
        raise ValueError("Image-space direction vectors must have unit length")
    if np.any(xyz[..., 2] <= math.cos(math.radians(80))):
        raise ValueError("Image-space directions must propagate into the sensor below 80 degrees")
    if np.any(np.abs(xyz[0, :, 1]) > 1e-12) or np.any(xyz[0, :, 0] > 1e-12):
        raise ValueError("On-axis base rays must preserve the traced negative-x annulus meridian")
    power = _array(
        response["conditional_weights"],
        (len(wave), len(fields), count),
        "conditional_weights",
        0,
        1,
    )
    if not np.allclose(power.sum(axis=-1), 1, atol=1e-12, rtol=0):
        raise ValueError("Conditional transmitted power must sum to one at every wavelength/field")
    result = dict(declarations)
    result.update(
        wavelength_nm=wave,
        field_radius=fields,
        image_height_mm=heights,
        field_reference_radius_mm=reference,
        prescription_sha256=response["prescription_sha256"],
        stop_transmission_sha256=response["stop_transmission_sha256"],
        directions_xyz=xyz.tolist(),
        conditional_weights=power.tolist(),
        on_axis_rotation_phi_deg=rotations.tolist(),
    )
    if finite:
        result["object_conjugate"] = validate_conjugate(response["object_conjugate"])
    return result


def from_ray_samples(prescription, arrays):
    """Build directions from v3/v4 traced rays, acceptance, and stop power.

    The caller supplies the validated full prescription and original fine-pass
    arrays. Both the absolute integral and its joint conditional distribution
    use the same accepted quadrature power; no theta histogram is inverted.
    """
    if prescription.get("schema") not in {
        "lens_prescription_v3", "lens_prescription_v4", "lens_prescription_v5",
    }:
        raise ValueError("Joint directions require explicit lens_prescription_v3, v4 or v5")
    finite = prescription["schema"] == "lens_prescription_v5"
    if finite and not np.array_equal(arrays["accepted_rays"], arrays["object_plane_valid"]):
        raise ValueError("Finite joint directions require the original valid object-plane rays")
    power = (
        np.asarray(arrays["quadrature_weights_sr"])[None, :, :]
        * np.asarray(arrays["accepted_rays"])
        * np.asarray(arrays["stop_power_transmittance"])
    )
    total = power.sum(axis=-1)
    if not np.isfinite(power).all() or np.any(power < 0) or np.any(total <= 0):
        raise ValueError("Joint direction normalization requires positive finite transmitted power")
    if not np.allclose(total, arrays["irradiance_per_radiance_sr"], atol=0, rtol=1e-12):
        raise ValueError("Joint power must match the original absolute irradiance integral")
    count = prescription["sampling"]["on_axis_azimuth_samples"]
    return validate_directional_response(
        {
            **(
                _FINITE_DECLARATIONS
                if finite
                else _ASPHERE_DECLARATIONS
                if prescription["schema"] == "lens_prescription_v4"
                else _DECLARATIONS
            ),
            "wavelength_nm": prescription["wavelength_nm"],
            "field_radius": prescription["field_radius"],
            "image_height_mm": prescription["image_height_mm"],
            "field_reference_radius_mm": prescription["image_height_mm"][-1],
            "prescription_sha256": digest(prescription),
            "stop_transmission_sha256": digest(prescription["stop_transmission"]),
            "directions_xyz": np.asarray(arrays["image_direction_cosines"]).tolist(),
            "conditional_weights": (power / total[:, :, None]).tolist(),
            "on_axis_rotation_phi_deg": (np.arange(count) * (360.0 / count)).tolist(),
            **({"object_conjugate": prescription["object_conjugate"]} if finite else {}),
        }
    )


def iter_direction_samples(
    response,
    wave_index,
    field_index,
    rotation_phi_deg=0,
    chunk_size=4096,
):
    """Yield positive-power ``(xyz[N,3], weights[N])`` in bounded chunks.

    Rotate the complete joint distribution by the native field azimuth (+x
    column toward +y row). Field interpolation is a mixture of separately
    averaged field-node responses, not interpolation of corresponding rays.
    Axis samples use exactly the declared finite azimuth quadrature; this is
    not an analytic continuous-phi average. Zero-power rays are never queried.
    """
    data = validate_directional_response(response)
    for index, size, name in (
        (wave_index, len(data["wavelength_nm"]), "wave_index"),
        (field_index, len(data["field_radius"]), "field_index"),
    ):
        if type(index) is not int or not 0 <= index < size:
            raise ValueError(f"{name} is outside the exact response nodes")
    if type(chunk_size) is not int or not 1 <= chunk_size <= MAX_CHUNK_SAMPLES:
        raise ValueError(f"chunk_size must be an integer in [1, {MAX_CHUNK_SAMPLES}]")
    angle = _number(rotation_phi_deg, "rotation_phi_deg", -1e12, 1e12) % 360
    xyz = np.asarray(data["directions_xyz"][field_index])
    power = np.asarray(data["conditional_weights"][wave_index][field_index])
    positive = power > 0
    xyz, power = xyz[positive], power[positive]
    rotations = data["on_axis_rotation_phi_deg"] if field_index == 0 else [0]
    for offset in rotations:
        rad = math.radians((angle + offset) % 360)
        cosine, sine = math.cos(rad), math.sin(rad)
        for start in range(0, len(power), chunk_size):
            source = xyz[start : start + chunk_size]
            rotated = source.copy()
            rotated[:, 0] = cosine * source[:, 0] - sine * source[:, 1]
            rotated[:, 1] = sine * source[:, 0] + cosine * source[:, 1]
            yield rotated, power[start : start + chunk_size] / len(rotations)


def response_moments(response):
    """Finite quadrature first/second direction moments; not an accuracy bound."""
    data = validate_directional_response(response)
    shape = (len(data["wavelength_nm"]), len(data["field_radius"]))
    first, second = np.zeros((*shape, 3)), np.zeros((*shape, 3, 3))
    counts = np.zeros(shape, dtype=np.int64)
    for w in range(shape[0]):
        for f in range(shape[1]):
            for xyz, weights in iter_direction_samples(data, w, f):
                first[w, f] += np.einsum("n,ni->i", weights, xyz)
                second[w, f] += np.einsum("n,ni,nj->ij", weights, xyz, xyz)
                counts[w, f] += len(weights)
    # Keep all summary values JSON-safe and make no inference about the Sensor.
    result = {
        "mean_direction_xyz": first.tolist(),
        "second_direction_moment_xyz": second.tolist(),
        "positive_expanded_sample_count": counts.tolist(),
    }
    json.dumps(result, allow_nan=False)
    return result
