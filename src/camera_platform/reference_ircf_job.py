"""IRCF studies use the shared Reference queue, immutable artifacts and SSD budget."""

from __future__ import annotations

import copy
import json
import re
import shutil
import time
from pathlib import Path

import numpy as np

from . import design_workshop_storage as storage
from . import reference_workshop_job as workshop
from .registry import IntegrityError, canonical, digest
from .virtual_lab_job import sha256

SCHEMA = "camera_reference_ircf_study_v1"
ROUTE = workshop.ROUTE + "/ircf-studies"
FILTERS = ("baseline", "cutoff_minus20", "cutoff_plus20", "nir_leak_1pct")
FIELDS = (
    "center",
    "mid-right",
    "mid-top",
    "mid-left",
    "mid-bottom",
    "outer-ne",
    "outer-nw",
    "outer-sw",
    "outer-se",
)
ASSUMPTIONS = [
    (
        "400–700 nm structure-based visible response; >700–1000 nm separately declared NIR QE "
        "proxy, not measured IMX462 performance."
    ),
    (
        "Thin filter parallel to the sensor in the final air space; pupil incidence, not scene "
        "field angle."
    ),
    (
        "Interference angle shift uses declared effective index; imported transmission is not "
        "measured angular calibration."
    ),
    (
        "Separable geometric PSF and pupil spectral weighting; filter-dependent PSF shape, "
        "refraction, focus shift and reflected ghosts excluded."
    ),
    (
        "Patent apertures and nd/Vd dispersion are research hypotheses; NIR glass transmission "
        "and diffraction are not measured."
    ),
    (
        "Fixed 1000 cd/m² photopic input, 4 ms, gain 1, 25 C, native RGGB256² and eight fresh "
        "readouts per case."
    ),
    (
        "Same persistent electrical model/FPN policy, distinct seed sequence per acquisition; "
        "CCM variants share identical RAW."
    ),
    (
        "Munsell64 fits only visible400–700 D65; Macbeth24 full-band holdout is never used for "
        "fitting."
    ),
    (
        "Fixed baseline-filter LSC and common display; one center CCM per filter used "
        "throughout the field."
    ),
    (
        "Study longwave contribution means wavelength >700 nm, not a claim that all such light "
        "is invisible."
    ),
]


def template():
    from .reference_sensor_job import template as sensor_template

    return {
        "schema": SCHEMA,
        "name": "Reference IRCF Design Lab",
        "source": sensor_template()["source"],
        "fields": ["center", "outer-ne"],
        "charts": ["color", "uniform"],
        "illuminants": ["day", "ill_a"],
        "filters": list(FILTERS),
        "wavelength_step_nm": 10,
        "nir_scale": 1.0,
        "effective_index": 1.8,
        "angular_mode": "pupil",
        "include_angle_diagnostic": True,
        "frames": 8,
        "exposure_ms": 4.0,
        "gain": 1.0,
        "luminance_cd_m2": 1000.0,
        "seed": 42,
        "isp": {"display_white_level": 1.0, "tone_gamma": 2.2},
    }


def specification(payload):
    if not isinstance(payload, dict) or set(payload) != set(template()):
        raise ValueError("IRCF study requires explicit template fields.")
    s = copy.deepcopy(payload)
    if s["schema"] != SCHEMA or not isinstance(s["name"], str) or not 1 <= len(s["name"]) <= 160:
        raise ValueError("Invalid IRCF schema or name.")
    src = s["source"]
    if (
        not isinstance(src, dict)
        or set(src) != {"run_id", "candidate_id", "manifest_sha256"}
        or not re.fullmatch(r"run-[0-9a-f]{32}", str(src["run_id"]))
        or not re.fullmatch(r"[0-9a-f]{64}", str(src["manifest_sha256"]))
    ):
        raise ValueError("Pin a Reference source run and manifest.")
    if src["candidate_id"] is not None and not re.fullmatch(
        r"candidate-[0-9]{2}", str(src["candidate_id"])
    ):
        raise ValueError("Invalid source candidate.")
    for key, choices in (
        ("fields", FIELDS),
        ("charts", ("color", "uniform")),
        ("illuminants", ("day", "ill_a")),
        ("filters", FILTERS),
    ):
        v = s[key]
        if (
            not isinstance(v, list)
            or not v
            or any(not isinstance(x, str) or x not in choices for x in v)
            or len(set(v)) != len(v)
        ):
            raise ValueError("Choose supported unique " + key)
    for key, choices in (
        ("wavelength_step_nm", (5, 10)),
        ("nir_scale", (0.5, 1, 2)),
        ("frames", (8,)),
        ("exposure_ms", (4,)),
        ("gain", (1,)),
        ("luminance_cd_m2", (1000,)),
    ):
        if type(s[key]) not in (int, float) or s[key] not in choices:
            raise ValueError("Unsupported fixed study condition: " + key)
    if (
        type(s["effective_index"]) not in (int, float)
        or not np.isfinite(s["effective_index"])
        or not 1.4 <= s["effective_index"] <= 2.5
    ):
        raise ValueError("Effective index requires1.4..2.5.")
    if (
        s["angular_mode"] not in ("pupil", "independent")
        or type(s["include_angle_diagnostic"]) is not bool
    ):
        raise ValueError("Invalid filter angular mode.")
    if type(s["seed"]) is not int or not 0 <= s["seed"] < 2**31:
        raise ValueError("Invalid seed.")
    if not isinstance(s["isp"], dict) or set(s["isp"]) != {"display_white_level", "tone_gamma"}:
        raise ValueError("Fixed common display settings required.")
    for key, lo, hi in (("display_white_level", 1, 100), ("tone_gamma", 0.5, 4)):
        v = s["isp"][key]
        if type(v) not in (float, int) or not np.isfinite(v) or not lo <= v <= hi:
            raise ValueError("Invalid display setting.")
    return s


