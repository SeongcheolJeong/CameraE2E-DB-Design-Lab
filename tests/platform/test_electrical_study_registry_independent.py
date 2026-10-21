"""Independent interleavings at paired-job publication/claim/cancel boundaries."""
from copy import deepcopy

import pytest

from camera_platform.registry import ConflictError, IntegrityError, SQLiteRegistry, digest


def batch():
    children = [
        {
            "cell_id": "condition-0",
            "role": role,
            "request": {"plugin_id": "electrical_control_v1", "input": role},
        }
        for role in ("baseline", "candidate")
    ]
    document = {
        "job_kind": "electrical_response_study",
        "plugin_id": "electrical_response_study_v1",
        "planned_children": [
            {"cell_id": c["cell_id"], "role": c["role"], "request_sha256": digest(c["request"])}
            for c in children
        ],
    }
    record = {"id": "fixed-test", "name": "immutable test", "created_at": "2026-09-24"}
    record["content_hash"] = digest(record)
    return document, [("tests", record)], children


def table_snapshot(registry):
    with registry.connection() as conn:
        return {
            name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in ("records", "jobs", "audit", "artifact_owners", "artifact_heads")
        }


def test_concurrent_equal_publication_reuses_complete_winning_batch(tmp_path, monkeypatch):
    registry, other = SQLiteRegistry(tmp_path), SQLiteRegistry(tmp_path)
    read, winner = registry._read_rows, []

    def interleave(queries):
        rows = read(queries)
        if not winner and "cache_key=?" in queries[0][0]:
            winner.append(other.publish_electrical_response(*batch()))
        return rows

    monkeypatch.setattr(registry, "_read_rows", interleave)
    loser = registry.publish_electrical_response(*batch())
    assert loser["id"] == winner[0]["id"] and loser["cache_hit"]
    assert loser["children"] == winner[0]["children"]
    assert len(registry.list_runs()) == 3
    assert len(registry.list("tests")) == 1
    events = registry.audit()["events"]
    assert sum(e["action"] == "runs.queued" for e in events) == 3


def test_revision_collision_arriving_after_preflight_refuses_without_partial_jobs(
    tmp_path, monkeypatch
):
    registry, other = SQLiteRegistry(tmp_path), SQLiteRegistry(tmp_path)
    read, injected = registry._read_rows, []
    changed = deepcopy(batch()[1][0][1])
    changed["name"] = "different immutable author"
    changed["content_hash"] = digest({k: v for k, v in changed.items() if k != "content_hash"})

    def interleave(queries):
        rows = read(queries)
        if not injected and queries and "FROM records" in queries[0][0]:
            staged = other._stage_record("tests", changed)
            with other.transaction() as conn:
                other._insert_staged_record(conn, staged)
            injected.append(True)
        return rows

    monkeypatch.setattr(registry, "_read_rows", interleave)
    with pytest.raises(ConflictError, match="identity collision"):
        registry.publish_electrical_response(*batch())
    assert registry.list_runs() == []
    assert registry.get("tests", "fixed-test") == changed
    assert [e["action"] for e in registry.audit()["events"]] == ["tests.created"]


def test_mid_batch_index_failure_rolls_back_records_jobs_indexes_and_audit(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path)
    before, index, calls = table_snapshot(registry), registry.index_job, []

    def failing_index(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 2:
            raise RuntimeError("injected second-child index failure")
        return index(*args, **kwargs)

    monkeypatch.setattr(registry, "index_job", failing_index)
    with pytest.raises(RuntimeError, match="second-child index"):
        registry.publish_electrical_response(*batch())
    assert len(calls) == 2
    assert table_snapshot(registry) == before
    assert registry.audit()["integrity"] == "verified"


def test_child_finishing_between_cancel_read_and_cas_preserves_its_result(tmp_path, monkeypatch):
    registry, other = SQLiteRegistry(tmp_path), SQLiteRegistry(tmp_path)
    parent = registry.publish_electrical_response(*batch())
    child, sibling = registry.claim(), registry.claim()
    outside = registry.submit({"name": "unrelated"}, "unrelated")
    read, injected = registry._read_rows, []
    result = {"artifacts": [], "immutable_observation": [11, 17]}

    def interleave(queries):
        rows = read(queries)
        if not injected and len(queries) == 2 and queries[0][1][0] in {
            child["id"], sibling["id"]
        }:
            other.finish(child["id"], result=result)
            injected.append(True)
        return rows

    monkeypatch.setattr(registry, "_read_rows", interleave)
    registry.cancel_electrical_response(parent["id"])
    assert injected
    assert registry.get_run(child["id"])["result"] == result
    assert registry.get_run(child["id"])["status"] == "succeeded"
    assert registry.get_run(sibling["id"])["status"] == "cancelled"
    assert registry.get_run(outside["id"])["status"] == "queued"
    cancelled = [
        e["subject"] for e in registry.audit()["events"] if e["action"] == "runs.cancelled"
    ]
    assert set(cancelled) == {parent["id"], sibling["id"]}
    assert len(cancelled) == 2


def test_cancel_between_claim_snapshot_and_cas_never_claims_cancelled_children(
    tmp_path, monkeypatch
):
    registry, other = SQLiteRegistry(tmp_path), SQLiteRegistry(tmp_path)
    parent = registry.publish_electrical_response(*batch())
    outside = registry.submit({"name": "independent remaining work"}, "outside")
    read, injected = registry._read_rows, []

    def interleave(queries):
        rows = read(queries)
        if not injected and queries and "status='queued' ORDER BY" in queries[0][0]:
            other.cancel_electrical_response(parent["id"])
            injected.append(True)
        return rows

    monkeypatch.setattr(registry, "_read_rows", interleave)
    assert registry.claim()["id"] == outside["id"]
    assert registry.claim() is None
    for identity in [parent["id"], *[c["run_id"] for c in parent["children"]]]:
        assert registry.get_run(identity)["status"] == "cancelled"


def test_forged_parent_cannot_adopt_or_cancel_another_parents_children(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    owner = registry.publish_electrical_response(*batch())
    forged = registry.submit(
        {"job_kind": "electrical_response_study", "children": deepcopy(owner["children"])},
        "forged-owner",
    )
    before = table_snapshot(registry)
    with pytest.raises(IntegrityError, match="ownership"):
        registry.cancel_electrical_response(forged["id"])
    assert table_snapshot(registry) == before
    assert all(registry.get_run(c["run_id"])["status"] == "queued" for c in owner["children"])
    assert registry.get_run(owner["id"])["status"] == "queued"
