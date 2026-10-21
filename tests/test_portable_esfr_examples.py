import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from install_esfr_examples import install
from esfr_demo_source import source_selection, bind_server, latest_query
from build_esfr_example_pack import portable_report
from camera_platform import example_gallery


def package(tmp_path, *, duplicate=False, symlink=False):
    identity = 'synthetic-example'
    content = b'<html><body>historical synthetic result</body></html>'
    name = identity + '/index.html'
    archive = tmp_path / 'pack.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        if symlink:
            member.type = tarfile.SYMTYPE
            member.linkname = '/tmp/outside'
            member.size = 0
        tar.addfile(member, None if symlink else io.BytesIO(content))
        if duplicate:
            tar.addfile(member, io.BytesIO(content))
    manifest = {'schema': example_gallery.SCHEMA, 'id': identity,
                'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'example': {'title': 'synthetic historical'},
                'files': {name: {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}}}
    (tmp_path / 'EXAMPLE_PACK_MANIFEST.json').write_text(json.dumps(manifest))
    return archive, manifest


def test_full_install_checks_data_and_does_not_register_live_workflow(tmp_path):
    archive, value = package(tmp_path)
    result = install(archive, tmp_path)
    assert result['live_state_imported'] is False
    entry = example_gallery.catalog(tmp_path)['examples'][0]
    assert entry['installed'] and entry['source_kind'] == 'synthetic'
    assert entry['live_workflow_registered'] is False
    assert not (tmp_path / 'registry.sqlite').exists()
    response = example_gallery.file_response(tmp_path, value['id'], 'index.html')
    assert response.headers['X-Example-Scope'] == 'historical-synthetic-read-only'
    assert "script-src 'none'" in response.headers['content-security-policy']
    original = (tmp_path / 'examples-data' / value['id'] / 'index.html').read_bytes()
    with pytest.raises(ValueError, match='already exists'):
        install(archive, tmp_path)
    assert (tmp_path / 'examples-data' / value['id'] / 'index.html').read_bytes() == original


@pytest.mark.parametrize('kind', ['duplicate', 'symlink', 'wrong_sha', 'traversal'])
def test_unsafe_or_changed_archive_is_rejected_before_install(tmp_path, kind):
    archive, value = package(tmp_path, duplicate=kind == 'duplicate', symlink=kind == 'symlink')
    if kind == 'wrong_sha':
        archive.write_bytes(b'changed')
    if kind == 'traversal':
        value['files'] = {'synthetic-example/../../outside': {'sha256': '0' * 64, 'bytes': 0}}
        (tmp_path / 'EXAMPLE_PACK_MANIFEST.json').write_text(json.dumps(value))
    with pytest.raises(ValueError):
        install(archive, tmp_path)
    assert not (tmp_path / 'examples-data/synthetic-example').exists()


@pytest.mark.parametrize('relative', ['../index.html', '/index.html', 'a\\b', 'secret.json'])
def test_gallery_rejects_files_outside_exact_manifest(tmp_path, relative):
    archive, value = package(tmp_path)
    install(archive, tmp_path)
    with pytest.raises(HTTPException) as exc:
        example_gallery.file_response(tmp_path, value['id'], relative)
    assert exc.value.status_code == 404


def test_gallery_detects_tampering_and_parent_symlink(tmp_path):
    archive, value = package(tmp_path)
    install(archive, tmp_path)
    target = tmp_path / 'examples-data' / value['id'] / 'index.html'
    target.write_text('tampered')
    with pytest.raises(HTTPException) as exc:
        example_gallery.file_response(tmp_path, value['id'], 'index.html')
    assert exc.value.status_code == 409
    base = tmp_path / 'examples-data'
    base.rename(tmp_path / 'retained')
    base.symlink_to(tmp_path / 'retained', target_is_directory=True)
    with pytest.raises(HTTPException) as exc:
        example_gallery.file_response(tmp_path, value['id'], 'index.html')
    assert exc.value.status_code == 409


