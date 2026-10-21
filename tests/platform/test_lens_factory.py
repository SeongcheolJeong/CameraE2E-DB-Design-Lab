"""Synthetic prescriptions, analytic radiometry, and actual RayOptics tracing."""

import builtins
import json
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import lens_factory
from camera_platform.lens_factory import LensFactoryError, characterize, request_template


def _require_backend():
    pytest.importorskip("rayoptics", reason="optional factory dependency is not installed")


@pytest.fixture(scope="module")
def singlet():
    _require_backend()
    return characterize(request_template())


def _aperture(radius=2.0, distance=10.0):
    data = request_template()
    data.update(wavelength_nm=[450, 650], image_height_mm=[0, 0.1], field_radius=[0, 1])
    stop = deepcopy(data["surfaces"][0])
    stop.update(semi_diameter_mm=radius, thickness_mm=distance, refractive_index_after=[1, 1])
    data["surfaces"] = [stop]
    data["sampling"].update(polar_rings=16, azimuth_samples=16)
    return data


def test_template_is_explicit_synthetic_json_and_normalization_preserves_original():
    request = request_template()
    original = deepcopy(request)
    normalized = lens_factory.validate_request(request)
    assert request == original and normalized is not request
    assert all(s["id"].startswith("synthetic-") for s in normalized["surfaces"])
    assert normalized["wavelength_nm"] == [450, 500, 550, 600, 650]
    assert normalized["image_height_mm"][-1] == 3.6
    json.dumps(normalized, allow_nan=False)


def test_actual_singlet_proposes_spectral_lut_with_explicit_unknowns(singlet):
    summary, arrays, payload = singlet["summary"], singlet["arrays"], singlet["model_candidate"]
    assert summary["status"] == "PROPOSED", summary["convergence"]
    assert summary["physical_validation"] == "not_established"
    assert summary["approval_status"] == "unapproved"
    assert summary["uncertainty"] == {"kind": "unknown"}
    assert summary["image_circle_quality_validation"] == "not_established"
    assert summary["declared_sampling_coverage_diameter_mm"] == 7.2
    assert set(payload) == {
        "wavelength_nm",
        "field_radius",
        "theta_deg",
        "angular_weights",
        "relative_illumination",
    }
    assert np.array(payload["angular_weights"]).shape == (5, 3, 33)
    assert np.array(payload["relative_illumination"]).shape == (5, 3)
    assert 0 < payload["theta_deg"][-1] < 30
    np.testing.assert_allclose(arrays["angular_weights"].sum(axis=-1), 1, atol=1e-14)
    np.testing.assert_allclose(arrays["relative_illumination"][:, 0], 1, atol=1e-14)
    assert np.ptp(arrays["relative_illumination"][:, -1]) > 0.001
    assert np.any(arrays["failure_counts"][:, :, 0] > 0)
    assert not np.any(arrays["failure_counts"][:, :, 1:])
    assert np.all(arrays["irradiance_per_radiance_sr"] > 0)
    assert {"fresnel_or_coating_loss", "diffraction_psf_mtf", "physical_validation"} <= set(
        summary["unsupported"]
    )
    json.dumps(summary, allow_nan=False)


def test_ray_artifacts_have_unit_directions_and_explicit_visibility(singlet):
    arrays = singlet["arrays"]
    accepted = arrays["accepted_rays"]
    np.testing.assert_allclose(np.linalg.norm(arrays["image_direction_cosines"], axis=-1), 1)
    np.testing.assert_allclose(
        np.linalg.norm(arrays["object_exit_direction_cosines"][accepted], axis=-1), 1
    )
    assert np.all(arrays["object_exit_direction_cosines"][~accepted] == 0)
    assert np.all(arrays["image_direction_cosines"][:, :, 2] > 0)
    # The actual refracting singlet bends an admitted off-axis ray.
    image_reverse = -np.broadcast_to(arrays["image_direction_cosines"], (*accepted.shape, 3))
    assert (
        np.max(np.abs(arrays["object_exit_direction_cosines"][accepted] - image_reverse[accepted]))
        > 0.01
    )
    power = np.sum(accepted * arrays["quadrature_weights_sr"][None, :, :], axis=-1)
    np.testing.assert_allclose(power, arrays["irradiance_per_radiance_sr"], rtol=1e-13)
    attempted = accepted.shape[-1]
    np.testing.assert_array_equal(
        accepted.sum(axis=-1) + arrays["failure_counts"].sum(axis=-1), attempted
    )


def test_clear_aperture_matches_analytic_irradiance_and_reciprocity():
    _require_backend()
    radius, distance = 2.0, 10.0
    result = characterize(_aperture(radius, distance))
    arrays = result["arrays"]
    # Exact integral of cos(theta) dOmega for an on-axis circular plane aperture.
    expected = np.pi * radius**2 / (distance**2 + radius**2)
    np.testing.assert_allclose(arrays["irradiance_per_radiance_sr"][:, 0], expected, rtol=1e-7)
    assert result["summary"]["status"] == "PROPOSED"
    assert arrays["accepted_rays"].all()
    np.testing.assert_allclose(
        arrays["object_exit_direction_cosines"],
        -np.broadcast_to(
            arrays["image_direction_cosines"], arrays["object_exit_direction_cosines"].shape
        ),
        atol=1e-14,
    )
    assert np.all(arrays["relative_illumination"][:, 1] < 1)


