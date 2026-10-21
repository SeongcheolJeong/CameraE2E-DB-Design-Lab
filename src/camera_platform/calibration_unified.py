"Immutable DB-source core calibration workflows and explicit scoped activation."

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

KIND = "calibration_unified_workflow"
MODELS = "calibration_unified_models"
ACTIVATIONS = "calibration_unified_activations"
ROOT = "/api/v1/calibration-lab/unified-workflows"
# Retain fixed-density plus adaptive full-sensor evidence and its portable ZIP.
# Includes the ZIP itself and all 57 direct kernels/ray evidence records.
MAX_BYTES = 6 * 1024**3
# Cold-cache qualification traces 57x21 new optical nodes plus nested references.
WORKER_TIMEOUT_S = 21600


def active(service, *, include_pending=False):
    result = {}
    for row in sorted(service.registry.list(ACTIVATIONS), key=lambda r: (r["created_at"], r["id"])):
        status = service.registry.get_run(row["run_id"])["status"]
        if status == "succeeded" or (include_pending and status == "running"):
            result[row["target_key"]] = row
    return result


def spec(payload):
    allowed = {
        "source",
        "color_model_id",
        "core_model_id",
        "seed",
        "name",
        "target_camera_id",
        "apply",
    }
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("Unified workflow requires DB source and exact color/core parent models")
    seed = payload.get("seed", secrets.randbelow(2**31 - 400000))
    if type(seed) is not int or not 0 <= seed < 2**31 - 400000:
        raise ValueError("Invalid unified acquisition seed")
    camera = payload.get("target_camera_id", "synthetic-db-unified")
    db._identity(camera, "synthetic unified target", r"synthetic-db-[A-Za-z0-9_.-]{1,100}")
    if type(payload.get("apply", True)) is not bool:
        raise ValueError("apply must be boolean")
    name = payload.get("name", "통합 카메라 · QE·CCM·Dark·PTC·MTF·PSF")
    if not isinstance(name, str) or not 1 <= len(name) <= 160:
        raise ValueError("Invalid unified name")
    source._id(payload.get("color_model_id"), "db-color-model")
    db._identity(payload.get("core_model_id"), "core parent", r"db-core-model-[0-9a-f]{32}")
    from .calibration_adaptive_anchors import POLICY as ADAPTIVE_POLICY
    from .calibration_anchor_density import POLICY as ANCHOR_POLICY
    from .calibration_direct_convergence import POLICY as DIRECT_POLICY
    from .calibration_psf_convergence import POLICY
    from .calibration_spatial_psf import POLICY as SPATIAL_POLICY

    return dict(
        psf_convergence_policy=POLICY,
        direct_psf_policy=DIRECT_POLICY,
        spatial_psf_policy=SPATIAL_POLICY,
        anchor_density_policy=ANCHOR_POLICY,
        adaptive_anchor_policy=ADAPTIVE_POLICY,
        source=payload.get("source"),
        color_model_id=payload["color_model_id"],
        core_model_id=payload["core_model_id"],
        seed=seed,
        name=name,
        target_camera_id=camera,
        apply=payload.get("apply", True),
    )


def parents(service, config, context):
    from . import calibration_db_core as core

    color = source.get_model(service, config["color_model_id"])
    source._verify_color(service, color["calibration_record_snapshot"])
    electrical = core.get_model(service, config["core_model_id"])
    keys = (
        "run_id",
        "candidate_id",
        "condition_id",
        "manifest_sha256",
        "candidate_sha256",
        "condition_sha256",
        "run_request_sha256",
        "run_result_sha256",
    )
    for p in (color, electrical):
        if any(p["source_context"].get(k) != context.get(k) for k in keys):
            raise ValueError(
                "Unified parents must match the exact DB source/candidate/condition and provenance"
            )
        if p["source_kind"] != "synthetic" or p["physical_validation"] != "not_established":
            raise ValueError("Unified v1 supports explicitly synthetic parents only")
    result = core.get(service, electrical["run_id"])["result"]["summary"]
    from . import calibration_color, calibration_db_core_engine

    truth = json.loads(core.file(service, electrical["run_id"], "authored-truth.json").read_bytes())
    if truth != calibration_db_core_engine.authored_truth(
        result["baseline_profiles"], ["electronics", "optics"]
    ):
        raise IntegrityError("Core parent synthetic reference differs from its declared generator")
    task = json.loads(
        calibration_color.file(service, color["color_calibration_id"], "inputs.json").read_bytes()
    )
    if digest(task) != color["calibration_record_snapshot"]["input_sha256"]:
        raise IntegrityError("Color parent original input changed")
    return color, electrical, result, task


