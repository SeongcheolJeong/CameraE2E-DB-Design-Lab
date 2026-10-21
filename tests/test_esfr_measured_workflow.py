"""Orchestration tests use explicit test doubles, never published physical data."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from camera_platform import esfr_measured_workflow as flow, esfr_measured, calibration_db, calibration_completion
from camera_platform.registry import ConflictError


class Registry:
    def __init__(self, directory):
        self.directory = directory
        self.records = {}
        self.jobs = {}
    def get(self, kind, identity):
        return deepcopy(self.records[kind, identity])
    def put_verified_record(self, kind, value, refs, identity):
        row = {**deepcopy(value), 'id':identity, 'content_hash':'test-input-sha'}
        self.records[kind,identity] = row
        return deepcopy(row)
    def list(self, kind):
        return [deepcopy(v) for (k,_),v in self.records.items() if k==kind]
    def list_runs(self, **kw):
        return []
    def get_run_state(self, identity, expected_request_hash=None):
        return {'id':identity,'status':'succeeded','content_hash':expected_request_hash}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    registry=Registry(tmp_path)
    service=SimpleNamespace(registry=registry,loaded_environment={'test':'source'})
    captures=[{'id':f'capture-{i}', 'raw_shape':[100,100], 'metadata':{'source_kind':'measured','role':'development' if i<3 else 'validation'},
               'files':{'raw':{'sha256':f'raw-{i}'}}} for i in range(6)]
    monkeypatch.setattr(flow.esfr_lab,'capture',lambda s,i:deepcopy(next(c for c in captures if c['id']==i)))
    monkeypatch.setattr(flow.esfr_lab,'_rois',lambda r,shape:r)
    monkeypatch.setattr(flow.esfr_lab,'_lineage',lambda c:{c['id']})
    monkeypatch.setattr(esfr_measured,'validate_measured_study',lambda *a,**kw:None)
    monkeypatch.setattr(esfr_measured,'roi_review_digest',lambda r:'roi-sha')
    monkeypatch.setattr(calibration_db,'source_selection',lambda s,p:({},p))
    monkeypatch.setattr(flow.lab,'_source_code',lambda:{'test':'source'})
    calls=[]
    def submit(s,p):
        key='measure-val' if p['validation_ids'] else 'measure-dev'
        calls.append((key,deepcopy(p)))
        row={'id':key,'content_hash':key+'-hash','status':'succeeded'}
        registry.jobs[key]=row
        return deepcopy(row)
    monkeypatch.setattr(flow.esfr_lab,'submit',submit)
    monkeypatch.setattr(flow.esfr_lab,'get',lambda s,i:deepcopy(registry.jobs[i]))
    export={'study_request':{'campaign_id':'campaign','measurement_ids':['training']},'campaign_id':'campaign','calibration_lab_url':'/calibration-lab.html?campaign=campaign'}
    monkeypatch.setattr(flow.esfr_bridge,'export',lambda *a,**k:deepcopy(export))
    def fit(s,p):
        calls.append(('fit',deepcopy(p)))
        row={'id':'fit','content_hash':'fit-hash','status':'succeeded'};registry.jobs['fit']=row;return deepcopy(row)
    monkeypatch.setattr(flow.lab,'submit',fit)
    contract={'id':'contract','content_hash':'contract-hash','candidate_sha256':'candidate-sha'}
    monkeypatch.setattr(flow.esfr_bridge,'create_validation_contract',lambda *a:deepcopy(contract))
    monkeypatch.setattr(flow.esfr_bridge,'verify_measured_validation_metadata',lambda *a:deepcopy(contract))
    monkeypatch.setattr(flow.esfr_bridge,'export_validation',lambda *a,**k:{'validation_ids':['holdout']})
    def validate(s,i,p):
        calls.append(('validate',deepcopy(p)))
        image={'validation':{'status':'pass','scope':'raw_native_sfr_only'},'metrics':{},'report_url':'/images/report.html'}
        row={'id':'validation','content_hash':'validation-hash','status':'succeeded','result':{'summary':{'esfr_image_validation':image}}}
        registry.jobs['validation']=row;return deepcopy(row)
    monkeypatch.setattr(flow.lab,'validate',validate)
    monkeypatch.setattr(flow.lab,'get',lambda s,i:deepcopy(registry.jobs[i]))
    def apply(s,i,p):
        calls.append(('apply',deepcopy(p)));return {'profile':{'id':'profile'},'activation':{} if p['activate'] else None}
    monkeypatch.setattr(flow.lab,'apply',apply)
    monkeypatch.setattr(calibration_completion,'create',lambda *a:{'id':'report','report_url':'/report.html','bundle_url':'/bundle'})
    payload={'source':{'run_id':'db-run','candidate_id':'candidate'},'development_ids':[c['id'] for c in captures[:3]],
             'rois':[{'id':'edge','field_id':'edge','kind':'edge','box':[0,0,20,20]}],
             'chart_profile':{'physical_chart_id':'chart'},'measured_review':{'review':'development'}}
    return service,payload,calls


def test_staged_flow_freezes_before_validation_and_preserves_thresholds(environment):
    service,payload,calls=environment
    row=flow.create(service,payload,launch=False)
    flow._run(service,row['id'])
    state=flow.get(service,row['id'])
    assert state['status']=='awaiting_validation' and state['contract']['candidate_sha256']=='candidate-sha'
    assert [x[0] for x in calls]==['measure-dev','fit']
    assert calls[0][1]['fit'] is False and calls[0][1]['validation_ids']==[]
    assert calls[0][1]['thresholds']=={'raw_sfr_rmse':.03,'isp_sfr_rmse':.05}
    request={'validation_ids':['capture-3','capture-4','capture-5'],'measured_review':{'review':'all-six'}}
    flow.start_validation(service,row['id'],request,launch=False)
    flow._run(service,row['id'])
    state=flow.get(service,row['id'])
    assert state['status']=='completed' and state['profile_activated'] is True
    assert [x[0] for x in calls].count('fit')==1
    assert [x[0] for x in calls].count('measure-dev')==1
    measured=next(p for key,p in calls if key=='measure-val')
    assert measured['fit'] is False
    assert measured['measured_review']=={**request['measured_review'], 'development_review':payload['measured_review']}
    assert state['validation']['image_validation']['scope']=='raw_native_sfr_only'
    assert 'receipts' not in state
    with pytest.raises(ConflictError):flow.start_validation(service,row['id'],request,launch=False)


def test_synthetic_and_missing_review_rejected_before_start(environment,monkeypatch):
    service,payload,calls=environment
    monkeypatch.setattr(flow.esfr_lab,'capture',lambda s,i:{'metadata':{'source_kind':'synthetic','role':'development'}})
    with pytest.raises(ValueError,match='measured development'):flow.create(service,payload,launch=False)
    assert not calls
    with pytest.raises(ValueError,match='acknowledgements'):flow.review_contract(service,{'capture_ids':['c']})


def test_source_drift_and_uncertain_mutation_preserve_failed_journal(environment):
    service,payload,calls=environment
    row=flow.create(service,payload,launch=False)
    _,state=flow._read(service,row['id']);state['pending_operation']='development_measurement'
    flow._write(service,row['id'],state)
    flow._run(service,row['id'])
    failed=flow.get(service,row['id'])
    assert failed['status']=='failed' and 'receipt reconciliation' in failed['error']
    assert not calls
    with pytest.raises(ConflictError):flow.resume(service,row['id'])


def test_validation_cannot_rewrite_development_attestation(environment):
    service,payload,calls=environment
    payload['measured_review']={'operator_id':'original-reviewer',
        'capture_reviews':[{'capture_id':'capture-0','raw_sha256':'frozen-raw'}]}
    row=flow.create(service,payload,launch=False);flow._run(service,row['id'])
    request={'validation_ids':['capture-3','capture-4','capture-5'],
        'measured_review':{'operator_id':'new-reviewer', 'capture_reviews':[
            {'capture_id':'capture-0','raw_sha256':'changed-raw'}]}}
    with pytest.raises(ValueError,match='preserved unchanged'):
        flow.start_validation(service,row['id'],request,launch=False)
    request['measured_review']['capture_reviews']=deepcopy(payload['measured_review']['capture_reviews'])
    request['measured_review']['development_review']={'operator_id':'rewritten'}
    with pytest.raises(ValueError,match='attribution'):
        flow.start_validation(service,row['id'],request,launch=False)


def test_failed_raw_gate_still_reports_without_activating(environment,monkeypatch):
    service,payload,calls=environment
    original=flow.lab.validate
    def fail(s,i,p):
        result=original(s,i,p)
        service.registry.jobs[result['id']]['result']['summary']['esfr_image_validation']['validation']['status']='fail'
        return result
    monkeypatch.setattr(flow.lab,'validate',fail)
    row=flow.create(service,payload,launch=False);flow._run(service,row['id'])
    flow.start_validation(service,row['id'],{'validation_ids':['capture-3','capture-4','capture-5'],'measured_review':{}},launch=False)
    flow._run(service,row['id'])
    state=flow.get(service,row['id'])
    assert state['status']=='completed' and state['report']['id']=='report'
    assert state['profile_activated'] is False
    assert next(p for key,p in calls if key=='apply')['activate'] is False
