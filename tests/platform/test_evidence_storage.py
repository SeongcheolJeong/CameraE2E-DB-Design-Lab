"""Large logical evidence stays exact across graph storage, corruption and backup."""

import json
import zipfile
from copy import deepcopy

import pytest

from camera_platform.backup import backup_state, restore_state
from camera_platform.evidence_storage import PREFIX, THRESHOLD
from camera_platform.registry import (
    ConflictError,
    IntegrityError,
    SQLiteRegistry,
    canonical,
    digest,
    now,
)


@pytest.fixture
def registry(tmp_path):
    return SQLiteRegistry(tmp_path / "registry")


def large_document(name="fixed camera evidence"):
    shared = {
        "samples": list(range(4096)),
        "provenance": {"kind": "simulated", "note": "declared response only"},
    }
    value = {"name": name, "snapshots": [shared] * 20}
    assert len(canonical(value).encode()) > THRESHOLD
    return value


def stored(registry, table, identity):
    with registry.connection() as conn:
        return dict(conn.execute("SELECT * FROM " + table + " WHERE id=?", (identity,)).fetchone())


def blobs(registry):
    return {path.name: path.read_bytes() for path in registry.blobs.iterdir() if path.is_file()}


def complete(registry, request, result):
    queued = registry.submit(request, digest(request))
    assert registry.claim()["id"] == queued["id"]
    return registry.finish(queued["id"], result=result)


def rewrite_record_storage(registry, identity, document):
    # Deliberate database corruption, including bypassing the immutable trigger.
    with registry.connection() as conn:
        conn.execute("DROP TRIGGER immutable_records_update")
        conn.execute("UPDATE records SET document=? WHERE id=?", (document, identity))


def test_large_records_and_jobs_share_nodes_preserve_logical_hashes_and_restart(registry):
    document = large_document()
    first = registry.put("assets", document)
    first_blobs = blobs(registry)
    second = registry.put("assets", document)
    second_blobs = blobs(registry)
    assert set(first_blobs) < set(second_blobs)
    assert sum(len(value) for key, value in second_blobs.items() if key not in first_blobs) < 4096
    artifact = registry.store_blob(b"retained RAW bytes", "raw.npy", "application/octet-stream")
    request = {"snapshot": document, "input_artifacts": [artifact]}
    result = {"snapshot": document, "artifacts": [artifact]}
    run = complete(registry, request, result)
    physical = [stored(registry, "records", value["id"])["document"] for value in (first, second)]
    job = stored(registry, "jobs", run["id"])
    physical += [job["document"], job["result"]]
    assert all(value.startswith(PREFIX) and len(value.encode()) < 1024 for value in physical)
    logical_size = sum(len(canonical(value).encode()) for value in (first, second, request, result))
    assert sum(map(len, blobs(registry).values())) + sum(map(len, physical)) < logical_size / 4
    assert job["request_hash"] == run["content_hash"] == digest(request)
    for record in (first, second):
        assert record["content_hash"] == digest(
            {key: value for key, value in record.items() if key != "content_hash"}
        )
    audit = registry.audit()
    completion = next(event for event in audit["events"] if event["action"] == "runs.succeeded")
    assert completion["details"]["result_hash"] == digest(result)
    restarted = SQLiteRegistry(registry.directory)
    assert restarted.get("assets", first["id"]) == first
    assert restarted.get("assets", second["id"]) == second
    assert restarted.get_run(run["id"]) == run
    assert restarted.audit() == audit
    assert [
        stored(restarted, "records", value["id"])["document"] for value in (first, second)
    ] == physical[:2]


@pytest.mark.parametrize(
    "fault",
    [
        "same_size_node",
        "missing_node",
        "root_sha",
        "root_bytes",
        "root_semantic",
        "root_schema",
        "unknown_root_field",
    ],
)
def test_next_record_read_rejects_corruption_after_successful_read(registry, fault):
    record = registry.put("assets", large_document())
    assert registry.get("assets", record["id"]) == record
    raw = stored(registry, "records", record["id"])["document"]
    assert raw.startswith(PREFIX)
    reference = json.loads(raw[len(PREFIX) :])
    if fault in {"same_size_node", "missing_node"}:
        path = max(registry.blobs.iterdir(), key=lambda item: item.stat().st_size)
        if fault == "same_size_node":
            original = path.read_bytes()
            changed = original.replace(b"4095", b"4094", 1)
            assert len(changed) == len(original) and changed != original
            path.write_bytes(changed)
        else:
            path.unlink()
    else:
        if fault == "root_sha":
            reference["sha256"] = "0" * 64
        elif fault == "root_bytes":
            reference["bytes"] += 1
        elif fault == "root_semantic":
            reference["semantic_sha256"] = "0" * 64
        elif fault == "root_schema":
            reference["schema"] = "camera_evidence_ref_v999"
        else:
            reference["unexpected"] = True
        rewrite_record_storage(registry, record["id"], PREFIX + canonical(reference))
    with pytest.raises(IntegrityError):
        registry.get("assets", record["id"])


@pytest.mark.parametrize("column", ["document", "result"])
def test_next_job_read_checks_each_stored_graph_again(registry, column):
    request = {"request": large_document("source")}
    result = {"result": large_document("result"), "artifacts": []}
    run = complete(registry, request, result)
    assert registry.get_run(run["id"]) == run
    raw = stored(registry, "jobs", run["id"])[column]
    assert raw.startswith(PREFIX)
    reference = json.loads(raw[len(PREFIX) :])
    path = registry.blobs / reference["sha256"]
    original = path.read_bytes()
    path.write_bytes(original.replace(b'"schema"', b'"schemX"', 1))
    assert path.stat().st_size == len(original)
    with pytest.raises(IntegrityError):
        registry.get_run(run["id"])


