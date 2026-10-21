"""Independent finite-object geometry/OPL references, not physical qualification.

The references use surface ABCD matrices, Euclidean material paths and a line /
sphere intersection. They do not use backend first-order or EIC OPD formulas.
"""

import math
import time
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import lens_factory, prescription_psf


def lens_matrix(surfaces, wavelength_index):
    """First vertex to last vertex, with coordinates (height, n * slope)."""
    matrix = np.eye(2)
    before = 1.0
    for index, surface in enumerate(surfaces):
        after = surface["refractive_index_after"][wavelength_index]
        radius = surface["radius_mm"]
        power = 0.0 if radius is None else (after - before) / radius
        matrix = np.array([[1.0, 0.0], [-power, 1.0]]) @ matrix
        if index < len(surfaces) - 1:
            matrix = np.array([[1.0, surface["thickness_mm"] / after], [0.0, 1.0]]) @ matrix
        before = after
    return matrix


def conjugate_from_matrix(surfaces, distance, wavelength_index):
    matrix = lens_matrix(surfaces, wavelength_index) @ np.array([[1.0, distance], [0.0, 1.0]])
    image_gap = -matrix[0, 1] / matrix[1, 1]
    final = np.array([[1.0, image_gap], [0.0, 1.0]]) @ matrix
    assert abs(final[0, 1]) < 1e-10
    return image_gap, final[0, 0]


def finite_request(distance=500.0):
    request = prescription_psf.request_template(version=5)
    prescription = request["prescription"]
    # Independently author the sensor location and reference chart at the last
    # wavelength; other wavelengths must retain this one physical chart.
    gap, magnification = conjugate_from_matrix(
        prescription["surfaces"], distance, len(prescription["wavelength_nm"]) - 1
    )
    prescription["surfaces"][-1]["thickness_mm"] = float(gap)
    prescription["object_conjugate"]["distance_mm"] = distance
    prescription["object_conjugate"]["reference_magnification"] = float(magnification)
    return request


def physical_path_to_sphere(ray, prescription, wavelength_index, center, radius):
    """Object -> material surfaces -> near reference-sphere intersection."""
    surfaces = prescription["surfaces"]
    vertices = np.r_[0.0, np.cumsum([s["thickness_mm"] for s in surfaces[:-1]])]
    object_point = np.array(ray[0][0], dtype=float)
    object_point[2] -= prescription["object_conjugate"]["distance_mm"]
    before = 1.0
    previous = object_point
    terms = []
    for index, (surface, vertex) in enumerate(zip(surfaces, vertices, strict=True)):
        point = np.array(ray[index + 1][0], dtype=float)
        point[2] += vertex
        length = float(np.linalg.norm(point - previous))
        assert ray[index][2] == pytest.approx(length, rel=1e-12, abs=1e-10)
        terms.append(before * length)
        previous = point
        before = surface["refractive_index_after"][wavelength_index]
    point, direction = ray[-2][:2]
    offset = point - center
    dot = float(direction @ offset)
    discriminant = dot * dot - float(offset @ offset) + radius * radius
    assert discriminant >= 0
    distance = -dot - math.sqrt(discriminant)
    sphere_point = point + distance * direction
    assert np.linalg.norm(sphere_point - center) == pytest.approx(radius, abs=1e-10)
    return math.fsum([*terms, before * distance]), terms[0]


@pytest.mark.parametrize("distance", [100.0, 500.0, 2000.0])
@pytest.mark.parametrize("wavelength_index", [0, 1])
def test_finite_paraxial_focus_and_magnification_match_independent_surface_matrix(
    distance, wavelength_index, monkeypatch
):
    from rayoptics.raytr import waveabr

    request = finite_request(distance)
    prescription = request["prescription"]
    surfaces = prescription["surfaces"]
    model = prescription_psf._model(request, wavelength_index)
    expected_gap, expected_m = conjugate_from_matrix(surfaces, distance, wavelength_index)
    first_order = model["ar"]["parax_data"].fod
    assert first_order.img_dist == pytest.approx(expected_gap, rel=1e-12)
    assert first_order.m == pytest.approx(expected_m, rel=1e-12)
    assert first_order.efl == pytest.approx(-1 / lens_matrix(surfaces, wavelength_index)[1, 0])
    sensor_gap = surfaces[-1]["thickness_mm"]
    total = (
        np.array([[1.0, sensor_gap], [0.0, 1.0]])
        @ lens_matrix(surfaces, wavelength_index)
        @ np.array([[1.0, distance], [0.0, 1.0]])
    )
    reference_m = prescription["object_conjugate"]["reference_magnification"]
    sensor_y = abs(reference_m) * 1e-5
    seen = []
    original = waveabr.wave_abr_full_calc

    def inspect_chief(fod, field, wavelength, focus, ray, chief, sphere):
        physical = chief[0].ray
        np.testing.assert_allclose(physical[0][0], [0, sensor_y / reference_m, 0], atol=1e-13)
        initial = physical[0][1]
        prediction = total @ [physical[0][0][1], initial[1] / initial[2]]
        assert physical[-1][0][1] == pytest.approx(prediction[0], abs=1e-11)
        assert np.linalg.norm(physical[model.seq_model.stop_surface][0][:2]) < 1e-8
        seen.append(True)
        return original(fod, field, wavelength, focus, ray, chief, sphere)

    monkeypatch.setattr(waveabr, "wave_abr_full_calc", inspect_chief)
    prescription_psf._wavefront(
        request, model, prescription["wavelength_nm"][wavelength_index], 0, sensor_y, 16,
        time.monotonic() + 20,
    )
    assert seen


