"""One request-local run decode retains every existing artifact integrity gate."""

import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from camera_platform import reference_workshop_job as workshop
from camera_platform.api import create_app
from camera_platform.registry import IntegrityError, SQLiteRegistry, canonical, digest


def completed(registry, *, request=None, payload=b'original result bytes'):
    entry = registry.store_blob(payload, 'summary.json', 'application/json')
    request = {"kind": "fixture"} if request is None else request
    run = registry.submit(request, digest(request), force=True)
    assert registry.claim()['id'] == run['id']
    return registry.finish(run['id'], result={"artifacts": [entry]}), entry


def test_combined_read_decodes_once_and_matches_separate_public_reads(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path)
    run, entry = completed(registry)
    expected_run = registry.get_run(run['id'])
    expected_binding = registry.artifact_binding(entry['id'], owner_kind='runs', owner_id=run['id'])
    calls, original = [], registry._decode_job

    def counted(*args, **kwargs):
        calls.append(args[0]['id'])
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, '_decode_job', counted)
    for _ in range(2):
        calls.clear()
        record, binding = registry.get_run_with_result_binding(run['id'])
        assert calls == [run['id']]
        assert record == expected_run
        assert binding == expected_binding
        record['result']['artifacts'][0]['name'] = 'caller changed result'
        binding['descriptor']['name'] = 'caller changed binding'


@pytest.mark.parametrize('part', ['request', 'result', 'audit', 'head', 'index', 'blob', 'symlink'])
def test_combined_read_rejects_fresh_same_size_and_binding_tampering(tmp_path, part):
    registry = SQLiteRegistry(tmp_path / 'state')
    run, entry = completed(registry)
    registry.get_run_with_result_binding(run['id'])
    with registry.connection() as conn:
        if part in {'request', 'result'}:
            conn.execute('DROP TRIGGER immutable_completed_job')
            column = 'document' if part == 'request' else 'result'
            conn.execute('UPDATE jobs SET ' + column + "='{}' WHERE id=?", (run['id'],))
        elif part == 'audit':
            conn.execute('DROP TRIGGER immutable_audit_update')
            conn.execute("UPDATE audit SET details='{}' WHERE action='runs.succeeded'")
        elif part == 'head':
            conn.execute('UPDATE artifact_heads SET binding_hash=?', ('f' * 64,))
        elif part == 'index':
            conn.execute('UPDATE artifact_owners SET descriptor_hash=?', ('f' * 64,))
            forged = dict(conn.execute('SELECT * FROM artifact_owners').fetchone())
            conn.execute('UPDATE artifact_heads SET binding_hash=?', (digest(forged),))
        elif part == 'blob':
            (registry.blobs / entry['id']).write_bytes(b'x' * entry['bytes'])
        else:
            original = tmp_path / 'external.bin'
            original.write_bytes(b'original result bytes')
            target = registry.blobs / entry['id']
            target.unlink()
            target.symlink_to(original)
    with pytest.raises(IntegrityError):
        registry.get_run_with_result_binding(run['id'])


@pytest.mark.parametrize('change', ['wrong_owner', 'missing_owner', 'ambiguous_owner'])
def test_combined_read_requires_unique_exact_owner(tmp_path, change):
    registry = SQLiteRegistry(tmp_path)
    run, entry = completed(registry)
    with registry.connection() as conn:
        if change == 'wrong_owner':
            conn.execute("UPDATE artifact_owners SET owner_id='another-run'")
            forged = dict(conn.execute('SELECT * FROM artifact_owners').fetchone())
            conn.execute('UPDATE artifact_heads SET binding_hash=?', (digest(forged),))
        elif change == 'missing_owner':
            conn.execute('DELETE FROM artifact_owners')
        else:
            row = dict(conn.execute('SELECT * FROM artifact_owners').fetchone())
            row['descriptor_order'] = 99
            row['descriptor_path'] = '["result","artifacts",99]'
            conn.execute('INSERT INTO artifact_owners VALUES(?,?,?,?,?,?,?,?)',
                         tuple(row[key] for key in registry.ARTIFACT_COLUMNS))
    with pytest.raises((IntegrityError, KeyError)):
        registry.get_run_with_result_binding(run['id'])


