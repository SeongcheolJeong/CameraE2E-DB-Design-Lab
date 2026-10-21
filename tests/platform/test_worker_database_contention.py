"""Real SQLite writer contention must not lose claims or repeat completed physics."""

from __future__ import annotations

import os
import signal
import sqlite3
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from camera_platform import worker
from camera_platform.registry import digest
from camera_platform.service import PlatformService


class HeldWrite:
    """Own the blocking connection in its thread and always release it on exit."""

    def __init__(self, path):
        self.path = path
        self.entered = threading.Event()
        self.release = threading.Event()
        self.errors = []
        self.thread = threading.Thread(target=self._hold, name="sqlite-write-holder")

    def _hold(self):
        try:
            with sqlite3.connect(self.path, timeout=0.1, isolation_level=None) as conn:
                conn.execute("BEGIN IMMEDIATE")
                self.entered.set()
                if not self.release.wait(5):
                    raise AssertionError("Blocking transaction was not released by its fixture")
                conn.rollback()
        except BaseException as error:
            self.errors.append(error)
            self.entered.set()

    def start(self):
        self.thread.start()
        assert self.entered.wait(2), "Blocking transaction did not initialize"
        if self.errors:
            raise self.errors[0]

    def close(self):
        self.release.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)
            assert not self.thread.is_alive()
        if self.errors:
            raise self.errors[0]


@pytest.fixture
def service(tmp_path, monkeypatch):
    service = PlatformService(tmp_path / "state")
    service.registry.busy_timeout_ms = 173
    monkeypatch.setattr(worker, "DATABASE_BUSY_TIMEOUT_MS", 20)
    monkeypatch.setattr(worker, "DATABASE_RETRY_TIMEOUT_S", 1.5)
    monkeypatch.setattr(worker, "DATABASE_RETRY_INITIAL_S", 0.01)
    monkeypatch.setattr(worker, "DATABASE_RETRY_MAX_S", 0.03)
    return service


def _queued(service):
    document = {"request": {"seed": 618}, "purpose": "real SQLite contention"}
    return service.registry.submit(document, digest(document))


def _busy_release_thread(busy, held):
    errors = []

    def release():
        try:
            assert busy.wait(2), "No actual SQLITE_BUSY was observed"
            held.release.set()
        except BaseException as error:
            errors.append(error)
            held.release.set()

    thread = threading.Thread(target=release, name="busy-release-observer")
    thread.start()
    return thread, errors


def _watch_busy(monkeypatch, registry, method, busy, calls):
    original = getattr(registry, method)

    def observed(*args, **kwargs):
        calls.append(method)
        with registry.connection() as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 20
        try:
            return original(*args, **kwargs)
        except sqlite3.OperationalError as error:
            assert error.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
            busy.set()
            raise

    monkeypatch.setattr(registry, method, observed)


@pytest.mark.parametrize("boundary", ["recover_interrupted", "claim"])
def test_once_waits_for_real_writer_then_executes_exactly_once(service, monkeypatch, boundary):
    job = _queued(service)
    held = HeldWrite(service.registry.path)
    busy, calls, executions, launches = threading.Event(), [], [], []
    original_recover = service.registry.recover_interrupted
    if boundary == "recover_interrupted":
        held.start()
    else:

        def recover_then_lock():
            original_recover()
            held.start()

        monkeypatch.setattr(service.registry, "recover_interrupted", recover_then_lock)
    _watch_busy(monkeypatch, service.registry, boundary, busy, calls)
    observer, errors = _busy_release_thread(busy, held)

    def compute(run):
        executions.append(run["id"])
        return {"retained": "one completed calculation"}

    def launch(command, **kwargs):
        assert kwargs["start_new_session"] is True
        launches.append(command[-1])
        # Process lifecycle itself has independent real-process tests. Here the
        # real database claim/publication protocol is the boundary under test.
        worker.execute(service, command[-1])
        return SimpleNamespace(pid=99999999, returncode=0, poll=lambda: 0, wait=lambda timeout: 0)

    monkeypatch.setattr(service, "execute_run", compute)
    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    monkeypatch.setattr(worker, "signal_private_group", lambda *args: None)
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        worker.work(service, once=True, timeout=2)
        observer.join(timeout=2)
        assert not observer.is_alive() and not errors
        assert busy.is_set() and len(calls) >= 2
        assert executions == launches == [job["id"]]
        assert service.registry.busy_timeout_ms == 173
        finished = service.registry.get_run(job["id"])
        assert finished["status"] == "succeeded"
        assert finished["result"] == {"retained": "one completed calculation"}
        events = service.registry.audit()["events"]
        assert sum(event["action"] == "runs.running" for event in events) == 1
        assert sum(event["action"] == "runs.succeeded" for event in events) == 1
        assert all(signal.getsignal(sig) == handler for sig, handler in previous_handlers.items())
    finally:
        held.close()
        observer.join(timeout=2)


