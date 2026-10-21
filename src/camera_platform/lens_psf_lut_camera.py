"""Retained optical LUT -> IMX462 native field probes -> electrical RAW -> ISP.

The nine probes are local stationary response calculations at physical sensor
coordinates. They are not a full-frame renderer or a calibrated product model.
"""
from __future__ import annotations
import copy
import json
import math
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.optimize import brentq

from .registry import canonical, digest, IntegrityError, ConflictError
from . import lens_psf_lut as lut, lens_psf_lut_job as library

JOB_KIND='lens_psf_lut_camera'
ROUTE='/api/v1/lens-psf-cameras'
WORKER_TIMEOUT_S=7200
SHAPE=(1097,1937)
PITCH_UM=2.9


def specification(value):
    defaults=dict(name='IMX462 · retained optical LUT RAW/ISP',lut_run_id='',
                  exposure_ms=4.,gain=1.,seed=7,probe_size=128,photon_radiance=1e17)
    if not isinstance(value,dict) or set(value)-set(defaults):
        raise ValueError('Use the declared native probe camera settings')
    s={**defaults,**copy.deepcopy(value)}
    if not isinstance(s['lut_run_id'],str) or not s['lut_run_id'].startswith('run-'):
        raise ValueError('A completed optical LUT run is required')
    if not isinstance(s['name'],str) or not 1<=len(s['name'])<=160:
        raise ValueError('Invalid camera study name')
    for key,lo,hi in [('exposure_ms',.1,30.),('gain',1.,8.),('photon_radiance',1e12,1e20)]:
        if type(s[key]) not in (int,float) or not math.isfinite(s[key]) or not lo<=s[key]<=hi:
            raise ValueError('Invalid '+key)
    if s['probe_size'] not in (64,128,256) or type(s['seed']) is not int or not 0<=s['seed']<2**31:
        raise ValueError('Use a 64/128/256 native probe and an explicit seed')
    return s


def submit(service,value):
    from .service import environment_fingerprint
    s=specification(value);source=library.get(service,s['lut_run_id'])
    if source['status']!='succeeded' or not source['result']['summary']['field_interpolation_qualified']:
        raise ValueError('Native full-field demo requires a fully qualified field LUT')
    d=source['result']['summary']
    if d['specification']['wavelength_nm']!=[450.,550.,650.]:
        raise ValueError('This camera recipe uses three declared 10nm bands at450/550/650nm')
    env=environment_fingerprint()
    if service.loaded_environment!=env: raise ConflictError('Restart API after source changes')
    doc=dict(name=s['name'],job_kind=JOB_KIND,specification=s,
        optical_density_sha256=d['density_sha256'],lut_id=d['lut_id'],environment=env,
        default_worker_timeout_seconds=WORKER_TIMEOUT_S)
    return service.registry.submit(doc,digest(doc))


