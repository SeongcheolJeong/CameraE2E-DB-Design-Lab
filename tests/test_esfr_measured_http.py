"""HTTP admission and wiring only; no generated data is called a physical result."""
from test_esfr_api import lab, acquisition

ROOT = '/api/v1/camera-validation/esfr/measured'


def test_unknown_camera_template_and_static_routes(lab):
    client, _, _ = lab
    response = client.get(ROOT + '/template')
    assert response.status_code == 200
    template = response.json()
    assert template['capture_metadata']['camera_id'] is None
    assert template['capture_metadata']['raw_format']['width'] is None
    assert template['capture_metadata']['isp']['comparison_mode'] == 'diagnostic'
    assert template['protocol']['development_capture_count'] == 3
    for path in ('esfr-measured.html', 'esfr-measured.js', 'esfr-measured.css', 'esfr-measured-guide.html'):
        assert client.get('/' + path).status_code == 200


def test_missing_data_cannot_create_a_workflow(lab):
    client, service, headers = lab
    response = client.post(ROOT + '/workflows', json={}, headers=headers)
    assert response.status_code == 422 and 'three' in response.json()['error']
    assert service.registry.list('camera_esfr_measured_workflows') == []


def test_synthetic_acquisitions_are_never_promoted_to_measured(lab):
    client, service, headers = lab
    rows = [acquisition(client, headers, serial=i)[0] for i in (1, 2, 3)]
    response = client.post(ROOT + '/workflows', json={'development_ids': [r['id'] for r in rows]}, headers=headers)
    assert response.status_code == 422 and 'measured development' in response.json()['error']
    assert service.registry.list('camera_esfr_measured_workflows') == []


def test_review_needs_explicit_attestation_and_session_token(lab):
    client, _, headers = lab
    assert client.post(ROOT + '/review-contract', json={}).status_code == 403
    response = client.post(ROOT + '/review-contract', json={}, headers=headers)
    assert response.status_code == 422 and 'acknowledgements' in response.json()['error']


def test_db_latest_ab_comparison_preserves_exact_source_tuple(lab,tmp_path):
    client,service,_=lab
    source={'run_id':'source-run','candidate_id':'candidate-03','condition_id':'condition-006'}
    query='?source_run=source-run&candidate=candidate-03&condition=condition-006'
    assert client.get(ROOT+'/iq-comparisons/latest'+query).json()=={'comparison':None}
    value={'schema':'camera_esfr_iq_comparison_v1','comparison_id':'comparison-source-test',
        'workflow_a_id':'normal-workflow','workflow_b_id':'hdr-workflow',
        'mode_a':{'source':source},'mode_b':{'source':source},
        'report_url':ROOT+'/iq-comparisons/comparison-source-test/files/report.html',
        'bundle_url':ROOT+'/iq-comparisons/comparison-source-test/files/bundle.zip',
        'source_kind':'synthetic','physical_validation':'not_established','comparison_revision':'chart_range_ab_v3'}
    path=tmp_path/'report.html';path.write_text('Explicit API link fixture; no numerical validation')
    artifact=service.registry.store_file(path,'report.html','text/html')
    value.update(files={'report.html':artifact['sha256']},artifacts=[artifact])
    service.registry.put('camera_esfr_iq_comparisons',value,identity='comparison-source-test')
    result=client.get(ROOT+'/iq-latest'+query).json()
    assert result['report'] is None
    assert result['comparison']['workflow_b_id']=='hdr-workflow'
    assert result['comparison']['mtf_status']['a']['fail_count'] is None
    assert client.get(value['report_url']).text=='Explicit API link fixture; no numerical validation'
    assert client.get(ROOT+'/iq-comparisons/latest'+query.replace('candidate-03','candidate-02')).json()=={'comparison':None}
