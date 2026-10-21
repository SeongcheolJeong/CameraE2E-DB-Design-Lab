"""Bounded, API-free noise/time trade-off over one retained-code ISP backend.

Inputs are verified retained-source snapshots and selected original integer RAW.
The caller must re-resolve their artifacts and registered temporal reference before
calling this pure evaluator. A stationary repeat is a separate noise-enabled run
of the same frozen static Test, not an adjacent frame of one moving run.
"""

from __future__ import annotations

import hashlib
import itertools
import math
from copy import deepcopy

import numpy as np

from .registry import IntegrityError, canonical, digest
from .retained_isp import process_raw, validate_inputs, validate_profile
from .retained_isp_comparison import AXES
from .retained_isp_replay import _format_comparison_key
from .retained_isp_source import array_sha256
from .temporal_signal_metrics import METRIC_UNITS as TIME_UNITS
from .temporal_signal_metrics import OBJECTIVE_METRICS as TIME_OBJECTIVES
from .temporal_signal_metrics import measure as measure_time
from .temporal_signal_reference import _reference_array, _support

SCHEMA = "camera_retained_isp_joint_study_v1"
NOISE = "stationary_repeat_residual_std"
UNITS = {NOISE: "normalized_code_rgb", **TIME_UNITS}
MAX_REPEATS = 16
MIN_REPEATS = 4
MAX_CANDIDATES = 12
MAX_TOTAL_RAW_PIXELS = 1_048_576
RESULT_SCHEMA_V2 = "camera_retained_isp_joint_study_result_v2"
RESULT_SCHEMA = "camera_retained_isp_joint_study_result_v3"
NOISE_DEFINITION = (
    "sqrt(mean_frame_pixel_RGB(sample_variance_across_independent_runs_at_same_frame))"
)
LEGACY_NOISE_MEANING = (
    "code-domain repeat residual; frozen static fixture and distinct simulation seeds"
)
NOISE_MEANING = (
    "code-domain repeat residual; byte-identical static scene and distinct simulation seeds"
)


