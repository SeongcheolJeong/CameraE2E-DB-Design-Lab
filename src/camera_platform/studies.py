"""Immutable parameter variants, whole-plan preflight and evidence-based comparison.

These are finite design experiments over declared response models. They do not
create measured characteristics, a validated surrogate or a global optimum.
"""

from __future__ import annotations

import copy
import itertools
import math

import numpy as np

from .contracts import AssemblyCreate, ModelCreate, TestCreate
from .registry import ConflictError, canonical, digest, now
from .study_contracts import StudyPlan, StudySubmit

PARAMETERS = {
    "exposure_s": {
        "unit": "s",
        "label": "노출 시간",
        "owner": "test",
        "path": "conditions.exposure_s.value",
        "operation": "set",
    },
    "irradiance_scale": {
        "unit": "1",
        "label": "센서면 분광 광량 배율",
        "owner": "test",
        "path": "stimulus.spectral_irradiance_w_m2_nm",
        "operation": "scale",
    },
    "qe_scale": {
        "unit": "1",
        "label": "유효 QE 배율",
        "owner": "sensor",
        "path": "qe",
        "operation": "scale",
    },
    "read_noise_e": {
        "unit": "electron",
        "label": "Read noise",
        "owner": "sensor",
        "path": "readout.read_noise_e",
        "operation": "set",
    },
    "dark_current_e_per_s": {
        "unit": "electron/s",
        "label": "Dark current",
        "owner": "sensor",
        "path": "readout.dark_current_e_per_s",
        "operation": "set",
    },
    "full_well_e": {
        "unit": "electron",
        "label": "Full well",
        "owner": "sensor",
        "path": "readout.full_well_e",
        "operation": "set",
    },
    "gain": {
        "unit": "1",
        "label": "Readout gain",
        "owner": "sensor",
        "path": "readout.gain",
        "operation": "set",
    },
}

METRICS = {
    "min_theoretical_snr_db": {
        "unit": "dB",
        "label": "최저 이론 SNR",
        "definition": "Min unsaturated theoretical SNR; unknown if any sample is undefined.",
    },
    "min_saturation_margin_e": {
        "unit": "electron/pixel",
        "label": "최소 포화 여유",
        "definition": "Minimum readout capacity minus expected photoelectrons and dark electrons.",
    },
    "mean_photoelectrons_e": {
        "unit": "electron/pixel",
        "label": "평균 기대 광전자",
        "definition": "Arithmetic mean over modeled field/channel samples; not area-weighted.",
    },
    "max_relative_color_shading": {
        "unit": "1",
        "label": "최대 상대 색 비율 변화",
        "definition": "Max R/G and B/G deviation from on-axis ratios; not color accuracy.",
    },
    "exposure_ms": {
        "unit": "ms",
        "label": "노출 시간",
        "definition": "Declared test exposure in milliseconds; no motion or frame-rate model.",
    },
}


def catalog():
    return {
        "schema": "camera_parameter_study_v1",
        "parameters": PARAMETERS,
        "metrics": METRICS,
        "max_runs": 24,
        "runtimes": ["quick_estimate_v1", "response_db_v1"],
    }


def _fields(record, contract):
    return {
        key: copy.deepcopy(value) for key, value in record.items() if key in contract.model_fields
    }


def _get(record, path):
    for key in path.split("."):
        record = record[key]
    return record


def _set(record, path, value):
    keys = path.split(".")
    for key in keys[:-1]:
        record = record[key]
    record[keys[-1]] = value


class _DraftRegistry:
    """Read-through staging adapter. Validation creates no files, jobs or records."""

    def __init__(self, actual):
        self.actual, self.records = actual, []
        self.directory = actual.directory

    def get(self, kind, identity):
        for k, record in self.records:
            if k == kind and record["id"] == identity:
                return copy.deepcopy(record)
        return self.actual.get(kind, identity)

    def list(self, kind):
        return self.actual.list(kind) + [copy.deepcopy(r) for k, r in self.records if k == kind]

    def put(self, kind, document):
        # Draft IDs depend on semantics; final publication retains these exact snapshots.
        value = {
            **copy.deepcopy(document),
            "id": kind.rstrip("s") + "-study-" + digest(document),
            "created_at": now(),
        }
        value["content_hash"] = digest(value)
        self.records.append((kind, value))
        return copy.deepcopy(value)


def _invalidate(record, path, unit, study_hash):
    if path in record.get("parameter_uncertainty", {}):
        record["parameter_uncertainty"][path] = {
            "kind": "unknown",
            "unit": unit,
            "value": None,
            "source_kind": "estimated",
            "source_uri": "study://" + study_hash,
            "method_and_version": "parameter-study/1",
            "reason": "Original input bounds are unverified for this changed nominal value.",
        }


