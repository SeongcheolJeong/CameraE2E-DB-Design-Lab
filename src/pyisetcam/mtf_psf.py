"""Measured radial MTF -> constrained PSF with explicit round-trip diagnostics.

MTF does not determine phase. This module constructs a zero-phase, radially
symmetric approximation on a finite square grid, not a uniquely recovered lens.
No pixel aperture, CFA, or ISP effect is added or removed by these functions.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import numpy as np


def _real_array(value: Any, name: str, ndim: int) -> np.ndarray:
    raw = np.asarray(value)
    if raw.dtype.kind not in "iuf" or raw.ndim != ndim:
        raise ValueError(f"{name} must be a real numeric {ndim}-D array.")
    result = np.asarray(raw, dtype=float)
    if not np.isfinite(result).all():
        raise ValueError(f"{name} must be finite.")
    return result


def _positive(value: float, name: str, *, zero: bool = False) -> float:
    if isinstance(value, (bool, np.bool_)) or not np.isscalar(value):
        raise ValueError(f"{name} must be a finite scalar.")
    result = float(value)
    if not np.isfinite(result) or result < 0 or (not zero and result == 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}.")
    return result


def _frequency_scale(
    frequency_unit: str, pitch_um: float, measurement_pixel_pitch_um: float | None = None,
) -> float:
    if frequency_unit == "cycles/mm":
        if measurement_pixel_pitch_um is not None:
            raise ValueError("measurement_pixel_pitch_um is only used with cycles/pixel.")
        return 1.0
    if frequency_unit == "cycles/pixel":
        measurement_pitch = (
            pitch_um if measurement_pixel_pitch_um is None
            else _positive(measurement_pixel_pitch_um, "measurement_pixel_pitch_um")
        )
        return 1000.0 / measurement_pitch
    raise ValueError("frequency_unit must be 'cycles/mm' or 'cycles/pixel'.")


def _simplex_projection(values: np.ndarray) -> np.ndarray:
    """Euclidean projection onto nonnegative arrays with unit total mass.

    By Parseval, this also minimizes full-grid squared complex-OTF change.
    It does not minimize error only at the supplied measurement frequencies.
    """
    ordered = np.sort(values.ravel())[::-1]
    with np.errstate(over="ignore", invalid="ignore"):
        offsets = (np.cumsum(ordered) - 1.0) / np.arange(1, ordered.size + 1)
    active = np.flatnonzero(ordered > offsets)
    if not np.isfinite(offsets).all() or not active.size:
        raise ValueError("OTF inverse exceeds the numerical range for unit-sum projection.")
    projected = np.maximum(values - offsets[active[-1]], 0.0)
    return projected / projected.sum()


def otf_to_psf(
    otf: Any,
    *,
    negative_policy: str = "raise",
    negative_tolerance: float = 1e-12,
    imaginary_tolerance: float = 1e-10,
) -> dict[str, Any]:
    """Invert an unshifted FFT-order OTF, preserving phase and checking reality.

    Positive real DC is normalized to one. Significant imaginary content always
    raises. Significant negative intensity raises unless ``project`` is explicit;
    projection returns a nonnegative unit-sum PSF and reports the change. Tiny
    negative roundoff is clipped, never reflected with abs(). Tolerances are
    absolute in the unit-sum PSF. Output zero displacement is at shape // 2.
    """
    if negative_policy not in {"raise", "project"}:
        raise ValueError("negative_policy must be 'raise' or 'project'.")
    negative_tolerance = _positive(negative_tolerance, "negative_tolerance", zero=True)
    imaginary_tolerance = _positive(imaginary_tolerance, "imaginary_tolerance", zero=True)
    raw = np.asarray(otf)
    if raw.dtype.kind not in "iufc" or raw.ndim != 2 or min(raw.shape) < 2:
        raise ValueError("otf must be a finite numeric 2-D array, at least 2 x 2.")
    transfer = np.asarray(raw, dtype=complex)
    if not np.isfinite(transfer).all():
        raise ValueError("otf must be finite.")
    dc = transfer[0, 0]
    if dc.real <= 0 or abs(dc.imag) > imaginary_tolerance * dc.real:
        raise ValueError("otf DC must be positive and real.")
    with np.errstate(over="ignore", invalid="ignore"):
        normalized = transfer / dc.real
    if not np.isfinite(normalized).all():
        raise ValueError("OTF normalization exceeds the finite numerical range.")
    inverse = np.fft.fftshift(np.fft.ifft2(normalized))
    if not np.isfinite(inverse).all():
        raise ValueError("OTF inverse must be finite.")
    imaginary = float(np.max(np.abs(inverse.imag)))
    if imaginary > imaginary_tolerance:
        raise ValueError(
            "OTF has no real PSF within imaginary_tolerance; check Hermitian symmetry "
            f"and FFT ordering (imaginary maximum {imaginary:.6g})."
        )
    signed = inverse.real
    if not np.isfinite(np.abs(signed).sum()):
        raise ValueError("OTF inverse energy exceeds the finite numerical range.")
    negative_mass = float(-np.minimum(signed, 0).sum())
    minimum = float(signed.min())
    significant = minimum < -negative_tolerance
    if significant and negative_policy == "raise":
        raise ValueError(
            f"OTF produces negative PSF intensity (minimum {minimum:.6g}, "
            f"negative mass {negative_mass:.6g}); choose negative_policy='project' "
            "to obtain an explicitly modified approximation."
        )
    if significant:
        psf = _simplex_projection(signed)
    else:
        psf = np.maximum(signed, 0)
        psf = psf / psf.sum()
    edge = np.ones(psf.shape, dtype=bool)
    edge[1:-1, 1:-1] = False
    reconstructed = np.fft.fft2(np.fft.ifftshift(psf))
    return {
        "psf": psf,
        "diagnostics": {
            "input_dc": float(dc.real),
            "minimum_before_projection": minimum,
            "negative_mass": negative_mass,
            "negative_mass_fraction": negative_mass / float(np.abs(signed).sum()),
            "imaginary_max_abs": imaginary,
            "projection_applied": bool(significant),
            "roundoff_clipped": bool(not significant and minimum < 0),
            "correction_l1": float(np.abs(psf - signed).sum()),
            "otf_max_abs_change": float(np.max(np.abs(reconstructed - normalized))),
            "edge_energy_fraction": float(psf[edge].sum()),
        },
    }


def psf_to_mtf(
    psf: Any,
    *,
    sample_pitch_um: float,
    frequency: Any | None = None,
    frequency_unit: str = "cycles/mm",
    measurement_pixel_pitch_um: float | None = None,
    angles_deg: Any = (0.0, 45.0, 90.0, 135.0),
) -> dict[str, Any]:
    """Return normalized complex OTF and MTF on centered FFT frequency axes.

    Optional curves are evaluated by the discrete Fourier sum at the *exact*
    requested frequencies/directions, without grid interpolation or radial
    averaging. Centered array entries represent discrete impulse weights; no
    continuous pixel-aperture factor is silently introduced. For cycles/pixel,
    measurement_pixel_pitch_um defines the frequency unit's pixel; it defaults
    to sample_pitch_um. Keep it fixed when refining the PSF grid.
    """
    kernel = _real_array(psf, "psf", 2)
    if min(kernel.shape) < 2 or np.any(kernel < 0) or kernel.max() <= 0:
        raise ValueError("psf must be nonnegative, nonzero and at least 2 x 2.")
    pitch_um = _positive(sample_pitch_um, "sample_pitch_um")
    scale = _frequency_scale(frequency_unit, pitch_um, measurement_pixel_pitch_um)
    pitch_mm = pitch_um * 1e-3
    kernel = kernel / kernel.max()
    kernel = kernel / kernel.sum()
    rows, cols = kernel.shape
    transfer = np.fft.fftshift(np.fft.fft2(np.fft.ifftshift(kernel)))
    result = {
        "otf": transfer,
        "mtf_2d": np.abs(transfer),
        "frequency_x": np.fft.fftshift(np.fft.fftfreq(cols, pitch_mm)) / scale,
        "frequency_y": np.fft.fftshift(np.fft.fftfreq(rows, pitch_mm)) / scale,
        "frequency_unit": frequency_unit,
        "measurement_pixel_pitch_um": 1000.0 / scale if frequency_unit == "cycles/pixel" else None,
    }
    if frequency is None:
        return result
    samples = _real_array(frequency, "frequency", 1)
    angles = _real_array(angles_deg, "angles_deg", 1)
    if samples.size == 0 or angles.size == 0:
        raise ValueError("frequency and angles_deg must be nonempty.")
    if np.any(samples < 0) or np.any(samples * scale > 0.5 / pitch_mm * (1 + 1e-12)):
        raise ValueError("frequency must lie between zero and the axial Nyquist frequency.")
    x = (np.arange(cols) - cols // 2) * pitch_mm
    y = (np.arange(rows) - rows // 2) * pitch_mm
    curves = np.empty((angles.size, samples.size), dtype=float)
    for index, angle in enumerate(np.deg2rad(angles)):
        for start in range(0, samples.size, 128):
            stop = start + 128
            f = samples[start:stop, None] * scale
            ex = np.exp(-2j * np.pi * f * np.cos(angle) * x[None, :])
            ey = np.exp(-2j * np.pi * f * np.sin(angle) * y[None, :])
            curves[index, start:stop] = np.abs(np.sum((ey @ kernel) * ex, axis=1))
    result.update(frequency=samples.copy(), angles_deg=angles.copy(), mtf_by_angle=curves)
    return result


def measured_mtf_to_psf(
    frequency: Any,
    mtf: Any,
    *,
    sample_pitch_um: float,
    kernel_size: int = 129,
    frequency_unit: str = "cycles/mm",
    measurement_pixel_pitch_um: float | None = None,
    high_frequency: str = "zero",
    negative_policy: str = "project",
    max_abs_error: float = 0.02,
    rmse_tolerance: float = 0.01,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct and validate an approximate PSF from a normalized radial MTF.

    Frequencies must increase strictly from DC and stay within axial Nyquist.
    MTF(0) must equal 1; no automatic normalization, sorting, clipping or smoothing
    of measurements occurs. Linear interpolation is used between observations.
    Beyond the measured band, ``zero`` truncates and ``hold`` holds the last
    value. Both are declared assumptions, including the square grid's corners.

    A failed round-trip returns the candidate plus ``passed=False`` and residuals,
    not a successful calibration. Thresholds are absolute normalized MTF errors.
    Repeat with larger support/finer pitch to assess numerical convergence;
    passing this input-consistency check does not establish physical validation.
    For cycles/pixel the measurement pixel pitch defaults to the PSF sample pitch;
    supply measurement_pixel_pitch_um separately to refine the grid without
    changing the physical meaning of the measured frequencies.
    """
    samples = _real_array(frequency, "frequency", 1)
    values = _real_array(mtf, "mtf", 1)
    if samples.size < 2 or samples.shape != values.shape:
        raise ValueError("frequency and mtf must have matching lengths of at least two.")
    if samples[0] != 0 or np.any(np.diff(samples) <= 0):
        raise ValueError("frequency must start at zero and be strictly increasing.")
    if np.any(values < 0) or np.any(values > 1) or not np.isclose(values[0], 1, atol=1e-12, rtol=0):
        raise ValueError("mtf must be normalized to DC=1 with all values between 0 and 1.")
    pitch_um = _positive(sample_pitch_um, "sample_pitch_um")
    scale = _frequency_scale(frequency_unit, pitch_um, measurement_pixel_pitch_um)
    if samples[-1] * scale > 500.0 / pitch_um * (1 + 1e-12):
        raise ValueError("Measured frequencies exceed the axial Nyquist frequency for this pitch.")
    if (
        isinstance(kernel_size, (bool, np.bool_))
        or not isinstance(kernel_size, (int, np.integer))
        or not 3 <= kernel_size <= 1025
        or kernel_size % 2 != 1
    ):
        raise ValueError("kernel_size must be an odd integer between 3 and 1025.")
    if high_frequency not in {"zero", "hold"}:
        raise ValueError("high_frequency must be 'zero' or 'hold'.")
    maximum = _positive(max_abs_error, "max_abs_error", zero=True)
    rms_limit = _positive(rmse_tolerance, "rmse_tolerance", zero=True)
    axis = np.fft.fftfreq(kernel_size, d=pitch_um * 1e-3)
    fx, fy = np.meshgrid(axis, axis, indexing="xy")
    radial = np.hypot(fx, fy)
    target = np.interp(
        radial, samples * scale, values,
        right=0.0 if high_frequency == "zero" else float(values[-1]),
    )
    inverted = otf_to_psf(target, negative_policy=negative_policy)
    psf = inverted["psf"]
    forward = psf_to_mtf(
        psf, sample_pitch_um=pitch_um, frequency=samples, frequency_unit=frequency_unit,
        measurement_pixel_pitch_um=measurement_pixel_pitch_um,
    )
    residual = forward["mtf_by_angle"] - values[None, :]
    observed_maximum = float(np.max(np.abs(residual)))
    observed_rms = float(np.sqrt(np.mean(residual**2)))
    warnings = []
    if inverted["diagnostics"]["projection_applied"]:
        warnings.append("Negative inverse intensity required nonnegative unit-sum projection.")
    if high_frequency == "zero" and values[-1] > 0.01:
        warnings.append("Nonzero measured endpoint is abruptly truncated beyond the measured band.")
    if inverted["diagnostics"]["edge_energy_fraction"] > 1e-3:
        warnings.append("PSF reaches the grid boundary; increase support and check convergence.")
    passed = observed_maximum <= maximum and observed_rms <= rms_limit
    if not passed:
        warnings.append(
            "Round-trip error exceeds the requested tolerance; candidate is not accepted."
        )
    return {
        "schema": "measured_mtf_psf_v1",
        "method": "radial_zero_phase_simplex_v1",
        "psf": psf,
        "sample_pitch_um": pitch_um,
        "spatial_axis_um": (np.arange(kernel_size) - kernel_size // 2) * pitch_um,
        "input": {
            "frequency": samples.copy(), "mtf": values.copy(), "frequency_unit": frequency_unit,
            "measurement_pixel_pitch_um": forward["measurement_pixel_pitch_um"],
            "source": deepcopy(source),
        },
        "roundtrip": {
            "frequency": samples.copy(), "frequency_unit": frequency_unit,
            "angles_deg": forward["angles_deg"], "mtf_by_angle": forward["mtf_by_angle"],
            "residual_by_angle": residual, "max_abs_error": observed_maximum,
            "rmse": observed_rms, "max_abs_error_tolerance": maximum,
            "rmse_tolerance": rms_limit, "passed": bool(passed),
        },
        "diagnostics": inverted["diagnostics"],
        "assumptions": {
            "phase": "zero_before_projection", "symmetry": "radial_before_square_grid_sampling",
            "interpolation": "linear", "high_frequency": high_frequency,
            "negative_policy": negative_policy, "normalization": "unit_sum",
            "centering": "array_center", "sampling": "discrete_impulse_weights",
            "measurement_band_cycles_mm": [0.0, float(samples[-1] * scale)],
            "frequency_step_cycles_mm": float(1000.0 / (kernel_size * pitch_um)),
            "pixel_aperture_added": False, "unique_psf_recovery": False,
            "sampling_convergence": "not_established",
        },
        "warnings": warnings,
        "physical_validation": "not_established",
    }
