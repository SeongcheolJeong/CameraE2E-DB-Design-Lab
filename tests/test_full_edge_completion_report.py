"""Completion evidence binds explicit jobs and keeps numerical/physical claims apart."""

import base64
import hashlib
import json
from copy import deepcopy

import pytest

from camera_platform import calibration_lab as lab
from camera_platform.api import create_app
from camera_platform.calibration_completion_report import (
    CHARTS,
    SCHEMA,
    _input_evidence,
    build_completion_report,
    render_completion_report,
)
from camera_platform.registry import IntegrityError, digest
from camera_platform.worker import execute


@pytest.fixture(scope="module")
def completed(tmp_path_factory):
    root = tmp_path_factory.mktemp("completion-report")
    app = create_app(root / "state", workspace=root, start_worker=False, seed_demo=False)
    service = app.state.service
    fixture = lab.demo(service)
    study_id = fixture["study"]["id"]
    assert service.registry.claim()["id"] == study_id
    execute(service, study_id)
    study = lab.get(service, study_id)
    assert study["status"] == "succeeded", study.get("error")
    validation = lab.validate(
        service,
        study_id,
        {
            "measurement_ids": [
                m["id"] for m in fixture["measurements"] if m["role"] == "final_validation"
            ]
        },
    )
    assert service.registry.claim()["id"] == validation["id"]
    execute(service, validation["id"])
    validation = lab.get(service, validation["id"])
    assert validation["status"] == "succeeded", validation.get("error")
    profile = lab.apply(service, study_id, {"campaign_id": fixture["campaign"]["id"]})["profile"]
    return service, study, validation, profile


def build(completed):
    service, study, validation, profile = completed
    return build_completion_report(service, study["id"], validation["id"], profile["id"])


def test_completed_report_is_deterministic_json_and_keeps_physical_boundary(completed):
    report = build(completed)
    assert report == build(completed)
    assert json.loads(json.dumps(report, allow_nan=False)) == report
    assert report["schema"] == SCHEMA
    assert report["completion"]["physical_validation"] == "not_established"
    assert report["completion"]["required_chart_coverage_complete"] is True
    assert report["completion"]["original_image_count"] == 0  # Demo is authored tables.
    assert set(c["chart"] for c in report["charts"]) == set(CHARTS)
    assert next(c for c in report["charts"] if c["chart"] == "texture")["mode"] == "validation_only"
    assert report["final_profile"]["profile_sha256"] == digest(report["final_profile"]["profile"])
    assert report["final_profile"]["current_activation_state"] == "not_inferred"
    assert report["independent_validation"]["refit_performed"] is False
    assert report["hashes"]["outputs"]["study"]["file_count"] > 0
    assert report["hashes"]["inputs"]
    assert all(
        i["study_id"] in {report["study_id"], report["validation_id"]} and i["path"] and i["sha256"]
        for i in report["images"]
    )


def test_inactive_saved_candidate_is_not_presented_as_activated(completed):
    report = build(completed)
    report['final_profile']['selection_status'] = 'saved_without_activation'
    html = render_completion_report(report).decode()
    assert '후보를 비활성 상태로 저장했습니다' in html
    assert '기존 활성 프로파일을 바꾸지 않았습니다' in html
    assert '명시적 적용 기록이 보존되어 있습니다' not in html


def test_real_inactive_apply_can_publish_completion_without_activation(tmp_path):
    from camera_platform import calibration_completion
    service = create_app(tmp_path / 'inactive', workspace=tmp_path,
                         start_worker=False, seed_demo=False).state.service
    fixture = lab.demo(service)
    fit_id = fixture['study']['id']
    assert service.registry.claim()['id'] == fit_id
    execute(service, fit_id)
    val = lab.validate(service, fit_id, {'measurement_ids': [
        m['id'] for m in fixture['measurements'] if m['role'] == 'final_validation']})
    assert service.registry.claim()['id'] == val['id']
    execute(service, val['id'])
    before = service.registry.list(lab.ACTIVATIONS)
    applied = lab.apply(service, fit_id, {'campaign_id': fixture['campaign']['id'], 'activate': False})
    assert applied['activation'] is None
    assert service.registry.list(lab.ACTIVATIONS) == before
    record = calibration_completion.create(service, {'study_id': fit_id,
        'validation_id': val['id'], 'profile_id': applied['profile']['id']})
    report = build_completion_report(service, fit_id, val['id'], applied['profile']['id'])
    assert record['report_url'].endswith('/files/report.html')
    assert report['final_profile']['selection_status'] == 'saved_without_activation'
    assert report['completion']['physical_validation'] == 'not_established'


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("parent_study_id", "run-another", "another parent"),
        ("parent_result_sha256", "0" * 64, "another parent"),
        ("candidate_sha256", "0" * 64, "exact frozen"),
    ],
)
def test_parent_and_candidate_mismatch_rejected(completed, monkeypatch, field, value, expected):
    service, study, validation, profile = completed
    changed = deepcopy(validation)
    changed[field] = value
    original = lab.get
    monkeypatch.setattr(
        lab,
        "get",
        lambda s, identity: changed if identity == validation["id"] else original(s, identity),
    )
    with pytest.raises(IntegrityError, match=expected):
        build_completion_report(service, study["id"], validation["id"], profile["id"])