@pytest.mark.parametrize('status', ['queued', 'running', 'failed', 'cancelled'])
def test_unfinished_or_failed_run_has_no_result_binding(tmp_path, status):
    registry = SQLiteRegistry(tmp_path)
    run = registry.submit({'kind': 'fixture'}, 'pending')
    if status in {'running', 'failed'}:
        registry.claim()
    if status == 'failed':
        registry.finish(run['id'], error='expected failure')
    elif status == 'cancelled':
        registry.cancel(run['id'])
    record, binding = registry.get_run_with_result_binding(run['id'])
    assert record == registry.get_run(run['id'])
    assert record['status'] == status
    assert binding is None


def test_job_and_completion_proof_share_one_read_transaction(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path)
    run, _ = completed(registry)
    observed, original = [], registry._decode_job

    def capture(row, completion_events=None):
        observed.append(completion_events)
        assert completion_events is not None
        assert completion_events[run['id']]['subject'] == row['id']
        assert completion_events[run['id']]['action'] == 'runs.succeeded'
        return original(row, completion_events)

    monkeypatch.setattr(registry, '_decode_job', capture)
    registry.get_run_with_result_binding(run['id'])
    assert len(observed) == 1


@pytest.fixture
def workshop_run(tmp_path, monkeypatch):
    storage = tmp_path / 'ssd'
    monkeypatch.setenv('CAMERA_PLATFORM_WORKSHOP_ROOT', str(storage))
    registry = SQLiteRegistry(tmp_path / 'state')
    request = {'job_kind': workshop.JOB_KIND, 'storage_root': str(storage), 'name': 'bound files'}
    run = registry.submit(request, 'workshop-file')
    registry.claim()
    directory = storage / 'reference-workshop' / run['id']
    directory.mkdir(parents=True)
    data = b'actual native artifact'
    (directory / 'raw.bin').write_bytes(data)
    manifest = {'schema': 'camera_reference_workshop_files_v1',
                'files': {'raw.bin': hashlib.sha256(data).hexdigest()}}
    (directory / 'manifest.json').write_text(canonical(manifest))
    summary = {'manifest_sha256': hashlib.sha256((directory / 'manifest.json').read_bytes()).hexdigest()}
    entry = registry.store_blob(canonical(summary).encode(), 'summary.json', 'application/json')
    registry.finish(run['id'], result={'summary': summary, 'artifacts': [entry]})
    return registry, run['id'], directory, data


def test_workshop_get_uses_one_decode_and_retains_manifest_and_summary_checks(workshop_run, monkeypatch):
    registry, identity, directory, _ = workshop_run
    service = SimpleNamespace(registry=registry)
    calls, decode = [], registry._decode_job

    def counted(*args, **kwargs):
        calls.append(True)
        return decode(*args, **kwargs)

    monkeypatch.setattr(registry, '_decode_job', counted)
    assert workshop.get(service, identity)['status'] == 'succeeded'
    assert len(calls) == 1
    (directory / 'manifest.json').write_text('{}')
    with pytest.raises(IntegrityError, match='manifest changed'):
        workshop.get(service, identity)


def test_workshop_file_still_checks_native_bytes_each_request(workshop_run):
    registry, identity, directory, data = workshop_run
    client = TestClient(create_app(state_dir=registry.directory, start_worker=False, seed_demo=False))
    url = f'{workshop.ROUTE}/runs/{identity}/files/raw.bin'
    response = client.get(url)
    assert response.status_code == 200 and response.content == data
    (directory / 'raw.bin').write_bytes(b'x' * len(data))
    response = client.get(url)
    assert response.status_code == 409
    assert 'artifact changed' in response.text.lower()


@pytest.mark.parametrize('status', ['queued', 'running', 'failed', 'cancelled'])
def test_workshop_incomplete_file_routes_remain_not_found(tmp_path, monkeypatch, status):
    storage = tmp_path / 'ssd'
    monkeypatch.setenv('CAMERA_PLATFORM_WORKSHOP_ROOT', str(storage))
    registry = SQLiteRegistry(tmp_path / 'state')
    request = {'job_kind': workshop.JOB_KIND, 'storage_root': str(storage)}
    run = registry.submit(request, 'not-complete')
    if status in {'running', 'failed'}:
        registry.claim()
    if status == 'failed':
        registry.finish(run['id'], error='expected')
    elif status == 'cancelled':
        registry.cancel(run['id'])
    client = TestClient(create_app(state_dir=registry.directory, start_worker=False, seed_demo=False))
    response = client.get(f"{workshop.ROUTE}/runs/{run['id']}/files/raw.bin")
    assert response.status_code == 404