@pytest.mark.parametrize("failed", [False, True])
def test_publication_retries_same_result_without_reexecuting_physics(service, monkeypatch, failed):
    job = _queued(service)
    service.registry.claim()
    held, busy, calls, calculations = HeldWrite(service.registry.path), threading.Event(), [], []
    evidence = {"retained": "success or failure evidence"}

    class CalculationFailureError(ValueError):
        evidence_result = evidence

    def compute(run):
        calculations.append(run["id"])
        held.start()
        if failed:
            raise CalculationFailureError("intentional calculation failure")
        return evidence

    monkeypatch.setattr(service, "execute_run", compute)
    _watch_busy(monkeypatch, service.registry, "finish", busy, calls)
    observer, errors = _busy_release_thread(busy, held)
    try:
        worker.execute(service, job["id"])
        observer.join(timeout=2)
        assert not observer.is_alive() and not errors
        assert busy.is_set() and len(calls) >= 2
        assert calculations == [job["id"]]
        assert service.registry.busy_timeout_ms == 173
        finished = service.registry.get_run(job["id"])
        assert finished["status"] == ("failed" if failed else "succeeded")
        assert finished["result"] == evidence
        if failed:
            assert finished["error"] == "CalculationFailureError: intentional calculation failure"
        action = "runs.failed" if failed else "runs.succeeded"
        assert sum(event["action"] == action for event in service.registry.audit()["events"]) == 1
    finally:
        held.close()
        observer.join(timeout=2)


def test_cancel_between_publication_attempts_is_not_overwritten(service, monkeypatch):
    job = _queued(service)
    service.registry.claim()
    held, calculations, finishes = HeldWrite(service.registry.path), [], []
    original_finish = service.registry.finish

    def compute(run):
        calculations.append(run["id"])
        held.start()
        return {"calculated": True}

    def finish(*args, **kwargs):
        finishes.append(1)
        try:
            return original_finish(*args, **kwargs)
        except sqlite3.OperationalError as error:
            assert error.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
            held.close()
            service.registry.cancel(job["id"])
            raise

    monkeypatch.setattr(service, "execute_run", compute)
    monkeypatch.setattr(service.registry, "finish", finish)
    try:
        worker.execute(service, job["id"])
        assert len(finishes) == 2 and calculations == [job["id"]]
        assert service.registry.get_run(job["id"])["status"] == "cancelled"
        events = service.registry.audit()["events"]
        assert not any(event["action"] == "runs.succeeded" for event in events)
    finally:
        held.close()


def test_real_nonbusy_sql_error_is_not_swallowed_or_retried(service):
    calls = []

    def broken_query():
        calls.append(1)
        with service.registry.connection() as conn:
            conn.execute("SELECT * FROM deliberately_missing_worker_test_table")

    with pytest.raises(sqlite3.OperationalError) as captured:
        worker._retry_database_busy(broken_query, label="nonbusy SQL test")
    assert captured.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
    assert calls == [1]


def test_real_sqlite_locked_is_not_reclassified_as_busy():
    uri = f"file:worker-locked-{uuid.uuid4().hex}?mode=memory&cache=shared"
    first = sqlite3.connect(uri, uri=True, isolation_level=None)
    second = sqlite3.connect(uri, uri=True, isolation_level=None)
    calls = []
    try:
        first.execute("CREATE TABLE fixture(value)")
        first.execute("BEGIN IMMEDIATE")

        def blocked():
            calls.append(1)
            second.execute("BEGIN IMMEDIATE")

        with pytest.raises(sqlite3.OperationalError) as captured:
            worker._retry_database_busy(blocked, label="SQLITE_LOCKED test")
        assert captured.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_LOCKED
        assert calls == [1]
    finally:
        first.rollback()
        first.close()
        second.close()


