"""Physical-path and diffraction evidence for the centered even-asphere adapter."""

import hashlib
import math
import time
from copy import deepcopy

import numpy as np
import pytest
from scipy.optimize import brentq

from camera_platform import lens_factory, optics_surfaces, prescription_psf
from camera_platform.pupil_transmission import digest
from camera_platform.spatial import validate_psf


def _wavefront(request, x=0.3, y=0.4, count=32):
    return prescription_psf._wavefront(
        request, prescription_psf._model(request, 0), 500, x, y, count,
        time.monotonic() + 30,
    )


@pytest.fixture(scope="module")
def characterized():
    request = prescription_psf.request_template(4)
    return request, prescription_psf.characterize(request)


def test_authored_v4_qualifies_without_relaxing_the_old_acceptance_gates(characterized):
    request, result = characterized
    previous = prescription_psf.request_template(3)
    for name in (
        "max_kernel_l1_change", "minimum_retained_energy",
        "maximum_exit_pupil_mapping_fraction", "maximum_ray_cone_deg",
        "pupil_samples", "kernel_size", "subpixel_samples",
    ):
        assert request[name] == previous[name]
    assert request["prescription"]["sampling"] == previous["prescription"]["sampling"]
    summary = result["summary"]
    assert summary["status"] == "PROPOSED"
    assert summary["numerical_agreement"] and summary["approximation_domain_checks"]
    assert summary["schema"] == "prescription_field_psf_characterization_v4"
    assert summary["factory_id"] == summary["method_and_version"] == (
        prescription_psf.ASPHERE_FACTORY_ID
    )
    assert summary["physical_validation"] == "not_established"
    assert summary["uncertainty"] == {"kind": "unknown"}
    assert "even-asphere" in summary["assumptions"][0]
    assert "Scalar Fraunhofer" in " ".join(summary["assumptions"])
    assert "not prediction-error bounds" in " ".join(summary["limitations"])
    assert summary["surface_geometry"] == optics_surfaces.certify_prescription(
        request["prescription"]["surfaces"]
    )
    candidate = validate_psf(result["model_candidate"])
    assert candidate["schema"] == "field_intensity_psf_v1"
    np.testing.assert_allclose(np.sum(candidate["values"], axis=(-1, -2)), 1, atol=1e-14)
    assert result["arrays"]["fine_opd_nm"].shape == (2, 3, 3, 64, 64)
    assert summary["maximum_neighbor_opd_waves_observed"] <= 0.25


def test_identical_physical_v4_prescription_qualifies_reciprocal_radiometry(characterized):
    request, psf = characterized
    original = deepcopy(request)
    radiometry = lens_factory.characterize(request["prescription"])
    assert request == original
    assert radiometry["summary"]["status"] == "PROPOSED"
    assert radiometry["summary"]["normalized_request"] == (
        psf["summary"]["normalized_request"]["prescription"]
    )


def test_nonzero_asphere_changes_actual_wavefront_and_psf_without_changing_paraxial_power():
    request = prescription_psf.request_template(4)
    zero = deepcopy(request)
    zero["prescription"]["surfaces"][1]["asphere"]["coefficients"] = [0.0, 0.0, 0.0]
    changed, original = _wavefront(request), _wavefront(zero)
    np.testing.assert_array_equal(changed["admitted"], original["admitted"])
    assert np.max(np.abs(changed["opd_nm"] - original["opd_nm"])) > 20
    a = prescription_psf._propagate(request, changed, 500, 7)
    b = prescription_psf._propagate(zero, original, 500, 7)
    assert np.abs(a / a.sum() - b / b.sum()).sum() > 0.02
    assert prescription_psf._model(request, 0)["ar"]["parax_data"].fod.efl == (
        prescription_psf._model(zero, 0)["ar"]["parax_data"].fod.efl
    )


def _independent_sag_and_gradient(surface, x, y):
    """Evaluate the authored equation directly, without shared profile helpers."""
    if surface["profile"] == "plane":
        return 0.0, np.zeros(2)
    c = 1 / surface["radius_mm"]
    k, coefficients = 0.0, [0.0, 0.0, 0.0]
    if surface["profile"] == "even_asphere":
        k = surface["asphere"]["conic_constant"]
        coefficients = surface["asphere"]["coefficients"]
    r2 = x * x + y * y
    radical = math.sqrt(1 - (1 + k) * c * c * r2)
    value = c * r2 / (1 + radical)
    gradient_scale = c / radical
    for coefficient, exponent in zip(coefficients, (2, 3, 4), strict=True):
        value += coefficient * r2**exponent
        gradient_scale += 2 * exponent * coefficient * r2**(exponent - 1)
    return value, gradient_scale * np.array([x, y])