def _coordinates(spec, baseline):
    if spec["design"] == "cartesian":
        product_size = math.prod(len(a["values"]) for a in spec["axes"])
        if product_size > 24:
            raise ValueError("The complete study including baselines is limited to 24 runs")
        points = [
            dict(zip(baseline, values, strict=True))
            for values in itertools.product(*(a["values"] for a in spec["axes"]))
        ]
    else:
        points = [
            {**baseline, axis["parameter"]: value}
            for axis in spec["axes"]
            for value in axis["values"]
        ]
    # Always retain a baseline and one execution for each distinct point.
    result, seen = [], set()
    for point in [baseline, *points]:
        key = canonical(point)
        if key not in seen:
            result.append(point)
            seen.add(key)
    return result


def _validate_definitions(spec):
    for axis in spec["axes"]:
        descriptor = PARAMETERS.get(axis["parameter"])
        if descriptor is None or descriptor["unit"] != axis["unit"]:
            raise ValueError("Unsupported study parameter or unit: " + axis["parameter"])
        if min(axis["values"]) < 0:
            raise ValueError("Study parameters must be nonnegative")
    for metric in [o["metric"] for o in spec["objectives"]] + [
        c["metric"] for c in spec["constraints"]
    ]:
        if metric not in METRICS:
            raise ValueError("Unsupported study metric: " + metric)
        if spec["plugin_id"] == "quick_estimate_v1" and metric == "max_relative_color_shading":
            raise ValueError("Color shading requires Response-DB field samples")
    for constraint in spec["constraints"]:
        if constraint["unit"] != METRICS[constraint["metric"]]["unit"]:
            raise ValueError("Constraint unit does not match the selected metric")


def prepare(service, payload):
    spec = StudyPlan.model_validate(payload).model_dump()
    _validate_definitions(spec)
    test = service.registry.get("tests", spec["test_id"])
    if test.get("observation_method"):
        raise ValueError("RAW observation conditions are frozen; use a design test for exploration")
    assemblies = [service.registry.get("assemblies", i) for i in spec["assembly_ids"]]
    models = {i: service.registry.get("models", i) for a in assemblies for i in a["model_ids"]}
    from .service import environment_fingerprint

    environment = environment_fingerprint()
    if environment != service.loaded_environment:
        raise ConflictError("Executable source changed; restart before planning a study")
    source_hashes = {r["id"]: r["content_hash"] for r in [test, *assemblies, *models.values()]}
    plan_hash = digest(
        {
            "schema": "camera_parameter_study_v1",
            "specification": spec,
            "source_hashes": source_hashes,
            "environment": environment,
        }
    )
    draft = copy.copy(service)
    draft.registry = _DraftRegistry(service.registry)
    candidates, requests = [], []
    for assembly in assemblies:
        sensor = next(models[i] for i in assembly["model_ids"] if models[i]["kind"] == "sensor")
        baseline = {}
        for axis in spec["axes"]:
            key, descriptor = axis["parameter"], PARAMETERS[axis["parameter"]]
            owner = test if descriptor["owner"] == "test" else sensor["payload"]
            baseline[key] = (
                1.0
                if descriptor["operation"] == "scale"
                else float(_get(owner, descriptor["path"]))
            )
        for point in _coordinates(spec, baseline):
            if len(candidates) >= 24:
                raise ValueError("The complete study including baselines is limited to 24 runs")
            index = len(candidates)
            changed = {k: v for k, v in point.items() if v != baseline[k]}
            selected_sensor, selected_test, selected_assembly = sensor, test, assembly
            sensor_data, test_data = _fields(sensor, ModelCreate), _fields(test, TestCreate)
            sensor_changed, test_changed = False, False
            for key, value in changed.items():
                descriptor = PARAMETERS[key]
                owner = test_data if descriptor["owner"] == "test" else sensor_data["payload"]
                nominal = _get(owner, descriptor["path"])
                result = (
                    (np.asarray(nominal, dtype=float) * value).tolist()
                    if descriptor["operation"] == "scale"
                    else value
                )
                _set(owner, descriptor["path"], result)
                record = test_data if descriptor["owner"] == "test" else sensor_data
                _invalidate(
                    record,
                    descriptor["path"],
                    "electron/photon"
                    if key == "qe_scale"
                    else "W/m^2/nm"
                    if key == "irradiance_scale"
                    else descriptor["unit"],
                    plan_hash,
                )
                sensor_changed |= descriptor["owner"] == "sensor"
                test_changed |= descriptor["owner"] == "test"
                if key == "gain":
                    test_data["conditions"]["gain"]["value"] = value
                    test_changed = True
            suffix = f"study-{plan_hash[:16]}-{index}"
            if sensor_changed:
                sensor_data.update(
                    name=sensor["name"][:120] + " · 설계 변수",
                    revision=suffix,
                    parents=[sensor["id"]],
                    source_kind="estimated",
                    physical_instance=None,
                    source_uri="study://" + plan_hash,
                    method_and_version="parameter-study/1",
                    uncertainty={"kind": "unknown"},
                )
                selected_sensor = draft.create_model(sensor_data)
            if test_changed:
                test_data.update(
                    name=test["name"][:120] + " · 설계 조건",
                    revision=suffix,
                    parent_test_id=test["id"],
                )
                selected_test = draft.create_test(test_data)
            if changed:
                assembly_data = _fields(assembly, AssemblyCreate)
                assembly_data.update(
                    name=assembly["name"][:120] + " · 설계 실험",
                    revision=suffix,
                    model_ids=[
                        selected_sensor["id"] if i == sensor["id"] else i
                        for i in assembly["model_ids"]
                    ],
                    conditions=copy.deepcopy(selected_test["conditions"]),
                )
                selected_assembly = draft.create_assembly(assembly_data)
            frozen, _ = draft._prepare_run(
                {
                    "assembly_id": selected_assembly["id"],
                    "test_id": selected_test["id"],
                    "plugin_id": spec["plugin_id"],
                    "parameters": {
                        "seed": spec["seed"],
                        "frames": spec["frames"],
                        "output_size": spec["output_size"],
                    },
                }
            )
            requests.append(frozen)
            candidates.append(
                {
                    "index": index,
                    "base_assembly_id": assembly["id"],
                    "assembly_id": selected_assembly["id"],
                    "test_id": selected_test["id"],
                    "is_baseline": not changed,
                    "coordinates": point,
                    "changed": changed,
                    "resources": frozen["resources"],
                }
            )
    plan = {
        "schema": "camera_parameter_study_v1",
        "name": spec["name"],
        "question": spec["question"],
        "specification": spec,
        "plan_hash": plan_hash,
        "source_hashes": source_hashes,
        "environment": environment,
        "candidates": candidates,
        "run_count": len(candidates),
        "new_revision_count": len(draft.registry.records),
        "metric_definitions": METRICS,
        "limitations": [
            "Finite sampled design space; not a global optimum or validated physical ranking.",
            "Changed sensor parameters are estimates; no independent physical validation.",
            "Changed nominal input bounds become unknown; untouched bounds remain conditional.",
            "Array budgets are estimates, not measured memory/time or monetary cost.",
            "One-at-a-time slopes do not establish interactions or convergence.",
        ],
    }
    return plan, draft.registry.records, requests