def test_http_routes_show_historical_example_and_optional_condition(tmp_path, monkeypatch):
    from camera_platform.api import create_app
    archive, value = package(tmp_path)
    install(archive, tmp_path)
    from camera_platform import esfr_iq_service
    seen = []
    def latest(service, source):
        seen.append(source);return None
    monkeypatch.setattr(esfr_iq_service, 'latest', latest)
    monkeypatch.setattr(esfr_iq_service, 'latest_comparison', latest)
    with TestClient(create_app(tmp_path / 'state', tmp_path, seed_demo=False, start_worker=False)) as client:
        assert client.get('/examples/').status_code == 200
        assert client.get('/api/v1/examples').json()['examples'][0]['installed']
        assert client.get('/examples/' + value['id'] + '/index.html').status_code == 200
        assert client.get('/api/v1/camera-validation/esfr/measured/iq-latest', params={
            'source_run': 'run-' + 'a' * 32, 'candidate': 'candidate-01'}).status_code == 200
        assert client.get('/api/v1/camera-validation/esfr/measured/iq-comparisons/latest', params={
            'source_run': 'run-' + 'a' * 32, 'candidate': 'candidate-01'}).status_code == 200
        assert all('condition_id' not in source for source in seen)


def args(**kw):
    return SimpleNamespace(source_json=None, source_run=None, candidate=None, condition=None, **kw)


def api_fixture():
    calls = []
    def request(path, payload=None):
        calls.append((path, payload))
        if payload is not None:
            assert path == '/api/v1/db-design/studies'
            assert payload['lens_keys'] == ['reference']
        return {'id': 'run-' + 'b' * 32, 'plugin_id': 'camera_db_design_study_v1',
                'status': 'succeeded', 'result': {'summary': {'comparison_mode': 'lens_sensor',
                'candidates': [{'id': 'candidate-01', 'status': 'succeeded'}]}}}
    return request, calls


def test_fresh_source_created_once_and_resumed_using_http_only(tmp_path):
    request, calls = api_fixture()
    selected = source_selection(request, args(), tmp_path)
    assert selected == {'run_id': 'run-' + 'b' * 32, 'candidate_id': 'candidate-01'}
    assert 'condition=' not in latest_query(selected)
    assert source_selection(request, args(), tmp_path) == selected
    assert sum(payload is not None for _, payload in calls) == 1
    assert not (tmp_path / 'registry.sqlite').exists()


def test_explicit_candidate_and_server_identity_cannot_be_silently_replaced(tmp_path):
    request, calls = api_fixture()
    source_selection(request, args(), tmp_path)
    a = args(); a.source_run = 'run-' + 'c' * 32; a.candidate = 'candidate-01'
    with pytest.raises(ValueError, match='different DB source'):
        source_selection(request, a, tmp_path)
    health = {'state_dir': '/arbitrary/server/state', 'workspace': '/workspace', 'source_code': 'v1'}
    bind_server(tmp_path, health)
    with pytest.raises(ValueError, match='another server'):
        bind_server(tmp_path, {**health, 'state_dir': '/new/server/state'})


def test_nonexistent_source_candidate_rejected_before_any_upload(tmp_path):
    request, calls = api_fixture()
    a = args(); a.source_run = 'run-' + 'b' * 32; a.candidate = 'candidate-99'
    with pytest.raises(ValueError, match='does not exist'):
        source_selection(request, a, tmp_path)
    assert all(payload is None for _, payload in calls)
    assert not (tmp_path / 'source.json').exists()


def test_historical_links_rewritten_without_changing_numbers(tmp_path):
    folder = tmp_path / 'normal'; folder.mkdir()
    path = folder / 'report.html'
    path.write_text('<html><body><p>RMSE=0.0387102 failure</p><a href="/old/run">old</a><a href="/native">MTF</a></body></html>')
    row = portable_report(path, tmp_path, {'/native': 'normal-mtf/report.html'})
    text = path.read_text()
    assert 'RMSE=0.0387102 failure' in text
    assert 'data-original-href="/old/run"' in text
    assert 'href="../normal-mtf/report.html"' in text
    assert row['original_sha256'] != row['portable_sha256']
