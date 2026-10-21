"""Actual reciprocal rays, annular quadrature, and strict joint direction contracts."""

import hashlib
import math
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import lens_direction_response as joint
from camera_platform import lens_factory, pupil_transmission


def _request(version=3, power=1):
    request = lens_factory.request_template(version)
    request.update(wavelength_nm=[500, 600], image_height_mm=[0, 1], field_radius=[0, 1])
    stop = deepcopy(request["surfaces"][0])
    stop.update(thickness_mm=10, semi_diameter_mm=2, refractive_index_after=[1, 1])
    request["surfaces"] = [stop]
    request["sampling"].update(polar_rings=8, azimuth_samples=8)
    if version >= 2:
        request["stop_transmission"] = pupil_transmission.uniform_transmission(request, power)
    return request


def _trace(request):
    pytest.importorskip("rayoptics")
    result = lens_factory.characterize(request)
    assert result["summary"]["status"] == "PROPOSED", result["summary"]["convergence"]
    return result


@pytest.fixture(scope="module")
def aperture():
    return _trace(_request())


def _samples(response, w, f, **kwargs):
    chunks = list(joint.iter_direction_samples(response, w, f, **kwargs))
    return np.concatenate([c[0] for c in chunks]), np.concatenate([c[1] for c in chunks])


def _fixture():
    # Authored unit-test quadrature only; these marker hashes are not Factory evidence.
    sine = 0.2
    cosine = math.sqrt(1 - sine**2)
    return {
        "schema": "camera_lens_directional_response_v1",
        "wavelength_nm": [500, 600],
        "field_radius": [0, 1],
        "image_height_mm": [0, 1],
        "field_reference_radius_mm": 1,
        "direction_convention": "propagation_into_sensor_x_column_y_row",
        "reference_plane": "sensor_surface_before_pixel_optics",
        "field_reference": "positive_x_column",
        "symmetry": "centered_spherical_or_plane_prescription_radial_stop",
        "normalization": "conditional_transmitted_irradiance_fraction",
        "prescription_sha256": "a" * 64,
        "stop_transmission_sha256": "b" * 64,
        "directions_xyz": [
            [[-sine, 0, cosine], [0, 0, 1]],
            [[sine, 0, cosine], [0, sine, cosine]],
        ],
        "conditional_weights": [[[1, 0], [0.25, 0.75]], [[1, 0], [0.8, 0.2]]],
        "on_axis_rotation_phi_deg": list(range(0, 360, 45)),
    }


def test_factory_keeps_actual_direction_and_accepted_stop_power_together(aperture):
    arrays = aperture["arrays"]
    response = aperture["model_candidate"]["directional_response"]
    np.testing.assert_array_equal(response["directions_xyz"], arrays["image_direction_cosines"])
    power = (
        arrays["quadrature_weights_sr"][None]
        * arrays["accepted_rays"]
        * arrays["stop_power_transmittance"]
    )
    np.testing.assert_allclose(
        response["conditional_weights"], power / power.sum(axis=-1, keepdims=True), atol=0, rtol=0
    )
    # Independent sign check from physical sensor position toward the sampled plane.
    reverse = np.concatenate(
        (
            arrays["plane_sample_xy_mm"] - np.array([[[0, 0]], [[1, 0]]]),
            np.full((*arrays["plane_sample_xy_mm"].shape[:2], 1), -10),
        ),
        axis=-1,
    )
    forward = -reverse / np.linalg.norm(reverse, axis=-1, keepdims=True)
    np.testing.assert_allclose(response["directions_xyz"], forward, atol=0, rtol=0)
    assert np.all(forward[..., 2] > 0)
    for w in range(2):
        for f in range(2):
            xyz, weights = _samples(response, w, f)
            assert weights.sum() == pytest.approx(1, abs=1e-14)
            np.testing.assert_allclose(np.linalg.norm(xyz, axis=-1), 1, atol=1e-14)


def test_actual_positive_x_field_rotates_into_native_cardinal_directions(aperture):
    response = aperture["model_candidate"]["directional_response"]
    means = []
    for angle in (0, 90, 180, 270):
        xyz, weights = _samples(response, 0, 1, rotation_phi_deg=angle, chunk_size=17)
        means.append(weights @ xyz)
    assert means[0][0] > 0.09  # Actual light advances toward the positive-x sensor field.
    np.testing.assert_allclose(means[0][1], 0, atol=1e-16)
    np.testing.assert_allclose(means[1], [-means[0][1], means[0][0], means[0][2]], atol=1e-15)
    np.testing.assert_allclose(means[2], [-means[0][0], -means[0][1], means[0][2]], atol=1e-15)
    np.testing.assert_allclose(means[3], [means[0][1], -means[0][0], means[0][2]], atol=1e-15)


