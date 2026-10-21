"""Focused checks for the opt-in geometric ray re-histogram contract."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

from camera_platform.lens_geometric_psf_rehistogram import (
    _array_sha,
    _histogram,
    _json_sha,
    _mass_metrics,
    _metric_deltas,
    _rotated_points,
    _strict_style_interpolation,
    _target_side,
    rehistogram_pinned_geometric_psf,
)


def test_same_intersections_rehistogram_at_two_pitches_keeps_mass_and_centroid():
    rng = np.random.default_rng(31)
    points = rng.normal(size=(200, 2)) * [0.011, 0.006] + [0.025, -0.014]
    source, source_retained = _histogram(points, 64, 0.005, min_retained=0.999)
    side = _target_side(64, 0.005, 0.0025)
    target, target_retained = _histogram(points, side, 0.0025, min_retained=0.999)
    assert side == 127
    assert source_retained == pytest.approx(1.0)
    assert target_retained == pytest.approx(1.0)
    assert source.sum() == pytest.approx(1.0)
    assert target.sum() == pytest.approx(1.0)
    assert _mass_metrics(source, 0.005)["centroid_um_xy"] == pytest.approx(
        _mass_metrics(target, 0.0025)["centroid_um_xy"], abs=1e-9
    )


def test_out_of_support_rays_cannot_be_hidden_by_unit_sum():
    points = np.zeros((100, 2))
    points[-1] = [1.0, 1.0]
    with pytest.raises(ValueError, match="retains"):
        _histogram(points, 31, 0.01, min_retained=0.999)
    source, fraction = _histogram(points, 31, 0.01, min_retained=0.98)
    assert source.sum() == pytest.approx(1.0)
    assert fraction == pytest.approx(0.99)


def test_target_pitch_and_rotation_are_distinct_from_strict_interpolation():
    rng = np.random.default_rng(3)
    points = rng.normal(size=(1000, 2)) * [0.002, 0.001] + [0.035, 0.018]
    source, _ = _histogram(points, 64, 0.005, min_retained=0.999)
    target_side = _target_side(64, 0.005, 0.0025)
    target, _ = _histogram(points, target_side, 0.0025, min_retained=0.999)
    current, _ = _strict_style_interpolation(source, 0.005, 0.0025, target_side)
    assert target.sum() == pytest.approx(1.0)
    assert current.sum() == pytest.approx(1.0)
    assert np.abs(target - current).sum() > 0.05
    direct_rotated, _ = _histogram(
        _rotated_points(points, 45.0), target_side, 0.0025, min_retained=0.999
    )
    current_rotated, retained_stored = _strict_style_interpolation(
        source, 0.005, 0.0025, target_side, azimuth_deg=45.0
    )
    assert direct_rotated.sum() == pytest.approx(1.0)
    assert current_rotated.sum() == pytest.approx(1.0)
    # Bilinear rotate can even exceed unit mass before normalization; this is
    # a sampled-grid ratio, not a physical retained-ray fraction.
    assert retained_stored > 0
    assert np.abs(direct_rotated - current_rotated).sum() > 0.05


def test_hash_contract_changes_with_array_shape_dtype_pitch_and_source():
    values = np.arange(16, dtype=np.float32).reshape(4, 4)
    array_hash = _array_sha(values, "<f4")
    assert array_hash != _array_sha(values.reshape(2, 8), "<f4")
    assert array_hash != _array_sha(values, "<f8")
    base = {"array": array_hash, "pitch_mm": 0.001, "source_sha256": "a" * 64}
    assert _json_sha(base) != _json_sha({**base, "pitch_mm": 0.0005})
    assert _json_sha(base) != _json_sha({**base, "source_sha256": "b" * 64})
    assert _json_sha({"object_distance_m": float("inf")})
    with pytest.raises(ValueError, match="2049"):
        _target_side(512, 0.001, 0.00001)


def test_target_grid_mtf_deltas_exclude_above_nyquist_sampled_response():
    impulse = np.zeros((9, 9), dtype=float)
    impulse[4, 4] = 1.0
    other = impulse.copy()
    other[4, 4] = 0.5
    other[4, 5] = 0.5
    reference = _mass_metrics(impulse, 0.001151)
    changed = _mass_metrics(other, 0.001151)
    assert reference["sampled_grid_nyquist_cycles_per_mm"] < 500
    assert reference["mtf_frequency_status"][-1] == "aliased_discrete_response"
    deltas = _metric_deltas(reference, changed)
    assert deltas["mtf_frequencies_compared_cycles_per_mm"] == [100.0, 250.0]
    assert deltas["mtf_max_abs"] > 0


@pytest.mark.skipif(
    os.environ.get("CAMERA_TEST_PINNED_RAY_REHISTOGRAM") != "1",
    reason="Run explicitly with the local pinned Lens DB and RayOptics extra",
)
def test_full_pinned_p0014_field_band_grid_reproduces_source_and_hashes():
    root = Path(__file__).resolve().parents[2] / "camerae2e_db"
    result = rehistogram_pinned_geometric_psf(
        "6c48e967-74b4-5901-8186-ba6fafe4797b",
        catalog_root=root, convolution_pitch_um=1.151213026686389,
    )
    evidence = result.provenance
    json.dumps(evidence, allow_nan=False)
    assert evidence["field_wavelength_node_count"] == 15
    assert evidence["worst_source_grid_max_abs_cell_error"] < 2e-7
    assert evidence["worst_source_grid_l1_mass_error"] < 1e-4
    assert all(node["source_grid_retained_ray_fraction"] >= 0.999
               and node["target_grid_retained_ray_fraction"] >= 0.999
               for node in evidence["node_reports"])
    assert (
        result.optics["raytrace"]["psf"]["derived_kernel_sha256"]
        == evidence["derived_psf_signature_sha256"]
    )
    assert result.optics["raytrace"]["asset_sha256"] == evidence["source_psf_asset_sha256"]
    assert result.optics["raytrace"]["computation"]["psf_spacing_m"] == pytest.approx(
        evidence["derived_computation_psf_spacing_m"]
    )
    assert result.optics["raytrace"]["computation"]["psf_spacing_m"] == pytest.approx(
        evidence["declared_optical_convolution_pitch_um"] * 1e-6
    )
    assert evidence["azimuth_rotation_qualified"] is False
    assert evidence["physical_mtf_comparison_allowed"] is False
    diagnostic = evidence["selected_off_axis_45_degree_rotation_diagnostic"]
    assert diagnostic["direct_vs_v5_stored_interpolation_l1_mass_difference"] > 0
    assert diagnostic["direct_vs_v8_derived_rotation_l1_mass_difference"] > 0
    assert diagnostic["qualified"] is False
