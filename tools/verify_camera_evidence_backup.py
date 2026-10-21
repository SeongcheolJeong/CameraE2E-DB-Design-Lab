#!/usr/bin/env python3
"""Verify backup/restore of an explicitly isolated, quiescent QA registry.

Creates only the requested new archive, new restore directory and proof file.
The source is opened read-only; no Registry constructor, migration or worker is
started there. Existing destinations are refused. Backup's original record/job/
audit bytes and graph closure are preserved, not re-encoded or re-published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from camera_platform import __version__
from camera_platform.backup import (
    MAX_ENTRIES,
    MAX_MEMBER_BYTES,
    MAX_TOTAL_BYTES,
    _artifact_references,
    _json,
    backup_state,
    restore_state,
)
from camera_platform.evidence_objects import NODE_SCHEMA, validate_reference
from camera_platform.evidence_storage import PREFIX
from camera_platform.registry import digest

HASH = re.compile(r"[0-9a-f]{64}\Z")
MAX_ROWS = 250_000
MAX_NODE_BYTES = 256 * 1024**2
MAX_PROOF_BYTES = 32 * 1024**2
TABLES = {
    "records": "kind,id",
    "jobs": "id",
    "audit": "seq",
    "schema_version": "version",
    "sqlite_sequence": "name",
    "artifact_owners": "artifact_id,owner_kind,owner_id,descriptor_path",
    "artifact_heads": "artifact_id",
}


def readonly(database):
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=20)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def hash_file(path, *, limit=MAX_MEMBER_BYTES):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular file: " + str(path))
    if path.stat().st_size > limit:
        raise ValueError("File exceeds verification byte budget: " + str(path))
    checksum, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while block := stream.read(1024**2):
            size += len(block)
            if size > limit:
                raise ValueError("File grew beyond verification byte budget")
            checksum.update(block)
    return {"sha256": checksum.hexdigest(), "bytes": size}


def row_fingerprint(rows):
    """Hash framed SQLite scalar values, retaining original JSON text byte-for-byte."""
    checksum, count, size = hashlib.sha256(), 0, 0
    for row in rows:
        count += 1
        if count > MAX_ROWS:
            raise ValueError("Table exceeds verification row budget")
        checksum.update(b"row\0")
        for value in row:
            if value is None:
                tag, encoded = b"null", b""
            elif type(value) is int:
                tag, encoded = b"int", str(value).encode()
            elif type(value) is float:
                tag, encoded = b"float", value.hex().encode()
            elif type(value) is str:
                tag, encoded = b"text", value.encode()
            elif type(value) is bytes:
                tag, encoded = b"blob", value
            else:
                raise ValueError("Unsupported SQLite scalar in verification")
            size += len(encoded)
            if size > MAX_TOTAL_BYTES:
                raise ValueError("Table exceeds verification byte budget")
            checksum.update(tag + b":" + str(len(encoded)).encode() + b":")
            checksum.update(encoded)
            checksum.update(b"\0")
        checksum.update(b"end\0")
    return {"rows": count, "scalar_bytes": size, "sha256": checksum.hexdigest()}


def state_rows(state):
    with closing(readonly(state / "registry.sqlite")) as connection:
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if not {"records", "jobs", "audit", "schema_version", "sqlite_sequence"} <= tables:
            raise ValueError("QA registry lacks required tables")
        result = {
            table: row_fingerprint(connection.execute(
                "SELECT * FROM " + table + " ORDER BY " + order
            ))
            for table, order in TABLES.items() if table in tables
        }
        result["sqlite_schema"] = row_fingerprint(connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ))
        result["schema_versions"] = [r[0] for r in connection.execute(
            "SELECT version FROM schema_version ORDER BY version"
        )]
        result["job_states"] = dict(connection.execute(
            "SELECT status,count(*) FROM jobs GROUP BY status ORDER BY status"
        ))
        return result


def classify_closure(state, declared):
    """Walk physical typed references once, avoiding repeated logical expansion.

    restore_state has already verified semantic reconstruction, exact source
    hashes/audit and complete artifact/index closure. This pass distinguishes
    storage nodes from logical artifacts and from HTTP-public artifact owners.
    """
    graph_nodes, logical_artifacts, roots = {}, {}, []
    inline_documents = graph_documents = 0
    with closing(readonly(state / "registry.sqlite")) as connection:
        connection.execute("BEGIN")
        tables = {r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        public = {r[0] for r in connection.execute(
            "SELECT DISTINCT artifact_id FROM artifact_owners"
        )} if "artifact_owners" in tables else None
        for table, field in (("records", "document"), ("jobs", "document"), ("jobs", "result")):
            count = 0
            for (text,) in connection.execute(
                "SELECT " + field + " FROM " + table + " WHERE " + field + " IS NOT NULL"
            ):
                count += 1
                if count > MAX_ROWS:
                    raise ValueError("Document scan exceeds verification row budget")
                if text.startswith(PREFIX):
                    graph_documents += 1
                    roots.append(validate_reference(_json(text[len(PREFIX):])))
                else:
                    inline_documents += 1
                    _artifact_references(_json(text), logical_artifacts)
        for (text,) in connection.execute("SELECT details FROM audit"):
            _artifact_references(_json(text), logical_artifacts)
    pending, references = list(roots), {}
    while pending:
        reference = validate_reference(pending.pop())
        sha = reference["sha256"]
        if sha in references:
            if references[sha] != reference:
                raise ValueError("Inconsistent repeated storage node reference")
            continue
        if len(references) >= MAX_ENTRIES:
            raise ValueError("Storage closure exceeds verification node budget")
        references[sha] = reference
        if declared.get(sha) != reference["bytes"]:
            raise ValueError("Storage node differs from archived blob inventory")
        path = state / "blobs" / sha
        if reference["bytes"] > MAX_NODE_BYTES:
            raise ValueError("Storage node exceeds bounded parser budget")
        proof = hash_file(path, limit=MAX_NODE_BYTES)
        if proof != {"sha256": sha, "bytes": reference["bytes"]}:
            raise ValueError("Storage node bytes differ from exact reference")
        node = _json(path.read_bytes())
        if (not isinstance(node, dict) or set(node) != {"schema", "value", "children"}
                or node["schema"] != NODE_SCHEMA or not isinstance(node["children"], list)):
            raise ValueError("Invalid typed storage node")
        graph_nodes[sha] = proof["bytes"]
        _artifact_references(node["value"], logical_artifacts)
        for child in node["children"]:
            if not isinstance(child, dict) or set(child) != {"path", "reference"}:
                raise ValueError("Invalid typed child reference")
            pending.append(validate_reference(child["reference"]))
    union = dict(graph_nodes)
    for sha, size in logical_artifacts.items():
        if sha in union and union[sha] != size:
            raise ValueError("Storage/artifact overlap has conflicting byte lengths")
        union[sha] = size
    if union != declared:
        raise ValueError("Archived blobs differ from reachable storage and artifact closure")
    if public is not None and not public <= logical_artifacts.keys():
        raise ValueError("Public artifact owner has no logical descriptor in preserved evidence")
    return {
        "graph_documents": graph_documents,
        "inline_documents": inline_documents,
        "graph_root_count": len(roots),
        "unique_graph_nodes": len(graph_nodes),
        "unique_graph_node_bytes": sum(graph_nodes.values()),
        "logical_artifact_count": len(logical_artifacts),
        "public_artifact_count": len(public) if public is not None else None,
        "public_classification": "derived_owner_index" if public is not None else "unavailable_v1",
        "graph_nodes_with_public_owner": (
            sorted(set(graph_nodes) & public) if public is not None else []
        ),
        "private_graph_nodes": len(set(graph_nodes) - public) if public is not None else None,
        "referenced_artifacts_without_public_owner": (
            len(set(logical_artifacts) - public) if public is not None else None
        ),
        "graph_node_sha256": sorted(graph_nodes),
        "logical_artifact_sha256": sorted(logical_artifacts),
        "public_artifact_sha256": sorted(public) if public is not None else None,
        "exact_reachable_blob_inventory_sha256": digest(union),
    }


def paths(state, archive, restore, output):
    values = [Path(p).expanduser().absolute() for p in (state, archive, restore, output)]
    state, archive, restore, output = values
    if "qa" not in re.split(r"[-_]", state.name.lower()):
        raise ValueError("Only an explicitly named isolated QA state is accepted")
    if state.is_symlink() or not state.is_dir():
        raise ValueError("Source must be an existing non-symlink QA directory")
    for target in (archive, restore, output):
        if os.path.lexists(target):
            raise FileExistsError("Destination already exists: " + str(target))
        if not target.parent.is_dir():
            raise ValueError("Destination parent must already exist: " + str(target.parent))
    resolved = [p.resolve() for p in values]
    state_real, archive_real, restore_real, output_real = resolved
    if len(set(resolved)) != len(resolved):
        raise ValueError("Source and output paths must be distinct")
    if state_real.is_relative_to(restore_real) or restore_real.is_relative_to(state_real):
        raise ValueError("Source and restore directories must not overlap")
    for target in (archive_real, output_real):
        if target.is_relative_to(state_real) or target.is_relative_to(restore_real):
            raise ValueError("Archive and proof must be outside source and restored state")
    return state, archive, restore, output


def verify(state, archive, restore, output):
    state, archive, restore, output = paths(state, archive, restore, output)
    started = time.monotonic()
    proof = {
        "schema": "camera_evidence_backup_verification_v1",
        "platform_version": __version__,
        "state_dir": str(state), "archive": str(archive),
        "restore_dir": str(restore), "status": "running", "phase": "source_snapshot",
        "workers_started": False, "source_registry_initialized": False,
        "logical_rows_rewritten": False,
    }
    failure = None
    try:
        proof["source_before"] = state_rows(state)
        proof["phase"] = "backup"
        stamp = time.monotonic()
        manifest = backup_state(state, archive)
        proof["backup_seconds"] = time.monotonic() - stamp
        proof["backup_manifest_hash"] = manifest["manifest_hash"]
        proof["backup_registry"] = manifest["registry"]
        proof["phase"] = "restore"
        stamp = time.monotonic()
        restored = restore_state(archive, restore)
        proof["restore_seconds"] = time.monotonic() - stamp
        if restored["manifest_hash"] != manifest["manifest_hash"] or restored["workers_started"]:
            raise ValueError("Restored manifest or worker startup contract differs")
        proof["restore_result"] = restored
        proof["phase"] = "raw_row_comparison"
        proof["restored_rows"] = state_rows(restore)
        if proof["source_before"] != proof["restored_rows"]:
            raise ValueError("Original and restored raw rows differ; source must be quiescent")
        declared = {entry["path"][6:]: entry["bytes"] for entry in manifest["files"]
                    if entry["path"].startswith("blobs/")}
        files = list((restore / "blobs").iterdir())
        if len(files) > MAX_ENTRIES or {p.name for p in files} != set(declared):
            raise ValueError("Restored blob directory differs from archived inventory")
        proof["phase"] = "exact_blob_verification"
        for sha, size in declared.items():
            if HASH.fullmatch(sha) is None:
                raise ValueError("Invalid archived blob identity")
            expected = {"sha256": sha, "bytes": size}
            if hash_file(restore / "blobs" / sha) != expected:
                raise ValueError("Restored blob hash or byte length differs")
            if hash_file(state / "blobs" / sha) != expected:
                raise ValueError("Source reachable blob changed during backup verification")
        database = next(entry for entry in manifest["files"] if entry["path"] == "registry.sqlite")
        if hash_file(restore / "registry.sqlite") != {
            "sha256": database["sha256"], "bytes": database["bytes"],
        }:
            raise ValueError("Restored database bytes differ from original archived snapshot")
        proof["phase"] = "closure_classification"
        proof["closure"] = classify_closure(restore, declared)
        proof["verified_blob_count"] = len(declared)
        proof["verified_blob_bytes"] = sum(declared.values())
        proof["archive_proof"] = hash_file(archive, limit=MAX_TOTAL_BYTES)
        proof["source_after"] = state_rows(state)
        if proof["source_before"] != proof["source_after"]:
            raise ValueError(
                "Source raw rows changed during verification; source must be quiescent"
            )
        proof["all_original_and_derived_rows_exact"] = True
        proof["all_reachable_blob_bytes_exact"] = True
        proof["source_rows_unchanged"] = True
        proof["status"], proof["phase"] = "passed", "complete"
    except Exception as exc:
        failure = exc
        proof.update(status="failed", error_type=type(exc).__name__, error=str(exc))
    proof["elapsed_seconds"] = time.monotonic() - started
    encoded = json.dumps(proof, ensure_ascii=False, indent=2).encode() + b"\n"
    if len(encoded) > MAX_PROOF_BYTES:
        raise ValueError("Proof exceeds bounded output budget")
    with output.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    if failure is not None:
        raise RuntimeError(
            "Backup verification failed; proof retained at " + str(output)
        ) from failure
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--restore-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.state_dir, args.archive, args.restore_dir, args.output)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "output": str(args.output.absolute()),
                      "verified_blob_count": result["verified_blob_count"],
                      "graph_nodes": result["closure"]["unique_graph_nodes"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