def test_saved_profile_from_other_study_rejected(completed, monkeypatch):
    service, _, _, profile = completed
    changed = deepcopy(profile)
    changed["source_study_id"] = "run-other-study"
    original = service.registry.get
    monkeypatch.setattr(
        service.registry,
        "get",
        lambda kind, identity: changed if kind == lab.PROFILES else original(kind, identity),
    )
    with pytest.raises(IntegrityError, match="Saved profile is not bound"):
        build(completed)


def test_reused_final_input_lineage_rejected(completed, monkeypatch):
    _, _, validation, _ = completed
    changed = deepcopy(validation)
    changed["development_lineage"].append(changed["validation_lineage"][0])
    original = lab.get
    monkeypatch.setattr(
        lab,
        "get",
        lambda s, identity: changed if identity == validation["id"] else original(s, identity),
    )
    with pytest.raises(IntegrityError, match="lineage differs"):
        build(completed)


def test_validation_refit_rejected(completed, monkeypatch):
    _, _, validation, _ = completed
    changed = deepcopy(validation)
    changed["result"]["summary"]["fit"]["status"] = "fitted_again"
    original = lab.get
    monkeypatch.setattr(
        lab,
        "get",
        lambda s, identity: changed if identity == validation["id"] else original(s, identity),
    )
    with pytest.raises(IntegrityError, match="must not refit"):
        build(completed)


@pytest.mark.parametrize(
    "verdict,label", [("fail", "수치 검증 실패"), ("inconclusive", "수치 검증 판단 보류")]
)
def test_failed_or_inconclusive_validation_never_rendered_as_success(
    completed, monkeypatch, verdict, label
):
    _, _, validation, _ = completed
    changed = deepcopy(validation)
    changed["result"]["summary"]["validation"].update(
        status=verdict, numerical_tolerance_pass=False
    )
    original = lab.get
    monkeypatch.setattr(
        lab,
        "get",
        lambda s, identity: changed if identity == validation["id"] else original(s, identity),
    )
    report = build(completed)
    assert report["completion"]["numerical_status"] == verdict
    assert report["completion"]["calibration_status"] != "completed_numerical_pass"
    rendered = render_completion_report(report).decode()
    assert label in rendered
    assert "수치 검증 합격" not in rendered
    assert "물리 검증 미확립" in rendered


def test_standalone_html_escapes_metadata_and_uses_static_inline_curves(completed):
    report = build(completed)
    report["name"] = '<script src="https://bad.test/">bad</script>'
    rendered = render_completion_report(report).decode()
    assert "<script" not in rendered
    assert "&lt;script src=&quot;" in rendered
    assert "<svg" in rendered
    assert "<details open><summary>독립 검증" in rendered
    assert "localhost" not in rendered and "127.0.0.1" not in rendered
    assert "Content-Security-Policy" in rendered
    assert 'src="http' not in rendered
    assert "상태: fit_and_independent_comparison" not in rendered
    assert "· baseline" not in rendered


def test_embedded_rendered_image_must_match_saved_hash(completed):
    report = build(completed)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMd8AAAAASUVORK5CYII="
    )
    report["images"] = [{**report["images"][0], "sha256": hashlib.sha256(png).hexdigest()}]
    rendered = render_completion_report(report, image_resolver=lambda _: (png, "image/png"))
    assert b"data:image/png;base64," in rendered
    with pytest.raises(IntegrityError, match="saved artifact hash"):
        render_completion_report(report, image_resolver=lambda _: (png + b"changed", "image/png"))
    with pytest.raises(ValueError, match="PNG/JPEG"):
        render_completion_report(report, image_resolver=lambda _: (b"<svg/>", "image/svg+xml"))


