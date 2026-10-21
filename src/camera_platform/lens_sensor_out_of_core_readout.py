"""Opt-in, bounded-transient-memory digital RAW sampling from a mean map.

This is the same *physical readout order* as ``resample_sensor_capture`` for a
single linear mosaic exposure: dark charge, shot noise, optional response FPN,
well clipping, signed readout bias, read noise, gain, and ADC.  The random
realization deliberately differs from NumPy's native sequential generator.
Each random variate is keyed by seed, absolute row, absolute column and stream,
so changing the row batch size cannot change a capture.  Pixel FPN generated
from declared sigmas is regenerated from the fixed-pattern seed rather than
stored as a whole-frame array.  Supplied calibration maps are honored.

The caller supplies either a complete mean-electron image or a declared ROI
in the full Sensor's global coordinates, preferably as a read-only
``np.memmap``, and an explicit resource policy. Outputs are .npy memmaps on
disk; this module never returns a full Sensor clone. The policy estimates
transient arrays and disk space but is not an operating-system RSS limit.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats
from scipy.special import ndtri

from pyisetcam.sensor_capture import _readout_values, _require_single_linear_capture
from pyisetcam.types import Sensor

SCHEMA = "camera_lens_sensor_out_of_core_readout_v1"
RANDOM_CONTRACT = "absolute_row_col_splitmix64_quantile_v1"
_UINT64_MASK = (1 << 64) - 1
_ROW_SALT = np.uint64(0xD2B74407B1CE6E93)
_COL_SALT = np.uint64(0xCA5A826395121157)
_STREAM_SALT = np.uint64(0x9E3779B97F4A7C15)
_STREAM_SHOT = 1
_STREAM_READ = 2
_STREAM_PIXEL_OFFSET = 3
_STREAM_PIXEL_GAIN = 4
_STREAM_COLUMN_OFFSET = 5
_STREAM_COLUMN_GAIN = 6
_OUTPUT_BYTES_PER_CELL = 4 + 8 + 8  # raw int32, reference/volts float64
_OUTPUT_HEADER_ALLOWANCE_BYTES = 12 * 1024
# This estimates simultaneous NumPy/SciPy intermediates, including quantile
# evaluation and FPN. It is deliberately conservative, not a proven RSS bound.
_POLICY_TRANSIENT_BYTES_PER_CHUNK_CELL = 512
_POLICY_COLUMN_BYTES_PER_CELL = 256


@dataclass(frozen=True)
class OutOfCoreReadoutPolicy:
    """Caller-declared admission caps, none of which are hardware limits."""

    max_sensor_cells: int
    max_row_chunk_cells: int
    max_transient_bytes: int
    max_output_bytes: int
    max_resident_input_bytes: int
    min_free_after_output_bytes: int

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            minimum = 0 if name == "min_free_after_output_bytes" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be a positive integer policy cap.")


def _integer_seed(value: Any, name: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be an integer in [0, 2**64).")
    result = int(value)
    if result < 0 or result > _UINT64_MASK:
        raise ValueError(f"{name} must be an integer in [0, 2**64).")
    return result


def _splitmix64(value: np.ndarray | np.uint64) -> np.ndarray:
    """Vectorized 64-bit finalizer; overflow is the specified modulo-2**64 operation."""
    with np.errstate(over="ignore"):
        z = np.asarray(value, dtype=np.uint64) + np.uint64(0x9E3779B97F4A7C15)
        z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        return z ^ (z >> np.uint64(31))


def _uniform(
    seed: int, rows: np.ndarray, cols: np.ndarray, stream: int
) -> np.ndarray:
    """Open-interval uniform values keyed by absolute sensor coordinates."""
    with np.errstate(over="ignore"):
        row_key = _splitmix64(rows[:, None] + _ROW_SALT)
        col_key = _splitmix64(cols[None, :] + _COL_SALT)
        stream_key = _splitmix64(np.uint64(seed) + np.uint64(stream) * _STREAM_SALT)
        bits = _splitmix64(row_key ^ col_key ^ stream_key)
    return ((bits >> np.uint64(11)).astype(np.float64) + 0.5) * (2.0**-53)


def _normal(
    seed: int, rows: np.ndarray, cols: np.ndarray, stream: int
) -> np.ndarray:
    return ndtri(_uniform(seed, rows, cols, stream))


def _resident_payload_bytes(item: Any) -> int:
    """Count retained ndarray owners once; memmap-backed views stay disk-backed.

    A tiny view can retain a much larger resident allocation.  Walking to the
    ultimate ndarray owner avoids admitting that allocation as the view's small
    ``nbytes``.  Arrays backed by an opaque external buffer are rejected
    because its retained size cannot be established from the view.
    """
    visited_containers: set[int] = set()
    visited_owners: set[int] = set()

    def walk(value: Any) -> int:
        if isinstance(value, np.ndarray):
            owner = value
            chain: set[int] = set()
            while isinstance(owner.base, np.ndarray):
                if id(owner) in chain:
                    raise ValueError("Cyclic ndarray base chain is unsupported.")
                chain.add(id(owner))
                owner = owner.base
            if id(owner) in visited_owners:
                return 0
            visited_owners.add(id(owner))
            if isinstance(owner, np.memmap):
                return 0
            if owner.base is not None:
                raise ValueError("Opaque external-buffer ndarray owner is unsupported.")
            return int(owner.nbytes)
        if id(value) in visited_containers:
            return 0
        visited_containers.add(id(value))
        if isinstance(value, dict):
            return sum(walk(child) for child in value.values())
        if isinstance(value, (tuple, list)):
            return sum(walk(child) for child in value)
        return 0

    return walk(item)


def _shape(
    sensor: Sensor,
    mean_electrons: np.ndarray,
    global_sensor_roi_bounds_row_col_exclusive: tuple[int, int, int, int] | None,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int, int, int]]:
    if not isinstance(sensor, Sensor):
        raise ValueError("A CameraE2E Sensor is required.")
    if sensor.fields.get("sensor_output_mode") != "digital_raw":
        raise ValueError("A configured digital_raw sensor is required.")
    if not isinstance(mean_electrons, np.ndarray) or mean_electrons.ndim != 2:
        raise ValueError("Mean electrons must be a two-dimensional NumPy array or memmap.")
    if not (
        np.issubdtype(mean_electrons.dtype, np.floating)
        or np.issubdtype(mean_electrons.dtype, np.integer)
    ):
        raise ValueError("Mean electrons require a real numeric dtype.")
    size = np.asarray(sensor.fields.get("size"))
    if size.shape != (2,) or any(int(v) != v or int(v) < 1 for v in size):
        raise ValueError("Sensor size must be two positive integers.")
    full_shape = int(size[0]), int(size[1])
    if global_sensor_roi_bounds_row_col_exclusive is None:
        bounds = (0, full_shape[0], 0, full_shape[1])
    else:
        supplied = global_sensor_roi_bounds_row_col_exclusive
        if (
            not isinstance(supplied, (tuple, list))
            or len(supplied) != 4
            or any(type(item) is not int for item in supplied)
            or not (0 <= supplied[0] < supplied[1] <= full_shape[0])
            or not (0 <= supplied[2] < supplied[3] <= full_shape[1])
        ):
            raise ValueError(
                "Global mean ROI requires four half-open bounds inside the full sensor."
            )
        bounds = tuple(supplied)
    shape = bounds[1] - bounds[0], bounds[3] - bounds[2]
    if mean_electrons.shape != shape:
        raise ValueError("Mean electrons must match the declared global sensor ROI shape.")
    return shape, full_shape, bounds


def _config(sensor: Sensor) -> dict[str, Any]:
    exposure = _require_single_linear_capture(sensor)
    stored_readout = sensor.fields.get("sensor_readout")
    if not isinstance(stored_readout, dict):
        raise ValueError("A configured digital RAW readout is required.")
    readout = _readout_values(stored_readout)
    flag_value = sensor.fields.get("noise_flag", 2)
    if isinstance(flag_value, (bool, np.bool_)) or int(flag_value) != flag_value:
        raise ValueError("Digital RAW noise flag must be -2, -1, 0, 1, or 2.")
    flag = int(flag_value)
    if flag not in {-2, -1, 0, 1, 2}:
        raise ValueError("Digital RAW noise flag must be -2, -1, 0, 1, or 2.")
    pixel = sensor.fields.get("pixel")
    if not isinstance(pixel, dict):
        raise ValueError("Configured sensor pixel fields are required.")
    swing = float(pixel.get("voltage_swing", math.nan))
    if not math.isfinite(swing) or swing <= 0:
        raise ValueError("Digital RAW voltage swing must be finite and positive.")
    column_sigma = np.asarray(sensor.fields.get("column_fpn", [0.0, 0.0]), dtype=float)
    if column_sigma.shape != (2,) or not np.isfinite(column_sigma).all():
        raise ValueError("Digital RAW column FPN requires two finite offset/gain sigmas.")
    sigmas = {
        "pixel_offset_v": float(pixel.get("dsnu_sigma_v", 0)),
        "pixel_gain": float(pixel.get("prnu_sigma", 0)),
        "column_offset_v": float(column_sigma[0]),
        "column_gain": float(column_sigma[1]),
    }
    if any(not math.isfinite(value) or value < 0 for value in sigmas.values()):
        raise ValueError("Digital RAW fixed-pattern sigmas must be finite and nonnegative.")
    conversion_gain = swing / readout["full_well_e"]
    white = 2 ** readout["bit_depth"] - 1
    dn_per_e = (white - readout["black_level_dn"]) * readout["gain"] / readout["full_well_e"]
    dark_charge = readout["dark_current_e_per_s"] * exposure
    if (
        not math.isfinite(conversion_gain) or conversion_gain <= 0
        or not math.isfinite(dn_per_e) or dn_per_e <= 0
        or not math.isfinite(dark_charge)
    ):
        raise ValueError("Derived digital RAW readout quantities must be finite.")
    return {
        "exposure_time_s": exposure,
        "readout": readout,
        "noise_flag": flag,
        "conversion_gain_v_per_electron": conversion_gain,
        "fixed_pattern_seed": _integer_seed(
            sensor.fields.get("fixed_pattern_seed", 0), "fixed_pattern_seed"
        ),
        "fixed_pattern_sigmas": sigmas,
    }


def _calibrated_maps(sensor: Sensor, shape: tuple[int, int]) -> dict[str, np.ndarray | None]:
    expected_shapes = {
        "offset_fpn_image": shape,
        "gain_fpn_image": shape,
        "column_offset_fpn": (shape[1],),
        "column_gain_fpn": (shape[1],),
    }
    maps: dict[str, np.ndarray | None] = {}
    for name, expected_shape in expected_shapes.items():
        stored = sensor.fields.get(name)
        if stored is None:
            maps[name] = None
            continue
        if not isinstance(stored, np.ndarray) or stored.shape != expected_shape:
            raise ValueError(f"Digital RAW {name} must be an array matching the sensor shape.")
        if not (
            np.issubdtype(stored.dtype, np.floating)
            or np.issubdtype(stored.dtype, np.integer)
        ):
            raise ValueError(f"Digital RAW {name} requires a real numeric dtype.")
        maps[name] = stored
    return maps


def _block(base: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {**base, "status": status, "reason": reason}


def preflight_out_of_core_readout(
    sensor: Sensor,
    mean_electrons: np.ndarray,
    output_dir: str | Path,
    *,
    seed: int,
    row_chunk_rows: int,
    policy: OutOfCoreReadoutPolicy,
    global_sensor_roi_bounds_row_col_exclusive: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """Validate contract and admission without writing output or scanning pixels."""
    if not isinstance(policy, OutOfCoreReadoutPolicy):
        raise ValueError("An explicit out-of-core readout resource policy is required.")
    selected_seed = _integer_seed(seed, "seed")
    if type(row_chunk_rows) is not int or row_chunk_rows < 1:
        raise ValueError("row_chunk_rows must be a positive integer.")
    shape, full_shape, bounds = _shape(
        sensor, mean_electrons, global_sensor_roi_bounds_row_col_exclusive
    )
    config = _config(sensor)
    _calibrated_maps(sensor, full_shape)
    rows, cols = shape
    cells = rows * cols
    chunk_cells = min(rows, row_chunk_rows) * cols
    estimated_transient = (
        _POLICY_TRANSIENT_BYTES_PER_CHUNK_CELL * chunk_cells
        + _POLICY_COLUMN_BYTES_PER_CELL * cols
    )
    output_bytes = _OUTPUT_BYTES_PER_CELL * cells + _OUTPUT_HEADER_ALLOWANCE_BYTES
    resident_bytes = _resident_payload_bytes(
        {
            "sensor_fields": sensor.fields,
            "sensor_metadata": sensor.metadata,
            "sensor_data": sensor.data,
            "mean": mean_electrons,
        }
    )
    destination = Path(output_dir).expanduser().resolve()
    base = {
        "schema": SCHEMA,
        "status": "preflight_incomplete",
        "sensor_shape_row_col": [rows, cols],
        "full_sensor_shape_row_col": list(full_shape),
        "global_sensor_roi_bounds_row_col_exclusive": list(bounds),
        "sensor_cells": cells,
        "row_chunk_rows": row_chunk_rows,
        "maximum_chunk_cells": chunk_cells,
        "estimated_transient_bytes": estimated_transient,
        "estimated_output_bytes": output_bytes,
        "resident_input_ndarray_payload_bytes": resident_bytes,
        "policy": asdict(policy),
        "noise_seed": selected_seed,
        "fixed_pattern_seed": config["fixed_pattern_seed"],
        "noise_flag": config["noise_flag"],
        "random_contract": RANDOM_CONTRACT,
        "native_seed_bit_parity": False,
        "same_physical_readout_order_as_native": True,
        "mean_input_is_memmap": isinstance(mean_electrons, np.memmap),
        "output_dtypes": {"raw_dn": "int32", "reference_dn": "float64", "volts": "float64"},
        "estimated_transient_bytes_is_peak_rss_bound": False,
        "hard_process_rss_limit_enforced": False,
        "resource_policy_is_hardware_limit": False,
        "free_disk_snapshot_is_reservation": False,
        "physical_fov_qualified": False,
        "product_full_frame_qualified": False,
        "output_directory": str(destination),
    }
    if row_chunk_rows >= rows:
        return _block(
            base, "blocked_full_height_row_chunk_policy",
            "At least two row chunks are required to keep the readout arrays out of RAM.",
        )
    if cells > policy.max_sensor_cells:
        return _block(base, "blocked_sensor_cells_policy", "Sensor exceeds declared cells.")
    if chunk_cells > policy.max_row_chunk_cells:
        return _block(base, "blocked_chunk_cells_policy", "Row chunk exceeds declared cells.")
    if estimated_transient > policy.max_transient_bytes:
        return _block(
            base, "blocked_transient_memory_policy",
            "Estimated transient arrays exceed declared budget.",
        )
    if output_bytes > policy.max_output_bytes:
        return _block(
            base, "blocked_output_bytes_policy", "Output files exceed declared disk budget."
        )
    if resident_bytes > policy.max_resident_input_bytes:
        return _block(
            base, "blocked_resident_input_policy", "Resident input arrays exceed declared budget."
        )
    if destination.exists():
        return _block(base, "blocked_existing_output", "Output directory already exists.")
    if not destination.parent.is_dir():
        return _block(base, "blocked_output_parent", "Output parent directory does not exist.")
    free_bytes = shutil.disk_usage(destination.parent).free
    base["observed_free_disk_bytes_before_output"] = free_bytes
    if free_bytes - output_bytes < policy.min_free_after_output_bytes:
        return _block(
            base, "blocked_free_disk_policy",
            "Free disk after output would violate the declared reserve.",
        )
    # Shapes and dtypes are checked above. Values are scanned chunkwise during
    # execution; a bad value aborts and discards only this call's temporary dir.
    return {**base, "status": "ready_under_declared_policy"}


def _map_chunk(
    stored: np.ndarray | None,
    bounds: slice | tuple[slice, slice] | None,
    *,
    name: str,
) -> np.ndarray:
    if stored is None:
        raise ValueError(f"Digital RAW {name} has no supplied calibration map.")
    value = np.asarray(stored if bounds is None else stored[bounds], dtype=np.float64)
    if not np.isfinite(value).all():
        raise ValueError(f"Digital RAW {name} must be finite.")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def compute_out_of_core_readout(
    sensor: Sensor,
    mean_electrons: np.ndarray,
    output_dir: str | Path,
    *,
    seed: int,
    row_chunk_rows: int,
    policy: OutOfCoreReadoutPolicy,
    global_sensor_roi_bounds_row_col_exclusive: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """Write full-frame or global-coordinate ROI RAW arrays as bounded chunks.

    Returned paths point at .npy files with recorded SHA-256 hashes.  The
    caller may reopen them with ``np.load(path, mmap_mode='r')`` and should
    verify their hashes before relying on later reads.  File-system
    immutability is not enforced here.  This is not a native-seed realization
    and has no Worker/API entry point by default.
    """
    plan = preflight_out_of_core_readout(
        sensor, mean_electrons, output_dir,
        seed=seed, row_chunk_rows=row_chunk_rows, policy=policy,
        global_sensor_roi_bounds_row_col_exclusive=global_sensor_roi_bounds_row_col_exclusive,
    )
    if plan["status"] != "ready_under_declared_policy":
        raise ValueError(f"resource_blocked: {plan['status']}: {plan['reason']}")
    shape = tuple(plan["sensor_shape_row_col"])
    rows, cols = shape
    full_shape = tuple(plan["full_sensor_shape_row_col"])
    global_r0, _, global_c0, global_c1 = plan["global_sensor_roi_bounds_row_col_exclusive"]
    config = _config(sensor)
    maps = _calibrated_maps(sensor, full_shape)
    readout = config["readout"]
    flag = config["noise_flag"]
    fixed_seed = config["fixed_pattern_seed"]
    sigmas = config["fixed_pattern_sigmas"]
    conversion_gain = config["conversion_gain_v_per_electron"]
    full_well = readout["full_well_e"]
    white = 2 ** readout["bit_depth"] - 1
    black = readout["black_level_dn"]
    dn_per_e = (white - black) * readout["gain"] / full_well
    dark_charge = readout["dark_current_e_per_s"] * config["exposure_time_s"]
    column_index = np.arange(global_c0, global_c1, dtype=np.uint64)
    column_row = np.array([0], dtype=np.uint64)
    if maps["column_offset_fpn"] is not None:
        column_offset = _map_chunk(
            maps["column_offset_fpn"], slice(global_c0, global_c1), name="column_offset_fpn"
        )
    elif sigmas["column_offset_v"] > 0:
        column_offset = sigmas["column_offset_v"] * _normal(
            fixed_seed, column_row, column_index, _STREAM_COLUMN_OFFSET
        )[0]
    else:
        column_offset = 0.0
    if maps["column_gain_fpn"] is not None:
        column_gain = _map_chunk(
            maps["column_gain_fpn"], slice(global_c0, global_c1), name="column_gain_fpn"
        )
    elif sigmas["column_gain"] > 0:
        column_gain = 1.0 + sigmas["column_gain"] * _normal(
            fixed_seed, column_row, column_index, _STREAM_COLUMN_GAIN
        )[0]
    else:
        column_gain = 1.0
    destination = Path(plan["output_directory"])
    temporary = Path(tempfile.mkdtemp(prefix=".readout_tmp_", dir=destination.parent))
    committed = False
    try:
        raw_out = np.lib.format.open_memmap(
            temporary / "raw_dn.npy", mode="w+", dtype=np.int32, shape=shape
        )
        reference_out = np.lib.format.open_memmap(
            temporary / "reference_dn.npy", mode="w+", dtype=np.float64, shape=shape
        )
        volts_out = np.lib.format.open_memmap(
            temporary / "volts.npy", mode="w+", dtype=np.float64, shape=shape
        )
        well_count = adc_white_count = adc_zero_count = 0
        mean_digest = hashlib.sha256()
        for first in range(0, rows, row_chunk_rows):
            last = min(first + row_chunk_rows, rows)
            selection = slice(first, last)
            source = mean_electrons[selection, :]
            mean_digest.update(np.ascontiguousarray(source).tobytes())
            mean = np.asarray(source, dtype=np.float64)
            if not np.isfinite(mean).all() or np.any(mean < 0):
                raise ValueError("Digital RAW mean_electrons must be finite and nonnegative.")
            row_index = np.arange(global_r0 + first, global_r0 + last, dtype=np.uint64)
            total_mean = mean + dark_charge
            if not np.isfinite(total_mean).all():
                raise ValueError("Dark-adjusted mean electrons must remain finite.")
            electrons = (
                stats.poisson.ppf(_uniform(seed, row_index, column_index, _STREAM_SHOT), total_mean)
                if flag in {-2, 1, 2} else total_mean.copy()
            )
            if not np.isfinite(electrons).all():
                raise ValueError("Shot-noise quantiles must remain finite.")
            expected = total_mean.copy()
            bias_e: np.ndarray | float = 0.0
            if flag in {1, 2}:
                if maps["offset_fpn_image"] is not None:
                    offset = _map_chunk(
                        maps["offset_fpn_image"],
                        (slice(global_r0 + first, global_r0 + last), slice(global_c0, global_c1)),
                        name="offset_fpn_image",
                    )
                elif sigmas["pixel_offset_v"] > 0:
                    offset = sigmas["pixel_offset_v"] * _normal(
                        fixed_seed, row_index, column_index, _STREAM_PIXEL_OFFSET
                    )
                else:
                    offset = 0.0
                if maps["gain_fpn_image"] is not None:
                    gain = _map_chunk(
                        maps["gain_fpn_image"],
                        (slice(global_r0 + first, global_r0 + last), slice(global_c0, global_c1)),
                        name="gain_fpn_image",
                    )
                elif sigmas["pixel_gain"] > 0:
                    gain = 1.0 + sigmas["pixel_gain"] * _normal(
                        fixed_seed, row_index, column_index, _STREAM_PIXEL_GAIN
                    )
                else:
                    gain = 1.0
                response_gain = gain * column_gain
                bias_e = (offset * column_gain + column_offset) / conversion_gain
                if not np.isfinite(response_gain).all() or not np.isfinite(bias_e).all():
                    raise ValueError("Fixed-pattern response and bias must remain finite.")
                electrons *= response_gain
                expected *= response_gain
            if not np.isfinite(electrons).all() or not np.isfinite(expected).all():
                raise ValueError("Pre-well charge must remain finite.")
            well_count += int(np.count_nonzero(electrons >= full_well))
            np.clip(electrons, 0.0, full_well, out=electrons)
            np.clip(expected, 0.0, full_well, out=expected)
            electrons += bias_e
            expected += bias_e
            if flag == 2 and readout["read_noise_e"] > 0:
                electrons += readout["read_noise_e"] * _normal(
                    seed, row_index, column_index, _STREAM_READ
                )
            if not np.isfinite(electrons).all() or not np.isfinite(expected).all():
                raise ValueError("Readout charge must remain finite.")
            raw = np.clip(np.rint(black + dn_per_e * electrons), 0, white).astype(np.int32)
            adc_white_count += int(np.count_nonzero(raw == white))
            adc_zero_count += int(np.count_nonzero(raw == 0))
            raw_out[selection, :] = raw
            reference_out[selection, :] = np.clip(black + dn_per_e * expected, 0.0, white)
            volts_out[selection, :] = electrons * readout["gain"] * conversion_gain
        for mapped in (raw_out, reference_out, volts_out):
            mapped.flush()
        del raw_out, reference_out, volts_out
        files = {
            name: {
                "file": f"{name}.npy",
                "sha256": _sha256_file(temporary / f"{name}.npy"),
                "bytes": (temporary / f"{name}.npy").stat().st_size,
            }
            for name in ("raw_dn", "reference_dn", "volts")
        }
        metadata = {
            "schema": SCHEMA,
            "status": "diagnostic_complete_sensor_raw_executed",
            "sensor_capture": {
                "schema": "camerae2e_sensor_capture_streamed_v1",
                "mode": "digital_raw",
                "readout": dict(readout),
                "white_level_dn": white,
                "black_level_dn": black,
                "dn_per_electron": dn_per_e,
                "exposure_time_s": config["exposure_time_s"],
                "noise_seed": seed,
                "fixed_pattern_seed": fixed_seed,
                "noise_flag": flag,
                "fixed_pattern_order": (
                    "response_gain_then_well_clip_then_signed_bias_then_read_noise"
                ),
                "dsnu_definition": "fixed_readout_bias_per_capture_not_dark_current_variation",
                "random_contract": RANDOM_CONTRACT,
                "native_seed_bit_parity": False,
                "generated_fixed_pattern_maps_materialized": False,
                "units": {
                    "mean_electrons": "photoelectrons/pixel before dark, FPN, noise and gain",
                    "reference_dn": "continuous DN before rounding, clipped to ADC range",
                    "raw_dn": "integer DN including digital black pedestal",
                    "volts": (
                        "signed post-gain ADC-input signal V relative to digital black pedestal"
                    ),
                },
                "saturation": {
                    "well_fraction": well_count / (rows * cols),
                    "adc_white_fraction": adc_white_count / (rows * cols),
                    "adc_zero_fraction": adc_zero_count / (rows * cols),
                },
                "provenance": "model-derived capture; readout values are configured assumptions",
            },
            "shape_row_col": list(shape),
            "full_sensor_shape_row_col": list(full_shape),
            "global_sensor_roi_bounds_row_col_exclusive": plan[
                "global_sensor_roi_bounds_row_col_exclusive"
            ],
            "mean_input_dtype": str(mean_electrons.dtype),
            "mean_input_content_sha256": mean_digest.hexdigest(),
            "output_files": files,
            "dv_alias": "raw_dn",
            "preflight": plan,
            "complete_sensor_mean_materialized_by_readout": False,
            "full_sensor_output_arrays_in_ram": False,
            "estimated_transient_bytes_is_peak_rss_bound": False,
            "hard_process_rss_limit_enforced": False,
            "physical_fov_qualified": False,
            "product_full_frame_qualified": False,
        }
        (temporary / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if destination.exists():
            raise ValueError("Output directory appeared during capture; refusing to overwrite it.")
        os.rename(temporary, destination)
        committed = True
        return {
            **metadata,
            "output_directory": str(destination),
            "raw_dn_path": str(destination / "raw_dn.npy"),
            "reference_dn_path": str(destination / "reference_dn.npy"),
            "volts_path": str(destination / "volts.npy"),
            "metadata_path": str(destination / "metadata.json"),
        }
    finally:
        if not committed:
            shutil.rmtree(temporary)
