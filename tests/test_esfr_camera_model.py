"""Numerical model selection must change prediction, never only provenance."""
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from camera_platform import esfr_camera_model as models, esfr_model_prediction as prediction
from camera_platform.esfr_analysis import edge_metrics
from camera_platform.registry import IntegrityError, digest


def edge():
    return {'normal_xy':[.9987523388778446,.04993761694389223], 'offset_px':50.,'crop_origin':[11,17],'box':[0,0,96,112]}


def levels():
    return {c:{'black_dn':64.,'white_dn':4095.,'low_dn':500.,'high_dn':3000.} for c in ('R','Gr','Gb','B')}


def test_selected_native_kernel_changes_raw_and_isp_prediction():
    delta=np.zeros((4,9,9));delta[:,4,4]=1
    y,x=np.indices((9,9));broad=np.exp(-((x-4)**2+(y-4)**2)/(2*1.5**2));broad/=broad.sum()
    a=prediction.simulate(delta,edge(),levels(),(96,112))
    b=prediction.simulate(np.stack([broad]*4),edge(),levels(),(96,112))
    assert not np.array_equal(a['raw'],b['raw'])
    assert not np.array_equal(a['isp'],b['isp'])
    ma=edge_metrics(a['raw_planes']['Gr'],pitch_native=2)
    mb=edge_metrics(b['raw_planes']['Gr'],pitch_native=2)
    assert ma['status']==mb['status']=='ok'
    assert np.interp(.15,mb['frequency_cy_native_px'],mb['sfr']) < np.interp(.15,ma['frequency_cy_native_px'],ma['sfr'])
    # Predicting cannot mutate or normalize the owner's kernels/plateaus.
    assert delta.sum()==4 and broad.sum()==pytest.approx(1)


def test_four_site_kernels_and_native_cfa_origin_are_used():
    kernels=np.zeros((4,7,7));kernels[:,3,3]=1
    kernels[1]=0;kernels[1,3,2:5]=[.25,.5,.25]
    raw=prediction.simulate(kernels,edge(),levels(),(96,112))['raw']
    assert raw.dtype==np.uint16 and raw.shape==(96,112)
    flat=np.zeros_like(kernels);flat[:,3,3]=1
    other=prediction.simulate(flat,edge(),levels(),(96,112))['raw']
    # Gr is global even row/odd col => odd local row/even local col.
    changed=raw!=other
    mask=np.zeros(changed.shape,bool);mask[1::2,0::2]=True
    assert changed.any() and not changed[~mask].any()


@pytest.mark.parametrize('bad',[np.zeros((4,3,3)),np.ones((4,4,4)),np.full((4,3,3),np.nan),-np.ones((4,3,3))])
def test_invalid_kernels_never_become_zero_error(bad):
    with pytest.raises(ValueError):prediction.simulate(bad,edge(),levels(),(96,112))


def test_device_identity_is_explicit_and_is_not_inferred_from_sensor():
    value={'camera_id':'physical-unit','manufacturer':'maker','model':'camera','serial':'unit-1','lens':'lens','sensor':'sensor','mode':'mode'}
    assert models.identity_spec(value,'physical-unit')==value
    with pytest.raises(ValueError,match='camera_id'):models.identity_spec(value,'another')
    for key in value:
        bad=deepcopy(value);bad[key]=''
        with pytest.raises(ValueError):models.identity_spec(bad,'physical-unit')


def test_spatial_assumptions_cannot_silently_fabricate_peripheral_psfs():
    good={'mode':'uniform_selected_db_field','source_field_id':'center','acknowledged':True}
    assert models.spatial_spec(good,['center'])==good
    for bad in [{**good,'acknowledged':False},{**good,'source_field_id':'corner'}, {'mode':'nearest_sensor','acknowledged':True}, {'mode':'explicit_roi_assignment','roi_fields':{'one':'center'},'acknowledged':True}]:
        with pytest.raises(ValueError):models.spatial_spec(bad,['center'])


def test_every_context_value_is_frozen_and_source_substitution_is_rejected(monkeypatch):
    meta={'camera_id':'camera','source_kind':'synthetic','raw_format':{'bit_depth':12},'geometry':{'crop_origin':[0,0]},'conditions':{'exposure_s':.01},'pixel_pitch_um':2.9,'isp':{'transfer':'linear'}}
    model={'input_contract':models.contract(meta),'source':{'run_id':'one','candidate_id':'candidate-00'}}
    monkeypatch.setattr(models,'get',lambda *a:deepcopy(model));monkeypatch.setattr(models,'kernels',lambda *a:{})
    assert models.check_captures(None,'id',[{'metadata':meta}],model['source'])==model
    for key,value in [('camera_id','other'),('pixel_pitch_um',1.4),('conditions',{'exposure_s':.02}),('isp',{'transfer':'srgb'}),('geometry',{'crop_origin':[1,1]})]:
        wrong={**deepcopy(meta),key:value}
        with pytest.raises(ValueError,match='differ'):models.check_captures(None,'id',[{'metadata':wrong}],model['source'])
    with pytest.raises(ValueError,match='another DB'):models.check_captures(None,'id',[{'metadata':meta}],{'run_id':'two','candidate_id':'candidate-00'})


