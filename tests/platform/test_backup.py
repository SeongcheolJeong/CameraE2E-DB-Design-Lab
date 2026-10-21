"""Backup/restore must preserve evidence and reject unverified state atomically."""

import hashlib
import json
import os
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest

from camera_platform import backup
from camera_platform.backup import BackupError, backup_state, restore_state
from camera_platform.registry import SQLiteRegistry, canonical, digest, now


@pytest.fixture
def populated(tmp_path):
    registry = SQLiteRegistry(tmp_path / "source")
    raw = b'{\r\n  "original": "measurement bytes"\r\n}\n'
    artifact = registry.store_blob(raw, "original.json", "application/json")
    image = registry.store_blob(bytes(range(256)) * 8, "raw.bin", "application/octet-stream")
    orphan = registry.store_blob(b"not referenced by a committed record", "orphan", "text/plain")
    registry.put("assets", {"name": "Pinned camera"}, identity="asset:camera")
    registry.put("measurements", {"original_artifact": artifact, "values": [0.5]})
    registry.put("captures", {"sessions": [{"frames": [{"raw": image}]}]})
    successful = registry.submit({"request": {"seed": 42}, "input": artifact}, "success-key")
    registry.claim()
    registry.finish(successful["id"], result={"summary": {"mean": 0.5}, "artifacts": [image]})
    failed = registry.submit({"request": "failed"}, "failed-key")
    registry.claim()
    registry.finish(failed["id"], error="Solver failed")
    cancelled = registry.submit({"request": "cancelled"}, "cancel-key")
    registry.cancel(cancelled["id"])
    registry.submit({"request": "interrupted"}, "interrupted-key")
    registry.claim()
    registry.recover_interrupted()
    running = registry.submit({"request": "still running"}, "running-key")
    registry.claim()
    queued = registry.submit({"request": "queued"}, "queued-key")
    return {
        "registry": registry,
        "raw": raw,
        "artifact": artifact,
        "image": image,
        "orphan": orphan,
        "running": running,
        "queued": queued,
        "succeeded": successful,
    }


def read_zip(path):
    with zipfile.ZipFile(path) as archive:
        return {entry.filename: archive.read(entry) for entry in archive.infolist()}


def write_zip(path, entries):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in entries.items():
            archive.writestr(name, contents)


def rehash_manifest(entries):
    manifest = json.loads(entries["manifest.json"])
    for descriptor in manifest["files"]:
        content = entries[descriptor["path"]]
        descriptor.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    manifest.pop("manifest_hash")
    manifest["manifest_hash"] = digest(manifest)
    entries["manifest.json"] = json.dumps(manifest).encode()


def test_roundtrip_preserves_original_bytes_nested_artifacts_audit_and_active_jobs(
    populated, tmp_path
):
    source = populated["registry"]
    original_audit, original_runs = source.audit(), source.list_runs()
    archive, destination = tmp_path / "backup.zip", tmp_path / "restored"
    manifest = backup_state(source.directory, archive)
    entries = read_zip(archive)
    assert set(entries) == {
        "manifest.json",
        "registry.sqlite",
        "blobs/" + populated["artifact"]["sha256"],
        "blobs/" + populated["image"]["sha256"],
    }
    assert "blobs/" + populated["orphan"]["sha256"] not in entries
    assert entries["blobs/" + populated["artifact"]["sha256"]] == populated["raw"]
    assert source.audit() == original_audit
    assert source.list_runs() == original_runs
    report = restore_state(archive, destination)
    assert report["integrity"] == "verified"
    assert report["manifest_hash"] == manifest["manifest_hash"]
    assert report["workers_started"] is False
    restored = SQLiteRegistry(destination)
    assert restored.audit() == original_audit
    assert restored.list_runs() == original_runs
    for kind in ("assets", "measurements", "captures"):
        assert restored.list(kind) == source.list(kind)
    assert restored.blob_path(populated["artifact"]["sha256"]).read_bytes() == populated["raw"]
    assert restored.get_run(populated["running"]["id"])["status"] == "running"
    assert restored.get_run(populated["queued"]["id"])["status"] == "queued"
    assert report["registry"]["active_jobs"] == {
        "queued": [populated["queued"]["id"]],
        "running": [populated["running"]["id"]],
    }
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700


