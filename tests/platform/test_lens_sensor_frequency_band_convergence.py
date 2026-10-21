"""Numerical MTF curve comparison without physical-accuracy promotion."""

from __future__ import annotations

import pytest

from camera_platform.lens_sensor_frequency_band_convergence import (
    compare_frequency_band_mtf_curves,
)


def _compare(**overrides):
    kwargs = {
        "target_frequency_cycles_per_mm": [250.0, 500.0, 750.0],
        "native_nyquist_cycles_per_mm": 819.67,
        "maximum_absolute_mtf_delta": 0.02,
        "coarse_normalized_signal_contrast": 0.8,
        "fine_normalized_signal_contrast": 0.8,
    }
    kwargs.update(overrides)
    return compare_frequency_band_mtf_curves(
        [0.0, 250.0, 500.0, 750.0, 800.0],
        [1.0, 0.8, 0.6, 0.4, 0.35],
        [0.0, 200.0, 400.0, 600.0, 800.0],
        [1.0, 0.84, 0.68, 0.52, 0.36],
        **kwargs,
    )


def test_interpolates_each_curve_within_support_at_declared_sub_nyquist_targets():
    result = _compare()
    assert result["status"] == "within_declared_target_tolerance_model_only_single_step"
    assert result["comparison"]["coarse_mtf_at_targets"] == pytest.approx([0.8, 0.6, 0.4])
    assert result["comparison"]["fine_mtf_at_targets"] == pytest.approx([0.8, 0.6, 0.4])
    assert result["comparison"]["maximum_absolute_mtf_delta"] == pytest.approx(0)
    assert result["numerical_sampling_convergence_established"] is False
    assert result["physical_mtf_comparison_allowed"] is False
    assert result["phase_or_aliasing_assessed"] is False


def test_high_frequency_difference_is_not_hidden_by_same_low_band_mtf50():
    result = compare_frequency_band_mtf_curves(
        [0, 250, 500, 750, 800], [1, 0.8, 0.5, 0.32, 0.30],
        [0, 250, 500, 750, 800], [1, 0.8, 0.5, 0.20, 0.16],
        target_frequency_cycles_per_mm=[250, 500, 750],
        native_nyquist_cycles_per_mm=819.67,
        maximum_absolute_mtf_delta=0.02,
        coarse_normalized_signal_contrast=0.8,
        fine_normalized_signal_contrast=0.8,
    )
    assert result["status"] == "outside_declared_target_tolerance_model_only_single_step"
    assert result["comparison"]["maximum_absolute_mtf_delta"] == pytest.approx(0.12)
    assert result["within_declared_single_step_tolerance"] is False


def test_identical_normalized_curves_with_low_signal_are_inconclusive():
    result = _compare(coarse_normalized_signal_contrast=0.01)
    assert result["status"] == "inconclusive_low_signal_contrast"
    assert "comparison" not in result


def test_never_extrapolates_past_measured_frequency_support():
    result = _compare(target_frequency_cycles_per_mm=[250.0, 500.0, 810.0])
    assert result["status"] == "inconclusive_insufficient_frequency_support"
    assert "comparison" not in result


@pytest.mark.parametrize("targets", [[250, 819.67], [250, 900], [500, 250], [0, 250]])
def test_nyquist_and_malformed_target_frequencies_are_rejected(targets):
    with pytest.raises(ValueError, match="below native Nyquist"):
        _compare(target_frequency_cycles_per_mm=targets)


def test_malformed_curve_and_nonfinite_contrast_are_rejected():
    with pytest.raises(ValueError, match="Coarse curve"):
        compare_frequency_band_mtf_curves(
            [0, 250, 250], [1, 0.7, 0.6],
            [0, 250, 500], [1, 0.7, 0.5],
            target_frequency_cycles_per_mm=[250],
            native_nyquist_cycles_per_mm=819.67,
            maximum_absolute_mtf_delta=0.02,
            coarse_normalized_signal_contrast=0.8,
            fine_normalized_signal_contrast=0.8,
        )
    with pytest.raises(ValueError, match="Contrasts"):
        _compare(fine_normalized_signal_contrast=float("nan"))
