"""Actual aspheric reciprocal radiometry and v1-v3 compatibility checks."""

import math
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import lens_factory, optics_surfaces, pupil_transmission


def small_request(version=4):
    request = lens_factory.request_template(version)
    request["wavelength_nm"] = [450, 650]
    for surface in request["surfaces"]:
        surface["refractive_index_after"] = [
            surface["refractive_index_after"][0],
            surface["refractive_index_after"][-1],
        ]
    if version >= 2:
        request["stop_transmission"] = pupil_transmission.uniform_transmission(request)
    return request


@pytest.fixture(scope="module")
def aspheric_result():
    pytest.importorskip("rayoptics")
    return lens_factory.characterize(small_request())


def test_v4_factory_preserves_authored_units_geometry_directions_and_unknowns(aspheric_result):
    result = aspheric_result
    summary, arrays, payload = result["summary"], result["arrays"], result["model_candidate"]
    assert summary["status"] == "PROPOSED", summary["convergence"]
    assert summary["schema"] == "lens_characterization_v4"
    assert summary["normalized_request"]["surfaces"][1]["asphere"]["coefficients"] == [1e-5, 0, 0]
    assert summary["physical_validation"] == "not_established"
    assert summary["uncertainty"] == {"kind": "unknown"}
    assert summary["approval_status"] == "unapproved"
    assert summary["geometry_certificate"]["adjacent_gaps"][0]["minimum_gap_mm"] > 0
    assert payload["directional_response"]["schema"] == "camera_lens_directional_response_v2"
    assert payload["radiometry"]["prescription_sha256"] == summary["request_sha256"]
    assert payload["directional_response"]["prescription_sha256"] == summary["request_sha256"]
    accepted = arrays["accepted_rays"]
    assert accepted.any()
    assert not arrays["failure_counts"][:, :, 3].any()
    np.testing.assert_allclose(
        np.linalg.norm(arrays["object_exit_direction_cosines"][accepted], axis=-1), 1
    )
    np.testing.assert_allclose(
        np.sum(
            arrays["quadrature_weights_sr"][None, :, :]
            * accepted
            * arrays["stop_power_transmittance"],
            axis=-1,
        ),
        arrays["irradiance_per_radiance_sr"],
        rtol=2e-14,
    )


def test_zero_departure_v4_reproduces_actual_v3_ray_optics_without_changing_v3_input():
    old_request = small_request(3)
    preserved = deepcopy(old_request)
    old = lens_factory.characterize(old_request)
    request = deepcopy(old_request)
    request["schema"] = "lens_prescription_v4"
    request["surfaces"][1].update(
        profile="even_asphere",
        asphere={
            "schema": "camera_even_asphere_v1",
            "conic_constant": 0.0,
            "orders": [4, 6, 8],
            "coefficients": [0.0, 0.0, 0.0],
            "coefficient_units": ["mm^-3", "mm^-5", "mm^-7"],
        },
    )
    new = lens_factory.characterize(request)
    assert old_request == preserved
    assert old["summary"]["schema"] == "lens_characterization_v3"
    assert "geometry_certificate" not in old["summary"]
    assert (
        old["model_candidate"]["directional_response"]["schema"]
        == "camera_lens_directional_response_v1"
    )
    assert old["summary"]["status"] == new["summary"]["status"] == "PROPOSED"
    np.testing.assert_array_equal(new["arrays"]["accepted_rays"], old["arrays"]["accepted_rays"])
    for key in [
        "relative_illumination",
        "angular_weights",
        "irradiance_per_radiance_sr",
        "image_direction_cosines",
        "object_exit_direction_cosines",
    ]:
        np.testing.assert_allclose(new["arrays"][key], old["arrays"][key], atol=3e-12, rtol=3e-12)


