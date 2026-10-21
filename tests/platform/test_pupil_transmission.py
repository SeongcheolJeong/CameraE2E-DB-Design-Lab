"""Independent stop radiometry, physical hit coordinates, and complex-pupil DFT."""

import json
import math
import time
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import lens_factory as rays
from camera_platform import prescription_psf as psf
from camera_platform import pupil_transmission as masks
from camera_platform.registry import digest


def aperture(version=2):
    request = rays.request_template(version)
    request.update(wavelength_nm=[450, 650], image_height_mm=[0, 0.1], field_radius=[0, 1])
    stop = deepcopy(request["surfaces"][0])
    stop.update(semi_diameter_mm=2, thickness_mm=10, refractive_index_after=[1, 1])
    request["surfaces"] = [stop]
    request["sampling"].update(polar_rings=16, azimuth_samples=16)
    if version == 2:
        request["stop_transmission"] = masks.uniform_transmission(request)
    return request


def small_psf(version=2):
    request = psf.request_template(version)
    request.update(field_x_mm=[0, 0.02], field_y_mm=[0, 0.02])
    return request


def set_uniform(request, value):
    prescription = request.get("prescription", request)
    prescription["stop_transmission"] = masks.uniform_transmission(prescription, value)


def test_exact_contract_normalization_unicode_hash_and_spectral_linear_power():
    request = aperture()
    spec = request["stop_transmission"]
    spec.update(normalized_radius=[0, 0.5, 1], power_transmittance=[[1, 0.2, 0], [0, 0.6, 1]])
    spec["source"]["description"] = "합성 조리개 투과율"
    original = deepcopy(spec)
    normalized = masks.validate_transmission(spec)
    assert spec == original
    assert masks.digest(normalized) == digest(normalized)
    np.testing.assert_allclose(
        masks.sample_power(normalized, 450, [0, 0.25, 0.75, 1]), [1, 0.6, 0.1, 0]
    )
    np.testing.assert_allclose(
        masks.sample_power(normalized, 650, [0, 0.25, 0.75, 1]), [0, 0.3, 0.8, 1]
    )
    with pytest.raises(ValueError, match="exact requested wavelength"):
        masks.sample_power(normalized, 550, 0.5)


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "camera_stop_transmission_v2"},
        {"interpolation": "linear_amplitude"},
        {"normalized_radius": [0.1, 1]},
        {"normalized_radius": [0, 0.5]},
        {"normalized_radius": [0, 0, 1]},
        {"normalized_radius": [False, 1]},
        {"power_transmittance": [[-0.1, 1], [0, 1]]},
        {"power_transmittance": [[0, 1.01], [0, 1]]},
        {"power_transmittance": [[True, 1], [0, 1]]},
        {"power_transmittance": [[float("nan"), 1], [0, 1]]},
        {"power_transmittance": [[0, 1]]},
        {"phase_rad": 0},
        {"source": {"kind": "measured", "description": " "}},
    ],
)
def test_unsupported_masks_refused(change):
    spec = aperture()["stop_transmission"]
    spec.update(change)
    with pytest.raises(ValueError):
        masks.validate_transmission(spec)


@pytest.mark.parametrize(
    "mutation", ["stop", "wavelength", "powered", "mixed", "legacy_mask", "tolerance"]
)
def test_binding_and_request_versions_are_explicit(mutation):
    request = small_psf()
    prescription = request["prescription"]
    if mutation == "stop":
        prescription["stop_transmission"]["stop_surface_id"] = "synthetic-front"
    elif mutation == "wavelength":
        prescription["stop_transmission"]["wavelength_nm"] = [500, 601]
    elif mutation == "powered":
        prescription["surfaces"][0]["refractive_index_after"] = [1.5, 1.5]
    elif mutation == "mixed":
        request["schema"] = "prescription_field_psf_v1"
    elif mutation == "legacy_mask":
        prescription["schema"] = "lens_prescription_v1"
    else:
        prescription["sampling"]["irradiance_tolerance_relative"] = 0.031
    with pytest.raises(ValueError):
        psf.validate_request(request)


@pytest.mark.parametrize("point", [[2.0001, 0, 0], [0, 0, 0.01], [0, float("inf"), 0]])
def test_wrong_stop_intersection_is_not_clipped_or_extrapolated(point):
    with pytest.raises(ValueError):
        masks.power_at_stop(aperture()["stop_transmission"], 450, point, 2)


