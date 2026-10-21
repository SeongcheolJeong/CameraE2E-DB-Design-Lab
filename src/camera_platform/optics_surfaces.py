"""Bounded centered even-asphere geometry, shared by both optical Factories.

Interval enclosures certify the whole clear aperture. Trace intersections are
restricted to that finite surface patch and require a monotone line/sag
equation. An uncertifiable ray is a numerical refusal, not vignetting.
"""

from __future__ import annotations

import copy
import math

import numpy as np

SCHEMA = "camera_even_asphere_v1"
ORDERS = [4, 6, 8]
UNITS = ["mm^-3", "mm^-5", "mm^-7"]
SCHEMA_A10 = "camera_even_asphere_v2"
ORDERS_A10 = [4, 6, 8, 10]
UNITS_A10 = ["mm^-3", "mm^-5", "mm^-7", "mm^-9"]
SCHEMA_A12 = "camera_even_asphere_v3"
ORDERS_A12 = [4, 6, 8, 10, 12]
UNITS_A12 = ["mm^-3", "mm^-5", "mm^-7", "mm^-9", "mm^-11"]
CERTIFICATE_INTERVALS = 256
MAX_ROOT_ITERATIONS = 96
MIN_RADICAND = 1e-10
MAX_SAG_MM = 5000.0
MAX_SLOPE = 4.0
MIN_CLEARANCE_MM = 1e-6
ROOT_RESIDUAL_MM = 1e-9
MAX_SURFACES = 40


class AsphereIntersectionError(ValueError):
    """The finite asphere intersection cannot be uniquely certified."""


def _finite(value, label, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{label} requires a finite number in [{low},{high}]")
    return float(value)


def validate_asphere(surface):
    """Validate the explicit v4-only asphere member without changing the caller."""
    if surface.get("profile") != "even_asphere" or surface.get("is_stop") is not False:
        raise ValueError("An even asphere must be a non-stop centered refracting surface")
    radius = _finite(surface.get("radius_mm"), "radius_mm", -1e6, 1e6)
    if abs(radius) < 1e-6:
        raise ValueError("An even asphere requires finite nonzero signed vertex radius")
    value = surface.get("asphere")
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "conic_constant",
        "orders",
        "coefficients",
        "coefficient_units",
    }:
        raise ValueError("Declare exactly the camera_even_asphere_v1 fields")
    contracts = {
        SCHEMA: (ORDERS, UNITS),
        SCHEMA_A10: (ORDERS_A10, UNITS_A10),
        SCHEMA_A12: (ORDERS_A12, UNITS_A12),
    }
    contract = contracts.get(value["schema"])
    if (
        contract is None
        or value["orders"] != contract[0]
        or any(type(v) is not int for v in value["orders"])
        or value["coefficient_units"] != contract[1]
    ):
        raise ValueError(
            "Use explicit v1 A4/A6/A8, v2 A4/A6/A8/A10 or v3 A4/A6/A8/A10/A12 "
            "with matching mm units; no A2"
        )
    coefficients = value["coefficients"]
    if not isinstance(coefficients, list) or len(coefficients) != len(contract[0]):
        raise ValueError("Declare exactly the coefficients in the selected asphere order")
    return {
        "schema": value["schema"],
        "conic_constant": _finite(value["conic_constant"], "conic_constant", -100, 100),
        "orders": contract[0].copy(),
        "coefficients": [_finite(v, "asphere coefficient", -1e12, 1e12) for v in coefficients],
        "coefficient_units": contract[1].copy(),
    }


def _parameters(surface):
    radius = surface["radius_mm"]
    if surface["profile"] == "plane":
        return 0.0, 0.0, [], []
    if surface["profile"] == "even_asphere":
        data = surface["asphere"]
        return 1.0 / radius, data["conic_constant"], data["coefficients"], data["orders"]
    return 1.0 / radius, 0.0, [], []


def sag(surface, radius):
    c, k, coefficients, orders = _parameters(surface)
    r = float(radius)
    q = 1.0 - (1.0 + k) * c * c * r * r
    if not math.isfinite(r) or r < 0 or q <= 0:
        raise ValueError("Surface radial coordinate leaves its real finite sag domain")
    return c * r * r / (1.0 + math.sqrt(q)) + sum(
        a * r**order for a, order in zip(coefficients, orders, strict=True)
    )