def test_original_raw_classification_requires_original_bytes_and_observer(completed):
    service, study, _, _ = completed
    record = deepcopy(study["measurement_snapshots"][0])
    assert _input_evidence(record)["original_image_available"] is False
    record["provenance"]["filename"] = "capture.npy"
    record["source_frame_hashes"] = ["a" * 64]
    record["quality"] = {"method": "native_bayer_dark_v1"}
    assert _input_evidence(record)["original_image_available"] is True
    record["provenance"]["source_sha256"] = "b" * 64
    with pytest.raises(IntegrityError, match="source bytes are absent"):
        _input_evidence(record)


def test_all_112_positions_and_variants_remain_in_standalone_report(completed):
    report = build(completed)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMd8AAAAASUVORK5CYII="
    )
    proto = report["images"][0]
    report["images"] = [
        {**proto, "chart": "mtf", "measurement_id": f"edge-{i:03d}",
         "role": role, "variant": variant, "sha256": hashlib.sha256(png).hexdigest()}
        for i in range(112)
        for role in ("study", "independent_validation")
        for variant in ("baseline", "candidate")
    ]
    rendered = render_completion_report(report, image_resolver=lambda _: (png, "image/png")).decode()
    assert rendered.count('data:image/png;base64,') == 448
    for i in range(112):
        assert rendered.count(f"edge-{i:03d}") == 8  # Every alt and caption.
    assert '이미지 미포함' not in rendered


def test_image_validation_links_exact_validation_and_preserves_fail(completed):
    report = build(completed)
    report["esfr_image_validation"] = {
        "report_url": f"/api/v1/calibration-lab/studies/{report['validation_id']}/files/esfr-images/report.html",
        "candidate_sha256": report["candidate_sha256"],
        "field_count": 112, "comparison_count": 336,
        "metrics": {"raw_sfr_rmse": {"before": .22, "after": .04, "count": 336},
                    "isp_sfr_rmse": {"before": .15, "after": .03, "count": 336}},
        "validation": {"status": "fail"}, "basis": "frozen_candidate_no_holdout_refit",
    }
    rendered = render_completion_report(report).decode()
    assert '고정 후보의 RAW·ISP 영상 비교' in rendered
    assert '0.04' in rendered and '336' in rendered
    assert 'data-image-validation-status="fail"' in rendered
    assert 'RAW·ISP 영상 검증 기준 미충족' in rendered
    report['esfr_image_validation']['report_url'] = '/other/report.html'
    with pytest.raises(IntegrityError, match='URL differs'):
        render_completion_report(report)


def test_dense_image_check_ledger_has_all_failures_and_compact_clear_summary():
    from pathlib import Path
    from camera_platform.calibration_completion_report import _esfr_image_verdict_html
    source = Path(__file__).parent / 'fixtures/previous_image_validation.json'
    result = json.loads(source.read_text())
    original = deepcopy(result['validation'])
    assert len(original['checks']) == 672
    image = {'validation': result['validation'], 'metrics': result['metrics']}
    rendered = _esfr_image_verdict_html(image)
    assert '전체 672개 검사 · 통과 669 · 실패 3' in rendered
    assert '최대 RMSE 후' in rendered and '위치별 허용 RMSE' in rendered
    assert rendered.count('star-corner-bottom-right-edge-2') == 3
    for row in original['checks']:
        if row['status'] == 'fail':
            assert row['capture_id'] in rendered
    assert 'star-corner-top-left-edge-1' not in rendered  # Pass ledger remains in JSON.
    assert len(rendered) < 8000
    assert image['validation'] == original


def test_image_verdict_never_truncates_many_failed_checks():
    from camera_platform.calibration_completion_report import _esfr_image_verdict_html
    rows = [{'roi_id': f'failed-edge-{i:03d}', 'capture_id': f'capture-{i:03d}',
             'metric': 'raw_sfr_rmse', 'status': 'fail', 'value': .04, 'threshold': .03}
            for i in range(112)]
    rendered = _esfr_image_verdict_html({'validation': {'status': 'fail', 'checks': rows}})
    assert '기준을 넘은 모든 검사 · 112건' in rendered
    for row in rows:
        assert row['roi_id'] in rendered
