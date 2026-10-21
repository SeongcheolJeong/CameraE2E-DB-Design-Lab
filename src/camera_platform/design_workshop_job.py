"""Immutable design revisions and durable workshop capture/sweep/replay jobs."""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

from . import design_workshop_storage as storage
from .registry import IntegrityError, canonical, digest
from .virtual_lab_job import _number, isp_specification, sha256

SCHEMA = "camera_design_workshop_v1"
JOB_KIND = "design_workshop"
ROUTE = "/api/v1/design-workshop"
WORKER_TIMEOUT_S = 7200


def workshop_isp(payload):
    if not isinstance(payload, dict):
        raise ValueError("Workshop ISP must be an object.")
    values = copy.deepcopy(payload)
    white = _number(values.pop("display_white_level", 1), "Display white level", 1, 100)
    return {**isp_specification(values), "display_white_level": white}


def defaults():
    from .design_workshop_optics import default_specification

    return {
        "schema": SCHEMA,
        "name": "Lens · Pixel · ISP Design Workshop",
        "lens": default_specification(pixel_pitch_um=3.45),
        "pixel": {"cfa_thickness_scale": 1.0},
        "scene": "composite",
        "exposure_ms": 4,
        "gain": 1,
        "luminance_cd_m2": 1000,
        "seed": 42,
        "frames": 8,
        "isp": workshop_isp({}),
    }


def specification(payload):
    from .design_workshop_optics import normalize

    base = defaults()
    if not isinstance(payload, dict) or set(payload) - set(base):
        raise ValueError("Unsupported workshop specification fields.")
    spec = {**base, **copy.deepcopy(payload)}
    if (
        spec["schema"] != SCHEMA
        or not isinstance(spec["name"], str)
        or not 1 <= len(spec["name"]) <= 160
    ):
        raise ValueError("A versioned workshop specification and name are required.")
    spec["lens"] = normalize(spec["lens"])
    from .design_workshop_optics import geometry

    if min(geometry(spec["lens"])["native_shape_row_col"]) < 64:
        raise ValueError("Workshop chart metrics require at least 64 native samples per axis.")
    if spec["lens"]["pixel_pitch_um"] != 3.45:
        raise ValueError("This research cell keeps its 3.45 um pitch fixed.")
    if not isinstance(spec["pixel"], dict) or set(spec["pixel"]) != {"cfa_thickness_scale"}:
        raise ValueError("Pixel controls require cfa_thickness_scale only.")
    spec["pixel"]["cfa_thickness_scale"] = _number(
        spec["pixel"]["cfa_thickness_scale"], "CFA thickness scale", 0.9, 1.1
    )
    if spec["pixel"]["cfa_thickness_scale"] not in (0.9, 1.0, 1.1):
        raise ValueError("Prepared CFA cells support 0.9, 1.0 and 1.1 thickness scales.")
    if spec["scene"] not in {"composite", "resolution", "color", "texture", "uniform"}:
        raise ValueError("Unsupported workshop scene.")
    for key, lo, hi in (("exposure_ms", 0.1, 100), ("gain", 1, 16), ("luminance_cd_m2", 1, 10000)):
        spec[key] = _number(spec[key], key, lo, hi)
    if type(spec["seed"]) is not int or not 0 <= spec["seed"] < 2**32 - 32:
        raise ValueError("A bounded integer seed is required.")
    if type(spec["frames"]) is not int or not 2 <= spec["frames"] <= 16:
        raise ValueError("Temporal noise needs 2..16 independent readouts.")
    spec["isp"] = workshop_isp(spec["isp"])
    return spec


