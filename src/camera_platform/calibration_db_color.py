"""Versioned QE/CCM DB models, explicit activation and fresh spectral recapture."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import tempfile
import zipfile
from pathlib import Path

from . import calibration_color, calibration_db, calibration_lab
from .registry import ConflictError, IntegrityError, canonical, digest, now

ROUTE = "/api/v1/db-design"
MODELS = "calibration_db_color_models"
ACTIVATIONS = "calibration_db_color_activations"
SCHEMA = "camera_db_color_model_v1"
JOB_KIND = "calibration_db_color_replay"
MAX_BYTES = 512 * 1024**2


def _id(value, prefix):
    return calibration_db._identity(value, prefix, prefix + r"-[0-9a-f]{32}")


def _prepare(service, source, root=None):
    from . import calibration_db_color_engine as engine
    from .reference_workshop_job import _directory

    run, context = calibration_db.source_selection(service, source)
    prepared = engine.prepare(
        service.workspace,
        root or _directory(service, run["id"]),
        run["result"]["summary"] if root is None else None,
        context["candidate_id"],
        condition_id=context["condition_id"],
        expected_manifest_sha256=context["manifest_sha256"],
    )
    return run, context, prepared


def _files(prepared):
    original = prepared["frozen_files"]
    derived = prepared.get("derived_files", {})
    if set(original) & set(derived):
        raise IntegrityError("Derived spectral inputs must not replace original DB bytes")
    combined = {**original, **derived}
    if not combined or len(combined) > 1024:
        raise ValueError("DB model requires a bounded frozen source bundle")
    if sum(len(value) for value in combined.values()) > MAX_BYTES:
        raise ValueError("DB source bundle exceeds 512 MiB")
    for name, value in combined.items():
        calibration_db._relative(name)
        if not isinstance(value, bytes):
            raise IntegrityError("DB source bundle must preserve bytes")
    return combined


def _same_application(resolved, application):
    keys = {
        "applied_response": (
            "wavelength_nm",
            "qe_rgb",
            "qe_rggb",
            "angular_policy",
            "green_policy",
            "application",
        ),
        "isp_adapter": (
            "reference_white_e",
            "white_balance",
            "exposure_scale",
            "baseline_matrix",
            "matrix",
            "electron_to_output_matrix",
            "baseline_electron_to_output_matrix",
            "matrix_convention",
            "target_space",
        ),
    }
    for section, fields in keys.items():
        for key in fields:
            if key not in resolved.get(section, {}) or resolved[section][key] != application.get(
                section, {}
            ).get(key):
                raise IntegrityError(
                    "DB execution differs from the resolved profile: " + section + "." + key
                )


def _verify_color(service, record):
    for name in record["files"]:
        path = calibration_color.file(service, record["id"], name)
        if name in {"qe-profile.json", "ccm-profile.json"}:
            key = "qe" if name.startswith("qe-") else "ccm"
            if json.loads(path.read_bytes()) != record[key]["profile"]:
                raise IntegrityError(
                    "Retained color profile bytes differ from the calibrated record"
                )


def create_model(service, payload):
    from . import calibration_db_color_contracts as contracts
    from . import calibration_db_color_engine as engine

    code = calibration_db._current(service)
    spec = contracts.model_spec(payload)
    color = calibration_color.get(service, spec["color_calibration_id"])
    _verify_color(service, color)
    run, context, prepared = _prepare(service, spec["source"])
    projection = engine.projection(prepared)
    resolved = contracts.freeze_model(spec, context, color, projection)
    application = engine.binding(prepared, color)
    _same_application(resolved, application)
    target_key = digest(
        {"source": spec["source"], "camera_id": spec["binding"]["target_camera_id"]}
    )
    identity = (
        "db-color-model-"
        + digest(
            {"specification": spec, "source": context, "color": color["content_hash"], "code": code}
        )[:32]
    )
    with calibration_lab._lock(service):
        try:
            return get_model(service, identity)
        except KeyError:
            pass
        artifacts, files = [], {}
        for relative, data in sorted(_files(prepared).items()):
            descriptor = service.registry.store_blob(
                data, relative, mimetypes.guess_type(relative)[0] or "application/octet-stream"
            )
            artifacts.append(descriptor)
            files[relative] = {"sha256": descriptor["sha256"], "bytes": len(data)}
        again, again_context = calibration_db.source_selection(service, spec["source"])
        if again != run or again_context != context or calibration_db._current(service) != code:
            raise IntegrityError("DB source changed while creating its derived model")
        return service.registry.put_verified_record(
            MODELS,
            {
                "schema": SCHEMA,
                "name": spec["name"],
                "specification": spec,
                "source_context": context,
                "target_key": target_key,
                "binding": spec["binding"],
                "color_calibration_id": color["id"],
                "calibration_record_snapshot": color,
                "resolved": resolved,
                "profile_sha256": resolved["profile_sha256"],
                "projection": projection,
                "application": application,
                "source_files": files,
                "manifest_bindings": prepared.get("manifest_bindings", {}),
                "artifacts": calibration_lab._unique_artifacts(artifacts),
                "source_code": code,
                "source_kind": "synthetic",
                "physical_validation": "not_established",
                "activation_scope": "derived DB camera target; explicit fresh color-replay jobs",
            },
            [(calibration_color.COLLECTION, color)],
            identity=identity,
        )


def get_model(service, identity):
    _id(identity, "db-color-model")
    model = service.registry.get(MODELS, identity)
    if (
        model.get("schema") != SCHEMA
        or model["resolved"].get("profile_sha256") != model["profile_sha256"]
        or digest({k: v for k, v in model["resolved"].items() if k != "profile_sha256"})
        != model["profile_sha256"]
    ):
        raise IntegrityError("DB color model profile identity differs")
    if (
        calibration_color.get(service, model["color_calibration_id"])
        != model["calibration_record_snapshot"]
    ):
        raise IntegrityError("DB model's color calibration reference changed")
    return model


def active_models(service):
    latest = {}
    for row in sorted(
        service.registry.list(ACTIVATIONS), key=lambda item: (item["created_at"], item["id"])
    ):
        latest[row["target_key"]] = row
    return latest


def activate(service, identity, payload):
    calibration_db._current(service)
    if not isinstance(payload, dict) or set(payload) != {"expected_active_model_id"}:
        raise ValueError("Activation requires expected_active_model_id for compare-and-swap")
    expected = payload["expected_active_model_id"]
    if expected is not None:
        _id(expected, "db-color-model")
    with calibration_lab._lock(service):
        model = get_model(service, identity)
        previous = active_models(service).get(model["target_key"])
        current = previous["model_id"] if previous else None
        if current != expected:
            raise ConflictError("Active DB model changed; refresh before applying this revision")
        if current == identity:
            return previous
        return service.registry.put_verified_record(
            ACTIVATIONS,
            {
                "schema": "camera_db_color_activation_v1",
                "model_id": identity,
                "target_key": model["target_key"],
                "previous_model_id": current,
                "profile_sha256": model["profile_sha256"],
                "source_context": model["source_context"],
                "binding": model["binding"],
                "scope": model["activation_scope"],
            },
            [(MODELS, model)],
        )


def submit(service, payload):
    code = calibration_db._current(service)
    if not isinstance(payload, dict) or set(payload) != {"model_id"}:
        raise ValueError("DB color replay requires model_id")
    with calibration_lab._lock(service):
        model = get_model(service, payload["model_id"])
        activation = active_models(service).get(model["target_key"])
        if activation is None or activation["model_id"] != model["id"]:
            raise ConflictError("Activate this DB model before requesting its fresh simulation")
        if model["source_code"] != code:
            raise ConflictError("DB model execution source changed; create a new model revision")
        document = {
            "name": "DB QE · CCM Macbeth · " + model["name"],
            "job_kind": JOB_KIND,
            "plugin_id": "camera_db_color_replay_v1",
            "specification": {"model_id": model["id"]},
            "model_snapshot": model,
            "activation_snapshot": activation,
            "input_artifacts": calibration_lab._unique_artifacts(
                [*model["artifacts"], *model["calibration_record_snapshot"]["artifacts"]]
            ),
            "source_code": code,
            "holdout_plan": _holdout_plan(service, model),
            "default_worker_timeout_seconds": 1800,
        }
        return service.registry.submit(document, digest(document), force=True)


def _holdout_plan(service, model):
    from . import calibration_db_holdout

    task = json.loads(
        calibration_color.file(service, model["color_calibration_id"], "inputs.json").read_bytes()
    )
    if digest(task) != model["calibration_record_snapshot"]["input_sha256"]:
        raise IntegrityError("Color task input changed before automatic validation")
    return calibration_db_holdout.plan(task)


def workflow(service, payload):
    """One explicit action: calibrate/apply, CAS activate, then queued automatic validation."""
    from .calibration_db_color_contracts import model_spec

    calibration_db._current(service)
    if (
        not isinstance(payload, dict)
        or "model" not in payload
        or set(payload)
        not in (
            {"model", "calibration"},
            {"model", "calibration_json"},
            {"model", "color_calibration_id"},
        )
    ):
        raise ValueError(
            "Workflow requires a DB model and calibration input or saved calibration ID"
        )
    config = payload["model"]
    if not isinstance(config, dict) or set(config) != {"name", "source", "binding"}:
        raise ValueError("Workflow model requires name, source and explicit synthetic binding")
    validated = model_spec({**config, "color_calibration_id": "color-calibration-" + "0" * 32})
    config = {key: validated[key] for key in ("name", "source", "binding")}
    calibration_db.source_selection(service, config["source"])
    target_key = digest(
        {"source": config["source"], "camera_id": config["binding"]["target_camera_id"]}
    )
    prior = active_models(service).get(target_key)
    if "calibration_json" in payload:
        payload = {
            "model": config,
            "calibration": calibration_color.decode_input_json(payload["calibration_json"]),
        }
    color = (
        calibration_color.create(service, payload["calibration"])
        if "calibration" in payload
        else calibration_color.get(service, payload["color_calibration_id"])
    )
    response = {
        "schema": "camera_db_color_workflow_v1",
        "calibration": color,
        "model": None,
        "activation": None,
        "run": None,
    }
    if color.get("status") != "pass":
        return {**response, "status": "calibration_failed"}
    try:
        model = create_model(service, {**config, "color_calibration_id": color["id"]})
        response["model"] = model
        response["activation"] = activate(
            service, model["id"], {"expected_active_model_id": prior["model_id"] if prior else None}
        )
        response["run"] = submit(service, {"model_id": model["id"]})
    except (ConflictError, ValueError) as exc:
        if isinstance(exc, IntegrityError):
            raise
        return {**response, "status": "blocked", "error": str(exc)}
    return {**response, "status": "submitted"}


def _directory(service, identity):
    _id(identity, "run")
    return service.registry.directory / "calibration-db-color-replays" / identity


def get_run(service, identity):
    directory = _directory(service, identity)
    run, binding = service.registry.get_run_with_result_binding(identity)
    if run.get("job_kind") != JOB_KIND:
        raise ValueError("This run is not a DB QE/CCM recapture")
    if run["status"] == "succeeded":
        if binding is None:
            raise IntegrityError("DB color replay has no bound summary artifact")
        if json.loads(binding["path"].read_bytes()) != run["result"]["summary"]:
            raise IntegrityError("DB color summary differs from its saved artifact")
    elif run["status"] in {"queued", "running"}:
        path = directory / "progress.json"
        if path.is_file() and not path.is_symlink():
            try:
                run["progress"] = json.loads(path.read_bytes())
            except (OSError, ValueError):
                pass
    return run


def execute(service, job):
    from . import calibration_db_color_engine as engine
    from .calibration_db_color_report import render_db_color_report

    if get_run(service, job["id"])["status"] != "running":
        raise ValueError("DB color replay must be claimed by its worker")
    if calibration_db._current(service) != job["source_code"]:
        raise IntegrityError("DB color runtime changed after submission")
    model = get_model(service, job["specification"]["model_id"])
    _verify_color(service, model["calibration_record_snapshot"])
    activation = service.registry.get(ACTIVATIONS, job["activation_snapshot"]["id"])
    if model != job["model_snapshot"] or activation != job["activation_snapshot"]:
        raise IntegrityError("Frozen DB model or activation changed")
    # Later activation does not retarget an already submitted immutable job.
    directory = _directory(service, job["id"])
    directory.mkdir(parents=True, exist_ok=False)

    def progress(message):
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("DB color replay cancelled")
        (directory / "progress.json").write_text(canonical({"message": str(message)}))

    with tempfile.TemporaryDirectory(prefix="camera-db-color-source-") as temporary:
        root = Path(temporary)
        for relative, row in model["source_files"].items():
            path = root / calibration_db._relative(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            binding = service.registry.artifact_binding(
                row["sha256"], owner_kind=MODELS, owner_id=model["id"]
            )
            data = binding["path"].read_bytes()
            if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise IntegrityError("Frozen DB model input changed")
            path.write_bytes(data)
        source = model["source_context"]
        prepared = engine.prepare(
            service.workspace,
            root,
            None,
            source["candidate_id"],
            condition_id=source["condition_id"],
            expected_manifest_sha256=source["manifest_sha256"],
        )
        actual = {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in _files(prepared).items()
        }
        color = model["calibration_record_snapshot"]
        if (
            actual != model["source_files"]
            or engine.projection(prepared) != model["projection"]
            or engine.binding(prepared, color) != model["application"]
        ):
            raise IntegrityError("DB spectral transport or profile application changed")
        _same_application(model["resolved"], engine.binding(prepared, color))
        summary = engine.execute(prepared, color, directory, progress=progress)
        plan = job.get(
            "holdout_plan",
            {"status": "not_evaluated", "reason": "Legacy replay has no validation plan"},
        )
        if plan["status"] == "planned":
            from . import calibration_db_holdout

            task = json.loads(
                calibration_color.file(service, color["id"], "inputs.json").read_bytes()
            )
            if digest(task) != plan["task_sha256"]:
                raise IntegrityError(
                    "Automatic holdout differs from its submitted calibration input"
                )
            validation = calibration_db_holdout.execute(
                prepared, model, task, directory / "holdout", plan["seed"], job["id"], progress
            )
            summary["holdout"] = {
                **validation,
                "report_path": "holdout/report.html",
                "json_path": "holdout/report.json",
                "bundle_path": "holdout/evidence.zip",
                "path_prefix": "holdout/",
            }
        else:
            summary["holdout"] = plan
    summary.update(
        id=job["id"],
        run_id=job["id"],
        model_id=model["id"],
        model_name=model["name"],
        activation_id=activation["id"],
        target_key=model["target_key"],
        binding=model["binding"],
        source_context=model["source_context"],
        color_calibration_id=model["color_calibration_id"],
        applied_profile_sha256=model["profile_sha256"],
        application=model["application"],
        physical_validation="not_established",
        fresh_simulation=True,
        report_generated_at=now(),
        source_code=job["source_code"],
        files_base=f"{ROUTE}/color-replays/{job['id']}/files/",
        report_url=f"{ROUTE}/color-replays/{job['id']}/files/report.html",
    )
    (directory / "model.json").write_text(canonical(model))
    (directory / "qe-profile.json").write_text(
        canonical(model["calibration_record_snapshot"]["qe"]["profile"])
    )
    (directory / "ccm-profile.json").write_text(
        canonical(model["calibration_record_snapshot"]["ccm"]["profile"])
    )
    (directory / "request.json").write_text(canonical(job))
    # Bind all engine outputs before rendering; report's own hash is outside itself.
    summary["files"] = {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != "progress.json"
    }
    (directory / "report.html").write_bytes(
        render_db_color_report(
            summary,
            image_resolver=lambda name: (directory / calibration_db._relative(name)).read_bytes(),
        )
    )
    (directory / "report.json").write_text(canonical(summary))
    artifacts, files, total = [], {}, 0
    input_hashes = {artifact["sha256"] for artifact in job["input_artifacts"]}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise IntegrityError("DB color output cannot contain symlinks")
        if not path.is_file() or path.name == "progress.json":
            continue
        total += path.stat().st_size
        if total > MAX_BYTES:
            raise ValueError("DB color output exceeds 512 MiB")
        relative = calibration_db._relative(path.relative_to(directory).as_posix())
        artifact = service.registry.store_file(
            path, relative, mimetypes.guess_type(relative)[0] or "application/octet-stream"
        )
        files[relative] = artifact["sha256"]
        if artifact["sha256"] not in input_hashes:
            artifacts.append(artifact)
    summary["files"] = files
    if calibration_db._current(service) != job["source_code"]:
        raise IntegrityError("DB color runtime changed during execution")
    progress("완료 · 갱신된 DB 모델의 RAW·ISP와 보고서 보존")
    result = service.registry.store_blob(
        canonical(summary).encode(), "db-color-result.json", "application/json"
    )
    return {
        "summary": summary,
        "artifacts": [result, *calibration_lab._unique_artifacts(artifacts)],
    }


def file(service, identity, relative):
    run = get_run(service, identity)
    if run["status"] != "succeeded":
        raise ValueError("Files require a completed DB color replay")
    checksum = run["result"]["summary"]["files"].get(calibration_db._relative(relative))
    if checksum is None:
        raise KeyError("File is outside the replay manifest")
    return service.registry.artifact_binding(checksum, owner_kind="runs", owner_id=identity)["path"]


def _validation_entry(row):
    summary = (row.get("result") or {}).get("summary", {})
    holdout = summary.get("holdout", {})
    return {
        "status": holdout.get("status", "not_evaluated"),
        "aggregate": holdout.get("aggregate", {}),
        "report_url": (
            summary.get("files_base", "") + holdout["report_path"]
            if holdout.get("report_path")
            else None
        ),
    }


def install_routes(app, service):
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    @app.get(ROUTE + "/color-models", tags=["db_design"])
    def models():
        active = active_models(service)
        return {
            "items": [
                {
                    **{
                        k: row[k]
                        for k in (
                            "id",
                            "name",
                            "target_key",
                            "binding",
                            "source_context",
                            "color_calibration_id",
                            "created_at",
                            "profile_sha256",
                        )
                    },
                    "active": active.get(row["target_key"], {}).get("model_id") == row["id"],
                }
                for row in service.registry.list(MODELS)
            ]
        }

    @app.post(ROUTE + "/color-models", status_code=201, tags=["db_design"])
    def model_create(payload: dict):
        return create_model(service, payload)

    @app.get(ROUTE + "/color-models/{identity}", tags=["db_design"])
    def model_get(identity: str):
        return get_model(service, identity)

    @app.post(ROUTE + "/color-models/{identity}/activate", status_code=201, tags=["db_design"])
    def model_activate(identity: str, payload: dict):
        return activate(service, identity, payload)

    @app.get(ROUTE + "/active-color-models", tags=["db_design"])
    def active():
        return {"items": list(active_models(service).values())}

    @app.get(ROUTE + "/color-replays", tags=["db_design"])
    def runs(color_calibration_id: str | None = None):
        return {
            "items": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "status": row["status"],
                    "model_id": row["specification"]["model_id"],
                    "result_url": "/calibration-db-color.html?run=" + row["id"],
                    "color_calibration_id": row["model_snapshot"]["color_calibration_id"],
                    "created_at": row["created_at"],
                    "source": {
                        k: row["model_snapshot"]["source_context"].get(k)
                        for k in ("run_id", "candidate_id", "condition_id")
                    },
                    "validation": _validation_entry(row),
                }
                for row in service.registry.list_runs(job_kinds={JOB_KIND})
                if color_calibration_id is None
                or row["model_snapshot"]["color_calibration_id"] == color_calibration_id
            ]
        }

    @app.post(ROUTE + "/color-replays", status_code=202, tags=["db_design"])
    def replay_create(payload: dict):
        return submit(service, payload)

    @app.post(ROUTE + "/color-workflows", status_code=202, tags=["db_design"])
    @app.post(
        "/api/v1/calibration-lab/color-db-workflows", status_code=202, tags=["calibration_lab"]
    )
    def color_workflow(payload: dict):
        return workflow(service, payload)

    @app.get(ROUTE + "/color-replays/{identity}", tags=["db_design"])
    def replay_get(identity: str):
        return get_run(service, identity)

    @app.post(ROUTE + "/color-replays/{identity}/cancel", tags=["db_design"])
    def replay_cancel(identity: str):
        get_run(service, identity)
        return service.registry.cancel(identity)

    @app.get(ROUTE + "/color-replays/{identity}/files/{relative:path}", tags=["db_design"])
    def replay_file(identity: str, relative: str):
        return FileResponse(
            file(service, identity, relative),
            media_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
        )

    @app.get(ROUTE + "/color-replays/{identity}/bundle", tags=["db_design"])
    def replay_bundle(identity: str):
        run = get_run(service, identity)
        if run["status"] != "succeeded":
            raise ValueError("Evidence ZIP requires a completed replay")
        with tempfile.NamedTemporaryFile(
            prefix="db-color-evidence-", suffix=".zip", delete=False
        ) as handle:
            path = Path(handle.name)
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("record.json", canonical(run))
                for relative, checksum in run["result"]["summary"]["files"].items():
                    relative = calibration_db._relative(relative)
                    binding = service.registry.artifact_binding(
                        checksum, owner_kind="runs", owner_id=identity
                    )
                    archive.write(binding["path"], relative)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return FileResponse(
            path,
            media_type="application/zip",
            filename=identity + ".zip",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )
