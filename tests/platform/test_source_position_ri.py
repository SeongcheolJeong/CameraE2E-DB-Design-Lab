"""Independent arithmetic for opt-in source-field relative illumination.

All response and scene values are authored synthetic fixtures. These tests
verify a discrete operator and RAW propagation, not measured Lens accuracy.
"""

from copy import deepcopy

import numpy as np
from test_native_pixel_workflow import _pure_request

from camera_platform import subcell_optics
from camera_platform.spatial import SpatialSceneRuntime, request_template

PLANCK = 6.62607015e-34
LIGHT = 299792458.0
MODE = "source_position_ri_v1"


def _native_case():
    request = _pure_request()
    response = np.broadcast_to([0.2, 0.35, 0.6, 0.8], (2, 2, 4, 4)).copy()
    request["native_pixel_response"]["values"] = response.tolist()
    request["lens"]["relative_illumination"] = [[1.0, 0.25], [1.0, 0.65]]
    image = np.full((8, 8, 2), 1e-5)
    image[4, 4] = [8e-5, 6e-5]
    image[2, 6] = [4e-5, 5e-5]
    request["scene"]["spectral_irradiance"] = image
    kernel = np.array([
        [[0, 0.1, 0], [0.2, 0.3, 0.2], [0, 0.2, 0]],
        [[0.1, 0, 0], [0, 0.2, 0.4], [0, 0.3, 0]],
    ])
    request["psf"]["values"] = kernel.tolist()
    request["field_radiometry_mode"] = MODE
    return request, image, kernel


def _source_cell_oracle(request, image, kernel):
    """Scatter every source/wavelength/tap; no runtime PSF or RI helper."""
    rows, cols, bands = image.shape
    blurred = np.zeros_like(image)
    source_ri = np.empty_like(image)
    full = request["scene"]["sampling"]["full_sensor_shape"]
    origin = request["scene"]["sampling"]["crop_origin"]
    reference = request["lens"]["field_reference_radius_mm"]
    for row in range(rows):
        for col in range(cols):
            y_mm = (origin[0] + row - (full[0] - 1) / 2) * 0.004
            x_mm = (origin[1] + col - (full[1] - 1) / 2) * 0.004
            radius = np.hypot(y_mm, x_mm) / reference
            for band in range(bands):
                source_ri[row, col, band] = (1 - radius) + radius * (
                    request["lens"]["relative_illumination"][band][1]
                )
                for tap_y in range(3):
                    for tap_x in range(3):
                        dest_y, dest_x = row + tap_y - 1, col + tap_x - 1
                        if 0 <= dest_y < rows and 0 <= dest_x < cols:
                            blurred[dest_y, dest_x, band] += (
                                image[row, col, band] * source_ri[row, col, band]
                                * kernel[band, tap_y, tap_x]
                            )
    site = np.array([[0, 1], [2, 3]])[
        (np.arange(rows)[:, None] + origin[0]) % 2,
        (np.arange(cols)[None, :] + origin[1]) % 2,
    ]
    qe = np.array([0.2, 0.35, 0.6, 0.8])[site]
    wave = request["wavelength_nm"]
    density = blurred * (np.asarray(wave) * 1e-9) / (PLANCK * LIGHT)
    electrons = density[..., 0] + density[..., 1]
    electrons *= (wave[1] - wave[0]) / 2 * (4e-6) ** 2 * request["exposure_s"] * qe
    return blurred, source_ri, electrons


def test_source_position_ri_matches_independent_direct_sum_electrons_and_integer_raw():
    request, image, kernel = _native_case()
    frozen_source = image.copy()
    expected_blurred, expected_ri, expected_e = _source_cell_oracle(request, image, kernel)
    output = SpatialSceneRuntime().execute(request)
    arrays, summary = output["arrays"], output["summary"]
    np.testing.assert_array_equal(image, frozen_source)
    np.testing.assert_allclose(arrays["source_relative_illumination_map"], expected_ri,
                               rtol=0, atol=1e-15)
    np.testing.assert_allclose(arrays["blurred_spectral_irradiance"], expected_blurred,
                               rtol=3e-14, atol=1e-19)
    np.testing.assert_allclose(arrays["sensor_spectral_irradiance"], expected_blurred,
                               rtol=3e-14, atol=1e-19)
    np.testing.assert_allclose(arrays["mean_electrons"], expected_e,
                               rtol=4e-14, atol=1e-11)
    readout = request["readout"]
    white, black = 2**readout["bit_depth"] - 1, readout["black_level_dn"]
    assert expected_e.max() < readout["full_well_e"]
    raw = np.clip(np.rint(black + expected_e * (white - black)
                          * readout["gain"] / readout["full_well_e"]), 0, white)
    np.testing.assert_array_equal(arrays["raw_frames"][0], raw.astype(np.int32))
    assert arrays["rgb_frames"].shape == (1, 8, 8, 3)
    assert summary["relative_illumination_application"]["receiver_relative_illumination_map"] == (
        "diagnostic_only_not_multiplied"
    )
    flux = summary["flux_diagnostics"]
    area = (4e-6) ** 2
    np.testing.assert_allclose(flux["pre_ri_source_spectral_flux"],
                               image.sum(axis=(0, 1)) * area, rtol=1e-15)
    np.testing.assert_allclose(flux["post_ri_source_spectral_flux"],
                               (image * expected_ri).sum(axis=(0, 1)) * area, rtol=1e-15)
    np.testing.assert_allclose(flux["input_spectral_flux"],
                               flux["post_ri_source_spectral_flux"], rtol=1e-15)


