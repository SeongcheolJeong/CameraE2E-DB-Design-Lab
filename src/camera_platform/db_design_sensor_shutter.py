"""Located active-array optics and shared full-array row timing.

Vendor dimension evidence is an overlay on the preserved structural sensor;
line timing, mounting/CFA phase and active-to-total placement remain declared.
"""
from __future__ import annotations

import copy
import json
import math
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np
from scipy.ndimage import convolve1d
from scipy.optimize import brentq

from . import db_design_conditions as acquisition
from . import db_design_engine as engine
from . import db_design_motion as motion
from . import db_design_optics as db_optics
from . import db_design_shutter as shutter
from . import db_design_sensor_timing as timing
from . import db_design_sensor_modes as modes
from . import db_design_sensor_shutter_metrics as metrics
from . import reference_workshop_optics as optics
from . import design_workshop_engine as shared
from .virtual_lab_job import sha256

SCHEMA='camera_db_sensor_shutter_result_v1'
SCOPE=(
    'IMX462 structural pixel/readout proxy with separately acquired IMX462LQR vendor active/total dimensions. '
    'Source optical fields are recomputed at declared anchors inside two located active-array ROIs; '
    'the center uses the actual optical-axis pixel, half a pixel from its geometric crop center. The former virtual '
    'outer field outside this sensor is not reused. All rows share active row548 midpoint t=0. '
    'Line period, total-array placement, scan order, optical-axis mounting and RGGB phase are assumptions, '
    'not a decoded or measured product mode. Distance only sets transverse pinhole image velocity; '
    'chart scale and lens focus remain fixed. Full four-site rates are integrated before CFA and fresh '
    'electrical capture. No 3D occlusion, vertical motion, frame cadence or product validation. '
    'Exposure/iris selection is separate within each line period; no GS-versus-RS ranking from SFR.'
)
MODE_SCOPE = (
    'Located full-active IMX462 structural proxy, fixed lens/pixel/ISP and shared row548 clock. '
    'Operating-mode evidence explicitly distinguishes acquired module-driver timing from assumed high-FPS profiles. '
    'Exposure is quantized to whole lines and limited to the fixed frame budget; matched GS uses the same actual exposure. '
    'Mean optical/pixel rates may be reused only with matching source/geometry/calibration hashes; noise and ADC are recaptured. '
    'Stored structural readout model retained; no module-specific circuit calibration, CSI latency or physical validation. '
    'Transverse local-pinhole motion and local stationary PSFs only.'
)


