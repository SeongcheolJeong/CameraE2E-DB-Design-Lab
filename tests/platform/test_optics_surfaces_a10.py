"""Independent bounded A10 asphere check for reviewed prescription import."""

import math

import numpy as np
import pytest
from scipy.optimize import brentq

from camera_platform import optics_surfaces


def _surface():
    return {
        "id": "a10-test",
        "profile": "even_asphere",
        "radius_mm": 20.0,
        "thickness_mm": 3.0,
        "semi_diameter_mm": 1.0,
        "refractive_index_after": [1.5, 1.5, 1.5],
        "is_stop": False,
        "asphere": {
            "schema": "camera_even_asphere_v2",
            "conic_constant": -0.4,
            "orders": [4, 6, 8, 10],
            "coefficients": [2e-4, -1e-5, 4e-6, 5e-4],
            "coefficient_units": ["mm^-3", "mm^-5", "mm^-7", "mm^-9"],
        },
    }


def _reference_sag(surface, radial):
    radius = surface["radius_mm"]
    k = surface["asphere"]["conic_constant"]
    root = math.sqrt(1.0 - (1.0 + k) * (radial / radius) ** 2)
    return radial**2 / (radius * (1.0 + root)) + math.fsum(
        coefficient * radial**power
        for power, coefficient in zip(
            [4, 6, 8, 10], surface["asphere"]["coefficients"], strict=True
        )
    )


def test_a10_is_preserved_in_geometry_and_ray_intersection():
    pytest.importorskip("rayoptics")
    surface = _surface()
    assert optics_surfaces.validate_asphere(surface) == surface["asphere"]
    no_a10 = _surface()
    no_a10["asphere"]["coefficients"][-1] = 0.0
    for radial in (0.0, 0.25, 0.6, 0.95):
        assert optics_surfaces.sag(surface, radial) == pytest.approx(
            _reference_sag(surface, radial), rel=2e-13, abs=2e-14
        )
        assert optics_surfaces.sag(surface, radial) - optics_surfaces.sag(
            no_a10, radial
        ) == pytest.approx(5e-4 * radial**10, rel=2e-9, abs=2e-16)
        if radial:
            step = 1e-5
            independent_slope = (
                _reference_sag(surface, radial + step)
                - _reference_sag(surface, radial - step)
            ) / (2 * step)
            assert optics_surfaces.radial_derivative(surface, radial) == pytest.approx(
                independent_slope, rel=2e-8, abs=2e-10
            )
    certificate = optics_surfaces.surface_bounds(surface)
    assert certificate["slope_abs_max"] < 4.0
    start = np.array([0.4, -0.25, 3.0])
    direction = np.array([0.035, 0.02, -1.0])
    direction /= np.linalg.norm(direction)

    def residual(distance):
        point = start + distance * direction
        return point[2] - _reference_sag(surface, math.hypot(*point[:2]))

    expected_distance = brentq(residual, 0.0, 6.0, xtol=1e-13)
    distance, point = optics_surfaces.profile(surface).intersect(
        start, direction, eps=1e-12, z_dir=-1
    )
    assert distance == pytest.approx(expected_distance, abs=3e-11)
    assert point[2] == pytest.approx(_reference_sag(surface, math.hypot(*point[:2])), abs=3e-11)
    radial = math.hypot(*point[:2])
    step = 1e-5
    slope = (
        _reference_sag(surface, radial + step)
        - _reference_sag(surface, radial - step)
    ) / (2 * step)
    expected_normal = np.array(
        [-point[0] * slope / radial, -point[1] * slope / radial, 1.0]
    )
    expected_normal /= np.linalg.norm(expected_normal)
    np.testing.assert_allclose(
        optics_surfaces.profile(surface).normal(point), expected_normal, atol=2e-10
    )


def test_a10_contract_rejects_truncated_or_mislabeled_coefficients():
    surface = _surface()
    surface["asphere"]["coefficients"] = [2e-4, -1e-5, 4e-6]
    with pytest.raises(ValueError, match="exactly the coefficients"):
        optics_surfaces.validate_asphere(surface)
    surface = _surface()
    surface["asphere"]["orders"] = [4, 6, 8]
    with pytest.raises(ValueError, match="explicit v1"):
        optics_surfaces.validate_asphere(surface)