def test_uniform_quarter_absolute_radiometry_and_unit_mask_legacy_identity():
    legacy = rays.characterize(aperture(1))
    unit = rays.characterize(aperture())
    request = aperture()
    set_uniform(request, 0.25)
    request["stop_transmission"]["source"]["description"] = "중성 25% 투과"
    quarter = rays.characterize(request)
    for key, value in legacy["arrays"].items():
        np.testing.assert_array_equal(unit["arrays"][key], value)
    for result in (unit, quarter):
        assert result["summary"]["status"] == "PROPOSED"
        radiometry = result["model_candidate"]["radiometry"]
        assert radiometry["prescription_sha256"] == digest(result["summary"]["normalized_request"])
        assert radiometry["stop_transmission_sha256"] == digest(radiometry["stop_transmission"])
        assert result["model_candidate"]["field_reference_radius_mm"] == 0.1
    exact = 0.25 * math.pi * 4 / 104
    np.testing.assert_allclose(
        quarter["arrays"]["irradiance_per_radiance_sr"][:, 0], exact, rtol=1e-7
    )
    np.testing.assert_allclose(
        quarter["arrays"]["irradiance_per_radiance_sr"],
        0.25 * unit["arrays"]["irradiance_per_radiance_sr"],
        rtol=1e-14,
    )
    for key in ("relative_illumination", "angular_weights"):
        np.testing.assert_allclose(quarter["arrays"][key], unit["arrays"][key], atol=1e-14)
    assert "radiometry" not in legacy["model_candidate"]


def plane_integral(radius, distance, knots, values):
    """Independent analytic integral 2*pi*d² int T(r) r/(d²+r²)² dr."""
    total = 0.0
    for lo, hi, tlo, thi in zip(knots[:-1], knots[1:], values[:-1], values[1:], strict=True):
        lo, hi = lo * radius, hi * radius
        slope = (thi - tlo) / (hi - lo)
        intercept = tlo - slope * lo

        def primitive(r, intercept=intercept, slope=slope):
            return -math.pi * distance**2 * intercept / (distance**2 + r**2) + math.pi * slope * (
                distance * math.atan(r / distance) - distance**2 * r / (distance**2 + r**2)
            )

        total += primitive(hi) - primitive(lo)
    return total


def test_spectral_nonuniform_ray_power_matches_independent_piecewise_integral():
    request = aperture()
    mask = request["stop_transmission"]
    mask.update(
        normalized_radius=[0, 0.3, 0.75, 1],
        power_transmittance=[[1, 0.1, 0.8, 0.2], [0.3, 0.4, 0.1, 0.8]],
    )
    result = rays.characterize(request)
    assert result["summary"]["status"] == "PROPOSED"
    expected = [
        plane_integral(2, 10, mask["normalized_radius"], row) for row in mask["power_transmittance"]
    ]
    np.testing.assert_allclose(
        result["arrays"]["irradiance_per_radiance_sr"][:, 0], expected, rtol=4e-5
    )
    clear = rays.characterize(aperture())
    assert (
        np.max(abs(clear["arrays"]["angular_weights"] - result["arrays"]["angular_weights"]))
        > 0.005
    )


def test_absolute_convergence_is_an_admission_gate_even_when_ri_and_angles_agree(monkeypatch):
    actual = rays._pass

    def biased(*args, **kwargs):
        result = actual(*args, **kwargs)
        if args[4] == 1:
            result["irradiance_per_radiance_sr"] *= 0.9
        return result

    monkeypatch.setattr(rays, "_pass", biased)
    result = rays.characterize(aperture())
    assert result["model_candidate"] is None
    delta = np.asarray(result["summary"]["convergence"]["irradiance_integral_relative_change"])
    assert np.all((delta > 0.099) & (delta < 0.101))
    assert result["summary"]["convergence"]["observed_two_level_agreement"] is False


@pytest.fixture(scope="module")
def uniform_psfs():
    legacy = psf.characterize(small_psf(1))
    unit = psf.characterize(small_psf())
    request = small_psf()
    set_uniform(request, 0.25)
    quarter = psf.characterize(request)
    return legacy, unit, quarter


def test_actual_uniform_quarter_psf_is_conditional_and_preserves_legacy(uniform_psfs):
    legacy, unit, quarter = uniform_psfs
    for result in uniform_psfs:
        assert result["summary"]["status"] == "PROPOSED"
        assert result["summary"]["physical_validation"] == "not_established"
    for key, value in legacy["arrays"].items():
        np.testing.assert_array_equal(unit["arrays"][key], value)
    for key in ("psf_kernel", "psf_bin_energy", "retained_energy_fraction"):
        np.testing.assert_allclose(quarter["arrays"][key], unit["arrays"][key], atol=1e-15)
    np.testing.assert_allclose(quarter["arrays"]["fine_pupil_transmitted_power_fraction"], 0.25)
    assert (
        quarter["summary"]["pupil_energy_normalization"]
        == "conditional_on_admitted_transmitted_power"
    )
    assert quarter["summary"]["prescription_sha256"] == digest(
        quarter["summary"]["normalized_request"]["prescription"]
    )