def test_actual_annuli_cancel_azimuth_terms_and_keep_second_moments(aperture):
    response = aperture["model_candidate"]["directional_response"]
    raw_base = np.array(response["directions_xyz"])[0]
    base_power = np.array(response["conditional_weights"])[0, 0]
    assert base_power @ raw_base[:, 0] < -0.1  # A single meridian is not the illumination.
    for rotation in (0, 37, 90, 179):
        xyz, weights = _samples(response, 0, 0, rotation_phi_deg=rotation, chunk_size=73)
        assert len(weights) == np.count_nonzero(base_power) * 32
        np.testing.assert_allclose(weights @ xyz[:, :2], [0, 0], atol=1e-15)
        second = np.einsum("n,ni,nj->ij", weights, xyz, xyz)
        assert second[0, 0] == pytest.approx(second[1, 1], abs=1e-15)
        np.testing.assert_allclose(second[np.triu_indices(3, k=1)], 0, atol=1e-15)
        assert np.trace(second) == pytest.approx(1, abs=2e-14)
        # Q = a + b sin(theta) cos(phi) = a + b*x must average to a.
        assert weights @ (0.4 + 0.2 * xyz[:, 0]) == pytest.approx(0.4, abs=1e-14)


def test_direction_power_correlation_and_spectral_pairs_survive_rotation():
    fixture = _fixture()
    for w, expected_x in ((0, 0.05), (1, 0.16)):
        xyz, weights = _samples(fixture, w, 1)
        assert weights @ xyz[:, 0] == pytest.approx(expected_x)
        # An independent phi marginal times independent theta marginal would not
        # generally preserve this joint quantity for an authored unequal-theta pair.
        xyz90, weights90 = _samples(fixture, w, 1, rotation_phi_deg=90)
        np.testing.assert_array_equal(weights90, weights)
        np.testing.assert_allclose(xyz90[:, 1], xyz[:, 0], atol=1e-15)
    altered = deepcopy(fixture)
    altered["directions_xyz"][1][1] = [0, 0.5, math.sqrt(0.75)]
    xyz, weights = _samples(altered, 0, 1)
    assert weights @ (xyz[:, 0] ** 2) == pytest.approx(0.01)
    assert weights @ (xyz[:, 1] ** 2) == pytest.approx(0.1875)


def test_uniform_quarter_stop_scales_absolute_power_not_conditional_distribution(aperture):
    quarter = _trace(_request(power=0.25))
    np.testing.assert_allclose(
        quarter["arrays"]["irradiance_per_radiance_sr"],
        aperture["arrays"]["irradiance_per_radiance_sr"] / 4,
        atol=0,
        rtol=0,
    )
    clear_response = aperture["model_candidate"]["directional_response"]
    response = quarter["model_candidate"]["directional_response"]
    assert response["conditional_weights"] == clear_response["conditional_weights"]
    assert response["directions_xyz"] == clear_response["directions_xyz"]
    assert response["prescription_sha256"] != clear_response["prescription_sha256"]
    assert response["stop_transmission_sha256"] != clear_response["stop_transmission_sha256"]


def test_actual_radial_spectral_stop_changes_conditional_joint_weights():
    request = _request()
    request["stop_transmission"]["power_transmittance"] = [[1, 0.2], [0.2, 1]]
    result = _trace(request)
    response = result["model_candidate"]["directional_response"]
    arrays = result["arrays"]
    weights = np.array(response["conditional_weights"])
    assert not np.allclose(weights[0], weights[1])
    expected_t = 1 - 0.8 * np.linalg.norm(arrays["stop_intersection_xy_mm"][0], axis=-1) / 2
    np.testing.assert_allclose(arrays["stop_power_transmittance"][0], expected_t, atol=1e-14)
    expected = expected_t * arrays["quadrature_weights_sr"] * arrays["accepted_rays"][0]
    np.testing.assert_allclose(weights[0], expected / expected.sum(axis=-1, keepdims=True))
    assert response["prescription_sha256"] == pupil_transmission.digest(
        lens_factory.validate_request(request)
    )
    assert response["stop_transmission_sha256"] == pupil_transmission.digest(
        result["summary"]["normalized_request"]["stop_transmission"]
    )
    assert (
        result["model_candidate"]["radiometry"]["prescription_sha256"]
        == response["prescription_sha256"]
    )
    assert result["summary"]["directional_response"]["sha256"] == pupil_transmission.digest(
        response
    )
    assert result["summary"]["directional_response"]["integration_error"] == "unknown"


def test_zero_power_samples_are_filtered_and_allocation_is_chunk_bounded():
    fixture = _fixture()
    original = deepcopy(fixture)
    chunks = list(joint.iter_direction_samples(fixture, 0, 0, chunk_size=1))
    assert len(chunks) == 8
    assert all(xyz.shape == (1, 3) and weight.shape == (1,) for xyz, weight in chunks)
    assert sum(weight.sum() for _, weight in chunks) == 1
    assert fixture == original
    normalized = joint.validate_directional_response(fixture)
    normalized["conditional_weights"][0][0][0] = 0.5
    assert fixture == original


