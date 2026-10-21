"""The re-histogram reference uses one ray set and refuses stale source PSFs."""

import numpy as np
import pytest

from tools.build_lens_patent_raytrace_psf_grid import histogram_psf
from tools.qualify_lens_psf_sampling import (
    _rotate_ray_points,
    compare_same_ray_histograms,
)


def _fixture():
    rng = np.random.default_rng(17)
    points = rng.normal(size=(300, 2)) * [0.035, 0.025] + [0.012, -0.008]
    source, retained = histogram_psf(
        points, 31, 0.02, min_retained_energy=0.999, return_retained=True,
    )
    assert retained == pytest.approx(1.0)
    return points, source


def test_same_ray_reference_reproduces_source_and_compares_target():
    points, source = _fixture()
    result = compare_same_ray_histograms(
        points, source, source_pitch_mm=0.02, target_pitch_mm=0.01,
        min_retained_energy=0.999,
    )
    assert result["same_successful_ray_points_for_source_and_target"] is True
    assert result["source_rehistogram_matches_pinned_asset"] is True
    assert result["target_psf_samples_row_col"] == [61, 61]
    assert result["source_rehistogram_retained_ray_fraction"] == pytest.approx(1.0)
    assert result["target_rehistogram_retained_ray_fraction"] == pytest.approx(1.0)
    assert result["reference_valid_for_pinned_source"] is True
    assert result["production_psf_resampling_qualified"] is False
    assert result["target_reference_vs_interpolation_l1_mass_difference"] > 0
    assert result["physical_mtf_comparison_allowed"] is False


def test_changed_stored_histogram_cannot_be_claimed_as_independent_reference():
    points, source = _fixture()
    altered = np.roll(source, 1, axis=1)
    result = compare_same_ray_histograms(
        points, altered, source_pitch_mm=0.02, target_pitch_mm=0.01,
        min_retained_energy=0.999,
    )
    assert result["source_rehistogram_matches_pinned_asset"] is False
    assert result["reference_valid_for_pinned_source"] is False
    assert result["production_psf_replaced"] is False


def test_direct_ray_rotation_and_stored_grid_rotation_share_orientation():
    rng = np.random.default_rng(8)
    points = rng.normal(size=(500, 2)) * 0.002 + [0.05, 0.02]
    source = histogram_psf(points, 81, 0.005)
    rotated_points = _rotate_ray_points(points, 90.0)
    assert rotated_points.mean(axis=0) == pytest.approx([-0.02, 0.05], abs=0.001)
    result = compare_same_ray_histograms(
        points, source, source_pitch_mm=0.005, target_pitch_mm=0.005,
        min_retained_energy=0.999, azimuth_deg=90.0,
    )
    direct = result["target_reference"]["centroid_um_xy"]
    interpolated = result["current_stored_histogram_interpolation"]["centroid_um_xy"]
    assert direct == pytest.approx(interpolated, abs=0.05)
    assert result["strict_rotation_retained_stored_mass_before_unit_sum"] == pytest.approx(1.0)
    assert result["production_psf_resampling_qualified"] is False


def test_unbounded_target_grid_is_rejected_before_allocation():
    points, source = _fixture()
    with pytest.raises(ValueError, match="2049-sample diagnostic limit"):
        compare_same_ray_histograms(
            points, source, source_pitch_mm=0.02, target_pitch_mm=0.00001,
            min_retained_energy=0.999,
        )
