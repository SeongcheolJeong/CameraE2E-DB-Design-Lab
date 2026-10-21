"""Explicit A12 support: independent geometry and pinned DB translation.

The source apertures/dispersion remain research assumptions. Admission of the
complete 36-surface prescription does not establish PSF or radiometry convergence.
"""

import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import brentq

from camera_platform import lens_db_wavefront_bridge as bridge
from camera_platform import optics_surfaces

ROOT = Path(__file__).resolve().parents[2] / "camerae2e_db"
WIDE_REVISION = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
WIDE_CSV = "b0cfd975001ae7e8ddff2144743cf500031253ce6ff1f160b50d0ac7dfc7c318"
ORDERS = (4, 6, 8, 10, 12)
COEFFICIENTS = {
    26: (-1.21184e-5, 9.60901e-9, 1.00214e-10, -1.03515e-12, 3.97208e-15),
    27: (1.23845e-5, -1.68216e-9, 6.82894e-11, -7.71348e-13, 3.45382e-15),
}


def surface(number=26):
    return {
        "id": f"pinned-a12-{number}",
        "profile": "even_asphere",
        "radius_mm": 38.604 if number == 26 else -38.617,
        "thickness_mm": 5.22 if number == 26 else 0.18,
        "semi_diameter_mm": 10.81 if number == 26 else 11.09,
        "is_stop": False,
        "refractive_index_after": [1.58313, 1.58313] if number == 26 else [1.0, 1.0],
        "asphere": {
            "schema": "camera_even_asphere_v3",
            "conic_constant": 0.0,
            "orders": list(ORDERS),
            "coefficients": list(COEFFICIENTS[number]),
            "coefficient_units": ["mm^-3", "mm^-5", "mm^-7", "mm^-9", "mm^-11"],
        },
    }


def reference(s, radial):
    """Signed conic vertex sheet plus an independently evaluated polynomial."""
    radius, k = s["radius_mm"], s["asphere"]["conic_constant"]
    conic = radial**2 / (radius * (1 + math.sqrt(1 - (1 + k) * (radial / radius) ** 2)))
    coefficients = s["asphere"]["coefficients"]
    sag = conic + math.fsum(a * radial**n for a, n in zip(coefficients, ORDERS, strict=True))
    # Differentiate the implicit conic r² - 2Rz + (1+k)z² = 0.
    slope = radial / (radius - (1 + k) * conic) + math.fsum(
        n * a * radial ** (n - 1) for a, n in zip(coefficients, ORDERS, strict=True)
    )
    return sag, slope


def test_a12_schema_is_explicit_dimensioned_and_detached():
    s = surface()
    parsed = optics_surfaces.validate_asphere(s)
    assert parsed == s["asphere"]
    parsed["coefficients"][-1] = 0.0
    parsed["orders"][-1] = 14
    parsed["coefficient_units"][-1] = "mm^-13"
    assert s["asphere"]["coefficients"][-1] == 3.97208e-15
    assert s["asphere"]["orders"][-1] == 12
    assert s["asphere"]["coefficient_units"][-1] == "mm^-11"


@pytest.mark.parametrize("mutation", [
    {"schema": "camera_even_asphere_v1"},
    {"schema": "camera_even_asphere_v2"},
    {"schema": "camera_even_asphere_v4"},
    {"orders": [4, 6, 8, 10, 14]},
    {"orders": [4, 6, 8, 10, 12.0]},
    {"coefficients": list(COEFFICIENTS[26][:-1])},
    {"coefficients": [*COEFFICIENTS[26][:-1], float("nan")]},
    {"coefficient_units": ["mm^-3", "mm^-5", "mm^-7", "mm^-9", "mm^-12"]},
])
def test_a12_cannot_be_silently_added_to_legacy_or_malformed_contracts(mutation):
    s = surface()
    s["asphere"].update(mutation)
    with pytest.raises(ValueError):
        optics_surfaces.validate_asphere(s)


@pytest.mark.parametrize("number", [26, 27])
def test_a12_analytic_sag_slope_and_whole_aperture_bounds(number):
    s = surface(number)
    bounds = optics_surfaces.surface_bounds(s)
    for radial in np.linspace(0.0, s["semi_diameter_mm"], 251):
        expected, slope = reference(s, float(radial))
        assert optics_surfaces.sag(s, radial) == pytest.approx(expected, abs=2e-15)
        assert optics_surfaces.radial_derivative(s, radial) == pytest.approx(slope, abs=2e-15)
        assert bounds["sag_min_mm"] <= expected <= bounds["sag_max_mm"]
        assert abs(slope) <= bounds["slope_abs_max"]
    without_a12 = deepcopy(s)
    without_a12["asphere"]["coefficients"][-1] = 0.0
    edge = s["semi_diameter_mm"]
    departure_um = (reference(s, edge)[0] - reference(without_a12, edge)[0]) * 1000
    assert departure_um == pytest.approx(
        10.114078365538434 if number == 26 else 11.953036724427841, abs=5e-12
    )
    # A12 cannot be discarded as numerically irrelevant at the authored aperture.
    assert departure_um > 10


