"""New electrical captures for an adopted source-derived readout design revision.

Only preserved, pre-noise photoelectron rates are reused. Original RAW stays
immutable; the original ISP, optical response, calibration and sample grid stay
fixed. This bounded adapter supports Reference RGGB probes, not product modes.
"""

from __future__ import annotations

import copy
import html
import json
import re
import shutil
import time
from pathlib import Path

import numpy as np

from . import design_workshop_storage as storage
from . import reference_workshop_job as workshop
from .electrical import validate_electrical_model
from .registry import IntegrityError, canonical, digest
from .virtual_lab_job import sha256

SCHEMA = "camera_reference_asset_trial_v1"
FIELDS = ("center", "outer-ne")
CHARTS = ("uniform", "texture", "color", "resolution")
ESTIMATED_BYTES = 512 * 1024**2


def fingerprint():
    names = (
        "reference_asset_trial.py",
        "electrical.py",
        "design_workshop_engine.py",
        "reference_workshop_engine.py",
        "reference_isp.py",
        "reference_isp_lab.py",
        "virtual_lab_engine.py",
        "virtual_lab_metrics.py",
    )
    return {name: sha256(Path(__file__).parent / name) for name in names}


def applied_model(model, baseline):
    """The model record owns two absolute values, never an implicit scale mapping."""
    baseline = validate_electrical_model(baseline)
    if model.get("baseline_electrical_model") != baseline:
        raise IntegrityError("Adopted model baseline differs from the preserved electrical model.")
    values = model.get("applied_inputs")
    if not isinstance(values, dict) or set(values) != {"read_noise_e", "dark_current_e_per_s"}:
        raise ValueError("The trial supports explicit read noise and dark current only.")
    return validate_electrical_model({**baseline, **values})


def recapture(rate, model, configuration, seed):
    from .design_workshop_engine import _capture

    values = np.asarray(rate)
    if values.ndim != 2 or not np.isfinite(values).all() or np.any(values < 0):
        raise ValueError("Preserved photoelectron rate must be a finite nonnegative plane.")
    return _capture(values, model, configuration, seed)


def _relative(root, name):
    part = Path(name)
    if part.is_absolute() or ".." in part.parts:
        raise IntegrityError("Source artifact must stay inside its acquisition directory.")
    path = root / part
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise IntegrityError("Required source artifact is missing.")
    return path


def _model(service, identity):
    record = service.registry.get("design_asset_models", identity)
    if record.get("execution_supported") is not True:
        raise ValueError(
            "Adopted design revision is not executable by the Reference readout adapter."
        )
    from .design_asset_readiness import _model_views

    view = next(
        (value for value in _model_views(service, record["asset_id"]) if value["id"] == identity),
        None,
    )
    ready = (view or {}).get("latest_check") or {}
    if ready.get("execution_ready") is not True or ready.get("status") != "passed":
        raise ValueError(
            "Run a current successful readiness check for this exact adopted model first."
        )
    return record