def test_asphere_rays_match_independent_bracketed_sag_and_vector_snell_and_opl():
    from rayoptics.raytr import trace, waveabr
    from rayoptics.raytr.opticalspec import Field

    request = prescription_psf.request_template(4)
    model = prescription_psf._model(request, 0)
    field = Field(y=0.1, fov=model.optical_spec["fov"])
    sphere, chief = trace.setup_pupil_coords(model, field, 500, 0, image_pt=[0, 0.04])
    surfaces = request["prescription"]["surfaces"]
    center = np.array([0, 0.04, surfaces[-1]["thickness_mm"]])

    def physical_path(ray):
        point, direction = ray[-2][:2]
        displacement = point - center
        distance = -direction @ displacement - np.sqrt(
            (direction @ displacement) ** 2 - displacement @ displacement + sphere[2] ** 2
        )
        return ray[0][1] @ ray[1][0] + sum(
            surface["refractive_index_after"][0] * ray[1 + i][2]
            for i, surface in enumerate(surfaces[:-1])
        ) + distance

    for pupil in ([0, 0], [0.4, 0.3], [0, 0.8], [-0.4, 0.3]):
        traced = trace.trace_safe(model, pupil, field, 500, None, "full", check_apertures=True)
        assert traced.err is None
        ray = traced.pkg[0]
        point, direction = (np.array(v) for v in ray[1][:2])
        for index, surface in enumerate(surfaces[1:], start=1):
            thickness = surfaces[index - 1]["thickness_mm"]
            point = point - [0, 0, thickness]

            def residual(distance, point=point, direction=direction, surface=surface):
                hit = point + distance * direction
                return hit[2] - _independent_sag_and_gradient(surface, *hit[:2])[0]

            distance = brentq(residual, 0, thickness + 0.1, xtol=1e-14)
            point = point + distance * direction
            _, gradient = _independent_sag_and_gradient(surface, *point[:2])
            normal = np.r_[-gradient, 1.0]
            normal /= np.linalg.norm(normal)
            eta = (surfaces[index - 1]["refractive_index_after"][0]
                   / surface["refractive_index_after"][0])
            tangent = eta * (direction - (direction @ normal) * normal)
            direction = tangent + math.sqrt(1 - tangent @ tangent) * normal
            np.testing.assert_allclose(point, ray[index + 1][0], rtol=0, atol=2e-10)
            np.testing.assert_allclose(direction, ray[index + 1][1], rtol=0, atol=2e-11)
        expected_nm = (physical_path(chief[0].ray) - physical_path(ray)) * 1e6
        actual_nm = waveabr.wave_abr_full_calc(
            model["ar"]["parax_data"].fod, field, 500, 0, traced.pkg, chief, sphere
        ) * 1e6
        assert actual_nm == pytest.approx(expected_nm, abs=0.002)


def test_zero_even_asphere_matches_the_spherical_v3_limit():
    request = prescription_psf.request_template(4)
    request["prescription"]["surfaces"][1]["asphere"].update(
        conic_constant=0.0, coefficients=[0.0, 0.0, 0.0],
    )
    aspheric = _wavefront(request)
    spherical = _wavefront(prescription_psf.request_template(3))
    np.testing.assert_array_equal(aspheric["admitted"], spherical["admitted"])
    np.testing.assert_allclose(aspheric["opd_nm"], spherical["opd_nm"], rtol=0, atol=2e-6)
    np.testing.assert_allclose(
        aspheric["ray_sensor_xy_mm"], spherical["ray_sensor_xy_mm"], rtol=0, atol=2e-10,
    )


def test_uncertifiable_surface_intersection_is_not_hidden_as_vignetting(monkeypatch):
    request = prescription_psf.request_template(4)
    model = prescription_psf._model(request, 0)

    def refuse(*args, **kwargs):
        raise optics_surfaces.AsphereIntersectionError("test uniqueness refusal")

    monkeypatch.setattr(type(model.seq_model.ifcs[2].profile), "intersect", refuse)
    with pytest.raises(optics_surfaces.AsphereIntersectionError, match="uniqueness refusal"):
        prescription_psf._wavefront(request, model, 500, 0, 0, 16, time.monotonic() + 20)


@pytest.mark.parametrize("corruption", ["incomplete", "backwards", "point", "direction", "normal"])
def test_invalid_chief_path_is_refused_before_it_becomes_the_opd_reference(monkeypatch, corruption):
    from rayoptics.raytr import trace

    request = prescription_psf.request_template(4)
    model = prescription_psf._model(request, 0)
    original = trace.setup_pupil_coords

    def corrupt_chief(*args, **kwargs):
        reference, chief = original(*args, **kwargs)
        if corruption == "incomplete":
            chief[0].ray.pop()
        elif corruption == "backwards":
            chief[0].ray[1][2] = -0.01
        else:
            component = {"point": 0, "direction": 1, "normal": 3}[corruption]
            chief[0].ray[1][component][0] = np.nan
        return reference, chief

    monkeypatch.setattr(trace, "setup_pupil_coords", corrupt_chief)
    with pytest.raises(ValueError, match="chief.*prescription ray"):
        prescription_psf._wavefront(request, model, 500, 0, 0, 16, time.monotonic() + 20)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_asphere_is_not_admitted_through_legacy_request_versions(version):
    request = prescription_psf.request_template(4)
    request["schema"] = f"prescription_field_psf_v{version}"
    request["prescription"]["schema"] = f"lens_prescription_v{version}"
    with pytest.raises(ValueError):
        prescription_psf.validate_request(request)


@pytest.mark.parametrize(
    "version,expected",
    [
        (1, "96e097a9bd1d6f4f3f0ffacd23723c178c1f657e7311f8e69b32d62bbe79488b"),
        (2, "998ff0079a98669b7a03d8995c5359037dad5ee32ef63dd37726b992649cf290"),
        (3, "e83fb62d81d1cf722a34842ca728d24de7a6bdf69e765aa5543925a1a42c2a71"),
    ],
)
def test_v1_v2_v3_entire_psf_results_preserve_pre_asphere_bytes(version, expected):
    # Measured before the v4 implementation on RayOptics 0.9.8/Prysm 0.21.1.
    request = prescription_psf.request_template(version)
    request.update(field_x_mm=[0, 0.02], field_y_mm=[0, 0.02])
    result = prescription_psf.characterize(request)
    fingerprint = {
        "summary": result["summary"], "model_candidate": result["model_candidate"],
        "arrays": {
            name: {"shape": list(value.shape), "dtype": str(value.dtype),
                   "sha256": hashlib.sha256(value.tobytes()).hexdigest()}
            for name, value in result["arrays"].items()
        },
    }
    assert digest(fingerprint) == expected
