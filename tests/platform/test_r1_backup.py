"""R1 project mode survives a verified backup without changing legacy v1 archives."""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from camera_platform import backup, project_mode
from camera_platform.backup import BackupError, backup_state, restore_state
from camera_platform.registry import SQLiteRegistry, digest


def _read_archive(path):
    with zipfile.ZipFile(path) as archive:
        return {item.filename: archive.read(item) for item in archive.infolist()}


def _write_archive(path, files):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _rehash_manifest(files):
    manifest = json.loads(files["manifest.json"])
    for descriptor in manifest["files"]:
        content = files[descriptor["path"]]
        descriptor["sha256"] = hashlib.sha256(content).hexdigest()
        descriptor["bytes"] = len(content)
    manifest.pop("manifest_hash")
    manifest["manifest_hash"] = digest(manifest)
    files["manifest.json"] = json.dumps(manifest).encode()


def _state(tmp_path, *, r1):
    state = tmp_path / "source"
    if r1:
        project_mode.configure(state, project_id="r1-example")
    registry = SQLiteRegistry(state)
    raw = registry.store_blob(b"original R1 RAW bytes", "raw.bin", "application/octet-stream")
    registry.put("captures", {"frames": [{"raw": raw}]})
    return state, registry, raw


def test_r1_v2_roundtrip_restores_project_marker_and_raw(tmp_path):
    state, registry, raw = _state(tmp_path, r1=True)
    original = (state / project_mode.FILENAME).read_bytes()
    archive = tmp_path / "r1.zip"
    manifest = backup_state(state, archive, expected_project_id="r1-example")
    assert manifest["format_version"] == 2
    assert manifest["project"] == project_mode.read(state)
    entries = _read_archive(archive)
    assert entries[project_mode.FILENAME] == original
    assert hashlib.sha256(original).hexdigest() == next(
        item["sha256"] for item in manifest["files"]
        if item["path"] == project_mode.FILENAME
    )
    destination = tmp_path / "restored"
    report = restore_state(archive, destination, expected_project_id="r1-example")
    assert report["project"] == manifest["project"]
    assert (destination / project_mode.FILENAME).read_bytes() == original
    assert project_mode.read(destination)["project_id"] == "r1-example"
    restored = SQLiteRegistry(destination)
    assert restored.list("captures") == registry.list("captures")
    assert restored.blob_path(raw["sha256"]).read_bytes() == b"original R1 RAW bytes"


def test_unmarked_legacy_backup_stays_v1_and_restores_without_marker(tmp_path):
    state, registry, raw = _state(tmp_path, r1=False)
    archive = tmp_path / "legacy.zip"
    manifest = backup_state(state, archive)
    assert manifest["format_version"] == 1 and "project" not in manifest
    assert project_mode.FILENAME not in _read_archive(archive)
    destination = tmp_path / "legacy-restored"
    report = restore_state(archive, destination)
    assert "project" not in report
    assert project_mode.read(destination) is None
    assert SQLiteRegistry(destination).blob_path(raw["sha256"]).read_bytes() == (
        registry.blob_path(raw["sha256"]).read_bytes()
    )
    with pytest.raises(BackupError, match="expected R1 project"):
        restore_state(archive, tmp_path / "not-r1", expected_project_id="r1-example")


@pytest.mark.parametrize("mutation", ["missing", "invalid", "symlink"])
def test_r1_source_marker_missing_invalid_or_symlink_never_publishes_backup(tmp_path, mutation):
    state, _, _ = _state(tmp_path, r1=True)
    marker = state / project_mode.FILENAME
    marker.unlink()
    if mutation == "invalid":
        marker.write_text('{"schema":"camera_platform_project_mode_v1","mode":"r1_engineering"}')
    elif mutation == "symlink":
        outside = tmp_path / "marker-target"
        outside.write_text("{}")
        marker.symlink_to(outside)
    archive = tmp_path / "rejected.zip"
    with pytest.raises(BackupError):
        backup_state(state, archive, expected_project_id="r1-example")
    assert not archive.exists()


@pytest.mark.parametrize("mutation", ["missing", "corrupt", "different_project"])
def test_v2_archive_marker_mutation_fails_before_destination_exists(tmp_path, mutation):
    state, _, _ = _state(tmp_path, r1=True)
    source = tmp_path / "valid.zip"
    backup_state(state, source, expected_project_id="r1-example")
    files = _read_archive(source)
    if mutation == "missing":
        del files[project_mode.FILENAME]
        manifest = json.loads(files["manifest.json"])
        manifest["files"] = [item for item in manifest["files"]
                             if item["path"] != project_mode.FILENAME]
        manifest.pop("manifest_hash")
        manifest["manifest_hash"] = digest(manifest)
        files["manifest.json"] = json.dumps(manifest).encode()
    elif mutation == "corrupt":
        files[project_mode.FILENAME] = b"not JSON"
        _rehash_manifest(files)
    else:
        marker = json.loads(files[project_mode.FILENAME])
        marker["project_id"] = "another-project"
        files[project_mode.FILENAME] = json.dumps(marker).encode()
        _rehash_manifest(files)
    changed = tmp_path / "changed.zip"
    _write_archive(changed, files)
    destination = tmp_path / "unpublished"
    with pytest.raises(BackupError):
        restore_state(changed, destination, expected_project_id="r1-example")
    assert not destination.exists()


def test_source_marker_change_during_snapshot_is_rejected(tmp_path, monkeypatch):
    state, _, _ = _state(tmp_path, r1=True)
    actual = backup._snapshot_database

    def changed(source, target):
        actual(source, target)
        marker = state / project_mode.FILENAME
        data = json.loads(marker.read_text())
        data["project_id"] = "changed-project"
        marker.write_text(json.dumps(data))

    monkeypatch.setattr(backup, "_snapshot_database", changed)
    archive = tmp_path / "changed-during-backup.zip"
    with pytest.raises(BackupError, match="changed"):
        backup_state(state, archive, expected_project_id="r1-example")
    assert not archive.exists()


def test_r1_backup_and_restore_reject_wrong_project_expectation(tmp_path):
    state, _, _ = _state(tmp_path, r1=True)
    with pytest.raises(BackupError, match="another project"):
        backup_state(state, tmp_path / "wrong.zip", expected_project_id="r1-other")
    archive = tmp_path / "valid.zip"
    backup_state(state, archive, expected_project_id="r1-example")
    destination = tmp_path / "wrong-restored"
    with pytest.raises(BackupError, match="expected R1 project"):
        restore_state(archive, destination, expected_project_id="r1-other")
    assert not destination.exists()