def located_fields(workspace,spec,context,sensor_profile):
    """Invert the chief trace to each declared local-PSF anchor inside its ROI."""
    from rayoptics.raytr import trace
    from rayoptics.raytr.opticalspec import Field
    request=optics._request(workspace,spec,[450.,550.,650.],source_context=context)
    model=optics.prescription_psf._model(request,1)
    def chief_radius(reference_radius):
        field=Field(y=math.degrees(math.atan2(reference_radius,optics.REFERENCE_FOCAL_MM)),fov=model.optical_spec['fov'])
        field.aim_info=trace.aim_chief_ray(model,field,550.)
        ray,_,_=trace.trace_base(model,[0.,0.],field,550.,check_apertures=True,apply_vignetting=False)
        optics.prescription_psf._validate_physical_ray(ray,model,request['prescription']['surfaces'],'located chief')
        return float(np.linalg.norm(np.asarray(ray[-1][0][:2])))
    halfdiag=np.linalg.norm(np.asarray(sensor_profile['active_shape_row_col'])*sensor_profile['pixel_pitch_um']/2000)
    fields=[]
    for roi in sensor_profile['roi_layout']:
        xy=np.asarray(roi['optical_anchor_image_xy_mm'],float); target=float(np.linalg.norm(xy))
        hi=max(target*optics.REFERENCE_FOCAL_MM/context['focal_length_mm']*1.5,.01)
        while chief_radius(hi)<target:
            hi*=1.5
            if hi>optics.MAX_HEIGHT_MM: raise ValueError('Located ROI leaves authored optical field support')
        radius=0. if target==0 else brentq(lambda r:chief_radius(r)-target,0.,hi,xtol=1e-12)
        angle=math.degrees(math.atan2(radius,optics.REFERENCE_FOCAL_MM))
        azimuth=math.degrees(math.atan2(xy[1],xy[0]))%360 if target else 0.
        origin=roi['origin_row_col']; shape=roi['shape_row_col']; active=sensor_profile['active_shape_row_col']
        if any(o%2 or o<0 or o+n>a for o,n,a in zip(origin,shape,active,strict=True)):
            raise ValueError('Located RGGB probe must be even-origin and wholly inside the active array')
        placement={'sensor_timing_profile_sha256':sensor_profile['profile_sha256'],
            'active_shape_row_col':active,'total_shape_row_col':sensor_profile['total_shape_row_col'],
            'roi_origin_active_row_col':origin,'roi_center_active_row_col':roi['center_active_row_col'],
            'roi_center_image_xy_mm':roi['roi_center_image_xy_mm'],
            'optical_anchor_active_row_col':roi['optical_anchor_active_row_col'],
            'optical_anchor_local_row_col':roi['optical_anchor_local_row_col'],
            'optical_anchor_image_xy_mm':roi['optical_anchor_image_xy_mm'],
            'target_image_xy_mm':xy.tolist(),'optical_axis_active_row_col':sensor_profile['optical_axis_active_row_col'],
            'cfa_origin':{'pattern':'RGGB','local_row':0,'local_col':0,
                'active_origin_row_col':origin,'scope':'even located crop under assumed active-array RGGB phase'},
            'product_mode':False,'field_approximation':'local_shift_invariant_at_declared_inside_ROI_optical_anchor'}
        fields.append({'id':roi['id'],'label':roi['label'],'normalized_radius':target/halfdiag,
            'azimuth_deg':azimuth,'x':float(xy[0]/halfdiag),'y':float(xy[1]/halfdiag),
            'baseline_reference_xy_mm':xy.tolist(),'target_image_xy_mm':xy.tolist(),
            'object_angle_deg':angle,'object_reference_radius_mm':radius,'sensor_placement':placement})
    return fields


def prepare_located_source(workspace,root,payload,sensor_profile,progress):
    candidate=payload['candidate']; key=candidate['lens_key']
    current=db_optics.source_metadata(workspace,key); frozen=candidate['source_identities']['lens']['source']
    for name in ('revision_id','revision_content_sha256','reviewed_csv_sha256','prescription_sha256',
                 'applied_prescription_sha256','pupil_condition','paraxial'):
        if current.get(name)!=frozen.get(name):
            raise ValueError('Current lens differs from bound source at '+name)
    context=db_optics._source(key); spec=db_optics.template(key,optical_profile='exit_pupil_diffraction')
    # The retained virtual-field quadrature is not converged at this real sensor
    # corner. Use the existing bounded dense ladder with unchanged tolerances.
    context['reciprocal_profile']=optics.db_design_radiometry.PROFILE
    context['located_fields']=located_fields(workspace,spec,context,sensor_profile)
    optical_root=root/'located-optics'/candidate['id']; optical_root.mkdir(parents=True)
    progress(candidate['id']+' · fresh diffraction optics at actual active-array ROI centers')
    optics.solve(workspace,spec,optical_root,on_progress=progress,source_context=context)
    solution=optics.load_solution(optical_root)
    if solution['result']['status']!='PROPOSED': raise ValueError('Located sensor optics failed unchanged qualification gates')
    folder=root/'located-source'/candidate['id']; folder.mkdir(parents=True)
    spec=copy.deepcopy(candidate['result']['configuration'])
    result=engine.camera(workspace,folder,spec,solution,payload['pixel-cell'],payload['calibration'],progress)
    result['limitations']=[*result['limitations'],SCOPE]
    fresh={**payload,'candidate':copy.deepcopy(candidate),'fields':{},'support':{},
           'optical-response':solution['result'],'generated_source_prefix':f'located-source/{candidate["id"]}/'}
    fresh['candidate']['result']=result
    fresh['candidate']['located_source']={'original_candidate_id':candidate['id'],
        'original_optical_revision':candidate['result'].get('optical_revision'),
        'new_optical_revision':solution['result']['revision'],
        'optical_result_file':f'located-optics/{candidate["id"]}/result.json',
        'optical_result_sha256':sha256(optical_root/'result.json'),
        'optical_arrays_sha256':solution['result']['arrays_sha256'],
        'sensor_profile_sha256':sensor_profile['profile_sha256'],
        'fixed_source_calibration_sha256':sha256(folder/'calibration.json'),
        'scope':'fresh physical field transport; original pixel structure/readout and fixed CCM retained'}
    for field in solution['result']['fields']:
        fid=field['id']; field_root=folder/'fields'/fid
        capture=json.loads((field_root/'capture.json').read_text())
        values={chart:{filename:np.load(field_root/chart/filename,allow_pickle=False)
                       for filename in ('mean-electron-rate.npy','planes-electron-rate.npy','source-reference-linear.npy')}
                for chart in acquisition.CHARTS}
        fresh['fields'][fid]={'optics':field,'capture':capture,'arrays':values}
        for relative in field.get('images',{}).values():
            fresh['support']['optics/'+relative]=(optical_root/relative).read_bytes()
    shared.write_json(folder/'source-provenance.json',fresh['candidate']['located_source'])
    shared.write_json(folder/'result.json',fresh['candidate'])
    return fresh


