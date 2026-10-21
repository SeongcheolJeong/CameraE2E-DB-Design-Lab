"""HTTP-only explicit DB model -> 3+3 independent synthetic E-SFR comparison.

The hidden synthetic camera is independent of the selected DB PSF prior. The
example verifies software linkage, not physical identity or absolute radiometry.
"""
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import time
import zipfile

import httpx
import numpy as np
import tifffile

from camera_platform.esfr_chart import render_chart
from camera_platform.esfr_lab import chart_template
from camera_platform.registry import canonical
from esfr_hdr_ab_fixture import capture, declaration


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url',default='http://127.0.0.1:8781')
    p.add_argument('--source-json',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();out=args.output;out.mkdir(parents=True,exist_ok=True)
    client=httpx.Client(base_url=args.base_url,timeout=900)
    client.headers['X-Platform-Token']=client.get('/api/v1/session').json()['token']
    root='/api/v1/camera-validation/esfr'
    def request(path,payload=None):
        r=client.get(path) if payload is None else client.post(path,json=payload)
        r.raise_for_status();return r.json()
    def saved(name,fn):
        file=out/name
        if file.exists():return json.loads(file.read_text())
        result=fn();file.write_text(json.dumps(result,ensure_ascii=False,indent=2));return result
    health=request('/api/v1/health');saved('server-binding.json',lambda:health)
    assert json.loads((out/'server-binding.json').read_text())['state_dir']==health['state_dir']
    source=json.loads(args.source_json.read_text())
    from urllib.parse import urlencode
    source_fields=saved('source-fields.json',lambda:request(root+'/camera-models/source-fields?'+urlencode(source)))
    requirements=source_fields['requirements']
    fixture=saved('private-generator.json',lambda:{'token':secrets.token_hex(8),'seeds':[secrets.randbits(50) for _ in range(6)],
        'sigma':1.4,'private_model_not_supplied_to_fit':True})
    scene,rois,manifest=render_chart(shape=(1440,2560),supersample=4)
    scene=(scene@np.array([[.72,.12,.06],[.18,.78,.16],[.10,.10,.78]]))*np.array([.78,.92,.68])
    template=request(root+'/template');chart=deepcopy(template['chart_profile'])
    chart.update(rois=rois,chart_geometry=chart_template(),render_manifest=manifest,reference_status='nominal_design_synthetic_not_measured_print')
    common=deepcopy(template['capture_metadata'])
    common.update(schema='camera_esfr_capture_v1',source_kind='synthetic',camera_id='selected-model-'+fixture['token'],pixel_pitch_um=2.9)
    common['raw_format']={'packing':'unpacked16','width':2560,'height':1440,'stride_bytes':5120,'header_bytes':0,
        'byte_order':'little','bit_depth':12,'bit_shift':0,'cfa':'RGGB','black_level_dn':64,'white_level_dn':4095}
    common['geometry']={'full_sensor_shape':[1440,2560],'crop_origin':[0,0]}
    common['conditions']={'exposure_s':.01,'gain':requirements['gain'],'temperature_c':requirements['temperature_c'],
        'aperture_f_number':requirements['aperture_f_number'],'light_id':'D65','mode_id':'normal-low-gain',
        'focus_setting':'fixed-independent-synthetic','focus_distance_m':2.,'distance_m':2.}
    common['isp']={'transfer':'linear','coordinate_mapping':'same_native_raster','comparison_mode':'matched_bilinear'}
    def acquire(role,i,contract=None):
        def upload():
            folder=out/(role+'-'+str(i));folder.mkdir(exist_ok=True)
            if not (folder/'metadata.json').exists():
                meta=deepcopy(common);meta.update(role=role,session_id=role+'-'+fixture['token'],
                    acquisition_id=role+'-'+str(i)+'-'+fixture['token'],generated_at=datetime.now(timezone.utc).isoformat(),synthetic_generator=declaration('normal'))
                if contract:meta['validation_contract']={'id':contract['id'],'content_hash':contract['content_hash']}
                raw,isp=capture(scene,fixture['seeds'][i+(3 if role=='validation' else 0)],'normal',fixture['sigma'])
                raw.tofile(folder/'capture.raw');tifffile.imwrite(folder/'capture.tiff',isp,photometric='rgb')
                (folder/'metadata.json').write_text(canonical(meta))
            with (folder/'capture.raw').open('rb') as raw,(folder/'capture.tiff').open('rb') as isp:
                r=client.post(root+'/captures',data={'metadata':(folder/'metadata.json').read_text()},files={'raw':('capture.raw',raw),'isp':('capture.tiff',isp)})
            r.raise_for_status();return r.json()
        row=saved(role+'-'+str(i)+'-upload.json',upload);print('capture',role,i,row['id'],flush=True);return row
    dev=[acquire('development',i) for i in range(3)]
    model=saved('camera-model.json',lambda:request(root+'/camera-models',{
        'source':source,'development_capture_id':dev[0]['id'],
        'camera':{'camera_id':common['camera_id'],'manufacturer':'SYNTHETIC','model':'Independent virtual camera / selected DB prior',
            'serial':fixture['token'],'lens':'synthetic effective Gaussian (private); selected DB is the baseline prior',
            'sensor':'synthetic RGGB 2.9 um; not a verified Sony product','mode':common['conditions']['mode_id']},
        'spatial':{'mode':'uniform_selected_db_field','source_field_id':'center','acknowledged':True}}))
    wrong=request(root+'/camera-models/'+model['id'])
    assert wrong['source']==source and wrong['source_context']['role']=='numerical_prediction_source'
    start=saved('workflow-submitted.json',lambda:request(root+'/measured/software-workflows',{
        'source':source,'camera_model_id':model['id'],'development_ids':[c['id'] for c in dev],
        'rois':rois,'chart_profile':chart,'iq':{'enabled':True,'reference_id':'imatest_esfr_2023_nominal_v1',
            'actual_isp_color_space':'srgb','patch_roi_reviewed_capture_ids':[c['id'] for c in dev],'noise_repeat_ids':[]}}))
    deadline=time.monotonic()+7200;previous=None
    while time.monotonic()<deadline:
        state=request(root+'/measured/workflows/'+start['id'])
        marker=(state['status'],state['stage'])
        if marker!=previous:print('workflow',start['id'],marker,flush=True);previous=marker
        if state.get('resume_required') and state.get('stage')=='selected_model_comparison':
            saved('report-resume.json',lambda:request(root+'/measured/workflows/'+start['id']+'/resume',{}))
        if state['status']=='failed':
            (out/'failed.json').write_text(json.dumps(state,ensure_ascii=False,indent=2));raise RuntimeError(state.get('error'))
        if state['status']=='awaiting_validation' and not (out/'validation-submitted.json').exists():
            saved('frozen.json',lambda:state)
            val=[acquire('validation',i,state['contract']) for i in range(3)]
            saved('validation-submitted.json',lambda:request(root+'/measured/workflows/'+start['id']+'/validate',{
                'validation_ids':[c['id'] for c in val],'iq_patch_roi_reviewed_capture_ids':[c['id'] for c in val],'iq_noise_repeat_ids':[]}))
        if state['status']=='completed':break
        time.sleep(5)
    else:raise TimeoutError('Model workflow deadline exceeded; receipts preserved')
    (out/'completed.json').write_text(json.dumps(state,ensure_ascii=False,indent=2))
    result=state['model_comparison'];assert result['model_sha256']==model['content_hash']
    assert not result['db_active_model_updated'] and state['profile_activated'] is False
    assert result['no_gaussian_substitution_for_db'] and not result['validation_refit_performed']
    assert result['thresholds']=={'raw_sfr_rmse':.03,'isp_sfr_rmse':.05}
    for name,key in [('report.html','report_url'),('bundle.zip','bundle_url')]:
        r=client.get(result[key]);r.raise_for_status();(out/name).write_bytes(r.content)
    with zipfile.ZipFile(out/'bundle.zip') as z:
        assert z.testzip() is None
        manifest=json.loads(z.read('bundle-manifest.json'))
        assert set(z.namelist())=={r['path'] for r in manifest['files']}|{'bundle-manifest.json'}
        for row in manifest['files']:
            b=z.read(row['path']);assert hashlib.sha256(b).hexdigest()==row['sha256']==result['files'][row['path']]
        full=json.loads(z.read('report.json'));assert len(full['edges'])==112 and len(full['comparisons'])==336
        assert len(full['source_bindings'])==112
        assert all(r['after_raw_sha256']==r['after_raw_after_ccm_sha256'] for r in full['source_bindings'])
        edges={e['roi_id']:e for e in full['edges']}
        for binding in full['source_bindings']:
            saved_raw=z.read(edges[binding['roi_id']]['images']['after']['raw_array'])
            assert hashlib.sha256(saved_raw).hexdigest()==binding['after_raw_sha256']==binding['after_raw_after_ccm_sha256']
        uploads=[json.loads(p.read_text()) for p in sorted(out.glob('*-upload.json'))]
        assert len(uploads)==6 and len({u['native_pixel_sha256'] for u in uploads})==6
        assert len({u['files']['raw']['sha256'] for u in uploads})==6
    query={'source_run':source['run_id'],'candidate':source['candidate_id']}
    if source.get('condition_id'):query['condition']=source['condition_id']
    latest=request(root+'/camera-models/comparisons/latest?'+urlencode(query));assert latest['comparison']['id']==result['id']
    saved('verification.json',lambda:{'status':'verified','source_kind':'synthetic','physical_validation':'not_established',
        'model_id':model['id'],'workflow_id':state['id'],'comparison_id':result['id'], 'summary':result['summary'],
        'report_url':args.base_url+result['report_url'],'bundle_url':args.base_url+result['bundle_url'],
        'db_url':args.base_url+result['db_url'],'zip_sha256':hashlib.sha256((out/'bundle.zip').read_bytes()).hexdigest()})
    print(json.dumps(json.loads((out/'verification.json').read_text()),ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
