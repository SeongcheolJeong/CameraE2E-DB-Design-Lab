"""A bounded noise/detail decision over registered, disjoint RAW acquisitions.

This recipe uses the existing advanced ISP Study v2 planner, Worker and evidence
store. It deliberately does not consume a pyisetcam FACA optimization report:
FACA's scene scores and its ``ip.demosaic_method`` axis have a different input
and output contract from a retained RAW charge-domain ISP profile.
"""

from __future__ import annotations

import hashlib
import math
import time

from . import isp_studies as studies
from . import isp_studies_v2 as charge_studies
from .isp_study_contracts_v2 import ISPAxis, ISPStudyDataset, ISPStudyPlan
from .registry import ConflictError, IntegrityError, canonical, digest

SCHEMA = "camera_source_bound_isp_noise_detail_v1"
LEGACY_RECEIPT_SCHEMA = "camera_source_bound_isp_noise_detail_receipt_v1"
RECEIPT_SCHEMA = "camera_source_bound_isp_noise_detail_receipt_v2"
RECEIPT_KIND = "isp_optimization_receipts"
LIMIT_FIELDS = {
    "flat_noise_max_e", "texture_ratio_min", "texture_ratio_max",
    "texture_abs_bias_max_e", "holdout_noise_noninferiority_e",
}


def _number(value, name):
    if type(value) not in (int, float):
        raise ValueError(f"{name} requires a finite nonnegative number")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} requires a finite nonnegative number") from error
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} requires a finite nonnegative number")
    return parsed


def _datasets(items, phase):
    if not isinstance(items, list) or len(items) != 2:
        raise ValueError(f"{phase} requires one flat and one texture acquisition")
    parsed = [ISPStudyDataset.model_validate(item).model_dump() for item in items]
    if {item["key"] for item in parsed} != {"flat", "texture"}:
        raise ValueError(f"{phase} requires exact flat and texture dataset keys")
    by_key = {item["key"]: item for item in parsed}
    for key, item in by_key.items():
        if item["purpose"] != phase or item["metric_spec"].get("kind") != key:
            raise ValueError(f"{phase} {key} purpose or metric kind differs")
        if item["metric_spec"].get("stationary") is not True:
            raise ValueError("Noise/detail recipe requires declared stationary acquisitions")
    if not by_key["texture"]["reference_id"]:
        raise ValueError("Texture amplitude and bias guards require an independent reference ID")
    return [by_key["flat"], by_key["texture"]]


