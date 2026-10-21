"""Full-patch certificates and the refusal/physical-miss boundary."""

import math
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import optics_surfaces as geometry


def surface(radius=20.0, aperture=2.0, coefficients=(0.0, 0.0, 0.0), conic=0.0):
    return {
        "id": "authored-asphere",
        "profile": "even_asphere",
        "radius_mm": radius,
        "thickness_mm": 10.0,
        "semi_diameter_mm": aperture,
        "refractive_index_after": [1.0, 1.0],
        "is_stop": False,
        "asphere": {
            "schema": "camera_even_asphere_v1",
            "conic_constant": conic,
            "orders": [4, 6, 8],
            "coefficients": list(coefficients),
            "coefficient_units": ["mm^-3", "mm^-5", "mm^-7"],
        },
    }


@pytest.mark.parametrize("radius", [-20.0, 20.0])
def test_zero_departure_reproduces_signed_sphere_sag_normal_and_intersection(radius):
    from rayoptics.elem.profiles import Spherical

    prescription = surface(radius=radius)
    profile = geometry.profile(prescription)
    sphere = Spherical(r=radius)
    for x, y in [(0.0, 0.0), (0.7, -0.2), (-1.1, 0.5)]:
        radial = math.hypot(x, y)
        expected = math.copysign(abs(radius) - math.sqrt(radius**2 - radial**2), radius)
        assert geometry.sag(prescription, radial) == pytest.approx(expected, abs=3e-15)
        p = np.array([x, y, 2.0])
        d = np.array([0.03, -0.02, -1.0])
        d /= np.linalg.norm(d)
        distance, hit = profile.intersect(p, d, z_dir=-1)
        sphere_distance, sphere_hit = sphere.intersect(p, d, eps=1e-12, z_dir=-1)
        assert distance == pytest.approx(sphere_distance, abs=2e-11)
        np.testing.assert_allclose(hit, sphere_hit, atol=2e-11)
        np.testing.assert_allclose(profile.normal(hit), sphere.normal(sphere_hit), atol=2e-12)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s["asphere"].update(schema="unknown"),
        lambda s: s["asphere"].update(orders=[True, 6, 8]),
        lambda s: s["asphere"].update(orders=[2, 4, 6]),
        lambda s: s["asphere"].update(coefficients=[0.0, 0.0]),
        lambda s: s["asphere"].update(coefficients=[0.0, float("nan"), 0.0]),
        lambda s: s["asphere"].update(coefficients=[False, 0.0, 0.0]),
        lambda s: s["asphere"].update(coefficients=[1e13, 0.0, 0.0]),
        lambda s: s["asphere"].update(conic_constant=float("inf")),
        lambda s: s["asphere"].update(coefficient_units=["m^-3", "mm^-5", "mm^-7"]),
        lambda s: s["asphere"].update(unknown=0),
        lambda s: s.update(is_stop=True),
        lambda s: s.update(radius_mm=0),
        lambda s: s.update(radius_mm=None),
        lambda s: s.update(radius_mm=10**1000),
        lambda s: s["asphere"].update(conic_constant=10**1000),
        lambda s: s["asphere"].update(coefficients=[10**1000, 0.0, 0.0]),
    ],
)
def test_authored_asphere_contract_is_explicit_and_finite(mutation):
    prescription = surface()
    mutation(prescription)
    with pytest.raises(ValueError):
        geometry.validate_asphere(prescription)


def test_normalization_and_profile_construction_preserve_authored_input():
    prescription = surface(coefficients=(0.001, -0.00001, 0.0000002), conic=-0.5)
    original = deepcopy(prescription)
    normalized = geometry.validate_asphere(prescription)
    profile = geometry.profile(prescription)
    normalized["coefficients"][0] = 2.0
    assert prescription == original
    # RayOptics stores A2 first. The authored A4 cannot become vertex power.
    assert profile.coefs == [0.0, 0.001, -0.00001, 0.0000002]


@pytest.mark.parametrize("conic", [0.0, 3.0])
def test_conic_radicand_is_checked_at_full_clear_aperture(conic):
    prescription = surface(radius=2.0, aperture=2.0, conic=conic)
    assert geometry.sag(prescription, 0.1) > 0
    with pytest.raises(ValueError, match="radicand"):
        geometry.surface_bounds(prescription)


def test_hyperboloid_can_extend_beyond_vertex_radius_when_entire_domain_is_real():
    prescription = surface(radius=1.0, aperture=2.0, conic=-2.0)
    bounds = geometry.surface_bounds(prescription)
    assert bounds["radicand_min"] > 0
    assert bounds["slope_abs_max"] < 1.0
    assert bounds["sag_max_mm"] >= math.sqrt(5.0) - 1.0


def test_mixed_polynomial_interior_extrema_are_enclosed():
    # r^4 * (4-r²)^2 peaks at sqrt(2), although both endpoints vanish.
    prescription = surface(radius=-100.0, coefficients=(0.096, -0.048, 0.006))
    certificate = geometry.surface_bounds(prescription)
    sampled = np.array([geometry.sag(prescription, r) for r in np.linspace(0, 2, 1001)])
    slopes = [abs(geometry.radial_derivative(prescription, r)) for r in np.linspace(0, 2, 1001)]
    assert sampled.max() > max(sampled[0], sampled[-1]) + 0.08
    assert certificate["sag_min_mm"] <= sampled.min()
    assert certificate["sag_max_mm"] >= sampled.max()
    assert certificate["slope_abs_max"] >= max(slopes)
    assert certificate["intervals"] == geometry.CERTIFICATE_INTERVALS


