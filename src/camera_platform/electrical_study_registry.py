"""Atomic publication and ownership for camera response experiment jobs.

Children use the ordinary camera Worker path. The aggregate may be claimed only
after its immutable dependencies terminate; no nested Worker or in-memory queue.
"""
from __future__ import annotations

import uuid
from copy import deepcopy

from .registry import ConflictError, IntegrityError, canonical, digest, now

JOB_KIND = "electrical_response_study"
OWNER = "electrical_response_owner"
TERMINAL = {"succeeded", "failed", "cancelled"}


def publish(registry, document, records, child_requests, *, force=False):
    if type(force) is not bool or document.get("job_kind") != JOB_KIND:
        raise ValueError("Electrical study publication requires its kind and boolean force")
    if "children" in document or not 2 <= len(child_requests) <= 24:
        raise ValueError("Electrical study publication requires two to twenty-four child runs")
    planned = document.get("planned_children")
    if not isinstance(planned, list) or len(planned) != len(child_requests):
        raise ValueError("Electrical study planned children differ from staged requests")
    pairs = set()
    for entry, expected in zip(child_requests, planned, strict=True):
        if set(entry) != {"cell_id", "role", "request"}:
            raise ValueError("Each electrical child requires cell_id, role and request")
        if (not isinstance(entry["cell_id"], str) or not entry["cell_id"]
                or entry["role"] not in {"baseline", "candidate"}
                or OWNER in entry["request"] or entry["request"].get("job_kind") == JOB_KIND
                or entry["request"].get("plugin_id") != "electrical_control_v1"):
            raise ValueError("Only fresh electrical camera requests may be study children")
        identity = entry["cell_id"], entry["role"]
        if identity in pairs:
            raise ValueError("Duplicate electrical comparison member")
        pairs.add(identity)
        actual = {"cell_id": entry["cell_id"], "role": entry["role"],
                  "request_sha256": digest(entry["request"])}
        if canonical(actual) != canonical(expected):
            raise IntegrityError("Staged electrical child differs from its frozen plan")
    if any((cell, role) not in pairs for cell, _ in pairs for role in ("baseline", "candidate")):
        raise ValueError("Every electrical condition needs one fixed and one AE run")
    cache_key = digest(document)
    cache_query = (
        "SELECT * FROM jobs WHERE cache_key=? AND status IN ('queued','running','succeeded') "
        "ORDER BY created_at DESC LIMIT 1", (cache_key,),
    )

    def reuse(row):
        parent = registry._job(row)
        frozen = registry.decode_document(row["document"])
        children = frozen.pop("children", None)
        if canonical(frozen) != canonical(document) or len(children or []) != len(child_requests):
            raise ConflictError("Electrical comparison cache input collision")
        for child, entry in zip(children, child_requests, strict=True):
            original = registry.get_run(child["run_id"])
            owner = {"parent_id": parent["id"], "cell_id": entry["cell_id"], "role": entry["role"]}
            expected = {**entry["request"], OWNER: owner}
            if child != {"run_id": original["id"], "cell_id": entry["cell_id"],
                         "role": entry["role"], "request_sha256": digest(expected)}:
                raise IntegrityError("Cached electrical child ownership differs")
            if original["content_hash"] != digest(expected):
                raise IntegrityError("Cached electrical child inputs differ")
        return {**parent, "artifacts": (parent.get("result") or {}).get("artifacts", []),
                "cache_hit": True}

    if not force:
        cached = registry._read_rows([cache_query])[0]
        if cached:
            return reuse(cached[0])
    parent_id, stamp = "run-" + uuid.uuid4().hex, now()
    children, staged_jobs = [], []
    for entry in child_requests:
        child_id = "run-" + uuid.uuid4().hex
        request = {**deepcopy(entry["request"]), OWNER: {
            "parent_id": parent_id, "cell_id": entry["cell_id"], "role": entry["role"],
        }}
        checksum = digest(request)
        children.append({"run_id": child_id, "cell_id": entry["cell_id"],
                         "role": entry["role"], "request_sha256": checksum})
        staged_jobs.append((child_id, checksum, request, registry.encode_document(request)))
    parent = {**deepcopy(document), "children": children}
    parent_checksum, parent_encoded = digest(parent), registry.encode_document(parent)
    staged_records = []
    seen = set()
    for kind, record in records:
        if kind not in {"capture_programs", "tests"} or (kind, record["id"]) in seen:
            raise ValueError("Electrical study stages distinct capture programs and Tests only")
        seen.add((kind, record["id"]))
        staged_records.append(registry._stage_record(kind, record))
    queries = [("SELECT * FROM records WHERE kind=? AND id=?", (kind, record["id"]))
               for kind, record, _ in staged_records]
    for _ in range(8):
        existing = registry._read_rows(queries)
        for (_, record, _), rows in zip(staged_records, existing, strict=True):
            if rows and canonical(registry._record(rows[0])) != canonical(record):
                raise ConflictError("Staged electrical revision identity collision")
        with registry.transaction() as conn:
            if not registry._same_read_rows(conn, queries, existing):
                continue
            cached = None if force else conn.execute(*cache_query).fetchone()
            if cached is None:
                count = conn.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()[0]
                if count + len(staged_jobs) + 1 > 64:
                    raise ConflictError(
                        "Insufficient queue capacity for complete electrical study (64 runs)"
                    )
                for staged, rows in zip(staged_records, existing, strict=True):
                    if not rows:
                        registry._insert_staged_record(conn, staged)
                for identity, checksum, request, encoded in staged_jobs:
                    conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", (
                        identity, checksum, "queued", encoded, checksum, stamp, stamp, None, None,
                    ))
                    registry.index_job(conn, identity, request, stamp)
                    registry._audit(
                        conn, "runs.queued", identity,
                        {"cache_key": checksum, "electrical_response_parent_id": parent_id},
                    )
                conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", (
                    parent_id, cache_key, "queued", parent_encoded, parent_checksum,
                    stamp, stamp, None, None,
                ))
                registry.index_job(conn, parent_id, parent, stamp)
                registry._audit(conn, "runs.queued", parent_id, {"cache_key": cache_key})
        if cached is not None:
            return reuse(cached)
        return {**parent, "id": parent_id, "status": "queued", "cache_key": cache_key,
                "content_hash": parent_checksum, "created_at": stamp, "updated_at": stamp,
                "result": None, "error": None, "artifacts": []}
    raise ConflictError("Electrical study records changed during publication; retry submission")


