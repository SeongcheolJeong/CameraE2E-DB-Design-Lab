"""Run fresh normal/DCG-HDR development+holdout workflows on the live Lab API.

All inputs remain synthetic. Sources, polling, uploads, fits and validation use
HTTP only. No historical run ID, server SQLite or external disk is required.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import secrets
import time
import zipfile

import numpy as np
import httpx
import tifffile

from camera_platform.esfr_chart import render_chart
from camera_platform.esfr_lab import chart_template
from camera_platform.registry import canonical, digest
from camera_platform.service import environment_fingerprint
from esfr_hdr_ab_fixture import capture, declaration

ROOT='/api/v1/camera-validation/esfr'
from esfr_demo_source import arguments, source_selection, bind_server, latest_query


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8781')
    parser.add_argument('--output',type=Path,required=True)
    arguments(parser)
    args=parser.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+args.timeout
    def check():
        if time.monotonic()>deadline:raise TimeoutError('E-SFR example deadline exceeded; receipts are preserved')
    session=httpx.Client()
    def request(path,payload=None):
        if payload is None:
            for attempt in range(4):
                try:r=session.get(args.base_url+path,timeout=900);break
                except (httpx.TransportError,httpx.TimeoutException):
                    if attempt==3:raise
                    time.sleep(attempt+1)
        else:r=session.post(args.base_url+path,json=payload,timeout=1800)
        r.raise_for_status();return r.json()
    def save(path,value):
        path.parent.mkdir(exist_ok=True,parents=True);path.write_text(json.dumps(value,ensure_ascii=False,indent=2))
    def saved(path,fn):
        if path.exists():return json.loads(path.read_text())
        value=fn();save(path,value);return value
    session.headers['X-Platform-Token']=request('/api/v1/session')['token']
    health=request('/api/v1/health');save(out/'health.json',health)
    bind_server(out,health)
    source=source_selection(request,args,out,check=check)
    fixture=saved(out/'private-generator.json',lambda:{'schema':'esfr_normal_dcg_ab_experiment_v1',
        'token':secrets.token_hex(8),'seeds':[secrets.randbits(50) for _ in range(12)],
        'matrix':[[.72,.12,.06],[.18,.78,.16],[.10,.10,.78]],'channel_gains':[.78,.92,.68],
        'sigma':1.4,'scope':'Private color/blur truth is never supplied to calibration; both modes use this same camera.',
        'created_at':datetime.now(timezone.utc).isoformat()})
    scene,rois,render_manifest=render_chart(shape=(1440,2560),supersample=4)
    camera_scene=(scene@np.asarray(fixture['matrix']))*np.asarray(fixture['channel_gains'])
    template=request(ROOT+'/template');chart=deepcopy(template['chart_profile'])
    chart.update(id='extended-esfr-normal-dcg-ab-v1',rois=rois,chart_geometry=chart_template(),
        render_manifest=render_manifest,reference_status='nominal_design_synthetic_not_measured_print')
    common=deepcopy(template['capture_metadata'])
    common.update(schema='camera_esfr_capture_v1',source_kind='synthetic',camera_id='normal-dcg-ab-'+fixture['token'],pixel_pitch_um=2.9)
    common['raw_format']={'packing':'unpacked16','width':2560,'height':1440,'stride_bytes':5120,'header_bytes':0,
        'byte_order':'little','bit_depth':12,'bit_shift':0,'cfa':'RGGB','black_level_dn':64,'white_level_dn':4095}
    common['geometry']={'full_sensor_shape':[1440,2560],'crop_origin':[0,0]}
    def acquire(mode,role,index,contract=None):
        directory=out/mode;receipt=directory/f'{role}-{index}-upload.json'
        if receipt.exists():return json.loads(receipt.read_text())
        folder=directory/f'{role}-{index}';folder.mkdir(parents=True,exist_ok=True)
        metadata_file=folder/'metadata.json'
        if not metadata_file.exists():
            meta=deepcopy(common)
            meta.update(role=role,session_id=f'{role}-{mode}-{fixture["token"]}',
                acquisition_id=f'{mode}-{role}-{index}-{fixture["token"]}',generated_at=datetime.now(timezone.utc).isoformat(),
                synthetic_generator=declaration(mode))
            meta['conditions']={'exposure_s':.01,'gain':1.,'light_id':'nominal-chart-linear-rgb',
                'mode_id':'normal-low-gain' if mode=='normal' else 'HDR-single-exposure-DCG',
                'focus_setting':'fixed-synthetic-gaussian','focus_distance_m':2.,'distance_m':2.,
                'temperature_c':25.,'aperture_f_number':4.12}
            meta['isp']={'transfer':'linear','coordinate_mapping':'same_native_raster',
                'comparison_mode':'matched_bilinear' if mode=='normal' else 'diagnostic'}
            if contract:meta['validation_contract']={'id':contract['id'],'content_hash':contract['content_hash']}
            seed_index=(6 if mode=='hdr' else 0)+(3 if role=='validation' else 0)+index
            raw,isp=capture(camera_scene,fixture['seeds'][seed_index],mode,fixture['sigma'])
            raw.tofile(folder/'capture.raw');tifffile.imwrite(folder/'capture.tiff',isp,photometric='rgb')
            metadata_file.write_text(canonical(meta))
        with (folder/'capture.raw').open('rb') as raw,(folder/'capture.tiff').open('rb') as isp:
            r=session.post(args.base_url+ROOT+'/captures',data={'metadata':metadata_file.read_text()},
                files={'raw':('capture.raw',raw),'isp':('capture.tiff',isp)},timeout=900)
        r.raise_for_status();value=r.json();save(receipt,value)
        print(json.dumps({'uploaded':mode,'role':role,'index':index+1,'id':value['id']}),flush=True)
        return value
    work={}
    for mode in ('normal','hdr'):
        directory=out/mode;directory.mkdir(exist_ok=True)
        dev=[acquire(mode,'development',i) for i in range(3)]
        start=saved(directory/'workflow-submitted.json',lambda:request(ROOT+'/measured/software-workflows',{
            'source':source,'development_ids':[c['id'] for c in dev],'rois':rois,'chart_profile':chart,
            'iq':{'enabled':True,'reference_id':'imatest_esfr_2023_nominal_v1','actual_isp_color_space':'srgb',
                'patch_roi_reviewed_capture_ids':[c['id'] for c in dev],'noise_repeat_ids':[]}}))
        work[mode]=start['id']
    save(out/'workflows.json',work)
    done={};markers={}
    while len(done)<2:
        check()
        for mode,identity in work.items():
            if mode in done:continue
            directory=out/mode
            state=request(ROOT+'/measured/workflows/'+identity)
            marker=(state.get('status'),state.get('stage'),state.get('pending_operation'))
            if markers.get(mode)!=marker:
                markers[mode]=marker;print(json.dumps({'mode':mode,'workflow':identity,'state':marker}),flush=True)
            if state['status']=='failed':
                save(directory/'failed.json',state);raise RuntimeError(mode+': '+str(state.get('error')))
            if state['status']=='awaiting_validation' and not (directory/'validation-submitted.json').exists():
                frozen=request(ROOT+'/measured/workflows/'+identity);save(directory/'frozen.json',frozen)
                val=[acquire(mode,'validation',i,frozen['contract']) for i in range(3)]
                receipt=request(ROOT+'/measured/workflows/'+identity+'/validate',{'validation_ids':[c['id'] for c in val],
                    'iq_patch_roi_reviewed_capture_ids':[c['id'] for c in val],'iq_noise_repeat_ids':[]})
                save(directory/'validation-submitted.json',receipt)
            if state['status']=='completed' and state.get('iq_result'):
                value=request(ROOT+'/measured/workflows/'+identity);save(directory/'completed.json',value)
                iq=value['iq_result']
                assert iq['provenance']['source_code']==environment_fingerprint()
                assert iq['source']==source and iq['physical_validation']=='not_established'
                assert iq['validation']['same_raw_before_after'] and not iq['provenance']['validation_fit_performed']
                assert not iq['db_active_model_updated'] and len(iq['combined_replay']['comparisons'])==336
                assert value['thresholds']=={'raw_sfr_rmse':.03,'isp_sfr_rmse':.05}
                done[mode]=iq
        if len(done)<2:time.sleep(10)
    comparison=saved(out/'comparison.json',lambda:request(ROOT+'/measured/iq-comparisons',{
        'workflow_a_id':work['normal'],'workflow_b_id':work['hdr']}))
    assert comparison['ab_comparison']['independence']['status']=='verified'
    assert len(comparison['ab_comparison']['mtf'])==112*8
    assert len(comparison['ab_comparison']['color'])==18
    assert len(comparison['ab_comparison']['tone'])==40
    assert len(comparison['ab_comparison']['noise'])==20*7
    assert all(row['metrics']['mtf50'][mode]['total']==3 for row in comparison['ab_comparison']['mtf'] for mode in ('a','b'))
    def download_bundle(directory,record):
        directory.mkdir(exist_ok=True)
        for name,key in [('report.html','report_url'),('evidence.zip','bundle_url')]:
            r=session.get(args.base_url+record[key],timeout=900);r.raise_for_status();(directory/name).write_bytes(r.content)
        with zipfile.ZipFile(directory/'evidence.zip') as archive:
            assert archive.testzip() is None
            manifest=json.loads(archive.read('bundle-manifest.json'))
            assert len(archive.namelist())==len(set(archive.namelist()))
            assert set(archive.namelist())=={r['path'] for r in manifest['files']}|{'bundle-manifest.json'}
            for item in manifest['files']:
                data=archive.read(item['path'])
                assert hashlib.sha256(data).hexdigest()==item['sha256']==record['files'][item['path']]
                assert len(data)==item['bytes']
        return {'zip_sha256':hashlib.sha256((directory/'evidence.zip').read_bytes()).hexdigest(),'zip_entries':len(manifest['files'])+1}
    archives={mode:download_bundle(out/mode,iq) for mode,iq in done.items()}
    archives['comparison']=download_bundle(out/'comparison',comparison)
    latest=request(ROOT+'/measured/iq-latest?'+latest_query(source))
    save(out/'db-latest.json',latest)
    assert latest['comparison']['id']==comparison['id']
    summary={'status':'verified','source_kind':'synthetic','physical_validation':'not_established',
        'workflow_ids':work,'comparison_id':comparison['id'],'report_url':args.base_url+comparison['report_url'],
        'bundle_url':args.base_url+comparison['bundle_url'],'archives':archives,
        'db_active_models_unchanged':all(not iq['db_active_model_updated'] for iq in done.values()),
        'preservation_scope':'Workflow assertion: no active model update; no broad registry audit claimed',
        'mode_metrics':{mode:{'color':iq['validation']['color']['summary'],'tone':iq['validation']['tone']['summary'],
            'mtf':iq['mtf_evidence']['image_validation'],'combined_isp':iq['combined_replay']['isp_sfr_rmse']}
            for mode,iq in done.items()},'ab_tables':comparison['ab_comparison']}
    save(out/'verification.json',summary)
    print(json.dumps({'status':'verified','comparison':comparison['id'],'report':summary['report_url']}),flush=True)


if __name__=='__main__':main()