def fingerprint():
    names = [*Path(__file__).parent.glob("design_workshop*.py")]
    names += [
        Path(__file__).parent / name
        for name in (
            "prescription_psf.py",
            "lens_factory.py",
            "electrical.py",
            "virtual_lab_engine.py",
            "virtual_lab_metrics.py",
        )
    ]
    names += [
        Path(__file__).parents[1] / "pyisetcam" / name
        for name in (
            "physical_estimate.py",
            "cfa_materials.py",
            "scene.py",
            "ip.py",
            "color.py",
            "iso.py",
            "metrics.py",
        )
    ]
    return {str(p.relative_to(Path(__file__).parents[1])): sha256(p) for p in sorted(names)}


def _directory(service, identity):
    if not identity.startswith("run-") or any(c not in "0123456789abcdef" for c in identity[4:]):
        raise ValueError("Invalid workshop job identity.")
    return storage.root(service) / "design-workshop" / identity


def revision(service, payload):
    if not isinstance(payload, dict) or set(payload) - {
        "name",
        "kind",
        "specification",
        "parent_id",
    }:
        raise ValueError("Revision accepts name, kind, specification and parent_id.")
    if payload.get("kind") not in {"lens", "pixel", "camera"}:
        raise ValueError("Revision kind must be lens, pixel or camera.")
    spec = specification(payload["specification"])
    parent_id = payload.get("parent_id")
    parent = service.registry.get("design_revisions", parent_id) if parent_id else None
    changed = [key for key in spec if parent is None or spec[key] != parent["specification"][key]]
    return service.registry.put(
        "design_revisions",
        {
            "name": str(payload.get("name", spec["name"]))[:160],
            "kind": payload["kind"],
            "specification": spec,
            "parent_id": parent_id,
            "changed_fields": changed,
            "specification_sha256": digest(spec),
            "source_code": fingerprint(),
            "physical_validation": "not_established",
        },
    )


def expand_sweep(spec, axes):
    if not isinstance(axes, list) or not 1 <= len(axes) <= 2:
        raise ValueError("Sweep requires one or two explicit axes.")
    allowed = {
        "lens.controls.front_radius_mm",
        "lens.controls.stop_gap_mm",
        "lens.controls.stop_radius_mm",
        "lens.controls.sensor_shift_um",
        "lens.controls.a4_mm3",
        "pixel.cfa_thickness_scale",
        "isp.denoise",
        "isp.sharpen",
        "isp.lsc_strength",
        "isp.tone_gamma",
    }
    paths = []
    for axis in axes:
        if (
            not isinstance(axis, dict)
            or set(axis) != {"path", "values"}
            or axis["path"] not in allowed
        ):
            raise ValueError("Unsupported design sweep axis.")
        if not isinstance(axis["values"], list) or not 2 <= len(axis["values"]) <= 5:
            raise ValueError("Each sweep axis needs 2..5 explicit values.")
        if axis["path"] in paths:
            raise ValueError("Sweep axes must be distinct.")
        paths.append(axis["path"])
    result = []
    for values in itertools.product(*(a["values"] for a in axes)):
        candidate = copy.deepcopy(spec)
        for path, value in zip(paths, values, strict=True):
            parts = path.split(".")
            current = candidate
            for part in parts[:-1]:
                current = current[part]
            current[parts[-1]] = value
        result.append(specification(candidate))
    if len(result) > 25:
        raise ValueError("Workshop sweeps support at most 25 candidates.")
    return result


def _source_summary(record, candidate_id=None):
    if record["status"] != "succeeded":
        raise ValueError("Completed acquisition required for replay.")
    summary = record["result"]["summary"]
    if record.get("axes"):
        selected = next((c for c in summary["candidates"] if c["id"] == candidate_id), None)
        if selected is None or selected.get("status") != "succeeded":
            raise ValueError("Choose an exact successful sweep candidate for replay.")
        return selected
    if candidate_id is not None:
        raise ValueError("This source is not a sweep.")
    return summary


