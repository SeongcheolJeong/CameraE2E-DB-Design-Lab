"""Reference-source revisions using the platform Registry, Worker and workshop lifecycle."""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

from . import design_workshop_storage as storage
from .registry import IntegrityError, canonical, digest
from .virtual_lab_job import _number, sha256

SCHEMA = "camera_reference_workshop_v1"
JOB_KIND = "reference_workshop"
ROUTE = "/api/v1/reference-workshop"
WORKER_TIMEOUT_S = 7200


REFERENCE_ID = "p0014-imx462-bench-v1"


def defaults():
    from .design_workshop_job import workshop_isp

    return {
        "schema": SCHEMA,
        "reference_id": REFERENCE_ID,
        "name": "Patent × TechInsights Reference Bench",
        "lens": {
            "curvature_scale": 1.0,
            "sensor_shift_um": 0.0,
            "optical_profile": "geometric_retrace",
        },
        "pixel": {"cfa_thickness_scale": 1.0},
        "exposure_ms": 4.0,
        "gain": 1.0,
        "luminance_cd_m2": 10000.0,
        "seed": 42,
        "frames": 8,
        "chart_suite": "classic_v1",
        "isp": {
            **workshop_isp({"display_white_level": 10, "denoise": 0, "sharpen": 0}),
            "lsc_enabled": False,
        },
    }


def specification(payload):
    from .design_workshop_job import workshop_isp

    base = defaults()
    if not isinstance(payload, dict) or set(payload) - set(base):
        raise ValueError("Unknown Reference workshop specification fields.")
    spec = {**base, **copy.deepcopy(payload)}
    if spec["schema"] != SCHEMA or spec["reference_id"] != REFERENCE_ID:
        raise ValueError("Select the pinned Reference bench version.")
    if not isinstance(spec["name"], str) or not 1 <= len(spec["name"]) <= 160:
        raise ValueError("A name of 1..160 characters is required.")
    if not isinstance(spec["lens"], dict) or set(spec["lens"]) != set(base["lens"]):
        raise ValueError("Lens requires curvature_scale, sensor_shift_um and optical_profile.")
    for key, lo, hi in (("curvature_scale", 0.995, 1.005), ("sensor_shift_um", -20, 20)):
        spec["lens"][key] = _number(spec["lens"][key], key, lo, hi)
    if spec["lens"]["optical_profile"] not in {"geometric_retrace", "wavefront"}:
        raise ValueError("Choose an explicit geometric_retrace or wavefront optical profile.")
    if not isinstance(spec["pixel"], dict) or set(spec["pixel"]) != {"cfa_thickness_scale"}:
        raise ValueError("Pixel requires cfa_thickness_scale.")
    scale = spec["pixel"]["cfa_thickness_scale"]
    if type(scale) not in (int, float) or scale not in (0.9, 1, 1.1):
        raise ValueError("CFA scale must be 0.9, 1 or 1.1.")
    for key, lo, hi in (("exposure_ms", 0.1, 100), ("gain", 1, 16), ("luminance_cd_m2", 1, 10000)):
        spec[key] = _number(spec[key], key, lo, hi)
    if type(spec["seed"]) is not int or not 0 <= spec["seed"] < 2**32 - 1000:
        raise ValueError("A bounded integer seed is required.")
    if type(spec["frames"]) is not int or not 2 <= spec["frames"] <= 16:
        raise ValueError("Temporal noise requires 2..16 independent readouts.")
    if spec["chart_suite"] not in {"classic_v1", "isp_lab_v1"}:
        raise ValueError("Choose classic_v1 or isp_lab_v1 charts.")
    isp = copy.deepcopy(spec["isp"])
    if not isinstance(isp, dict):
        raise ValueError("ISP must be an object.")
    from . import reference_isp

    if isp.get("schema") == reference_isp.SCHEMA:
        spec["isp"] = reference_isp.validate_profile(isp)
        return spec
    enabled = isp.pop("lsc_enabled", False)
    if type(enabled) is not bool:
        raise ValueError("lsc_enabled must be boolean.")
    spec["isp"] = {**workshop_isp(isp), "lsc_enabled": enabled}
    if spec["isp"]["lsc_strength"] != 0:
        raise ValueError("Reference mode uses the frozen baseline LSC, not a radial proxy.")
    return spec