def sensor_fields(solver,size):
    """Invert actual chief geometry; retain the full native CFA coordinate origin."""
    rows=[0,2*((SHAPE[0]-size)//4),2*((SHAPE[0]-size)//2)]
    cols=[0,2*((SHAPE[1]-size)//4),2*((SHAPE[1]-size)//2)]
    names=[['outer-nw','top','outer-ne'],['left','center','right'],['outer-sw','bottom','outer-se']]
    output=[]
    for iy,row in enumerate(rows):
        for ix,col in enumerate(cols):
            point=np.array([col+(size-1)/2-(SHAPE[1]-1)/2,
                            (SHAPE[0]-1)/2-row-(size-1)/2])*PITCH_UM/1000
            radius=float(np.linalg.norm(point))
            slope=brentq(lambda v:float(np.linalg.norm(solver.chief((0.,v))[1]))-radius,0.,.2,xtol=1e-12)
            uv=point/radius*slope if radius else np.zeros(2)
            _,anchor,_,rotation=solver.chief(uv)
            if np.max(np.abs(rotation@anchor-point))>1e-6:
                raise ValueError('Sensor-to-field inversion exceeded1nm geometry tolerance')
            if np.any(np.abs(uv)>lut.bounds(solver.spec)):
                raise ValueError('Native probe lies outside the stored optical field')
            output.append(dict(id=names[iy][ix],crop_origin_row_col=[row,col],
                shape_row_col=[size,size],sensor_xy_mm=point.tolist(),uv=uv.tolist(),
                angle_deg_xy=np.degrees(np.arctan(uv)).tolist(),azimuth_deg=math.degrees(math.atan2(point[1],point[0]))))
    return output


def sensor_coverage(solver):
    """Trace the active-area perimeter; do not infer coverage from nominal focal length."""
    hx,hy=SHAPE[1]*PITCH_UM/2000,SHAPE[0]*PITCH_UM/2000
    boundary=[(x,y) for x in np.linspace(-hx,hx,17) for y in (-hy,hy)]
    boundary += [(x,y) for y in np.linspace(-hy,hy,17) for x in (-hx,hx)]
    maximum=np.zeros(2);minimum_margin=np.full(2,np.inf)
    for point in boundary:
        p=np.asarray(point);radius=np.linalg.norm(p)
        slope=brentq(lambda v:np.linalg.norm(solver.chief((0.,v))[1])-radius,0.,.2,xtol=1e-12)
        uv=p/radius*slope
        maximum=np.maximum(maximum,np.abs(uv));minimum_margin=np.minimum(minimum_margin,lut.bounds(solver.spec)-np.abs(uv))
    if np.any(minimum_margin<0): raise ValueError('Optical field does not cover the active sensor perimeter')
    return dict(active_shape_row_col=list(SHAPE),pitch_um=PITCH_UM,
        active_extent_mm_xy=[2*hx,2*hy],perimeter_samples=len(boundary),
        required_full_angle_deg_xy=(2*np.degrees(np.arctan(maximum))).tolist(),
        stored_full_angle_deg_xy=(2*np.asarray(solver.spec['half_field_deg_xy'])).tolist(),
        maximum_chief_inverse_residual_mm=1e-6,
        geometry_source='IMX462 family active array; exact report subtype correspondence remains unverified',
        crop_mode='virtual native-area probes; not a product mode',
        field_mapping='actual550nm chief geometry; centered rotational prescription')


def radiometry_backend(document):
    from . import lens_psf_lut_radiometry
    from .virtual_lab_job import sha256
    return {**document['pinned_source']['backend'],
            'full_disk_radial_adapter_sha256':sha256(Path(lens_psf_lut_radiometry.__file__))}


def radiometry(workspace,document,field,response,directory):
    from . import reference_workshop_optics as optics,db_design_optics as db
    from . import lens_factory,db_design_radiometry as dense,reference_workshop_sensor as sensor
    from . import lens_psf_lut_radiometry as radial
    context=db._source(document['specification']['lens_key'])
    height=float(np.linalg.norm(field['sensor_xy_mm']));results={};evidence=[]
    for waves in ([450.,550.],[550.,650.]):
        request=optics._preflight(workspace,waves,source_context=context)['psf_request']['prescription']
        request.update(image_height_mm=[0.,max(height,1e-4)],field_radius=[0.,1.])
        request['sampling'].update(context.get('reciprocal_sampling',{}))
        backend=radiometry_backend(document)
        key=digest(dict(request=request,backend=backend));meta=directory/(key+'.json');archive=directory/(key+'.npz')
        if meta.exists():
            from .virtual_lab_job import sha256
            record=json.loads(meta.read_text())
            if record.get('backend')!=backend or sha256(archive)!=record['array_sha256']:
                raise IntegrityError('Radiometry cache source/backend changed')
            with np.load(archive,allow_pickle=False) as z: arrays={k:z[k] for k in z.files}
            summary=record['summary']
        else:
            result=radial.characterize(request) if height<.1 else lens_factory.characterize(request)
            if result['summary']['status']!='PROPOSED' and height>=.1:
                result=dense.characterize(dense.request(request))
            arrays,summary=result['arrays'],result['summary']
            if summary['status']!='PROPOSED':
                raise ValueError('Reciprocal radiometry did not meet its unchanged convergence gates: '+canonical(summary.get('convergence')))
            np.savez_compressed(archive,**{k:arrays[k] for k in ('irradiance_per_radiance_sr','directional_conditional_weights','image_direction_cosines')})
            from .virtual_lab_job import sha256
            meta.write_text(canonical(dict(summary=summary,array_sha256=sha256(archive),backend=backend)))
        evidence.append(dict(file=meta.name,summary=summary))
        for wi,w in enumerate(waves):
            weights=arrays['directional_conditional_weights'][wi,1];mask=weights>0
            directions=arrays['image_direction_cosines'][1,mask]
            theta=np.degrees(np.arccos(np.clip(directions[:,2],-1,1)))
            phi=np.degrees(np.arctan2(directions[:,1],directions[:,0]))+field['azimuth_deg']
            qe=sensor.absolute_angular_response(response,theta,phi%360,w)
            results[w]=(float(arrays['irradiance_per_radiance_sr'][wi,1]),np.sum(qe*weights[mask,None],axis=0))
    source=document['pinned_source']['lens_source'];waves=document['specification']['wavelength_nm']
    return dict(lens_revision_id=source['revision_id'],applied_prescription_sha256=source['applied_prescription_sha256'],
        wavelength_nm=waves,irradiance_per_radiance_sr=[results[w][0] for w in waves],
        sensor_xy_mm=field['sensor_xy_mm'],method='actual_reciprocal_radiometry_at_probe',
        assumptions='uniform-radiance absolute E/L; lossless uncoated lens; planar angle-dependent QE'),np.asarray([results[w][1] for w in waves]),evidence


def chart(size,pitch,step,support,radiance):
    count=math.ceil((size*pitch+support*step+4)/step)
    axis=(np.arange(count)-(count-1)/2)*step/pitch
    x,y=np.meshgrid(axis,-axis)
    base=np.where(x+.12*y>0,.75,.06)
    p=np.repeat(base[...,None],3,axis=2)
    region=(y<0)&(x>0)
    f=.03+.37*np.clip(x/(size/2),0,1)
    bars=.4+.3*np.cos(2*np.pi*f*x)
    p[region]=np.repeat(bars[...,None],3,axis=2)[region]
    # Declared three-band patches; not Macbeth or a color-accuracy reference.
    patches=(y<-size*.30)&(x>-size*.40)&(x<size*.40)
    band=np.clip(((x+size*.4)/(size*.8)*3).astype(int),0,2)
    colors=np.eye(3)[band]*.7+.04
    p[patches]=colors[patches]
    return p*radiance


def execute(service,job):
    from . import design_workshop_storage as storage,reference_workshop_sensor as sensor,electrical,isp
    from .lens_psf_lut_sensor import evaluate_local_sensor_rate
    from .virtual_lab_job import sha256
    s=specification(job['specification']);document,density=library.load(service,s['lut_run_id'])
    if document['density_sha256']!=job['optical_density_sha256']: raise IntegrityError('Parent optical density changed')
    if library.binding(service.workspace,document['specification'])!=document['pinned_source']:
        raise IntegrityError('Current prescription/backend differs from the retained LUT source')
    solver=lut.PatentSolver(service.workspace,document['specification'])
    coverage=sensor_coverage(solver);fields=sensor_fields(solver,s['probe_size'])
    started=time.monotonic()
    with storage.reserve(service,300_000_000) as (root,check):
        folder=root/'lens-psf-cameras'/job['id'];folder.mkdir(parents=True,exist_ok=True)
        cache=root/'lens-psf-camera-radiometry';cache.mkdir(exist_ok=True)
        cell=sensor.build_cell(service.workspace);response=sensor.build_response(cell,wavelength_nm=[450.,550.,650.])
        (folder/'pixel-cell.json').write_text(canonical(cell));(folder/'pixel-response.json').write_text(canonical(response))
        adc=cell['electrical_model']['adc'];black=adc['black_level_dn'];white=2**adc['bit_depth']-1
        profile=isp.default_profile();raw_format=dict(cfa='RGGB',bit_depth=adc['bit_depth'],black_level_dn=black,response_type='linear',packing='unpacked',layout='bayer',hdr_streams=1)
        photons=chart(s['probe_size'],PITCH_UM,document['optical_sample_um'],document['optical_size'],s['photon_radiance'])
        rows=[];exp=s['exposure_ms']/1000
        for field in fields:
            if service.registry.get_run(job['id'])['status']!='running': raise ValueError('Camera study cancelled')
            print('Native RAW/ISP · '+field['id'],flush=True)
            transfer,qe,trace_evidence=radiometry(service.workspace,document,field,response,cache)
            rate=evaluate_local_sensor_rate(document,density,photons,uv=field['uv'],pixel_pitch_um=PITCH_UM,
                output_shape_row_col=[s['probe_size']]*2,radiometry=transfer,absolute_qe=qe,
                spectral_bin_width_nm=[10.]*3,cfa_origin_row_col=field['crop_origin_row_col'])
            geometry=dict(full_sensor_shape=list(SHAPE),crop_origin=field['crop_origin_row_col'])
            acquisitions=[electrical.capture_electrons(rate['mean_electron_rate']*exp,exp,cell['electrical_model'],
                analog_gain=s['gain'],temperature_c=25.,geometry=geometry,noise_enabled=True,seed=s['seed']+i) for i in range(4)]
            raws=np.stack([a['arrays']['raw_dn'] for a in acquisitions]);raw=raws[0]
            processed=isp.process_raw(raw,raw_format,profile,geometry={'kind':'native_sensor',**geometry})
            rgb=np.asarray(processed['arrays']['rgb'])
            name=field['id'];np.savez_compressed(folder/(name+'.npz'),raw_dn=raws,mean_electron_rate=rate['mean_electron_rate'],
                planes_electron_rate=rate['planes_electron_rate'],isp_rgb=rgb)
            Image.fromarray(np.rint(np.clip(rgb,0,1)*255).astype(np.uint8)).save(folder/(name+'-isp.png'))
            raw_preview=np.clip((raw.astype(float)-black)/(white-black),0,1)
            Image.fromarray(np.rint(raw_preview*255).astype(np.uint8)).save(folder/(name+'-raw.png'))
            # Temporal repeat variance, never spatial chart texture as noise.
            region=(slice(s['probe_size']//8,s['probe_size']//3),slice(s['probe_size']//8,s['probe_size']//3))
            noise=float(np.sqrt(raws.astype(float).var(axis=0,ddof=1)[region].mean()))
            signal=float((raws.astype(float).mean(axis=0)[region]-black).mean())
            p,_=lut.interpolate(document,density,field['uv'],550.)
            axis=(np.arange(len(p))-len(p)//2)*document['optical_sample_um']
            phase=np.exp(-2j*np.pi*axis/(2*PITCH_UM))
            mtf=[float(abs(v@phase)/p.sum()) for v in (p.sum(0),p.sum(1))]
            rows.append(dict(**field,capture_archive_sha256=sha256(folder/(name+'.npz')),raw_array_sha256=__import__('hashlib').sha256(raws.tobytes()).hexdigest(),
                mean_photoelectrons=float(rate['mean_electron_rate'].mean()*exp),temporal_noise_dn=noise,
                flat_patch_snr=signal/noise if noise else None,output_saturation_fraction=float((raws>=white).mean()),
                acquisitions=[a['summary'] for a in acquisitions],raw_format=raw_format,
                optical_mtf550_at_sensor_nyquist_xy=mtf,transfer=transfer,effective_qe=qe.tolist(),
                optical_reuse=rate['metadata'],radiometry_evidence=trace_evidence))
        report=dict(schema='camera_lens_psf_native_demo_v1',job_id=job['id'],lut_run_id=s['lut_run_id'],lut_id=document['lut_id'],
            optical_density_sha256=document['density_sha256'],specification=s,coverage=coverage,fields=rows,
            source_classification=cell['field_classification'],pixel_cell_sha256=cell['cell_sha256'],isp_profile=profile,
            elapsed_s=time.monotonic()-started,psf_generation_calls=0,
            scope=['Nine local stationary native probes at their actual sensor coordinates; not a full-frame rendering.',
                'Three independent10nm bands at450/550/650nm; synthetic chart, not colorimetric D65 or Macbeth.',
                'Real integer RAW and fixed ISP;4 independent noise acquisitions per field; no per-frame normalization.',
                'Actual reciprocal lens throughput and pupil-averaged planar sensor QE; physical pixel aperture once.',
                'IMX462 structural reference plus explicit assumed readout/materials; product matching not established.'])
        (folder/'camera.json').write_text(canonical(report));check()
        import zipfile
        with zipfile.ZipFile(folder/'camera-bundle.zip','w',compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(folder.iterdir()):
                if path.is_file() and path.suffix!='.zip': bundle.write(path,path.name)
        artifacts=[service.registry.store_file(p,p.name,'image/png' if p.suffix=='.png' else 'application/json' if p.suffix=='.json' else 'application/zip' if p.suffix=='.zip' else 'application/x-npz')
                   for p in sorted(folder.iterdir()) if p.is_file()]
        return dict(summary=report,artifacts=artifacts)


def get(service,identity):
    j=service.registry.get_run(identity)
    if j.get('job_kind')!=JOB_KIND: raise ValueError('Choose a native optical LUT camera study')
    return j


def install_routes(app,service):
    @app.post(ROUTE,status_code=202,tags=['lens_psf_lut'])
    def create(payload:dict): return submit(service,payload)
    @app.get(ROUTE,tags=['lens_psf_lut'])
    def listing():
        return [{k:j[k] for k in ('id','name','status','specification')}
                for j in service.registry.list_runs(job_kinds={JOB_KIND})]
    @app.get(ROUTE+'/{identity}',tags=['lens_psf_lut'])
    def detail(identity:str): return get(service,identity)
    @app.post(ROUTE+'/{identity}/cancel',tags=['lens_psf_lut'])
    def cancel(identity:str):
        get(service,identity);return service.registry.cancel(identity)
