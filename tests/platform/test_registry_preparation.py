"""Slow evidence preparation must not own SQLite's single writer lock."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from camera_platform.registry import SQLiteRegistry, digest


@pytest.mark.parametrize("operation", ["put", "case", "submit", "finish"])
def test_slow_graph_encoding_does_not_block_a_waiting_job(tmp_path, monkeypatch, operation):
    registry = SQLiteRegistry(tmp_path)
    registry.busy_timeout_ms = 50
    completing = registry.submit({"setup": True}, "setup")
    assert registry.claim()["id"] == completing["id"]
    waiting = registry.submit({"waiting": True}, "waiting")
    entered, release = threading.Event(), threading.Event()
    encode = registry.encode_document

    def gated_encode(value):
        if value.get("slow_evidence"):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release the evidence encoder")
        return encode(value)

    monkeypatch.setattr(registry, "encode_document", gated_encode)
    payload = {"slow_evidence": True, "snapshots": [list(range(4096))] * 20}
    actions = {
        "put": lambda: registry.put("assets", payload),
        "case": lambda: registry.publish_case_review("case-test", {
            **payload, "case_id": "case-test", "revision": 1, "previous_revision_id": None,
        }),
        "submit": lambda: registry.submit(payload, digest(payload)),
        "finish": lambda: registry.finish(completing["id"], result=payload),
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(actions[operation])
        try:
            assert entered.wait(5)
            # A second connection can claim existing work while graph bytes are
            # being prepared. The pre-fix code instead holds BEGIN IMMEDIATE.
            claimed = registry.claim()
            assert claimed["id"] == waiting["id"]
            assert claimed["status"] == "running"
        finally:
            release.set()
        assert future.result(timeout=5)


def test_slow_claim_decode_does_not_block_other_publication(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path)
    registry.busy_timeout_ms = 50
    waiting = registry.submit({"large_request": [list(range(4096))] * 20}, "large")
    entered, release = threading.Event(), threading.Event()
    decode = registry._job

    def gated_decode(row, completion_events=None):
        if row["id"] == waiting["id"]:
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release the request decoder")
        return decode(row, completion_events)

    monkeypatch.setattr(registry, "_job", gated_decode)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(registry.claim)
        try:
            assert entered.wait(5)
            record = registry.put("assets", {"name": "independent write"})
            assert record["name"] == "independent write"
        finally:
            release.set()
        assert future.result(timeout=5)["id"] == waiting["id"]


def test_competing_claims_do_not_orphan_or_duplicate_jobs(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path)
    queued = [registry.submit({"ordinal": i}, str(i)) for i in range(2)]
    barrier, local = threading.Barrier(2), threading.local()
    decode = registry._job

    def overlapping_decode(row, completion_events=None):
        if not getattr(local, "has_decoded", False):
            local.has_decoded = True
            barrier.wait(timeout=5)
        return decode(row, completion_events)

    monkeypatch.setattr(registry, "_job", overlapping_decode)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(registry.claim) for _ in range(2)]
        claimed = [future.result(timeout=5) for future in futures]
    assert {job["id"] for job in claimed} == {job["id"] for job in queued}
    assert all(job["status"] == "running" for job in claimed)
    events = registry.audit()["events"]
    assert len([event for event in events if event["action"] == "runs.running"]) == 2
