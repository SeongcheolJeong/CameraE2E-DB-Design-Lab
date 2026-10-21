"""Immutable DB-source core calibration workflows and explicit scoped activation."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import secrets
import tempfile
from pathlib import Path

from . import calibration_db as db
from . import calibration_db_color as source
from . import calibration_lab as lab
from .registry import IntegrityError, canonical, digest

KIND = "calibration_db_core_workflow"
MODELS = "calibration_db_core_models"
ACTIVATIONS = "calibration_db_core_activations"
ROOT = "/api/v1/calibration-lab/core-workflows"
MAX_BYTES = 512 * 1024**2


def active(service, *, include_pending=False):
    result = {}
    for row in sorted(service.registry.list(ACTIVATIONS), key=lambda r: (r["created_at"], r["id"])):
        status = service.registry.get_run(row["run_id"])["status"]
        if status == "succeeded" or (include_pending and status == "running"):
            result[row["target_key"]] = row
    return result


def spec(payload):
    if not isinstance(payload, dict) or set(payload) - {
        "source",
        "modules",
        "seed",
        "name",
        "target_camera_id",
        "apply",
    }:
        raise ValueError("Core workflow requires source and selected parameter modules")
    modules = payload.get("modules", ["electronics", "optics"])
    if (
        not isinstance(modules, list)
        or not modules
        or not all(isinstance(m, str) for m in modules)
        or len(set(modules)) != len(modules)
        or set(modules) - {"electronics", "optics"}
    ):
        raise ValueError("Select electronics and/or optics once")
    seed = payload.get("seed", secrets.randbelow(2**31 - 500000))
    if type(seed) is not int or not 0 <= seed < 2**31 - 500000:
        raise ValueError("Core seed must be an integer in a bounded disjoint-stream range")
    camera = payload.get("target_camera_id", "synthetic-db-core-calibration")
    db._identity(camera, "synthetic target", r"synthetic-db-[A-Za-z0-9_.-]{1,100}")
    if type(payload.get("apply", True)) is not bool:
        raise ValueError("apply must be a boolean")
    name = payload.get("name", "Dark·PTC / MTF·PSF 자동 보정")
    if not isinstance(name, str) or not 1 <= len(name) <= 160:
        raise ValueError("Workflow name must be bounded text")
    return {
        "source": payload.get("source"),
        "modules": sorted(modules),
        "seed": seed,
        "target_camera_id": camera,
        "name": name,
        "apply": payload.get("apply", True),
    }


def submit(service, payload):
    code = db._current(service)
    config = spec(payload)
    run, context, prepared = source._prepare(service, config["source"])
    from .calibration_db_core_engine import profiles

    profiles(prepared)  # Refuse unsupported source transport before queueing.
    artifacts, files = [], {}
    for name, data in source._files(prepared).items():
        a = service.registry.store_blob(
            data, name, mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        artifacts.append(a)
        files[name] = {"sha256": a["sha256"], "bytes": len(data)}
    key = digest(
        {
            "source": config["source"],
            "target": config["target_camera_id"],
            "modules": config["modules"],
        }
    )
    with lab._lock(service):
        again, current = db.source_selection(service, config["source"])
        if run != again or current != context or db._current(service) != code:
            raise IntegrityError("Core source changed while freezing inputs")
        prior = active(service).get(key)
        document = {
            "name": config["name"],
            "job_kind": KIND,
            "plugin_id": "camera_db_core_v1",
            "specification": config,
            "source_context": context,
            "source_files": files,
            "source_code": code,
            "target_key": key,
            "expected_active_model_id": prior["model_id"] if prior else None,
            "input_artifacts": lab._unique_artifacts(artifacts),
            "default_worker_timeout_seconds": 1800,
        }
        return service.registry.submit(document, digest(document), force=True)


def directory(service, identity):
    db._identity(identity, "core run", r"run-[0-9a-f]{32}")
    return service.registry.directory / "calibration-db-core" / identity


def get(service, identity):
    directory(service, identity)
    row, bound = service.registry.get_run_with_result_binding(identity)
    if row.get("job_kind") != KIND:
        raise ValueError("Selected execution is not a core calibration workflow")
    if row["status"] == "succeeded":
        if bound is None or json.loads(bound["path"].read_bytes()) != row["result"]["summary"]:
            raise IntegrityError("Core result differs from its immutable binding")
    elif row["status"] in {"queued", "running"}:
        path = directory(service, identity) / "progress.json"
        if path.is_file() and not path.is_symlink():
            row["progress"] = json.loads(path.read_bytes())
    return row


def get_model(service, identity):
    db._identity(identity, "core model", r"db-core-model-[0-9a-f]{32}")
    row = service.registry.get(MODELS, identity)
    if (
        row.get("schema") != "camera_db_core_model_v1"
        or digest(row["profiles"]) != row["profile_sha256"]
    ):
        raise IntegrityError("Core model profile hash differs")
    run = get(service, row["run_id"])
    if run["status"] != "succeeded" or run["result"]["summary"]["model_id"] != identity:
        raise IntegrityError("Core model has no matching completed execution")
    return row


def execute(service, job):
    from . import calibration_db_color_engine as loader
    from . import calibration_db_core_engine as engine
    from .calibration_db_core_report import publish

    if get(service, job["id"])["status"] != "running" or db._current(service) != job["source_code"]:
        raise IntegrityError("Core execution source/claim changed")
    output = directory(service, job["id"])
    output.mkdir(parents=True, exist_ok=False)

    def progress(message):
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Core workflow cancelled")
        (output / "progress.json").write_text(canonical({"message": str(message)}))

    with tempfile.TemporaryDirectory(prefix="camera-db-core-source-") as temp:
        root = Path(temp)
        for name, entry in job["source_files"].items():
            p = root / db._relative(name)
            binding = service.registry.artifact_binding(
                entry["sha256"], owner_kind="runs", owner_id=job["id"]
            )
            data = binding["path"].read_bytes()
            if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise IntegrityError("Core frozen source bytes changed")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            retained = output / "source" / name
            retained.parent.mkdir(parents=True, exist_ok=True)
            retained.write_bytes(data)
        context = job["source_context"]
        prepared = loader.prepare(
            service.workspace,
            root,
            None,
            context["candidate_id"],
            context["condition_id"],
            context["manifest_sha256"],
        )
        actual = {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in source._files(prepared).items()
        }
        if actual != job["source_files"]:
            raise IntegrityError("Core reconstructed source differs from queued inputs")
        config = job["specification"]
        # Registry evidence objects accept JSON primitives only. Freeze the exact
        # same representation that reports and the immutable result blob serialize.
        summary = json.loads(
            canonical(engine.execute(prepared, output, config["seed"], config["modules"], progress))
        )
    model_id = (
        "db-core-model-" + digest({"run": job["id"], "profiles": summary["candidate_sha256"]})[:32]
    )
    model = service.registry.put(
        MODELS,
        {
            "schema": "camera_db_core_model_v1",
            "name": config["name"],
            "run_id": job["id"],
            "source_context": job["source_context"],
            "source_files": job["source_files"],
            "profiles": summary["candidate_profiles"],
            "profile_sha256": summary["candidate_sha256"],
            "modules": config["modules"],
            "target_key": job["target_key"],
            "target_camera_id": config["target_camera_id"],
            "source_code": job["source_code"],
            "source_kind": "synthetic",
            "physical_validation": "not_established",
            "activation_scope": (
                "selected modules on derived local-probe model; original DB recipes unchanged"
            ),
        },
        identity=model_id,
    )
    summary.update(
        run_id=job["id"],
        model_id=model_id,
        source_context=job["source_context"],
        source_code=job["source_code"],
        model=model,
        files_base=ROOT + "/" + job["id"] + "/files/",
        report_url=ROOT + "/" + job["id"] + "/files/report.html",
        bundle_url=ROOT + "/" + job["id"] + "/files/evidence.zip",
    )
    # A failed/CAS-blocked application still produces an inspectable complete report.
    with lab._lock(service):
        prior = active(service, include_pending=True).get(job["target_key"])
        current = prior["model_id"] if prior else None
        if (
            config["apply"]
            and summary["validation"]["numerical_tolerance_pass"]
            and current == job["expected_active_model_id"]
        ):
            activation = service.registry.put_verified_record(
                ACTIVATIONS,
                {
                    "schema": "camera_db_core_activation_v1",
                    "target_key": job["target_key"],
                    "model_id": model_id,
                    "previous_model_id": current,
                    "run_id": job["id"],
                    "source_context": job["source_context"],
                    "modules": config["modules"],
                },
                [(MODELS, model)],
            )
            summary["activation"] = {"status": "applied", "id": activation["id"]}
        else:
            summary["activation"] = {
                "status": "not_requested" if not config["apply"] else "blocked",
                "reason": "Numerical tolerance not met"
                if not summary["validation"]["numerical_tolerance_pass"]
                else "Active version changed during calculation"
                if config["apply"]
                else "Inspect-only workflow",
            }
    progress("차트·PSF·오차 보고서와 ZIP 생성")
    publish(output, summary)
    artifacts, files, size = [], {}, 0
    input_hashes = {a["sha256"] for a in job["input_artifacts"]}
    for p in sorted(output.rglob("*")):
        if p.is_symlink():
            raise IntegrityError("Core output cannot contain symlinks")
        if not p.is_file() or p.name == "progress.json":
            continue
        size += p.stat().st_size
        if size > MAX_BYTES:
            raise ValueError("Core output exceeds 512 MiB")
        name = db._relative(p.relative_to(output).as_posix())
        a = service.registry.store_file(
            p, name, mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        if a["sha256"] not in input_hashes:
            artifacts.append(a)
        files[name] = a["sha256"]
    summary["files"] = files
    if db._current(service) != job["source_code"]:
        raise IntegrityError("Core source changed during execution")
    result = service.registry.store_blob(
        canonical(summary).encode(), "core-result.json", "application/json"
    )
    progress("완료 · 가상 실측·보정 전후 차트·오차 보고서 보존")
    return {"summary": summary, "artifacts": [result, *lab._unique_artifacts(artifacts)]}


def file(service, identity, relative):
    row = get(service, identity)
    if row["status"] != "succeeded":
        raise ValueError("Core files require a completed workflow")
    checksum = row["result"]["summary"]["files"].get(db._relative(relative))
    if checksum is None:
        raise KeyError("File is outside the core result manifest")
    return service.registry.artifact_binding(checksum, owner_kind="runs", owner_id=identity)["path"]


def install_routes(app, service):
    from fastapi.responses import FileResponse

    @app.post(ROOT, status_code=202, tags=["calibration_lab"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get(ROOT, tags=["calibration_lab"])
    def listing():
        rows = []
        for r in service.registry.list_runs(job_kinds={KIND}):
            s = (r.get("result") or {}).get("summary", {})
            rows.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "status": r["status"],
                    "source": r["specification"]["source"],
                    "modules": r["specification"]["modules"],
                    "model_id": s.get("model_id"),
                    "report_url": s.get("report_url"),
                    "activation": s.get("activation"),
                    "created_at": r["created_at"],
                }
            )
        return {"items": rows}

    @app.get(ROOT + "/{identity}", tags=["calibration_lab"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(ROOT + "/{identity}/cancel", tags=["calibration_lab"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(ROOT + "/{identity}/files/{relative:path}", tags=["calibration_lab"])
    def artifact(identity: str, relative: str):
        return FileResponse(
            file(service, identity, relative),
            media_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
        )

    @app.get("/api/v1/db-design/active-core-models", tags=["db_design"])
    def models():
        return {
            "items": [
                {**get_model(service, a["model_id"]), "activation": a}
                for a in active(service).values()
            ]
        }
