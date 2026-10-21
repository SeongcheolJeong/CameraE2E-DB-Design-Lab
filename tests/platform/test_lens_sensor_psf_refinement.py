"""Target-band PSF histogram refinement and fail-closed source evidence."""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import pytest

from camera_platform.lens_sensor_psf_refinement import (
    compare_psf_target_band,
    prepare_pinned_geometric_psf_target_band,
)


def _gaussian(side: int, pitch_um: float) -> np.ndarray:
    rows, cols = np.indices((side, side), dtype=float)
    y_um = ((side - 1) / 2 - rows) * pitch_um
    x_um = (cols - (side - 1) / 2) * pitch_um
    return np.exp(-0.5 * (
        ((y_um - 0.3) / 0.6) ** 2 + ((x_um - 0.4) / 0.8) ** 2
    ))


def _compare(source: np.ndarray, fine: np.ndarray, **overrides: object) -> dict:
    inputs = {
        "stored_pitch_um_row_col": [0.1, 0.1],
        "refined_pitch_um_row_col": [0.05, 0.05],
        "target_frequencies_cycles_per_mm": [100.0, 250.0],
        "directions_deg": [0.0, 45.0, 90.0],
        "refinement_method": "pinned_prescription_retrace_same_rays",
        "source_retained_ray_fraction": 1.0,
        "refined_retained_ray_fraction": 1.0,
        "admitted_ray_count": 500,
        "requested_pupil_ray_count": 500,
        "source_reproduction_verified": True,
        "same_admitted_intersections_verified": True,
    }
    inputs.update(overrides)
    return compare_psf_target_band(source, fine, **inputs)


def test_same_physical_gaussian_finer_grid_has_2d_complex_otf_and_phase():
    source = _gaussian(121, 0.1)
    fine = _gaussian(241, 0.05)
    result = _compare(source, fine)
    assert result["histogram_grid_stability"] == "within_declared_tolerances"
    assert result["status"] == "inconclusive_psf_source_refinement_unavailable"
    assert result["psf_source_resolution_qualified"] is False
    assert result["pupil_ray_density_converged"] is False
    assert result["physical_mtf_comparison_allowed"] is False
    assert result["deltas"]["max_complex_otf_abs"] < 1e-8
    point = next(
        item for item in result["refined_histogram"]["dc_normalized_complex_otf"]
        if item["direction_deg_from_x_right_toward_y_up"] == 45.0
        and item["frequency_cycles_per_mm"] == 250.0
    )
    fx = 250 / math.sqrt(2) / 1000
    fy = fx
    expected_magnitude = math.exp(
        -2 * math.pi**2 * (0.8**2 * fx**2 + 0.6**2 * fy**2)
    )
    expected_phase = -2 * math.pi * (0.4 * fx + 0.3 * fy)
    assert point["magnitude"] == pytest.approx(expected_magnitude, abs=1e-8)
    assert point["phase_rad"] == pytest.approx(expected_phase, abs=1e-8)


def test_interpolated_stored_histogram_cannot_claim_independent_grid_evidence():
    source = _gaussian(121, 0.1)
    fine = _gaussian(241, 0.05)
    result = _compare(
        source, fine, refinement_method="stored_histogram_interpolation"
    )
    assert result["status"] == "inconclusive_stored_histogram_interpolation_only"
    assert result["histogram_grid_stability"] == "not_assessed"
    assert result["deltas"]["max_complex_otf_abs"] < 1e-8


def test_identical_integer_like_mass_can_hide_centroid_and_phase_difference():
    source = np.zeros((9, 9))
    fine = np.zeros((17, 17))
    source[4, 4] = 1
    fine[8, 10] = 1  # +0.1 um x shift on refined grid
    result = _compare(source, fine, max_centroid_shift_um=0.01)
    assert result["status"] == "inconclusive_histogram_grid_difference"
    assert result["histogram_grid_stability"] == "difference_above_declared_tolerances"
    assert result["deltas"]["centroid_shift_um"] == pytest.approx(0.1)
    assert result["deltas"]["max_wrapped_phase_rad"] > 0


