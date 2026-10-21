"""Strict static charge/voltage lookup and RAW inverse interval primitives.

The physical forward coordinate is nonnegative electron charge. Voltage is the
signal before analog gain and black pedestal. The separately declared negative
inverse coordinate continues the first segment only for signed readout residuals;
it does not describe negative physical electrons. Source labels record provenance,
not physical validation. This is a memoryless piecewise-linear behavioral model.
"""

from __future__ import annotations

import math

import numpy as np

SCHEMA = "camera_charge_voltage_response_v1"
MAX_KNOTS = 256
MAX_SAMPLES = 16_777_216
MAX_CHARGE_E = 1e13
MAX_VOLTAGE_V = 1e6
MIN_SLOPE_V_PER_E = 1e-15
MAX_SLOPE_V_PER_E = 1e3

_FIELDS = {
    "schema", "units", "charge_e", "signal_voltage_v", "valid_domain",
    "signed_inverse", "source",
}
_CONDITION_LIMITS = {"temperature_c": (-273.15, 1000), "analog_gain": (1e-6, 1e6)}


def _number(value, name, minimum, maximum):
    if (type(value) not in (int, float) or not minimum <= value <= maximum
            or not math.isfinite(value)):
        raise ValueError(f"{name} requires a finite number in [{minimum}, {maximum}]")
    return float(value)


def _fields(value, expected, name):
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{name} requires the exact fields {sorted(expected)}")


def _knots(values, name, maximum):
    if type(values) is not list or not 2 <= len(values) <= MAX_KNOTS:
        raise ValueError(f"{name} requires 2..{MAX_KNOTS} explicit JSON samples")
    result = [_number(value, name, 0, maximum) for value in values]
    if result[0] != 0 or any(b <= a for a, b in zip(result[:-1], result[1:], strict=True)):
        raise ValueError(f"{name} must start at zero and increase strictly")
    result[0] = 0.0
    return result


def validate_response(response):
    """Return detached finite JSON for the exact v1 lookup contract.

    Domains are inclusive and may describe a single temperature or gain. Bounds
    are implementation limits, not physical validity claims. The authored table
    starts at (0 e, 0 V); no forward extrapolation or curve fitting is performed.
    """
    _fields(response, _FIELDS, "charge voltage response")
    if type(response["schema"]) is not str or response["schema"] != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA}")
    units = response["units"]
    _fields(units, {"charge", "voltage"}, "units")
    if (type(units["charge"]) is not str or units["charge"] != "electron"
            or type(units["voltage"]) is not str or units["voltage"] != "V"):
        raise ValueError("units must declare charge='electron' and voltage='V'")
    charge = _knots(response["charge_e"], "charge_e", MAX_CHARGE_E)
    voltage = _knots(response["signal_voltage_v"], "signal_voltage_v", MAX_VOLTAGE_V)
    if len(charge) != len(voltage):
        raise ValueError("charge_e and signal_voltage_v must have the same shape")
    slopes = [(v1 - v0) / (q1 - q0) for q0, q1, v0, v1 in zip(
        charge[:-1], charge[1:], voltage[:-1], voltage[1:], strict=True
    )]
    if any(not MIN_SLOPE_V_PER_E <= slope <= MAX_SLOPE_V_PER_E for slope in slopes):
        raise ValueError(
            f"Segment slope must be in [{MIN_SLOPE_V_PER_E}, {MAX_SLOPE_V_PER_E}] V/e"
        )
    domain = response["valid_domain"]
    _fields(domain, set(_CONDITION_LIMITS), "valid_domain")
    normalized_domain = {}
    for key, limits in _CONDITION_LIMITS.items():
        pair = domain[key]
        if type(pair) is not list or len(pair) != 2:
            raise ValueError(f"valid_domain.{key} requires [minimum, maximum]")
        pair = [_number(value, f"valid_domain.{key}", *limits) for value in pair]
        if pair[0] > pair[1]:
            raise ValueError(f"valid_domain.{key} minimum exceeds maximum")
        normalized_domain[key] = pair
    signed = response["signed_inverse"]
    _fields(signed, {"minimum_voltage_v", "method"}, "signed_inverse")
    if type(signed["method"]) is not str or signed["method"] != "origin_tangent":
        raise ValueError("signed_inverse.method must be origin_tangent")
    minimum_v = _number(signed["minimum_voltage_v"], "minimum_voltage_v", -MAX_VOLTAGE_V, 0)
    if minimum_v >= 0 or minimum_v / slopes[0] < -MAX_CHARGE_E:
        raise ValueError(
            "Signed inverse requires negative voltage and charge no lower than -1e13 e"
        )
    source = response["source"]
    _fields(source, {"kind", "description"}, "source")
    if (type(source["kind"]) is not str
            or source["kind"] not in {"estimated", "simulated", "measured"}
            or type(source["description"]) is not str
            or not 1 <= len(source["description"].strip()) <= 4000):
        raise ValueError("source requires kind and a nonempty description up to 4000 characters")
    return {
        "schema": SCHEMA,
        "units": {"charge": "electron", "voltage": "V"},
        "charge_e": charge,
        "signal_voltage_v": voltage,
        "valid_domain": normalized_domain,
        "signed_inverse": {"minimum_voltage_v": minimum_v, "method": "origin_tangent"},
        "source": dict(source),
    }


def validate_conditions(response, temperature_c, analog_gain, capacity_e):
    """Require declared operating conditions and full capacity coverage.

    Return the normalized response for subsequent forward/inverse calls. These
    mathematical primitives do not infer settings or independently choose a
    capture capacity; callers validate their fixed settings here before capture.
    """
    response = validate_response(response)
    for key, value in (("temperature_c", temperature_c), ("analog_gain", analog_gain)):
        _number(value, key, *response["valid_domain"][key])
    capacity = _number(capacity_e, "capacity_e", 0, response["charge_e"][-1])
    if capacity <= 0:
        raise ValueError("capacity_e must be positive and covered by the response")
    return response


def _samples(value, name):
    if (type(value) in (int, float) or isinstance(value, (np.integer, np.floating))) and not (
        isinstance(value, (bool, np.bool_))
    ):
        try:
            result = np.asarray(float(value), dtype=np.float64)
        except (OverflowError, ValueError):
            raise ValueError(f"{name} requires finite float64 samples") from None
    elif type(value) is np.ndarray and value.dtype.kind in "iuf":
        if value.ndim > 2 or not value.size or value.size > MAX_SAMPLES:
            raise ValueError(
                f"{name} requires nonempty 0-D/1-D/2-D arrays up to {MAX_SAMPLES} samples"
            )
        if (not np.isfinite(value).all()
                or (value.dtype.kind == "f" and value.dtype.itemsize > 8
                    and np.any(np.abs(value) > np.finfo(np.float64).max))):
            raise ValueError(f"{name} requires finite float64 samples")
        result = value.astype(np.float64, copy=False)
    else:
        raise ValueError(f"{name} requires a real scalar or numeric NumPy array")
    if not np.isfinite(result).all():
        raise ValueError(f"{name} requires finite float64 samples")
    return result


def evaluate_voltage(charge_e, response):
    """Evaluate only covered nonnegative physical charge; return float64 array.

    Scalar input returns a 0-D array. Invalid charge is an error, never a clamp.
    The result is signal voltage before analog gain and black pedestal.
    """
    response = validate_response(response)
    charge = _samples(charge_e, "charge_e")
    if np.any(charge < 0) or np.any(charge > response["charge_e"][-1]):
        raise ValueError("Physical charge exceeds response domain; no extrapolation")
    return np.asarray(np.interp(charge, response["charge_e"], response["signal_voltage_v"]))


def _inverse(voltage, charge_knots, voltage_knots, slopes):
    # Clip only interval indices: input voltage has already passed the domain
    # check. Negative voltages intentionally use the declared first tangent.
    index = np.clip(np.searchsorted(voltage_knots, voltage, side="right") - 1,
                    0, len(voltage_knots) - 2)
    charge = charge_knots[index] + (voltage - voltage_knots[index]) * slopes[index]
    # Preserve exact authored endpoint charge despite floating multiplication.
    charge = np.where(voltage == voltage_knots[-1], charge_knots[-1], charge)
    return charge, slopes[index]


def invert_voltage_interval(low_v, high_v, response):
    """Invert a closed voltage interval without clamping or implicit extrapolation.

    Arrays must have identical 0-D/1-D/2-D shapes; no broadcasting is applied.
    Nonfinite or reversed endpoints are malformed input and raise ValueError.
    Intervals outside [signed minimum, final knot] return valid_mask=False and
    NaN numeric outputs. Their knot_crossing_mask is False.

    charge_mid_e is inverse((low_v + high_v) / 2), not the charge-bound midpoint.
    inverse_slope_e_per_v is the local derivative at that voltage midpoint,
    using the right segment at an internal knot and the left at the final knot.
    knot_crossing_mask means a slope-changing internal knot is strictly inside
    the interval; touching a knot does not count. This identifies a limitation
    of a local noise approximation, not an estimated stochastic variance.
    """
    response = validate_response(response)
    low, high = _samples(low_v, "low_v"), _samples(high_v, "high_v")
    if low.shape != high.shape:
        raise ValueError("Voltage interval endpoints must have the same shape")
    if np.any(high < low):
        raise ValueError("Voltage interval low_v exceeds high_v")
    q = np.asarray(response["charge_e"])
    v = np.asarray(response["signal_voltage_v"])
    slopes = np.diff(q) / np.diff(v)
    valid = (low >= response["signed_inverse"]["minimum_voltage_v"]) & (high <= v[-1])
    result = {name: np.full(low.shape, np.nan) for name in (
        "charge_low_e", "charge_high_e", "charge_mid_e", "inverse_slope_e_per_v"
    )}
    result["valid_mask"] = np.asarray(valid)
    crossing = np.zeros(low.shape, dtype=bool)
    if np.any(valid):
        bounded_low, bounded_high = low[valid], high[valid]
        midpoint = bounded_low + (bounded_high - bounded_low) / 2
        result["charge_low_e"][valid], _ = _inverse(bounded_low, q, v, slopes)
        result["charge_high_e"][valid], _ = _inverse(bounded_high, q, v, slopes)
        result["charge_mid_e"][valid], result["inverse_slope_e_per_v"][valid] = _inverse(
            midpoint, q, v, slopes
        )
        changes = v[1:-1][slopes[:-1] != slopes[1:]]
        crossing[valid] = (np.searchsorted(changes, bounded_high, side="left")
                           > np.searchsorted(changes, bounded_low, side="right"))
    result["knot_crossing_mask"] = crossing
    return result
