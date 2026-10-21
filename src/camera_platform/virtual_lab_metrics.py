"""Small, plot-ready Virtual Lab measurements using the existing CameraE2E engine.

These are model-based engineering diagnostics, not laboratory certification.
The caller supplies fixed geometry, spectral color references, and independent
repeat captures. No image-wise normalization or color-chart fitting is performed.
ROIs use zero-based half-open [row_start, row_end, column_start, column_end].
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from pyisetcam.iso import iso12233
from pyisetcam.metrics import delta_e_ab

SCHEMA = "camera_virtual_lab_metrics_v1"
# IEC sRGB primaries / D65. Inputs are signed linear sRGB, before display transfer.
LINEAR_SRGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=float,
)
LUMA = LINEAR_SRGB_TO_XYZ[1]


def _array(value: Any, name: str) -> np.ndarray:
    result = np.asarray(value, dtype=float)
    if not result.size or not np.isfinite(result).all():
        raise ValueError(f"{name} must be nonempty and finite")
    return result


def _positive(value: float, name: str) -> float:
    result = float(value)
    if not np.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return result


def _mono(value: Any, name: str = "image") -> np.ndarray:
    image = _array(value, name)
    if image.ndim == 3 and image.shape[-1] == 3:
        return image @ LUMA
    if image.ndim != 2:
        raise ValueError(f"{name} must be a 2D plane or linear RGB image")
    return image


def _roi(value: Sequence[int], shape: Sequence[int]) -> tuple[slice, slice]:
    if len(value) != 4 or any(int(v) != v for v in value):
        raise ValueError("ROI must contain four integer bounds")
    r0, r1, c0, c1 = map(int, value)
    if not (0 <= r0 < r1 <= shape[0] and 0 <= c0 < c1 <= shape[1]):
        raise ValueError("ROI is outside the image")
    return slice(r0, r1), slice(c0, c1)


def five_site_rois(shape: Sequence[int], *, fraction: float = 0.16) -> dict[str, list[int]]:
    """Fixed center/corner ROIs for charts generated at these same locations."""
    h, w = map(int, shape[:2])
    if min(h, w) < 32 or not 0.05 <= fraction <= 0.25:
        raise ValueError("Five-site charts require at least 32 pixels per axis")
    rh, rw = max(12, int(h * fraction)), max(12, int(w * fraction))
    sites = {
        "center": (0.5, 0.5),
        "upper_left": (0.22, 0.22),
        "upper_right": (0.22, 0.78),
        "lower_left": (0.78, 0.22),
        "lower_right": (0.78, 0.78),
    }
    result = {}
    for name, (y, x) in sites.items():
        r0, c0 = int(y * h) - rh // 2, int(x * w) - rw // 2
        result[name] = [r0, r0 + rh, c0, c0 + rw]
    return result


def _crossing(freq: np.ndarray, response: np.ndarray, level: float = 0.5) -> float | None:
    below = np.flatnonzero(response <= level)
    if not below.size or below[0] == 0:
        return None
    j = int(below[0])
    return float(np.interp(level, response[j - 1 : j + 1][::-1], freq[j - 1 : j + 1][::-1]))


def _frequency_axes(
    freq: np.ndarray, *, pixel_pitch_um: float | None, degrees_per_pixel: float | None
) -> dict[str, Any]:
    result: dict[str, Any] = {"cycles_per_pixel": freq.tolist()}
    if pixel_pitch_um is not None:
        result["cycles_per_mm"] = (
            freq / (_positive(pixel_pitch_um, "pixel_pitch_um") * 1e-3)
        ).tolist()
    if degrees_per_pixel is not None:
        result["cycles_per_degree"] = (
            freq / _positive(degrees_per_pixel, "degrees_per_pixel")
        ).tolist()
    return result


def slanted_edge_metrics(
    image: Any, *, pixel_pitch_um: float | None = None, degrees_per_pixel: float | None = None
) -> dict[str, Any]:
    """ISO12233-oriented SFR estimate, limited to this input raster's Nyquist.

    pixel_pitch_um is the physical spacing of this raster, not necessarily the
    native sensor pitch after regridding. Angular spacing must describe its ROI.
    """
    mono = _mono(image)
    if min(mono.shape) < 12:
        return {"status": "unavailable", "reason": "edge_roi_too_small"}
    left, right = float(np.mean(mono[:, :3])), float(np.mean(mono[:, -3:]))
    contrast = abs(right - left)
    if contrast <= max(1e-12, float(np.ptp(mono)) * 0.05):
        return {"status": "unavailable", "reason": "no_dominant_vertical_edge"}
    try:
        result = iso12233(mono, delta_x=1.0)
    except (ValueError, ArithmeticError, np.linalg.LinAlgError) as error:
        # A finite low-light ROI may pass the coarse edge-contrast check while
        # its noisy LSF has an out-of-support centroid. That is an unavailable
        # measurement, not a failed RAW/ISP capture or an invented zero SFR.
        return {
            "status": "unavailable",
            "reason": "edge_estimator_failed",
            "method": "pyisetcam.iso.iso12233",
            "estimator_error_type": type(error).__name__,
            "estimator_error": str(error),
            "edge_contrast": contrast,
        }
    freq, sfr = np.asarray(result.freq), np.asarray(result.mtf)
    good = (freq <= 0.5 + 1e-12) & np.isfinite(freq) & np.isfinite(sfr)
    freq, sfr = freq[good], sfr[good]
    if freq.size < 3 or sfr[0] <= 0:
        return {"status": "unavailable", "reason": "edge_estimator_failed"}
    # Legacy iso12233.mtf50 uses a .2 frequency grid; interpolate the curve here.
    mtf50 = _crossing(freq, sfr)
    low, high = min(left, right), max(left, right)
    esf = np.asarray(result.esf).reshape(-1)
    response = {
        "status": "ok",
        "method": "pyisetcam.iso.iso12233",
        "frequency": _frequency_axes(
            freq, pixel_pitch_um=pixel_pitch_um, degrees_per_pixel=degrees_per_pixel
        ),
        "sfr": sfr.tolist(),
        "mtf50_cycles_per_pixel": mtf50,
        "mtf50_reason": None if mtf50 is not None else "no_half_response_crossing_below_nyquist",
        "nyquist_cycles_per_pixel": 0.5,
        "nyquist_response": float(np.interp(0.5, freq, sfr)) if freq[-1] >= 0.5 else None,
        "edge_contrast": contrast,
        "overshoot_fraction": max(0.0, float(np.max(esf)) - high) / contrast,
        "undershoot_fraction": max(0.0, low - float(np.min(esf))) / contrast,
        "esf": esf.tolist(),
        "lsf": np.asarray(result.lsf).tolist(),
        "edge_slope": float(np.asarray(result.fitme).reshape(-1, 2)[0, 0]),
    }
    if pixel_pitch_um is not None:
        response["mtf50_cycles_per_mm"] = None if mtf50 is None else mtf50 / (pixel_pitch_um * 1e-3)
        response["nyquist_cycles_per_mm"] = 0.5 / (pixel_pitch_um * 1e-3)
    if degrees_per_pixel is not None:
        response["mtf50_cycles_per_degree"] = None if mtf50 is None else mtf50 / degrees_per_pixel
        response["nyquist_cycles_per_degree"] = 0.5 / degrees_per_pixel
    return response


def resolution_metrics(
    linear_rgb: Any,
    processed_rgb: Any,
    *,
    rois: Mapping[str, Sequence[int]] | None = None,
    pixel_pitch_um: float | None = None,
    degrees_per_pixel: float | None = None,
) -> dict[str, Any]:
    linear, processed = _mono(linear_rgb), _mono(processed_rgb)
    if linear.shape != processed.shape:
        raise ValueError("Linear and processed SFR need the same raster")
    sites = dict(rois) if rois is not None else five_site_rois(linear.shape)
    results = {}
    for name, bounds in sites.items():
        index = _roi(bounds, linear.shape)
        results[name] = {
            "roi": list(bounds),
            "linear_system": slanted_edge_metrics(
                linear[index], pixel_pitch_um=pixel_pitch_um, degrees_per_pixel=degrees_per_pixel
            ),
            "processed_system": slanted_edge_metrics(
                processed[index], pixel_pitch_um=pixel_pitch_um, degrees_per_pixel=degrees_per_pixel
            ),
        }
    return {
        "schema": SCHEMA,
        "status": "ok",
        "sites": results,
        "interpretation": "System SFR; processed response includes ISP and is not lens-only MTF.",
    }


def optical_psf_metrics(
    psf: Any,
    *,
    sample_pitch_um: float,
    sensor_pitch_um: float | None = None,
    focal_length_mm: float | None = None,
) -> dict[str, Any]:
    kernel = _array(psf, "PSF")
    if kernel.ndim != 2 or np.min(kernel) < 0 or float(np.sum(kernel)) <= 0:
        raise ValueError("PSF must be a nonnegative 2D energy kernel")
    pitch_mm = _positive(sample_pitch_um, "sample_pitch_um") * 1e-3
    kernel = kernel / np.sum(kernel)
    axes = {}
    for name, line in (("horizontal", kernel.sum(axis=0)), ("vertical", kernel.sum(axis=1))):
        n = max(512, 4 * line.size)
        freq = np.fft.rfftfreq(n, d=pitch_mm)
        mtf = np.abs(np.fft.rfft(line, n=n))
        mtf /= mtf[0]
        axis: dict[str, Any] = {
            "cycles_per_mm": freq.tolist(),
            "mtf": mtf.tolist(),
            "mtf50_cycles_per_mm": _crossing(freq, mtf),
        }
        if focal_length_mm is not None:
            scale = _positive(focal_length_mm, "focal_length_mm") * np.pi / 180.0
            axis["cycles_per_degree"] = (freq * scale).tolist()
        if sensor_pitch_um is not None:
            nyquist = 0.5 / (_positive(sensor_pitch_um, "sensor_pitch_um") * 1e-3)
            axis["sensor_nyquist_cycles_per_mm"] = nyquist
            axis["sensor_nyquist_mtf"] = (
                float(np.interp(nyquist, freq, mtf)) if nyquist <= freq[-1] else None
            )
        axes[name] = axis
    yy, xx = np.indices(kernel.shape)
    cx, cy = float(np.sum(xx * kernel)), float(np.sum(yy * kernel))
    return {
        "schema": SCHEMA,
        "status": "ok",
        "axes": axes,
        "sample_pitch_um": float(sample_pitch_um),
        "normalized_energy": float(kernel.sum()),
        "rms_radius_um": float(
            np.sqrt(np.sum(((xx - cx) ** 2 + (yy - cy) ** 2) * kernel)) * sample_pitch_um
        ),
        "angular_geometry": "local paraxial focal-length conversion"
        if focal_length_mm is not None
        else None,
        "interpretation": (
            "MTF of the supplied PSF; preserves that PSF's "
            "geometric/diffraction/pixel-aperture meaning."
        ),
    }


def color_metrics(
    linear_rgb: Any,
    reference_xyz: Any,
    *,
    patch_rois: Sequence[Sequence[int]],
    white_xyz: Any,
    reference_kind: str = "spectral_reflectance",
    reference_provenance: Any = None,
    neutral_patch_indices: Sequence[int] = (18, 19, 20, 21, 22, 23),
) -> dict[str, Any]:
    """Measure fixed linear-sRGB values against independent, same-scale XYZ.

    For a non-D65 input, the caller must supply the explicitly chosen reference
    adaptation to match the fixed ISP target white; no hidden adaptation occurs.
    """
    image, reference, white = (
        _array(linear_rgb, "linear_rgb"),
        _array(reference_xyz, "reference_xyz"),
        _array(white_xyz, "white_xyz"),
    )
    if (
        image.ndim != 3
        or image.shape[-1] != 3
        or reference.shape != (24, 3)
        or len(patch_rois) != 24
        or white.shape != (3,)
        or np.any(white <= 0)
    ):
        raise ValueError(
            "Color evaluation requires RGB, 24 XYZ references/ROIs, and a positive XYZ white"
        )
    patch_rgb = np.array([image[_roi(roi, image.shape)].mean(axis=(0, 1)) for roi in patch_rois])
    measured = patch_rgb @ LINEAR_SRGB_TO_XYZ.T
    delta = np.asarray(delta_e_ab(reference, measured, white, "2000"))
    neutral = np.asarray(list(neutral_patch_indices), dtype=int)
    if np.any((neutral < 0) | (neutral >= 24)):
        raise ValueError("Neutral patch indices must lie within the 24 patches")
    # Relative white-balance error uses chromaticity, so brightness errors remain
    # represented separately by Delta E rather than silently normalized away.
    ref_sum = reference.sum(axis=1)
    measured_sum = measured.sum(axis=1)
    valid = (ref_sum > 0) & (measured_sum > 0)
    chroma = np.linalg.norm(
        measured[:, :2] / np.maximum(measured_sum[:, None], 1e-15)
        - reference[:, :2] / np.maximum(ref_sum[:, None], 1e-15),
        axis=1,
    )
    records = [
        {
            "patch": i + 1,
            "roi": list(patch_rois[i]),
            "reference_xyz": reference[i].tolist(),
            "measured_xyz": measured[i].tolist(),
            "measured_linear_rgb": patch_rgb[i].tolist(),
            "delta_e00": float(delta[i]),
        }
        for i in range(24)
    ]
    usable_neutral = neutral[valid[neutral]]
    return {
        "schema": SCHEMA,
        "status": "ok",
        "delta_e00_mean": float(delta.mean()),
        "delta_e00_max": float(delta.max()),
        "patches": records,
        "white_xyz": white.tolist(),
        "reference_kind": reference_kind,
        "reference_provenance": reference_provenance,
        "neutral_xy_error_mean": float(chroma[usable_neutral].mean())
        if usable_neutral.size
        else None,
        "fitting": "none; fixed upstream WB/CCM and exposure normalization",
        "input_stage": "signed linear sRGB before tone/display transfer",
    }


def temporal_noise_metrics(
    frames: Any,
    *,
    signal_mean: float | None = None,
    roi: Sequence[int] | None = None,
    unit: str = "DN",
    black_level: float = 0.0,
) -> dict[str, Any]:
    """Pool per-pixel temporal variance; spatial content is never temporal noise."""
    values = _array(frames, "frames")
    if values.ndim not in (3, 4) or (values.ndim == 4 and values.shape[-1] != 3):
        raise ValueError("Frames need [repeat,row,col] or [repeat,row,col,RGB]")
    if values.shape[0] < 2:
        return {"status": "unavailable", "reason": "at_least_two_independent_captures_required"}
    if roi is not None:
        values = values[(slice(None), *_roi(roi, values.shape[1:]))]
    if values.ndim == 4:
        values = values @ LUMA
    mean_image = values.mean(axis=0)
    variance = values.var(axis=0, ddof=1)
    temporal_var = float(variance.mean())
    temporal_std = float(np.sqrt(temporal_var))
    signal = float(mean_image.mean() - black_level) if signal_mean is None else float(signal_mean)
    if not np.isfinite(signal):
        raise ValueError("signal_mean must be finite")
    spatial_var = float(mean_image.var())
    # This is still a flat-field nonuniformity proxy, not a PRNU/DSNU separation.
    fixed_var = max(0.0, spatial_var - temporal_var / values.shape[0])
    return {
        "schema": SCHEMA,
        "status": "ok",
        "unit": str(unit),
        "repeat_count": int(values.shape[0]),
        "pixel_count": int(mean_image.size),
        "mean_signal": signal,
        "temporal_variance": temporal_var,
        "temporal_std": temporal_std,
        "snr_linear": signal / temporal_std if temporal_std > 0 and signal > 0 else None,
        "snr_db": float(20 * np.log10(signal / temporal_std))
        if temporal_std > 0 and signal > 0
        else None,
        "snr_reason": None
        if temporal_std > 0 and signal > 0
        else "zero_noise_or_nonpositive_signal",
        "spatial_std_of_temporal_mean": float(np.sqrt(spatial_var)),
        "fixed_pattern_and_shading_std_estimate": float(np.sqrt(fixed_var)),
        "spatial_interpretation": (
            "Includes fixed pattern, shading and texture; not isolated PRNU/DSNU."
        ),
        "variance_definition": "mean_pixel(var_repeat(ddof=1)); independent repeat captures",
    }


def tone_metrics(
    input_levels: Any, mean_electrons: Any, raw_dn: Any, *, full_well_e: float, adc_white: float
) -> dict[str, Any]:
    """Response curve and distinct well/ADC clipping diagnostics per level."""
    levels = _array(input_levels, "input_levels").reshape(-1)
    electrons, raw = _array(mean_electrons, "mean_electrons"), _array(raw_dn, "raw_dn")
    if electrons.shape[0] != levels.size or raw.shape[0] != levels.size:
        raise ValueError("Response stacks must have input level as their first axis")
    well, white = _positive(full_well_e, "full_well_e"), _positive(adc_white, "adc_white")
    rows = []
    for j, level in enumerate(levels):
        e, dn = electrons[j], raw[j]
        rows.append(
            {
                "input_level": float(level),
                "mean_electrons": float(np.mean(e)),
                "mean_dn": float(np.mean(dn)),
                "well_saturation_fraction": float(np.mean(e >= well)),
                "adc_saturation_fraction": float(np.mean(dn >= white)),
            }
        )
    return {
        "schema": SCHEMA,
        "status": "ok",
        "response": rows,
        "full_well_e": well,
        "adc_white_dn": white,
        "well_interpretation": (
            "Fraction whose supplied pre-clipping mean reaches full well; "
            "stochastic clipping may differ."
        ),
    }


def shading_metrics(
    image: Any, *, rois: Mapping[str, Sequence[int]] | None = None
) -> dict[str, Any]:
    mono = _mono(image)
    sites = dict(rois) if rois is not None else five_site_rois(mono.shape)
    means = {name: float(mono[_roi(bounds, mono.shape)].mean()) for name, bounds in sites.items()}
    center = means.get("center", 0.0)
    return {
        "schema": SCHEMA,
        "status": "ok" if center > 0 else "unavailable",
        "reason": None if center > 0 else "nonpositive_center_response",
        "sites": {
            name: {
                "mean": value,
                "relative_to_center": value / center if center > 0 else None,
                "roi": list(sites[name]),
            }
            for name, value in means.items()
        },
    }
