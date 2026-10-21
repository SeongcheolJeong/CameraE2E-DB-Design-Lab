"""Pure delivered-code exposure feedback and causal frame-latched commands.

Only integer RAW codes enter the meter. Exposure ratios are a bounded control
heuristic, never a charge, irradiance, or nonlinear readout inversion. All timing
is declared simulation timing on the original relative integer-nanosecond clock.
"""

import math
from collections.abc import Mapping
from copy import deepcopy
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext

import numpy as np

from .timing import MAX_EXPOSURE_NS, MAX_FRAMES, MAX_TIME_NS, _endpoint, _integer, _mode, _span

POLICY_SCHEMA = "camera_electrical_exposure_policy_v1"
PERIOD_POLICY_SCHEMA = "camera_electrical_exposure_policy_v2"
STATISTICS_SCHEMA = "camera_electrical_raw_statistics_v1"
PROPOSAL_SCHEMA = "camera_electrical_exposure_proposal_v1"
SITES = ("R", "Gr", "Gb", "B")
LATENCY_EVENTS = (
    ("readout_tail_ns", "readout_complete_ns"),
    ("frame_transport_ns", "frame_available_ns"),
    ("statistics_ns", "statistics_ready_ns"),
    ("controller_ns", "controller_complete_ns"),
    ("command_transport_ns", "command_arrival_ns"),
    ("actuator_ns", "actuator_ready_ns"),
)
EVENT_KEYS = ("full_sensor_exposure_end_ns", *(event for _, event in LATENCY_EVENTS))


def _keys(value, required, label):
    if not isinstance(value, Mapping) or set(value) != set(required):
        raise ValueError(f"{label} requires exactly {sorted(required)}")


