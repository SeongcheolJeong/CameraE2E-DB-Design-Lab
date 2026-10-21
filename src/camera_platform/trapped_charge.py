"""One authored trap species coupled to a native photodiode free population.

This is a bounded first-order split approximation, not TCAD or an exact CTMC.
Each substep applies generation, PD overflow, trap release, PD overflow, then
capture with the vacancy fraction frozen for that substep. Photo generation is
uniform within each supplied segment; callers split changing illumination at
its real boundaries. A PD clear removes free charge only. Readout is separate.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

import numpy as np

SCHEMA = "camera_trapped_photodiode_v1"
STATE_SCHEMA = "camera_trapped_photodiode_state_v1"
ROI_STATE_SCHEMA = "camera_trapped_photodiode_state_v2"
METHOD = "bounded_split_step_v1"
MAX_TRANSITION_PROBABILITY = 0.02
MAX_PIXELS = 1_000_000
MAX_STEPS = 16
MAX_SEGMENTS = 512
MAX_SUBSTEPS = 100_000
MAX_PIXEL_SUBSTEPS = 20_000_000
MAX_CAPACITY_E = 10**12
MAX_MEAN_E = 1e13
MAX_SAMPLED_E = 10**14
MAX_DURATION_NS = 2**53 - 1
MAX_ELAPSED_NS = 2**63 - 1
MAX_WORKING_BYTES = 512 * 1024 * 1024
_STATE_FIELDS = {
    "schema",
    "model_sha256",
    "noise_enabled",
    "shape",
    "step_index",
    "substep_count",
    "elapsed_ns",
    "predecessor_state_sha256",
    "charge_e",
    "trapped_e",
    "state_sha256",
}
PROCESSING_ORDER = [
    "segment_generation",
    "generated_free_capacity_overflow",
    "trap_release_to_free",
    "released_free_capacity_overflow",
    "frozen_vacancy_capture_from_free",
]


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _number(value, name, maximum, *, positive=False):
    if (
        type(value) not in (int, float)
        or not 0 <= value <= maximum
        or not math.isfinite(value)
        or (positive and value == 0)
    ):
        raise ValueError(
            f"{name} requires a bounded finite {'positive' if positive else 'nonnegative'} number"
        )
    return float(value)


def _four(value, name, *, integer=False, positive=False, maximum=MAX_CAPACITY_E):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{name} requires four native R/Gr/Gb/B values")
    if integer:
        if any(type(v) is not int or not 1 <= v <= maximum for v in value):
            raise ValueError(f"{name} requires four bounded positive integers")
        return value.copy()
    return [_number(v, name, maximum, positive=positive) for v in value]


def _effective_step(model):
    rates = [*model["capture_rate_s"], *(1 / v for v in model["release_time_s"])]
    rates.extend(
        k * model["capacity_e"] / c
        for k, c in zip(model["capture_rate_s"], model["trap_capacity_e"], strict=True)
    )
    rate = max(rates)
    maximum = model["integration"]["max_step_ns"]
    bound = -math.log1p(-MAX_TRANSITION_PROBABILITY)
    step_ns = min(maximum, int(math.floor(bound / rate * 1e9)))
    # Verify the probability after float conversion instead of rounding upward.
    while step_ns > 0 and -math.expm1(-rate * (step_ns * 1e-9)) > MAX_TRANSITION_PROBABILITY:
        step_ns -= 1
    if step_ns < 1:
        raise ValueError(
            "Trap kinetics require a sub-nanosecond step; bounded integer-ns solver refuses"
        )
    return step_ns


def validate_model(model):
    """Normalize the exact authored model and reject unresolved kinetic steps."""
    keys = {
        "schema",
        "capacity_e",
        "transfer_efficiency",
        "trap_capacity_e",
        "capture_rate_s",
        "release_time_s",
        "integration",
    }
    if not isinstance(model, Mapping) or set(model) != keys or model["schema"] != SCHEMA:
        raise ValueError("Declare the exact trapped photodiode model fields/schema")
    capacity = model["capacity_e"]
    if type(capacity) is not int or not 1 <= capacity <= MAX_CAPACITY_E:
        raise ValueError("capacity_e requires a bounded positive integer")
    integration = model["integration"]
    if (
        not isinstance(integration, Mapping)
        or set(integration) != {"method", "max_step_ns", "max_transition_probability"}
        or integration["method"] != METHOD
        or type(integration["max_step_ns"]) is not int
        or not 1 <= integration["max_step_ns"] <= 10**12
        or type(integration["max_transition_probability"]) not in (int, float)
        or integration["max_transition_probability"] != MAX_TRANSITION_PROBABILITY
    ):
        raise ValueError(
            "Declare bounded_split_step_v1, integer max_step_ns and fixed probability 0.02"
        )
    result = {
        "schema": SCHEMA,
        "capacity_e": capacity,
        "transfer_efficiency": _four(
            model["transfer_efficiency"], "transfer_efficiency", maximum=1
        ),
        "trap_capacity_e": _four(model["trap_capacity_e"], "trap_capacity_e", integer=True),
        "capture_rate_s": _four(model["capture_rate_s"], "capture_rate_s", maximum=1e9),
        "release_time_s": _four(
            model["release_time_s"], "release_time_s", positive=True, maximum=1e9
        ),
        "integration": {
            "method": METHOD,
            "max_step_ns": integration["max_step_ns"],
            "max_transition_probability": MAX_TRANSITION_PROBABILITY,
        },
    }
    _effective_step(result)
    return result


def effective_max_step_ns(model):
    """Worst-case bound for release, per-free capture and per-vacancy filling."""
    return _effective_step(validate_model(model))


def _shape(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(v) is not int or v < 2 or v % 2 for v in value)
    ):
        raise ValueError("shape requires positive even native receiver H,W")
    if math.prod(value) > MAX_PIXELS or math.prod(value) * 80 * 8 > MAX_WORKING_BYTES:
        raise ValueError("Trap state exceeds bounded pixel/working-array limits")
    return list(value)


def _sites(shape, origin=(0, 0)):
    return (((np.arange(shape[0])[:, None] + origin[0]) % 2) * 2
            + (np.arange(shape[1])[None, :] + origin[1]) % 2).astype(
        np.uint8
    )


def _state_geometry(shape, geometry):
    if geometry is None:
        return None
    if not isinstance(geometry, Mapping) or set(geometry) != {"crop_origin", "full_sensor_shape"}:
        raise ValueError("Trap ROI state requires exact native origin and full sensor shape")
    origin, full = geometry["crop_origin"], geometry["full_sensor_shape"]
    if (not isinstance(origin, (list, tuple)) or not isinstance(full, (list, tuple))
            or len(origin) != 2 or len(full) != 2
            or any(type(value) is not int for value in (*origin, *full))
            or any(start < 0 or maximum < 2 or start + side > maximum
                   for start, side, maximum in zip(origin, shape, full, strict=True))
            or full[0] * full[1] > 16_777_216):
        raise ValueError("Trap ROI state geometry must fit a bounded full native sensor")
    return {"crop_origin": list(origin), "full_sensor_shape": list(full)}


def _state_digest(state):
    arrays = {"charge_e", "trapped_e", "elapsed_ns"}
    header = {k: value for k, value in state.items() if k not in arrays | {"state_sha256"}}
    dtype = "<i8" if state["noise_enabled"] else "<f8"
    data = _canonical(header)
    for key in sorted(arrays):
        data += (
            b"\n"
            + key.encode()
            + b"\n"
            + np.asarray(state[key], dtype="<i8" if key == "elapsed_ns" else dtype).tobytes(
                order="C"
            )
        )
    return hashlib.sha256(data).hexdigest()


def _make_state(free, trapped, elapsed, model_hash, noise, index, substeps, predecessor,
                geometry=None):
    state = {
        "schema": ROI_STATE_SCHEMA if geometry is not None else STATE_SCHEMA,
        "model_sha256": model_hash,
        "noise_enabled": noise,
        "shape": list(free.shape),
        "step_index": index,
        "substep_count": substeps,
        "elapsed_ns": elapsed.copy(),
        "predecessor_state_sha256": predecessor,
        "charge_e": free.copy(),
        "trapped_e": trapped.copy(),
    }
    if geometry is not None:
        state.update(native_origin=list(geometry["crop_origin"]),
                     full_sensor_shape=list(geometry["full_sensor_shape"]))
    state["state_sha256"] = _state_digest(state)
    return state


def initial_state(shape, model, *, noise_enabled, geometry=None):
    """Empty free and trap wells at each row's first exposure; no authored preload."""
    shape, model = _shape(shape), validate_model(model)
    geometry = _state_geometry(shape, geometry)
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean")
    zero = np.zeros(shape, dtype=np.int64 if noise_enabled else np.float64)
    return _make_state(
        zero,
        zero,
        np.zeros(shape[0], dtype=np.int64),
        hashlib.sha256(_canonical(model)).hexdigest(),
        noise_enabled,
        0,
        0,
        None,
        geometry,
    )