def _exact(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(name + " requires exactly its declared fields")


def _sha_array(value):
    array = np.ascontiguousarray(value)
    result = hashlib.sha256()
    result.update(digest({"shape": list(array.shape), "dtype": str(array.dtype)}).encode())
    result.update(array.tobytes())
    return result.hexdigest()


def _display_from_pre_tone(output, profile):
    """Independently replay the frozen final display transfer, not the ISP."""
    clipped = np.clip(output, 0.0, 1.0)
    if profile["tone"]["curve"] == "linear":
        return clipped
    low = 12.92 * clipped
    high = 1.055 * np.power(clipped, 1.0 / 2.4) - 0.055
    return np.clip(np.where(clipped <= 0.0031308, low, high), 0.0, 1.0)


def _output_evidence(output, gate, display, *, raw_shape, label, retain_array,
                     output_version, profile):
    """Retain metric inputs and, in v3, the actual delivered display RGB."""
    if (output.shape != (*raw_shape, 3) or output.dtype != np.dtype("float64")
            or gate.shape != raw_shape or gate.dtype != np.dtype("bool")
            or not np.isfinite(output).all()):
        raise IntegrityError("Joint Study processor output violates native shape/type")
    if output_version == 3 and (
            display.shape != output.shape or display.dtype != np.dtype("float64")
            or not np.isfinite(display).all()
            or np.any((display < 0) | (display > 1))
            or not np.array_equal(display, _display_from_pre_tone(output, profile))):
        raise IntegrityError("Joint Study final RGB differs from frozen display transfer")
    evidence = {
        "pre_tone_sha256": _sha_array(output),
        "temporal_gate_sha256": _sha_array(gate),
        "temporal_gate_active_pixels": int(np.count_nonzero(gate)),
    }
    if output_version == 3:
        evidence["display_rgb_sha256"] = _sha_array(display)
    if retain_array is not None:
        evidence["pre_tone_artifact"] = retain_array(label + "-pre-tone.npy", output)
        evidence["temporal_gate_artifact"] = retain_array(label + "-temporal-gate.npy", gate)
        if output_version == 3:
            evidence["display_rgb_artifact"] = retain_array(label + "-display-rgb.npy", display)
    return evidence


def _source_binding(source, raw, profile):
    """Bind selected RAW bytes to a detached, already verified source snapshot."""
    if not isinstance(source, dict) or source.get("source_kind") != "run":
        raise ValueError("Joint Study requires original retained RAW run sources")
    if not isinstance(raw, np.ndarray) or list(raw.shape) != source.get("raw_shape"):
        raise IntegrityError("Joint Study selected RAW shape differs from source")
    if source.get("selected_array_sha256") != array_sha256(raw):
        raise IntegrityError("Joint Study selected RAW bytes differ from source")
    parsed = validate_inputs(
        raw, source["raw_format"], profile, geometry=source["geometry"],
        frame_records=source["frame_records"], events=source["retained_charge_events"],
    )
    original = source["source_snapshot"]
    test = original["test_snapshot"]
    if original.get("test_id") != test.get("id"):
        raise IntegrityError("Joint Study original Test binding differs")
    program = source["capture_program_snapshot"]["payload"]
    model_parts = {key: program.get(key) for key in
                   ("electrical_model", "retained_charge_model")}
    if any(value is None for value in model_parts.values()):
        raise ValueError("Joint Study needs the frozen retained/electrical model")
    frame_ids = [item["frame_id"] for item in source["frame_records"]]
    acquisitions = source["acquisition_identity"]
    if (len(acquisitions) != len(frame_ids)
            or [item.get("acquisition_id") for item in acquisitions] != frame_ids
            or any(item.get("source_id") != source["source_id"] for item in acquisitions)):
        raise IntegrityError("Joint Study frame acquisition lineage differs")
    return {
        "source_id": source["source_id"],
        "source_sha256": digest(source),
        "source_snapshot_sha256": digest(original),
        "source_test_id": test["id"],
        "source_test_sha256": digest(test),
        "assembly_id": original["assembly_id"],
        "selector": deepcopy(source["selector"]),
        "raw_sha256": source["raw_sha256"],
        "selected_array_sha256": source["selected_array_sha256"],
        "raw_format": deepcopy(source["raw_format"]),
        "geometry": deepcopy(source["geometry"]),
        "pixel_pitch_um": source["pixel_pitch_um"],
        "raw_shape": list(raw.shape),
        "clock": deepcopy(source["clock"]),
        "frame_ids": frame_ids,
        "frame_conditions_sha256": digest(source["frame_conditions"]),
        "frame_records_sha256": digest(source["frame_records"]),
        "timing_sha256": digest(source["timing"]),
        "event_schedule_sha256": source["retained_charge_events"]["schedule_sha256"],
        "row_transfer_ns_sha256": digest(parsed["schedule"]["times"].tolist()),
        "model_parts_sha256": digest(model_parts),
        "source_provenance": deepcopy(source["source_provenance"]),
    }


def _normalize_axes(axes, profile):
    if not isinstance(axes, list) or not 1 <= len(axes) <= 2:
        raise ValueError("Joint Study needs one or two explicit ISP axes")
    normalized = []
    for axis in axes:
        _exact(axis, ("parameter", "values"), "axis")
        parameter, values = axis["parameter"], axis["values"]
        if parameter not in AXES:
            raise ValueError("Joint Study axis is outside retained-code ISP")
        group, _ = parameter.split(".")
        if profile[group]["mode"] == "bypass":
            raise ValueError("A swept Joint Study filter must be active")
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_CANDIDATES:
            raise ValueError("Joint Study axis needs one to twelve values")
        limit = 0.95 if parameter == "temporal_filter.previous_weight" else 1.0
        parsed = []
        for value in values:
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 <= value <= limit):
                raise ValueError("Joint Study axis has an invalid coordinate")
            parsed.append(float(value))
        if len(set(parsed)) != len(parsed):
            raise ValueError("Joint Study axis coordinates must be unique")
        normalized.append({"parameter": parameter, "values": parsed})
    if len({item["parameter"] for item in normalized}) != len(normalized):
        raise ValueError("Joint Study axes must be unique")
    if math.prod(len(item["values"]) for item in normalized) > MAX_CANDIDATES:
        raise ValueError("Joint Study exceeds twelve candidate profiles")
    return normalized


