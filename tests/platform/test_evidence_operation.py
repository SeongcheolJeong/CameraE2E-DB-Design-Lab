import pytest

from camera_platform.evidence_reads import OperationRegistry
from camera_platform.registry import IntegrityError, SQLiteRegistry, digest


def test_cached_json_returns_detached_values_and_next_operation_is_fresh(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    record = registry.put("models", {"nested": {"value": [1, 2]}})
    reader = OperationRegistry(registry)
    edited = reader.get("models", record["id"])
    edited["nested"]["value"].append(3)
    assert reader.get("models", record["id"]) == record
    request = {"input_artifacts": [], "nested": {"value": 1}}
    job = registry.submit(request, digest(request))
    snapshot = reader.get_run(job["id"])
    snapshot["nested"]["value"] = 9
    claimed = registry.claim()
    assert claimed["id"] == job["id"]
    registry.finish(job["id"], result={"artifacts": []})
    assert reader.get_run(job["id"])["status"] == "queued"
    assert reader.get_run(job["id"])["nested"]["value"] == 1
    assert OperationRegistry(registry).get_run(job["id"])["status"] == "succeeded"


def test_verified_path_is_not_reused_after_same_size_blob_change(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    artifact = registry.store_blob(b"abcd", "raw.bin", "application/octet-stream")
    reader = OperationRegistry(registry)
    path = reader.blob_path(artifact["sha256"])
    path.write_bytes(b"abce")
    with pytest.raises(IntegrityError):
        reader.blob_path(artifact["sha256"])


def test_read_context_refuses_publication(tmp_path):
    reader = OperationRegistry(SQLiteRegistry(tmp_path))
    with pytest.raises(ValueError, match="cannot mutate"):
        reader.put("models", {})
