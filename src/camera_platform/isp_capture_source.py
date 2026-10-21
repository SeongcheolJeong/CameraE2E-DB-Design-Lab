"""Explicit interpretations of preserved imported RAW; no synthetic capture program."""

from __future__ import annotations

from copy import deepcopy

import numpy as np

from .registry import IntegrityError, digest

SCHEMA = "camera_charge_isp_capture_source_v1"


def choices(service, capture):
    """Metadata-only discovery; selecting a choice verifies every original input."""
    paired = capture["readout_mode"] == "shared_charge_paths"
    result = []
    for response in service.registry.list("readout_calibrations"):
        if (
            response["sensor_model_id"] != capture["sensor_model_id"]
            or response["readout_mode"] != capture["readout_mode"]
            or response["raw_formats"] != capture["raw_formats"]
            or (response["physical_instance"] is not None
                and response["physical_instance"] != capture["physical_instance"])
            or (capture["source_kind"] == "measured"
                and response["charge_reference"]["kind"] == "authored_simulation")
        ):
            continue
        for path in (["hcg", "lcg"] if paired else [None]):
            selector = {
                "schema": "camera_nonlinear_raw_selection_v2",
                "mode": "shared_charge_path" if paired else "sequential_frames",
                "readout_calibration_id": response["id"],
            }
            if path:
                selector["readout_id"] = path
            result.append({
                "source_kind": "capture", "source_id": capture["id"],
                "name": capture["name"] + " · " + capture["id"][-8:]
                + (" · " + path.upper() if path else "")
                + " · 응답 " + response["name"] + " r" + response["revision"]
                + " · " + response["id"][-8:],
                "raw_shape": [capture["raw_shape"][0], *capture["raw_shape"][-2:]],
                "raw_container_shape": deepcopy(capture["raw_shape"]),
                "raw_sha256": capture["raw_sha256"],
                "raw_format": deepcopy(capture["raw_formats"][0 if path != "lcg" else 1]),
                "geometry": deepcopy(capture["geometry"]),
                "conditions": deepcopy(capture["conditions"]),
                "source_provenance": {key: deepcopy(capture[key]) for key in (
                    "source_kind", "source_uri", "physical_instance", "method_and_version",
                )},
                "response_calibration": {
                    "id": response["id"], "name": response["name"],
                    "revision": response["revision"],
                    "charge_reference": deepcopy(response["charge_reference"]),
                    "applicability": deepcopy(response["applicability"]),
                },
                "selector": selector, "replay_profile_version": 3,
                "integrity": "verified_on_selection",
            })
    return result


def _timing(capture):
    from .timing import _endpoint

    frames = []
    known_clock = capture["clock"].get("status") != "unknown"
    known_mode = capture["timing_mode"]["shutter"] != "unknown"
    for frame in capture["frames"]:
        entry = {
            "frame_id": frame["frame_id"],
            "frame_start_ns": frame["frame_start_ns"],
            "applied_at_ns": frame["applied"]["applied_at_ns"],
            "exposure_windows": None,
        }
        if known_clock and known_mode and frame["frame_start_ns"] is not None:
            mode = capture["timing_mode"]
            first = frame["frame_start_ns"] + (
                mode["hidden_top_rows"] + capture["geometry"]["crop_origin"][0]
            ) * mode["line_time_ns"]
            starts = [_endpoint(first + row * mode["line_time_ns"], "row_start_ns")
                      for row in range(capture["raw_shape"][-2])]
            entry["exposure_windows"] = {
                "row_start_ns": starts,
                "row_end_ns": [_endpoint(t + frame["applied"]["exposure_ns"], "row_end_ns")
                               for t in starts],
            }
        frames.append(entry)
    return {
        "schema": "camera_imported_acquisition_timing_v1",
        "clock": deepcopy(capture["clock"]),
        "mode": deepcopy(capture["timing_mode"]),
        "frames": frames,
        "timestamp_accuracy": "unknown",
        "interval_convention": "half_open",
        "electronic_schedule_simulated": False,
    }


