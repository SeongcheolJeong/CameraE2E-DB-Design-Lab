"""Virtual native-probe rolling shutter on source-bound full electron-rate planes.

Row timing changes the optical/pixel response BEFORE CFA sampling and electrical
capture. Motion and exposure use one exact integral of the native linear basis,
not a post-RAW shear or a second interpolation of an already blurred image.
"""

from __future__ import annotations

import copy
import math
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np
from scipy.ndimage import convolve1d

from . import db_design_conditions as acquisition
from . import db_design_motion as motion
from . import db_design_shutter_metrics as metrics
from . import design_workshop_engine as shared
from .virtual_lab_job import sha256

SCHEMA = 'camera_db_aperture_shutter_result_v1'
MODEL_SCHEMA = 'camera_virtual_probe_rolling_shutter_v1'
TIMING_ARRAY_KEYS = ('row_exposure_start_s', 'row_exposure_mid_s', 'row_exposure_end_s',
                     'row_mid_displacement_px')
SCOPE = (
    'Virtual 256-row native probe: readout_ms is the first-to-last row exposure-midpoint scan, '
    'not a full sensor product readout or transport latency. Fixed camera and horizontal constant '
    'transverse target velocity; one native linear-basis integral per row before CFA/readout. '
    'Distance sets nominal pinhole image velocity only; chart scale, focus, finite conjugates and '
    'source PSF remain unchanged. Original source diffraction/pixel response, common CCM/ISP/display. '
    'No rolling-shutter correction, new sub-native detail, 3D occlusion, independently moving '
    'foreground, vertical motion or physical product validation. Separate exposure selection '
    'within each readout; SFR does not rank global versus rolling shutter because shear changes edge geometry.'
)


def row_timing(height, exposure_ms, readout_ms):
    if type(height) is not int or height < 2:
        raise ValueError('Row timing requires at least two native rows')
    exposure = motion._positive(exposure_ms, 'exposure') / 1000
    readout = motion._positive(readout_ms, 'first-last row scan', zero=True) / 1000
    midpoint = (np.arange(height, dtype=float) / (height - 1) - .5) * readout
    return midpoint - exposure/2, midpoint, midpoint + exposure/2


