"""Analytic stray-light charts and fixed-mask measurements for Reference Ghost Lab.

The bright source is supplied by the optical transport, separately from these
background charts. Noise uses repeated captures at each pixel, never the spatial
variation of the chart. These are exploratory model diagnostics, not a flare
standard or a psychophysical visibility threshold.
"""

from __future__ import annotations

import numpy as np

SCHEMA = "camera_reference_ghost_chart_v1"
LEVELS = (0.002, 0.005, 0.01, 0.02)
CONTRASTS = (0.05, 0.10, 0.20)
Y_WEIGHTS = np.array([0.2126, 0.7152, 0.0722])


def _geometry(geometry):
    native = tuple(int(v) for v in geometry["native_shape_row_col"])
    full = tuple(int(v) for v in geometry["input_shape_row_col"])
    bounds = geometry["crop_bounds_row_col"]
    if len(bounds) == 2:
        bounds = [*bounds[0], *bounds[1]]
    r0, r1, c0, c1 = map(int, bounds)
    if (
        len(native) != 2
        or len(full) != 2
        or min(native) < 64
        or not (0 <= r0 < r1 <= full[0] and 0 <= c0 < c1 <= full[1])
        or (r1 - r0, c1 - c0) != native
    ):
        raise ValueError("Ghost charts require native dimensions >=64 and a matching input crop")
    return native, full, (r0, r1, c0, c1)


def _roi(r0, r1, c0, c1):
    # Even boundaries preserve equal RGGB site counts in the gray chart masks.
    return [
        int(np.ceil(r0 / 2)) * 2,
        int(np.floor(r1 / 2)) * 2,
        int(np.ceil(c0 / 2)) * 2,
        int(np.floor(c1 / 2)) * 2,
    ]


def chart_scene(chart, geometry, spectral):
    """Return spectral photons and small JSON metadata with native-coordinate ROIs.

    ``spectral`` is the existing ``_spectra(workspace, 'D65', 1000)`` result. The
    analytic pattern is evaluated at every input/halo coordinate. Level stripes
    continue outside the native crop; objects only exist at their fixed native
    coordinates. No edge replication or zero padding is used.
    """
    if chart not in {"black", "dark_contrast"}:
        raise ValueError("Unknown ghost-specific chart")
    native, full, bounds = _geometry(geometry)
    white = np.asarray(spectral["white"], dtype=float)
    if white.ndim != 1 or not white.size or not np.isfinite(white).all() or (white < 0).any():
        raise ValueError("Chart white must be a finite nonnegative spectral vector")
    h, w = native
    yy, xx = np.indices(full, dtype=float)
    yy, xx = yy - bounds[0], xx - bounds[2]
    metadata = {
        "schema": SCHEMA,
        "chart": chart,
        "native_shape_row_col": list(native),
        "input_shape_row_col": list(full),
        "crop_bounds_row_col": list(bounds),
        "coordinate_convention": "native row/column bounds, inclusive start and exclusive end",
        "halo": "same analytic coordinate formula; no edge padding",
        "illuminant": "D65",
        "reference_white_luminance_cd_m2": 1000.0,
        "level_unit": "fraction of supplied D65 reference-white photons",
        "source": "external optical point source; not painted into chart pixels",
        "measurement_roi": _roi(16, h - 16, 16, w - 16),
        "targets": [],
        "scope": "synthetic diagnostic; no flare certification or detection probability",
    }
    if chart == "black":
        metadata["description"] = "Zero-radiance background; only transported source light remains"
        return np.zeros((*full, white.size), dtype=float), metadata
    row_index = np.clip(np.floor(yy * 4 / h).astype(int), 0, 3)
    pattern = np.asarray(LEVELS)[row_index].copy()
    for row, level in enumerate(LEVELS):
        for col, contrast in enumerate(CONTRASTS):
            cell_h, cell_w = h / 4, w / 3
            object_bounds = [
                (row + 0.20) * cell_h,
                (row + 0.80) * cell_h,
                (col + 0.12) * cell_w,
                (col + 0.43) * cell_w,
            ]
            r0, r1, c0, c1 = object_bounds
            obj = (yy >= r0) & (yy < r1) & (xx >= c0) & (xx < c1)
            pattern[obj] = level * (1 + contrast)
            object_roi = _roi(
                (row + 0.35) * cell_h,
                (row + 0.65) * cell_h,
                (col + 0.20) * cell_w,
                (col + 0.35) * cell_w,
            )
            background_roi = _roi(
                (row + 0.35) * cell_h,
                (row + 0.65) * cell_h,
                (col + 0.64) * cell_w,
                (col + 0.79) * cell_w,
            )
            metadata["targets"].append(
                {
                    "id": f"level-{row + 1}-contrast-{round(contrast * 100):02d}",
                    "background_level": level,
                    "object_level": level * (1 + contrast),
                    "declared_weber_contrast": contrast,
                    "object_bounds_row_col": object_bounds,
                    "object_roi": object_roi,
                    "background_roi": background_roi,
                }
            )
    metadata["description"] = "Four dark gray levels with 5%, 10%, 20% brighter fixed objects"
    metadata["background_levels"] = list(LEVELS)
    return pattern[..., None] * white, metadata