@pytest.mark.parametrize(
    "failure",
    [
        "missing_identity",
        "bad_hash",
        "reverse",
        "cell_plane",
        "zero_power",
        "nan",
        "boolean",
        "nonunit",
        "grazing",
        "ragged",
        "bad_field",
        "nonuniform_phi",
        "duplicated_360",
        "bad_axis",
    ],
)
def test_malformed_or_wrong_reference_joint_response_is_rejected(failure):
    fixture = _fixture()
    if failure == "missing_identity":
        fixture.pop("prescription_sha256")
    elif failure == "bad_hash":
        fixture["prescription_sha256"] = "unverified"
    elif failure == "reverse":
        fixture["direction_convention"] = "toward_pupil"
    elif failure == "cell_plane":
        fixture["reference_plane"] = "periodic_cell"
    elif failure == "zero_power":
        fixture["conditional_weights"][0][0] = [0, 0]
    elif failure == "nan":
        fixture["conditional_weights"][0][0][0] = float("nan")
    elif failure == "boolean":
        fixture["conditional_weights"][0][0][0] = True
    elif failure == "nonunit":
        fixture["directions_xyz"][1][0] = [0, 0, 0.5]
    elif failure == "grazing":
        fixture["directions_xyz"][1][0] = [1, 0, 0]
    elif failure == "ragged":
        fixture["directions_xyz"][1].pop()
    elif failure == "bad_field":
        fixture["field_reference_radius_mm"] = 2
    elif failure == "nonuniform_phi":
        fixture["on_axis_rotation_phi_deg"][1] = 46
    elif failure == "duplicated_360":
        fixture["on_axis_rotation_phi_deg"][-1] = 360
    else:
        fixture["directions_xyz"][0][0] = [0.2, 0, math.sqrt(0.96)]
    with pytest.raises(ValueError):
        joint.validate_directional_response(fixture)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"wave_index": -1},
        {"field_index": 2},
        {"wave_index": True},
        {"rotation_phi_deg": float("inf")},
        {"chunk_size": 0},
        {"chunk_size": 131073},
    ],
)
def test_invalid_iteration_bounds_fail_explicitly(kwargs):
    with pytest.raises(ValueError):
        list(
            joint.iter_direction_samples(
                _fixture(), **{"wave_index": 0, "field_index": 0, **kwargs}
            )
        )


@pytest.mark.parametrize(
    "failure", ["missing_azimuth", "odd_azimuth", "base_limit", "expansion_limit"]
)
def test_v3_resource_limits_and_explicit_annulus_quadrature_before_solver(failure):
    request = _request()
    if failure == "missing_azimuth":
        request["sampling"].pop("on_axis_azimuth_samples")
    elif failure == "odd_azimuth":
        request["sampling"]["on_axis_azimuth_samples"] = 9
    elif failure == "base_limit":
        request["sampling"].update(polar_rings=48, azimuth_samples=48)
    else:
        request["sampling"].update(polar_rings=44, azimuth_samples=44, on_axis_azimuth_samples=128)
    with pytest.raises(lens_factory.LensFactoryError):
        lens_factory.validate_request(request)


def test_legacy_request_rejects_new_quadrature_field_and_zero_v3_never_normalizes():
    request = _request(version=2)
    request["sampling"]["on_axis_azimuth_samples"] = 32
    with pytest.raises(lens_factory.LensFactoryError):
        lens_factory.validate_request(request)
    pytest.importorskip("rayoptics")
    zero = lens_factory.characterize(_request(power=0))
    assert zero["model_candidate"] is None
    assert zero["summary"]["directional_response"] is None
    assert zero["summary"]["status"] == "REFUSED"


@pytest.mark.parametrize(
    "version,expected",
    [
        (1, "14c5b9f2f575d6c4ebc30afb19bc9592af5fca4587264716886ec51d844d6dae"),
        (2, "31deb5815c89413d89c6aa0ddb4bb92431c93a878988212291b0d1c7a7296f65"),
    ],
)
def test_v1_v2_complete_actual_result_hashes_unchanged(version, expected):
    # Captured from actual RayOptics 0.9.8 before the v3 change: includes the entire
    # normalized summary/candidate and byte SHA, shape, dtype of every array.
    result = _trace(_request(version))
    fingerprint = {
        "summary": result["summary"],
        "model_candidate": result["model_candidate"],
        "arrays": {
            key: {
                "shape": list(value.shape),
                "dtype": str(value.dtype),
                "sha256": hashlib.sha256(value.tobytes()).hexdigest(),
            }
            for key, value in result["arrays"].items()
        },
    }
    assert pupil_transmission.digest(fingerprint) == expected
