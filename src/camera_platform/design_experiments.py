"""Explicit, isolated design experiments using existing prescription/RAW engines.

One user submission freezes inputs, executes a finite grid, freezes its choice,
then evaluates it on separate inputs. No model publication or primary upgrade.
All earlier studies remain read-only. Evaluation recipes are declared research
inputs, not a claim of a previously unseen dataset or physical validation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone

import numpy as np

from . import isp
from .constrained_selection import SelectionLimits, select_candidate, summarize_output, violations
from .project_mode import require_design_experiments
from .r1_design_assets import catalog as source_catalog
from .r1_design_assets import load_sources
from .registry import IntegrityError, canonical, digest
from .response_chain import evaluate_response

SCHEMA = "camera_design_experiment_v2"
JOB_KIND = "design_experiment"
DEFAULT_MODELS = ("r1-stop-clear-v1", "r1-stop-quarter-v1", "r1-stop-radial-v1")
DEFAULT_SCENE = "r1-flat-v1"
MAX_CAPTURES = 128
LIMITS_NOTE = (
    "Registered, hash-pinned synthetic prescription examples; independent-reset static capture, not a real sensor. "
    "Green signal/SNR and clipping do not score motion, RAW color fidelity or task accuracy. "
    "Declared evaluation inputs are excluded from this selection but are not a blind dataset. "
    "R1 engineering simulation; physical prediction accuracy and hardware equivalence are not established. "
    "No model validity expansion, physical approval, HDR or temporal filtering."
)


def default_spec(catalog=None):
    models = [item["id"] for item in catalog["models"][:3]] if catalog else list(DEFAULT_MODELS)
    scene = catalog["scenes"][0]["id"] if catalog else DEFAULT_SCENE
    return {"schema": SCHEMA, "name": "새 처방·노출·gain 실험",
            "model_ids": models, "scene_id": scene,
            "exposures_ms": [1.0, 2.0, 4.0], "gains": [1.0, 2.0],
            "training_scales": [0.5, 2.0, 6.0], "evaluation_scales": [0.85, 3.5],
            "evaluation_noise_repeats": 2, "seed": 2601,
            "limits": asdict(SelectionLimits()), "acknowledge_simulation_limits": False}


def _numbers(values, name, low, high, count):
    if not isinstance(values, list) or not 1 <= len(values) <= count:
        raise ValueError(f"{name}: require 1..{count} explicit values")
    if any(type(v) not in (float, int) or not math.isfinite(v) or not low <= v <= high for v in values):
        raise ValueError(f"{name}: finite values in [{low}, {high}] required")
    if len(set(values)) != len(values):
        raise ValueError(name + ": duplicate values")
    return sorted(float(v) for v in values)


def specification(payload):
    if not isinstance(payload, dict) or set(payload) != set(default_spec()):
        raise ValueError("Declare exactly the experiment fields")
    if payload["schema"] != SCHEMA or payload["acknowledge_simulation_limits"] is not True:
        raise ValueError("Explicit R1 simulation-limit acknowledgement required")
    if not isinstance(payload["name"], str) or not 1 <= len(payload["name"].strip()) <= 160:
        raise ValueError("Experiment name requires 1..160 characters")
    model_ids = payload["model_ids"]
    if (not isinstance(model_ids, list) or not 1 <= len(model_ids) <= 3
            or any(not isinstance(item, str) or not 3 <= len(item) <= 80
                   or not item[0].islower() or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in item)
                   for item in model_ids)
            or len(set(model_ids)) != len(model_ids)):
        raise ValueError("Select one to three unique registered model IDs")
    if (not isinstance(payload["scene_id"], str) or not 3 <= len(payload["scene_id"]) <= 80
            or payload["scene_id"][0] not in "abcdefghijklmnopqrstuvwxyz"
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in payload["scene_id"])):
        raise ValueError("Select one registered scene ID")
    limits = payload["limits"]
    if not isinstance(limits, dict) or set(limits) != set(asdict(SelectionLimits())):
        raise ValueError("Declare all five selection limits")
    limits = SelectionLimits(**limits)
    if limits.maximum_exposure_ms > 20 or limits.minimum_green_dn > 4031:
        raise ValueError("Research bounds: exposure cap <=20 ms, green minimum <=4031 DN")
    result = deepcopy(payload)
    result.update(name=payload["name"].strip(), model_ids=sorted(model_ids), limits=asdict(limits))
    for key, low, high, count in (("exposures_ms", .05, 20, 4), ("gains", .25, 8, 4),
                                  ("training_scales", .05, 30, 4), ("evaluation_scales", .05, 30, 3)):
        result[key] = _numbers(payload[key], key, low, high, count)
    if type(payload["seed"]) is not int or not 0 <= payload["seed"] <= 2**32 - 10:
        raise ValueError("Seed must be a bounded integer")
    if type(payload["evaluation_noise_repeats"]) is not int or not 0 <= payload["evaluation_noise_repeats"] <= 4:
        raise ValueError("Evaluation noise repeats must be 0..4")
    if len(candidate_grid(result)) > 36 or capture_bound(result) > MAX_CAPTURES:
        raise ValueError("Experiment exceeds 36 candidates or 128 captures; reduce the grid")
    return result


def candidate_grid(spec):
    # Default ':g' rounds to six significant digits, merging distinct editable
    # settings. Keep familiar integer IDs and round-trip all fractional values.
    def setting(value):
        value = float(value)
        return str(int(value)) if value.is_integer() else repr(value)

    return [{"candidate_id": f"{model_id}-e{setting(exposure)}-g{setting(gain)}", "model_id": model_id,
             "scene_id": spec["scene_id"], "exposure_ms": exposure, "gain": gain}
            for model_id in spec["model_ids"] for exposure in spec["exposures_ms"] for gain in spec["gains"]]


def capture_bound(spec):
    count = sum(c["exposure_ms"] <= spec["limits"]["maximum_exposure_ms"] for c in candidate_grid(spec))
    return count * len(spec["training_scales"]) + (len(spec["evaluation_scales"]) *
            (1 + spec["evaluation_noise_repeats"]) if count else 0)


def development_only(service):
    return require_design_experiments(service)


def r1_project_only(service):
    mode = development_only(service)
    if mode.get("mode") != "r1_engineering":
        raise ValueError("New R1 experiments require an explicit R1 project mode")
    return mode


def prepare(service, payload):
    from .imaging import _current
    r1_project_only(service)
    spec = specification(payload)
    sources = load_sources(service.workspace, spec["model_ids"], spec["scene_id"])
    profile = isp.default_profile()
    return {"name": spec["name"], "job_kind": JOB_KIND, "plugin_id": SCHEMA,
            "specification": spec, "source_identities": {s: c.identity() for s, c in sources.items()},
            "isp_profile": profile, "isp_profile_sha256": digest(profile),
            "environment": _current(service), "maximum_captures": capture_bound(spec),
            "limitation": LIMITS_NOTE}


def submit(service, payload):
    frozen = prepare(service, payload)
    # Repeated identical submission reuses the registry's existing job. A changed
    # name or specification creates a distinct experiment, never overwrites one.
    return service.registry.submit(frozen, digest(frozen))


def input_request(case, scale, *, evaluation_index=None):
    if hasattr(case, "radiance_request"):
        # R1's scene is radiance. Transform it before the model applies E/L.
        request = case.radiance_request(scale, evaluation_index)
    else:
        # Historical authored test doubles supply an ideal-plane irradiance.
        request = case.rate_request()
        image = np.array(request["scene"]["spectral_irradiance"], dtype=float, copy=True)
        if evaluation_index is not None:
            if evaluation_index % 2 == 0:
                image = image[::-1].copy()
            else:
                image = image[:, ::-1].copy() * np.linspace(1.15, .85, image.shape[-1])
        request["scene"]["spectral_irradiance"] = image * scale
    request["scene"]["source"] = {**request["scene"]["source"], "kind": "estimated", "description": (
        f"User-declared R1 design experiment; registered scene radiance x{scale:g}; evaluation={evaluation_index}. "
        "Per-band on-axis E/L already included once; not new 3-D rendering.")}
    return request


class ExperimentFailure(RuntimeError):
    def __init__(self, message, result):
        super().__init__(message)
        self.evidence_result = result


def execute(service, job):
    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(k)) != canonical(v) for k, v in fresh.items()):
        raise IntegrityError("Frozen experiment source, specification or executable changed")
    spec = fresh["specification"]
    sources = load_sources(service.workspace, spec["model_ids"], spec["scene_id"])
    limits = SelectionLimits(**spec["limits"])
    grid = candidate_grid(spec)
    artifacts, rows = [], []
    summary = {"schema": SCHEMA, "specification": spec, "source_identities": fresh["source_identities"],
               "rows": rows, "selection": None, "evaluation": None, "new_captures": 0,
               "new_factory_jobs": 0, "physical_validation": "not_established",
               "automatic_approval": False, "limitation": LIMITS_NOTE,
               "selection_data_role": "development_inputs_not_machine_learning",
               "noise_basis": "Predicted SNR; evaluation repeats are finite synthetic samples"}

    def store_json(name, obj):
        artifact = service.registry.store_blob(canonical(obj).encode(), name, "application/json")
        artifacts.append(artifact)
        return artifact

    store_json("experiment-plan.json", {k: v for k, v in fresh.items() if k != "environment"})

    def acquire(candidate, scale, scenario, split, *, evaluation_index=None, noise=False, seed=0):
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Experiment cancelled or no longer running")
        case = sources[candidate["model_id"]]
        request = input_request(case, scale, evaluation_index=evaluation_index)
        output = evaluate_response(request, case.electrical_model,
            exposure_s=candidate["exposure_ms"] / 1000, analog_gain=candidate["gain"],
            noise_enabled=noise, seed=seed, isp_profile=fresh["isp_profile"])
        if output["summary"]["isp"]["profile_sha256"] != fresh["isp_profile_sha256"]:
            raise IntegrityError("Fixed R1 ISP profile differs from the submitted input")
        summary["new_captures"] += 1
        row = {**candidate, "scene_id": spec["scene_id"], **summarize_output(output), "scenario_id": scenario,
               "split": split, "radiance_scale": scale, "noise_enabled": noise, "seed": seed,
               "case_id": f"case-{len(rows):04d}", "evaluation_recipe_index": evaluation_index,
               "response_identity": output["summary"]["response_identity"]}
        row["violations"] = violations(row, limits)
        arrays = output["arrays"]
        # Keep original code bytes separate from diagnostic charge/mask arrays.
        stream = io.BytesIO(); np.save(stream, arrays["raw_dn"], allow_pickle=False)
        raw = service.registry.store_blob(stream.getvalue(), row["case_id"] + "-raw.npy", "application/octet-stream")
        raw.update(shape=list(arrays["raw_dn"].shape), dtype=str(arrays["raw_dn"].dtype), unit="DN")
        artifacts.append(raw)
        if (output["summary"]["isp"]["input_array_bytes_sha256"]
                != hashlib.sha256(arrays["raw_dn"].tobytes(order="C")).hexdigest()):
            raise IntegrityError("ISP did not consume the acquired integer RAW")
        stream = io.BytesIO()
        np.save(stream, arrays["rgb"], allow_pickle=False)
        rgb = service.registry.store_blob(stream.getvalue(), row["case_id"] + "-rgb.npy",
                                          "application/octet-stream")
        rgb.update(shape=list(arrays["rgb"].shape), dtype=str(arrays["rgb"].dtype), unit="1")
        artifacts.append(rgb)
        from PIL import Image

        pixels = (np.clip(arrays["rgb"], 0, 1) * 255).astype(np.uint8)
        stream = io.BytesIO()
        Image.fromarray(pixels, mode="RGB").save(stream, format="PNG")
        preview = service.registry.store_blob(stream.getvalue(), row["case_id"] + "-sensor-rgb.png",
                                              "image/png")
        artifacts.append(preview)
        stream = io.BytesIO()
        np.savez_compressed(stream, **{k: arrays[k] for k in (
            "mean_photocharge_e", "mean_total_charge_e", "well_saturated_mask", "adc_high_mask", "cfa_channel_index")})
        diagnostics = service.registry.store_blob(stream.getvalue(), row["case_id"] + "-diagnostics.npz", "application/octet-stream")
        artifacts.append(diagnostics)
        row.update(raw_artifact=raw, rgb_artifact=rgb, rgb_preview_artifact=preview,
                   isp_profile_sha256=output["summary"]["isp"]["profile_sha256"],
                   isp_input_raw_sha256=output["summary"]["isp"]["input_array_bytes_sha256"],
                   rgb_meaning=output["summary"]["isp"]["rgb_meaning"],
                   diagnostics_artifact=diagnostics,
                   raw_array_sha256=hashlib.sha256(arrays["raw_dn"].tobytes()).hexdigest(),
                   raw_format=deepcopy(output["summary"]["isp"]["raw_format"]),
                   geometry=deepcopy(output["summary"]["isp"]["geometry"]))
        rows.append(row)
        return row

    try:
        training_ids = [f"train-{i}-L{s:g}" for i, s in enumerate(spec["training_scales"])]
        for candidate in grid:
            if candidate["exposure_ms"] > limits.maximum_exposure_ms:
                continue
            for scenario, scale in zip(training_ids, spec["training_scales"], strict=True):
                acquire(candidate, scale, scenario, "train", seed=spec["seed"])
        selection = select_candidate(grid, rows, training_scenario_ids=training_ids, limits=limits)
        frozen = {"decision": selection, "specification_sha256": digest(spec),
                  "frozen_at_utc": datetime.now(timezone.utc).isoformat(), "evaluation_seen": False}
        # Persist this BEFORE constructing/evaluating any evaluation input.
        chosen = store_json("selection-before-evaluation.json", frozen)
        summary.update(selection=selection, selection_artifact=chosen)
        winner = next((c for c in grid if c["candidate_id"] == selection["selected_candidate_id"]), None)
        if winner is not None:
            for i, scale in enumerate(spec["evaluation_scales"]):
                scenario = f"evaluation-{i}-L{scale:g}"
                acquire(winner, scale, scenario, "evaluation", evaluation_index=i, seed=spec["seed"]+1)
                for n in range(spec["evaluation_noise_repeats"]):
                    acquire(winner, scale, scenario, "evaluation", evaluation_index=i,
                            noise=True, seed=spec["seed"]+2+n)
        assessed = [r for r in rows if r["split"] == "evaluation"]
        summary["evaluation"] = {"count": len(assessed), "passed": sum(not r["violations"] for r in assessed),
            "reselection": False, "skipped_reason": "no_feasible_candidate" if winner is None else None,
            "status": "not_run" if not assessed else "passed_samples" if all(not r["violations"] for r in assessed) else "failed_samples",
            "scope": "Declared separate inputs; neither continuous-domain guarantee nor blind physical holdout"}
        for case in sources.values():
            case.assert_unchanged()
        if hashlib.sha256(service.registry.blob_path(chosen["sha256"]).read_bytes()).hexdigest() != chosen["sha256"]:
            raise IntegrityError("Selection changed during evaluation")
        summary.update(status="completed", source_unchanged=True, selection_frozen_before_evaluation=True)
        # Final publication belongs to the same failure boundary as acquisition.
        # A failed CSV/result write must retain already stored RAW and selection
        # in the Worker's evidence_result, rather than encourage reacquisition.
        fields = ["case_id", "candidate_id", "split", "scenario_id", "exposure_ms", "gain",
                  "noise_enabled", "seed", "green_dn", "green_snr_p10", "well_fraction", "white_fraction", "violations"]
        text = io.StringIO(); writer = csv.DictWriter(text, fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
        artifacts.append(service.registry.store_blob(text.getvalue().encode("utf-8-sig"), "metrics.csv", "text/csv"))
        store_json("experiment-result.json", summary)
    except Exception as error:
        summary.update(status="failed", error=f"{type(error).__name__}: {error}")
        try:
            store_json("partial-experiment.json", summary)
        except Exception as store_error:
            # The database publication may remain available when blob storage
            # fails. Keep the original cause and existing artifact references.
            summary["partial_evidence_store_error"] = f"{type(store_error).__name__}: {store_error}"
        raise ExperimentFailure(str(error), {"summary": summary, "artifacts": artifacts}) from error
    return {"summary": summary, "artifacts": artifacts}


def get(service, identity):
    development_only(service)
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a design experiment")
    result = (job.get("result") or {}).get("summary")
    if result is not None:
        if (canonical(result["specification"]) != canonical(job["specification"])
                or canonical(result["source_identities"]) != canonical(job["source_identities"])):
            raise IntegrityError("Saved result no longer matches its frozen input")
        if job.get("status") == "succeeded":
            entries = [a for a in job.get("artifacts", []) if a["name"] == "experiment-result.json"]
            if len(entries) != 1:
                raise IntegrityError("Completed experiment lacks its saved result")
            content = service.registry.blob_path(entries[0]["sha256"]).read_bytes()
            if (hashlib.sha256(content).hexdigest() != entries[0]["sha256"]
                    or content != canonical(result).encode()):
                raise IntegrityError("Saved experiment summary differs from its result artifact")
    return job


def list_records(service):
    development_only(service)
    return [{"id": r["id"], "name": r["name"], "status": r["status"],
             "created_at": r["created_at"], "error": r.get("error"),
             "choice": ((r.get("result") or {}).get("summary", {}).get("selection") or {}).get("selected_candidate_id")}
            for r in service.registry.list_runs() if r.get("job_kind") == JOB_KIND]


def install_routes(app, service):
    from fastapi.responses import Response

    @app.get("/api/v1/design-experiments/template", tags=["design_experiments"])
    def template():
        mode = r1_project_only(service)
        assets = source_catalog(service.workspace)
        return {"specification": default_spec(assets), "catalog": assets,
                "project_mode": mode, "maximum_captures": MAX_CAPTURES,
                "limitation": LIMITS_NOTE}

    @app.post("/api/v1/design-experiments", status_code=202, tags=["design_experiments"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/design-experiments", tags=["design_experiments"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/design-experiments/{identity}", tags=["design_experiments"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/design-experiments/{identity}/cancel", tags=["design_experiments"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/design-experiments/{identity}/export", tags=["design_experiments"])
    def export(identity: str):
        return Response(canonical(get(service, identity)), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="design-experiment.json"'})

    @app.get("/api/v1/design-experiments/{identity}/cases/{case_id}", tags=["design_experiments"])
    def case_detail(identity: str, case_id: str):
        import base64

        from PIL import Image
        job = get(service, identity)
        rows = (job.get("result") or {}).get("summary", {}).get("rows", [])
        row = next((r for r in rows if r["case_id"] == case_id), None)
        if row is None:
            raise KeyError("Unknown experiment case")
        path = service.registry.blob_path(row["raw_artifact"]["sha256"])
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != row["raw_artifact"]["sha256"]:
            raise IntegrityError("Saved RAW bytes differ")
        raw = np.load(io.BytesIO(content), allow_pickle=False)
        fmt = row["raw_format"]
        pixels = np.rint(np.clip((raw.astype(float)-fmt["black_level_dn"])/
                      (fmt["white_level_dn"]-fmt["black_level_dn"]), 0, 1)*255).astype(np.uint8)
        images = {}
        for key, image in (("raw", pixels), ("final_code_rail", (raw == fmt["white_level_dn"]).astype(np.uint8)*255)):
            buffer = io.BytesIO(); Image.fromarray(image).save(buffer, format="PNG")
            images[key] = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        if job["specification"]["schema"] == SCHEMA:
            if not all(key in row for key in ("rgb_artifact", "rgb_preview_artifact",
                                                "isp_profile_sha256", "isp_input_raw_sha256")):
                raise IntegrityError("R1 case lacks its original ISP output")
            rgb_ref, preview_ref = row["rgb_artifact"], row["rgb_preview_artifact"]
            rgb_content = service.registry.blob_path(rgb_ref["sha256"]).read_bytes()
            preview_content = service.registry.blob_path(preview_ref["sha256"]).read_bytes()
            if (hashlib.sha256(rgb_content).hexdigest() != rgb_ref["sha256"]
                    or hashlib.sha256(preview_content).hexdigest() != preview_ref["sha256"]
                    or row["isp_input_raw_sha256"] != hashlib.sha256(raw.tobytes(order="C")).hexdigest()):
                raise IntegrityError("Saved ISP output differs from acquired RAW or artifact hashes")
            rgb_array = np.load(io.BytesIO(rgb_content), allow_pickle=False)
            if (rgb_array.shape != (*raw.shape, 3) or rgb_array.dtype != np.float64
                    or not np.isfinite(rgb_array).all() or np.any((rgb_array < 0) | (rgb_array > 1))):
                raise IntegrityError("Saved RGB has an invalid shape or range")
            expected_pixels = (rgb_array * 255).astype(np.uint8)
            with Image.open(io.BytesIO(preview_content)) as saved_preview:
                preview_pixels = np.asarray(saved_preview.convert("RGB"))
            if not np.array_equal(preview_pixels, expected_pixels):
                raise IntegrityError("Saved RGB preview differs from the original float RGB")
            images["rgb"] = "data:image/png;base64," + base64.b64encode(preview_content).decode()
        return {"row": row, "images": images, "display": "same declared black-to-white range; no autoscale"}

    from .design_isp_replay import install_routes as install_isp_replay_routes

    install_isp_replay_routes(app, service)
