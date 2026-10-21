from copy import deepcopy
import hashlib
import json

import numpy as np
from PIL import Image
import pytest

from camera_platform import esfr_iq_report as report
from camera_platform.registry import IntegrityError


def payload():
    metric = {'delta_e00': 2., 'linear_rgb_rmse': .01, 'delta_e_status': 'available'}
    qc = {'fit_eligible': True, 'exclusion_reasons': []}
    color = [{'capture_id': 'V1', 'patch_id': f'color-{i:02d}', 'raw_sha256': 'a'*64,
              'target_linear_srgb': [-.01, .4, 1.1], 'baseline_reference_rgb': [.1,.3,.6],
              'wb_only_reference_rgb': [.2,.3,.6], 'candidate_reference_rgb': [-.02,.4,1.2],
              'actual_isp_rgb': [.2,.4,.6], 'qc': qc,
              'before': metric, 'wb_only': metric, 'after': metric,
              'actual_isp': {**metric, 'delta_e00': None, 'delta_e_status': 'unknown_actual_isp_colorimetry'}} for i in range(18)]
    gray = [{'capture_id':'V1','patch_id':f'gray-{i:02d}','domain':domain,'nominal_reflectance':i/20,
             'observed':i/22,'prediction':i/21,'residual':-.01,'qc':qc,'status':'diagnostic'}
            for i in range(20) for domain in ('raw_green','actual_isp_luma')]
    evaluation = {'color':{'patches':color,'summary':{}},'tone':{'patches':gray},
                  'same_raw_before_after':True,'independent_of_training':True}
    return {'workflow_id':'measured-esfr-test','source_kind':'synthetic','source':{'run_id':'run-test'},
            'validation':evaluation,'development':deepcopy(evaluation),
            'provenance':{'validation_fit_performed':False,'same_raw_before_after':True,'profile_sha256':'b'*64},
            'reference':{'id':'imatest_esfr_2023_nominal_v1'},'profile':{'ccm':{'matrix':np.eye(3).tolist()}},
            'combined_replay':{'edge_count':112,'comparison_count':336,'refit_performed':False,
                'raw_changed_by_ccm':False,'isp_sfr_rmse':{'before':.1,'after':.01,'worst_after':.02,'count':336},
                'comparisons':[{'capture_id':'V1','roi_id':f'edge-{i:03d}','raw_before_rmse':.2,'raw_after_rmse':.01,'isp_before_rmse':.1,'isp_after_rmse':.01,'isp_threshold_status':'diagnostic'} for i in range(112)],
                'raw_gate_unchanged':{'status':'pass'}},
            'noise_repeat_analysis':{'validation':{'status':'unavailable','reason':'fixture'}}}


def test_ab_report_shows_mtf_failure_and_diagnostic_separately(tmp_path):
    import csv
    checks=[{'roi_id':'right-edge-4','capture_id':'V3','metric':'raw_sfr_rmse','value':.0387,'threshold':.03,'status':'fail'},
            {'roi_id':'right-edge-4','capture_id':'V3','metric':'isp_sfr_rmse','value':.06,'threshold':.05,'status':'diagnostic'}]
    value={'mode_a':{'mtf_evidence':{'image_validation':{'status':'fail','scope':'raw_native_sfr_only','checks':checks}}},'mode_b':{}}
    directory=tmp_path/'output';html=report._mode_gate_summary(directory,value)
    assert 'MTF 기준 초과 1건' in html and '0.0387' in html
    assert '<th>진단</th>' in html and '미제공' in html
    rows=list(csv.DictReader((directory/'ab-mtf-acceptance.csv').open(encoding='utf-8-sig')))
    assert [r['status'] for r in rows]==['fail','diagnostic']
    assert rows[0]['threshold']=='0.03'


def test_comparison_html_keeps_bulk_curve_data_in_json_not_dom(tmp_path):
    value=payload()
    comparison={'source_kind':'synthetic','mode_a':{'source':value['source'],'validation':value['validation'],
        'combined_replay':{'curves':{'BULK_CURVE_SENTINEL':list(range(3000))}}},'mode_b':{}}
    report.publish_comparison(tmp_path,comparison)
    assert 'BULK_CURVE_SENTINEL' not in (tmp_path/'report.html').read_text()
    assert 'BULK_CURVE_SENTINEL' in (tmp_path/'iq-result.json').read_text()


