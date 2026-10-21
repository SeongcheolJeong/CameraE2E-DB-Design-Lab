"""Independent static radiance-to-RAW/ISP arithmetic for an authored sensor.

This tests implementation and units, not lens/sensor accuracy or calibration.
The paraxial projection, PSF and native-site EQE are explicit synthetic inputs.
"""

from math import hypot, pi

import numpy as np

from camera_platform import scene_render, spatial
from camera_platform.isp import default_profile


WAVELENGTH_NM = (500.0, 600.0)
PITCH_UM = 4.0
EXPOSURE_S = 0.004
PLANCK_J_S = 6.62607015e-34
LIGHT_M_S = 299792458.0
SITE_QE = ((0.30, 0.50, 0.40, 0.20), (0.25, 0.45, 0.35, 0.15))
PSF = ((0.00, 0.00, 0.00), (0.10, 0.60, 0.20), (0.00, 0.10, 0.00))
FULL_SHAPE = (8, 8)
FIELD_RADIUS_MM = 0.020


def _projection(transmittance=(0.8, 0.6)):
    return {
        "projection_model": "paraxial_on_axis_projection_v1",
        "focal_length_mm": 4.0,
        "f_number": 4.0,
        "on_axis_transmittance": {
            "wavelength_nm": list(WAVELENGTH_NM), "values": list(transmittance)
        },
        "geometry_model": "pinhole_pixel_centre",
        "irradiance_model": "pi_L_T_over_4N2_before_relative_illumination",
        "approximation_uncertainty": "unknown",
        "source_kind": "estimated",
        "source_uri": "fixture://static-physical-ledger/paraxial-projection",
        "license": "MIT",
        "method_and_version": "authored-paraxial-static-ledger:1",
        "physical_instance": None,
        "parents": [],
    }


def _radiance():
    image = np.empty((*FULL_SHAPE, len(WAVELENGTH_NM)), dtype=float)
    for row in range(FULL_SHAPE[0]):
        for col in range(FULL_SHAPE[1]):
            factor = 1 + 0.06 * row + 0.04 * col
            image[row, col] = (0.003 * factor, 0.004 * factor)
    image[4, 4] += (0.005, 0.002)
    return image  # W/m^2/sr/nm at a frozen, synthetic scene instant


def _request(projected):
    request = spatial.request_template()
    request.pop("qe")
    request.pop("sensor_angular_response")
    request.update(
        wavelength_nm=list(WAVELENGTH_NM),
        pixel_pitch_um=PITCH_UM,
        exposure_s=EXPOSURE_S,
        frames=1,
        noise_enabled=False,
        native_pixel_response={
            "schema": "camera_native_pixel_response_v1",
            "site_names": ["R", "Gr", "Gb", "B"],
            "site_index_tile": [[0, 1], [2, 3]],
            "pixel_pitch_um": PITCH_UM,
            "reference_plane": "sensor_surface_before_pixel_optics",
            "direction_convention": "propagation_into_sensor_x_column_y_row",
            "polarization": "unpolarized",
            "response_basis": "independent_site_collected_electrons_per_incident_pixel_photon",
            "wavelength_nm": list(WAVELENGTH_NM),
            "theta_deg": [0.0],
            "phi_deg": [0.0],
            "interpolation": "linear_wavelength_theta_periodic_phi",
            "values_axis_order": ["wavelength", "theta", "phi", "site"],
            "values": [[[list(sites)]] for sites in SITE_QE],
            "source": {"kind": "estimated", "description": "Authored site EQE; includes collection"},
        },
    )
    request["readout"].update(
        full_well_e=10000, read_noise_e=0, dark_current_e_per_s=0,
        gain=1, bit_depth=12, black_level_dn=64,
    )
    request["scene"].update(
        wavelength_nm=list(WAVELENGTH_NM),
        spectral_irradiance=projected,
        included_effects=["on_axis_lens_throughput"],
        source={"kind": "estimated", "description": "Projected authored radiance"},
    )
    request["scene"]["sampling"].update(
        full_sensor_shape=list(FULL_SHAPE), crop_origin=[0, 0], pixel_pitch_um=PITCH_UM,
    )
    request["psf"].update(
        wavelength_nm=list(WAVELENGTH_NM),
        values=[PSF, PSF],
        sample_pitch_um=PITCH_UM,
        source={"kind": "estimated", "description": "Authored unit-mass asymmetric PSF"},
    )
    request["lens"] = {
        "wavelength_nm": list(WAVELENGTH_NM),
        "field_radius": [0.0, 1.0],
        "theta_deg": [0.0, 20.0],
        "angular_weights": [[[1.0, 0.0], [1.0, 0.0]]] * 2,
        "relative_illumination": [[1.0, 0.7], [1.0, 0.9]],
        "field_reference_radius_mm": FIELD_RADIUS_MM,
        "directional_response": {
            "schema": "camera_lens_directional_response_v1",
            "wavelength_nm": list(WAVELENGTH_NM),
            "field_radius": [0.0, 1.0],
            "image_height_mm": [0.0, FIELD_RADIUS_MM],
            "field_reference_radius_mm": FIELD_RADIUS_MM,
            "direction_convention": "propagation_into_sensor_x_column_y_row",
            "reference_plane": "sensor_surface_before_pixel_optics",
            "field_reference": "positive_x_column",
            "symmetry": "centered_spherical_or_plane_prescription_radial_stop",
            "normalization": "conditional_transmitted_irradiance_fraction",
            # Structurally valid authored identifiers, never Factory evidence.
            "prescription_sha256": "a" * 64,
            "stop_transmission_sha256": "b" * 64,
            "directions_xyz": [[[0.0, 0.0, 1.0]], [[0.0, 0.0, 1.0]]],
            "conditional_weights": [[[1.0], [1.0]], [[1.0], [1.0]]],
            "on_axis_rotation_phi_deg": list(range(0, 360, 45)),
        },
    }
    profile = default_profile()
    profile["tone"]["curve"] = "linear"
    request["isp_profile"] = profile
    return request


