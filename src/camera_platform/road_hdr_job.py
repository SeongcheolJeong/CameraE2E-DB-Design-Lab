"""Common Registry/Worker lifecycle for road HDR acquire, replay and evaluation."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .registry import IntegrityError, canonical, digest
from .road_design_inputs import write_json
from .road_hdr_inputs import bound_specification, freeze_first, freeze_sequences, resource_plan
from .road_hdr_study import run, verify_manifest
from .road_rgb_source import file_sha256

SCHEMA = "camera_road_hdr_job_v1"
JOB_KIND = "road_hdr_study"
ROUTE = "/api/v1/road-hdr-studies"
WORKER_TIMEOUT_S = 7 * 24 * 3600


def specification(payload):
    required = {"schema", "name", "package_path", "specification_sha256", "action"}
    if not isinstance(payload, dict) or set(payload) - {"profiles"} != required:
        raise ValueError("Declare exact versioned road HDR job fields.")
    if payload["schema"] != SCHEMA or payload["action"] not in {
        "first-photo",
        "acquire",
        "reprocess",
        "evaluate",
    }:
        raise ValueError("Unsupported HDR action.")
    if not isinstance(payload["name"], str) or not 1 <= len(payload["name"]) <= 160:
        raise ValueError("Name must contain 1..160 characters.")
    if (
        not isinstance(payload["package_path"], str)
        or not payload["package_path"]
        or ".." in Path(payload["package_path"]).parts
    ):
        raise ValueError("Choose a bounded package directory.")
    sha = payload["specification_sha256"]
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("Frozen specification SHA256 required.")
    if "profiles" in payload:
        profiles = payload["profiles"]
        if (
            payload["action"] != "reprocess"
            or not isinstance(profiles, list)
            or not profiles
            or not all(isinstance(p, str) for p in profiles)
            or len(set(profiles)) != len(profiles)
            or any(p not in {"static", "global", "local"} for p in profiles)
        ):
            raise ValueError("Choose distinct supported reprocessing profiles.")
    return dict(payload)


def authorized_path(service, path):
    root = service.workspace.resolve()
    target = (root / path).resolve()
    roots = [root] + [
        Path(p).resolve()
        for p in os.environ.get("CAMERA_PLATFORM_ROAD_HDR_PACKAGE_ROOTS", "").split(os.pathsep)
        if p
    ]
    if not any(target.is_relative_to(r) for r in roots):
        raise ValueError("Package outside configured storage roots.")
    return target


def _package(service, spec):
    path = authorized_path(service, spec["package_path"])
    _, binding = bound_specification(path)
    if binding["sha256"] != spec["specification_sha256"]:
        raise IntegrityError("HDR specification changed.")
    return path


def submit(service, payload):
    spec = specification(payload)
    package = _package(service, spec)
    # One writer per acquisition package. Previously completed stages are replayable.
    for job in service.registry.list_runs():
        if (
            job.get("job_kind") == JOB_KIND
            and job["status"] in {"queued", "running"}
            and authorized_path(service, job["specification"]["package_path"]) == package
        ):
            raise ValueError("An HDR job already owns this package.")
    document = {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        "physical_validation": "not_established",
    }
    return service.registry.submit(document, digest(document))


def execute(service, job):
    if (
        job.get("job_kind") != JOB_KIND
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Claimed running HDR job required.")
    spec = specification(job["specification"])
    package = _package(service, spec)

    def progress(message):
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("HDR job cancelled.")
        write_json(package / "progress.json", {"action": spec["action"], "message": message})

    if "profiles" in spec:
        from .road_hdr_study import reprocess

        result = reprocess(
            service.workspace, package, profiles=tuple(spec["profiles"]), on_progress=progress
        )
    else:
        result = run(service.workspace, package, spec["action"], on_progress=progress)
    viewer = None
    if result.get("result_directory"):
        relative = str((Path(result["result_directory"]) / "report.html").relative_to(package))
        viewer = ROUTE + "/" + job["id"] + "/files/" + relative
    elif (package / "decision.json").exists():
        viewer = ROUTE + "/" + job["id"] + "/files/decision-report.html"
    manifest_bindings = {
        str(p.relative_to(package)): file_sha256(p)
        for p in package.rglob("*manifest.json")
        if p.is_file()
    }
    summary = {
        "schema": SCHEMA,
        "action": spec["action"],
        "status": result.get("status", "native_RAW_preserved"),
        "specification_sha256": spec["specification_sha256"],
        "viewer_path": viewer,
        "package_path": spec["package_path"],
        "original_RAW_retained": True,
        "result_directory": result.get("result_directory"),
        "manifest_bindings": manifest_bindings,
        "physical_validation": "not_established",
    }
    artifact = service.registry.store_blob(
        canonical(summary).encode(), "road-hdr-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [artifact]}


def get(service, identity):
    record = service.registry.get_run(identity)
    if record.get("job_kind") != JOB_KIND:
        raise ValueError("Selected Run is not a road HDR job.")
    package = _package(service, specification(record["specification"]))
    if record["status"] == "succeeded":
        result = record["result"]
        artifact = result["artifacts"][0]
        binding = service.registry.artifact_binding(
            artifact["sha256"], owner_kind="runs", owner_id=identity
        )
        if json.loads(binding["path"].read_bytes()) != result["summary"]:
            raise IntegrityError("HDR summary artifact changed.")
        for relative, sha in result["summary"].get("manifest_bindings", {}).items():
            target = (package / relative).resolve()
            if (
                not target.is_relative_to(package)
                or not target.is_file()
                or file_sha256(target) != sha
            ):
                raise IntegrityError("Completed HDR manifest changed.")
    return record


def install_routes(app, service):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    @app.get(ROUTE + "/template", tags=["road_hdr"])
    def template():
        return {
            "specification": {
                "schema": SCHEMA,
                "name": "도로 HDR 설계평가",
                "package_path": "outputs/road-hdr-first-photo-20261004",
                "specification_sha256": "",
                "action": "first-photo",
            },
            "supported_actions": ["first-photo", "acquire", "reprocess", "evaluate"],
        }

    @app.post(ROUTE + "/freeze", tags=["road_hdr"])
    def freeze(payload: dict):
        if set(payload) not in (
            {"package_path"},
            {"package_path", "dataset_root"},
            {"package_path", "dataset_root", "version"},
        ):
            raise ValueError("Freeze requires package path and optional complete dataset binding.")
        package = authorized_path(service, payload["package_path"])
        if "version" in payload:
            freeze_sequences(
                service.workspace, payload["dataset_root"], package, version=payload["version"]
            )
        elif "dataset_root" in payload:
            from .road_hdr_inputs import freeze_pedestrian_first

            freeze_pedestrian_first(service.workspace, payload["dataset_root"], package)
        else:
            local = service.workspace / ".cache/pedestrian-braking/nuscenes-mini"
            if local.is_dir():
                from .road_hdr_inputs import freeze_pedestrian_first

                freeze_pedestrian_first(service.workspace, local, package)
            else:
                freeze_first(service.workspace, package)
        return {
            "package_path": payload["package_path"],
            "specification_sha256": bound_specification(package)[1]["sha256"],
        }

    @app.post(ROUTE + "/preflight", tags=["road_hdr"])
    def preflight(payload: dict):
        spec = specification(payload)
        package = _package(service, spec)
        study, _ = bound_specification(package)
        stage = "first-photo" if spec["action"] == "first-photo" else "full"
        if spec["action"] == "reprocess" and (package / "captures/index.json").exists():
            stage = json.loads((package / "captures/index.json").read_text())["stage"]
        plan = resource_plan(service.workspace, package, study, stage)
        if spec["action"] == "reprocess":
            verify_manifest(package / "captures", "capture-manifest.json")
            plan["status"] = (
                "ready"
                if plan["free_bytes"]
                > plan["native_cells"] * 27 * 18 + study["storage"]["reserve_bytes"]
                else "blocked"
            )
            plan["reasons"] = (
                [] if plan["status"] == "ready" else ["insufficient_disk_for_reprocessing"]
            )
        return plan

    @app.post(ROUTE, status_code=202, tags=["road_hdr"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get(ROUTE, tags=["road_hdr"])
    def listing():
        return [r for r in service.registry.list_runs() if r.get("job_kind") == JOB_KIND]

    @app.get(ROUTE + "/{identity}", tags=["road_hdr"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(ROUTE + "/{identity}/cancel", tags=["road_hdr"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(ROUTE + "/{identity}/files/{relative:path}", tags=["road_hdr"])
    def file(identity: str, relative: str):
        record = get(service, identity)
        package = _package(service, record["specification"])
        path = (package / relative).resolve()
        if (
            not path.is_relative_to(package)
            or not path.is_file()
            or any(p.startswith(".") for p in Path(relative).parts)
        ):
            raise HTTPException(404, "HDR file not found.")
        allowed = False
        for directory, name in (
            (package, "study-manifest.json"),
            (package / "captures", "capture-manifest.json"),
        ):
            if (directory / name).exists() and path.is_relative_to(directory):
                doc = json.loads((directory / name).read_text())
                rel = str(path.relative_to(directory))
                if rel in doc["files"]:
                    if file_sha256(path) != doc["files"][rel]:
                        raise IntegrityError("HDR artifact changed.")
                    allowed = True
        for directory in (
            (package / "reprocessing").glob("*") if (package / "reprocessing").exists() else []
        ):
            if path.is_relative_to(directory) and (directory / "manifest.json").exists():
                doc = json.loads((directory / "manifest.json").read_text())
                sha = doc["files"].get(str(path.relative_to(directory)))
                if sha is not None:
                    if sha != file_sha256(path):
                        raise IntegrityError("HDR replay artifact changed.")
                    allowed = True
        if not allowed and relative not in {
            "specification.json",
            "specification-binding.json",
            "progress.json",
            "resource-plan.json",
        }:
            raise HTTPException(404, "File is not a completed bound HDR artifact.")
        return FileResponse(path)