def test_uniform_geometric_scaling_preserves_angles_and_relative_irradiance(singlet):
    request = request_template()
    request["image_height_mm"] = [x * 2 for x in request["image_height_mm"]]
    for surface in request["surfaces"]:
        for key in ("radius_mm", "thickness_mm", "semi_diameter_mm"):
            if surface[key] is not None:
                surface[key] *= 2
    scaled = characterize(request)
    for name in (
        "theta_deg",
        "relative_illumination",
        "angular_weights",
        "irradiance_per_radiance_sr",
    ):
        np.testing.assert_allclose(scaled["arrays"][name], singlet["arrays"][name], atol=1e-13)


def test_execution_is_deterministic(singlet):
    repeated = characterize(request_template())
    assert repeated["summary"] == singlet["summary"]
    for name in repeated["arrays"]:
        np.testing.assert_array_equal(repeated["arrays"][name], singlet["arrays"][name])


def test_tightened_observed_agreement_gate_refuses_candidate_and_preserves_diagnostics():
    _require_backend()
    request = request_template()
    request["sampling"].update(ri_tolerance_absolute=1e-6, angular_cdf_tolerance_absolute=1e-6)
    result = characterize(request)
    assert result["model_candidate"] is None
    assert result["summary"]["status"] == "REFUSED"
    assert result["summary"]["convergence"]["observed_two_level_agreement"] is False
    assert result["arrays"]["accepted_rays"].any()
    assert result["summary"]["uncertainty"] == {"kind": "unknown"}


def test_unsampled_tiny_stop_does_not_generate_a_reusable_relative_illumination():
    _require_backend()
    request = request_template()
    request["surfaces"][0]["semi_diameter_mm"] = 1e-4
    result = characterize(request)
    assert result["model_candidate"] is None
    assert result["summary"]["status"] == "REFUSED"
    assert not result["arrays"]["accepted_rays"].any()
    assert result["summary"]["relative_illumination"] == [[None] * 3] * 5


@pytest.mark.parametrize(
    "mutation",
    [
        "units",
        "nan",
        "duplicate_surface",
        "missing_stop",
        "powered_stop",
        "asphere",
        "missing_aperture",
        "index_shape",
        "nonair_sensor",
        "negative_gap",
        "intersecting",
        "sensor_in_rear",
        "incorrect_radius",
        "resource_limit",
        "bool_sample",
        "unknown_transmission",
    ],
)
def test_invalid_or_unsupported_prescriptions_fail_before_tracing(mutation):
    request = request_template()
    if mutation == "units":
        request["length_unit"] = "m"
    elif mutation == "nan":
        request["surfaces"][0]["semi_diameter_mm"] = float("nan")
    elif mutation == "duplicate_surface":
        request["surfaces"][1]["id"] = request["surfaces"][0]["id"]
    elif mutation == "missing_stop":
        request["surfaces"][0]["is_stop"] = False
    elif mutation == "powered_stop":
        request["surfaces"][0]["refractive_index_after"] = [1.5] * 5
    elif mutation == "asphere":
        request["surfaces"][1]["conic"] = -1
    elif mutation == "missing_aperture":
        request["surfaces"][1].pop("semi_diameter_mm")
    elif mutation == "index_shape":
        request["surfaces"][1]["refractive_index_after"] = [1.5]
    elif mutation == "nonair_sensor":
        request["surfaces"][-1]["refractive_index_after"] = [1.5] * 5
    elif mutation == "negative_gap":
        request["surfaces"][1]["thickness_mm"] = -1
    elif mutation == "intersecting":
        request["surfaces"][1]["thickness_mm"] = 0.1
    elif mutation == "sensor_in_rear":
        request["surfaces"][-1].update(radius_mm=20, thickness_mm=0.01)
    elif mutation == "incorrect_radius":
        request["field_radius"][1] = 0.4
    elif mutation == "resource_limit":
        request["sampling"].update(polar_rings=128, azimuth_samples=256)
    elif mutation == "bool_sample":
        request["sampling"]["polar_rings"] = True
    else:
        request["illumination"]["transmission"] = "real_coatings"
    with pytest.raises(LensFactoryError) as error:
        lens_factory.validate_request(request)
    assert error.value.issues[0]["path"] and error.value.issues[0]["code"]


def test_missing_backend_has_explicit_capability_failure(monkeypatch):
    original = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name.startswith("rayoptics"):
            raise ImportError("fixture missing optional solver")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(LensFactoryError) as error:
        characterize(request_template())
    assert error.value.issues[0]["code"] == "MISSING_BACKEND"


def test_compute_deadline_is_enforced(monkeypatch):
    _require_backend()
    monkeypatch.setattr(lens_factory, "MAX_SECONDS", -1)
    with pytest.raises(LensFactoryError) as error:
        characterize(request_template())
    assert error.value.issues[0]["code"] == "TIME_LIMIT"