def reuse_located_source(root, payload, sensor_profile, reader, progress):
    """Reuse manifest-verified pre-noise rates at the identical physical anchors."""
    cid = payload['candidate']['id']
    prior = reader.json('result.json')
    if prior.get('sensor_timing_profile') != sensor_profile:
        raise ValueError('Retained located source uses different sensor geometry')
    optical_prefix, source_prefix = f'located-optics/{cid}/', f'located-source/{cid}/'
    candidate = reader.json(source_prefix+'result.json')
    if candidate['source_identities'] != payload['candidate']['source_identities']:
        raise ValueError('Retained located source lens/sensor identities differ')
    for name in ('pixel-cell', 'pixel-response', 'calibration'):
        if reader.json(source_prefix+name+'.json') != payload[name]:
            raise ValueError('Retained located source differs in '+name)
    progress(cid+' · reusing hash-verified located PSFs and pre-noise rates')
    for relative in sorted(reader.files):
        if relative.startswith((optical_prefix, source_prefix)):
            destination = root/relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(reader.read(relative))
    solution = optics.load_solution(root/optical_prefix)
    if solution['result']['status'] != 'PROPOSED':
        raise ValueError('Retained located optics are not qualified')
    fresh = {**payload, 'candidate':candidate, 'fields':{}, 'support':{},
             'optical-response':solution['result'], 'generated_source_prefix':source_prefix,
             'pre_noise_source_reused':True}
    for field in solution['result']['fields']:
        fid = field['id']; prefix = source_prefix+f'fields/{fid}/'
        arrays = {chart:{name:reader.array(prefix+chart+'/'+name) for name in (
            'mean-electron-rate.npy','planes-electron-rate.npy','source-reference-linear.npy')}
            for chart in acquisition.CHARTS}
        fresh['fields'][fid] = {'optics':field,'capture':reader.json(prefix+'capture.json'),'arrays':arrays}
        for relative in field.get('images',{}).values():
            fresh['support']['optics/'+relative] = reader.read(optical_prefix+relative)
    return fresh


def absolute_row_kernel(midpoints_s,velocity_px_s,exposure_ms):
    mids=np.asarray(midpoints_s,float); velocity=motion._positive(velocity_px_s,'image velocity',zero=True)
    duration=motion._positive(exposure_ms,'exposure')/1000
    if mids.ndim!=1 or not np.isfinite(mids).all(): raise ValueError('Finite absolute row times required')
    if velocity==0: return np.array([0]),np.ones((len(mids),1))
    lower=velocity*(mids-duration/2); upper=velocity*(mids+duration/2)
    radius=math.ceil(float(max(abs(lower.min()),abs(upper.max())))+1)
    if radius>256: raise ValueError('Absolute row motion exceeds retained native support')
    offsets=np.arange(-radius,radius+1); j=offsets[None,:]
    lo,hi=lower[:,None],upper[:,None]
    l0,l1=np.maximum(lo,j-1),np.minimum(hi,j)
    r0,r1=np.maximum(lo,j),np.minimum(hi,j+1)
    weights=(np.maximum(0,l1-l0)*np.maximum(0,(l0+l1)/2-j+1)
             +np.maximum(0,r1-r0)*np.maximum(0,j+1-(r0+r1)/2))/(velocity*duration)
    if np.any(weights<0) or not np.allclose(weights.sum(axis=1),1,atol=1e-12,rtol=1e-12):
        raise ValueError('Absolute row integration lost exposure energy')
    return offsets,weights


