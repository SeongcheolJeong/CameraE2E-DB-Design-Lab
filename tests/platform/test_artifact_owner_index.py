"""Bound artifact reads to their chosen owner without weakening fresh evidence checks."""

import io
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_platform import registry as registry_module
from camera_platform.api import create_app
from camera_platform.backup import BackupError, backup_state, restore_state
from camera_platform.evidence_storage import PREFIX, THRESHOLD
from camera_platform.registry import IntegrityError, SQLiteRegistry, canonical, digest, now


def artifact(registry, name="original.bin"):
    return registry.store_blob(b"original target bytes", name, "application/octet-stream")


def completed(registry, entry, *, inputs=None):
    request = {"input_artifacts": inputs or [], "request": {"seed": 9}}
    run = registry.submit(request, digest(request), force=True)
    registry.claim()
    return registry.finish(run["id"], result={"artifacts": [entry]})


def legacy(registry):
    with registry.transaction() as conn:
        conn.execute("DROP TABLE artifact_heads")
        conn.execute("DROP TABLE artifact_owners")
        conn.execute("DROP INDEX audit_subject_action")
        conn.execute("UPDATE schema_version SET version=1")


def originals(registry):
    with registry.connection() as conn:
        return {name: [tuple(row) for row in conn.execute("SELECT * FROM " + name)]
                for name in ("records", "jobs", "audit")}