def test_target_must_be_inside_both_psf_grid_nyquist_rectangles():
    source = np.zeros((9, 9))
    fine = np.zeros((17, 17))
    source[4, 4] = fine[8, 8] = 1
    result = _compare(
        source, fine,
        stored_pitch_um_row_col=[1.0, 1.0],
        refined_pitch_um_row_col=[0.5, 0.5],
        target_frequencies_cycles_per_mm=[500.0],
        directions_deg=[0.0],
    )
    assert result["status"] == "inconclusive_target_outside_psf_grid_band"
    # A diagonal 600 cycles/mm has 424 cycles/mm along either grid axis.
    diagonal = _compare(
        source, fine,
        stored_pitch_um_row_col=[1.0, 1.0],
        refined_pitch_um_row_col=[0.5, 0.5],
        target_frequencies_cycles_per_mm=[600.0],
        directions_deg=[45.0],
    )
    assert diagonal["histogram_grid_stability"] == "within_declared_tolerances"


def test_missing_ray_provenance_and_energy_support_remain_inconclusive():
    source = _gaussian(121, 0.1)
    fine = _gaussian(241, 0.05)
    unverified = _compare(source, fine, source_reproduction_verified=False)
    assert unverified["status"] == "inconclusive_pinned_retrace_provenance_unverified"
    clipped = _compare(source, fine, refined_retained_ray_fraction=0.95)
    assert clipped["status"] == "inconclusive_ray_or_energy_support_limit"
    few = _compare(source, fine, admitted_ray_count=20)
    assert few["status"] == "inconclusive_ray_or_energy_support_limit"
    absent = compare_psf_target_band(
        source, None,
        stored_pitch_um_row_col=[0.1, 0.1],
        refined_pitch_um_row_col=None,
        target_frequencies_cycles_per_mm=[100.0],
    )
    assert absent["status"] == "inconclusive_psf_source_refinement_unavailable"


def test_analysis_resource_cap_and_invalid_frequency_budget():
    source = _gaussian(121, 0.1)
    fine = _gaussian(241, 0.05)
    blocked = _compare(source, fine, max_analysis_bytes=1024)
    assert blocked["status"] == "inconclusive_psf_analysis_resource_limit"
    with pytest.raises(ValueError, match="direction/frequency points"):
        _compare(
            source, fine,
            target_frequencies_cycles_per_mm=list(range(1, 34)),
            directions_deg=[0.0, 90.0],
        )
    with pytest.raises(ValueError, match="finite nonnegative 2D PSF"):
        _compare(np.full((3, 3), 1 + 1j), fine)


@pytest.mark.skipif(
    os.environ.get("CAMERA_TEST_PINNED_RAY_REHISTOGRAM") != "1",
    reason="Run explicitly with local pinned Lens DB and RayOptics extra",
)
def test_full_pinned_retrace_reports_histogram_vs_source_resolution_separately():
    root = Path(__file__).resolve().parents[2] / "camerae2e_db"
    prepared = prepare_pinned_geometric_psf_target_band(
        "6c48e967-74b4-5901-8186-ba6fafe4797b",
        catalog_root=root,
        target_pitch_um=0.5756065133431946,
        field_height_mm=0.0,
        wavelength_nm=550.0,
        target_frequencies_cycles_per_mm=[100.0, 250.0],
    )
    result = prepared.evidence
    assert prepared.optics is not None
    assert prepared.optics["raytrace"]["psf"]["derivation"][
        "derived_psf_signature_sha256"
    ] == result["derived_psf_signature_sha256"]
    assert prepared.optics["raytrace"]["computation"]["psf_spacing_m"] == pytest.approx(
        0.5756065133431946 * 1e-6
    )
    assert result["status"].startswith("inconclusive_")
    assert result["psf_source_resolution_qualified"] is False
    assert result["independent_histogram_grid_comparison_executed"] is True
    assert result["source_psf_asset_sha256"]
    assert result["derived_psf_signature_sha256"]
