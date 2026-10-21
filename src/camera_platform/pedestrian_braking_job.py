"""Pedestrian study lifecycle, input audit and verified local viewer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .pedestrian_braking_inputs import SCHEMA as STUDY_SCHEMA
from .pedestrian_braking_inputs import bound_specification
from .pedestrian_braking_study import run_study
from .registry import IntegrityError, canonical, digest
from .road_rgb_source import file_sha256

SCHEMA = "camera_pedestrian_braking_job_v1"
JOB_KIND = "pedestrian_braking_study"
WORKER_TIMEOUT_S = 24 * 3600
ROUTE = "/api/v1/pedestrian-braking-studies"


def specification(payload):
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "name",
        "package_path",
        "specification_sha256",
        "stage",
    }:
        raise ValueError("Declare the exact versioned road study job fields.")
    if payload["schema"] != SCHEMA or payload["stage"] not in {"first-sequence", "full"}:
        raise ValueError("Unsupported road study schema or execution stage.")
    if not isinstance(payload["name"], str) or not 1 <= len(payload["name"]) <= 160:
        raise ValueError("Road study name requires 1..160 characters.")
    path = payload["package_path"]
    if not isinstance(path, str) or not path or ".." in Path(path).parts:
        raise ValueError(
            "Package path must be a bounded directory under an authorized storage root."
        )
    sha = payload["specification_sha256"]
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("Choose a frozen specification SHA-256.")
    return dict(payload)


def _package(service, spec):
    root = service.workspace.resolve()
    package = (root / spec["package_path"]).resolve()
    roots = [root] + [
        Path(p).resolve()
        for p in os.environ.get("CAMERA_PLATFORM_PEDESTRIAN_PACKAGE_ROOTS", "").split(os.pathsep)
        if p
    ]
    if not any(package.is_relative_to(p) for p in roots) or not package.is_dir():
        raise ValueError("Study package is outside or absent from the workspace.")
    _, binding = bound_specification(package)
    if binding["sha256"] != spec["specification_sha256"]:
        raise IntegrityError("Frozen study specification differs from the submitted binding.")
    return package


def submit(service, payload):
    spec = specification(payload)
    package = _package(service, spec)
    study, _ = bound_specification(package)
    document = {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "input_kind": "RGB-derived spectral proxy",
        "study_schema": STUDY_SCHEMA,
        "model_bindings": [
            {"id": c["id"], "lens": c["lens"]["identity"], "sensor": c["sensor"]["revision"]}
            for c in study["candidates"]
        ],
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        "physical_validation": "not_established",
        "commercial_export_allowed": False,
    }
    return service.registry.submit(document, digest(document))


def execute(service, job):
    if (
        job.get("job_kind") != JOB_KIND
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Road Worker requires its claimed running job.")
    spec = specification(job["specification"])
    package = _package(service, spec)
    result = run_study(service.workspace, package, stage=spec["stage"])
    manifest_name = (
        "package-manifest.json" if spec["stage"] == "full" else "first-sequence-manifest.json"
    )
    if not (package / manifest_name).is_file():
        raise IntegrityError("Completed study has no review manifest.")
    summary = {
        "schema": SCHEMA,
        "stage": spec["stage"],
        "status": result["status"],
        "recommended_id": result.get("recommended_id"),
        "selected_id": result.get("selected_id"),
        "specification_sha256": spec["specification_sha256"],
        "package_path": spec["package_path"],
        "viewer_path": ROUTE + "/" + job["id"] + "/files/report.html",
        "review_manifest_name": manifest_name,
        "package_manifest_sha256": file_sha256(package / manifest_name)
        if (package / manifest_name).exists()
        else None,
        "physical_validation": "not_established",
        "commercial_export_allowed": False,
        "decision": result if spec["stage"] == "full" else None,
    }
    artifact = service.registry.store_blob(
        canonical(summary).encode(), "pedestrian-braking-decision.json", "application/json"
    )
    return {"summary": summary, "artifacts": [artifact]}


def get(service, identity):
    record = service.registry.get_run(identity)
    if record.get("job_kind") != JOB_KIND:
        raise ValueError("Selected Run is not a road design study.")
    _package(service, specification(record["specification"]))
    if record["status"] == "succeeded":
        result = record["result"]
        summary = result["summary"]
        artifacts = result["artifacts"]
        if (
            len(artifacts) != 1
            or summary["specification_sha256"] != record["specification"]["specification_sha256"]
        ):
            raise IntegrityError("Road result lost its specification/artifact binding.")
        binding = service.registry.artifact_binding(
            artifacts[0]["sha256"], owner_kind="runs", owner_id=identity
        )
        if (
            binding["descriptor"] != artifacts[0]
            or json.loads(binding["path"].read_bytes()) != summary
        ):
            raise IntegrityError("Road result artifact differs from the saved decision.")
        manifest_sha = summary.get("package_manifest_sha256")
        if manifest_sha is not None:
            name = summary.get("review_manifest_name", "package-manifest.json")
            if name not in {"package-manifest.json", "first-sequence-manifest.json"}:
                raise IntegrityError("Unknown bound study manifest.")
            manifest = _package(service, record["specification"]) / name
            if not manifest.exists() or file_sha256(manifest) != manifest_sha:
                raise IntegrityError(
                    "Study package manifest differs from its completed job binding."
                )
    return record


def list_records(service):
    return [
        {
            key: job.get(key)
            for key in ("id", "name", "status", "created_at", "error", "specification", "result")
        }
        for job in service.registry.list_runs()
        if job.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    def intake_path(name):
        root = service.workspace.resolve()
        path = (root / "outputs/pedestrian-braking" / name).resolve()
        if not path.is_relative_to(root):
            raise HTTPException(404, "Intake audit is outside the workspace.")
        return path

    @app.get(ROUTE + "/intake", tags=["pedestrian_braking"])
    def intake():
        path = intake_path("implementation-status.json")
        if not path.is_file():
            return {"status": "input_audit_not_available", "study_complete": False}
        if path.stat().st_size > 65536:
            raise IntegrityError("Intake audit exceeds its small-summary contract.")
        return {**json.loads(path.read_text()), "audit_sha256": file_sha256(path)}

    @app.get(ROUTE + "/connection-files/{relative:path}", tags=["pedestrian_braking"])
    def connection_file(relative: str):
        status = intake()
        if not status.get("connection_manifest_sha256"):
            raise HTTPException(404, "First-frame diagnostic is not complete.")
        directory = intake_path("full-window-first-frame")
        manifest = directory / "manifest.json"
        if file_sha256(manifest) != status["connection_manifest_sha256"]:
            raise IntegrityError("First-frame manifest changed after the intake audit.")
        expected = json.loads(manifest.read_text())["files"].get(relative)
        path = (directory / relative).resolve()
        if expected is None or not path.is_relative_to(directory) or not path.is_file():
            raise HTTPException(404, "First-frame artifact is not in the diagnostic manifest.")
        if file_sha256(path) != expected:
            raise IntegrityError("First-frame diagnostic artifact changed.")
        return FileResponse(path)

    @app.get(ROUTE + "/template", tags=["pedestrian_braking"])
    def template():
        return {
            "specification": {
                "schema": SCHEMA,
                "name": "보행자 안정 인지·제동 여유",
                "package_path": "",
                "specification_sha256": "",
                "stage": "full",
            },
            "input_kind": "RGB-derived spectral proxy",
            "freeze_command": (
                "tools/run_pedestrian_braking_study.py freeze --dataset-root <local nuScenes "
                "root> --package <workspace-relative new directory>"
            ),
            "commercial_export_allowed": False,
        }

    @app.post(ROUTE, status_code=202, tags=["pedestrian_braking"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get(ROUTE, tags=["pedestrian_braking"])
    def listing():
        return list_records(service)

    @app.get(ROUTE + "/{identity}", tags=["pedestrian_braking"])
    def detail(identity: str):
        record = get(service, identity)
        package = _package(service, record["specification"])
        path = package / "progress.json"
        return {**record, "study_progress": json.loads(path.read_text()) if path.exists() else None}

    @app.post(ROUTE + "/{identity}/cancel", tags=["pedestrian_braking"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(ROUTE + "/{identity}/files/{relative:path}", tags=["pedestrian_braking"])
    def file(identity: str, relative: str):
        record = get(service, identity)
        if record["status"] != "succeeded":
            raise HTTPException(409, "Study artifacts are not complete for this job.")
        package = _package(service, record["specification"])
        path = (package / relative).resolve()
        if (
            not path.is_relative_to(package)
            or not path.is_file()
            or ".tmp" in path.name
            or path.name.startswith(".")
        ):
            raise HTTPException(404, "Study file not found.")
        manifest_name = (
            (record.get("result") or {})
            .get("summary", {})
            .get("review_manifest_name", "package-manifest.json")
        )
        manifest = package / manifest_name
        if manifest.exists():
            allowed = json.loads(manifest.read_text())["files"]
            expected = allowed.get(relative)
            if relative == manifest_name:
                expected = file_sha256(path)
            if expected is None:
                raise HTTPException(404, "File is not in the study package.")
            if file_sha256(path) != expected:
                raise IntegrityError("Study artifact changed since package completion.")
        elif relative not in {
            "report.html",
            "pedestrian-braking-report.js",
            "specification.json",
            "first-sequence.json",
            "viewer.json",
            "resource-plan.json",
            "README.md",
        } and not relative.startswith(("inputs/", "calculations/")):
            raise HTTPException(404, "File is not a reviewable first-sequence artifact.")
        return FileResponse(path)