@pytest.mark.parametrize("distance", [100.0, 500.0, 2000.0])
@pytest.mark.parametrize("sensor_xy", [(0.0, 0.0), (0.024, -0.032)])
def test_finite_opd_matches_actual_object_path_and_sphere_not_plane_wave(
    distance, sensor_xy, monkeypatch
):
    from rayoptics.raytr import waveabr

    request = finite_request(distance)
    prescription = request["prescription"]
    model = prescription_psf._model(request, 0)
    radial = math.hypot(*sensor_xy)
    expected_object_y = radial / prescription["object_conjugate"]["reference_magnification"]
    center = np.array([0.0, radial, prescription["surfaces"][-1]["thickness_mm"]])
    expected_values, errors_without_object_leg, initial_directions = [], [], []
    original = waveabr.wave_abr_full_calc

    def independent_opd(fod, field, wavelength, focus, ray, chief, sphere):
        np.testing.assert_allclose(ray[0][0][0], [0, expected_object_y, 0], atol=1e-11)
        np.testing.assert_allclose(chief[0].ray[0][0], [0, expected_object_y, 0], atol=1e-11)
        actual = original(fod, field, wavelength, focus, ray, chief, sphere)
        reference, chief_object_leg = physical_path_to_sphere(
            chief[0].ray, prescription, 0, center, sphere[2]
        )
        sampled, ray_object_leg = physical_path_to_sphere(
            ray[0], prescription, 0, center, sphere[2]
        )
        expected = reference - sampled
        assert actual * 1e6 == pytest.approx(expected * 1e6, abs=0.002)
        expected_values.append(expected * 1e6)
        errors_without_object_leg.append(abs(chief_object_leg - ray_object_leg) * 1e6)
        initial_directions.append(ray[0][0][1])
        return actual

    monkeypatch.setattr(waveabr, "wave_abr_full_calc", independent_opd)
    result = prescription_psf._wavefront(
        request, model, prescription["wavelength_nm"][0], *sensor_xy, 16,
        time.monotonic() + 20,
    )
    assert len(expected_values) == int(result["admitted"].sum())
    np.testing.assert_allclose(result["opd_nm"][result["admitted"]], expected_values, atol=0.002)
    assert max(errors_without_object_leg) > 20  # A flat input phase is detectably wrong.
    assert np.ptp(np.array(initial_directions)[:, :2], axis=0).max() > 1e-4


def test_effective_focal_length_is_not_finite_projection_scale():
    request = finite_request(100.0)
    surfaces = request["prescription"]["surfaces"]
    _, magnification = conjugate_from_matrix(surfaces, 100.0, 0)
    old_scale = -1 / lens_matrix(surfaces, 0)[1, 0] / 100.0
    assert abs(abs(magnification) - old_scale) / abs(magnification) > 0.15
    assert surfaces[-1]["thickness_mm"] > -1 / lens_matrix(surfaces, 0)[1, 0]


@pytest.mark.parametrize("sensor_gap", [18.0, 19.5, 23.0])
def test_exit_pupil_is_the_stop_image_and_does_not_follow_sensor_defocus(sensor_gap):
    request = finite_request()
    surfaces = request["prescription"]["surfaces"]
    assert surfaces[0]["is_stop"] and surfaces[0]["radius_mm"] is None
    surfaces[-1]["thickness_mm"] = sensor_gap
    # Image the first-surface stop through the powered surfaces. The physical
    # pupil position cannot depend on the later arbitrary sensor location.
    matrix = lens_matrix(surfaces, 0)
    exit_z = -matrix[0, 1] / matrix[1, 1]
    pupil_m = matrix[0, 0] + exit_z * matrix[1, 0]
    model = prescription_psf._model(request, 0)
    _, reference, chief = prescription_psf._finite_reference(model, 500, 0, 0)
    np.testing.assert_allclose(chief[1][0], [0, 0, exit_z], atol=1e-11)
    assert reference[2] == pytest.approx(sensor_gap - exit_z, abs=1e-11)
    assert model["ar"]["parax_data"].fod.exp_radius == pytest.approx(
        abs(pupil_m) * surfaces[0]["semi_diameter_mm"], rel=1e-12
    )