def integrate_located_planes(planes,velocity,exposure_ms,field_clock):
    values=np.asarray(planes)
    if (values.ndim!=3 or values.shape[-1]!=4 or values.dtype.kind not in 'iuf'
        or not np.isfinite(values).all() or np.any(values<0)):
        raise ValueError('Located shutter needs finite positive full four-site rate planes')
    if velocity==0: return values.copy()
    if field_clock['line_time_us']==0:
        return motion.integrate_planes(values,velocity*exposure_ms/1000)
    mids=field_clock['row_exposure_mid_s']
    if len(mids)!=values.shape[0]: raise ValueError('Absolute row times must match native response rows')
    _,weights=absolute_row_kernel(mids,velocity,exposure_ms)
    output=np.empty(values.shape,float)
    for row in range(values.shape[0]):
        output[row]=convolve1d(values[row].astype(float),weights[row],axis=0,mode='nearest')
    return output


def field_metadata(sensor_profile,fid,line_us,exposure,projection,speed,depth,valid_rect):
    clock=timing.field_timing(sensor_profile,fid,line_us,exposure)
    velocity=motion.image_velocity_px_s(speed,depth,projection['effective_focal_length_mm'],projection['pixel_pitch_um'])
    roi=next(r for r in sensor_profile['roi_layout'] if r['id']==fid)
    mid=clock['roi_center_mid_s']; first=clock['row_exposure_mid_s'][0]; last=clock['row_exposure_mid_s'][-1]
    return {**clock,**projection,'schema':'camera_located_sensor_shutter_motion_v1',
        'object_speed_m_s':speed,'object_distance_m':depth,'velocity_px_s':velocity,
        'blur_length_px':velocity*exposure/1000,
        'top_bottom_skew_px':velocity*clock['local_roi_scan_ms']/1000,
        'local_roi_skew_px':velocity*clock['local_roi_scan_ms']/1000,
        'full_sensor_skew_px':velocity*clock['total_scan_ms']/1000,
        'active_sensor_skew_px':velocity*clock['active_scan_ms']/1000,
        'full_sensor_scan_ms':clock['total_scan_ms'],'roi_start_row':roi['origin_row_col'][0],
        'roi_start_col':roi['origin_row_col'][1],'roi_rows':256,'roi_cols':256,
        'roi_center_time_s':mid,'roi_center_displacement_px':velocity*mid,
        'reference_active_row':sensor_profile['reference_active_row'],
        'row_mid_displacement_px':(np.asarray(clock['row_exposure_mid_s'])*velocity).tolist(),
        'row_timing_summary':{name:{'row_index':index,'active_row':roi['origin_row_col'][0]+index,
            'start_s':t-exposure/2000,'mid_s':t,'end_s':t+exposure/2000}
            for name,index,t in [('first',0,first),('center',127.5,mid),('last',255,last)]},
        'direction':'positive horizontal, top-to-bottom active-row scan',
        'projection':'local_pinhole_transverse_constant_velocity','distortion_derivative_applied':False,
        'valid_rect_row_col':valid_rect,'isp_support_halo_native_px':motion.ISP_HALO,
        'assumptions':SCOPE}


