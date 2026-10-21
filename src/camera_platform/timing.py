"""Strict relative-time contracts for recorded, frame-latched camera settings.

This module calculates exposure windows, not an electronic readout or transport
schedule. Commands remain distinct from applied settings. All arithmetic stays
integer nanoseconds; unknown observed events are never estimated.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

SCENE_SCHEMA = "timed_spectral_scene_v1"
CONTROL_SCHEMA = "recorded_control_sequence_v1"
MAX_TIME_NS = 2**53 - 1  # Exact in JSON consumers using binary64 numbers.
MAX_EXPOSURE_NS = 3600 * 1_000_000_000
MAX_FRAMES = 16
MAX_KNOTS = 128
MAX_FRAME_PIXELS = 1_048_576
EVENTS = ("sensor_readout_end_ns", "transport_end_ns", "application_arrival_ns")


class TimingError(ValueError):
    """Malformed, incompatible, out-of-coverage, or unrepresentable timing."""

    def __init__(self, message: str, field: str = "timing", code: str = "INVALID_TIMING"):
        self.issues = [{"code": code, "field": field, "message": message, "severity": "error"}]
        super().__init__(message)


def _fail(message: str, field: str, code: str = "INVALID_TIMING"):
    raise TimingError(message, field, code)


def _keys(value: Any, required: set[str], field: str, optional: set[str] | None = None):
    if (not isinstance(value, Mapping) or not required <= set(value)
            or set(value) - required - (optional or set())):
        _fail(f"{field} requires {sorted(required)}; unknown fields are refused", field)
    return value


def _integer(value: Any, field: str, low: int = -MAX_TIME_NS, high: int = MAX_TIME_NS):
    if type(value) is not int or not low <= value <= high:
        _fail(f"{field} must be a JSON integer in [{low}, {high}]", field)
    return value


def _text(value: Any, field: str, high: int = 200):
    if not isinstance(value, str) or not value.strip() or len(value) > high:
        _fail(f"{field} requires nonempty text of at most {high} characters", field)
    return value


def _gain(value: Any, field: str):
    if type(value) not in (int, float) or not 1e-6 <= value <= 1e6 or not math.isfinite(value):
        _fail("Applied/requested gain must be finite and in [1e-6,1e6]", field)
    return float(value)


def _clock(value: Any, field: str):
    clock = _keys(value, {"clock_id", "unit", "reference", "origin"}, field)
    if clock["unit"] != "ns" or clock["reference"] != "relative":
        _fail("An explicit relative nanosecond clock is required", field)
    _text(clock["clock_id"], field + ".clock_id")
    _text(clock["origin"], field + ".origin", 2000)
    return dict(clock)


def _span(times: list[int], field: str):
    if times and max(times) - min(times) > MAX_TIME_NS:
        _fail("Relative time span exceeds exact supported integer range", field, "TIME_OVERFLOW")


def _endpoint(value: int, field: str):
    if not -MAX_TIME_NS <= value <= MAX_TIME_NS:
        _fail("Derived exposure/event timestamp exceeds supported range", field, "TIME_OVERFLOW")
    return value


def _geometry(geometry: Any, frame_shape: Any):
    shape = _pair(frame_shape, "frame_shape", 2, 512)
    if any(value % 2 for value in shape):
        _fail("Bayer frame dimensions must be even", "frame_shape")
    geometry = _keys(geometry, {"kind", "full_sensor_shape", "crop_origin"}, "geometry")
    if geometry["kind"] != "native_sensor":
        _fail("Temporal exposure requires native_sensor geometry", "geometry.kind")
    full = _pair(geometry["full_sensor_shape"], "geometry.full_sensor_shape", 2, 1_000_000)
    origin = _pair(geometry["crop_origin"], "geometry.crop_origin", 0, 1_000_000)
    if any(origin[i] + shape[i] > full[i] for i in (0, 1)):
        _fail("Native crop must lie inside full sensor without resampling", "geometry")
    return shape, full, origin


def _pair(value: Any, field: str, low: int, high: int):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        _fail("A two-element integer shape/coordinate is required", field)
    return [_integer(v, field, low, high) for v in value]


def _mode(value: Any):
    data = _keys(value, {"shutter", "line_time_ns", "hidden_top_rows", "readout_basis"}, "mode")
    if data["readout_basis"] != "full_sensor_rows":
        _fail("Only an explicit full_sensor_rows readout basis is supported", "mode")
    if data["shutter"] not in ("global", "rolling"):
        _fail("Only global and rolling shutters are supported", "mode.shutter")
    line = _integer(data["line_time_ns"], "mode.line_time_ns", 0, MAX_TIME_NS)
    hidden = _integer(data["hidden_top_rows"], "mode.hidden_top_rows", 0, 1_000_000)
    if data["shutter"] == "global" and (line != 0 or hidden != 0):
        _fail("Global shutter requires zero line time and zero hidden rows", "mode")
    if data["shutter"] == "rolling" and line == 0:
        _fail("Rolling shutter requires positive line_time_ns", "mode.line_time_ns")
    return dict(data)


def _settings(value: Any, field: str, commanded: bool = False):
    time_key = "issued_at_ns" if commanded else "applied_at_ns"
    data = _keys(value, {time_key, "exposure_ns", "gain", "isp_model_id", "command_id"}, field,
                 {"wb_gains_rgb"})
    result = dict(data)
    result[time_key] = _integer(data[time_key], field + "." + time_key)
    _integer(data["exposure_ns"], field + ".exposure_ns", 1, MAX_EXPOSURE_NS)
    result["gain"] = _gain(data["gain"], field + ".gain")
    _text(data["isp_model_id"], field + ".isp_model_id")
    if commanded or data["command_id"] is not None:
        _text(data["command_id"], field + ".command_id")
    if "wb_gains_rgb" in data:
        gains = data["wb_gains_rgb"]
        if (not isinstance(gains, list) or len(gains) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v)
                       or not 1 / 16 <= v <= 16 for v in gains)):
            _fail("Explicit per-frame RGB WB gains must be three finite values in [1/16,16]",
                  field + ".wb_gains_rgb")
        result["wb_gains_rgb"] = [float(v) for v in gains]
    return result


def validate_timed_scene(data: Any, *, expanded: bool = False) -> dict[str, Any]:
    """Validate a public ID timeline, or explicitly expanded internal scene knots.

    The expanded option checks timing and the scene-object boundary only. The
    spatial runtime remains responsible for spectral samples, units, and optics.
    """
    data = _keys(data, {"schema", "clock", "interpolation", "knots"}, "scene_sequence")
    if data["schema"] != SCENE_SCHEMA:
        _fail("Unsupported timed spectral scene schema", "scene_sequence.schema")
    clock = _clock(data["clock"], "scene_sequence.clock")
    if data["interpolation"] not in ("linear_irradiance", "zero_order_hold"):
        _fail("Explicit supported irradiance interpolation is required", "interpolation")
    knots = data["knots"]
    if not isinstance(knots, list) or not 2 <= len(knots) <= MAX_KNOTS:
        _fail("A timeline needs 2..128 ordered knots", "knots")
    times = []
    scene_key = "scene" if expanded else "spectral_image_id"
    for index, knot in enumerate(knots):
        field = f"knots[{index}]"
        _keys(knot, {"time_ns", scene_key}, field)
        time = _integer(knot["time_ns"], field + ".time_ns")
        if times and time <= times[-1]:
            _fail("Knot timestamps must strictly increase", field + ".time_ns")
        times.append(time)
        if expanded:
            if not isinstance(knot[scene_key], Mapping):
                _fail("An expanded knot must contain a scene object", field + ".scene")
        else:
            _text(knot[scene_key], field + ".spectral_image_id")
    _span(times, "knots")
    return {"schema": SCENE_SCHEMA, "clock": clock, "interpolation": data["interpolation"],
            "knots": copy.deepcopy(knots)}


def row_windows(frame: Any, mode: Any, geometry: Any, frame_shape: Any) -> dict[str, list[int]]:
    """Return half-open native crop row exposure windows in integer nanoseconds."""
    shape, _, origin = _geometry(geometry, frame_shape)
    mode = _mode(mode)
    if not isinstance(frame, Mapping) or "frame_start_ns" not in frame or "applied" not in frame:
        _fail("A frame start and complete applied settings are required", "frame")
    start = _integer(frame["frame_start_ns"], "frame.frame_start_ns")
    applied = _settings(frame["applied"], "frame.applied")
    first = start + (mode["hidden_top_rows"] + origin[0]) * mode["line_time_ns"]
    starts = [_endpoint(first + row * mode["line_time_ns"], "row_start_ns")
              for row in range(shape[0])]
    ends = [_endpoint(time + applied["exposure_ns"], "row_end_ns") for time in starts]
    _span([*starts, *ends], "row_windows")
    return {"row_start_ns": starts, "row_end_ns": ends}


def validate_control_sequence(data: Any, *, geometry: Any, frame_shape: Any) -> dict[str, Any]:
    """Validate complete applied records and globally linked optional commands.

    Same-pixel exposure overlap is unsupported, but different rows from adjacent
    rolling frames may integrate simultaneously. No electronic schedule is guessed.
    """
    shape, full, _ = _geometry(geometry, frame_shape)
    data = _keys(data, {"schema", "clock", "mode", "frames"}, "control_sequence")
    if data["schema"] != CONTROL_SCHEMA:
        _fail("Unsupported recorded control schema", "control_sequence.schema")
    clock = _clock(data["clock"], "control_sequence.clock")
    mode = _mode(data["mode"])
    frames = data["frames"]
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_FRAMES:
        _fail("A control sequence needs 1..16 frames", "frames")
    if len(frames) * shape[0] * shape[1] > MAX_FRAME_PIXELS:
        _fail("Temporal sequence exceeds 1048576 total frame pixels", "frames")
    ids, commands, normalized, times = set(), {}, [], []
    previous = None
    for index, frame in enumerate(frames):
        field = f"frames[{index}]"
        _keys(frame, {"frame_id", "frame_start_ns", "applied"}, field, {"commanded", *EVENTS})
        frame_id = _text(frame["frame_id"], field + ".frame_id")
        if frame_id in ids:
            _fail("Frame IDs must be unique", field + ".frame_id")
        ids.add(frame_id)
        start = _integer(frame["frame_start_ns"], field + ".frame_start_ns")
        applied = _settings(frame["applied"], field + ".applied")
        first = _endpoint(start + mode["hidden_top_rows"] * mode["line_time_ns"], field)
        last_end = _endpoint(first + (full[0] - 1) * mode["line_time_ns"]
                             + applied["exposure_ns"], field)
        if applied["applied_at_ns"] > first:
            _fail("Applied settings must be known by the first full-sensor active row", field)
        if previous is not None:
            if start <= previous["frame_start_ns"]:
                _fail("Frame start timestamps must strictly increase", field)
            if previous["frame_start_ns"] + previous["applied"]["exposure_ns"] > start:
                _fail("Same physical pixel exposures overlap; multibuffer modes unsupported",
                      field, "OVERLAPPING_EXPOSURES")
            previous_applied = previous["applied"]
            if applied["applied_at_ns"] < previous_applied["applied_at_ns"]:
                _fail("Applied records cannot move backwards in time", field)
            if (applied["applied_at_ns"] == previous_applied["applied_at_ns"]
                    and applied != previous_applied):
                _fail("One applied event cannot declare conflicting settings or command IDs", field)
        item = {"frame_id": frame_id, "frame_start_ns": start, "applied": applied}
        if "commanded" in frame:
            command = _settings(frame["commanded"], field + ".commanded", commanded=True)
            if command["command_id"] in commands:
                _fail("Command IDs must identify one unique issued record", field + ".commanded")
            commands[command["command_id"]] = command
            item["commanded"] = command
            times.append(command["issued_at_ns"])
        observed = last_end
        for key in EVENTS:
            if key in frame:
                value = frame[key]
                if value is not None:
                    value = _integer(value, field + "." + key)
                    if value < observed:
                        _fail("Known completed-frame events must follow exposure and event order",
                              field + "." + key)
                    observed = value
                    times.append(value)
                item[key] = value
        times.extend([start, applied["applied_at_ns"], first, last_end])
        row_windows(item, mode, geometry, shape)
        normalized.append(item)
        previous = item
    for index, frame in enumerate(normalized):
        applied = frame["applied"]
        command_id = applied["command_id"]
        if command_id is not None:
            if command_id not in commands:
                _fail("Applied command_id has no issued record in this sequence",
                      f"frames[{index}]")
            if commands[command_id]["issued_at_ns"] > applied["applied_at_ns"]:
                _fail("A command cannot apply before it was issued", f"frames[{index}]")
    _span(times, "control_sequence")
    return {"schema": CONTROL_SCHEMA, "clock": clock, "mode": mode, "frames": normalized}


def validate_timing_coverage(scene: Any, control: Any, *, geometry: Any, frame_shape: Any,
                             expanded: bool = False) -> dict[str, Any]:
    """Check common clocks and full crop exposure coverage; never extrapolate."""
    scene = validate_timed_scene(scene, expanded=expanded)
    control = validate_control_sequence(control, geometry=geometry, frame_shape=frame_shape)
    if scene["clock"] != control["clock"]:
        _fail("Scene and control must declare exactly the same clock and origin", "clock",
              "CLOCK_MISMATCH")
    windows = [row_windows(frame, control["mode"], geometry, frame_shape)
               for frame in control["frames"]]
    first = min(window["row_start_ns"][0] for window in windows)
    last = max(window["row_end_ns"][-1] for window in windows)
    begin, end = scene["knots"][0]["time_ns"], scene["knots"][-1]["time_ns"]
    _span([begin, end, first, last], "scene_control_coverage")
    if first < begin or last > end:
        _fail("Every crop-row exposure must be covered by the supplied scene knots",
              "scene_sequence.knots", "OUT_OF_TIME_DOMAIN")
    return {"status": "COVERED", "clock": scene["clock"],
            "scene_start_ns": begin, "scene_end_ns": end,
            "exposure_start_ns": first, "exposure_end_ns": last,
            "interval_convention": "half_open", "extrapolation": False}


def summarize_timing(control: Any, *, geometry: Any, frame_shape: Any) -> dict[str, Any]:
    """Report exposure windows and only directly observed event differences."""
    data = validate_control_sequence(control, geometry=geometry, frame_shape=frame_shape)
    commands = {frame["commanded"]["command_id"]: frame["commanded"]
                for frame in data["frames"] if "commanded" in frame}
    frames = []
    for frame in data["frames"]:
        applied = frame["applied"]
        command = commands.get(applied["command_id"])
        observations = {key: frame.get(key) for key in EVENTS}
        readout, transport, arrival = [observations[key] for key in EVENTS]
        frames.append({
            "frame_id": frame["frame_id"], "frame_start_ns": frame["frame_start_ns"],
            "applied": dict(applied), "commanded": copy.deepcopy(command),
            **row_windows(frame, data["mode"], geometry, frame_shape), **observations,
            "command_to_applied_ns": (applied["applied_at_ns"] - command["issued_at_ns"]
                                      if command is not None else None),
            "readout_to_transport_end_ns": (
                transport - readout if readout is not None and transport is not None else None),
            "transport_end_to_application_ns": (
                arrival - transport if transport is not None and arrival is not None else None),
            "command_transmission_ns": None, "command_arrival_ns": None,
        })
    return {"schema": "recorded_timing_summary_v1", "clock": data["clock"], "mode": data["mode"],
            "frames": frames, "unknown_observed_events": "null",
            "interval_convention": "half_open", "electronic_schedule_simulated": False,
            "closed_loop_control_simulated": False, "timestamp_accuracy": "unknown"}