def test_reference_shaped_user_values_are_literal_and_returned_nodes_do_not_alias(registry):
    value = large_document()
    literal = {
        "schema": "camera_evidence_ref_v1",
        "sha256": "0" * 64,
        "bytes": 42,
        "semantic_sha256": "1" * 64,
        "expanded_bytes": 500,
    }
    value["literal_ref"] = literal
    value["literal_node"] = {
        "schema": "camera_evidence_node_v1",
        "value": None,
        "children": [literal],
    }
    value["literal_prefix"] = PREFIX + canonical(literal)
    record = registry.put("assets", value)
    returned = registry.get("assets", record["id"])
    assert returned == record
    assert returned["literal_ref"] == literal
    returned["snapshots"][0]["samples"][0] = 999
    assert returned["snapshots"][1]["samples"][0] == 0
    returned["literal_ref"]["sha256"] = "f" * 64
    assert registry.get("assets", record["id"]) == record
    value["snapshots"][0]["samples"][1] = 999
    assert registry.get("assets", record["id"])["snapshots"][0]["samples"][1] == 1


def test_small_and_preexisting_large_inline_bytes_are_never_rewritten(registry):
    small = registry.put("assets", {"name": "old small", "payload": {"value": 1.25}})
    small_row = stored(registry, "records", small["id"])
    assert small_row["document"] == canonical(
        {k: v for k, v in small.items() if k != "content_hash"}
    )
    stamp = now()
    old_value = {**large_document("old inline"), "id": "asset:old-inline", "created_at": stamp}
    original_bytes = canonical(old_value)
    with registry.transaction() as conn:
        conn.execute(
            "INSERT INTO records VALUES(?,?,?,?,?)",
            ("assets", old_value["id"], original_bytes, digest(old_value), stamp),
        )
        registry.index_record(conn, "assets", old_value)
        registry._audit(
            conn, "assets.created", old_value["id"], {"content_hash": digest(old_value)}
        )
    old = {**old_value, "content_hash": digest(old_value)}
    assert registry.get("assets", old["id"]) == old
    registry.put("assets", large_document("new graph"))
    audit = registry.audit()
    restarted = SQLiteRegistry(registry.directory)
    assert restarted.get("assets", old["id"]) == old
    assert stored(restarted, "records", old["id"])["document"] == original_bytes
    assert stored(restarted, "records", small["id"]) == small_row
    assert restarted.audit() == audit


def test_cached_submit_compares_logical_request_without_duplicate_jobs(registry):
    request = {"request": large_document()}
    key = digest(request)
    first = registry.submit(request, key)
    row = stored(registry, "jobs", first["id"])
    before = registry.audit()
    cached = registry.submit(deepcopy(request), key)
    assert cached["cache_hit"] is True
    assert cached["id"] == first["id"]
    assert cached["content_hash"] == digest(request)
    assert cached["request"] == request["request"]
    assert stored(registry, "jobs", first["id"]) == row
    assert registry.audit() == before
    changed = deepcopy(request)
    changed["request"]["snapshots"][0]["samples"][0] = -1
    with pytest.raises(ConflictError, match="Cache key collision"):
        registry.submit(changed, key)
    assert len(registry.list_runs()) == 1
    assert registry.audit() == before


def test_completed_result_artifact_is_indexed_but_graph_objects_stay_private(registry):
    artifact = registry.store_blob(b"result pixels", "rgb.npy", "application/octet-stream")
    run = complete(
        registry,
        {"request": large_document()},
        {"summary": large_document(), "artifacts": [artifact]},
    )
    owner = registry.artifact_owner(artifact["sha256"])
    assert owner["owner"]["id"] == run["id"]
    assert owner["descriptor"] == artifact
    private = set(blobs(registry)) - {artifact["sha256"]}
    assert private
    for checksum in private:
        with pytest.raises(KeyError, match="not associated"):
            registry.artifact_owner(checksum)


def test_backup_restore_preserves_private_graph_closure_and_public_artifact_boundary(
    registry, tmp_path
):
    artifact = registry.store_blob(b"source pixels", "raw.npy", "application/octet-stream")
    record = registry.put("assets", large_document())
    run = complete(
        registry,
        {"request": large_document(), "input_artifacts": [artifact]},
        {"summary": large_document(), "artifacts": [artifact]},
    )
    closure = blobs(registry)
    orphan = registry.store_blob(b"uncommitted private object", "orphan.json", "application/json")
    audit = registry.audit()
    archive = tmp_path / "evidence.zip"
    backup_state(registry.directory, archive)
    with zipfile.ZipFile(archive) as bundle:
        archived_blobs = {
            name.removeprefix("blobs/"): bundle.read(name)
            for name in bundle.namelist()
            if name.startswith("blobs/")
        }
    assert archived_blobs == closure
    assert orphan["sha256"] not in archived_blobs
    destination = tmp_path / "restored"
    assert restore_state(archive, destination)["integrity"] == "verified"
    restored = SQLiteRegistry(destination)
    assert restored.get("assets", record["id"]) == record
    assert restored.get_run(run["id"]) == run
    assert restored.audit() == audit
    assert stored(restored, "records", record["id"]) == stored(registry, "records", record["id"])
    assert stored(restored, "jobs", run["id"]) == stored(registry, "jobs", run["id"])
    assert restored.artifact_owner(artifact["sha256"])["descriptor"] == artifact
    for checksum in set(closure) - {artifact["sha256"]}:
        with pytest.raises(KeyError, match="not associated"):
            restored.artifact_owner(checksum)
