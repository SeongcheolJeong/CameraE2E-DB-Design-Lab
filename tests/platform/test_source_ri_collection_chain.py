"""Independent source RI -> PSF -> generation -> carrier routing -> RAW ledger.

Synthetic coefficients verify the combined discrete calculation and native crop
contract. They do not characterize or validate a physical lens or sensor.
"""

import math
from copy import deepcopy

import numpy as np
import pytest
from test_charge_collection_runtime import GENERATION, _collection
from test_native_pixel_workflow import _pure_request

from camera_platform.spatial import SpatialSceneRuntime

PLANCK_J_S = 6.62607015e-34
LIGHT_M_S = 299792458.0


def _case():
    request = _pure_request()
    request.update(exposure_s=0.04, field_radiometry_mode="source_position_ri_v1")
    response = request["native_pixel_response"]
    response["schema"] = "camera_native_generation_response_v1"
    response["response_basis"] = (
        "independent_site_generated_electrons_per_incident_pixel_photon"
    )
    values = np.broadcast_to(
        GENERATION[:, None, None, :], (2, 2, 4, 4)
    ).copy()
    for band in range(2):
        for phi_index in range(4):
            values[band, 1, phi_index] += (
                0.025 * (band + 1) * math.cos(phi_index * math.pi / 2)
                * np.array([1, -1, 1, -1])
            )
    response["values"] = values.tolist()
    response["source"]["description"] = (
        "Authored direction-dependent generated-electron efficiency; excludes collection"
    )
    request["charge_collection"] = _collection()
    request["lens"]["field_reference_radius_mm"] = 0.05
    request["lens"]["directional_response"]["field_reference_radius_mm"] = 0.05
    request["lens"]["directional_response"]["image_height_mm"] = [0, 0.05]
    request["lens"]["relative_illumination"] = [[1, 0.25], [1, 0.7]]
    rows, cols = np.indices((16, 16))
    image = 1e-3 * np.stack([1 + rows / 25 + cols / 15,
                            1 + rows / 12 + cols / 30], axis=-1)
    image[5, 5] += [4e-3, 1e-3]
    image[10, 6] += [1e-3, 3e-3]
    request["scene"]["spectral_irradiance"] = image
    request["scene"]["sampling"].update(full_sensor_shape=[16, 16], crop_origin=[0, 0])
    kernel = np.array([
        [[0, 0, 0], [0.1, 0.5, 0.4], [0, 0, 0]],
        [[0, 0.2, 0], [0, 0.6, 0], [0, 0.2, 0]],
    ])
    request["psf"]["values"] = kernel.tolist()
    request["readout"].update(read_noise_e=0, dark_current_e_per_s=0)
    return request, image, kernel


def _scalar_ledger(request, image, kernel):
    """Direct source/tap/carrier scatter, without production optical helpers."""
    response = request["native_pixel_response"]["values"]

    def generated_efficiency(band, site, row, col):
        # At the receiver, independently interpolate the joint angle/azimuth
        # response at the two declared edge rays and the axial center ray.
        y_mm, x_mm = (row - 7.5) * 0.004, (col - 7.5) * 0.004
        radius = math.hypot(y_mm, x_mm) / 0.05
        field_phi = math.degrees(math.atan2(y_mm, x_mm))

        def response_at(theta_deg, phi_deg):
            position = (phi_deg % 360) / 90
            left = int(math.floor(position))
            fraction = position - left
            plane = []
            for theta_index in range(2):
                plane.append(
                    response[band][theta_index][left][site] * (1 - fraction)
                    + response[band][theta_index][(left + 1) % 4][site] * fraction
                )
            return plane[0] * (1 - theta_deg / 20) + plane[1] * theta_deg / 20

        weights = ((0.25, 0.75), (0.8, 0.2))[band]
        edge = (weights[0] * response_at(10, field_phi)
                + weights[1] * response_at(20, 90 + field_phi))
        center = response[band][0][0][site]
        return center * (1 - radius) + edge * radius

    blurred = np.zeros_like(image)
    for sy in range(16):
        for sx in range(16):
            radius = np.hypot(sy - 7.5, sx - 7.5) * 0.004 / 0.05
            for band in range(2):
                edge = request["lens"]["relative_illumination"][band][1]
                irradiance = image[sy, sx, band] * (1 + radius * (edge - 1))
                for ky in range(3):
                    for kx in range(3):
                        dy, dx = sy + ky - 1, sx + kx - 1
                        if 0 <= dy < 16 and 0 <= dx < 16:
                            blurred[dy, dx, band] += irradiance * kernel[band, ky, kx]
    routed = np.zeros((16, 16))
    generated = np.zeros((16, 16))
    lost = np.zeros((16, 16))
    for sy in range(16):
        for sx in range(16):
            site = (sy % 2) * 2 + (sx % 2)
            for band, wave in enumerate((500, 600)):
                electron_rate_density = (
                    blurred[sy, sx, band] * wave * 1e-9
                    / (PLANCK_J_S * LIGHT_M_S) * (4e-6)**2
                    * generated_efficiency(band, site, sy, sx) * 50
                )
                generated[sy, sx] += electron_rate_density
                probabilities = request["charge_collection"]["probabilities"][band][site]
                lost[sy, sx] += electron_rate_density * (1 - sum(probabilities))
                for (oy, ox), probability in zip(
                    request["charge_collection"]["destination_offsets_rc"],
                    probabilities, strict=True,
                ):
                    dy, dx = sy + oy, sx + ox
                    if 0 <= dy < 16 and 0 <= dx < 16:
                        routed[dy, dx] += electron_rate_density * probability
    return blurred, generated, routed, lost