def acquisition_configuration(spec):
    return {
        **{k: v for k, v in spec.items() if k not in {"name", "isp"}},
        "chart_suite": spec.get("chart_suite", "classic_v1"),
    }


def fingerprint():
    names = [*Path(__file__).parent.glob("reference_workshop*.py")]
    names += [*Path(__file__).parent.glob("reference_isp*.py")]
    names += [*Path(__file__).parent.glob("design_workshop*.py")]
    names += [
        Path(__file__).parent / name
        for name in (
            "lens_db_wavefront_bridge.py",
            "lens_catalog_bridge.py",
            "optics_surfaces.py",
            "prescription_psf.py",
            "lens_factory.py",
            "electrical.py",
            "isp_v2.py",
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
            "bayer_demosaic.py",
            "color.py",
            "iso.py",
            "metrics.py",
        )
    ]
    return {str(p.relative_to(Path(__file__).parents[1])): sha256(p) for p in sorted(names)}


def _directory(service, identity):
    if not identity.startswith("run-") or any(c not in "0123456789abcdef" for c in identity[4:]):
        raise ValueError("Invalid workshop job identity.")
    return storage.root(service) / "reference-workshop" / identity


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
    parent = service.registry.get("reference_design_revisions", parent_id) if parent_id else None
    changed = [key for key in spec if parent is None or spec[key] != parent["specification"][key]]
    return service.registry.put(
        "reference_design_revisions",
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
        "lens.curvature_scale",
        "lens.sensor_shift_um",
        "pixel.cfa_thickness_scale",
        "isp.denoise",
        "isp.sharpen",
        "isp.lsc_enabled",
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
    if summary.get("preview_scope"):
        raise ValueError(
            "A single-frame preview is not a complete capture; use its preserved source."
        )
    if "candidates" in summary:
        selected = next((c for c in summary["candidates"] if c["id"] == candidate_id), None)
        if selected is None or selected.get("status") != "succeeded":
            raise ValueError("Choose an exact successful sweep candidate for replay.")
        return selected
    if candidate_id is not None:
        raise ValueError("This source is not a sweep.")
    return summary


def submit(
    service, payload, *, source_run_id=None, source_candidate_id=None, axes=None, isp_study=None,
    isp_preview=None,
):
    spec = specification(payload)
    source = None
    if source_run_id:
        record = get(service, source_run_id)
        if record["status"] != "succeeded":
            raise ValueError("ISP replay requires a completed workshop capture.")
        original = _source_summary(record, source_candidate_id)
        verify_files(service, record)
        if acquisition_configuration(spec) != acquisition_configuration(original["configuration"]):
            raise ValueError("Same-RAW replay may change only ISP and name.")
        source = {
            "run_id": source_run_id,
            "candidate_id": source_candidate_id,
            "manifest_sha256": record["result"]["summary"]["manifest_sha256"],
        }
    color_id = spec["isp"].get("color_calibration_id")
    if color_id:
        if not source:
            raise ValueError("A color calibration may only replay its preserved source camera.")
        from .reference_isp_color import validate_binding

        calibration = service.registry.get("reference_isp_calibrations", color_id)["calibration"]
        source_path = _directory(service, source_run_id)
        if source_candidate_id:
            source_path /= source_candidate_id
        validate_binding(calibration, source_path)
    if axes is not None:
        expand_sweep(spec, axes)
    snapshot = revision(service, {"kind": "camera", "specification": spec})
    state = storage.status(service)
    count = len(expand_sweep(spec, axes)) if axes else 1
    if isp_study:
        count = (isp_study["candidate_count"] if isp_study["recipe"] == "parameter_tuning"
                 else {"noise_detail": 6, "demosaic": 3,
                       "color_recalibration": 2}[isp_study["recipe"]])
    estimate = 64 * 1024**2 if isp_preview else 2 * 1024**3 * count
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
        "isp_study": copy.deepcopy(isp_study),
        "isp_preview": copy.deepcopy(isp_preview),
        "source_code": fingerprint(),
        "reference_snapshot": reference_snapshot(service),
        "storage_root": state["path"],
        "estimated_bytes": estimate,
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
    }
    return service.registry.submit(doc, digest(doc))