def test_opt_in_differs_from_legacy_for_gradient_and_agrees_for_uniform_ri():
    request, _, _ = _native_case()
    legacy = deepcopy(request)
    legacy.pop("field_radiometry_mode")
    selected = SpatialSceneRuntime().execute(request)
    previous = SpatialSceneRuntime().execute(legacy)
    assert not np.allclose(selected["arrays"]["mean_electrons"],
                           previous["arrays"]["mean_electrons"], rtol=1e-3)
    assert "source_relative_illumination_map" not in previous["arrays"]
    assert "field_radiometry_mode" not in previous["summary"]
    for variant in (request, legacy):
        variant["lens"]["relative_illumination"] = [[1, 1], [1, 1]]
    selected = SpatialSceneRuntime().execute(request)
    previous = SpatialSceneRuntime().execute(legacy)
    np.testing.assert_array_equal(selected["arrays"]["raw_frames"],
                                  previous["arrays"]["raw_frames"])
    np.testing.assert_array_equal(selected["arrays"]["rgb_frames"],
                                  previous["arrays"]["rgb_frames"])
    np.testing.assert_allclose(selected["arrays"]["mean_electrons"],
                               previous["arrays"]["mean_electrons"], rtol=0, atol=1e-11)


def test_native_receiver_roi_matches_full_with_required_psf_halo():
    request, _, kernel = _native_case()
    rows, cols = np.indices((12, 12))
    image = np.stack([(1 + rows / 12 + cols / 20) * 1e-5,
                      (1 + cols / 9) * 1e-5], axis=-1)
    image[4, 3] += 4e-5
    request["scene"]["spectral_irradiance"] = image
    request["scene"]["sampling"].update(full_sensor_shape=[13, 13], crop_origin=[0, 0])
    request["lens"]["field_reference_radius_mm"] = 0.05
    request["lens"]["directional_response"].update(
        field_reference_radius_mm=0.05, image_height_mm=[0, 0.05]
    )
    request["psf"]["values"] = kernel.tolist()
    full = SpatialSceneRuntime().execute(request)
    cropped = deepcopy(request)
    cropped["scene"]["spectral_irradiance"] = image[1:11, 1:11].copy()
    cropped["scene"]["sampling"].update(
        crop_origin=[1, 1], capture_crop={"crop_origin": [2, 2], "shape": [8, 8]}
    )
    result = SpatialSceneRuntime().execute(cropped)
    for name in ("blurred_spectral_irradiance", "sensor_spectral_irradiance",
                 "mean_electrons", "relative_illumination_map"):
        np.testing.assert_allclose(result["arrays"][name],
                                   full["arrays"][name][2:10, 2:10],
                                   rtol=4e-14, atol=1e-11)
    np.testing.assert_array_equal(result["arrays"]["raw_frames"],
                                  full["arrays"]["raw_frames"][:, 2:10, 2:10])
    np.testing.assert_allclose(result["arrays"]["source_relative_illumination_map"],
                               full["arrays"]["source_relative_illumination_map"][1:11, 1:11])
    assert result["summary"]["array_geometry"]["source_relative_illumination_map"] == (
        "source_support"
    )
    assert result["summary"]["flux_diagnostics"]["capture_flux_scope"] == (
        "after_source_ri_and_psf_before_receiver_angular_qe"
    )


