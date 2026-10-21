"""Useful projection and native/angular SFR regressions for the wide Lab demo."""

import json

import numpy as np
import pytest
from scipy.ndimage import gaussian_filter
from scipy.special import erf

from camera_platform import virtual_lab_wide_metrics as metrics
from camera_platform.virtual_lab_wide_geometry import (
    chart_pattern,
    pixel_directions,
    resolution_sites,
)


def geometry(projection="rectilinear"):
    width, height, pitch, fov = 1920, 1080, 3.45, 120.0
    angle = np.radians(fov)
    focal_pixels = width / (2 * np.tan(angle / 2)) if projection == "rectilinear" else width / angle
    return {
        "projection": projection,
        "native_shape": [height, width],
        "horizontal_fov_deg": fov,
        "pixel_pitch_um": pitch,
        "focal_length_mm": focal_pixels * pitch * 0.001,
        "source_scene_angular_extent_deg": [120.0, 67.5],
    }


@pytest.mark.parametrize("projection", ["rectilinear", "equidistant"])
def test_projection_jacobian_matches_rays_and_finite_difference(projection):
    model = geometry(projection)
    rows, cols = np.array([539.5, 140.2, 930.7]), np.array([959.5, 151.6, 1788.4])
    rays = metrics.pixel_to_direction(model, rows, cols)
    np.testing.assert_allclose(rays, pixel_directions(rows, cols, model), atol=1e-14)
    jac = metrics.local_projection_jacobian(model, rows, cols)
    delta = 1e-3
    numeric = np.stack(
        [
            (
                metrics.pixel_to_direction(model, rows, cols + delta)
                - metrics.pixel_to_direction(model, rows, cols - delta)
            )
            / (2 * delta),
            (
                metrics.pixel_to_direction(model, rows + delta, cols)
                - metrics.pixel_to_direction(model, rows - delta, cols)
            )
            / (2 * delta),
        ],
        axis=-1,
    )
    np.testing.assert_allclose(jac, numeric, rtol=1e-8, atol=1e-12)
    np.testing.assert_allclose(np.einsum("...i,...ij->...j", rays, jac), 0, atol=1e-17)


@pytest.mark.parametrize("projection", ["rectilinear", "equidistant"])
def test_principal_angular_scales_follow_projection_not_fov_over_width(projection):
    model = geometry(projection)
    center = metrics.local_projection_jacobian(model, 539.5, 959.5)
    peripheral = metrics.local_projection_jacobian(model, 539.5, 1799.5)

    def scale(jac, normal):
        return float(metrics._angular_scale(jac, np.asarray(normal)))

    center_scale = scale(center, [1.0, 0.0])
    radial = scale(peripheral, [1.0, 0.0])
    tangential = scale(peripheral, [0.0, 1.0])
    if projection == "equidistant":
        assert radial == pytest.approx(center_scale)
        assert tangential < radial
    else:
        assert radial < tangential < center_scale
        assert center_scale != pytest.approx(120 / 1920, rel=0.1)


def test_oblique_angular_frequency_uses_covector_under_anisotropy():
    model = geometry()
    jac = metrics.local_projection_jacobian(model, 539.5, 1799.5)
    normal = np.ones(2) / np.sqrt(2)
    inverse_metric = np.linalg.inv(jac.T @ jac)
    expected = (180 / np.pi) / np.sqrt(normal @ inverse_metric @ normal)
    scale = metrics._angular_scale(jac, normal)
    assert scale == pytest.approx(expected, rel=1e-12)
    incorrect_displacement_scale = np.linalg.norm(jac @ normal) * 180 / np.pi
    assert scale < incorrect_displacement_scale * 0.9


