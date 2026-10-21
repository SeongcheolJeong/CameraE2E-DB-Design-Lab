"""Finite pre-pixel histogram OTF behavior and scope boundaries."""

from __future__ import annotations

import math

import numpy as np
import pytest

from camera_platform.lens_sensor_psf_frequency_diagnostic import (
    psf_axis_target_frequency_diagnostic,
)


def test_analytic_gaussian_like_histogram_axis_mtf_and_signed_phase():
    # Five-plus-sigma support makes finite-window error negligible here.
    rows, cols = np.indices((241, 241), dtype=float)
    pitch_um = 0.05
    y_um = (120 - rows) * pitch_um
    x_um = (cols - 120) * pitch_um
    cy_um, cx_um = 0.3, 0.4
    sigma_y_um, sigma_x_um = 0.6, 0.8
    psf = np.exp(-0.5 * (
        ((y_um - cy_um) / sigma_y_um) ** 2
        + ((x_um - cx_um) / sigma_x_um) ** 2
    ))
    frequencies = [100.0, 250.0]
    result = psf_axis_target_frequency_diagnostic(
        psf,
        pitch_um_row_col=[pitch_um, pitch_um],
        target_frequency_cycles_per_mm_row_col=[frequencies, frequencies],
    )
    assert result["status"] == "numeric_supplied_histogram_axis_otf_only"
    assert result["centroid_um_row_y_up_col_x_right"] == pytest.approx([cy_um, cx_um], abs=1e-8)
    assert result["dc_normalized_mass"] == pytest.approx(1.0)
    for axis, sigma_um, offset_um in (
        ("row_y_up", sigma_y_um, cy_um),
        ("col_x_right", sigma_x_um, cx_um),
    ):
        for point in result["axis_otf"][axis]:
            f_cyc_per_um = point["frequency_cycles_per_mm"] / 1000
            expected_magnitude = math.exp(-2 * math.pi**2 * sigma_um**2 * f_cyc_per_um**2)
            expected_phase = -2 * math.pi * offset_um * f_cyc_per_um
            assert point["magnitude"] == pytest.approx(expected_magnitude, abs=1e-8)
            assert point["phase_rad"] == pytest.approx(expected_phase, abs=1e-8)
            assert point["phase_defined"] is True
    assert result["physical_mtf_comparison_allowed"] is False
    assert result["psf_source_convergence_established"] is False


def test_positive_y_up_row_shift_has_negative_otf_phase():
    psf = np.zeros((5, 5))
    psf[1, 2] = 2.0  # one row above center => y=+1 um
    result = psf_axis_target_frequency_diagnostic(
        psf,
        pitch_um_row_col=[1.0, 1.0],
        target_frequency_cycles_per_mm_row_col=[[250.0], [250.0]],
    )
    assert result["centroid_um_row_y_up_col_x_right"] == pytest.approx([1.0, 0.0])
    row = result["axis_otf"]["row_y_up"][0]
    col = result["axis_otf"]["col_x_right"][0]
    assert row["magnitude"] == pytest.approx(1.0)
    assert row["phase_rad"] == pytest.approx(-math.pi / 2)
    assert col["phase_rad"] == pytest.approx(0.0)


def test_sensor_nyquist_is_allowed_only_when_below_histogram_grid_nyquist():
    psf = np.ones((5, 5))
    supported = psf_axis_target_frequency_diagnostic(
        psf,
        pitch_um_row_col=[0.5, 0.5],
        target_frequency_cycles_per_mm_row_col=[[500.0], [500.0]],
    )
    assert supported["status"] == "numeric_supplied_histogram_axis_otf_only"
    blocked = psf_axis_target_frequency_diagnostic(
        psf,
        pitch_um_row_col=[1.0, 0.5],
        target_frequency_cycles_per_mm_row_col=[[500.0], [500.0]],
    )
    assert blocked["status"] == "inconclusive_target_at_or_above_psf_grid_nyquist"
    assert "axis_otf" not in blocked


def test_phase_is_undefined_when_axis_otf_magnitude_cancels():
    psf = np.zeros((5, 5))
    psf[2, 1] = psf[2, 3] = 1.0
    result = psf_axis_target_frequency_diagnostic(
        psf,
        pitch_um_row_col=[1.0, 1.0],
        target_frequency_cycles_per_mm_row_col=[[250.0], [250.0]],
    )
    column = result["axis_otf"]["col_x_right"][0]
    assert column["magnitude"] == pytest.approx(0.0, abs=1e-12)
    assert column["phase_rad"] is None
    assert column["phase_defined"] is False


@pytest.mark.parametrize("invalid", [np.zeros((3, 3)), [[1, -1], [0, 0]], [[1, math.nan]]])
def test_invalid_or_zero_mass_histogram_rejected(invalid):
    with pytest.raises(ValueError, match="PSF histogram"):
        psf_axis_target_frequency_diagnostic(
            invalid,
            pitch_um_row_col=[1.0, 1.0],
            target_frequency_cycles_per_mm_row_col=[[100.0], [100.0]],
        )