@pytest.mark.parametrize("number", [26, 27])
@pytest.mark.parametrize("sense", [-1, 1])
def test_actual_rayoptics_a12_intersection_normal_snell_and_reciprocity(number, sense):
    from rayoptics.elem.profiles import Spherical
    from rayoptics.elem.surface import Surface
    from rayoptics.raytr.raytrace import trace_raw

    s = surface(number)
    start = np.array([8.0, 2.0, -5.0 * sense])
    direction = np.array([0.025, -0.018, float(sense)])
    direction /= np.linalg.norm(direction)

    def residual(distance):
        p = start + distance * direction
        return p[2] - reference(s, math.hypot(*p[:2]))[0]

    distance = brentq(residual, 0, 10, xtol=1e-13, rtol=1e-14)
    expected = start + distance * direction
    radial = math.hypot(*expected[:2])
    slope = reference(s, radial)[1] / radial
    normal = np.array([-expected[0] * slope, -expected[1] * slope, 1.0])
    normal /= np.linalg.norm(normal)
    before, after = (1.0, 1.58313) if sense == 1 else (1.58313, 1.0)

    def snell(d, n1, n2):
        tangent = n1 / n2 * (d - np.dot(d, normal) * normal)
        return tangent + math.copysign(
            math.sqrt(1 - np.dot(tangent, tangent)), np.dot(d, normal)
        ) * normal

    dummy = Surface(profile=Spherical(c=0), interact_mode="dummy")
    actual = optics_surfaces.profile(s)
    interface = Surface(profile=actual, max_ap=s["semi_diameter_mm"])
    transform = (np.eye(3), np.zeros(3))
    path = [(dummy, None, transform, before, sense), (interface, None, transform, after, sense)]
    rays, opl, _ = trace_raw(
        iter(path), start, direction, 550.0, intersect_obj=False, check_apertures=True
    )
    np.testing.assert_allclose(rays[-1][0], expected, atol=3e-11, rtol=0)
    np.testing.assert_allclose(rays[-1][3], normal, atol=3e-12, rtol=0)
    np.testing.assert_allclose(rays[-1][1], snell(direction, before, after), atol=3e-12, rtol=0)
    assert opl == pytest.approx(before * distance, abs=5e-11)
    np.testing.assert_allclose(snell(-rays[-1][1], after, before), -direction, atol=3e-12)
    # Backend polynomial coefficients and the guarded root must agree on A12.
    assert actual.sag(*expected[:2]) == pytest.approx(expected[2], abs=3e-11)


@pytest.mark.parametrize("scale", [0.25, 2.0, 10.0])
def test_a12_dimensioned_scaling_preserves_actual_hits_and_angles(scale):
    s = surface(27)
    larger = deepcopy(s)
    for key in ("radius_mm", "thickness_mm", "semi_diameter_mm"):
        larger[key] *= scale
    larger["asphere"]["coefficients"] = [
        a / scale ** (n - 1) for a, n in zip(s["asphere"]["coefficients"], ORDERS, strict=True)
    ]
    p, d = np.array([8.0, 2.0, 5.0]), np.array([0.025, -0.018, -1.0])
    d /= np.linalg.norm(d)
    original, scaled = optics_surfaces.profile(s), optics_surfaces.profile(larger)
    distance, hit = original.intersect(p, d, z_dir=-1)
    new_distance, new_hit = scaled.intersect(p * scale, d, z_dir=-1)
    assert new_distance == pytest.approx(distance * scale, abs=3e-10)
    np.testing.assert_allclose(new_hit, hit * scale, atol=3e-10)
    np.testing.assert_allclose(scaled.normal(new_hit), original.normal(hit), atol=3e-11)