def case_specs(spec):
    result = []
    for field in spec["fields"]:
        for illuminant in spec["illuminants"]:
            for chart in spec["charts"]:
                for profile in spec["filters"]:
                    result.append(
                        {
                            "field_id": field,
                            "illuminant": illuminant,
                            "chart": chart,
                            "filter_id": profile,
                            "angular_mode": spec["angular_mode"],
                        }
                    )
    if (
        spec["include_angle_diagnostic"]
        and spec["angular_mode"] == "pupil"
        and "outer-ne" in spec["fields"]
        and "uniform" in spec["charts"]
        and "baseline" in spec["filters"]
    ):
        for illuminant in spec["illuminants"]:
            result.append(
                {
                    "field_id": "outer-ne",
                    "illuminant": illuminant,
                    "chart": "uniform",
                    "filter_id": "baseline",
                    "angular_mode": "independent",
                }
            )
    return [{"id": f"case-{i:03d}", **r, "seed_offset": i * 100} for i, r in enumerate(result)]


def fingerprint():
    names = (
        "reference_ircf_job.py",
        "reference_ircf_engine.py",
        "reference_ircf_spectra.py",
        "reference_ircf_optics.py",
        "reference_workshop_optics.py",
        "reference_workshop_sensor.py",
        "design_workshop_sensor.py",
        "reference_isp_color.py",
        "reference_sensor_modes.py",
        "electrical.py",
    )
    result = {name: sha256(Path(__file__).parent / name) for name in names}
    from pyisetcam import physical_estimate

    result["pyisetcam/physical_estimate.py"] = sha256(Path(physical_estimate.__file__))
    return result


def source(service, spec, verify=False):
    from .reference_sensor_job import _load_source

    directory, capture = _load_source(
        service, {**spec, "charts": ["uniform", "color"]}, verify=verify
    )
    if capture["configuration"]["lens"] != {
        "curvature_scale": 1.0,
        "optical_profile": "geometric_retrace",
        "sensor_shift_um": 0.0,
    }:
        raise ValueError("IRCF optical profile requires the pinned unchanged patent prescription.")
    return directory, capture


def submit(service, payload):
    spec = specification(payload)
    source(service, spec)
    count = len(case_specs(spec))
    estimate = count * 96 * 1024**2 + 1536 * 1024**2
    state = storage.status(service)
    if (
        state["remaining_bytes"] < estimate
        or state["volume_free_bytes"] - estimate < storage.RESERVE_BYTES
    ):
        raise ValueError("IRCF study exceeds SSD budget; no resolution reduction.")
    doc = {
        "name": spec["name"],
        "job_kind": workshop.JOB_KIND,
        "plugin_id": SCHEMA,
        "ircf_study": spec,
        "source_code": fingerprint(),
        "storage_root": state["path"],
        "estimated_bytes": estimate,
        "default_worker_timeout_seconds": 7200,
    }
    record = service.registry.submit(doc, digest(doc))
    if not any(
        e["run_id"] == record["id"] for e in service.registry.list("reference_ircf_studies")
    ):
        service.registry.put(
            "reference_ircf_studies", {"run_id": record["id"], "name": spec["name"]}
        )
    return record


