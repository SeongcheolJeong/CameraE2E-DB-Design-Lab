"""Bounded absolute generation or collected EQE for native R/Gr/Gb/B sites.

The collected response includes CFA, microlens, absorption and collection.
The alternate generation response excludes collection and requires a separate
charge-collection model at execution. Both replace legacy QE and relative
angular response. Cell-normalized FDTD redistribution is unsupported.
"""

from __future__ import annotations

import math

import numpy as np

SCHEMA = "camera_native_pixel_response_v1"
GENERATION_SCHEMA = "camera_native_generation_response_v1"
SITE_NAMES = ("R", "Gr", "Gb", "B")
REFERENCE_PLANE = "sensor_surface_before_pixel_optics"
DIRECTION_CONVENTION = "propagation_into_sensor_x_column_y_row"
RESPONSE_BASIS = "independent_site_collected_electrons_per_incident_pixel_photon"
GENERATION_RESPONSE_BASIS = "independent_site_generated_electrons_per_incident_pixel_photon"
MAX_LUT_VALUES = 262_144
MAX_QUERY_POINTS = 262_144
MAX_SITE_PIXELS = 1_048_576
MAX_NATIVE_COORDINATE = 2**53 - 1

_FIELDS = {
    "schema",
    "site_names",
    "site_index_tile",
    "pixel_pitch_um",
    "reference_plane",
    "direction_convention",
    "polarization",
    "response_basis",
    "wavelength_nm",
    "theta_deg",
    "phi_deg",
    "interpolation",
    "values_axis_order",
    "values",
    "source",
}


def _number(value, name, lower, upper):
    if type(value) not in (int, float) or not lower <= value <= upper or not math.isfinite(value):
        raise ValueError(f"{name} requires a finite real number in [{lower}, {upper}]")
    return float(value)


def _axis(value, name, minimum, maximum, lower, upper):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} requires {minimum}..{maximum} explicit samples")
    axis = [_number(v, name, lower, upper) for v in value]
    if any(b <= a for a, b in zip(axis[:-1], axis[1:], strict=True)):
        raise ValueError(f"{name} must increase strictly")
    return axis


def validate_response(value):
    """Return detached normalized finite JSON; reject unsupported physics and axes.

    Limits: 256 wavelengths (200..2500 nm), 91 theta nodes (0..89.9 degrees),
    144 phi nodes and 262144 scalar LUT values. Oblique responses require a
    complete periodic phi axis with gaps at most 90 degrees. Normal-only models
    may use phi=[0]. No allocation proportional to a malformed LUT is needed.
    """
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("Declare the exact native pixel response fields")
    schema = value["schema"]
    if type(schema) is not str or schema not in (SCHEMA, GENERATION_SCHEMA):
        raise ValueError("Unsupported native pixel schema")
    expected = {
        "schema": schema,
        "site_names": list(SITE_NAMES),
        "reference_plane": REFERENCE_PLANE,
        "direction_convention": DIRECTION_CONVENTION,
        "polarization": "unpolarized",
        "response_basis": (RESPONSE_BASIS if schema == SCHEMA else GENERATION_RESPONSE_BASIS),
        "interpolation": "linear_wavelength_theta_periodic_phi",
        "values_axis_order": ["wavelength", "theta", "phi", "site"],
    }
    for name, supported in expected.items():
        if type(value[name]) is not type(supported) or value[name] != supported:
            raise ValueError(f"Unsupported native pixel {name}; expected {supported!r}")
    tile = value["site_index_tile"]
    if (
        not isinstance(tile, list)
        or len(tile) != 2
        or any(
            not isinstance(row, list) or len(row) != 2 or any(type(v) is not int for v in row)
            for row in tile
        )
        or tile != [[0, 1], [2, 3]]
    ):
        raise ValueError("site_index_tile must preserve native R/Gr/Gb/B as [[0,1],[2,3]]")
    pitch = _number(value["pixel_pitch_um"], "pixel_pitch_um", 0.05, 100)
    wave = _axis(value["wavelength_nm"], "wavelength_nm", 2, 256, 200, 2500)
    theta = _axis(value["theta_deg"], "theta_deg", 1, 91, 0, 89.9)
    phi = _axis(value["phi_deg"], "phi_deg", 1, 144, 0, 360)
    if theta[0] != 0 or phi[0] != 0 or phi[-1] >= 360:
        raise ValueError("Theta and phi start at zero; phi has no duplicate 360-degree node")
    if len(phi) == 1:
        if len(theta) != 1:
            raise ValueError("Oblique response requires full periodic phi coverage")
    elif len(phi) < 4 or max(b - a for a, b in zip(phi, [*phi[1:], 360], strict=True)) > 90:
        raise ValueError("Periodic phi requires at least four nodes with gaps at most 90 degrees")
    shape = (len(wave), len(theta), len(phi), 4)
    if math.prod(shape) > MAX_LUT_VALUES:
        raise ValueError(f"Native pixel LUT exceeds {MAX_LUT_VALUES} scalar values")
    source = value["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"kind", "description"}
        or not isinstance(source["kind"], str)
        or source["kind"] not in ("measured", "estimated", "simulated")
        or not isinstance(source["description"], str)
        or not 1 <= len(source["description"].strip()) <= 4000
    ):
        raise ValueError(
            "source requires kind and a nonempty description of at most 4000 characters"
        )

    def copy_values(node, dimension):
        if dimension == len(shape):
            return _number(node, "values absolute independent-site EQE", 0, 1)
        if not isinstance(node, list) or len(node) != shape[dimension]:
            raise ValueError("values must have exact shape [wavelength,theta,phi,4]")
        return [copy_values(child, dimension + 1) for child in node]

    values = copy_values(value["values"], 0)
    if any(any(sites != plane[0][0] for sites in plane[0]) for plane in values):
        raise ValueError("Normal-incidence EQE must be identical at every phi for each site")
    return {
        **expected,
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": pitch,
        "wavelength_nm": wave,
        "theta_deg": theta,
        "phi_deg": phi,
        "values": values,
        "source": {"kind": source["kind"], "description": source["description"]},
    }


