"""Common-support metrics and joint iris/exposure selection for lateral motion.

The integration adapter supplies a shared native rectangle after motion and
ISP support guards. Invalid padded borders must never improve the noise or
saturation scores. No exposure or aperture is selected from a failed SFR.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import db_design_aperture as aperture

DEFAULT_POLICY = {**aperture.DEFAULT_POLICY, "min_relative_signal": 0.0}


def edge_support_roi(edge, native_shape, isp_guard_px=8):
    """Native support contributing to the fixed central processed-edge ROI."""
    if (
        type(isp_guard_px) is not int or isp_guard_px < 0
        or not isinstance(edge, (list, tuple)) or len(edge) != 4
        or any(type(v) is not int for v in edge)
        or len(native_shape) != 2
        or any(type(v) is not int or v <= 0 for v in native_shape)
    ):
        raise ValueError("Invalid central edge support geometry")
    bounds = [edge[0] - isp_guard_px, edge[1] + isp_guard_px,
              edge[2] - isp_guard_px, edge[3] + isp_guard_px]
    if not (0 <= bounds[0] < bounds[1] <= native_shape[0]
            and 0 <= bounds[2] < bounds[3] <= native_shape[1]
            and edge[0] < edge[1] and edge[2] < edge[3]):
        raise ValueError("Central edge and ISP support extend outside the native crop")
    return bounds


def measure_field(field_root, rendered_field, *, valid_rect, expected_frames=8, isp_guard_px=8):
    """Measure only a common motion-and-ISP-supported rectangle.

    ``valid_rect`` already excludes the ISP guard; it is not eroded again.
    The fixed central edge must be fully contained. High clipping in that
    edge or its ISP influence support invalidates its SFR. Clipping elsewhere
    is counted only by the common-rectangle saturation policy.
    """
    measured = aperture.measure_field(
        field_root, rendered_field, expected_frames=expected_frames, roi_row_col=valid_rect
    )
    bounds = measured["measurement"]["roi_row_col"]
    edge = measured["measurement"]["processed_sfr_roi"]
    reasons = []
    if (
        not isinstance(edge, (list, tuple)) or len(edge) != 4
        or any(type(v) is not int for v in edge)
        or not (bounds[0] <= edge[0] < edge[1] <= bounds[1]
                and bounds[2] <= edge[2] < edge[3] <= bounds[3])
    ):
        reasons.append("central_edge_roi_outside_motion_and_ISP_support")
    capture = json.loads((Path(field_root) / "capture.json").read_text())
    try:
        sfr_support = edge_support_roi(edge, capture["geometry"]["native_shape_row_col"], isp_guard_px)
    except ValueError:
        sfr_support = None
        reasons.append("central_edge_and_ISP_support_outside_native_crop")
    saturation_keys = ("well_saturated_mask", "adc_high_mask", "digital_high_mask")
    resolution_saturation = []
    for record in capture["captures"]["resolution"]["raw"]:
        if sfr_support is None:
            break
        if record.get("sfr_support_roi_row_col") != sfr_support:
            raise ValueError("Per-frame SFR saturation support differs from the central edge and ISP support")
        fractions = record.get("sfr_support_saturation_fractions", {})
        values = [fractions.get(key) for key in saturation_keys]
        if any(not aperture._number(v) or not 0 <= v <= 1 for v in values):
            reasons.append("resolution_sfr_missing_supported_saturation_evidence")
            break
        resolution_saturation.append(min(1.0, sum(values)))
    high_fraction = max(resolution_saturation) if len(resolution_saturation) == expected_frames else None
    if high_fraction is not None and high_fraction > 0:
        reasons.append("resolution_sfr_censored_by_well_or_readout_saturation")
    if reasons:
        measured.update(
            processed_sfr50_cpp=None,
            processed_sfr_status="unavailable",
            processed_sfr_reason=reasons[0],
            measurement_status="inconclusive",
        )
        measured["measurement_issues"].extend(
            reason for reason in reasons if reason not in measured["measurement_issues"]
        )
    measured["measurement"].update(
        common_motion_support_row_col=bounds,
        support_definition="Shared native support after motion integration and ISP guard; "
        "same rectangle across all light/speed cells and aperture/exposure candidates",
        resolution_high_saturation_upper_bound=high_fraction,
        sfr_support_roi_row_col=sfr_support,
        sfr_isp_guard_px=isp_guard_px,
        sfr_clipping_policy="Any well, ADC-high or digital-high clipping in the central "
        "edge ROI plus its ISP influence support invalidates SFR; clipping elsewhere "
        "remains subject to the common-rectangle saturation constraint",
    )
    return measured


def assess(rows, policy=None):
    """Joint choice with unchanged aperture constraints and exposure tie-break."""
    selection = aperture.assess(rows, {**DEFAULT_POLICY, **(policy or {})})
    by_id = {row["candidate_id"]: row for row in rows}
    feasible = []
    for decision in selection["decisions"]:
        row = by_id[decision["candidate_id"]]
        exposure, blur = row.get("exposure_ms"), row.get("blur_length_px")
        if not aperture._number(exposure) or exposure <= 0:
            decision["reasons"].append("invalid_exposure_ms")
        if not aperture._number(blur) or blur < 0:
            decision["reasons"].append("invalid_blur_length_px")
        decision["eligible"] = not decision["reasons"]
        if decision["eligible"]:
            feasible.append((
                -decision["worst_field_sfr50_cpp"], -decision["worst_field_snr_db"],
                exposure, row["f_number"], row["candidate_id"],
            ))
    feasible.sort()
    eligible_ids = [record[-1] for record in feasible]
    selection.update(
        recommended_candidate_id=eligible_ids[0] if eligible_ids else None,
        eligible_candidate_ids=eligible_ids,
        status="selected" if feasible else "no_feasible_candidate",
        tie_breakers=["higher worst-field SNR", "shorter exposure", "lower applied F-number", "candidate ID"],
        scope="Joint iris/exposure demo selection conditional on fixed charts, scene luminance, "
        "declared constant-depth lateral motion, readout, ISP and policy; not a product optimum",
    )
    return selection