def test_nonuniform_complex_pupil_matches_independent_dft_and_energy():
    request = small_psf()
    request["kernel_size"] = 21
    n, distance, radius, wavelength, subpixels = 64, 20.0, 0.5, 500.0, 7
    dx = 2.4 * radius / n
    coordinate = (np.arange(n) - n // 2) * dx
    x, y = np.meshgrid(coordinate, coordinate)
    admitted = x * x + y * y <= radius**2
    rho = np.hypot(x, y) / radius
    transmission = np.where(admitted, 0.1 + 0.9 * (1 - np.minimum(rho, 1)), 0)
    opd = -x * 0.003 / distance * 1e6
    wf = {
        "admitted": admitted,
        "stop_power_transmittance": transmission,
        "opd_nm": opd,
        "exit_pupil_radius_mm": radius,
        "reference_sphere_radius_mm": distance,
    }
    actual = psf._propagate(request, wf, wavelength, subpixels)
    count = request["kernel_size"] * subpixels
    receive_pitch = request["pixel_pitch_um"] / subpixels
    receive = (np.arange(count) - count // 2) * receive_pitch
    basis = np.exp(-2j * np.pi * np.outer(receive, coordinate) / (wavelength * 1e-3 * distance))
    complex_pupil = np.sqrt(transmission) * np.exp(-2j * np.pi * opd / wavelength)
    field = basis @ complex_pupil @ basis.T
    density = (
        abs(field) ** 2
        * (dx * receive_pitch / (wavelength * 1e-3 * distance)) ** 2
        / transmission.sum()
    )
    expected = density.reshape(21, subpixels, 21, subpixels).sum((1, 3))
    np.testing.assert_allclose(actual, expected, rtol=2e-13, atol=1e-16)
    assert 0.95 < actual.sum() <= 1
    # Full native DFT Parseval verifies the independent transmitted energy, and
    # a T-as-amplitude implementation gives a meaningfully different PSF.
    fft = np.fft.fft2(complex_pupil, norm="ortho")
    assert np.sum(abs(fft) ** 2) == pytest.approx(transmission.sum(), rel=2e-15)
    wrong_field = basis @ (transmission * np.exp(-2j * np.pi * opd / wavelength)) @ basis.T
    wrong = abs(wrong_field) ** 2
    assert np.sum(abs(wrong / wrong.sum() - density / density.sum())) > 0.05


def test_powered_surfaces_use_physical_stop_hit_in_both_trace_directions():
    request = small_psf()
    prescription = request["prescription"]
    stop, front, rear = deepcopy(prescription["surfaces"])
    front["thickness_mm"] = 1.5
    stop.update(thickness_mm=1.5, refractive_index_after=front["refractive_index_after"])
    prescription["surfaces"] = [front, stop, rear]
    prescription["stop_transmission"].update(power_transmittance=[[1, 0.1], [1, 0.1]])
    normalized = psf.validate_request(request)
    model = psf._model(normalized, 0)
    wf = psf._wavefront(normalized, model, 500, 0.02, 0, 32, time.monotonic() + 20)
    good = wf["admitted"]
    actual_radius = np.linalg.norm(wf["stop_intersection_xy_mm"], axis=-1) / 0.5
    np.testing.assert_allclose(wf["stop_power_transmittance"][good], 1 - 0.9 * actual_radius[good])
    axis = (np.arange(32) - 16) * 2.4 / 32
    nominal = np.hypot(*np.meshgrid(axis, axis))
    assert np.max(abs(nominal[good] - actual_radius[good])) > 1e-5
    data = rays.validate_request(prescription)
    data["sampling"].update(polar_rings=8, azimuth_samples=8)
    backend = rays._backend()
    paths = rays._paths(data, backend)
    result = rays._pass(data, backend, paths, np.linspace(0, 10, 33), 1, time.monotonic() + 20)
    good = result["accepted_rays"]
    rho = np.linalg.norm(result["stop_intersection_xy_mm"], axis=-1) / 0.5
    np.testing.assert_allclose(result["stop_power_transmittance"][good], 1 - 0.9 * rho[good])
    # Reconstruct independently: image is index 0, rear is 1, physical stop is 2.
    n = np.flatnonzero(good[0, 0])[0]
    direction = -result["image_direction_cosines"][0, n]
    ray, _, _ = backend[2](
        iter(paths[0]),
        np.zeros(3),
        direction,
        500,
        check_apertures=True,
        intersect_obj=False,
        pt_inside_fuzz=0.0,
    )
    np.testing.assert_allclose(result["stop_intersection_xy_mm"][0, 0, n], ray[2][0][:2])
    assert np.linalg.norm(ray[1][0][:2] - ray[2][0][:2]) > 1e-4


def test_zero_mask_withholds_both_candidates_with_finite_diagnostics():
    ray_request, psf_request = aperture(), small_psf()
    set_uniform(ray_request, 0)
    set_uniform(psf_request, 0)
    for result in (rays.characterize(ray_request), psf.characterize(psf_request)):
        assert result["summary"]["status"] == "REFUSED"
        assert result["model_candidate"] is None
        json.dumps(result["summary"], allow_nan=False)
        assert all(np.isfinite(array).all() for array in result["arrays"].values())


def test_mask_does_not_bypass_existing_mapping_gate():
    request = small_psf()
    request["maximum_exit_pupil_mapping_fraction"] = 1e-6
    set_uniform(request, 0.25)
    result = psf.characterize(request)
    assert result["summary"]["approximation_domain_checks"] is False
    assert result["model_candidate"] is None
