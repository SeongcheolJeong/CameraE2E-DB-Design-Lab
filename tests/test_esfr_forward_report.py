from copy import deepcopy
from camera_platform.calibration_completion_report import (
    FORWARD_OBSERVER_METHOD, _forward_provenance_html, _history_report_url,
)


def evidence():
    forward = {'method': FORWARD_OBSERVER_METHOD, 'fit_data_role': 'development_only',
               'field_count': 112, 'source_study_id': 'source-1',
               'development_metrics': {'raw_sfr_rmse': {'before': .04, 'after': .005, 'count': 336, 'worst_after': .009}},
               'input_bindings': {'development_capture_ids': ['D1','D2','D3'],
                                  'training_measurement_ids': [f'T{i}' for i in range(112)],
                                  'roi_geometry_sha256': 'geometry-hash'}}
    image = {'candidate_sha256': 'candidate-hash', 'candidate_frozen_before_new_acquisition': True,
             'validation_acquisition_status': 'new_independent_synthetic_acquisition',
             'acquisition_contract': {'id': 'contract-1', 'created_at': '2026-10-18T12:00:00Z',
                                      'candidate_sha256': 'candidate-hash', 'new_validation_capture_ids': ['N1','N2','N3'],
                                      'thresholds': {'raw_sfr_rmse': .03, 'isp_sfr_rmse': .05}},
             'comparison_history': {'focus_roi_id': 'bottom-right-edge', 'thresholds_unchanged': True,
                                    'prior_report_url': '/api/v1/calibration-lab/completion-reports/prior-1/files/report.html',
                                    'rows': [
                                        {'dataset_role': role, 'fit_method': method, 'capture_id': capture,
                                         'roi_id': 'bottom-right-edge', 'raw_rmse': raw, 'isp_rmse': .01,
                                         'raw_threshold': .03, 'isp_threshold': .05, 'status': status}
                                        for role, method, capture, raw, status in [
                                            ('development','analytic_curve','D1',.04,'fail'),
                                            ('development','forward_observer','D1',.005,'pass'),
                                            ('previous_holdout','analytic_curve','OLD1',.041,'fail'),
                                            ('new_holdout','forward_observer','NEW1',.006,'pass'),
                                        ]
                                    ]}}
    return forward, image


def test_methods_roles_frozen_order_and_old_failure_remain_distinct():
    forward, image = evidence()
    before = deepcopy((forward, image))
    rendered = _forward_provenance_html(forward, image)
    for text in ('해석식 곡선 피팅', 'Forward 영상 관측 연산자', '개발 촬영 · 보정에 사용',
                 '이전 검증 촬영 · 이미 확인한 이력', '새 독립 검증 촬영', '서버 근거로 확인',
                 '기존 허용 기준 유지 확인', 'OLD1', '0.041', '0.03', '0.05', '112'):
        assert text in rendered
    assert '실패' in rendered and '합격' in rendered
    assert (forward, image) == before


def test_missing_or_mismatched_contract_never_claims_frozen_before_new_acquisition():
    forward, image = evidence()
    for key in ('candidate_frozen_before_new_acquisition', 'candidate_sha256', 'validation_acquisition_status'):
        altered = deepcopy(image);altered.pop(key)
        assert '서버 근거로 확인' not in _forward_provenance_html(forward, altered)
    image['acquisition_contract']['candidate_sha256'] = 'different-candidate'
    assert '서버 근거로 확인' not in _forward_provenance_html(forward, image)


def test_unknown_method_and_unsafe_history_url_do_not_invent_scope_or_link():
    forward, image = evidence()
    forward['method'] = '<unknown>'
    image['comparison_history']['prior_report_url'] = 'https://untrusted.example/report.html'
    rendered = _forward_provenance_html(forward, image)
    assert '&lt;unknown&gt;' in rendered
    assert 'native Bayer 생성' not in rendered
    assert 'https://untrusted.example' not in rendered
    assert _history_report_url('/api/v1/calibration-lab/studies/../files/report.html') is None


def test_every_history_row_is_visible_without_an_arbitrary_limit():
    forward, image = evidence()
    proto = image['comparison_history']['rows'][2]
    image['comparison_history']['rows'] = [{**proto, 'capture_id': f'old-{i:03d}'} for i in range(112)]
    rendered = _forward_provenance_html(forward, image)
    assert all(f'old-{i:03d}' in rendered for i in range(112))


def test_development_baseline_and_focused_operator_diagnostics_are_explicit():
    forward, image = evidence()
    forward['fields'] = [
        {'roi_id': 'unrelated', 'analytic_curve_rmse': {'before': .123456, 'after': .654321}},
        {'roi_id': 'bottom-right-edge', 'analytic_curve_rmse': {'before': .00456, 'after': .03727},
         'raw_sfr_rmse': {'before': .03549, 'after': .010},
         'isp_sfr_rmse': {'before': .01584, 'after': .0073}},
    ]
    rendered = _forward_provenance_html(forward, image)
    for text in ('기존 해석식 후보 평균 RMSE', 'Forward 후보 평균 RMSE',
                 '해석식 곡선 RMSE · 목적함수 아님', 'Native RAW Gr RMSE · 피팅 목적함수',
                 '고정 bilinear ISP RMSE · 별도 진단', '별도 진단으로 증가할 수 있습니다',
                 '0.00456', '0.03727', '0.03549', 'N1, N2, N3'):
        assert text in rendered
    assert '0.123456' not in rendered and '초기 평균 RMSE' not in rendered


def test_measured_chronology_is_operator_attested_and_isp_is_diagnostic():
    from camera_platform.calibration_completion_report import _esfr_image_verdict_html
    forward, image = evidence()
    image['validation_acquisition_status'] = 'new_independent_measured_acquisition'
    rendered = _forward_provenance_html(forward, image)
    assert '작업자 시각·확인서와 서버 등록 순서 확인' in rendered
    assert '서버 근거로 확인' not in rendered
    image['validation'] = {'status': 'pass', 'scope': 'raw_native_sfr_only',
        'thresholds': {'raw_sfr_rmse': .03, 'isp_sfr_rmse': .05},
        'checks': [{'metric': 'raw_sfr_rmse', 'status': 'pass', 'value': .01},
                   {'metric': 'isp_sfr_rmse', 'status': 'diagnostic', 'value': .2}]}
    html = _esfr_image_verdict_html(image)
    assert 'RAW 단독 기준' in html and 'ISP는 진단 전용' in html
    assert '합격 판정에 사용하지 않습니다' in html
    assert '실패 0' in html
