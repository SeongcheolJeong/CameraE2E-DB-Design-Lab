# Camera Platform: single-host backup and restore

`camera_platform.backup` preserves the committed SQLite registry, its audit history,
all runs, and every referenced immutable artifact. Restore verifies this evidence
before publishing a new state directory. It starts neither an API server nor a worker.

```python
from camera_platform.backup import backup_state, restore_state

manifest = backup_state(
    "/absolute/path/to/current-state",
    "/absolute/path/to/backups/camera-state-2026-09-13.zip",
)
report = restore_state(
    "/absolute/path/to/backups/camera-state-2026-09-13.zip",
    "/absolute/path/to/restored-state",
)
assert report["integrity"] == "verified"
```

Both destination parents must already exist. The backup filename and restore
directory must **not exist**, including an empty directory or a symlink. The module
never overwrites a live state directory or an earlier backup.

## What is included

- A consistent `registry.sqlite` snapshot made through SQLite's backup API,
  including committed WAL data. A transaction in progress is included only if it is
  committed before the snapshot's read point; a half transaction is never copied.
- All immutable records, frozen run requests and outcomes, queued/running jobs,
  and the complete audit chain as of that snapshot.
- Content-addressed blobs referenced by nested standard artifact descriptors
  (`sha256` and `bytes`) in record/run JSON and audit event details. This includes
  `measurement.original_artifact`, run artifacts, run input artifacts, and nested
  capture/frame records. Shared artifacts are stored once, under `blobs/<sha256>`.
- A `manifest.json` with the format and platform versions, file SHA-256 hashes and
  byte counts, schema signature, registry counts, active job IDs, and audit head.
  Its `manifest_hash` covers the manifest without the `manifest_hash` field.

Unreferenced blobs, partially written temporary blobs, worker/server lock files,
local authentication tokens, logs, source code, Python dependencies, and external
solver installations are excluded. The snapshot is an evidence/state backup,
not a complete machine or application deployment image.

The backup can run while the local service is writing. Blobs are published before
their referencing records and remain immutable. The backup first freezes SQLite,
then copies only references in that frozen database. Each copied blob must match
its recorded hash and byte count. A missing or altered blob aborts publication.

## Verification before restore

The restore path does not call `extractall`. It accepts only `manifest.json`,
`registry.sqlite`, and hash-named regular files under `blobs/`. It rejects absolute
paths, traversal, backslashes, duplicate entries, symlinks, directories, unsupported
compression, encrypted members, and unexpected files. Every member is streamed
into private staging and checked against the manifest's SHA-256 and byte count.

The database is opened read-only. Verification checks:

1. SQLite integrity and the exact installed registry v1 schema, including the
   immutability triggers. Missing triggers are rejected, not silently recreated.
2. The complete audit hash chain, sequence continuity, and audit sequence counter.
3. Every record's content hash, identity, timestamp, and creation audit linkage.
4. Every run's frozen request hash and its audited lifecycle. Successful and failed
   results must match the completion audit. Active jobs cannot contain completed
   outcomes. Interrupted jobs must match the existing recovery contract.
5. The manifest's counts/audit head and exact agreement between database artifact
   references and the archive's blob set. Missing or surplus blobs are rejected.

After verification, staging is published with an atomic **no-replace** directory
rename. A destination created concurrently is refused, even when empty. Restored
directories use mode `0700`, files use `0600`, and the backup ZIP uses `0600`.
Files and directory metadata are flushed before publication.

Atomic no-replace restore currently supports macOS (`renamex_np`, `RENAME_EXCL`)
and Linux with `renameat2(RENAME_NOREPLACE)`. Unsupported platforms fail closed.
This is for a local filesystem; network-filesystem durability is not certified.

## Active jobs and bringing the restored workspace online

Restore retains `queued` and `running` exactly as recorded, including their frozen
inputs and original audit history. It never marks an interrupted calculation as
successfully completed and never invents output artifacts.

The returned `registry.active_jobs` lists these jobs. Review this report before
starting a worker. The existing worker acquires its exclusive process lock and
performs its audited recovery lifecycle: a previously `running` job becomes
`failed` with a `runs.interrupted` audit event. Queued jobs remain queued for normal
execution. Submit a new run to retry a recovered failed job.

Reinstall the matching application/dependencies and inspect the restored state
before directing the API server to it. Frozen execution environment checks still
apply: an incompatible source/dependency fingerprint can prevent queued work from
running. Point the service at the newly restored directory as an explicit operating
step; this module does not switch a running service or change the original state.

## Limits and trust boundary

The format's default resource limits are 16 GiB compressed archive size, 16 GiB total
expanded size, 4 GiB per member, 100,000 ZIP entries, an 8 MiB manifest, and 120 seconds
for the SQLite snapshot phase. These are module constants for an operator to review
before choosing a different deployment envelope. Restore enforces declared ZIP and
manifest sizes before extracting and actual sizes while streaming. ZIP64 is supported.

SHA-256 and the audit chain detect corruption and inconsistent history. They are
not an external digital signature: someone who can rewrite all data and recompute
every hash can construct a different internally consistent archive. Preserve the
returned manifest hash/audit head in an independently trusted local record when
stronger provenance is needed. This module does not provide rollback protection,
encryption, retention rotation, remote replication, or disaster-recovery certification.

**Backups contain original RAW frames, measurement data, physical sample identifiers,
and source metadata.** Treat them with the same access restrictions as the workspace.
The ZIP is not encrypted. Store it only on an appropriately protected local volume;
the backup and restore functions perform no external upload or network access.

## Validation

Run the focused suite from the repository root:

```sh
.venv-platform/bin/python -m pytest tests/platform/test_backup.py
```

Tests cover exact original bytes and nested capture artifacts, deduplication and
orphan exclusion, committed WAL data with an active transaction, all existing job
lifecycle states, immutable audit preservation, existing/raced destinations,
traversal/symlink/duplicate archives, resource limits, and semantic database
corruption even after an attacker recomputes the outer archive hashes.
