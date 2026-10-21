"""Source-bound IMX462 dimensions and explicitly assumed full-array timing.

The original TechInsights catalog revision has no row/column counts. Sony's
newly acquired public flyer supplies counts, not a register-level scan mode.
Line time, active-array placement and coordinate orientation remain assumptions.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path

import numpy as np

SCHEMA = 'camera_db_sensor_row_timing_profile_v1'
PROFILE_ID = 'imx462lqr_full_active_assumed_scan_v1'
REVISION = '0ccf0fae-faa3-5001-88d5-0e30d37bfec6'
REVISION_SHA = '67bd5db3cfc8c86734ab1ee03eedfff72a87308a09d6a994d2e2c56d0621878b'
PDF_SHA = '98aab78ddd5d86b9c12d0b98cfda06ccbc3950dab71c511bd9ff694422a4232a'
SOURCE_RECORD_SHA = '0aecc485f3100ea49a6ac3f6b3b990d774861c02dad71f37e093aba239836b11'
SOURCE_DIRECTORY = 'camerae2e_db/sources/sensor_timing/imx462'
SOURCE_URL = 'https://www.sony-semicon.com/files/62/pdf/p-12_IMX462LQR_LQR1_LLR_Flyer.pdf'


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _digest(value):
    return _sha(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())


def validate_line_times(axis):
    """Declared microseconds per native row; never inferred from an advertised FPS."""
    if (not isinstance(axis, list) or not 2 <= len(axis) <= 8
            or any(type(v) not in (int, float) or not math.isfinite(v) or not (v == 0 or .1 <= v <= 100) for v in axis)
            or axis[0] != 0 or any(a >= b for a, b in zip(axis, axis[1:]))):
        raise ValueError('line_time_us requires 2..8 ascending unique values: zero GS or [0.1,100] us')
    return [float(v) for v in axis]


def profile(workspace):
    """Read and verify source identities without mutating the historical catalog."""
    root = Path(workspace).resolve()
    source_root = root / SOURCE_DIRECTORY
    pdf = (source_root / 'sony-imx462-family-flyer.pdf').read_bytes()
    source_bytes = (source_root / 'sony-imx462-family-flyer.source.json').read_bytes()
    if _sha(pdf) != PDF_SHA or _sha(source_bytes) != SOURCE_RECORD_SHA:
        raise ValueError('Pinned Sony timing source bytes changed or are unavailable')
    source = json.loads(source_bytes)
    if (source.get('sha256') != PDF_SHA or source.get('source_url') != SOURCE_URL
            or source.get('variant') != 'IMX462LQR Ver2.0'):
        raise ValueError('Sony source variant or document binding differs')
    with sqlite3.connect((root / 'camerae2e_db/catalog.sqlite').as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        revision = db.execute('SELECT component_id,content_sha256,snapshot_json FROM model_revisions '
                              'WHERE revision_id=?', (REVISION,)).fetchone()
        geometry = db.execute('SELECT pixel_pitch_um,width_px,height_px,active_width_mm,active_height_mm '
                              'FROM sensor_geometry WHERE revision_id=?', (REVISION,)).fetchone()
    if (revision is None or geometry is None or revision['content_sha256'] != REVISION_SHA
            or _sha(revision['snapshot_json'].encode()) != REVISION_SHA):
        raise ValueError('Pinned legacy sensor revision changed or is unavailable')
    snapshot = json.loads(revision['snapshot_json'])
    if (snapshot.get('metadata', {}).get('code') != 'DEF-2011-804'
            or geometry['pixel_pitch_um'] != 2.9
            or any(geometry[key] is not None for key in ('width_px', 'height_px', 'active_width_mm', 'active_height_mm'))):
        raise ValueError('Legacy dimension provenance differs from the pinned missing-value contract')
    pitch = 2.9
    optical_axis = [548., 968.]
    active_origin = [12, 4]
    layout = []
    for identity, label, origin in (('center', '중심', [420, 840]), ('outer-ne', '센서 우상단', [64, 1616])):
        center = [coordinate + 127.5 for coordinate in origin]
        center_xy = [(center[1] - optical_axis[1]) * pitch / 1000,
                     -(center[0] - optical_axis[0]) * pitch / 1000]
        # A local stationary PSF has an explicit anchor inside the ROI. The
        # central ROI uses its on-axis native pixel; row timing still follows
        # every actual active row and never recenters on this optical anchor.
        anchor = optical_axis.copy() if identity == 'center' else center.copy()
        anchor_xy = [(anchor[1] - optical_axis[1]) * pitch / 1000,
                     -(anchor[0] - optical_axis[0]) * pitch / 1000]
        layout.append({
            'id': identity, 'label': label, 'origin_row_col': origin, 'shape_row_col': [256, 256],
            'center_active_row_col': center,
            'center_total_row_col': [center[i] + active_origin[i] for i in range(2)],
            'image_xy_mm': center_xy,
            'roi_center_image_xy_mm': center_xy,
            'optical_anchor_active_row_col': anchor,
            'optical_anchor_local_row_col': [anchor[i] - origin[i] for i in range(2)],
            'optical_anchor_image_xy_mm': anchor_xy,
            'optical_anchor_definition': 'one local-stationary PSF/pupil response at the declared anchor; '
                'central ROI uses its on-axis pixel, distinct from its half-pixel geometric center',
            'input_halo_native_px': 64,
            'placement_classification': 'authored_design_assumption_within_vendor_active_dimensions',
            'coordinate_convention': 'active row increases down, col increases right; optical x right and y up',
            'cfa_origin': {'pattern': 'RGGB', 'active_origin_phase': 'assumed', 'local_pattern': 'RGGB'},
        })
    result = {
        'schema': SCHEMA, 'profile_id': PROFILE_ID, 'sensor_name': 'IMX462 structural model / IMX462LQR public dimensions',
        'total_shape_row_col': [1109, 1945], 'effective_shape_row_col': [1097, 1945],
        'active_shape_row_col': [1097, 1937], 'recording_shape_row_col': [1080, 1920],
        'pixel_pitch_um': pitch, 'active_to_total_origin_row_col': active_origin,
        'reference_active_row': 548., 'reference_total_row': 560.,
        'optical_axis_active_row_col': optical_axis, 'roi_layout': layout,
        'legacy_catalog': {'revision_id': REVISION, 'component_id': revision['component_id'],
            'snapshot_sha256': REVISION_SHA, 'report_code': 'DEF-2011-804',
            'dimensions_before_enrichment': dict(geometry), 'catalog_modified': False},
        'dimension_source': {'classification': 'newly_acquired_official_vendor_document',
            'url': SOURCE_URL, 'document_sha256': PDF_SHA, 'source_record_sha256': SOURCE_RECORD_SHA,
            'document_relative_path': SOURCE_DIRECTORY + '/sony-imx462-family-flyer.pdf',
            'source_record_relative_path': SOURCE_DIRECTORY + '/sony-imx462-family-flyer.source.json',
            'variant': 'IMX462LQR Ver2.0', 'pages_1_based': [1, 2],
            'extraction': 'manually checked Device Structure table on page 2',
            'exact_variant_match_to_TechInsights_unit': 'not_verified',
            'source_redistribution': 'not_established; package factual values and source digest only'},
        'acquired_values': {
            'total_shape_row_col': [1109, 1945], 'effective_shape_row_col': [1097, 1945],
            'active_shape_row_col': [1097, 1937], 'recording_shape_row_col': [1080, 1920],
            'pixel_pitch_um': pitch, 'optical_black_vertical_front_rows': 10,
            'optical_black_vertical_rear_rows': 0,
            'advertised_max_full_hd_fps': 120,
            'fps_use': 'informational only; neither line time nor frame timing is derived from this maximum'},
        'assumptions': {
            'line_time_us': 'user-declared sensitivity axis; no verified HMAX/clock/mode or measured line time',
            'active_to_total_origin_row_col': {'value': active_origin,
                'basis': 'all 12 total-minus-active rows assumed leading; horizontal difference centered',
                'warning': 'front OB count10 does not locate the remaining2 rows; this is not a vendor register fact'},
            'row_order': 'top_to_bottom_sequential_native_rows',
            'total_row_clock': 'one line period for each of 1109 total rows; frame blanking/cadence unspecified',
            'optical_axis_active_row_col': optical_axis,
            'optical_orientation': 'optical x right, optical y up, sensor row down; mounting/flip not verified',
            'cfa_phase': 'RGGB at active origin; even ROI origins preserve this assumed phase',
            'roi_placement': 'chosen native crops with optical halo, not extracted vendor mode windows',
            'optical_field_sampling': 'one local-stationary PSF per ROI at explicit optical anchor; '
                'no within-ROI field-gradient or distortion transport',
            'mode': 'full-active behavioral scan, not a decoded product mode; no binning/skipping/DOL',
            'zero_line_time': 'matched hypothetical global shutter; not an IMX462 global-shutter product claim'},
        'missing_product_information': ['verified_line_time', 'HMAX', 'VMAX', 'mode_registers',
            'active_to_total_row_origin', 'active_to_total_col_origin', 'row_order_and_mirror_state',
            'native_CFA_phase', 'frame_blanking_and_cadence', 'measurement_validation'],
        'physical_validation': 'not_established',
    }
    result['profile_sha256'] = _digest(result)
    return result


def field_timing(sensor_profile, field_id, line_us, exposure_ms):
    """All crops share active-row548 midpoint zero, never their own crop center."""
    if (sensor_profile.get('schema') != SCHEMA or sensor_profile.get('profile_id') != PROFILE_ID
            or sensor_profile.get('active_shape_row_col') != [1097, 1937]
            or sensor_profile.get('total_shape_row_col') != [1109, 1945]
            or sensor_profile.get('reference_active_row') != 548.):
        raise ValueError('Unsupported or altered sensor timing profile')
    supplied = dict(sensor_profile)
    fingerprint = supplied.pop('profile_sha256', None)
    if fingerprint != _digest(supplied):
        raise ValueError('Sensor timing profile content differs from its bound hash')
    if (type(line_us) not in (int, float) or not math.isfinite(line_us) or not (line_us == 0 or .1 <= line_us <= 100)
            or type(exposure_ms) not in (int, float) or not math.isfinite(exposure_ms) or not 0 < exposure_ms <= 1000):
        raise ValueError('Line time and exposure require finite supported native-row units')
    matches = [field for field in sensor_profile['roi_layout'] if field['id'] == field_id]
    if len(matches) != 1:
        raise ValueError('Unknown sensor ROI')
    field = matches[0]
    active_rows = field['origin_row_col'][0] + np.arange(field['shape_row_col'][0])
    total_rows = active_rows + sensor_profile['active_to_total_origin_row_col'][0]
    period = float(line_us) * 1e-6
    mids = (active_rows - sensor_profile['reference_active_row']) * period
    exposure_s = float(exposure_ms) / 1000
    starts, ends = mids - exposure_s / 2, mids + exposure_s / 2
    total_reference = sensor_profile['reference_total_row']
    return {
        'schema': 'camera_sensor_located_roi_row_timing_v1', 'profile_id': PROFILE_ID,
        'profile_sha256': fingerprint, 'field_id': field_id, 'line_time_us': float(line_us),
        'line_time_ms': float(line_us) / 1000, 'exposure_ms': float(exposure_ms),
        'shutter_mode': 'global' if line_us == 0 else 'rolling',
        'active_row_indices': active_rows.tolist(), 'total_row_indices': total_rows.tolist(),
        'origin_active_row_col': field['origin_row_col'],
        'origin_total_row_col': [field['origin_row_col'][i] + sensor_profile['active_to_total_origin_row_col'][i] for i in range(2)],
        'row_exposure_start_s': starts.tolist(), 'row_exposure_mid_s': mids.tolist(),
        'row_exposure_end_s': ends.tolist(),
        'roi_center_mid_s': float(np.mean([mids[0], mids[-1]])),
        'local_roi_scan_ms': float((len(active_rows) - 1) * period * 1000),
        'readout_ms': float((len(active_rows) - 1) * period * 1000),
        'readout_definition': 'local ROI first-to-last row midpoint span; not full-sensor readout',
        'active_scan_ms': 1096 * period * 1000, 'total_scan_ms': 1108 * period * 1000,
        'total_row0_mid_s': -total_reference * period,
        'total_row0_exposure_start_s': -total_reference * period - exposure_s / 2,
        'total_last_row_mid_s': (1108 - total_reference) * period,
        'global_reference_active_row': 548., 'global_reference_total_row': total_reference,
        'time_reference': 'shared active-array center row548 exposure midpoint t=0 for every ROI',
        'relative_timing_provenance': 'derived from declared line time and chosen active ROI coordinates',
        'total_timing_provenance': 'assumed active-to-total origin and sequential total-row scan',
        'line_time_provenance': 'authored_assumption_not_measured_or_vendor_mode_verified',
        'frame_cadence_s': None, 'physical_validation': 'not_established',
    }