def resolve_inputs(service, source_id, selector):
    from . import nonlinear_captures, nonlinear_dcg, nonlinear_readout, readout_calibrations
    from .hdr_runtime import _raw_format
    from .isp_charge_source import ARRAY_ENCODING, array_sha256, validate_selector

    selector = validate_selector(selector)
    if selector["schema"] != "camera_nonlinear_raw_selection_v2":
        raise ValueError("Imported RAW requires an explicit v2 response calibration selection")
    capture = nonlinear_captures.get(service, source_id)
    paired = capture["readout_mode"] == "shared_charge_paths"
    if selector["mode"] != ("shared_charge_path" if paired else "sequential_frames"):
        raise ValueError("Selected path does not match preserved acquisition readout mode")
    prepared = readout_calibrations.prepare_for_capture(
        service, selector["readout_calibration_id"], capture
    )
    response = prepared["record"]
    lineage = readout_calibrations.lineage(service, response)
    container = nonlinear_captures.load_raw(service, capture)
    index = {"hcg": 0, "lcg": 1}.get(selector.get("readout_id"))
    raw = container[:, index] if paired else container
    if len(prepared["calibrations"]) != len(capture["frames"]):
        raise IntegrityError("Response calibration count differs from original acquisition")
    calibrations, conditions, frames, identities = [], [], [], []
    for i, (original, calibration) in enumerate(
        zip(capture["frames"], prepared["calibrations"], strict=True)
    ):
        if original["settings_evidence"]["status"] != "recorded_applied":
            raise ValueError("Charge replay requires recorded applied settings for every RAW frame")
        selected = (
            nonlinear_dcg.calibration_for_path(calibration, index) if paired else calibration
        )
        selected = nonlinear_readout.validate_calibration(selected, raw.shape[-2:])
        if prepared["raw_formats"][index if paired else 0] != _raw_format(selected["model"]):
            raise IntegrityError("Imported RAW format differs from selected response calibration")
        if np.any(raw[i] < 0) or np.any(raw[i] > 2 ** selected["model"]["adc"]["bit_depth"] - 1):
            raise ValueError("RAW codes exceed the selected path ADC range")
        identity = {
            "source_id": source_id,
            "acquisition_id": original["frame_id"],
            "acquisition_index": i,
            "acquisition_group_id": capture["acquisition_group_id"],
            "shared_charge_group_id": original["frame_id"] if paired else None,
            "readout_id": selector.get("readout_id"),
            "readout_index": index,
        }
        frame = deepcopy(original)
        # An assembly ISP is an authored replay starting point, not evidence of an
        # ISP applied at the camera. Preserve the original pre-ISP RAW boundary.
        frame["applied"]["isp_model_id"] = None
        frame.update(frame_index=i, source_acquisition=identity)
        applied = original["applied"]
        conditions.append({
            "frame_id": original["frame_id"],
            "frame_start_ns": original["frame_start_ns"],
            "applied_at_ns": applied["applied_at_ns"],
            "exposure_ns": applied["exposure_ns"],
            "gain": applied["gain"],
            "isp_model_id": None,
            "temperature_c": applied["temperature_c"],
            "readout_analog_gain": selected["acquisition"]["analog_gain"],
            "readout_digital_gain": selected["model"]["digital_gain"],
        })
        frames.append(frame)
        identities.append(identity)
        calibrations.append(selected)
    raw_artifact = next(a for a in capture["artifacts"] if a["name"] == "raw-capture.npy")
    dark_artifact = next(a for a in response["artifacts"] if a["name"] == "expected-dark-rate.npy")
    acquisition_lineage = {
        "schema": "camera_capture_lineage_v1",
        "acquisition_group_id": capture["acquisition_group_id"],
        "parent_capture_ids": deepcopy(capture["parents"]),
        "dataset_ids": deepcopy(capture["dataset_ids"]),
        "exposure_tokens": nonlinear_captures.lineage(service, capture),
        "decoder_fitting_tokens": deepcopy(lineage["decoder_fitting_tokens"]),
        "calibration_evidence_refs": deepcopy(lineage["evidence_artifacts"]),
    }
    if "decoder_evaluation_tokens" in lineage:
        acquisition_lineage["decoder_evaluation_tokens"] = deepcopy(
            lineage["decoder_evaluation_tokens"]
        )
    source = {
        "schema": SCHEMA,
        "source_kind": "capture",
        "source_id": source_id,
        "name": capture["name"],
        "selector": selector,
        "raw_sha256": raw_artifact["sha256"],
        "raw_container_shape": list(container.shape),
        "raw_shape": list(raw.shape),
        "selected_array_sha256": array_sha256(raw),
        "selected_array_hash_encoding": ARRAY_ENCODING,
        "raw_format": deepcopy(prepared["raw_formats"][index if paired else 0]),
        "geometry": deepcopy(capture["geometry"]),
        "normalization_e": prepared["normalization_e"],
        "pixel_pitch_um": capture["pixel_pitch_um"],
        "source_snapshot": capture,
        "source_provenance": {key: deepcopy(capture[key]) for key in (
            "source_kind", "source_uri", "physical_instance", "method_and_version",
        )},
        "conditions": deepcopy(capture["test_snapshot"]["conditions"]),
        "frame_conditions": conditions,
        "frame_records": frames,
        "clock": deepcopy(capture["clock"]),
        "timing": _timing(capture),
        "response_calibration_snapshot": response,
        "response_calibration_lineage": lineage,
        "decoder_binding": {
            "schema": "camera_imported_charge_decoder_binding_v1",
            "readout_calibration_id": response["id"],
            "readout_calibration_sha256": digest(response),
            "model_sha256": digest(response["electrical_model"]),
            "readout_id": selector.get("readout_id"),
            "normalization_e": prepared["normalization_e"],
            "charge_scale": deepcopy(response["charge_reference"]),
            "dark_rate_sha256": dark_artifact["sha256"],
        },
        "acquisition_lineage": acquisition_lineage,
        "calibration_summaries": [
            {k: v for k, v in c.items() if k != "dark_e"} for c in calibrations
        ],
        "raw_artifact": deepcopy(raw_artifact),
        "dark_rate_artifact": deepcopy(dark_artifact),
        "input_artifacts": deepcopy(capture["artifacts"] + response["artifacts"]),
        "original_acquisition_records": deepcopy(capture["frames"]),
        "acquisition_identity": identities,
    }
    closure = evidence_closure(service, [source])
    # Retain all original dependency artifacts in the frozen request; explicit
    # record bindings above and fresh traversal preserve their ownership/lineage.
    entries = source["input_artifacts"] + [
        a for records in closure.values() for record in records for a in record["artifacts"]
    ]
    unique = {(a["sha256"], a["name"]): a for a in entries}
    source["input_artifacts"] = [unique[k] for k in sorted(unique)]
    return source, raw, calibrations