def radial_derivative(surface, radius):
    c, k, coefficients, orders = _parameters(surface)
    r = float(radius)
    q = 1.0 - (1.0 + k) * c * c * r * r
    if not math.isfinite(r) or r < 0 or q <= 0:
        raise ValueError("Surface derivative leaves its real finite domain")
    return c * r / math.sqrt(q) + sum(
        order * a * r ** (order - 1) for a, order in zip(coefficients, orders, strict=True)
    )


def _out(low, high):
    return math.nextafter(low, -math.inf), math.nextafter(high, math.inf)


def _add(a, b):
    return _out(a[0] + b[0], a[1] + b[1])


def _mul(a, b):
    values = [x * y for x in a for y in b]
    return _out(min(values), max(values))


def _divide(a, b):
    if b[0] <= 0 <= b[1]:
        raise AsphereIntersectionError("Cannot certify division across zero")
    return _mul(a, _out(1.0 / b[1], 1.0 / b[0]))


def _square(a):
    if a[0] <= 0 <= a[1]:
        return 0.0, math.nextafter(max(a[0] ** 2, a[1] ** 2), math.inf)
    return _out(min(a[0] ** 2, a[1] ** 2), max(a[0] ** 2, a[1] ** 2))


def _finite_patch_interval(point, direction, aperture, certificate):
    """Enclose line parameters crossing the finite surface's cylinder and slab.

    Return None only when the enclosures prove disjointness. This comes before
    monotonicity admission: a grazing ray demonstrably outside the patch is a
    physical miss, while one crossing an ambiguous patch remains a refusal.
    """
    low, high = -math.inf, math.inf
    px, py, pz = map(float, point)
    dx, dy, dz = map(float, direction)
    try:
        if dx == 0 and dy == 0:
            radial2 = _add(_square((px, px)), _square((py, py)))
            if radial2[0] > _square((aperture, aperture))[1]:
                return None
        else:
            transverse2 = _add(_square((dx, dx)), _square((dy, dy)))
            dot = _add(_mul((px, px), (dx, dx)), _mul((py, py), (dy, dy)))
            center = _divide((-dot[1], -dot[0]), transverse2)
            cross = _add(_mul((px, px), (dy, dy)), _mul((-py, -py), (dx, dx)))
            distance2 = _divide(_square(cross), transverse2)
            remaining = _add(_square((aperture, aperture)), (-distance2[1], -distance2[0]))
            if remaining[1] < 0:
                return None
            half2 = _divide((0.0, max(0.0, remaining[1])), transverse2)[1]
            half = math.nextafter(math.sqrt(half2), math.inf)
            low, high = _add(center, (-half, half))
            if not math.isfinite(low) or not math.isfinite(high):
                raise AsphereIntersectionError("Nonfinite finite-patch cylinder enclosure")
        slab = (certificate["sag_min_mm"], certificate["sag_max_mm"])
        if dz == 0:
            if pz < slab[0] or pz > slab[1]:
                return None
        else:
            axial = _divide(_add(slab, (-pz, -pz)), (dz, dz))
            if not all(math.isfinite(value) for value in axial):
                raise AsphereIntersectionError("Nonfinite finite-patch axial enclosure")
            low, high = max(low, axial[0]), min(high, axial[1])
        return None if low > high else (low, high)
    except (OverflowError, ZeroDivisionError) as exc:
        raise AsphereIntersectionError("Finite-patch enclosure exceeds numeric range") from exc


def _power(r, exponent):
    value = (1.0, 1.0)
    for _ in range(exponent):
        value = _mul(value, r)
    return value


def _enclosure(surface, low, high):
    c, k, coefficients, orders = _parameters(surface)
    # Enclose reciprocal curvature rounding as well as every arithmetic step.
    ci = (0.0, 0.0) if c == 0 else _out(c, c)
    r = (float(low), float(high))
    r2 = _power(r, 2)
    q = _add((1.0, 1.0), _mul(_mul((-1.0, -1.0), _add((1.0, 1.0), (k, k))), _mul(_mul(ci, ci), r2)))
    if q[0] <= MIN_RADICAND:
        raise ValueError("Conic radicand is not safely positive over the entire clear aperture")
    root = _out(math.sqrt(q[0]), math.sqrt(q[1]))
    denominator = _add((1.0, 1.0), root)
    value = _mul(_mul(ci, r2), _out(1.0 / denominator[1], 1.0 / denominator[0]))
    derivative = _mul(_mul(ci, r), _out(1.0 / root[1], 1.0 / root[0]))
    for a, order in zip(coefficients, orders, strict=True):
        value = _add(value, _mul((a, a), _power(r, order)))
        derivative = _add(
            derivative, _mul(_mul((float(order), float(order)), (a, a)), _power(r, order - 1))
        )
    return value, derivative, q[0]