def _captures(arrays, native):
    electrons = np.asarray(arrays["electron_frames"], dtype=float)
    linear = np.asarray(arrays["linear_frames"], dtype=float)
    if electrons.ndim != 3 or electrons.shape[1:] != native or not electrons.shape[0]:
        raise ValueError("electron_frames must be nonempty T,H,W matching the native chart")
    if linear.shape != (*electrons.shape, 3):
        raise ValueError("linear_frames must match the electron frames and have three channels")

    def flags(key, default=None):
        value = np.asarray(arrays.get(key, default))
        if value.shape != electrons.shape or not np.isin(value, [0, 1]).all():
            raise ValueError(f"{key} must be a matching boolean T,H,W array")
        return value.astype(bool)

    valid = flags("valid_frames")
    raw_valid = flags("raw_valid_frames", valid)
    for key in ("well_saturated_frames", "adc_saturated_frames"):
        if key in arrays:
            raw_valid &= ~flags(key)
    # Full-region comparison requires the same physical support at both stages.
    common = (valid & raw_valid & np.isfinite(electrons) & np.isfinite(linear).all(axis=-1)).all(
        axis=0
    )
    return electrons, linear @ Y_WEIGHTS, common


def _region(capture, bounds):
    e, y, support = capture
    r0, r1, c0, c1 = map(int, bounds)
    if not (0 <= r0 < r1 <= e.shape[1] and 0 <= c0 < c1 <= e.shape[2]):
        raise ValueError("Measurement ROI is empty or outside the native image")
    sl = (slice(r0, r1), slice(c0, c1))
    mask = support[sl]
    if not mask.all():
        return {
            "status": "unavailable",
            "reason": "invalid_or_saturated_support",
            "valid_fraction": float(mask.mean()),
        }
    es, ys = e[:, sl[0], sl[1]], y[:, sl[0], sl[1]]
    return {
        "status": "ok" if len(e) > 1 else "partial",
        "reason": None if len(e) > 1 else "at_least_two_repeats_required_for_noise",
        "valid_fraction": 1.0,
        "mean_e": float(es.mean()),
        "mean_y": float(ys.mean()),
        # Shift by the first frame to avoid a tiny numerical variance from
        # repeatedly summing identical non-binary floating-point values.
        "temporal_variance_e": float(np.var(es - es[:1], axis=0, ddof=1).mean())
        if len(e) > 1
        else None,
        "temporal_variance_y": float(np.var(ys - ys[:1], axis=0, ddof=1).mean())
        if len(e) > 1
        else None,
    }


def _pair(capture, target):
    obj = _region(capture, target["object_roi"])
    bg = _region(capture, target["background_roi"])
    result = {
        "status": "unavailable",
        "reason": "invalid_or_saturated_support",
        "object": obj,
        "background": bg,
        "observed_contrast": None,
        "cnr": None,
        "temporal_noise_e": None,
        "temporal_noise_linear_y": None,
    }
    if obj["status"] == "unavailable" or bg["status"] == "unavailable":
        return result
    difference = obj["mean_y"] - bg["mean_y"]
    result["status"] = "ok"
    result["reason"] = None
    if abs(bg["mean_y"]) > 1e-15:
        result["observed_contrast"] = difference / abs(bg["mean_y"])
    else:
        result.update(status="partial", reason="zero_background_for_contrast")
    if obj["temporal_variance_y"] is None:
        result.update(status="partial", reason="at_least_two_repeats_required_for_noise")
        return result
    variance = obj["temporal_variance_y"] + bg["temporal_variance_y"]
    result["temporal_noise_e"] = float(
        np.sqrt((obj["temporal_variance_e"] + bg["temporal_variance_e"]) / 2)
    )
    result["temporal_noise_linear_y"] = float(np.sqrt(variance / 2))
    if variance > 0:
        result["cnr"] = difference / np.sqrt(variance)
    else:
        result.update(status="partial", reason="zero_temporal_variance_for_cnr")
    return result