def test_live_wal_snapshot_includes_commits_and_excludes_an_in_progress_transaction(tmp_path):
    registry = SQLiteRegistry(tmp_path / "live")
    with registry.connection() as keep_wal_open:
        keep_wal_open.execute("SELECT count(*) FROM records").fetchone()
        committed = registry.put("assets", {"name": "Committed WAL revision"})
        assert Path(str(registry.path) + "-wal").is_file()
        # A writer remains active throughout backup. Snapshotting must neither
        # block it for the complete blob copy nor include half a record/audit pair.
        with registry.transaction() as writer:
            stamp = now()
            document = {"id": "asset:uncommitted", "name": "Pending", "created_at": stamp}
            writer.execute(
                "INSERT INTO records VALUES(?,?,?,?,?)",
                ("assets", document["id"], canonical(document), digest(document), stamp),
            )
            registry._audit(
                writer, "assets.created", document["id"], {"content_hash": digest(document)}
            )
            backup_state(registry.directory, tmp_path / "live.zip")
            assert keep_wal_open.execute("SELECT count(*) FROM records").fetchone()[0] == 1
        assert len(registry.list("assets")) == 2
    restore_state(tmp_path / "live.zip", tmp_path / "snapshot")
    restored = SQLiteRegistry(tmp_path / "snapshot")
    assert restored.list("assets") == [committed]
    assert len(restored.audit()["events"]) == 1


@pytest.mark.parametrize("existing", ["empty", "live", "symlink"])
def test_restore_refuses_any_existing_destination(populated, tmp_path, existing):
    archive = tmp_path / "backup.zip"
    backup_state(populated["registry"].directory, archive)
    destination = tmp_path / "destination"
    if existing == "symlink":
        destination.symlink_to(populated["registry"].directory, target_is_directory=True)
    else:
        destination.mkdir()
        if existing == "live":
            (destination / "sentinel").write_text("must survive")
    with pytest.raises(FileExistsError):
        restore_state(archive, destination)
    if existing == "live":
        assert (destination / "sentinel").read_text() == "must survive"
    assert os.path.lexists(destination)


def test_atomic_publication_refuses_destination_created_during_verification(
    populated, tmp_path, monkeypatch
):
    archive, destination = tmp_path / "backup.zip", tmp_path / "raced"
    backup_state(populated["registry"].directory, archive)
    real_publish = backup._rename_no_replace

    def publish_with_race(source, target):
        target.mkdir()
        real_publish(source, target)

    monkeypatch.setattr(backup, "_rename_no_replace", publish_with_race)
    with pytest.raises(FileExistsError):
        restore_state(archive, destination)
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert not list(tmp_path.glob(".raced.restore-*"))


