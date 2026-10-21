"""Offline archive safety and full-tree startup integrity with tiny authored fixtures."""

import hashlib
import importlib.util
import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools/camera_runtime_assets.py"
spec = importlib.util.spec_from_file_location("runtime_assets_under_test", TOOL)
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)
PREFIX = "ISET-isetcam-" + assets.UPSTREAM_COMMIT[:7]


def archive(tmp_path, extras=()):
    path = tmp_path / "authored-fixture.tar.gz"
    records = [
        (PREFIX, "dir", b""),
        (PREFIX + "/LICENSE", "file", b"fixture license\n"),
        (PREFIX + "/README.md", "file", "fixture 한글\n".encode()),
        (PREFIX + "/data", "dir", b""),
        (PREFIX + "/data/pixels.bin", "file", b"pixels"),
        (PREFIX + "/empty", "dir", b""),
        *extras,
    ]
    with tarfile.open(path, "w:gz") as output:
        for name, kind, content in records:
            entry = tarfile.TarInfo(name)
            entry.type = {
                "dir": tarfile.DIRTYPE,
                "file": tarfile.REGTYPE,
                "symlink": tarfile.SYMTYPE,
                "hardlink": tarfile.LNKTYPE,
                "fifo": tarfile.FIFOTYPE,
            }[kind]
            entry.mode = 0o755 if kind == "dir" else 0o644
            if kind in {"symlink", "hardlink"}:
                entry.linkname = content.decode()
            if kind == "file":
                entry.size = len(content)
                output.addfile(entry, io.BytesIO(content))
            else:
                output.addfile(entry)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def prepared(tmp_path):
    path, checksum = archive(tmp_path)
    result = assets._prepare(path, tmp_path / "snapshot", checksum)
    yield result, checksum
    root = Path(result["root"])
    root.chmod(0o755)
    for child in root.rglob("*"):
        if not child.is_symlink():
            child.chmod(0o755 if child.is_dir() else 0o644)


def test_roundtrip_exact_archive_manifest_readonly_and_identical_bootstrap(prepared):
    result, checksum = prepared
    root = Path(result["root"])
    assert result["file_count"] == 3 and result["directory_count"] == 2
    assert result["exact_authenticated_archive_tree"] is True
    assert (root / "data/pixels.bin").read_bytes() == b"pixels"
    assert all(stat.S_IMODE(p.stat().st_mode) & 0o222 == 0 for p in [root, *root.rglob("*")])
    checked = assets._validate_binding(result["binding"], checksum)
    assert checked["manifest"] == result["manifest"]
    code = assets.BOOTSTRAP_ASSET_VERIFY_CODE.replace(
        "ARCHIVE_SHA256=" + repr(assets.ARCHIVE_SHA256), "ARCHIVE_SHA256=" + repr(checksum)
    ).replace(
        "MANIFEST_SHA256=" + repr(assets.MANIFEST_SHA256),
        "MANIFEST_SHA256=" + repr(result["manifest_sha256"]),
    )
    namespace = {}
    exec(compile(code, "<authored-fixture-bootstrap>", "exec"), namespace)
    assert namespace["validate_binding"](result["binding"])["manifest"] == result["manifest"]
    # A fixture inventory cannot acquire production-upstream authority.
    with pytest.raises(RuntimeError, match="complete authenticated"):
        assets.validate_binding(result["binding"])


@pytest.mark.parametrize(
    "name,kind,content",
    [
        ("/absolute", "file", b"x"),
        (PREFIX + "/../escape", "file", b"x"),
        (PREFIX + "/a//b", "file", b"x"),
        (PREFIX + "/a\\b", "file", b"x"),
        (PREFIX + "/LICENSE", "file", b"duplicate"),
        (PREFIX + "/license", "file", b"case collision"),
        (PREFIX + "/other", "symlink", b"LICENSE"),
        (PREFIX + "/other", "hardlink", (PREFIX + "/LICENSE").encode()),
        (PREFIX + "/pipe", "fifo", b""),
        ("Other-root/file", "file", b"x"),
        (PREFIX + "/asset-manifest.json", "file", b"{}"),
        (PREFIX + "/data/pixels.bin/child", "file", b"file parent alias"),
    ],
)
def test_unsafe_archive_is_rejected_before_destination_write(tmp_path, name, kind, content):
    path, checksum = archive(tmp_path, [(name, kind, content)])
    destination = tmp_path / "rejected"
    with pytest.raises(RuntimeError):
        assets._prepare(path, destination, checksum)
    assert not destination.exists()


