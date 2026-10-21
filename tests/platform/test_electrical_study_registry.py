"""Transaction, dependency and cancellation boundaries of paired acquisitions."""
from copy import deepcopy

import pytest

from camera_platform.registry import ConflictError, IntegrityError, SQLiteRegistry, digest


def inputs():
    requests = [{"cell_id": "cell-0", "role": role,
                 "request": {"plugin_id": "electrical_control_v1", "name": role}}
                for role in ("baseline", "candidate")]
    document = {
        "job_kind": "electrical_response_study", "plugin_id": "electrical_response_study_v1",
        "name": "matched case", "planned_children": [
            {"cell_id": r["cell_id"], "role": r["role"], "request_sha256": digest(r["request"])}
            for r in requests
        ],
    }
    record = {"id": "test-fixed", "name": "fixed test", "created_at": "2026-09-24T00:00:00Z"}
    record["content_hash"] = digest(record)
    return document, [("tests", record)], requests


def test_dependency_barrier_does_not_claim_aggregate_before_running_children(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    parent = registry.publish_electrical_response(*inputs())
    first, second = registry.claim(), registry.claim()
    assert {first["id"], second["id"]} == {c["run_id"] for c in parent["children"]}
    assert registry.claim() is None
    unrelated = registry.submit({"name": "unrelated"}, "outside")
    assert registry.claim()["id"] == unrelated["id"]
    registry.finish(first["id"], result={"artifacts": [], "observation": 1})
    assert registry.claim() is None
    registry.finish(second["id"], error="deliberate acquisition failure")
    assert registry.claim()["id"] == parent["id"]
    assert registry.get_run(second["id"])["status"] == "failed"


def test_queue_capacity_failure_rolls_back_all_sources_and_children(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    for i in range(62):
        registry.submit({"name": f"busy-{i}"}, str(i))
    before = registry.audit()
    with pytest.raises(ConflictError, match="complete electrical study"):
        registry.publish_electrical_response(*inputs())
    assert len(registry.list_runs()) == 62
    assert registry.list("tests") == []
    assert registry.audit() == before


def test_cancel_is_owned_atomic_and_preserves_finished_child_and_other_jobs(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    parent = registry.publish_electrical_response(*inputs())
    first, second = registry.claim(), registry.claim()
    registry.finish(first["id"], result={"artifacts": [], "raw_evidence": "preserved"})
    outside = registry.submit({"name": "another user job"}, "outside")
    registry.cancel(parent["id"])
    assert registry.get_run(parent["id"])["status"] == "cancelled"
    assert registry.get_run(first["id"])["status"] == "succeeded"
    assert registry.get_run(second["id"])["status"] == "cancelled"
    assert registry.get_run(outside["id"])["status"] == "queued"
    late = registry.finish(second["id"], result={"artifacts": [], "late": True})
    assert late["status"] == "cancelled" and late["result"] is None
    assert registry.claim()["id"] == outside["id"]
    assert registry.audit()["integrity"] == "verified"


def test_interrupted_child_remains_failed_and_aggregate_can_record_partial_evidence(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    parent = registry.publish_electrical_response(*inputs())
    interrupted = registry.claim()
    registry.recover_interrupted()
    other = registry.claim()
    assert other["id"] != parent["id"]
    registry.finish(other["id"], result={"artifacts": []})
    assert registry.get_run(interrupted["id"])["status"] == "failed"
    assert registry.claim()["id"] == parent["id"]


def test_repeat_reuses_entire_batch_and_force_owns_new_children(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    parent = registry.publish_electrical_response(*inputs())
    again = registry.publish_electrical_response(*inputs())
    assert again["cache_hit"] and again["id"] == parent["id"]
    forced = registry.publish_electrical_response(*inputs(), force=True)
    assert forced["id"] != parent["id"]
    assert not ({c["run_id"] for c in parent["children"]} &
                {c["run_id"] for c in forced["children"]})
    assert len(registry.list("tests")) == 1 and len(registry.list_runs()) == 6
    registry.cancel(parent["id"])
    assert registry.get_run(forced["id"])["status"] == "queued"


def test_publication_refuses_tampered_child_before_writing_any_record(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    document, records, requests = inputs()
    requests[0]["request"]["name"] = "swapped source"
    with pytest.raises(IntegrityError, match="frozen plan"):
        registry.publish_electrical_response(document, records, requests)
    assert registry.list_runs() == [] and registry.list("tests") == []


def test_existing_revision_collision_never_queues_partial_batch(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    registry.publish_electrical_response(*inputs())
    document, records, requests = deepcopy(inputs())
    records[0][1]["name"] = "collision"
    records[0][1].pop("content_hash")
    records[0][1]["content_hash"] = digest(records[0][1])
    before = registry.audit()
    with pytest.raises(ConflictError, match="identity collision"):
        registry.publish_electrical_response(document, records, requests, force=True)
    assert len(registry.list_runs()) == 3 and registry.audit() == before
