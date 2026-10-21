#!/usr/bin/env python3
"""Read a preserved Study under a frozen source using an isolated SQLite snapshot.

Only the explicit fresh clone and report directory are written. No original
Registry constructor, Worker, or submission is used. The old external logical
manifest digest is pinned; ZIP timestamps are not treated as evidence identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from verify_camera_evidence_backup import hash_file, readonly, state_rows

from camera_platform import isp_studies
from camera_platform.evidence_package import read_bundle
from camera_platform.registry import digest
from camera_platform.service import PlatformService, environment_fingerprint


def json_hash(value):
    checksum = hashlib.sha256()
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    for chunk in encoder.iterencode(value):
        checksum.update(chunk.encode())
    return checksum.hexdigest()


def blob_inventory(state):
    return {
        str(path.relative_to(state / "blobs")): hash_file(path)
        for path in sorted((state / "blobs").rglob("*"))
        if path.is_file()
    }


def zip_members(path):
    result = {}
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            if item.filename in result:
                raise ValueError("Duplicate ZIP member")
            checksum = hashlib.sha256()
            with archive.open(item) as stream:
                while block := stream.read(1024 * 1024):
                    checksum.update(block)
            result[item.filename] = {"bytes": item.file_size, "sha256": checksum.hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--clone", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--legacy", type=Path, required=True)
    args = parser.parse_args()
    source, clone = args.source.resolve(), args.clone.resolve()
    if source == clone or clone.exists() or not source.is_dir():
        parser.error("Source must exist and the isolated clone must be fresh")
    expected = json.loads((args.expected / "http-verification.json").read_bytes())
    frozen = json.loads(args.freeze.read_bytes())
    identity = expected["records"]["study"]["id"]
    proof = {
        "schema": "camera_frozen_legacy_study_readback_v1",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "source": str(source),
        "clone": str(clone),
        "study_id": identity,
        "frozen_environment_sha256": frozen["environment_sha256"],
        "expected_record_sha256": expected["records"]["study"]["sha256"],
        "expected_logical_manifest_sha256": expected["logical_manifest_sha256"],
        "timings": {},
        "checks": [],
        "physical_validation": "not_established",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / "legacy-study-readback.json"
    if target.exists():
        parser.error("Refusing to overwrite an existing readback proof")

    def checkpoint(phase):
        proof["phase"] = phase
        target.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
        print(phase, flush=True)

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        proof["checks"].append(name)

    stamp = time.monotonic()
    try:
        checkpoint("verify_frozen_source")
        check(digest(environment_fingerprint()) == frozen["environment_sha256"], "source frozen")
        checkpoint("snapshot_original_read_only")
        before = state_rows(source)
        database_before = hash_file(source / "registry.sqlite")
        blobs_before = blob_inventory(source)
        proof["source_before"] = {
            "tables": before,
            "database": database_before,
            "blobs": len(blobs_before),
            "blob_inventory_sha256": digest(blobs_before),
        }
        clone.mkdir(parents=True)
        with (
            closing(readonly(source / "registry.sqlite")) as src,
            closing(sqlite3.connect(clone / "registry.sqlite")) as dst,
        ):
            src.backup(dst)
        shutil.copytree(source / "blobs", clone / "blobs")
        check(state_rows(clone) == before, "SQLite snapshot preserves every row and schema")
        check(blob_inventory(clone) == blobs_before, "clone blob bytes match original")
        proof["timings"]["snapshot_seconds"] = time.monotonic() - stamp
        service = PlatformService(clone)
        clone_before = state_rows(clone)
        checkpoint("public_get_study")
        stage = time.monotonic()
        study = isp_studies.get_study(service, identity)
        check(
            json_hash(study) == proof["expected_record_sha256"], "full Study record SHA preserved"
        )
        check(
            "evidence_forecast_version" not in study["plan_hash_basis"],
            "old Study gains no forecast fields",
        )
        proof["record_content_hash"] = study["content_hash"]
        proof["plan_hash"] = study["plan_hash"]
        proof["timings"]["public_get_study_seconds"] = time.monotonic() - stage
        checkpoint("public_export_bundle")
        stage = time.monotonic()
        bundle = isp_studies.export_bundle(service, identity)
        proof["timings"]["public_export_bundle_seconds"] = time.monotonic() - stage
        target_bundle = args.output / "legacy-study-bundle.zip"
        if target_bundle.exists():
            raise ValueError("Refusing to overwrite an existing bundle")
        shutil.move(bundle, target_bundle)
        checkpoint("verify_exact_export_reconstruction")
        stage = time.monotonic()
        restored = read_bundle(
            target_bundle, expected_manifest_sha256=proof["expected_logical_manifest_sha256"]
        )
        check(
            json_hash(restored) == proof["expected_logical_manifest_sha256"],
            "full logical export matches externally pinned dev9 manifest SHA",
        )
        check(restored["study"] == study, "reconstructed Study exactly equals public readback")
        actual_members = zip_members(target_bundle)
        prior_members = zip_members(args.expected / "study-bundle.zip")
        check(
            actual_members == prior_members,
            "every ZIP member exactly matches preserved dev9 content",
        )
        total = sum(item["bytes"] for item in actual_members.values())
        check(
            total == 49_779_887 == expected["bundle"]["uncompressed_bytes"],
            "uncompressed bundle remains exactly 49,779,887 bytes",
        )
        proof["bundle"] = {
            "path": str(target_bundle),
            **hash_file(target_bundle),
            "uncompressed_bytes": total,
            "members": len(actual_members),
            "logical_manifest_sha256": json_hash(restored),
            "member_inventory_sha256": digest(actual_members),
        }
        proof["timings"]["reconstruction_and_all_member_comparison_seconds"] = (
            time.monotonic() - stage
        )
        del restored
        del study
        checkpoint("public_get_and_export_legacy_v1_study")
        stage = time.monotonic()
        legacy_ref = expected["source_records"]["study"]
        legacy = isp_studies.get_study(service, legacy_ref["id"])
        check(
            json_hash(legacy) == legacy_ref["sha256"], "full legacy v1 Study record SHA preserved"
        )
        check(
            "evidence_forecast_version" not in legacy["plan_hash_basis"],
            "legacy v1 Study gains no forecast fields",
        )
        check(
            legacy.get("evidence_format") != "camera_evidence_bundle_v2",
            "legacy v1 Study is not upgraded to graph export",
        )
        legacy_bundle = isp_studies.export_bundle(service, legacy_ref["id"])
        legacy_target = args.output / "legacy-v1-study-bundle.zip"
        if legacy_target.exists():
            raise ValueError("Refusing to overwrite legacy v1 bundle")
        shutil.move(legacy_bundle, legacy_target)
        legacy_members = zip_members(legacy_target)
        check(
            legacy_members == zip_members(args.legacy / "study-bundle.zip"),
            "every legacy v1 ZIP member exactly matches preserved content",
        )
        check(
            legacy_members["manifest.json"]["sha256"]
            == expected["legacy_bundle"]["manifest_sha256"],
            "legacy v1 manifest retains externally pinned byte SHA",
        )
        proof["legacy_v1"] = {
            "id": legacy_ref["id"],
            "record_sha256": json_hash(legacy),
            "plan_hash": legacy["plan_hash"],
            "content_hash": legacy["content_hash"],
            "bundle": {"path": str(legacy_target), **hash_file(legacy_target)},
            "manifest": legacy_members["manifest.json"],
            "uncompressed_bytes": sum(i["bytes"] for i in legacy_members.values()),
            "members": len(legacy_members),
            "member_inventory_sha256": digest(legacy_members),
        }
        proof["timings"]["legacy_v1_public_read_and_export_seconds"] = time.monotonic() - stage
        del legacy
        checkpoint("verify_original_and_clone_remain_unchanged")
        check(state_rows(clone) == clone_before, "public reads/export do not change clone DB rows")
        check(
            blob_inventory(clone) == blobs_before, "public reads/export do not change clone blobs"
        )
        check(state_rows(source) == before, "original source rows remain byte-for-byte identical")
        check(
            hash_file(source / "registry.sqlite") == database_before,
            "original SQLite file SHA unchanged",
        )
        check(blob_inventory(source) == blobs_before, "original source blob bytes unchanged")
        check(
            digest(environment_fingerprint()) == frozen["environment_sha256"],
            "source remained frozen throughout readback",
        )
        proof["status"] = "passed"
    except BaseException as error:
        proof["status"] = "failed"
        proof["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        proof["timings"]["total_seconds"] = time.monotonic() - stamp
        proof["finished_at"] = datetime.now(UTC).isoformat()
        checkpoint(proof.get("phase", "failed"))


if __name__ == "__main__":
    main()
