"""Causal exposure AE over one persistent native retained-photodiode session.

Only completed integer RAW codes reach the policy. Optical integration uses the
realized exposure and PWM phase; charge generation/transfer and read noise each
advance their original, independent sequence RNG exactly once per frame.
"""

from collections.abc import Mapping
from copy import deepcopy

import numpy as np

from . import (
    electrical,
    electrical_control,
    retained_charge,
    trapped_charge,
    trapped_charge_runtime,
    trapped_control_runtime,
)
from .hdr_runtime import _integration
from .registry import digest
from .retained_charge_runtime import (
    EVENT_CONTRACT,
    MAX_EVIDENCE_BYTES,
    PREVIEW,
    TRAP_EVENT_CONTRACT,
    RetainedChargeRuntime,
    _paired,
    _raw_formats,
    _readout_module,
    event_schedule,
)
from .retained_charge_runtime import (
    _parse as _parse_retained,
)
from .retained_charge_runtime import (
    capture_effects as _retained_effects,
)
from .retained_charge_runtime import (
    default_capture_program as _default_retained_program,
)
from .retained_charge_runtime import default_trapped_capture_program as _default_trapped_program
from .retained_charge_runtime import (
    validate_capture_program as _validate_retained_program,
)
from .runtime import CHANNELS, RuntimeRequestError, _issue, with_sensor_effects
from .spatial import (
    _field_psf_arrays,
    _native_response_arrays,
    _native_response_summary,
    _native_response_units,
    _psf_summary,
    _radial_response_arrays,
    _radial_response_summary,
)
from .temporal import TemporalSceneRuntime
from .timing import _span, summarize_timing, validate_control_sequence, validate_timing_coverage

PLUGIN_ID = "electrical_control_v1"
PROGRAM_SCHEMA = "camera_electrical_control_capture_program_v1"
PAIRED_PROGRAM_SCHEMA = "camera_electrical_control_capture_program_v2"
TRAPPED_PROGRAM_SCHEMA = "camera_electrical_control_capture_program_v3"
PROGRAM_SCHEMAS = (PROGRAM_SCHEMA, PAIRED_PROGRAM_SCHEMA, TRAPPED_PROGRAM_SCHEMA)
METRIC_ID = "raw.electrical_control_delivered_channel_mean_dn_sequence"
OBSERVATION = "sampled_electrical_control_delivered_raw_black_relative_v1"
MAX_TRACE_BYTES = 1024 * 1024


def retained_program(payload):
    """Convert only this declared capture program to the existing PD/readout contract."""
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") not in PROGRAM_SCHEMAS
        or "electrical_control_policy" not in payload
    ):
        raise ValueError("Electrical control requires its explicit capture program and policy")
    excluded = {"electrical_control_policy"}
    if payload["schema"] == PAIRED_PROGRAM_SCHEMA:
        if payload.get("observation_readout_id") not in ("HCG", "LCG"):
            raise ValueError("Paired electrical control requires a fixed HCG or LCG observation")
        excluded.add("observation_readout_id")
    if payload["schema"] == TRAPPED_PROGRAM_SCHEMA:
        if payload.get("observation_readout_id") not in ("single", "HCG", "LCG"):
            raise ValueError(
                "Trapped electrical control requires a fixed single/HCG/LCG observation"
            )
        excluded.add("observation_readout_id")
    value = {key: deepcopy(value) for key, value in payload.items() if key not in excluded}
    value["schema"] = (
        "camera_retained_charge_capture_program_v2"
        if payload["schema"] == TRAPPED_PROGRAM_SCHEMA
        else "camera_retained_charge_capture_program_v1"
    )
    return _validate_retained_program(value)


def observation_path(program):
    """Resolve the fixed feedback path; display/replay selection cannot change it."""
    paired = _paired(program["electrical_model"])
    if program["schema"] == PROGRAM_SCHEMA and paired:
        raise ValueError("Electrical exposure control v1 supports one fixed single readout only")
    if program["schema"] == PAIRED_PROGRAM_SCHEMA:
        readout_id = program.get("observation_readout_id")
        if not paired or readout_id not in ("HCG", "LCG"):
            raise ValueError(
                "Electrical control v2 requires paired readout and a fixed HCG/LCG path"
            )
        index = ("HCG", "LCG").index(readout_id)
    elif program["schema"] == TRAPPED_PROGRAM_SCHEMA:
        readout_id = program.get("observation_readout_id")
        allowed = ("HCG", "LCG") if paired else ("single",)
        if readout_id not in allowed:
            raise ValueError(
                "Trapped electrical control observation must match its fixed readout family"
            )
        index = allowed.index(readout_id)
    elif program["schema"] == PROGRAM_SCHEMA:
        index, readout_id = 0, "single"
    else:
        raise ValueError("Unsupported electrical control program schema")
    return index, readout_id, _raw_formats(program["electrical_model"])[index]