def _camera(root,payload,condition,projection,sensor_profile,exposure,ei,line_us,li,requested_f,identity,notify,check):
    depth=condition['object_distance_m']; speed=condition['object_speed_m_s']
    fields={fid:field_metadata(sensor_profile,fid,line_us,exposure,projection,speed,depth,
                              condition['common_valid_rect_row_col']) for fid in acquisition.FIELDS}
    operating = condition.get('operating_mode')
    if operating:
        record = next(row for row in condition['exposure_schedule']
                      if row['status']=='eligible' and row['actual_exposure_ms']==exposure)
        fields = {fid:modes.apply_clock(value,operating,record) for fid,value in fields.items()}
    frame=copy.deepcopy(fields['center'])
    for key in (*shutter.TIMING_ARRAY_KEYS,'active_row_indices','total_row_indices','field_id',
                'roi_start_row','roi_start_col','roi_center_time_s','roi_center_displacement_px','row_timing_summary'):
        frame.pop(key,None)
    frame.pop('row_exposure_next_frame_start_s',None)
    frame.update(timing_mode='full_sensor_located_roi',readout_ms=frame['total_scan_ms'],
        readout_definition='modeled total-row span; explicit line time plus vendor total rows',
        sensor_profile_sha256=sensor_profile['profile_sha256'])
    prepared={**payload,'candidate':copy.deepcopy(payload['candidate']),'fields':{},
              'source_candidate_id':payload['candidate']['id']}
    cid=f"{payload['candidate']['id']}-e{ei:02d}-l{li:02d}"
    prepared['candidate'].update(id=cid,source_candidate_id=payload['candidate']['id'],
        exposure_ms=exposure,line_time_us=line_us,readout_ms=frame['total_scan_ms'],
        shutter_mode='global' if line_us==0 else 'rolling',requested_f_number=requested_f,motion=frame,
        label=f'F/{requested_f:g} · {exposure:g}ms · line {line_us:g}µs')
    for fid,field in payload['fields'].items():
        values={}
        for chart,arrays in field['arrays'].items():
            original=arrays['planes-electron-rate.npy']
            if not np.array_equal(motion.remosaic_native(original),arrays['mean-electron-rate.npy']):
                raise ValueError('Located source planes/CFA disagree')
            integrated=integrate_located_planes(original,frame['velocity_px_s'],exposure,fields[fid])
            values[chart]={**arrays,'planes-electron-rate.npy':integrated,
                           'mean-electron-rate.npy':motion.remosaic_native(integrated)}
        prepared['fields'][fid]={**field,'arrays':values}
    source_identity={'run_id':root.name,'generated_source_prefix':payload['generated_source_prefix'],
        'parent_run_id':identity['run_id'],'parent_manifest_sha256':identity['manifest_sha256'],
        'generated_optical_revision':payload['optical-response']['revision'],
        'origin':('retained_hash_verified_located_pre_noise_response' if payload.get('pre_noise_source_reused')
                  else 'fresh_located_optics_and_pixel_response_not_original_virtual_field_rate')}
    candidate=acquisition._camera(root/'conditions',prepared,
        {**condition,'exposure_ms':exposure,'motion':frame,'_field_motion':fields,
         '_scope':MODE_SCOPE if operating else SCOPE},
        source_identity,notify,check)
    if operating:
        candidate['parameter_sources'].extend([
            {'parameter':key,'value':operating[key], 'unit':unit,
             'classification':operating['line_time_classification'] if key=='line_time_us' else operating['classification'],
             'source':operating['source_url'], 'scope':'timing only; stored structural readout retained'}
            for key,unit in [('hmax','clock cycles'),('timing_clock_hz','Hz'),('vmax','lines'),
                             ('line_time_us','us/row'),('frame_period_ms','ms'),('actual_fps','frames/s')]])
    folder=root/candidate['prefix']; scale=condition['luminance_cd_m2']/1000
    for field in candidate['result']['fields']:
        fid=field['id']; field_root=folder/'fields'/fid; meta=fields[fid]
        shared.write_json(field_root/'row-timing.json',meta)
        offsets,kernels=absolute_row_kernel(meta['row_exposure_mid_s'],frame['velocity_px_s'],exposure)
        np.save(field_root/'row-integration-kernels.npy',kernels)
        field['motion'].update(row_timing_file='row-timing.json',row_timing_sha256=sha256(field_root/'row-timing.json'),
            row_kernel_file='row-integration-kernels.npy',row_kernel_sha256=sha256(field_root/'row-integration-kernels.npy'),
            kernel_offset_min_px=int(offsets[0]),kernel_offset_max_px=int(offsets[-1]))
        original=payload['fields'][fid]['arrays']['resolution']['planes-electron-rate.npy']
        gs=motion.integrate_planes(original,frame['blur_length_px'])*scale
        path=field_root/'resolution'/'global-shutter-planes-electron-rate.npy'; np.save(path,gs)
        actual=np.load(field_root/'resolution'/'planes-electron-rate.npy',allow_pickle=False)
        g=metrics.geometry_from_planes(actual,gs,valid_rect=condition['common_valid_rect_row_col'],
            edge_roi=field['tests']['resolution']['sites']['center']['roi'],
            theoretical_skew_px=meta['top_bottom_skew_px'],theoretical_center_displacement_px=meta['roi_center_displacement_px'])
        g.update(global_reference_file='resolution/global-shutter-planes-electron-rate.npy',
            global_reference_sha256=sha256(path),actual_planes_file='resolution/planes-electron-rate.npy',
            actual_planes_sha256=sha256(field_root/'resolution'/'planes-electron-rate.npy'))
        field['shutter_geometry']=g; field['tests']['shutter_geometry']=g
        shared.write_json(field_root/'shutter-geometry.json',g)
    return candidate