def validate_request(value):
    """Normalize the complete decision rule before any Study is published."""
    fields = {"schema", "name", "baseline_isp_model_id", "calibration_id",
              "axes", "tuning_datasets", "holdout_datasets", "limits"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != SCHEMA:
        raise ValueError("Source-bound ISP recipe requires exactly its v1 fields")
    for key in ("name", "baseline_isp_model_id"):
        item = value[key]
        limit = 135 if key == "name" else 160  # Reserve the longest Study phase suffix.
        if not isinstance(item, str) or not item or item != item.strip() or len(item) > limit:
            raise ValueError(f"{key} requires a bounded nonblank identifier")
    calibration_id = value["calibration_id"]
    if calibration_id is not None and (
        not isinstance(calibration_id, str) or not calibration_id
        or calibration_id != calibration_id.strip() or len(calibration_id) > 160
    ):
        raise ValueError("calibration_id requires a bounded immutable ID or null")
    axes = value["axes"]
    if not isinstance(axes, list) or not 1 <= len(axes) <= 2:
        raise ValueError("Recipe requires one or two bounded advanced ISP axes")
    parsed_axes = [ISPAxis.model_validate(item).model_dump() for item in axes]
    names = [item["parameter"] for item in parsed_axes]
    if len(set(names)) != len(names) or "raw_denoise.strength" not in names:
        raise ValueError("Recipe requires one unique raw denoise strength axis")
    if math.prod(len(item["values"]) for item in parsed_axes) + 1 > 12:
        raise ValueError("Recipe exceeds the twelve-profile two-dataset Study budget")
    limits = value["limits"]
    if not isinstance(limits, dict) or set(limits) != LIMIT_FIELDS:
        raise ValueError("Recipe requires all five explicit metric limits")
    limits = {key: _number(limits[key], key) for key in sorted(LIMIT_FIELDS)}
    if limits["texture_ratio_max"] < limits["texture_ratio_min"]:
        raise ValueError("Texture ratio upper bound must be at least its lower bound")
    tuning = _datasets(value["tuning_datasets"], "tuning")
    holdout = _datasets(value["holdout_datasets"], "independent_validation")
    identities = [(item["source_kind"], item["source_id"])
                  for item in [*tuning, *holdout]]
    if len(set(identities)) != len(identities):
        raise ValueError("Tuning and holdout must name four distinct original sources")
    return {
        "schema": SCHEMA, "name": value["name"],
        "baseline_isp_model_id": value["baseline_isp_model_id"],
        "calibration_id": calibration_id, "axes": parsed_axes,
        "tuning_datasets": tuning, "holdout_datasets": holdout,
        "limits": limits,
    }


def _targets(limits):
    return ([{
        "metric": "temporal_residual_std_e", "dataset_keys": ["flat"],
        "reducer": "max", "direction": "minimize",
    }], [
        {"metric": "temporal_residual_std_e", "dataset_keys": ["flat"],
         "reducer": "max", "operator": "<=", "value": limits["flat_noise_max_e"],
         "unit": "electron_equivalent"},
        {"metric": "texture_amplitude_ratio", "dataset_keys": ["texture"],
         "reducer": "min", "operator": ">=", "value": limits["texture_ratio_min"],
         "unit": "1"},
        {"metric": "texture_amplitude_ratio", "dataset_keys": ["texture"],
         "reducer": "max", "operator": "<=", "value": limits["texture_ratio_max"],
         "unit": "1"},
        {"metric": "mean_abs_bias_e", "dataset_keys": ["texture"],
         "reducer": "max", "operator": "<=", "value": limits["texture_abs_bias_max_e"],
         "unit": "electron_equivalent"},
    ])


def _spec(request, phase, *, selection=None, parent=None):
    objectives, constraints = _targets(request["limits"])
    return ISPStudyPlan.model_validate({
        "schema": "camera_isp_study_v2", "name": request["name"] + " · " + phase,
        "question": (
            "On fixed original RAW, which denoise/sharpen candidate minimizes "
            "stationary flat temporal residual within reference-bound texture "
            "amplitude, bias, and declared noise limits?"
        ),
        "phase": phase, "calibration_id": request["calibration_id"],
        "baseline_isp_model_id": request["baseline_isp_model_id"],
        "selection_id": selection, "parent_study_id": parent,
        "axes": request["axes"] if phase == "tuning" else [],
        "datasets": request["tuning_datasets"] if phase == "tuning"
                    else request["holdout_datasets"],
        "objectives": objectives, "constraints": constraints,
    }).model_dump()


def _preflight_disjoint(service, request):
    """Reject reused RAW/lineage before publishing either Study."""
    groups = []
    for name in ("tuning_datasets", "holdout_datasets"):
        groups.append([charge_studies._dataset(service, item, request["calibration_id"])
                       for item in request[name]])
    for index, left in enumerate(groups[0]):
        for right in [*groups[0][index + 1:], *groups[1]]:
            if charge_studies._overlap(left, right):
                shared = set(left["acquisition_lineage_ids"]) & set(
                    right["acquisition_lineage_ids"]
                )
                raise ValueError(
                    "Recipe acquisitions overlap by RAW bytes or lineage: "
                    f"{left['key']}:{left['source']['source_id']} and "
                    f"{right['key']}:{right['source']['source_id']} "
                    f"shared={sorted(shared)[:4]}"
                )
    if charge_studies._overlap(*groups[1]):
        raise ValueError("Holdout acquisitions overlap by RAW bytes or lineage")
    bindings = [item["processing_binding"] for group in groups for item in group]
    if any(binding != bindings[0] for binding in bindings[1:]):
        raise ValueError(
            "Noise/detail recipe requires one common selector, readout, geometry "
            "and normalization across all four acquisitions"
        )
    return [
        {
            "phase": phase,
            "dataset_key": item["key"],
            "source_id": item["source"]["source_id"],
            "source_kind_declared": item["source"]["source_provenance"]["source_kind"],
            "raw_sha256": item["raw_sha256"],
            "reference_derivation_declared": (
                None if item["reference_snapshot"] is None
                else item["reference_snapshot"]["derivation"]
            ),
            "reference_source_kind_declared": (
                None if item["reference_snapshot"] is None
                else item["reference_snapshot"]["source_kind"]
            ),
        }
        for phase, group in zip(("tuning", "holdout"), groups, strict=True)
        for item in group
    ]


def _product_claim_scope(source_evidence):
    blockers = [
        "holdout_inputs_visible_and_read_during_planning_no_access_controlled_blinding",
        "source_and_reference_origin_are_registry_pinned_declarations_not_authenticated",
        "metric_is_fixed_charge_raw_isp_proxy_not_product_image_quality_or_perception",
        "independent_physical_camera_validation_and_uncertainty_not_established",
    ]
    if any(item["source_kind_declared"] != "measured" for item in source_evidence):
        blockers.append("one_or_more_sources_are_not_declared_measured_acquisitions")
    if any(item["reference_derivation_declared"] == "authored_independent"
           for item in source_evidence):
        blockers.append("one_or_more_texture_references_are_authored_not_measured")
    return {
        "available": False,
        "status": "not_established",
        "claim_type": "product_isp_performance",
        "holdout_blinding_enforced": False,
        "holdout_semantics": "source_disjoint_retrospective_unblinded",
        "blockers": blockers,
    }


def plan_recipe(service, payload):
    """Read-only preflight with the exact tuning plan and frozen holdout sources."""
    request = validate_request(payload)
    source_evidence = _preflight_disjoint(service, request)
    tuning_spec = _spec(request, "tuning")
    tuning_plan = studies.plan(service, tuning_spec)
    return {"schema": SCHEMA, "request": request, "tuning_specification": tuning_spec,
            "tuning_plan": tuning_plan,
            "holdout_source_bindings_sha256": digest(request["holdout_datasets"]),
            "source_evidence": source_evidence,
            "product_performance": _product_claim_scope(source_evidence),
            "physical_validation": "not_established",
            "product_ranking_allowed": False}


def _wait_for_study(service, identity, deadline, poll_seconds):
    while True:
        analysis = studies.analyze(service, identity)
        if analysis["status"] == "complete":
            return analysis
        if time.monotonic() >= deadline:
            raise TimeoutError(f"ISP Study {identity} still needs the registered Worker")
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def _noise_reduction(analysis):
    matches = [item["id"] for item in analysis["reduction_definitions"]
               if item["metric"] == "temporal_residual_std_e"
               and item["dataset_ids"] == ["flat"] and item["reducer"] == "max"]
    if len(matches) != 1:
        raise IntegrityError("Recipe flat noise reduction is missing or ambiguous")
    return matches[0]


def _choice(analysis):
    identity = _noise_reduction(analysis)
    rows = analysis["aggregate"]["rows"]
    eligible = [row for row in rows if row["status"] == "succeeded"
                and row["feasibility"] == "feasible"
                and row["metrics"].get(identity, {}).get("status") == "ok"]
    return (None if not eligible else min(
        eligible, key=lambda row: (row["metrics"][identity]["value"], row["candidate_id"])
    ))


def _assessment(analysis, study, selection, tolerance):
    noise = _noise_reduction(analysis)
    by_id = {row["candidate_id"]: row for row in analysis["aggregate"]["rows"]}
    baseline = next(candidate for candidate in study["candidates"] if candidate["is_baseline"])
    selected = next(candidate for candidate in study["candidates"]
                    if candidate["profile_sha256"] == selection["profile_sha256"])
    a, b = by_id[baseline["candidate_id"]], by_id[selected["candidate_id"]]
    base_value = a["metrics"].get(noise, {}).get("value")
    selected_value = b["metrics"].get(noise, {}).get("value")
    if (analysis["coverage"]["failed"] or analysis["coverage"]["cancelled"]
            or base_value is None or selected_value is None
            or b["feasibility"] == "unknown"):
        status = "inconclusive"
    elif b["feasibility"] != "feasible" or selected_value > base_value + tolerance:
        status = "retrospective_criteria_fail"
    elif selected["candidate_id"] == baseline["candidate_id"]:
        status = "retrospective_baseline_selected"
    else:
        status = "retrospective_criteria_pass"
    return {
        "status": status,
        "scope": (
            "source_disjoint_unblinded_holdout_noise_noninferiority_"
            "with_texture_and_bias_guards"
        ),
        "baseline_candidate_id": baseline["candidate_id"],
        "selected_candidate_id": selected["candidate_id"],
        "baseline_flat_noise_e": base_value,
        "selected_flat_noise_e": selected_value,
        "noninferiority_tolerance_e": tolerance,
        "selected_feasibility": b["feasibility"],
        "holdout_blinding_enforced": False,
        "product_performance_available": False,
        "physical_validation": "not_established",
    }


def _save_receipt(service, payload, references):
    content = canonical(payload).encode()
    artifact = service.registry.store_blob(
        content, "source-bound-isp-optimization.json", "application/json",
    )
    identity = "isp_optimization_receipt-" + digest(payload)
    document = {"schema": RECEIPT_SCHEMA, "payload": payload, "artifact": artifact}
    try:
        return service.registry.put_verified_record(
            RECEIPT_KIND, document, references, identity=identity,
        )
    except ConflictError:
        existing = get_receipt(service, identity)
        if existing["payload"] != payload:
            raise
        return existing


def get_receipt(service, identity):
    """Reopen Study evidence and check hashes; do not replay the decision rule."""
    record = service.registry.get(RECEIPT_KIND, identity)
    if record.get("schema") not in {LEGACY_RECEIPT_SCHEMA, RECEIPT_SCHEMA}:
        raise IntegrityError("ISP optimization receipt schema differs")
    artifact = record["artifact"]
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    if (len(content) != artifact["bytes"]
            or hashlib.sha256(content).hexdigest() != artifact["sha256"]
            or content != canonical(record["payload"]).encode()):
        raise IntegrityError("ISP optimization receipt artifact differs")
    payload = record["payload"]
    if payload.get("schema") != record["schema"]:
        raise IntegrityError("ISP optimization receipt payload schema differs")
    if record["schema"] == RECEIPT_SCHEMA:
        if (payload.get("product_performance") != _product_claim_scope(
            payload.get("source_evidence", [])
        ) or payload.get("product_ranking_allowed") is not False
                or payload.get("physical_validation") != "not_established"
                or payload.get("assessment", {}).get("status") in {
                    "pass", "fail", "baseline_selected"
                }):
            raise IntegrityError("ISP optimization v2 receipt lost its product claim gate")
    for prefix in ("tuning", "holdout"):
        study_id = payload.get(prefix + "_study_id")
        if study_id is None:
            continue
        study = studies.get_study(service, study_id)
        analysis = studies.analyze(service, study_id)
        if (study["content_hash"] != payload[prefix + "_study_content_hash"]
                or analysis["analysis_sha256"] != payload[prefix + "_analysis_sha256"]):
            raise IntegrityError("ISP optimization Study evidence differs from receipt")
    selection_id = payload.get("selection_id")
    if selection_id is not None and (
        studies.get_selection(service, selection_id)["content_hash"]
        != payload["selection_content_hash"]
    ):
        raise IntegrityError("ISP optimization selection differs from receipt")
    return record


def run_recipe(service, payload, *, timeout_seconds=300.0, poll_seconds=0.1):
    """Run the registered two-stage Study with an already running Worker.

    Returns a saved evidence-linked receipt. A timeout leaves immutable queued work
    intact; retrying the same request resumes the idempotent Study identities.
    """
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0 or timeout_seconds > 3600
            or type(poll_seconds) not in (int, float) or not math.isfinite(poll_seconds)
            or not 0.01 <= poll_seconds <= 10):
        raise ValueError("Recipe requires a bounded positive wait and polling interval")
    deadline = time.monotonic() + timeout_seconds
    planned = plan_recipe(service, payload)
    request = planned["request"]
    tuning = studies.submit(service, {
        "specification": planned["tuning_specification"],
        "plan_hash": planned["tuning_plan"]["plan_hash"],
    })
    tuning_analysis = _wait_for_study(service, tuning["id"], deadline, poll_seconds)
    chosen = _choice(tuning_analysis)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "request": request,
        "tuning_study_id": tuning["id"],
        "tuning_study_content_hash": tuning["content_hash"],
        "tuning_analysis_sha256": tuning_analysis["analysis_sha256"],
        "selection_rule": "minimum_feasible_flat_temporal_residual_then_candidate_id",
        "selected_candidate_id": None if chosen is None else chosen["candidate_id"],
        "selection_id": None, "selection_content_hash": None,
        "holdout_study_id": None, "holdout_study_content_hash": None,
        "holdout_analysis_sha256": None,
        "assessment": {
            "status": "inconclusive", "reason": "no_feasible_tuning_candidate",
            "holdout_blinding_enforced": False,
            "product_performance_available": False,
            "physical_validation": "not_established",
        },
        "source_evidence": planned["source_evidence"],
        "product_performance": planned["product_performance"],
        "physical_validation": "not_established", "product_ranking_allowed": False,
    }
    references = [("isp_studies", tuning)]
    if chosen is None:
        return _save_receipt(service, receipt, references)
    selection = studies.select_candidate(service, tuning["id"], {
        "candidate_id": chosen["candidate_id"],
        "rationale": (
            "Predeclared source-bound recipe: minimum feasible stationary flat "
            "temporal residual, with independent-reference texture ratio and "
            "bias guards. This is a calculation choice, not physical approval."
        ),
        "analysis_sha256": tuning_analysis["analysis_sha256"],
    })
    receipt["selection_id"] = selection["id"]
    receipt["selection_content_hash"] = selection["content_hash"]
    references.append(("isp_selections", selection))
    holdout_spec = _spec(request, "independent_validation",
                         selection=selection["id"], parent=tuning["id"])
    holdout_plan = studies.plan(service, holdout_spec)
    holdout = studies.submit(service, {
        "specification": holdout_spec, "plan_hash": holdout_plan["plan_hash"],
    })
    holdout_analysis = _wait_for_study(service, holdout["id"], deadline, poll_seconds)
    receipt["holdout_study_id"] = holdout["id"]
    receipt["holdout_study_content_hash"] = holdout["content_hash"]
    receipt["holdout_analysis_sha256"] = holdout_analysis["analysis_sha256"]
    receipt["assessment"] = _assessment(
        holdout_analysis, holdout, selection,
        request["limits"]["holdout_noise_noninferiority_e"],
    )
    references.append(("isp_studies", holdout))
    return _save_receipt(service, receipt, references)
