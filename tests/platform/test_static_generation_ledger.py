"""Independent static radiance-to-collected-charge-to-RAW/ISP ledger.

All optics, pixel generation and collection values are authored synthetic
fixtures. Passing this test establishes software arithmetic, not physical
camera accuracy. Charge collection currently requires full-sensor support.
"""

from copy import deepcopy
from math import hypot, pi

import numpy as np
from test_static_physical_ledger import (
    EXPOSURE_S,
    FIELD_RADIUS_MM,
    FULL_SHAPE,
    LIGHT_M_S,
    PITCH_UM,
    PLANCK_J_S,
    SITE_QE,
    WAVELENGTH_NM,
    _direct_scatter,
    _projection,
    _radiance,
    _request,
)

from camera_platform import scene_render, spatial

OFFSETS = ((0, 0), (0, 1), (1, 0))
PROBABILITIES = (
    ((0.5, 0.1, 0.2), (0.4, 0.2, 0.3), (0.3, 0.3, 0.1), (0.2, 0.4, 0.2)),
    ((0.1, 0.6, 0.2), (0.2, 0.5, 0.1), (0.4, 0.1, 0.3), (0.5, 0.2, 0.2)),
)
CFA_TILE = ((0, 1), (2, 3))


def _collection():
    return {
        "schema": "camera_charge_collection_v1",
        "site_names": ["R", "Gr", "Gb", "B"],
        "site_index_tile": [[0, 1], [2, 3]],
        "pixel_pitch_um": PITCH_UM,
        "wavelength_nm": list(WAVELENGTH_NM),
        "destination_offsets_rc": [list(offset) for offset in OFFSETS],
        "values_axis_order": ["wavelength", "source_site", "destination_offset"],
        "probabilities": [[list(probability) for probability in band]
                          for band in PROBABILITIES],
        "interpolation": "linear_wavelength",
        "input_plane": "silicon_generated_charge",
        "output_plane": "collection_well_before_readout",
        "temporal_model": "instantaneous_memoryless",
        "boundary": "absorb_outside_full_sensor",
        "angular_assumption": "direction_independent_depth_marginal",
        "statistics": "single_destination_poisson_thinning",
        "regime": "linear_low_injection",
        "source": {"kind": "estimated", "description": "Authored scalar routing fixture"},
    }


def _generated_request(projected):
    request = _request(projected)
    response = request["native_pixel_response"]
    response["schema"] = "camera_native_generation_response_v1"
    response["response_basis"] = (
        "independent_site_generated_electrons_per_incident_pixel_photon"
    )
    response["source"]["description"] = "Authored carrier generation, excludes collection"
    request["charge_collection"] = _collection()
    return request


def _independent_charge(blurred):
    """Route two spectral endpoints by scalar source/destination bookkeeping."""
    results = {name: np.zeros(FULL_SHAPE, dtype=float) for name in
               ("generated", "collected", "intrinsic_loss", "off_sensor_loss")}
    spectral_step = WAVELENGTH_NM[1] - WAVELENGTH_NM[0]
    for row in range(FULL_SHAPE[0]):
        for col in range(FULL_SHAPE[1]):
            site = CFA_TILE[row % 2][col % 2]
            field_fraction = (hypot(row - 3.5, col - 3.5) * PITCH_UM / 1000
                              / FIELD_RADIUS_MM)
            for band, wavelength in enumerate(WAVELENGTH_NM):
                relative_illumination = 1 + field_fraction * ((0.7, 0.9)[band] - 1)
                generated = (blurred[row, col, band] * relative_illumination
                             * wavelength * 1e-9 / (PLANCK_J_S * LIGHT_M_S)
                             * (PITCH_UM * 1e-6) ** 2 * SITE_QE[band][site]
                             * spectral_step / 2 * EXPOSURE_S)
                probabilities = PROBABILITIES[band][site]
                results["generated"][row, col] += generated
                results["intrinsic_loss"][row, col] += generated * (1 - sum(probabilities))
                for (dy, dx), probability in zip(OFFSETS, probabilities, strict=True):
                    destination = row + dy, col + dx
                    if (0 <= destination[0] < FULL_SHAPE[0]
                            and 0 <= destination[1] < FULL_SHAPE[1]):
                        results["collected"][destination] += generated * probability
                    else:
                        results["off_sensor_loss"][row, col] += generated * probability
    return results