def test_all_patches_scope_unknown_colorimetry_and_complete_edge_rows(tmp_path):
    value=payload();before=deepcopy(value)
    result=report.publish(tmp_path,value)
    content=(tmp_path/'report.html').read_text()
    for text in ('합성 입력','IQ 판정 대기','DB 자동 활성화 없음','112개 edge','336개 독립','WB·노출만','unknown_actual_isp_colorimetry','동일 RAW','음수·1 초과','차트 영상 미제공'):
        assert text in content
    for i in range(18):assert f'color-{i:02d}' in content
    for i in range(20):assert f'gray-{i:02d}' in content
    for i in range(112):assert f'edge-{i:03d}' in content
    assert value==before
    assert json.loads((tmp_path/'iq-result.json').read_text())==value
    assert result['image_count']==0


def test_escaped_text_and_no_external_report_link(tmp_path):
    value=payload();value['workflow_id']='<script>alert(1)</script>'
    value['mtf_evidence']={'report_url':'https://outside.invalid/x'}
    report.publish(tmp_path,value)
    content=(tmp_path/'report.html').read_text()
    assert '<script>alert(1)</script>' not in content
    assert '&lt;script&gt;' in content
    assert 'https://outside.invalid/x' not in content
    with pytest.raises(ValueError,match='Unsafe'):
        report.publish(tmp_path,value,images=[{'files':{'raw':'../elsewhere.png'}}])


def test_chart_previews_preserve_same_raw_and_all_four_images(tmp_path, monkeypatch):
    from camera_platform import esfr_iq_engine as engine, esfr_io
    raw=tmp_path/'raw.bin';raw.write_bytes(b'raw-native')
    isp=tmp_path/'isp.png';Image.fromarray(np.full((64,64,3),100,np.uint8)).save(isp)
    raw_sha=hashlib.sha256(raw.read_bytes()).hexdigest();isp_sha=hashlib.sha256(isp.read_bytes()).hexdigest()
    capture={'record':{'id':'C1','metadata':{'isp':{'transfer':'srgb'}},'files':{'raw':{'sha256':raw_sha},'isp':{'sha256':isp_sha}}},'raw_path':raw,'isp_path':isp}
    def preview(*args,**kwargs):
        return {'normalized_raw_preview':np.full((16,16),.1), 'baseline_linear_rgb':np.full((16,16,3),.1),
                'candidate_linear_rgb':np.full((16,16,3),.5), 'raw_sha256':raw_sha,'same_raw_before_after':True,
                'native_shape':[64,64],'preview_shape':[16,16],'method':'fixture_native_then_decimation','tone_applied':False}
    monkeypatch.setattr(engine,'render_reference_preview',preview)
    images=report.render_chart_previews([capture],{},tmp_path)
    assert images[0]['raw_sha256']==raw_sha and images[0]['same_raw_before_after']
    assert set(images[0]['files'])=={'raw','baseline','candidate','actual_isp'}
    assert images[0]['tone_applied'] is False
    assert all((tmp_path/p).exists() for p in images[0]['files'].values())
    result=report.publish(tmp_path,payload(),images=images)
    assert result['image_count']==4
    assert (tmp_path/'report.html').read_text().count('<img ')==4
    assert hashlib.sha256(raw.read_bytes()).hexdigest()==raw_sha
    assert hashlib.sha256(isp.read_bytes()).hexdigest()==isp_sha
    isp.write_bytes(b'changed')
    with pytest.raises(IntegrityError,match='source hash'):
        report.render_chart_previews([capture],{},tmp_path)


def test_comparison_preserves_mode_conditions_and_never_claims_camera_dr(tmp_path):
    value=payload()
    comparison={'source_kind':'synthetic','mode_a':{'source':value['source'],'conditions':{'mode_id':'linear'},'validation':value['validation']},
                'mode_b':{'source':value['source'],'conditions':{'mode_id':'HDR'},'validation':value['validation']},
                'condition_differences':{'mode_id':{'a':'linear','b':'HDR'}}}
    report.publish_comparison(tmp_path,comparison)
    content=(tmp_path/'report.html').read_text()
    assert '모드 A' in content and '모드 B' in content
    assert '카메라 전체 동적 범위 측정이 아닙니다' in content
    assert json.loads((tmp_path/'iq-result.json').read_text())==comparison


