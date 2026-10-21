"""Strict OI-grid kernel fidelity stays separate from source and camera MTF."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.ndimage import map_coordinates, rotate

from camera_platform.lens_executed_kernel_otf_diagnostic import (
    _kernel_axis_otf,
    _strict_kernel,
    diagnose_executed_kernel_vs_admitted_rays,
)
from camera_platform.lens_geometric_psf_rehistogram import _histogram
from pyisetcam.optics import (
    _raytrace_psf_grid,
    _raytrace_psf_support_axes,
    _strict_raytrace_spectral,
)


def _inputs(points, *, side=9, source_pitch_mm=0.001):
    planes = {}
    rays = {}
    for name, factor in (("source", 1), ("q2", 2), ("q4", 4)):
        plane, retained = _histogram(
            np.asarray(points, dtype=float), (side - 1) * factor + 1,
            source_pitch_mm / factor, min_retained=1.0 - 1e-12,
        )
        assert retained == pytest.approx(1.0)
        planes[name] = plane.astype(np.float32)
        rays[name] = np.asarray(points, dtype=float)
    return planes, rays


def _run(planes, rays, *, oi_pitch_um=1.0, frequencies=((100.0, 200.0), (100.0, 200.0))):
    return diagnose_executed_kernel_vs_admitted_rays(
        planes, rays,
        physical_source_pitch_mm_row_col=(0.001, 0.001),
        physical_source_support_shape_row_col=planes["source"].shape,
        stored_field_index=3,
        stored_wavelength_nm=550.0,
        executed_oi_pitch_um=oi_pitch_um,
        target_frequency_cycles_per_mm_row_col=frequencies,
    )


def test_strict_pixel_center_alignment_matches_direct_ray_complex_phase():
    planes, rays = _inputs([[0.001, 0.001]])
    report = _run(planes, rays)
    assert report["status"] == (
        "numeric_executed_kernel_vs_ray_otf_selected_node_angle_zero_only"
    )
    assert report["physical_support_shape_row_col_by_level"] == {
        "source": [9, 9], "q2": [17, 17], "q4": [33, 33],
    }
    assert report["physical_pitch_mm_row_col_by_level"] == {
        "source": [0.001, 0.001], "q2": [0.0005, 0.0005],
        "q4": [0.00025, 0.00025],
    }
    assert report["executed_kernel_shape_row_col_by_level"] == {
        "source": [9, 9], "q2": [9, 9], "q4": [9, 9],
    }
    source = report["level_reports"]["source"]
    assert source["maximum_complex_otf_abs_gap"] < 1e-6
    assert source["executed_complex_otf_row_y_up"][0][1] < 0
    assert source["executed_complex_otf_col_x_right"][0][1] < 0
    assert source["direct_ray_complex_otf_row_y_up"][0] == pytest.approx(
        source["executed_complex_otf_row_y_up"][0]
    )
    assert report["stored_field_index"] == 3
    assert report["stored_wavelength_nm"] == 550.0


def test_executed_oi_pitch_changes_interpolated_kernel_and_otf_gap():
    planes, rays = _inputs([[0.0002, 0.0]])
    native = _run(planes, rays, oi_pitch_um=1.0)
    refined = _run(planes, rays, oi_pitch_um=0.5)
    assert native["executed_kernel_shape_row_col_by_level"]["source"] == [9, 9]
    assert refined["executed_kernel_shape_row_col_by_level"]["source"] == [17, 17]
    assert native["level_reports"]["source"]["executed_kernel_sha256"] != (
        refined["level_reports"]["source"]["executed_kernel_sha256"]
    )
    assert native["level_reports"]["source"]["maximum_complex_otf_abs_gap"] != pytest.approx(
        refined["level_reports"]["source"]["maximum_complex_otf_abs_gap"], abs=1e-8
    )
    assert native["level_reports"]["source"]["direct_ray_complex_otf_col_x_right"] == (
        refined["level_reports"]["source"]["direct_ray_complex_otf_col_x_right"]
    )


def test_unsupported_frequency_returns_inconclusive_without_comparison():
    planes, rays = _inputs([[0.0, 0.0]])
    report = _run(planes, rays, frequencies=((100.0, 600.0), (100.0, 200.0)))
    assert report["status"] == "inconclusive_target_outside_source_or_executed_grid_band"
    assert "level_reports" not in report
    assert report["physical_mtf_comparison_allowed"] is False


def test_three_zero_gaps_do_not_imply_pupil_or_scene_convergence():
    planes, rays = _inputs([[0.0, 0.0]])
    report = _run(planes, rays)
    assert report["all_levels_within_declared_gap_tolerance_selected_node_only"] is True
    assert report["maximum_complex_otf_abs_gap_all_levels"] < 1e-6
    assert report["pupil_density_convergence_established"] is False
    assert report["psf_bin_convergence_established"] is False
    assert report["scene_or_sensor_sampling_convergence_established"] is False
    assert report["sensor_or_raw_executed"] is False
    assert report["product_ranking_allowed"] is False


def test_mismatched_plane_or_rays_fail_closed():
    planes, rays = _inputs([[0.0, 0.0]])
    bad = dict(planes)
    bad["q4"] = planes["q4"][:-1]
    with pytest.raises(ValueError, match="exact physical support"):
        _run(bad, rays)
    bad = dict(planes)
    bad["q2"] = planes["source"].copy()
    with pytest.raises(ValueError, match="exact physical support"):
        _run(bad, rays)
    bad_rays = dict(rays)
    bad_rays["q2"] = np.array([[np.nan, 0.0]])
    with pytest.raises(ValueError, match="finite admitted"):
        _run(planes, bad_rays)


def test_even_source_has_8_15_29_physical_support_and_common_extent():
    planes, rays = _inputs([[0.0, 0.0]], side=8)
    report = _run(planes, rays)
    assert report["status"].startswith("numeric_")
    assert report["physical_support_shape_row_col_by_level"] == {
        "source": [8, 8], "q2": [15, 15], "q4": [29, 29],
    }
    for extent in report["physical_support_half_extent_mm_row_col_by_level"].values():
        assert extent == pytest.approx([0.0035, 0.0035])
    assert report["executed_kernel_shape_row_col_by_level"] == {
        "source": [9, 9], "q2": [9, 9], "q4": [9, 9],
    }


def test_valid_narrow_ray_psf_can_be_inconclusive_at_coarse_oi_grid():
    planes, rays = _inputs([[0.0005, 0.0]])
    report = _run(planes, rays, oi_pitch_um=1.0)
    assert report["status"] == "inconclusive_strict_kernel_zero_energy_on_oi_grid"
    assert report["blocked_level"] == "q2"
    assert report["physical_mtf_comparison_allowed"] is False


def test_strict_even_support_angle_zero_map_and_float32_parity():
    # Reproduce the strict optics primitive in independent test code: even
    # source pixels have half-pixel support, while the OI kernel is odd-sized.
    points = np.array([[-0.0005, 0.0005], [0.0005, 0.0005], [0.0015, -0.0005]])
    plane, _retained = _histogram(points, 8, 0.001, min_retained=1.0 - 1e-12)
    plane = plane.astype(np.float32)
    table = {
        "function": np.repeat(plane[:, :, None, None], 2, axis=2),
        "sample_spacing_mm": np.array([0.001, 0.001]),
        "coordinate_convention": "pixel_centers",
        "field_height_mm": np.array([0.0, 1.0]),
        "wavelength_nm": np.array([550.0]),
    }
    sx, sy = _raytrace_psf_support_axes(table)
    tx, ty = _raytrace_psf_grid(0.0006, table)
    rows = (ty - sy[0]) / (sy[1] - sy[0])
    cols = (tx - sx[0]) / (sx[1] - sx[0])
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    spectral_plane = np.asarray(plane, dtype=float)
    spectral_plane = spectral_plane / spectral_plane.sum(dtype=float)
    assert np.array_equal(
        _strict_raytrace_spectral(table, 550.0, psf=True)[:, :, 0], spectral_plane
    )
    rotated = rotate(
        spectral_plane, 0.0, reshape=False, order=1, mode="constant", cval=0.0,
        prefilter=False,
    )
    sampled = map_coordinates(
        rotated, [rr, cc], order=1, mode="constant", cval=0.0,
        prefilter=False,
    )
    expected = np.asarray(sampled / sampled.sum(), dtype=np.float32)
    actual, x_mm, y_mm, mass = _strict_kernel(plane, (0.001, 0.001), 0.0006)
    assert np.array_equal(actual, expected)
    assert actual.dtype == np.float32
    assert np.array_equal(x_mm, tx)
    assert np.array_equal(y_mm, ty)
    assert mass == pytest.approx(float(sampled.sum()), abs=0)
    row_otf, col_otf = _kernel_axis_otf(actual, x_mm, y_mm, (200.0,), (200.0,))
    yy, xx = np.meshgrid(y_mm, x_mm, indexing="ij")
    assert row_otf[0] == pytest.approx(np.sum(
        actual * np.exp(-2j * np.pi * 200.0 * yy)
    ))
    assert col_otf[0] == pytest.approx(np.sum(
        actual * np.exp(-2j * np.pi * 200.0 * xx)
    ))
