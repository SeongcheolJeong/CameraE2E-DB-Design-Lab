"""Causal AE/AWB runtime over the physical temporal capture kernel.

The program declares frame starts, actuator grids and latency components. Only
completed-frame RAW statistics can issue commands. AE and AWB have independent
queues and minimum frame delays, and commands latch no earlier than their ready
time. No hardware transport observations are invented.
"""

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy

import numpy as np

from .control_contracts import MAX_CONTROL_FRAMES, ClosedLoopProgram
from .controls import default_policy, propose_ae, propose_awb, raw_statistics
from .runtime import RuntimeRequestError, _issue
from .temporal import TemporalCaptureSession, TemporalSceneRuntime
from .temporal import _parse as parse_temporal
from .timing import MAX_FRAME_PIXELS, MAX_FRAMES, _endpoint, _span, validate_timing_coverage

PLUGIN_ID = "closed_loop_scene_v1"
SHARED_ARRAYS = {
    "channel_photoelectron_rate_scenes",
    "radius_map",
    "cfa_channel_index",
    "relative_illumination_map",
    "psf_intensity",
    "psf_field_y_mm",
    "psf_field_x_mm",
    "psf_source_y_mm",
    "psf_source_x_mm",
    "sensor_radius_mm",
    "response_radius_map",
}


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _latch(frame, mode):
    return _endpoint(
        frame["frame_start_ns"] + mode["hidden_top_rows"] * mode["line_time_ns"],
        "control_program.latch_ns",
    )


def _capture_frame(frame, state, mode):
    return {
        **frame,
        "applied": {**deepcopy(state), "applied_at_ns": _latch(frame, mode), "command_id": None},
    }


def _events(frame, exposure, mode, full_height, latency):
    end = _endpoint(
        _latch(frame, mode) + (full_height - 1) * mode["line_time_ns"] + exposure,
        "control_program.full_sensor_exposure_end_ns",
    )
    result = {"full_sensor_exposure_end_ns": end}
    fields = [
        ("readout_tail_ns", "readout_complete_ns"),
        ("frame_transport_ns", "frame_available_ns"),
        ("statistics_ns", "statistics_ready_ns"),
        ("controller_ns", "controller_complete_ns"),
        ("command_transport_ns", "command_arrival_ns"),
        ("actuator_ns", "actuator_ready_ns"),
    ]
    for duration, name in fields:
        end = _endpoint(end + latency[duration], "control_program." + name)
        result[name] = end
    return result


