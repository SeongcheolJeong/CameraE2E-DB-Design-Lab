"""Exposure-motion and RAW channel-ratio costs for offline design comparison.

The analytic input is a translating sinusoid in a fixed, explicitly bounded
ideal-image window. Both cameras see the same pose at their exposure midpoint.
For time-invariant linear optics, averaging irradiance before spatial transport
is equivalent to averaging transported irradiance. This is NOT rolling-shutter,
trap-history, geometric motion, an ISP correction, or physical colorimetry.
"""
from __future__ import annotations

import math
import numpy as np


def _finite(value: float, name: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(name + " must be a finite non-boolean number")
    if positive and value <= 0:
        raise ValueError(name + " must be positive")
    return float(value)


def box_shutter_transfer(frequency_cpp: float, velocity_px_s: float,
                         exposure_s: float) -> float:
    """Signed temporal transfer sinc(f*v*T); a negative value is inversion.

    This is relative to the same candidate's static response, not its total
    lens/pixel OTF, and is a linear-radiometric value before clipping/ADC.
    """
    f = _finite(frequency_cpp, "frequency_cpp")
    v = _finite(velocity_px_s, "velocity_px_s")
    t = _finite(exposure_s, "exposure_s", positive=True)
    if not 0 <= f <= .5 or abs(v) > 1e6 or t > 10:
        raise ValueError("Use f in [0,.5] cpp, |v| <= 1e6 px/s and T <= 10s")
    return float(np.sinc(f * v * t))


def exposure_averaged_grating(shape: tuple[int, int], *, frequency_cpp: float,
                              velocity_px_s: float, exposure_s: float,
                              modulation: float = .5,
                              phase_at_midpoint_cycles: float = 0.0) -> np.ndarray:
    """Exact point-source-grid time average; no image warp or wrap-around.

    Supplied source cells retain the existing engine's center-sample convention.
    No second receiving-pixel aperture is included. Outside this fixed image
    window the existing boundary remains dark; source halos are not invented.
    """
    if (len(shape) != 2 or any(type(n) is not int or not 2 <= n <= 512 for n in shape)):
        raise ValueError("Require two bounded integer image dimensions")
    m = _finite(modulation, "modulation")
    phase = _finite(phase_at_midpoint_cycles, "phase_at_midpoint_cycles")
    if not 0 <= m <= 1:
        raise ValueError("modulation must be in [0,1]")
    transfer = box_shutter_transfer(frequency_cpp, velocity_px_s, exposure_s)
    x = np.arange(shape[1], dtype=float) - (shape[1] - 1) / 2
    wave = 1 + m * transfer * np.cos(2 * np.pi * (frequency_cpp * x + phase % 1))
    return np.broadcast_to(wave, shape).copy()


def projected_transfer(current: np.ndarray, flat: np.ndarray,
                       static: np.ndarray, mask: np.ndarray) -> dict:
    """Offline signed projection against the same camera's static template.

    The flat field removes the fixed finite-window/PSF envelope. The caller
    must reject clipping before interpreting this as a RAW contrast transfer.
    Charge truth is permitted for offline diagnosis, never an ISP input here.
    """
    arrays = [np.asarray(a, dtype=float) for a in (current, flat, static)]
    chosen = np.asarray(mask)
    if (any(a.shape != arrays[0].shape or not np.isfinite(a).all() for a in arrays)
            or chosen.shape != arrays[0].shape or chosen.dtype != bool or not chosen.any()):
        raise ValueError("Require finite same-shape arrays and a nonempty boolean mask")
    delta = (arrays[0] - arrays[1])[chosen]
    template = (arrays[2] - arrays[1])[chosen]
    energy = float(np.dot(template, template))
    if energy <= np.finfo(float).eps * max(1., float(np.dot(arrays[2][chosen], arrays[2][chosen]))):
        raise ValueError("No resolvable static modulation template")
    transfer = float(np.dot(delta, template) / energy)
    residual = delta - transfer * template
    return {"signed_transfer": transfer,
            "template_relative_residual": float(np.linalg.norm(residual) / math.sqrt(energy)),
            "template_rms": float(math.sqrt(energy / template.size))}


def _rgb(values, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.shape != (3,) or not np.isfinite(array).all() or np.any(array <= 0):
        raise ValueError(name + " requires three finite positive R/G/B signals")
    return array


def fit_neutral_channel_gains(candidate_rgb, reference_rgb) -> np.ndarray:
    """Fit one fixed R/G/B multiplier on a declared neutral calibration input.

    Green stays one. This is neither a color correction matrix nor a physical
    white-balance calibration. The same gains must be frozen for later inputs.
    """
    candidate, reference = _rgb(candidate_rgb, "candidate"), _rgb(reference_rgb, "reference")
    return (reference / reference[1]) / (candidate / candidate[1])


def channel_ratio_error(candidate_rgb, reference_rgb, gains=(1., 1., 1.)) -> dict:
    """Relative R/G and B/G residual against a reference camera, not Delta-E."""
    candidate = _rgb(candidate_rgb, "candidate") * _rgb(gains, "gains")
    reference = _rgb(reference_rgb, "reference")
    ratios, target = candidate[[0, 2]] / candidate[1], reference[[0, 2]] / reference[1]
    residual = ratios / target - 1
    return {"R_over_G": float(ratios[0]), "B_over_G": float(ratios[1]),
            "relative_R_over_G_error": float(residual[0]),
            "relative_B_over_G_error": float(residual[1]),
            "maximum_absolute_relative_error": float(np.max(np.abs(residual))),
            "meaning": "RAW_channel_ratio_consistency_not_physical_color_accuracy"}