def test_retained_subcell_source_ri_uses_fractional_native_positions():
    request = request_template()
    source = np.zeros((8, 8, 2, 2, 5))
    source[3, 4, 0, 0] = [1e-5, 2e-5, 3e-5, 4e-5, 5e-5]
    source[3, 4, 0, 1] = [5e-5, 4e-5, 3e-5, 2e-5, 1e-5]
    request["scene"]["spectral_irradiance"] = source.mean(axis=(2, 3))
    request["scene"]["sampling"].update(full_sensor_shape=[10, 10], crop_origin=[1, 1])
    request["scene"]["source_subcell_irradiance"] = source
    request["scene"]["source_subcell_evidence"] = {
        "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
        "radiance_sha256": "a" * 64, "source_render_id": "fixture-render",
        "projection_job_id": None,
    }
    request.update(frames=1, noise_enabled=False, field_radiometry_mode=MODE)
    kernel = np.zeros((5, 3, 3))
    kernel[:, 1, 1] = 0.7
    kernel[:, 1, 2] = 0.3
    request["psf"]["values"] = kernel.tolist()
    before = source.copy()
    output = SpatialSceneRuntime().execute(request)
    arrays = output["arrays"]
    np.testing.assert_array_equal(source, before)
    ri = arrays["source_relative_illumination_map"]
    assert ri.shape == source.shape
    assert ri[3, 4, 0, 0, 0] != ri[3, 4, 0, 1, 0]
    # Independent scalar scatter with a tent response for each fractional
    # source position; the receiver-integrated PSF is not box-filtered twice.
    expected = np.zeros((8, 8, 5))
    denominator = np.hypot(4.5, 4.5)
    for sy in range(2):
        for sx in range(2):
            dy, dx = (sy + 0.5) / 2 - 0.5, (sx + 0.5) / 2 - 0.5
            radius = np.hypot(1 + 3 + dy - 4.5, 1 + 4 + dx - 4.5) / denominator
            for band in range(5):
                value = source[3, 4, sy, sx, band] * (1 - 0.2 * radius) / 4
                for tap_x, power in ((0, 0.7), (1, 0.3)):
                    position = 4 + tap_x + dx
                    for dest_x in range(8):
                        weight_x = max(0, 1 - abs(dest_x - position))
                        for dest_y in range(8):
                            weight_y = max(0, 1 - abs(dest_y - (3 + dy)))
                            expected[dest_y, dest_x, band] += value * power * weight_x * weight_y
    np.testing.assert_allclose(arrays["blurred_spectral_irradiance"], expected,
                               rtol=4e-14, atol=1e-19)
    flux = output["summary"]["flux_diagnostics"]
    np.testing.assert_allclose(flux["post_ri_source_spectral_flux"],
                               (source * ri).sum(axis=(0, 1, 2, 3)) * (4e-6)**2 / 4)


def test_source_ri_preflight_rejects_unknown_mode_invalid_sensor_and_subcell_extrapolation():
    request, _, _ = _native_case()
    for invalid in (None, "receiver_position_ri_v1", False):
        changed = deepcopy(request)
        changed["field_radiometry_mode"] = invalid
        assert SpatialSceneRuntime().validate_request(changed)
    changed = deepcopy(request)
    changed["scene"]["sampling"]["full_sensor_shape"] = [1, 1]
    assert "full_sensor_shape" in SpatialSceneRuntime().validate_request(changed)[0]["message"]
    subcell = request_template()
    subcell["scene"]["spectral_irradiance"] = np.ones((8, 8, 5)) * 1e-5
    subcell["scene"]["sampling"].update(full_sensor_shape=[8, 8], crop_origin=[0, 0])
    subcell["scene"]["source_subcell_irradiance"] = np.ones((8, 8, 2, 2, 5)) * 1e-5
    subcell["scene"]["source_subcell_evidence"] = {
        "model": "bilinear_receiver_integrated_impulse_v1", "grid_side": 2,
        "radiance_sha256": "a" * 64, "source_render_id": "fixture-render",
        "projection_job_id": None,
    }
    subcell["field_radiometry_mode"] = MODE
    issue = SpatialSceneRuntime().validate_request(subcell)
    assert issue and "Source-position RI does not cover" in issue[0]["message"]


