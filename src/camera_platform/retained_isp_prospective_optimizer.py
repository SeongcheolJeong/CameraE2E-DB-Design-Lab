"""Prospective, source-disjoint ISP assessment after a locked development choice.

The selection cannot name or read a holdout. Registry chronology proves only
that holdout observations were registered later, not that a shared-host user
was blind to them. All metrics remain baseline-relative code/display results.
"""

from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime

import numpy as np

from . import nonlinear_captures, retained_isp_source
from . import retained_isp_joint_workflow as joint
from . import retained_isp_observable_optimizer as observable
from .registry import IntegrityError, canonical, digest

SCHEMA = "camera_retained_isp_prospective_optimizer_v1"
SELECTION_SCHEMA = "camera_retained_isp_prospective_selection_v1"
ASSESSMENT_SCHEMA = "camera_retained_isp_prospective_assessment_v1"
SELECTION_ARTIFACT = "isp-prospective-selection.json"
ASSESSMENT_ARTIFACT = "isp-prospective-assessment.json"
DISPLAY_TIME = "display_temporal_change_rms_delta"
METRIC_UNITS = {
    **observable.METRIC_UNITS,
    DISPLAY_TIME: "normalized_display_rgb",
}


def validate_request(value):
    required = (
        "schema", "name", "development_joint_study_id", "baseline_candidate_id",
        "objectives", "constraints",
    )
    if (not isinstance(value, dict)
            or set(value) not in (set(required), {*required, "dynamic_roi"})):
        raise ValueError("Prospective optimizer request requires exactly its declared fields")
    if value["schema"] != SCHEMA:
        raise ValueError("Prospective optimizer schema is unsupported")
    result = {"schema": SCHEMA}
    for field in ("name", "development_joint_study_id", "baseline_candidate_id"):
        result[field] = observable._identity(value[field], field)
    objectives = value["objectives"]
    if not isinstance(objectives, list) or not 3 <= len(objectives) <= 4:
        raise ValueError("Prospective optimizer needs three or four bounded objectives")
    normalized = []
    for item in objectives:
        observable._exact(item, ("metric", "weight", "scale"), "objective")
        if item["metric"] not in METRIC_UNITS:
            raise ValueError("Prospective optimizer objective metric is unsupported")
        normalized.append({
            "metric": item["metric"],
            "weight": observable._positive(item["weight"], "objective weight"),
            "scale": observable._positive(item["scale"], "objective scale"),
        })
    if (len({item["metric"] for item in normalized}) != len(normalized)
            or not observable.REQUIRED_OBJECTIVES <= {item["metric"] for item in normalized}):
        raise ValueError("Prospective optimizer needs unique noise, time and color objectives")
    result["objectives"] = normalized
    constraints = value["constraints"]
    if not isinstance(constraints, list) or not 3 <= len(constraints) <= 4:
        raise ValueError("Prospective optimizer needs three or four hard constraints")
    normalized = []
    for item in constraints:
        observable._exact(item, ("metric", "operator", "value", "unit"), "constraint")
        metric = item["metric"]
        if (metric not in METRIC_UNITS or item["operator"] != "<="
                or item["unit"] != METRIC_UNITS.get(metric)):
            raise ValueError("Prospective optimizer constraint metric/unit is unsupported")
        normalized.append({
            "metric": metric, "operator": "<=",
            "value": observable._positive(item["value"], "constraint", zero=True),
            "unit": item["unit"],
        })
    if (len({item["metric"] for item in normalized}) != len(normalized)
            or not observable.REQUIRED_CONSTRAINTS <= {
                item["metric"] for item in normalized
            }):
        raise ValueError("Prospective optimizer needs unique time/color/brightness guards")
    result["constraints"] = normalized
    if "dynamic_roi" in value:
        result["dynamic_roi"] = _dynamic_roi(value["dynamic_roi"])
    return result


def _dynamic_roi(value, shape=None):
    """Predeclare the native dynamic region separately from repeat-noise ROI."""
    observable._exact(value, (
        "row_start", "row_end", "column_start", "column_end",
    ), "dynamic ROI")
    if (any(type(coordinate) is not int for coordinate in value.values())
            or not 0 <= value["row_start"] < value["row_end"]
            or not 0 <= value["column_start"] < value["column_end"]
            or value["row_end"] - value["row_start"] < 2
            or value["column_end"] - value["column_start"] < 2
            or shape is not None and (
                value["row_end"] > shape[0] or value["column_end"] > shape[1]
            )):
        raise ValueError("Prospective dynamic ROI exceeds native support")
    return deepcopy(value)


