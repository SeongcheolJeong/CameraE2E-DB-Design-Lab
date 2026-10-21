"""Pinned, complete ISETCam runtime data preparation and startup verification.

No downloader or existing-cache fallback is provided. The complete archive is
authenticated before destination creation, and every launch verifies tree bytes.
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import stat
import tarfile
import time
import unicodedata
from pathlib import Path, PurePosixPath

UPSTREAM_COMMIT = "412b9f9bdb3262f2552b96f0e769b5ad6cdff821"
ARCHIVE_SHA256 = "12a7b97f02e83b2986c1b255d483a8f49fe32063bf5326343cc8a43ec27763a3"
MANIFEST_SHA256 = "e3d0ee848fd69c79921b34776c553dc99d6aa0efd97ca208402106efc65286d1"
BINDING_SCHEMA = "camera_runtime_asset_binding_v1"
MANIFEST_SCHEMA = "camera_runtime_asset_snapshot_v1"
MANIFEST_NAME = "asset-manifest.json"
MAX_ARCHIVE_BYTES = 512 * 1024**2
MAX_TOTAL_BYTES = 768 * 1024**2
MAX_FILE_BYTES = 128 * 1024**2
MAX_MEMBERS = 10000
MAX_MANIFEST_BYTES = 8 * 1024**2
REQUIRED_FILES = ("LICENSE", "README.md")


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _relative(name):
    _require(
        isinstance(name, str) and bool(name) and len(name.encode()) <= 1024,
        "Invalid asset relative path",
    )
    path = PurePosixPath(name)
    _require(
        not path.is_absolute()
        and ".." not in path.parts
        and str(path) == name
        and "\\" not in name
        and not any(ord(c) < 32 or ord(c) == 127 for c in name),
        "Unsafe asset relative path: " + name,
    )
    _require(name not in {".", MANIFEST_NAME}, "Reserved asset relative path")
    return path


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate asset manifest key")
        result[key] = value
    return result


def _walk_error(error):
    raise error


def _manifest_contract(manifest, expected_archive_sha256):
    keys = {
        "schema",
        "upstream_commit",
        "archive_sha256",
        "archive_bytes",
        "archive_root",
        "file_count",
        "directory_count",
        "total_bytes",
        "files",
        "directories",
    }
    _require(isinstance(manifest, dict) and set(manifest) == keys, "Invalid asset manifest fields")
    _require(
        manifest["schema"] == MANIFEST_SCHEMA
        and manifest["upstream_commit"] == UPSTREAM_COMMIT
        and manifest["archive_sha256"] == expected_archive_sha256,
        "Asset manifest differs from the pinned upstream archive",
    )
    _require(
        type(manifest["archive_bytes"]) is int
        and 0 < manifest["archive_bytes"] <= MAX_ARCHIVE_BYTES,
        "Invalid archive byte count",
    )
    _require(
        manifest["archive_root"] == "ISET-isetcam-" + UPSTREAM_COMMIT[:7],
        "Unexpected upstream archive root",
    )
    files, directories = manifest["files"], manifest["directories"]
    _require(
        isinstance(files, list)
        and isinstance(directories, list)
        and 0 < len(files) + len(directories) <= MAX_MEMBERS,
        "Invalid asset member count",
    )
    paths, folded, total = {}, set(), 0
    for name, directory, row in [(d, True, None) for d in directories] + [
        (f.get("path") if isinstance(f, dict) else None, False, f) for f in files
    ]:
        _relative(name)
        alias = unicodedata.normalize("NFC", name).casefold()
        _require(name not in paths and alias not in folded, "Duplicate/case-colliding asset path")
        folded.add(alias)
        paths[name] = directory
        if directory:
            continue
        _require(
            set(row) == {"path", "bytes", "sha256"}
            and type(row["bytes"]) is int
            and 0 <= row["bytes"] <= MAX_FILE_BYTES
            and isinstance(row["sha256"], str)
            and len(row["sha256"]) == 64
            and all(c in "0123456789abcdef" for c in row["sha256"]),
            "Invalid asset file descriptor",
        )
        total += row["bytes"]
    _require(total <= MAX_TOTAL_BYTES, "Asset tree exceeds byte budget")
    for name in paths:
        for parent in PurePosixPath(name).parents:
            if str(parent) != ".":
                _require(
                    paths.get(str(parent)) is True, "Asset parent directory is absent or aliased"
                )
    _require(
        all(paths.get(name) is False for name in REQUIRED_FILES), "Upstream LICENSE/README missing"
    )
    _require(
        files == sorted(files, key=lambda row: row["path"]) and directories == sorted(directories),
        "Asset manifest ordering is not canonical",
    )
    for name, expected in (
        ("file_count", len(files)),
        ("directory_count", len(directories)),
        ("total_bytes", total),
    ):
        _require(
            type(manifest[name]) is int and manifest[name] == expected,
            "Asset manifest totals differ",
        )
    return files, directories


def _validate_binding(binding, expected_archive_sha256, *, require_readonly=True):
    started = time.perf_counter()
    try:
        _require(
            isinstance(binding, dict)
            and set(binding) == {"schema", "root", "manifest_sha256"}
            and binding["schema"] == BINDING_SCHEMA,
            "Invalid asset binding fields",
        )
        value = binding["root"]
        _require(
            isinstance(value, str) and value and Path(value).is_absolute(),
            "Asset root must be canonical absolute path",
        )
        root = Path(value)
        _require(
            str(root.resolve(strict=True)) == value and not root.is_symlink() and root.is_dir(),
            "Asset root is missing, aliased or not canonical",
        )
        for ancestor in [root, *root.parents]:
            _require(not ancestor.is_symlink(), "Symbolic asset root ancestor")
        manifest_path = root / MANIFEST_NAME
        meta = manifest_path.lstat()
        _require(
            stat.S_ISREG(meta.st_mode)
            and meta.st_nlink == 1
            and 0 < meta.st_size <= MAX_MANIFEST_BYTES,
            "Invalid asset manifest file",
        )
        content = manifest_path.read_bytes()
        _require(
            isinstance(binding["manifest_sha256"], str)
            and _digest(content) == binding["manifest_sha256"],
            "Asset manifest hash mismatch",
        )
        if expected_archive_sha256 == ARCHIVE_SHA256:
            _require(
                binding["manifest_sha256"] == MANIFEST_SHA256,
                "Asset inventory is not the complete authenticated upstream manifest",
            )
        manifest = json.loads(content, object_pairs_hook=_no_duplicate_keys)
        files, directories = _manifest_contract(manifest, expected_archive_sha256)
        expected = {row["path"]: row for row in files}
        observed_files, observed_dirs = set(), set()
        identities = set()
        for directory, dirnames, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
            current = Path(directory)
            info = current.lstat()
            _require(stat.S_ISDIR(info.st_mode), "Asset tree contains a non-directory")
            if require_readonly:
                _require(info.st_mode & 0o222 == 0, "Asset directory is writable")
            for name in dirnames:
                child = current / name
                _require(
                    stat.S_ISDIR(child.lstat().st_mode), "Asset tree contains a directory symlink"
                )
                observed_dirs.add(child.relative_to(root).as_posix())
            for name in filenames:
                child = current / name
                relative = child.relative_to(root).as_posix()
                info = child.lstat()
                _require(
                    stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                    "Asset tree contains a link or non-regular file",
                )
                identity = (info.st_dev, info.st_ino)
                _require(identity not in identities, "Asset files alias the same filesystem object")
                identities.add(identity)
                if require_readonly:
                    _require(info.st_mode & 0o222 == 0, "Asset file is writable")
                if relative == MANIFEST_NAME:
                    continue
                row = expected.get(relative)
                _require(
                    row is not None and info.st_size == row["bytes"], "Asset file set/size differs"
                )
                _require(_checksum(child) == row["sha256"], "Asset file hash mismatch: " + relative)
                observed_files.add(relative)
        _require(
            observed_files == set(expected) and observed_dirs == set(directories),
            "Asset tree has extra or missing files/directories",
        )
        return {
            "binding": dict(binding),
            "root": str(root),
            "manifest_path": str(manifest_path),
            "manifest_sha256": binding["manifest_sha256"],
            "file_count": len(files),
            "directory_count": len(directories),
            "total_bytes": manifest["total_bytes"],
            "verification_seconds": time.perf_counter() - started,
            "manifest": manifest,
        }
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RuntimeError("Asset binding verification failed: " + str(error)) from error


def validate_binding(binding):
    """Verify the pinned manifest and every readonly tree byte; return JSON info."""
    return _validate_binding(binding, ARCHIVE_SHA256)


def _prepare(archive, destination, expected_archive_sha256):
    started = time.perf_counter()
    archive, requested = Path(archive), Path(destination)
    _require(archive.is_file() and not archive.is_symlink(), "Select a regular archive file")
    _require(0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES, "Archive exceeds byte budget")
    with archive.open("rb") as stream:
        content = stream.read(MAX_ARCHIVE_BYTES + 1)
    _require(
        len(content) <= MAX_ARCHIVE_BYTES and _digest(content) == expected_archive_sha256,
        "Upstream archive hash mismatch",
    )
    # All extraction reads from these authenticated immutable bytes, never a
    # second file read that could observe a replaced/modified source archive.
    root = requested.absolute()
    _require(not os.path.lexists(root), "Asset destination must be fresh")
    for ancestor in root.parents:
        _require(not ancestor.is_symlink(), "Asset destination has a symbolic ancestor")
    root = root.resolve()
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "upstream_commit": UPSTREAM_COMMIT,
        "archive_sha256": expected_archive_sha256,
        "archive_bytes": len(content),
        "archive_root": "ISET-isetcam-" + UPSTREAM_COMMIT[:7],
    }
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
        members = []
        for member in tar:
            _require(len(members) < MAX_MEMBERS, "Archive exceeds member budget")
            _require(member.size <= MAX_FILE_BYTES, "Archive member exceeds byte budget")
            members.append(member)
        _require(0 < len(members) <= MAX_MEMBERS, "Archive exceeds member budget")
        seen, folded, file_members, directories = set(), set(), {}, set()
        explicit_root = False
        for member in members:
            name = member.name.rstrip("/") if member.isdir() else member.name
            _require(
                (member.isfile() or member.isdir()) and not member.issparse(),
                "Archive links/sparse/special files are forbidden",
            )
            path = _relative(name)
            alias = unicodedata.normalize("NFC", name).casefold()
            _require(
                name not in seen and alias not in folded, "Archive duplicate/case-colliding path"
            )
            seen.add(name)
            folded.add(alias)
            _require(path.parts[0] == manifest["archive_root"], "Archive must have one pinned root")
            if len(path.parts) == 1:
                _require(member.isdir(), "Archive root must be a directory")
                explicit_root = True
                continue
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            _relative(relative)
            if member.isdir():
                directories.add(relative)
            else:
                _require(0 <= member.size <= MAX_FILE_BYTES, "Archive file exceeds byte budget")
                file_members[relative] = member
        _require(explicit_root, "Archive root directory missing")
        for name in [*file_members, *directories]:
            for parent in PurePosixPath(name).parents:
                if str(parent) != ".":
                    directories.add(str(parent))
        _require(not set(file_members) & directories, "Archive file/directory path alias")
        _require(
            sum(m.size for m in file_members.values()) <= MAX_TOTAL_BYTES,
            "Archive exceeds expanded byte budget",
        )
        # Hash all archive file bytes before any destination is written.
        rows = []
        for name, member in sorted(file_members.items()):
            hasher, count = hashlib.sha256(), 0
            with tar.extractfile(member) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    count += len(chunk)
                    hasher.update(chunk)
            _require(count == member.size, "Archive member size differs")
            rows.append({"path": name, "bytes": count, "sha256": hasher.hexdigest()})
        manifest.update(
            files=rows,
            directories=sorted(directories),
            file_count=len(rows),
            directory_count=len(directories),
            total_bytes=sum(row["bytes"] for row in rows),
        )
        _manifest_contract(manifest, expected_archive_sha256)
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir(mode=0o700)
        for name in sorted(directories, key=lambda item: (item.count("/"), item)):
            (root / name).mkdir(mode=0o700)
        for name, member in sorted(file_members.items()):
            with tar.extractfile(member) as source, (root / name).open("xb") as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
        encoded = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
        with (root / MANIFEST_NAME).open("xb") as target:
            target.write(encoded)
    binding = {"schema": BINDING_SCHEMA, "root": str(root), "manifest_sha256": _digest(encoded)}
    _validate_binding(binding, expected_archive_sha256, require_readonly=False)
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    for path in sorted(
        [p for p in root.rglob("*") if p.is_dir()], key=lambda p: len(p.parts), reverse=True
    ):
        path.chmod(0o555)
    root.chmod(0o555)
    result = _validate_binding(binding, expected_archive_sha256)
    result.update(
        preparation_seconds=time.perf_counter() - started,
        exact_authenticated_archive_tree=True,
        license={
            "path": "LICENSE",
            "sha256": next(r["sha256"] for r in rows if r["path"] == "LICENSE"),
        },
        redistribution_authorization="not_assessed",
    )
    return result


def prepare(archive, destination):
    """Prepare a fresh immutable snapshot from the fixed production archive pin."""
    try:
        return _prepare(archive, destination, ARCHIVE_SHA256)
    except (OSError, ValueError, tarfile.TarError) as error:
        raise RuntimeError("Asset snapshot preparation failed: " + str(error)) from error


def bootstrap_verifier_source():
    """Embed exactly the live validator implementation before product imports."""
    imports = (
        "import hashlib,json,os,stat,time,unicodedata\nfrom pathlib import Path,PurePosixPath\n"
    )
    names = (
        "UPSTREAM_COMMIT",
        "ARCHIVE_SHA256",
        "MANIFEST_SHA256",
        "BINDING_SCHEMA",
        "MANIFEST_SCHEMA",
        "MANIFEST_NAME",
        "MAX_ARCHIVE_BYTES",
        "MAX_TOTAL_BYTES",
        "MAX_FILE_BYTES",
        "MAX_MEMBERS",
        "MAX_MANIFEST_BYTES",
        "REQUIRED_FILES",
    )
    constants = "\n".join(name + "=" + repr(globals()[name]) for name in names)
    functions = (
        _require,
        _digest,
        _checksum,
        _relative,
        _no_duplicate_keys,
        _walk_error,
        _manifest_contract,
        _validate_binding,
        validate_binding,
    )
    return imports + constants + "\n" + "\n".join(inspect.getsource(f) for f in functions)


BOOTSTRAP_ASSET_VERIFY_CODE = bootstrap_verifier_source()