def reciprocal_pass(prescription):
    data = lens_factory.validate_request(prescription)
    backend = lens_factory._backend()
    paths = lens_factory._paths(data, backend)
    theta = np.linspace(0, 85, data["sampling"]["theta_bins"] + 1)
    return lens_factory._pass(data, backend, paths, theta, 1, time.monotonic() + 20)


@pytest.mark.parametrize("distance", [100.0, 2000.0])
def test_reciprocal_plane_hits_forward_retrace_to_original_sensor(distance):
    from rayoptics.raytr.raytrace import trace_raw

    request = finite_request(distance)
    prescription = request["prescription"]
    prescription["sampling"].update(polar_rings=8, azimuth_samples=8)
    observed = reciprocal_pass(prescription)
    accepted = observed["accepted_rays"]
    np.testing.assert_array_equal(observed["object_plane_valid"], accepted)
    assert not observed["failure_counts"][..., -1].any()
    checked = 0
    for wi, wavelength in enumerate(prescription["wavelength_nm"]):
        model = prescription_psf._model(request, wi)
        for fi, sensor_x in enumerate(prescription["image_height_mm"]):
            indices = np.flatnonzero(accepted[wi, fi])
            assert len(indices) >= 2
            for sample in indices[::max(1, len(indices) // 4)]:
                first = observed["object_exit_position_xyz_mm"][wi, fi, sample]
                direction = observed["object_exit_direction_cosines"][wi, fi, sample]
                xy = observed["object_plane_xy_mm"][wi, fi, sample]
                point = np.r_[xy, -distance]
                delta = point - first
                travel = float(np.linalg.norm(delta))
                assert travel > 0
                np.testing.assert_allclose(delta / travel, direction, atol=2e-12)
                assert observed["object_plane_ray_distance_mm"][wi, fi, sample] == pytest.approx(
                    travel, rel=1e-12
                )
                # Trace the reciprocal plane endpoint forward through the
                # actual material sequence; no production plane-hit helper.
                ray, _, _ = trace_raw(
                    model.seq_model.path(wl=wavelength), np.r_[xy, 0.0], -direction, wavelength,
                    intersect_obj=False, check_apertures=True, pt_inside_fuzz=0.0,
                )
                np.testing.assert_allclose(ray[-1][0], [sensor_x, 0, 0], atol=2e-9)
                np.testing.assert_allclose(
                    ray[-1][1], observed["image_direction_cosines"][fi, sample], atol=2e-11
                )
                checked += 1
    assert checked >= 12
    for key in (
        "object_exit_position_xyz_mm", "object_plane_xy_mm", "object_plane_ray_distance_mm"
    ):
        assert np.all(observed[key][~accepted] == 0)


def test_uniform_plane_radiometry_has_no_object_distance_inverse_square_factor():
    first = finite_request(100.0)["prescription"]
    first["sampling"].update(polar_rings=8, azimuth_samples=8)
    second = deepcopy(first)
    second["object_conjugate"]["distance_mm"] = 2000.0
    near, far = reciprocal_pass(first), reciprocal_pass(second)
    for key in (
        "irradiance_per_radiance_sr", "relative_illumination", "angular_weights",
        "accepted_rays", "quadrature_weights_sr", "stop_power_transmittance",
    ):
        np.testing.assert_array_equal(near[key], far[key])
    assert not np.array_equal(near["object_plane_xy_mm"], far["object_plane_xy_mm"])
    assert np.max(far["object_plane_ray_distance_mm"]) > np.max(
        near["object_plane_ray_distance_mm"]
    )


@pytest.mark.parametrize("image_gap", [10.0, 20.0])
def test_uniform_finite_plane_matches_exact_aperture_solid_angle(image_gap):
    prescription = finite_request()["prescription"]
    stop = deepcopy(next(s for s in prescription["surfaces"] if s["is_stop"]))
    stop.update(thickness_mm=image_gap, semi_diameter_mm=0.5)
    prescription["surfaces"] = [stop]
    prescription["sampling"].update(polar_rings=16, azimuth_samples=16)
    output = reciprocal_pass(prescription)
    # Integral over the visible cone: 2*pi*integral(cos(theta)*sin(theta),dtheta).
    expected = math.pi * 0.5**2 / (image_gap**2 + 0.5**2)
    np.testing.assert_allclose(output["irradiance_per_radiance_sr"][:, 0], expected, rtol=1e-10)