def test_legacy_retained_subcell_capture_uses_source_origin_for_field_psf():
    """A receiver ROI must not move the field at which source rays enter the PSF."""
    request = request_template()
    request.update(frames=1, noise_enabled=False)
    request["lens"]["relative_illumination"] = [[1, 1]] * 5
    source = np.zeros((20, 20, 2, 2, 5))
    source[6, 9, 0, 1] = np.arange(1, 6) * 2e-5
    source[13, 14, 1, 0] = np.arange(5, 0, -1) * 1e-5
    request["scene"]["spectral_irradiance"] = source.mean(axis=(2, 3))
    request["scene"]["source_subcell_irradiance"] = source
    request["scene"]["source_subcell_evidence"] = {
        "model": subcell_optics.TRANSFER, "grid_side": 2,
        "radiance_sha256": "a" * 64, "source_render_id": "fixture-render",
        "projection_job_id": None,
    }
    request["scene"]["sampling"].update(full_sensor_shape=[20, 20], crop_origin=[0, 0])
    axes = [-0.05, 0.05]
    kernel = np.zeros((5, 2, 2, 3, 3))
    for iy in range(2):
        for ix in range(2):
            center = 0.1 + 0.2 * iy + 0.3 * ix
            kernel[:, iy, ix, 1, 1] = center
            kernel[:, iy, ix, 1, 2] = 1 - center
    request["psf"].update(
        schema="field_intensity_psf_v1", spatial_model="field_grid",
        field_y_mm=axes, field_x_mm=axes,
        field_coordinates="sensor_center_yx", interpolation="bilinear_source_position",
        values=kernel.tolist(),
    )
    source_copy = source.copy()
    complete = SpatialSceneRuntime().execute(request)
    # Directly scatter each nonzero fractional source through an independently
    # interpolated field kernel and receiver tent. No runtime PSF helper is used.
    expected = np.zeros((20, 20, 5))
    for src_y, src_x, sy, sx in ((6, 9, 0, 1), (13, 14, 1, 0)):
        dy, dx = (sy + 0.5) / 2 - 0.5, (sx + 0.5) / 2 - 0.5
        fy = (src_y + dy - 9.5) * 0.004
        fx = (src_x + dx - 9.5) * 0.004
        wy = [(axes[1] - fy) / 0.1, (fy - axes[0]) / 0.1]
        wx = [(axes[1] - fx) / 0.1, (fx - axes[0]) / 0.1]
        for iy in range(2):
            for ix in range(2):
                for tap_y in range(3):
                    for tap_x in range(3):
                        power = kernel[:, iy, ix, tap_y, tap_x] * wy[iy] * wx[ix]
                        if not np.any(power):
                            continue
                        hit_y = src_y + tap_y - 1 + dy
                        hit_x = src_x + tap_x - 1 + dx
                        for dst_y in range(20):
                            tent_y = max(0, 1 - abs(dst_y - hit_y))
                            if tent_y == 0:
                                continue
                            for dst_x in range(20):
                                tent_x = max(0, 1 - abs(dst_x - hit_x))
                                if tent_x:
                                    expected[dst_y, dst_x] += (
                                        source[src_y, src_x, sy, sx] * power
                                        * tent_y * tent_x / 4
                                    )
    np.testing.assert_allclose(complete["arrays"]["blurred_spectral_irradiance"], expected,
                               rtol=3e-13, atol=1e-19)
    cropped = deepcopy(request)
    cropped["scene"]["spectral_irradiance"] = source.mean(axis=(2, 3))[5:17, 5:17]
    cropped["scene"]["source_subcell_irradiance"] = source[5:17, 5:17].copy()
    cropped["scene"]["sampling"].update(
        crop_origin=[5, 5], capture_crop={"crop_origin": [7, 7], "shape": [8, 8]}
    )
    result = SpatialSceneRuntime().execute(cropped)
    np.testing.assert_allclose(result["arrays"]["blurred_spectral_irradiance"],
                               expected[7:15, 7:15], rtol=3e-13, atol=1e-19)
    np.testing.assert_allclose(result["arrays"]["mean_electrons"],
                               complete["arrays"]["mean_electrons"][7:15, 7:15],
                               rtol=3e-13, atol=1e-11)
    np.testing.assert_array_equal(result["arrays"]["raw_frames"],
                                  complete["arrays"]["raw_frames"][:, 7:15, 7:15])
    np.testing.assert_array_equal(source, source_copy)
    # The old implementation passed receiver [7,7] instead of source [5,5]
    # to this transfer. It remains inside the sensor here, so its numerical
    # discrepancy is visible instead of only triggering a bounds error.
    wrong, _ = subcell_optics.transfer(
        source[5:17, 5:17], kernel, 4, [20, 20], [7, 7],
        field_axes=(axes, axes),
    )
    difference = np.max(np.abs(wrong[2:10, 2:10] - expected[7:15, 7:15]))
    assert difference > 1e-7