def row_kernel(height, velocity_px_s, exposure_ms, readout_ms):
    """Exact triangular-basis integrals for source(x - velocity*t) per row."""
    velocity = motion._positive(velocity_px_s, 'image velocity', zero=True)
    starts, mids, ends = row_timing(height, exposure_ms, readout_ms)
    if velocity == 0:
        return np.array([0]), np.ones((height, 1))
    if readout_ms == 0:
        kernel = motion.box_kernel(velocity * exposure_ms / 1000)
        return np.arange(-(len(kernel)//2), len(kernel)//2+1), np.tile(kernel, (height, 1))
    lo, hi = velocity * starts, velocity * ends
    radius = math.ceil(float(max(abs(lo.min()), abs(hi.max()))) + 1)
    if radius > 256:
        raise ValueError('Rolling motion exceeds saved native probe support')
    offsets = np.arange(-radius, radius + 1, dtype=int)
    j = offsets[None, :]
    lower, upper = lo[:, None], hi[:, None]
    left0, left1 = np.maximum(lower, j - 1), np.minimum(upper, j)
    right0, right1 = np.maximum(lower, j), np.minimum(upper, j + 1)
    left = np.maximum(0, left1-left0) * np.maximum(0, (left0+left1)/2-j+1)
    right = np.maximum(0, right1-right0) * np.maximum(0, j+1-(right0+right1)/2)
    weights = (left + right) / (velocity * exposure_ms / 1000)
    nonzero = np.flatnonzero(np.any(weights > 0, axis=0))
    offsets, weights = offsets[nonzero[0]:nonzero[-1]+1], weights[:, nonzero[0]:nonzero[-1]+1]
    if (len(offsets) % 2 != 1 or offsets[0] != -offsets[-1]
            or not np.allclose(weights.sum(axis=1), 1, rtol=1e-12, atol=1e-12)
            or np.any(weights < 0)):
        raise ValueError('Row exposure integral lost energy or centered timing support')
    return offsets, weights


def integrate_row_planes(planes, velocity_px_s, exposure_ms, readout_ms):
    values = np.asarray(planes)
    if (values.ndim != 3 or values.shape[2] != 4 or values.dtype.kind not in 'iuf'
            or not np.isfinite(values).all() or np.any(values < 0)):
        raise ValueError('Shutter input requires finite nonnegative native full R/Gr/Gb/B planes')
    velocity = motion._positive(velocity_px_s, 'image velocity', zero=True)
    exposure = motion._positive(exposure_ms, 'exposure')
    readout = motion._positive(readout_ms, 'first-last row scan', zero=True)
    if velocity == 0:
        return values.copy()
    if readout == 0:
        # Retain the exact old global-shutter implementation, including its
        # floating point operation order and zero-motion RAW equivalence.
        return motion.integrate_planes(values, velocity * exposure / 1000)
    _, weights = row_kernel(values.shape[0], velocity, exposure, readout)
    output = np.empty(values.shape, dtype=float)
    for row in range(values.shape[0]):
        output[row] = convolve1d(values[row].astype(float), weights[row], axis=0, mode='nearest')
    return output


def shutter_metadata(projection, speed, depth, exposure, readout, valid_rect, height=256):
    velocity = motion.image_velocity_px_s(speed, depth, projection['effective_focal_length_mm'],
                                          projection['pixel_pitch_um'])
    starts, mids, ends = row_timing(height, exposure, readout)
    summary = {}
    for label, row in (('first', 0), ('center', (height-1)/2), ('last', height-1)):
        midpoint = (row/(height-1)-.5)*readout/1000
        summary[label] = {'row_index': row, 'start_s': midpoint-exposure/2000,
                          'mid_s': midpoint, 'end_s': midpoint+exposure/2000}
    return {
        'schema': MODEL_SCHEMA, **projection, 'object_speed_m_s': speed, 'object_distance_m': depth,
        'distance_definition': 'axial depth; projection only, no chart rescale or refocus',
        'exposure_ms': exposure, 'readout_ms': readout,
        'shutter_mode': 'global' if readout == 0 else 'rolling',
        'native_probe_rows': height, 'line_time_ms': readout/(height-1),
        'readout_definition': 'first-to-last native-probe row exposure midpoint; virtual probe, not product sensor',
        'velocity_px_s': velocity, 'blur_length_px': velocity*exposure/1000,
        'top_bottom_skew_px': velocity*readout/1000,
        'direction': 'positive horizontal; top earlier/left, bottom later/right',
        'camera_motion': 'fixed', 'time_reference': 'center-row exposure midpoint t=0',
        'row_exposure_start_s': starts.tolist(), 'row_exposure_mid_s': mids.tolist(),
        'row_exposure_end_s': ends.tolist(), 'row_mid_displacement_px': (velocity*mids).tolist(),
        'row_timing_summary': summary, 'projection': 'local_pinhole_transverse_constant_velocity',
        'distortion_derivative_applied': False,
        'interpolation': 'one_exact_native_linear_triangle_basis_integral_per_row',
        'valid_rect_row_col': list(valid_rect), 'isp_support_halo_native_px': motion.ISP_HALO,
        'edge_policy': 'nearest_preview_padding_excluded_from_all_selection_metrics',
        'processing_order': 'full R/Gr/Gb/B rates → row exposure integral → remosaic → fresh electrical capture → fixed ISP',
        'assumptions': SCOPE,
    }


def prepare_payload(payload, metadata, exposure_index, readout_index, requested_f_number):
    result = {**payload, 'candidate': copy.deepcopy(payload['candidate']), 'fields': {}}
    source_id = payload['candidate']['id']
    result['source_candidate_id'] = source_id
    result['candidate'].update(
        id=f'{source_id}-e{exposure_index:02d}-r{readout_index:02d}', source_candidate_id=source_id,
        exposure_ms=metadata['exposure_ms'], readout_ms=metadata['readout_ms'],
        shutter_mode=metadata['shutter_mode'], requested_f_number=requested_f_number,
        motion=copy.deepcopy(metadata),
        label=f"F/{requested_f_number:g} · {metadata['exposure_ms']:g} ms · {metadata['shutter_mode']} {metadata['readout_ms']:g} ms",
    )
    for field_id, field in payload['fields'].items():
        values = {}
        for chart, arrays in field['arrays'].items():
            original = arrays['planes-electron-rate.npy']
            if not np.array_equal(motion.remosaic_native(original), arrays['mean-electron-rate.npy']):
                raise ValueError('Source full planes do not reproduce their bound native CFA mosaic')
            integrated = integrate_row_planes(original, metadata['velocity_px_s'],
                                               metadata['exposure_ms'], metadata['readout_ms'])
            values[chart] = {**arrays, 'planes-electron-rate.npy': integrated,
                            'mean-electron-rate.npy': motion.remosaic_native(integrated)}
        result['fields'][field_id] = {**field, 'arrays': values}
    return result


def _camera(root, payload, condition, projection, depth, exposure, ei, readout, ri, requested_f, identity, notify, check):
    metadata = shutter_metadata(projection, condition['object_speed_m_s'], depth, exposure, readout,
                                 condition['common_valid_rect_row_col'])
    prepared = prepare_payload(payload, metadata, ei, ri, requested_f)
    captured = acquisition._camera(root/'conditions', prepared,
        {**condition, 'exposure_ms': exposure, 'motion': metadata, '_scope': SCOPE}, identity, notify, check)
    folder = root / captured['prefix']
    shared.write_json(folder/'row-timing.json', metadata)
    offsets, kernels = row_kernel(256, metadata['velocity_px_s'], exposure, readout)
    np.save(folder/'row-integration-kernels.npy', kernels)
    captured['motion'].update(row_timing_file='row-timing.json', row_timing_sha256=sha256(folder/'row-timing.json'),
        kernel_file='row-integration-kernels.npy', kernel_sha256=sha256(folder/'row-integration-kernels.npy'),
        kernel_offset_min_px=int(offsets[0]), kernel_offset_max_px=int(offsets[-1]))
    light_scale = condition['luminance_cd_m2']/payload['candidate']['result']['configuration']['luminance_cd_m2']
    for field in captured['result']['fields']:
        check()
        source = payload['fields'][field['id']]['arrays']['resolution']['planes-electron-rate.npy']
        gs = motion.integrate_planes(source, metadata['blur_length_px']) * light_scale
        field_root = folder/'fields'/field['id']
        gs_path = field_root/'resolution'/'global-shutter-planes-electron-rate.npy'
        np.save(gs_path, gs)
        actual = np.load(field_root/'resolution'/'planes-electron-rate.npy', allow_pickle=False)
        edge = field['tests']['resolution']['sites']['center']['roi']
        geometry = metrics.geometry_from_planes(actual, gs, valid_rect=condition['common_valid_rect_row_col'],
            edge_roi=edge, theoretical_skew_px=metadata['top_bottom_skew_px'])
        geometry.update(global_reference_file='resolution/global-shutter-planes-electron-rate.npy',
                        global_reference_sha256=sha256(gs_path),
                        actual_planes_file='resolution/planes-electron-rate.npy',
                        actual_planes_sha256=sha256(field_root/'resolution'/'planes-electron-rate.npy'))
        field['tests']['shutter_geometry'] = geometry
        field['shutter_geometry'] = geometry
        shared.write_json(field_root/'shutter-geometry.json', geometry)
    shared.write_json(folder/'result.json', captured)
    return captured


def comparison(root, candidates, f_numbers, policy, valid_rect, baseline_id):
    rows=[]
    for candidate in candidates:
        source = candidate['source_identities']['lens']['source']
        iris = source.get('pupil_condition')
        applied = iris['effective_f_number'] if iris else source['paraxial']['effective_f_number']
        fields=[]
        for field in candidate['result']['fields']:
            measured = metrics.measure_field(Path(root)/candidate['prefix']/'fields'/field['id'],field,valid_rect=valid_rect)
            fields.append({'field_id':field['id'], **measured, 'shutter_geometry':field['shutter_geometry']})
        rows.append({'candidate_id':candidate['id'],'source_candidate_id':candidate['source_candidate_id'],
            'f_number':applied,'requested_f_number':candidate['requested_f_number'],
            'exposure_ms':candidate['exposure_ms'],'readout_ms':candidate['readout_ms'],
            'shutter_mode':candidate['shutter_mode'],
            'blur_length_px':candidate['motion']['blur_length_px'],
            'top_bottom_skew_px':candidate['motion']['top_bottom_skew_px'],
            'velocity_px_s':candidate['motion']['velocity_px_s'],
            'aperture_condition':iris or {'mode':'source_stop','effective_f_number':applied,'reference_wavelength_nm':550},
            'fields':fields})
    expected = len(f_numbers)*len({r['exposure_ms'] for r in rows})*len({r['readout_ms'] for r in rows})
    if len(rows)!=expected:
        raise ValueError('Every requested aperture/exposure/readout must complete before selection')
    reference={(r['exposure_ms'],r['readout_ms'],f['field_id']):f['mean_signal_e']
               for r in rows if r['requested_f_number']==min(f_numbers) for f in r['fields']}
    for row in rows:
        for field in row['fields']:
            denominator=reference[(row['exposure_ms'],row['readout_ms'],field['field_id'])]
            value=field.get('mean_signal_e')
            field['relative_signal']=value/denominator if value is not None and denominator and denominator>0 else None
        for name,key,aggregate in (
            ('worst_field_snr_db','snr_db',min),('worst_field_sfr50_cpp','processed_sfr50_cpp',min),
            ('min_relative_signal','relative_signal',min),('max_saturation_fraction','saturation_fraction',max)):
            values=[f.get(key) for f in row['fields']]
            row[name]=aggregate(values) if all(v is not None for v in values) else None
    selection=metrics.assess(rows,policy)
    return {'schema':'camera_db_aperture_sweep_result_v1','joint_exposure_selection':True,
        'readout_grouped_selection':True,'rows':rows,'baseline_candidate_id':baseline_id,
        'selection':selection,'selections_by_readout_ms':selection['selections_by_readout_ms'],
        'signal_reference':'Same-field lowest SELECTED F at the same exposure and row readout',
        'sfr_scope':'Processed edge response inside common support; shear changes edge orientation, no GS versus RS SFR ranking',
        'common_valid_rect_row_col':valid_rect,'scope':SCOPE,'physical_validation':'not_established'}


def _compact_motion(metadata):
    return {key:copy.deepcopy(value) for key,value in metadata.items() if key not in TIMING_ARRAY_KEYS}


def compact_candidate(candidate, digest):
    result=motion.compact_candidate(candidate,digest)
    result.update(readout_ms=candidate['readout_ms'],shutter_mode=candidate['shutter_mode'],
                  motion=_compact_motion(candidate['motion']))
    for compact,full in zip(result['result']['fields'],candidate['result']['fields'],strict=True):
        compact['motion']=_compact_motion(full['motion'])
        if 'measurement_support' in compact:
            compact['measurement_support']['motion']=_compact_motion(compact['measurement_support']['motion'])
        compact['shutter_geometry']=copy.deepcopy(full['shutter_geometry'])
        compact['tests']['shutter_geometry']=copy.deepcopy(full['shutter_geometry'])
    return result


def run(workspace,directory,request,source_root,source_summary,progress,*,check=lambda:None,cancel_check=lambda:None):
    del workspace
    started=time.monotonic(); root=Path(directory); root.mkdir(parents=True,exist_ok=True)
    reader=acquisition.VerifiedSource(source_root,source_summary.get('manifest_sha256'))
    if request['source_run_id']!=source_summary.get('id'):
        raise ValueError('Shutter request does not identify the bound source')
    all_payloads=acquisition._validate_source(reader,source_summary)
    original_f=source_summary['configuration']['f_numbers']
    f_numbers=request['f_numbers']
    chosen={f:p for f,p in zip(original_f,all_payloads,strict=True)}
    if not f_numbers or any(f not in chosen for f in f_numbers):
        raise ValueError('Requested iris is not a pinned source aperture')
    payloads=[chosen[f] for f in f_numbers]
    projection=motion.source_projection(payloads)
    exposures=request['exposure_ms']; readouts=request['readout_ms']
    if 0 not in readouts:
        raise ValueError('Every shutter study requires matched global-shutter readout0')
    depth=motion._positive(request['object_distance_m'],'object axial depth')
    max_velocity=motion.image_velocity_px_s(max(request['object_speed_m_s']),depth,
        projection['effective_focal_length_mm'],projection['pixel_pitch_um'])
    support=motion.common_valid_rect((256,256),max_velocity*(max(exposures)+max(readouts))/1000)
    base_exposure=4 if 4 in exposures else exposures[0]
    baseline_id=f"{payloads[0]['candidate']['id']}-e{exposures.index(base_exposure):02d}-r{readouts.index(0):02d}"
    comparison_id=f"{payloads[0]['candidate']['id']}-e{exposures.index(base_exposure):02d}-r{readouts.index(max(readouts)):02d}"
    identity={'run_id':request['source_run_id'],'manifest_sha256':reader.manifest_sha256}
    shared.write_json(root/'source-binding.json',{'schema':'camera_db_shutter_source_v1',**identity,
        'verified_files':reader.used,'projection':projection,'optics_recomputed':False,
        'reuse':'Full pre-mosaic optical/pixel rate planes; independent row-time integration and new RAW capture'})
    check(); cells=[]; messages=queue.Queue()
    for luminance in request['luminance_cd_m2']:
        for speed in request['object_speed_m_s']:
            cell={'id':f'condition-{len(cells):03d}','luminance_cd_m2':luminance,
                  'object_speed_m_s':speed,'object_distance_m':depth,'common_valid_rect_row_col':support,
                  'baseline_candidate_id':baseline_id,'default_comparison_candidate_id':comparison_id,
                  'default_candidate_a_id':baseline_id,'default_candidate_b_id':comparison_id,
                  'shutter_comparison':True}
            tasks=[(payload,fi,exposure,ei,readout,ri) for fi,payload in enumerate(payloads)
                   for ei,exposure in enumerate(exposures) for ri,readout in enumerate(readouts)]
            stopped=threading.Event(); ordered={}
            def worker_check():
                if stopped.is_set():
                    raise RuntimeError('Shutter worker stopped after cancellation or sibling failure')
            with ThreadPoolExecutor(max_workers=min(acquisition.WORKERS,len(tasks))) as pool:
                futures={pool.submit(_camera,root,payload,cell,projection,depth,exposure,ei,readout,ri,
                    f_numbers[fi],identity,messages.put,worker_check):i
                    for i,(payload,fi,exposure,ei,readout,ri) in enumerate(tasks)}
                pending=set(futures)
                try:
                    while pending:
                        done,pending=wait(pending,timeout=.2,return_when=FIRST_COMPLETED)
                        cancel_check()
                        while not messages.empty(): progress(messages.get_nowait())
                        for future in done: ordered[futures[future]]=future.result()
                except BaseException:
                    stopped.set()
                    for future in pending: future.cancel()
                    raise
            candidates=[ordered[i] for i in range(len(tasks))]
            sweep=comparison(root,candidates,f_numbers,request['selection_policy'],support,baseline_id)
            compact=[]
            for candidate,row in zip(candidates,sweep['rows'],strict=True):
                candidate['supported_measurements']=row
                for field,measured in zip(candidate['result']['fields'],row['fields'],strict=True):
                    field['supported_motion_metrics']=measured
                    field['metrics'].update(supported_green_photoelectrons=measured.get('mean_signal_e'),
                        supported_green_temporal_noise_e=measured.get('temporal_noise_e'),
                        supported_snr_db=measured.get('snr_db'),supported_saturation_fraction=measured.get('saturation_fraction'))
                path=root/candidate['prefix']/'result.json'; shared.write_json(path,candidate)
                compact.append(compact_candidate(candidate,sha256(path)))
            cell.update(candidates=compact,aperture_sweep=sweep,
                        capture_count=len(tasks)*len(acquisition.FIELDS)*len(acquisition.CHARTS)*8)
            shared.write_json(root/'conditions'/cell['id']/'index.json',cell); cells.append(cell)
            progress(f"{cell['id']} completed · {luminance:g} cd/m² / {speed:g} m/s · global/rolling shutter")
            check()
    default=next((c for c in cells if c['luminance_cd_m2']==1000 and c['object_speed_m_s']==4),cells[-1])
    conditions={'schema':SCHEMA,'comparison_mode':'shutter_motion','conditions':cells,
        'default_condition_id':default['id'],'default_comparison_candidate_id':comparison_id,
        'default_candidate_a_id':baseline_id,'default_candidate_b_id':comparison_id,'shutter_comparison':True,
        'luminance_cd_m2':request['luminance_cd_m2'],'object_speed_m_s':request['object_speed_m_s'],
        'object_distance_m':depth,'exposure_ms':exposures,'readout_ms':readouts,'f_numbers':f_numbers,
        'projection':projection,'common_valid_rect_row_col':support,
        'common_support_scope':'Same rectangle across all light/speed/exposure/aperture/readout, including GS and stationary cells',
        'source_run_id':request['source_run_id'],'source_manifest_sha256':reader.manifest_sha256,
        'motion_blur_included':True,'rolling_shutter_included':True,'auto_brightness':False,
        'optical_response_recomputed':False,'calibration_reused':True,'scope':SCOPE,
        'native_probe_rows':256,'readout_definition':'Virtual native256 first-to-last row scan; not product sensor mode',
        'selection_scope':'Separate exposure/iris choice within each readout; no readout superiority recommendation',
        'baseline':{'source_candidate_id':payloads[0]['candidate']['id'],'exposure_ms':base_exposure,
                    'readout_ms':0,'candidate_id':baseline_id},
        'detail_policy':'Compact index; complete row timing, RAW hashes and arrays in separate manifest-bound artifacts'}
    shared.write_json(root/'shutter-motion.json',conditions)
    return {'schema':'camera_db_design_study_result_v1','configuration':request,'comparison_mode':'shutter_motion',
        'candidates':default['candidates'],'baseline_candidate_id':baseline_id,
        'default_comparison_candidate_id':comparison_id,'aperture_sweep':default['aperture_sweep'],
        'default_candidate_a_id':baseline_id,'default_candidate_b_id':comparison_id,'shutter_comparison':True,
        'aperture_conditions':conditions,'capture_count':sum(c['capture_count'] for c in cells),
        'raw_shape_row_col':[256,256],'raw_preserved':True,'physical_validation':'not_established',
        'product_ranking_allowed':False,'comparison_scope':SCOPE,'calibration':payloads[0]['calibration'],
        'performance':{'elapsed_seconds':time.monotonic()-started,'candidate_workers':acquisition.WORKERS,
                       'optics_recomputed':False,'compact_candidate_index':True}}
