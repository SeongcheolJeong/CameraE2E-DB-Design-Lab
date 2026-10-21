"""Immutable DB source bindings and explicit electrical-parameter recaptures.

A DB candidate is a local sensor-plane probe. Its optical rates and reference ISP
remain frozen; this adapter transfers only four compatible electrical parameters.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import tempfile
from copy import deepcopy
from pathlib import Path, PurePosixPath

from .registry import ConflictError, IntegrityError, canonical, digest

ROUTE = "/api/v1/calibration-lab"
BRIDGES = "calibration_db_bridges"
SCHEMA = "camera_calibration_db_bridge_v1"
JOB_KIND = "calibration_db_replay"
MAX_SOURCE_BYTES = 256 * 1024**2
MAX_OUTPUT_BYTES = 512 * 1024**2
TRANSFERRED = ("black_level_dn", "dn_per_e", "read_noise_e", "dark_current_e_per_s")


def _identity(value, name, pattern=r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {name}")
    return value


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 500:
        raise IntegrityError("Invalid frozen source path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or "\\" in value:
        raise IntegrityError("Frozen source paths must be normalized relative paths")
    return value


def _current(service):
    from .calibration_lab import _source_code

    current = _source_code()
    if service.loaded_environment != current:
        raise ConflictError(
            "Executable source changed after startup; restart the API before submitting"
        )
    return current


def source_selection(service, source):
    """Resolve an exact candidate, including an explicit condition when required."""
    from .reference_workshop_job import get as get_reference

    if not isinstance(source, dict) or set(source) - {"run_id", "candidate_id", "condition_id"}:
        raise ValueError("Source requires run_id, candidate_id and optional condition_id")
    run_id = _identity(source.get("run_id"), "source run ID", r"run-[0-9a-f]{32}")
    candidate_id = _identity(source.get("candidate_id"), "candidate ID", r"candidate-[0-9]{2,6}")
    run = get_reference(service, run_id)
    if run.get("plugin_id") != "camera_db_design_study_v1" or run["status"] != "succeeded":
        raise ValueError("Source requires a completed DB Design Lab study")
    summary = run["result"]["summary"]
    condition_id = source.get("condition_id")
    selection = summary
    if summary.get("comparison_mode") == "aperture_conditions":
        _identity(condition_id, "explicit condition ID", r"condition-[0-9]{3,6}")
        matches = [
            c
            for c in summary.get("aperture_conditions", {}).get("conditions", [])
            if c.get("id") == condition_id
        ]
        if len(matches) != 1:
            raise ValueError("Source does not contain this exact condition")
        selection = matches[0]
    elif condition_id is not None:
        raise ValueError("condition_id is only valid for a conditional DB source")
    matches = [c for c in selection.get("candidates", []) if c.get("id") == candidate_id]
    if len(matches) != 1 or matches[0].get("status") != "succeeded":
        raise ValueError("Source does not contain this completed candidate")
    candidate = matches[0]
    context = {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "condition_id": condition_id,
        "run_request_sha256": run["content_hash"],
        "run_result_sha256": digest(run["result"]),
        "manifest_sha256": summary["manifest_sha256"],
        "candidate_snapshot": candidate,
        "candidate_sha256": digest(candidate),
        "condition_sha256": digest(selection) if condition_id is not None else None,
        "role": "design_reference_only",
        "profile_conversion": "not_inferred_from_DB_candidate",
    }
    return run, context


def create_bridge(service, payload):
    from . import calibration_db_engine as engine
    from .calibration_lab import _unique_artifacts
    from .reference_workshop_job import _directory as source_directory

    code = _current(service)
    if not isinstance(payload, dict) or set(payload) != {"source"}:
        raise ValueError("DB bridge requires source only")
    run, context = source_selection(service, payload["source"])
    prepared = engine.prepare_source(
        source_directory(service, run["id"]),
        run["result"]["summary"],
        context["candidate_id"],
        condition_id=context["condition_id"],
        expected_manifest_sha256=context["manifest_sha256"],
    )
    frozen = prepared["frozen_files"]
    if not isinstance(frozen, dict) or not 1 <= len(frozen) <= 512:
        raise ValueError("DB bridge requires a bounded set of preserved source files")
    manifest_bytes = frozen.get("manifest.json")
    if (
        not isinstance(manifest_bytes, bytes)
        or hashlib.sha256(manifest_bytes).hexdigest() != context["manifest_sha256"]
    ):
        raise IntegrityError("DB bridge does not preserve its pinned source manifest")
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema") != "camera_reference_workshop_files_v1":
        raise IntegrityError("Invalid frozen DB manifest schema")
    artifacts, files, total = [], {}, 0
    for relative, data in sorted(frozen.items()):
        _relative(relative)
        if not isinstance(data, bytes):
            raise IntegrityError("DB adapter must preserve exact source bytes")
        if relative != "manifest.json" and hashlib.sha256(data).hexdigest() != manifest[
            "files"
        ].get(relative):
            raise IntegrityError("Frozen DB bytes do not match the source manifest: " + relative)
        total += len(data)
        if total > MAX_SOURCE_BYTES:
            raise ValueError("DB bridge exceeds the 256 MiB source budget")
        descriptor = service.registry.store_blob(
            data, relative, mimetypes.guess_type(relative)[0] or "application/octet-stream"
        )
        artifacts.append(descriptor)
        files[relative] = {"sha256": descriptor["sha256"], "bytes": len(data)}
    if files.get("manifest.json", {}).get("sha256") != context["manifest_sha256"]:
        raise IntegrityError("DB bridge does not preserve its pinned source manifest")
    projection = engine.baseline_projection(prepared)
    if _current(service) != code:
        raise IntegrityError("DB bridge adapter changed while binding the source")
    # Re-read the source completion and selected snapshot after preparing the bytes.
    again, again_context = source_selection(service, payload["source"])
    if again != run or again_context != context:
        raise IntegrityError("DB source changed while binding the bridge")
    context.update(role="frozen_db_electrical_source", profile_conversion="electronics_only")
    return service.registry.put(
        BRIDGES,
        {
            "schema": SCHEMA,
            "source_context": context,
            "source_files": files,
            "artifacts": _unique_artifacts(artifacts),
            "projection": projection,
            "adapter_source_code": code,
            "compatibility": {
                "status": "qualified_for_electronics_only",
                "transferred": list(TRANSFERRED),
                "geometry_scope": "independent_virtual_bench_ROI",
                "native_sensor_shape_inferred": False,
            },
            "physical_validation": "not_established",
        },
    )


def get_bridge(service, identity):
    _identity(identity, "DB bridge ID")
    bridge = service.registry.get(BRIDGES, identity)
    if bridge.get("schema") != SCHEMA:
        raise IntegrityError("Invalid DB bridge schema")
    for relative, row in bridge["source_files"].items():
        _relative(relative)
        binding = service.registry.artifact_binding(
            row["sha256"], owner_kind=BRIDGES, owner_id=identity
        )
        if binding["path"].stat().st_size != row["bytes"]:
            raise IntegrityError("DB bridge source size changed")
    return bridge


def campaign_bridge(service, campaign):
    """Gate explicit physical metadata against only the modeled source quantities."""
    identity = campaign.get("db_bridge_id")
    if identity is None:
        return None
    bridge = get_bridge(service, identity)
    projection, profile = bridge["projection"], campaign["baseline_profile"]
    for key in ("pixel_pitch_um", "cfa"):
        if profile[key] != projection[key]:
            raise ValueError(f"Campaign {key} differs from its frozen DB source")
    if profile["electronics"] != projection["electronics"]:
        raise ValueError("Campaign baseline electronics differ from its frozen DB source")
    for key in ("gain", "temperature_c"):
        if campaign["conditions"][key] != projection["conditions"][key]:
            raise ValueError(f"Campaign {key} differs from its frozen DB source")
    source = campaign.get("source")
    if source and any(
        source.get(k) != bridge["source_context"].get(k)
        for k in ("run_id", "candidate_id", "condition_id")
    ):
        raise ValueError("Campaign source conflicts with its immutable DB bridge")
    return bridge


def _profile(service, campaign, identity):
    from . import calibration_lab as lab
    from .calibration_lab_engine import validate_profile

    _identity(identity, "applied profile ID")
    revision = service.registry.get(lab.PROFILES, identity)
    if revision["campaign_id"] != campaign["id"]:
        raise ValueError("Applied profile belongs to another campaign")
    if not any(
        a["campaign_id"] == campaign["id"] and a["profile_id"] == identity
        for a in service.registry.list(lab.ACTIVATIONS)
    ):
        raise ValueError("DB replay requires a previously applied saved profile")
    parent = lab.get(service, revision["source_study_id"])
    if (
        parent["status"] != "succeeded"
        or parent["job_kind"] != lab.JOB_KIND
        or digest(parent["result"]) != revision["source_result_sha256"]
        or parent["result"]["summary"]["candidate_sha256"] != revision["profile_sha256"]
        or digest(revision["profile"]) != revision["profile_sha256"]
    ):
        raise IntegrityError("Applied profile differs from its completed calibration result")
    profile = validate_profile(revision["profile"])
    baseline = campaign["baseline_profile"]
    for key in ("native_shape", "pixel_pitch_um", "cfa", "conditions"):
        if profile[key] != baseline[key]:
            raise ValueError(f"Applied profile changes frozen campaign {key}")
    for key in ("bit_depth", "full_well_e", "full_well_identified"):
        if profile["electronics"][key] != baseline["electronics"][key]:
            raise ValueError(f"Electrical replay cannot transfer {key}")
    return revision


def submit(service, payload):
    from . import calibration_lab as lab

    code = _current(service)
    if not isinstance(payload, dict) or set(payload) - {
        "campaign_id",
        "profile_id",
        "parameter_scope",
        "seed",
    }:
        raise ValueError("DB replay accepts campaign_id, profile_id, parameter_scope and seed")
    if payload.get("parameter_scope") != "electronics_only":
        raise ValueError("DB replay requires explicit parameter_scope electronics_only")
    seed = payload.get("seed", 17)
    if type(seed) is not int or not 0 <= seed <= 2**31 - 65536:
        raise ValueError("DB replay seed must be an integer in 0..2147418112")
    campaign = service.registry.get(
        lab.CAMPAIGNS, _identity(payload.get("campaign_id"), "campaign ID")
    )
    bridge = campaign_bridge(service, campaign)
    if bridge is None:
        raise ValueError("Campaign requires an immutable DB bridge before replay")
    revision = _profile(service, campaign, payload.get("profile_id"))
    spec = {
        "campaign_id": campaign["id"],
        "profile_id": revision["id"],
        "db_bridge_id": bridge["id"],
        "parameter_scope": "electronics_only",
        "seed": seed,
    }
    document = {
        "name": "DB electrical replay · " + campaign["name"],
        "job_kind": JOB_KIND,
        "plugin_id": "calibration_db_electronics_v1",
        "specification": spec,
        "campaign_snapshot": campaign,
        "bridge_snapshot": bridge,
        "profile_snapshot": revision,
        "input_artifacts": bridge["artifacts"],
        "source_code": code,
        "default_worker_timeout_seconds": 1800,
    }
    return service.registry.submit(document, digest(document), force=True)


def _directory(service, identity):
    _identity(identity, "DB replay ID", r"run-[0-9a-f]{32}")
    return service.registry.directory / "calibration-db-replays" / identity


def get(service, identity):
    _directory(service, identity)
    run = service.registry.get_run(identity)
    if run.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a DB calibration replay")
    if run["status"] == "succeeded":
        artifact = run["result"]["artifacts"][0]
        binding = service.registry.artifact_binding(
            artifact["sha256"], owner_kind="runs", owner_id=identity
        )
        if json.loads(binding["path"].read_bytes()) != run["result"]["summary"]:
            raise IntegrityError("DB replay summary differs from its saved artifact")
    if run["status"] in {"queued", "running"}:
        path = _directory(service, identity) / "progress.json"
        if path.is_file() and not path.is_symlink():
            try:
                run["progress"] = json.loads(path.read_bytes())
            except (ValueError, OSError):
                pass
    return run


def execute(service, job):
    from . import calibration_db_engine as engine
    from . import calibration_lab as lab

    if get(service, job["id"])["status"] != "running":
        raise ValueError("A claimed running DB replay is required")
    if _current(service) != job["source_code"]:
        raise IntegrityError("DB replay adapter changed after submission")
    campaign = service.registry.get(lab.CAMPAIGNS, job["campaign_snapshot"]["id"])
    bridge = campaign_bridge(service, campaign)
    revision = _profile(service, campaign, job["profile_snapshot"]["id"])
    if (
        campaign != job["campaign_snapshot"]
        or bridge != job["bridge_snapshot"]
        or revision != job["profile_snapshot"]
    ):
        raise IntegrityError("Frozen DB replay inputs changed")
    directory = _directory(service, job["id"])
    directory.mkdir(parents=True, exist_ok=False)

    def progress(message, *_args):
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("DB replay cancelled")
        data = message if isinstance(message, dict) else {"message": str(message)}
        (directory / "progress.json").write_text(canonical(data))

    with tempfile.TemporaryDirectory(prefix="camera-calibration-db-source-") as temporary:
        root = Path(temporary)
        for relative, row in bridge["source_files"].items():
            path = root / _relative(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            binding = service.registry.artifact_binding(
                row["sha256"], owner_kind=BRIDGES, owner_id=bridge["id"]
            )
            data = binding["path"].read_bytes()
            if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise IntegrityError("Frozen DB source changed while materializing")
            path.write_bytes(data)
        source = bridge["source_context"]
        prepared = engine.prepare_source(
            root,
            None,
            source["candidate_id"],
            condition_id=source["condition_id"],
            expected_manifest_sha256=source["manifest_sha256"],
        )
        if engine.baseline_projection(prepared) != bridge["projection"]:
            raise IntegrityError("DB baseline projection changed; create a new bridge")
        progress("고정된 DB 광전자율에서 기준·보정 RAW를 새로 생성")
        summary = engine.render_replay(
            prepared,
            deepcopy(revision["profile"]),
            directory,
            seed=job["specification"]["seed"],
            on_progress=progress,
        )
    summary.update(
        {
            "run_id": job["id"],
            "campaign_id": campaign["id"],
            "profile_id": revision["id"],
            "profile_sha256": revision["profile_sha256"],
            "db_bridge_id": bridge["id"],
            "db_bridge_sha256": bridge["content_hash"],
            "source_context": bridge["source_context"],
            "parameter_scope": "electronics_only",
            "fresh_simulation": True,
            "physical_validation": "not_established",
            "evidence_kind": "model_replay",
            "source_code": job["source_code"],
            "files_base": f"{ROUTE}/db-replays/{job['id']}/files/",
            "report_url": f"{ROUTE}/db-replays/{job['id']}/files/report.html",
        }
    )
    (directory / "request.json").write_text(canonical(job))
    (directory / "report.html").write_bytes(lab._report(summary))
    artifacts, files, total = [], {}, 0
    inputs = {a["sha256"] for a in job["input_artifacts"]}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise IntegrityError("DB replay output must not contain symlinks")
        if not path.is_file() or path.name == "progress.json":
            continue
        total += path.stat().st_size
        if total > MAX_OUTPUT_BYTES:
            raise ValueError("DB replay exceeds the 512 MiB output budget")
        relative = _relative(path.relative_to(directory).as_posix())
        artifact = service.registry.store_file(
            path, relative, mimetypes.guess_type(relative)[0] or "application/octet-stream"
        )
        files[relative] = artifact["sha256"]
        if artifact["sha256"] not in inputs:
            artifacts.append(artifact)
    summary["files"] = files
    if _current(service) != job["source_code"]:
        raise IntegrityError("DB replay adapter changed during execution")
    progress("완료 · DB 원본과 분리된 새 실행 근거 저장")
    result = service.registry.store_blob(
        canonical(summary).encode(), "db-replay-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [result, *lab._unique_artifacts(artifacts)]}


def _file(service, identity, relative):
    run = get(service, identity)
    if run["status"] != "succeeded":
        raise ValueError("DB replay files require a completed job")
    checksum = run["result"]["summary"]["files"].get(_relative(relative))
    if checksum is None:
        raise KeyError("File is not in the DB replay manifest")
    return service.registry.artifact_binding(checksum, owner_kind="runs", owner_id=identity)["path"]


def install_routes(app, service):
    from fastapi.responses import FileResponse

    @app.post(ROUTE + "/db-bridges", status_code=201, tags=["calibration_lab"])
    def bridge_create(payload: dict):
        return create_bridge(service, payload)

    @app.get(ROUTE + "/db-bridges/{identity}", tags=["calibration_lab"])
    def bridge_get(identity: str):
        return get_bridge(service, identity)

    @app.post(ROUTE + "/db-replays", status_code=202, tags=["calibration_lab"])
    def replay_create(payload: dict):
        return submit(service, payload)

    @app.get(ROUTE + "/db-replays/{identity}", tags=["calibration_lab"])
    def replay_get(identity: str):
        return get(service, identity)

    @app.post(ROUTE + "/db-replays/{identity}/cancel", tags=["calibration_lab"])
    def replay_cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(ROUTE + "/db-replays/{identity}/files/{relative:path}", tags=["calibration_lab"])
    def replay_file(identity: str, relative: str):
        return FileResponse(
            _file(service, identity, relative),
            media_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
        )