def execute(service, job):
    from . import reference_ircf_engine as engine
    from . import reference_ircf_optics as optics
    from . import reference_ircf_spectra as spectral
    from .reference_workshop_engine import write_json

    if job["source_code"] != fingerprint() or job["storage_root"] != str(storage.root(service)):
        raise IntegrityError("IRCF code or storage changed; submit again.")
    spec = specification(job["ircf_study"])
    start = time.monotonic()
    directory = workshop._directory(service, job["id"])
    with storage.reserve(service, job["estimated_bytes"]) as (project, check):
        directory.mkdir(parents=True, exist_ok=True)

        def progress(message=None):
            if service.registry.get_run(job["id"])["status"] != "running":
                raise RuntimeError("IRCF study cancelled.")
            if message is not None:
                write_json(
                    directory / "progress.json",
                    message if isinstance(message, dict) else {"stage": str(message)},
                )

        progress("Checking pinned source and broad-band assets")
        source(service, spec, verify=True)
        wave = spectral.wavelength_grid(spec["wavelength_step_nm"])
        assets = spectral.load_assets(service.workspace, wave)
        pixel = spectral.build_pixel_response(service.workspace, wave, nir_scale=spec["nir_scale"])
        solution, folder = optics.solve(
            service.workspace,
            project / "reference-workshop-cache",
            step_nm=spec["wavelength_step_nm"],
            on_progress=progress,
        )
        fields = [
            f for f in solution["result"]["fields"] if f["id"] in set(["center", *spec["fields"]])
        ]
        if any(f["status"] != "succeeded" for f in fields):
            reasons = "; ".join(
                f["id"] + ": " + "; ".join(f["issues"])
                for f in fields
                if f["status"] != "succeeded"
            )
            raise ValueError(
                "Requested IRCF optical field is unsupported; no fallback used. " + reasons
            )
        progress("Fitting visible Munsell CCM and fixed baseline LSC")
        matrices, lsc, calibration = engine.calibrations(
            solution, assets, pixel["response"], fields, spec["filters"], spec["effective_index"]
        )
        write_json(directory / "specification.json", spec)
        write_json(directory / "spectral-assets.json", engine.json_value(assets["metadata"]))
        write_json(directory / "pixel-response.json", engine.json_value(pixel))
        write_json(directory / "calibration.json", engine.json_value(calibration))
        write_json(directory / "optical-response.json", solution["result"])
        shutil.copyfile(folder / "optics.npz", directory / "optics.npz")
        np.savez_compressed(directory / "lsc.npz", **lsc)
        cases = []
        for item in case_specs(spec):
            progress(
                f"{item['id']} · {item['field_id']} · {item['illuminant']} · "
                f"{item['chart']} · {item['filter_id']}"
            )
            cases.append(
                engine.run_case(
                    directory, item, spec, solution, assets, pixel, matrices, lsc, progress
                )
            )
            check()
        progress("Publishing preserved RAW, CCM and comparison results")
        source(service, spec, verify=True)
        result = {
            "schema": SCHEMA,
            "name": spec["name"],
            "specification": spec,
            "source": spec["source"],
            "cases": [
                {
                    k: c[k]
                    for k in ("id", "field_id", "illuminant", "chart", "filter_id", "angular_mode")
                }
                for c in cases
            ],
            "curves": [
                {
                    k: c[k]
                    for k in (
                        "id",
                        "field_id",
                        "illuminant",
                        "chart",
                        "filter_id",
                        "angular_mode",
                        "metrics",
                    )
                }
                for c in cases
            ],
            "assumptions": ASSUMPTIONS,
            "spectral_profile": pixel["metadata"],
            "source_code": job["source_code"],
            "new_raw_frames": len(cases) * 8,
            "raw_preserved": True,
            "ccm_reprocessing_reused_raw": True,
            "optical_revision": solution["result"]["revision"],
            "calibration_sha256": sha256(directory / "calibration.json"),
            "elapsed_seconds": time.monotonic() - start,
            "physical_validation": "not_established",
            "product_mode": False,
        }
        engine.report(directory, result, cases)
        write_json(directory / "result.json", result)
        files = {
            str(p.relative_to(directory)): sha256(p)
            for p in sorted(directory.rglob("*"))
            if p.is_file()
            and not p.name.startswith("._")
            and p.name not in {"manifest.json", "progress.json"}
        }
        write_json(
            directory / "manifest.json",
            {"schema": "camera_reference_workshop_files_v1", "files": files},
        )
        summary = {
            **result,
            "files_base": f"{ROUTE}/{job['id']}/files/",
            "report_url": f"{ROUTE}/{job['id']}/files/report.html",
            "bundle_url": f"{ROUTE}/{job['id']}/bundle.zip",
            "manifest_sha256": sha256(directory / "manifest.json"),
            "package_bytes": sum((directory / p).stat().st_size for p in files),
        }
        artifact = service.registry.store_blob(
            canonical(summary).encode(), "reference-ircf-study.json", "application/json"
        )
        return {"summary": summary, "artifacts": [artifact]}


