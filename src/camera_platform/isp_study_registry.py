"""Atomic publication of owned retained-RAW study cells and staged ISP models."""

from .registry import ConflictError, IntegrityError, digest, now


def publish(registry, plan, records, requests):
    identity = "isp_study-" + plan["plan_hash"]
    if not 1 <= len(requests) <= 24:
        raise ValueError("An ISP Study requires at most 24 owned cells")
    query = ("SELECT * FROM records WHERE kind='isp_studies' AND id=?", (identity,))

    def reuse(row):
        value = registry._record(row)
        if value["plan_hash_basis"] != plan["plan_hash_basis"]:
            raise ConflictError("ISP Study plan identity collision")
        return value

    before = registry._read_rows([query])[0]
    if before:
        return reuse(before[0])
    # Durable graph objects may precede the transaction. They remain private and
    # unreferenced if publication conflicts or rolls back; no GC is performed.
    staged_records, by_id = [], {}
    for kind, record in records:
        staged = registry._stage_record(
            kind, record, error="Staged ISP Study record failed its exact checksum",
        )
        if record["id"] in by_id:
            if by_id[record["id"]][:2] != staged[:2]:
                raise ConflictError("Staged model identity already has different content")
        else:
            staged_records.append(staged)
            by_id[record["id"]] = staged
    cells, staged_jobs = [], []
    for index, (cell, request) in enumerate(zip(plan["cells"], requests, strict=True)):
        job_id, stamp = "run-isp-study-" + plan["plan_hash"] + f"-{index:02d}", now()
        checksum = digest(request)
        if cell["request_sha256"] != checksum or request["study_id"] != identity:
            raise IntegrityError("ISP Study cell differs from the complete frozen manifest")
        staged_jobs.append((job_id, stamp, request, checksum, registry.encode_document(request)))
        cells.append({**cell, "job_id": job_id})
    record = {**plan, "cells": cells, "run_ids": [c["job_id"] for c in cells],
              "id": identity, "created_at": now()}
    record["content_hash"] = digest(record)
    staged_study = registry._stage_record("isp_studies", record)
    queries = [query] + [("SELECT * FROM records WHERE id=?", (entry[1]["id"],))
                         for entry in staged_records]
    for _attempt in range(8):
        before = registry._read_rows(queries)
        if before[0]:
            return reuse(before[0][0])
        for (kind, value, _encoded), rows in zip(staged_records, before[1:], strict=True):
            if rows and (rows[0]["kind"] != kind or registry._record(rows[0]) != value):
                raise ConflictError("Staged model identity already has different content")
        with registry.transaction() as conn:
            if not registry._same_read_rows(conn, queries, before):
                continue
            active = conn.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]
            if active + len(staged_jobs) > 64:
                raise ConflictError("Insufficient queue capacity for all ISP Study cells (64)")
            for staged, rows in zip(staged_records, before[1:], strict=True):
                if not rows:
                    registry._insert_staged_record(conn, staged)
            for job_id, stamp, request, checksum, encoded in staged_jobs:
                conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", (
                    job_id, checksum, "queued", encoded, checksum, stamp, stamp, None, None,
                ))
                registry.index_job(conn, job_id, request, stamp)
                registry._audit(conn, "runs.queued", job_id,
                                {"cache_key": checksum, "study_id": identity})
            registry._insert_staged_record(conn, staged_study)
        return record
    raise ConflictError("ISP Study registry changed during preparation; retry publication")
