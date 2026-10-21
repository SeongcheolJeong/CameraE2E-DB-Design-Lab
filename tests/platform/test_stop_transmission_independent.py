"""Independent stop radiometry, geometric intersections and Hankel diffraction.

These references do not reuse the production ray quadrature or propagation
transform: continuous radial integration, straight-line geometry and a Bessel
integral establish separate checks of the authored passive-mask approximation.
They do not establish physical-camera accuracy or general amplitude transport.
"""

from copy import deepcopy

import numpy as np
from numpy.polynomial.legendre import leggauss
from scipy.integrate import quad
from scipy.special import j0

from camera_platform import lens_factory, prescription_psf, pupil_transmission


def _plane_stop_request(*, middle_stop=False):
    request = lens_factory.request_template(version=2)
    request.update(wavelength_nm=[450, 650], field_radius=[0, 1], image_height_mm=[0, 0.1])
    stop = deepcopy(request["surfaces"][0])
    stop.update(thickness_mm=10, semi_diameter_mm=2, refractive_index_after=[1, 1])
    request["surfaces"] = [stop]
    if middle_stop:
        front = deepcopy(stop)
        front.update(id="clear-before", is_stop=False, semi_diameter_mm=5, thickness_mm=2)
        stop["thickness_mm"] = 3
        rear = deepcopy(front)
        rear.update(id="clear-after", thickness_mm=10)
        request["surfaces"] = [front, stop, rear]
    request["sampling"].update(polar_rings=16, azimuth_samples=16)
    request["stop_transmission"] = pupil_transmission.uniform_transmission(request)
    request["stop_transmission"].update(
        normalized_radius=[0, 0.35, 1],
        power_transmittance=[[1, 0.8, 0.2], [0.4, 0.25, 0.1]],
    )
    return request


def test_spectral_stop_irradiance_matches_continuous_radial_integral():
    request = _plane_stop_request()
    result = lens_factory.characterize(request)
    radius, distance = 2.0, 10.0
    knots = np.asarray(request["stop_transmission"]["normalized_radius"]) * radius
    expected = []
    # E/L = integral T*cos(theta) dOmega. A plane annulus has the exact
    # measure 2*pi*d**2*r/(d**2+r**2)**2 dr; no ray count enters this reference.
    for values in request["stop_transmission"]["power_transmittance"]:
        integral = 0.0
        for left, right, low, high in zip(
            knots[:-1], knots[1:], values[:-1], values[1:], strict=True
        ):
            slope = (high - low) / (right - left)
            integral += quad(
                lambda r, low=low, slope=slope, left=left: (
                    2
                    * np.pi
                    * distance**2
                    * (low + slope * (r - left))
                    * r
                    / (distance**2 + r**2) ** 2
                ),
                left,
                right,
                epsabs=1e-13,
                epsrel=1e-13,
            )[0]
        expected.append(integral)
    assert result["summary"]["status"] == "PROPOSED"
    np.testing.assert_allclose(
        result["arrays"]["irradiance_per_radiance_sr"][:, 0],
        expected,
        rtol=1e-5,
        atol=0,
    )
    # Neither power transmission squared nor an unweighted pupil mean gives
    # this spectral result. The downstream coefficient retains steradians.
    assert expected[0] > 2 * expected[1] > 0


def test_reciprocal_middle_stop_intersection_matches_independent_line_geometry():
    result = lens_factory.characterize(_plane_stop_request(middle_stop=True))
    arrays = result["arrays"]
    admitted = arrays["accepted_rays"]
    plane = arrays["plane_sample_xy_mm"]
    height = np.asarray([0.0, 0.1])
    # Sensor-to-rear plane distance is 10 mm; sensor-to-stop is 13 mm.
    # All surfaces are unpowered, so extrapolate the sampled line to the stop.
    expected = plane.copy()
    expected[:, :, 0] = height[:, None] + (plane[:, :, 0] - height[:, None]) * 1.3
    expected[:, :, 1] = plane[:, :, 1] * 1.3
    expected = np.broadcast_to(expected, arrays["stop_intersection_xy_mm"].shape)
    assert admitted.sum() > 100
    np.testing.assert_allclose(
        arrays["stop_intersection_xy_mm"][admitted],
        expected[admitted],
        atol=2e-14,
        rtol=0,
    )
    # A test of a first or rear stop alone would not expose every reverse-path
    # index error. The middle stop differs substantially from the rear plane.
    rear = np.broadcast_to(plane, expected.shape)
    assert np.max(np.abs(expected[admitted] - rear[admitted])) > 0.1