def _expected_raw(collected):
    white, black, full_well = 4095, 64, 10000
    return np.rint(black + collected * (white - black) / full_well).astype(np.uint16)


def test_authored_radiance_generation_collection_raw_and_isp_share_one_ledger():
    radiance = _radiance()
    projected = scene_render.project_radiance(radiance, _projection())
    expected_projection = radiance * np.array([pi * 0.8 / 64, pi * 0.6 / 64])
    np.testing.assert_allclose(projected, expected_projection, rtol=0, atol=1e-18)

    request = _generated_request(projected)
    result = spatial.SpatialSceneRuntime().execute(request)
    arrays, summary = result["arrays"], result["summary"]
    scattered = _direct_scatter(expected_projection)
    charge = _independent_charge(scattered)
    expected_raw = _expected_raw(charge["collected"])

    np.testing.assert_allclose(arrays["blurred_spectral_irradiance"], scattered,
                               rtol=1e-12, atol=1e-18)
    for name in ("generated", "collected", "intrinsic_loss", "off_sensor_loss"):
        observed = (arrays["mean_electrons"] if name == "collected"
                    else arrays["collection_" + name])
        np.testing.assert_allclose(observed, charge[name], rtol=4e-13, atol=1e-11)
    np.testing.assert_allclose(
        charge["generated"].sum(),
        sum(charge[name].sum() for name in
            ("collected", "intrinsic_loss", "off_sensor_loss")),
        rtol=1e-14,
    )
    assert charge["intrinsic_loss"].sum() > 0
    assert charge["off_sensor_loss"].sum() > 0
    np.testing.assert_array_equal(arrays["raw_frames"][0], expected_raw)

    # The destination's own CFA response must not filter transported charge.
    for row, col in ((4, 4), (4, 5), (5, 4), (5, 5)):
        site = CFA_TILE[row % 2][col % 2]
        np.testing.assert_allclose(arrays["channel_electrons"][row, col, site],
                                   charge["collected"][row, col], rtol=4e-13)
        assert np.count_nonzero(arrays["channel_electrons"][row, col]) == 1

    row, col = 4, 4  # Interior red site; its neighboring RAW codes are independent.
    normalized = (expected_raw.astype(float) - 64) / (4095 - 64)
    expected_rgb = [
        normalized[row, col],
        sum(normalized[y, x] for y, x in
            ((row - 1, col), (row, col - 1), (row, col + 1), (row + 1, col))) / 4,
        sum(normalized[y, x] for y, x in
            ((row - 1, col - 1), (row - 1, col + 1),
             (row + 1, col - 1), (row + 1, col + 1))) / 4,
    ]
    np.testing.assert_allclose(arrays["demosaiced_rgb_frames"][0, row, col],
                               expected_rgb, rtol=1e-12)
    np.testing.assert_allclose(arrays["rgb_frames"][0, row, col], expected_rgb,
                               rtol=1e-12)
    assert summary["native_pixel_response"]["response_basis"].endswith(
        "generated_electrons_per_incident_pixel_photon")
    assert summary["charge_collection"]["physical_validation"] == "not_established"
    assert summary["physical_validation"] == "not_established"

    identity = deepcopy(request)
    identity["charge_collection"]["probabilities"] = [
        [[1, 0, 0] for _ in range(4)] for _ in range(2)
    ]
    identity_output = spatial.SpatialSceneRuntime().execute(identity)["arrays"]
    assert np.count_nonzero(arrays["raw_frames"] != identity_output["raw_frames"]) > 16
    assert not np.array_equal(arrays["rgb_frames"], identity_output["rgb_frames"])