def test_hdr_summary_derives_available_native_mtf_rows_and_shows_b_minus_a(tmp_path):
    source=payload()
    a={'validation':deepcopy(source['validation']), 'combined_replay':{
        'comparison_count':2, 'isp_sfr_rmse':{'after':999},
        'comparisons':[{'raw_after_rmse':.01,'isp_after_rmse':.01},
                       {'raw_after_rmse':.03,'isp_after_rmse':.03}]},
       'mtf_evidence':{'validation_id':'V-A','validation_result_sha256':'a'*64,
          'report_url':'/api/v1/calibration-lab/studies/V-A/files/esfr-images/report.html'}}
    b=deepcopy(a)
    b['combined_replay']['comparisons']=[{'raw_after_rmse':.03,'isp_after_rmse':None},
                                        {'raw_after_rmse':.05,'isp_after_rmse':.06}]
    b['mtf_evidence']['validation_id']='V-B'
    for row in b['validation']['color']['patches']:
        row['after']['delta_e00']=4.
    values=report._mode_comparison_metrics(a)
    assert values['raw_after_sfr_rmse']=={'mean':.02,'count':2,'total':2,'unavailable_count':0}
    assert values['combined_isp_after_sfr_rmse']['mean']==.02
    assert report._mode_comparison_metrics(b)['combined_isp_after_sfr_rmse']=={
        'mean':.06,'count':1,'total':2,'unavailable_count':1}
    document={'source_kind':'synthetic','mode_a':a,'mode_b':b}
    snapshot=deepcopy(document)
    report.publish_comparison(tmp_path,document)
    html=(tmp_path/'report.html').read_text()
    assert 'A/B 정량 요약 · B−A' in html
    assert '명목 색 ΔE00 평균</td><td>2</td><td>4</td><td>2</td>' in html
    assert 'RAW SFR RMSE 평균</td><td>0.02</td><td>0.04</td><td>0.02</td>' in html
    assert 'ISP SFR RMSE 평균</td><td>0.02</td><td>0.06</td><td>0.04</td>' in html
    assert '1 / 2 (미제공 1)' in html and '기준 미설정 · 판정 대기' in html
    assert '모드 A · 차트 범위 MTF 근거' in html and '모드 B · 차트 범위 MTF 근거' in html
    assert a['mtf_evidence']['report_url'] in html
    assert document==snapshot


def test_hdr_missing_mtf_metrics_stay_unavailable_not_zero(tmp_path):
    mode={'combined_replay':{'comparison_count':336,'isp_sfr_rmse':{'after':0},
                            'comparisons':[{'raw_after_rmse':None,'isp_after_rmse':None}]}}
    metrics=report._mode_comparison_metrics(mode)
    for key in ('raw_after_sfr_rmse','combined_isp_after_sfr_rmse'):
        assert metrics[key]=={'mean':None,'count':0,'total':336,'unavailable_count':336}
    summary=report._mode_summary({'mode_a':mode,'mode_b':{}})
    assert summary.count('<td>미제공</td>')==9
    assert '<td>0</td>' not in summary
    assert '0 / 336 (미제공 336)' in summary
    report.publish_comparison(tmp_path,{'source_kind':'synthetic','mode_a':{},'mode_b':{}})
    assert 'MTF 근거 미제공' in (tmp_path/'report.html').read_text()


def test_report_links_encode_exact_db_source_and_workflow_tuple(tmp_path):
    from html.parser import HTMLParser
    from urllib.parse import urlparse, parse_qs
    class Links(HTMLParser):
        def __init__(self):
            super().__init__();self.hrefs=[]
        def handle_starttag(self,tag,attrs):
            if tag=='a':self.hrefs.extend(v for k,v in attrs if k=='href')
    value=payload()
    value['source']={'run_id':'run-1&injected=other','candidate_id':'candidate-03 space',
                     'condition_id':'condition-006/#"<script>'}
    value['workflow_id']='measured-esfr-1&source_run=wrong'
    report.publish(tmp_path,value)
    links=Links();links.feed((tmp_path/'report.html').read_text())
    db=next(u for u in links.hrefs if u.startswith('/db-design.html?'))
    iq=next(u for u in links.hrefs if u.startswith('/esfr-measured.html?'))
    assert urlparse(db).netloc=='' and urlparse(iq).netloc==''
    assert parse_qs(urlparse(db).query)=={'run':[value['source']['run_id']],
        'candidate-b':[value['source']['candidate_id']], 'condition':[value['source']['condition_id']]}
    assert parse_qs(urlparse(iq).query)=={'source_run':[value['source']['run_id']],
        'candidate':[value['source']['candidate_id']], 'condition':[value['source']['condition_id']],
        'workflow':[value['workflow_id']]}
    assert '<script>' not in db and '<script>' not in iq
    assert report._report_navigation({'source':{'run_id':'run-only'},'workflow_id':'unbound'})==''