def test_wrong_archive_pin_and_existing_destination_fail_without_writes(tmp_path):
    path, checksum = archive(tmp_path)
    with pytest.raises(RuntimeError, match="hash mismatch"):
        assets.prepare(path, tmp_path / "wrong")
    assert not (tmp_path / "wrong").exists()
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "preserve").write_text("unchanged")
    with pytest.raises(RuntimeError, match="fresh"):
        assets._prepare(path, destination, checksum)
    assert (destination / "preserve").read_text() == "unchanged"


@pytest.mark.parametrize("mutation", ["changed", "deleted", "extra", "symlink", "hardlink"])
def test_full_tree_changes_are_refused(prepared, mutation):
    result, checksum = prepared
    root = Path(result["root"])
    target = root / "data/pixels.bin"
    if mutation == "changed":
        target.chmod(0o644)
        target.write_bytes(b"Pixels")
        target.chmod(0o444)
    elif mutation == "deleted":
        target.parent.chmod(0o755)
        target.unlink()
        target.parent.chmod(0o555)
    else:
        root.chmod(0o755)
        if mutation == "extra":
            (root / "extra").write_bytes(b"x")
            (root / "extra").chmod(0o444)
        elif mutation == "symlink":
            (root / "extra").symlink_to(target)
        else:
            os.link(target, root / "extra")
        root.chmod(0o555)
    with pytest.raises(RuntimeError):
        assets._validate_binding(result["binding"], checksum)


@pytest.mark.parametrize("change", ["extra", "hash", "relative", "alias", "schema"])
def test_binding_requires_exact_canonical_identity(prepared, change):
    result, checksum = prepared
    binding = result["binding"].copy()
    if change == "extra":
        binding["unchecked"] = True
    elif change == "hash":
        binding["manifest_sha256"] = "0" * 64
    elif change == "relative":
        binding["root"] = "relative/root"
    elif change == "alias":
        binding["root"] += "/."
    else:
        binding["schema"] = "unverified"
    with pytest.raises(RuntimeError):
        assets._validate_binding(binding, checksum)


def test_writable_data_and_manifest_total_alias_refused(prepared):
    result, checksum = prepared
    root = Path(result["root"])
    file = root / "LICENSE"
    file.chmod(0o644)
    with pytest.raises(RuntimeError, match="writable"):
        assets._validate_binding(result["binding"], checksum)
    file.chmod(0o444)
    path = root / assets.MANIFEST_NAME
    path.chmod(0o644)
    manifest = json.loads(path.read_bytes())
    manifest["file_count"] = True
    path.write_text(json.dumps(manifest))
    path.chmod(0o444)
    binding = {
        **result["binding"],
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    with pytest.raises(RuntimeError, match="totals"):
        assets._validate_binding(binding, checksum)


def test_expanded_byte_budget_is_checked_before_writing(tmp_path, monkeypatch):
    path, checksum = archive(tmp_path)
    monkeypatch.setattr(assets, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(RuntimeError, match="byte budget"):
        assets._prepare(path, tmp_path / "too-large", checksum)
    assert not (tmp_path / "too-large").exists()


@pytest.mark.parametrize("limit", ["MAX_MEMBERS", "MAX_FILE_BYTES"])
def test_archive_inventory_budgets_refuse_before_writing(tmp_path, monkeypatch, limit):
    path, checksum = archive(tmp_path)
    monkeypatch.setattr(assets, limit, 1)
    with pytest.raises(RuntimeError, match="budget"):
        assets._prepare(path, tmp_path / "limited", checksum)
    assert not (tmp_path / "limited").exists()