def _development(service, request):
    job = service.registry.get_run(request["development_joint_study_id"])
    if (job.get("status") != "succeeded" or job.get("job_kind") != joint.JOB_KIND
            or job.get("plugin_id") != joint.PLUGIN_ID
            or job.get("cost", {}).get("schema")
            != "camera_retained_isp_joint_budget_v3"):
        raise ValueError("Prospective choice requires a completed Joint Study v3")
    return joint.verify(service, job)


def _dynamic_metrics(service, job, metrics, candidate_ids, roi):
    """Replace only dynamic comparisons; repeat noise retains its own Joint ROI."""
    source = job["dynamic_source_snapshot"]
    _dynamic_roi(roi, source["raw_shape"][1:])
    raw = retained_isp_source.load_inputs(service, source)
    if list(raw.shape) != source["raw_shape"]:
        raise IntegrityError("Prospective dynamic RAW support differs")
    rows = slice(roi["row_start"], roi["row_end"])
    columns = slice(roi["column_start"], roi["column_end"])
    selected_raw = raw[:, rows, columns]
    black = source["raw_format"]["black_level_dn"]
    white = 2 ** source["raw_format"]["bit_depth"] - 1
    rail_fraction = float(np.mean((selected_raw <= black) | (selected_raw >= white)))
    raw_change = float(np.sqrt(np.mean(
        np.diff(selected_raw.astype(np.float64), axis=0) ** 2,
    )))
    cases = {case["candidate_id"]: case for case in job["result"]["summary"]["cases"]}
    base_code, base_display = observable._saved_display_and_code(
        service, job, cases[candidate_ids[0]],
    )
    base_code = base_code[:, rows, columns, :]
    base_display = base_display[:, rows, columns, :]
    baseline_change = float(np.sqrt(np.mean(np.diff(base_code, axis=0) ** 2)))
    if not math.isfinite(raw_change) or not math.isfinite(baseline_change):
        raise IntegrityError("Prospective dynamic support is not finite")
    support_reason = (
        "Dynamic original RAW ROI contains a black/white code rail"
        if rail_fraction > 0 else
        "Dynamic original RAW ROI has no observed frame-to-frame change"
        if raw_change == 0 else
        "Baseline dynamic pre-tone ROI contains clipped code"
        if np.any((base_code < 0) | (base_code > 1)) else
        "Baseline ISP has no resolvable observed frame-to-frame change"
        if baseline_change < observable.MIN_OBSERVED_CHANGE_RMS else None
    )
    for candidate_id in candidate_ids:
        case = cases[candidate_id]
        code, display = observable._saved_display_and_code(service, job, case)
        code = code[:, rows, columns, :]
        display = display[:, rows, columns, :]
        reason = support_reason
        if reason is None and np.any((code < 0) | (code > 1)):
            reason = "Candidate dynamic pre-tone ROI contains clipped code"
        change_delta = np.diff(code, axis=0) - np.diff(base_code, axis=0)
        chroma_delta = np.stack((
            (display[..., 0] - display[..., 1])
            - (base_display[..., 0] - base_display[..., 1]),
            (display[..., 2] - display[..., 1])
            - (base_display[..., 2] - base_display[..., 1]),
        ), axis=-1)
        values = {
            "temporal_change_rms_delta": (
                None if reason else float(np.sqrt(np.mean(change_delta ** 2)))
            ),
            "display_chroma_rms_delta": float(np.sqrt(np.mean(chroma_delta ** 2))),
            "display_rgb_mae_delta": float(np.mean(np.abs(display - base_display))),
        }
        if any(value is not None and not math.isfinite(value)
               for value in values.values()):
            raise IntegrityError("Prospective dynamic metric is not finite")
        record = metrics[candidate_id]
        record["metrics"].update(values)
        record["dynamic_support"] = {
            "status": "unknown" if reason else "ok",
            "reason": reason,
            "evaluation_roi": deepcopy(roi),
            "raw_rail_fraction": rail_fraction,
            "raw_observed_change_rms_dn": raw_change,
            "baseline_observed_change_rms": baseline_change,
            "minimum_observed_change_rms": observable.MIN_OBSERVED_CHANGE_RMS,
        }
        noise = case["metrics"][observable.engine.NOISE]
        record["unknown_reason"] = noise["reason"] if noise["status"] != "ok" else reason
    return metrics