def _validate_state(state, model, noise):
    if not isinstance(state, Mapping) or set(state) not in (
        _STATE_FIELDS, _STATE_FIELDS | {"native_origin", "full_sensor_shape"}
    ):
        raise ValueError("State fields differ from trapped charge contract")
    shape = _shape(state["shape"])
    roi = state["schema"] == ROI_STATE_SCHEMA
    if roi != ("native_origin" in state):
        raise ValueError("Trap ROI state schema and geometry differ")
    geometry = (_state_geometry(shape, {
        "crop_origin": state["native_origin"],
        "full_sensor_shape": state["full_sensor_shape"],
    }) if roi else None)
    if (
        state["schema"] not in (STATE_SCHEMA, ROI_STATE_SCHEMA)
        or type(state["noise_enabled"]) is not bool
        or state["noise_enabled"] != noise
        or state["model_sha256"] != hashlib.sha256(_canonical(model)).hexdigest()
    ):
        raise ValueError("State schema/model/mode identity mismatch")
    index, count = state["step_index"], state["substep_count"]
    if type(index) is not int or not 0 <= index < MAX_STEPS:
        raise ValueError("State step_index exceeds the 16-frame bound")
    if type(count) is not int or not 0 <= count <= index * MAX_SUBSTEPS:
        raise ValueError("State substep counter is invalid")
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
    for key, cap in (
        ("charge_e", model["capacity_e"]),
        ("trapped_e", np.asarray(model["trap_capacity_e"])[
            _sites(shape, (0, 0) if geometry is None else geometry["crop_origin"])]),
    ):
        array = state[key]
        if (
            not isinstance(array, np.ndarray)
            or list(array.shape) != shape
            or array.dtype.kind != ("i" if noise else "f")
            or array.dtype.itemsize != 8
            or not np.isfinite(array).all()
            or np.any(array < 0)
            or np.any(array > cap)
            or (index == 0 and np.any(array))
        ):
            raise ValueError(
                "State population is outside canonical native receiver capacity/initial domain"
            )
    elapsed = state["elapsed_ns"]
    if (
        not isinstance(elapsed, np.ndarray)
        or elapsed.shape != (shape[0],)
        or elapsed.dtype.kind != "i"
        or elapsed.dtype.itemsize != 8
        or np.any(elapsed < 0)
        or (index == 0 and np.any(elapsed))
    ):
        raise ValueError("State elapsed row clocks are invalid")
    if state["state_sha256"] != _state_digest(state):
        raise ValueError("State content hash mismatch")
    return shape, geometry


