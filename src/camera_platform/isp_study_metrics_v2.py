"""Pure, fixed-support ISP comparisons; no fitting, physical ranking or winner.

RGB inputs are signed normalized linear values before tone. Reference RGB uses
the same coordinate system, common WB/CCM and normalization. electron_equivalent is an
arithmetic RGB guide multiplied by fixed normalization_e, not photometric luminance.
"""

from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from numbers import Real

import numpy as np

VERSION = "camera_isp_study_metrics_v2"
ELECTRON = "electron_equivalent"
METRICS = {
    "temporal_residual_std_e": {
        "unit": ELECTRON,
        "definition": "sqrt(mean_pixel(sum_frame((guide-mean_frame(guide))^2)/(F-1)))",
    },
    "mean_bias_e": {
        "unit": ELECTRON,
        "definition": "mean_pixel(mean_frame(guide)-independent_reference_guide)",
    },
    "mean_abs_bias_e": {
        "unit": ELECTRON,
        "definition": "mean_pixel(abs(mean_frame(guide)-independent_reference_guide))",
    },
    "mean_baseline_delta_e": {
        "unit": ELECTRON,
        "definition": "mean_pixel(mean_frame(guide-baseline_guide)); not truth bias",
    },
    "mean_abs_baseline_delta_e": {
        "unit": ELECTRON,
        "definition": "mean_pixel(abs(mean_frame(guide-baseline_guide))); not truth bias",
    },
    "edge_overshoot_e": {
        "unit": ELECTRON,
        "definition": "max(0, max(averaged_edge_band)-reference_high_plateau)",
    },
    "edge_undershoot_e": {
        "unit": ELECTRON,
        "definition": "max(0, reference_low_plateau-min(averaged_edge_band))",
    },
    "texture_amplitude_e": {
        "unit": ELECTRON,
        "definition": "hypot(sin_coefficient,cos_coefficient) of fixed harmonic fit",
    },
    "texture_reference_amplitude_e": {
        "unit": ELECTRON,
        "definition": "same fixed harmonic fit on independent reference guide",
    },
    "texture_amplitude_ratio": {
        "unit": "1",
        "definition": "output harmonic amplitude / independent reference amplitude",
    },
    "texture_attenuation_fraction": {
        "unit": "1",
        "definition": "1-amplitude_ratio; negative means amplitude boost",
    },
}


def catalog():
    return {"schema": VERSION, "metrics": deepcopy(METRICS)}