def submit(service, payload, *, source_run_id=None, source_candidate_id=None, axes=None):
    spec = specification(payload)
    source = None
    if source_run_id:
        record = get(service, source_run_id)
        if record["status"] != "succeeded":
            raise ValueError("ISP replay requires a completed workshop capture.")
        original = _source_summary(record, source_candidate_id)
        verify_files(service, record)
        if any(spec[k] != original["configuration"][k] for k in spec if k not in {"name", "isp"}):
            raise ValueError("Same-RAW replay may change only ISP and name.")
        source = {
            "run_id": source_run_id,
            "candidate_id": source_candidate_id,
            "manifest_sha256": record["result"]["summary"]["manifest_sha256"],
        }
    if axes is not None:
        expand_sweep(spec, axes)
    snapshot = revision(service, {"kind": "camera", "specification": spec})
    state = storage.status(service)
    estimate = 2 * 1024**3 * (len(expand_sweep(spec, axes)) if axes else 1)
    if (
        state["remaining_bytes"] < estimate
        or state["volume_free_bytes"] - estimate < storage.RESERVE_BYTES
    ):
        raise ValueError(
            "Workshop storage budget is insufficient; no precision reduction was applied."
        )
    doc = {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "revision_id": snapshot["id"],
        "source": source,
        "axes": copy.deepcopy(axes),
        "source_code": fingerprint(),
        "storage_root": state["path"],
        "estimated_bytes": estimate,
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
    }
    return service.registry.submit(doc, digest(doc))


def get(service, identity):
    record = service.registry.get_run(identity)
    if record.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a design workshop run.")
    directory = _directory(service, identity)
    if record.get("storage_root") != str(storage.root(service)):
        raise IntegrityError("Configured project storage differs from the submitted location.")
    if record["status"] == "succeeded":
        summary = record["result"]["summary"]
        if sha256(directory / "manifest.json") != summary["manifest_sha256"]:
            raise IntegrityError("Workshop manifest changed.")
        binding = service.registry.artifact_binding(
            record["result"]["artifacts"][0]["sha256"], owner_kind="runs", owner_id=identity
        )
        if json.loads(binding["path"].read_bytes()) != summary:
            raise IntegrityError("Workshop result binding changed.")
    progress = directory / "progress.json"
    if record["status"] in {"queued", "running"} and progress.exists():
        try:
            record["progress"] = json.loads(progress.read_text())
        except json.JSONDecodeError:
            pass
    return record


def verify_files(service, record):
    directory = _directory(service, record["id"])
    manifest = json.loads((directory / "manifest.json").read_text())
    for relative, expected in manifest["files"].items():
        path = (directory / relative).resolve()
        if (
            not path.is_relative_to(directory.resolve())
            or not path.is_file()
            or sha256(path) != expected
        ):
            raise IntegrityError("Workshop source artifact changed: " + relative)


