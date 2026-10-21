"""Numerically checked metric surrogates derived from immutable camera studies.

Training is a durable worker job. Numerical holdout acceptance never confers
physical approval, and a finite residual is never a guaranteed uncertainty bound.
"""

from __future__ import annotations

import time

import numpy as np

from .registry import ConflictError, canonical, digest
from .studies import METRICS, PARAMETERS, metrics
from .surrogate_contracts import SurrogatePlan, SurrogatePredict, SurrogateSubmit
from .surrogate_math import METHOD, domain_and_split, evaluate, fit

LIMITATIONS = [
    "Metric-only approximation of one fixed numerical camera/test/reference engine; no RAW/RGB.",
    "Held-out coordinates are separate from this fit; not blinded physical measurements.",
    "Finite validation residuals do not bound errors throughout the continuous input domain.",
    "No physical approval, model-discrepancy bound or full uncertainty propagation is established.",
    "Changing the base camera, test, reference engine or an untrained input requires a new model.",
]


def catalog():
    return {
        "schema": "camera_metric_surrogate_v1",
        "method_and_version": METHOD,
        "metrics": METRICS,
        "parameters": PARAMETERS,
        "max_training_points": 192,
        "max_validation_points": 64,
        "max_prediction_points": 4096,
        "max_dimensions": 6,
        "domain_policy": "All training-box corners must exist; no extrapolation",
        "limitations": LIMITATIONS,
    }


def list_models(service):
    return [r for r in service.registry.list_runs() if r.get("job_kind") == "surrogate_training"]


def get_model(service, identity):
    job = service.registry.get_run(identity)
    if job.get("job_kind") != "surrogate_training":
        raise KeyError("Not a surrogate training job")
    return job


def _resolve(service, spec):
    datasets, contexts, study_hashes = {}, [], {}
    for split, limit in (("training", 192), ("validation", 64)):
        if sum(len(s["run_ids"]) for s in spec[split]) > limit:
            raise ValueError(f"Too many {split} reference points (maximum {limit})")
        rows = []
        for selection in spec[split]:
            study = service.registry.get("studies", selection["study_id"])
            study_hashes[study["id"]] = study["content_hash"]
            ss = study["specification"]
            axes = sorted(
                [{"parameter": a["parameter"], "unit": a["unit"]} for a in ss["axes"]],
                key=lambda a: a["parameter"],
            )
            candidates = dict(zip(study["run_ids"], study["candidates"], strict=True))
            for identity in selection["run_ids"]:
                if identity not in candidates:
                    raise ValueError("Selected run is not a member of its declared study")
                candidate = candidates[identity]
                run = service.registry.get_run(identity)
                if run["status"] != "succeeded":
                    raise ValueError("Surrogate references require successful completed runs")
                if (
                    run["environment"] != study["environment"]
                    or run["plugin_id"] != ss["plugin_id"]
                ):
                    raise ConflictError("Reference execution differs from its study environment")
                if (
                    run["assembly_id"] != candidate["assembly_id"]
                    or run["test_id"] != candidate["test_id"]
                ):
                    raise ConflictError("Reference execution differs from its frozen candidate")
                values = metrics(run)
                output = [values[m["metric"]] for m in spec["metrics"]]
                if any(v is None or not np.isfinite(v) for v in output):
                    raise ValueError("A selected reference output is undefined or non-finite")
                base = service.registry.get("assemblies", candidate["base_assembly_id"])
                fixed = [service.registry.get("models", i) for i in base["model_ids"]]
                context = {
                    "base_assembly_id": base["id"],
                    "base_test_id": ss["test_id"],
                    "source_hashes": {
                        r["id"]: r["content_hash"]
                        for r in [base, *fixed, service.registry.get("tests", ss["test_id"])]
                    },
                    "reference_plugin_id": ss["plugin_id"],
                    "reference_environment": study["environment"],
                    "seed": ss["seed"],
                    "frames": ss["frames"],
                    "output_size": ss["output_size"],
                    "axes": axes,
                }
                contexts.append(context)
                rows.append(
                    {
                        "study_id": study["id"],
                        "study_hash": study["content_hash"],
                        "run_id": identity,
                        "run_hash": run["content_hash"],
                        "result_hash": digest(run["result"]),
                        "coordinates": candidate["coordinates"],
                        "outputs": output,
                    }
                )
        datasets[split] = sorted(rows, key=lambda r: canonical(r["coordinates"]))
    if any(c != contexts[0] for c in contexts):
        raise ValueError(
            "All references must share base camera, test, axes and execution environment/settings"
        )
    return datasets, contexts[0], study_hashes


def _matrices(datasets, axes, split):
    rows = datasets[split]
    return (
        [[r["coordinates"][a["parameter"]] for a in axes] for r in rows],
        [r["outputs"] for r in rows],
    )


def prepare(service, payload):
    from .service import environment_fingerprint

    spec = SurrogatePlan.model_validate(payload).model_dump()
    for metric in spec["metrics"]:
        if metric["metric"] not in METRICS or metric["unit"] != METRICS[metric["metric"]]["unit"]:
            raise ValueError("Unsupported surrogate output metric or unit")
    environment = environment_fingerprint()
    if environment != service.loaded_environment:
        raise ConflictError("Executable source changed; restart before planning a surrogate")
    datasets, context, source_hashes = _resolve(service, spec)
    x, _ = _matrices(datasets, context["axes"], "training")
    v, _ = _matrices(datasets, context["axes"], "validation")
    domain = domain_and_split(x, v)
    plan = {
        "schema": "camera_surrogate_training_plan_v1",
        "specification": spec,
        "method_and_version": METHOD,
        "datasets": datasets,
        "context": context,
        "domain": domain,
        "source_hashes": source_hashes,
        "environment": environment,
        "metric_definitions": {m["metric"]: METRICS[m["metric"]] for m in spec["metrics"]},
        "limitations": LIMITATIONS,
    }
    plan["plan_hash"] = digest(plan)
    return plan


def submit(service, payload):
    data = SurrogateSubmit.model_validate(payload).model_dump()
    plan = prepare(service, data["specification"])
    if plan["plan_hash"] != data["plan_hash"]:
        raise ConflictError("Surrogate plan, source evidence or environment changed; preview again")
    job = {
        "name": plan["specification"]["name"],
        "job_kind": "surrogate_training",
        "plugin_id": "metric_surrogate_rbf_v1",
        "environment": plan["environment"],
        "plan": plan,
        "resources": {"worker_timeout_s": 120, "max_training_points": 192},
    }
    return service.registry.submit(job, digest(job))


def _physical_output_issues(values, definitions):
    issues = []
    for col, item in enumerate(definitions):
        key = item["metric"]
        if key in {"mean_photoelectrons_e", "max_relative_color_shading"} and np.any(
            values[:, col] < 0
        ):
            issues.append(key + " is negative; surrogate output is not admissible")
        if key == "exposure_ms" and np.any(values[:, col] <= 0):
            issues.append("Predicted exposure must be positive")
    return issues


def execute(service, job):
    from .service import environment_fingerprint

    if job["environment"] != environment_fingerprint():
        raise ConflictError("Worker environment differs from the frozen surrogate submission")
    plan = job["plan"]
    verified = prepare(service, plan["specification"])
    if verified != plan:
        raise ConflictError("Surrogate source evidence changed after submission")
    axes, datasets = plan["context"]["axes"], plan["datasets"]
    x, y = _matrices(datasets, axes, "training")
    v, actual = _matrices(datasets, axes, "validation")
    start = time.perf_counter()
    coefficients = fit(x, y, plan["domain"])
    fit_seconds = time.perf_counter() - start
    predicted = evaluate(coefficients, v)
    residuals = predicted - np.asarray(actual)
    training_residual = evaluate(coefficients, x) - np.asarray(y)
    definitions = plan["specification"]["metrics"]
    checks = []
    for j, metric in enumerate(definitions):
        maximum = float(np.max(np.abs(residuals[:, j])))
        checks.append(
            {
                **metric,
                "max_observed_absolute_error": maximum,
                "rmse": float(np.sqrt(np.mean(residuals[:, j] ** 2))),
                "training_max_absolute_residual": float(np.max(np.abs(training_residual[:, j]))),
                "status": "PASS" if maximum <= metric["max_absolute_error"] else "FAIL",
            }
        )
    issues = _physical_output_issues(predicted, definitions)
    accepted = all(c["status"] == "PASS" for c in checks) and not issues
    validation_ids = {r["run_id"] for r in datasets["validation"]}
    previous_uses = [
        {
            "model_id": other["id"],
            "validation_run_ids": sorted(
                validation_ids & {r["run_id"] for r in other["result"]["datasets"]["validation"]}
            ),
        }
        for other in list_models(service)
        if other.get("result")
    ]
    previous_uses = [u for u in previous_uses if u["validation_run_ids"]]
    result = {
        "schema": "camera_metric_surrogate_model_v1",
        "method_and_version": METHOD,
        "coefficients": coefficients,
        "axes": axes,
        "metrics": definitions,
        "context": plan["context"],
        "training_plan_hash": plan["plan_hash"],
        "source_hashes": plan["source_hashes"],
        "datasets": datasets,
        "source_kind": "estimated",
        "approval_status": "candidate",
        "numerical_validation": {
            "kind": "held_out_reference_coordinate_comparison",
            "status": "PASS" if accepted else "FAIL",
            "checks": checks,
            "issues": issues,
            "previous_validation_uses": previous_uses,
            "rows": [
                {
                    **row,
                    "predicted": predicted[i].tolist(),
                    "residual": residuals[i].tolist(),
                }
                for i, row in enumerate(datasets["validation"])
            ],
            "independence_scope": (
                "Disjoint coordinates within this fit; no claim of blinded or physical independence"
            ),
            "error_bound": "unknown_outside_observed_validation_points",
        },
        "prediction_eligible": accepted,
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
        "timing": {
            "fit_seconds": fit_seconds,
            "scope": "Actual coefficient fit only; excludes source loading and queue",
        },
        "limitations": LIMITATIONS,
        "artifacts": [],
    }
    return result


def predict(service, identity, payload):
    from .service import environment_fingerprint

    data = SurrogatePredict.model_validate(payload).model_dump()
    environment = environment_fingerprint()
    if environment != service.loaded_environment:
        raise ConflictError("Executable source changed; restart before surrogate prediction")
    job = get_model(service, identity)
    if job["status"] != "succeeded" or not job["result"]["prediction_eligible"]:
        raise ValueError(
            "Prediction requires a completed model that passed its numerical holdout checks"
        )
    model = job["result"]
    units = {a["parameter"]: a["unit"] for a in model["axes"]}
    if data["units"] != units or any(set(p) != set(units) for p in data["points"]):
        raise ValueError("Prediction axes and units must exactly match the trained model")
    x = [[p[a["parameter"]] for a in model["axes"]] for p in data["points"]]
    start = time.perf_counter()
    values = evaluate(model["coefficients"], x)
    evaluation_seconds = time.perf_counter() - start
    issues = _physical_output_issues(values, model["metrics"])
    if issues:
        raise ValueError("; ".join(issues))
    request = {
        "model_id": identity,
        "model_result_hash": digest(model),
        "inputs": data,
        "environment": environment,
    }
    document = {
        "schema": "camera_surrogate_prediction_v1",
        **request,
        "method_and_version": METHOD,
        "model_run_hash": job["content_hash"],
        "domain": model["coefficients"]["domain"],
        "reference_context": model["context"],
        "numerical_validation": model["numerical_validation"],
        "rows": [
            {
                "coordinates": point,
                "outputs": {
                    m["metric"]: float(values[i, j]) for j, m in enumerate(model["metrics"])
                },
            }
            for i, point in enumerate(data["points"])
        ],
        "metric_definitions": {m["metric"]: METRICS[m["metric"]] for m in model["metrics"]},
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
        "timing": {
            "evaluation_seconds": evaluation_seconds,
            "points": len(x),
            "scope": "Kernel evaluation only; excludes HTTP, evidence loading and persistence",
        },
        "limitations": LIMITATIONS,
    }
    # Every evaluation is a distinct immutable observation of runtime/timing.
    return service.registry.put("surrogate_predictions", document)