def _parse(request):
    if not isinstance(request, Mapping) or "control_program" not in request:
        raise ValueError("Closed-loop request requires an explicit control_program")
    if "native_pixel_response" in request:
        raise ValueError(
            "Native four-site response is not yet supported by closed-loop AE/AWB statistics; "
            "use recorded temporal settings or an HDR bracket"
        )
    if "control_sequence" in request:
        raise ValueError("Recorded controls cannot substitute for a closed-loop program")
    program = ClosedLoopProgram.model_validate(request["control_program"]).model_dump()
    initial, frames, mode, policy = [program[k] for k in ("initial", "frames", "mode", "policy")]
    control = {
        "schema": "recorded_control_sequence_v1",
        "clock": program["clock"],
        "mode": mode,
        "frames": [_capture_frame(frames[0], initial, mode)],
    }
    recorded = {k: v for k, v in request.items() if k != "control_program"}
    parsed = parse_temporal({**recorded, "control_sequence": control})
    base = parsed["base"]
    height, width = base["size"]
    if len(frames) * height * width > MAX_FRAME_PIXELS:
        raise ValueError("Closed-loop sequence exceeds 1048576 total frame pixels")
    roi = policy["statistics_roi"]
    if any(roi["origin"][i] + roi["shape"][i] > base["size"][i] for i in range(2)):
        raise ValueError("Control statistics ROI exceeds native crop")
    if policy["awb"]["minimum_valid_cells"] > roi["shape"][0] * roi["shape"][1] // 4:
        raise ValueError("AWB minimum valid cells exceeds the declared ROI capacity")
    maximum = (
        policy["ae"]["exposure_ns"]["maximum"]
        if policy["ae"]["enabled"]
        else initial["exposure_ns"]
    )
    if any(
        a["frame_start_ns"] + maximum > b["frame_start_ns"]
        for a, b in zip(frames, frames[1:], strict=False)
    ):
        raise ValueError("Maximum AE exposure overlaps the next same-pixel exposure")
    # Preflight every possible exposure before calculating any frames. Existing
    # recorded contracts remain limited to 16 frames; this program is its own schema.
    maximum_frames = [_capture_frame(f, {**initial, "exposure_ns": maximum}, mode) for f in frames]
    coverages = []
    for offset in range(0, len(frames), MAX_FRAMES):
        coverages.append(
            validate_timing_coverage(
                parsed["timeline"],
                {**control, "frames": maximum_frames[offset : offset + MAX_FRAMES]},
                geometry=base["geometry"],
                frame_shape=base["size"],
                expanded=True,
            )
        )
    times = [k["time_ns"] for k in parsed["timeline"]["knots"]]
    for frame in frames:
        times += list(
            _events(
                frame, maximum, mode, base["geometry"]["full_sensor_shape"][0], policy["latency"]
            ).values()
        )
        times.append(frame["frame_start_ns"])
    _span(times, "control_program")
    return (
        program,
        parsed,
        {
            "status": "COVERED",
            "maximum_exposure_ns": maximum,
            "scene_start_ns": coverages[0]["scene_start_ns"],
            "scene_end_ns": coverages[0]["scene_end_ns"],
            "exposure_start_ns": coverages[0]["exposure_start_ns"],
            "exposure_end_ns": coverages[-1]["exposure_end_ns"],
            "extrapolation": False,
            "interval_convention": "half_open",
        },
    )


def _apply_pending(commands, index, frame, mode, state, applied_sources):
    latch = _latch(frame, mode)
    for kind in ("ae", "awb"):
        eligible = [
            c
            for c in commands
            if c["kind"] == kind
            and c["status"] == "pending"
            and c["earliest_frame_index"] <= index
            and c["actuator_ready_ns"] <= latch
        ]
        newer = []
        for command in eligible:
            if command["source_frame_index"] <= applied_sources[kind]["source_frame_index"]:
                command["status"] = "stale"
                command["resolved_at_frame_id"] = frame["frame_id"]
            else:
                newer.append(command)
        if not newer:
            continue
        chosen = max(newer, key=lambda c: c["source_frame_index"])
        for command in newer:
            command["status"] = "applied" if command is chosen else "superseded"
            command["resolved_at_frame_id"] = frame["frame_id"]
        state.update(deepcopy(chosen["proposal"]["commanded"]))
        chosen.update(
            applied_at_ns=latch,
            applied_frame_index=index,
            applied_frame_id=frame["frame_id"],
            command_to_applied_ns=latch - chosen["issued_at_ns"],
            source_to_applied_frames=index - chosen["source_frame_index"],
        )
        applied_sources[kind] = {
            "source_frame_index": chosen["source_frame_index"],
            "command_id": chosen["command_id"],
        }