def test_same_endpoint_sags_cannot_hide_internal_surface_collision_or_rear_obstruction():
    front = surface(radius=100.0, coefficients=(0.096, -0.048, 0.006))
    front["thickness_mm"] = 0.04
    rear = surface(radius=100.0)
    rear["id"] = "rear"
    for radial in [0.0, 2.0]:
        assert 0.04 + geometry.sag(rear, radial) - geometry.sag(front, radial) > 0
    with pytest.raises(ValueError, match="gap"):
        geometry.certify_prescription([front, rear])
    front["thickness_mm"] = 0.05
    with pytest.raises(ValueError, match="Sensor plane"):
        geometry.certify_prescription([front])
    front["thickness_mm"] = 0.2
    certificate = geometry.certify_prescription([front, rear])
    assert certificate["adjacent_gaps"][0]["minimum_gap_mm"] > 0


@pytest.mark.parametrize(
    "start,direction",
    [
        ([0.0, 3.0, 0.05], [1.0, 0.0, 0.0]),
        ([0.0, 0.0, 2.0], [1.0, 0.0, 0.0]),
        ([3.0, 0.0, 2.0], [0.0, 0.0, -1.0]),
        ([0.0, 0.0, 2.0], [1.0, 0.0, -0.001]),
    ],
)
def test_disjoint_finite_patch_is_physical_miss_before_grazing_ambiguity(start, direction):
    from rayoptics.raytr.traceerror import TraceMissedSurfaceError

    profile = geometry.profile(surface())
    direction = np.asarray(direction, dtype=float)
    direction /= np.linalg.norm(direction)
    with pytest.raises(TraceMissedSurfaceError):
        profile.intersect(np.array(start), direction, z_dir=-1)


def test_grazing_line_with_two_valid_patch_intersections_is_numerical_refusal():
    prescription = surface(radius=2.0, aperture=1.0, conic=-1.0)
    profile = geometry.profile(prescription)
    for x in [-math.sqrt(0.5), math.sqrt(0.5)]:
        assert geometry.sag(prescription, abs(x)) == pytest.approx(0.125)
    with pytest.raises(geometry.AsphereIntersectionError, match="uniqueness"):
        profile.intersect(np.array([-2.0, 0.0, 0.125]), np.array([1.0, 0.0, 0.0]))


@pytest.mark.parametrize("x", [0.0, 2.0, math.nextafter(2.0, 0.0)])
def test_axis_parallel_intersection_retains_axis_and_clear_aperture_boundary(x):
    prescription = surface(conic=-1.0)
    distance, hit = geometry.profile(prescription).intersect(
        np.array([x, 0.0, 0.0]), np.array([0.0, 0.0, -1.0]), z_dir=-1
    )
    expected = x * x / 40.0
    assert hit[2] == pytest.approx(expected, abs=1e-11)
    assert distance == pytest.approx(-expected, abs=1e-11)


def test_root_solver_exhaustion_does_not_become_a_physical_miss(monkeypatch):
    profile = geometry.profile(surface(conic=-1.0))
    monkeypatch.setattr(geometry, "MAX_ROOT_ITERATIONS", 0)
    with pytest.raises(geometry.AsphereIntersectionError, match="iteration budget"):
        profile.intersect(np.array([0.7, 0.0, 1.0]), np.array([0.0, 0.0, -1.0]), z_dir=-1)


@pytest.mark.parametrize(
    "start,direction",
    [([0, 0, float("nan")], [0, 0, -1]), ([0, 0], [0, 0, -1]), ([0, 0, 1], [0, 0, -2])],
)
def test_nonfinite_or_malformed_ray_is_numerical_refusal(start, direction):
    with pytest.raises(geometry.AsphereIntersectionError, match="Invalid ray"):
        geometry.profile(surface()).intersect(np.array(start), np.array(direction), z_dir=-1)


def test_small_aperture_oblique_edge_rounding_cannot_be_mislabeled_physical_loss():
    from rayoptics.raytr.traceerror import TraceMissedSurfaceError

    prescription = surface(radius=0.001, aperture=0.0001, conic=-1.0)
    exact_edge = np.array([0.0001, 0.0, 0.0001**2 / 0.002])
    direction = np.array([0.5, 0.0, -math.sqrt(0.75)])
    start = exact_edge - direction
    try:
        _, point = geometry.profile(prescription).intersect(start, direction, z_dir=-1)
    except geometry.AsphereIntersectionError:
        # A floating-point edge cannot always be resolved at this scale. It
        # must refuse the whole numerical characterization, never lose flux.
        return
    except TraceMissedSurfaceError:
        pytest.fail("The authored exact edge is not a certified physical miss")
    np.testing.assert_allclose(point, exact_edge, atol=1e-12)
