"""Authored retained-photodiode charge flow, separate from read-node encoding.

Photocharge enters only during exposure; dark charge enters during gaps and
exposures. No charge is generated before each row's first exposure start. There
is no release, decay, trap, blooming, transfer-duration or measured-device model.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

import numpy as np

SCHEMA = "camera_retained_photodiode_v1"
STATE_SCHEMA = "camera_retained_photodiode_state_v1"
ROI_STATE_SCHEMA = "camera_retained_photodiode_state_v2"
MAX_PIXELS = 1_000_000
MAX_STEPS = 16
MAX_CAPACITY_E = 10**12
MAX_MEAN_E = 1e13
MAX_SAMPLED_E = 10**14
MAX_INTERVAL_S = (2**53 - 1) * 1e-9
MAX_WORKING_BYTES = 512 * 1024 * 1024
_STATE_FIELDS = {
    "schema",
    "model_sha256",
    "noise_enabled",
    "shape",
    "step_index",
    "predecessor_state_sha256",
    "charge_e",
    "state_sha256",
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _number(value, name, maximum):
    if type(value) not in (int, float) or not 0 <= value <= maximum or not math.isfinite(value):
        raise ValueError(f"{name} requires a finite number in [0,{maximum}]")
    return float(value)


def validate_model(model):
    """Normalize three explicit fields; native site order is R, Gr, Gb, B."""
    if not isinstance(model, Mapping) or set(model) != {
        "schema",
        "capacity_e",
        "transfer_efficiency",
    }:
        raise ValueError("Declare the exact retained photodiode model fields")
    if model["schema"] != SCHEMA:
        raise ValueError("Unsupported retained photodiode schema")
    capacity = model["capacity_e"]
    if type(capacity) is not int or not 1 <= capacity <= MAX_CAPACITY_E:
        raise ValueError("capacity_e requires an integer in [1,1000000000000]; no rounding")
    efficiency = model["transfer_efficiency"]
    if not isinstance(efficiency, list) or len(efficiency) != 4:
        raise ValueError("transfer_efficiency requires four native R/Gr/Gb/B values")
    return {
        "schema": SCHEMA,
        "capacity_e": capacity,
        "transfer_efficiency": [_number(v, "transfer_efficiency", 1) for v in efficiency],
    }


def _shape(shape):
    if (
        not isinstance(shape, (list, tuple))
        or len(shape) != 2
        or any(type(v) is not int or v < 2 or v % 2 for v in shape)
    ):
        raise ValueError("shape requires two positive even native receiver dimensions")
    pixels = shape[0] * shape[1]
    if pixels > MAX_PIXELS or pixels * 48 * 8 > MAX_WORKING_BYTES:
        raise ValueError("Retained charge exceeds bounded pixel/working-array limits")
    return list(shape)


def _state_digest(state):
    header = {k: value for k, value in state.items() if k not in {"charge_e", "state_sha256"}}
    dtype = "<i8" if state["noise_enabled"] else "<f8"
    # Canonical little-endian C-order bytes make state independent of array layout.
    data = np.asarray(state["charge_e"], dtype=dtype).tobytes(order="C")
    return hashlib.sha256(_canonical(header) + b"\n" + data).hexdigest()


def _make_state(charge, model_hash, noise_enabled, index, predecessor, geometry=None):
    state = {
        "schema": ROI_STATE_SCHEMA if geometry is not None else STATE_SCHEMA,
        "model_sha256": model_hash,
        "noise_enabled": noise_enabled,
        "shape": list(charge.shape),
        "step_index": index,
        "predecessor_state_sha256": predecessor,
        "charge_e": charge.copy(),
    }
    if geometry is not None:
        state.update(native_origin=list(geometry["crop_origin"]),
                     full_sensor_shape=list(geometry["full_sensor_shape"]))
    state["state_sha256"] = _state_digest(state)
    return state


def _state_geometry(shape, geometry):
    if geometry is None:
        return None
    if not isinstance(geometry, Mapping) or set(geometry) != {"crop_origin", "full_sensor_shape"}:
        raise ValueError("ROI state requires exact native origin and full sensor shape")
    origin, full = geometry["crop_origin"], geometry["full_sensor_shape"]
    if (not isinstance(origin, (list, tuple)) or not isinstance(full, (list, tuple))
            or len(origin) != 2 or len(full) != 2
            or any(type(value) is not int for value in (*origin, *full))
            or any(start < 0 or maximum < 2 or start + side > maximum
                   for start, side, maximum in zip(origin, shape, full, strict=True))
            or full[0] * full[1] > 16_777_216):
        raise ValueError("ROI state geometry must fit a bounded full native sensor")
    return {"crop_origin": list(origin), "full_sensor_shape": list(full)}


def initial_state(shape, model, *, noise_enabled, geometry=None):
    """Zero state only; each receiver row starts at its first exposure opening."""
    shape, model = _shape(shape), validate_model(model)
    geometry = _state_geometry(shape, geometry)
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean")
    charge = np.zeros(shape, dtype=np.int64 if noise_enabled else np.float64)
    return _make_state(
        charge, hashlib.sha256(_canonical(model)).hexdigest(), noise_enabled, 0, None,
        geometry,
    )


def _validate_state(state, model, noise_enabled):
    if not isinstance(state, Mapping) or set(state) not in (
        _STATE_FIELDS, _STATE_FIELDS | {"native_origin", "full_sensor_shape"}
    ):
        raise ValueError("State fields differ from the retained charge contract")
    shape = _shape(state["shape"])
    roi = state["schema"] == ROI_STATE_SCHEMA
    if roi != ("native_origin" in state):
        raise ValueError("ROI state schema and geometry differ")
    geometry = (_state_geometry(shape, {
        "crop_origin": state["native_origin"],
        "full_sensor_shape": state["full_sensor_shape"],
    }) if roi else None)
    if (
        state["schema"] not in (STATE_SCHEMA, ROI_STATE_SCHEMA)
        or type(state["noise_enabled"]) is not bool
        or state["noise_enabled"] != noise_enabled
        or state["model_sha256"] != hashlib.sha256(_canonical(model)).hexdigest()
    ):
        raise ValueError("State schema/model/mode identity mismatch")
    index = state["step_index"]
    if type(index) is not int or not 0 <= index < MAX_STEPS:
        raise ValueError("State step_index is outside the 16-step execution bound")
    predecessor = state["predecessor_state_sha256"]
    if (index == 0 and predecessor is not None) or (
        index > 0
        and (
            not isinstance(predecessor, str)
            or len(predecessor) != 64
            or any(c not in "0123456789abcdef" for c in predecessor)
        )
    ):
        raise ValueError("State predecessor identity is invalid")
    charge = state["charge_e"]
    kind = "i" if noise_enabled else "f"
    if (
        not isinstance(charge, np.ndarray)
        or list(charge.shape) != shape
        or charge.dtype.kind != kind
        or charge.dtype.itemsize != 8
    ):
        raise ValueError("State charge requires canonical-sized integer/continuous native array")
    if (
        not np.isfinite(charge).all()
        or np.any(charge < 0)
        or np.any(charge > model["capacity_e"])
        or (index == 0 and np.any(charge))
    ):
        raise ValueError("State charge is outside capacity or nonzero initial state")
    if state["state_sha256"] != _state_digest(state):
        raise ValueError("State content hash mismatch")
    return shape, charge, geometry


def _array(value, shape, name, maximum):
    if (
        not isinstance(value, np.ndarray)
        or value.dtype.kind not in "iuf"
        or list(value.shape) != shape
    ):
        raise ValueError(f"{name} requires a real native receiver HxW array")
    if not np.isfinite(value).all() or np.any(value < 0) or np.any(value > maximum):
        raise ValueError(f"{name} is outside the finite nonnegative domain")
    return np.asarray(value, dtype=np.float64)


def _gap(value, shape):
    if type(value) in (int, float):
        return np.broadcast_to(_number(value, "gap_s", MAX_INTERVAL_S), shape)
    if (
        not isinstance(value, np.ndarray)
        or value.dtype.kind not in "iuf"
        or value.shape not in {(shape[0],), tuple(shape)}
    ):
        raise ValueError("gap_s requires a scalar, receiver-row vector or HxW array")
    if not np.isfinite(value).all() or np.any(value < 0) or np.any(value > MAX_INTERVAL_S):
        raise ValueError("gap_s is outside the finite nonnegative timing domain")
    value = np.asarray(value, dtype=np.float64)
    return np.broadcast_to(value[:, None] if value.ndim == 1 else value, shape)


def _total(value):
    if value.dtype.kind not in "iu":
        return float(value.sum())
    # A valid full grid can exceed int64 in aggregate even though each site is
    # bounded. Sum small safe blocks into an arbitrary-precision Python integer.
    flat = value.reshape(-1)
    return sum(int(flat[i : i + 4096].sum()) for i in range(0, flat.size, 4096))


def _sample(mean, noise_enabled, rng):
    value = rng.poisson(mean) if noise_enabled else mean.copy()
    if noise_enabled and (np.any(value < 0) or np.any(value > MAX_SAMPLED_E)):
        raise ArithmeticError("Sampled population exceeds the bounded integer count domain")
    return value


def step(
    state,
    expected_photo_e,
    exposure_s,
    gap_s,
    model,
    prnu,
    dark_rate_e_s,
    clear_before_exposure,
    noise_enabled,
    rng,
):
    """Pure state transition; only the explicitly supplied Generator advances.

    Runtime owns exact row timestamps, full-sensor geometry, readout-capacity
    equality and event provenance. This function never performs electron-to-RAW
    encoding and never applies read noise or resets the photodiode implicitly.
    """
    model = validate_model(model)
    if type(noise_enabled) is not bool or type(clear_before_exposure) is not bool:
        raise ValueError("noise_enabled and clear_before_exposure must be boolean")
    shape, previous, geometry = _validate_state(state, model, noise_enabled)
    exposure = _number(exposure_s, "exposure_s", 3600)
    photo = _array(expected_photo_e, shape, "expected_photo_e", MAX_MEAN_E)
    if exposure == 0 and np.any(photo != 0):
        raise ValueError("A zero exposure cannot admit nonzero expected photocharge")
    response = _array(prnu, shape, "prnu", MAX_MEAN_E)
    dark = _array(dark_rate_e_s, shape, "dark_rate_e_s", MAX_MEAN_E)
    gap = _gap(gap_s, shape)
    if state["step_index"] == 0 and np.any(gap != 0):
        raise ValueError("Initial state begins at each row first exposure; first gap must be zero")
    if (noise_enabled and not isinstance(rng, np.random.Generator)) or (
        not noise_enabled and rng is not None and not isinstance(rng, np.random.Generator)
    ):
        raise ValueError("rng must be a NumPy Generator (or None for deterministic flow)")
    with np.errstate(over="ignore", invalid="ignore"):
        expected_gap = dark * gap
        expected_exposure = photo * response + dark * exposure
    for mean in (expected_gap, expected_exposure):
        if not np.isfinite(mean).all() or np.any(mean > MAX_MEAN_E):
            raise ValueError("Generated mean charge exceeds the supported 1e13 electron range")
    # Validate everything before drawing. Generated populations are sampled once.
    generated_gap = _sample(expected_gap, noise_enabled, rng)
    gap_total = previous + generated_gap
    gap_overflow = np.maximum(gap_total - model["capacity_e"], 0)
    after_gap = np.minimum(gap_total, model["capacity_e"])
    clear_loss = after_gap.copy() if clear_before_exposure else np.zeros_like(after_gap)
    generated_exposure = _sample(expected_exposure, noise_enabled, rng)
    exposure_total = after_gap - clear_loss + generated_exposure
    exposure_overflow = np.maximum(exposure_total - model["capacity_e"], 0)
    pretransfer = np.minimum(exposure_total, model["capacity_e"])
    origin = (0, 0) if geometry is None else geometry["crop_origin"]
    sites = (((np.arange(shape[0])[:, None] + origin[0]) % 2) * 2
             + (np.arange(shape[1])[None, :] + origin[1]) % 2).astype(
        np.uint8
    )
    efficiency = np.asarray(model["transfer_efficiency"])[sites]
    delivered = rng.binomial(pretransfer, efficiency) if noise_enabled else pretransfer * efficiency
    residual = pretransfer - delivered
    next_state = _make_state(
        residual,
        state["model_sha256"],
        noise_enabled,
        state["step_index"] + 1,
        state["state_sha256"],
        geometry,
    )
    arrays = {
        "initial_residual_e": previous.copy(),
        "expected_gap_e": expected_gap,
        "gap_generated_e": generated_gap,
        "gap_overflow_e": gap_overflow,
        "clear_loss_e": clear_loss,
        "expected_exposure_e": expected_exposure,
        "exposure_generated_e": generated_exposure,
        "exposure_overflow_e": exposure_overflow,
        "pretransfer_e": pretransfer,
        "delivered_e": delivered,
        "residual_e": residual,
        "native_site_index": sites,
    }
    incoming = previous + generated_gap + generated_exposure
    outgoing = gap_overflow + clear_loss + exposure_overflow + delivered + residual
    error = float(np.max(np.abs(incoming - outgoing)))
    tolerance = (
        0.0 if noise_enabled else float(16 * np.finfo(float).eps * max(1.0, float(incoming.max())))
    )
    if error > tolerance:
        raise ArithmeticError("Retained photodiode charge conservation failed")
    return {
        "state": next_state,
        "arrays": arrays,
        "summary": {
            "schema": SCHEMA,
            "model": model,
            "model_sha256": state["model_sha256"],
            "input_state_sha256": state["state_sha256"],
            "output_state_sha256": next_state["state_sha256"],
            "step_index": next_state["step_index"],
            "noise_enabled": noise_enabled,
            "clear_before_exposure": clear_before_exposure,
            "site_names": ["R", "Gr", "Gb", "B"],
            "site_index_tile": [[0, 1], [2, 3]],
            "conservation_max_error_e": error,
            "conservation_tolerance_e": tolerance,
            "estimated_working_array_bytes": math.prod(shape) * 48 * 8,
            "totals_e": {k: _total(v) for k, v in arrays.items() if k != "native_site_index"},
            "processing_order": [
                "gap_dark_generation",
                "gap_capacity_overflow",
                "optional_photodiode_clear",
                "exposure_generation",
                "exposure_capacity_overflow",
                "one_population_transfer_split",
            ],
            "assumptions": [
                "photo_inflow_gated_upstream_outside_exposure",
                "continuous_dark_during_gaps_and_exposures",
                "initial_zero_at_each_row_first_exposure_start",
                "no_release_decay_traps_or_transfer_duration_model",
                "read_node_reset_does_not_clear_photodiode",
                "deterministic_flow_is_not_stochastic_expectation_near_capacity",
            ],
        },
    }
