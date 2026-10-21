"""Axis OTF diagnostics for one supplied pre-pixel PSF histogram.

Each bin is treated as mass at its physical center.  The horizontal ``x``
coordinate increases with columns; the vertical ``y`` coordinate increases
upward, opposite the row index.  The result describes this finite histogram
only.  It cannot establish missing tail energy, ray/pupil/spectral convergence,
diffraction, pixel integration, system MTF, or physical lens accuracy.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

SCHEMA = "camera_lens_sensor_pre_pixel_psf_axis_otf_diagnostic_v1"
_PHASE_MAGNITUDE_FLOOR = 1e-12


def _positive_pitch(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return result


def _frequencies(values: Sequence[float], name: str) -> np.ndarray:
    result = np.asarray(values, dtype=float)
    if (
        result.ndim != 1 or result.size == 0
        or not np.isfinite(result).all() or result[0] <= 0
        or np.any(np.diff(result) <= 0)
    ):
        raise ValueError(f"{name} requires increasing finite positive frequencies.")
    return result


def _axis_otf(
    normalized_profile: np.ndarray,
    coordinates_mm: np.ndarray,
    frequencies_cycles_per_mm: np.ndarray,
) -> list[dict[str, Any]]:
    results = []
    for frequency in frequencies_cycles_per_mm:
        value = complex(np.sum(
            normalized_profile * np.exp(-2j * math.pi * frequency * coordinates_mm)
        ))
        magnitude = abs(value)
        results.append({
            "frequency_cycles_per_mm": float(frequency),
            "complex_otf_real": float(value.real),
            "complex_otf_imag": float(value.imag),
            "magnitude": float(magnitude),
            "phase_rad": float(np.angle(value)) if magnitude > _PHASE_MAGNITUDE_FLOOR else None,
            "phase_defined": magnitude > _PHASE_MAGNITUDE_FLOOR,
        })
    return results


def psf_axis_target_frequency_diagnostic(
    psf_histogram: np.ndarray | Sequence[Sequence[float]],
    *,
    pitch_um_row_col: Sequence[float],
    target_frequency_cycles_per_mm_row_col: Sequence[Sequence[float]],
) -> dict[str, Any]:
    """Evaluate DC-normalized complex axis OTF below each histogram-grid Nyquist.

    ``row`` target frequencies probe the ``+y up`` axis and ``col`` target
    frequencies probe the ``+x right`` axis.  A target at the *sensor* Nyquist
    is permitted if it remains strictly below this PSF histogram's own grid
    Nyquist.  The output does not include the sensor pixel aperture.
    """
    histogram = np.asarray(psf_histogram, dtype=float)
    if (
        histogram.ndim != 2 or histogram.size == 0
        or not np.isfinite(histogram).all() or np.any(histogram < 0)
    ):
        raise ValueError("PSF histogram must be a finite nonnegative 2D array.")
    input_mass = float(np.sum(histogram))
    if not math.isfinite(input_mass) or input_mass <= 0:
        raise ValueError("PSF histogram must have finite positive input mass.")
    if len(pitch_um_row_col) != 2 or len(target_frequency_cycles_per_mm_row_col) != 2:
        raise ValueError("Provide separate row and column pitches and frequency targets.")
    row_pitch_um = _positive_pitch(pitch_um_row_col[0], "Row pitch")
    col_pitch_um = _positive_pitch(pitch_um_row_col[1], "Column pitch")
    row_targets = _frequencies(
        target_frequency_cycles_per_mm_row_col[0], "Row target"
    )
    col_targets = _frequencies(
        target_frequency_cycles_per_mm_row_col[1], "Column target"
    )
    nyquist_row = 500.0 / row_pitch_um
    nyquist_col = 500.0 / col_pitch_um

    normalized = histogram / input_mass
    row_y_um = (
        (histogram.shape[0] - 1) / 2 - np.arange(histogram.shape[0])
    ) * row_pitch_um
    col_x_um = (
        np.arange(histogram.shape[1]) - (histogram.shape[1] - 1) / 2
    ) * col_pitch_um
    row_profile = np.sum(normalized, axis=1)
    col_profile = np.sum(normalized, axis=0)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "inconclusive",
        "scope": "supplied_finite_pre_pixel_histogram_only",
        "histogram_shape_row_col": list(histogram.shape),
        "pitch_um_row_col": [row_pitch_um, col_pitch_um],
        "histogram_grid_nyquist_cycles_per_mm_row_col": [nyquist_row, nyquist_col],
        "target_frequency_cycles_per_mm_row_col": [
            row_targets.tolist(), col_targets.tolist(),
        ],
        "input_mass": input_mass,
        "dc_normalized_mass": float(np.sum(normalized)),
        "centroid_um_row_y_up_col_x_right": [
            float(np.sum(row_profile * row_y_um)),
            float(np.sum(col_profile * col_x_um)),
        ],
        "otf_convention": "sum(normalized_bin_mass * exp(-2pi*i*(fx*x_right+fy*y_up)))",
        "histogram_bins_treated_as_point_mass_at_centers": True,
        "phase_wrapped_to_minus_pi_pi": True,
        "pixel_aperture_included": False,
        "unobserved_psf_tail_energy_assessed": False,
        "psf_source_convergence_established": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
    }
    if row_targets[-1] >= nyquist_row or col_targets[-1] >= nyquist_col:
        result["status"] = "inconclusive_target_at_or_above_psf_grid_nyquist"
        return result

    result["axis_otf"] = {
        "row_y_up": _axis_otf(row_profile, row_y_um * 1e-3, row_targets),
        "col_x_right": _axis_otf(col_profile, col_x_um * 1e-3, col_targets),
    }
    result["status"] = "numeric_supplied_histogram_axis_otf_only"
    return result
