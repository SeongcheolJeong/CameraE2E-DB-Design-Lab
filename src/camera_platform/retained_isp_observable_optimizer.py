"""Two-stage ISP choice from cold-verified RAW/ISP outputs, without truth input.

Development selects a profile before any holdout output array is read.  The
selection and subsequent holdout assessment are separate content-addressed
JSON artifacts.  Color and temporal metrics are relative to a declared ISP
baseline on the same integer RAW, not claims of scene color or temporal truth.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy

import numpy as np

from . import retained_isp_joint_study as engine
from . import retained_isp_joint_workflow as joint
from .registry import IntegrityError, canonical, digest
from .retained_isp_source import load_inputs

SCHEMA = "camera_retained_isp_observable_optimizer_v1"
SELECTION_SCHEMA = "camera_retained_isp_observable_selection_v1"
ASSESSMENT_SCHEMA = "camera_retained_isp_observable_holdout_v1"
MAX_REPORT_BYTES = 2 * 1024 * 1024
METRIC_UNITS = {
    engine.NOISE: "normalized_code_rgb",
    "temporal_change_rms_delta": "normalized_code_rgb",
    "display_chroma_rms_delta": "normalized_display_rgb",
    "display_rgb_mae_delta": "normalized_display_rgb",
}
REQUIRED_OBJECTIVES = {
    engine.NOISE, "temporal_change_rms_delta", "display_chroma_rms_delta",
}
REQUIRED_CONSTRAINTS = {
    "temporal_change_rms_delta", "display_chroma_rms_delta",
    "display_rgb_mae_delta",
}
MIN_OBSERVED_CHANGE_RMS = 1e-6


def _exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(label + " requires exactly its declared fields")


def _identity(value, label):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 200):
        raise ValueError(label + " requires a bounded identifier")
    return value


def _positive(value, label, *, zero=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (value < 0 if zero else value < 1e-12) or value > 1e12):
        raise ValueError(label + " requires a finite nonnegative bound" if zero
                         else label + " requires a finite positive value")
    return float(value)


def validate_request(value):
    """Freeze the declared baseline, normalizers, weights and hard guards."""
    _exact(value, ("schema", "name", "development_joint_study_id",
                   "holdout_joint_study_id", "baseline_candidate_id",
                   "objectives", "constraints"), "observable optimizer request")
    if value["schema"] != SCHEMA:
        raise ValueError("Observable optimizer schema is unsupported")
    result = {"schema": SCHEMA}
    for field in ("name", "development_joint_study_id",
                  "holdout_joint_study_id", "baseline_candidate_id"):
        result[field] = _identity(value[field], field)
    if result["development_joint_study_id"] == result["holdout_joint_study_id"]:
        raise ValueError("Development and holdout must be different Joint Study jobs")
    objectives = value["objectives"]
    if not isinstance(objectives, list) or not 3 <= len(objectives) <= 4:
        raise ValueError("Observable optimizer needs three or four bounded objectives")
    normalized = []
    for item in objectives:
        _exact(item, ("metric", "weight", "scale"), "objective")
        if item["metric"] not in METRIC_UNITS:
            raise ValueError("Observable optimizer objective metric is unsupported")
        normalized.append({"metric": item["metric"],
                           "weight": _positive(item["weight"], "objective weight"),
                           "scale": _positive(item["scale"], "objective scale")})
    if (len({item["metric"] for item in normalized}) != len(normalized)
            or not REQUIRED_OBJECTIVES <= {item["metric"] for item in normalized}):
        raise ValueError("Observable optimizer needs unique noise, time and color objectives")
    result["objectives"] = normalized
    constraints = value["constraints"]
    if not isinstance(constraints, list) or not 3 <= len(constraints) <= 4:
        raise ValueError("Observable optimizer needs three or four hard constraints")
    normalized = []
    for item in constraints:
        _exact(item, ("metric", "operator", "value", "unit"), "constraint")
        metric = item["metric"]
        if (metric not in METRIC_UNITS or item["operator"] != "<="
                or item["unit"] != METRIC_UNITS.get(metric)):
            raise ValueError("Observable optimizer constraint metric/unit is unsupported")
        normalized.append({"metric": metric, "operator": "<=",
                           "value": _positive(item["value"], "constraint", zero=True),
                           "unit": item["unit"]})
    if (len({item["metric"] for item in normalized}) != len(normalized)
            or not REQUIRED_CONSTRAINTS <= {item["metric"] for item in normalized}):
        raise ValueError("Observable optimizer needs unique time/color/brightness guards")
    result["constraints"] = normalized
    return result


def _bindings(job):
    spec = job["engine_specification"]
    return [*spec["stationary_bindings"], spec["dynamic_binding"]]


def _compatible(development, holdout):
    """Check only frozen holdout metadata; never access holdout result arrays."""
    for label, job in (("development", development), ("holdout", holdout)):
        if (job.get("status") != "succeeded" or job.get("job_kind") != joint.JOB_KIND
                or job.get("plugin_id") != joint.PLUGIN_ID
                or job.get("cost", {}).get("schema")
                != "camera_retained_isp_joint_budget_v3"):
            raise ValueError(label + " requires a completed retained-array Joint Study v3")
    a, b = (job["engine_specification"] for job in (development, holdout))
    for key in ("isp_model_sha256", "base_profile_sha256", "axes", "noise_roi"):
        if canonical(a[key]) != canonical(b[key]):
            raise ValueError("Development/holdout candidate or ROI contract differs: " + key)
    if development["isp_model_snapshot"] != holdout["isp_model_snapshot"]:
        raise ValueError("Development/holdout ISP model snapshot differs")
    for key in ("assembly_id", "selector", "raw_format", "geometry",
                "pixel_pitch_um", "model_parts_sha256"):
        if canonical(a["dynamic_binding"][key]) != canonical(b["dynamic_binding"][key]):
            raise ValueError("Development/holdout camera or RAW contract differs: " + key)
    if a["dynamic_binding"]["raw_shape"][1:] != b["dynamic_binding"]["raw_shape"][1:]:
        raise ValueError("Development/holdout native sensor support differs")
    for key in ("source_id", "selected_array_sha256", "source_snapshot_sha256"):
        left = {item[key] for item in _bindings(development)}
        right = {item[key] for item in _bindings(holdout)}
        if left & right:
            raise ValueError("Holdout reuses development acquisition or original RAW: " + key)
    return {
        "development_job_id": development["id"],
        "development_job_content_hash": development["content_hash"],
        "development_plan_hash": development["plan_hash"],
        "development_source_bindings_sha256": digest(_bindings(development)),
        "holdout_job_id": holdout["id"],
        "holdout_job_content_hash": holdout["content_hash"],
        "holdout_plan_hash": holdout["plan_hash"],
        "holdout_source_bindings_sha256": digest(_bindings(holdout)),
        "candidate_grid_sha256": digest({
            "model": development["isp_model_snapshot"], "axes": a["axes"],
            "roi": a["noise_roi"],
        }),
    }


def _saved_display_and_code(service, job, case):
    source = job["dynamic_source_snapshot"]
    shape = tuple(source["raw_shape"])
    record = case["dynamic_processing"]
    code = joint._load_saved_array(
        service, record["pre_tone_artifact"], (*shape, 3), np.dtype("float64"),
    )
    display = joint._load_saved_array(
        service, record["display_rgb_artifact"], (*shape, 3), np.dtype("float64"),
    )
    return code, display


def _observable_metrics(service, job, *, candidate_ids):
    """Read only verified noise and saved processor arrays, never temporal truth."""
    cases = job["result"]["summary"]["cases"]
    by_id = {case["candidate_id"]: case for case in cases}
    if len(by_id) != len(cases) or not set(candidate_ids) <= set(by_id):
        raise IntegrityError("Observable optimizer candidate identity is missing")
    baseline_id = candidate_ids[0]
    baseline_code, baseline_display = _saved_display_and_code(
        service, job, by_id[baseline_id],
    )
    roi = job["engine_specification"]["noise_roi"]
    rows = slice(roi["row_start"], roi["row_end"])
    columns = slice(roi["column_start"], roi["column_end"])
    base_code = baseline_code[:, rows, columns, :]
    base_display = baseline_display[:, rows, columns, :]
    if base_code.shape[0] < 2:
        raise ValueError("Observable time comparison needs at least two original frames")
    source = job["dynamic_source_snapshot"]
    raw = load_inputs(service, source)
    if raw.shape != baseline_code.shape[:3]:
        raise IntegrityError("Observable ISP native RAW/RGB support differs")
    selected_raw = raw[:, rows, columns]
    black = source["raw_format"]["black_level_dn"]
    white = 2 ** source["raw_format"]["bit_depth"] - 1
    raw_rail_fraction = float(np.mean((selected_raw <= black) | (selected_raw >= white)))
    raw_change_dn = float(np.sqrt(np.mean(
        np.diff(selected_raw.astype(np.float64), axis=0) ** 2,
    )))
    baseline_change = float(np.sqrt(np.mean(np.diff(base_code, axis=0) ** 2)))
    if not math.isfinite(baseline_change) or not math.isfinite(raw_change_dn):
        raise IntegrityError("Observable ISP baseline time change is not finite")
    support_reason = (
        "Dynamic original RAW ROI contains a black/white code rail"
        if raw_rail_fraction > 0 else
        "Dynamic original RAW ROI has no observed frame-to-frame change"
        if raw_change_dn == 0 else
        "Baseline dynamic pre-tone ROI contains clipped code"
        if np.any((base_code < 0) | (base_code > 1)) else
        "Baseline ISP has no resolvable observed frame-to-frame change"
        if baseline_change < MIN_OBSERVED_CHANGE_RMS else None
    )
    result = {}
    for candidate_id in candidate_ids:
        case = by_id[candidate_id]
        code, display = _saved_display_and_code(service, job, case)
        code = code[:, rows, columns, :]
        display = display[:, rows, columns, :]
        candidate_support_reason = support_reason
        if candidate_support_reason is None and np.any((code < 0) | (code > 1)):
            candidate_support_reason = "Candidate dynamic pre-tone ROI contains clipped code"
        change_delta = np.diff(code, axis=0) - np.diff(base_code, axis=0)
        chroma_delta = np.stack((
            (display[..., 0] - display[..., 1])
            - (base_display[..., 0] - base_display[..., 1]),
            (display[..., 2] - display[..., 1])
            - (base_display[..., 2] - base_display[..., 1]),
        ), axis=-1)
        noise = case["metrics"][engine.NOISE]
        if noise["status"] not in ("ok", "unknown"):
            raise IntegrityError("Observable optimizer repeat-noise support differs")
        values = {
            engine.NOISE: noise["value"] if noise["status"] == "ok" else None,
            "temporal_change_rms_delta": (None if candidate_support_reason else
                                          float(np.sqrt(np.mean(change_delta ** 2)))),
            "display_chroma_rms_delta": float(np.sqrt(np.mean(chroma_delta ** 2))),
            "display_rgb_mae_delta": float(np.mean(np.abs(display - base_display))),
        }
        if any(value is not None and not math.isfinite(value)
               for value in values.values()):
            raise IntegrityError("Observable ISP output produced a nonfinite metric")
        result[candidate_id] = {
            "profile_sha256": case["profile_sha256"],
            "metrics": values,
            "unknown_reason": noise["reason"] if noise["status"] != "ok"
            else candidate_support_reason,
            "dynamic_support": {
                "status": "unknown" if candidate_support_reason else "ok",
                "reason": candidate_support_reason,
                "raw_rail_fraction": raw_rail_fraction,
                "raw_observed_change_rms_dn": raw_change_dn,
                "baseline_observed_change_rms": baseline_change,
                "minimum_observed_change_rms": MIN_OBSERVED_CHANGE_RMS,
            },
        }
    return result


def _rank(metrics, request):
    rows = []
    for candidate_id, record in metrics.items():
        values = record["metrics"]
        checks = [{**limit, "actual": values[limit["metric"]],
                   "status": ("unknown" if values[limit["metric"]] is None else
                              "pass" if values[limit["metric"]] <= limit["value"]
                              else "fail")}
                  for limit in request["constraints"]]
        unknown = (any(values[item["metric"]] is None for item in request["objectives"])
                   or any(check["status"] == "unknown" for check in checks))
        failed = any(check["status"] == "fail" for check in checks)
        feasibility = "infeasible" if failed else "unknown" if unknown else "feasible"
        score = _score(values, request) if feasibility == "feasible" else None
        rows.append({"candidate_id": candidate_id,
                     "profile_sha256": record["profile_sha256"],
                     "metrics": values, "unknown_reason": record["unknown_reason"],
                     "constraints": checks, "feasibility": feasibility,
                     "normalized_weighted_score": score, "pareto": False,
                     "dominated_by": []})
    names = [item["metric"] for item in request["objectives"]]
    feasible = [row for row in rows if row["feasibility"] == "feasible"]
    for row in feasible:
        for other in feasible:
            if other is row:
                continue
            own = [row["metrics"][name] for name in names]
            theirs = [other["metrics"][name] for name in names]
            if (all(a <= b for a, b in zip(theirs, own, strict=True))
                    and any(a < b for a, b in zip(theirs, own, strict=True))):
                row["dominated_by"].append(other["candidate_id"])
        row["pareto"] = not row["dominated_by"]
    selected = min(feasible, key=lambda row: (
        row["normalized_weighted_score"], row["candidate_id"],
    )) if feasible else None
    return rows, selected


def _score(values, request):
    if any(values[item["metric"]] is None for item in request["objectives"]):
        return None
    score = sum(item["weight"] * values[item["metric"]] / item["scale"]
                for item in request["objectives"])
    if not math.isfinite(score):
        raise IntegrityError("Observable ISP weighted score is not finite")
    return score


def _selection(service, request):
    development = service.registry.get_run(request["development_joint_study_id"])
    holdout = service.registry.get_run(request["holdout_joint_study_id"])
    provenance = _compatible(development, holdout)
    # This validates every development source, result metric and saved array.
    # The holdout job is intentionally not cold-verified or evaluated here.
    development = joint.verify(service, development)
    cases = development["result"]["summary"]["cases"]
    candidate_ids = [case["candidate_id"] for case in cases]
    if request["baseline_candidate_id"] not in candidate_ids:
        raise ValueError("Declared baseline is absent from the frozen candidate grid")
    comparison_ids = [request["baseline_candidate_id"], *(
        candidate_id for candidate_id in candidate_ids
        if candidate_id != request["baseline_candidate_id"]
    )]
    metrics = _observable_metrics(service, development, candidate_ids=comparison_ids)
    rows, selected = _rank(metrics, request)
    selected_identity = (None if selected is None else {
        "candidate_id": selected["candidate_id"],
        "profile_sha256": selected["profile_sha256"],
        "development_score": selected["normalized_weighted_score"],
    })
    summary_artifact = next(artifact for artifact in development["result"]["artifacts"]
                            if artifact["name"] == "summary.json")
    return {
        "schema": SELECTION_SCHEMA, "request": deepcopy(request),
        "provenance": {**provenance,
                       "development_summary_sha256": summary_artifact["sha256"]},
        "development_rows": rows,
        "selected": selected_identity,
        "selection_frozen_before_optimizer_holdout_scoring": True,
        "holdout_outputs_read": False,
        "holdout_blinding_enforced": False,
        "holdout_semantics": "source_disjoint_retrospective_assessment",
        "metric_basis": "saved_integer_raw_and_isp_outputs_only",
        "color_meaning": "baseline_relative_display_chroma_not_color_accuracy",
        "time_meaning": "baseline_relative_frame_change_not_temporal_truth",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
    }


def _store_json(service, document, name):
    content = canonical(document).encode()
    if len(content) > MAX_REPORT_BYTES:
        raise ValueError("Observable optimizer report exceeds its artifact budget")
    return service.registry.store_blob(content, name, "application/json")


def _read_json(service, artifact, name):
    if (not isinstance(artifact, dict)
            or set(artifact) != {"id", "name", "sha256", "bytes", "media_type", "url"}
            or artifact.get("id") != artifact.get("sha256")
            or artifact.get("url") != "/api/v1/artifacts/" + str(artifact.get("sha256"))
            or artifact.get("name") != name
            or artifact.get("media_type") != "application/json"
            or type(artifact.get("bytes")) is not int
            or not 0 < artifact["bytes"] <= MAX_REPORT_BYTES):
        raise IntegrityError("Observable optimizer artifact descriptor differs")
    content = service.registry.blob_path(artifact["sha256"]).read_bytes()
    if len(content) != artifact["bytes"]:
        raise IntegrityError("Observable optimizer artifact length differs")
    try:
        document = json.loads(content)
    except (UnicodeError, ValueError) as error:
        raise IntegrityError("Observable optimizer artifact is not JSON") from error
    if not isinstance(document, dict) or canonical(document).encode() != content:
        raise IntegrityError("Observable optimizer artifact is not canonical JSON")
    return document


def freeze_selection(service, payload):
    """Store a reviewable selection without reading holdout output metrics."""
    request = validate_request(payload)
    document = _selection(service, request)
    return {"selection": document,
            "artifact": _store_json(service, document, "isp-observable-selection.json")}


def verify_selection(service, artifact):
    document = _read_json(service, artifact, "isp-observable-selection.json")
    if document.get("schema") != SELECTION_SCHEMA:
        raise IntegrityError("Observable optimizer selection schema differs")
    try:
        request = validate_request(document["request"])
        expected = _selection(service, request)
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError("Observable optimizer selection source differs") from error
    if canonical(document) != canonical(expected):
        raise IntegrityError("Observable optimizer selection differs from saved RAW/ISP")
    return document


def _assessment(service, selection_artifact):
    selection = verify_selection(service, selection_artifact)
    selected = selection["selected"]
    if selected is None:
        return {"schema": ASSESSMENT_SCHEMA,
                "selection_sha256": selection_artifact["sha256"],
                "selected": None, "holdout_status": "not_run_no_feasible_development_candidate",
                "holdout_metrics": None, "holdout_constraints": None,
                "no_reselection_after_holdout": True,
                "holdout_blinding_enforced": False,
                "holdout_semantics": "source_disjoint_retrospective_assessment",
                "physical_validation": "not_established"}
    holdout = service.registry.get_run(
        selection["request"]["holdout_joint_study_id"],
    )
    holdout = joint.verify(service, holdout)
    development = service.registry.get_run(
        selection["request"]["development_joint_study_id"],
    )
    if _compatible(development, holdout) != {
            key: selection["provenance"][key]
            for key in selection["provenance"] if key != "development_summary_sha256"}:
        raise IntegrityError("Observable optimizer holdout lineage changed after selection")
    ids = [selection["request"]["baseline_candidate_id"]]
    if selected["candidate_id"] not in ids:
        ids.append(selected["candidate_id"])
    metrics = _observable_metrics(service, holdout, candidate_ids=ids)
    if metrics[selected["candidate_id"]]["profile_sha256"] != selected["profile_sha256"]:
        raise IntegrityError("Holdout selected profile differs from frozen development choice")
    selected_metrics = metrics[selected["candidate_id"]]["metrics"]
    checks = [{**limit, "actual": selected_metrics[limit["metric"]],
               "status": ("unknown" if selected_metrics[limit["metric"]] is None else
                          "pass" if selected_metrics[limit["metric"]] <= limit["value"]
                          else "fail")}
              for limit in selection["request"]["constraints"]]
    baseline_score = _score(metrics[ids[0]]["metrics"], selection["request"])
    selected_score = _score(selected_metrics, selection["request"])
    score_delta = (None if baseline_score is None or selected_score is None else
                   selected_score - baseline_score)
    status = ("fail" if (any(item["status"] == "fail" for item in checks)
                        or score_delta is not None and score_delta > 0)
              else "inconclusive" if (any(item["status"] == "unknown" for item in checks)
                                      or score_delta is None) else "pass")
    summary_artifact = next(artifact for artifact in holdout["result"]["artifacts"]
                            if artifact["name"] == "summary.json")
    return {
        "schema": ASSESSMENT_SCHEMA,
        "selection_sha256": selection_artifact["sha256"],
        "holdout_job_id": holdout["id"],
        "holdout_summary_sha256": summary_artifact["sha256"],
        "selected": deepcopy(selected),
        "baseline_candidate_id": ids[0],
        "holdout_status": status,
        "holdout_normalized_weighted_score": selected_score,
        "baseline_normalized_weighted_score": baseline_score,
        "selected_minus_baseline_score": score_delta,
        "holdout_metrics": {candidate_id: metrics[candidate_id] for candidate_id in ids},
        "holdout_constraints": checks,
        "no_reselection_after_holdout": True,
        "holdout_blinding_enforced": False,
        "holdout_semantics": "source_disjoint_retrospective_assessment",
        "metric_basis": "saved_integer_raw_and_isp_outputs_only",
        "physical_validation": "not_established",
    }


def assess_holdout(service, selection_artifact):
    """Read the locked choice, then assess only that profile and its baseline."""
    document = _assessment(service, selection_artifact)
    return {"assessment": document,
            "artifact": _store_json(service, document, "isp-observable-holdout.json")}


def verify_assessment(service, selection_artifact, assessment_artifact):
    document = _read_json(service, assessment_artifact, "isp-observable-holdout.json")
    if document.get("schema") != ASSESSMENT_SCHEMA:
        raise IntegrityError("Observable optimizer assessment schema differs")
    if canonical(document) != canonical(_assessment(service, selection_artifact)):
        raise IntegrityError("Observable optimizer holdout differs from saved ISP arrays")
    return document
