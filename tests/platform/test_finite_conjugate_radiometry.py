"""Finite-plane reciprocal geometry and independently known irradiance integrals."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import (
    finite_conjugate,
    lens_direction_response,
    lens_factory,
    optics_surfaces,
    pupil_transmission,
)


def conjugate(distance=500.0, magnification=-0.04):
    return {
        **finite_conjugate.DECLARATIONS,
        "distance_mm": distance,
        "reference_magnification": magnification,
    }


def aperture_request(distance=500.0, sensor_gap=10.0):
    request = lens_factory.request_template(5)
    request.update(wavelength_nm=[500, 600], field_radius=[0, 1], image_height_mm=[0, 0.1])
    stop = request["surfaces"][0]
    stop.update(thickness_mm=sensor_gap, semi_diameter_mm=2.0, refractive_index_after=[1, 1])
    request["surfaces"] = [stop]
    request["sampling"].update(polar_rings=16, azimuth_samples=16)
    request["stop_transmission"] = pupil_transmission.uniform_transmission(request)
    request["object_conjugate"] = conjugate(distance)
    return request


@pytest.fixture(scope="module")
def aperture():
    return lens_factory.characterize(aperture_request())


def test_planar_aperture_irradiance_is_analytic_without_object_inverse_square(aperture):
    expected = np.pi * 2**2 / (10**2 + 2**2)
    np.testing.assert_allclose(
        aperture["arrays"]["irradiance_per_radiance_sr"][:, 0], expected, rtol=1e-7,
    )
    assert aperture["summary"]["status"] == "PROPOSED"
    for distance in [1.0, 50.0, 5000.0]:
        other = lens_factory.characterize(aperture_request(distance))
        assert other["summary"]["status"] == "PROPOSED"
        for key in ["irradiance_per_radiance_sr", "angular_weights", "accepted_rays",
                    "image_direction_cosines", "object_exit_direction_cosines"]:
            np.testing.assert_array_equal(aperture["arrays"][key], other["arrays"][key])
        assert not np.array_equal(
            aperture["arrays"]["object_plane_xy_mm"], other["arrays"]["object_plane_xy_mm"]
        )


def test_actual_plane_positions_match_independent_straight_line_aperture_formula(aperture):
    arrays = aperture["arrays"]
    assert arrays["object_plane_valid"].all()
    stop_xy = arrays["stop_intersection_xy_mm"]
    # Similar triangles from sensor z=10 through aperture z=0 to object z=-500.
    expected = 51 * stop_xy
    expected[:, 1, :, 0] -= 50 * 0.1
    np.testing.assert_allclose(arrays["object_plane_xy_mm"], expected, atol=4e-12, rtol=0)
    exit_xyz = arrays["object_exit_position_xyz_mm"]
    np.testing.assert_allclose(exit_xyz[..., :2], stop_xy, atol=0, rtol=0)
    np.testing.assert_allclose(exit_xyz[..., 2], 0, atol=1e-14, rtol=0)
    displacement = np.concatenate(
        (expected - stop_xy, np.full((*expected.shape[:-1], 1), -500.0)), axis=-1
    )
    length = np.linalg.norm(displacement, axis=-1)
    np.testing.assert_allclose(arrays["object_plane_ray_distance_mm"], length, atol=1e-12)
    np.testing.assert_allclose(
        arrays["object_exit_direction_cosines"], displacement / length[..., None], atol=1e-14,
    )
    assert aperture["summary"]["object_plane_coordinates"]["z_mm"] == -500.0


def test_sensor_gap_changes_solid_angle_while_object_distance_alone_does_not(aperture):
    shifted = lens_factory.characterize(aperture_request(sensor_gap=20))
    expected = np.pi * 2**2 / (20**2 + 2**2)
    np.testing.assert_allclose(
        shifted["arrays"]["irradiance_per_radiance_sr"][:, 0], expected, rtol=1e-7,
    )
    assert not np.array_equal(
        aperture["arrays"]["irradiance_per_radiance_sr"],
        shifted["arrays"]["irradiance_per_radiance_sr"],
    )


def test_wavelength_common_signed_chart_does_not_recenter_or_change_radiometry(aperture):
    source = conjugate()
    xy = np.array([[0.0, 0.0], [0.4, -0.2]])
    np.testing.assert_array_equal(finite_conjugate.object_xy_mm(xy, source), [[0, 0], [-10, 5]])
    assert finite_conjugate.application_conditions(source) == {
        "object_distance_mm": {"value": 500.0, "unit": "mm"},
        "object_reference_magnification": {"value": -0.04, "unit": "1"},
    }
    request = aperture_request()
    request["object_conjugate"]["reference_magnification"] = -0.1
    changed = lens_factory.characterize(request)
    for name, array in aperture["arrays"].items():
        np.testing.assert_array_equal(changed["arrays"][name], array)
    assert changed["summary"]["request_sha256"] != aperture["summary"]["request_sha256"]


def test_v5_reuses_same_physical_v4_rays_and_weights_without_mutating_legacy(aperture):
    request = aperture_request()
    request["schema"] = "lens_prescription_v4"
    del request["object_conjugate"]
    before = deepcopy(request)
    legacy = lens_factory.characterize(request)
    assert request == before
    for name, array in legacy["arrays"].items():
        np.testing.assert_array_equal(aperture["arrays"][name], array)
    assert "object_conjugate" not in legacy["model_candidate"]["radiometry"]
    assert legacy["model_candidate"]["directional_response"]["schema"] == (
        "camera_lens_directional_response_v2"
    )


def test_new_radiometry_and_directional_payloads_bind_same_full_conjugate(aperture):
    candidate = aperture["model_candidate"]
    radiometry, directional = candidate["radiometry"], candidate["directional_response"]
    assert radiometry["schema"] == "camera_lens_radiometry_v2"
    assert directional["schema"] == "camera_lens_directional_response_v3"
    assert radiometry["object_conjugate"] == directional["object_conjugate"] == conjugate()
    assert radiometry["prescription_sha256"] == directional["prescription_sha256"] == (
        aperture["summary"]["request_sha256"]
    )
    checked = lens_direction_response.validate_directional_response(directional)
    assert checked == directional and checked is not directional
    checked["object_conjugate"]["distance_mm"] = 1
    assert directional["object_conjugate"]["distance_mm"] == 500
    forged = deepcopy(directional)
    del forged["object_conjugate"]
    with pytest.raises(ValueError, match="exactly"):
        lens_direction_response.validate_directional_response(forged)
    arrays = {**aperture["arrays"], "object_plane_valid": np.zeros_like(
        aperture["arrays"]["object_plane_valid"]
    )}
    with pytest.raises(ValueError, match="valid object-plane"):
        lens_direction_response.from_ray_samples(
            aperture["summary"]["normalized_request"], arrays
        )


@pytest.mark.parametrize("field,value", [
    ("distance_mm", True), ("distance_mm", 0), ("distance_mm", 1e6 + 1),
    ("distance_mm", 10**1000), ("distance_mm", float("nan")),
    ("reference_magnification", 0.04), ("reference_magnification", -1e-7),
    ("reference_magnification", -10.1), ("reference_magnification", False),
    ("support", "finite_rectangle"), ("orientation", "tilted"),
])
def test_conjugate_is_exact_bounded_and_never_inferred(field, value):
    source = conjugate()
    source[field] = value
    with pytest.raises(ValueError):
        finite_conjugate.validate_conjugate(source)
    source = conjugate() | {"inferred_focal_length_mm": 20}
    with pytest.raises(ValueError, match="exactly"):
        finite_conjugate.validate_conjugate(source)


def test_front_clearance_checks_interior_asphere_minimum_not_only_vertex_and_edge():
    front = {
        "profile": "even_asphere", "radius_mm": 1e6, "semi_diameter_mm": 3.0,
        "asphere": {"schema": "camera_even_asphere_v1", "conic_constant": 0,
                    "orders": [4, 6, 8], "coefficient_units": ["mm^-3", "mm^-5", "mm^-7"],
                    "coefficients": [-0.01, 0.001, 0.0]},
    }
    assert 0.1 + optics_surfaces.sag(front, 0) > 0
    assert 0.1 + optics_surfaces.sag(front, 3) > 0
    with pytest.raises(ValueError, match="full front surface"):
        finite_conjugate.validate_conjugate(conjugate(0.1), [front])
    assert finite_conjugate.validate_conjugate(conjugate(0.2), [front])["distance_mm"] == 0.2


@pytest.mark.parametrize("point,direction", [
    ([0, 0, 0], [0, 0, 1]), ([0, 0, 0], [1, 0, 0]),
    ([0, 0, 0], [1, 0, -1e-12]), ([0, 0, 0], [0, 0, -2]),
    ([0, 0, -501], [0, 0, -1]), ([0, 0, float("inf")], [0, 0, -1]),
    (np.array([-(2**63), 0, 0], dtype=np.int64), [0, 0, -1]),
])
def test_invalid_object_plane_ray_is_a_refusal(point, direction):
    with pytest.raises(ValueError):
        finite_conjugate.intersect_object_plane(point, direction, conjugate())


def test_one_numerically_invalid_object_ray_refuses_whole_candidate(monkeypatch):
    original = finite_conjugate.intersect_object_plane
    calls = 0

    def invalid_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("authored numerical counterexample")
        return original(*args)

    monkeypatch.setattr(finite_conjugate, "intersect_object_plane", invalid_once)
    result = lens_factory.characterize(aperture_request())
    assert result["summary"]["status"] == "REFUSED" and result["model_candidate"] is None
    assert result["summary"]["sampling"]["coarse_failure_counts"][0][0][3] == 1


def test_object_plane_visit_and_array_work_is_counted_before_tracing(aperture):
    request = aperture_request()
    request["surfaces"] = [
        {**deepcopy(request["surfaces"][0]), "id": str(i), "is_stop": i == 0}
        for i in range(24)
    ]
    request["sampling"].update(polar_rings=40, azimuth_samples=50, on_axis_azimuth_samples=8)
    request["stop_transmission"] = pupil_transmission.uniform_transmission(request)
    with pytest.raises(lens_factory.LensFactoryError) as caught:
        lens_factory.validate_request(request)
    assert caught.value.issues[0]["code"] == "RESOURCE_LIMIT"
    old = deepcopy(request)
    old["schema"] = "lens_prescription_v4"
    del old["object_conjugate"]
    lens_factory.validate_request(old)  # Exactly one million old visits; v5 adds one per ray.
    size = aperture["arrays"]["accepted_rays"].size
    assert aperture["summary"]["sampling"]["additional_fine_object_array_bytes"] == size * 49


def test_uniform_geometric_scaling_preserves_radiometry_but_scales_object_hits(aperture):
    request = aperture_request()
    factor = 3
    for surface in request["surfaces"]:
        surface["semi_diameter_mm"] *= factor
        surface["thickness_mm"] *= factor
    request["image_height_mm"] = [h * factor for h in request["image_height_mm"]]
    request["object_conjugate"]["distance_mm"] *= factor
    request["stop_transmission"] = pupil_transmission.uniform_transmission(request)
    scaled = lens_factory.characterize(request)
    np.testing.assert_allclose(scaled["arrays"]["irradiance_per_radiance_sr"],
                               aperture["arrays"]["irradiance_per_radiance_sr"], atol=1e-15)
    np.testing.assert_allclose(scaled["arrays"]["object_plane_xy_mm"],
                               factor * aperture["arrays"]["object_plane_xy_mm"], atol=1e-11)


def test_finite_asphere_preserves_actual_refracted_directions_and_masked_plane_hits():
    request = lens_factory.request_template(4)
    request["wavelength_nm"] = [450, 650]
    for surface in request["surfaces"]:
        surface["refractive_index_after"] = [
            surface["refractive_index_after"][0], surface["refractive_index_after"][-1],
        ]
    request["stop_transmission"] = pupil_transmission.uniform_transmission(request)
    legacy = lens_factory.characterize(request)
    request.update(schema="lens_prescription_v5", object_conjugate=conjugate())
    finite = lens_factory.characterize(request)
    assert legacy["summary"]["status"] == finite["summary"]["status"] == "PROPOSED"
    for key, array in legacy["arrays"].items():
        np.testing.assert_array_equal(finite["arrays"][key], array)
    arrays = finite["arrays"]
    valid = arrays["object_plane_valid"]
    assert valid.any() and (~valid).any()
    for key in ["object_plane_xy_mm", "object_exit_position_xyz_mm",
                "object_plane_ray_distance_mm"]:
        assert np.all(arrays[key][~valid] == 0)
    reached = (arrays["object_exit_position_xyz_mm"][valid]
               + arrays["object_plane_ray_distance_mm"][valid, None]
               * arrays["object_exit_direction_cosines"][valid])
    np.testing.assert_allclose(reached[:, 2], -500.0, atol=2e-12, rtol=0)
    np.testing.assert_allclose(reached[:, :2], arrays["object_plane_xy_mm"][valid],
                               atol=2e-12, rtol=0)


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_legacy_schema_does_not_silently_admit_finite_conjugate(version):
    request = lens_factory.request_template(version)
    request["object_conjugate"] = conjugate()
    with pytest.raises(lens_factory.LensFactoryError, match="exactly"):
        lens_factory.validate_request(request)