def validate_capture_program(payload):
    retained = retained_program(payload)
    value = {**retained, "schema": payload["schema"]}
    if payload["schema"] in (PAIRED_PROGRAM_SCHEMA, TRAPPED_PROGRAM_SCHEMA):
        value["observation_readout_id"] = payload["observation_readout_id"]
    _, _, raw_format = observation_path(value)
    policy = electrical_control.validate_policy(
        payload["electrical_control_policy"],
        raw_format=raw_format,
    )
    if policy["schema"] == electrical_control.PERIOD_POLICY_SCHEMA:
        waveform = value["waveform"]
        if (
            waveform is None
            or waveform["kind"] != "pwm"
            or waveform["period_ns"] != policy["period_lattice"]["period_ns"]
            or not 0 < waveform["on_time_ns"] < waveform["period_ns"]
        ):
            raise ValueError("Whole-period AE requires a matching nonconstant PWM waveform")
    return {**value, "electrical_control_policy": policy}


def default_capture_program(legacy_readout, frame_ids, policy=None):
    result = _default_retained_program(legacy_readout, frame_ids)
    result["schema"] = PROGRAM_SCHEMA
    result["electrical_control_policy"] = (
        electrical_control.default_policy([2, 2], _raw_formats(result["electrical_model"])[0])
        if policy is None
        else deepcopy(policy)
    )
    return validate_capture_program(result)


def default_trapped_capture_program(legacy_readout, frame_ids, policy=None):
    """An authored trap AE example with an explicit fixed single feedback path."""
    result = _default_trapped_program(legacy_readout, frame_ids)
    result["schema"] = TRAPPED_PROGRAM_SCHEMA
    result["observation_readout_id"] = "single"
    result["electrical_control_policy"] = (
        electrical_control.default_policy([2, 2], _raw_formats(result["electrical_model"])[0])
        if policy is None
        else deepcopy(policy)
    )
    return validate_capture_program(result)


def capture_effects(payload):
    value = validate_capture_program(payload)
    effects = [
        *_retained_effects(retained_program(value)),
        "closed_loop_electrical_exposure_ae",
        "signed_black_relative_raw_feedback",
        "declared_control_latency",
    ]
    if value["electrical_control_policy"]["schema"] == electrical_control.PERIOD_POLICY_SCHEMA:
        effects.append("whole_pwm_period_exposure_constraint")
    return effects


def _with_exposure(control, exposure):
    result = deepcopy(control)
    for frame in result["frames"]:
        frame["applied"]["exposure_ns"] = exposure
    return result