def _query(value, name):
    # Check list members before NumPy can coerce bool into an ordinary integer.
    def count(node, depth=0):
        if isinstance(node, (list, tuple)):
            if depth >= 32 or len(node) > MAX_QUERY_POINTS:
                raise ValueError(f"{name} exceeds the bounded query shape")
            total = 0
            for child in node:
                total += count(child, depth + 1)
                if total > MAX_QUERY_POINTS:
                    raise ValueError(f"{name} exceeds the bounded query size")
            return total
        if isinstance(node, np.ndarray):
            if node.dtype.kind not in "iuf" or node.size > MAX_QUERY_POINTS:
                raise ValueError(f"{name} requires a bounded real numeric array")
            return node.size
        if isinstance(node, (bool, np.bool_)) or not isinstance(
            node, (int, float, np.integer, np.floating)
        ):
            raise ValueError(f"{name} requires real numeric values; booleans are unsupported")
        return 1

    count(value)
    try:
        array = np.asarray(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} requires a rectangular numeric array") from exc
    if array.dtype.kind not in "iuf" or not np.isfinite(array).all():
        raise ValueError(f"{name} requires finite real values")
    return array


def _bracket(axis, query):
    if len(axis) == 1:
        lower = np.zeros(query.shape, dtype=np.intp)
        return lower, lower, np.zeros(query.shape)
    lower = np.clip(np.searchsorted(axis, query, side="right") - 1, 0, len(axis) - 2)
    return lower, lower + 1, (query - axis[lower]) / (axis[lower + 1] - axis[lower])


def evaluate(response, wavelength_nm, theta_deg, phi_deg):
    """Return declared generation/EQE with query shape plus the final four sites.

    Phi wraps periodically for any finite angle. Wavelength/theta must lie in
    the supplied closed domain. Interpolation is a convex trilinear combination;
    neither normal-incidence division nor normalization/clipping is performed.
    At most 262144 query positions are allocated, independently of LUT size.
    """
    data = validate_response(response)
    queries = [
        _query(value, name)
        for value, name in (
            (wavelength_nm, "wavelength_nm"),
            (theta_deg, "theta_deg"),
            (phi_deg, "phi_deg"),
        )
    ]
    try:
        shape = np.broadcast_shapes(*(array.shape for array in queries))
    except ValueError as exc:
        raise ValueError("Wavelength, theta and phi query shapes cannot broadcast") from exc
    if math.prod(shape) > MAX_QUERY_POINTS:
        raise ValueError(f"Broadcast query exceeds {MAX_QUERY_POINTS} positions")
    wave, theta, phi = [np.broadcast_to(array, shape) for array in queries]
    if np.any((wave < data["wavelength_nm"][0]) | (wave > data["wavelength_nm"][-1])):
        raise ValueError("Wavelength exceeds supplied response domain; no extrapolation")
    if np.any((theta < 0) | (theta > data["theta_deg"][-1])):
        raise ValueError("Theta exceeds supplied response domain; no extrapolation")
    wi, wj, fw = _bracket(np.asarray(data["wavelength_nm"]), wave)
    ti, tj, ft = _bracket(np.asarray(data["theta_deg"]), theta)
    axis = np.asarray(data["phi_deg"])
    # Keep integer periodic reduction exact beyond float64's consecutive range.
    phi = np.mod(phi, 360)
    pi = np.searchsorted(axis, phi, side="right") - 1
    pj = (pi + 1) % len(axis)
    upper = np.where(pj == 0, 360.0, axis[pj])
    fp = (phi - axis[pi]) / (upper - axis[pi])
    values = np.asarray(data["values"])
    result = np.zeros((*shape, 4), dtype=np.float64)
    for w, weight_w in ((wi, 1 - fw), (wj, fw)):
        for t, weight_t in ((ti, 1 - ft), (tj, ft)):
            for p, weight_p in ((pi, 1 - fp), (pj, fp)):
                result += values[w, t, p] * (weight_w * weight_t * weight_p)[..., None]
    if not np.isfinite(result).all():
        raise ValueError("Nonfinite native pixel interpolation")
    return result


def site_index(shape, origin):
    """Return uint8 native R/Gr/Gb/B indices for a bounded crop, without RGB merging."""
    for value, name, lower, upper in (
        (shape, "shape", 1, MAX_SITE_PIXELS),
        (origin, "origin", 0, MAX_NATIVE_COORDINATE),
    ):
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or any(type(v) is not int or not lower <= v <= upper for v in value)
        ):
            raise ValueError(f"{name} requires two integers in [{lower}, {upper}]")
    if math.prod(shape) > MAX_SITE_PIXELS:
        raise ValueError(f"Native site map exceeds {MAX_SITE_PIXELS} pixels")
    rows = (np.arange(shape[0], dtype=np.uint32) + origin[0] % 2) % 2
    cols = (np.arange(shape[1], dtype=np.uint32) + origin[1] % 2) % 2
    return (2 * rows[:, None] + cols[None, :]).astype(np.uint8)
