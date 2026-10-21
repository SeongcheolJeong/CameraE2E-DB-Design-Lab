"""Compare one pair of numerical MTF curves at declared sub-Nyquist targets.

This is one refinement step, not a convergence certificate.  The caller owns
the Scene, PSF, field position, orientation, phase, channel, and observable
contracts.  MTF magnitude alone cannot detect optical phase errors or sensor
aliasing.  A successful comparison never qualifies physical or product MTF.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

SCHEMA = "camera_lens_sensor_frequency_band_mtf_comparison_v1"


def _positive(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return number


def _curve(
    frequency: Sequence[float], mtf: Sequence[float], name: str,
) -> tuple[np.ndarray, np.ndarray]:
    frequencies = np.asarray(frequency, dtype=float)
    values = np.asarray(mtf, dtype=float)
    if (
        frequencies.ndim != 1 or values.ndim != 1
        or frequencies.size < 2 or values.size != frequencies.size
        or not np.isfinite(frequencies).all() or not np.isfinite(values).all()
        or frequencies[0] < 0 or np.any(np.diff(frequencies) <= 0)
        or np.any(values < 0)
    ):
        raise ValueError(
            f"{name} requires equal-length finite one-dimensional arrays, "
            "strictly increasing nonnegative frequencies, and nonnegative MTF."
        )
    return frequencies, values


def compare_frequency_band_mtf_curves(
    coarse_frequency_cycles_per_mm: Sequence[float],
    coarse_mtf: Sequence[float],
    fine_frequency_cycles_per_mm: Sequence[float],
    fine_mtf: Sequence[float],
    *,
    target_frequency_cycles_per_mm: Sequence[float],
    native_nyquist_cycles_per_mm: float,
    maximum_absolute_mtf_delta: float,
    coarse_normalized_signal_contrast: float,
    fine_normalized_signal_contrast: float,
    minimum_normalized_signal_contrast: float = 0.05,
) -> dict[str, Any]:
    """Interpolate within both curves and compare MTF only at declared targets.

    Both MTF curves must be DC-normalized by their producer.  A contrast value
    must be measured from each corresponding unnormalized output; a flat input
    can otherwise yield identical but meaningless normalized MTF curves.  The
    target frequencies must lie strictly below the native sampling Nyquist.
    For a Bayer or ISP output, the caller must define the channel and effective
    sampling contract before using that Nyquist value.
    """
    coarse_f, coarse_values = _curve(
        coarse_frequency_cycles_per_mm, coarse_mtf, "Coarse curve"
    )
    fine_f, fine_values = _curve(
        fine_frequency_cycles_per_mm, fine_mtf, "Fine curve"
    )
    target = np.asarray(target_frequency_cycles_per_mm, dtype=float)
    nyquist = _positive(native_nyquist_cycles_per_mm, "Native Nyquist")
    tolerance = float(maximum_absolute_mtf_delta)
    minimum_contrast = _positive(
        minimum_normalized_signal_contrast, "Minimum normalized signal contrast"
    )
    contrasts = (
        float(coarse_normalized_signal_contrast),
        float(fine_normalized_signal_contrast),
    )
    if (
        target.ndim != 1 or target.size == 0
        or not np.isfinite(target).all()
        or target[0] <= 0 or target[-1] >= nyquist
        or np.any(np.diff(target) <= 0)
    ):
        raise ValueError(
            "Target frequencies must be strictly increasing, finite, positive, "
            "and below native Nyquist."
        )
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("Maximum absolute MTF delta must be finite and nonnegative.")
    if minimum_contrast > 1 or any(
        not math.isfinite(contrast) or contrast < 0 for contrast in contrasts
    ):
        raise ValueError("Contrasts must be finite and nonnegative; minimum must be <= 1.")

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "inconclusive",
        "target_frequency_cycles_per_mm": target.tolist(),
        "native_nyquist_cycles_per_mm": nyquist,
        "frequency_fraction_of_native_nyquist": (target / nyquist).tolist(),
        "maximum_absolute_mtf_delta_allowed": tolerance,
        "normalized_signal_contrast": {
            "coarse": contrasts[0], "fine": contrasts[1],
            "minimum_required": minimum_contrast,
        },
        "curve_support_cycles_per_mm": {
            "coarse": [float(coarse_f[0]), float(coarse_f[-1])],
            "fine": [float(fine_f[0]), float(fine_f[-1])],
        },
        "interpolation": "linear_within_each_measured_frequency_support",
        "single_refinement_comparison_only": True,
        "numerical_sampling_convergence_established": False,
        "phase_or_aliasing_assessed": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
    }
    if min(contrasts) < minimum_contrast:
        result["status"] = "inconclusive_low_signal_contrast"
        return result
    if (
        target[0] < coarse_f[0] or target[-1] > coarse_f[-1]
        or target[0] < fine_f[0] or target[-1] > fine_f[-1]
    ):
        result["status"] = "inconclusive_insufficient_frequency_support"
        return result

    coarse_target = np.interp(target, coarse_f, coarse_values)
    fine_target = np.interp(target, fine_f, fine_values)
    absolute_delta = np.abs(fine_target - coarse_target)
    maximum_delta = float(np.max(absolute_delta))
    result["comparison"] = {
        "coarse_mtf_at_targets": coarse_target.tolist(),
        "fine_mtf_at_targets": fine_target.tolist(),
        "absolute_mtf_delta_at_targets": absolute_delta.tolist(),
        "maximum_absolute_mtf_delta": maximum_delta,
    }
    result["within_declared_single_step_tolerance"] = maximum_delta <= tolerance
    result["status"] = (
        "within_declared_target_tolerance_model_only_single_step"
        if result["within_declared_single_step_tolerance"]
        else "outside_declared_target_tolerance_model_only_single_step"
    )
    return result