def _load_source(service, model, source_run_id, source_candidate_id=None, *, verify=False):
    source = workshop.get(service, source_run_id)
    if source.get("plugin_id") != workshop.SCHEMA or source.get("asset_trial"):
        raise ValueError(
            "Select an original Reference acquisition or preserved-RAW Reference replay."
        )
    selected = workshop._source_summary(source, source_candidate_id)
    bound = model.get("source_binding", {})
    if (
        bound.get("run_id") != source_run_id
        or bound.get("candidate_id") != source_candidate_id
        or bound.get("manifest_sha256") != source["result"]["summary"]["manifest_sha256"]
    ):
        raise IntegrityError("Trial source differs from the adopted revision's exact acquisition.")
    if verify:
        workshop.verify_files(service, source)
    directory = workshop._directory(service, source_run_id)
    if source_candidate_id is not None:
        if not re.fullmatch(r"candidate-[0-9]{2}", source_candidate_id):
            raise ValueError("Choose an exact saved source candidate.")
        directory /= source_candidate_id
    cell = json.loads(_relative(directory, "pixel-cell.json").read_text())
    if cell.get("cell_sha256") != bound.get("cell_sha256"):
        raise IntegrityError("Source cell hash differs from the adopted design revision.")
    if cell.get("source_report") != bound.get("source_report"):
        raise IntegrityError("Source sensor report differs from the adopted design revision.")
    if bound.get("source_evidence_sha256") and bound["source_evidence_sha256"] != cell.get(
        "provenance", {}
    ).get("source_evidence_sha256"):
        raise IntegrityError("Reviewed sensor evidence differs from the adopted design revision.")
    capture = json.loads(_relative(directory, "capture.json").read_text())
    configuration = capture["configuration"]
    if configuration != selected["configuration"]:
        # A replay changes its displayed ISP configuration, while retaining the
        # source acquisition configuration in capture.json. Its requested ISP is
        # the one which actually produced the selected baseline result.
        if workshop.acquisition_configuration(configuration) != workshop.acquisition_configuration(
            selected["configuration"]
        ):
            raise IntegrityError(
                "Source capture and selected result describe different acquisitions."
            )
        configuration = selected["configuration"]
    if configuration["frames"] != 8:
        raise ValueError("First adopted-readout trial requires eight preserved Reference captures.")
    if sha256(_relative(directory, "calibration.json")) != capture.get("calibration_sha256"):
        raise IntegrityError("Preserved Reference calibration changed.")
    for field in FIELDS:
        saved = capture.get("fields", {}).get(field)
        if not saved or saved["geometry"]["native_shape_row_col"] != [256, 256]:
            raise ValueError("Trial requires existing center and outer-ne native 256x256 probes.")
        origin = saved["geometry"].get("cfa_origin", {})
        if (
            origin.get("pattern") != "RGGB"
            or origin.get("local_row") != 0
            or origin.get("local_col") != 0
        ):
            raise ValueError("Trial supports the preserved RGGB origin [0,0] only.")
        for chart in CHARTS:
            info = saved.get("captures", {}).get(chart)
            if not info or len(info.get("raw", [])) != 8:
                raise ValueError(
                    "Source must contain uniform, texture, color and resolution charts."
                )
            contract = info.get("optical_pixel_contract", {})
            if (
                contract.get("unit") != "electrons/s"
                or contract.get("geometry") != saved["geometry"]
            ):
                raise IntegrityError(
                    "Source response must be photoelectron rate in the exact CFA geometry."
                )
            path = _relative(directory, f"fields/{field}/{chart}/mean-electron-rate.npy")
            if sha256(path) != info["rate_sha256"]:
                raise IntegrityError("Preserved pre-noise mean photoelectron rate changed.")
            values = np.load(path, allow_pickle=False)
            if values.shape != (256, 256) or not np.isfinite(values).all() or np.any(values < 0):
                raise ValueError("Mean photoelectron rate has invalid native samples.")
    electrical = applied_model(model, cell["electrical_model"])
    return source, directory, capture, copy.deepcopy(configuration), cell, electrical


def submit(service, payload):
    allowed = {"model_revision_id", "source_run_id", "source_candidate_id", "name"}
    if (
        not isinstance(payload, dict)
        or set(payload) - allowed
        or not {"model_revision_id", "source_run_id"} <= set(payload)
    ):
        raise ValueError("Trial requires model_revision_id and its exact source_run_id.")
    name = payload.get("name", "Adopted readout design · Reference trial")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError("Trial name requires 1..160 characters.")
    model = _model(service, payload["model_revision_id"])
    source, _, _, configuration, _, electrical = _load_source(
        service,
        model,
        payload["source_run_id"],
        payload.get("source_candidate_id"),
        verify=True,
    )
    state = storage.status(service)
    if (
        state["remaining_bytes"] < ESTIMATED_BYTES
        or state["volume_free_bytes"] - ESTIMATED_BYTES < storage.RESERVE_BYTES
    ):
        raise ValueError(
            "Trial exceeds the SSD budget; no resolution or precision reduction applied."
        )
    configuration["name"] = name.strip()
    source_ref = {
        "run_id": source["id"],
        "candidate_id": payload.get("source_candidate_id"),
        "manifest_sha256": source["result"]["summary"]["manifest_sha256"],
    }
    trial = {
        "schema": SCHEMA,
        "model_revision_id": model["id"],
        "model_record_sha256": digest(model),
        "source": source_ref,
        "applied_electrical_sha256": digest(electrical),
        "fields": list(FIELDS),
        "charts": list(CHARTS),
        "frames": 8,
    }
    document = {
        "name": name.strip(),
        "job_kind": workshop.JOB_KIND,
        "plugin_id": SCHEMA,
        "asset_trial": trial,
        "design_asset_model_id": model["id"],
        "design_asset_model_content_hash": model["content_hash"],
        "specification": configuration,
        "source": source_ref,
        "reference_snapshot": source.get("reference_snapshot"),
        "source_code": fingerprint(),
        "storage_root": state["path"],
        "estimated_bytes": ESTIMATED_BYTES,
        "default_worker_timeout_seconds": 1800,
    }
    job = service.registry.submit(document, digest(document))
    link = {
        "model_revision_id": model["id"],
        "model_content_hash": model["content_hash"],
        "run_id": job["id"],
        "source_run_id": source["id"],
        "source": source_ref,
    }
    link_id = "design_asset_trial_link-" + digest(link)[:24]
    try:
        service.registry.get("design_asset_trial_links", link_id)
    except KeyError:
        service.registry.put("design_asset_trial_links", link, identity=link_id)
    return job