def _parse(request):
    if not isinstance(request, Mapping) or "capture_program" not in request:
        raise ValueError("Electrical control requires an explicit capture program")
    if "retained_charge_events" in request:
        raise ValueError("Future realized electrical control events cannot be supplied as inputs")
    program = validate_capture_program(request["capture_program"])
    parsed = _parse_retained({**request, "capture_program": retained_program(program)})
    base, control = parsed["base"], parsed["control"]
    observation_index, observation_id, observation_format = observation_path(program)
    policy = electrical_control.validate_policy(
        program["electrical_control_policy"],
        frame_shape=base["size"],
        raw_format=observation_format,
    )
    initial = {
        key: deepcopy(value)
        for key, value in control["frames"][0]["applied"].items()
        if key not in {"applied_at_ns", "command_id"}
    }
    for frame in control["frames"]:
        settings = {
            key: value
            for key, value in frame["applied"].items()
            if key not in {"applied_at_ns", "command_id"}
        }
        if (
            set(frame) != {"frame_id", "frame_start_ns", "applied"}
            or settings != initial
            or frame["applied"]["command_id"] is not None
            or frame["applied"]["applied_at_ns"] != electrical_control.latch(frame, control["mode"])
        ):
            raise ValueError(
                "Electrical scaffold requires uniform initial fixed settings at each latch"
            )
    grid = policy["exposure_grid_ns"]
    exposure = initial["exposure_ns"]
    if (
        not grid["minimum"] <= exposure <= grid["maximum"]
        or (exposure - grid["minimum"]) % grid["step"]
    ):
        raise ValueError("Initial exposure must lie on the declared electrical control grid")
    if policy["schema"] == electrical_control.PERIOD_POLICY_SCHEMA:
        electrical_control._exposure(exposure, policy)
    effective = electrical_control.effective_exposure_grid(policy)
    low, high = (
        (effective["minimum"], effective["maximum"])
        if policy["enabled"]
        else (exposure, exposure)
    )
    model, temperature = program["electrical_model"], base["temperature"]
    temperature = model["reference_temperature_c"] if temperature is None else temperature
    coverage, schedules, all_times = {}, {}, []
    for label, value in (("minimum", low), ("maximum", high)):
        extreme = validate_control_sequence(
            _with_exposure(control, value), geometry=base["geometry"], frame_shape=base["size"]
        )
        coverage[label] = validate_timing_coverage(
            parsed["timeline"],
            extreme,
            geometry=base["geometry"],
            frame_shape=base["size"],
            expanded=True,
        )
        schedules[label] = event_schedule(
            extreme, retained_program(program), base["geometry"], base["size"]
        )
        for frame in extreme["frames"]:
            all_times.extend(
                electrical_control.events(
                    frame, value, control["mode"], base["size"][0], policy["latency"]
                ).values()
            )
        if _paired(model):
            _readout_module(model).validate_capture_conditions(model, value / 1e9, temperature)
        else:
            _readout_module(model).validate_capture_conditions(
                model, value / 1e9, initial["gain"], temperature
            )
    all_times += [knot["time_ns"] for knot in parsed["timeline"]["knots"]]
    _span(all_times, "electrical_control_policy")
    # Minimum preceding exposure creates the largest continuous dark gap.
    dark = parsed["generation_patterns"][1]
    durations = [high / 1e9] + [
        np.asarray(event["row_gap_ns"])[:, None] / 1e9 for event in schedules["minimum"]["frames"]
    ]
    for duration in durations:
        with np.errstate(over="ignore", invalid="ignore"):
            expected = dark * duration
        if not np.isfinite(expected).all() or np.any(expected > electrical.MAX_CHARGE_E):
            raise ValueError(
                "Reachable electrical exposure/gap dark exceeds the finite charge bound"
            )
    parsed.update(
        program=program,
        policy=policy,
        observation_index=observation_index,
        observation_id=observation_id,
        observation_format=observation_format,
        initial_settings=initial,
        maximum_control=_with_exposure(control, high),
        coverage_envelope={
            "minimum_exposure_ns": low,
            "maximum_exposure_ns": high,
            "scene_coverage": coverage,
            "grid_checked_as_interval": True,
        },
    )
    parsed["resources"] = {
        **parsed["resources"],
        "control_trace_bytes_bound": MAX_TRACE_BYTES,
        "state_scope": "one_immutable_closed_loop_sequence_job",
    }
    exposure_bytes = len(control["frames"]) * np.dtype(np.int64).itemsize
    parsed["resources"]["output_array_bytes_estimate"] += exposure_bytes
    parsed["resources"]["working_array_bytes_estimate"] += MAX_TRACE_BYTES * 2 + exposure_bytes
    if program["schema"] == TRAPPED_PROGRAM_SCHEMA:
        trapped_control_runtime.add_resources(parsed)
    if (
        parsed["resources"]["working_array_bytes_estimate"] > 1024**3
        or parsed["resources"]["output_array_bytes_estimate"] > MAX_EVIDENCE_BYTES
    ):
        raise ValueError("Electrical control exceeds bounded working memory estimate")
    return parsed