def test_source_position_ri_generation_and_carrier_transfer_match_scalar_raw_ledger():
    request, image, kernel = _case()
    before = image.copy()
    blurred, generated, routed, lost = _scalar_ledger(request, image, kernel)
    result = SpatialSceneRuntime().execute(request)
    arrays = result["arrays"]
    np.testing.assert_array_equal(image, before)
    np.testing.assert_allclose(arrays["blurred_spectral_irradiance"], blurred,
                               rtol=5e-14, atol=1e-17)
    np.testing.assert_allclose(arrays["collection_generated"],
                               generated * request["exposure_s"], rtol=5e-14, atol=1e-10)
    np.testing.assert_allclose(arrays["collection_intrinsic_loss"],
                               lost * request["exposure_s"], rtol=5e-14, atol=1e-10)
    charge = routed * request["exposure_s"]
    np.testing.assert_allclose(arrays["mean_electrons"], charge,
                               rtol=5e-14, atol=1e-10)
    readout = request["readout"]
    white = 2**readout["bit_depth"] - 1
    expected_raw = np.clip(np.rint(
        readout["black_level_dn"] + charge * (white - readout["black_level_dn"])
        * readout["gain"] / readout["full_well_e"]
    ), 0, white).astype(np.int32)
    np.testing.assert_array_equal(arrays["raw_frames"][0], expected_raw)
    legacy = deepcopy(request)
    legacy.pop("field_radiometry_mode")
    previous = SpatialSceneRuntime().execute(legacy)
    assert np.max(np.abs(previous["arrays"]["mean_electrons"] - charge)) > 1
    assert result["summary"]["charge_collection"]["order"] == (
        "source site, wavelength routing, spectral sum, receiver-row exposure, Poisson"
    )


def test_combined_psf_carrier_halo_produces_same_receiver_crop_and_rejects_missing_source():
    request, image, _ = _case()
    full = SpatialSceneRuntime().execute(request)
    cropped = deepcopy(request)
    cropped["scene"]["spectral_irradiance"] = image[1:15, 1:15].copy()
    cropped["scene"]["sampling"].update(
        crop_origin=[1, 1], capture_crop={"crop_origin": [4, 4], "shape": [8, 8]},
    )
    selected = SpatialSceneRuntime().execute(cropped)
    for name in ("mean_electrons", "blurred_spectral_irradiance",
                 "sensor_spectral_irradiance"):
        np.testing.assert_allclose(selected["arrays"][name],
                                   full["arrays"][name][4:12, 4:12],
                                   rtol=5e-14, atol=1e-10)
    np.testing.assert_array_equal(selected["arrays"]["raw_frames"],
                                  full["arrays"]["raw_frames"][:, 4:12, 4:12])
    missing = deepcopy(cropped)
    missing["scene"]["spectral_irradiance"] = image[3:13, 3:13].copy()
    missing["scene"]["sampling"]["crop_origin"] = [3, 3]
    with pytest.raises(ValueError, match="combined PSF and carrier halo"):
        SpatialSceneRuntime().execute(missing)
