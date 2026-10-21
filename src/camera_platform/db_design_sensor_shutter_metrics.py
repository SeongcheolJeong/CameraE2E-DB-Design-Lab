"""Located sensor ROI displacement alongside row shear and RAW/ISP quality."""
from __future__ import annotations

import math

from .db_design_shutter_metrics import assess, measure_field
from .db_design_shutter_metrics import geometry_from_planes as _paired_geometry


def geometry_from_planes(rolling_planes, global_planes, *, valid_rect, edge_roi,
                         theoretical_skew_px, theoretical_center_displacement_px,
                         active_rows=1097):
    """Measure ROI shift against GS at one common full-sensor reference time.

    The intercept comes from paired image-derived edge positions, not row timing.
    Theory is only a post-measurement residual. No crop-specific time recentering
    or theoretical displacement is used to move the geometry search window.
    """
    if (type(theoretical_center_displacement_px) not in (int, float)
            or not math.isfinite(theoretical_center_displacement_px)
            or type(active_rows) is not int or active_rows < 2):
        raise ValueError('Located sensor geometry requires finite center displacement and active row count')
    result = _paired_geometry(rolling_planes, global_planes, valid_rect=valid_rect,
        edge_roi=edge_roi, theoretical_skew_px=theoretical_skew_px, search_halo_native_px=24)
    result['schema'] = 'camera_sensor_located_paired_edge_geometry_v1'
    result['reference'] = 'same camera, field, luminance and exposure; GS at the shared active-array center row548 midpoint'
    measured = result.get('midpoint_relative_offset_px') if result['status'] == 'ok' else None
    result.update(
        measured_roi_center_displacement_px=measured,
        theoretical_roi_center_displacement_px=float(theoretical_center_displacement_px),
        measured_minus_theoretical_center_displacement_px=(
            measured - theoretical_center_displacement_px if measured is not None else None),
        measured_local_roi_skew_px=result['measured_full_probe_skew_px'],
        measured_active_array_skew_extrapolated_px=(result['relative_slope_dx_dy'] * (active_rows - 1)
            if result['status'] == 'ok' else None),
        active_array_extrapolation_definition='Local fitted relative edge slope times active_rows-1; not a full-frame measurement',
        center_displacement_definition='Paired image edge fit intercept at the native ROI center row; common GS time for all sensor ROIs',
        active_rows=active_rows,
    )
    return result
