"""Mode timing facts, conditional profiles and exposure scheduling for IMX462.

Numeric facts are independently extracted from a module manufacturer's pinned
driver. The driver is evidence only, never loaded or executed by this package.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

SCHEMA = 'camera_sensor_operating_mode_v1'
COMMIT = '59bdf67a21b67572039bd31d8c4e71f66dd2c441'
SOURCE_DIRECTORY = 'camerae2e_db/sources/sensor_timing/imx462/kurokesu'
HASHES = {
    'imx462_mode_tbls.h': '7d28a97708e80a438b0d6c2e0d85f6f793ac04862866610a2dbc55f0a3cb6f37',
    'nv_imx462.c': '69546565fdfed81b3231d6c7bc17e49434aef2e1c53d85a4f42d70a3b29c29b9',
    'tegra234-p3767-camera-p3768-imx462-A.dts': '7715a3a4a893b94d1608990275f082e484505aa2c1dc8625ba48e4beb1aa1d7a',
}
SOURCE_URL = f'https://github.com/Kurokesu/imx462-jetson-driver/tree/{COMMIT}'
SONY_URL = 'https://www.sony-semicon.com/files/62/pdf/p-12_IMX462LQR_LQR1_LLR_Flyer.pdf'
IDS = ('kurokesu_1080p30_raw10', 'assumed_1080p60', 'assumed_1080p120')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def mode(mode_id, fps=None):
    if mode_id not in IDS:
        raise ValueError('Select a supported timing operating_mode_id')
    acquired = mode_id == IDS[0]
    maximum = 30 if acquired else (60 if mode_id == IDS[1] else 120)
    fps = maximum if fps is None else fps
    if (type(fps) not in (int, float) or not math.isfinite(fps)
            or not (2 <= fps <= maximum if acquired else fps == maximum)):
        raise ValueError(f'{mode_id}: FPS must be in [2,30]' if acquired else f'{mode_id}: only declared {maximum} FPS is supported')
    clock = 148_500_000
    hmax = 4400 if acquired else (2200 if maximum == 60 else 1100)
    vmax = max(1125, int(clock / hmax / fps))
    line_us = hmax / clock * 1e6
    period_ms = vmax * line_us / 1000
    result = {
        'schema': SCHEMA, 'id': mode_id,
        'label': f'1080p {maximum}fps · '+('제조사 드라이버 근거' if acquired else 'line time 가정'),
        'resolution_row_col': [1080, 1920],
        'classification': 'module_manufacturer_driver_derived' if acquired else 'authored_timing_assumption',
        'line_time_classification': 'derived_from_acquired_HMAX_and_clock' if acquired else 'assumed_unverified_HMAX_and_clock',
        'hmax': hmax, 'timing_clock_hz': clock, 'vmax': vmax,
        'line_time_us': line_us, 'frame_period_ms': period_ms,
        'frame_length_lines': vmax, 'physical_total_rows':1109,
        'mode_blanking_lines_derived':vmax-1109,
        'requested_fps': float(fps), 'actual_fps': 1000 / period_ms,
        'min_fps': 2 if acquired else maximum, 'max_fps': maximum,
        'source_output_bit_depth': 10 if acquired else None,
        'requested_exposure_min_us': 30 if acquired else line_us,
        'requested_exposure_max_us': 33274 if acquired else (vmax - 2) * line_us,
        'coarse_min_lines': 1, 'coarse_max_lines': vmax - 2,
        'exposure_quantization': 'nearest whole line, ties upward; same actual exposure used for GS control',
        'source_url': SOURCE_URL if acquired else SONY_URL,
        'source_commit': COMMIT if acquired else None,
        'source_references': ({
            'hmax': 'imx462_mode_tbls.h:190-191',
            'clock_fps_exposure': 'tegra234-p3767-camera-p3768-imx462-A.dts:247-283',
            'vmax_and_shutter_formula': 'nv_imx462.c:199-206,252-297',
        } if acquired else {}),
        'assumptions': [
            'Driver timing is transferred to the existing structural sensor model; exact TechInsights sample/module variant match not verified',
            'Stored electrical noise, well and ADC model retained; module RAW10 register profile is timing evidence, not readout calibration',
            'Active-to-total origin, ROI layout, mounting, CFA phase and row order retain the separate geometry assumptions',
            'Row result availability assumes end of integration; CSI/ISP transfer latency not obtained',
            'Eight noise realizations are independent repeat acquisitions, not a time-evolving frame sequence',
        ] + ([] if acquired else [
            'Sony maximum FPS does not specify this line period',
            f'HMAX {hmax}, timing clock 148.5MHz, VMAX1125 and two-line exposure guard are authored sensitivity assumptions',
            'Interface lane count, ADC selection, HMAX minimum and mode register table not obtained',
        ]),
        'frame_policy': 'fixed requested cadence; overlong exposures rejected, never extend the frame silently',
        'physical_validation': 'not_established',
    }
    result['mode_sha256'] = digest(result)
    return result


def catalog(workspace):
    folder = Path(workspace) / SOURCE_DIRECTORY
    for name, expected in HASHES.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Mode timing evidence changed: '+name)
    source = json.loads((folder/'source.json').read_text())
    if source['commit'] != COMMIT or any(source['files'][n]['sha256'] != sha for n, sha in HASHES.items()):
        raise ValueError('Mode source record differs from pinned evidence')
    return {
        'schema': 'camera_sensor_mode_catalog_v1', 'modes': [mode(i) for i in IDS],
        'source_files': copy.deepcopy(source),
        'sensor_vendor_register_datasheet': 'not_obtained',
        'legacy_DB_mode': 'No sensor_readout_modes row for the pinned TechInsights revision; line period, clock, frame period and exposure limits absent',
        'unresolved_modes': [{'label': 'Sony 720p120', 'max_fps': 120,
            'line_time_us': None, 'status': 'not_executable_with_fixed_full_active_ROI_geometry',
            'missing': ['window placement', 'row mapping', 'HMAX/VMAX/clock', 'exposure limits']}],
    }


def exposure(mode_value, requested_ms):
    """Return a schedulable actual exposure, or explicit reasons with no capture."""
    if type(requested_ms) not in (int, float) or not math.isfinite(requested_ms) or requested_ms <= 0:
        raise ValueError('Exposure requires finite positive milliseconds')
    value = dict(mode_value); fingerprint = value.pop('mode_sha256', None)
    if fingerprint != digest(value):
        raise ValueError('Operating mode fingerprint differs')
    us = requested_ms * 1000
    reasons = []
    if us < value['requested_exposure_min_us']:
        reasons.append('below_mode_exposure_minimum')
    if us > value['requested_exposure_max_us'] + 1e-9:
        reasons.append('above_mode_exposure_maximum')
    lines = math.floor(us / value['line_time_us'] + .5)
    if not value['coarse_min_lines'] <= lines <= value['coarse_max_lines']:
        reasons.append('outside_coarse_integration_or_fixed_frame_budget')
    actual = lines * value['line_time_us'] / 1000
    return {'requested_exposure_ms': float(requested_ms), 'actual_exposure_ms': actual if not reasons else None,
        'coarse_integration_lines': lines if not reasons else None,
        'shs1_register': value['vmax'] - lines - 1 if not reasons else None,
        'status': 'eligible' if not reasons else 'blocked', 'reasons': reasons,
        'mode_id': value['id'], 'mode_sha256': fingerprint,
        'frame_period_ms': value['frame_period_ms'], 'actual_fps': value['actual_fps'],
        'policy': value['frame_policy']}


def schedule(mode_value, requested_exposures):
    rows = [exposure(mode_value, e) for e in requested_exposures]
    accepted = [row for row in rows if row['status'] == 'eligible']
    if not accepted:
        raise ValueError('No requested exposure fits this FPS/mode; no RAW will be generated')
    if len({row['actual_exposure_ms'] for row in accepted}) != len(accepted):
        raise ValueError('Requested exposure values collapse to the same coarse-line exposure')
    return rows


def apply_clock(clock, mode_value, exposure_value):
    """Attach cadence to both real RS and the matched hypothetical GS control."""
    result = dict(clock)
    result.update(operating_mode=mode_value, exposure_schedule=exposure_value,
        assumptions=mode_value['assumptions'],
        requested_exposure_ms=exposure_value['requested_exposure_ms'],
        actual_exposure_ms=exposure_value['actual_exposure_ms'],
        frame_cadence_s=mode_value['frame_period_ms']/1000,
        frame_period_ms=mode_value['frame_period_ms'], actual_fps=mode_value['actual_fps'],
        timing_scope='source-derived or explicitly assumed operating-mode timing; stored structural readout model retained',
        line_time_provenance=(mode_value['line_time_classification'] if clock['line_time_us'] else 'hypothetical_matched_GS_control'),
        frame_start_reference='Frame n is translated by n*frame_cadence_s from shared row548 midpoint zero')
    result['row_exposure_next_frame_start_s'] = [t+result['frame_cadence_s'] for t in result['row_exposure_start_s']]
    result['next_frame_reference_mid_s'] = result['frame_cadence_s']
    return result