def comparison(root,candidates,f_numbers,policy,support,baseline):
    result=shutter.comparison(root,candidates,f_numbers,policy,support,baseline)
    result['scope']=SCOPE;result['line_time_grouped_selection']=True
    for candidate,row in zip(candidates,result['rows'],strict=True):
        row.update(line_time_us=candidate['line_time_us'],local_roi_scan_ms=candidate['motion']['local_roi_scan_ms'],
            full_sensor_scan_ms=candidate['motion']['full_sensor_scan_ms'],
            full_sensor_skew_px=candidate['motion']['full_sensor_skew_px'])
    return result


def compact_candidate(candidate,digest):
    compact=shutter.compact_candidate(candidate,digest)
    compact['line_time_us']=candidate['line_time_us'];compact['located_source']=candidate['located_source']
    for field in compact['result']['fields']:
        for container in (field['motion'],field.get('measurement_support',{}).get('motion',{})):
            for key in ('active_row_indices','total_row_indices'): container.pop(key,None)
    return compact


def run(workspace,directory,request,source_root,source_summary,progress,*,check=lambda:None,cancel_check=lambda:None,
        sensor_timing_profile=None, located_source_root=None, located_source_manifest_sha256=None):
    started=time.monotonic();root=Path(directory);root.mkdir(parents=True,exist_ok=True)
    profile=timing.profile(workspace)
    if sensor_timing_profile is not None and profile!=sensor_timing_profile:
        raise ValueError('Pinned sensor timing profile changed')
    shared.write_json(root/'sensor-timing-profile.json',profile)
    reader=acquisition.VerifiedSource(source_root,source_summary.get('manifest_sha256'))
    if request['source_run_id']!=source_summary.get('id'):raise ValueError('Sensor shutter source identity mismatch')
    source_payloads=acquisition._validate_source(reader,source_summary)
    chosen=dict(zip(source_summary['configuration']['f_numbers'],source_payloads,strict=True))
    f_numbers=request['f_numbers']
    if not f_numbers or any(f not in chosen for f in f_numbers):raise ValueError('Missing pinned source aperture')
    identity={'run_id':request['source_run_id'],'manifest_sha256':reader.manifest_sha256}
    shared.write_json(root/'source-binding.json',{'schema':'camera_located_sensor_shutter_source_v1',**identity,
        'verified_original_files':reader.used,'sensor_profile_sha256':profile['profile_sha256'],
        'original_field_rates_reused':False,'original_calibration_reused':True})
    operating = None
    exposure_schedule = None
    if request.get('operating_mode_id'):
        modes.catalog(workspace)
        operating = modes.mode(request['operating_mode_id'],request.get('fps'))
        exposure_schedule = modes.schedule(operating,request['exposure_ms'])
        shared.write_json(root/'operating-mode.json',operating)
        shared.write_json(root/'exposure-schedule.json',exposure_schedule)
    scope = MODE_SCOPE if operating else SCOPE
    cached = (acquisition.VerifiedSource(located_source_root,located_source_manifest_sha256)
              if located_source_root else None)
    check();payloads=[]
    for f in f_numbers:
        cancel_check()
        payloads.append(reuse_located_source(root,chosen[f],profile,cached,progress) if cached
                        else prepare_located_source(workspace,root,chosen[f],profile,progress))
        check()
    if cached:
        shared.write_json(root/'located-source-reuse.json',{'run_id':request['located_source_run_id'],
            'manifest_sha256':cached.manifest_sha256,'verified_files':cached.used,
            'pre_noise_response_reused':True,'old_noisy_raw_used_for_recapture':False})
    projection=motion.source_projection(payloads);depth=request['object_distance_m'];exposures=request['exposure_ms']
    if operating:
        exposures = [row['actual_exposure_ms'] for row in exposure_schedule if row['status']=='eligible']
    lines=timing.validate_line_times(request['line_time_us'])
    if operating and lines != [0.,operating['line_time_us']]:
        raise ValueError('Operating mode line clock differs from the pinned request')
    maxv=motion.image_velocity_px_s(max(request['object_speed_m_s']),depth,projection['effective_focal_length_mm'],projection['pixel_pitch_um'])
    support_line = max(lines)
    support_exposure = max(exposures)
    if operating:
        # Prepared mode runs share one conservative physical ROI even when a
        # high-FPS mode blocks the longest requested exposure before capture.
        support_line = max(modes.mode(identity)['line_time_us'] for identity in modes.IDS)
        support_exposure = max(request['exposure_ms'])
    clocks=[timing.field_timing(profile,fid,support_line,support_exposure) for fid in acquisition.FIELDS]
    maxshift=max(abs(t) for c in clocks for key in ('row_exposure_start_s','row_exposure_end_s') for t in c[key])*maxv
    support=motion.common_valid_rect((256,256),2*maxshift)
    baseexp=4 if 4 in exposures else exposures[0];ei=exposures.index(baseexp)
    baseline=f"{payloads[0]['candidate']['id']}-e{ei:02d}-l00"
    compared=f"{payloads[0]['candidate']['id']}-e{ei:02d}-l{len(lines)-1:02d}"
    messages=queue.Queue();cells=[]
    for light in request['luminance_cd_m2']:
        for speed in request['object_speed_m_s']:
            cell={'id':f'condition-{len(cells):03d}','luminance_cd_m2':light,'object_speed_m_s':speed,
                'object_distance_m':depth,'common_valid_rect_row_col':support,'baseline_candidate_id':baseline,
                'default_candidate_a_id':baseline,'default_candidate_b_id':compared,
                'default_comparison_candidate_id':compared,'shutter_comparison':True}
            if operating:
                cell.update(operating_mode=operating,exposure_schedule=exposure_schedule)
            tasks=[(p,fi,e,ei,line,li) for fi,p in enumerate(payloads) for ei,e in enumerate(exposures) for li,line in enumerate(lines)]
            stop=threading.Event();ordered={}
            def worker_check():
                if stop.is_set():raise RuntimeError('Located sensor capture cancelled after sibling failure')
            with ThreadPoolExecutor(max_workers=min(4,len(tasks))) as pool:
                futures={pool.submit(_camera,root,p,cell,projection,profile,e,ei,line,li,f_numbers[fi],identity,messages.put,worker_check):i
                         for i,(p,fi,e,ei,line,li) in enumerate(tasks)}
                pending=set(futures)
                try:
                    while pending:
                        done,pending=wait(pending,timeout=.2,return_when=FIRST_COMPLETED);cancel_check()
                        while not messages.empty():progress(messages.get_nowait())
                        for fut in done:ordered[futures[fut]]=fut.result()
                except BaseException:
                    stop.set()
                    for fut in pending:fut.cancel()
                    raise
            full=[ordered[i] for i in range(len(tasks))]
            sweep=comparison(root,full,f_numbers,request['selection_policy'],support,baseline);compact=[]
            sweep['scope']=scope
            for candidate,row in zip(full,sweep['rows'],strict=True):
                candidate['supported_measurements']=row
                for field,measured in zip(candidate['result']['fields'],row['fields'],strict=True):
                    field['supported_motion_metrics']=measured
                    field['metrics'].update(supported_green_photoelectrons=measured.get('mean_signal_e'),
                        supported_green_temporal_noise_e=measured.get('temporal_noise_e'),supported_snr_db=measured.get('snr_db'),
                        supported_saturation_fraction=measured.get('saturation_fraction'))
                path=root/candidate['prefix']/'result.json';shared.write_json(path,candidate)
                compact.append(compact_candidate(candidate,sha256(path)))
            cell.update(candidates=compact,aperture_sweep=sweep,capture_count=len(tasks)*64)
            shared.write_json(root/'conditions'/cell['id']/'index.json',cell);cells.append(cell);check()
            progress(f"{cell['id']} completed · {light:g}cd/m² / {speed:g}m/s · located full-sensor row timing")
    default=next((c for c in cells if c['luminance_cd_m2']==1000 and c['object_speed_m_s']==4),cells[-1])
    conditions={'schema':SCHEMA,'comparison_mode':'sensor_shutter','conditions':cells,'timing_profile':profile,
        'default_condition_id':default['id'],'default_candidate_a_id':baseline,'default_candidate_b_id':compared,
        'default_comparison_candidate_id':compared,'shutter_comparison':True,
        'luminance_cd_m2':request['luminance_cd_m2'],'object_speed_m_s':request['object_speed_m_s'],
        'object_distance_m':depth,'exposure_ms':exposures,'line_time_us':lines,'f_numbers':f_numbers,
        'readout_ms':[(profile['total_shape_row_col'][0]-1)*line/1000 for line in lines],
        'projection':projection,'common_valid_rect_row_col':support,'source_run_id':request['source_run_id'],
        'source_manifest_sha256':reader.manifest_sha256,'optical_response_recomputed':not bool(cached),'calibration_reused':True,
        'motion_blur_included':True,'rolling_shutter_included':True,'auto_brightness':False,'scope':scope,
        'baseline':{'source_candidate_id':payloads[0]['candidate']['id'],'exposure_ms':baseexp,'line_time_us':0,
                    'readout_ms':0,'candidate_id':baseline},
        'selection_scope':'Exposure/iris within each declared line period; dimensions acquired, row mode assumed'}
    if operating:
        conditions.update(operating_mode=operating,exposure_schedule=exposure_schedule,
            requested_exposure_ms=request['exposure_ms'],
            common_mode_support={'line_time_us':support_line,'exposure_ms':support_exposure,
                'scope':'conservative support only, not a capture; same requested axes use this rectangle across prepared modes'},
            selection_scope='Exposure/iris within this fixed FPS/mode; blocked exposure requests produce no RAW')
    shared.write_json(root/'sensor-shutter.json',conditions)
    return {'schema':'camera_db_design_study_result_v1','configuration':request,'comparison_mode':'sensor_shutter',
        'sensor_timing_profile':profile,'candidates':default['candidates'],'baseline_candidate_id':baseline,
        'default_candidate_a_id':baseline,'default_candidate_b_id':compared,'default_comparison_candidate_id':compared,
        'shutter_comparison':True,'aperture_sweep':default['aperture_sweep'],'aperture_conditions':conditions,
        'capture_count':sum(c['capture_count'] for c in cells),'raw_shape_row_col':[256,256],'raw_preserved':True,
        'source_preparation_capture_count':0 if cached else len(payloads)*64,
        'retained_source_preparation_raw_count':len(payloads)*64 if cached else 0,
        'source_preparation_reused':bool(cached),
        'operating_mode':operating,'exposure_schedule':exposure_schedule,
        'timing_readout_application':({'source_mode_bit_depth':operating['source_output_bit_depth'],
            'simulated_readout':payloads[0]['pixel-cell']['readout'],
            'scope':'timing-only transfer; stored structural electrical assumptions retained'} if operating else None),
        'total_capture_count_including_source_preparation':sum(c['capture_count'] for c in cells)+(0 if cached else len(payloads)*64),
        'physical_validation':'not_established','product_ranking_allowed':False,'comparison_scope':scope,
        'calibration':payloads[0]['calibration'],'located_sources':[p['candidate']['located_source'] for p in payloads],
        'performance':{'elapsed_seconds':time.monotonic()-started,'candidate_workers':4,'optics_recomputed':not bool(cached),
                       'compact_candidate_index':True}}