def test_radial_power_mask_psf_matches_continuous_hankel_and_pixel_integrals():
    request = prescription_psf.request_template(version=2)
    samples, radius_mm, distance_mm, wavelength_nm = 256, 0.5, 20.0, 500
    coordinate = (np.arange(samples) - samples // 2) * 2.4 * radius_mm / samples
    x, y = np.meshgrid(coordinate, coordinate)
    radius = np.hypot(x, y)
    admitted = radius <= radius_mm
    transmission = np.where(admitted, 1 - 0.8 * radius / radius_mm, 0)
    wavefront = {
        "admitted": admitted,
        "opd_nm": np.zeros((samples, samples)),
        "exit_pupil_radius_mm": radius_mm,
        "reference_sphere_radius_mm": distance_mm,
        "stop_power_transmittance": transmission,
    }
    actual = prescription_psf._propagate(request, wavefront, wavelength_nm, 7)
    radial_nodes, radial_weights = leggauss(400)
    radial_coordinate = (radial_nodes + 1) * radius_mm / 2
    radial_weights = radial_weights * radius_mm / 2
    power = 1 - 0.8 * radial_coordinate / radius_mm
    transmitted_energy = 2 * np.pi * np.sum(power * radial_coordinate * radial_weights)
    wrong_energy = 2 * np.pi * np.sum(power**2 * radial_coordinate * radial_weights)
    pixel_nodes, pixel_weights = leggauss(16)
    pixel_coordinate = pixel_nodes * 1.5  # Three-micrometre receiving pixels.
    pixel_area = pixel_weights[:, None] * pixel_weights[None, :] * 1.5**2
    propagation_scale = wavelength_nm * 1e-6 * distance_mm  # lambda*d in mm**2.
    selected = [(20, 20), (20, 21), (20, 22), (20, 23), (20, 24), (20, 25), (21, 23), (23, 23)]
    expected, squared_power_amplitude = [], []
    # A(q)=2*pi*integral sqrt(T(r))*J0(2*pi*q*r)*r dr. This continuous
    # axisymmetric transform is independent of Prysm's two-dimensional DFT.
    for row, col in selected:
        q = (
            np.hypot(
                (col - 20) * 3 + pixel_coordinate[:, None],
                (row - 20) * 3 + pixel_coordinate[None, :],
            )
            * 1e-3
            / propagation_scale
        )
        bessel = j0(2 * np.pi * q[..., None] * radial_coordinate)
        amplitude = (
            2
            * np.pi
            * np.sum(
                bessel * np.sqrt(power) * radial_coordinate * radial_weights,
                axis=-1,
            )
        )
        expected.append(
            float(
                np.sum(
                    amplitude**2 / propagation_scale**2 / transmitted_energy * 1e-6 * pixel_area,
                )
            )
        )
        # Deliberately wrong: using T as amplitude gives T**2 power. Uniform
        # neutral-density tests cannot detect that error after normalization.
        wrong_amplitude = (
            2
            * np.pi
            * np.sum(
                bessel * power * radial_coordinate * radial_weights,
                axis=-1,
            )
        )
        squared_power_amplitude.append(
            float(
                np.sum(
                    wrong_amplitude**2 / propagation_scale**2 / wrong_energy * 1e-6 * pixel_area,
                )
            )
        )
    observed = np.asarray([actual[row, col] for row, col in selected])
    np.testing.assert_allclose(observed, expected, rtol=0, atol=3e-5)
    assert np.max(np.abs(observed - squared_power_amplitude)) > 0.006
    assert 0.98 < actual.sum() < 1  # Finite receiving support; not hidden unit-sum renormalization.