def dependencies(registry, parent):
    """Return verified ownership rows for claim/cancel optimistic transactions."""
    children = parent.get("children")
    if not isinstance(children, list) or not 2 <= len(children) <= 24:
        raise IntegrityError("Electrical aggregate is missing its bounded children")
    ids = [item.get("run_id") for item in children]
    if len(set(ids)) != len(ids) or parent["id"] in ids:
        raise IntegrityError("Electrical aggregate has duplicate or recursive children")
    queries = [("SELECT * FROM jobs WHERE id=?", (identity,)) for identity in ids]
    rows = registry._read_rows(queries)
    for item, child_rows in zip(children, rows, strict=True):
        if not child_rows or child_rows[0]["request_hash"] != item["request_sha256"]:
            raise IntegrityError("Electrical child is missing or its frozen binding changed")
        child = registry.decode_document(child_rows[0]["document"])
        if (digest(child) != item["request_sha256"] or child.get(OWNER) != {
            "parent_id": parent["id"], "cell_id": item["cell_id"], "role": item["role"],
        } or child.get("plugin_id") != "electrical_control_v1"):
            raise IntegrityError("Electrical child ownership failed verification")
    return queries, rows


def cancel(registry, identity):
    parent_query = ("SELECT * FROM jobs WHERE id=?", (identity,))
    for _ in range(8):
        before = registry._read_rows([parent_query])
        if not before[0]:
            raise KeyError("Unknown electrical study")
        parent = registry._job(before[0][0])
        if parent.get("job_kind") != JOB_KIND:
            raise ValueError("Expected electrical response aggregate")
        if parent["status"] in TERMINAL:
            return registry.get_run(identity)
        queries, rows = dependencies(registry, parent)
        with registry.transaction() as conn:
            if not registry._same_read_rows(conn, [parent_query, *queries], [*before, *rows]):
                continue
            for target in [identity, *[item["run_id"] for item in parent["children"]]]:
                changed = conn.execute(
                    "UPDATE jobs SET status='cancelled',updated_at=? "
                    "WHERE id=? AND status IN ('queued','running')", (now(), target),
                ).rowcount
                if changed:
                    registry._audit(conn, "runs.cancelled", target,
                                    {"electrical_response_parent_id": identity})
        return registry.get_run(identity)
    raise ConflictError("Electrical study changed during cancellation; retry cancellation")
