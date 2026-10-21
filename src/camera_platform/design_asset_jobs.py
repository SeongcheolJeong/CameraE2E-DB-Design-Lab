"""Durable local source inspection and readiness jobs; explicit, server-only watch."""

from __future__ import annotations

import fcntl
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from .registry import ConflictError, canonical, digest

ROUTE = "/api/v1/component-explorer"
JOB_KIND = "design_asset_update"
WATCH_KIND = "design_asset_watch_settings"
JOB_REFS = "design_asset_job_refs"
INTERVAL = 900


@contextmanager
def _lock(service):
    with (service.registry.directory / "design-asset-submit.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _active(service, operation, model_id=None):
    with service.registry.connection() as connection:
        rows = connection.execute(
            "SELECT id,document FROM jobs WHERE status IN ('queued','running')"
        ).fetchall()
    for row in rows:
        request = service.registry.decode_document(row["document"])
        if (
            request.get("job_kind") == JOB_KIND
            and request.get("operation") == operation
            and request.get("model_revision_id") == model_id
        ):
            return service.registry.get_run(row["id"])
    return None


def submit(service, operation, payload=None, *, trigger="manual"):

    payload = payload or {}
    if operation not in {"refresh", "check"}:
        raise ValueError("Choose refresh or check")
    allowed = {"model_revision_id"} if operation == "check" else set()
    if set(payload) != allowed:
        raise ValueError("Refresh accepts no fields; check requires model_revision_id")
    model_id = payload.get("model_revision_id")
    if model_id:
        service.registry.get("design_asset_models", model_id)
    with _lock(service):
        active = _active(service, operation, model_id)
        if active:
            return {**active, "already_active": True}
        doc = {
            "job_kind": JOB_KIND,
            "operation": operation,
            "name": "Local asset refresh" if operation == "refresh" else "Model readiness check",
            "model_revision_id": model_id,
            "trigger": trigger,
            "schema": "camera_design_asset_job_v1",
            "submitted_ns": time.time_ns(),
            "default_worker_timeout_seconds": 300,
        }
        job = service.registry.submit(doc, digest(doc))
        service.registry.put(JOB_REFS, {"run_id": job["id"], "operation": operation})
        return job


def execute(service, job):
    from . import design_asset_readiness as readiness

    directory = service.registry.directory / "design-asset-work" / job["id"]
    directory.mkdir(parents=True, exist_ok=True)

    def progress(value):
        if service.registry.get_run(job["id"])["status"] != "running":
            raise ConflictError("Asset job cancelled")
        data = value if isinstance(value, dict) else {"stage": str(value)}
        temporary = directory / "progress.tmp"
        temporary.write_text(canonical(data))
        temporary.replace(directory / "progress.json")

    progress({"stage": "Inspecting pinned inputs", "completed": 0, "total": 1})
    report = (
        readiness.scan(service, progress=progress)
        if job["operation"] == "refresh"
        else readiness.check(service, job["model_revision_id"], progress=progress, job_id=job["id"])
    )
    progress({"stage": "Completed", "completed": 1, "total": 1})
    artifact = service.registry.store_blob(
        canonical(report).encode(), "asset-report.json", "application/json"
    )
    return {"report": report, "artifacts": [artifact]}


def get(service, identity):
    job = service.registry.get_run(identity)
    if job.get("job_kind") == "reference_workshop" and job.get("asset_trial"):
        from .reference_workshop_job import get as get_workshop

        return get_workshop(service, identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Select a design asset inspection, check or trial job")
    path = service.registry.directory / "design-asset-work" / identity / "progress.json"
    if path.is_file() and job["status"] in {"queued", "running"}:
        try:
            job["progress"] = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    return job


def watch_status(service):
    settings = service.registry.list(WATCH_KIND)
    selected = settings[0] if settings else {}
    refs = service.registry.list(JOB_REFS)
    refresh = next((r for r in refs if r.get("operation") == "refresh"), None)
    latest = service.registry.get_run(refresh["run_id"]) if refresh else None
    return {
        "enabled": selected.get("enabled", False),
        "interval_seconds": INTERVAL,
        "server_only": True,
        "last_job_id": latest["id"] if latest else None,
        "last_scan_at": latest["created_at"] if latest else None,
        "last_job_status": latest["status"] if latest else None,
        "last_error": latest.get("error") if latest else None,
        "settings_id": selected.get("id"),
    }


def set_watch(service, payload):
    if set(payload) != {"enabled"} or type(payload["enabled"]) is not bool:
        raise ValueError("Watch requires enabled: true or false; interval is 900 seconds")
    with _lock(service):
        old = watch_status(service)
        if old["enabled"] != payload["enabled"]:
            service.registry.put(
                WATCH_KIND, {"enabled": payload["enabled"], "interval_seconds": INTERVAL}
            )
    if payload["enabled"] and not old["enabled"]:
        submit(service, "refresh", trigger="watch_enabled")
    return watch_status(service)


def watch_tick(service, *, timestamp=None):
    status = watch_status(service)
    if not status["enabled"]:
        return None
    stamp = status["last_scan_at"]
    last = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() if stamp else 0
    if (time.time() if timestamp is None else timestamp) - last < INTERVAL:
        return None
    return submit(service, "refresh", trigger="watch_interval")


class Watcher:
    """A server-lifetime leader; no external automation or catch-up backlog."""

    def __init__(self, service):
        self.service = service
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="design-asset-watch", daemon=True)
        self.thread.start()

    def _run(self):
        with (self.service.registry.directory / "design-asset-watch.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            while not self.stop_event.is_set():
                try:
                    watch_tick(self.service)
                    self.error = None
                except Exception as exc:
                    self.error = f"{type(exc).__name__}: local watch check failed"
                self.stop_event.wait(10)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)


def trial_sources(service, detail):
    """Only exact source-linked original captures, never arbitrary same-name models."""
    from . import component_explorer_results as evidence

    item = detail["asset"]
    if item["family"] != "sensor":
        return []
    links = evidence.bindings(service, [item]).get(item["id"], [])
    sources = []
    for link in links:
        if not link.get("available") or link.get("kind") != "reference_bench":
            continue
        run = service.registry.get_run(link["run_id"])
        if run.get("source") or run.get("axes") or run.get("asset_trial"):
            continue
        summary = run.get("result", {}).get("summary", {})
        config = summary.get("configuration", run.get("specification", {}))
        if config.get("frames") != 8:
            continue
        sources.append(
            {
                "run_id": run["id"],
                "candidate_id": None,
                "name": run.get("name", run["id"]),
                "label": run.get("name", run["id"]),
                "frames": config["frames"],
                "configuration": config,
                "fields": [f.get("id", f.get("field_id")) for f in summary.get("fields", [])],
                "source_manifest_sha256": summary.get("manifest_sha256"),
            }
        )
    return sources


def install_routes(app, service):
    from . import design_asset_readiness as readiness

    @app.get(ROUTE + "/readiness", tags=["design_asset_readiness"])
    def overview():
        result = readiness.overview(service)
        result["watch"] = watch_status(service)
        return result

    @app.get(ROUTE + "/assets/{identity}/readiness", tags=["design_asset_readiness"])
    def detail(identity: str):
        result = readiness.detail(service, identity)
        result["trial_sources"] = trial_sources(service, result)
        return result

    @app.post(ROUTE + "/refreshes", status_code=202)
    def refresh(payload: dict):
        return submit(service, "refresh", payload)

    @app.get(ROUTE + "/watch")
    def watch():
        result = watch_status(service)
        watcher = getattr(app.state, "design_asset_watcher", None)
        result["watcher_error"] = watcher.error if watcher else None
        return result

    @app.put(ROUTE + "/watch")
    def configure_watch(payload: dict):
        return set_watch(service, payload)

    @app.post(ROUTE + "/proposals", status_code=201)
    def propose(payload: dict):
        return readiness.propose(service, payload)

    @app.post(ROUTE + "/proposals/{identity}/adopt", status_code=201)
    def adopt(identity: str, payload: dict):
        if payload:
            raise ValueError("Adoption uses the exact proposal; no replacement values")
        with _lock(service):
            return readiness.adopt(service, identity)

    @app.post(ROUTE + "/readiness-checks", status_code=202)
    def check(payload: dict):
        return submit(service, "check", payload)

    @app.post(ROUTE + "/model-trials", status_code=202)
    def trial(payload: dict):
        from . import reference_asset_trial

        return reference_asset_trial.submit(service, payload)

    @app.post(ROUTE + "/validation-links", status_code=201)
    def validation_link(payload: dict):
        return readiness.link_validation(service, payload)

    @app.get(ROUTE + "/jobs/{identity}")
    def job(identity: str):
        return get(service, identity)

    @app.post(ROUTE + "/jobs/{identity}/cancel")
    def cancel(identity: str, payload: dict):
        if payload:
            raise ValueError("Cancel accepts no fields")
        get(service, identity)
        return service.registry.cancel(identity)