def _normalize_targets(items, *, constraints):
    if not isinstance(items, list) or not 1 <= len(items) <= (8 if constraints else 4):
        raise ValueError("Joint Study needs bounded explicit objectives and constraints")
    result = []
    for item in items:
        _exact(item, ("metric", "operator", "value", "unit") if constraints
               else ("metric", "direction"), "target")
        metric = item["metric"]
        if (metric not in UNITS or metric == "signal_amplitude_retention"
                or (not constraints and metric not in {NOISE, *TIME_OBJECTIVES})):
            raise ValueError("Joint Study target metric is unsupported")
        if constraints:
            if (item["unit"] != UNITS[metric] or item["operator"] != "<="
                    or type(item["value"]) not in (int, float)
                    or not math.isfinite(item["value"]) or item["value"] < 0):
                raise ValueError("Joint Study constraint has invalid direction/unit/limit")
            result.append({**item, "value": float(item["value"])})
        else:
            if item["direction"] != "minimize":
                raise ValueError("Joint Study only minimizes nonnegative errors")
            result.append(deepcopy(item))
    if len({item["metric"] for item in result}) != len(result):
        raise ValueError("Joint Study target metrics must be unique")
    return result


def _roi(value, shape):
    _exact(value, ("row_start", "row_end", "column_start", "column_end"), "noise ROI")
    if any(type(v) is not int for v in value.values()):
        raise ValueError("Joint Study ROI needs exact integer coordinates")
    if not (0 <= value["row_start"] < value["row_end"] <= shape[0]
            and 0 <= value["column_start"] < value["column_end"] <= shape[1]
            and value["row_end"] - value["row_start"] >= 2
            and value["column_end"] - value["column_start"] >= 2):
        raise ValueError("Joint Study noise ROI exceeds native support")
    return deepcopy(value)


def make_specification(model, stationary_repeats, dynamic, temporal_reference, *,
                       axes, noise_roi, objectives, constraints):
    """Freeze the exact candidate plan; pairs are (resolved source, selected RAW)."""
    profile = validate_profile(model["payload"])
    if model.get("kind") != "isp" or not isinstance(model.get("id"), str):
        raise ValueError("Joint Study requires a registered ISP model snapshot")
    if (not isinstance(stationary_repeats, list)
            or not MIN_REPEATS <= len(stationary_repeats) <= MAX_REPEATS):
        raise ValueError("Joint Study needs four to sixteen independent repeat runs")
    bindings = [_source_binding(source, raw, profile) for source, raw in stationary_repeats]
    moving = _source_binding(*dynamic, profile)
    if any(_format_comparison_key(model["input_raw_format"]) !=
           _format_comparison_key(binding["raw_format"]) for binding in [*bindings, moving]):
        raise ValueError("Joint Study model RAW format differs from selected source")
    if len({b["source_id"] for b in [*bindings, moving]}) != len(bindings) + 1:
        raise ValueError("Joint Study acquisitions must have distinct source IDs")
    first = bindings[0]
    shared = ("selector", "raw_format", "geometry", "pixel_pitch_um", "assembly_id",
              "model_parts_sha256")
    if any(any(digest(b[key]) != digest(first[key]) for key in shared)
           for b in [*bindings[1:], moving]):
        raise ValueError("Joint Study camera/readout/format/geometry differs across sources")
    if sum(raw.size for _, raw in [*stationary_repeats, dynamic]) > MAX_TOTAL_RAW_PIXELS:
        raise ValueError("Joint Study exceeds its combined RAW pixel budget")
    normalized_axes = _normalize_axes(axes, profile)
    normalized_objectives = _normalize_targets(objectives, constraints=False)
    normalized_constraints = _normalize_targets(constraints, constraints=True)
    if not {NOISE, "signal_change_rms_error"} <= {
            target["metric"] for target in normalized_objectives}:
        raise ValueError("Joint Study must trade repeat noise against true time-signal error")
    if not {"truth_code_mae", "signal_amplitude_error", "signal_change_rms_error"} <= {
            target["metric"] for target in normalized_constraints}:
        raise ValueError("Joint Study needs absolute and signal-fidelity constraints")
    return {
        "schema": SCHEMA,
        "isp_model_id": model["id"],
        "isp_model_sha256": digest(model),
        "base_profile_sha256": digest(profile),
        "stationary_bindings": bindings,
        "dynamic_binding": moving,
        "temporal_reference_id": temporal_reference["id"],
        "temporal_reference_sha256": digest(temporal_reference),
        "axes": normalized_axes,
        "noise_roi": _roi(noise_roi, first["raw_shape"][1:]),
        "objectives": normalized_objectives,
        "constraints": normalized_constraints,
    }


