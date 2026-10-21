"""Atomic fitted-calibration publication does not escape job completion evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from camera_platform.registry import IntegrityError, SQLiteRegistry, digest


@pytest.fixture
def publication(tmp_path):
    registry = SQLiteRegistry(tmp_path / 'state')
    parent = registry.put('readout_calibrations', {'name': 'declared parent'})
    queued = registry.submit({'job_kind': 'readout_fit', 'name': 'bounded fit'}, 'fit')
    registry.claim()
    artifact = registry.store_blob(b'original result', 'fit-diagnostics.json', 'application/json')
    job = registry.finish(queued['id'], result={'artifacts': [artifact], 'candidate': {'gain': 1}})
    document = {'name': 'fitted', 'parent_id': parent['id'], 'artifacts': [artifact],
                'fit_job_id': job['id'], 'fit_result_sha256': digest(job['result'])}
    return registry, parent, job, document


def publish(values, identity='readout_calibration-one'):
    registry, parent, job, document = values
    return registry.publish_readout_calibration(
        document, [('readout_calibrations', parent)], job, identity=identity,
    )


def test_concurrent_retries_create_one_revision_and_audit(publication):
    registry, _, _, _ = publication
    before = len(registry.audit()["events"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(publication), range(2)))
    assert results[0] == results[1] == publish(publication)
    assert len(registry.audit()["events"]) == before + 1
    assert len(registry.list('readout_calibrations')) == 2


def test_no_publication_from_failed_or_cancelled_jobs(publication):
    registry, parent, _, document = publication
    for kind in ('failed', 'cancelled', 'queued'):
        job = registry.submit({'job_kind': 'readout_fit', 'name': kind}, kind)
        if kind == 'failed':
            registry.claim()
            job = registry.finish(job['id'], error='numerical failure')
        elif kind == 'cancelled':
            job = registry.cancel(job['id'])
        with pytest.raises(ValueError, match='succeeded'):
            registry.publish_readout_calibration(
                document, [('readout_calibrations', parent)], job,
                identity='readout_calibration-' + kind,
            )
    assert len(registry.list('readout_calibrations')) == 1


def test_changed_job_or_parent_snapshot_cannot_publish(publication):
    registry, parent, job, document = publication
    altered = deepcopy(job)
    altered['result']['candidate']['gain'] = 2
    with pytest.raises(IntegrityError, match='job or result changed'):
        registry.publish_readout_calibration(
            document, [('readout_calibrations', parent)], altered,
            identity='readout_calibration-altered-job',
        )
    altered = {**parent, 'name': 'substituted'}
    with pytest.raises(IntegrityError, match='input changed'):
        registry.publish_readout_calibration(
            document, [('readout_calibrations', altered)], job,
            identity='readout_calibration-altered-parent',
        )
    assert len(registry.list('readout_calibrations')) == 1


def test_record_index_audit_rollback_together(publication, monkeypatch):
    registry, _, _, _ = publication
    before = registry.audit()
    insert = registry._insert_staged_record

    def fail_after_insertion(conn, staged, **kwargs):
        insert(conn, staged, **kwargs)
        raise RuntimeError('injected publication failure')

    monkeypatch.setattr(registry, '_insert_staged_record', fail_after_insertion)
    with pytest.raises(RuntimeError, match='injected'):
        publish(publication)
    assert registry.audit() == before
    assert len(registry.list('readout_calibrations')) == 1


def test_ancestor_job_snapshots_and_completion_are_required(publication):
    registry, parent, job, document = publication
    ancestor = registry.submit({'job_kind': 'readout_fit', 'name': 'ancestor'}, 'ancestor')
    registry.claim()
    ancestor = registry.finish(ancestor['id'], result={'candidate': 'base', 'artifacts': []})
    altered = deepcopy(ancestor)
    altered['result']['candidate'] = 'substituted'
    with pytest.raises(IntegrityError, match='job or result changed'):
        registry.publish_readout_calibration(
            document, [('readout_calibrations', parent)], job,
            identity='readout_calibration-descendant', ancestor_jobs=[altered],
        )
    saved = registry.publish_readout_calibration(
        document, [('readout_calibrations', parent)], job,
        identity='readout_calibration-descendant', ancestor_jobs=[ancestor],
    )
    assert saved['fit_job_id'] == job['id']
    with registry.connection() as conn:
        assert conn.execute("SELECT count(*) FROM artifact_owners WHERE owner_id=?",
                            ('readout_calibration-one',)).fetchone()[0] == 0
