"""Real synthetic RAW patch arithmetic + immutable registry service boundaries.

MTF jobs and acquisition-contract source verification are explicit doubles here;
no test represents these generated rasters as measured camera captures.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sys
import zipfile

import pytest

from camera_platform import esfr_iq_service as service_iq, esfr_iq_engine as engine
from camera_platform import calibration_lab as lab, esfr_lab, esfr_bridge
from camera_platform.registry import SQLiteRegistry, IntegrityError, ConflictError, digest, canonical
from test_esfr_iq_engine import fixture as pixel_fixture


def import_pixels(service, tmp_path, role, *, contract=None, count=3, offset=0):
    sources, rois = pixel_fixture(tmp_path, role, count, offset=offset)
    result = []
    for source in sources:
        meta = source["record"]["metadata"]
        if contract:
            meta["validation_contract"] = {"id": contract["id"], "content_hash": contract["content_hash"]}
            meta["generated_at"] = datetime.now(timezone.utc).isoformat()
        imported = esfr_lab.import_capture(service, meta, source["raw_path"], source["isp_path"])
        result.append(esfr_lab.capture(service, imported["id"]))
    return result, rois


def context(tmp_path, monkeypatch, *, extra_count=0):
    service = SimpleNamespace(registry=SQLiteRegistry(tmp_path / "registry"), loaded_environment={"test": "iq-source"})
    monkeypatch.setattr(lab, "_source_code", lambda: service.loaded_environment)
    dev, rois = import_pixels(service, tmp_path, "development", count=3 + extra_count)
    iq = service_iq.normalize_iq(service, {"enabled": True, "reference_id": service_iq.REFERENCE_ID,
        "actual_isp_color_space": "unknown", "patch_roi_reviewed_capture_ids": [c["id"] for c in dev],
        "noise_repeat_ids": [c["id"] for c in dev[3:]]}, dev[:3], rois)
    source = {"run_id": "run-source", "candidate_id": "candidate-03", "condition_id": "condition-006"}
    record = service.registry.put(service_iq.WORKFLOWS, {"schema": "camera_esfr_measured_workflow_v1",
        "source": source, "source_context": {"source": source, "immutable": "db-source-hash"},
        "source_code": service.loaded_environment, "source_kind": "synthetic", "development_ids": [c["id"] for c in dev[:3]],
        "rois": rois, "chart_profile": {"id": "nominal-software-fixture-chart"}, "iq": iq})
    candidate = {"schema": "unit-MTF-candidate", "blur": {"sigma_px": 1.5}}
    fit = {"id": "fit", "status": "succeeded", "measurement_snapshots": [{"provenance": {"esfr_run_id": "source"}}],
        "result": {"summary": {"candidate_profile": candidate, "candidate_sha256": digest(candidate)}}}
    source_row = {"id": "source", "development_ids": record["development_ids"], "validation_ids": [],
                  "specification": {"source": source, "rois": rois}}
    jobs = {"fit": fit}
    monkeypatch.setattr(lab, "get", lambda s, i: deepcopy(jobs[i]))
    monkeypatch.setattr(esfr_lab, "get", lambda s, i: deepcopy(source_row))
    frozen = service_iq.freeze(service, record, record["id"], fit)
    contract = service.registry.put(esfr_bridge.VALIDATION_CONTRACTS, {
        "schema": "camera_esfr_frozen_holdout_contract_v1", "parent_study_id": "fit",
        "source": source, "candidate_sha256": digest(candidate), "source_kind": "synthetic"})
    monkeypatch.setattr(esfr_bridge, "_verify_validation_contract", lambda s, i, parent: (
        s.registry.get(esfr_bridge.VALIDATION_CONTRACTS, i), deepcopy(jobs[parent])))
    val, _ = import_pixels(service, tmp_path, "validation", contract=contract, count=3 + extra_count, offset=300)
    state = {"iq_frozen": frozen, "contract": contract, "validation_ids": [c["id"] for c in val[:3]], "receipts": {}}
    payload = {"iq_patch_roi_reviewed_capture_ids": [c["id"] for c in val], "iq_noise_repeat_ids": [c["id"] for c in val[3:]]}
    state["iq_validation"] = service_iq.validation_request(service, record, state, payload)
    profile = service.registry.put(lab.PROFILES, {"profile": candidate, "profile_sha256": digest(candidate)})
    jobs["validation"] = {"id": "validation", "status": "succeeded", "parent_study_id": "fit",
        "candidate_sha256": digest(candidate), "result": {"summary": {"unit_stub": True}}}
    state.update(profile_id=profile["id"], validation={"id": "validation", "image_validation": {"status": "pass", "scope": "unit_stub_not_numerical_MTF"}},
        report={"report_url": "/unit-mtf-report"}, links={"image_report": "/unit-mtf-images"})
    return service, record, state, jobs, dev, val, payload


def stub_report_modules(monkeypatch):
    # These legacy controller fixtures do not create a second numerical MTF run.
    from camera_platform import esfr_iq_ab
    monkeypatch.setattr(esfr_iq_ab, 'native_mtf', lambda *a: {'status':'explicit_test_double','rows':[]})
    monkeypatch.setattr(esfr_iq_ab, 'independence', lambda *a: {'status':'explicit_test_double_not_independent_evidence'})
    # Service tests verify artifact ownership, manifest, no-refit and arithmetic;
    # renderer and combined MTF engine have their own independent test suites.
    def publish(directory, payload, images=None):
        (directory / "report.html").write_text("<html>Explicit service test double</html>")
        (directory / "report.json").write_text(canonical(payload))
        (directory / "patches.csv").write_text("domain,scope\nIQ,synthetic\n")
    renderer = SimpleNamespace(publish=publish, render_chart_previews=lambda *a: [],
                               publish_comparison=lambda directory, payload: publish(directory, payload))
    replay = SimpleNamespace(execute=lambda *a: {"status": "explicit_test_double", "numerical_MTF_verified": False})
    monkeypatch.setitem(sys.modules, "camera_platform.esfr_iq_report", renderer)
    monkeypatch.setitem(sys.modules, "camera_platform.esfr_iq_replay", replay)
    import camera_platform
    monkeypatch.setattr(camera_platform, "esfr_iq_report", renderer, raising=False)
    monkeypatch.setattr(camera_platform, "esfr_iq_replay", replay, raising=False)


def test_real_synthetic_pixels_frozen_once_report_bundle_and_no_activation(tmp_path, monkeypatch):
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch)
    frozen = state["iq_frozen"]
    assert frozen["created_at"] <= state["contract"]["created_at"] < min(c["created_at"] for c in val)
    assert frozen["source_kind"] == "synthetic" and frozen["db_activation"] is False
    assert frozen["profile"]["ccm"]["fitted_observations"] == 54
    before = [c["files"]["raw"]["sha256"] for c in val]
    monkeypatch.setattr(engine, "fit_profile", lambda *a: pytest.fail("Fresh validation must not refit"))
    assert service_iq.freeze(service, record, record["id"], jobs["fit"]) == frozen
    stub_report_modules(monkeypatch)
    report = service_iq.validate(service, record, state)
    assert service_iq.validate(service, record, state) == report
    assert report["source"] == record["source"] and report["source_kind"] == "synthetic"
    assert report["validation"]["refit_performed"] is False
    assert report["validation"]["color"]["summary"]["after"]["linear_rgb_rmse"]["mean"] < .001
    assert report["provenance"]["same_raw_before_after"] is True
    assert report["provenance"]["validation_fit_performed"] is False
    assert report["validation"]["raw_sha256"] == before
    assert report["db_active_model_updated"] is False
    assert service.registry.list(lab.ACTIVATIONS) == []
    assert report["noise_repeat_analysis"]["validation"]["fit_performed"] is False
    assert all(row["frame_count"] == 3 for row in report["noise_repeat_analysis"]["validation"]["noise"])
    assert report["composite_candidate"]["reference_isp"]["profile_sha256"] == frozen["profile_sha256"]
    assert report["composite_candidate"]["qe_updated"] is False
    response = service_iq.file_response(service, record["id"], "bundle.zip")
    with zipfile.ZipFile(response.path) as archive:
        manifest = json.loads(archive.read("bundle-manifest.json"))
        assert "composite-candidate.json" in archive.namelist()
        for row in manifest["files"]:
            assert hashlib.sha256(archive.read(row["path"])).hexdigest() == row["sha256"]
    assert service_iq.latest(service, record["source"])["workflow_id"] == record["id"]
    assert service_iq.latest(service, {**record["source"], "condition_id": "condition-007"}) is None
    with pytest.raises(KeyError):
        service_iq.file_response(service, record["id"], "../../state.json")


def test_one_extra_repeat_is_reviewed_frozen_and_never_fitted(tmp_path, monkeypatch):
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch, extra_count=1)
    assert len(record["iq"]["noise_repeat_ids"]) == 1 and record["iq"]["noise_frame_count"] == 4
    assert len(state["iq_frozen"]["development_capture_evidence"]) == 4
    assert state["iq_frozen"]["profile"]["ccm"]["fitted_observations"] == 54
    with pytest.raises(ValueError, match="count"):
        service_iq.validation_request(service, record, state, {**payload, "iq_noise_repeat_ids": []})
    with pytest.raises(ValueError, match="Review"):
        service_iq.validation_request(service, record, state, {**payload, "iq_patch_roi_reviewed_capture_ids": state["validation_ids"]})
    stub_report_modules(monkeypatch)
    report = service_iq.validate(service, record, state)
    assert all(row["frame_count"] == 4 for row in report["noise_repeat_analysis"]["validation"]["noise"])
    assert len(report["validation"]["color"]["patches"]) == 54


@pytest.mark.parametrize("problem", ["profile", "contract_before_freeze", "source", "fresh_time", "capture_hash", "review", "repeat_core_overlap"])
def test_candidate_and_fresh_acquisition_guards_fail_closed(tmp_path, monkeypatch, problem):
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch)
    if problem == "profile":
        state["iq_frozen"]["profile"]["white_balance"][0] += .1
    elif problem == "contract_before_freeze":
        contract = deepcopy(state["contract"])
        contract["created_at"] = "2020-01-01T00:00:00+00:00"
        state["contract"] = contract
        monkeypatch.setattr(esfr_bridge, "_verify_validation_contract", lambda *a: (contract, jobs["fit"]))
    elif problem == "source":
        state["contract"] = {**state["contract"], "source": {"run_id": "other"}}
    elif problem == "fresh_time":
        old = deepcopy(val[0]); old["metadata"]["generated_at"] = "2020-01-01T00:00:00+00:00"
        current = esfr_lab.capture
        monkeypatch.setattr(esfr_lab, "capture", lambda s, i: old if i == old["id"] else current(s, i))
    elif problem == "capture_hash":
        row = val[0]
        binding = service.registry.artifact_binding(row["files"]["raw"]["sha256"], owner_kind=esfr_lab.CAPTURES, owner_id=row["id"])
        path = Path(binding["path"])
        with path.open("r+b") as out:
            out.write(b"XX")
        stub_report_modules(monkeypatch)
        with pytest.raises(IntegrityError):
            service_iq.validate(service, record, state)
        return
    elif problem == "review":
        payload["iq_patch_roi_reviewed_capture_ids"] = state["validation_ids"][:2]
    else:
        payload["iq_noise_repeat_ids"] = state["validation_ids"][:1]
    with pytest.raises((ValueError, IntegrityError, ConflictError)):
        service_iq.validation_request(service, record, state, payload)


def test_normalization_disables_backcompat_and_rejects_mislabelled_patch_protocol(tmp_path, monkeypatch):
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch)
    assert service_iq.normalize_iq(service, None, [], []) == {"enabled": False}
    iq = {"enabled": True, "reference_id": service_iq.REFERENCE_ID, "actual_isp_color_space": "unknown",
          "patch_roi_reviewed_capture_ids": [c["id"] for c in dev], "noise_repeat_ids": []}
    with pytest.raises(ValueError, match="18 color"):
        service_iq.normalize_iq(service, iq, dev, record["rois"][:-1])
    with pytest.raises(ValueError, match="EXTRA"):
        service_iq.normalize_iq(service, {**iq, "noise_repeat_ids": [dev[0]["id"]]}, dev, record["rois"])
    with pytest.raises(ValueError, match="reference"):
        service_iq.normalize_iq(service, {**iq, "reference_id": "inferred-QE"}, dev, record["rois"])


def test_no_cross_candidate_repeat_exposure(tmp_path, monkeypatch):
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch, extra_count=1)
    service.registry.submit({"job_kind": esfr_lab.KIND, "development_ids": [], "validation_ids": [val[-1]["id"]]}, "other-candidate")
    with pytest.raises(ConflictError, match="already exposed"):
        service_iq.validation_request(service, record, state, payload)


def test_noise_admission_only_exempts_exact_fresh_companions(tmp_path, monkeypatch):
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch, extra_count=1)
    request = state["iq_validation"]
    assert service_iq.validation_request(service, record, state, payload) == request
    assert len(service.registry.list(service_iq.ADMISSIONS)) == 1
    assert service_iq.mtf_noise_companions(service, request["admission_id"], state["contract"], state["validation_ids"]) == {val[-1]["id"]}
    with pytest.raises(IntegrityError, match="exact MTF"):
        service_iq.mtf_noise_companions(service, request["admission_id"], state["contract"], list(reversed(state["validation_ids"])))
    with pytest.raises(IntegrityError, match="another acquisition"):
        service_iq.mtf_noise_companions(service, request["admission_id"], {**state["contract"], "content_hash": "different"}, state["validation_ids"])
    service.registry.submit({"job_kind": esfr_lab.KIND, "development_ids": record["development_ids"],
        "validation_ids": state["validation_ids"], "specification": {"iq_validation_admission_id": request["admission_id"]}}, "same-frozen-candidate")
    assert service_iq.mtf_noise_companions(service, request["admission_id"], state["contract"], state["validation_ids"]) == {val[-1]["id"]}
    service.registry.submit({"job_kind": esfr_lab.KIND, "development_ids": [], "validation_ids": [val[-1]["id"]]}, "different-candidate")
    with pytest.raises(ConflictError, match="different chart"):
        service_iq.mtf_noise_companions(service, request["admission_id"], state["contract"], state["validation_ids"])


def test_real_renderer_service_manifest_and_native_chart_previews(tmp_path, monkeypatch):
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch)
    from camera_platform import esfr_iq_replay
    monkeypatch.setattr(esfr_iq_replay, "execute", lambda *a: {"status": "test_double_not_numerical_MTF"})
    result = service_iq.validate(service, record, state)
    html = Path(service_iq.file_response(service, record["id"], "report.html").path).read_text()
    assert "synthetic" in html or "합성" in html
    assert len([p for p in result["files"] if p.endswith(".png")]) == 12
    assert {"report.html", "report.json", "composite-candidate.json", "bundle.zip"} <= set(result["files"])
    assert result["validation"]["status"] == "diagnostic_no_acceptance_thresholds"


def test_hdr_comparison_is_chart_range_only_and_rejects_reference_mismatch(tmp_path, monkeypatch):
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch)
    stub_report_modules(monkeypatch)
    a = service_iq.validate(service, record, state)
    # Report mutation is a new immutable comparison fixture; the original stays
    # unchanged and this test does not claim a second numerical workflow.
    bdoc = {k: deepcopy(v) for k, v in a.items() if k not in {"id", "content_hash", "created_at"}}
    bdoc["workflow_id"] = "explicit-second-report-fixture"
    bdoc["composite_candidate"]["fixed_conditions"]["conditions"]["mode_id"] = "fixture-hdr"
    b = service.registry.put(service_iq.REPORTS, bdoc)
    comparison = service_iq.compare(service, {"workflow_a_id": a["workflow_id"], "workflow_b_id": b["workflow_id"]})
    assert comparison["camera_dynamic_range_measured"] is False
    assert comparison["scope"] == "same_chart_range_mode_comparison_only"
    assert comparison["condition_differences"]["mode_id"]["b"] == "fixture-hdr"
    assert comparison["physical_validation"] == "not_established"
    assert service_iq.comparison_file_response(service, comparison["id"], "bundle.zip").path
    bad = deepcopy(bdoc)
    bad["workflow_id"] = "wrong-reference-fixture"
    bad["reference"]["id"] = "unrelated-print-reference"
    service.registry.put(service_iq.REPORTS, bad)
    with pytest.raises(ValueError, match="same source kind and nominal reference"):
        service_iq.compare(service, {"workflow_a_id": a["workflow_id"], "workflow_b_id": bad["workflow_id"]})


def test_hdr_mtf_evidence_is_retained_and_prior_comparison_is_not_reused(tmp_path, monkeypatch):
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch)
    stub_report_modules(monkeypatch)
    a=service_iq.validate(service,record,state)
    bdoc={k:deepcopy(v) for k,v in a.items() if k not in {'id','content_hash','created_at'}}
    bdoc['workflow_id']='hdr-mtf-second-fixture'
    bdoc['composite_candidate']['fixed_conditions']['conditions']['mode_id']='fixture-HDR'
    bdoc['mtf_evidence']={'validation_id':'mtf-second','validation_result_sha256':'b'*64,
        'report_url':'/api/v1/calibration-lab/studies/mtf-second/files/esfr-images/report.html'}
    bdoc['combined_replay']={'edge_count':112,'comparison_count':336,
        'comparisons':[{'capture_id':'V2','roi_id':'edge-a','raw_after_rmse':None,'isp_after_rmse':.04}],
        'raw_gate_unchanged':{'status':'not_evaluated'}}
    b=service.registry.put(service_iq.REPORTS,bdoc)
    old_id='esfr-iq-comparison-'+digest({'a':a['content_hash'],'b':b['content_hash']})[:32]
    legacy=service.registry.put(service_iq.COMPARISONS,{'schema':'camera_esfr_iq_comparison_v1',
        'historical_report':'without MTF','files':deepcopy(a['files']),
        'artifacts':deepcopy(a['artifacts'])},identity=old_id)
    comparison=service_iq.compare(service,{'workflow_a_id':a['workflow_id'],'workflow_b_id':b['workflow_id']})
    assert comparison['id']!=old_id
    assert comparison['comparison_revision']=='chart_range_ab_v3'
    assert comparison['judgement']=='pending_criteria'
    for key,original in [('mode_a',a),('mode_b',b)]:
        assert comparison[key]['mtf_evidence']==original['mtf_evidence']
        assert comparison[key]['combined_replay']==original['combined_replay']
    assert comparison['mode_b']['combined_replay']['comparisons'][0]['raw_after_rmse'] is None
    assert service.registry.get(service_iq.COMPARISONS,old_id)==legacy
    response=service_iq.comparison_file_response(service,comparison['id'],'report.json')
    saved=json.loads(Path(response.path).read_text())
    assert saved['mode_b']['combined_replay']==b['combined_replay']
    assert service_iq.compare(service,{'workflow_a_id':a['workflow_id'],'workflow_b_id':b['workflow_id']})==comparison


def test_hdr_old_report_missing_mtf_remains_missing(tmp_path, monkeypatch):
    service, record, state, jobs, dev, val, payload=context(tmp_path,monkeypatch)
    stub_report_modules(monkeypatch)
    a=service_iq.validate(service,record,state)
    bdoc={k:deepcopy(v) for k,v in a.items() if k not in {'id','content_hash','created_at','mtf_evidence','combined_replay'}}
    bdoc['workflow_id']='hdr-missing-mtf-fixture'
    b=service.registry.put(service_iq.REPORTS,bdoc)
    result=service_iq.compare(service,{'workflow_a_id':a['workflow_id'],'workflow_b_id':b['workflow_id']})
    assert result['mode_b']['mtf_evidence']=={}
    assert result['mode_b']['combined_replay']=={}


def test_real_renderer_patch_csvs_are_owned_and_covered_by_zip_manifest(tmp_path, monkeypatch):
    import csv
    from io import StringIO
    service,record,state,jobs,dev,val,payload=context(tmp_path,monkeypatch,extra_count=1)
    from camera_platform import esfr_iq_replay
    monkeypatch.setattr(esfr_iq_replay,'execute',lambda *a:{'status':'test_double_not_numerical_MTF'})
    result=service_iq.validate(service,record,state)
    required={'color-patches.csv','gray-tone.csv','noise-patches.csv','provenance.csv'}
    assert required<=set(result['files'])
    with zipfile.ZipFile(service_iq.file_response(service,record['id'],'bundle.zip').path) as archive:
        manifest={r['path']:r for r in json.loads(archive.read('bundle-manifest.json'))['files']}
        assert required<=set(manifest)
        for name in required:
            data=archive.read(name)
            assert hashlib.sha256(data).hexdigest()==manifest[name]['sha256']==result['files'][name]
            assert len(data)==manifest[name]['bytes']
            rows=list(csv.DictReader(StringIO(data.decode('utf-8-sig'))))
            assert rows
        colors=list(csv.DictReader(StringIO(archive.read('color-patches.csv').decode('utf-8-sig'))))
        assert len(colors)==432
        assert all(r['delta_e00']==r['linear_rgb_rmse']=='' for r in colors if r['stage']=='actual_isp')
        assert all(r['profile_document_sha256']==state['iq_frozen']['profile_sha256'] for r in colors)
        assert all(r['reference_document_sha256']==state['iq_frozen']['reference_sha256'] for r in colors)
        assert {r['independent_of_training'] for r in colors if r['role']=='validation'}=={'true'}

        noise=list(csv.DictReader(StringIO(archive.read('noise-patches.csv').decode('utf-8-sig'))))
        for row in noise:
            if row['statistic_kind']=='temporal_repeat':
                assert row['frame_count']=='4'
                assert row['observation_sha256']==result['noise_repeat_analysis'][row['role']]['observation_sha256']
                assert row['observation_sha256']!=result[row['role']]['observation_sha256']
            assert row['variance_unit']==('DN^2' if row['unit']=='DN' else 'decoded_relative_output^2')


def test_ab_curve_snapshot_preserves_exact_owner_reference_without_inline_duplication():
    report={'id':'source-iq','content_hash':'source-report-hash',
        'report_url':'/api/v1/source-iq/files/report.html',
        'files':{'combined-replay/result.json':'a'*64},
        'combined_replay':{'comparison_count':336,'comparisons':[],
            'curves':{'edge-1':{'frequencies':[0.,.25],'sfr':[1.,.2]}}}}
    before=deepcopy(report)
    snapshot=service_iq._comparison_replay_snapshot(report)
    assert 'curves' not in snapshot
    assert snapshot['retained_curves']['curves_payload_sha256']==digest(report['combined_replay']['curves'])
    assert snapshot['retained_curves']['owner_id']=='source-iq'
    assert snapshot['retained_curves']['source_file_sha256']=='a'*64
    assert report==before
    report['files']={}
    with pytest.raises(IntegrityError,match='source artifact'):
        service_iq._comparison_replay_snapshot(report)