def _constant_scene(sequence):
    """Read the immutable per-knot spectral bytes named in a frozen scene."""
    if not isinstance(sequence, dict):
        return None
    knots = sequence.get("payload", {}).get("knots")
    images = sequence.get("image_snapshots")
    if not isinstance(knots, list) or not knots or not isinstance(images, list):
        return None
    if len(knots) != len(images):
        return None
    hashes = []
    for knot, image in zip(knots, images, strict=True):
        if not isinstance(knot, dict) or not isinstance(image, dict):
            return None
        if knot.get("spectral_image_id") != image.get("id"):
            return None
        artifacts = [artifact for artifact in image.get("artifacts", [])
                     if isinstance(artifact, dict) and artifact.get("name") == "spectral-image.npy"]
        if not artifacts:
            return None
        # Published scene images may carry the same descriptor once as their
        # image artifact and once in the frozen render provenance. That alias
        # is one byte identity; conflicting descriptors are not static proof.
        first = artifacts[0]
        if any((artifact.get("sha256") != first.get("sha256")
                or artifact.get("bytes") != first.get("bytes"))
               for artifact in artifacts[1:]):
            return None
        sha = artifacts[0].get("sha256")
        if not isinstance(sha, str) or len(sha) != 64 or any(
                character not in "0123456789abcdef" for character in sha):
            return None
        hashes.append(sha)
    return hashes[0] if len(set(hashes)) == 1 else None


def _stationarity(repeats, bindings, profile):
    """Unknown source behavior never becomes a stationary noise measurement."""
    first = repeats[0][0]
    reference_test = first["source_snapshot"]["test_id"]
    scene = first["source_snapshot"].get("scene_sequence_snapshot", {})
    if not isinstance(scene, dict):
        return "No frozen byte-identical static-scene support"
    scene_hash = scene.get("content_hash")
    fixture = scene.get("fixture_specification")
    if fixture is not None and (not isinstance(fixture, dict)
                                or not isinstance(fixture.get("parameters"), dict)
                                or fixture["parameters"].get("pattern") != "static"):
        return "Authored fixture does not declare a static scene"
    if not scene_hash or _constant_scene(scene) is None:
        return "No frozen byte-identical static-scene support"
    if first["source_snapshot"].get("unmodulated_scene_sequence_id") and not first[
            "source_snapshot"].get("unmodulated_scene_sequence_snapshot"):
        return "Unmodulated scene snapshot is missing"
    secondary = first["source_snapshot"].get("unmodulated_scene_sequence_snapshot")
    if secondary is not None and _constant_scene(secondary) is None:
        return "Unmodulated scene changes across authored knots"
    waveform = first.get("capture_program_snapshot", {}).get("payload", {}).get("waveform", ...)
    if waveform is ... or (waveform is not None and (
            not isinstance(waveform, dict) or waveform.get("kind") != "constant")):
        return "Periodic or unbound illumination cannot support repeat-noise metrics"
    seeds = []
    first_schedule = validate_inputs(
        repeats[0][1], first["raw_format"], profile, geometry=first["geometry"],
        frame_records=first["frame_records"], events=first["retained_charge_events"],
    )["schedule"]
    if len(set(first_schedule["signatures"])) != 1:
        return "Stationary repeat changes exposure, gain or row timing within a run"
    for (source, raw), binding in zip(repeats, bindings, strict=True):
        original = source["source_snapshot"]
        parameters = original.get("parameters", {})
        seed = parameters.get("seed")
        if (source["source_provenance"].get("source_kind") != "simulated"
                or type(seed) is not int or parameters.get("noise_enabled") is not True):
            return "Independent noise-enabled simulated run seeds are not established"
        seeds.append(seed)
        if (original.get("test_id") != reference_test
                or original.get("scene_sequence_snapshot", {}).get("content_hash") != scene_hash
                or _constant_scene(original.get("scene_sequence_snapshot"))
                != _constant_scene(scene)
                or original.get("unmodulated_scene_sequence_snapshot") != secondary
                or (source.get("capture_program_snapshot", {}).get("payload", {})
                    .get("waveform", ...) != waveform)
                or source["frame_conditions"] != first["frame_conditions"]
                or source["retained_charge_events"] != first["retained_charge_events"]
                or binding["frame_ids"] != bindings[0]["frame_ids"]
                or binding["clock"] != bindings[0]["clock"]):
            return "Repeat scene, test, controls or sampling time differs"
        schedule = validate_inputs(
            raw, source["raw_format"], profile, geometry=source["geometry"],
            frame_records=source["frame_records"], events=source["retained_charge_events"],
        )["schedule"]
        if (not np.array_equal(schedule["times"], first_schedule["times"])
                or schedule["signatures"] != first_schedule["signatures"]
                or schedule["clear"] != first_schedule["clear"]):
            return "Independent runs have different row clocks or reset controls"
    if len(set(seeds)) != len(seeds):
        return "Repeat runs reuse a simulation noise seed"
    return None