def _number(value, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(name + " must be a real finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(name + " must be representable as a finite float") from error
    if not math.isfinite(result):
        raise ValueError(name + " must be a real finite number")
    return result


def _integer(value, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ValueError(name + " must be an integer")
    return int(value)


def _keys(value, required, optional=(), *, name):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        raise ValueError(name + " has missing required fields")
    if value.keys() - set(required) - set(optional):
        raise ValueError(name + " has unsupported fields")


def validate_dataset(dataset, shape):
    """Coordinates are local native-raster centers; version is explicit."""
    if not isinstance(dataset, dict) or dataset.get("schema") != "camera_isp_metric_spec_v2":
        raise ValueError("Charge metrics require explicit camera_isp_metric_spec_v2")
    dataset = {k: v for k, v in dataset.items() if k != "schema"}
    _keys(dataset, {"kind", "stationary", "roi"}, {"texture", "edge"}, name="dataset")
    if len(shape) != 2 or any(_integer(v, "shape") <= 0 for v in shape):
        raise ValueError("shape must contain positive height and width")
    if dataset["kind"] not in {"flat", "edge", "texture"}:
        raise ValueError("Unsupported dataset kind")
    if type(dataset["stationary"]) is not bool:
        raise ValueError("stationary must be explicitly declared boolean")
    roi = dataset["roi"]
    _keys(roi, {"row_start", "row_end", "column_start", "column_end"}, name="roi")
    roi = {key: _integer(value, "roi." + key) for key, value in roi.items()}
    if not (
        0 <= roi["row_start"] < roi["row_end"] <= shape[0]
        and 0 <= roi["column_start"] < roi["column_end"] <= shape[1]
    ):
        raise ValueError("ROI must be a nonempty rectangle within the preserved raster")
    result = {
        "schema": "camera_isp_metric_spec_v2",
        "kind": dataset["kind"],
        "stationary": dataset["stationary"],
        "roi": roi,
        "texture": None,
        "edge": None,
    }
    for name in ("texture", "edge"):
        value = dataset.get(name)
        if dataset["kind"] != name:
            if value is not None:
                raise ValueError(name + " settings require the matching dataset kind")
            continue
        required = (
            {"direction", "period_px"}
            if name == "texture"
            else {"direction", "position_px", "half_width_px"}
        )
        _keys(value, required, name=name)
        if value["direction"] not in {"row", "column"}:
            raise ValueError(name + " direction must be row or column")
        parsed = {"direction": value["direction"]}
        for key in required - {"direction"}:
            parsed[key] = _number(value[key], name + "." + key)
        if name == "texture":
            if parsed["period_px"] <= 2:
                raise ValueError("Texture period must exceed two native pixels")
        else:
            start, end = roi[value["direction"] + "_start"], roi[value["direction"] + "_end"]
            position, width = parsed["position_px"], parsed["half_width_px"]
            if not (width > 0 and start < position - width < position + width < end):
                raise ValueError("Edge band must leave reference plateaus on both ROI sides")
            if not (
                math.ceil(position - width) < position + width
                and math.ceil(position + width) <= end - 1
            ):
                raise ValueError("Edge band and both reference plateaus need sampled pixel centers")
        result[name] = parsed
    return result


def _float_array(value, name, shape=None):
    result = np.asarray(value)
    if result.dtype.kind != "f" or not np.isfinite(result).all():
        raise ValueError(name + " must contain finite floating linear RGB values")
    if shape is not None and result.shape != shape:
        raise ValueError(name + " shape differs from the preserved RGB raster")
    with np.errstate(over="raise", invalid="raise"):
        try:
            return result.astype(np.float64, copy=False)
        except FloatingPointError as error:
            raise ValueError(name + " cannot be represented in float64") from error


def _bool_array(value, name, shape):
    result = np.asarray(value)
    if result.dtype.kind != "b" or result.shape != shape:
        raise ValueError(name + " must be a boolean array with the exact declared shape")
    return result


def _record(value, unit, reason=None):
    return {
        "value": None if value is None else _number(value, "metric value"),
        "unit": unit,
        "status": "unknown" if value is None else "ok",
        "reason": reason,
    }


def _guide(rgb, scale):
    # Divide before summation: do not create an avoidable channel-sum overflow.
    return np.sum(rgb / 3.0, axis=-1) * scale


def _texture(metrics, diagnostics, average, reference, mask, settings):
    y, x = np.indices(mask.shape)
    coordinate = y if settings["direction"] == "row" else x
    phase = (coordinate[mask] / settings["period_px"]) * (2 * np.pi)
    basis = np.column_stack([np.ones(phase.shape), np.sin(phase), np.cos(phase)])
    if len(phase) < 3 or np.linalg.matrix_rank(basis) < 3 or np.linalg.cond(basis) > 1e12:
        reason = "Fixed ROI does not resolve the declared harmonic (rank/conditioning)"
        for name in METRICS:
            if name.startswith("texture_"):
                metrics[name] = _record(None, METRICS[name]["unit"], reason)
        return
    observed = np.linalg.lstsq(basis, average[mask], rcond=None)[0]
    amplitude = float(np.hypot(observed[1], observed[2]))
    metrics["texture_amplitude_e"] = _record(amplitude, ELECTRON)
    diagnostics["texture"] = {
        **settings,
        "basis": "constant+sin+cos",
        "sample_count": int(mask.sum()),
        "condition_limit": 1e12,
    }
    if reference is None:
        return
    fitted = np.linalg.lstsq(basis, reference[mask], rcond=None)[0]
    expected = float(np.hypot(fitted[1], fitted[2]))
    # This is numerical resolvability, not a measurement uncertainty threshold.
    resolution = float(64 * np.finfo(np.float64).eps * float(np.max(np.abs(reference[mask]))))
    metrics["texture_reference_amplitude_e"] = _record(expected, ELECTRON)
    diagnostics["texture"]["reference_numerical_resolution_e"] = resolution
    if expected <= resolution:
        for name in ("texture_amplitude_ratio", "texture_attenuation_fraction"):
            metrics[name] = _record(None, "1", "Reference harmonic is numerically zero")
        return
    ratio = amplitude / expected
    metrics["texture_amplitude_ratio"] = _record(ratio, "1")
    metrics["texture_attenuation_fraction"] = _record(1 - ratio, "1")


def _edge(metrics, diagnostics, average, reference, mask, settings, roi):
    if reference is None:
        return
    direction = settings["direction"]
    reduce_axis = 1 if direction == "row" else 0
    coordinates = np.arange(mask.shape[1 - reduce_axis])
    selected = (coordinates >= roi[direction + "_start"]) & (coordinates < roi[direction + "_end"])
    counts = mask.sum(axis=reduce_axis)
    if np.any(counts[selected] == 0):
        reason = "Fixed edge ROI has a missing profile coordinate"
        for name in ("edge_overshoot_e", "edge_undershoot_e"):
            metrics[name] = _record(None, ELECTRON, reason)
        return
    profile = np.sum(np.where(mask, average, 0), axis=reduce_axis)[selected] / counts[selected]
    ref_profile = (
        np.sum(np.where(mask, reference, 0), axis=reduce_axis)[selected] / counts[selected]
    )
    coordinate = coordinates[selected]
    low_end = settings["position_px"] - settings["half_width_px"]
    high_start = settings["position_px"] + settings["half_width_px"]
    band = (coordinate >= low_end) & (coordinate < high_start)
    left, right = coordinate < low_end, coordinate >= high_start
    if not (band.any() and left.any() and right.any()):
        raise ValueError("Edge ROI must contain sampled band and both reference plateaus")
    a, b = float(ref_profile[left].mean()), float(ref_profile[right].mean())
    if a == b:
        reason = "Independent reference has zero edge contrast"
        for name in ("edge_overshoot_e", "edge_undershoot_e"):
            metrics[name] = _record(None, ELECTRON, reason)
        return
    low, high = min(a, b), max(a, b)
    metrics["edge_overshoot_e"] = _record(max(0.0, float(profile[band].max() - high)), ELECTRON)
    metrics["edge_undershoot_e"] = _record(max(0.0, float(low - profile[band].min())), ELECTRON)
    diagnostics["edge"] = {
        **settings,
        "coordinates_px": coordinate.tolist(),
        "mean_profile_e": profile.tolist(),
        "reference_profile_e": ref_profile.tolist(),
        "reference_low_e": low,
        "reference_high_e": high,
    }


def _masked_physical(value, valid, name, shape=None):
    """Finite arithmetic workspace; invalid physical samples remain counted by mask."""
    array = np.asarray(value)
    if array.dtype.kind != "f" or (shape is not None and array.shape != shape):
        raise ValueError(name + " requires matching floating physical values")
    if array.shape != (*valid.shape, 3):
        raise ValueError(name + " shape differs from its validity mask")
    if np.isinf(array).any() or not np.isfinite(array[valid]).all():
        raise ValueError(name + " contains nonfinite valid samples or infinity")
    return np.where(valid[..., None], array, 0.0)


def measure(
    rgb_frames,
    *,
    normalization_e,
    dataset,
    evaluation_mask,
    valid_mask_frames,
    baseline_rgb_frames=None,
    baseline_valid_mask_frames=None,
    reference_rgb=None,
):
    """Measure fixed-support pre-tone RGB; reject malformed/overflowing numeric input.

    NaN physical samples are accepted only under false validity. No candidate-dependent
    pixel deletion is permitted. Any invalid selected sample makes every metric unknown.
    An absent reference never becomes common-color baseline truth.
    The caller binds dataset/source/calibration IDs and artifact hashes.
    """
    rgb = np.asarray(rgb_frames)
    if rgb.dtype.kind != "f":
        raise ValueError("rgb_frames must be floating physical values")
    if rgb.ndim != 4 or rgb.shape[-1] != 3 or not 4 <= rgb.shape[0] <= 16:
        raise ValueError("RGB must be [4..16,H,W,3] stationary repeat frames")
    frames, height, width, _ = rgb.shape
    context = validate_dataset(dataset, (height, width))
    scale = _number(normalization_e, "normalization_e")
    if scale <= 0:
        raise ValueError("normalization_e must be a positive fixed electron scale")
    fixed = _bool_array(evaluation_mask, "evaluation_mask", (height, width))
    valid = _bool_array(valid_mask_frames, "valid_mask_frames", (frames, height, width))
    rgb = _masked_physical(rgb, valid, "rgb_frames")
    roi = context["roi"]
    mask = np.zeros((height, width), dtype=bool)
    rows, columns = (
        slice(roi["row_start"], roi["row_end"]),
        slice(roi["column_start"], roi["column_end"]),
    )
    mask[rows, columns] = fixed[rows, columns]
    count = int(mask.sum())
    if count == 0:
        raise ValueError("The fixed evaluation ROI is empty")
    baseline = (
        None
        if baseline_rgb_frames is None
        else _masked_physical(
            baseline_rgb_frames,
            _bool_array(baseline_valid_mask_frames, "baseline_valid_mask_frames", valid.shape),
            "baseline_rgb_frames",
            rgb.shape,
        )
    )
    reference = (
        None
        if reference_rgb is None
        else _float_array(reference_rgb, "reference_rgb", (height, width, 3))
    )
    if baseline_rgb_frames is not None:
        valid = valid & baseline_valid_mask_frames
    elif baseline_valid_mask_frames is not None:
        raise ValueError("Baseline mask requires baseline physical RGB")
    invalid_per_frame = np.count_nonzero(~valid[:, mask], axis=1)
    invalid = int(invalid_per_frame.sum())
    metrics = {}
    for key, definition in METRICS.items():
        if key.startswith("edge_") and context["kind"] != "edge":
            reason = "Edge metric is not applicable to this dataset kind"
        elif key.startswith("texture_") and context["kind"] != "texture":
            reason = "Texture metric is not applicable to this dataset kind"
        elif "baseline" in key:
            reason = "Required calibrated common-color baseline unavailable"
        else:
            reason = "Independent reference unavailable"
        metrics[key] = _record(None, definition["unit"], reason)
    result = {
        "schema": VERSION,
        "dataset": context,
        "metrics": metrics,
        "coverage": {
            "frame_count": frames,
            "shape": [height, width],
            "selected_pixels": count,
            "required_samples": count * frames,
            "invalid_samples": invalid,
            "valid_samples": count * frames - invalid,
            "invalid_per_frame": invalid_per_frame.tolist(),
            "complete": invalid == 0,
            "fixed_mask_bytes_sha256": hashlib.sha256(mask.tobytes()).hexdigest(),
            "reference_available": reference is not None,
            "baseline_available": baseline is not None,
        },
        "definitions": deepcopy(METRICS),
        "diagnostics": {},
        "input_stage": "signed_normalized_charge_linear_rgb_before_tone",
        "unit_meaning": ("fixed-color arithmetic sensor RGB times normalization_e; "
                         "electron-equivalent, not photometric luminance"),
        "guide": "arithmetic_sensor_RGB_not_photometric_luminance",
        "normalization_e": scale,
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
    }
    if invalid:
        for key in metrics:
            metrics[key] = _record(
                None,
                METRICS[key]["unit"],
                "Invalid sample in fixed evaluation ROI; support was not reduced",
            )
        return result
    try:
        with np.errstate(over="raise", divide="raise", invalid="raise"):
            guide = _guide(rgb, scale)
            average = guide.mean(axis=0)
            if context["stationary"]:
                residual = guide[:, mask] - average[mask]
                std = np.sqrt(np.mean(np.sum(residual**2, axis=0) / (frames - 1)))
                metrics["temporal_residual_std_e"] = _record(float(std), ELECTRON)
            else:
                metrics["temporal_residual_std_e"] = _record(
                    None, ELECTRON, "Stationary repeated acquisition is not declared"
                )
            ref = None if reference is None else _guide(reference, scale)
            for values, prefix in (
                (ref, "bias"),
                (
                    None if baseline is None else _guide(baseline, scale).mean(axis=0),
                    "baseline_delta",
                ),
            ):
                if values is not None:
                    delta = average[mask] - values[mask]
                    metrics["mean_" + prefix + "_e"] = _record(float(delta.mean()), ELECTRON)
                    metrics["mean_abs_" + prefix + "_e"] = _record(
                        float(np.abs(delta).mean()), ELECTRON
                    )
            if context["kind"] == "texture":
                _texture(metrics, result["diagnostics"], average, ref, mask, context["texture"])
            elif context["kind"] == "edge":
                _edge(metrics, result["diagnostics"], average, ref, mask, context["edge"], roi)
    except (FloatingPointError, OverflowError, np.linalg.LinAlgError) as error:
        raise ValueError("ISP metric arithmetic overflowed or failed to resolve") from error
    return result


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError(name + " must be a nonempty string of at most 200 characters")
    return value


def _metric_record(value, name):
    _keys(value, {"value", "unit", "status", "reason"}, name="metric " + name)
    unit = _identifier(value["unit"], "metric unit")
    if name in METRICS and unit != METRICS[name]["unit"]:
        raise ValueError("Metric unit differs from its definition: " + name)
    if value["status"] == "unknown":
        if (
            value["value"] is not None
            or not isinstance(value["reason"], str)
            or not value["reason"].strip()
        ):
            raise ValueError("Unknown metric needs a null value and a reason")
        return _record(None, unit, value["reason"])
    if value["status"] != "ok" or value["reason"] is not None:
        raise ValueError("Known metric needs status ok and no unknown reason")
    return _record(_number(value["value"], "metric value"), unit)


def reduce_datasets(results, reducers):
    """Explicit named dataset reductions only; never silently pool/miss a scene."""
    if not isinstance(results, dict) or not isinstance(reducers, list) or not reducers:
        raise ValueError("Dataset results and explicit reducer list are required")
    metrics, definitions = {}, {}
    for spec in reducers:
        _keys(spec, {"id", "metric", "dataset_ids", "reducer"}, name="reducer")
        identity = _identifier(spec["id"], "reducer id")
        name = spec["metric"]
        if (
            identity in metrics
            or name not in METRICS
            or spec["reducer"] not in {"max", "min", "max_abs"}
        ):
            raise ValueError("Duplicate reducer or unsupported metric/reduction")
        ids = spec["dataset_ids"]
        if not isinstance(ids, list) or not ids:
            raise ValueError("Reducer must name distinct nonempty dataset ids")
        for dataset_id in ids:
            _identifier(dataset_id, "dataset id")
        if len(set(ids)) != len(ids):
            raise ValueError("Reducer must name distinct nonempty dataset ids")
        values, unavailable = [], []
        for dataset_id in ids:
            dataset_result = results.get(dataset_id, {})
            if not isinstance(dataset_result, dict) or not isinstance(
                dataset_result.get("metrics", {}), dict
            ):
                raise ValueError("Dataset result must contain a metric mapping")
            raw = dataset_result.get("metrics", {}).get(name)
            record = None if raw is None else _metric_record(raw, name)
            if record is None or record["status"] != "ok":
                unavailable.append(dataset_id)
            else:
                values.append(record["value"])
        unit = METRICS[name]["unit"]
        if unavailable:
            metrics[identity] = _record(
                None, unit, "Missing/unknown datasets: " + ", ".join(unavailable)
            )
        else:
            operation = spec["reducer"]
            value = (
                min(values)
                if operation == "min"
                else max(abs(v) for v in values)
                if operation == "max_abs"
                else max(values)
            )
            metrics[identity] = _record(value, unit)
        definitions[identity] = deepcopy(spec)
    return {
        "schema": "camera_isp_study_reduction_v2",
        "metrics": metrics,
        "definitions": definitions,
        "coverage": {
            "defined": sum(v["status"] == "ok" for v in metrics.values()),
            "unknown": sum(v["status"] == "unknown" for v in metrics.values()),
        },
    }


def aggregate(rows, objectives, constraints, *, expected_candidate_ids=None):
    """Constraint checks and sampled Pareto; ties survive, unknowns never pass."""
    if not isinstance(rows, list) or not isinstance(objectives, list) or not objectives:
        raise ValueError("Rows and at least one explicit objective are required")
    if not isinstance(constraints, list):
        raise ValueError("Constraints must be an explicit list")
    objective_names = []
    for objective in objectives:
        _keys(objective, {"metric", "direction"}, name="objective")
        name = _identifier(objective["metric"], "objective metric")
        if name in objective_names or objective["direction"] not in {"minimize", "maximize"}:
            raise ValueError("Duplicate objective or unsupported direction")
        objective_names.append(name)
    units = {}
    normalized_constraints = []
    for constraint in constraints:
        _keys(constraint, {"metric", "operator", "value", "unit"}, name="constraint")
        name, unit = (
            _identifier(constraint["metric"], "constraint metric"),
            _identifier(constraint["unit"], "constraint unit"),
        )
        if constraint["operator"] not in {"<=", ">="}:
            raise ValueError("Unsupported constraint operator")
        if (name in METRICS and unit != METRICS[name]["unit"]) or (
            name in units and units[name] != unit
        ):
            raise ValueError("Constraint units disagree")
        units[name] = unit
        normalized_constraints.append(
            {**constraint, "value": _number(constraint["value"], "constraint value")}
        )
    normalized = {}
    states = {"succeeded", "failed", "cancelled", "queued", "running"}
    for row in rows:
        _keys(row, {"candidate_id", "status", "metrics"}, name="candidate row")
        identity = _identifier(row["candidate_id"], "candidate id")
        if (
            identity in normalized
            or row["status"] not in states
            or not isinstance(row["metrics"], dict)
        ):
            raise ValueError("Duplicate candidate, invalid status or invalid metrics")
        values = {}
        for name, record in row["metrics"].items():
            _identifier(name, "metric id")
            values[name] = _metric_record(record, name)
            if name in units and values[name]["unit"] != units[name]:
                raise ValueError("Candidate metric units disagree")
            units[name] = values[name]["unit"]
        normalized[identity] = {
            "candidate_id": identity,
            "status": row["status"],
            "metrics": values,
        }
    ids = list(normalized) if expected_candidate_ids is None else expected_candidate_ids
    if not isinstance(ids, list) or not ids:
        raise ValueError("Expected candidate ids must be distinct and nonempty")
    for identity in ids:
        _identifier(identity, "expected candidate id")
    if len(set(ids)) != len(ids):
        raise ValueError("Expected candidate ids must be distinct and nonempty")
    if normalized.keys() - set(ids):
        raise ValueError("Result candidate is outside the fixed manifest")
    analyzed, provisional = [], False
    for identity in ids:
        row = normalized.get(
            identity, {"candidate_id": identity, "status": "missing", "metrics": {}}
        )
        checks = []
        for constraint in normalized_constraints:
            metric = row["metrics"].get(constraint["metric"])
            actual = (
                None
                if row["status"] != "succeeded" or not metric or metric["status"] != "ok"
                else metric["value"]
            )
            passed = actual is not None and (
                actual <= constraint["value"]
                if constraint["operator"] == "<="
                else actual >= constraint["value"]
            )
            checks.append(
                {
                    **constraint,
                    "actual": actual,
                    "status": "unknown"
                    if actual is None
                    else "satisfied"
                    if passed
                    else "violated",
                }
            )
        missing_objective = any(
            row["metrics"].get(name, {}).get("status") != "ok" for name in objective_names
        )
        unknown = row["status"] != "succeeded" or any(c["status"] == "unknown" for c in checks)
        violated = any(c["status"] == "violated" for c in checks)
        feasible = "infeasible" if violated else "unknown" if unknown else "feasible"
        eligible = feasible == "feasible" and not missing_objective
        provisional |= unknown or missing_objective
        analyzed.append(
            {
                **row,
                "constraints": checks,
                "feasibility": feasible,
                "pareto_status": "pending" if eligible else "excluded",
                "exclusion_reason": None
                if eligible
                else "Unfinished/failed/missing candidate, unmet constraint or undefined metric",
            }
        )
    eligible_rows = [row for row in analyzed if row["pareto_status"] == "pending"]
    for row in eligible_rows:

        def dominates(other, row=row):
            no_worse, better = True, False
            for objective in objectives:
                a = other["metrics"][objective["metric"]]["value"]
                b = row["metrics"][objective["metric"]]["value"]
                minimize = objective["direction"] == "minimize"
                no_worse &= a <= b if minimize else a >= b
                better |= a < b if minimize else a > b
            return no_worse and better

        row["pareto_status"] = (
            "dominated"
            if any(dominates(other) for other in eligible_rows if other is not row)
            else "non_dominated"
        )
    return {
        "schema": "camera_isp_study_comparison_v1",
        "rows": analyzed,
        "objectives": deepcopy(objectives),
        "constraints": normalized_constraints,
        "metric_units": units,
        "pareto_provisional": bool(provisional),
        "coverage": {
            state: sum(row["status"] == state for row in analyzed)
            for state in sorted(states | {"missing"})
        },
        "pareto_scope": "Feasible evaluated samples only; no combined score or automatic winner",
        "physical_validation": "not_established",
        "total_uncertainty": {"kind": "unknown"},
    }