def get(service, identity):
    record = workshop.get(service, identity)
    if record.get("plugin_id") != SCHEMA:
        raise ValueError("Selected run is not an adopted design trial.")
    return record


def _updated_cell(original, electrical, model):
    cell = copy.deepcopy(original)
    cell.pop("cell_sha256", None)
    changes = {key: electrical[key] for key in ("read_noise_e", "dark_current_e_per_s")}
    cell["electrical_model"] = electrical
    cell["readout"].update(changes)
    if "physical_model" in cell:
        cell["physical_model"]["readout"].update(changes)
    cell["provenance"]["design_asset_trial"] = {
        "model_revision_id": model["id"],
        "model_content_hash": model["content_hash"],
        "source_cell_sha256": original["cell_sha256"],
        "applied_inputs": changes,
        "optical_response_unchanged": True,
    }
    cell["field_classification"]["user_overrides"]["electrical_readout"] = {
        "baseline": original["electrical_model"],
        "value": electrical,
        "origin_kind": "user_assumption",
    }
    cell["cell_sha256"] = digest(cell)
    return cell


def _copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if sha256(source) != sha256(target):
        raise IntegrityError("Source copy failed its byte identity check.")


def _noise(raw, readout, configuration):
    stack = np.asarray(raw, dtype=float)
    dn_per_e = (
        (2 ** readout["bit_depth"] - 1 - readout["black_level_dn"])
        * configuration["gain"]
        / readout["full_well_e"]
    )
    variance = np.var(stack, axis=0, ddof=1)
    return {
        "temporal_noise_dn": float(np.sqrt(variance.mean())),
        "temporal_noise_e": float(np.sqrt(variance.mean()) / dn_per_e),
        "mean_dn": float(stack.mean()),
        "repeat_count": int(stack.shape[0]),
        "definition": "sqrt(mean(per-pixel temporal sample variance)); not spatial texture",
    }


def calculate(
    source_dir, directory, capture, configuration, original_cell, electrical, model, progress
):
    """Bounded filesystem adapter; original source files are read only."""
    from . import reference_workshop_engine as engine

    start = time.monotonic()
    directory.mkdir(parents=True, exist_ok=True)
    cell = _updated_cell(original_cell, electrical, model)
    calibration = json.loads((source_dir / "calibration.json").read_text())
    color_id = configuration["isp"].get("color_calibration_id")
    if color_id:
        path = _relative(source_dir, "color-calibrations/" + color_id + ".json")
        calibration["color"] = json.loads(path.read_text())
        _copy(path, directory / "color-calibrations" / path.name)
    optical = json.loads((source_dir / "optical-response.json").read_text())
    optical["fields"] = [f for f in optical["fields"] if f["id"] in FIELDS]
    if len(optical["fields"]) != 2 or any(not engine._success(f) for f in optical["fields"]):
        raise ValueError("Both selected optical fields must have saved successful results.")
    response = json.loads((source_dir / "pixel-response.json").read_text())
    for field in optical["fields"]:
        for filename in field.get("images", {}).values():
            _copy(_relative(source_dir / "optics", filename), directory / "optics" / filename)
    for name, value in (
        ("pixel-cell.json", cell),
        ("pixel-response.json", response),
        ("optical-response.json", optical),
        ("calibration.json", calibration),
        ("applied-electrical-model.json", electrical),
        ("adopted-model.json", model),
    ):
        engine.write_json(directory / name, value)
    outputs, captures, comparisons = [], {}, []
    for field in optical["fields"]:
        identity = field["id"]
        saved = capture["fields"][identity]
        current = copy.deepcopy(saved)
        current["configuration"] = configuration
        current["readout"].update(
            {k: electrical[k] for k in ("read_noise_e", "dark_current_e_per_s")}
        )
        current["captures"] = {}
        for chart in CHARTS:
            progress(f"{identity} · {chart} · new electrical RAW")
            original = source_dir / "fields" / identity / chart
            folder = directory / "fields" / identity / chart
            info = copy.deepcopy(saved["captures"][chart])
            for filename in ("mean-electron-rate.npy", "source-reference-linear.npy"):
                _copy(original / filename, folder / filename)
            rate = np.load(folder / "mean-electron-rate.npy", allow_pickle=False)
            frames, raw_values, baseline_values = [], [], []
            for number, previous in enumerate(info["raw"]):
                generated = recapture(rate, electrical, configuration, previous["seed"])
                name = f"raw-{number:02d}.npy"
                np.save(folder / name, generated["arrays"]["raw_dn"])
                raw_values.append(generated["arrays"]["raw_dn"])
                baseline_raw = np.load(_relative(original, previous["file"]), allow_pickle=False)
                reproduced = recapture(
                    rate, original_cell["electrical_model"], configuration, previous["seed"]
                )["arrays"]["raw_dn"]
                if not np.array_equal(baseline_raw, reproduced):
                    raise IntegrityError(
                        "Current readout does not reproduce the preserved baseline; "
                        "review this source before comparing a design change."
                    )
                baseline_values.append(baseline_raw)
                frames.append(
                    {"file": name, "sha256": sha256(folder / name), "seed": previous["seed"]}
                )
                if number == 0:
                    engine.write_json(folder / "capture.json", generated["summary"])
            info["source_raw_hashes"] = [v["sha256"] for v in info["raw"]]
            info["raw"] = frames
            info["response_cache_reused"] = True
            info["rate_reused_unchanged"] = True
            info["applied_electrical_sha256"] = digest(electrical)
            current["captures"][chart] = info
            comparisons.append(
                {
                    "field_id": identity,
                    "chart": chart,
                    "source_rate_sha256": info["rate_sha256"],
                    "trial_rate_sha256": sha256(folder / "mean-electron-rate.npy"),
                    "cfa_geometry_sha256": digest(saved["geometry"]),
                    "baseline": _noise(baseline_values, saved["readout"], configuration),
                    "trial": _noise(raw_values, current["readout"], configuration),
                    "raw_identical": all(
                        np.array_equal(a, b)
                        for a, b in zip(raw_values, baseline_values, strict=True)
                    ),
                }
            )
        engine.write_json(directory / "fields" / identity / "capture.json", current)
        captures[identity] = current
        progress(f"{identity} · fixed ISP / temporal noise / color / SFR")
        outputs.append(
            engine._render_field(
                directory / "fields" / identity,
                current,
                configuration,
                field,
                response,
                cell,
                calibration,
            )
        )
    engine.write_json(
        directory / "capture.json",
        {
            "configuration": configuration,
            "fields": captures,
            "calibration_sha256": sha256(directory / "calibration.json"),
        },
    )
    result = engine._summary(
        directory, configuration, {"result": optical}, cell, calibration, outputs
    )
    result["design_asset_model_id"] = model["id"]
    result["design_asset_model_content_hash"] = model["content_hash"]
    result["asset_trial"] = {
        "schema": SCHEMA,
        "new_raw_frames": len(FIELDS) * len(CHARTS) * 8,
        "source_cell_sha256": original_cell["cell_sha256"],
        "applied_cell_sha256": cell["cell_sha256"],
        "applied_electrical_model": electrical,
        "applied_electrical_sha256": digest(electrical),
        "applied_inputs": model["applied_inputs"],
        "source_binding": model["source_binding"],
        "comparisons": comparisons,
        "original_raw_unchanged": True,
        "baseline_seeded_readout_reproduced_exactly": True,
        "mean_photoelectron_rates_reused": True,
        "independent_readouts": True,
        "paired_seed_policy": "same source seed for each repeat",
        "calibration_policy": "source ISP, spectral CCM and native LSC kept fixed",
        "elapsed_seconds": time.monotonic() - start,
        "scope": (
            "user-assumed readout variation on a reviewed structural proxy; not product validation"
        ),
    }
    return result


def execute(service, job):
    from . import reference_workshop_engine as engine

    if (
        service.registry.get_run(job["id"])["status"] != "running"
        or job["source_code"] != fingerprint()
    ):
        raise IntegrityError("Claimed trial and unchanged implementation required.")
    model = _model(service, job["design_asset_model_id"])
    if digest(model) != job["asset_trial"]["model_record_sha256"]:
        raise IntegrityError("Adopted model record changed after submission.")
    source, source_dir, capture, configuration, cell, electrical = _load_source(
        service,
        model,
        job["source"]["run_id"],
        job["source"].get("candidate_id"),
        verify=True,
    )
    if digest(electrical) != job["asset_trial"]["applied_electrical_sha256"]:
        raise IntegrityError("Applied readout inputs changed after submission.")
    if job["storage_root"] != str(storage.root(service)):
        raise IntegrityError("Trial storage root changed after submission.")
    configuration["name"] = job["name"]
    with storage.reserve(service, job["estimated_bytes"]) as (_, check):
        directory = workshop._directory(service, job["id"])
        directory.mkdir(parents=True, exist_ok=True)

        def progress(stage):
            if service.registry.get_run(job["id"])["status"] != "running":
                raise RuntimeError("Adopted design trial cancelled.")
            check()
            engine.write_json(directory / "progress.json", {"stage": stage})

        result = calculate(
            source_dir, directory, capture, configuration, cell, electrical, model, progress
        )
        workshop.verify_files(service, source)
        progress("Verifying adopted revision before publication")
        if (
            digest(_model(service, job["design_asset_model_id"]))
            != job["asset_trial"]["model_record_sha256"]
            or job["source_code"] != fingerprint()
        ):
            raise IntegrityError("Adopted model readiness or implementation changed during trial.")
        result.update(id=job["id"], status="succeeded", source_run_id=source["id"])
        engine.write_json(directory / "result.json", result)
        engine.report(result, directory)
        report_path = directory / "report.html"
        report = report_path.read_text()
        receipt = (
            "<h2>Adopted readout trial · actual new RAW</h2><pre>"
            + html.escape(json.dumps(result["asset_trial"], ensure_ascii=False, indent=2))
            + "</pre>"
        )
        report_path.write_text(report.replace("</html>", receipt + "</html>"))
        files = {
            str(p.relative_to(directory)): sha256(p)
            for p in sorted(directory.rglob("*"))
            if p.is_file()
            and p.name not in {"manifest.json", "progress.json"}
            and not p.name.startswith("._")
        }
        engine.write_json(
            directory / "manifest.json",
            {"schema": "camera_reference_workshop_files_v1", "files": files},
        )
        summary = {
            **result,
            "files_base": f"{workshop.ROUTE}/runs/{job['id']}/files/",
            "report_url": f"{workshop.ROUTE}/runs/{job['id']}/files/report.html",
            "manifest_sha256": sha256(directory / "manifest.json"),
            "result_url": (
                f"/reference-workshop.html?run={job['id']}"
                f"&baseline={source['id']}&field=outer-ne&chart=uniform"
            ),
            "manifest_url": f"{workshop.ROUTE}/runs/{job['id']}/files/manifest.json",
            "bundle_url": f"{workshop.ROUTE}/runs/{job['id']}/bundle",
            "package_bytes": sum((directory / p).stat().st_size for p in files),
            "physical_validation": "not_established",
        }
        artifact = service.registry.store_blob(
            canonical(summary).encode(), "reference-asset-trial.json", "application/json"
        )
        return {"summary": summary, "artifacts": [artifact]}