def surface_bounds(surface, *, radius=None):
    """Outward-rounded interval certificate, never a sampled extrema estimate."""
    aperture = surface["semi_diameter_mm"] if radius is None else radius
    if not 0 < aperture <= surface["semi_diameter_mm"]:
        raise ValueError("Certificate radius must lie inside the physical clear aperture")
    low, high, slope, radicand = math.inf, -math.inf, 0.0, math.inf
    for i in range(CERTIFICATE_INTERVALS):
        value, derivative, q = _enclosure(
            surface,
            aperture * i / CERTIFICATE_INTERVALS,
            aperture * (i + 1) / CERTIFICATE_INTERVALS,
        )
        low, high = min(low, value[0]), max(high, value[1])
        slope, radicand = max(slope, abs(derivative[0]), abs(derivative[1])), min(radicand, q)
    if not all(math.isfinite(v) for v in (low, high, slope, radicand)):
        raise ValueError("Nonfinite asphere enclosure")
    if max(abs(low), abs(high)) > MAX_SAG_MM or slope > MAX_SLOPE:
        raise ValueError("Surface exceeds the bounded sag/slope certification domain")
    return {
        "sag_min_mm": low,
        "sag_max_mm": high,
        "slope_abs_max": slope,
        "radicand_min": radicand,
        "radius_mm": float(aperture),
        "intervals": CERTIFICATE_INTERVALS,
    }


def certify_prescription(surfaces):
    """Certify all adjacent clear-aperture gaps and rear sensor-plane clearance."""
    if not isinstance(surfaces, list) or not 1 <= len(surfaces) <= MAX_SURFACES:
        raise ValueError(f"Certify 1..{MAX_SURFACES} physical surfaces")
    bounds = [surface_bounds(surface) for surface in surfaces]
    gaps = []
    for front, rear in zip(surfaces[:-1], surfaces[1:], strict=True):
        aperture = min(front["semi_diameter_mm"], rear["semi_diameter_mm"])
        minimum = math.inf
        for i in range(CERTIFICATE_INTERVALS):
            low, high = (
                aperture * i / CERTIFICATE_INTERVALS,
                aperture * (i + 1) / CERTIFICATE_INTERVALS,
            )
            fv, _, _ = _enclosure(front, low, high)
            rv, _, _ = _enclosure(rear, low, high)
            gap = _add(_add((front["thickness_mm"],) * 2, rv), (-fv[1], -fv[0]))[0]
            minimum = min(minimum, gap)
        if minimum <= MIN_CLEARANCE_MM:
            raise ValueError(
                "Adjacent surface gap lacks certified positive clearance over the full aperture"
            )
        gaps.append({"front_id": front["id"], "rear_id": rear["id"], "minimum_gap_mm": minimum})
    rear_upper = max(0.0, bounds[-1]["sag_max_mm"])
    clearance = _add((surfaces[-1]["thickness_mm"],) * 2, (-rear_upper,) * 2)[0]
    if clearance <= 1e-5:
        raise ValueError("Sensor plane lacks certified clearance behind the full rear surface")
    return {
        "schema": "camera_centered_surface_certificate_v1",
        "method": "outward_interval_radial_subdivision",
        "intervals_per_surface_or_gap": CERTIFICATE_INTERVALS,
        "surfaces": [{"id": s["id"], **b} for s, b in zip(surfaces, bounds, strict=True)],
        "adjacent_gaps": gaps,
        "rear_sag_upper_mm": rear_upper,
        "rear_plane_distance_mm": clearance,
        "physical_uncertainty": "unknown",
    }


