"""Bounded same-source code-domain ISP candidate comparisons.

One durable job preserves every candidate's actual pre-tone/display arrays.
This is authored tuning evidence, never a scene estimate, noise measurement,
physical ranking, automatic selection, or model publication.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import math
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path

import numpy as np

from .registry import ConflictError, IntegrityError, canonical, digest

SCHEMA = "camera_retained_code_comparison_v1"
SCHEMA_V2 = "camera_retained_code_comparison_v2"
JOB_KIND = "retained_isp_comparison"
PLUGIN_ID = "retained_isp_comparison_v1"
PLUGIN_ID_V2 = "retained_isp_comparison_v2"
AXES = ("spatial_filter.strength", "temporal_filter.previous_weight", "detail.amount")
METRICS = (
    "mean_abs_baseline_delta",
    "rms_baseline_delta",
    "max_abs_baseline_delta",
    "display_clipping_fraction",
    "spatial_tv",
    "temporal_change_distortion",
)
MAX_CANDIDATES = 12
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_WORKING_BYTES = 1024**3
MAX_ARRAY_BYTES = 128 * 1024 * 1024
MAX_GENERATED_SUMMARY_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_OVERHEAD_BYTES = 2 * 1024 * 1024
NPY_HEADER_BYTES_LIMIT = 512
AUTHORITY = {
    "physical_validation": "not_established",
    "physical_measurement_count": 0,
    "uncertainty": {"kind": "unknown"},
    "automatic_selection": False,
    "model_promotion": False,
    "current_exposure_photocharge_estimation": False,
}


def _exact(value, keys, label, optional=()):
    if (
        not isinstance(value, dict)
        or not set(keys) <= value.keys()
        or value.keys() - set(keys) - set(optional)
    ):
        raise ValueError(label + " requires exactly its declared fields")


def _number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(label + " requires a finite number, never a boolean")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(label + " exceeds the finite numerical domain") from error
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(label + " is outside its finite bounds")
    return result


def _identity(value, label):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 200:
        raise ValueError(label + " must be a nonblank bounded identifier")
    return value


def validate_specification(payload):
    from .retained_isp_source import validate_selector
    from .temporal_signal_metrics import METRIC_UNITS, OBJECTIVE_METRICS

    version_two = isinstance(payload, dict) and payload.get("schema") == SCHEMA_V2

    keys = {
        "schema",
        "name",
        "source_kind",
        "source_id",
        "selector",
        "isp_model_id",
        "axes",
        "roi",
        "objectives",
        "limits",
    }
    extra = {"force"}
    if version_two:
        keys |= {"temporal_test_id", "temporal_objectives", "temporal_constraints"}
    _exact(payload, keys, "comparison", extra)
    if payload["schema"] not in (SCHEMA, SCHEMA_V2) or payload["source_kind"] != "run":
        raise ValueError("Comparison requires the retained code schema and an original run")
    if type(payload.get("force", False)) is not bool:
        raise ValueError("force must be boolean")
    result = {k: deepcopy(payload[k]) for k in keys}
    for key in ("name", "source_id", "isp_model_id"):
        result[key] = _identity(payload[key], key)
    result["selector"] = validate_selector(payload["selector"])
    axes = payload["axes"]
    if not isinstance(axes, list) or not 1 <= len(axes) <= 2:
        raise ValueError("Comparison requires one or two authored filter axes")
    result["axes"] = []
    for axis in axes:
        _exact(axis, {"parameter", "values"}, "axis")
        if axis["parameter"] not in AXES:
            raise ValueError("Only declared filter axes may vary; fixed color/tone cannot vary")
        if not isinstance(axis["values"], list) or not 1 <= len(axis["values"]) <= MAX_CANDIDATES:
            raise ValueError("An axis needs one to twelve explicit values")
        maximum = 0.95 if axis["parameter"] == "temporal_filter.previous_weight" else 1
        values = [_number(v, 0, maximum, axis["parameter"]) for v in axis["values"]]
        if len(set(values)) != len(values):
            raise ValueError("Axis values must be unique")
        result["axes"].append({"parameter": axis["parameter"], "values": values})
    if len({a["parameter"] for a in axes}) != len(axes):
        raise ValueError("Comparison axes must be unique")
    if math.prod(len(a["values"]) for a in axes) > MAX_CANDIDATES:
        raise ValueError("Comparison has more than twelve candidates")
    roi = payload["roi"]
    _exact(roi, {"row_start", "row_end", "column_start", "column_end"}, "roi")
    if any(type(value) is not int or not 0 <= value <= 4096 for value in roi.values()):
        raise ValueError("ROI coordinates must be bounded native integer indices")
    if any(roi[axis + "_end"] - roi[axis + "_start"] < 2 for axis in ("row", "column")):
        raise ValueError("The fixed ROI must be at least two by two pixels")
    objectives = payload["objectives"]
    if not isinstance(objectives, list) or not 2 <= len(objectives) <= len(METRICS):
        raise ValueError("Objectives must explicitly include fidelity and spatial variation")
    for item in objectives:
        _exact(item, {"metric", "direction"}, "objective")
        if item["metric"] not in METRICS or item["direction"] != "minimize":
            raise ValueError(
                "Only explicit minimize objectives over declared code metrics are supported"
            )
    selected = [item["metric"] for item in objectives]
    if len(set(selected)) != len(selected) or not {"mean_abs_baseline_delta", "spatial_tv"} <= set(
        selected
    ):
        raise ValueError("Distinct objectives must include mean_abs_baseline_delta and spatial_tv")
    _exact(
        payload["limits"],
        {"max_frame_mean_abs_delta", "max_frame_temporal_change_distortion"},
        "limits",
    )
    result["limits"] = {k: _number(v, 0, 1e6, k) for k, v in payload["limits"].items()}
    if version_two:
        result["temporal_test_id"] = _identity(payload["temporal_test_id"], "temporal_test_id")
        temporal_objectives = payload["temporal_objectives"]
        if not isinstance(temporal_objectives, list) or not 1 <= len(temporal_objectives) <= 4:
            raise ValueError("Temporal comparison needs one to four explicit truth objectives")
        if any(not isinstance(item, dict) for item in temporal_objectives):
            raise ValueError("Temporal objectives must be exact metric/direction mappings")
        for item in temporal_objectives:
            _exact(item, {"metric", "direction"}, "temporal objective")
            if item["metric"] not in OBJECTIVE_METRICS or item["direction"] != "minimize":
                raise ValueError("Unknown temporal objective or direction")
        if (len({item["metric"] for item in temporal_objectives}) != len(temporal_objectives)
                or "signal_change_rms_error" not in {
                    item["metric"] for item in temporal_objectives
                }):
            raise ValueError("Distinct temporal objectives must include actual signal-change error")
        constraints = payload["temporal_constraints"]
        if not isinstance(constraints, list) or not 1 <= len(constraints) <= 8:
            raise ValueError("Temporal comparison needs one to eight explicit truth constraints")
        normalized_constraints = []
        for item in constraints:
            _exact(item, {"metric", "operator", "value", "unit"}, "temporal constraint")
            if item["metric"] not in METRIC_UNITS or item["unit"] != METRIC_UNITS[item["metric"]]:
                raise ValueError("Temporal constraint metric or unit is not declared")
            if item["metric"] == "signal_amplitude_retention" or item["operator"] != "<=":
                raise ValueError("Temporal constraint direction differs from its metric")
            normalized_constraints.append({**item, "value": _number(
                item["value"], 0, 1e15, "temporal constraint value"
            )})
        if (len({item["metric"] for item in constraints}) != len(constraints)
                or not {"truth_code_mae", "signal_amplitude_error", "signal_change_rms_error"}
                <= {item["metric"] for item in constraints}):
            raise ValueError(
                "Temporal limits must bound absolute code bias and actual signal error"
            )
        result["temporal_objectives"] = deepcopy(temporal_objectives)
        result["temporal_constraints"] = normalized_constraints
    return result


def _roi(shape, roi):
    _exact(roi, {"row_start", "row_end", "column_start", "column_end"}, "roi")
    if any(type(v) is not int or not 0 <= v <= 4096 for v in roi.values()):
        raise ValueError("ROI requires bounded integer coordinates")
    if any(roi[k + "_end"] - roi[k + "_start"] < 2 for k in ("row", "column")):
        raise ValueError("ROI requires at least two by two pixels")
    rows, columns = shape
    if roi["row_end"] > rows or roi["column_end"] > columns:
        raise ValueError("Comparison ROI exceeds the original native raster")
    return (slice(roi["row_start"], roi["row_end"]), slice(roi["column_start"], roi["column_end"]))


def _metric(value, unit="normalized_code_rgb", reason=None):
    return {
        "value": value,
        "unit": unit,
        "status": "unknown" if value is None else "ok",
        "reason": reason,
    }


def measure(candidate, baseline, roi):
    """Per-frame RGB-component errors on fixed support, with no time/channel cancellation."""
    a, b = np.asarray(candidate), np.asarray(baseline)
    if (
        a.dtype.kind != "f"
        or b.dtype.kind != "f"
        or a.shape != b.shape
        or a.ndim != 4
        or a.shape[-1] != 3
        or not 1 <= a.shape[0] <= 16
        or not np.isfinite(a).all()
        or not np.isfinite(b).all()
    ):
        raise ValueError("Metrics require equal finite floating [frame,row,column,RGB] arrays")
    rows, columns = _roi(a.shape[1:3], roi)
    x, y = a[:, rows, columns], b[:, rows, columns]
    results = []
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        for frame in range(len(a)):
            delta = x[frame] - y[frame]
            absolute = np.abs(delta)
            scale = float(absolute.max())
            rms = 0.0 if scale == 0 else scale * float(np.sqrt(np.mean(np.square(delta / scale))))
            variation = (
                np.abs(np.diff(x[frame], axis=0)).sum() + np.abs(np.diff(x[frame], axis=1)).sum()
            )
            edges = 3 * ((x.shape[1] - 1) * x.shape[2] + x.shape[1] * (x.shape[2] - 1))
            temporal = (
                None
                if frame == 0
                else float(np.mean(np.abs((x[frame] - x[frame - 1]) - (y[frame] - y[frame - 1]))))
            )
            values = {
                "mean_abs_baseline_delta": _metric(float(absolute.mean())),
                "rms_baseline_delta": _metric(rms),
                "max_abs_baseline_delta": _metric(scale),
                "display_clipping_fraction": _metric(
                    float(np.mean((x[frame] < 0) | (x[frame] > 1))), "1"
                ),
                "spatial_tv": _metric(float(variation / edges)),
                "temporal_change_distortion": _metric(
                    temporal, reason="No preceding selected frame" if frame == 0 else None
                ),
            }
            if any(
                v["value"] is not None and not math.isfinite(v["value"]) for v in values.values()
            ):
                raise ValueError("Comparison metric overflowed")
            results.append({"frame_index": frame, "metrics": values})
    worst = {}
    for key in METRICS:
        supported = results[1:] if key == "temporal_change_distortion" else results
        records = [row["metrics"][key] for row in supported]
        values = [r["value"] for r in records]
        worst[key] = _metric(
            max(values) if values and all(v is not None for v in values) else None,
            "1" if key == "display_clipping_fraction" else "normalized_code_rgb",
            None
            if values and all(v is not None for v in values)
            else "No defined complete frame support",
        )
    return {
        "schema": "camera_retained_code_metrics_v1",
        "per_frame": results,
        "worst_frame": worst,
        "roi": deepcopy(roi),
        "rgb_component_samples_per_frame": int(x[0].size),
        "input_stage": "pre_tone_code_rgb_frames",
        "reducer": "explicit_max_over_frames",
        "spatial_tv_meaning": (
            "mean absolute neighboring code-RGB difference; not scene detail or noise"
        ),
        "baseline_meaning": "same saved RAW with three filters disabled; not independent truth",
        **deepcopy(AUTHORITY),
    }


def rank(candidates, objectives, limits):
    """Feasibility first; Pareto membership is explicit and never chooses a winner."""
    result = []
    for row in candidates:
        worst = row["metrics"]["worst_frame"]
        constraints = []
        for key, metric in (
            ("max_frame_mean_abs_delta", "mean_abs_baseline_delta"),
            ("max_frame_temporal_change_distortion", "temporal_change_distortion"),
        ):
            value = worst[metric]["value"]
            constraints.append(
                {
                    "metric": metric,
                    "operator": "<=",
                    "limit": limits[key],
                    "value": value,
                    "status": "unknown"
                    if value is None
                    else "pass"
                    if value <= limits[key]
                    else "fail",
                }
            )
        known = all(worst[o["metric"]]["value"] is not None for o in objectives)
        feasible = known and all(c["status"] == "pass" for c in constraints)
        result.append(
            {
                "candidate_id": row["candidate_id"],
                "constraints": constraints,
                "feasible": feasible,
                "objective_values": {o["metric"]: worst[o["metric"]]["value"] for o in objectives},
                "pareto": False,
                "dominated_by": [],
            }
        )
    for row in result:
        if not row["feasible"]:
            continue
        values = list(row["objective_values"].values())
        for other in result:
            if other is row or not other["feasible"]:
                continue
            comparison = list(other["objective_values"].values())
            if all(a <= b for a, b in zip(comparison, values, strict=True)) and any(
                a < b for a, b in zip(comparison, values, strict=True)
            ):
                row["dominated_by"].append(other["candidate_id"])
        row["pareto"] = not row["dominated_by"]
    return {
        "candidates": result,
        "pareto_candidate_ids": [r["candidate_id"] for r in result if r["pareto"]],
        "selected_candidate_id": None,
        "objectives": deepcopy(objectives),
        **deepcopy(AUTHORITY),
    }


def rank_with_temporal_truth(candidates, specification):
    """Pareto over evaluated v2 metrics; unknown truth never enters dominance."""
    legacy = rank(candidates, specification["objectives"], specification["limits"])
    result = deepcopy(legacy["candidates"])
    objectives = [*specification["objectives"], *specification["temporal_objectives"]]
    for row, candidate in zip(result, candidates, strict=True):
        temporal = candidate["temporal_metrics"]
        checks = []
        for limit in specification["temporal_constraints"]:
            metric = temporal["metrics"][limit["metric"]]
            value = metric["value"]
            status = ("unknown" if metric["status"] != "ok" else
                      "pass" if (value <= limit["value"] if limit["operator"] == "<="
                                 else value >= limit["value"]) else "fail")
            checks.append({**deepcopy(limit), "observed_value": value, "status": status})
        row["temporal_constraints"] = checks
        row["objective_values"].update({
            item["metric"]: temporal["metrics"][item["metric"]]["value"]
            for item in specification["temporal_objectives"]
        })
        objective_unknown = any(row["objective_values"][item["metric"]] is None
                                for item in objectives)
        all_checks = [*row["constraints"], *checks]
        definite_fail = any(check["status"] == "fail" for check in all_checks)
        unknown = (temporal["support_status"] != "ok" or objective_unknown or
                   any(check["status"] == "unknown" for check in all_checks))
        row["feasibility"] = ("infeasible" if definite_fail else
                              "unknown" if unknown else "feasible")
        row["feasible"] = row["feasibility"] == "feasible"
        row["pareto"] = False
        row["dominated_by"] = []
    for row in result:
        if not row["feasible"]:
            continue
        values = [row["objective_values"][o["metric"]] *
                  (-1 if o["direction"] == "maximize" else 1) for o in objectives]
        for other in result:
            if other is row or not other["feasible"]:
                continue
            compared = [other["objective_values"][o["metric"]] *
                        (-1 if o["direction"] == "maximize" else 1) for o in objectives]
            if all(a <= b for a, b in zip(compared, values, strict=True)) and any(
                a < b for a, b in zip(compared, values, strict=True)
            ):
                row["dominated_by"].append(other["candidate_id"])
        row["pareto"] = not row["dominated_by"]
    return {
        "schema": "camera_retained_code_truth_ranking_v1",
        "candidates": result,
        "pareto_candidate_ids": [r["candidate_id"] for r in result if r["pareto"]],
        "excluded_candidate_ids": [r["candidate_id"] for r in result if not r["feasible"]],
        "selected_candidate_id": None,
        "objectives": deepcopy(objectives),
        "pareto_scope": "feasible_evaluated_samples_only_no_physical_or_automatic_winner",
        "pareto_provisional": any(r["feasibility"] == "unknown" for r in result),
        **deepcopy(AUTHORITY),
    }


def _artifacts(value):
    """Preserve semantic aliases as distinct ZIP members, even for equal bytes."""
    entries = {}

    def visit(item):
        if isinstance(item, dict):
            if {"sha256", "name", "bytes"} <= item.keys():
                sha, name, size = item["sha256"], item["name"], item["bytes"]
                if (
                    not isinstance(sha, str)
                    or len(sha) != 64
                    or any(c not in "0123456789abcdef" for c in sha)
                    or not isinstance(name, str)
                    or not name
                    or len(name.encode()) > 255
                    or Path(name).name != name
                    or name in (".", "..")
                    or "\\" in name
                    or type(size) is not int
                    or not 0 <= size <= MAX_ARCHIVE_BYTES
                ):
                    raise IntegrityError("Invalid comparison artifact identity")
                key = (sha, name)
                if key in entries and entries[key]["bytes"] != size:
                    raise IntegrityError("Artifact aliases disagree about byte length")
                entries[key] = deepcopy(item)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return [entries[k] for k in sorted(entries)]


def _verify_artifacts(service, artifacts):
    checked = {}
    for artifact in artifacts:
        sha, size = artifact["sha256"], artifact["bytes"]
        if sha in checked:
            if checked[sha] != size:
                raise IntegrityError("Artifact identity has conflicting lengths")
            continue
        path = service.registry.blob_path(sha)
        hasher = hashlib.sha256()
        count = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                count += len(chunk)
                hasher.update(chunk)
        if count != size or hasher.hexdigest() != sha:
            raise IntegrityError("Comparison artifact bytes differ from their immutable identity")
        checked[sha] = size


def _profiles(profile, spec):
    from .retained_isp import validate_profile

    for axis in spec["axes"]:
        if profile[axis["parameter"].split(".")[0]]["mode"] == "bypass":
            raise ValueError(
                "A swept filter axis requires its registered profile mode to be active"
            )
    result = []
    for index, values in enumerate(itertools.product(*(a["values"] for a in spec["axes"]))):
        candidate, coordinates = deepcopy(profile), {}
        for axis, value in zip(spec["axes"], values, strict=True):
            group, key = axis["parameter"].split(".")
            candidate[group][key] = value
            coordinates[axis["parameter"]] = value
        candidate = validate_profile(candidate)
        result.append(
            {
                "candidate_id": f"candidate-{index:03d}",
                "coordinates": coordinates,
                "profile": candidate,
                "profile_hash": digest(candidate),
            }
        )
    return result


def _process_kwargs(source):
    return {
        "geometry": source["geometry"],
        "frame_records": source["frame_records"],
        "events": source["retained_charge_events"],
    }


def _acquisition_summary_fields(source):
    """Retain realized AE evidence separately from the processor's RAW inputs.

    Existing recorded-control comparison summaries remain byte-compatible.
    """
    if source.get("acquisition_mode") != "closed_loop":
        return {}
    return {
        key: deepcopy(source[key])
        for key in (
            "source_plugin_id", "acquisition_mode", "planning_contract", "realized_acquisition",
            "control_trace", "control_trace_role",
            "frame_conditions", "clock", "timing", "retained_charge_events",
            "acquisition_identity", "source_provenance", "processing_contract",
        )
    }


def _prepare(service, payload, *, historical_environment=None):
    from .retained_isp import bypass_profile, validate_inputs
    from .retained_isp_replay import prepare
    from .retained_isp_source import load_inputs

    specification = validate_specification(payload)
    base = prepare(
        service,
        {
            "schema": "camera_retained_code_isp_replay_v1",
            "name": "Comparison source preparation",
            "source_kind": "run",
            **{k: specification[k] for k in ("source_id", "selector", "isp_model_id")},
        },
    )
    if historical_environment is not None:
        base["environment"] = deepcopy(historical_environment)
        # Replay forecast includes the size of its own environment. Recalculate it
        # below after replacing that field to preserve historical byte forecasts.
    source = base["source"]
    raw = load_inputs(service, source)
    _roi(raw.shape[1:], specification["roi"])
    temporal_test = temporal_reference = None
    if specification["schema"] == SCHEMA_V2:
        from .temporal_signal_reference import validate_registered_test

        temporal_test = service.registry.get("tests", specification["temporal_test_id"])
        temporal_reference = validate_registered_test(service, temporal_test)
        signal_limit = next(limit["value"] for limit in specification["temporal_constraints"]
                            if limit["metric"] == "signal_change_rms_error")
        if signal_limit > temporal_test["absolute_tolerance"]["value"]:
            raise ValueError(
                "Candidate signal-error limit exceeds the frozen temporal Test tolerance"
            )
        binding = temporal_reference["source_binding"]
        if (binding["source_id"] != specification["source_id"]
                or binding["selector"] != specification["selector"]
                or binding["isp_model_id"] != specification["isp_model_id"]
                or binding["raw_sha256"] != source["raw_sha256"]
                or binding["selected_array_sha256"] != source["selected_array_sha256"]
                or binding["source_snapshot_sha256"] != digest(source["source_snapshot"])
                or temporal_reference["roi"] != specification["roi"]):
            raise ValueError("Temporal Test does not pin this exact receiver RAW, ISP and ROI")
    baseline = bypass_profile(base["profile"])
    for parameter in AXES:
        group, key = parameter.split(".")
        baseline[group][key] = 0.0
    candidates = _profiles(base["profile"], specification)
    resources = [
        validate_inputs(raw, source["raw_format"], p, **_process_kwargs(source))["resources"]
        for p in [baseline, *(c["profile"] for c in candidates)]
    ]
    if historical_environment is not None:
        from .retained_isp_replay import forecast

        replay_body = {k: v for k, v in base.items() if k != "evidence_forecast"}
        base["evidence_forecast"] = forecast(service, replay_body, resources[0], raw.nbytes)
    original_export = service.export_run(specification["source_id"])
    artifacts = _artifacts([original_export, base["input_artifacts"],
                            temporal_reference["artifacts"] if temporal_reference else []])
    _verify_artifacts(service, artifacts)
    frozen = {
        "job_kind": JOB_KIND,
        "plugin_id": PLUGIN_ID_V2 if temporal_reference else PLUGIN_ID,
        **{k: specification[k] for k in ("name", "source_id", "selector", "isp_model_id")},
        "specification": specification,
        "base_replay": base,
        "source_export": original_export,
        "input_artifacts": artifacts,
        "baseline_profile": baseline,
        "candidates": candidates,
        "environment": deepcopy(base["environment"]),
    }
    if temporal_reference is not None:
        frozen["temporal_test_snapshot"] = deepcopy(temporal_test)
        frozen["temporal_reference_snapshot"] = deepcopy(temporal_reference)
    cases = 1 + len(candidates)
    one_array = int(raw.size) * 3 * 8
    stored_arrays = 4 if temporal_reference else 2
    output = cases * stored_arrays * (one_array + NPY_HEADER_BYTES_LIMIT)
    frames, rows, columns = raw.shape
    png_bytes = cases * frames * (rows * (columns * 3 + 1) + rows * columns // 100 + 4096)
    source_bytes = sum(a["bytes"] for a in artifacts)
    copied_summary_bytes = len(
        canonical(
            {
                "frame_records": source["frame_records"],
                "raw_format": source["raw_format"],
                "geometry": source["geometry"],
                "profiles": [baseline, *(c["profile"] for c in candidates)],
                **_acquisition_summary_fields(source),
                **({"temporal_test_id": temporal_test["id"],
                    "temporal_reference_id": temporal_reference["id"]}
                   if temporal_reference else {}),
            }
        ).encode()
    )
    summary_limit = copied_summary_bytes + MAX_GENERATED_SUMMARY_BYTES
    # Preserve the baseline while processing one candidate, plus input and NPY
    # serialization buffers. All candidate arrays are stored sequentially.
    working = (max(r["working_array_bytes_bound"] for r in resources)
               + 3 * one_array + raw.nbytes
               + (temporal_reference["array_artifact"]["bytes"] if temporal_reference else 0))
    manifest = 4 * len(canonical(frozen).encode()) + 4 * summary_limit + MAX_MANIFEST_OVERHEAD_BYTES
    # ZIP_STORED is used: include local/central filename headers for each member.
    zip_overhead = (len(artifacts) + cases * (stored_arrays + frames) + 3) * 4096 + 65536
    archive = source_bytes + output + png_bytes + summary_limit + manifest + zip_overhead
    if (
        one_array + NPY_HEADER_BYTES_LIMIT > MAX_ARRAY_BYTES
        or working > MAX_WORKING_BYTES
        or archive > MAX_ARCHIVE_BYTES
    ):
        raise ValueError(
            "Comparison full source/output closure exceeds bounded memory/archive budget"
        )
    cost = {
        "schema": "camera_retained_code_comparison_budget_v1",
        "candidate_count": len(candidates),
        "processor_invocations": cases,
        "selected_raw_bytes": int(raw.nbytes),
        "source_artifact_bytes": source_bytes,
        "output_array_bytes_bound": output,
        "png_bytes_bound": png_bytes,
        "max_preview_count": cases * frames,
        "working_array_bytes_bound": working,
        "summary_bytes_limit": summary_limit,
        "copied_summary_bytes": copied_summary_bytes,
        "generated_summary_bytes_limit": MAX_GENERATED_SUMMARY_BYTES,
        "logical_manifest_bytes_bound": manifest,
        "zip_overhead_bytes_bound": zip_overhead,
        "archive_bytes_bound": archive,
        "archive_bytes_limit": MAX_ARCHIVE_BYTES,
        "stored_arrays_per_case": stored_arrays,
        "processor_peak_bytes_bound": max(r["working_array_bytes_bound"] for r in resources),
    }
    frozen["cost"] = cost
    return frozen


def plan(service, payload):
    frozen = _prepare(service, payload)
    return {"plan_hash": digest(frozen), "frozen_request": frozen, "cost": deepcopy(frozen["cost"])}


def submit(service, payload, plan_hash):
    proposal = plan(service, payload)
    if not isinstance(plan_hash, str) or plan_hash != proposal["plan_hash"]:
        raise ConflictError("Comparison source/profile/specification changed after planning")
    document = {**proposal["frozen_request"], "plan_hash": plan_hash}
    job = service.registry.submit(document, digest(document), force=payload.get("force", False))
    return verify(service, job)


def verify(service, job, current_environment=False):
    if (job.get("job_kind") != JOB_KIND
            or job.get("plugin_id") not in (PLUGIN_ID, PLUGIN_ID_V2)
            or job.get("plugin_id") != (PLUGIN_ID_V2 if job.get("specification", {}).get("schema")
                                        == SCHEMA_V2 else PLUGIN_ID)):
        raise ValueError("Job is not a retained code ISP comparison")
    fresh = _prepare(
        service,
        job["specification"],
        historical_environment=None if current_environment else job["environment"],
    )
    for key, value in fresh.items():
        if canonical(job.get(key)) != canonical(value):
            raise IntegrityError("Frozen retained ISP comparison differs: " + key)
    if digest(fresh) != job.get("plan_hash"):
        raise IntegrityError("Comparison plan hash differs from its immutable request")
    result = job.get("result")
    if result is not None:
        artifacts = _artifacts(result["artifacts"])
        _verify_artifacts(service, artifacts)
        summary = result["summary"]
        if (
            summary.get("plan_hash") != job["plan_hash"]
            or summary.get("selected_array_sha256")
            != fresh["base_replay"]["source"]["selected_array_sha256"]
        ):
            raise IntegrityError("Comparison result lost its source/plan binding")
        source = fresh["base_replay"]["source"]
        expected_source = {
            "source_id": job["source_id"],
            "selector": job["selector"],
            **{key: source[key] for key in (
                "raw_sha256", "raw_format", "geometry", "frame_records",
            )},
            **_acquisition_summary_fields(source),
        }
        for key, value in expected_source.items():
            if canonical(summary.get(key)) != canonical(value):
                raise IntegrityError("Comparison actual acquisition binding differs: " + key)
        expected_cases = [
            {
                "candidate_id": "baseline",
                "profile": job["baseline_profile"],
                "profile_hash": digest(job["baseline_profile"]),
                "coordinates": {},
            }
        ] + job["candidates"]
        saved_cases = [summary["baseline"], *summary["candidates"]]
        if len(saved_cases) != len(expected_cases):
            raise IntegrityError("Comparison result has a different candidate count")
        for expected, saved in zip(expected_cases, saved_cases, strict=True):
            if any(canonical(saved.get(k)) != canonical(v) for k, v in expected.items()):
                raise IntegrityError("Comparison result candidate differs from its planned profile")
            if canonical(saved["metrics"]["roi"]) != canonical(job["specification"]["roi"]):
                raise IntegrityError("Comparison result metric support differs")
            if job["plugin_id"] == PLUGIN_ID_V2:
                if (saved.get("temporal_metrics", {}).get("roi") != job["specification"]["roi"]
                        or "ccm_code_rgb_frames" not in saved.get("arrays", {})):
                    raise IntegrityError("Temporal comparison lost fixed-color evidence")
        if job["plugin_id"] == PLUGIN_ID_V2:
            reference = job["temporal_reference_snapshot"]
            test = job["temporal_test_snapshot"]
            temporal_summary = {
                "test_id": test["id"],
                "test_content_hash": test["content_hash"],
                "reference_id": reference["id"],
                "reference_content_hash": reference["content_hash"],
                "reference_sha256": reference["array_artifact"]["sha256"],
                "source_binding": reference["source_binding"],
                "truth_stage": "ccm_code_rgb_frames",
                "candidate_stage": "pre_tone_code_rgb_frames",
                "stage_mapping": "fixed_color_truth_to_post_detail_pre_tone_candidate",
                "processor_truth_arrays_used": False,
                "claim_scope": "authored_synthetic_software_validation",
            }
            if summary.get("temporal_validation") != temporal_summary:
                raise IntegrityError("Temporal result differs from frozen Test/reference")
            expected_rank = rank_with_temporal_truth(summary["candidates"], job["specification"])
        else:
            expected_rank = rank(
                summary["candidates"],
                job["specification"]["objectives"],
                job["specification"]["limits"],
            )
        if canonical(summary["ranking"]) != canonical(expected_rank):
            raise IntegrityError(
                "Comparison ranking differs from its declared constraints/objectives"
            )
        summaries = [a for a in artifacts if a["name"] == "summary.json"]
        if (
            len(summaries) != 1
            or service.registry.blob_path(summaries[0]["sha256"]).read_bytes()
            != canonical(summary).encode()
        ):
            raise IntegrityError("Comparison summary differs from its saved bytes")
        required = _artifacts([summary["baseline"], summary["candidates"]])
        if not {(a["sha256"], a["name"]) for a in required} <= {
            (a["sha256"], a["name"]) for a in artifacts
        }:
            raise IntegrityError("Comparison result is missing an actual candidate array")
    return job


def execute(service, job):
    from PIL import Image

    from .isp_studies import _store_array
    from .retained_isp import process_raw
    from .retained_isp_source import load_inputs

    verify(service, job, current_environment=True)
    source = job["base_replay"]["source"]
    raw = load_inputs(service, source)
    raw_hash = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    artifacts, cases, previews, baseline_array = [], [], [], None
    array_bytes = png_bytes = 0
    profiles = [
        {
            "candidate_id": "baseline",
            "profile": job["baseline_profile"],
            "profile_hash": digest(job["baseline_profile"]),
            "coordinates": {},
        }
    ] + job["candidates"]
    for candidate in profiles:
        output = process_raw(
            raw, source["raw_format"], candidate["profile"], **_process_kwargs(source)
        )
        arrays = output["arrays"]
        actual = arrays["pre_tone_code_rgb_frames"]
        if baseline_array is None:
            baseline_array = actual.copy()
        metrics = measure(actual, baseline_array, job["specification"]["roi"])
        stored = {}
        keys = ["pre_tone_code_rgb_frames", "rgb_frames"]
        if job["plugin_id"] == PLUGIN_ID_V2:
            keys += ["ccm_code_rgb_frames", "temporal_gate_mask_frames"]
        for key in keys:
            array = arrays[key]
            if (
                array.dtype != (np.dtype("bool") if key == "temporal_gate_mask_frames"
                                else np.dtype("float64"))
                or array.shape != (raw.shape if key == "temporal_gate_mask_frames"
                                  else (*raw.shape, 3))
                or not np.isfinite(array).all()
            ):
                raise IntegrityError("Comparison processor output violates its frozen shape/type")
            artifact = _store_array(service, array, candidate["candidate_id"] + "-" + key)
            if artifact["bytes"] > array.nbytes + NPY_HEADER_BYTES_LIMIT:
                raise IntegrityError("Comparison NPY header exceeded its declared bound")
            artifacts.append(artifact)
            array_bytes += artifact["bytes"]
            stored[key] = artifact
        preview_frames = []
        for index, pixels in enumerate(arrays["rgb_frames"]):
            stream = io.BytesIO()
            Image.fromarray((np.clip(pixels, 0, 1) * 255).astype(np.uint8)).save(stream, "PNG")
            artifact = service.registry.store_blob(
                stream.getvalue(), candidate["candidate_id"] + f"-rgb-{index:03d}.png", "image/png"
            )
            artifacts.append(artifact)
            png_bytes += artifact["bytes"]
            preview_frames.append(
                {
                    "frame_id": source["frame_records"][index]["frame_id"],
                    "frame_index": index,
                    "preview_artifact": artifact["name"],
                    "preview_url": artifact["url"],
                    "preview_artifact_id": artifact["id"],
                    "display_transfer": "fixed_0_to_1_no_autoscale",
                }
            )
        previews.append(
            {
                "id": candidate["candidate_id"],
                "label": candidate["candidate_id"],
                "frames": preview_frames,
            }
        )
        cases.append(
            {
                **deepcopy(candidate),
                "metrics": metrics,
                "arrays": stored,
                "processor_summary": deepcopy(output["summary"]),
            }
        )
        del output, arrays, actual
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != raw_hash:
        raise IntegrityError("Comparison processor mutated the selected original RAW")
    if job["plugin_id"] == PLUGIN_ID_V2:
        # Preflight verified reference bytes earlier, but only this pass uses
        # truth numerically. No truth array enters process_raw or its arguments.
        from .temporal_signal_metrics import measure as measure_truth
        from .temporal_signal_reference import load_truth

        reference = job["temporal_reference_snapshot"]
        truth = load_truth(service, reference)
        for case in cases:
            evaluated = {}
            for key in ("ccm_code_rgb_frames", "pre_tone_code_rgb_frames",
                        "temporal_gate_mask_frames"):
                descriptor = case["arrays"][key]
                _verify_artifacts(service, [descriptor])
                value = np.load(
                    io.BytesIO(service.registry.blob_path(descriptor["sha256"]).read_bytes()),
                    allow_pickle=False,
                )
                expected_shape = (raw.shape if key == "temporal_gate_mask_frames"
                                  else (*raw.shape, 3))
                expected_type = (np.dtype("bool") if key == "temporal_gate_mask_frames"
                                 else np.dtype("float64"))
                if value.shape != expected_shape or value.dtype != expected_type:
                    raise IntegrityError(
                        "Stored temporal candidate array differs from processor output"
                    )
                evaluated[key] = value
            case["temporal_metrics"] = measure_truth(
                evaluated["pre_tone_code_rgb_frames"], truth, raw, source["raw_format"],
                evaluated["temporal_gate_mask_frames"], case["profile"],
                reference["row_transfer_ns"], reference,
                pre_tone=evaluated["pre_tone_code_rgb_frames"],
            )
    if (
        array_bytes > job["cost"]["output_array_bytes_bound"]
        or png_bytes > job["cost"]["png_bytes_bound"]
    ):
        raise IntegrityError("Comparison outputs exceed their prequeue byte forecast")
    summary = {
        "schema": "camera_retained_code_comparison_result_v1",
        "job_kind": JOB_KIND,
        "plugin_id": job["plugin_id"],
        "plan_hash": job["plan_hash"],
        "source_id": job["source_id"],
        "selector": deepcopy(job["selector"]),
        "isp_model_id": job["isp_model_id"],
        "raw_sha256": source["raw_sha256"],
        "selected_array_sha256": source["selected_array_sha256"],
        "selected_array_hash_encoding": source.get("selected_array_hash_encoding"),
        "raw_format": deepcopy(source["raw_format"]),
        "geometry": deepcopy(source["geometry"]),
        "frame_records": deepcopy(source["frame_records"]),
        **_acquisition_summary_fields(source),
        "baseline": cases[0],
        "candidates": cases[1:],
        "preview_groups": previews,
        "roi": deepcopy(job["specification"]["roi"]),
        "ranking": (rank_with_temporal_truth(cases[1:], job["specification"])
                    if job["plugin_id"] == PLUGIN_ID_V2 else
                    rank(cases[1:], job["specification"]["objectives"],
                         job["specification"]["limits"])),
        "cost": deepcopy(job["cost"]),
        "processing_domain": "delivered_signal_code_domain",
        "source_snapshot_role": "evidence_only_not_processing_inputs",
        **deepcopy(AUTHORITY),
    }
    if job["plugin_id"] == PLUGIN_ID_V2:
        reference = job["temporal_reference_snapshot"]
        test = job["temporal_test_snapshot"]
        summary["schema"] = "camera_retained_code_comparison_result_v2"
        summary["temporal_validation"] = {
            "test_id": test["id"],
            "test_content_hash": test["content_hash"],
            "reference_id": reference["id"],
            "reference_content_hash": reference["content_hash"],
            "reference_sha256": reference["array_artifact"]["sha256"],
            "source_binding": reference["source_binding"],
            "truth_stage": "ccm_code_rgb_frames",
            "candidate_stage": "pre_tone_code_rgb_frames",
            "stage_mapping": "fixed_color_truth_to_post_detail_pre_tone_candidate",
            "processor_truth_arrays_used": False,
            "claim_scope": "authored_synthetic_software_validation",
        }
    content = canonical(summary).encode()
    if len(content) > job["cost"]["summary_bytes_limit"]:
        raise IntegrityError("Comparison summary exceeded its prequeue bound")
    artifacts.append(service.registry.store_blob(content, "summary.json", "application/json"))
    return {"summary": summary, "artifacts": artifacts, "physical_validation": "not_established"}


def list_jobs(service):
    return [
        verify(service, j) for j in service.registry.list_runs() if j.get("job_kind") == JOB_KIND
    ]


def get(service, identity):
    return verify(service, service.registry.get_run(identity))


def cancel(service, identity):
    get(service, identity)
    return service.registry.cancel(identity)


def export_manifest(service, identity):
    job = get(service, identity)
    artifacts = _artifacts([job["input_artifacts"], job.get("artifacts", []), job.get("result")])
    _verify_artifacts(service, artifacts)
    manifest = {
        "schema": "camera_retained_code_comparison_export_v1",
        "comparison": job,
        "artifacts": artifacts,
        "replay": {
            "specification": job["specification"],
            "plan_hash": job["plan_hash"],
            "instruction": (
                "Replan against the original retained run and registered ISP model; "
                "run the complete selected frame sequence from empty ISP state."
            ),
        },
        **deepcopy(AUTHORITY),
    }
    if len(canonical(manifest).encode()) > job["cost"]["logical_manifest_bytes_bound"]:
        raise IntegrityError("Comparison logical export exceeds its prequeue byte forecast")
    return manifest


def export_bundle(service, identity):
    manifest = export_manifest(service, identity)
    directory = service.registry.directory / "downloads"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="retained-comparison-", suffix=".zip", dir=directory, delete=False
    ) as stream:
        path = Path(stream.name)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", canonical(manifest).encode())
            archive.writestr(
                "README.txt",
                "Authored code-domain comparison. Baseline is not truth; "
                "no physical camera validation or automatic promotion.\n",
            )
            for artifact in manifest["artifacts"]:
                archive.write(
                    service.registry.blob_path(artifact["sha256"]),
                    f"artifacts/{artifact['sha256']}/{artifact['name']}",
                )
        if path.stat().st_size > manifest["comparison"]["cost"]["archive_bytes_bound"]:
            raise IntegrityError("Comparison ZIP exceeds its full prequeue byte forecast")
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise
