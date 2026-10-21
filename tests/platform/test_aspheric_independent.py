"""Independent asphere geometry/trace checks, not measured-lens validation.

References use an implicit conic, scalar polynomials, analytic quadratic roots,
and separate bracketing/Snell calculations. No production sag, derivative or
intersection routine participates in the reference calculation.
"""

import math
from copy import deepcopy

import numpy as np
import pytest
from scipy.optimize import brentq

from camera_platform import lens_factory, optics_surfaces


def asphere(*, radius=12.0, conic=-0.8, coefficients=(1e-3, -1e-5, 2e-7), aperture=2.0):
    return {
        "id": "independent-asphere",
        "profile": "even_asphere",
        "radius_mm": radius,
        "thickness_mm": 4.0,
        "semi_diameter_mm": aperture,
        "refractive_index_after": [1.5, 1.5],
        "is_stop": False,
        "asphere": {
            "schema": "camera_even_asphere_v1",
            "conic_constant": conic,
            "orders": [4, 6, 8],
            "coefficients": list(coefficients),
            "coefficient_units": ["mm^-3", "mm^-5", "mm^-7"],
        },
    }


def reference_sag(surface, radial):
    radius = surface["radius_mm"]
    if radius is None:
        return 0.0
    data = surface.get("asphere", {"conic_constant": 0.0, "coefficients": [0.0] * 3})
    k = data["conic_constant"]
    root = math.sqrt(1 - (1 + k) * (radial / radius) ** 2)
    # Vertex sheet, including signed curvature. Rationalized evaluation avoids
    # cancellation in the implicit quadratic root near the axis.
    conic = radial**2 / (radius * (1 + root))
    return conic + math.fsum(
        a * radial**n for a, n in zip(data["coefficients"], (4, 6, 8), strict=True)
    )


def reference_derivative(surface, radial):
    radius = surface["radius_mm"]
    data = surface["asphere"]
    k = data["conic_constant"]
    conic_surface = deepcopy(surface)
    conic_surface["asphere"]["coefficients"] = [0.0] * 3
    z = reference_sag(conic_surface, radial)
    # Differentiate r² - 2Rz + (1+k)z² = 0, independently of library df().
    return radial / (radius - (1 + k) * z) + math.fsum(
        n * a * radial ** (n - 1) for n, a in zip((4, 6, 8), data["coefficients"], strict=True)
    )


def reference_normal(surface, point):
    radial = math.hypot(*point[:2])
    slope = 0.0 if radial == 0 else reference_derivative(surface, radial) / radial
    normal = np.array([-point[0] * slope, -point[1] * slope, 1.0])
    return normal / np.linalg.norm(normal)


def quadratic_vertex_hit(surface, start, direction):
    """Analytic conic line intersection, rejecting the other implicit sheet."""
    p, d = np.asarray(start), np.asarray(direction)
    radius, k = surface["radius_mm"], surface["asphere"]["conic_constant"]
    a = d[0] ** 2 + d[1] ** 2 + (1 + k) * d[2] ** 2
    b = 2 * (p[0] * d[0] + p[1] * d[1] + (1 + k) * p[2] * d[2] - radius * d[2])
    c = p[0] ** 2 + p[1] ** 2 + (1 + k) * p[2] ** 2 - 2 * radius * p[2]
    if abs(a) < 1e-14:
        roots = [-c / b]
    else:
        determinant = b * b - 4 * a * c
        assert determinant >= 0
        q = -0.5 * (b + math.copysign(math.sqrt(determinant), b))
        roots = [q / a, c / q]
    candidates = []
    for distance in roots:
        point = p + distance * d
        radial = math.hypot(*point[:2])
        if (
            distance >= 0
            and radial <= surface["semi_diameter_mm"]
            and abs(point[2] - reference_sag(surface, radial)) < 1e-9
        ):
            candidates.append((distance, point))
    assert candidates, "Authored analytic fixture must intersect the vertex sheet"
    return min(candidates, key=lambda item: item[0])


def bracket_hit(surface, start, direction, lower, upper):
    p, d = np.asarray(start), np.asarray(direction)

    def residual(distance):
        point = p + distance * d
        return point[2] - reference_sag(surface, math.hypot(*point[:2]))

    distance = brentq(residual, lower, upper, xtol=1e-13, rtol=1e-14)
    point = p + distance * d
    assert math.hypot(*point[:2]) < surface["semi_diameter_mm"]
    return distance, point


def snell(direction, normal, before, after):
    d, n = np.asarray(direction), np.asarray(normal)
    tangent = (before / after) * (d - np.dot(d, n) * n)
    transmitted_normal2 = 1 - np.dot(tangent, tangent)
    if transmitted_normal2 < 0:
        raise ValueError("Independent reference total internal reflection")
    return tangent + math.copysign(math.sqrt(transmitted_normal2), np.dot(d, n)) * n


def actual_profile(surface):
    pytest.importorskip("rayoptics")
    return optics_surfaces.profile(surface)


def scaled_surface(surface, scale):
    result = deepcopy(surface)
    for name in ("radius_mm", "thickness_mm", "semi_diameter_mm"):
        result[name] *= scale
    result["asphere"]["coefficients"] = [
        coefficient / scale ** (order - 1)
        for coefficient, order in zip(surface["asphere"]["coefficients"], (4, 6, 8), strict=True)
    ]
    return result


@pytest.mark.parametrize("radius", [-12.0, 12.0])
@pytest.mark.parametrize("conic", [-2.0, -1.0, 0.0, 0.5, 3.0])
def test_conic_vertex_sheet_and_analytic_radial_derivative(radius, conic):
    surface = asphere(radius=radius, conic=conic)
    for radial in (0.0, 0.3, 1.1, 1.9):
        assert optics_surfaces.sag(surface, radial) == pytest.approx(
            reference_sag(surface, radial), rel=2e-13, abs=2e-14
        )
        assert optics_surfaces.radial_derivative(surface, radial) == pytest.approx(
            reference_derivative(surface, radial), rel=2e-12, abs=2e-14
        )
        if radial:
            h = 1e-4
            finite_difference = math.fsum(
                [
                    reference_sag(surface, radial - 2 * h),
                    -8 * reference_sag(surface, radial - h),
                    8 * reference_sag(surface, radial + h),
                    -reference_sag(surface, radial + 2 * h),
                ]
            ) / (12 * h)
            assert optics_surfaces.radial_derivative(surface, radial) == pytest.approx(
                finite_difference, rel=2e-10, abs=2e-12
            )


@pytest.mark.parametrize("radius", [-12.0, 12.0])
@pytest.mark.parametrize("conic", [-2.0, -1.0, 0.0, 0.5, 3.0])
def test_actual_profile_intersection_matches_independent_quadratic_conic(radius, conic):
    surface = asphere(radius=radius, conic=conic, coefficients=(0.0, 0.0, 0.0))
    start = np.array([0.4, -0.25, 3.0])
    direction = np.array([0.035, 0.02, -1.0])
    direction /= np.linalg.norm(direction)
    expected_distance, expected = quadratic_vertex_hit(surface, start, direction)
    distance, actual = actual_profile(surface).intersect(start, direction, eps=1e-12, z_dir=-1)
    np.testing.assert_allclose(actual, expected, atol=3e-11, rtol=1e-12)
    assert distance == pytest.approx(expected_distance, abs=3e-11)
    assert abs(actual[2] - reference_sag(surface, math.hypot(*actual[:2]))) < 3e-11


@pytest.mark.parametrize("radius", [-12.0, 12.0])
@pytest.mark.parametrize("forward", [False, True])
def test_actual_polynomial_hit_normal_refraction_and_reciprocity(radius, forward):
    from rayoptics.elem.profiles import Spherical
    from rayoptics.elem.surface import Surface
    from rayoptics.raytr.raytrace import trace_raw

    surface = asphere(radius=radius)
    sense = 1 if forward else -1
    start = np.array([0.5, 0.2, -3.0 * sense])
    direction = np.array([0.025, -0.018, float(sense)])
    direction /= np.linalg.norm(direction)
    expected_distance, expected = bracket_hit(surface, start, direction, 2.0, 4.0)
    normal = reference_normal(surface, expected)
    before, after = (1.0, 1.5) if forward else (1.5, 1.0)
    expected_direction = snell(direction, normal, before, after)
    dummy = Surface(profile=Spherical(c=0), interact_mode="dummy")
    interface = Surface(profile=actual_profile(surface), max_ap=surface["semi_diameter_mm"])
    transform = (np.eye(3), np.zeros(3))
    path = [(dummy, None, transform, before, sense), (interface, None, transform, after, sense)]
    ray, optical_path, _ = trace_raw(
        iter(path), start, direction, 550.0, intersect_obj=False, check_apertures=True
    )
    np.testing.assert_allclose(ray[-1][0], expected, atol=3e-11)
    np.testing.assert_allclose(ray[-1][1], expected_direction, atol=3e-12)
    np.testing.assert_allclose(ray[-1][3], normal, atol=3e-12)
    assert ray[0][2] == pytest.approx(expected_distance, abs=3e-11)
    assert optical_path == pytest.approx(before * expected_distance, abs=5e-11)
    np.testing.assert_allclose(snell(-ray[-1][1], normal, after, before), -direction, atol=3e-12)


@pytest.mark.parametrize("scale", [0.25, 2.0, 10.0])
def test_dimensioned_coefficients_preserve_geometry_angles_and_intersection_scaling(scale):
    surface = asphere(radius=-12.0, conic=-1.7)
    scaled = scaled_surface(surface, scale)
    p = np.array([0.4, 0.1, 3.0])
    d = np.array([-0.02, 0.04, -1.0])
    d /= np.linalg.norm(d)
    distance, hit = actual_profile(surface).intersect(p, d, eps=1e-12, z_dir=-1)
    larger_distance, larger_hit = actual_profile(scaled).intersect(
        p * scale, d, eps=1e-12, z_dir=-1
    )
    assert larger_distance == pytest.approx(distance * scale, abs=3e-10)
    np.testing.assert_allclose(larger_hit, hit * scale, atol=3e-10)
    np.testing.assert_allclose(
        actual_profile(scaled).normal(larger_hit), actual_profile(surface).normal(hit), atol=3e-11
    )
    for radial in (0.0, 0.25, 1.75):
        assert optics_surfaces.sag(scaled, radial * scale) == pytest.approx(
            reference_sag(surface, radial) * scale, abs=3e-12
        )
        assert optics_surfaces.radial_derivative(scaled, radial * scale) == pytest.approx(
            reference_derivative(surface, radial), abs=3e-12
        )


@pytest.mark.parametrize("azimuth", [0.0, 0.73, 2.1])
def test_two_aspheric_interfaces_match_independent_paths_and_actual_reverse_trace(azimuth):
    """An actual reverse trace must retrace both refracting interfaces and OPL."""
    pytest.importorskip("rayoptics")
    from rayoptics.elem.profiles import Spherical
    from rayoptics.elem.surface import Surface
    from rayoptics.raytr.raytrace import trace_raw

    surfaces = [
        asphere(radius=12.0, conic=-0.8, coefficients=(1e-3, -1e-5, 2e-7)),
        asphere(radius=-15.0, conic=-1.6, coefficients=(-4e-4, 3e-6, -1e-8)),
    ]
    surfaces[1]["id"] = "independent-second-asphere"
    vertices = np.array([-3.0, 0.0, 4.0, 8.0])
    media = [1.0, 1.5, 1.0]
    ca, sa = math.cos(azimuth), math.sin(azimuth)
    rotation = np.array([[ca, -sa, 0.0], [sa, ca, 0.0], [0.0, 0.0, 1.0]])
    start = rotation @ np.array([0.5, 0.2, -3.0])
    direction = rotation @ np.array([0.025, -0.018, 1.0])
    direction /= np.linalg.norm(direction)

    # Independent scalar bracket plus tangent/normal Snell, entirely in global
    # coordinates. No RayOptics or production profile helper defines this path.
    expected_points, expected_directions, expected_lengths = [start], [direction], []
    for index, surface in enumerate(surfaces, start=1):
        offset = np.array([0.0, 0.0, vertices[index]])
        length, point = bracket_hit(
            surface, expected_points[-1] - offset, expected_directions[-1], 2.0, 5.0
        )
        expected_lengths.append(length)
        expected_points.append(point + offset)
        expected_directions.append(
            snell(
                expected_directions[-1], reference_normal(surface, point),
                media[index - 1], media[index],
            )
        )
    last = (vertices[-1] - expected_points[-1][2]) / expected_directions[-1][2]
    expected_lengths.append(last)
    expected_points.append(expected_points[-1] + last * expected_directions[-1])
    expected_directions.append(expected_directions[-1])
    expected_points = np.array(expected_points)
    expected_opl = sum(n * length for n, length in zip(media, expected_lengths, strict=True))

    interfaces = [Surface(profile=Spherical(c=0), interact_mode="dummy")]
    interfaces.extend(
        Surface(profile=actual_profile(surface), max_ap=surface["semi_diameter_mm"])
        for surface in surfaces
    )
    interfaces.append(Surface(profile=Spherical(c=0), interact_mode="dummy"))

    def trace_path(ordered_interfaces, vertex_positions, indexes, origin, direction, sense):
        path = []
        for i, interface in enumerate(ordered_interfaces):
            separation = vertex_positions[i + 1] - vertex_positions[i] if i < 3 else 0.0
            transform = (np.eye(3), np.array([0.0, 0.0, separation]))
            path.append((interface, None, transform, indexes[i], sense))
        return trace_raw(
            iter(path), origin - np.array([0.0, 0.0, vertex_positions[0]]), direction,
            550.0, intersect_obj=False, check_apertures=True, pt_inside_fuzz=0.0,
        )

    forward, forward_opl, _ = trace_path(
        interfaces, vertices, [1.0, 1.5, 1.0, 1.0], start, direction, 1
    )
    global_forward = np.array([segment[0] for segment in forward])
    global_forward[:, 2] += vertices
    np.testing.assert_allclose(global_forward, expected_points, rtol=0, atol=5e-11)
    np.testing.assert_allclose(
        [segment[1] for segment in forward], expected_directions, rtol=0, atol=5e-12
    )
    np.testing.assert_allclose(
        [segment[2] for segment in forward[:-1]], expected_lengths, rtol=0, atol=5e-11
    )
    assert forward_opl == pytest.approx(expected_opl, abs=8e-11)

    reverse, reverse_opl, _ = trace_path(
        interfaces[::-1], vertices[::-1], [1.0, 1.5, 1.0, 1.0],
        global_forward[-1], -forward[-1][1], -1,
    )
    global_reverse = np.array([segment[0] for segment in reverse])
    global_reverse[:, 2] += vertices[::-1]
    np.testing.assert_allclose(global_reverse, expected_points[::-1], rtol=0, atol=8e-11)
    np.testing.assert_allclose(reverse[-1][1], -direction, rtol=0, atol=5e-12)
    np.testing.assert_allclose(
        [segment[2] for segment in reverse[:-1]], expected_lengths[::-1], rtol=0, atol=8e-11
    )
    assert all(segment[2] >= 0 for segment in forward + reverse)
    assert reverse_opl == pytest.approx(expected_opl, abs=1e-10)


def test_negative_local_parameter_is_valid_after_rayoptics_perpendicular_foot_shift():
    surface = asphere(radius=12.0, conic=-1.0, coefficients=(0, 0, 0))
    shifted_start = np.array([0.6, 0.0, 0.0])
    direction = np.array([0.0, 0.0, -1.0])
    distance, point = actual_profile(surface).intersect(
        shifted_start, direction, eps=1e-12, z_dir=-1
    )
    assert distance == pytest.approx(-(0.6**2) / 24, abs=2e-12)
    assert point[2] == pytest.approx(0.6**2 / 24, abs=2e-12)
    # A physical start at z=1 first advances +1 to this local foot, so the
    # complete segment is positive even though the local correction is negative.
    assert 1.0 + distance > 0