def evidence_closure(service, selections):
    """Return imported calibration/capture ancestry for portable replay packages."""
    from . import nonlinear_captures, readout_calibrations

    responses, captures, observations, fit_jobs = {}, {}, {}, {}
    pending_responses = [s["selector"]["readout_calibration_id"] for s in selections
                         if s["source_kind"] == "capture"]
    pending_captures = [s["source_id"] for s in selections if s["source_kind"] == "capture"]
    while pending_responses:
        identity = pending_responses.pop()
        if identity in responses:
            continue
        response = readout_calibrations.get(service, identity)
        responses[identity] = response
        pending_responses.extend(response["parents"])
        pending_captures.extend(response["reference_capture_ids"])
        if response.get("schema") == "camera_fitted_response_calibration_v1":
            closure = readout_calibrations.lineage(service, response)
            observations.update({r["id"]: r for r in closure["readout_observations"]})
            fit_jobs.update({r["id"]: r for r in closure["readout_fit_jobs"]})
            pending_captures.extend(r["id"] for r in closure["evaluation_captures"])
    while pending_captures:
        identity = pending_captures.pop()
        if identity in captures:
            continue
        record = service.registry.get("captures", identity)
        if record.get("schema") == "camera_nonlinear_capture_v1":
            record = nonlinear_captures.get(service, identity)
        # Legacy ancestry was verified by nonlinear capture/calibration readers.
        captures[identity] = record
        pending_captures.extend(record.get("parents", []))
    result = {
        "readout_calibrations": [responses[k] for k in sorted(responses)],
        "nonlinear_acquisition_lineage": [captures[k] for k in sorted(captures)],
    }
    if fit_jobs:
        result["readout_observations"] = [observations[k] for k in sorted(observations)]
        result["readout_fit_jobs"] = [fit_jobs[k] for k in sorted(fit_jobs)]
    return result