def _array(value, shape, name, maximum=MAX_MEAN_E):
    if (
        not isinstance(value, np.ndarray)
        or value.dtype.kind not in "iuf"
        or list(value.shape) != shape
        or not np.isfinite(value).all()
        or np.any(value < 0)
        or np.any(value > maximum)
    ):
        raise ValueError(f"{name} requires bounded finite nonnegative native HxW array")
    return np.asarray(value, dtype=np.float64)


def _duration(segment, height):
    key = "duration_ns" if "duration_ns" in segment else "duration_s"
    value = segment[key]
    if (
        not isinstance(value, np.ndarray)
        or value.shape != (height,)
        or value.dtype.kind not in ("iu" if key == "duration_ns" else "iuf")
        or not np.isfinite(value).all()
        or np.any(value < 0)
    ):
        raise ValueError("Segment duration requires a bounded native H-vector")
    if key == "duration_ns":
        if np.any(value > MAX_DURATION_NS):
            raise ValueError("Segment duration exceeds exact integer clock bound")
        return value.astype(np.int64, copy=True)
    if np.any(value > MAX_DURATION_NS * 1e-9):
        raise ValueError("Segment duration exceeds exact integer clock bound")
    nanoseconds = np.asarray(value, dtype=float) * 1e9
    rounded = np.rint(nanoseconds)
    # Permit only float conversion roundoff, never a fractional-ns authored step.
    tolerance = np.minimum(0.125, 4 * np.spacing(np.maximum(1.0, np.abs(nanoseconds))))
    if np.any(np.abs(nanoseconds - rounded) > tolerance):
        raise ValueError(
            "duration_s must represent integer nanoseconds; use duration_ns for exact clocks"
        )
    return rounded.astype(np.int64)