class ClosedLoopSceneRuntime:
    plugin_id = PLUGIN_ID

    def describe_capabilities(self):
        base = TemporalSceneRuntime().describe_capabilities()
        return {
            **base,
            "plugin_id": self.plugin_id,
            "mode": "closed_loop_scene",
            "supports": [s for s in base["supports"] if s not in {
                "recorded_applied_settings", "native_four_site_absolute_response"}]
            + [
                "raw_statistics_ae_awb",
                "independent_delayed_controls",
                "declared_actuator_grids",
                "command_and_applied_trace",
            ],
            "unsupported": [s for s in base["unsupported"] if s != "closed_loop_ae_awb"]
            + ["native_four_site_feedback"],
            "latency_evidence": "declared_simulation_not_hardware_observation",
            "limits": {**base["limits"], "max_frames": MAX_CONTROL_FRAMES},
        }

    def validate_request(self, request):
        try:
            _parse(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return getattr(exc, "issues", [_issue("request", str(exc))])
        return []

    def _checked(self, request):
        try:
            return _parse(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise RuntimeRequestError(
                getattr(exc, "issues", [_issue("request", str(exc))])
            ) from exc

    def estimate_resources(self, request):
        program, parsed, _ = self._checked(request)
        frames, base = len(program["frames"]), parsed["base"]
        pixels = int(np.prod(base["size"]))
        estimate = {
            "cpu_threads": 1,
            "gpu_required": False,
            "execution": "in_process",
            "working_array_bytes_estimate": int(
                parsed["unique_spectral_samples"] * 8
                + pixels * len(parsed["unique"]) * 24
                + pixels * frames * 448
                + base["image"].nbytes * 8
            ),
            "estimate_kind": "array_budget_not_measured_peak",
            "frames": frames,
            "readouts": frames,
            "output_size": list(base["size"]),
            "automatic_coarsening": False,
        }
        if base["field_psf"] is not None:
            estimate["working_array_bytes_estimate"] += (
                base["field_psf"]["working_array_bytes_estimate"] + base["kernels"].nbytes
            )
            estimate["field_psf_node_convolutions"] = (
                len(parsed["unique"]) * base["field_psf"]["convolutions"]
            )
            estimate["field_psf_fft_work_units"] = (
                len(parsed["unique"]) * base["field_psf"]["fft_work_units"]
            )
        return estimate

    def execute(self, request):
        program, parsed, coverage = self._checked(request)
        capture = TemporalCaptureSession(parsed, TemporalSceneRuntime._optical_rates(parsed))
        policy, mode = program["policy"], program["mode"]
        state = deepcopy(program["initial"])
        anchor = state["wb_gains_rgb"][1]
        sources = {kind: {"source_frame_index": -1, "command_id": None} for kind in ("ae", "awb")}
        results, trace, commands = [], [], []
        for index, frame in enumerate(program["frames"]):
            _apply_pending(commands, index, frame, mode, state, sources)
            output = capture.capture_frame(_capture_frame(frame, state, mode), seed_index=index)
            statistics = raw_statistics(
                output["arrays"]["raw_frames"][0],
                output["arrays"]["cfa_channel_index"],
                output["summary"]["raw_format"],
                policy,
            )
            events = _events(
                frame,
                state["exposure_ns"],
                mode,
                capture.geometry["full_sensor_shape"][0],
                policy["latency"],
            )
            proposals = {
                "ae": propose_ae(statistics, state, policy),
                "awb": propose_awb(statistics, state, policy, green_anchor=anchor),
            }
            issued = []
            for kind, proposal in proposals.items():
                if proposal["commanded"] is None:
                    continue
                identity = f"control-{index}-{kind}"
                commands.append(
                    {
                        "command_id": identity,
                        "kind": kind,
                        "status": "pending",
                        "source_frame_id": frame["frame_id"],
                        "source_frame_index": index,
                        "earliest_frame_index": index + policy[kind]["minimum_delay_frames"],
                        **events,
                        "issued_at_ns": events["controller_complete_ns"],
                        "proposal": proposal,
                    }
                )
                issued.append(identity)
            trace.append(
                {
                    **frame,
                    "frame_index": index,
                    "latch_at_ns": _latch(frame, mode),
                    "applied": deepcopy(state),
                    "applied_sources": deepcopy(sources),
                    "declared_events": {
                        k: v
                        for k, v in events.items()
                        if k not in {"command_arrival_ns", "actuator_ready_ns"}
                    },
                    "statistics": statistics,
                    "control_proposals": proposals,
                    "issued_command_ids": issued,
                }
            )
            results.append(output)
        for command in commands:
            if command["status"] == "pending":
                command["status"] = "pending_after_sequence"
        arrays = {
            key: (
                value
                if key in SHARED_ARRAYS
                else np.concatenate([r["arrays"][key] for r in results], axis=0)
            )
            for key, value in results[0]["arrays"].items()
            if key != "rgb"
        }
        arrays["rgb"] = arrays["rgb_frames"][0].copy()
        summary = deepcopy(results[0]["summary"])
        for key in ("control_sequence", "timing", "coverage"):
            summary.pop(key)
        frame_ids = [f["frame_id"] for f in program["frames"]]
        merge_keys = [
            "conditions_per_frame",
            "native_cfa_channel_mean_dn",
            "sampled_raw_dark_corrected_channel_mean_dn",
            "linear_unsaturated_per_frame",
            "capture_metadata",
            "isp_per_frame",
            "seed_sequence",
        ]
        for key in merge_keys:
            summary[key] = [item for result in results for item in result["summary"][key]]
        summary.update(
            plugin_id=self.plugin_id,
            frames=len(results),
            frame_ids=frame_ids,
            readout_count=len(results),
            control_program=program,
            control_program_sha256=_digest(program),
            control_policy_sha256=_digest(policy),
            maximum_exposure_coverage=coverage,
            control_trace={
                "schema": "closed_loop_control_trace_v1",
                "clock": program["clock"],
                "mode": mode,
                "frames": trace,
                "commands": commands,
                "latency_evidence": "declared_simulation_not_hardware_observation",
                "closed_loop_control_simulated": True,
                "latch_rule": "first_full_sensor_active_row",
                "queue_rule": (
                    "latest_source_eligible_per_control; older_applied_source_never_replaces_newer"
                ),
                "timestamp_accuracy": "unknown",
            },
        )
        prediction = summary["prediction"]
        prediction["values"] = [
            v for result in results for v in result["summary"]["prediction"]["values"]
        ]
        prediction["axes"][0]["values"] = frame_ids
        prediction["conditions_per_frame"] = summary["conditions_per_frame"]
        prediction["control_program_sha256"] = summary["control_program_sha256"]
        prediction["conditioning"] = "declared_scene_and_realized_applied_controls"
        summary["limitations"] = [
            s
            for s in summary["limitations"]
            if not s.startswith(("Recorded command/transport", "No 3D"))
        ] + [
            "AE uses observed black-subtracted green mean; dark current is not removed.",
            "Readout gain is a charge-to-code scale, not a general analogue/conversion-gain model.",
            "AWB assumes gray-world on common valid Bayer cells; insufficient statistics hold WB.",
            "Control latencies are declared inputs, not measured hardware events.",
            "Frame-latched exposure/gain and WB are controlled; the remaining ISP graph is fixed.",
            "No 3D, occlusion, distortion, HDR, device simulation or physical validation.",
        ]
        return {"summary": summary, "arrays": arrays}


def request_template():
    from .temporal import request_template as temporal_template

    request = temporal_template()
    recorded = request.pop("control_sequence")
    scene = request["scene_sequence"]["knots"][0]["scene"]
    shape = np.shape(scene["spectral_irradiance"])[:2]
    request["scene_sequence"]["knots"][-1]["time_ns"] = 160_000_000
    request["control_program"] = {
        "schema": "closed_loop_program_v1",
        "clock": deepcopy(recorded["clock"]),
        "mode": deepcopy(recorded["mode"]),
        "frames": [
            {"frame_id": f"control-frame-{i}", "frame_start_ns": i * 10_000_000} for i in range(16)
        ],
        "initial": {
            "exposure_ns": 1_000_000,
            "gain": 1.0,
            "wb_gains_rgb": [1.0, 1.0, 1.0],
            "isp_model_id": "authored-isp",
        },
        "policy": default_policy(shape),
    }
    return request