def _temporal_truth(source, raw, model, profile, reference, content):
    from .temporal_signal_contracts import TemporalReferenceImport

    data = TemporalReferenceImport.model_validate({
        key: reference[key] for key in TemporalReferenceImport.model_fields
    }).model_dump()
    test = source["source_snapshot"]["test_snapshot"]
    fixed = {key: deepcopy(profile[key]) for key in ("demosaic", "white_balance", "ccm")}
    expected = {
        "source_id": source["source_id"], "selector": source["selector"],
        "raw_sha256": source["raw_sha256"],
        "selected_array_sha256": source["selected_array_sha256"],
        "source_snapshot_sha256": digest(source["source_snapshot"]),
        "source_test_id": test["id"], "source_test_sha256": digest(test),
        "geometry": source["geometry"], "raw_shape": source["raw_shape"],
        "raw_format": source["raw_format"], "clock": source["clock"],
        "frame_ids": [r["frame_id"] for r in source["frame_records"]],
        "frame_conditions_sha256": digest(source["frame_conditions"]),
        "frame_records_sha256": digest(source["frame_records"]),
        "timing_sha256": digest(source["timing"]),
        "event_schedule_sha256": source["retained_charge_events"]["schedule_sha256"],
        "source_provenance": source["source_provenance"],
        "isp_model_id": model["id"], "isp_model_sha256": digest(model),
        "fixed_color_profile_sha256": digest(fixed),
    }
    if data["source_binding"] != expected or data["fixed_color_profile"] != fixed:
        raise IntegrityError("Joint Study temporal truth lost source/profile/clock binding")
    artifact = reference["array_artifact"]
    if (type(content) is not bytes or hashlib.sha256(content).hexdigest() != artifact["sha256"]
            or len(content) != artifact["bytes"]):
        raise IntegrityError("Joint Study temporal truth bytes differ from frozen artifact")
    truth = _reference_array(content, [*raw.shape, 3])
    schedule = validate_inputs(
        raw, source["raw_format"], profile, geometry=source["geometry"],
        frame_records=source["frame_records"], events=source["retained_charge_events"],
    )["schedule"]
    _support(data, truth, raw, schedule["times"])
    return data, truth, schedule["times"]


def _noise(outputs, raws, raw_format, roi, unknown_reason):
    if unknown_reason is not None:
        return {"value": None, "unit": UNITS[NOISE], "status": "unknown",
                "reason": unknown_reason}
    rows = slice(roi["row_start"], roi["row_end"])
    columns = slice(roi["column_start"], roi["column_end"])
    white, black = 2 ** raw_format["bit_depth"] - 1, raw_format["black_level_dn"]
    if any(np.any((raw[:, rows, columns] <= black) | (raw[:, rows, columns] == white))
           for raw in raws):
        reason = "Stationary fixed ROI contains a RAW code rail"
    elif any(np.any((out[:, rows, columns] < 0) | (out[:, rows, columns] > 1))
             for out in outputs):
        reason = "Stationary fixed ROI contains clipped pre-tone code"
    else:
        reason = None
    if reason:
        return {"value": None, "unit": UNITS[NOISE], "status": "unknown", "reason": reason}
    selected = np.stack([out[:, rows, columns] for out in outputs], axis=0)
    centered = selected - np.mean(selected, axis=0, keepdims=True)
    std = float(np.sqrt(np.mean(np.sum(np.square(centered), axis=0) / (len(outputs) - 1))))
    return {"value": std, "unit": UNITS[NOISE], "status": "ok", "reason": None}


