"""Compact retained ISP display from one operation-local verified job history.

Jobs and their completion events share the Registry read snapshot. Models are a
separate immutable-record snapshot; fresh blob checks retain the existing detail
verifiers. This is not an atomic database-and-filesystem snapshot or a cache.
"""

from __future__ import annotations

from copy import copy, deepcopy

from . import retained_isp_comparison as comparison
from . import retained_isp_replay as replay
from .registry import canonical, digest

SCHEMA = "camera_retained_isp_workbench_v1"
MAX_RESPONSE_BYTES = 4 * 1024**2
MODEL_FIELDS = (
    "id", "name", "revision", "kind", "payload", "input_raw_format", "content_hash", "created_at",
)
HISTORY_FIELDS = (
    "id", "name", "status", "created_at", "updated_at", "job_kind", "plugin_id",
    "source_id", "selector", "isp_model_id", "error", "content_hash",
)


class _SnapshotRegistry:
    """Private read facade: only internally fetched, fully verified JSON is reused."""

    def __init__(self, registry):
        self._registry = registry
        # Do not filter unverified stored metadata. list_runs also verifies every
        # request/result and the matching completion event before returning.
        self._jobs = {job["id"]: job for job in registry.list_runs()}
        self._models = {model["id"]: model for model in registry.list("models")}
        self.directory = registry.directory

    def get_run(self, identity):
        if identity not in self._jobs:
            raise KeyError("Unknown run in retained ISP read snapshot: " + identity)
        return deepcopy(self._jobs[identity])

    def list_runs(self):
        return deepcopy(list(self._jobs.values()))

    def get(self, kind, identity):
        if kind != "models":
            return self._registry.get(kind, identity)
        if identity not in self._models:
            raise KeyError("Unknown model in retained ISP read snapshot: " + identity)
        return deepcopy(self._models[identity])

    def blob_path(self, identity):
        # No byte/hash cache: every existing evidence check reaches the original
        # content-addressed store, including same-size edits on a later call.
        return self._registry.blob_path(identity)


def _history(record, route):
    specification = record.get("specification", {})
    result = {field: deepcopy(record.get(field, specification.get(field)))
              for field in HISTORY_FIELDS}
    result.update(full_record_sha256=digest(record), detail_path=route + "/" + record["id"])
    return result


def build(service):
    """Verify the original history obligations, then return detached display data."""
    registry = _SnapshotRegistry(service.registry)
    scoped = copy(service)
    scoped.registry = registry
    sources = replay.choices(scoped)
    models = [
        {**{field: deepcopy(model.get(field)) for field in MODEL_FIELDS},
         "full_record_sha256": digest(model), "detail_path": "/models/" + model["id"]}
        for model in registry._models.values()
        if model.get("kind") == "isp" and model.get("payload", {}).get("schema")
        == replay.PROFILE_SCHEMA
    ]
    replays, comparisons = [], []
    for original in registry._jobs.values():
        if (original.get("job_kind") == "isp_replay"
                and original.get("plugin_id") == replay.PLUGIN_ID):
            record = replay.verify(scoped, deepcopy(original))
            replays.append(_history(record, "/replays"))
        elif original.get("job_kind") == comparison.JOB_KIND:
            record = comparison.verify(scoped, deepcopy(original))
            comparisons.append(_history(record, "/retained-isp/comparisons"))
    result = {
        "schema": SCHEMA, "sources": sources, "models": models,
        "replays": replays, "comparisons": comparisons,
        "source_discovery": {
            "scope": "metadata_only", "raw_integrity": "verified_on_selection",
            "physical_validation": "not_established",
        },
        "verification": {
            "jobs_verified": len(registry._jobs), "models_verified": len(registry._models),
            "retained_replays_verified": len(replays),
            "retained_comparisons_verified": len(comparisons),
            "scope": "operation_local_snapshot",
            "consistency": "job_completion_snapshot_separate_model_snapshot_fresh_blob_checks",
        },
    }
    if len(canonical(result).encode()) > MAX_RESPONSE_BYTES:
        raise ValueError("Retained ISP workbench response exceeds 4 MiB; no history was truncated")
    return result