def preview(service, payload):
    return prepare(service, payload)[0]


def submit(service, payload):
    data = StudySubmit.model_validate(payload).model_dump()
    plan, records, requests = prepare(service, data["specification"])
    if plan["plan_hash"] != data["plan_hash"]:
        raise ConflictError(
            "Study specification, sources or environment changed; preview the current plan"
        )
    return service.registry.publish_study(plan, records, requests)


def _finite_reduce(value, operation):
    if value is None:
        return None
    values = np.asarray(value, dtype=float)
    return float(operation(values)) if values.size and np.isfinite(values).all() else None


def metrics(run):
    summary = (run.get("result") or {}).get("summary", {})
    ratios = np.asarray(summary.get("channel_ratios", []), dtype=float)
    shading = None
    if (
        ratios.ndim == 2
        and ratios.shape[0] > 1
        and np.isfinite(ratios).all()
        and np.all(ratios[0] != 0)
    ):
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            shading = _finite_reduce(np.abs(ratios / ratios[0] - 1), np.max)
    return {
        "min_theoretical_snr_db": _finite_reduce(summary.get("theoretical_snr_db"), np.min),
        "min_saturation_margin_e": _finite_reduce(summary.get("saturation_margin_e"), np.min),
        "mean_photoelectrons_e": _finite_reduce(summary.get("channel_electrons"), np.mean),
        "max_relative_color_shading": shading,
        "exposure_ms": float(run["test_snapshot"]["conditions"]["exposure_s"]["value"]) * 1000,
    }


