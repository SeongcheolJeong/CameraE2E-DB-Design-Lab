"""One durable job lifecycle for virtual camera Lab capture and RAW replay."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import shutil
from pathlib import Path

from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_virtual_lab_v1"
JOB_KIND = "virtual_lab"
ROUTE = "/api/v1/virtual-lab"
WORKER_TIMEOUT_S = 3600


def wide_angle_defaults():
    """Explicit approximation and resource policy; never a DB-lens extrapolation."""
    return {
        "projection": "rectilinear",
        "native_shape_row_col": [1080, 1920],
        "exit_pupil_distance_mm": 6.0,
        "radial_blur_um": 0.0,
        "tangential_blur_um": 0.0,
        "transmission": 1.0,
        "relative_illumination_power": 0.0,
        "sampling": "auto",
        "tile_size": 256,
        "max_working_bytes": 4 * 1024**3,
        "min_free_disk_bytes": 10 * 1024**3,
    }


def wide_angle_specification(value):
    defaults = wide_angle_defaults()
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("Unsupported wide-angle configuration fields.")
    result = {**defaults, **copy.deepcopy(value)}
    if result["projection"] not in ("rectilinear", "equidistant"):
        raise ValueError("Choose rectilinear or equidistant wide-angle projection.")
    if result["native_shape_row_col"] != [1080, 1920]:
        raise ValueError("Wide-angle capture requires actual 1920 by 1080 native RAW.")
    for key, low, high in (
        ("exit_pupil_distance_mm", 0.1, 1000),
        ("radial_blur_um", 0, 30),
        ("tangential_blur_um", 0, 30),
        ("transmission", 0.001, 1),
        ("relative_illumination_power", 0, 8),
    ):
        result[key] = _number(result[key], key, low, high)
    sampling = result["sampling"]
    if not (sampling == "auto" or type(sampling) is int and sampling in (2, 4, 8)):
        raise ValueError("Wide-angle sampling must be auto, 2, 4 or 8.")
    for key, low, high in (
        ("tile_size", 32, 512),
        ("max_working_bytes", 64 * 1024**2, 64 * 1024**3),
        ("min_free_disk_bytes", 0, 1024**4),
    ):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key} must be an integer within {low}..{high}.")
    if result["tile_size"] % 2:
        raise ValueError("Wide-angle tiles must preserve even RGGB coordinates.")
    return result


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _number(value, label, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be within {low}..{high}.")
    return float(value)


def isp_specification(value):
    defaults = {
        "profile": "balanced",
        "wb": [1, 1, 1],
        "ccm": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "denoise": 0.2,
        "sharpen": 0.3,
        "tone_gamma": 2.2,
        "black_offset_dn": 0,
        "lsc_strength": 0,
    }
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("Unsupported ISP settings.")
    presets = {"balanced": (0.2, 0.3), "low_noise": (0.85, 0), "detail": (0, 1.2)}
    if value.get("profile", "balanced") in presets:
        defaults["denoise"], defaults["sharpen"] = presets[value.get("profile", "balanced")]
    result = {**defaults, **value}
    if result["profile"] not in {"balanced", "low_noise", "detail"}:
        raise ValueError("Choose balanced, low_noise or detail ISP.")
    if not isinstance(result["wb"], list) or len(result["wb"]) != 3:
        raise ValueError("WB requires three channel multipliers.")
    result["wb"] = [_number(v, "WB", 0.1, 8) for v in result["wb"]]
    ccm = result["ccm"]
    if (
        not isinstance(ccm, list)
        or len(ccm) != 3
        or any(not isinstance(r, list) or len(r) != 3 for r in ccm)
    ):
        raise ValueError("CCM requires a 3 by 3 matrix.")
    result["ccm"] = [[_number(v, "CCM", -8, 8) for v in row] for row in ccm]
    for key, low, high in (
        ("denoise", 0, 1),
        ("sharpen", 0, 3),
        ("tone_gamma", 0.5, 4),
        ("black_offset_dn", -32, 32),
        ("lsc_strength", 0, 2),
    ):
        result[key] = _number(result[key], key, low, high)
    return result


def default_specification(catalog):
    return {
        "schema": SCHEMA,
        "name": "Camera Virtual Lab",
        "lens_id": catalog["lenses"][0]["id"],
        "sensor_id": catalog["sensors"][0]["id"],
        "scene": "resolution",
        "test_suite": "all",
        "illuminant": "D65",
        "luminance_cd_m2": 1000,
        "exposure_ms": 4,
        "gain": 1,
        "frames": 8,
        "isp": isp_specification({}),
        "optics": {
            "f_number": round(
                catalog["lenses"][0].get(
                    "f_number", catalog["lenses"][0].get("model", {}).get("effective_f_number", 4)
                ),
                6,
            ),
            "defocus_um": 0,
        },
        "seed": 42,
        "field_model": {"mode": "full_field", "horizontal_fov_deg": None},
        "angular_response": {
            "mode": "physical_relative",
            "exponents_r_gr_gb_b": [2, 2, 2, 2],
        },
        "wide_angle": wide_angle_defaults(),
    }


def specification(payload, catalog):
    defaults = default_specification(catalog)
    if not isinstance(payload, dict) or set(payload) - set(defaults):
        raise ValueError("Unsupported Virtual Lab fields.")
    spec = {**defaults, **copy.deepcopy(payload)}
    # Captures from before field/CRA support keep their original acquisition semantics.
    wide = spec["lens_id"] == "wide_parametric"
    if "field_model" not in payload and not wide:
        spec["field_model"] = {"mode": "center_local", "horizontal_fov_deg": None}
    if "angular_response" not in payload and not wide:
        spec["angular_response"] = {
            "mode": "off",
            "exponents_r_gr_gb_b": [2, 2, 2, 2],
        }
    if (
        spec["schema"] != SCHEMA
        or not isinstance(spec["name"], str)
        or not 1 <= len(spec["name"]) <= 160
    ):
        raise ValueError("Versioned Lab specification and name required.")
    for key, collection in (("lens_id", "lenses"), ("sensor_id", "sensors")):
        if spec[key] not in {entry["id"] for entry in catalog[collection]}:
            raise ValueError(f"Unknown {key}.")
    if spec["scene"] not in {"resolution", "color", "uniform", "tone", "natural", "grid"}:
        raise ValueError("Unknown Lab scene.")
    if spec["scene"] == "grid" and not wide:
        raise ValueError("Directional grid scene requires the parametric wide-angle lens.")
    if spec["test_suite"] not in {"all", "resolution", "color", "noise", "tone"}:
        raise ValueError("Unknown Lab test suite.")
    if spec["illuminant"] not in {"D65", "A"}:
        raise ValueError("Choose D65 or A illumination.")
    for key, low, high in (
        ("luminance_cd_m2", 0.01, 10000),
        ("exposure_ms", 0.1, 100),
        ("gain", 1, 16),
    ):
        spec[key] = _number(spec[key], key, low, high)
    if type(spec["frames"]) is not int or not 2 <= spec["frames"] <= 16:
        raise ValueError("Temporal noise requires 2..16 independent captures.")
    if type(spec["seed"]) is not int or not 0 <= spec["seed"] < 2**32:
        raise ValueError("Seed must be an unsigned 32-bit integer.")
    spec["isp"] = isp_specification(spec["isp"])
    spec["wide_angle"] = wide_angle_specification(spec["wide_angle"])
    if not wide and spec["wide_angle"] != wide_angle_defaults():
        raise ValueError("Wide-angle controls require the parametric wide-angle lens.")
    if wide and "optics" not in payload:
        spec["optics"] = {"f_number": 2.8, "defocus_um": 0}
    optics = spec["optics"]
    if not isinstance(optics, dict) or set(optics) != {"f_number", "defocus_um"}:
        raise ValueError("Declare aperture and defocus settings.")
    spec["optics"] = {
        "f_number": _number(optics["f_number"], "f-number", 1, 16),
        "defocus_um": _number(optics["defocus_um"], "defocus", -100, 100),
    }
    lens = next(item for item in catalog["lenses"] if item["id"] == spec["lens_id"])
    if lens.get("supports", {}).get("f_number") is False:
        fixed = lens.get("f_number", lens.get("model", {}).get("effective_f_number"))
        if fixed is None or not math.isclose(spec["optics"]["f_number"], fixed, rel_tol=1e-6):
            raise ValueError(
                "Stored DB PSF has fixed f-number; use the declared value or general optics."
            )
    if lens.get("supports", {}).get("defocus_um") is False and spec["optics"]["defocus_um"] != 0:
        raise ValueError("Stored DB PSF cannot change focus; choose general optics.")
    field = spec["field_model"]
    if (
        not isinstance(field, dict)
        or set(field) != {"mode", "horizontal_fov_deg"}
        or field["mode"] not in ("center_local", "full_field")
    ):
        raise ValueError("Choose center_local or full_field and declare horizontal_fov_deg.")
    if field["horizontal_fov_deg"] is not None:
        field["horizontal_fov_deg"] = _number(
            field["horizontal_fov_deg"], "horizontal FOV degrees", 2, 120 if wide else 40
        )
    if wide:
        if field["mode"] != "full_field":
            raise ValueError("Wide-angle capture requires full_field optics.")
        if field["horizontal_fov_deg"] is None:
            field["horizontal_fov_deg"] = 120.0
    angular = spec["angular_response"]
    if (
        not isinstance(angular, dict)
        or set(angular) != {"mode", "exponents_r_gr_gb_b"}
        or angular["mode"] not in ("off", "physical_relative", "physical_pupil", "cosine_power")
    ):
        raise ValueError(
            "Declare off, physical_relative, physical_pupil or cosine_power angular response."
        )
    if angular["mode"] == "physical_pupil" and (
        lens["id"] == "analytic" or lens.get("supports", {}).get("pupil_directions") is False
    ):
        raise ValueError("Pupil averaging requires stored lens pupil directions and weights.")
    exponents = angular["exponents_r_gr_gb_b"]
    if not isinstance(exponents, list) or len(exponents) != 4:
        raise ValueError("Angular cosine exponents require R, Gr, Gb, B values.")
    angular["exponents_r_gr_gb_b"] = [
        _number(v, "angular cosine exponent", 0, 16) for v in exponents
    ]
    if field["mode"] == "center_local" and angular["mode"] != "off":
        raise ValueError("Enable full_field optics before applying angular pixel response.")
    return spec


def _catalog(service):
    from .virtual_lab_engine import catalog

    return catalog(service.workspace)


def _directory(service, identity):
    if not identity.startswith("run-") or any(c not in "0123456789abcdef" for c in identity[4:]):
        raise ValueError("Invalid Lab run identity.")
    return service.registry.directory / "virtual-lab" / identity


def _source_fingerprint():
    return {p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("virtual_lab*.py"))}


def submit(service, payload, *, source_run_id=None):
    catalog = _catalog(service)
    spec = specification(payload, catalog)
    source = None
    if source_run_id:
        record = get(service, source_run_id)
        if record["status"] != "succeeded":
            raise ValueError("Replay requires a completed Lab capture.")
        _verify_files(service, record)
        original = specification(record["specification"], catalog)
        if any(spec[key] != original[key] for key in spec if key not in {"name", "isp"}):
            raise ValueError("RAW replay may change only the name and ISP.")
        source = {
            "run_id": source_run_id,
            "manifest_sha256": record["result"]["summary"]["manifest_sha256"],
        }
    document = {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source": source,
        "catalog_sha256": digest(catalog),
        "source_code": _source_fingerprint(),
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
    }
    return service.registry.submit(document, digest(document))


def resume(service, identity):
    """New immutable job, reusing only byte-bound partial wide acquisition tiles."""
    record = get(service, identity)
    if record["status"] not in {"failed", "cancelled"}:
        raise ValueError("Resume requires a failed or cancelled wide-angle acquisition.")
    if record["specification"]["lens_id"] != "wide_parametric" or record.get("source"):
        raise ValueError("Only wide-angle acquisition tiles can be resumed.")
    catalog = _catalog(service)
    if record["source_code"] != _source_fingerprint() or record["catalog_sha256"] != digest(
        catalog
    ):
        raise IntegrityError(
            "Code or models changed; submit a new acquisition instead of resuming."
        )
    root = _directory(service, identity) / "wide-checkpoints"
    if not root.is_dir() or root.is_symlink():
        raise ValueError("No wide-angle acquisition checkpoints are available.")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise IntegrityError("Checkpoint path escapes its acquisition directory.")
        if path.is_file():
            files[str(path.relative_to(root))] = sha256(path)
    if not files:
        raise ValueError("No wide-angle acquisition checkpoints are available.")
    document = {
        "name": record["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": specification(record["specification"], catalog),
        "source": None,
        "resume_source": {"run_id": identity, "files": files},
        "catalog_sha256": digest(catalog),
        "source_code": _source_fingerprint(),
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
    }
    return service.registry.submit(document, digest(document))


def _copy_resume_checkpoints(service, job, directory):
    source = _directory(service, job["resume_source"]["run_id"]) / "wide-checkpoints"
    target = directory / "wide-checkpoints"
    snapshot_bytes = sum(
        (source / relative).stat().st_size for relative in job["resume_source"]["files"]
    )
    reserve = job["specification"]["wide_angle"]["min_free_disk_bytes"]
    if shutil.disk_usage(directory).free - snapshot_bytes < reserve:
        raise ValueError(
            "Insufficient storage to resume while preserving the requested free space."
        )
    for relative, expected in job["resume_source"]["files"].items():
        original, copied = source / relative, target / relative
        if (
            original.is_symlink()
            or not original.resolve().is_relative_to(source.resolve())
            or not copied.resolve().is_relative_to(target.resolve())
            or not original.is_file()
            or sha256(original) != expected
        ):
            raise IntegrityError("Frozen checkpoint changed; request resume again: " + relative)
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, copied)
        if sha256(copied) != expected:
            raise IntegrityError("Checkpoint changed while copying: " + relative)


def _report(result, directory):
    from .virtual_lab_report import render

    render(result, directory)


def execute(service, job):
    from . import virtual_lab_engine as engine

    if (
        job.get("job_kind") != JOB_KIND
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError("Claimed running Lab job required.")
    if job["source_code"] != _source_fingerprint() or job["catalog_sha256"] != digest(
        _catalog(service)
    ):
        raise IntegrityError("Lab code or model catalog changed after submission; submit again.")
    directory = _directory(service, job["id"])
    directory.mkdir(parents=True, exist_ok=True)

    def progress(message):
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Lab job cancelled.")
        value = message if isinstance(message, dict) else {"message": str(message)}
        (directory / "progress.json").write_text(json.dumps(value, ensure_ascii=False))

    if job.get("source"):
        source_record = get(service, job["source"]["run_id"])
        _verify_files(service, source_record)
        source = _directory(service, source_record["id"])
        result = engine.replay(
            service.workspace, source, directory, job["specification"], on_progress=progress
        )
        _verify_files(service, source_record)
    else:
        if job.get("resume_source"):
            progress("Restoring hash-verified wide-angle acquisition tiles")
            _copy_resume_checkpoints(service, job, directory)
        result = engine.run(
            service.workspace, directory, job["specification"], on_progress=progress
        )
    result["configuration"] = job["specification"]
    result["source_run_id"] = job.get("source", {}).get("run_id") if job.get("source") else None
    if job.get("resume_source"):
        result["resumed_from_run_id"] = job["resume_source"]["run_id"]
    (directory / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    )
    _report(result, directory)
    files = {
        str(p.relative_to(directory)): sha256(p)
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p.name not in {"progress.json", "manifest.json"}
    }
    (directory / "manifest.json").write_text(
        json.dumps({"schema": "camera_virtual_lab_files_v1", "files": files}, indent=2)
    )
    summary = {
        **result,
        "files_base": f"{ROUTE}/runs/{job['id']}/files/",
        "report_url": f"{ROUTE}/runs/{job['id']}/files/report.html",
        "manifest_sha256": sha256(directory / "manifest.json"),
        "physical_validation": "not_established",
    }
    artifact = service.registry.store_blob(
        canonical(summary).encode(), "virtual-lab-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [artifact]}


def get(service, identity):
    record = service.registry.get_run(identity)
    if record.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a Virtual Lab run.")
    if record["status"] == "succeeded":
        summary = record["result"]["summary"]
        artifact = record["result"]["artifacts"][0]
        binding = service.registry.artifact_binding(
            artifact["sha256"], owner_kind="runs", owner_id=identity
        )
        if json.loads(binding["path"].read_bytes()) != summary:
            raise IntegrityError("Lab summary changed.")
        if sha256(_directory(service, identity) / "manifest.json") != summary["manifest_sha256"]:
            raise IntegrityError("Lab file manifest changed.")
    progress = _directory(service, identity) / "progress.json"
    if record["status"] in {"queued", "running"} and progress.exists():
        try:
            record["progress"] = json.loads(progress.read_text())
        except json.JSONDecodeError:
            pass
    return record


def _verify_files(service, record):
    directory = _directory(service, record["id"])
    manifest = json.loads((directory / "manifest.json").read_text())
    for relative, expected in manifest["files"].items():
        target = (directory / relative).resolve()
        if (
            not target.is_relative_to(directory.resolve())
            or not target.is_file()
            or sha256(target) != expected
        ):
            raise IntegrityError("Lab source artifact changed: " + relative)


def install_routes(app, service):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    @app.get(ROUTE + "/template", tags=["virtual_lab"])
    def template():
        catalog = _catalog(service)
        return {"catalog": catalog, "specification": default_specification(catalog)}

    @app.post(ROUTE + "/runs", status_code=202, tags=["virtual_lab"])
    def create(payload: dict):
        return submit(service, payload)

    @app.post(ROUTE + "/replays", status_code=202, tags=["virtual_lab"])
    def replay(payload: dict):
        if set(payload) != {"source_run_id", "name", "isp"}:
            raise ValueError("Replay requires source_run_id, name and ISP.")
        original = get(service, payload["source_run_id"])
        spec = {**original["specification"], "name": payload["name"], "isp": payload["isp"]}
        return submit(service, spec, source_run_id=payload["source_run_id"])

    @app.get(ROUTE + "/runs", tags=["virtual_lab"])
    def listing():
        return [r for r in service.registry.list_runs() if r.get("job_kind") == JOB_KIND]

    @app.get(ROUTE + "/runs/{identity}", tags=["virtual_lab"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(ROUTE + "/runs/{identity}/cancel", tags=["virtual_lab"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.post(ROUTE + "/runs/{identity}/resume", status_code=202, tags=["virtual_lab"])
    def resume_run(identity: str):
        return resume(service, identity)

    @app.get(ROUTE + "/runs/{identity}/bundle", tags=["virtual_lab"])
    def bundle(identity: str):
        import tempfile
        import zipfile

        record = get(service, identity)
        if record["status"] != "succeeded":
            raise HTTPException(409, "Complete the Lab run before exporting.")
        _verify_files(service, record)
        directory = _directory(service, identity)
        files = json.loads((directory / "manifest.json").read_text())["files"]
        handle = tempfile.NamedTemporaryFile(
            prefix="camera-virtual-lab-", suffix=".zip", delete=False
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
                filename=f"camera-virtual-lab-{identity}.zip",
                media_type="application/zip",
                background=BackgroundTask(target.unlink, missing_ok=True),
            )
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    @app.get(ROUTE + "/runs/{identity}/files/{relative:path}", tags=["virtual_lab"])
    def file(identity: str, relative: str):
        record = get(service, identity)
        if record["status"] != "succeeded":
            raise HTTPException(404, "Result is not completed.")
        directory = _directory(service, identity).resolve()
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory) or not target.is_file():
            raise HTTPException(404, "Lab file not found.")
        files = json.loads((directory / "manifest.json").read_text())["files"]
        expected = (
            record["result"]["summary"]["manifest_sha256"]
            if relative == "manifest.json"
            else files.get(relative)
        )
        if expected is None:
            raise HTTPException(404, "Not a bound Lab artifact.")
        if sha256(target) != expected:
            raise IntegrityError("Lab artifact changed.")
        return FileResponse(target)