def test_public_precedence_preserves_names_and_output_before_input(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    registry.put("captures", {"artifacts": [{**entry, "name": "capture.bin"}]})
    registry.put("measurements", {"original_artifact": {**entry, "name": "measurement.bin"}})
    assert registry.artifact_owner(entry["id"])["descriptor"]["name"] == "measurement.bin"
    first = completed(registry, {**entry, "name": "old-output.bin"})
    second = completed(registry, {**entry, "name": "new-output.bin"},
                       inputs=[{**entry, "name": "input.bin"}])
    binding = registry.artifact_owner(entry["id"])
    assert binding["owner"]["id"] == second["id"] != first["id"]
    assert binding["descriptor"]["name"] == "new-output.bin"


def test_exact_binding_resolves_nonpreferred_owner_without_changing_public_precedence(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    first = registry.put("captures", {"artifacts": [{**entry, "name": "first.bin"}]})
    second = registry.put("measurements", {
        "original_artifact": {**entry, "name": "second.bin"},
    })
    assert registry.artifact_owner(entry["id"])["owner"]["id"] == second["id"]
    bound = registry.artifact_binding(
        entry["id"], owner_kind="captures", owner_id=first["id"]
    )
    assert bound["descriptor"]["name"] == "first.bin"
    with pytest.raises(KeyError, match="exact owner"):
        registry.artifact_binding(entry["id"], owner_kind="captures", owner_id=second["id"])


def test_queued_input_is_published_and_completion_updates_same_transaction(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    run = registry.submit({"input_artifacts": [entry]}, "queued")
    assert registry.artifact_owner(entry["id"])["owner"]["status"] == "queued"
    registry.claim()
    registry.finish(run["id"], result={"artifacts": [{**entry, "name": "output.bin"}]})
    assert registry.artifact_owner(entry["id"])["descriptor"]["name"] == "output.bin"


def test_unregistered_and_arbitrary_nested_blob_are_not_public(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    registry.put("assets", {"arbitrary": entry})
    registry.put("captures", {"arbitrary": entry})
    with pytest.raises(KeyError, match="not associated"):
        registry.artifact_owner(entry["id"])


@pytest.mark.parametrize("change", ["descriptor_hash", "descriptor_path", "owner_priority",
                                     "owner_id", "delete_index", "delete_guard"])
def test_stale_forged_deleted_index_never_falls_back_to_lower_owner(tmp_path, change):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    registry.put("captures", {"artifacts": [entry]})
    run = completed(registry, entry)
    with registry.connection() as conn:
        if change == "delete_index":
            conn.execute("DELETE FROM artifact_owners WHERE owner_kind='runs'")
        elif change == "delete_guard":
            conn.execute("DELETE FROM artifact_heads")
        else:
            value = {"descriptor_hash": "f" * 64, "descriptor_path": '["no-such-field"]',
                     "owner_priority": 99, "owner_id": "no-such-owner"}[change]
            conn.execute("UPDATE artifact_owners SET " + change + "=? WHERE owner_id=?",
                         (value, run["id"]))
    with pytest.raises(IntegrityError, match="index binding"):
        registry.artifact_owner(entry["id"])


def test_forged_index_even_with_matching_guard_requires_exact_original(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    completed(registry, entry)
    with registry.connection() as conn:
        conn.execute("UPDATE artifact_owners SET descriptor_hash=?", ("f" * 64,))
        forged = dict(conn.execute("SELECT * FROM artifact_owners").fetchone())
        conn.execute("UPDATE artifact_heads SET binding_hash=?", (digest(forged),))
    with pytest.raises(IntegrityError, match="exact owner binding"):
        registry.artifact_owner(entry["id"])


@pytest.mark.parametrize("part", ["request", "result", "audit", "blob"])
def test_fresh_target_corruption_is_detected_on_each_operation(tmp_path, part):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    run = completed(registry, entry)
    assert registry.artifact_owner(entry["id"])["descriptor"] == entry
    with registry.connection() as conn:
        if part in {"request", "result"}:
            conn.execute("DROP TRIGGER immutable_completed_job")
            field = "document" if part == "request" else "result"
            conn.execute("UPDATE jobs SET " + field + "='{}' WHERE id=?", (run["id"],))
        elif part == "audit":
            conn.execute("DROP TRIGGER immutable_audit_update")
            conn.execute("UPDATE audit SET details='{}' WHERE action='runs.succeeded'")
        else:
            (registry.blobs / entry["id"]).write_bytes(b"x" * entry["bytes"])
    with pytest.raises(IntegrityError):
        registry.artifact_owner(entry["id"])


def raw_owner_fixture(tmp_path, storage):
    registry = SQLiteRegistry(tmp_path)
    raw = np.array([[64, 512], [2048, 4095]], dtype=np.uint16)
    stream = io.BytesIO()
    np.save(stream, raw, allow_pickle=False)
    entry = registry.store_blob(stream.getvalue(), "raw.npy", "application/x-npy")
    request = {"input_artifacts": [{**entry, "name": "input-raw.npy"}], "seed": 9}
    if storage == "graph":
        request["source_evidence"] = [{"samples": list(range(4096))}] * 20
        assert len(canonical(request).encode()) > THRESHOLD
    run = registry.submit(request, digest(request), force=True)
    assert registry.claim()["id"] == run["id"]
    run = registry.finish(run["id"], result={"artifacts": [entry], "summary": {"seed": 9}})
    with registry.connection() as conn:
        stored = conn.execute(
            "SELECT document,result FROM jobs WHERE id=?", (run["id"],)).fetchone()
    assert stored["document"].startswith(PREFIX) == (storage == "graph")
    return registry, raw, entry, run, dict(stored)


@pytest.mark.parametrize("storage", ["inline", "graph"])
def test_artifact_owner_reconstructs_one_request_and_preserves_raw_semantics(
    tmp_path, monkeypatch, storage
):
    registry, raw, entry, run, stored = raw_owner_fixture(tmp_path, storage)
    before = originals(registry)
    calls, decode = [], registry.decode_document

    def counted(text):
        calls.append(text)
        return decode(text)

    monkeypatch.setattr(registry, "decode_document", counted)
    for _ in range(2):
        calls.clear()
        binding = registry.artifact_owner(entry["id"])
        assert calls == [stored["document"], stored["result"]]
        assert binding["owner"] == {key: value for key, value in run.items() if key != "artifacts"}
        assert binding["descriptor"] == entry  # Output retains precedence over same-byte input.
        assert binding["kind"] == "runs"
        np.testing.assert_array_equal(np.load(binding["path"], allow_pickle=False), raw)
        # Returned objects are not a trusted cache for the next operation.
        binding["owner"]["input_artifacts"][0]["name"] = "caller edit"
        binding["descriptor"]["name"] = "caller edit"
    assert originals(registry) == before
    assert registry.get_run(run["id"]) == run


@pytest.mark.parametrize("storage", ["inline", "graph"])
@pytest.mark.parametrize("descriptor_kind", ["input", "output"])
def test_nested_descriptor_aliases_preserve_original_owner_semantics(
    tmp_path, monkeypatch, storage, descriptor_kind
):
    registry = SQLiteRegistry(tmp_path)
    entry = {**artifact(registry), "metadata": {"review": ["original"]}}
    request = {"input_artifacts": [entry] if descriptor_kind == "input" else []}
    if storage == "graph":
        request["source_evidence"] = [{"samples": list(range(4096))}] * 20
    run = registry.submit(request, digest(request), force=True)
    registry.claim()
    result = {"artifacts": [entry] if descriptor_kind == "output" else []}
    run = registry.finish(run["id"], result=result)
    before = originals(registry)
    with registry.connection() as conn:
        stored = conn.execute(
            "SELECT document,result FROM jobs WHERE id=?", (run["id"],)).fetchone()
    assert stored["document"].startswith(PREFIX) == (storage == "graph")
    calls, decode = [], registry.decode_document

    def counted(text):
        calls.append(text)
        return decode(text)

    monkeypatch.setattr(registry, "decode_document", counted)
    binding = registry.artifact_owner(entry["id"])
    assert calls == [stored["document"], stored["result"]]
    owner_descriptor = (binding["owner"]["input_artifacts"][0] if descriptor_kind == "input"
                        else binding["owner"]["result"]["artifacts"][0])
    binding["descriptor"]["metadata"]["review"].append("caller edit")
    # Before request reuse, input came from a separate decode; output came from
    # the owner result. Preserve both public alias behaviors (read-efficiency-v0161).
    expected = ["original"] if descriptor_kind == "input" else ["original", "caller edit"]
    assert owner_descriptor["metadata"]["review"] == expected
    assert registry.artifact_owner(entry["id"])["descriptor"] == entry
    assert registry.get_run(run["id"]) == run
    assert originals(registry) == before


@pytest.mark.parametrize("storage", ["inline", "graph"])
@pytest.mark.parametrize("fault", ["request", "blob", "audit", "binding"])
def test_reused_request_keeps_fresh_artifact_integrity_checks(tmp_path, storage, fault):
    registry, _, entry, run, stored = raw_owner_fixture(tmp_path, storage)
    assert registry.artifact_owner(entry["id"])["descriptor"] == entry
    if fault == "request" and storage == "graph":
        reference = json.loads(stored["document"][len(PREFIX):])
        path = registry.blobs / reference["sha256"]
        original = path.read_bytes()
        changed = original.replace(b'"schema"', b'"schemX"', 1)
        assert changed != original and len(changed) == len(original)
        path.write_bytes(changed)
    elif fault == "blob":
        path = registry.blobs / entry["sha256"]
        original = path.read_bytes()
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        assert path.stat().st_size == entry["bytes"]
    else:
        with registry.connection() as conn:
            if fault == "request":
                changed = stored["document"].replace('"seed":9', '"seed":8')
                assert changed != stored["document"] and len(changed) == len(stored["document"])
                conn.execute("DROP TRIGGER immutable_completed_job")
                conn.execute("UPDATE jobs SET document=? WHERE id=?", (changed, run["id"]))
            elif fault == "audit":
                conn.execute("DROP TRIGGER immutable_audit_update")
                conn.execute("UPDATE audit SET event_hash=? WHERE action='runs.succeeded'",
                             ("f" * 64,))
            else:
                conn.execute("UPDATE artifact_owners SET descriptor_hash=? WHERE owner_id=?",
                             ("f" * 64, run["id"]))
                preferred = dict(conn.execute(
                    "SELECT * FROM artifact_owners WHERE artifact_id=? ORDER BY "
                    + registry.ARTIFACT_ORDER + " LIMIT 1", (entry["id"],)).fetchone())
                conn.execute("UPDATE artifact_heads SET binding_hash=? WHERE artifact_id=?",
                             (digest(preferred), entry["id"]))
    with pytest.raises(IntegrityError):
        registry.artifact_owner(entry["id"])


def test_target_record_hash_and_declared_size_are_verified(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    record = registry.put("measurements", {"original_artifact": {**entry, "bytes": 999}})
    with pytest.raises(IntegrityError, match="size differs"):
        registry.artifact_owner(entry["id"])
    with registry.connection() as conn:
        conn.execute("DROP TRIGGER immutable_records_update")
        conn.execute("UPDATE records SET document='{}' WHERE id=?", (record["id"],))
    with pytest.raises(IntegrityError, match="Record integrity"):
        registry.artifact_owner(entry["id"])


def add_unrelated(registry, count, padding):
    with registry.transaction() as conn:
        for number in range(count):
            identity, stamp = f"unrelated-{number}", now()
            request = {"unrelated": "x" * padding, "index": number}
            checksum = digest(request)
            conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", (
                identity, checksum, "failed", canonical(request), checksum,
                stamp, stamp, None, "unrelated",
            ))
            registry.index_job(conn, identity, request, stamp)
            registry._audit(conn, "runs.queued", identity, {"cache_key": checksum})
            registry._audit(conn, "runs.failed", identity, {"result_hash": None,
                                                           "error": "unrelated"})


@pytest.mark.parametrize("history", [10, 100, 1000])
@pytest.mark.parametrize("padding", [32, 16_384])
def test_lookup_decodes_only_fixed_target_with_growing_history(tmp_path, monkeypatch,
                                                               history, padding):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    target = completed(registry, entry)
    calls, real_loads = [], json.loads

    def loads(text, *args, **kwargs):
        calls.append(text)
        return real_loads(text, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(registry_module.json, "loads", loads)
        registry.artifact_owner(entry["id"])
    baseline = (len(calls), sum(len(text) for text in calls))
    add_unrelated(registry, history, padding)
    # An unrelated corrupt completed request must not poison the target lookup.
    with registry.connection() as conn:
        conn.execute("DROP TRIGGER immutable_completed_job")
        conn.execute("UPDATE jobs SET document='broken-json' WHERE id='unrelated-0'")
    calls.clear()
    monkeypatch.setattr(registry_module.json, "loads", loads)
    assert registry.artifact_owner(entry["id"])["owner"]["id"] == target["id"]
    assert (len(calls), sum(len(text) for text in calls)) == baseline
    assert all("unrelated" not in text and "broken-json" not in text for text in calls)


def test_migration_backfills_once_without_rewriting_originals(tmp_path, monkeypatch):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    registry.put("measurements", {"original_artifact": entry})
    run = completed(registry, entry)
    before = originals(registry)
    legacy(registry)
    migrated = SQLiteRegistry(tmp_path)
    assert originals(migrated) == before
    assert migrated.artifact_owner(entry["id"])["owner"]["id"] == run["id"]
    with migrated.connection() as conn:
        assert [r[0] for r in conn.execute("SELECT version FROM schema_version")] == [2]
    monkeypatch.setattr(registry_module.json, "loads",
                        lambda *args, **kwargs: pytest.fail("restart rescanned history"))
    SQLiteRegistry(tmp_path)


def test_failed_migration_rolls_back_and_can_retry_after_source_repair(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    run = completed(registry, entry)
    legacy(registry)
    with registry.connection() as conn:
        original = conn.execute("SELECT document FROM jobs WHERE id=?", (run["id"],)).fetchone()[0]
        conn.execute("DROP TRIGGER immutable_completed_job")
        conn.execute("UPDATE jobs SET document='{}' WHERE id=?", (run["id"],))
    with pytest.raises(IntegrityError, match="Frozen run"):
        SQLiteRegistry(tmp_path)
    with registry.connection() as conn:
        assert [r[0] for r in conn.execute("SELECT version FROM schema_version")] == [1]
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name='artifact_owners'"
        ).fetchall()
        conn.execute("DROP TRIGGER immutable_completed_job")
        conn.execute("UPDATE jobs SET document=? WHERE id=?", (original, run["id"]))
    assert SQLiteRegistry(tmp_path).artifact_owner(entry["id"])["descriptor"] == entry


@pytest.mark.parametrize("version", [1, 2])
def test_backup_restore_accepts_verified_old_and_new_schema(tmp_path, version):
    registry = SQLiteRegistry(tmp_path / "source")
    entry = artifact(registry)
    completed(registry, entry)
    before = originals(registry)
    if version == 1:
        legacy(registry)
    manifest = backup_state(registry.directory, tmp_path / "backup.zip")
    assert manifest["registry"]["schema_version"] == version
    restore_state(tmp_path / "backup.zip", tmp_path / "restored")
    restored = SQLiteRegistry(tmp_path / "restored")
    assert originals(restored) == before
    assert restored.artifact_owner(entry["id"])["descriptor"] == entry


def test_backup_rejects_incomplete_derived_index(tmp_path):
    registry = SQLiteRegistry(tmp_path / "source")
    entry = artifact(registry)
    completed(registry, entry)
    with registry.connection() as conn:
        conn.execute("DELETE FROM artifact_owners")
    with pytest.raises(BackupError, match="owner index"):
        backup_state(registry.directory, tmp_path / "bad.zip")


def test_http_uses_index_returns_original_bytes_and_refuses_orphan(tmp_path, monkeypatch):
    app = create_app(tmp_path / "state", start_worker=False, seed_demo=False)
    registry = app.state.service.registry
    entry = artifact(registry)
    run = completed(registry, entry)
    orphan = registry.store_blob(b"orphan", "orphan", "text/plain")
    monkeypatch.setattr(registry, "list_runs", lambda: pytest.fail("full history scan"))
    monkeypatch.setattr(registry, "list", lambda _kind: pytest.fail("full records scan"))
    with TestClient(app) as client:
        response = client.get(entry["url"])
        assert response.status_code == 200
        assert response.content == b"original target bytes"
        assert "original.bin" in response.headers["content-disposition"]
        assert client.get(orphan["url"]).status_code == 404
        with registry.connection() as conn:
            conn.execute("DROP TRIGGER immutable_completed_job")
            conn.execute("UPDATE jobs SET document='{}' WHERE id=?", (run["id"],))
        assert client.get(entry["url"]).status_code == 409


def test_record_and_index_rehash_cannot_bypass_original_creation_audit(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    record = registry.put("measurements", {"original_artifact": entry})
    with registry.transaction() as conn:
        conn.execute("DROP TRIGGER immutable_records_update")
        document = {k: v for k, v in record.items() if k != "content_hash"}
        document["original_artifact"]["name"] = "forged.bin"
        conn.execute("UPDATE records SET document=?,content_hash=? WHERE id=?",
                     (canonical(document), digest(document), record["id"]))
        registry.index_record(conn, "measurements", document)
    with pytest.raises(IntegrityError, match="creation audit"):
        registry.artifact_owner(entry["id"])


def test_missing_lookup_index_fails_without_history_fallback(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    completed(registry, entry)
    with registry.connection() as conn:
        conn.execute("DROP INDEX artifact_owners_lookup")
    with pytest.raises(IntegrityError, match="index is unavailable"):
        registry.artifact_owner(entry["id"])
    with pytest.raises(IntegrityError, match="indexes are missing"):
        SQLiteRegistry(tmp_path)


def test_all_multi_record_publication_hooks_are_atomic(tmp_path):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    image = registry.publish_spectral_image("fixture", {"name": "asset"}, {"artifacts": [entry]})
    assert registry.artifact_owner(entry["id"])["owner"]["id"] == image["id"]
    request = {"input_artifacts": [entry]}
    study = registry.publish_study({"plan_hash": "fixture", "specification": {},
                                    "source_hashes": {}, "environment": {}}, [], [request])
    assert registry.artifact_owner(entry["id"])["owner"]["id"] == study["run_ids"][0]
    # A malformed descriptor rolls back the original and derived rows together.
    before = originals(registry)
    with pytest.raises(IntegrityError, match="invalid descriptor"):
        registry.publish_spectral_image("invalid", {"name": "asset"}, {"artifacts": [{"id": "x"}]})
    assert originals(registry) == before


@pytest.mark.parametrize("damage", ["chosen_row", "all_rows", "guard", "forged_row"])
@pytest.mark.parametrize("publication", ["record", "job"])
def test_publication_cannot_repair_corrupt_preference_guard(tmp_path, damage, publication):
    registry = SQLiteRegistry(tmp_path)
    entry = artifact(registry)
    registry.put("captures", {"artifacts": [entry]})
    chosen = completed(registry, entry)
    with registry.connection() as conn:
        if damage == "chosen_row":
            conn.execute("DELETE FROM artifact_owners WHERE owner_id=?", (chosen["id"],))
        elif damage == "all_rows":
            conn.execute("DELETE FROM artifact_owners")
        elif damage == "guard":
            conn.execute("DELETE FROM artifact_heads")
        else:
            conn.execute("UPDATE artifact_owners SET descriptor_hash=? WHERE owner_id=?",
                         ("f" * 64, chosen["id"]))
        old_rows = [tuple(row) for row in conn.execute("SELECT * FROM artifact_owners")]
        old_heads = [tuple(row) for row in conn.execute("SELECT * FROM artifact_heads")]
    before = originals(registry)
    with pytest.raises(IntegrityError, match="before publication"):
        if publication == "record":
            registry.put("captures", {"artifacts": [entry]})
        else:
            registry.submit({"input_artifacts": [entry]}, "new-publication")
    assert originals(registry) == before
    with registry.connection() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM artifact_owners")] == old_rows
        assert [tuple(row) for row in conn.execute("SELECT * FROM artifact_heads")] == old_heads
    with pytest.raises(IntegrityError, match="index binding"):
        registry.artifact_owner(entry["id"])