def _prospective_metrics(service, job, request, *, candidate_ids):
    """Optionally compare displayed motion with the locked ISP baseline.

    This is output agreement, not a comparison with scene or measured motion.
    The extra metric is opt-in so old frozen selection/assessment documents
    continue to cold-verify without a schema or semantic migration.
    """
    metrics = observable._observable_metrics(service, job, candidate_ids=candidate_ids)
    roi = request.get("dynamic_roi", job["engine_specification"]["noise_roi"])
    if "dynamic_roi" in request:
        metrics = _dynamic_metrics(service, job, metrics, candidate_ids, roi)
    requested = {
        item["metric"] for name in ("objectives", "constraints")
        for item in request[name]
    }
    if DISPLAY_TIME not in requested:
        return metrics
    cases = {case["candidate_id"]: case for case in job["result"]["summary"]["cases"]}
    rows = slice(roi["row_start"], roi["row_end"])
    columns = slice(roi["column_start"], roi["column_end"])
    _, base_display = observable._saved_display_and_code(
        service, job, cases[candidate_ids[0]],
    )
    base_display = base_display[:, rows, columns, :]
    base_change = np.diff(base_display, axis=0)
    base_change_rms = float(np.sqrt(np.mean(base_change ** 2)))
    if not math.isfinite(base_change_rms):
        raise IntegrityError("Prospective baseline display time change is not finite")
    for candidate_id in candidate_ids:
        record = metrics[candidate_id]
        _, display = observable._saved_display_and_code(
            service, job, cases[candidate_id],
        )
        display_change = np.diff(display[:, rows, columns, :], axis=0)
        reason = record["dynamic_support"]["reason"]
        if reason is None and base_change_rms < observable.MIN_OBSERVED_CHANGE_RMS:
            reason = "Baseline display has no resolvable observed frame-to-frame change"
        delta = float(np.sqrt(np.mean((display_change - base_change) ** 2)))
        if not math.isfinite(delta):
            raise IntegrityError("Prospective display time change is not finite")
        record["metrics"][DISPLAY_TIME] = None if reason else delta
        record["display_temporal_support"] = {
            "status": "unknown" if reason else "ok",
            "reason": reason,
            "baseline_observed_change_rms": base_change_rms,
            "minimum_observed_change_rms": observable.MIN_OBSERVED_CHANGE_RMS,
        }
        if reason and reason != record["unknown_reason"]:
            record["unknown_reason"] = "; ".join(filter(None, (
                record["unknown_reason"], reason,
            )))
    return metrics


def _selection(service, request):
    development = _development(service, request)
    cases = development["result"]["summary"]["cases"]
    candidate_ids = [case["candidate_id"] for case in cases]
    if request["baseline_candidate_id"] not in candidate_ids:
        raise ValueError("Declared baseline is absent from the development candidate grid")
    ids = [request["baseline_candidate_id"], *(
        candidate_id for candidate_id in candidate_ids
        if candidate_id != request["baseline_candidate_id"]
    )]
    metrics = _prospective_metrics(service, development, request, candidate_ids=ids)
    rows, chosen = observable._rank(metrics, request)
    if "dynamic_roi" in request:
        for row in rows:
            row["dynamic_support"] = deepcopy(metrics[row["candidate_id"]]["dynamic_support"])
    selected = None if chosen is None else {
        "candidate_id": chosen["candidate_id"],
        "profile_sha256": chosen["profile_sha256"],
        "development_score": chosen["normalized_weighted_score"],
    }
    summary_artifact = next(
        artifact for artifact in development["result"]["artifacts"]
        if artifact["name"] == "summary.json"
    )
    specification = development["engine_specification"]
    return {
        "schema": SELECTION_SCHEMA,
        "request": deepcopy(request),
        "development_provenance": {
            "joint_study_id": development["id"],
            "joint_study_content_hash": development["content_hash"],
            "joint_study_plan_hash": development["plan_hash"],
            "joint_study_summary_sha256": summary_artifact["sha256"],
            "source_bindings_sha256": digest(observable._bindings(development)),
            "candidate_grid_sha256": digest({
                "model": development["isp_model_snapshot"],
                "axes": specification["axes"],
                "roi": specification["noise_roi"],
            }),
        },
        "development_rows": rows,
        "selected": selected,
        "holdout_job_id": None,
        "holdout_outputs_read": False,
        "holdout_blinding_enforced": False,
        "metric_basis": "saved_integer_raw_and_isp_outputs_only",
        "time_meaning": "baseline_relative_frame_change_not_temporal_truth",
        "color_meaning": "baseline_relative_display_chroma_not_color_accuracy",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
    }