def submit(service, payload):
    from . import calibration_unified_engine as engine

    code = db._current(service)
    config = spec(payload)
    run, context, prepared = source._prepare(service, config["source"])
    color, core, result, task = parents(service, config, context)
    profiles = engine.compose(
        prepared, color, core, result, task, config["target_camera_id"], service.workspace
    )
    excluded = {v for c in result["comparisons"] for s in c["series"].values() for v in s["seeds"]}
    origin_seed = color["calibration_record_snapshot"]["evidence"]["inputs"]["source_notes"]["seed"]
    excluded.update((origin_seed + 100, origin_seed + 101))
    frozen = canonical(profiles).encode()
    profile_blob = service.registry.store_blob(frozen, "parent-profiles.json", "application/json")
    artifacts = [profile_blob]
    files = {}
    for name, data in source._files(prepared).items():
        a = service.registry.store_blob(
            data, name, mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        artifacts.append(a)
        files[name] = {"sha256": a["sha256"], "bytes": len(data)}
    pins = {p["id"]: p["content_hash"] for p in (color, core)}
    key = digest({"source": config["source"], "target": config["target_camera_id"]})
    with lab._lock(service):
        again, current = db.source_selection(service, config["source"])
        if again != run or current != context or db._current(service) != code:
            raise IntegrityError("Unified source changed during input freeze")
        previous = active(service).get(key)
        document = {
            "name": config["name"],
            "job_kind": KIND,
            "plugin_id": "camera_unified_v1",
            "specification": config,
            "source_context": context,
            "source_files": files,
            "source_code": code,
            "parent_pins": pins,
            "frozen_profiles_sha256": profile_blob["sha256"],
            "excluded_seeds": sorted(excluded),
            "target_key": key,
            "expected_active_model_id": previous["model_id"] if previous else None,
            "input_artifacts": lab._unique_artifacts(artifacts),
            "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        }
        return service.registry.submit(document, digest(document), force=True)


def directory(service, identity):
    db._identity(identity, "unified run", r"run-[0-9a-f]{32}")
    return service.registry.directory / "calibration-unified" / identity


def get(service, identity):
    directory(service, identity)
    row, bound = service.registry.get_run_with_result_binding(identity)
    if row.get("job_kind") != KIND:
        raise ValueError("Selected execution is not a unified calibration workflow")
    if row["status"] == "succeeded":
        if bound is None or json.loads(bound["path"].read_bytes()) != row["result"]["summary"]:
            raise IntegrityError("Unified result differs from its immutable binding")
    elif row["status"] in {"queued", "running"}:
        path = directory(service, identity) / "progress.json"
        if path.is_file() and not path.is_symlink():
            row["progress"] = json.loads(path.read_bytes())
    return row


def get_model(service, identity):
    db._identity(identity, "unified camera model", r"unified-camera-model-[0-9a-f]{32}")
    row = service.registry.get(MODELS, identity)
    profile = row["camera_profile"]
    if (
        row.get("schema") != "camera_unified_model_v1"
        or digest({k: v for k, v in profile.items() if k != "profile_sha256"})
        != row["profile_sha256"]
        or profile["profile_sha256"] != row["profile_sha256"]
    ):
        raise IntegrityError("Unified camera profile failed its pinned hash")
    run = get(service, row["run_id"])
    if run["status"] != "succeeded" or run["result"]["summary"]["model_id"] != identity:
        raise IntegrityError("Unified profile has no completed owner workflow")
    return row


def execute(service, job):
    from . import calibration_db_color_engine as loader
    from . import calibration_unified_engine as engine
    from .calibration_unified_report import publish

    if get(service, job["id"])["status"] != "running" or db._current(service) != job["source_code"]:
        raise IntegrityError("Unified execution source/claim changed")
    output = directory(service, job["id"])
    output.mkdir(parents=True, exist_ok=False)

    def progress(message):
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Unified workflow cancelled")
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
                raise IntegrityError("Unified frozen source bytes changed")
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
            raise IntegrityError("Unified reconstructed source differs from queued inputs")
        config = job["specification"]
        color, core, _, _ = parents(service, config, context)
        if {p["id"]: p["content_hash"] for p in (color, core)} != job["parent_pins"]:
            raise IntegrityError("Unified parent revisions changed")
        binding = service.registry.artifact_binding(
            job["frozen_profiles_sha256"], owner_kind="runs", owner_id=job["id"]
        )
        frozen = binding["path"].read_bytes()
        if hashlib.sha256(frozen).hexdigest() != job["frozen_profiles_sha256"]:
            raise IntegrityError("Frozen unified profiles changed")
        (output / "parent-profiles.json").write_bytes(frozen)
        profiles = json.loads(frozen)
        prepared["direct_workspace"] = service.workspace
        summary = json.loads(
            canonical(
                engine.execute(
                    prepared, profiles, output, config["seed"], job["excluded_seeds"], progress
                )
            )
        )
    # Never prepare an apparently applied report from a stale-source computation.
    # The numerical checkpoint remains inspectable and can be hash-qualified
    # separately; it does not become a model or an activation.
    if lab._source_code() != job["source_code"]:
        (output / "source-invalidated-numerical-summary.json").write_text(
            canonical({"status": "not_published", "reason": "source_changed", "summary": summary})
        )
        raise IntegrityError("Unified source changed before publication")
    model_id = (
        "unified-camera-model-"
        + digest({"run": job["id"], "profile": summary["profile_sha256"]})[:32]
    )
    from . import calibration_db_core as core_service

    model = service.registry.put_verified_record(
        MODELS,
        {
            "schema": "camera_unified_model_v1",
            "name": config["name"],
            "run_id": job["id"],
            "source_context": context,
            "camera_profile": summary["camera_profile"],
            "profile_sha256": summary["profile_sha256"],
            "target_key": job["target_key"],
            "target_camera_id": config["target_camera_id"],
            "parent_pins": job["parent_pins"],
            "source_code": job["source_code"],
            "source_kind": "synthetic",
            "physical_validation": "not_established",
            "activation_scope": (
                "synthetic camera with qualified direct-boundary anchors and full-sensor "
                "tiled RAW/ISP and scoped local holdout"
            ),
        },
        [(source.MODELS, color), (core_service.MODELS, core)],
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
                    "schema": "camera_unified_activation_v1",
                    "target_key": job["target_key"],
                    "model_id": model_id,
                    "previous_model_id": current,
                    "run_id": job["id"],
                    "source_context": job["source_context"],
                    "profile_sha256": summary["profile_sha256"],
                },
                [(MODELS, model)],
            )
            summary["activation"] = {"status": "applied", "id": activation["id"]}
        else:
            summary["activation"] = {
                "status": "not_requested" if not config["apply"] else "blocked",
                "reason": "Numerical tolerance not met"
                if not summary["validation"]["numerical_tolerance_pass"]
                else ("Active version changed during calculation")
                if config["apply"]
                else "Inspect-only workflow",
            }
    progress("차트·PSF·오차 보고서와 ZIP 생성")
    publish(output, summary)
    artifacts, files, size = [], {}, 0
    input_hashes = {a["sha256"] for a in job["input_artifacts"]}
    for p in sorted(output.rglob("*")):
        if p.is_symlink():
            raise IntegrityError("Unified output cannot contain symlinks")
        if not p.is_file() or p.name == "progress.json":
            continue
        size += p.stat().st_size
        if size > MAX_BYTES:
            raise ValueError("Unified output exceeds bounded 6 GiB optical-evidence budget")
        name = db._relative(p.relative_to(output).as_posix())
        a = service.registry.store_file(
            p, name, mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        if a["sha256"] not in input_hashes:
            artifacts.append(a)
        files[name] = a["sha256"]
    summary["files"] = files
    if db._current(service) != job["source_code"]:
        raise IntegrityError("Unified source changed during execution")
    result = service.registry.store_blob(
        canonical(summary).encode(), "unified-result.json", "application/json"
    )
    progress("완료 · 가상 실측·보정 전후 차트·오차 보고서 보존")
    return {"summary": summary, "artifacts": [result, *lab._unique_artifacts(artifacts)]}


def file(service, identity, relative):
    row = get(service, identity)
    if row["status"] != "succeeded":
        raise ValueError("Unified files require a completed workflow")
    checksum = row["result"]["summary"]["files"].get(db._relative(relative))
    if checksum is None:
        raise KeyError("File is outside the unified result manifest")
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
                    "parents": {
                        k: r["specification"][k] for k in ("color_model_id", "core_model_id")
                    },
                    "model_id": s.get("model_id"),
                    "report_url": s.get("report_url"),
                    "activation": s.get("activation"),
                    "created_at": r["created_at"],
                }
            )
        return {"items": rows}

    @app.get(ROOT + "/options", tags=["calibration_lab"])
    def options():
        from . import calibration_db_core as core

        color_models = [
            source.get_model(service, a["model_id"]) for a in source.active_models(service).values()
        ]
        core_models = [
            core.get_model(service, a["model_id"]) for a in core.active(service).values()
        ]

        def light(rows):
            return [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "source_context": {
                        k: r["source_context"].get(k)
                        for k in ("run_id", "candidate_id", "condition_id")
                    },
                }
                for r in sorted(rows, key=lambda r: r["created_at"], reverse=True)
            ]

        return {"color": light(color_models), "core": light(core_models)}

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

    @app.get(("/api/v1/db-design/active-unified-models"), tags=["db_design"])
    def models():
        return {
            "items": [
                {**get_model(service, a["model_id"]), "activation": a}
                for a in active(service).values()
            ]
        }
