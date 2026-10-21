"""Ordering and admission tests; numerical evidence is tested separately."""
from copy import deepcopy
import pytest
from test_esfr_measured_workflow import environment
from camera_platform import esfr_measured_workflow as flow, esfr_iq_service as iq


def test_ccm_freezes_before_acquisition_contract_and_is_never_refitted(environment, monkeypatch):
    service, payload, calls = environment
    events = []
    monkeypatch.setattr(iq, 'normalize_iq', lambda *args: {'enabled': True})
    def freeze(s, record, identity, fitted):
        events.append('ccm_frozen')
        assert not any(event == 'contract' for event in events)
        return {'id':'frozen-iq', 'profile_sha256':'fixed-iq-hash'}
    monkeypatch.setattr(iq, 'freeze', freeze)
    old_contract = flow.esfr_bridge.create_validation_contract
    def contract(*args):
        events.append('contract');return old_contract(*args)
    monkeypatch.setattr(flow.esfr_bridge, 'create_validation_contract', contract)
    monkeypatch.setattr(iq, 'validation_request', lambda *args: {'captured_after_freeze':True})
    def validate(s, record, state):
        assert state['iq_frozen']['profile_sha256'] == 'fixed-iq-hash'
        assert state['validation']['id'] == 'validation'
        events.append('iq_validation')
        return {'report_url':'/iq/report.html','bundle_url':'/iq/bundle.zip','db_active_model_updated':False}
    monkeypatch.setattr(iq, 'validate', validate)
    row=flow.create(service,payload,launch=False);flow._run(service,row['id'])
    assert events == ['ccm_frozen','contract']
    flow.start_validation(service,row['id'],{'validation_ids':['capture-3','capture-4','capture-5'],'measured_review':{}},launch=False)
    flow._run(service,row['id'])
    state=flow.get(service,row['id'])
    assert state['status']=='completed'
    assert events == ['ccm_frozen','contract','iq_validation']
    assert state['links']['iq_report']=='/iq/report.html'
    assert state['iq_result']['db_active_model_updated'] is False
    assert state['thresholds']=={'raw_sfr_rmse':.03,'isp_sfr_rmse':.05}


def test_failed_iq_admission_does_not_start_or_modify_validation(environment, monkeypatch):
    service,payload,calls=environment
    monkeypatch.setattr(iq,'normalize_iq',lambda *args:{'enabled':True})
    monkeypatch.setattr(iq,'freeze',lambda *args:{'id':'frozen-iq'})
    row=flow.create(service,payload,launch=False);flow._run(service,row['id'])
    before=deepcopy(flow.get(service,row['id']))
    def reject(*args):raise ValueError('Missing per-capture patch review')
    monkeypatch.setattr(iq,'validation_request',reject)
    with pytest.raises(ValueError,match='patch review'):
        flow.start_validation(service,row['id'],{'validation_ids':['capture-3','capture-4','capture-5'],'measured_review':{}},launch=False)
    assert flow.get(service,row['id'])==before
    assert [key for key,_ in calls]==['measure-dev','fit']


def test_combined_replay_refuses_nonfrozen_or_incomplete_mtf():
    from camera_platform.esfr_iq_replay import execute
    with pytest.raises(ValueError,match='completed frozen'):
        execute(None,{'status':'running'},None,None)
    with pytest.raises(ValueError,match='112-edge'):
        execute(None,{'status':'succeeded','result':{'summary':{'esfr_image_validation':{'field_count':111,'refit_performed':False}}}},None,None)


@pytest.mark.parametrize("iq_fails", [False, True], ids=["success", "failure"])
def test_completed_is_never_published_before_iq_result_and_links(environment, monkeypatch, iq_fails):
    """GET after every durable write covers both once() publication windows."""
    service, payload, calls = environment
    monkeypatch.setattr(iq, "normalize_iq", lambda *args: {"enabled": True})
    monkeypatch.setattr(iq, "freeze", lambda *args: {"id": "frozen-iq"})
    monkeypatch.setattr(iq, "validation_request", lambda *args: {})
    writes, during_iq = [], []
    original_write = flow._write

    def observe_write(s, identity, state):
        original_write(s, identity, state)
        _, persisted = flow._read(s, identity)
        writes.append({"public": deepcopy(flow.get(s, identity)),
            "pending": persisted.get("pending_operation"),
            "iq_receipt_saved": "iq_report" in persisted["receipts"]})

    monkeypatch.setattr(flow, "_write", observe_write)

    def publish_iq(s, record, state):
        during_iq.append(deepcopy(flow.get(s, record["id"])))
        if iq_fails:
            raise RuntimeError("IQ publication failed in test")
        return {"report_url": "/iq/report.html", "bundle_url": "/iq/bundle.zip"}

    monkeypatch.setattr(iq, "validate", publish_iq)
    row = flow.create(service, payload, launch=False)
    flow._run(service, row["id"])
    flow.start_validation(service, row["id"], {
        "validation_ids": ["capture-3", "capture-4", "capture-5"],
        "measured_review": {},
    }, launch=False)
    flow._run(service, row["id"])
    final = flow.get(service, row["id"])

    assert len(during_iq) == 1
    assert during_iq[0]["status"] == "running"
    assert during_iq[0]["stage"] == "iq_report"
    assert "iq_result" not in during_iq[0]
    assert any(w["pending"] == "iq_report" for w in writes)
    completed = [w["public"] for w in writes if w["public"]["status"] == "completed"]
    for snapshot in completed:
        assert snapshot["iq_result"]["report_url"] == snapshot["links"]["iq_report"]
        assert snapshot["iq_result"]["bundle_url"] == snapshot["links"]["iq_bundle"]
    assert [key for key, _ in calls].count("fit") == 1
    if iq_fails:
        assert final["status"] == "failed"
        assert final["error"] == "IQ publication failed in test"
        assert not completed
    else:
        receipt_window = [w["public"] for w in writes
            if w["iq_receipt_saved"] and "iq_result" not in w["public"]]
        assert receipt_window, "Exercise the receipt write before result assignment"
        assert all(w["status"] == "running" for w in receipt_window)
        assert len(completed) == 1
        assert final["status"] == "completed" and final["stage"] == "report_ready"
        assert final["links"]["iq_report"] == "/iq/report.html"
        assert final["links"]["iq_bundle"] == "/iq/bundle.zip"
