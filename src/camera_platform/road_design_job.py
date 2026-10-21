"""Road design study Registry/Worker/SDK lifecycle and verified local viewer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .registry import IntegrityError, canonical, digest
from .road_design_inputs import SCHEMA as STUDY_SCHEMA
from .road_design_study import bound_specification, run_study
from .road_rgb_source import file_sha256

SCHEMA = "camera_road_design_job_v1"
JOB_KIND = "road_design_study"
WORKER_TIMEOUT_S = 4 * 3600
ROUTE = "/api/v1/road-design-studies"


def specification(payload):
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "name",
        "package_path",
        "specification_sha256",
        "stage",
    }:
        raise ValueError("Declare the exact versioned road study job fields.")
    if payload["schema"] != SCHEMA or payload["stage"] not in {"first-photo", "full"}:
        raise ValueError("Unsupported road study schema or execution stage.")
    if not isinstance(payload["name"], str) or not 1 <= len(payload["name"]) <= 160:
        raise ValueError("Road study name requires 1..160 characters.")
    path = payload["package_path"]
    if (
        not isinstance(path, str)
        or not path
        or Path(path).is_absolute()
        or ".." in Path(path).parts
    ):
        raise ValueError("Package path must be a bounded workspace-relative directory.")
    sha = payload["specification_sha256"]
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("Choose a frozen specification SHA-256.")
    return dict(payload)


def _package(service, spec):
    root = service.workspace.resolve()
    package = (root / spec["package_path"]).resolve()
    if not package.is_relative_to(root) or not package.is_dir():
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
    summary = {
        "schema": SCHEMA,
        "stage": spec["stage"],
        "status": result["status"],
        "recommended_id": result.get("recommended_id"),
        "specification_sha256": spec["specification_sha256"],
        "package_path": spec["package_path"],
        "viewer_path": ROUTE + "/" + job["id"] + "/files/report.html",
        "package_manifest_sha256": file_sha256(package / "package-manifest.json")
        if (package / "package-manifest.json").exists()
        else None,
        "physical_validation": "not_established",
        "commercial_export_allowed": False,
        "decision": result if spec["stage"] == "full" else None,
    }
    artifact = service.registry.store_blob(
        canonical(summary).encode(), "road-design-decision.json", "application/json"
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
            manifest = _package(service, record["specification"]) / "package-manifest.json"
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

    @app.get(ROUTE + "/template", tags=["road_design"])
    def template():
        return {
            "specification": {
                "schema": SCHEMA,
                "name": "도로 장면 설계평가",
                "package_path": "",
                "specification_sha256": "",
                "stage": "full",
            },
            "input_kind": "RGB-derived spectral proxy",
            "freeze_command": (
                "tools/run_road_camera_design.py freeze --dataset-root <local nuImages "
                "root> --package <workspace-relative new directory>"
            ),
            "commercial_export_allowed": False,
        }

    @app.post(ROUTE, status_code=202, tags=["road_design"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get(ROUTE, tags=["road_design"])
    def listing():
        return list_records(service)

    @app.get(ROUTE + "/{identity}", tags=["road_design"])
    def detail(identity: str):
        record = get(service, identity)
        package = _package(service, record["specification"])
        path = package / "progress.json"
        return {**record, "study_progress": json.loads(path.read_text()) if path.exists() else None}

    @app.post(ROUTE + "/{identity}/cancel", tags=["road_design"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(ROUTE + "/{identity}/files/{relative:path}", tags=["road_design"])
    def file(identity: str, relative: str):
        record = get(service, identity)
        package = _package(service, record["specification"])
        path = (package / relative).resolve()
        if (
            not path.is_relative_to(package)
            or not path.is_file()
            or ".tmp" in path.name
            or path.name.startswith(".")
        ):
            raise HTTPException(404, "Study file not found.")
        manifest = package / "package-manifest.json"
        if manifest.exists():
            allowed = json.loads(manifest.read_text())["files"]
            expected = allowed.get(relative)
            if relative == "package-manifest.json":
                expected = file_sha256(path)
            if expected is None:
                raise HTTPException(404, "File is not in the study package.")
            if file_sha256(path) != expected:
                raise IntegrityError("Study artifact changed since package completion.")
        elif relative not in {
            "report.html",
            "road-design-report.js",
            "specification.json",
            "first-photo.json",
            "README.md",
        } and not relative.startswith(("inputs/", "calculations/")):
            raise HTTPException(404, "File is not a reviewable first-photo artifact.")
        return FileResponse(path)