def _analysis_rows(plan, runs):
    spec = plan["specification"]
    rows = []
    for candidate, run in zip(plan["candidates"], runs, strict=True):
        values = metrics(run) if run["status"] == "succeeded" else {}
        checks = []
        for constraint in spec["constraints"]:
            value = values.get(constraint["metric"])
            status = (
                "unknown"
                if value is None
                else "satisfied"
                if (
                    value <= constraint["value"]
                    if constraint["operator"] == "<="
                    else value >= constraint["value"]
                )
                else "violated"
            )
            checks.append({**constraint, "actual": value, "status": status})
        feasible = (
            "infeasible"
            if any(c["status"] == "violated" for c in checks)
            else "unknown"
            if (run["status"] != "succeeded" or any(c["status"] == "unknown" for c in checks))
            else "feasible"
        )
        eligible = feasible == "feasible" and all(
            values.get(o["metric"]) is not None for o in spec["objectives"]
        )
        rows.append(
            {
                **candidate,
                "run_id": run["id"],
                "run_status": run["status"],
                "run_hash": run["content_hash"],
                "result_hash": digest(run["result"]) if run.get("result") is not None else None,
                "metrics": values,
                "constraints": checks,
                "feasibility": feasible,
                "pareto_status": "pending" if eligible else "excluded",
                "exclusion_reason": None
                if eligible
                else "Unfinished/failed run, unmet constraint or undefined objective",
                "error": run.get("error"),
                "physical_validation": "not_established",
            }
        )
    for row in rows:
        if row["pareto_status"] != "pending":
            continue

        def dominates(other, row=row):
            if other["pareto_status"] == "excluded":
                return False
            signs = [(1 if o["direction"] == "minimize" else -1) for o in spec["objectives"]]
            a = [
                s * other["metrics"][o["metric"]]
                for s, o in zip(signs, spec["objectives"], strict=True)
            ]
            b = [
                s * row["metrics"][o["metric"]]
                for s, o in zip(signs, spec["objectives"], strict=True)
            ]
            return all(x <= y for x, y in zip(a, b, strict=True)) and any(
                x < y for x, y in zip(a, b, strict=True)
            )

        row["pareto_status"] = (
            "dominated"
            if any(dominates(other) for other in rows if other is not row)
            else "non_dominated"
        )
    return rows


def sensitivities(plan, rows):
    result = []
    for assembly_id in plan["specification"]["assembly_ids"]:
        baseline = next(
            r for r in rows if r["base_assembly_id"] == assembly_id and r["is_baseline"]
        )
        for axis in plan["specification"]["axes"]:
            key = axis["parameter"]
            points = [
                r
                for r in rows
                if r["base_assembly_id"] == assembly_id
                and all(
                    r["coordinates"][k] == baseline["coordinates"][k]
                    for k in r["coordinates"]
                    if k != key
                )
            ]
            points.sort(key=lambda r: r["coordinates"][key])
            for left, right in zip(points, points[1:], strict=False):
                dx = right["coordinates"][key] - left["coordinates"][key]
                values = {}
                for metric, definition in METRICS.items():
                    a, b = left["metrics"].get(metric), right["metrics"].get(metric)
                    slope = (b - a) / dx if a is not None and b is not None and dx > 0 else None
                    values[metric] = {
                        "slope": slope if slope is not None and math.isfinite(slope) else None,
                        "unit": definition["unit"] + "/" + axis["unit"],
                    }
                result.append(
                    {
                        "base_assembly_id": assembly_id,
                        "parameter": key,
                        "lower": left["coordinates"][key],
                        "upper": right["coordinates"][key],
                        "left_run_id": left["run_id"],
                        "right_run_id": right["run_id"],
                        "metrics": values,
                        "method": "adjacent_secant_other_axes_at_baseline",
                        "derivative_error": "unknown",
                    }
                )
    return result


def analyze(service, identity):
    plan = service.registry.get("studies", identity)
    runs = [service.registry.get_run(i) for i in plan["run_ids"]]
    rows = _analysis_rows(plan, runs)
    terminal = all(r["status"] in {"succeeded", "failed", "cancelled"} for r in runs)
    return {
        "schema": "camera_study_analysis_v1",
        "study_id": identity,
        "study_hash": plan["content_hash"],
        "source_hashes": plan["source_hashes"],
        "environment": plan["environment"],
        "pareto_provisional": any(r["status"] != "succeeded" for r in runs),
        "status": "complete" if terminal else "running",
        "coverage": {
            state: sum(r["status"] == state for r in runs)
            for state in ["queued", "running", "succeeded", "failed", "cancelled"]
        },
        "specification": plan["specification"],
        "rows": rows,
        "pareto_scope": "Feasible samples only; provisional if any run is missing or unfinished",
        "sensitivities": sensitivities(plan, rows),
        "metric_definitions": METRICS,
        "total_uncertainty": {"kind": "unknown"},
        "physical_validation": "not_established",
        "limitations": plan["limitations"],
    }
