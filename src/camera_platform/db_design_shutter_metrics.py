"""Separate rolling-shutter geometry from edge-following SFR and RAW quality."""
from __future__ import annotations

import copy
import math

import numpy as np

from . import db_design_motion_metrics as motion


def measure_field(field_root, rendered_field, *, valid_rect, expected_frames=8):
    measured = motion.measure_field(
        field_root, rendered_field, valid_rect=valid_rect, expected_frames=expected_frames
    )
    measured["measurement"]["shutter_sfr_interpretation"] = (
        "SFR follows the locally fitted edge orientation; rolling-shutter skew is a "
        "separate paired geometry measurement and cannot be inferred from SFR alone"
    )
    return measured


def _bounds(value, shape):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) is not int for v in value)):
        return False
    r0, r1, c0, c1 = value
    return 0 <= r0 < r1 <= shape[0] and 0 <= c0 < c1 <= shape[1]


def geometry_from_planes(rolling_planes, global_planes, *, valid_rect, edge_roi, theoretical_skew_px,
                         search_halo_native_px=8):
    """Measure paired edge displacement from generated planes, never the motion input.

    The positive horizontal-gradient centroid locates the rising chart edge in
    each row. Fitting RS-minus-GS locations cancels the existing chart angle and
    same-exposure optical/blur shape. Full-probe skew is explicitly extrapolated
    from this short supported row span, not observed across the entire probe.
    Theory is retained only for a post-measurement residual diagnostic.
    """
    rolling, baseline = np.asarray(rolling_planes), np.asarray(global_planes)
    if (rolling.ndim != 3 or rolling.shape[-1] != 4 or rolling.shape != baseline.shape
            or rolling.dtype.kind not in "iuf" or baseline.dtype.kind not in "iuf"
            or not np.isfinite(rolling).all() or not np.isfinite(baseline).all()
            or np.any(rolling < 0) or np.any(baseline < 0)):
        raise ValueError("Shutter geometry requires matching finite nonnegative R/Gr/Gb/B planes")
    if (isinstance(theoretical_skew_px, bool) or not isinstance(theoretical_skew_px, (int, float))
            or not math.isfinite(theoretical_skew_px)):
        raise ValueError("Theoretical skew must be a finite signed native displacement")
    if type(search_halo_native_px) is not int or not 0 <= search_halo_native_px <= 64:
        raise ValueError("Geometry search halo must be a bounded integer native margin")
    shape = rolling.shape[:2]
    result = {
        "schema": "camera_paired_shutter_edge_geometry_v1", "status": "unavailable", "reason": None,
        "method": "positive_horizontal_gradient_centroid_per_row; linear fit of RS-minus-same-exposure-GS",
        "input": "noise-free mean of co-located Gr/Gb electron-rate planes; no RAW clipping or ISP",
        "reference": "same camera, field, luminance and exposure; global shutter at center-row midpoint",
        "native_shape_row_col": list(shape),
        "edge_roi_row_col": list(edge_roi) if isinstance(edge_roi, (list, tuple)) else None,
        "common_valid_rect_row_col": list(valid_rect) if isinstance(valid_rect, (list, tuple)) else None,
        "row_positions": [],
        "gs_edge_x_px": [], "rs_edge_x_px": [], "relative_displacement_px": [],
        "measured_observed_skew_px": None, "measured_full_probe_skew_px": None,
        "relative_slope_dx_dy": None, "gs_edge_tilt_deg": None, "rs_edge_tilt_deg": None,
        "relative_tilt_deg": None, "fit_rmse_px": None, "fit_r_squared": None,
        "observed_row_span_px": None, "valid_row_count": 0,
        "theoretical_full_probe_skew_px": float(theoretical_skew_px),
        "measured_minus_theoretical_skew_px": None,
        "full_probe_skew_definition": "Fitted relative slope multiplied by native_rows-1; "
        "extrapolation from the reported observed edge rows, not a full-height measurement",
        "physical_validation": "not_established",
    }
    if not _bounds(valid_rect, shape) or not _bounds(edge_roi, shape):
        result["reason"] = "invalid_edge_or_common_support_rectangle"
        return result
    r0, r1, c0, c1 = edge_roi
    # The fixed context is chosen before viewing results, never shifted from
    # theoretical motion. Negative falling patch boundaries are rejected.
    search = [r0, r1, c0 - search_halo_native_px, c1 + search_halo_native_px]
    result["search_roi_row_col"] = search
    vr0, vr1, vc0, vc1 = valid_rect
    if not (vr0 <= r0 < r1 <= vr1 and vc0 <= search[2] < search[3] <= vc1):
        result["reason"] = "paired_edge_search_outside_common_support"
        return result
    green_rs = (rolling[..., 1].astype(float) + rolling[..., 2]) * .5
    green_gs = (baseline[..., 1].astype(float) + baseline[..., 2]) * .5
    x = np.arange(search[2], search[3] - 1, dtype=float) + .5

    def location(profile):
        positive = np.maximum(np.diff(profile), 0.)
        total = float(positive.sum())
        if total <= max(1e-20, float(np.max(np.abs(profile))) * 1e-8):
            return None
        return float(np.dot(x, positive) / total)

    for row in range(r0, r1):
        a = location(green_gs[row, search[2]:search[3]])
        b = location(green_rs[row, search[2]:search[3]])
        if a is not None and b is not None:
            result["row_positions"].append(row)
            result["gs_edge_x_px"].append(a)
            result["rs_edge_x_px"].append(b)
    count = len(result["row_positions"])
    result["valid_row_count"] = count
    result["requested_row_count"] = r1 - r0
    result["valid_row_fraction"] = count / (r1 - r0)
    if count < 12 or count < .9 * (r1 - r0):
        result["reason"] = "insufficient_supported_positive_edge_rows"
        return result
    y = np.asarray(result["row_positions"], float)
    centered_y = y - (shape[0] - 1) / 2
    gs_x, rs_x = np.asarray(result["gs_edge_x_px"]), np.asarray(result["rs_edge_x_px"])
    displacement = rs_x - gs_x
    slope, offset = np.polyfit(centered_y, displacement, 1)
    residual = displacement - (slope * centered_y + offset)
    rmse = float(np.sqrt(np.mean(residual**2)))
    ss_total = float(np.sum((displacement - displacement.mean()) ** 2))
    ss_residual = float(np.sum(residual**2))
    r_squared = 1.0 if ss_total < 1e-20 and ss_residual < 1e-20 else (
        float(1 - ss_residual / ss_total) if ss_total > 0 else None
    )
    gs_slope = float(np.polyfit(centered_y, gs_x, 1)[0])
    rs_slope = float(np.polyfit(centered_y, rs_x, 1)[0])
    full = float(slope * (shape[0] - 1))
    span = float(y[-1] - y[0])
    gs_angle, rs_angle = math.degrees(math.atan(gs_slope)), math.degrees(math.atan(rs_slope))
    result.update(
        status="ok", reason=None, relative_displacement_px=displacement.tolist(),
        measured_observed_skew_px=float(slope * span), measured_full_probe_skew_px=full,
        relative_slope_dx_dy=float(slope), gs_edge_tilt_deg=gs_angle, rs_edge_tilt_deg=rs_angle,
        relative_tilt_deg=rs_angle - gs_angle, fit_rmse_px=rmse, fit_r_squared=r_squared,
        observed_row_span_px=span, midpoint_relative_offset_px=float(offset),
        measured_minus_theoretical_skew_px=full - theoretical_skew_px,
        fit_quality_note="Inspect row coverage, residual and R-squared; no geometry quality threshold "
        "is used to select a readout or make a product claim",
    )
    return result