def freeze_selection(service, payload):
    request = validate_request(payload)
    document = _selection(service, request)
    return {"selection": document, "artifact": observable._store_json(
        service, document, SELECTION_ARTIFACT,
    )}


def verify_selection(service, artifact):
    document = observable._read_json(service, artifact, SELECTION_ARTIFACT)
    if document.get("schema") != SELECTION_SCHEMA:
        raise IntegrityError("Prospective selection schema differs")
    try:
        expected = _selection(service, validate_request(document["request"]))
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError("Prospective selection source differs") from error
    if canonical(document) != canonical(expected):
        raise IntegrityError("Prospective selection differs from saved development RAW/ISP")
    return document


def _instant(value, label):
    if not isinstance(value, str):
        raise IntegrityError(label + " has no registry timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise IntegrityError(label + " has malformed registry timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntegrityError(label + " has no timezone-bound registry timestamp")
    return parsed


def _later(value, lock, label):
    if _instant(value, label) <= lock:
        raise ValueError(label + " predates or equals the frozen selection")
    return value


def _frame_fingerprints(service, job):
    """Re-read bounded original source frames; aliases survive NPY rewrapping."""
    result = set()
    for binding in observable._bindings(job):
        source = retained_isp_source.resolve(
            service, binding["source_id"], binding["selector"],
        )
        raw = retained_isp_source.load_inputs(service, source)
        if (source["selected_array_sha256"] != binding["selected_array_sha256"]
                or digest(source["source_snapshot"])
                != binding["source_snapshot_sha256"]):
            raise IntegrityError("Prospective source binding differs from original RAW")
        result.update(nonlinear_captures.pixel_hash(frame) for frame in raw)
    return result


def holdout_provenance(service, selection_job, holdout):
    """Bound the future original observations and reject development pixel reuse."""
    selected = selection_job["result"]["summary"]
    development = _development(service, selected["request"])
    compatible = observable._compatible(development, holdout)
    if (compatible["development_job_content_hash"]
            != selected["development_provenance"]["joint_study_content_hash"]
            or compatible["development_source_bindings_sha256"]
            != selected["development_provenance"]["source_bindings_sha256"]
            or compatible["candidate_grid_sha256"]
            != selected["development_provenance"]["candidate_grid_sha256"]):
        raise IntegrityError("Prospective holdout differs from locked development grid")
    lock_at = selection_job["updated_at"]
    lock = _instant(lock_at, "selection completion")
    joint_created = _later(holdout["created_at"], lock, "Holdout Joint Study")
    reference = holdout["temporal_reference_snapshot"]
    reference_created = _later(reference["created_at"], lock, "Holdout temporal reference")
    if reference["id"] == development["temporal_reference_snapshot"]["id"]:
        raise ValueError("Holdout reuses the development temporal reference record")
    source_times = []
    for binding in observable._bindings(holdout):
        run = service.registry.get_run(binding["source_id"])
        if run["id"] != binding["source_id"]:
            raise IntegrityError("Holdout original Run ID differs")
        source_times.append({
            "run_id": run["id"],
            "created_at": _later(run["created_at"], lock, "Holdout original RAW Run"),
        })
    development_pixels = _frame_fingerprints(service, development)
    holdout_pixels = _frame_fingerprints(service, holdout)
    if development_pixels & holdout_pixels:
        raise ValueError("Holdout reuses an original development RAW frame's pixels")
    return {
        **compatible,
        "selection_job_id": selection_job["id"],
        "selection_job_content_hash": selection_job["content_hash"],
        "selection_completed_at": lock_at,
        "holdout_joint_created_at": joint_created,
        "holdout_temporal_reference_id": reference["id"],
        "holdout_temporal_reference_created_at": reference_created,
        "holdout_original_run_creation": source_times,
        "development_frame_pixel_fingerprints_sha256": digest(sorted(development_pixels)),
        "holdout_frame_pixel_fingerprints_sha256": digest(sorted(holdout_pixels)),
        "registry_chronology": "holdout_observations_registered_after_locked_selection",
        "blinding_enforced": False,
    }


def _assessment(service, selection_job, holdout):
    selection_artifact = selection_job["result"]["artifacts"][0]
    selection = verify_selection(service, selection_artifact)
    selected = selection["selected"]
    if selected is None:
        raise ValueError("No feasible locked development candidate can be assessed")
    holdout = joint.verify(service, holdout)
    provenance = holdout_provenance(service, selection_job, holdout)
    ids = [selection["request"]["baseline_candidate_id"]]
    if selected["candidate_id"] not in ids:
        ids.append(selected["candidate_id"])
    metrics = _prospective_metrics(
        service, holdout, selection["request"], candidate_ids=ids,
    )
    if metrics[selected["candidate_id"]]["profile_sha256"] != selected["profile_sha256"]:
        raise IntegrityError("Prospective holdout profile differs from the locked candidate")
    chosen_values = metrics[selected["candidate_id"]]["metrics"]
    checks = [{
        **limit, "actual": chosen_values[limit["metric"]],
        "status": (
            "unknown" if chosen_values[limit["metric"]] is None else
            "within_bound" if chosen_values[limit["metric"]] <= limit["value"] else
            "exceeded"
        ),
    } for limit in selection["request"]["constraints"]]
    baseline_score = observable._score(metrics[ids[0]]["metrics"], selection["request"])
    chosen_score = observable._score(chosen_values, selection["request"])
    delta = None if baseline_score is None or chosen_score is None else (
        chosen_score - baseline_score
    )
    status = (
        "inconsistent" if any(item["status"] == "exceeded" for item in checks)
        or delta is not None and delta > 0 else
        "inconclusive" if any(item["status"] == "unknown" for item in checks)
        or delta is None else "consistent"
    )
    summary_artifact = next(
        artifact for artifact in holdout["result"]["artifacts"]
        if artifact["name"] == "summary.json"
    )
    return {
        "schema": ASSESSMENT_SCHEMA,
        "selection_job_id": selection_job["id"],
        "selection_artifact_sha256": selection_artifact["sha256"],
        "holdout_job_id": holdout["id"],
        "holdout_summary_sha256": summary_artifact["sha256"],
        "provenance": provenance,
        "selected": deepcopy(selected),
        "baseline_candidate_id": ids[0],
        "consistency_status": status,
        "selected_normalized_weighted_score": chosen_score,
        "baseline_normalized_weighted_score": baseline_score,
        "selected_minus_baseline_score": delta,
        "holdout_metrics": {candidate_id: metrics[candidate_id] for candidate_id in ids},
        "holdout_constraints": checks,
        "no_reselection_after_holdout": True,
        "holdout_outputs_read_at_selection": False,
        "holdout_blinding_enforced": False,
        "holdout_semantics": "prospective_registry_order_source_disjoint_unblinded",
        "metric_basis": "saved_integer_raw_and_isp_outputs_only",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
    }


def assess_holdout(service, selection_job, holdout):
    document = _assessment(service, selection_job, holdout)
    return {"assessment": document, "artifact": observable._store_json(
        service, document, ASSESSMENT_ARTIFACT,
    )}


def verify_assessment(service, selection_job, holdout, artifact):
    document = observable._read_json(service, artifact, ASSESSMENT_ARTIFACT)
    if document.get("schema") != ASSESSMENT_SCHEMA:
        raise IntegrityError("Prospective assessment schema differs")
    if canonical(document) != canonical(_assessment(service, selection_job, holdout)):
        raise IntegrityError("Prospective assessment differs from frozen RAW/ISP evidence")
    return document