def execute(service, job):
    from . import design_workshop_engine as engine

    if (
        service.registry.get_run(job["id"])["status"] != "running"
        or job["source_code"] != fingerprint()
    ):
        raise IntegrityError(
            "Claimed workshop job and unchanged code required; submit again after code changes."
        )
    if job["storage_root"] != str(storage.root(service)):
        raise IntegrityError("Project storage changed after submission.")
    with storage.reserve(service, job["estimated_bytes"]) as (project, check):
        directory = _directory(service, job["id"])
        directory.mkdir(parents=True, exist_ok=True)

        def progress(value):
            if service.registry.get_run(job["id"])["status"] != "running":
                raise RuntimeError("Workshop job cancelled.")
            check()
            data = value if isinstance(value, dict) else {"stage": str(value)}
            (directory / "progress.json").write_text(json.dumps(data))

        if job.get("source"):
            source = get(service, job["source"]["run_id"])
            verify_files(service, source)
            source_directory = _directory(service, source["id"])
            if job["source"].get("candidate_id"):
                source_directory /= job["source"]["candidate_id"]
            result = engine.replay(source_directory, directory, job["specification"], progress)
            verify_files(service, source)
        else:
            specs = (
                expand_sweep(job["specification"], job["axes"])
                if job.get("axes")
                else [job["specification"]]
            )
            results = []
            acquisitions = {}
            for index, spec in enumerate(specs):
                progress(
                    {"stage": f"Candidate {index + 1}/{len(specs)}", "fraction": index / len(specs)}
                )
                candidate_dir = (
                    directory / f"candidate-{index:02d}" if job.get("axes") else directory
                )
                acquisition_key = digest(
                    {k: v for k, v in spec.items() if k not in {"name", "isp"}}
                )
                try:
                    if acquisition_key in acquisitions:
                        item = engine.replay(
                            acquisitions[acquisition_key], candidate_dir, spec, progress
                        )
                    else:
                        item = engine.run(
                            service.workspace,
                            candidate_dir,
                            spec,
                            project / "workshop-cache",
                            progress,
                        )
                        acquisitions[acquisition_key] = candidate_dir
                    item["status"] = "succeeded"
                except ValueError as error:
                    if not job.get("axes"):
                        raise
                    item = {
                        "configuration": spec,
                        "status": "failed",
                        "error": str(error),
                        "images": {},
                        "metrics": {},
                        "limitations": [str(error)],
                    }

                item["id"] = f"candidate-{index:02d}"
                item["files_base"] = f"{ROUTE}/runs/{job['id']}/files/" + (
                    item["id"] + "/" if job.get("axes") else ""
                )
                results.append(item)
            result = (
                results[0]
                if not job.get("axes")
                else {
                    "schema": "camera_design_workshop_sweep_v1",
                    "candidates": results,
                    "axes": job["axes"],
                    "configuration": job["specification"],
                    "selection": "user_exploration_no_automatic_winner",
                    "successful_candidates": sum(c["status"] == "succeeded" for c in results),
                }
            )
        result["revision_ids"] = [job["revision_id"]]
        result["source_run_id"] = job["source"]["run_id"] if job.get("source") else None
        engine.write_json(directory / "result.json", result)
        engine.report(result, directory)
        files = {
            str(p.relative_to(directory)): sha256(p)
            for p in sorted(directory.rglob("*"))
            if p.is_file() and p.name not in {"progress.json", "manifest.json"}
        }
        engine.write_json(
            directory / "manifest.json",
            {"schema": "camera_design_workshop_files_v1", "files": files},
        )
        summary = {
            **result,
            "files_base": f"{ROUTE}/runs/{job['id']}/files/",
            "report_url": f"{ROUTE}/runs/{job['id']}/files/report.html",
            "manifest_sha256": sha256(directory / "manifest.json"),
            "physical_validation": "not_established",
            "storage": storage.status(service),
        }
        artifact = service.registry.store_blob(
            canonical(summary).encode(), "design-workshop-result.json", "application/json"
        )
        return {"summary": summary, "artifacts": [artifact]}


def catalog(service):
    from .design_workshop_optics import CONTROL_LIMITS

    spec = defaults()
    return {
        "specification": spec,
        "catalog": {
            "lenses": [
                {"id": "authored_aspheric", "label": "RayOptics/Prysm 실제 처방 · 지원 시야"}
            ],
            "pixels": [{"id": "research_bayer", "label": "IMX462 자료 기반 RGB 연구 셀 · 절대 QE"}],
            "isp_profiles": ["balanced", "low_noise", "detail"],
            "scenes": ["composite", "resolution", "color", "texture", "uniform"],
            "lens_controls": {k: {"min": v[0], "max": v[1]} for k, v in CONTROL_LIMITS.items()},
            "maximum_candidates": 25,
            "spectral_support_nm": [450, 650],
            "limitations": [
                "Authored narrow-field prescription, not a 120 degree product lens.",
                "Incoherent layer absorption; OCL routing and DTI charge collection "
                "are not simulated.",
                "Model based diagnostics; no measured product or EMVA certification claim.",
            ],
        },
        "storage": storage.status(service),
    }


def install_routes(app, service):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    @app.get(ROUTE + "/catalog", tags=[JOB_KIND])
    def template():
        return catalog(service)

    @app.get(ROUTE + "/revisions", tags=[JOB_KIND])
    def revisions():
        return {"revisions": service.registry.list("design_revisions")}

    @app.post(ROUTE + "/revisions", status_code=201, tags=[JOB_KIND])
    def create_revision(payload: dict):
        return revision(service, payload)

    @app.get(ROUTE + "/runs", tags=[JOB_KIND])
    def listing():
        return {"runs": [r for r in service.registry.list_runs() if r.get("job_kind") == JOB_KIND]}

    @app.post(ROUTE + "/runs", status_code=202, tags=[JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.post(ROUTE + "/sweeps", status_code=202, tags=[JOB_KIND])
    def sweep(payload: dict):
        if set(payload) - {"name", "specification", "axes"}:
            raise ValueError("Sweep accepts name, specification and axes.")
        return submit(
            service,
            {**payload["specification"], "name": payload.get("name", "Design sweep")},
            axes=payload["axes"],
        )

    @app.post(ROUTE + "/replays", status_code=202, tags=[JOB_KIND])
    def replay(payload: dict):
        if set(payload) - {"source_run_id", "source_candidate_id", "name", "isp"} or not {
            "source_run_id",
            "name",
            "isp",
        } <= set(payload):
            raise ValueError("Replay requires source_run_id, name and isp.")
        original = get(service, payload["source_run_id"])
        selected = _source_summary(original, payload.get("source_candidate_id"))
        spec = {**selected["configuration"], "name": payload["name"], "isp": payload["isp"]}
        return submit(
            service,
            spec,
            source_run_id=original["id"],
            source_candidate_id=payload.get("source_candidate_id"),
        )

    @app.get(ROUTE + "/runs/{identity}", tags=[JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.post(ROUTE + "/runs/{identity}/cancel", tags=[JOB_KIND])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(ROUTE + "/runs/{identity}/bundle", tags=[JOB_KIND])
    def bundle(identity: str):
        import tempfile
        import zipfile

        from starlette.background import BackgroundTask

        record = get(service, identity)
        if record["status"] != "succeeded":
            raise HTTPException(409, "Complete the run before exporting its package.")
        verify_files(service, record)
        directory = _directory(service, identity)
        files = json.loads((directory / "manifest.json").read_text())["files"]
        estimated = sum((directory / relative).stat().st_size for relative in files) + 1024**2
        with storage.reserve(service, estimated) as (project, check):
            handle = tempfile.NamedTemporaryFile(
                prefix="workshop-export-", suffix=".zip", dir=project, delete=False
            )
            target = Path(handle.name)
            handle.close()
            try:
                with zipfile.ZipFile(
                    target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
                ) as archive:
                    for relative in [*files, "manifest.json"]:
                        archive.write(directory / relative, relative)
                return FileResponse(
                    target,
                    filename=f"design-workshop-{identity}.zip",
                    media_type="application/zip",
                    background=BackgroundTask(target.unlink, missing_ok=True),
                )
            except BaseException:
                target.unlink(missing_ok=True)
                raise

    @app.get(ROUTE + "/runs/{identity}/files/{relative:path}", tags=[JOB_KIND])
    def file(identity: str, relative: str):
        record = get(service, identity)
        if record["status"] != "succeeded":
            raise HTTPException(404, "Workshop result is not complete.")
        directory = _directory(service, identity).resolve()
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory) or not target.is_file():
            raise HTTPException(404, "Workshop artifact not found.")
        manifest = json.loads((directory / "manifest.json").read_text())
        expected = (
            record["result"]["summary"]["manifest_sha256"]
            if relative == "manifest.json"
            else manifest["files"].get(relative)
        )
        if expected is None:
            raise HTTPException(404, "Not a bound artifact.")
        if sha256(target) != expected:
            raise IntegrityError("Workshop artifact changed.")
        return FileResponse(target)