def submit_isp_study(service, payload):
    from . import reference_isp_studies as studies

    source = get(service, payload["source_run_id"])
    selected = _source_summary(source, payload.get("source_candidate_id"))
    study = studies.specification(payload, selected["configuration"])
    if (
        study["recipe"] == "demosaic"
        and selected["configuration"].get("chart_suite") != "isp_lab_v1"
    ):
        raise ValueError(
            "Demosaic study requires a new isp_lab_v1 acquisition with chirp; "
            "no chart was fabricated."
        )
    spec = {
        **selected["configuration"],
        "name": payload.get("name", "Reference ISP · " + study["recipe"]),
        "isp": study["isp"],
    }
    return submit(
        service,
        spec,
        source_run_id=source["id"],
        source_candidate_id=payload.get("source_candidate_id"),
        isp_study=study,
    )


def submit_isp_preview(service, payload):
    from . import reference_isp
    from .reference_isp_studies import FIELD_IDS

    allowed = {"source_run_id", "source_candidate_id", "name", "isp", "field_id", "chart"}
    if (not isinstance(payload, dict) or set(payload) - allowed
            or not {"source_run_id", "isp", "field_id", "chart"} <= set(payload)):
        raise ValueError("Preview requires an exact preserved source, ISP, field_id and chart.")
    if payload["field_id"] not in FIELD_IDS or payload["chart"] not in {
        "resolution", "color", "texture", "uniform", "demosaic_chirp"
    }:
        raise ValueError("Preview requires a supported field and existing chart.")
    selected = _source_summary(get(service, payload["source_run_id"]),
                               payload.get("source_candidate_id"))
    if payload["chart"] == "demosaic_chirp" and selected["configuration"].get(
        "chart_suite", "classic_v1"
    ) != "isp_lab_v1":
        raise ValueError("The preserved source has no chirp capture.")
    profile = reference_isp.validate_profile(payload["isp"])
    spec = {**selected["configuration"], "isp": profile,
            "name": payload.get("name", "Reference ISP · one-frame preview")}
    return submit(service, spec, source_run_id=payload["source_run_id"],
                  source_candidate_id=payload.get("source_candidate_id"),
                  isp_preview={"field_id": payload["field_id"], "chart": payload["chart"]})


def get(service, identity):
    record, binding = service.registry.get_run_with_result_binding(identity)
    if record.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a design workshop run.")
    directory = _directory(service, identity)
    if record.get("storage_root") != str(storage.root(service)):
        raise IntegrityError("Configured project storage differs from the submitted location.")
    if record["status"] == "succeeded":
        summary = record["result"]["summary"]
        if sha256(directory / "manifest.json") != summary["manifest_sha256"]:
            raise IntegrityError("Workshop manifest changed.")
        if binding is None:
            raise IntegrityError("Workshop result has no bound artifact.")
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