def _direct_scatter(image):
    """Source-cell to receiver-cell scalar sum, independent of FFT convolution."""
    result = np.zeros_like(image)
    for row in range(FULL_SHAPE[0]):
        for col in range(FULL_SHAPE[1]):
            for kr in range(3):
                for kc in range(3):
                    receiver = (row + kr - 1, col + kc - 1)
                    if 0 <= receiver[0] < FULL_SHAPE[0] and 0 <= receiver[1] < FULL_SHAPE[1]:
                        result[receiver] += image[row, col] * PSF[kr][kc]
    return result


def _scalar_photocharge(blurred, row, col, site):
    radius_mm = hypot(row - 3.5, col - 3.5) * PITCH_UM / 1000
    field_fraction = radius_mm / FIELD_RADIUS_MM
    densities = []
    for band, wavelength in enumerate(WAVELENGTH_NM):
        edge_ri = (0.7, 0.9)[band]
        ri = 1 + field_fraction * (edge_ri - 1)
        photons = blurred[row, col, band] * ri * wavelength * 1e-9 / (PLANCK_J_S * LIGHT_M_S)
        densities.append(photons * SITE_QE[band][site])
    return 0.5 * (densities[0] + densities[1]) * 100 * (PITCH_UM * 1e-6) ** 2 * EXPOSURE_S


def _scalar_raw(blurred, row, col):
    site = ((0, 1), (2, 3))[row % 2][col % 2]
    electrons = _scalar_photocharge(blurred, row, col, site)
    return int(round(64 + electrons * (4095 - 64) / 10000))


