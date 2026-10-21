"""Independent synthetic software verification of the staged 112-edge + IQ workflow.

Generated inputs always retain synthetic source_kind. Holdout pixels are created
only after the server has frozen both MTF and CCM. Private matrix/noise truth is
never supplied to the fitter. Existing results are resumed, never overwritten.
"""
from pathlib import Path
from copy import deepcopy
from datetime import datetime, timezone
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
from camera_platform.esfr_demo import chart_capture
from camera_platform.esfr_lab import chart_template
from camera_platform.registry import canonical

ROOT = '/api/v1/camera-validation/esfr'
from esfr_demo_source import arguments, source_selection, bind_server, latest_query


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8784')
    parser.add_argument('--output',required=True,type=Path)
    arguments(parser)
    args=parser.parse_args(); out=args.output; out.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+args.timeout
    def check():
        if time.monotonic()>deadline:raise TimeoutError("E-SFR example deadline exceeded; receipts preserved")
    session=httpx.Client()
    def request(path,payload=None):
        # Retry only idempotent reads. An uncertain mutation needs reconciliation.
        for attempt in range(4):
            try:
                response=session.get(args.base_url+path,timeout=180) if payload is None else session.post(args.base_url+path,json=payload,timeout=900)
                break
            except (httpx.TransportError, httpx.TimeoutException):
                if payload is not None or attempt == 3: raise
                time.sleep(1 + attempt)
        response.raise_for_status(); return response.json()
    session.headers['X-Platform-Token']=request('/api/v1/session')['token']
    def save(name,value): (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2))
    def saved(name,fn):
        p=out/name
        if p.exists(): return json.loads(p.read_text())
        result=fn(); save(name,result); return result
    health=request('/api/v1/health');save('health.json',health)
    bind_server(out,health)
    source=source_selection(request,args,out,check=check)
    fixture=saved('private-generator.json',lambda:{'token':secrets.token_hex(8),
        'seeds': [secrets.randbits(50) for _ in range(6)],
        'matrix':[[.72,.12,.06],[.18,.78,.16],[.10,.10,.78]], 'channel_gains':[.78,.92,.68],
        'sigma':1.4,'source_kind':'synthetic','scope':'private generator truth; never fitter input'})
    # A second execution reuses only this acquisition fixture's truth; never any
    # validation pixel or error to tune the development profile.
    scene,rois,render_manifest=render_chart(shape=(1440,2560),supersample=4)
    camera_scene=(scene @ np.asarray(fixture['matrix']))*np.asarray(fixture['channel_gains'])
    template=request(ROOT+'/template')
    chart=deepcopy(template['chart_profile'])
    chart.update(id='extended-esfr-iq-software-fixture',rois=rois,chart_geometry=chart_template(),
        render_manifest=render_manifest,reference_status='nominal_design_synthetic_not_measured_print')
    meta=deepcopy(template['capture_metadata'])
    meta.update(schema='camera_esfr_capture_v1',source_kind='synthetic',camera_id='iq-software-'+fixture['token'],pixel_pitch_um=2.9)
    meta['conditions']={'exposure_s':.01,'gain':1.,'light_id':'nominal-chart-linear-rgb','mode_id':'linear-bayer',
        'focus_setting':'fixed-synthetic-gaussian','focus_distance_m':2.,'distance_m':2.,'temperature_c':25.,'aperture_f_number':4.12}
    meta['raw_format']={'packing':'unpacked16','width':2560,'height':1440,'stride_bytes':5120,'header_bytes':0,
        'byte_order':'little','bit_depth':12,'bit_shift':0,'cfa':'RGGB','black_level_dn':64,'white_level_dn':4095}
    meta['geometry']={'full_sensor_shape':[1440,2560],'crop_origin':[0,0]}
    meta['isp']={'transfer':'linear','coordinate_mapping':'same_native_raster'}
    def acquire(role,index,contract=None):
        receipt=out/f'{role}-{index}-upload.json'
        if receipt.exists():return json.loads(receipt.read_text())
        folder=out/f'{role}-{index}'; folder.mkdir(exist_ok=True)
        metadata_file=folder/'metadata.json'
        if not metadata_file.exists():
            m=deepcopy(meta);m.update(role=role,session_id=role+'-'+fixture['token'],
                acquisition_id=f'iq-{fixture["token"]}-{role}-{index}',generated_at=datetime.now(timezone.utc).isoformat(),
                synthetic_generator={'method':'independent_full_chart_color_mix_v1','truth_uploaded':False})
            if contract:m['validation_contract']={'id':contract['id'],'content_hash':contract['content_hash']}
            raw,isp=chart_capture(camera_scene,fixture['seeds'][index+(3 if role=='validation' else 0)],fixture['sigma'])
            raw.tofile(folder/'capture.raw');tifffile.imwrite(folder/'capture.tiff',isp,photometric='rgb')
            metadata_file.write_text(canonical(m))
        with (folder/'capture.raw').open('rb') as raw,(folder/'capture.tiff').open('rb') as isp:
            response=session.post(args.base_url+ROOT+'/captures',data={'metadata':metadata_file.read_text()},
                files={'raw':('capture.raw',raw),'isp':('capture.tiff',isp)},timeout=900)
        response.raise_for_status();result=response.json();save(receipt.name,result)
        print('Uploaded '+role+' '+str(index+1),flush=True);return result
    dev=[acquire('development',i) for i in range(3)]
    start=saved('workflow-submitted.json',lambda:request(ROOT+'/measured/software-workflows',{
        'source':source,'development_ids':[c['id'] for c in dev],'rois':rois,'chart_profile':chart,
        'iq':{'enabled':True,'reference_id':'imatest_esfr_2023_nominal_v1','actual_isp_color_space':'srgb',
              'patch_roi_reviewed_capture_ids':[c['id'] for c in dev],'noise_repeat_ids':[]}}))
    def wait(target):
        marker=None
        while True:
            check()
            state=request(ROOT+'/measured/workflows/'+start['id'])
            now=(state['status'],state['stage'],state.get('current_job',{}).get('status') if state.get('current_job') else None)
            if now!=marker:print(json.dumps({'workflow':start['id'],'status':now},ensure_ascii=False),flush=True);marker=now
            if state['status']=='failed':raise RuntimeError(state['error'])
            if state['status'] in target and (state['status'] != 'completed' or state.get('iq_result')):return state
            time.sleep(5)
    frozen=wait({'awaiting_validation','completed'})
    if frozen['status']!='completed':
        save('frozen.json',frozen)
        val=[acquire('validation',i,frozen['contract']) for i in range(3)]
        saved('validation-submitted.json',lambda:request(ROOT+'/measured/workflows/'+start['id']+'/validate',{
            'validation_ids':[c['id'] for c in val],
            'iq_patch_roi_reviewed_capture_ids':[c['id'] for c in val],'iq_noise_repeat_ids':[]}))
    completed=wait({'completed'});save('completed.json',completed)
    assert completed['source_kind']=='synthetic' and completed['thresholds']=={'raw_sfr_rmse':.03,'isp_sfr_rmse':.05}
    assert completed['physical_validation']=='not_established'
    iq=completed['iq_result']
    from camera_platform.service import environment_fingerprint
    assert iq['provenance']['source_code'] == environment_fingerprint()
    assert iq['source'] == source and iq['db_active_model_updated'] is False
    assert iq['provenance']['same_raw_before_after'] is True
    assert iq['provenance']['validation_fit_performed'] is False
    assert iq['validation']['color']['total_patch_observations'] == 54
    combined=iq['combined_replay']
    assert combined['edge_count'] == 112 and combined['comparison_count'] == 336
    assert len(combined['source_bindings']) == 224
    assert all(b['raw_before_ccm_sha256'] == b['raw_after_ccm_sha256'] and b['raw_changed_by_ccm'] is False
               for b in combined['source_bindings'])
    original_raw={json.loads((out/f'validation-{i}-upload.json').read_text())['files']['raw']['sha256'] for i in range(3)}
    assert set(iq['validation']['raw_sha256']) == original_raw
    assert {c['raw_sha256'] for c in iq['provenance']['validation_captures']} == original_raw
    save('result-summary.json',{'workflow_id':start['id'],'source':source,'source_kind':'synthetic',
        'physical_validation':'not_established','mtf':iq['mtf_evidence']['image_validation'],
        'color':iq['validation']['color']['summary'],'combined_isp':combined['isp_sfr_rmse'],
        'profile_sha256':iq['provenance']['profile_sha256'],'db_active_model_updated':False,
        'ccm_raw_identity_verified':True,'current_source_verified':True})
    for name,url in [('report.html',iq['report_url']),('evidence.zip',iq['bundle_url'])]:
        r=session.get(args.base_url+url,timeout=900);r.raise_for_status();(out/name).write_bytes(r.content)
    with zipfile.ZipFile(out/'evidence.zip') as z:
        assert z.testzip() is None
        entries={name:hashlib.sha256(z.read(name)).hexdigest() for name in z.namelist() if not name.endswith('/')}
        manifest=json.loads(z.read('bundle-manifest.json'))
        for item in manifest['files']:
            assert entries[item['path']] == item['sha256'], item['path']
            assert len(z.read(item['path'])) == item['bytes'], item['path']
        assert set(entries) == {item['path'] for item in manifest['files']} | {'bundle-manifest.json'}
    latest=request(ROOT+'/measured/iq-latest?'+latest_query(source))
    save('db-latest.json',latest)
    assert latest['report']['workflow_id']==start['id']
    save('verification.json',{'status':'verified','workflow_id':start['id'],'source':source,
        'source_kind':'synthetic','physical_validation':'not_established','report_url':iq['report_url'],
        'bundle_url':iq['bundle_url'],'zip_entries_sha256':entries,'iq_result':iq})
    print(json.dumps({'status':'verified','workflow_id':start['id'],'report':iq['report_url'],'zip_entries':len(entries)}),flush=True)


if __name__=='__main__':main()
