"""Verified, local-only snapshots of the single-host registry and referenced blobs.

No source mutation, worker startup, recovery transition, external transmission, or
overwrite occurs here. Restore publishes a verified staging directory atomically.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from . import __version__, project_mode
from .registry import IntegrityError, SQLiteRegistry, canonical, digest, now

SCHEMA = "camera_platform_backup_v1"
MAX_ARCHIVE_BYTES = 16 * 1024**3
MAX_TOTAL_BYTES = 16 * 1024**3
MAX_MEMBER_BYTES = 4 * 1024**3
MAX_ENTRIES = 100_000
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_SNAPSHOT_SECONDS = 120
CHUNK_BYTES = 1024**2
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}


class BackupError(IntegrityError):
    """An unsupported or unverified snapshot must not be published."""


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BackupError("Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(value):
        raise BackupError(f"Non-finite JSON value: {value}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise BackupError("Invalid snapshot JSON") from error


def _hash_file(path):
    checksum, size = hashlib.sha256(), 0
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"Expected a regular file: {path.name}")
    with path.open("rb") as stream:
        while block := stream.read(CHUNK_BYTES):
            size += len(block)
            if size > MAX_MEMBER_BYTES:
                raise BackupError("Snapshot member size limit exceeded")
            checksum.update(block)
    return {"sha256": checksum.hexdigest(), "bytes": size}


def _project_marker(path):
    """Apply the same exact marker contract that API and Worker enforce."""
    try:
        marker = project_mode.read(path.parent)
    except ValueError as error:
        raise BackupError("Invalid R1 project mode marker") from error
    if marker is None:
        raise BackupError("R1 project mode marker is missing")
    return marker


def _freeze_project_marker(source, stage):
    """Copy one bounded regular marker; recheck source before publishing ZIP."""
    original = source / project_mode.FILENAME
    if not os.path.lexists(original):
        return None
    if original.is_symlink() or not original.is_file():
        raise BackupError("R1 project mode marker must be a regular file")
    target = stage / project_mode.FILENAME
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(original, flags)
        with os.fdopen(descriptor, "rb") as reader:
            if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                raise BackupError("R1 project mode marker must be a regular file")
            content = reader.read(2049)
    except OSError as error:
        raise BackupError("Cannot read R1 project mode marker") from error
    if len(content) > 2048:
        raise BackupError("R1 project mode marker exceeds its size limit")
    with target.open("xb") as writer:
        writer.write(content)
    marker = _project_marker(target)
    if _hash_file(original) != _hash_file(target):
        raise BackupError("R1 project mode marker changed while taking the backup")
    return marker


def _readonly(path, *, immutable=True):
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    connection = sqlite3.connect(uri, uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _schema_signature(connection):
    return [
        {
            "type": row["type"],
            "name": row["name"],
            "table": row["tbl_name"],
            "sql": re.sub(r"\s+", " ", row["sql"] or "").strip(),
        }
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )
    ]


def _expected_schema(version=2):
    # Use this installed registry's schema only in a disposable empty directory.
    # Opening an untrusted snapshot through SQLiteRegistry would recreate missing
    # triggers and conceal schema corruption, so verification never does that.
    with tempfile.TemporaryDirectory(prefix="camera-backup-schema-") as directory:
        registry = SQLiteRegistry(directory)
        with registry.connection() as connection:
            signature = _schema_signature(connection)
            if version == 1:
                signature = [row for row in signature if row["table"] not in {
                    "artifact_owners", "artifact_heads"
                } and row["name"] != "audit_subject_action"]
            return signature


def _snapshot_database(source, target):
    start = time.monotonic()
    page_size = 0

    def progress(_status, _remaining, _total):
        if time.monotonic() - start > MAX_SNAPSHOT_SECONDS:
            raise BackupError("SQLite snapshot time limit exceeded; retry with fewer writes")
        if _total * page_size > MAX_MEMBER_BYTES:
            raise BackupError("Snapshot database size limit exceeded")

    with closing(_readonly(source, immutable=False)) as reader:
        page_size = reader.execute("PRAGMA page_size").fetchone()[0]
        with closing(sqlite3.connect(target)) as writer:
            reader.backup(writer, pages=256, progress=progress, sleep=0.01)
            # The archive is one complete database; no WAL/SHM sidecars are needed.
            writer.execute("PRAGMA journal_mode=DELETE")


def _artifact_references(value, references):
    # Future record kinds can carry descriptors at arbitrary nesting levels.
    if isinstance(value, dict):
        if "sha256" in value and any(
            key in value for key in ("bytes", "media_type", "url", "name")
        ):
            checksum, size = value.get("sha256"), value.get("bytes")
            if (
                not isinstance(checksum, str)
                or not _HASH.fullmatch(checksum)
                or type(size) is not int
                or not 0 <= size <= MAX_MEMBER_BYTES
            ):
                raise BackupError("Invalid immutable artifact descriptor")
            if checksum in references and references[checksum] != size:
                raise BackupError("Conflicting sizes for the same immutable artifact")
            references[checksum] = size
        for child in value.values():
            _artifact_references(child, references)
    elif isinstance(value, list):
        for child in value:
            _artifact_references(child, references)


def _verify_snapshot(database, blobs=None):
    """Validate the database without executing its triggers or modifying it."""
    references, expected_owners = {}, []
    blob_directory = Path(blobs) if blobs is not None else database.parent / "blobs"

    def decode_document(text):
        from .evidence_storage import PREFIX, decode

        if not text.startswith(PREFIX):
            return _json(text)

        def load(checksum):
            path = blob_directory / checksum
            if path.is_symlink() or not path.is_file():
                raise BackupError("Referenced evidence object is missing or not a regular file")
            proof = _hash_file(path)
            if proof["sha256"] != checksum:
                raise BackupError("Evidence object checksum differs")
            if checksum in references and references[checksum] != proof["bytes"]:
                raise BackupError("Conflicting evidence object sizes")
            references[checksum] = proof["bytes"]
            return path.read_bytes()

        return decode(text, load)
    try:
        with closing(_readonly(database)) as connection:
            signature = _schema_signature(connection)
            versions = [row[0] for row in connection.execute("SELECT version FROM schema_version")]
            if versions not in ([1], [2]):
                raise BackupError("Unsupported registry schema version")
            version = versions[0]
            if signature != _expected_schema(version):
                raise BackupError("Registry schema or immutability triggers differ")
            if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
                raise BackupError("SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise BackupError("SQLite foreign key check failed")

            events, previous, by_subject = [], "0" * 64, {}
            for expected_seq, row in enumerate(
                connection.execute("SELECT * FROM audit ORDER BY seq"), start=1
            ):
                event = {
                    key: row[key] for key in ("timestamp", "action", "subject", "previous_hash")
                }
                event["details"] = _json(row["details"])
                if (
                    row["seq"] != expected_seq
                    or event["previous_hash"] != previous
                    or digest(event) != row["event_hash"]
                    or not isinstance(event["details"], dict)
                ):
                    raise BackupError("Audit hash chain verification failed")
                previous = row["event_hash"]
                events.append(event)
                by_subject.setdefault(event["subject"], []).append(event)
                _artifact_references(event["details"], references)
            sequence = connection.execute("SELECT name,seq FROM sqlite_sequence").fetchall()
            if [tuple(row) for row in sequence] != ([("audit", len(events))] if events else []):
                raise BackupError("Audit sequence does not match its history")

            record_ids, counts = set(), {}
            for row in connection.execute("SELECT * FROM records ORDER BY id"):
                document = decode_document(row["document"])
                if (
                    not isinstance(document, dict)
                    or digest(document) != row["content_hash"]
                    or document.get("id") != row["id"]
                    or document.get("created_at") != row["created_at"]
                ):
                    raise BackupError(f"Record integrity failed: {row['id']}")
                creation = [
                    event
                    for event in by_subject.get(row["id"], [])
                    if event["action"] == row["kind"] + ".created"
                ]
                if (
                    len(creation) != 1
                    or creation[0]["details"].get("content_hash") != row["content_hash"]
                ):
                    raise BackupError("Record does not match its creation audit")
                record_ids.add(row["id"])
                counts[row["kind"]] = counts.get(row["kind"], 0) + 1
                _artifact_references(document, references)
                if version == 2:
                    expected_owners.extend(SQLiteRegistry.artifact_rows(
                        row["kind"], row["id"], row["created_at"], document,
                    ))

            run_ids, job_counts = set(), dict.fromkeys(sorted(_STATUSES), 0)
            active = {"queued": [], "running": []}
            for row in connection.execute("SELECT * FROM jobs ORDER BY id"):
                request = decode_document(row["document"])
                result = decode_document(row["result"]) if row["result"] is not None else None
                if not isinstance(request, dict) or digest(request) != row["request_hash"]:
                    raise BackupError("Frozen run inputs failed checksum")
                history = [
                    event
                    for event in by_subject.get(row["id"], [])
                    if event["action"].startswith("runs.")
                ]
                _verify_job(row, result, history)
                run_ids.add(row["id"])
                job_counts[row["status"]] += 1
                if row["status"] in active:
                    active[row["status"]].append(row["id"])
                _artifact_references(request, references)
                _artifact_references(result, references)
                if version == 2:
                    expected_owners.extend(SQLiteRegistry.artifact_rows(
                        "runs", row["id"], row["created_at"], request, result,
                    ))
            if version == 2:
                actual_owners = [dict(row) for row in connection.execute(
                    "SELECT * FROM artifact_owners"
                )]
                if sorted(map(canonical, actual_owners)) != sorted(map(canonical, expected_owners)):
                    raise BackupError("Artifact owner index differs from its immutable originals")
                expected_heads = {}
                for row in connection.execute("SELECT * FROM artifact_owners ORDER BY "
                                              + SQLiteRegistry.ARTIFACT_ORDER):
                    expected_heads.setdefault(row["artifact_id"], digest(dict(row)))
                actual_heads = {row[0]: row[1] for row in connection.execute(
                    "SELECT artifact_id,binding_hash FROM artifact_heads"
                )}
                if actual_heads != expected_heads:
                    raise BackupError("Artifact owner preference guard differs from its index")
            for event in events:
                if event["action"].startswith("runs.") and event["subject"] not in run_ids:
                    raise BackupError("Audit references a missing run")
                if event["action"].endswith(".created") and event["subject"] not in record_ids:
                    raise BackupError("Audit references a missing record")
            if record_ids & run_ids:
                raise BackupError("Record and run identities overlap")
            return {
                "schema_version": version,
                "schema_hash": digest(signature),
                "records": dict(sorted(counts.items())),
                "runs": job_counts,
                "active_jobs": active,
                "audit_events": len(events),
                "audit_head": previous,
                "blob_count": len(references),
            }, references
    except BackupError:
        raise
    except (sqlite3.DatabaseError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise BackupError("Snapshot database verification failed") from error


def _verify_job(row, result, history):
    current, completion = None, None
    for event in history:
        action = event["action"]
        if action == "runs.queued" and current is None:
            if event["details"].get("cache_key") != row["cache_key"]:
                raise BackupError("Run cache key differs from its audit")
            current = "queued"
        elif action == "runs.running" and current == "queued":
            current = "running"
        elif action == "runs.cancelled" and current in {"queued", "running"}:
            current = "cancelled"
        elif (
            action in {"runs.succeeded", "runs.failed", "runs.interrupted"}
            and current == "running"
        ):
            current = "succeeded" if action == "runs.succeeded" else "failed"
            completion = event
        else:
            raise BackupError("Invalid audited run lifecycle")
    if current != row["status"] or current not in _STATUSES:
        raise BackupError("Run status differs from its audit history")
    if current in {"queued", "running", "cancelled"}:
        if result is not None or row["error"] is not None:
            raise BackupError("Unfinished run contains an unaudited result")
    elif completion["action"] == "runs.interrupted":
        if result is not None or row["error"] != (
            "Worker interrupted; frozen inputs retained. Submit again to retry."
        ):
            raise BackupError("Interrupted run result differs from the recovery contract")
    else:
        details = completion["details"]
        if details.get("result_hash") != (digest(result) if result is not None else None):
            raise BackupError("Completed run result failed audit checksum")
        if details.get("error") != row["error"]:
            raise BackupError("Completed run error differs from its audit")
        if current == "succeeded" and (result is None or row["error"] is not None):
            raise BackupError("Successful run lacks an explicit successful result")
        if current == "failed" and not row["error"]:
            raise BackupError("Failed run lacks its recorded failure")


def _new_path(value):
    path = Path(value).expanduser().absolute()
    if os.path.lexists(path):
        raise FileExistsError(f"Destination already exists; refusing to overwrite: {path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"Destination parent must already exist: {path.parent}")
    return path


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_no_replace(source, destination):
    """Atomic directory publication must refuse even a raced empty destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        # Darwin sys/stdio.h: RENAME_EXCL = 0x00000004.
        result = function(os.fsencode(source), os.fsencode(destination), 0x00000004)
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint
        ]
        # Linux renameat2: AT_FDCWD = -100, RENAME_NOREPLACE = 1.
        result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    else:
        raise BackupError("Atomic no-replace restore requires supported macOS or Linux")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def backup_state(state_dir, destination_file, *, expected_project_id=None):
    """Create a new ZIP of a consistent SQLite snapshot and its referenced blobs."""
    if (expected_project_id is not None and
            (not isinstance(expected_project_id, str)
             or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", expected_project_id))):
        raise BackupError("Expected R1 project ID must be a valid lowercase slug")
    source = Path(state_dir).expanduser().absolute()
    destination = _new_path(destination_file)
    database, blobs = source / "registry.sqlite", source / "blobs"
    if source.is_symlink() or database.is_symlink() or blobs.is_symlink():
        raise BackupError("State database and blob directory must not be symlinks")
    if not database.is_file() or not blobs.is_dir():
        raise FileNotFoundError("Source must contain registry.sqlite and blobs/")
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.backup-", dir=destination.parent
    ) as temporary:
        stage = Path(temporary)
        marker = _freeze_project_marker(source, stage)
        if expected_project_id is not None and (
            marker is None or marker["project_id"] != expected_project_id
        ):
            raise BackupError("R1 project marker is missing or belongs to another project")
        snapshot = stage / "registry.sqlite"
        _snapshot_database(database, snapshot)
        registry_report, references = _verify_snapshot(snapshot, blobs)
        if len(references) + 2 + int(marker is not None) > MAX_ENTRIES:
            raise BackupError("Snapshot entry count limit exceeded")
        files = [{"path": "registry.sqlite", **_hash_file(snapshot)}]
        if marker is not None:
            files.append({"path": project_mode.FILENAME,
                          **_hash_file(stage / project_mode.FILENAME)})
        if files[0]["bytes"] + sum(references.values()) > MAX_TOTAL_BYTES:
            raise BackupError("Snapshot total size limit exceeded")
        copied = stage / "blobs"
        copied.mkdir(mode=0o700)
        total = sum(item["bytes"] for item in files)
        for checksum, expected_size in sorted(references.items()):
            original, target = blobs / checksum, copied / checksum
            if original.is_symlink() or not original.is_file():
                raise BackupError(
                    f"Referenced artifact is missing or not a regular file: {checksum}"
                )
            # Copy first, then hash the frozen copy; source changes cannot silently
            # substitute bytes between a preliminary hash and archive creation.
            with original.open("rb") as reader, target.open("xb") as writer:
                size = 0
                while block := reader.read(CHUNK_BYTES):
                    size += len(block)
                    if size > expected_size or size > MAX_MEMBER_BYTES:
                        raise BackupError("Referenced artifact size differs from its descriptor")
                    writer.write(block)
            attributes = _hash_file(target)
            if attributes != {"sha256": checksum, "bytes": expected_size}:
                raise BackupError("Referenced artifact checksum or size mismatch")
            files.append({"path": "blobs/" + checksum, **attributes})
            total += expected_size
            if total > MAX_TOTAL_BYTES:
                raise BackupError("Snapshot total size limit exceeded")
        source_marker = source / project_mode.FILENAME
        if marker is None:
            if os.path.lexists(source_marker):
                raise BackupError("R1 project mode marker appeared during the backup")
        elif (_project_marker(source_marker) != marker
              or _hash_file(source_marker) != _hash_file(stage / project_mode.FILENAME)):
            raise BackupError("R1 project mode marker changed during the backup")
        manifest = {
            "schema": SCHEMA,
            "format_version": 2 if marker is not None else 1,
            "created_at": now(),
            "platform_version": __version__,
            "registry": registry_report,
            "files": files,
            **({"project": marker} if marker is not None else {}),
        }
        manifest["manifest_hash"] = digest(manifest)
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
        if len(encoded) > MAX_MANIFEST_BYTES or total + len(encoded) > MAX_TOTAL_BYTES:
            raise BackupError("Snapshot manifest or total size limit exceeded")
        archive = stage / "snapshot.zip"
        with zipfile.ZipFile(
            archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as bundle:
            bundle.writestr("manifest.json", encoded)
            for entry in files:
                bundle.write(stage / entry["path"], entry["path"])
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise BackupError("Archive size limit exceeded")
        os.chmod(archive, 0o600)
        with archive.open("rb") as stream:
            os.fsync(stream.fileno())
        # Same-filesystem exclusive hard-link publication cannot replace a raced
        # destination file. The temporary link is removed when staging is cleaned.
        os.link(archive, destination)
        _fsync_directory(destination.parent)
        return manifest


def _zip_inventory(bundle):
    entries, total = {}, 0
    inventory = bundle.infolist()
    if len(inventory) > MAX_ENTRIES:
        raise BackupError("Archive entry count limit exceeded")
    for entry in inventory:
        name = entry.filename
        parts = PurePosixPath(name).parts
        mode = stat.S_IFMT(entry.external_attr >> 16)
        if (
            entry.orig_filename != name
            or not name
            or "\\" in name
            or "\x00" in name
            or name.startswith("/")
            or any(part in {".", ".."} for part in parts)
            or PurePosixPath(name).as_posix() != name
            or entry.is_dir()
            or mode not in {0, stat.S_IFREG}
            or entry.external_attr & 0x10
        ):
            raise BackupError("Unsafe archive path or non-regular member")
        if name not in {"manifest.json", "registry.sqlite", project_mode.FILENAME} and not (
            len(parts) == 2 and parts[0] == "blobs" and _HASH.fullmatch(parts[1])
        ):
            raise BackupError("Unexpected archive member path")
        if name in entries:
            raise BackupError("Duplicate archive member")
        if entry.flag_bits & 1 or entry.compress_type not in {
            zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED
        }:
            raise BackupError("Encrypted or unsupported archive member")
        limit = MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_MEMBER_BYTES
        if not 0 <= entry.file_size <= limit or not 0 <= entry.compress_size <= MAX_ARCHIVE_BYTES:
            raise BackupError("Archive member size limit exceeded")
        total += entry.file_size
        if total > MAX_TOTAL_BYTES:
            raise BackupError("Archive expanded size limit exceeded")
        entries[name] = entry
    if not {"manifest.json", "registry.sqlite"} <= entries.keys():
        raise BackupError("Archive is missing the database or manifest")
    return entries


def _validate_manifest(manifest, entries):
    common = {"schema", "format_version", "created_at", "platform_version",
              "registry", "files", "manifest_hash"}
    if (not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int
            or manifest["format_version"] not in (1, 2)
            or set(manifest) != (common | ({"project"} if manifest["format_version"] == 2
                                         else set()))):
        raise BackupError("Invalid backup manifest contract")
    if manifest["schema"] != SCHEMA:
        raise BackupError("Unsupported backup manifest version")
    if manifest["format_version"] == 2 and (
        not isinstance(manifest["project"], dict)
        or set(manifest["project"]) != {"schema", "mode", "project_id"}
        or manifest["project"].get("schema") != project_mode.SCHEMA
        or manifest["project"].get("mode") != project_mode.R1_MODE
        or not isinstance(manifest["project"].get("project_id"), str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", manifest["project"]["project_id"])
    ):
        raise BackupError("Invalid R1 project marker declaration")
    if not all(
        isinstance(manifest[key], str) and 0 < len(manifest[key]) <= 100
        for key in ("created_at", "platform_version")
    ):
        raise BackupError("Invalid backup version or creation timestamp")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if digest(unsigned) != manifest["manifest_hash"]:
        raise BackupError("Manifest hash verification failed")
    if not isinstance(manifest["files"], list):
        raise BackupError("Manifest files must be a list")
    expected = {}
    for entry in manifest["files"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "sha256", "bytes"}
            or not isinstance(entry["path"], str)
            or not isinstance(entry["sha256"], str)
            or not _HASH.fullmatch(entry["sha256"])
            or type(entry["bytes"]) is not int
            or not 0 <= entry["bytes"] <= MAX_MEMBER_BYTES
            or entry["path"] in expected
        ):
            raise BackupError("Invalid or duplicate manifest file descriptor")
        expected[entry["path"]] = entry
    if set(expected) != set(entries) - {"manifest.json"}:
        raise BackupError("Archive members differ from the manifest")
    if (project_mode.FILENAME in expected) != (manifest["format_version"] == 2):
        raise BackupError("R1 project marker must appear exactly in backup format v2")
    for path, entry in expected.items():
        if entry["bytes"] != entries[path].file_size:
            raise BackupError("Manifest file size differs from ZIP metadata")
        if path.startswith("blobs/") and path.split("/")[1] != entry["sha256"]:
            raise BackupError("Content-addressed artifact filename differs from its hash")
    return expected


def restore_state(backup_file, destination_dir, *, expected_project_id=None):
    """Restore into a nonexistent directory after every check passes; start nothing."""
    if (expected_project_id is not None and
            (not isinstance(expected_project_id, str)
             or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", expected_project_id))):
        raise BackupError("Expected R1 project ID must be a valid lowercase slug")
    archive = Path(backup_file).expanduser().absolute()
    destination = _new_path(destination_dir)
    if archive.is_symlink() or not archive.is_file():
        raise BackupError("Backup must be a regular local file")
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise BackupError("Archive size limit exceeded")
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent))
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = _zip_inventory(bundle)
            manifest = _json(bundle.read(entries["manifest.json"]))
            expected = _validate_manifest(manifest, entries)
            if expected_project_id is not None and (
                manifest["format_version"] != 2
                or manifest["project"]["project_id"] != expected_project_id
            ):
                raise BackupError("Backup does not bind the expected R1 project")
            (stage / "blobs").mkdir(mode=0o700)
            for name, descriptor in expected.items():
                target, checksum, size = stage / name, hashlib.sha256(), 0
                with bundle.open(entries[name]) as reader, target.open("xb") as writer:
                    os.chmod(target, 0o600)
                    while block := reader.read(CHUNK_BYTES):
                        size += len(block)
                        if size > descriptor["bytes"] or size > MAX_MEMBER_BYTES:
                            raise BackupError("Expanded archive member exceeds its declared size")
                        checksum.update(block)
                        writer.write(block)
                    writer.flush()
                    os.fsync(writer.fileno())
                if size != descriptor["bytes"] or checksum.hexdigest() != descriptor["sha256"]:
                    raise BackupError("Archive member checksum or size mismatch")
        if manifest["format_version"] == 2:
            if _project_marker(stage / project_mode.FILENAME) != manifest["project"]:
                raise BackupError("Restored R1 marker differs from the manifest project")
        registry_report, references = _verify_snapshot(stage / "registry.sqlite")
        if registry_report != manifest["registry"]:
            raise BackupError("Restored registry differs from its manifest summary")
        declared_blobs = {
            name.split("/")[1]: value["bytes"]
            for name, value in expected.items()
            if name.startswith("blobs/")
        }
        if references != declared_blobs:
            raise BackupError("Referenced artifacts differ from the archived blob set")
        _fsync_directory(stage / "blobs")
        _fsync_directory(stage)
        _rename_no_replace(stage, destination)
        _fsync_directory(destination.parent)
        return {
            "integrity": "verified",
            "destination": str(destination),
            "manifest_hash": manifest["manifest_hash"],
            "registry": registry_report,
            **({"project": manifest["project"]} if manifest["format_version"] == 2 else {}),
            "workers_started": False,
            "active_jobs_preserved": True,
        }
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, EOFError) as error:
        raise BackupError("Invalid or corrupt ZIP snapshot") from error
    finally:
        if stage.exists():
            shutil.rmtree(stage)
