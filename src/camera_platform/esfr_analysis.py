"""Versioned original-pixel edge analysis for the ISO 2023 slanted-star chart.

This is an engineering SFR estimator, not an ISO compliance implementation. A
four-cycle slanted star supplies straight edges away from its junction; it is
not analysed as a Siemens star. Detection proposes ROIs for human review and
never establishes chart identity, physical references, or a certification.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy.ndimage import gaussian_filter, gaussian_filter1d

METHOD = "camera_esfr_projected_edge_v1"
DETECTOR = "camera_esfr_straight_segment_proposals_v1"
REFERENCE_URL = "https://www.imatest.com/imaging/iso-12233/"


def _failure(reason: str, quality: dict | None = None) -> dict:
    return {
        "status": "unavailable",
        "reason": reason,
        "method": METHOD,
        "frequency_cy_native_px": [],
        "sfr": [],
        "mtf50": None,
        "mtf50p": None,
        "esf": [],
        "lsf": [],
        "overshoot": None,
        "undershoot": None,
        "edge_angle_deg": None,
        "quality": quality or {},
    }


def _crossing(freq: np.ndarray, response: np.ndarray, level: float) -> float | None:
    # The descending crossing after the global peak is appropriate for MTF50P
    # and avoids selecting pre-peak crossings in sharpened responses.
    peak = int(np.argmax(response))
    below = np.flatnonzero(response[peak:] <= level)
    if not below.size:
        return None
    index = peak + int(below[0])
    if index == 0:
        return None
    a, b = response[index - 1 : index + 1]
    if a == b:
        return float(freq[index])
    return float(freq[index - 1] + (level - a) * (freq[index] - freq[index - 1]) / (b - a))


def edge_metrics(
    plane: Any,
    pitch_native: float = 1.0,
    *,
    black_level: float | None = None,
    white_level: float | None = None,
    max_clipped_fraction: float = 0.01,
) -> dict:
    """Measure a single straight edge without rotating or resampling pixels.

    ``pitch_native`` is the spacing of the input raster in native sensor pixels:
    1 for native ISP, 2 for one Bayer sublattice. Inputs must already be linear
    for a linear-system SFR interpretation; gamma is never guessed. Edge angle
    describes the *edge tangent*, frequency direction is its normal.

    Geometry uses Gaussian-smoothed data only for edge localisation, followed by
    robust linear fitting. Original samples are projected into quarter-raster
    bins. A Hann-windowed central-difference LSF receives the exact derivative
    response correction; no optical/pixel-aperture or chart-MTF deconvolution is
    performed. Sparse phase coverage, crossings and unreliable fits are rejected.
    """
    data = np.asarray(plane, dtype=np.float64)
    if data.ndim != 2 or min(data.shape, default=0) < 16:
        return _failure("edge_roi_too_small_or_not_plane")
    if not np.isfinite(data).all():
        return _failure("nonfinite_input")
    if not np.isfinite(pitch_native) or pitch_native <= 0:
        raise ValueError("pitch_native must be positive and finite")
    if not 0 <= max_clipped_fraction <= 1:
        raise ValueError("max_clipped_fraction must be between zero and one")
    for name, limit in (("black_level", black_level), ("white_level", white_level)):
        if limit is not None and not np.isfinite(limit):
            raise ValueError(f"{name} must be finite")
    if black_level is not None and white_level is not None and black_level >= white_level:
        raise ValueError("black_level must be below white_level")
    quality: dict[str, Any] = {"shape": list(data.shape), "warnings": []}
    clipped = np.zeros(data.shape, dtype=bool)
    if black_level is not None:
        clipped |= data <= black_level
    if white_level is not None:
        clipped |= data >= white_level
    quality["clipped_fraction"] = (
        float(np.mean(clipped)) if (black_level is not None or white_level is not None) else None
    )
    quality["clipping_limits_declared"] = black_level is not None or white_level is not None
    if (
        quality["clipped_fraction"] is not None
        and quality["clipped_fraction"] > max_clipped_fraction
    ):
        return _failure("clipped_edge", quality)

    low, high = np.percentile(data, [10, 90])
    contrast = float(high - low)
    if contrast <= max(1e-12, float(np.max(np.abs(data))) * 1e-9):
        return _failure("no_edge_contrast", quality)
    norm = (data - low) / contrast
    smooth = gaussian_filter(norm, 1.0, mode="nearest")
    gy, gx = np.gradient(smooth)
    magnitude = np.hypot(gx, gy)
    support = magnitude > max(float(np.max(magnitude)) * 0.18, 1e-8)
    if np.count_nonzero(support) < 16:
        return _failure("insufficient_gradient_support", quality)
    vectors = np.stack([gx[support], gy[support]], axis=1)
    tensor = vectors.T @ vectors
    eigenvalues, eigenvectors = np.linalg.eigh(tensor)
    coherence = float((eigenvalues[-1] - eigenvalues[0]) / max(eigenvalues.sum(), 1e-15))
    quality["gradient_coherence"] = coherence
    if coherence < 0.70:
        return _failure("multiple_edge_directions_or_noise", quality)
    normal = eigenvectors[:, -1]
    transpose = abs(normal[1]) > abs(normal[0])
    work = norm.T if transpose else norm
    height, width = work.shape
    derivative = np.gradient(gaussian_filter1d(work, 1.0, axis=1, mode="nearest"), axis=1)
    polarity = 1.0 if float(np.sum(derivative)) >= 0 else -1.0
    derivative *= polarity
    rows, points, strengths = [], [], []
    margin = max(4, int(width * 0.08))
    for row in range(2, height - 2):
        line = derivative[row]
        peak = margin + int(np.argmax(line[margin : width - margin]))
        start, stop = max(1, peak - 6), min(width - 1, peak + 7)
        weights = np.maximum(line[start:stop], 0)
        strength = float(weights.sum())
        if strength <= 0.15:
            continue
        location = float(np.dot(np.arange(start, stop), weights) / strength)
        rows.append(row)
        points.append(location)
        strengths.append(strength)
    if len(rows) < max(12, height // 2):
        return _failure("edge_does_not_cross_enough_scanlines", quality)
    rows, points, strengths = map(np.asarray, (rows, points, strengths))
    weights = np.minimum(strengths, np.median(strengths) * 2)
    fit = np.polyfit(rows, points, 1, w=np.sqrt(weights))
    for _ in range(5):
        residual = points - np.polyval(fit, rows)
        scale = max(0.05, 1.4826 * float(np.median(np.abs(residual - np.median(residual)))))
        robust = np.minimum(1, 1.5 * scale / np.maximum(np.abs(residual), 1e-12))
        fit = np.polyfit(rows, points, 1, w=np.sqrt(weights * robust))
    residual = points - np.polyval(fit, rows)
    inliers = np.abs(residual) < 0.75
    quality["edge_fit_rmse_px"] = float(np.sqrt(np.mean(residual**2)))
    quality["edge_fit_inlier_fraction"] = float(np.mean(inliers))
    if np.mean(inliers) < 0.85 or quality["edge_fit_rmse_px"] > 0.75:
        return _failure("non_straight_or_contaminated_edge", quality)
    slope, intercept = map(float, fit)
    yy, xx = np.indices(work.shape, dtype=float)
    distance = (xx - slope * yy - intercept) / np.sqrt(1 + slope * slope)
    if polarity < 0:
        distance = -distance
    half_span = min(24.0, min(work.shape) * 0.30)
    delta = 0.25
    bin_count = int(2 * half_span / delta) | 1
    index = np.floor(distance / delta + bin_count / 2).astype(int)
    valid = (index >= 0) & (index < bin_count)
    counts = np.bincount(index[valid], minlength=bin_count)
    sums = np.bincount(index[valid], weights=work[valid], minlength=bin_count)
    occupied = counts >= 3
    quality["phase_bin_coverage"] = float(np.mean(occupied))
    quality["minimum_bin_samples"] = int(counts.min())
    if np.mean(occupied) < 0.95:
        return _failure("insufficient_subpixel_phase_coverage", quality)
    coordinates = (np.arange(bin_count) - (bin_count - 1) / 2) * delta
    esf = np.interp(
        np.arange(bin_count), np.flatnonzero(occupied), sums[occupied] / counts[occupied]
    )
    plateau = max(4, bin_count // 8)
    left, right = float(np.median(esf[:plateau])), float(np.median(esf[-plateau:]))
    step = right - left
    quality["edge_contrast_input_units"] = float(step * contrast)
    if step < 0.2:
        return _failure("insufficient_edge_plateaus", quality)
    esf = (esf - left) / step
    # The original raster should be described by one edge. Multiple transitions
    # and star centres have high residual energy even when one direction wins.
    reconstruction = np.interp(distance[valid], coordinates, esf)
    observed = (work[valid] - left) / step
    residual_noise = float(np.sqrt(np.mean((observed - reconstruction) ** 2)))
    quality["edge_model_residual_rms_fraction"] = residual_noise
    if residual_noise > 0.15:
        return _failure("edge_model_residual_too_large", quality)
    if residual_noise > 0.05:
        quality["warnings"].append("noisy_edge_repeat_capture_recommended")
    lsf = np.gradient(esf, delta)
    windowed = lsf * np.hanning(len(lsf))
    nfft = max(4096, 2 ** int(np.ceil(np.log2(len(lsf)))))
    spectrum = np.abs(np.fft.rfft(windowed, n=nfft))
    freq_raster = np.fft.rfftfreq(nfft, delta)
    use = freq_raster <= 0.5 + 1e-12
    if spectrum[0] <= 1e-10:
        return _failure("degenerate_lsf", quality)
    freq_raster, sfr = freq_raster[use], spectrum[use] / spectrum[0]
    sfr /= np.sinc(2 * freq_raster * delta)
    freq = freq_raster / float(pitch_native)
    edge_angle = (
        float(np.degrees(np.arctan2(slope, 1)) if transpose else np.degrees(np.arctan2(1, slope)))
        % 180
    )
    return {
        "status": "ok",
        "method": METHOD,
        "frequency_cy_native_px": freq.tolist(),
        "sfr": sfr.tolist(),
        "mtf50": _crossing(freq, sfr, 0.5),
        "mtf50p": _crossing(freq, sfr, float(sfr.max()) * 0.5),
        "mtf50_reason": None
        if _crossing(freq, sfr, 0.5) is not None
        else "no_crossing_in_measured_band",
        "esf": esf.tolist(),
        "lsf": lsf.tolist(),
        "esf_distance_native_px": (coordinates * pitch_native).tolist(),
        "overshoot": max(0.0, float(esf.max() - 1)),
        "undershoot": max(0.0, float(-esf.min())),
        "edge_angle_deg": edge_angle,
        "frequency_direction_deg": (edge_angle + 90) % 180,
        "quality": quality,
        "method_details": {
            "iso_compliance": "not_established",
            "edge_fit": "robust_linear",
            "sample_projection": "original_pixels_signed_normal_distance",
            "image_resampling": False,
            "esf_bins_per_input_pixel": 4,
            "window": "Hann",
            "differentiation": "central_difference",
            "derivative_correction": "1/sinc(2*f*bin_pitch)",
            "bin_aperture_correction": False,
            "chart_mtf_compensation": False,
            "pitch_native": float(pitch_native),
            "nyquist_cy_native_px": 0.5 / pitch_native,
        },
    }


def _mono(image: Any) -> np.ndarray:
    value = np.asarray(image, dtype=float)
    if value.ndim == 3 and value.shape[-1] >= 3:
        value = value[..., :3] @ np.array([0.2126, 0.7152, 0.0722])
    if value.ndim != 2 or not np.isfinite(value).all() or min(value.shape) < 16:
        raise ValueError(
            "image must be a finite plane or RGB image with at least 16 pixels per axis"
        )
    return value


def _manual_rois(profile: Mapping, shape: tuple[int, int]) -> list[dict] | None:
    supplied = profile.get("rois")
    if supplied is None:
        return None
    if not isinstance(supplied, list):
        raise ValueError("chart_profile.rois must be a list")
    result, ids = [], set()
    for index, source in enumerate(supplied):
        roi = dict(source)
        box = roi.get("box", [])
        if len(box) != 4 or any(not np.isfinite(x) or int(x) != x for x in box):
            raise ValueError("ROI box must contain integer [row,col,height,width]")
        row, col, height, width = map(int, box)
        if (
            min(row, col) < 0
            or min(height, width) < 1
            or row + height > shape[0]
            or col + width > shape[1]
        ):
            raise ValueError("ROI box is outside image")
        kind = roi.get("kind", "edge")
        if kind not in {"edge", "gray", "color", "wedge"}:
            raise ValueError("Unsupported ROI kind")
        identity = str(roi.get("id", f"manual-{index:03d}"))
        if identity in ids:
            raise ValueError("ROI ids must be unique")
        ids.add(identity)
        roi.update(
            {
                "id": identity,
                "kind": kind,
                "box": [row, col, height, width],
                "field_id": str(roi.get("field_id", identity)),
                "orientation_label": str(roi.get("orientation_label", "user_defined")),
                "confidence": 1.0,
                "source": "explicit_profile_roi",
            }
        )
        result.append(roi)
    return result


def detect_rois(image: Any, chart_profile: Mapping | None = None) -> dict:
    """Return conservative straight-edge proposals or exact explicit ROIs.

    No fixed layout is inferred from the words 'ISO 2023'. The physical print
    size, pre-distortion and framing are unknown. Automatic candidates must be
    visually confirmed; no color/gray/wedge patch identity is guessed. Templates
    can supply their registered pixel ROIs via chart_profile.rois.
    """
    import cv2

    profile = dict(chart_profile or {})
    mono = _mono(image)
    manual = _manual_rois(profile, mono.shape)
    if manual is not None:
        return {
            "rois": manual,
            "method": DETECTOR,
            "warnings": [],
            "registration": {
                "status": "explicit_rois",
                "image_shape": list(mono.shape),
                "chart_identity_verified": bool(profile.get("chart_identity_verified", False)),
                "requires_review": not bool(profile.get("rois_approved", False)),
                "transform_applied_to_measurements": False,
            },
        }
    lo, hi = np.percentile(mono, [1, 99])
    if hi <= lo:
        return {
            "rois": [],
            "method": DETECTOR,
            "warnings": ["No usable chart contrast"],
            "registration": {
                "status": "unavailable",
                "requires_review": True,
                "chart_identity_verified": False,
            },
        }
    normalized = np.clip((mono - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
    # Only proposal detection is downscaled; every accepted metric uses the
    # original sensor/ISP pixels and an unwarped integer crop.
    scale = min(1.0, 1600 / max(mono.shape))
    preview = (
        cv2.resize(normalized, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if scale < 1
        else normalized
    )
    segments = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD).detect(preview)[0]
    candidates = []
    if segments is not None:
        for segment in segments[:, 0]:
            x1, y1, x2, y2 = map(float, segment / scale)
            length = float(np.hypot(x2 - x1, y2 - y1))
            if length < 36:
                continue
            # The central portion of a detected finite straight segment avoids
            # corners and star junctions; the edge residual gate rejects any
            # remaining crossings. Long segments receive multiple proposals.
            for fraction in (0.30, 0.70) if length > 100 else (0.5,):
                cx, cy = x1 + fraction * (x2 - x1), y1 + fraction * (y2 - y1)
                size = int(min(96, max(24, length * 0.30)))
                r0, c0 = int(round(cy - size / 2)), int(round(cx - size / 2))
                if r0 < 0 or c0 < 0 or r0 + size > mono.shape[0] or c0 + size > mono.shape[1]:
                    continue
                result = edge_metrics(mono[r0 : r0 + size, c0 : c0 + size])
                if result["status"] != "ok":
                    continue
                angle = result["edge_angle_deg"]
                if any(
                    np.hypot(cy - item["cy"], cx - item["cx"]) < size * 0.6
                    and abs((angle - item["angle"] + 90) % 180 - 90) < 12
                    for item in candidates
                ):
                    continue
                candidates.append(
                    {
                        "box": [r0, c0, size, size],
                        "cy": cy,
                        "cx": cx,
                        "angle": angle,
                        "quality": result["quality"],
                    }
                )
    candidates.sort(key=lambda item: (item["cy"], item["cx"], item["angle"]))
    rois = []
    for index, item in enumerate(candidates[: int(profile.get("max_auto_rois", 96))]):
        angle = item["angle"]
        label = (
            "horizontal_edge"
            if min(angle, 180 - angle) < 20
            else "vertical_edge"
            if abs(angle - 90) < 20
            else "diagonal_edge"
        )
        row_band = min(2, int(item["cy"] / mono.shape[0] * 3))
        col_band = min(2, int(item["cx"] / mono.shape[1] * 3))
        rois.append(
            {
                "id": f"edge-{index:03d}",
                "kind": "edge",
                "box": item["box"],
                "field_id": f"image-region-{row_band}-{col_band}",
                "orientation_label": label,
                "orientation_deg": angle,
                "confidence": min(0.95, item["quality"]["gradient_coherence"]),
                "source": "straight_segment_candidate",
                "quality": item["quality"],
            }
        )
    contours, _ = cv2.findContours(
        cv2.threshold(preview, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1],
        cv2.RETR_LIST,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    fiducials = []
    for contour in contours:
        area, perimeter = cv2.contourArea(contour), cv2.arcLength(contour, True)
        if (
            10 < area < preview.size * 0.003
            and perimeter > 0
            and 4 * np.pi * area / perimeter**2 > 0.82
        ):
            (cx, cy), radius = cv2.minEnclosingCircle(contour)
            fiducials.append(
                {
                    "center_xy": [float(cx / scale), float(cy / scale)],
                    "radius": float(radius / scale),
                }
            )
    warnings = [
        "Automatic ROIs are unregistered straight-edge proposals: "
        "confirm chart identity and every ROI overlay.",
        "Color, gray and wedge patch identities require explicit registered ROIs; "
        "no chart reference values were inferred.",
    ]
    if not rois:
        warnings.append(
            "No safe edge candidates found; provide manual ROIs away from star junctions and text."
        )
    return {
        "rois": rois,
        "method": DETECTOR,
        "warnings": warnings,
        "registration": {
            "status": "proposals_require_review",
            "requires_review": True,
            "image_shape": list(mono.shape),
            "chart_identity_verified": False,
            "transform_applied_to_measurements": False,
            "fiducial_candidates": fiducials[:64],
            "fiducial_correspondence": "not_established",
            "reference_url": REFERENCE_URL,
        },
    }