def test_shared_angular_chart_sites_stay_inside_both_projection_windows():
    by_projection = {
        name: resolution_sites(geometry(name)) for name in ("rectilinear", "equidistant")
    }
    for projection, sites in by_projection.items():
        for name, site in sites.items():
            for orientation in ("radial", "tangential"):
                edge = site[orientation]
                r0, r1, c0, c1 = edge["bounds_row_col"]
                assert 0 <= r0 < r1 <= 1080, (projection, name, orientation)
                assert 0 <= c0 < c1 <= 1920, (projection, name, orientation)
                np.testing.assert_allclose(
                    edge["source_direction_angles_rad"],
                    by_projection["rectilinear"][name][orientation]["source_direction_angles_rad"],
                    atol=1e-12,
                )


@pytest.mark.parametrize("projection", ["rectilinear", "equidistant"])
def test_production_angular_edges_are_not_contaminated_by_neighbor_chart_patches(projection):
    model = geometry(projection)
    sites = resolution_sites(model)
    for name, site in sites.items():
        for orientation in ("radial", "tangential"):
            edge = site[orientation]
            r0, r1, c0, c1 = edge["bounds_row_col"]
            # Query only a haloed ROI of the complete directional scene. The
            # generator still sees every site, so overlapping patches cannot
            # be hidden by rendering one edge at a time.
            rr, cc = np.meshgrid(
                np.arange(r0 - 8, r1 + 8), np.arange(c0 - 8, c1 + 8), indexing="ij"
            )
            image = gaussian_filter(chart_pattern("resolution", rr, cc, model, sites), 1.3)[
                8:-8, 8:-8
            ]
            hint = np.asarray(edge["normal_xy"])
            normal, center = metrics._fit_normal(image, hint)
            angular = metrics.angular_geometry(model, edge["bounds_row_col"], normal)
            result = metrics._sfr(image, normal, center, model["pixel_pitch_um"], angular)
            assert result["status"] == "ok", (name, orientation, result)
            # This angular chart can curve after projection, so this is a
            # usable-edge/layout bound, not the strict Gaussian identity test.
            assert 0.10 < result["mtf50_cycles_per_pixel"] < 0.18, (name, orientation)


def test_large_roi_flags_nonlinear_angular_axis_and_preserves_native_sfr():
    model = geometry()
    bounds = [190, 490, 1280, 1580]
    normal = np.array([1.0, 0.09])
    normal /= np.linalg.norm(normal)
    angular = metrics.angular_geometry(model, bounds, normal)
    assert not angular["angular_frequency_qualified"]
    assert angular["roi_relative_scale_variation"] > 0.05
    yy, xx = np.indices((300, 300), dtype=float)
    edge = 0.4 + 0.3 * erf(((xx - 149.5) + (yy - 149.5) * 0.09) / 3)
    fitted, center = metrics._fit_normal(edge, normal)
    result = metrics._sfr(edge, fitted, center, 3.45, angular)
    assert result["status"] == "ok"
    assert result["mtf50_cycles_per_pixel"] > 0
    assert "cycles_per_degree" not in result["frequency"]
    assert result["mtf50_cycles_per_degree"] is None
    assert result["nyquist_cycles_per_degree"] is None


def test_angular_resampling_stays_in_original_roi_and_reports_interpolation():
    model, bounds = geometry(), [200, 296, 1400, 1496]
    yy, xx = np.indices((96, 96), dtype=float)
    normal = np.array([1, 0.087])
    edge = 0.4 + 0.3 * erf(((xx - 47.5) + (yy - 47.5) * normal[1]) / 2)
    result = metrics.angular_resampled_metrics(
        edge, gaussian_filter(edge, 1.2), geometry=model, bounds=bounds, normal_xy=normal
    )
    assert result["status"] == "ok"
    assert result["all_samples_inside_original_roi"]
    actual = result["source_pixel_center_bounds_row_col"]
    assert bounds[0] <= actual[0] <= actual[1] <= bounds[1] - 1
    assert bounds[2] <= actual[2] <= actual[3] <= bounds[3] - 1
    assert result["maximum_tangent_chart_metric_variation"] < 0.01
    assert result["linear_system"]["status"] == "ok"
    assert result["linear_system"]["native_pixels_interpolated"]
    assert list(result["linear_system"]["frequency"]) == ["cycles_per_degree"]
    assert (
        result["processed_system"]["mtf50_cycles_per_degree"]
        < (result["linear_system"]["mtf50_cycles_per_degree"])
    )