def prepare_segments(segments, shape, model, prnu, dark_rate_e_s):
    """Validate all generation/timing and bound work before any random draw.

    Returns {'segments': normalized detached descriptors, 'resources': ...}.
    Each descriptor has phase, duration_ns and expected_generation_e, where
    generation is an integrated mean assumed uniform over that row's segment.
    """
    shape, model = _shape(shape), validate_model(model)
    response = _array(prnu, shape, "prnu")
    dark = _array(dark_rate_e_s, shape, "dark_rate_e_s")
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
        raise ValueError("Declare 1..512 ordered gap/exposure segments")
    working = math.prod(shape) * (80 + len(segments)) * 8
    if working > MAX_WORKING_BYTES:
        raise ValueError("Segment storage exceeds bounded working-array memory")
    normalized, seen_exposure, substeps = [], False, 0
    total_mean = np.zeros(shape)
    total_ns = np.zeros(shape[0], dtype=np.int64)
    max_step = _effective_step(model)
    for segment in segments:
        if (
            not isinstance(segment, Mapping)
            or set(segment)
            not in (
                {"phase", "duration_ns", "expected_photo_e"},
                {"phase", "duration_s", "expected_photo_e"},
            )
            or not isinstance(segment["phase"], str)
            or segment["phase"] not in {"gap", "exposure"}
        ):
            raise ValueError(
                "Declare exact segment phase, duration_ns or duration_s, expected_photo_e"
            )
        phase = segment["phase"]
        if phase == "gap" and seen_exposure:
            raise ValueError("Gap segments must precede all exposure segments")
        seen_exposure |= phase == "exposure"
        duration = _duration(segment, shape[0])
        photo = _array(segment["expected_photo_e"], shape, "expected_photo_e")
        if (phase == "gap" and np.any(photo)) or np.any(photo[duration == 0]):
            raise ValueError("Gap/zero-duration rows cannot admit photocharge")
        if np.any(total_ns > MAX_DURATION_NS - duration):
            raise ValueError("Frame total duration exceeds exact clock bound")
        total_ns += duration
        with np.errstate(over="ignore", invalid="ignore"):
            mean = photo * response + dark * (duration[:, None] * 1e-9)
            total_mean += mean
        if not np.isfinite(total_mean).all() or np.any(total_mean > MAX_MEAN_E):
            raise ValueError("Total generated mean exceeds bounded 1e13 electron domain")
        count = (int(duration.max()) + max_step - 1) // max_step
        substeps += count
        if substeps > MAX_SUBSTEPS or substeps * math.prod(shape) > MAX_PIXEL_SUBSTEPS:
            raise ValueError("Trap integration exceeds bounded substep/pixel-substep budget")
        normalized.append(
            {
                "phase": phase,
                "duration_ns": duration,
                "expected_generation_e": mean.copy(),
                "substeps": count,
            }
        )
    if not seen_exposure:
        raise ValueError("A frame requires an exposure phase, including zero-duration transfer")
    return {
        "segments": normalized,
        "resources": {
            "effective_max_step_ns": max_step,
            "substeps": substeps,
            "pixel_substeps": substeps * math.prod(shape),
            "estimated_working_array_bytes": working,
            "output_array_bytes_bound": math.prod(shape) * 24 * 8,
        },
    }


def _release_population(trapped, probability, noise_enabled, rng):
    return rng.binomial(trapped, probability) if noise_enabled else trapped * probability


