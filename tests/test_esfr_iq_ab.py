from copy import deepcopy
import json
import sys
from pathlib import Path
import numpy as np
import pytest

from camera_platform import esfr_iq_ab as ab
from camera_platform.esfr_measured import isp_is_diagnostic
from camera_platform.registry import IntegrityError, digest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from esfr_hdr_ab_fixture import capture, readout, READOUT


def test_dcg_shared_charge_preserves_highlights_reduces_read_noise():
    photo=np.full((300,300),100.)
    a=readout(photo,np.random.default_rng(1),'normal')
    b=readout(photo,np.random.default_rng(2),'hdr')
    assert a.std()>10 and b.std()<3
    high=np.full((300,300),15000.)
    a=readout(high,np.random.default_rng(3),'normal')
    b=readout(high,np.random.default_rng(3),'hdr')
    np.testing.assert_array_equal(a,b)
    assert abs(b.mean()-15000)<.2
    with pytest.raises(ValueError):readout(photo,np.random.default_rng(1),'bogus')


def test_fixture_native_same_seed_reproducible_and_no_frame_normalization():
    scene=np.full((128,128,3),.15)
    a,ia=capture(scene,11,'normal');again,_=capture(scene,11,'normal')
    b,ib=capture(scene,12,'hdr')
    np.testing.assert_array_equal(a,again)
    assert not np.array_equal(a,b) and np.abs(a.mean()-b.mean())<1
    assert ib.mean()>ia.mean()*1.3
    assert a.dtype==np.dtype('<u2') and b.max()<=4095
    assert isp_is_diagnostic({'source_kind':'synthetic','isp':{'comparison_mode':'diagnostic'}})
    assert not isp_is_diagnostic({'source_kind':'synthetic','isp':{}})


def fixture(mode):
    captures=[{'id':mode+str(i), 'raw_sha256':mode+'r'+str(i), 'isp_sha256':mode+'i'+str(i),
               'native_pixel_sha256':mode+'n'+str(i),'isp_pixel_sha256':mode+'p'+str(i)} for i in range(6)]
    colors=[{'capture_id':mode+str(i),'patch_id':'color-01','qc':{'fit_eligible':True},
             'after':{'delta_e00':1.+i},'actual_isp':{'delta_e00':None}} for i in range(3)]
    tone=[{'patch_id':p,'domain':'raw_green','nominal_reflectance':r,'observed':r,'residual':.01,
           'qc':{'fit_eligible':True}} for p,r in [('gray-01',.1),('gray-02',.2)] for _ in range(3)]
    return {'provenance':{'development_captures':captures[:3],'validation_captures':captures[3:]},
            'validation':{'color':{'patches':colors},'tone':{'patches':tone}},
            'reference':{'grays':[{'id':'gray-01','nominal_reflectance':.1},{'id':'gray-02','nominal_reflectance':.2}]},
            'noise_repeat_analysis':{'validation':{'noise':[{'patch_id':'gray-01','domain':'raw','channel':'Gr',
                'frame_count':3,'temporal_std':2.,'temporal_variance':4.,'mean_signal':100.,'snr_db':34.}]}},
            'combined_replay':{'comparisons':[]}}


def test_ab_missing_values_repeats_and_pixel_independence():
    a,b=fixture('A'),fixture('B');before=deepcopy(a)
    result=ab.assemble(a,b,{'rows':[]},{'rows':[]})
    assert result['independence']['status']=='verified'
    actual=result['color'][0]['metrics']['actual_isp_delta_e00']
    assert actual['a']['mean'] is None and actual['delta_b_minus_a'] is None
    assert actual['a']['unavailable_count']==3
    assert result['noise'][0]['repeat_frames_a']==3
    assert result['tone_diagnostics']['raw_green']['a']['nonmonotonic_steps']==0
    assert a==before
    b['provenance']['validation_captures'][0]['native_pixel_sha256']=a['provenance']['development_captures'][0]['native_pixel_sha256']
    with pytest.raises(ValueError,match='independent'):ab.assemble(a,b,{'rows':[]},{'rows':[]})


def test_mtf_uses_observed_units_coordinates_and_three_samples(tmp_path):
    a,b=fixture('A'),fixture('B')
    def native(value):
        return {'rows':[{'roi_id':'edge-1','capture_id':str(i),'domain':'raw','channel':'Gr',
            'box':[10,20,60,60],'mtf50':value+i*.01,'overshoot':0.,'undershoot':None} for i in range(3)]}
    result=ab.assemble(a,b,native(.1),native(.12))
    row=result['mtf'][0];assert row['unit']=='cycles/native_sensor_pixel'
    assert row['metrics']['mtf50']['a']['count']==3
    assert row['metrics']['mtf50']['delta_b_minus_a']==pytest.approx(.02)
    assert row['metrics']['undershoot']['a']['mean'] is None
    payload={'ab_comparison':result,'workflow_a_id':'A','workflow_b_id':'B','report_a_sha256':'aa',
             'report_b_sha256':'bb','source_kind':'synthetic','physical_validation':'not_established',
             'mode_a':{'conditions':{'mode_id':'normal'}},'mode_b':{'conditions':{'mode_id':'HDR'}}}
    ab.publish_tables(tmp_path,payload)
    assert len(list((tmp_path/'plots').glob('*.png')))==4
    assert 'cycles/native_sensor_pixel' in (tmp_path/'ab-mtf.csv').read_text(encoding='utf-8-sig')
    html=ab.html_tables(payload)
    assert '20 Gray' in html and '112개 edge' in html
    assert json.loads((tmp_path/'ab-tables.json').read_text())==result


def test_native_mtf_exact_owner_full_112_edges_and_independent_captures(tmp_path,monkeypatch):
    import gzip
    import hashlib
    from types import SimpleNamespace
    from camera_platform import calibration_lab
    path=tmp_path/'full-result.json.gz'
    metric={'mtf50':.12,'status':'ok','method_details':{'nyquist_cy_native_px':.25}}
    observations=[{'capture_id':'V'+str(i),'role':'validation','rois':[
        {'id':'edge-'+str(j),'box':[j,10,60,60],'kind':'edge',
         'raw':{c:deepcopy(metric) for c in ('R','Gr','Gb','B')},
         'isp':{c:deepcopy(metric) for c in ('R','G','B','Y')}} for j in range(112)]} for i in range(3)]
    with gzip.open(path,'wt') as stream:json.dump({'observations':observations,'validation':{'scope':'raw_native_sfr_only'}},stream)
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    result={'summary':{'files':{'esfr-images/full-result.json.gz':checksum}}}
    monkeypatch.setattr(calibration_lab,'get',lambda s,i:{'status':'succeeded','result':result})
    def binding(value,*,owner_kind,owner_id):
        assert owner_kind=='runs' and owner_id=='validation-run'
        if hashlib.sha256(path.read_bytes()).hexdigest()!=value:raise IntegrityError('Source SHA changed')
        return {'path':str(path)}
    service=SimpleNamespace(registry=SimpleNamespace(artifact_binding=binding,get=lambda *a:{'content_hash':'capture-hash','metadata':{}}))
    report={'mtf_evidence':{'validation_id':'validation-run','validation_result_sha256':digest(result)},
        'provenance':{'validation_captures':[{'id':'V'+str(i),'content_hash':'capture-hash'} for i in range(3)]}}
    value=ab.native_mtf(service,report)
    assert len(value['rows'])==2688 and value['edge_count']==112
    assert value['rows'][0]['unit']=='cycles/native_sensor_pixel'
    path.write_bytes(b'changed')
    with pytest.raises(IntegrityError,match='SHA'):ab.native_mtf(service,report)