def test_nonzero_departure_changes_actual_refracted_ray_directions(aspheric_result):
    request = small_request()
    request["surfaces"][1]["asphere"]["coefficients"][0] = 8e-4
    changed = lens_factory.characterize(request)
    before, after = aspheric_result["arrays"], changed["arrays"]
    admitted = before["accepted_rays"] & after["accepted_rays"]
    assert admitted.any()
    assert (
        np.max(
            np.abs(
                before["object_exit_direction_cosines"][admitted]
                - after["object_exit_direction_cosines"][admitted]
            )
        )
        > 0.005
    )
    assert changed["summary"]["request_sha256"] != aspheric_result["summary"]["request_sha256"]
    assert not after["failure_counts"][:, :, 3].any()


def test_dimensioned_scaling_preserves_aspheric_radiometry_and_angles(aspheric_result):
    request = small_request()
    scale = 2.0
    request["image_height_mm"] = [x * scale for x in request["image_height_mm"]]
    for surface in request["surfaces"]:
        for key in ["radius_mm", "thickness_mm", "semi_diameter_mm"]:
            if surface[key] is not None:
                surface[key] *= scale
        if surface["profile"] == "even_asphere":
            surface["asphere"]["coefficients"] = [
                coefficient / scale ** (order - 1)
                for order, coefficient in zip(
                    [4, 6, 8], surface["asphere"]["coefficients"], strict=True
                )
            ]
    scaled = lens_factory.characterize(request)
    np.testing.assert_array_equal(
        scaled["arrays"]["accepted_rays"], aspheric_result["arrays"]["accepted_rays"]
    )
    for key in ["irradiance_per_radiance_sr", "angular_weights", "object_exit_direction_cosines"]:
        np.testing.assert_allclose(
            scaled["arrays"][key], aspheric_result["arrays"][key], atol=3e-12, rtol=3e-12
        )


def test_reciprocal_sampling_plane_uses_full_interior_rear_sag_bound():
    request = small_request()
    rear = request["surfaces"][-1]
    rear.update(profile="even_asphere", radius_mm=-100.0, semi_diameter_mm=2.0)
    rear["asphere"] = {
        "schema": "camera_even_asphere_v1",
        "conic_constant": 0.0,
        "orders": [4, 6, 8],
        "coefficients": [0.096, -0.048, 0.006],
        "coefficient_units": ["mm^-3", "mm^-5", "mm^-7"],
    }
    normalized = lens_factory.validate_request(request)
    _, _, _, distance = lens_factory._sampling(normalized, 1)
    edge = optics_surfaces.sag(rear, 2.0)
    interior = optics_surfaces.sag(rear, math.sqrt(2))
    assert interior > max(0.0, edge)
    assert distance <= rear["thickness_mm"] - interior
    assert distance < rear["thickness_mm"] - max(0.0, edge) - 0.08


def test_asphere_uniqueness_failure_refuses_entire_candidate_not_just_ray_flux(monkeypatch):
    real_profile = optics_surfaces.profile

    def bad_profile(surface):
        profile = real_profile(surface)
        if surface["profile"] == "even_asphere":

            def ambiguous(*args, **kwargs):
                raise optics_surfaces.AsphereIntersectionError("Authored multiroot counterexample")

            profile.intersect = ambiguous
        return profile

    monkeypatch.setattr(optics_surfaces, "profile", bad_profile)
    request = small_request()
    request["sampling"].update(polar_rings=4, azimuth_samples=8)
    result = lens_factory.characterize(request)
    assert result["summary"]["status"] == "REFUSED"
    assert result["model_candidate"] is None
    assert result["arrays"]["failure_counts"][:, :, 3].any()
    assert not result["summary"]["convergence"]["observed_two_level_agreement"]


@pytest.mark.parametrize("version", [1, 2, 3])
def test_legacy_prescriptions_do_not_silently_admit_new_asphere_member(version):
    request = small_request(version)
    request["surfaces"][1] = small_request(4)["surfaces"][1]
    with pytest.raises(lens_factory.LensFactoryError):
        lens_factory.validate_request(request)