def test_static_radiance_psf_native_site_readout_and_basic_isp_have_one_ledger():
    radiance = _radiance()
    projection = _projection()
    ideal = scene_render.project_radiance(radiance, projection)
    coefficients = [pi * transmission / (4 * 4**2) for transmission in (0.8, 0.6)]
    np.testing.assert_allclose(ideal, radiance * coefficients, rtol=0, atol=1e-18)
    request = _request(ideal)
    result = spatial.SpatialSceneRuntime().execute(request)
    arrays, summary = result["arrays"], result["summary"]

    direct_blur = _direct_scatter(ideal)
    np.testing.assert_allclose(arrays["ideal_spectral_irradiance"], ideal, rtol=0, atol=0)
    np.testing.assert_allclose(arrays["blurred_spectral_irradiance"], direct_blur, rtol=1e-12, atol=1e-18)
    area = (PITCH_UM * 1e-6) ** 2
    flux = summary["flux_diagnostics"]
    np.testing.assert_allclose(flux["input_spectral_flux"], ideal.sum((0, 1)) * area, rtol=1e-14)
    np.testing.assert_allclose(flux["full_convolution_spectral_flux"], ideal.sum((0, 1)) * area, rtol=1e-12)
    np.testing.assert_allclose(flux["retained_crop_spectral_flux"], direct_blur.sum((0, 1)) * area, rtol=1e-12)
    np.testing.assert_allclose(flux["outside_crop_spectral_flux"],
                               (ideal - direct_blur).sum((0, 1)) * area, rtol=1e-12)

    row, col = 4, 4  # Interior R pixel: asymmetric PSF, RI and two spectra all affect it.
    field_fraction = hypot(row - 3.5, col - 3.5) * PITCH_UM / 1000 / FIELD_RADIUS_MM
    expected_sensor_irradiance = direct_blur[row, col] * [
        1 - 0.3 * field_fraction, 1 - 0.1 * field_fraction
    ]
    np.testing.assert_allclose(arrays["sensor_spectral_irradiance"][row, col],
                               expected_sensor_irradiance, rtol=1e-12)
    expected_electrons = _scalar_photocharge(direct_blur, row, col, 0)
    np.testing.assert_allclose(arrays["channel_electrons"][row, col, 0], expected_electrons,
                               rtol=1e-12)
    np.testing.assert_allclose(arrays["mean_electrons"][row, col], expected_electrons, rtol=1e-12)
    expected_raw = np.array([[_scalar_raw(direct_blur, y, x) for x in range(3, 6)]
                             for y in range(3, 6)], dtype=np.uint16)
    np.testing.assert_array_equal(arrays["raw_frames"][0, 3:6, 3:6], expected_raw)

    # A red Bayer site keeps its red code, averages cross green sites and
    # diagonal blue sites. This oracle reads independent expected RAW codes.
    red = (expected_raw[1, 1] - 64) / 4031
    green = (sum(int(expected_raw[y, x]) for y, x in ((0, 1), (1, 0), (1, 2), (2, 1))) / 4 - 64) / 4031
    blue = (sum(int(expected_raw[y, x]) for y, x in ((0, 0), (0, 2), (2, 0), (2, 2))) / 4 - 64) / 4031
    expected_rgb = np.clip([red, green, blue], 0, 1)
    np.testing.assert_allclose(arrays["demosaiced_rgb_frames"][0, row, col], expected_rgb, rtol=1e-12)
    np.testing.assert_allclose(arrays["rgb_frames"][0, row, col], expected_rgb, rtol=1e-12)
    assert summary["array_units"]["mean_electrons"] == "electron/pixel"
    assert summary["array_units"]["raw_frames"] == "DN"
    assert summary["prediction"]["reference_plane"] == "sensor_raw"
    assert summary["native_pixel_response"]["reference_plane"] == "sensor_surface_before_pixel_optics"
    assert summary["physical_validation"] == "not_established"

    # Neutral throughput changes photons and charge once, with unchanged
    # conditional PSF/site response. It cannot be silently reapplied in Lens.
    half = scene_render.project_radiance(radiance, _projection((0.4, 0.3)))
    halved = spatial.SpatialSceneRuntime().execute(_request(half))["arrays"]
    np.testing.assert_allclose(halved["mean_electrons"], arrays["mean_electrons"] / 2, rtol=1e-12)
    assert int(halved["raw_frames"][0, row, col]) == _scalar_raw(_direct_scatter(half), row, col)
    np.testing.assert_array_equal(request["scene"]["spectral_irradiance"], ideal)


def test_wavelength_grid_refinement_converges_to_independent_quadratic_integral():
    """A linear spectral irradiance has an exactly integrable photon density."""
    amplitude = 1e-5
    pitch_m = PITCH_UM * 1e-6
    expected_rate = (
        amplitude * 1e-9 / (550 * PLANCK_J_S * LIGHT_M_S)
        * (600**3 - 500**3) / 3 * pitch_m**2 * 0.5
    )
    errors = []
    for count in (2, 5, 9):
        wave = np.linspace(500, 600, count)
        request = spatial.request_template()
        for key in ("exposure_s", "frames", "isp_profile"):
            request.pop(key, None)
        request["wavelength_nm"] = wave.tolist()
        request["pixel_pitch_um"] = PITCH_UM
        request["qe"] = [[0.5] * count] * 3
        request["lens"].update(
            wavelength_nm=wave.tolist(),
            angular_weights=[[[1.0, 0.0], [1.0, 0.0]]] * count,
            relative_illumination=[[1.0, 1.0]] * count,
        )
        request["sensor_angular_response"] = {
            "theta_deg": [0.0, 20.0],
            "relative_response": [[[1.0, 1.0]] * count] * 3,
        }
        request["scene"].update(
            wavelength_nm=wave.tolist(),
            spectral_irradiance=np.broadcast_to(
                amplitude * wave / 550, (8, 8, count)
            ).copy(),
        )
        request["scene"]["sampling"].update(
            full_sensor_shape=[8, 8], crop_origin=[0, 0], pixel_pitch_um=PITCH_UM,
        )
        request["psf"].update(
            wavelength_nm=wave.tolist(), values=[[[1.0]]] * count,
            sample_pitch_um=PITCH_UM,
        )
        rate = spatial.photoelectron_rate(request)["arrays"]["photoelectron_rate"][4, 4]
        errors.append(abs(rate - expected_rate))
    assert errors[0] > errors[1] > errors[2] > 0
    assert errors[1] < errors[0] / 10
    assert errors[2] < errors[1] / 3.9