@pytest.mark.parametrize(
    "path", ["../outside", "/absolute", "blobs/../outside", "blobs\\outside", "./registry.sqlite"]
)
def test_restore_rejects_archive_path_traversal(populated, tmp_path, path):
    archive = tmp_path / "unsafe.zip"
    backup_state(populated["registry"].directory, archive)
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr(path, b"payload")
    with pytest.raises(BackupError, match="path|member"):
        restore_state(archive, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("kind", ["duplicate", "symlink", "directory"])
def test_restore_rejects_duplicate_and_nonregular_zip_members(populated, tmp_path, kind):
    archive = tmp_path / "unsafe.zip"
    backup_state(populated["registry"].directory, archive)
    if kind == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            with zipfile.ZipFile(archive, "a") as bundle:
                bundle.writestr("registry.sqlite", b"shadow")
    else:
        entries = read_zip(archive)
        with zipfile.ZipFile(archive, "w") as bundle:
            for name, content in entries.items():
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (
                    (stat.S_IFLNK if kind == "symlink" else stat.S_IFDIR) | 0o700
                ) << 16
                bundle.writestr(info if name == "registry.sqlite" else name, content)
    with pytest.raises(BackupError, match="Duplicate|non-regular"):
        restore_state(archive, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("mutation", ["blob", "manifest", "missing_blob"])
def test_restore_rejects_outer_integrity_corruption(populated, tmp_path, mutation):
    archive = tmp_path / "corrupt.zip"
    backup_state(populated["registry"].directory, archive)
    entries = read_zip(archive)
    blob = "blobs/" + populated["artifact"]["sha256"]
    if mutation == "blob":
        entries[blob] = b"X" * len(entries[blob])
    elif mutation == "manifest":
        manifest = json.loads(entries["manifest.json"])
        manifest["registry"]["audit_head"] = "0" * 64
        entries["manifest.json"] = json.dumps(manifest).encode()
    else:
        del entries[blob]
    write_zip(archive, entries)
    with pytest.raises(BackupError):
        restore_state(archive, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()
    assert not list(tmp_path.glob(".rejected.restore-*"))


@pytest.mark.parametrize("mutation", ["schema", "audit", "record", "result", "status"])
def test_rehashed_archive_still_rejects_database_semantic_corruption(populated, tmp_path, mutation):
    archive = tmp_path / "semantic-corruption.zip"
    backup_state(populated["registry"].directory, archive)
    entries = read_zip(archive)
    database = tmp_path / "edited.sqlite"
    database.write_bytes(entries["registry.sqlite"])
    with sqlite3.connect(database) as connection:
        trigger = {
            "schema": "immutable_audit_update",
            "audit": "immutable_audit_update",
            "record": "immutable_records_update",
            "result": "immutable_completed_job",
            "status": "immutable_completed_job",
        }[mutation]
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (trigger,)
        ).fetchone()[0]
        connection.execute("DROP TRIGGER " + trigger)
        if mutation == "audit":
            connection.execute("UPDATE audit SET details='{}' WHERE seq=1")
        elif mutation == "record":
            connection.execute("UPDATE records SET document='{}' WHERE kind='assets'")
        elif mutation == "result":
            connection.execute("UPDATE jobs SET result='{}' WHERE status='succeeded'")
        elif mutation == "status":
            connection.execute("UPDATE jobs SET status='succeeded' WHERE status='queued'")
        if mutation != "schema":
            connection.execute(original_sql)
    entries["registry.sqlite"] = database.read_bytes()
    rehash_manifest(entries)  # Defeat simple archive checks intentionally.
    write_zip(archive, entries)
    with pytest.raises(BackupError, match="schema|Audit|Record|result|status"):
        restore_state(archive, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_backup_rejects_changed_or_missing_referenced_blob_without_publishing(populated, tmp_path):
    path = populated["registry"].blobs / populated["artifact"]["sha256"]
    path.write_bytes(b"X" * len(populated["raw"]))
    with pytest.raises(BackupError, match="checksum"):
        backup_state(populated["registry"].directory, tmp_path / "not-created.zip")
    assert not (tmp_path / "not-created.zip").exists()
    path.unlink()
    with pytest.raises(BackupError, match="missing"):
        backup_state(populated["registry"].directory, tmp_path / "not-created.zip")
    assert not (tmp_path / "not-created.zip").exists()


@pytest.mark.parametrize(
    "limit",
    [
        "MAX_ARCHIVE_BYTES",
        "MAX_TOTAL_BYTES",
        "MAX_MEMBER_BYTES",
        "MAX_ENTRIES",
        "MAX_MANIFEST_BYTES",
    ],
)
def test_restore_applies_declared_resource_limits_before_publication(
    populated, tmp_path, monkeypatch, limit
):
    archive = tmp_path / "backup.zip"
    backup_state(populated["registry"].directory, archive)
    monkeypatch.setattr(backup, limit, 1)
    with pytest.raises(BackupError, match="limit"):
        restore_state(archive, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_backup_never_replaces_an_existing_file(populated, tmp_path):
    archive = tmp_path / "existing.zip"
    archive.write_bytes(b"existing backup")
    with pytest.raises(FileExistsError):
        backup_state(populated["registry"].directory, archive)
    assert archive.read_bytes() == b"existing backup"