@pytest.fixture(scope="module")
def nine_site_result():
    model = geometry()
    # Independent nonoverlapping native straight edges test the estimator's
    # known Gaussian response. Production angular-scene layout is tested
    # separately and can have curved projected edges.
    sites = {}
    angle = np.radians(5)
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    for ri, row in enumerate((238, 540, 842), 1):
        for ci, col in enumerate((384, 960, 1536), 1):
            radial = np.array([col - 960, row - 540], dtype=float)
            radial = radial / np.linalg.norm(radial) if np.linalg.norm(radial) else np.array([1, 0])
            site = {"center_row_col": [row, col]}
            for orientation, normal, offset in (
                ("radial", radial, -61),
                ("tangential", np.array([-radial[1], radial[0]]), 61),
            ):
                site[orientation] = {
                    "bounds_row_col": [row + offset - 48, row + offset + 48, col - 48, col + 48],
                    "normal_xy": (rotation @ normal).tolist(),
                }
            sites[f"row{ri}_col{ci}"] = site
    image = np.full(model["native_shape"], 0.4)
    sigma = 1.3
    for site in sites.values():
        for orientation in ("radial", "tangential"):
            edge = site[orientation]
            r0, r1, c0, c1 = edge["bounds_row_col"]
            normal = np.asarray(edge["normal_xy"])
            yy, xx = np.indices((r1 - r0, c1 - c0), dtype=float)
            distance = (xx - (c1 - c0 - 1) / 2) * normal[0] + (yy - (r1 - r0 - 1) / 2) * normal[1]
            image[r0:r1, c0:c1] = 0.4 + 0.3 * erf(distance / (sigma * np.sqrt(2)))
    return metrics.resolution_metrics(
        image, gaussian_filter(image, 1.5), sites=sites, geometry=model
    )


def test_nine_sites_native_gaussian_response_and_processed_blur_trend(nine_site_result):
    result = nine_site_result
    assert result["schema"] == metrics.SCHEMA
    assert len(result["sites"]) == 9
    assert "row2_col2" in result["sites"] and "center" not in result["sites"]
    expected = np.sqrt(2 * np.log(2)) / (2 * np.pi * 1.3)
    angular_diagnostics = 0
    for site in result["sites"].values():
        for direction in ("radial", "tangential"):
            edge = site[direction]
            linear, processed = edge["linear_system"], edge["processed_system"]
            assert linear["status"] == "ok"
            assert processed["status"] == "ok"
            assert linear["mtf50_cycles_per_pixel"] == pytest.approx(expected, rel=0.08)
            assert processed["mtf50_cycles_per_pixel"] < linear["mtf50_cycles_per_pixel"] * 0.8
            assert linear["native_pixels_interpolated"] is False
            assert linear["frequency"]["cycles_per_pixel"][-1] == 0.5
            if not edge["angular_geometry"]["angular_frequency_qualified"]:
                angular_diagnostics += 1
                assert edge["angular_resampled"]["status"] == "ok"
                assert edge["angular_resampled"]["linear_system"]["status"] == "ok"
    assert angular_diagnostics > 0
    json.dumps(result, allow_nan=False)


def test_invalid_projection_and_mismatched_native_array_are_rejected():
    with pytest.raises(ValueError, match="projection"):
        metrics.pixel_to_direction({**geometry(), "projection": "generic_fisheye"}, 1, 1)
    with pytest.raises(ValueError, match="full native"):
        metrics.resolution_metrics(
            np.zeros((360, 640)), np.zeros((360, 640)), sites={}, geometry=geometry()
        )
