"""Reference Pixel Design Lab: immutable diagnostics of preserved acquisitions.

This is a Reference workshop job, not another capture engine. Source manifests
are pinned before queueing; its package contains references, never RAW copies.
"""

from __future__ import annotations

import copy
import html
import json
import re
import time
from pathlib import Path

from . import design_workshop_storage as storage
from . import reference_workshop_job as workshop
from .registry import IntegrityError, canonical, digest
from .virtual_lab_job import sha256

SCHEMA = "camera_reference_pixel_study_v1"
ROUTE = workshop.ROUTE + "/pixel-studies"
ESTIMATED_BYTES = 32 * 1024**2


def template():
    return json.loads((Path(__file__).parent / "data/reference_pixel_study.json").read_text())


def fingerprint():
    paths = sorted(Path(__file__).parent.glob("reference_pixel_*.py"))
    paths += [Path(__file__).parent / "design_workshop_sensor.py"]
    paths += [Path(__file__).parent / "data/reference_pixel_study.json"]
    return {p.name: sha256(p) for p in paths}


def specification(payload):
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "name",
        "baseline_candidate_id",
        "sources",
    }:
        raise ValueError("Pixel study requires schema, name, baseline_candidate_id and sources.")
    spec = copy.deepcopy(payload)
    if (
        spec["schema"] != SCHEMA
        or not isinstance(spec["name"], str)
        or not 1 <= len(spec["name"]) <= 160
    ):
        raise ValueError("Select the current Pixel study schema and a name of 1..160 characters.")
    if not isinstance(spec["sources"], list) or len(spec["sources"]) != 3:
        raise ValueError("The first Pixel lab requires three explicitly pinned CFA sources.")
    fields = {
        "id",
        "cfa_thickness_scale",
        "source_run_id",
        "source_candidate_id",
        "source_manifest_sha256",
        "color_run_id",
        "color_manifest_sha256",
    }
    for source in spec["sources"]:
        if not isinstance(source, dict) or set(source) != fields:
            raise ValueError(
                "Each Pixel source must specify exact acquisition and color study pins."
            )
        if type(source["cfa_thickness_scale"]) not in (int, float):
            raise ValueError("CFA thickness scale must be numeric.")
        if not re.fullmatch(r"candidate-[0-9]{2}", str(source["id"])):
            raise ValueError("Invalid candidate identity.")
        for key in ("source_run_id", "color_run_id"):
            if not re.fullmatch(r"run-[0-9a-f]{32}", str(source[key])):
                raise ValueError("Invalid source run identity.")
        if source["source_candidate_id"] is not None and not re.fullmatch(
            r"candidate-[0-9]{2}", str(source["source_candidate_id"])
        ):
            raise ValueError("Invalid source candidate identity.")
        for key in ("source_manifest_sha256", "color_manifest_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(source[key])):
                raise ValueError("A SHA-256 source manifest pin is required.")
    if (
        len({s["id"] for s in spec["sources"]}) != 3
        or {s["cfa_thickness_scale"] for s in spec["sources"]} != {0.9, 1.0, 1.1}
        or not any(
            s["id"] == spec["baseline_candidate_id"] and s["cfa_thickness_scale"] == 1
            for s in spec["sources"]
        )
    ):
        raise ValueError("Use unique CFA 0.9/1/1.1 candidates with CFA 1 as baseline.")
    return spec


def _load_source(service, source):
    acquisition = workshop.get(service, source["source_run_id"])
    color = workshop.get(service, source["color_run_id"])
    for record, key in ((acquisition, "source_manifest_sha256"), (color, "color_manifest_sha256")):
        if (
            record["status"] != "succeeded"
            or record["result"]["summary"]["manifest_sha256"] != source[key]
        ):
            raise IntegrityError("Pixel study source does not match its completed manifest pin.")
    selected = workshop._source_summary(acquisition, source["source_candidate_id"])
    config = selected["configuration"]
    if (
        config["reference_id"] != workshop.REFERENCE_ID
        or config["pixel"]["cfa_thickness_scale"] != source["cfa_thickness_scale"]
        or config["lens"] != workshop.defaults()["lens"]
        or (config["luminance_cd_m2"], config["exposure_ms"], config["gain"], config["frames"])
        != (1000, 4, 1, 8)
    ):
        raise ValueError(
            "Pixel lab v1 requires the pinned Reference lens, "
            "1000 cd/m², 4 ms, gain 1 and 8 frames."
        )
    summary = color["result"]["summary"]
    if summary.get("study", {}).get("recipe") != "color_recalibration":
        raise ValueError("Select a completed frozen/Munsell color recalibration study.")
    profiles = []
    for candidate_id in ("candidate-00", "candidate-01"):
        candidate = workshop._source_summary(color, candidate_id)
        if workshop.acquisition_configuration(config) != workshop.acquisition_configuration(
            candidate["configuration"]
        ):
            raise ValueError("Color source does not describe the same acquisition configuration.")
        isp = candidate["configuration"]["isp"]
        if (
            isp.get("schema") != "camera_reference_isp_v2"
            or isp["demosaic_method"] != "adaptive_laplacian"
            or isp["raw_denoise"]["strength"] != 0
            or isp["linear_sharpen"]["amount"] != 0
            or not isp["lsc_enabled"]
        ):
            raise ValueError(
                "Color comparison requires fixed Adaptive Laplacian, native LSC and NR/sharpen OFF."
            )
        profiles.append({k: v for k, v in isp.items() if k != "color_calibration_id"})
    if profiles[0] != profiles[1]:
        raise ValueError("Only the color calibration may differ within a color study.")
    color_candidates = summary["candidates"]
    if color_candidates[0]["configuration"]["isp"].get(
        "color_calibration_id"
    ) is not None or not color_candidates[1]["configuration"]["isp"].get("color_calibration_id"):
        raise ValueError("Color study must compare the frozen matrix and a fitted calibration.")
    source_dir = workshop._directory(service, acquisition["id"])
    if source["source_candidate_id"]:
        source_dir /= source["source_candidate_id"]
    return source_dir, selected, workshop._directory(service, color["id"]), summary


def submit(service, payload):
    spec = specification(payload)
    common = []
    for source in spec["sources"]:
        _, selected, _, color = _load_source(service, source)
        config = workshop.acquisition_configuration(selected["configuration"])
        config.pop("pixel")
        isp = copy.deepcopy(color["candidates"][0]["configuration"]["isp"])
        common.append((config, isp))
    if any(value != common[0] for value in common[1:]):
        raise ValueError(
            "Pixel comparison must keep all acquisition and ISP conditions fixed "
            "except CFA and calibration."
        )
    state = storage.status(service)
    if (
        state["remaining_bytes"] < ESTIMATED_BYTES
        or state["volume_free_bytes"] - ESTIMATED_BYTES < storage.RESERVE_BYTES
    ):
        raise ValueError("Pixel diagnostic package exceeds the external storage budget.")
    doc = {
        "name": spec["name"],
        "job_kind": workshop.JOB_KIND,
        "plugin_id": SCHEMA,
        "pixel_study": spec,
        "source_code": fingerprint(),
        "storage_root": state["path"],
        "estimated_bytes": ESTIMATED_BYTES,
        "default_worker_timeout_seconds": 7200,
    }
    record = service.registry.submit(doc, digest(doc))
    # A small index avoids loading historical image/metric arrays for the picker.
    service.registry.put("reference_pixel_studies", {"run_id": record["id"], "name": spec["name"]})
    return record


def _source_refs(source):
    prefix = workshop.ROUTE + "/runs/"
    return {
        "acquisition": {
            "run_id": source["source_run_id"],
            "candidate_id": source["source_candidate_id"],
            "manifest_sha256": source["source_manifest_sha256"],
            "files_base": prefix
            + source["source_run_id"]
            + "/files/"
            + (source["source_candidate_id"] + "/" if source["source_candidate_id"] else ""),
        },
        "color": {
            "run_id": source["color_run_id"],
            "manifest_sha256": source["color_manifest_sha256"],
            "candidates": {"frozen": "candidate-00", "munsell": "candidate-01"},
            "files_base": prefix + source["color_run_id"] + "/files/",
        },
    }


def _verify_source_files(service, spec, progress):
    """Verify each pinned package once, yielding cancellation between file batches."""
    seen = set()
    for source in spec["sources"]:
        for identity in (source["source_run_id"], source["color_run_id"]):
            if identity in seen:
                continue
            seen.add(identity)
            directory = workshop._directory(service, identity).resolve()
            manifest = json.loads((directory / "manifest.json").read_text())
            for index, (relative, expected) in enumerate(manifest["files"].items()):
                if index % 128 == 0:
                    progress(
                        f"Verify preserved RAW and artifacts · {identity} · "
                        f"{index}/{len(manifest['files'])}"
                    )
                path = (directory / relative).resolve()
                if (
                    not path.is_relative_to(directory)
                    or not path.is_file()
                    or sha256(path) != expected
                ):
                    raise IntegrityError("Pixel source artifact changed: " + relative)


def execute(service, job):
    from . import reference_pixel_lab as lab
    from .reference_pixel_report import write_report
    from .reference_workshop_engine import write_json

    if (
        job["source_code"] != fingerprint()
        or job["storage_root"] != str(storage.root(service))
        or service.registry.get_run(job["id"])["status"] != "running"
    ):
        raise IntegrityError(
            "Pixel job must be claimed with unchanged diagnostic code and storage."
        )
    started = time.monotonic()
    spec = specification(job["pixel_study"])
    directory = workshop._directory(service, job["id"])
    with storage.reserve(service, ESTIMATED_BYTES) as (_, check):
        directory.mkdir(parents=True, exist_ok=True)

        def progress(message=None):
            if service.registry.get_run(job["id"])["status"] != "running":
                raise RuntimeError("Pixel study cancelled.")
            if message:
                write_json(directory / "progress.json", {"stage": message})

        _verify_source_files(service, spec, progress)
        candidates = []
        for pin in spec["sources"]:
            progress("Preserved response diagnostics · " + pin["id"])
            source_dir, source_summary, color_dir, color_summary = _load_source(service, pin)
            candidate = lab.build_candidate(
                source_dir,
                source_summary,
                color_directory=color_dir,
                color_summary=color_summary,
                cancel=progress,
            )
            candidate.update(id=pin["id"], source_refs=_source_refs(pin))
            candidates.append(candidate)
            check()
        baseline = next(c for c in candidates if c["id"] == spec["baseline_candidate_id"])
        for candidate in candidates:
            lab.add_delta(candidate, baseline)
            target = directory / "candidates" / (candidate["id"] + ".json")
            target.parent.mkdir(exist_ok=True)
            write_json(target, candidate)
        progress("Binding sources and publishing Pixel package")
        for pin in spec["sources"]:
            _load_source(service, pin)
        for candidate in candidates:
            for dependency in candidate["dependencies"]:
                if sha256(Path(dependency["path"])) != dependency["sha256"]:
                    raise IntegrityError("Pixel source changed during diagnostic execution.")
        write_json(directory / "specification.json", spec)
        result = {
            "schema": SCHEMA,
            "name": spec["name"],
            "baseline_candidate_id": spec["baseline_candidate_id"],
            "candidates": [
                {
                    "id": c["id"],
                    "cfa_thickness_scale": c["cfa_thickness_scale"],
                    "status": c.get("status", "succeeded"),
                    "source_refs": c["source_refs"],
                    "detail_url": ROUTE + f"/{job['id']}/candidates/{c['id']}",
                }
                for c in candidates
            ],
            "raw_preserved": True,
            "raw_copied": False,
            "new_acquisitions": 0,
            "selection": "manual_tradeoff_comparison_no_automatic_winner",
            "source_specification_sha256": digest(spec),
            "source_code": job["source_code"],
            "elapsed_seconds": time.monotonic() - started,
            "physical_validation": "not_established",
        }
        write_report({**result, "candidate_details": candidates}, directory)
        write_json(directory / "result.json", result)
        files = {
            str(p.relative_to(directory)): sha256(p)
            for p in sorted(directory.rglob("*"))
            if p.is_file()
            and not p.name.startswith("._")
            and p.name not in {"progress.json", "manifest.json"}
        }
        write_json(
            directory / "manifest.json",
            {"schema": "camera_reference_workshop_files_v1", "files": files},
        )
        summary = {
            **result,
            "files_base": f"{workshop.ROUTE}/runs/{job['id']}/files/",
            "report_url": f"{workshop.ROUTE}/runs/{job['id']}/files/report.html",
            "manifest_sha256": sha256(directory / "manifest.json"),
            "package_bytes": sum(p.stat().st_size for p in directory.rglob("*") if p.is_file()),
        }
        artifact = service.registry.store_blob(
            canonical(summary).encode(), "reference-pixel-study.json", "application/json"
        )
        return {"summary": summary, "artifacts": [artifact]}


def get(service, identity):
    record = workshop.get(service, identity)
    if record.get("plugin_id") != SCHEMA:
        raise ValueError("Selected run is not a Pixel study.")
    return record


def listing(service):
    studies = []
    for entry in service.registry.list("reference_pixel_studies"):
        record = get(service, entry["run_id"])
        summary = (record.get("result") or {}).get("summary", {})
        studies.append(
            {
                "id": record["id"],
                "name": record["name"],
                "status": record["status"],
                "created_at": record["created_at"],
                **{
                    k: summary[k]
                    for k in ("candidates", "report_url", "files_base", "baseline_candidate_id")
                    if k in summary
                },
            }
        )
    return {"studies": studies, "template": template()}


def candidate_detail(service, identity, candidate_id):
    record = get(service, identity)
    if record["status"] != "succeeded":
        raise ValueError("Pixel diagnostics are not complete.")
    if candidate_id not in {c["id"] for c in record["result"]["summary"]["candidates"]}:
        raise ValueError("Candidate is not bound to this Pixel study.")
    directory = workshop._directory(service, identity)
    relative = "candidates/" + candidate_id + ".json"
    path = directory / relative
    manifest = json.loads((directory / "manifest.json").read_text())
    if sha256(path) != manifest["files"][relative]:
        raise IntegrityError("Pixel diagnostic artifact changed.")
    return json.loads(path.read_text())


def save_comparison(service, identity, payload):
    allowed = {
        "name",
        "candidate_id",
        "baseline_candidate_id",
        "field_id",
        "wavelength_nm",
        "site",
        "color_mode",
        "story",
        "angle_mode",
    }
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("Unknown Pixel comparison selection.")
    value = {
        "name": "Pixel design comparison",
        "wavelength_nm": 550,
        "site": "Gr",
        "color_mode": "same_cfa",
        "story": "photon_budget",
        "angle_mode": "pupil",
        **payload,
    }
    if not {"candidate_id", "baseline_candidate_id", "field_id"} <= set(value):
        raise ValueError("Comparison requires candidate, baseline and field identities.")
    if not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 160:
        raise ValueError("Comparison name must have 1..160 characters.")
    if (
        value["site"] not in {"R", "Gr", "Gb", "B"}
        or type(value["wavelength_nm"]) not in {float, int}
        or value["wavelength_nm"] not in range(450, 651, 10)
        or value["color_mode"] not in {"frozen", "munsell", "same_cfa"}
        or value["angle_mode"] not in {"normal", "chief", "pupil"}
        or value["story"] not in {"photon_budget", "angular_match", "color_noise"}
    ):
        raise ValueError("Comparison diagnostic selection is outside the supported domain.")
    candidates = [
        candidate_detail(service, identity, value[k])
        for k in ("candidate_id", "baseline_candidate_id")
    ]
    if any(value.get("field_id") not in {f["id"] for f in c["fields"]} for c in candidates):
        raise ValueError("Choose a field supported by both compared candidates.")
    record = get(service, identity)
    saved = service.registry.put(
        "reference_pixel_comparisons",
        {
            "study_id": identity,
            "selection": value,
            "study_manifest_sha256": record["result"]["summary"]["manifest_sha256"],
            "candidate_sha256": {c["id"]: digest(c) for c in candidates},
            "source_refs": {c["id"]: c["source_refs"] for c in candidates},
            "physical_validation": "not_established",
        },
    )
    return {
        **saved,
        "report_url": f"{ROUTE}/{identity}/comparisons/{saved['id']}/report",
        "download_url": f"{ROUTE}/{identity}/comparisons/{saved['id']}",
    }


def comparison(service, identity, comparison_id):
    record = get(service, identity)
    saved = service.registry.get("reference_pixel_comparisons", comparison_id)
    if (
        saved["study_id"] != identity
        or saved["study_manifest_sha256"] != record["result"]["summary"]["manifest_sha256"]
    ):
        raise IntegrityError("Saved Pixel comparison belongs to another result.")
    for key, expected in saved["candidate_sha256"].items():
        if digest(candidate_detail(service, identity, key)) != expected:
            raise IntegrityError("Saved candidate diagnostic changed.")
    return saved


def install_routes(app, service):
    from fastapi.responses import HTMLResponse

    @app.get(ROUTE, tags=[workshop.JOB_KIND])
    def list_studies():
        return listing(service)

    @app.post(ROUTE, status_code=202, tags=[workshop.JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get(ROUTE + "/{identity}/candidates/{candidate_id}", tags=[workshop.JOB_KIND])
    def detail(identity: str, candidate_id: str):
        return candidate_detail(service, identity, candidate_id)

    @app.post(ROUTE + "/{identity}/comparisons", status_code=201, tags=[workshop.JOB_KIND])
    def save(identity: str, payload: dict):
        return save_comparison(service, identity, payload)

    @app.get(ROUTE + "/{identity}/comparisons/{comparison_id}", tags=[workshop.JOB_KIND])
    def download(identity: str, comparison_id: str):
        return comparison(service, identity, comparison_id)

    @app.get(
        ROUTE + "/{identity}/comparisons/{comparison_id}/report",
        response_class=HTMLResponse,
        tags=[workshop.JOB_KIND],
    )
    def report(identity: str, comparison_id: str):
        from urllib.parse import urlencode

        value = comparison(service, identity, comparison_id)
        selection = value["selection"]
        url = "/reference-workshop.html?" + urlencode(
            {
                "pixel_study": identity,
                "pixel_candidate": selection["candidate_id"],
                "pixel_baseline": selection["baseline_candidate_id"],
                "field": selection["field_id"],
                "pixel_wave": selection["wavelength_nm"],
                "pixel_site": selection["site"],
                "pixel_color": selection["color_mode"],
                "pixel_story": selection["story"],
                "pixel_mode": selection["angle_mode"],
            }
        )
        return (
            "<!doctype html><html lang='ko'><meta charset='utf-8'>"
            "<title>Pixel comparison</title><style>body{font:16px system-ui;"
            "max-width:1000px;margin:40px auto;padding:20px}"
            "pre{white-space:pre-wrap}</style><h1>"
            + html.escape(selection["name"])
            + "</h1><p>Saved comparison of model-derived responses. "
            "RAW remains in the referenced external acquisitions.</p><a href='"
            + html.escape(url, quote=True)
            + "'>Open Pixel Design Lab</a><pre>"
            + html.escape(json.dumps(value, indent=2, ensure_ascii=False))
            + "</pre></html>"
        )