def validate_intersection(surface, point):
    p = np.asarray(point, dtype=float)
    if p.shape != (3,) or not np.isfinite(p).all():
        raise AsphereIntersectionError("Nonfinite or malformed surface intersection")
    r = math.hypot(float(p[0]), float(p[1]))
    if r > surface["semi_diameter_mm"] * (1 + 1e-12):
        raise AsphereIntersectionError("Intersection lies outside the finite clear surface")
    if abs(float(p[2]) - sag(surface, r)) > ROOT_RESIDUAL_MM:
        raise AsphereIntersectionError("Intersection fails the independent sag residual")


def profile(surface):
    """Build the exact shared profile; optional RayOptics imports remain lazy."""
    from rayoptics.elem.profiles import EvenPolynomial, Spherical
    from rayoptics.raytr.traceerror import TraceMissedSurfaceError

    if surface["profile"] != "even_asphere":
        return (
            Spherical(c=0.0) if surface["radius_mm"] is None else Spherical(r=surface["radius_mm"])
        )
    data = copy.deepcopy(surface)
    data["asphere"] = validate_asphere(data)
    certificate = surface_bounds(data)

    class CertifiedEvenPolynomial(EvenPolynomial):
        def intersect(self, p0, d, eps=1e-12, z_dir=1.0):
            p, direction = np.asarray(p0, dtype=float), np.asarray(d, dtype=float)
            if (
                p.shape != (3,)
                or direction.shape != (3,)
                or not np.isfinite(p).all()
                or not np.isfinite(direction).all()
                or abs(float(np.linalg.norm(direction)) - 1.0) > 1e-10
                or z_dir not in (-1, 1)
            ):
                raise AsphereIntersectionError("Invalid ray coordinates or direction")
            interval = _finite_patch_interval(p, direction, data["semi_diameter_mm"], certificate)
            if interval is None:
                raise TraceMissedSurfaceError()
            dz = float(direction[2])
            transverse = math.hypot(float(direction[0]), float(direction[1]))
            if abs(dz) <= certificate["slope_abs_max"] * transverse + 1e-12:
                raise AsphereIntersectionError("Ray/sag monotonic uniqueness is not certified")
            if dz * z_dir <= 0:
                raise AsphereIntersectionError(
                    "Ray propagation contradicts its declared axial direction"
                )
            low, high = interval

            def residual(t):
                point = p + t * direction
                # Outward-rounded cylinder endpoints can sit just beyond the
                # disk. Constant radial extension preserves the slope bound
                # and avoids evaluating the conic outside its admitted domain.
                radial = min(math.hypot(float(point[0]), float(point[1])), data["semi_diameter_mm"])
                return float(point[2]) - sag(data, radial)

            fl, fh = residual(low), residual(high)
            if fl == 0:
                root = low
            elif fh == 0:
                root = high
            elif (fl > 0) == (fh > 0):
                if min(abs(fl), abs(fh)) <= ROOT_RESIDUAL_MM:
                    raise AsphereIntersectionError(
                        "Finite-patch boundary intersection is numerically unresolved"
                    )
                raise TraceMissedSurfaceError()
            else:
                for _ in range(MAX_ROOT_ITERATIONS):
                    root = low + (high - low) / 2
                    value = residual(root)
                    if value == 0 or high - low <= min(max(float(eps), 1e-14), 1e-11):
                        break
                    if (fl > 0) == (value > 0):
                        low, fl = root, value
                    else:
                        high = root
                else:
                    raise AsphereIntersectionError("Safeguarded root iteration budget exhausted")
            point = p + root * direction
            radial = math.hypot(float(point[0]), float(point[1]))
            if radial > data["semi_diameter_mm"] * (1 + 1e-12):
                # Near an aperture edge the bracket and rounded root do not
                # prove that the true hit lies outside. Never hide that numeric
                # uncertainty as physical loss of transmitted flux.
                raise AsphereIntersectionError(
                    "Rounded intersection cannot be certified inside the finite patch"
                )
            validate_intersection(data, point)
            # Signed root is intentional: RayOptics first moves to the line's
            # perpendicular foot. Callers verify the final total segment length.
            return float(root), point

    result = CertifiedEvenPolynomial(
        r=data["radius_mm"],
        cc=data["asphere"]["conic_constant"],
        coefs=[0.0, *data["asphere"]["coefficients"]],
    )
    result.geometry_certificate = certificate
    return result