@pytest.mark.parametrize("boundary", ["recover_interrupted", "claim"])
def test_shutdown_interrupts_busy_recovery_or_claim_and_restores_configuration(
    service, monkeypatch, boundary
):
    job = _queued(service)
    held, busy, calls = HeldWrite(service.registry.path), threading.Event(), []
    original_recover = service.registry.recover_interrupted
    if boundary == "recover_interrupted":
        held.start()
    else:

        def recover_then_lock():
            original_recover()
            held.start()

        monkeypatch.setattr(service.registry, "recover_interrupted", recover_then_lock)
    _watch_busy(monkeypatch, service.registry, boundary, busy, calls)
    errors = []

    def stop_when_busy():
        try:
            assert busy.wait(2), "Worker did not encounter the held transaction"
            os.kill(os.getpid(), signal.SIGTERM)
        except BaseException as error:
            errors.append(error)
            held.release.set()

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **k: pytest.fail("No job claimed"))
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    observer = threading.Thread(target=stop_when_busy, name="stop-busy-worker")
    started = time.monotonic()
    try:
        observer.start()
        worker.work(service, once=True)
        elapsed = time.monotonic() - started
        observer.join(timeout=2)
        assert not observer.is_alive() and not errors
        assert elapsed < 1.0
        assert not held.release.is_set(), "Shutdown must not require writer release"
        assert service.registry.busy_timeout_ms == 173
        assert all(signal.getsignal(sig) == handler for sig, handler in previous_handlers.items())
    finally:
        held.close()
        observer.join(timeout=2)
    assert service.registry.get_run(job["id"])["status"] == "queued"


def test_publication_busy_deadline_is_visible_and_never_reruns_calculation(service, monkeypatch):
    job = _queued(service)
    service.registry.claim()
    held, calculations = HeldWrite(service.registry.path), []
    monkeypatch.setattr(worker, "DATABASE_RETRY_TIMEOUT_S", 0.09)

    def compute(run):
        calculations.append(run["id"])
        held.start()
        return {"not_published": "caller must observe a hard failure"}

    monkeypatch.setattr(service, "execute_run", compute)
    started = time.monotonic()
    try:
        with pytest.raises(
            worker.WorkerDatabaseBusyTimeoutError, match="publishing run"
        ) as captured:
            worker.execute(service, job["id"])
        assert isinstance(captured.value.__cause__, sqlite3.OperationalError)
        assert time.monotonic() - started < 0.5
        assert calculations == [job["id"]]
        assert service.registry.busy_timeout_ms == 173
    finally:
        held.close()
    # An unpublished result is not marked succeeded or automatically recomputed.
    assert service.registry.get_run(job["id"])["status"] == "running"


def test_connection_timeout_is_worker_local_and_restored_after_exception(service):
    with pytest.raises(ValueError, match="intentional"):
        with worker._short_database_waits(service.registry):
            with service.registry.connection() as conn:
                assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 20
            raise ValueError("intentional")
    assert service.registry.busy_timeout_ms == 173
    with service.registry.connection() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 173


def test_active_status_probe_avoids_job_decode(service, monkeypatch):
    job=_queued(service);service.registry.claim()
    monkeypatch.setattr(service.registry,'get_run',lambda *_:pytest.fail('Do not decode job graph'))
    assert worker._execution_status(service.registry,job['id'],threading.Event())=='running'


def test_active_status_probe_retries_exclusive_lock(service):
    job=_queued(service);service.registry.claim()
    with sqlite3.connect(service.registry.path,isolation_level=None) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('BEGIN EXCLUSIVE')
        result=[]
        thread=threading.Thread(target=lambda:result.append(worker._execution_status(service.registry,job['id'],threading.Event())))
        thread.start();time.sleep(.1)
        assert thread.is_alive()
        conn.rollback();thread.join(timeout=2)
        assert result==['running']