def _rank(cases, spec):
    rows = []
    for case in cases:
        metrics = case["metrics"]
        checks = []
        for limit in spec["constraints"]:
            record = metrics[limit["metric"]]
            value = record["value"]
            checks.append({"metric": limit["metric"], "operator": "<=",
                           "limit": limit["value"], "unit": limit["unit"],
                           "value": value, "status": "unknown" if record["status"] != "ok"
                           else "pass" if value <= limit["value"] else "fail"})
        objectives = {item["metric"]: metrics[item["metric"]]["value"]
                      for item in spec["objectives"]}
        unknown = any(value is None for value in objectives.values()) or any(
            item["status"] == "unknown" for item in checks
        ) or case["temporal_support_status"] != "ok"
        failed = any(item["status"] == "fail" for item in checks)
        feasibility = "infeasible" if failed else "unknown" if unknown else "feasible"
        rows.append({"candidate_id": case["candidate_id"],
                     "profile_sha256": case["profile_sha256"],
                     "objective_values": objectives, "constraints": checks,
                     "feasibility": feasibility, "pareto": False, "dominated_by": []})
    feasible = [row for row in rows if row["feasibility"] == "feasible"]
    names = [item["metric"] for item in spec["objectives"]]
    for row in feasible:
        for other in feasible:
            if other is row:
                continue
            own = [row["objective_values"][name] for name in names]
            theirs = [other["objective_values"][name] for name in names]
            if all(a <= b for a, b in zip(theirs, own, strict=True)) and any(
                    a < b for a, b in zip(theirs, own, strict=True)):
                row["dominated_by"].append(other["candidate_id"])
        row["pareto"] = not row["dominated_by"]
    return {"candidates": rows,
            "pareto_candidate_ids": [row["candidate_id"] for row in rows if row["pareto"]],
            "excluded_candidate_ids": [row["candidate_id"] for row in rows
                                       if row["feasibility"] != "feasible"],
            "pareto_provisional": any(row["feasibility"] == "unknown" for row in rows),
            "selected_candidate_id": None,
            "scope": "same_frozen_profile_on_independent_static_repeats_and_authored_dynamic_truth"}