def assess(rows, policy=None):
    """Choose exposure independently for each readout; never rank readouts by SFR."""
    groups = {}
    identities = [row.get("candidate_id") for row in rows]
    if (any(not isinstance(value, str) or not value for value in identities)
            or len(set(identities)) != len(identities)):
        raise ValueError("Shutter rows require unique candidate IDs across readout groups")
    for row in rows:
        readout = row.get("readout_ms")
        if (isinstance(readout, bool) or not isinstance(readout, (int, float))
                or not math.isfinite(readout) or readout < 0):
            raise ValueError("Shutter rows require finite nonnegative readout_ms")
        expected_mode = "global" if readout == 0 else "rolling"
        if row.get("shutter_mode") != expected_mode:
            raise ValueError("Shutter mode and first-to-last-row readout disagree")
        groups.setdefault(float(readout), []).append(row)
    if 0.0 not in groups:
        raise ValueError("Shutter selection requires the same-exposure global-shutter comparison group")
    selections = {}
    for readout, group in sorted(groups.items()):
        selected = motion.assess(group, policy)
        selected.update(
            readout_ms=readout, shutter_mode="global" if readout == 0 else "rolling",
            scope="Exposure choice within this fixed readout only; edge-following SFR is not "
            "a measure of rolling-shutter geometric fidelity or overall readout superiority",
        )
        # Six-significant-digit formatting aliases distinct configurable scans.
        # Keep familiar integer keys while preserving every fractional value.
        key = str(int(readout)) if readout.is_integer() else repr(readout)
        selections[key] = selected
    result = copy.deepcopy(selections["0"])
    result.update(
        decisions=[copy.deepcopy(decision) for group in selections.values() for decision in group["decisions"]],
        selections_by_readout_ms=selections, default_readout_ms=0.0,
        cross_readout_ranking=False, selection_basis="within_readout_only",
        scope="Separate exposure choices for each readout; top-level selection is the global-shutter "
        "reference only. Report measured geometry independently; no overall readout ranking.",
    )
    return result