def execute_source_study(service, job, source_directory, directory, progress):
    from . import reference_isp_studies as studies
    from . import reference_workshop_engine as engine

    study = job.get("isp_study")
    fitted = None
    if study:
        if study["recipe"] == "color_recalibration":
            from .reference_isp_color import fit_calibration

            progress({"stage": "Munsell64 color fitting · preserved optics and pixel response"})
            fitted = fit_calibration(service.workspace, source_directory)
            known = next(
                (
                    r
                    for r in service.registry.list("reference_isp_calibrations")
                    if r["id"] == fitted["id"]
                ),
                None,
            )
            if known is None:
                service.registry.put(
                    "reference_isp_calibrations",
                    {
                        "calibration": fitted,
                        "source_run_id": job["source"]["run_id"],
                        "source_candidate_id": job["source"].get("candidate_id"),
                    },
                    identity=fitted["id"],
                )
            elif known["calibration"] != fitted:
                raise IntegrityError("Immutable fitted color calibration changed.")
        specs = [
            {**job["specification"], "name": label, "isp": profile}
            for label, profile in studies.profiles(study, fitted["id"] if fitted else None)
        ]
    else:
        specs = expand_sweep(job["specification"], job["axes"])
    results = []
    for index, spec in enumerate(specs):
        identity = f"candidate-{index:02d}"
        progress(
            {
                "stage": f"Preserved RAW · ISP {index + 1}/{len(specs)}",
                "fraction": index / len(specs),
            }
        )
        target = directory / identity
        target.mkdir(exist_ok=True)
        try:
            color_id = spec["isp"].get("color_calibration_id")
            color = (
                fitted
                if fitted and color_id == fitted["id"]
                else (
                    service.registry.get("reference_isp_calibrations", color_id)["calibration"]
                    if color_id
                    else None
                )
            )
            kwargs = {"color_calibration": color} if color else {}
            item = engine.replay(source_directory, target, spec, progress, **kwargs)
            item["status"] = "succeeded"
        except ValueError as error:
            item = {
                "configuration": spec,
                "status": "failed",
                "error": str(error),
                "fields": [],
                "images": {},
                "metrics": {},
            }
        item.update(id=identity, files_base=f"{ROUTE}/runs/{job['id']}/files/{identity}/")
        results.append(item)
    result = {
        "schema": "camera_reference_workshop_sweep_v1",
        "candidates": results,
        "axes": job.get("axes"),
        "configuration": job["specification"],
        "successful_candidates": sum(c["status"] == "succeeded" for c in results),
        "selection": "user_exploration_no_automatic_winner",
        "raw_preserved": True,
        "acquisition_reused": True,
    }
    if study:
        result["study"] = {
            **study,
            "selection": studies.select(results, study),
            "source": job["source"],
            "baseline_candidate_id": "candidate-00",
            "calibration_id": fitted["id"] if fitted else None,
        }
        engine.write_json(directory / "study.json", result["study"])
    return result


def execute(service, job):
    if job.get("db_design_study"):
        from .db_design_job import execute as execute_db_design

        return execute_db_design(service, job)

    if job.get("asset_trial"):
        from .reference_asset_trial import execute as execute_asset_trial

        return execute_asset_trial(service, job)

    if job.get("ircf_study"):
        from .reference_ircf_job import execute as execute_ircf_study

        return execute_ircf_study(service, job)

    if job.get("ghost_study"):
        from .reference_ghost_job import execute as execute_ghost_study

        return execute_ghost_study(service, job)

    if job.get("sensor_mode_study"):
        from .reference_sensor_job import execute as execute_sensor_mode_study

        return execute_sensor_mode_study(service, job)

    if job.get("pixel_study"):
        from .reference_pixel_job import execute as execute_pixel_study

        return execute_pixel_study(service, job)

    from . import reference_workshop_engine as engine

    if job["reference_snapshot"] != reference_snapshot(service):
        raise IntegrityError("Reference source changed after submission.")
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
            if job.get("isp_study") or job.get("axes"):
                result = execute_source_study(service, job, source_directory, directory, progress)
            else:
                color_id = job["specification"]["isp"].get("color_calibration_id")
                kwargs = (
                    {
                        "color_calibration": service.registry.get(
                            "reference_isp_calibrations", color_id
                        )["calibration"]
                    }
                    if color_id
                    else {}
                )
                if job.get("isp_preview"):
                    result = engine.preview(source_directory, directory, job["specification"],
                                            progress, **job["isp_preview"], **kwargs)
                    result["preview_scope"]["source"] = job["source"]
                else:
                    result = engine.replay(
                        source_directory, directory, job["specification"], progress, **kwargs
                    )
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
                            project / "reference-workshop-cache",
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
                    "schema": "camera_reference_workshop_sweep_v1",
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
            {"schema": "camera_reference_workshop_files_v1", "files": files},
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
            canonical(summary).encode(), "reference-workshop-result.json", "application/json"
        )
        return {"summary": summary, "artifacts": [artifact]}