def test_multiple_valid_vertex_sheet_roots_are_refused_not_counted_as_vignetting():
    surface = asphere(radius=2.0, conic=-1.0, coefficients=(0, 0, 0), aperture=1.0)
    roots = [np.array([-math.sqrt(0.5), 0.0, 0.125]), np.array([math.sqrt(0.5), 0.0, 0.125])]
    for point in roots:
        assert reference_sag(surface, abs(point[0])) == pytest.approx(point[2])
    with pytest.raises(optics_surfaces.AsphereIntersectionError):
        actual_profile(surface).intersect(
            np.array([-2.0, 0.0, 0.125]), np.array([1.0, 0.0, 0.0]), eps=1e-12, z_dir=1
        )


def bump_surface(*, radius=100.0):
    # B*r^4*(a²-r²)^2: zero at center and edge, positive inside.
    aperture, b = 2.0, 0.006
    return asphere(
        radius=radius,
        conic=0.0,
        aperture=aperture,
        coefficients=(b * aperture**4, -2 * b * aperture**2, b),
    )


def test_full_aperture_clearance_refuses_collision_hidden_at_center_and_edge():
    front = bump_surface()
    front["thickness_mm"] = 0.04
    rear = deepcopy(front)
    rear.update(
        id="independent-rear",
        profile="spherical",
        thickness_mm=10.0,
        refractive_index_after=[1.0, 1.0],
    )
    rear.pop("asphere")
    def gap(r):
        return 0.04 + reference_sag(rear, r) - reference_sag(front, r)

    assert gap(0) > 0 and gap(2) > 0
    assert gap(math.sqrt(2)) == pytest.approx(-0.056)
    with pytest.raises(ValueError, match="(?i)gap|intersect|overlap|clearance"):
        optics_surfaces.certify_prescription([front, rear])


def test_rear_sag_enclosure_covers_interior_bump_not_only_aperture_edge():
    rear = bump_surface(radius=-100.0)
    center, edge, inside = [reference_sag(rear, radius) for radius in (0, 2, math.sqrt(2))]
    assert inside > max(center, edge) + 0.08
    bounds = optics_surfaces.surface_bounds(rear)
    assert bounds["sag_max_mm"] >= inside
    assert bounds["sag_min_mm"] <= min(center, edge)
    for radius in np.linspace(0, 2, 401):
        assert bounds["sag_min_mm"] <= reference_sag(rear, radius) <= bounds["sag_max_mm"]
        assert abs(reference_derivative(rear, radius)) <= bounds["slope_abs_max"]
    rear["thickness_mm"] = 0.05
    with pytest.raises(ValueError, match="(?i)sensor|rear|gap|intersect"):
        optics_surfaces.certify_prescription([rear])


def test_factory_validation_rejects_interior_collision_before_backend_loading(monkeypatch):
    request = lens_factory.request_template(4)
    request["wavelength_nm"] = [500, 600]
    request["stop_transmission"].update(
        wavelength_nm=[500, 600], power_transmittance=[[1.0, 1.0], [1.0, 1.0]]
    )
    request["image_height_mm"], request["field_radius"] = [0.0, 0.1], [0.0, 1.0]
    front = bump_surface()
    front["thickness_mm"] = 0.04
    rear = deepcopy(front)
    rear.update(
        id="independent-rear",
        profile="spherical",
        thickness_mm=10.0,
        refractive_index_after=[1.0, 1.0],
    )
    rear.pop("asphere")
    stop = deepcopy(request["surfaces"][0])
    stop.update(refractive_index_after=[1.0, 1.0], semi_diameter_mm=1.0, thickness_mm=1.0)
    request["surfaces"] = [stop, front, rear]
    monkeypatch.setattr(lens_factory, "_backend", lambda: pytest.fail("Backend must not load"))
    with pytest.raises(ValueError, match="(?i)gap|intersect|overlap|clearance"):
        lens_factory.characterize(request)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s["asphere"].update(coefficient_units=["mm^-3", "mm^-5", "mm^-9"]),
        lambda s: s["asphere"].update(orders=[2, 4, 6]),
        lambda s: s["asphere"].update(coefficients=[True, 0.0, 0.0]),
        lambda s: s.update(radius_mm=0),
    ],
)
def test_explicit_dimensional_and_curvature_contract_refuses_ambiguous_data(mutate):
    surface = asphere()
    mutate(surface)
    with pytest.raises(ValueError):
        optics_surfaces.validate_asphere(surface)
