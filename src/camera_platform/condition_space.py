"""Read-only, model-owned applicability map for candidate camera conditions.

This is an authoring diagnostic, not a model revision or physical validation.
An omitted axis never means that a model is valid at every value of that axis.
"""

from __future__ import annotations

import math
from copy import deepcopy

from .composer import check_assembly

REQUEST_SCHEMA = "camera_condition_space_request_v1"
RESULT_SCHEMA = "camera_condition_space_result_v1"
MAX_AXES = 4
MAX_VALUES = 32


def validate_request(value):
    if not isinstance(value, dict) or set(value) != {"schema", "assembly_id", "candidates"}:
        raise ValueError("Condition-space request requires schema, assembly_id and candidates")
    if value["schema"] != REQUEST_SCHEMA:
        raise ValueError("Unsupported condition-space request")
    if (not isinstance(value["assembly_id"], str)
            or not value["assembly_id"] or len(value["assembly_id"]) > 100):
        raise ValueError("Condition-space request needs a saved assembly ID")
    candidates = value["candidates"]
    if (not isinstance(candidates, dict) or not 1 <= len(candidates) <= MAX_AXES
            or sum(len(v) if isinstance(v, list) else MAX_VALUES + 1
                   for v in candidates.values()) > MAX_VALUES):
        raise ValueError("Condition-space request needs 1..4 axes and at most 32 values")
    for axis, values in candidates.items():
        if (not isinstance(axis, str) or not axis or len(axis) > 64
                or not axis.replace("_", "").isalnum()
                or not isinstance(values, list) or not values
                or any(type(point) not in (int, float) or not math.isfinite(point)
                       for point in values)
                or len(set(values)) != len(values)):
            raise ValueError("Condition-space axes need distinct finite numeric candidate values")
    return deepcopy(value)


def _bounds(model, axis):
    domain = model.get("valid_domain")
    bounds = domain.get(axis) if isinstance(domain, dict) else None
    if bounds is None:
        return "missing", None
    if (not isinstance(bounds, dict) or not {"min", "max", "unit"} <= set(bounds)
            or type(bounds["min"]) not in (int, float)
            or type(bounds["max"]) not in (int, float)
            or not math.isfinite(bounds["min"]) or not math.isfinite(bounds["max"])
            or bounds["min"] > bounds["max"]
            or not isinstance(bounds["unit"], str) or not bounds["unit"]):
        return "invalid", deepcopy(bounds)
    return "declared", {name: deepcopy(bounds[name]) for name in ("min", "max", "unit")}


def _axis_space(models, axis):
    declarations = []
    for model in models:
        status, bounds = _bounds(model, axis)
        declarations.append({"model_id": model["id"], "kind": model["kind"],
                             "status": status, "bounds": bounds})
    declared = [item["bounds"] for item in declarations if item["status"] == "declared"]
    units = {item["unit"] for item in declared}
    if any(item["status"] == "invalid" for item in declarations):
        status = "invalid_declaration"
    elif len(units) > 1:
        status = "unit_mismatch"
    elif any(item["status"] == "missing" for item in declarations):
        status = "unknown_missing_declaration"
    elif not declared:
        status = "unknown_missing_declaration"
    elif max(item["min"] for item in declared) > min(item["max"] for item in declared):
        status = "empty_intersection"
    else:
        status = "covered"
    unit = next(iter(units)) if len(units) == 1 else None
    intersection = None
    if status == "covered":
        intersection = {"min": max(item["min"] for item in declared),
                        "max": min(item["max"] for item in declared),
                        "unit": unit}
    return status, intersection, declarations, unit


def analyze_records(assembly, models, specification):
    spec = validate_request(specification)
    if assembly["id"] != spec["assembly_id"]:
        raise ValueError("Condition-space assembly differs from its saved request")
    model_map = {model["id"]: model for model in models}
    selected = [model_map[identity] for identity in assembly["model_ids"]]
    result = {
        "schema": RESULT_SCHEMA,
        "assembly_id": assembly["id"],
        "assembly_content_hash": assembly.get("content_hash"),
        "model_content_hashes": {model["id"]: model.get("content_hash") for model in selected},
        "base_composer_status": check_assembly(assembly, selected)["status"],
        "axes": {},
        "physical_validation": "not_established",
        "execution_feasibility": "not_established_by_model_domain_check",
        "automatic_model_revision": False,
        "scope": "declared_model_domain_and_composer_preflight_only",
    }
    for axis, values in spec["candidates"].items():
        status, intersection, declarations, unit = _axis_space(selected, axis)
        outcomes = []
        for point in values:
            if unit is None or status == "invalid_declaration":
                outcomes.append({"value": point, "status": "unknown", "composer_status": None,
                                 "blocking_models": [item["model_id"] for item in declarations
                                                     if item["status"] != "declared"
                                                     or status == "unit_mismatch"],
                                 "issues": []})
                continue
            conditions = deepcopy(assembly["conditions"])
            conditions[axis] = {"value": point, "unit": unit}
            report = check_assembly({**assembly, "conditions": conditions}, selected)
            blockers = [item["model_id"] for item in declarations
                        if item["status"] != "declared"
                        or not item["bounds"]["min"] <= point <= item["bounds"]["max"]]
            outcome_status = ("unknown" if status == "unknown_missing_declaration"
                              else "compatible" if status == "covered" and report["status"] == "PASS"
                              else "blocked")
            outcomes.append({"value": point, "status": outcome_status,
                             "composer_status": report["status"],
                             "blocking_models": blockers, "issues": report["issues"]})
        result["axes"][axis] = {"space_status": status, "intersection": intersection,
                                "model_declarations": declarations, "candidates": outcomes}
    return result


def analyze(service, specification):
    spec = validate_request(specification)
    assembly = service.registry.get("assemblies", spec["assembly_id"])
    models = [service.registry.get("models", identity) for identity in assembly["model_ids"]]
    return analyze_records(assembly, models, spec)