def csv_payload():
    value=payload()
    value['physical_validation']='not_established'
    value['reference'].update(sha256='r'*64,status='manufacturer_nominal_not_measured_print',source={'url':'https://www.imatest.com/imaging/iso-12233/'})
    value['profile'].update(profile_sha256='i'*64,white_balance=[1.1,1,1.2],exposure_scale=1.3)
    value['provenance'].update(reference_sha256='d'*64,patch_roi_sha256='p'*64,source_code={'source_hashes':{'numeric.py':'n'*64}})
    value['export_provenance']={'source_report_id':'original-report','source_report_sha256':'o'*64,
        'numerical_source_code':deepcopy(value['provenance']['source_code']),
        'render_source_code':{'source_hashes':{'render.py':'e'*64}},'renderer_sha256':'e'*64,
        'numerical_rerun':False,'refit_performed':False}
    for role in ('development','validation'):
        evaluation=value[role]
        evaluation.update(role=role,independent_of_training=True if role=='validation' else None,observation_sha256=role+'-observation')
        colors=[];tones=[];neutral=[];bindings=[]
        for repeat in range(3):
            capture_id=role+str(repeat)
            bindings.append({'id':capture_id,'raw_sha256':capture_id+'-raw','isp_sha256':capture_id+'-isp'})
            for original in evaluation['color']['patches']:
                row=deepcopy(original);row.update(capture_id=capture_id,role=role)
                row['actual_isp'].update(delta_e00=None,linear_rgb_rmse=None,max_abs_component_error=None)
                colors.append(row)
            for original in evaluation['tone']['patches']:
                row=deepcopy(original);row.update(capture_id=capture_id,role=role);tones.append(row)
            for i in range(20):
                neutral.append({'capture_id':capture_id,'patch_id':f'gray-{i:02d}','target_reflectance':.1,
                    'baseline_reference_rgb':[.1,.1,.1],'candidate_reference_rgb':[.11,.1,.09],
                    'before_rgb_rmse':0.,'after_rgb_rmse':.01,'qc':{'fit_eligible':True,'exclusion_reasons':[]}})
        evaluation['color']['patches']=colors;evaluation['tone']['patches']=tones;evaluation['neutral_reference_isp']={'patches':neutral}
        colors[0]['qc']={'fit_eligible':False,'exclusion_reasons':['raw_clipped']}
        value['provenance'][role+'_captures']=bindings
        value.setdefault('noise_repeat_analysis',{})[role]={'observation_sha256':role+'-repeat-observation','capture_bindings':[{'capture_id':b['id'],**b} for b in bindings],
            'noise':[{'patch_id':'color-00','domain':'raw','channel':'Gr','frame_count':3,'mean_signal':100.,'temporal_variance':None,
                'temporal_std':None,'snr':None,'snr_db':None,'status':'insufficient_repeats','uncertainty':'unavailable','unit':'DN'}]}
    return value


def read_csv(path):
    import csv
    with path.open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream))


def test_csv_full_color_gray_stages_missing_metrics_and_provenance(tmp_path):
    value=csv_payload();snapshot=deepcopy(value)
    result=report.publish(tmp_path,value)
    colors=read_csv(tmp_path/'color-patches.csv')
    assert len(colors)==54*4*2
    assert {r['stage'] for r in colors}=={'before','wb_only','after','actual_isp'}
    actual=[r for r in colors if r['stage']=='actual_isp']
    assert len(actual)==108 and all(r['delta_e00']==r['linear_rgb_rmse']==r['max_abs_component_error']=='' for r in actual)
    assert all(r['delta_e_status']=='unknown_actual_isp_colorimetry' for r in actual)
    assert all(r['profile_self_sha256']=='i'*64 and r['profile_document_sha256']=='b'*64 for r in colors)
    assert all(r['reference_sha256']=='r'*64 and r['reference_document_sha256']=='d'*64 for r in colors)
    assert json.loads(colors[0]['reference_source'])==value['reference']['source']
    assert json.loads(colors[0]['exclusion_reasons'])==['raw_clipped'] and colors[0]['fit_eligible']=='false'
    assert {r['independent_of_training'] for r in colors if r['role']=='validation'}=={'true'}
    assert {r['independent_of_training'] for r in colors if r['role']=='development'}=={''}
    grays=read_csv(tmp_path/'gray-tone.csv')
    assert len(grays)==360 and sum(r['analysis']=='forward_response' for r in grays)==240
    noise=read_csv(tmp_path/'noise-patches.csv')
    assert len(noise)==2 and all(r['temporal_std']==r['snr_db']=='' for r in noise)
    assert all(len(json.loads(r['capture_ids']))==3 for r in noise)
    provenance=read_csv(tmp_path/'provenance.csv')
    values={(r['section'],r['field']):json.loads(r['value_json']) for r in provenance}
    assert values['profile','white_balance']==[1.1,1,1.2]
    assert values['profile','ccm.matrix']==np.eye(3).tolist()
    assert values['numerical_provenance','source_code.source_hashes.numeric.py']=='n'*64
    assert values['export_provenance','render_source_code.source_hashes.render.py']=='e'*64
    assert values['export_provenance','numerical_rerun'] is False
    html=(tmp_path/'report.html').read_text()
    for name in report.CSV_FILES:
        assert f'href="{name}"' in html and name in result['files']
    assert '보고서 재발행 근거' in html
    assert value==snapshot