@pytest.mark.parametrize("number", [26, 27])
def test_zero_a12_preserves_v2_sag_and_actual_hit_exactly(number):
    new, old = surface(number), surface(number)
    new["asphere"]["coefficients"][-1] = 0.0
    old["asphere"]["schema"] = "camera_even_asphere_v2"
    for key in ("coefficients", "orders", "coefficient_units"):
        old["asphere"][key].pop()
    assert optics_surfaces.validate_asphere(old) == old["asphere"]
    for r in (0.0, 1.0, 8.0, 10.0):
        assert optics_surfaces.sag(new, r) == optics_surfaces.sag(old, r)
        assert optics_surfaces.radial_derivative(new, r) == optics_surfaces.radial_derivative(
            old, r
        )
    start, direction = np.array([8.0, 2.0, 5.0]), np.array([0.0, 0.0, -1.0])
    new_profile, old_profile = optics_surfaces.profile(new), optics_surfaces.profile(old)
    new_hit = new_profile.intersect(start, direction, z_dir=-1)
    old_hit = old_profile.intersect(start, direction, z_dir=-1)
    assert new_hit[0] == old_hit[0]
    np.testing.assert_array_equal(new_hit[1], old_hit[1])
    np.testing.assert_array_equal(new_profile.normal(new_hit[1]), old_profile.normal(old_hit[1]))


def test_large_a12_is_still_refused_by_unchanged_sag_slope_certificate():
    s = surface()
    s["asphere"]["coefficients"][-1] = 1e-5
    assert optics_surfaces.validate_asphere(s)
    with pytest.raises(ValueError, match="bounded sag/slope"):
        optics_surfaces.profile(s)


def test_pinned_wide_angle_retains_all_36_surfaces_and_exact_a12_terms():
    evidence = bridge.db_wavefront_preflight(
        WIDE_REVISION, catalog_root=ROOT, reviewed_csv_sha256=WIDE_CSV,
        aperture_hypothesis=bridge.APERTURE_HYPOTHESIS,
    )
    assert evidence["status"] == "research_request_ready"
    assert evidence["blockers"] == []
    assert evidence["source_surface_count"] == evidence["sequential_surface_count"] == 36
    assert evidence["translation_audit"] == {
        "converted_surface_count": 36,
        "nonzero_profile_orders": [4, 6, 8, 10, 12],
        "a10_surface_orders": [26, 27],
        "retained_numbered_image_surface_orders": [36],
        "stop_surface_orders": [15],
    }
    assert evidence["source"]["reviewed_csv_sha256"] == WIDE_CSV
    surfaces = evidence["psf_request"]["prescription"]["surfaces"]
    assert len(surfaces) == 36
    for number in (26, 27):
        converted = surfaces[number - 1]
        assert converted["asphere"] == surface(number)["asphere"]
        assert converted["semi_diameter_mm"] == surface(number)["semi_diameter_mm"]
    assert surfaces[14]["is_stop"] is True
    assert surfaces[-1]["radius_mm"] == -56.995
    assert surfaces[-1]["thickness_mm"] == 13.5
    certificate = optics_surfaces.certify_prescription(surfaces)
    assert len(certificate["surfaces"]) == 36
    assert min(gap["minimum_gap_mm"] for gap in certificate["adjacent_gaps"]) > 1e-6
    assert evidence["physical_validation"] == "not_established"
    assert evidence["product_ranking_allowed"] is False
    assert evidence["directional_radiometry_preflight"]["status"] == (
        "input_domain_supported_uncharacterized"
    )


def test_existing_p0014_a10_psf_request_bytes_unchanged():
    evidence = bridge.db_wavefront_preflight(
        "6c48e967-74b4-5901-8186-ba6fafe4797b", catalog_root=ROOT,
        reviewed_csv_sha256="f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd",
        aperture_hypothesis=bridge.APERTURE_HYPOTHESIS,
    )
    content = json.dumps(evidence["psf_request"], sort_keys=True, separators=(",", ":")).encode()
    # Pinned before A12 admission; radiometry sampling is a separate contract.
    assert hashlib.sha256(content).hexdigest() == (
        "4dd49df1142c0ff58055723f31455b10d6a47946af3badd251a564adc0953e50"
    )


def test_bridge_still_refuses_a14_instead_of_truncating_source():
    row = {
        "surface_order": 1, "surface_key": "S1", "surface_kind": "surface",
        "radius_mm": 38.604, "thickness_mm": 5.22, "effective_aperture_mm": 21.62,
        "coefficients": {"a14": 1e-18}, "conic": 0.0,
        "nd": 1.0, "vd": None, "material": "air",
    }
    with pytest.raises(bridge.LensDBWavefrontError) as error:
        bridge._surface(row, [550.0, 650.0])
    assert error.value.code == "asphere_order_unsupported"