def measure_chart(arrays, baseline, metadata):
    """Fixed-mask contrast/CNR and signed black excess relative to a named caller baseline.

    Baseline/source-off/direct-only labels belong to the job. All arrays use the
    same fixed electron-to-linear-color transform. CNR is signed object minus
    background mean divided by sqrt(sum of mean per-pixel temporal variances),
    not the standard error of a large ROI average.
    """
    if metadata.get("schema") != SCHEMA or metadata.get("chart") not in {"black", "dark_contrast"}:
        raise ValueError("Expected a supported ghost chart metadata contract")
    native = tuple(metadata["native_shape_row_col"])
    capture = _captures(arrays, native)
    reference = None if baseline is None else _captures(baseline, native)
    result = {
        "schema": "camera_reference_ghost_chart_measurements_v1",
        "chart": metadata["chart"],
        "status": "ok",
        "targets": [],
        "repeat_count": len(capture[0]),
        "contrast_definition": "signed (mean object Y - mean background Y) / abs(background Y)",
        "noise_definition": "sqrt(mean per-pixel temporal variance, ddof=1); no spatial texture",
        "cnr_definition": (
            "signed mean Y difference / "
            "sqrt(object temporal variance + background temporal variance)"
        ),
        "electron_basis": "signed black/dark-subtracted electrons; equally represented RGGB sites",
        "support_policy": "entire fixed ROI valid in every repeat; no shrinking to valid pixels",
        "interpretation": "diagnostic contrast/CNR; not detection or visibility probability",
    }
    if metadata["chart"] == "black":
        region = _region(capture, metadata["measurement_roi"])
        black = {**region, "mean_excess_e": None, "peak_excess_e": None, "minimum_excess_e": None}
        black["excess_definition"] = (
            "current minus baseline temporal mean at identical native pixels; signed, unclipped"
        )
        if reference is not None:
            ref_region = _region(reference, metadata["measurement_roi"])
            if region["status"] != "unavailable" and ref_region["status"] != "unavailable":
                r0, r1, c0, c1 = metadata["measurement_roi"]
                difference = capture[0].mean(axis=0) - reference[0].mean(axis=0)
                values = difference[r0:r1, c0:c1]
                black.update(
                    mean_excess_e=float(values.mean()),
                    peak_excess_e=float(values.max()),
                    minimum_excess_e=float(values.min()),
                )
            else:
                black.update(status="unavailable", reason="invalid_or_saturated_comparison_support")
        result.update(status=black["status"], black_level=black)
        return result
    for target in metadata["targets"]:
        current = _pair(capture, target)
        row = {
            **target,
            **current,
            "baseline_contrast": None,
            "contrast_retention": None,
            "baseline_cnr": None,
        }
        if reference is not None:
            ref = _pair(reference, target)
            row["baseline_status"] = ref["status"]
            row["baseline_contrast"], row["baseline_cnr"] = ref["observed_contrast"], ref["cnr"]
            if (
                current["observed_contrast"] is not None
                and ref["observed_contrast"] is not None
                and ref["observed_contrast"] > 1e-15
            ):
                row["contrast_retention"] = current["observed_contrast"] / ref["observed_contrast"]
            else:
                row.update(status="unavailable", reason="invalid_or_nonpositive_baseline_contrast")
        result["targets"].append(row)
    statuses = [row["status"] for row in result["targets"]]
    result["status"] = (
        "ok"
        if all(s == "ok" for s in statuses)
        else "partial"
        if any(s != "unavailable" for s in statuses)
        else "unavailable"
    )
    result["valid_targets"] = sum(s == "ok" for s in statuses)
    return result