def _cancel(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError(
            "Electrical exposure control was cancelled; no partial result published"
        )


def _target_outcome(frames, policy):
    """Describe this finite code sequence without inventing a physical step/window."""
    target = policy["target_black_relative_dn"]
    values = [frame["statistics"]["value_dn"] for frame in frames]
    inside = [target["minimum"] <= value <= target["maximum"] for value in values]
    terminal = 0
    for value in reversed(inside):
        if not value:
            break
        terminal += 1
    hold_ns = (
        frames[-1]["events"]["statistics_ready_ns"]
        - frames[-terminal]["events"]["statistics_ready_ns"]
        if terminal
        else 0
    )
    result = {
        "scope": "observed_sequence_only",
        "quantity": "signed_black_relative_delivered_total_code_dn",
        "target_dn": deepcopy(target),
        "observed_frame_count": len(frames),
        "in_target_frame_count": sum(inside),
        "terminal_consecutive_in_target_frames": terminal,
        "terminal_in_target_statistics_span_ns": hold_ns,
        "hold_time_basis": "statistics_ready_ns_first_to_last_observation",
        "peak_above_upper_dn": max(0.0, max(values) - target["maximum"]),
        "peak_below_lower_dn": max(0.0, target["minimum"] - min(values)),
        "settling": "undefined_no_declared_step_window",
        "physical_stability": "not_established",
        "lfm_performance": "not_established",
    }
    if policy.get("schema") == electrical_control.PERIOD_POLICY_SCHEMA:
        from . import lfm_tradeoff

        result["period_lattice"] = {
            **electrical_control.effective_exposure_grid(policy),
            "period_ns": policy["period_lattice"]["period_ns"],
            "target_feasibility": "not_established_by_finite_raw_sequence",
            "observed_target_status": (
                "observed_in_target" if any(inside) else "not_observed_in_target"
            ),
            "phase_invariance": "ideal_pwm_window_only_not_trap_or_motion_guarantee",
        }
        result["exposure_space_diagnosis"] = lfm_tradeoff.analyze(policy, frames)
    return result


class ElectricalControlRuntime(RetainedChargeRuntime):
    plugin_id = PLUGIN_ID

    def describe_capabilities(self):
        result = super().describe_capabilities()
        result.update(plugin_id=PLUGIN_ID, mode="electrical_control")
        result["supports"] += [
            "causal_single_path_exposure_ae",
            "integer_raw_only_feedback",
            "fixed_hcg_or_lcg_exposure_feedback",
            "causal_trap_bearing_exposure_feedback",
            "finite_pwm_exposure_space_diagnosis",
        ]
        result["unsupported"] += [
            "dynamic_readout_selection",
            "gain_control",
            "awb",
            "physical_lfm_claims",
        ]
        return result

    def _checked(self, request):
        try:
            return _parse(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise RuntimeRequestError(
                getattr(exc, "issues", [_issue("request", str(exc))])
            ) from exc

    def execute(self, request):
        return run(request)

    def _execute(self, parsed, *, cancelled=None, progress=None):
        from .isp import process_raw

        base, control, program = parsed["base"], parsed["control"], parsed["program"]
        model = program["electrical_model"]
        trapped = program["schema"] == TRAPPED_PROGRAM_SCHEMA
        paired, module = _paired(model), _readout_module(model)
        formats, readouts = _raw_formats(model), ["HCG", "LCG"] if paired else ["single"]
        temperature = (
            model["reference_temperature_c"] if base["temperature"] is None else base["temperature"]
        )
        _cancel(cancelled)
        rates, flux, radius, cfa, ri_map = TemporalSceneRuntime._optical_rates(parsed)
        _cancel(cancelled)
        prnu, dark, pattern = parsed["generation_patterns"]
        # Bound every reachable photo expectation before advancing either RNG.
        # Nonnegative irradiance/PWM makes the maximum window an upper bound.
        maximum_weights = _integration({**parsed, "control": parsed["maximum_control"]})[0]
        maximum_channels = np.einsum("fsr,srwc->frwc", maximum_weights, rates, optimize=False)
        maximum_means = np.take_along_axis(maximum_channels, cfa[None, ..., None], axis=-1)[..., 0]
        maximum_exposure = parsed["coverage_envelope"]["maximum_exposure_ns"] / 1e9
        with np.errstate(over="ignore", invalid="ignore"):
            maximum_generation = maximum_means * prnu + dark * maximum_exposure
            if trapped:
                maximum_gaps = (
                    np.asarray(
                        [
                            frame["maximum_gap_ns"]
                            for frame in parsed["trap_work_envelope"]["frames"]
                        ]
                    )[:, None, None]
                    * 1e-9
                )
                maximum_generation += dark * maximum_gaps
        if (
            not np.isfinite(maximum_channels).all()
            or np.any(maximum_channels < 0)
            or np.any(maximum_channels > electrical.MAX_CHARGE_E)
            or not np.isfinite(maximum_generation).all()
            or np.any(maximum_generation > electrical.MAX_CHARGE_E)
        ):
            raise ValueError("Reachable optical generation exceeds finite electrical charge bound")
        del maximum_weights, maximum_channels, maximum_means, maximum_generation
        planned_control = control
        control = {**deepcopy(control), "frames": []}
        policy, initial = parsed["policy"], parsed["initial_settings"]
        exposure_ns, last_source, current_command = initial["exposure_ns"], -1, None
        commands, trace, events_seen = [], [], []
        weight_frames, effective_frames, ratio_frames, windows = [], [], [], []
        channel_frames, mean_frames, exposures = [], [], []
        previous_ends = None
        trap_plans = []
        if trapped:
            trap_rates = trapped_charge_runtime.native_rates(rates, cfa)
        streams = np.random.SeedSequence(base["seed"]).spawn(2)
        charge_rng, readout_rng = (np.random.default_rng(seed) for seed in streams)
        charge_model = trapped_charge if trapped else retained_charge
        state = charge_model.initial_state(
            base["size"], program["retained_charge_model"], noise_enabled=base["noise_enabled"]
        )
        ledger, encoded, frame_ledger, descriptors, preview, sampled = {}, {}, [], [], [], []
        sites = list(base.get("channels", CHANNELS))
        for index, scaffold in enumerate(planned_control["frames"]):
            _cancel(cancelled)
            pending = electrical_control.apply_pending(
                commands,
                frame_index=index,
                frame=scaffold,
                mode=control["mode"],
                exposure_ns=exposure_ns,
                last_applied_source_index=last_source,
            )
            commands, exposure_ns = pending["commands"], pending["exposure_ns"]
            last_source = pending["last_applied_source_index"]
            current_command = pending["applied_command_id"] or current_command
            frame = {
                "frame_id": scaffold["frame_id"],
                "frame_start_ns": scaffold["frame_start_ns"],
                "applied": {
                    **deepcopy(initial),
                    "exposure_ns": exposure_ns,
                    "applied_at_ns": pending["latch_at_ns"],
                    "command_id": current_command,
                },
            }
            integrated = _integration({**parsed, "control": {**control, "frames": [frame]}})
            weights_one, effective_one, ratio_one, exposure_one, windows_one = integrated
            channels_one = np.einsum("fsr,srwc->frwc", weights_one, rates, optimize=False)[0]
            means_one = np.take_along_axis(channels_one, cfa[..., None], axis=-1)[..., 0]
            if (
                not np.isfinite(channels_one).all()
                or np.any(channels_one < 0)
                or np.any(channels_one > electrical.MAX_CHARGE_E)
            ):
                raise ValueError("Integrated generation exceeds the finite electrical charge bound")
            window = windows_one[0]
            starts, ends = window["row_start_ns"], window["row_end_ns"]
            gaps = (
                [0] * len(starts)
                if previous_ends is None
                else [a - b for a, b in zip(starts, previous_ends, strict=True)]
            )
            if min(gaps) < 0:
                raise ValueError(
                    "Applied electrical control creates overlapping same-row exposures"
                )
            clear = program["pd_clear_before_exposure"][index]["clear"]
            event = {
                "frame_id": frame["frame_id"],
                "row_open_ns": starts,
                "row_transfer_ns": ends,
                "row_clear_ns": starts if clear else None,
                "row_gap_ns": gaps,
                "clear_before_exposure": clear,
            }
            previous_ends = ends
            events_seen.append(event)
            weight_frames.append(weights_one[0])
            effective_frames.append(effective_one[0])
            ratio_frames.append(ratio_one[0])
            windows.append(window)
            channel_frames.append(channels_one)
            mean_frames.append(means_one)
            exposures.append(float(exposure_one[0]))
            if trapped:
                frame_plan = trapped_control_runtime.actual_frame_plan(parsed, event, index)
                trap_plans.append(frame_plan)
                segments = trapped_charge_runtime.frame_segments(
                    parsed, frame_plan["frames"][0], trap_rates
                )
                integrated_photo = sum(
                    (
                        segment["expected_photo_e"]
                        for segment in segments
                        if segment["phase"] == "exposure"
                    ),
                    start=np.zeros(tuple(base["size"])),
                )
                if not np.allclose(integrated_photo, means_one, rtol=1e-12, atol=1e-10):
                    raise ValueError(
                        "Trap chronological photo intervals differ from optical integral"
                    )
                result = trapped_charge.step(
                    state,
                    segments,
                    program["retained_charge_model"],
                    prnu,
                    dark,
                    clear,
                    base["noise_enabled"],
                    charge_rng,
                )
            else:
                result = retained_charge.step(
                    state,
                    means_one,
                    float(exposures[index]),
                    np.array(event["row_gap_ns"]) / 1e9,
                    program["retained_charge_model"],
                    prnu,
                    dark,
                    event["clear_before_exposure"],
                    base["noise_enabled"],
                    charge_rng,
                )
            state = result["state"]
            for name, array in result["arrays"].items():
                ledger.setdefault(name, []).append(array)
            frame_ledger.append({"frame_id": frame["frame_id"], **result["summary"]})
            kwargs = {
                "temperature_c": temperature,
                "noise_enabled": base["noise_enabled"],
                "rng": readout_rng,
            }
            if not paired:
                kwargs["analog_gain"] = frame["applied"]["gain"]
            read = module.read_delivered_charge(result["arrays"]["delivered_e"], model, **kwargs)
            descriptors.append({"frame_id": frame["frame_id"], **read["readout_descriptor"]})
            for name, array in read["arrays"].items():
                encoded.setdefault(name, []).append(array if paired else array[None, ...])
            raw = read["arrays"]["raw_dn"] if paired else read["arrays"]["raw_dn"][None, ...]
            sampled.append(
                [
                    [
                        float((codes.astype(float) - fmt["black_level_dn"])[cfa == c].mean())
                        for c in range(len(sites))
                    ]
                    for codes, fmt in zip(raw, formats, strict=True)
                ]
            )
            # This adapter supplies code coordinates only. The original format remains
            # in every descriptor; no charge-response inverse or calibration is used.
            profile = deepcopy(parsed["profiles"][frame["applied"]["isp_model_id"]])
            if "wb_gains_rgb" in frame["applied"]:
                profile["white_balance"]["gains_rgb"] = frame["applied"]["wb_gains_rgb"]
            preview.append(
                np.stack(
                    [
                        process_raw(
                            codes,
                            {**fmt, "response_type": "linear"},
                            profile,
                            geometry=base["geometry"],
                        )["arrays"]["rgb"]
                        for codes, fmt in zip(raw, formats, strict=True)
                    ]
                )
            )
            declared = electrical_control.events(
                scaffold, exposure_ns, control["mode"], base["size"][0], policy["latency"]
            )
            observed_raw = raw[parsed["observation_index"]].copy()
            observed_raw.flags.writeable = False
            statistics = electrical_control.raw_statistics(
                observed_raw, parsed["observation_format"], policy
            )
            proposal = electrical_control.propose_ae(statistics, exposure_ns, policy)
            command = electrical_control.make_command(proposal, scaffold, index, declared, policy)
            issued = []
            if command is not None:
                commands.append(command)
                issued.append(command["command_id"])
                frame["commanded"] = {
                    **deepcopy(initial),
                    **proposal["commanded"],
                    "issued_at_ns": command["issued_at_ns"],
                    "command_id": command["command_id"],
                }
            control["frames"].append(frame)
            trace.append(
                {
                    "frame_id": frame["frame_id"],
                    "index": index,
                    "latch_at_ns": pending["latch_at_ns"],
                    "applied": deepcopy(frame["applied"]),
                    "applied_command_id": current_command,
                    "events": declared,
                    "statistics": statistics,
                    "proposal": proposal,
                    "issued_command_ids": issued,
                }
            )
            if progress is not None:
                progress(
                    {
                        "stage": "capture",
                        "completed_frames": index + 1,
                        "total_frames": len(planned_control["frames"]),
                    }
                )
        _cancel(cancelled)
        commands = electrical_control.finalize_pending(
            commands,
            last_frame_index=len(control["frames"]) - 1,
            last_latch_ns=electrical_control.latch(control["frames"][-1], control["mode"]),
        )
        control = validate_control_sequence(
            control, geometry=base["geometry"], frame_shape=base["size"]
        )
        parsed["events"] = event_schedule(
            control, retained_program(program), base["geometry"], base["size"]
        )
        if parsed["events"]["frames"] != events_seen:
            raise ArithmeticError("Electrical control row events diverged from actual integration")
        weights, effective, ratio = map(np.stack, (weight_frames, effective_frames, ratio_frames))
        channels, means = np.stack(channel_frames), np.stack(mean_frames)
        arrays = {
            "channel_photoelectron_rate_scenes": rates,
            "row_scene_integration_weights_s": weights,
            "channel_electrons": channels,
            "mean_electrons": means,
            **{"retained_" + key: np.stack(value) for key, value in ledger.items()},
            **{"delivered_readout_" + key: np.stack(value) for key, value in encoded.items()},
            "raw_groups": np.stack(encoded["raw_dn"]),
            "rgb_groups": np.stack(preview),
            "radius_map": radius,
            "cfa_channel_index": cfa,
            "relative_illumination_map": ri_map,
            "psf_intensity": base["kernels"].copy(),
            **_field_psf_arrays(base),
            **_radial_response_arrays(base),
            **_native_response_arrays(base),
            **parsed.get("collection_rate_arrays", {}),
            "retained_prnu": prnu,
            "retained_dark_rate_e_s": dark,
            "row_start_ns": np.asarray([w["row_start_ns"] for w in windows], dtype=np.int64),
            "row_end_ns": np.asarray([w["row_end_ns"] for w in windows], dtype=np.int64),
            "row_gap_ns": np.asarray(
                [e["row_gap_ns"] for e in parsed["events"]["frames"]], dtype=np.int64
            ),
            "waveform_row_effective_s": effective,
            "waveform_row_exposure_ratio": ratio,
            "applied_exposure_ns": np.asarray(
                [f["applied"]["exposure_ns"] for f in control["frames"]], dtype=np.int64
            ),
        }
        if trapped:
            parsed["trap_schedule"] = trapped_control_runtime.complete_schedule(parsed, trap_plans)
            arrays.update(trapped_charge_runtime.evidence(parsed["trap_schedule"], base["size"][0]))
        arrays["rgb_frames"] = arrays["rgb_groups"][:, 0].copy()
        arrays["rgb"] = arrays["rgb_frames"][0].copy()
        units = {name: "1" for name in arrays}
        units.update(
            {
                "channel_photoelectron_rate_scenes": "electron/pixel/s",
                "row_scene_integration_weights_s": "s",
                "channel_electrons": "electron/pixel",
                "mean_electrons": "electron/pixel",
                "raw_groups": "DN",
                "retained_dark_rate_e_s": "electron/pixel/s",
                "row_start_ns": "ns",
                "row_end_ns": "ns",
                "row_gap_ns": "ns",
                "waveform_row_effective_s": "s",
                "applied_exposure_ns": "ns",
            }
        )
        units.update(
            {
                "retained_" + key: "index" if key == "native_site_index" else "electron/pixel"
                for key in ledger
            }
        )
        units.update(
            {
                "delivered_readout_" + key: "DN"
                if key.endswith("dn")
                else "electron"
                if key.endswith("_e")
                else "V"
                if key.endswith("_v")
                else "1"
                for key in encoded
            }
        )
        units.update({name: "mm" for name in _field_psf_arrays(base)})
        units.update(_native_response_units(base))
        units.update({key: "electron/pixel/s" for key in parsed.get("collection_rate_arrays", {})})
        if trapped:
            units.update(
                {
                    name: "ns" if name.endswith(("start_ns", "end_ns", "duration_ns")) else "index"
                    for name in arrays
                    if name.startswith("trap_segment_")
                }
            )
        if "field_reference_radius_mm" in base:
            units.update(sensor_radius_mm="mm", response_radius_map="1")
        if (
            any(a.nbytes > 128 * 1024**2 for a in arrays.values())
            or sum(a.nbytes for a in arrays.values()) > MAX_EVIDENCE_BYTES
        ):
            raise ValueError("Retained result exceeds bounded evidence storage")
        timing = summarize_timing(control, geometry=base["geometry"], frame_shape=base["size"])
        effects = with_sensor_effects(capture_effects(program), base)
        ids = [frame["frame_id"] for frame in control["frames"]]
        summary = {
            "schema_version": (
                "camera_electrical_control_result_v2"
                if trapped
                else "camera_electrical_control_result_v1"
            ),
            "plugin_id": PLUGIN_ID,
            "source_kind": "simulated",
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "channels": sites,
            "frame_ids": ids,
            "frame_count": len(ids),
            "readout_count": len(ids) * len(readouts),
            "readout_ids": readouts,
            "frames": [
                {**deepcopy(frame), "frame_index": index, "timing": timing["frames"][index]}
                for index, frame in enumerate(control["frames"])
            ],
            "geometry": base["geometry"],
            "output_size": list(base["size"]),
            "pixel_pitch_um": base["pitch"],
            "wavelength_nm": base["wave"].tolist(),
            "raw_formats": formats,
            "capture_program": program,
            "control_sequence": control,
            "timing": timing,
            "clock": control["clock"],
            "coverage": validate_timing_coverage(
                parsed["timeline"],
                control,
                geometry=base["geometry"],
                frame_shape=base["size"],
                expanded=True,
            ),
            "maximum_exposure_coverage": parsed["coverage_envelope"],
            "included_effects": effects,
            "retained_charge": {
                "model": program["retained_charge_model"],
                "events": parsed["events"],
                "frame_ledger": frame_ledger,
                "state_scope": "one_immutable_full_sequence_job",
                "state_time_basis": "per_row_post_transfer_not_common_wall_time",
                "initial_state": (TRAP_EVENT_CONTRACT if trapped else EVENT_CONTRACT)[
                    "initial_state"
                ],
                "final_state_sha256": frame_ledger[-1]["output_state_sha256"],
                "generation_pattern": pattern,
            },
            "readout_descriptors": descriptors,
            "preview_contract": deepcopy(PREVIEW),
            "main_preview": "rgb_frames[0]",
            "preview_readout_id": readouts[0],
            "hdr_merge_applied": False,
            "current_exposure_charge_estimation": False,
            "noise_enabled": base["noise_enabled"],
            "rng": {
                "seed": base["seed"],
                "method": "numpy.SeedSequence.spawn:2",
                "charge_spawn_key": list(streams[0].spawn_key),
                "readout_spawn_key": list(streams[1].spawn_key),
                "external_state": False,
            },
            "array_units": units,
            "prediction": {
                "metric_id": METRIC_ID,
                "acquisition_mode": "closed_loop",
                "unit": "DN",
                "values": np.asarray(sampled).ravel().tolist(),
                "axes": [
                    {"name": "frame", "unit": "1", "values": ids},
                    {"name": "readout", "unit": "1", "values": readouts},
                    {"name": "channel", "unit": "1", "values": sites},
                ],
                "reference_plane": "sensor_raw",
                "included_effects": effects,
                "conditions": {},
                "source_kind": "simulated",
                "method_and_version": OBSERVATION,
                "dark_correction": "not_applied_to_delivered_code_metric",
                "uncertainty": {"kind": "unknown"},
            },
            "waveform": {
                "specification": program["waveform"],
                "on_time_correction": False,
                "scope": "uniform modulation during admitted exposure windows",
            },
            "simulation_oracle": {
                "arrays": ["retained_" + key for key in ledger],
                "used_by_prediction": False,
                "used_by_preview": False,
            },
            "scene_sources": [item["scene"]["source"] for item in parsed["unique"]],
            "psf_source": base["psf_source"],
            "flux_diagnostics_per_scene": flux,
            "limitations": [
                "Delivered total charge includes retained history and dark generation.",
                "No release, decay, charge traps, current-exposure inverse or HDR fusion.",
                "Preview colors operate on code coordinates, including nonlinear RAW.",
                "Simulation implementation evidence does not establish physical validation.",
            ],
            **_radial_response_summary(base),
            **_native_response_summary(base),
        }
        if trapped:
            summary["retained_charge"]["trap_integration"] = trapped_charge_runtime.summary(
                parsed["trap_schedule"]
            )
            summary["trap_control_work_envelope"] = deepcopy(parsed["trap_work_envelope"])
            summary["limitations"][1] = (
                "One authored trap species with bounded first-order capture/release; "
                "not exact CTMC, measured device traps, SRH/TCAD or transfer-gate kinetics."
            )
            summary["limitations"].append(
                "PD clear removes free charge only; trapped charge persists. "
                "No current-exposure inverse or HDR fusion."
            )
        if base["field_psf"] is not None:
            summary["psf"] = _psf_summary(base)
        from .registry import canonical
        from .sequences import frame_conditions

        summary["frame_conditions"] = frame_conditions(control)
        summary["electrical_control"] = {
            "schema": "camera_electrical_control_trace_v1",
            "policy": policy,
            "policy_sha256": digest(policy),
            "initial_settings": deepcopy(initial),
            "observation_source": "integer_raw_dn",
            "readout_id": parsed["observation_id"],
            "raw_format": parsed["observation_format"],
            "observation_quantity": "signed_black_relative_delivered_total_code_dn",
            "frames": trace,
            "commands": commands,
            "target_outcome": _target_outcome(trace, policy),
            "latency_evidence": "declared_simulation_not_hardware_observation",
            "source_truth_used": False,
            "clock": deepcopy(control["clock"]),
            "mode": deepcopy(control["mode"]),
            "fixed_controls": ["gain", "isp_model_id", "white_balance", "pd_clear_policy"],
        }
        if paired or trapped:
            summary["electrical_control"]["observation_readout_index"] = parsed["observation_index"]
            summary["electrical_control"]["fixed_controls"].append("observation_readout_id")
        if len(canonical(summary["electrical_control"]).encode()) > MAX_TRACE_BYTES:
            raise ValueError("Electrical control trace exceeds its bounded evidence envelope")
        summary["simulation_oracle"]["used_by_controller"] = False
        if policy["schema"] == electrical_control.PERIOD_POLICY_SCHEMA:
            summary["limitations"].append(
                "Whole-period exposure limits ideal PWM window dependence on phase; "
                "trap history, motion and real LFM performance remain unvalidated. "
                "The finite RAW sequence cannot establish target feasibility. "
                "The exposure-space diagnosis proves only which settings the frozen "
                "policy permits and what the observed RAW frames did."
            )
        summary["limitations"] += [
            "AE observes delivered signed black-relative codes; current photocharge is unknown.",
            (
                "Exposure-only fixed HCG/LCG feedback; both paths share delivered charge. "
                "Readout gains, white balance, ISP and PD clear remain fixed."
                if paired
                else "Exposure-only single-path policy; gain, white balance, ISP "
                "and PD clear remain fixed."
            ),
            "Declared command latency is simulated, not hardware timing or stability evidence.",
            "Reduced temporal variation is not a measured LFM improvement.",
        ]
        return {"arrays": arrays, "summary": summary}


def run(request, cancelled=None, progress=None):
    """Run from zero state; cancellation never returns or resumes a partial capture."""
    if any(callback is not None and not callable(callback) for callback in (cancelled, progress)):
        raise TypeError("cancelled and progress must be callables or None")
    _cancel(cancelled)
    runtime = ElectricalControlRuntime()
    parsed = runtime._checked(request)
    try:
        return runtime._execute(parsed, cancelled=cancelled, progress=progress)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise RuntimeRequestError(getattr(exc, "issues", [_issue("request", str(exc))])) from exc