def get(service, identity):
    record = workshop.get(service, identity)
    if record.get("plugin_id") != SCHEMA:
        raise ValueError("Selected run is not an IRCF study.")
    return record


def case_detail(service, identity, case_id):
    record = get(service, identity)
    if record["status"] != "succeeded" or case_id not in {
        c["id"] for c in record["result"]["summary"]["cases"]
    }:
        raise ValueError("A completed case belonging to this study is required.")
    directory = workshop._directory(service, identity)
    relative = f"cases/{case_id}/case.json"
    manifest = json.loads((directory / "manifest.json").read_text())
    if sha256(directory / relative) != manifest["files"][relative]:
        raise IntegrityError("IRCF case artifact changed.")
    return json.loads((directory / relative).read_text())


def save_profile(service, identity, payload):
    if not isinstance(payload, dict) or set(payload) != {
        "name",
        "case_id",
        "baseline_case_id",
        "ccm_a",
        "ccm_b",
        "stage",
    }:
        raise ValueError("Profile requires name, two case IDs, two CCM choices and stage.")
    if (
        not isinstance(payload["name"], str)
        or not 1 <= len(payload["name"]) <= 160
        or payload["stage"] not in {"display", "raw"}
        or any(payload[k] not in {"baseline", "refit"} for k in ("ccm_a", "ccm_b"))
    ):
        raise ValueError("Invalid IRCF comparison profile.")
    a, b = [case_detail(service, identity, payload[k]) for k in ("baseline_case_id", "case_id")]
    if any(a[k] != b[k] for k in ("field_id", "illuminant", "chart")):
        raise ValueError("Compare the same field, illuminant and chart.")
    record = get(service, identity)
    return service.registry.put(
        "reference_ircf_profiles",
        {
            "study_id": identity,
            "selection": payload,
            "manifest_sha256": record["result"]["summary"]["manifest_sha256"],
            "specification": record["ircf_study"],
            "case_sha256": digest(b),
            "baseline_case_sha256": digest(a),
        },
    )


def install_routes(app, service):
    from urllib.parse import quote

    from fastapi.responses import RedirectResponse

    @app.get(ROUTE, tags=[workshop.JOB_KIND])
    def listing():
        studies = []
        for item in service.registry.list("reference_ircf_studies"):
            row = get(service, item["run_id"])
            studies.append({k: row[k] for k in ("id", "name", "status")})
        return {"studies": studies, "template": template(), "assumptions": ASSUMPTIONS}

    @app.post(ROUTE, status_code=202, tags=[workshop.JOB_KIND])
    def create(payload: dict):
        return submit(service, payload)

    @app.get(ROUTE + "/{identity}", tags=[workshop.JOB_KIND])
    def detail(identity: str):
        return get(service, identity)

    @app.get(ROUTE + "/{identity}/cases/{case_id}", tags=[workshop.JOB_KIND])
    def case(identity: str, case_id: str):
        return case_detail(service, identity, case_id)

    @app.post(ROUTE + "/{identity}/profiles", status_code=201, tags=[workshop.JOB_KIND])
    def profile(identity: str, payload: dict):
        return save_profile(service, identity, payload)

    @app.get(ROUTE + "/{identity}/profiles/{profile_id}", tags=[workshop.JOB_KIND])
    def saved_profile(identity: str, profile_id: str):
        record = get(service, identity)
        saved = service.registry.get("reference_ircf_profiles", profile_id)
        if (
            saved["study_id"] != identity
            or saved["manifest_sha256"] != record["result"]["summary"]["manifest_sha256"]
        ):
            raise IntegrityError("Profile belongs to another study.")
        for selection_key, hash_key in (
            ("case_id", "case_sha256"),
            ("baseline_case_id", "baseline_case_sha256"),
        ):
            current = case_detail(service, identity, saved["selection"][selection_key])
            if digest(current) != saved[hash_key]:
                raise IntegrityError("Saved IRCF comparison case changed.")
        return saved

    @app.get(ROUTE + "/{identity}/files/{relative:path}", tags=[workshop.JOB_KIND])
    def file(identity: str, relative: str):
        get(service, identity)
        return RedirectResponse(f"{workshop.ROUTE}/runs/{identity}/files/{quote(relative)}")

    @app.get(ROUTE + "/{identity}/bundle.zip", tags=[workshop.JOB_KIND])
    def bundle(identity: str):
        get(service, identity)
        return RedirectResponse(f"{workshop.ROUTE}/runs/{identity}/bundle")
