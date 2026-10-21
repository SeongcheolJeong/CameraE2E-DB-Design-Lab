"""Analytic scalar light modulation over integer, half-open nanosecond windows.

PWM is high when ``(time_ns - phase_origin_ns) % period_ns < on_time_ns``.
Its edges are ideal authored inputs, not a claim about a measured LED or clock.
Callers must establish matching clocks and apply the scalar to irradiance before
charge integration. This module neither aligns clocks nor samples a waveform.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

WAVEFORM_SCHEMA = "camera_light_waveform_v1"
MAX_TIME_NS = 2**53
_NS_PER_SECOND = 1_000_000_000


def _integer(value: Any, field: str, minimum: int = -MAX_TIME_NS) -> int:
    if type(value) is not int or not minimum <= value <= MAX_TIME_NS:
        raise ValueError(f"{field} must be an integer in [{minimum}, {MAX_TIME_NS}]")
    return value


def _level(value: Any, field: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be a finite nonnegative number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be a finite nonnegative number") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return result


def validate_waveform(spec: Any) -> dict[str, Any]:
    """Return a fresh, strict JSON-shaped constant or PWM specification.

    Time fields are integers bounded by 2**53 inclusive; booleans and floats
    (including integral floats) are refused. Levels normalize to finite floats.
    ``clock_id`` remains unchanged, so normalization cannot conceal a mismatch.
    """
    if not isinstance(spec, Mapping):
        raise ValueError("waveform must be an object")
    if spec.get("schema") != WAVEFORM_SCHEMA:
        raise ValueError("Unsupported light waveform schema")
    kind = spec.get("kind")
    if kind not in ("constant", "pwm"):
        raise ValueError("Light waveform kind must be constant or pwm")
    required = {"schema", "kind", "clock_id"}
    required |= (
        {"level"} if kind == "constant"
        else {"period_ns", "on_time_ns", "phase_origin_ns", "low", "high"}
    )
    if set(spec) != required:
        raise ValueError("Waveform fields must exactly match its kind")
    clock_id = spec["clock_id"]
    if not isinstance(clock_id, str) or not clock_id.strip():
        raise ValueError("clock_id must be a nonempty string")
    result = {"schema": WAVEFORM_SCHEMA, "kind": kind, "clock_id": clock_id}
    if kind == "constant":
        return {**result, "level": _level(spec["level"], "level")}
    period = _integer(spec["period_ns"], "period_ns", 1)
    on_time = _integer(spec["on_time_ns"], "on_time_ns", 0)
    if on_time > period:
        raise ValueError("on_time_ns must not exceed period_ns")
    low, high = _level(spec["low"], "low"), _level(spec["high"], "high")
    if high < low:
        raise ValueError("high must be greater than or equal to low")
    return {
        **result, "period_ns": period, "on_time_ns": on_time,
        "phase_origin_ns": _integer(spec["phase_origin_ns"], "phase_origin_ns"),
        "low": low, "high": high,
    }


def _interval(start_ns: Any, end_ns: Any) -> tuple[int, int]:
    start = _integer(start_ns, "start_ns")
    end = _integer(end_ns, "end_ns")
    if end < start:
        raise ValueError("end_ns must not precede start_ns")
    return start, end


def _on_moments(spec: dict, start: int, end: int, reference: int) -> tuple[int, int]:
    """Return ON duration and twice its first moment about reference, as integers.

    A head within the starting cycle, an arithmetic series of complete cycles,
    and a final tail suffice regardless of the number of PWM cycles. All time
    differences and products remain Python integers, including negative phases.
    """
    period, on_time = spec["period_ns"], spec["on_time_ns"]
    phase = (start - spec["phase_origin_ns"]) % period
    head = min(end - start, period - phase)
    head_on = min(head, max(0, on_time - phase))
    cycles, tail = divmod(end - start - head, period)
    full_start = start + head
    tail_start = full_start + cycles * period
    tail_on = min(tail, on_time)
    duration = head_on + cycles * on_time + tail_on
    twice_moment = (
        head_on * (2 * (start - reference) + head_on)
        + cycles * on_time * (2 * (full_start - reference) + on_time)
        + period * on_time * cycles * (cycles - 1)
        + tail_on * (2 * (tail_start - reference) + tail_on)
    )
    return duration, twice_moment


def _weighted_sum(low: float, off: int | float, high: float, on: int | float) -> float:
    # Nonnegative contributions avoid subtracting nearly equal floating moments.
    try:
        result = math.fsum((low * off, high * on))
    except OverflowError as exc:
        raise ValueError("Integrated waveform exceeds the finite float range") from exc
    if not math.isfinite(result):
        raise ValueError("Integrated waveform exceeds the finite float range")
    return result


def exact_on_duration_ns(spec: Any, start_ns: int, end_ns: int) -> int:
    """Return the exact integer time in PWM's high phase on [start_ns,end_ns).

    A constant specification is refused: it has no authored PWM high phase.
    Equal PWM levels still retain their authored phase and duty cycle.
    """
    waveform = validate_waveform(spec)
    start, end = _interval(start_ns, end_ns)
    if waveform["kind"] != "pwm":
        raise ValueError("exact_on_duration_ns requires a pwm waveform")
    return _on_moments(waveform, start, end, start)[0]


def integrate_waveform_ns(spec: Any, start_ns: int, end_ns: int) -> float:
    """Return integral(modulation dt) in weighted nanoseconds, without sampling."""
    waveform = validate_waveform(spec)
    start, end = _interval(start_ns, end_ns)
    duration = end - start
    if waveform["kind"] == "constant":
        return _weighted_sum(waveform["level"], duration, 0.0, 0)
    on = _on_moments(waveform, start, end, start)[0]
    return _weighted_sum(waveform["low"], duration - on, waveform["high"], on)


def phase_envelope_ns(spec: Any, duration_ns: int) -> tuple[float, float]:
    """Exact weighted-time extrema over every PWM phase for one fixed duration.

    This is a pointwise row-window diagnostic: different rows' extrema need
    not occur at the same global phase. It assumes the declared ideal periodic
    waveform, not driver jitter, phase drift, or a probability distribution.
    """
    waveform = validate_waveform(spec)
    duration = _integer(duration_ns, "duration_ns", 0)
    if waveform["kind"] == "constant":
        value = _weighted_sum(waveform["level"], duration, 0.0, 0)
        return value, value
    period, on_time = waveform["period_ns"], waveform["on_time_ns"]
    cycles, tail = divmod(duration, period)
    minimum_on = cycles * on_time + max(0, tail - (period - on_time))
    maximum_on = cycles * on_time + min(tail, on_time)
    return (
        _weighted_sum(waveform["low"], duration - minimum_on,
                      waveform["high"], minimum_on),
        _weighted_sum(waveform["low"], duration - maximum_on,
                      waveform["high"], maximum_on),
    )


def integrate_linear_basis(
    spec: Any,
    start_ns: int,
    end_ns: int,
    knot_left: int,
    knot_right: int,
    interpolation: str = "linear_irradiance",
) -> tuple[float, float]:
    """Integrate modulation times the two knot basis functions, in seconds.

    The interval is clipped to [knot_left,knot_right), without extrapolation.
    For linear irradiance the bases are (right-t)/span and (t-left)/span.
    Zero-order hold uses the left knot throughout and returns a zero right
    weight. Knot span must be positive even when the requested interval is empty.
    Integer zeroth and twice-first moments avoid absolute-time float cancellation.
    """
    waveform = validate_waveform(spec)
    start, end = _interval(start_ns, end_ns)
    left = _integer(knot_left, "knot_left")
    right = _integer(knot_right, "knot_right")
    if right <= left:
        raise ValueError("knot_right must be greater than knot_left")
    if interpolation not in ("linear_irradiance", "zero_order_hold"):
        raise ValueError("Unsupported irradiance interpolation")
    start, end = max(start, left), min(end, right)
    if end <= start:
        return 0.0, 0.0
    span, duration = right - left, end - start
    if waveform["kind"] == "constant":
        low = high = waveform["level"]
        on, on_moment = 0, 0
    else:
        low, high = waveform["low"], waveform["high"]
        on, on_moment = _on_moments(waveform, start, end, left)
    if interpolation == "zero_order_hold":
        return _weighted_sum(low, (duration - on) / _NS_PER_SECOND,
                             high, on / _NS_PER_SECOND), 0.0
    total_moment = duration * ((start - left) + (end - left))
    total_left = 2 * span * duration - total_moment
    on_left = 2 * span * on - on_moment
    denominator = 2 * span * _NS_PER_SECOND
    return (
        _weighted_sum(low, (total_left - on_left) / denominator,
                      high, on_left / denominator),
        _weighted_sum(low, (total_moment - on_moment) / denominator,
                      high, on_moment / denominator),
    )