def _react(
    free, trapped, trap_capacity, capture_rate, release_rate, capacity_e, dt_s, noise_enabled, rng
):
    """Actual reaction subflow, with already-validated arrays/rates and bounded dt.

    Private numerical helper, not a public route for preloading reservoir state.
    It returns detached populations plus the released/captured/overflow ledger.
    """
    release_probability = -np.expm1(-release_rate * dt_s)
    released = _release_population(trapped, release_probability, noise_enabled, rng)
    trapped = trapped - released
    free = free + released
    overflow = np.maximum(free - capacity_e, 0)
    free = free - overflow
    vacancy = trap_capacity - trapped
    capture_probability = -np.expm1(-capture_rate * (vacancy / trap_capacity) * dt_s)
    attempts = (
        rng.binomial(free, capture_probability) if noise_enabled else free * capture_probability
    )
    captured = np.minimum(vacancy, attempts)
    return {
        "free": free - captured,
        "trapped": trapped + captured,
        "released": released,
        "captured": captured,
        "overflow": overflow,
        "capture_probability": capture_probability,
        "release_probability": release_probability,
    }


def _total(array):
    if array.dtype.kind not in "iu":
        return float(array.sum())
    # Safe summation even if a population's full image sum exceeds int64.
    maximum = max(1, int(array.max()))
    block = max(1, min(4096, (2**63 - 1) // maximum))
    flat = array.reshape(-1)
    return sum(int(flat[i : i + block].sum()) for i in range(0, flat.size, block))


def step(state, segments, model, prnu, dark_rate_e_s, clear_before_exposure, noise_enabled, rng):
    """Advance one immutable ordered frame then transfer one free population.

    Runtime owns absolute event clocks and changes in illumination. This core
    owns only elapsed row times, both reservoirs and the documented split solver.
    All deterministic input validation completes before the supplied RNG advances.
    """
    model = validate_model(model)
    if type(noise_enabled) is not bool or type(clear_before_exposure) is not bool:
        raise ValueError("noise_enabled and clear_before_exposure must be boolean")
    shape, geometry = _validate_state(state, model, noise_enabled)
    plan = prepare_segments(segments, shape, model, prnu, dark_rate_e_s)
    prepared, resources = plan["segments"], plan["resources"]
    if state["step_index"] == 0 and any(
        np.any(s["duration_ns"]) for s in prepared if s["phase"] == "gap"
    ):
        raise ValueError("Initial state begins at first row exposure; first gap must be zero")
    elapsed = state["elapsed_ns"].copy()
    for segment in prepared:
        if np.any(elapsed > MAX_ELAPSED_NS - segment["duration_ns"]):
            raise ValueError("Accumulated elapsed row clock overflows int64")
        elapsed += segment["duration_ns"]
    if (noise_enabled and not isinstance(rng, np.random.Generator)) or (
        not noise_enabled and rng is not None and not isinstance(rng, np.random.Generator)
    ):
        raise ValueError("rng must be NumPy Generator, or None for deterministic flow")
    sites = _sites(shape, (0, 0) if geometry is None else geometry["crop_origin"])
    capacity = model["capacity_e"]
    trap_capacity = np.asarray(model["trap_capacity_e"])[sites]
    capture_rate = np.asarray(model["capture_rate_s"])[sites]
    release_rate = (1 / np.asarray(model["release_time_s"]))[sites]
    free, trapped = state["charge_e"].copy(), state["trapped_e"].copy()
    arrays = {"initial_residual_e": free.copy(), "initial_trapped_e": trapped.copy()}
    for phase in ("gap", "exposure"):
        arrays[f"expected_{phase}_e"] = np.zeros(shape)
        for name in ("generated", "overflow", "captured", "released"):
            arrays[f"{phase}_{name}_e"] = np.zeros_like(free)
    arrays["clear_loss_e"] = np.zeros_like(free)
    cleared = False
    max_capture_probability = max_release_probability = 0.0
    for segment in prepared:
        phase, duration = segment["phase"], segment["duration_ns"]
        if phase == "exposure" and not cleared:
            if clear_before_exposure:
                arrays["clear_loss_e"] = free.copy()
                free.fill(0)
            cleared = True
        mean = segment["expected_generation_e"]
        arrays[f"expected_{phase}_e"] += mean
        remaining = duration.copy()
        for _ in range(segment["substeps"]):
            dt_ns = np.minimum(remaining, resources["effective_max_step_ns"])
            dt = dt_ns[:, None] * 1e-9
            fraction = np.divide(dt_ns, duration, out=np.zeros(shape[0]), where=duration > 0)
            submean = mean * fraction[:, None]
            generated = rng.poisson(submean) if noise_enabled else submean
            if np.any(generated < 0) or np.any(generated > MAX_SAMPLED_E):
                raise ArithmeticError("Sampled generation exceeds bounded count domain")
            if np.any(
                arrays["gap_generated_e"] + arrays["exposure_generated_e"]
                > MAX_SAMPLED_E - generated
            ):
                raise ArithmeticError(
                    "Cumulative generated population exceeds bounded count domain"
                )
            arrays[f"{phase}_generated_e"] += generated
            free += generated
            overflow = np.maximum(free - capacity, 0)
            free -= overflow
            arrays[f"{phase}_overflow_e"] += overflow
            reaction = _react(
                free,
                trapped,
                trap_capacity,
                capture_rate,
                release_rate,
                capacity,
                dt,
                noise_enabled,
                rng,
            )
            free, trapped = reaction["free"], reaction["trapped"]
            arrays[f"{phase}_overflow_e"] += reaction["overflow"]
            arrays[f"{phase}_released_e"] += reaction["released"]
            arrays[f"{phase}_captured_e"] += reaction["captured"]
            max_capture_probability = max(
                max_capture_probability, float(reaction["capture_probability"].max())
            )
            max_release_probability = max(
                max_release_probability, float(reaction["release_probability"].max())
            )
            remaining -= dt_ns
    pretransfer = free.copy()
    efficiency = np.asarray(model["transfer_efficiency"])[sites]
    delivered = rng.binomial(free, efficiency) if noise_enabled else free * efficiency
    free -= delivered
    arrays.update(
        {
            "pretransfer_e": pretransfer,
            "delivered_e": delivered,
            "residual_e": free.copy(),
            "trapped_e": trapped.copy(),
            "captured_e": arrays["gap_captured_e"] + arrays["exposure_captured_e"],
            "released_e": arrays["gap_released_e"] + arrays["exposure_released_e"],
            "native_site_index": sites,
        }
    )
    incoming = (
        arrays["initial_residual_e"]
        + arrays["initial_trapped_e"]
        + arrays["gap_generated_e"]
        + arrays["exposure_generated_e"]
    )
    outgoing = (
        arrays["gap_overflow_e"]
        + arrays["exposure_overflow_e"]
        + arrays["clear_loss_e"]
        + delivered
        + free
        + trapped
    )
    error = float(np.max(np.abs(incoming - outgoing)))
    tolerance = (
        0.0
        if noise_enabled
        else float(
            (32 + 16 * resources["substeps"])
            * np.finfo(float).eps
            * max(1.0, float(incoming.max()))
        )
    )
    if error > tolerance or not np.isfinite(error):
        raise ArithmeticError("Two-reservoir charge conservation failed")
    next_state = _make_state(
        free,
        trapped,
        elapsed,
        state["model_sha256"],
        noise_enabled,
        state["step_index"] + 1,
        state["substep_count"] + resources["substeps"],
        state["state_sha256"],
        geometry,
    )
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
            "totals_e": {k: _total(v) for k, v in arrays.items() if k != "native_site_index"},
            "conservation_max_error_e": error,
            "conservation_tolerance_e": tolerance,
            **resources,
            "max_capture_probability": max_capture_probability,
            "max_release_probability": max_release_probability,
            "integration_method": METHOD,
            "accuracy": "first_order_split_approximation_no_universal_error_bound",
            "processing_order": PROCESSING_ORDER.copy(),
            "frame_order": [
                "gap_segments",
                "optional_free_PD_clear_preserving_traps",
                "exposure_segments",
                "one_free_population_transfer",
            ],
            "assumptions": [
                "one_authored_native_trap_species",
                "empty_initial_free_and_trap_wells",
                "uniform_generation_within_each_authored_segment",
                "continuous_dark_generation",
                "deterministic_flow_is_not_stochastic_expectation",
                "not_TCAD_or_exact_CTMC",
                "trap_release_returns_to_PD_before_transfer",
                "PD_clear_does_not_clear_traps",
            ],
        },
    }
