"""Exact storage-budget parity for the faster uncached directory traversal."""

import os
from pathlib import Path

import pytest

from camera_platform import design_workshop_storage as storage


def previous_usage(path):
    total = 0
    for entry in Path(path).rglob("*"):
        if entry.is_symlink():
            raise ValueError("Project storage may not contain symbolic links.")
        if entry.is_file():
            metadata = entry.stat()
            total += max(metadata.st_size, getattr(metadata, "st_blocks", 0) * 512)
    return total


def test_exact_allocated_logical_nested_and_hardlink_parity(tmp_path):
    nested = tmp_path / "nested" / "deeper"
    nested.mkdir(parents=True)
    (tmp_path / "empty").mkdir()
    (tmp_path / "small.bin").write_bytes(b"allocation" * 11)
    (nested / ".hidden.bin").write_bytes(b"hidden")
    sparse = nested / "sparse.bin"
    with sparse.open("wb") as stream:
        stream.truncate(1024 * 1024)
    os.link(tmp_path / "small.bin", nested / "hardlink.bin")
    files = [tmp_path / "small.bin", nested / ".hidden.bin", sparse, nested / "hardlink.bin"]
    expected = sum(max(p.stat().st_size, p.stat().st_blocks * 512) for p in files)
    assert storage.usage(tmp_path) == previous_usage(tmp_path) == expected
    # A second call must see growth; no cached total/stat from the earlier check.
    (nested / "later.bin").write_bytes(b"new" * 8192)
    assert storage.usage(tmp_path) == previous_usage(tmp_path) > expected


@pytest.mark.parametrize("target_kind", ["file", "directory", "missing"])
def test_symlink_entries_are_rejected(tmp_path, target_kind):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "target"
    if target_kind == "file":
        target.write_text("outside project")
    elif target_kind == "directory":
        target.mkdir()
        (target / "outside.bin").write_bytes(b"outside")
    (project / "link").symlink_to(target, target_is_directory=target_kind == "directory")
    with pytest.raises(ValueError, match="symbolic links"):
        storage.usage(project)
    with pytest.raises(ValueError, match="symbolic links"):
        previous_usage(project)


def test_root_semantics_and_special_files(tmp_path):
    assert storage.usage(tmp_path / "missing") == previous_usage(tmp_path / "missing") == 0
    regular = tmp_path / "regular.bin"
    regular.write_text("not a directory")
    assert storage.usage(regular) == previous_usage(regular) == 0
    os.mkfifo(tmp_path / "pipe")
    assert storage.usage(tmp_path) == previous_usage(tmp_path)
    alias = tmp_path.parent / (tmp_path.name + "-alias")
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        storage.usage(alias)


def test_unreadable_directory_error_is_not_silently_counted_as_zero(tmp_path, monkeypatch):
    def denied(_):
        raise PermissionError("fixture directory cannot be read")

    monkeypatch.setattr(storage.os, "scandir", denied)
    with pytest.raises(PermissionError, match="cannot be read"):
        storage.usage(tmp_path)


def test_internal_alias_opt_in_counts_physical_target_once(tmp_path):
    target=tmp_path/'target';target.mkdir();(target/'data').write_bytes(b'1234')
    before=storage.usage(tmp_path)
    alias=tmp_path/'current';alias.symlink_to(target,target_is_directory=True)
    link=alias.lstat()
    assert storage.usage(tmp_path,allow_internal_aliases=True)==before+max(link.st_size,link.st_blocks*512)
    with pytest.raises(ValueError):storage.usage(tmp_path)


def test_alias_opt_in_still_rejects_external_target(tmp_path):
    root=tmp_path/'project';root.mkdir()
    (root/'outside').symlink_to(tmp_path)
    with pytest.raises(ValueError,match='escapes'):storage.usage(root,allow_internal_aliases=True)