def _number(value, label, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{label} requires a finite number in [{low},{high}]")
    return float(value)


def _shape(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} requires two even dimensions")
    result = [_integer(v, label, 2, 512) for v in value]
    if any(v % 2 for v in result):
        raise ValueError(f"{label} requires even dimensions")
    return result


def _format(raw_format):
    _keys(
        raw_format,
        {"cfa", "bit_depth", "black_level_dn", "response_type", "packing", "layout", "hdr_streams"},
        "raw_format",
    )
    if (
        raw_format["cfa"] != "RGGB"
        or raw_format["layout"] != "bayer"
        or raw_format["packing"] != "unpacked"
        or type(raw_format["hdr_streams"]) is not int
        or raw_format["hdr_streams"] != 1
        or raw_format["response_type"] not in ("linear", "nonlinear_charge_voltage")
    ):
        raise ValueError("AE requires one unchanged native RGGB electrical readout")
    bits = _integer(raw_format["bit_depth"], "bit_depth", 1, 32)
    white = 2**bits - 1
    black = _number(raw_format["black_level_dn"], "black_level_dn", 0, white)
    if black == white:
        raise ValueError("RAW black level must be below the digital white code")
    return deepcopy(dict(raw_format)), black, white


def _latency(value):
    _keys(value, {"kind", *(duration for duration, _ in LATENCY_EVENTS)}, "latency")
    if value["kind"] != "declared_simulation":
        raise ValueError("Control latency must be explicitly declared simulation timing")
    for duration, _ in LATENCY_EVENTS:
        _integer(value[duration], "latency." + duration, 0, MAX_TIME_NS)
    return deepcopy(dict(value))


def validate_policy(policy, *, frame_shape=None, raw_format=None):
    """Return a detached strict policy; camera/scenario validation remains external."""
    schema = policy.get("schema") if isinstance(policy, Mapping) else None
    required = {
        "schema",
        "enabled",
        "exposure_grid_ns",
        "target_black_relative_dn",
        "max_step_factor",
        "minimum_delay_frames",
        "maximum_delay_frames",
        "statistics_roi",
        "meter",
        "latency",
    }
    if schema == PERIOD_POLICY_SCHEMA:
        required.add("period_lattice")
    _keys(
        policy,
        required,
        "electrical_control_policy",
    )
    if schema not in (POLICY_SCHEMA, PERIOD_POLICY_SCHEMA) or type(policy["enabled"]) is not bool:
        raise ValueError("Unsupported electrical exposure policy or non-boolean enabled flag")
    grid = policy["exposure_grid_ns"]
    _keys(grid, {"minimum", "maximum", "step"}, "exposure_grid_ns")
    for key in grid:
        _integer(grid[key], "exposure_grid_ns." + key, 1, MAX_EXPOSURE_NS)
    if grid["minimum"] > grid["maximum"] or (grid["maximum"] - grid["minimum"]) % grid["step"]:
        raise ValueError("Exposure maximum must lie on the grid anchored at minimum")
    if schema == PERIOD_POLICY_SCHEMA:
        guard = policy["period_lattice"]
        _keys(guard, {"kind", "period_ns"}, "period_lattice")
        if guard["kind"] != "whole_pwm_period_v1":
            raise ValueError("Unsupported PWM period lattice")
        period_ns = _integer(guard["period_ns"], "period_lattice.period_ns", 1, MAX_EXPOSURE_NS)
        _period_lattice_grid(grid, period_ns)
    target = policy["target_black_relative_dn"]
    _keys(target, {"minimum", "maximum"}, "target_black_relative_dn")
    for key in target:
        _number(target[key], "target_black_relative_dn." + key, 1e-6, 2**32 - 1)
    if target["minimum"] > target["maximum"]:
        raise ValueError("Target minimum must not exceed maximum")
    factor = _number(policy["max_step_factor"], "max_step_factor", 1, 16)
    if factor == 1:
        raise ValueError("max_step_factor must exceed one")
    minimum = _integer(policy["minimum_delay_frames"], "minimum_delay_frames", 1, MAX_FRAMES)
    _integer(policy["maximum_delay_frames"], "maximum_delay_frames", minimum, MAX_FRAMES)
    roi = policy["statistics_roi"]
    _keys(roi, {"reference", "origin", "shape"}, "statistics_roi")
    if roi["reference"] != "native_sensor":
        raise ValueError("Electrical AE ROI must refer to the full native sensor")
    shape = _shape(roi["shape"], "statistics_roi.shape")
    if not isinstance(roi["origin"], list) or len(roi["origin"]) != 2:
        raise ValueError("Statistics origin requires two nonnegative integer coordinates")
    origin = [_integer(v, "statistics_roi.origin", 0, 510) for v in roi["origin"]]
    full = _shape(frame_shape, "frame_shape") if frame_shape is not None else [512, 512]
    if any(origin[i] + shape[i] > full[i] for i in range(2)):
        raise ValueError("Statistics ROI exceeds the full native sensor")
    meter = policy["meter"]
    _keys(meter, {"algorithm", "sites"}, "meter")
    if meter["algorithm"] != "native_site_mean_black_relative_dn_v1":
        raise ValueError("Unsupported electrical exposure meter")
    sites = meter["sites"]
    if (
        not isinstance(sites, list)
        or not 1 <= len(sites) <= 4
        or any(type(site) is not str or site not in SITES for site in sites)
        or len(set(sites)) != len(sites)
    ):
        raise ValueError("Meter sites must be unique explicit native R/Gr/Gb/B sites")
    _latency(policy["latency"])
    if raw_format is not None:
        _, black, white = _format(raw_format)
        if target["maximum"] > white - black:
            raise ValueError("Target exceeds the selected readout's black-relative code range")
    return deepcopy(dict(policy))


def _period_lattice_grid(grid, period_ns):
    """Intersect an anchored integer grid with whole PWM periods analytically."""
    low, high, step = (grid[key] for key in ("minimum", "maximum", "step"))
    divisor = math.gcd(step, period_ns)
    if low % divisor:
        raise ValueError("Exposure grid has no whole-PWM-period setting")
    modulus = period_ns // divisor
    tick = ((-low // divisor) * pow(step // divisor, -1, modulus)) % modulus if modulus > 1 else 0
    first = low + tick * step
    if first > high:
        raise ValueError("Exposure grid has no whole-PWM-period setting")
    stride = step * modulus
    count = (high - first) // stride + 1
    return {
        "minimum": first,
        "maximum": first + (count - 1) * stride,
        "step": stride,
        "point_count": count,
    }


def effective_exposure_grid(policy):
    """Settings the controller can command; no scene or sensor truth is used."""
    policy = validate_policy(policy)
    if policy["schema"] == POLICY_SCHEMA:
        grid = policy["exposure_grid_ns"]
        return {**grid, "point_count": (grid["maximum"] - grid["minimum"]) // grid["step"] + 1}
    return _period_lattice_grid(policy["exposure_grid_ns"], policy["period_lattice"]["period_ns"])


def default_policy(frame_shape=None, raw_format=None, *, initial_exposure_ns=None, shape=None):
    if shape is not None:
        if frame_shape is not None:
            raise ValueError("Provide only one frame shape")
        frame_shape = shape
    headroom = 4095 - 64 if raw_format is None else _format(raw_format)[2] - _format(raw_format)[1]
    grid = {"minimum": 100_000, "maximum": 8_000_000, "step": 100_000}
    if initial_exposure_ns is not None:
        initial = _integer(initial_exposure_ns, "initial_exposure_ns", 1, MAX_EXPOSURE_NS)
        step = max(1, initial // 10)
        grid = {
            "minimum": initial - ((initial // 2) // step) * step,
            "maximum": initial + (min(initial, MAX_EXPOSURE_NS - initial) // step) * step,
            "step": step,
        }
    return validate_policy(
        {
            "schema": POLICY_SCHEMA,
            "enabled": True,
            "exposure_grid_ns": grid,
            "target_black_relative_dn": {"minimum": headroom * 0.162, "maximum": headroom * 0.198},
            "max_step_factor": 2.0,
            "minimum_delay_frames": 1,
            "maximum_delay_frames": MAX_FRAMES,
            "statistics_roi": {
                "reference": "native_sensor",
                "origin": [0, 0],
                "shape": list(frame_shape),
            },
            "meter": {"algorithm": "native_site_mean_black_relative_dn_v1", "sites": ["Gr", "Gb"]},
            "latency": {"kind": "declared_simulation", **{key: 0 for key, _ in LATENCY_EVENTS}},
        },
        frame_shape=frame_shape,
        raw_format=raw_format,
    )


def _mean(total, count, black):
    # Cancellation near a fractional black code must use identical precision in
    # the observation and policy; ambient Decimal contexts are not a contract.
    with localcontext() as context:
        context.prec = 80
        return Decimal(total) / Decimal(count) - Decimal(str(black))


def raw_statistics(raw, raw_format, policy):
    """Include every selected ROI code, including negative black-relative and rail codes."""
    raw = np.asarray(raw)
    if raw.ndim != 2 or raw.dtype.kind not in "ui":
        raise ValueError("Feedback requires exactly one integer RAW frame")
    policy = validate_policy(policy, frame_shape=raw.shape, raw_format=raw_format)
    fmt, black, white = _format(raw_format)
    if raw.min() < 0 or raw.max() > white:
        raise ValueError("RAW contains codes outside the declared digital code range")
    row, col = policy["statistics_roi"]["origin"]
    height, width = policy["statistics_roi"]["shape"]
    codes = raw[row : row + height, col : col + width]
    yy, xx = np.indices(codes.shape)
    site_index = 2 * ((yy + row) % 2) + (xx + col) % 2
    mask = np.isin(site_index, [SITES.index(site) for site in policy["meter"]["sites"]])
    selected = codes[mask]
    count, total = int(selected.size), int(selected.astype(np.uint64).sum())
    value = float(_mean(total, count, black))
    return {
        "schema": STATISTICS_SCHEMA,
        "status": "valid",
        "reference_plane": "delivered_raw",
        "unit": "DN",
        "quantity": "signed_black_relative_delivered_code",
        "raw_format": fmt,
        "roi": deepcopy(policy["statistics_roi"]),
        "meter": deepcopy(policy["meter"]),
        "sample_count": count,
        "sum_codes_dn": total,
        "value_dn": value,
        "native_site_mean_dn": {
            site: float(
                _mean(
                    int(codes[site_index == i].astype(np.uint64).sum()),
                    int((site_index == i).sum()),
                    black,
                )
            )
            for i, site in enumerate(SITES)
        },
        "digital_low_rail_fraction": float((selected == 0).mean()),
        "digital_high_rail_fraction": float((selected == white).mean()),
        "rail_policy": "include_all_selected_codes_no_rejection",
        "excluded_sites": [site for site in SITES if site not in policy["meter"]["sites"]],
        "dark_current_correction": "none",
        "charge_or_irradiance_inversion": False,
    }


def _exposure(value, policy):
    value = _integer(value, "applied exposure_ns", 1, MAX_EXPOSURE_NS)
    grid = policy["exposure_grid_ns"]
    if not grid["minimum"] <= value <= grid["maximum"] or (value - grid["minimum"]) % grid["step"]:
        raise ValueError("Applied exposure must lie on the declared integer-nanosecond grid")
    if policy["schema"] == PERIOD_POLICY_SCHEMA and value % policy["period_lattice"]["period_ns"]:
        raise ValueError("Applied exposure must contain a whole number of declared PWM periods")
    return value


def propose_ae(statistics, exposure_ns, policy):
    """Bounded proportional code feedback; exact decimal half-grid ties round upward."""
    if not isinstance(statistics, Mapping) or statistics.get("schema") != STATISTICS_SCHEMA:
        raise ValueError("AE requires its delivered integer-RAW observation")
    _keys(
        statistics,
        {
            "schema",
            "status",
            "reference_plane",
            "unit",
            "quantity",
            "raw_format",
            "roi",
            "meter",
            "sample_count",
            "sum_codes_dn",
            "value_dn",
            "native_site_mean_dn",
            "digital_low_rail_fraction",
            "digital_high_rail_fraction",
            "rail_policy",
            "excluded_sites",
            "dark_current_correction",
            "charge_or_irradiance_inversion",
        },
        "statistics",
    )
    policy = validate_policy(policy, raw_format=statistics["raw_format"])
    exposure_ns = _exposure(exposure_ns, policy)
    if (
        statistics.get("meter") != policy["meter"]
        or statistics.get("roi") != policy["statistics_roi"]
        or statistics.get("quantity") != "signed_black_relative_delivered_code"
        or statistics.get("status") != "valid"
        or statistics.get("reference_plane") != "delivered_raw"
        or statistics.get("unit") != "DN"
        or statistics.get("rail_policy") != "include_all_selected_codes_no_rejection"
        or statistics.get("dark_current_correction") != "none"
        or statistics.get("charge_or_irradiance_inversion") is not False
    ):
        raise ValueError("Observation must match the declared delivered-code ROI and meter")
    _, black, white = _format(statistics["raw_format"])
    _number(statistics["value_dn"], "value_dn", -black, white - black)
    count = _integer(statistics["sample_count"], "sample_count", 1, 512 * 512)
    height, width = policy["statistics_roi"]["shape"]
    if count != height * width // 4 * len(policy["meter"]["sites"]):
        raise ValueError("Observation count differs from the declared native-site selection")
    total = _integer(statistics["sum_codes_dn"], "sum_codes_dn", 0, count * white)
    with localcontext() as context:
        context.prec = 80
        meter = _mean(total, count, black)
        if statistics["value_dn"] != float(meter):
            raise ValueError("Observed mean must match the recorded integer RAW sum/count")
        lower, upper = [
            Decimal(str(policy["target_black_relative_dn"][key])) for key in ("minimum", "maximum")
        ]
        base = {
            "schema": PROPOSAL_SCHEMA,
            "observed_dn": float(meter),
            "observed_exposure_ns": exposure_ns,
            "commanded": None,
            "target_error_dn": float(meter - (lower + upper) / 2),
            "interpretation": "delivered_code_feedback_not_photocharge_estimation",
        }
        if policy["schema"] == PERIOD_POLICY_SCHEMA:
            base["period_lattice"] = {
                **effective_exposure_grid(policy),
                "period_ns": policy["period_lattice"]["period_ns"],
                "feasibility": "target_reachability_unknown_from_one_raw_observation",
                "physical_lfm_performance": "not_established",
            }
        if not policy["enabled"]:
            return {**base, "status": "disabled"}
        if lower <= meter <= upper:
            return {**base, "status": "within_target"}
        factor = Decimal(str(policy["max_step_factor"]))
        requested_ratio = (lower + upper) / (2 * meter) if meter > 0 else None
        ratio = factor if requested_ratio is None else min(factor, max(1 / factor, requested_ratio))
        requested = Decimal(exposure_ns) * ratio
        effective = effective_exposure_grid(policy)
        low, high, step = [Decimal(effective[key]) for key in ("minimum", "maximum", "step")]
        bounded = min(high, max(low, requested))
        ticks = ((bounded - low) / step).to_integral_value(rounding=ROUND_HALF_UP)
        rounded = low + ticks * step
        lower_tick = ((Decimal(exposure_ns) / factor - low) / step).to_integral_value(
            rounding=ROUND_CEILING
        )
        upper_tick = ((Decimal(exposure_ns) * factor - low) / step).to_integral_value(
            rounding=ROUND_FLOOR
        )
        candidate = int(
            min(high, low + upper_tick * step, max(low, low + lower_tick * step, rounded))
        )
        return {
            **base,
            "status": "period_lattice_hold_outside_target"
            if candidate == exposure_ns and policy["schema"] == PERIOD_POLICY_SCHEMA
            else "actuator_grid_hold"
            if candidate == exposure_ns
            else "probe_dark"
            if meter <= 0
            else "update",
            "requested_ratio": None if requested_ratio is None else float(requested_ratio),
            "bounded_ratio": float(ratio),
            "requested_exposure_ns": float(requested),
            "quantized_candidate": {"exposure_ns": candidate},
            "commanded": None if candidate == exposure_ns else {"exposure_ns": candidate},
            "clamped": requested != bounded,
            "quantized": Decimal(candidate) != bounded,
            "step_limited": Decimal(candidate) != rounded,
        }


def _frame(frame):
    if not isinstance(frame, Mapping) or not isinstance(frame.get("frame_id"), str):
        raise ValueError("A source frame ID and integer start are required")
    if not frame["frame_id"].strip() or len(frame["frame_id"]) > 200:
        raise ValueError("Frame IDs require 1..200 nonblank characters")
    return _integer(frame["frame_start_ns"], "frame_start_ns")


def latch(frame, mode):
    start, mode = _frame(frame), _mode(mode)
    return _endpoint(start + mode["hidden_top_rows"] * mode["line_time_ns"], "latch_at_ns")


def events(frame, exposure_ns, mode, full_height, latency):
    """Full-frame observation readiness, then controller/command/actuator events."""
    mode, latency = _mode(mode), _latency(latency)
    exposure_ns = _integer(exposure_ns, "exposure_ns", 1, MAX_EXPOSURE_NS)
    height = _integer(full_height, "full_height", 2, 1_000_000)
    first = latch(frame, mode)
    end = _endpoint(
        first + (height - 1) * mode["line_time_ns"] + exposure_ns, "full_sensor_exposure_end_ns"
    )
    result = {"full_sensor_exposure_end_ns": end}
    for duration, name in LATENCY_EVENTS:
        end = _endpoint(end + latency[duration], name)
        result[name] = end
    _span([frame["frame_start_ns"], first, *result.values()], "electrical_control_events")
    return result


def make_command(proposal, frame, index, event_times, policy):
    policy = validate_policy(policy)
    _frame(frame)
    index = _integer(index, "source_frame_index", 0, MAX_FRAMES - 1)
    if proposal.get("schema") != PROPOSAL_SCHEMA:
        raise ValueError("Unsupported electrical exposure proposal")
    _keys(event_times, EVENT_KEYS, "command events")
    previous = _integer(event_times[EVENT_KEYS[0]], EVENT_KEYS[0])
    if previous <= frame["frame_start_ns"]:
        raise ValueError("Complete RAW cannot precede the source exposure")
    for duration, name in LATENCY_EVENTS:
        value = _integer(event_times[name], name)
        if value != _endpoint(previous + policy["latency"][duration], name):
            raise ValueError("Command events differ from the declared causal latency chain")
        previous = value
    _span([frame["frame_start_ns"], *event_times.values()], "command events")
    if proposal.get("commanded") is None:
        return None
    _keys(proposal["commanded"], {"exposure_ns"}, "commanded")
    _exposure(proposal["commanded"]["exposure_ns"], policy)
    return {
        "command_id": f"electrical-ae-{index}",
        "kind": "exposure_ae",
        "status": "pending",
        "source_frame_id": frame["frame_id"],
        "source_frame_index": index,
        "earliest_frame_index": index + policy["minimum_delay_frames"],
        "latest_frame_index": index + policy["maximum_delay_frames"],
        **deepcopy(event_times),
        "issued_at_ns": event_times["controller_complete_ns"],
        "proposal": deepcopy(proposal),
    }


def _commands(commands):
    if not isinstance(commands, list) or len(commands) > MAX_FRAMES:
        raise ValueError("Exposure queue exceeds its bounded frame count")
    result, identities, sources = deepcopy(commands), set(), set()
    for command in result:
        if command.get("kind") != "exposure_ae" or command.get("status") not in {
            "pending",
            "applied",
            "stale",
            "superseded",
            "expired",
            "pending_after_sequence",
        }:
            raise ValueError("Invalid exposure command queue record")
        source = _integer(command["source_frame_index"], "source_frame_index", 0, MAX_FRAMES - 1)
        early = _integer(
            command["earliest_frame_index"], "earliest_frame_index", source + 1, source + MAX_FRAMES
        )
        _integer(command["latest_frame_index"], "latest_frame_index", early, source + MAX_FRAMES)
        times = [_integer(command[key], key) for key in EVENT_KEYS]
        _span(times, "command events")
        if any(a > b for a, b in zip(times, times[1:], strict=False)):
            raise ValueError("Command event clocks must be monotonic")
        if (
            command["issued_at_ns"] != command["controller_complete_ns"]
            or type(command["issued_at_ns"]) is not int
        ):
            raise ValueError("A command can only be issued after its controller completes")
        _keys(command["proposal"]["commanded"], {"exposure_ns"}, "commanded")
        _integer(command["proposal"]["commanded"]["exposure_ns"], "exposure_ns", 1, MAX_EXPOSURE_NS)
        if command["command_id"] in identities or source in sources:
            raise ValueError("Exposure commands require unique source frames and identities")
        identities.add(command["command_id"])
        sources.add(source)
    return result


def apply_pending(commands, *, frame_index, frame, mode, exposure_ns, last_applied_source_index=-1):
    """Resolve eligible proposals at one latch, returning detached queue and state."""
    commands = _commands(commands)
    index = _integer(frame_index, "frame_index", 0, MAX_FRAMES - 1)
    prior = _integer(last_applied_source_index, "last_applied_source_index", -1, index - 1)
    exposure_ns = _integer(exposure_ns, "exposure_ns", 1, MAX_EXPOSURE_NS)
    at = latch(frame, mode)
    eligible = []
    for command in commands:
        if "resolved_at_ns" in command:
            if (
                _integer(command["resolved_at_ns"], "resolved_at_ns") > at
                or _integer(
                    command["resolved_at_frame_index"], "resolved_at_frame_index", 0, MAX_FRAMES - 1
                )
                > index
            ):
                raise ValueError("Command scheduler cannot move backwards in clock/frame time")
        if command["status"] != "pending":
            continue
        resolution = None
        if index > command["latest_frame_index"] and command["command_arrival_ns"] <= at:
            resolution = "expired"
        elif command["earliest_frame_index"] <= index and command["actuator_ready_ns"] <= at:
            if command["source_frame_index"] <= prior:
                resolution = "stale"
            else:
                eligible.append(command)
        if resolution:
            command.update(
                status=resolution,
                resolved_at_frame_id=frame["frame_id"],
                resolved_at_frame_index=index,
                resolved_at_ns=at,
            )
    chosen = max(eligible, key=lambda command: command["source_frame_index"], default=None)
    for command in eligible:
        command.update(
            status="applied" if command is chosen else "superseded",
            resolved_at_frame_id=frame["frame_id"],
            resolved_at_frame_index=index,
            resolved_at_ns=at,
        )
    if chosen:
        exposure_ns, prior = (
            chosen["proposal"]["commanded"]["exposure_ns"],
            chosen["source_frame_index"],
        )
        chosen.update(
            applied_at_ns=at,
            applied_frame_index=index,
            applied_frame_id=frame["frame_id"],
            command_to_applied_ns=at - chosen["issued_at_ns"],
            source_to_applied_frames=index - prior,
        )
    return {
        "commands": commands,
        "exposure_ns": exposure_ns,
        "last_applied_source_index": prior,
        "applied_command_id": None if chosen is None else chosen["command_id"],
        "latch_at_ns": at,
    }


def finalize_pending(commands, *, last_frame_index, last_latch_ns):
    """Retain every unconsumed command and why the final latch could not apply it."""
    commands = _commands(commands)
    index = _integer(last_frame_index, "last_frame_index", 0, MAX_FRAMES - 1)
    at = _integer(last_latch_ns, "last_latch_ns")
    for command in commands:
        if "resolved_at_ns" in command and (
            _integer(command["resolved_at_ns"], "resolved_at_ns") > at
            or _integer(
                command["resolved_at_frame_index"], "resolved_at_frame_index", 0, MAX_FRAMES - 1
            )
            > index
        ):
            raise ValueError("Final command outcome cannot precede an earlier resolution")
        if command["status"] != "pending":
            continue
        reasons = []
        if command["earliest_frame_index"] > index:
            reasons.append("minimum_frame_delay_after_sequence")
        if command["command_arrival_ns"] > at:
            reasons.append("command_not_arrived_by_final_latch")
        elif command["actuator_ready_ns"] > at:
            reasons.append("actuator_not_ready_by_final_latch")
        if index > command["latest_frame_index"]:
            reasons.append("maximum_frame_delay_expired")
        command.update(
            status="pending_after_sequence",
            unapplied_reasons=reasons or ["no_remaining_latch_after_command_issuance"],
        )
    return commands