def test_record_hash_tampering_rejected():
    row={'id':'esfr-camera-'+'a'*32,'value':3};row['content_hash']=digest(row)
    registry=SimpleNamespace(get=lambda *a:deepcopy(row));service=SimpleNamespace(registry=registry)
    assert models.get(service,row['id'])==row
    row['value']=4
    with pytest.raises(IntegrityError):models.get(service,row['id'])


def test_unavailable_statistics_remain_null():
    assert prediction._stats([{'x':None}],'x')=={'mean':None,'max':None,'available':0,'unavailable':1}


def test_camera_model_artifacts_have_an_exact_typed_owner(tmp_path):
    import io
    from camera_platform.registry import SQLiteRegistry
    registry=SQLiteRegistry(tmp_path/'registry')
    service=SimpleNamespace(registry=registry)
    stream=io.BytesIO();kernel=np.zeros((4,3,3));kernel[:,1,1]=1
    np.savez_compressed(stream,center_rgba_sites=kernel)
    artifact=registry.store_blob(stream.getvalue(),'model/broadband-kernels.npz','application/octet-stream')
    document={'schema':'camera_esfr_camera_model_v1','files':{'model/broadband-kernels.npz':{'sha256':artifact['sha256'],'bytes':artifact['bytes']}},
        'artifacts':[artifact],'spatial':{'mode':'uniform_selected_db_field','source_field_id':'center','acknowledged':True}}
    row=registry.put_verified_record(models.KIND,document,[],identity='esfr-camera-'+'d'*32)
    assert np.array_equal(models.kernels(service,row)['center'],kernel)
    with pytest.raises((IntegrityError,KeyError)):
        registry.artifact_binding(artifact['sha256'],owner_kind=models.KIND,owner_id='esfr-camera-'+'e'*32)
    malformed=deepcopy(document);malformed['files']['model/broadband-kernels.npz']['sha256']='f'*64
    with pytest.raises(IntegrityError):registry.put_verified_record(models.KIND,malformed,[])


def test_new_http_workflows_require_model_and_legacy_sources_remain_explicit(tmp_path):
    from camera_platform.api import create_app
    from fastapi.testclient import TestClient
    app=create_app(state_dir=tmp_path/'state',workspace=tmp_path,start_worker=False,seed_demo=False)
    with TestClient(app) as client:
        token={'X-Platform-Token':client.get('/api/v1/session').json()['token']}
        assert client.get(models.ROOT).json()=={'models':[]}
        assert client.get(models.ROOT+'/sources').json()=={'sources':[]}
        response=client.post('/api/v1/camera-validation/esfr/measured/model-workflows',json={},headers=token)
        assert response.status_code==422 and 'model' in response.text
        response=client.get(prediction.ROOT+'/latest?source_run=run-test&candidate=candidate-00')
        assert response.json()=={'comparison':None}


def test_report_only_compatibility_rejects_any_numerical_or_dependency_change(monkeypatch):
    from camera_platform import calibration_lab as lab
    old={'packages':{'numpy':'same'},'source_hashes':{'camera_platform/esfr_model_prediction.py':prediction._REPORT_PREDECESSOR,
        'camera_platform/esfr_measured_workflow.py':'old','camera_platform/esfr_engine.py':'observer-same'}}
    current=deepcopy(old);current['source_hashes'].update({'camera_platform/esfr_model_prediction.py':'new','camera_platform/esfr_measured_workflow.py':'new'})
    service=SimpleNamespace(loaded_environment=current)
    monkeypatch.setattr(lab,'_source_code',lambda:deepcopy(current))
    proof=prediction.report_compatibility(service,{'source_code':old})
    assert proof['validation_refit_performed'] is False
    assert proof['all_other_sources_and_dependencies_identical']
    bad=deepcopy(old);bad['source_hashes']['camera_platform/esfr_engine.py']='different'
    with pytest.raises(IntegrityError):prediction.report_compatibility(service,{'source_code':bad})
    bad=deepcopy(old);bad['packages']['numpy']='different'
    with pytest.raises(IntegrityError):prediction.report_compatibility(service,{'source_code':bad})
    bad=deepcopy(old);bad['source_hashes']['camera_platform/esfr_model_prediction.py']='unrecognized-adapter'
    with pytest.raises(IntegrityError):prediction.report_compatibility(service,{'source_code':bad})