def test_csv_spatial_rows_separate_from_temporal_and_empty_headers_are_valid(tmp_path):
    value=csv_payload()
    value['numerical']={'observations':{'captures':[{'capture_id':'validation0','role':'validation',
        'patches':[{'id':'gray-00','raw':{'Gr':{'mean':120.,'spatial_std':3.,'spatial_variance':9.,'sample_count':20}},
        'isp':{'G':{'mean':.1,'spatial_std':.02,'spatial_variance':.0004,'sample_count':80}},
        'qc':{'fit_eligible':False,'exclusion_reasons':['clipped']}}]}]}}
    report.publish(tmp_path,value)
    rows=read_csv(tmp_path/'noise-patches.csv')
    spatial=[r for r in rows if r['statistic_kind']=='spatial_per_capture']
    assert len(spatial)==2 and all(r['temporal_variance']=='' for r in spatial)
    assert {r['unit'] for r in spatial}=={'DN','decoded_relative_output'}
    assert all(json.loads(r['exclusion_reasons'])==['clipped'] for r in spatial)
    empty=tmp_path/'empty';report.publish(empty,{'source_kind':'synthetic'})
    assert read_csv(empty/'color-patches.csv')==[]
    assert 'delta_e00' in (empty/'color-patches.csv').read_text(encoding='utf-8-sig')


def test_csv_noise_hash_binds_its_repeat_bundle_and_variance_units(tmp_path):
    value=csv_payload()
    for role in ('development','validation'):
        repeated=value['noise_repeat_analysis'][role]
        repeated['capture_bindings'].append({'capture_id':role+'-extra','raw_sha256':role+'-extra-raw','isp_sha256':role+'-extra-isp'})
        repeated['noise'][0]['frame_count']=4
    value['numerical']={'observations':{'captures':[{'capture_id':'validation0','role':'validation',
        'patches':[{'id':'gray-00','raw':{'Gr':{'mean':120.,'spatial_std':3.,'spatial_variance':9.,'sample_count':20}},
        'isp':{'G':{'mean':.1,'spatial_std':.02,'spatial_variance':.0004,'sample_count':80}}}]}]}}
    report.publish(tmp_path,value)
    rows=read_csv(tmp_path/'noise-patches.csv')
    temporal=[r for r in rows if r['statistic_kind']=='temporal_repeat']
    assert len(temporal)==2
    for row in temporal:
        assert row['observation_sha256']==row['role']+'-repeat-observation'
        assert row['observation_sha256']!=value[row['role']]['observation_sha256']
        assert row['frame_count']=='4' and len(json.loads(row['capture_ids']))==4
        assert row['unit']=='DN' and row['variance_unit']=='DN^2'
    spatial=[r for r in rows if r['statistic_kind']=='spatial_per_capture']
    assert {r['variance_unit'] for r in spatial}=={'DN^2','decoded_relative_output^2'}
    assert all(r['observation_sha256']=='validation-observation' for r in spatial)
    del value['noise_repeat_analysis']['validation']['observation_sha256']
    report.publish(tmp_path,value)
    assert next(r for r in read_csv(tmp_path/'noise-patches.csv') if r['role']=='validation' and r['statistic_kind']=='temporal_repeat')['observation_sha256']==''