def evaluate(specification, model, stationary_repeats, dynamic, temporal_reference,
             temporal_truth_npy_bytes, *, retain_array=None, output_version=None):
    """Run actual integer RAW through the same candidate on both source groups."""
    if retain_array is None:
        if output_version not in (None, 1):
            raise ValueError("Joint Study retained output version needs an array writer")
        output_version = 1
    elif output_version is None:
        output_version = 3
    elif output_version not in (2, 3):
        raise ValueError("Joint Study retained output version is unsupported")
    spec = make_specification(
        model, stationary_repeats, dynamic, temporal_reference,
        axes=specification["axes"], noise_roi=specification["noise_roi"],
        objectives=specification["objectives"], constraints=specification["constraints"],
    )
    if specification != spec:
        raise IntegrityError("Joint Study differs from its normalized frozen specification")
    profile = validate_profile(model["payload"])
    moving_source, moving_raw = dynamic
    reference, truth, times = _temporal_truth(
        moving_source, moving_raw, model, profile, temporal_reference,
        temporal_truth_npy_bytes,
    )
    reason = _stationarity(stationary_repeats, spec["stationary_bindings"], profile)
    cases = []
    for index, values in enumerate(itertools.product(*(axis["values"] for axis in spec["axes"]))):
        candidate_id = f"candidate-{index:03d}"
        candidate = deepcopy(profile)
        coordinates = {}
        for axis, value in zip(spec["axes"], values, strict=True):
            group, key = axis["parameter"].split(".")
            candidate[group][key] = value
            coordinates[axis["parameter"]] = value
        candidate = validate_profile(candidate)
        profile_sha = digest(candidate)
        repeat_outputs = []
        evidence = []
        for repeat_index, (source, raw) in enumerate(stationary_repeats):
            result = process_raw(
                raw, source["raw_format"], candidate, geometry=source["geometry"],
                frame_records=source["frame_records"], events=source["retained_charge_events"],
            )
            if result["summary"]["profile_sha256"] != profile_sha:
                raise IntegrityError("Joint Study repeat processor used a different profile")
            output = result["arrays"]["pre_tone_code_rgb_frames"]
            gate = result["arrays"]["temporal_gate_mask_frames"]
            display = result["arrays"]["rgb_frames"]
            repeat_outputs.append(output)
            evidence.append({"source_id": source["source_id"],
                             "selected_array_sha256": source["selected_array_sha256"],
                             "processor_profile_sha256": result["summary"]["profile_sha256"],
                             **_output_evidence(
                                 output, gate, display, raw_shape=raw.shape,
                                 label=f"{candidate_id}-stationary-{repeat_index:02d}",
                                 retain_array=retain_array, output_version=output_version,
                                 profile=candidate,
                             ),
                             })
        dynamic_result = process_raw(
            moving_raw, moving_source["raw_format"], candidate,
            geometry=moving_source["geometry"], frame_records=moving_source["frame_records"],
            events=moving_source["retained_charge_events"],
        )
        if dynamic_result["summary"]["profile_sha256"] != profile_sha:
            raise IntegrityError("Joint Study dynamic processor used a different profile")
        arrays = dynamic_result["arrays"]
        temporal = measure_time(
            arrays["pre_tone_code_rgb_frames"], truth, moving_raw,
            moving_source["raw_format"], arrays["temporal_gate_mask_frames"],
            candidate, times, reference,
            pre_tone=arrays["pre_tone_code_rgb_frames"],
        )
        noise = _noise(repeat_outputs, [raw for _, raw in stationary_repeats],
                       moving_source["raw_format"], spec["noise_roi"], reason)
        cases.append({
            "candidate_id": candidate_id, "coordinates": coordinates,
            "profile_sha256": profile_sha,
            "metrics": {NOISE: noise, **temporal["metrics"]},
            "temporal_support_status": temporal["support_status"],
            "temporal_support_reason": temporal["support_reason"],
            "noise_support_status": noise["status"],
            "noise_support_reason": noise["reason"],
            "repeat_processing": evidence,
            "dynamic_processing": {
                "source_id": moving_source["source_id"],
                "selected_array_sha256": moving_source["selected_array_sha256"],
                "processor_profile_sha256": dynamic_result["summary"]["profile_sha256"],
                **_output_evidence(
                    arrays["pre_tone_code_rgb_frames"],
                    arrays["temporal_gate_mask_frames"], arrays["rgb_frames"],
                    raw_shape=moving_raw.shape, label=f"{candidate_id}-dynamic",
                    retain_array=retain_array, output_version=output_version,
                    profile=candidate,
                ),
            },
        })
    if any(source["selected_array_sha256"] != array_sha256(raw)
           for source, raw in [*stationary_repeats, dynamic]):
        raise IntegrityError("Joint Study processor changed selected source RAW")
    return {
        "schema": {1: "camera_retained_isp_joint_study_result_v1",
                   2: RESULT_SCHEMA_V2, 3: RESULT_SCHEMA}[output_version],
        "specification": spec,
        "specification_sha256": digest(spec),
        "cases": cases,
        "ranking": _rank(cases, spec),
        "noise_definition": NOISE_DEFINITION,
        "noise_meaning": (NOISE_MEANING if output_version == 3 else LEGACY_NOISE_MEANING),
        "truth_role": "evaluation_only_never_processor_input",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "automatic_selection": False,
    }