def reference_snapshot(service):
    from .reference_workshop_optics import source_metadata
    from .reference_workshop_sensor import build_cell

    cell = build_cell(service.workspace)
    return {
        "lens": source_metadata(service.workspace),
        "pixel": {
            "cell_sha256": cell["cell_sha256"],
            "provenance": cell["provenance"],
            "source_report": cell["source_report"],
            "pixel_pitch_um": cell["pixel_pitch_um"],
            "cross_section": cell["cross_section"],
            "readout": cell["readout"],
        },
    }


def catalog(service):
    from . import reference_isp, reference_isp_studies
    from .reference_workshop_optics import field_layout

    return {
        "specification": defaults(),
        "isp_v2_profile": reference_isp.default_profile(
            {**defaults()["isp"], "display_white_level": 1}
        ),
        "isp_study_recipes": list(reference_isp_studies.RECIPES),
        "isp_study_constraints": reference_isp_studies.CONSTRAINTS,
        "isp_tuning_constraints": reference_isp_studies.TUNING_CONSTRAINTS,
        "isp_tuning_axes": reference_isp_studies.TUNING_AXES,
        "isp_tuning_paths": list(reference_isp_studies.TUNABLE_PATHS),
        "isp_tuning_max_candidates": reference_isp_studies.MAX_TUNING_CANDIDATES,
        "isp_color_calibrations": [
            {"id": r["id"], **r["calibration"]}
            for r in service.registry.list("reference_isp_calibrations")
        ],
        "prepared_isp_studies": [
            {"recipe": r["isp_study"]["recipe"], "run_id": r["id"], "name": r["name"]}
            for r in service.registry.list_runs()
            if r.get("job_kind") == JOB_KIND and r.get("isp_study") and r["status"] == "succeeded"
        ][:12],
        "catalog": {
            "reference": reference_snapshot(service),
            "fields": field_layout(service.workspace),
            "maximum_candidates": 25,
            "scenes": ["resolution", "color", "uniform", "texture", "demosaic_chirp"],
            "optical_profiles": {
                "geometric_retrace": "Actual patent rays; diffraction excluded",
                "wavefront": "Scalar diffraction; strict field-domain gates",
            },
            "limitations": [
                "Virtual detector bench, not a product sensor area.",
                "Explicit geometric profile excludes diffraction; no hidden fallback.",
                "Source structure measurements and assumed material/readout are distinct.",
            ],
        },
        "storage": storage.status(service),
    }


def install_routes(app, service):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    from .reference_pixel_job import install_routes as install_pixel_routes

    install_pixel_routes(app, service)
    from .reference_sensor_job import install_routes as install_sensor_routes

    install_sensor_routes(app, service)
    from .reference_ghost_job import install_routes as install_ghost_routes

    install_ghost_routes(app, service)
    from .reference_ircf_job import install_routes as install_ircf_routes

    install_ircf_routes(app, service)
    from .db_design_job import install_routes as install_db_design_routes

    install_db_design_routes(app, service)

    @app.get(ROUTE + "/catalog", tags=[JOB_KIND])
    def template():
        return catalog(service)

    @app.get(ROUTE + "/revisions", tags=[JOB_KIND])
    def revisions():
        return {"revisions": service.registry.list("reference_design_revisions")}

    @app.post(ROUTE + "/revisions", status_code=201, tags=[JOB_KIND])
    def create_revision(payload: dict):
        return revision(service, payload)

    @app.get(ROUTE + "/runs", tags=[JOB_KIND])
    def listing():
        return {"runs": [r for r in service.registry.list_runs()
                         if r.get("job_kind") == JOB_KIND and not r.get("pixel_study")
                         and not r.get("sensor_mode_study") and not r.get("ghost_study")
                         and not r.get("ircf_study") and not r.get("db_design_study")]}

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

    @app.post(ROUTE + "/isp-studies", status_code=202, tags=[JOB_KIND])
    def isp_study(payload: dict):
        return submit_isp_study(service, payload)

    @app.post(ROUTE + "/isp-previews", status_code=202, tags=[JOB_KIND])
    def isp_preview(payload: dict):
        return submit_isp_preview(service, payload)

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
                    filename=f"reference-workshop-{identity}.zip",
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