def verify_saved_evidence(specification, model, stationary_repeats, dynamic,
                          temporal_reference, temporal_truth_npy_bytes, summary,
                          load_array):
    """Recompute all reported metrics from saved arrays, never from ISP reruns.

    The loader must authenticate the named NPY artifact and enforce the expected
    native shape and dtype. Original RAW and registered truth are supplied by
    the durable workflow's existing source/Test closure verification.
    """
    if not isinstance(summary, dict) or summary.get("schema") not in (
            RESULT_SCHEMA_V2, RESULT_SCHEMA):
        raise IntegrityError("Joint Study result has no retained-array evidence contract")
    output_version = 3 if summary["schema"] == RESULT_SCHEMA else 2
    cases = summary.get("cases")
    if not isinstance(cases, list) or len(cases) != math.prod(
            len(axis["values"]) for axis in specification["axes"]):
        raise IntegrityError("Joint Study saved candidate count differs")
    profile = validate_profile(model["payload"])
    moving_source, moving_raw = dynamic
    reference, truth, times = _temporal_truth(
        moving_source, moving_raw, model, profile, temporal_reference,
        temporal_truth_npy_bytes,
    )
    reason = _stationarity(stationary_repeats, specification["stationary_bindings"], profile)

    def read_pair(record, raw_shape, label):
        if not isinstance(record, dict) or not {
                "pre_tone_artifact", "temporal_gate_artifact", "pre_tone_sha256",
                "temporal_gate_sha256", "temporal_gate_active_pixels",
        } <= record.keys():
            raise IntegrityError("Joint Study saved processor arrays are missing")
        if (not isinstance(record["pre_tone_artifact"], dict)
                or not isinstance(record["temporal_gate_artifact"], dict)):
            raise IntegrityError("Joint Study saved processor artifact is malformed")
        if (record["pre_tone_artifact"].get("name") != label + "-pre-tone.npy"
                or record["temporal_gate_artifact"].get("name")
                != label + "-temporal-gate.npy"):
            raise IntegrityError("Joint Study saved processor array/source alias differs")
        output = load_array(record["pre_tone_artifact"], (*raw_shape, 3),
                            np.dtype("float64"))
        gate = load_array(record["temporal_gate_artifact"], raw_shape,
                          np.dtype("bool"))
        if (record["pre_tone_sha256"] != _sha_array(output)
                or record["temporal_gate_sha256"] != _sha_array(gate)
                or record["temporal_gate_active_pixels"] != int(np.count_nonzero(gate))):
            raise IntegrityError("Joint Study saved processor array/hash/count differs")
        if output_version == 3:
            display_artifact = record.get("display_rgb_artifact")
            if (not isinstance(display_artifact, dict)
                    or display_artifact.get("name") != label + "-display-rgb.npy"):
                raise IntegrityError("Joint Study saved display RGB artifact/source alias differs")
            display = load_array(display_artifact, (*raw_shape, 3), np.dtype("float64"))
            if (record.get("display_rgb_sha256") != _sha_array(display)
                    or np.any((display < 0) | (display > 1))):
                raise IntegrityError("Joint Study saved display RGB hash/range differs")
        else:
            display = None
        return output, gate, display

    for index, (values, case) in enumerate(zip(
        itertools.product(*(axis["values"] for axis in specification["axes"])),
        cases, strict=True,
    )):
        candidate = deepcopy(profile)
        for axis, value in zip(specification["axes"], values, strict=True):
            group, key = axis["parameter"].split(".")
            candidate[group][key] = value
        candidate = validate_profile(candidate)
        if (case.get("candidate_id") != f"candidate-{index:03d}"
                or case.get("profile_sha256") != digest(candidate)):
            raise IntegrityError("Joint Study saved candidate profile differs")
        records = case.get("repeat_processing")
        if not isinstance(records, list) or len(records) != len(stationary_repeats):
            raise IntegrityError("Joint Study repeat array evidence count differs")
        repeat_outputs = []
        for repeat_index, ((source, raw), record) in enumerate(zip(
                stationary_repeats, records, strict=True)):
            output, _, display = read_pair(
                record, raw.shape, f"candidate-{index:03d}-stationary-{repeat_index:02d}",
            )
            if output_version == 3 and not np.array_equal(
                    display, _display_from_pre_tone(output, candidate)):
                raise IntegrityError("Joint Study saved display RGB differs from pre-tone output")
            if record.get("source_id") != source["source_id"]:
                raise IntegrityError("Joint Study repeat array source differs")
            repeat_outputs.append(output)
        moving_output, moving_gate, moving_display = read_pair(
            case.get("dynamic_processing"), moving_raw.shape,
            f"candidate-{index:03d}-dynamic",
        )
        if output_version == 3 and not np.array_equal(
                moving_display, _display_from_pre_tone(moving_output, candidate)):
            raise IntegrityError("Joint Study saved display RGB differs from pre-tone output")
        temporal = measure_time(
            moving_output, truth, moving_raw, moving_source["raw_format"], moving_gate,
            candidate, times, reference, pre_tone=moving_output,
        )
        noise = _noise(
            repeat_outputs, [raw for _, raw in stationary_repeats],
            moving_source["raw_format"], specification["noise_roi"], reason,
        )
        expected_metrics = {NOISE: noise, **temporal["metrics"]}
        if canonical(case.get("metrics")) != canonical(expected_metrics):
            raise IntegrityError("Joint Study saved metrics differ from retained arrays")
        if (case.get("noise_support_status") != noise["status"]
                or case.get("noise_support_reason") != noise["reason"]
                or case.get("temporal_support_status") != temporal["support_status"]
                or case.get("temporal_support_reason") != temporal["support_reason"]):
            raise IntegrityError("Joint Study saved support differs from retained arrays")
