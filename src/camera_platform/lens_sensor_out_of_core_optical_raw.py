"""Opt-in strict optical tiles -> disk-backed mean -> complete digital RAW.

The analytic Scene and full optical image are never assembled.  Each complete,
nonoverlapping sensor tile is evaluated in global coordinates and written to a
full-shape mean-electron .npy memmap.  A separate coordinate-keyed readout then
streams this mean into complete RAW/reference/voltage .npy files.  The native
CameraE2E stochastic seed realization is intentionally not claimed.  This is
a diagnostic library path, not an API/Worker entry point or physical FOV test.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

from pyisetcam.optics import (
    _strict_raytrace_apply_psf_bounded_roi_provider,
    _strict_raytrace_cache_signature,
)
from pyisetcam.types import Sensor

from .lens_sensor_centered_oi import _sensor_contract
from .lens_sensor_offaxis_sensor_tile import compute_fixed_sensor_tile_mean_electrons
from .lens_sensor_optical_source_halo import preflight_strict_optical_source_halo
from .lens_sensor_out_of_core_readout import (
    _OUTPUT_BYTES_PER_CELL,
    _OUTPUT_HEADER_ALLOWANCE_BYTES,
    _POLICY_COLUMN_BYTES_PER_CELL,
    _POLICY_TRANSIENT_BYTES_PER_CHUNK_CELL,
    OutOfCoreReadoutPolicy,
    _calibrated_maps,
    _config,
    _integer_seed,
    _resident_payload_bytes,
    compute_out_of_core_readout,
    preflight_out_of_core_readout,
)
from .lens_sensor_scene_geometry_tile import (
    AnalyticStrictGeometryTileProvider,
    _optical_state_fingerprint,
)
from .lens_sensor_streamed_analytic_capture import (
    AnalyticCaptureResourcePolicy,
    _optics_array_payload_bytes,
    _tile_indices,
)
from .lens_sensor_streamed_full_raw import _complete_tiles, _sensor_identity
from .lens_sensor_streamed_offaxis_mean import (
    _compute_preflighted_streamed_analytic_offaxis_mean,
    preflight_streamed_analytic_offaxis_mean,
)

SCHEMA = "camera_lens_sensor_out_of_core_optical_raw_v1"
_MEAN_HEADER_ALLOWANCE_BYTES = 4096
_TILE_EVIDENCE_POLICY_BYTES = 64 * 1024
_MANIFEST_POLICY_BYTES = 64 * 1024
_MAX_REUSE_GROUP_TILES = 4
_REUSE_GROUP_SCHEMA = "camera_lens_sensor_optical_reuse_group_v1"


@dataclass(frozen=True)
class OpticalDiskRawResourcePolicy:
    """Declared numerical and disk admission caps, not hardware bounds."""

    optical_tile: AnalyticCaptureResourcePolicy
    readout: OutOfCoreReadoutPolicy
    max_sensor_tiles: int
    max_mean_disk_bytes: int
    max_total_output_bytes: int
    max_combined_working_bytes: int
    min_free_after_output_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.optical_tile, AnalyticCaptureResourcePolicy):
            raise ValueError("An optical tile resource policy is required.")
        if not isinstance(self.readout, OutOfCoreReadoutPolicy):
            raise ValueError("An out-of-core readout resource policy is required.")
        for name in (
            "max_sensor_tiles", "max_mean_disk_bytes", "max_total_output_bytes",
            "max_combined_working_bytes", "min_free_after_output_bytes",
        ):
            value = getattr(self, name)
            minimum = 0 if name == "min_free_after_output_bytes" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer policy cap.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "optical_tile": self.optical_tile.as_dict(),
            "readout": asdict(self.readout),
            **{
                name: getattr(self, name)
                for name in (
                    "max_sensor_tiles", "max_mean_disk_bytes", "max_total_output_bytes",
                    "max_combined_working_bytes", "min_free_after_output_bytes",
                )
            },
        }


def _blocked(base: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {**base, "status": status, "reason": reason}


def _json_default(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Unsupported evidence value: {type(value).__name__}")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=_json_default,
        allow_nan=False,
    ).encode()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tiles_for_roi(
    roi: tuple[int, int, int, int],
    padding: tuple[int, int],
    tile_shape: tuple[int, int],
    global_shape: tuple[int, int],
) -> set[tuple[int, int]]:
    rows = _tile_indices(roi[0], roi[1], padding[0], tile_shape[0], global_shape[0])
    cols = _tile_indices(roi[2], roi[3], padding[1], tile_shape[1], global_shape[1])
    return {(row, col) for row in rows for col in cols}


def _reuse_group_key(
    provider: AnalyticStrictGeometryTileProvider,
    member_plans: list[dict[str, Any]],
    union_roi: tuple[int, int, int, int],
    source_tile_shape: tuple[int, int],
    angles: np.ndarray,
) -> str:
    first = member_plans[0]
    identity = {
        "schema": _REUSE_GROUP_SCHEMA,
        "source_identity": provider.source_identity,
        "full_optical_state_sha256": provider.optics_state_sha256,
        "strict_psf_source_signature": _strict_raytrace_cache_signature(provider.optics),
        "global_scene_shape_row_col": list(provider.global_shape),
        "oi_pitch_um": provider.oi_pitch_um,
        "wavelength_nm": provider.wavelengths_nm.tolist(),
        "sample_angles_deg": np.asarray(angles, dtype=float).tolist(),
        "strict_psf_padding_row_col": first["strict_psf_padding_row_col"],
        "source_tile_shape_row_col": list(source_tile_shape),
        "union_optical_roi_row_col_exclusive": list(union_roi),
        "member_sensor_tiles_row_col_exclusive": [
            item["sensor_tile_bounds_row_col_exclusive"] for item in member_plans
        ],
    }
    return hashlib.sha256(_json_bytes(identity)).hexdigest()


def _try_reuse_group(
    provider: AnalyticStrictGeometryTileProvider,
    member_plans: list[dict[str, Any]],
    *,
    source_tile_shape: tuple[int, int],
    angles: np.ndarray,
    resource_policy: OpticalDiskRawResourcePolicy,
    sensor_payload_bytes: int,
) -> dict[str, Any] | None:
    """Admit a group only when its global optical ROI has a proven bounded plan.

    Every source tile in the union must already belong to a member's admitted
    source-tile set. That makes the maximum per-source working estimate from
    the member plans a valid conservative group estimate. A group never changes
    the global source tiling, so its overlapping output pixels retain the same
    PSF field coordinates, FFT padding and source-tile accumulation order.
    """
    if not 2 <= len(member_plans) <= _MAX_REUSE_GROUP_TILES:
        return None
    rois = [tuple(item["optical_roi_row_col_exclusive"]) for item in member_plans]
    union = (
        min(item[0] for item in rois), max(item[1] for item in rois),
        min(item[2] for item in rois), max(item[3] for item in rois),
    )
    union_cells = (union[1] - union[0]) * (union[3] - union[2])
    optical_policy = resource_policy.optical_tile
    if union_cells > optical_policy.max_output_roi_cells:
        return None
    padding = tuple(member_plans[0]["strict_psf_padding_row_col"])
    if any(tuple(item["strict_psf_padding_row_col"]) != padding for item in member_plans):
        raise ValueError("Admitted optical tiles disagree on sampled PSF padding.")
    group_tiles = _source_tiles_for_roi(union, padding, source_tile_shape, provider.global_shape)
    covered_tiles: set[tuple[int, int]] = set()
    for roi in rois:
        covered_tiles.update(
            _source_tiles_for_roi(roi, padding, source_tile_shape, provider.global_shape)
        )
    if not group_tiles.issubset(covered_tiles):
        return None
    if (
        len(group_tiles) > optical_policy.max_source_tiles
        or len(group_tiles)
        >= sum(int(item["candidate_source_tile_count"]) for item in member_plans)
    ):
        return None
    bands = provider.wavelengths_nm.size
    source_queries = len(group_tiles) * source_tile_shape[0] * source_tile_shape[1] * bands
    if source_queries >= optical_policy.max_geometry_query_cells:
        return None
    halo = preflight_strict_optical_source_halo(
        provider.optics,
        global_scene_shape_row_col=provider.global_shape,
        optical_roi_row_col_exclusive=union,
        oi_pitch_um=provider.oi_pitch_um,
        wavelengths_nm=provider.wavelengths_nm,
        max_geometry_query_cells=optical_policy.max_geometry_query_cells,
        max_geometry_query_chunk_cells=optical_policy.max_geometry_query_chunk_cells,
    )
    if (
        halo["status"] != "inside_declared_scene"
        or source_queries + int(halo["geometry_query_cells"])
        > optical_policy.max_geometry_query_cells
    ):
        return None
    working = (
        int(member_plans[0]["static_optics_ndarray_payload_bytes"])
        + max(int(item["maximum_per_source_tile_policy_estimate_bytes"]) for item in member_plans)
        + union_cells * bands * 8
        + max(int(item["sensor_scratch_policy_bytes"]) for item in member_plans)
    )
    if (
        working > optical_policy.estimated_working_budget_bytes
        or working + sensor_payload_bytes > resource_policy.max_combined_working_bytes
    ):
        return None
    return {
        "schema": _REUSE_GROUP_SCHEMA,
        "key_sha256": _reuse_group_key(
            provider, member_plans, union, source_tile_shape, angles
        ),
        "member_sensor_tiles_row_col_exclusive": [
            item["sensor_tile_bounds_row_col_exclusive"] for item in member_plans
        ],
        "union_optical_roi_row_col_exclusive": list(union),
        "source_tiles_before_reuse_estimate": sum(
            int(item["candidate_source_tile_count"]) for item in member_plans
        ),
        "source_tiles_after_reuse_estimate": len(group_tiles),
        "combined_working_policy_estimate_bytes": working,
        "stored_psf_numeric_halo_sha256": hashlib.sha256(_json_bytes(halo)).hexdigest(),
        "cache_lifetime": "one_optical_group_until_all_member_sensor_tiles_complete",
    }


def preflight_optical_to_disk_raw(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    output_dir: str | Path,
    *,
    sensor_tiles_row_col_exclusive: tuple[tuple[int, int, int, int], ...]
    | list[tuple[int, int, int, int]],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    seed: int,
    row_chunk_rows: int,
    resource_policy: OpticalDiskRawResourcePolicy,
    reuse_optical_groups: bool = False,
    _admitted_plan_sink: Callable[[bytes], None] | None = None,
) -> dict[str, Any]:
    """Inspect all sensor tiles and declared budgets before optical execution.

    Mean/RAW file sizes and transient arrays are estimates.  Free disk is a
    snapshot, not a reservation.  This function does not allocate output.
    """
    if not isinstance(provider, AnalyticStrictGeometryTileProvider):
        raise ValueError("An analytic strict geometry tile provider is required.")
    if not isinstance(sensor, Sensor):
        raise ValueError("A CameraE2E Sensor is required.")
    if not isinstance(resource_policy, OpticalDiskRawResourcePolicy):
        raise ValueError("An explicit optical-to-disk resource policy is required.")
    if type(reuse_optical_groups) is not bool:
        raise ValueError("reuse_optical_groups must be a boolean opt-in.")
    if sensor.data or "sensor_capture" in sensor.metadata:
        raise ValueError("A fresh, uncomputed sensor is required.")
    if _optical_state_fingerprint(provider.optics) != provider.optics_state_sha256:
        raise ValueError("The provider optics changed after construction.")
    shape, _, _ = _sensor_contract(sensor)
    _config(sensor)
    _integer_seed(seed, "seed")
    _calibrated_maps(sensor, shape)
    if not isinstance(sensor_tiles_row_col_exclusive, (tuple, list)):
        raise ValueError("A sensor tile list is required.")
    destination = Path(output_dir).expanduser().resolve()
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "preflight_incomplete",
        "source_identity": provider.source_identity,
        "full_optical_state_sha256": provider.optics_state_sha256,
        "sensor_model_state_sha256": _sensor_identity(sensor),
        "full_sensor_shape_row_col": list(shape),
        "full_sensor_cells": shape[0] * shape[1],
        "sensor_tile_count": len(sensor_tiles_row_col_exclusive),
        "tile_preflight_order": "all_sensor_tiles_before_any_optical_execution",
        "resource_policy": resource_policy.as_dict(),
        "output_directory": str(destination),
        "resource_policy_is_hardware_limit": False,
        "estimated_working_bytes_is_peak_rss_bound": False,
        "hard_process_rss_limit_enforced": False,
        "free_disk_snapshot_is_reservation": False,
        "full_scene_or_oi_materialized": False,
        "full_sensor_mean_or_raw_materialized_in_ram": False,
        "native_seed_bit_parity": False,
        "physical_scene_halo_qualified": False,
        "physical_fov_qualified": False,
        "product_full_frame_qualified": False,
    }
    if len(sensor_tiles_row_col_exclusive) > resource_policy.max_sensor_tiles:
        return _blocked(base, "blocked_sensor_tile_count_policy", "Too many sensor tiles.")
    tiles = _complete_tiles(sensor_tiles_row_col_exclusive, shape)
    base["sensor_tiles_row_col_exclusive"] = [list(tile) for tile in tiles]
    base["sensor_tile_coverage"] = "complete_exact_nonoverlapping"
    if type(row_chunk_rows) is not int or row_chunk_rows < 1:
        raise ValueError("row_chunk_rows must be a positive integer.")
    if row_chunk_rows >= shape[0]:
        return _blocked(
            base, "blocked_full_readout_row_chunk_policy",
            "Readout must use at least two row chunks to avoid a full-frame RAM array.",
        )
    # Readout admission is repeated against the actual new mean memmap before
    # optics execute.  This shape estimate exists so a readout budget cannot
    # force expensive optical work that could have been rejected immediately.
    cells = shape[0] * shape[1]
    max_sensor_tile_cells = max((r1 - r0) * (c1 - c0) for r0, r1, c0, c1 in tiles)
    base["maximum_sensor_mean_tile_cells"] = max_sensor_tile_cells
    if max_sensor_tile_cells >= cells:
        return _blocked(
            base, "blocked_full_sensor_mean_tile_policy",
            "At least two smaller sensor tiles are required for out-of-core mean capture.",
        )
    chunk_cells = min(row_chunk_rows, shape[0]) * shape[1]
    mean_bytes = 8 * cells + _MEAN_HEADER_ALLOWANCE_BYTES
    readout_bytes = _OUTPUT_BYTES_PER_CELL * cells + _OUTPUT_HEADER_ALLOWANCE_BYTES
    estimated_output_bytes = (
        mean_bytes + readout_bytes + 2 * _TILE_EVIDENCE_POLICY_BYTES * len(tiles)
        + _MANIFEST_POLICY_BYTES
    )
    readout_transient = (
        _POLICY_TRANSIENT_BYTES_PER_CHUNK_CELL * chunk_cells
        + _POLICY_COLUMN_BYTES_PER_CELL * shape[1]
    )
    sensor_payload = _resident_payload_bytes(
        {"fields": sensor.fields, "metadata": sensor.metadata, "data": sensor.data}
    )
    optics_payload = _optics_array_payload_bytes(provider)
    base.update({
        "mean_memmap_policy_estimate_bytes": mean_bytes,
        "readout_output_policy_estimate_bytes": readout_bytes,
        "tile_evidence_policy_estimate_bytes": _TILE_EVIDENCE_POLICY_BYTES * len(tiles),
        "temporary_admitted_plan_spool_policy_estimate_bytes": (
            _TILE_EVIDENCE_POLICY_BYTES * len(tiles)
        ),
        "combined_output_policy_estimate_bytes": estimated_output_bytes,
        "readout_transient_policy_estimate_bytes": readout_transient,
        "resident_sensor_ndarray_payload_bytes": sensor_payload,
        "resident_optics_ndarray_payload_bytes": optics_payload,
    })
    readout_policy = resource_policy.readout
    if cells > readout_policy.max_sensor_cells:
        return _blocked(
            base, "blocked_readout_sensor_cells_policy", "Full sensor exceeds readout cells."
        )
    if chunk_cells > readout_policy.max_row_chunk_cells:
        return _blocked(base, "blocked_readout_chunk_cells_policy", "Readout chunk exceeds cells.")
    if readout_transient > readout_policy.max_transient_bytes:
        return _blocked(
            base, "blocked_readout_transient_memory_policy",
            "Readout transient estimate exceeds budget.",
        )
    if readout_bytes > readout_policy.max_output_bytes:
        return _blocked(base, "blocked_readout_output_bytes_policy", "Readout files exceed budget.")
    if sensor_payload > readout_policy.max_resident_input_bytes:
        return _blocked(
            base, "blocked_readout_resident_input_policy", "Sensor arrays exceed budget."
        )
    if mean_bytes > resource_policy.max_mean_disk_bytes:
        return _blocked(
            base, "blocked_mean_disk_policy", "Mean memmap exceeds declared disk budget."
        )
    if estimated_output_bytes > resource_policy.max_total_output_bytes:
        return _blocked(base, "blocked_total_output_policy", "Outputs exceed declared disk budget.")
    if destination.exists():
        return _blocked(base, "blocked_existing_output", "Output directory already exists.")
    if not destination.parent.is_dir():
        return _blocked(base, "blocked_output_parent", "Output parent directory does not exist.")
    free = shutil.disk_usage(destination.parent).free
    base["observed_free_disk_bytes_before_output"] = free
    if free - estimated_output_bytes < resource_policy.min_free_after_output_bytes:
        return _blocked(
            base, "blocked_free_disk_policy", "Declared disk reserve would be violated."
        )
    plan_digest = hashlib.sha256()
    worst_optical = 0
    worst_tile: list[int] | None = None
    reuse_groups: list[dict[str, Any]] = []
    pending_group: list[dict[str, Any]] = []
    pending_reuse: dict[str, Any] | None = None
    for tile in tiles:
        tile_plan = preflight_streamed_analytic_offaxis_mean(
            provider, sensor,
            sensor_tile_bounds_row_col_exclusive=tile,
            source_tile_shape_row_col=source_tile_shape_row_col,
            sample_angles_deg=sample_angles_deg,
            resource_policy=resource_policy.optical_tile,
        )
        encoded_plan = _json_bytes(tile_plan)
        plan_digest.update(encoded_plan)
        if tile_plan["status"] != "ready_under_declared_policy":
            return _blocked(
                {**base, "first_blocked_sensor_tile": list(tile), "tile_preflight": tile_plan},
                "blocked_sensor_tile_preflight",
                f"Sensor tile {list(tile)}: {tile_plan['status']}: {tile_plan['reason']}",
            )
        if (
            tile_plan["source_identity"] != provider.source_identity
            or tile_plan["full_optical_state_sha256"] != provider.optics_state_sha256
        ):
            raise ValueError("Sensor tile plan changed source or optical identity.")
        if _admitted_plan_sink is not None:
            if len(encoded_plan) + 1 > _TILE_EVIDENCE_POLICY_BYTES:
                return _blocked(
                    base, "blocked_plan_spool_policy",
                    "An admitted tile plan exceeds the declared spool budget.",
                )
            _admitted_plan_sink(encoded_plan)
        estimate = int(tile_plan["combined_policy_estimate_bytes"])
        if estimate > worst_optical:
            worst_optical = estimate
            worst_tile = list(tile)
        if reuse_optical_groups:
            if pending_group:
                candidate = _try_reuse_group(
                    provider, [*pending_group, tile_plan],
                    source_tile_shape=tuple(source_tile_shape_row_col),
                    angles=np.asarray(sample_angles_deg, dtype=float),
                    resource_policy=resource_policy,
                    sensor_payload_bytes=sensor_payload,
                )
                if candidate is None:
                    if pending_reuse is not None:
                        reuse_groups.append(pending_reuse)
                    pending_group = [tile_plan]
                    pending_reuse = None
                else:
                    pending_group.append(tile_plan)
                    pending_reuse = candidate
            else:
                pending_group = [tile_plan]
    if reuse_optical_groups and pending_reuse is not None:
        reuse_groups.append(pending_reuse)
    combined_working = max(
        worst_optical + sensor_payload,
        readout_transient + sensor_payload + optics_payload,
        *(int(group["combined_working_policy_estimate_bytes"]) + sensor_payload
          for group in reuse_groups),
    )
    base.update({
        "sensor_tile_preflight_sha256": plan_digest.hexdigest(),
        "maximum_optical_tile_policy_estimate_bytes": worst_optical,
        "maximum_optical_tile_row_col_exclusive": worst_tile,
        "combined_working_policy_estimate_bytes": combined_working,
    })
    if reuse_optical_groups:
        base.update({
            "optical_reuse_mode": "bounded_adjacent_group_opt_in_v1",
            "optical_reuse_group_plans": reuse_groups,
            "optical_reuse_group_count": len(reuse_groups),
            "optical_reuse_group_plan_sha256": hashlib.sha256(
                _json_bytes(reuse_groups)
            ).hexdigest(),
        })
        if len(_json_bytes(base)) > _MANIFEST_POLICY_BYTES:
            return _blocked(
                base, "blocked_reuse_manifest_policy",
                "Optical reuse preflight exceeds the declared manifest reserve.",
            )
    if combined_working > resource_policy.max_combined_working_bytes:
        return _blocked(base, "blocked_combined_working_policy", "Working estimate exceeds budget.")
    if (
        provider.source_identity != base["source_identity"]
        or _sensor_identity(sensor) != base["sensor_model_state_sha256"]
    ):
        raise ValueError("Model identity changed during preflight.")
    return {**base, "status": "ready_under_declared_policy"}


@contextmanager
def _validated_admitted_plan_spool(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    output_dir: str | Path,
    *,
    sensor_tiles_row_col_exclusive: tuple[tuple[int, int, int, int], ...]
    | list[tuple[int, int, int, int]],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    seed: int,
    row_chunk_rows: int,
    resource_policy: OpticalDiskRawResourcePolicy,
    reuse_optical_groups: bool = False,
) -> Iterator[tuple[dict[str, Any], BinaryIO]]:
    """Spool every admitted tile plan, then verify the whole spool before optics.

    The anonymous temporary file keeps the plan list off the Python heap. Each
    line is bounded by the same per-tile policy reserve accounted in preflight.
    No optical tile is executed until all plans, order, count, and digest match.
    """
    parent = Path(output_dir).expanduser().resolve().parent
    with tempfile.TemporaryFile(mode="w+b", dir=parent if parent.is_dir() else None) as spool:
        plan = preflight_optical_to_disk_raw(
            provider, sensor, output_dir,
            sensor_tiles_row_col_exclusive=sensor_tiles_row_col_exclusive,
            source_tile_shape_row_col=source_tile_shape_row_col,
            sample_angles_deg=sample_angles_deg,
            seed=seed,
            row_chunk_rows=row_chunk_rows,
            resource_policy=resource_policy,
            reuse_optical_groups=reuse_optical_groups,
            _admitted_plan_sink=lambda encoded: spool.write(encoded + b"\n"),
        )
        if plan["status"] != "ready_under_declared_policy":
            raise ValueError(f"resource_blocked: {plan['status']}: {plan['reason']}")
        spool.flush()
        spool.seek(0)
        digest = hashlib.sha256()
        for tile in plan["sensor_tiles_row_col_exclusive"]:
            line = spool.readline()
            if not line.endswith(b"\n"):
                raise ValueError("Admitted optical tile plan spool is incomplete.")
            encoded = line[:-1]
            tile_plan = json.loads(encoded)
            if tile_plan.get("sensor_tile_bounds_row_col_exclusive") != tile:
                raise ValueError("Admitted optical tile plan order changed.")
            digest.update(encoded)
        if spool.read(1) or digest.hexdigest() != plan["sensor_tile_preflight_sha256"]:
            raise ValueError("Admitted optical tile plan spool changed before execution.")
        spool.seek(0)
        yield plan, spool


def compute_optical_to_disk_raw(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    output_dir: str | Path,
    *,
    sensor_tiles_row_col_exclusive: tuple[tuple[int, int, int, int], ...]
    | list[tuple[int, int, int, int]],
    source_tile_shape_row_col: tuple[int, int],
    sample_angles_deg: np.ndarray,
    seed: int,
    row_chunk_rows: int,
    resource_policy: OpticalDiskRawResourcePolicy,
    reuse_optical_groups: bool = False,
) -> dict[str, Any]:
    """Execute complete strict optical/sensor tiles and disk-backed RAW."""
    angles = np.asarray(sample_angles_deg, dtype=float).copy()
    with _validated_admitted_plan_spool(
        provider, sensor, output_dir,
        sensor_tiles_row_col_exclusive=sensor_tiles_row_col_exclusive,
        source_tile_shape_row_col=source_tile_shape_row_col,
        sample_angles_deg=angles,
        seed=seed, row_chunk_rows=row_chunk_rows, resource_policy=resource_policy,
        reuse_optical_groups=reuse_optical_groups,
    ) as (plan, admitted_plans):
        return _compute_optical_to_disk_raw_admitted(
            provider, sensor, angles=angles,
            source_tile_shape_row_col=source_tile_shape_row_col,
            seed=seed, row_chunk_rows=row_chunk_rows,
            resource_policy=resource_policy,
            plan=plan, admitted_plans=admitted_plans,
        )


def _iter_reused_group_outputs(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    *,
    group: dict[str, Any],
    member_plans: list[dict[str, Any]],
    source_tile_shape: tuple[int, int],
    angles: np.ndarray,
    resource_policy: OpticalDiskRawResourcePolicy,
) -> Iterator[tuple[tuple[int, int, int, int], np.ndarray, dict[str, Any]]]:
    """Yield child means with one shared optical execution per group.

    Child evidence repeats the shared optical counters. Consumers must count
    those counters once per optical_reuse.key_sha256, as explicitly annotated
    in every child line; top-level actual work counters do so during writing.
    """
    union = tuple(group["union_optical_roi_row_col_exclusive"])
    members = [item["sensor_tile_bounds_row_col_exclusive"] for item in member_plans]
    sensor_shape, _, _ = _sensor_contract(sensor)
    for item in member_plans:
        if (
            item.get("status") != "ready_under_declared_policy"
            or item.get("source_identity") != provider.source_identity
            or item.get("full_optical_state_sha256") != provider.optics_state_sha256
            or item.get("global_scene_shape_row_col") != list(provider.global_shape)
            or item.get("sensor_global_shape_row_col") != list(sensor_shape)
            or item.get("source_tile_shape_row_col") != list(source_tile_shape)
            or item.get("sample_angles_deg") != angles.tolist()
            or item.get("wavelength_nm") != provider.wavelengths_nm.tolist()
            or item.get("resource_policy") != resource_policy.optical_tile.as_dict()
        ):
            raise ValueError("Admitted optical reuse member changed before execution.")
    if (
        group.get("schema") != _REUSE_GROUP_SCHEMA
        or group.get("member_sensor_tiles_row_col_exclusive") != members
        or group.get("key_sha256") != _reuse_group_key(
            provider, member_plans, union, source_tile_shape, angles
        )
        or union != (
            min(item["optical_roi_row_col_exclusive"][0] for item in member_plans),
            max(item["optical_roi_row_col_exclusive"][1] for item in member_plans),
            min(item["optical_roi_row_col_exclusive"][2] for item in member_plans),
            max(item["optical_roi_row_col_exclusive"][3] for item in member_plans),
        )
    ):
        raise ValueError("Admitted optical reuse group changed before execution.")
    padding = tuple(member_plans[0]["strict_psf_padding_row_col"])
    optical, optical_evidence = _strict_raytrace_apply_psf_bounded_roi_provider(
        provider.global_shape,
        provider.read_tile,
        provider.optics,
        provider.wavelengths_nm,
        provider.oi_pitch_um * 1e-6,
        padding,
        angles,
        output_roi_row_col=union,
        source_identity=provider.source_identity,
        source_tile_shape_row_col=source_tile_shape,
    )
    if (
        optical_evidence.get("source_identity") != provider.source_identity
        or optical_evidence.get("source_signature")
        != _strict_raytrace_cache_signature(provider.optics)
        or optical_evidence.get("output_roi_row_col") != list(union)
        or optical_evidence.get("source_tiles_processed")
        != group["source_tiles_after_reuse_estimate"]
    ):
        raise ValueError("Executed optical reuse group lost its admitted global identity.")
    optical.setflags(write=False)
    for member_index, item in enumerate(member_plans):
        child = tuple(item["optical_roi_row_col_exclusive"])
        bounds = tuple(item["sensor_tile_bounds_row_col_exclusive"])
        optical_child = optical[
            child[0] - union[0]:child[1] - union[0],
            child[2] - union[2]:child[3] - union[2],
        ]
        padded_child = (
            child[0] + padding[0], child[1] + padding[0],
            child[2] + padding[1], child[3] + padding[1],
        )
        mean, sensor_evidence = compute_fixed_sensor_tile_mean_electrons(
            sensor,
            optical_child,
            full_oi_shape_row_col=tuple(item["full_padded_oi_shape_row_col"]),
            optical_roi_bounds_row_col_exclusive=padded_child,
            sensor_tile_bounds_row_col_exclusive=bounds,
            wavelength_nm=provider.wavelengths_nm,
            oi_sample_spacing_m=provider.oi_pitch_um * 1e-6,
        )
        if (
            sensor_evidence["required_global_oi_indices_inclusive_by_axis"]
            != item["sensor_preflight"]["required_global_oi_indices_inclusive_by_axis"]
            or _optical_state_fingerprint(provider.optics) != provider.optics_state_sha256
        ):
            raise ValueError("Optical reuse member changed its admitted sensor dependency.")
        evidence = {
            "schema": item["schema"],
            "status": "diagnostic_offaxis_mean_executed",
            "provider": provider.metadata,
            "full_optical_state_sha256": provider.optics_state_sha256,
            "full_optical_state_verified_before_and_after": True,
            "resource_preflight": item,
            "optical_execution": optical_evidence,
            "optical_reuse": {
                "schema": _REUSE_GROUP_SCHEMA,
                "key_sha256": group["key_sha256"],
                "counter_aggregation": "shared_group_count_once_by_key_sha256",
                "group_member_index": member_index,
                "optical_execution_counted_in_summary": member_index == 0,
                "member_sensor_tiles_row_col_exclusive": members,
                "union_optical_roi_row_col_exclusive": list(union),
                "child_optical_roi_row_col_exclusive": list(child),
                "cache_lifetime": group["cache_lifetime"],
                "group_plan_sha256": hashlib.sha256(_json_bytes(group)).hexdigest(),
            },
            "sensor_execution": sensor_evidence,
            "output_mean_electrons_shape_row_col": list(mean.shape),
            "readout_or_isp_executed": False,
            "noise_or_fixed_pattern_executed": False,
            "full_frame_execution_qualified": False,
            "physical_fov_qualified": False,
            "physical_scene_halo_qualified": False,
            "actual_peak_rss_bytes_measured": None,
            "hard_process_rss_limit_enforced": False,
        }
        yield bounds, mean, evidence


def _compute_optical_to_disk_raw_admitted(
    provider: AnalyticStrictGeometryTileProvider,
    sensor: Sensor,
    *,
    angles: np.ndarray,
    source_tile_shape_row_col: tuple[int, int],
    seed: int,
    row_chunk_rows: int,
    resource_policy: OpticalDiskRawResourcePolicy,
    plan: dict[str, Any],
    admitted_plans: BinaryIO,
) -> dict[str, Any]:
    destination = Path(plan["output_directory"])
    temporary = Path(tempfile.mkdtemp(prefix=".opticalraw_tmp_", dir=destination.parent))
    committed = False
    try:
        shape = tuple(plan["full_sensor_shape_row_col"])
        mean_path = temporary / "mean_electrons.npy"
        mean = np.lib.format.open_memmap(
            mean_path, mode="w+", dtype=np.float64, shape=shape
        )
        # The true disk-backed input and final nested parent now exist.  Do a
        # second readout admission before any optical tile is executed.
        readout_plan = preflight_out_of_core_readout(
            sensor, mean, temporary / "readout", seed=seed,
            row_chunk_rows=row_chunk_rows, policy=resource_policy.readout,
        )
        if readout_plan["status"] != "ready_under_declared_policy":
            raise ValueError(
                f"resource_blocked: {readout_plan['status']}: {readout_plan['reason']}"
            )
        tile_digest = hashlib.sha256()
        tile_count = 0
        source_tiles_processed = 0
        sparse_convolutions = 0
        generated_kernels = 0
        group_plans = plan.get("optical_reuse_group_plans", [])
        if group_plans and (
            plan.get("optical_reuse_group_plan_sha256")
            != hashlib.sha256(_json_bytes(group_plans)).hexdigest()
        ):
            raise ValueError("Admitted optical reuse group plans changed before execution.")
        groups_by_start: dict[int, dict[str, Any]] = {}
        occupied: set[int] = set()
        all_tiles = plan["sensor_tiles_row_col_exclusive"]
        for group in group_plans:
            members = group["member_sensor_tiles_row_col_exclusive"]
            try:
                start = all_tiles.index(members[0])
            except ValueError as exc:
                raise ValueError("Optical reuse group names an unknown sensor tile.") from exc
            indices = set(range(start, start + len(members)))
            if (
                len(members) < 2
                or all_tiles[start:start + len(members)] != members
                or occupied.intersection(indices)
            ):
                raise ValueError("Optical reuse groups must cover consecutive disjoint tiles.")
            occupied.update(indices)
            groups_by_start[start] = group
        with (temporary / "tile_execution.jsonl").open("w", encoding="utf-8") as tile_file:
            tile_index = 0
            while tile_index < len(all_tiles):
                group = groups_by_start.get(tile_index)
                member_count = len(group["member_sensor_tiles_row_col_exclusive"]) if group else 1
                member_plans = []
                for offset in range(member_count):
                    tile = all_tiles[tile_index + offset]
                    line = admitted_plans.readline()
                    if not line.endswith(b"\n"):
                        raise ValueError("Admitted optical tile plan disappeared during execution.")
                    tile_plan = json.loads(line[:-1])
                    if tile_plan.get("sensor_tile_bounds_row_col_exclusive") != tile:
                        raise ValueError(
                            "Admitted optical tile plan order changed during execution."
                        )
                    member_plans.append(tile_plan)
                if (
                    provider.source_identity != plan["source_identity"]
                    or _optical_state_fingerprint(provider.optics)
                    != plan["full_optical_state_sha256"]
                    or _sensor_identity(sensor) != plan["sensor_model_state_sha256"]
                    or sensor.data
                ):
                    raise ValueError("Provider or sensor model mutated during optical capture.")
                if group is None:
                    tile = tuple(all_tiles[tile_index])
                    values, evidence = _compute_preflighted_streamed_analytic_offaxis_mean(
                        provider, sensor,
                        sensor_tile_bounds_row_col_exclusive=tile,
                        source_tile_shape_row_col=source_tile_shape_row_col,
                        sample_angles_deg=angles,
                        resource_policy=resource_policy.optical_tile,
                        admitted_plan=member_plans[0],
                    )
                    outputs = iter(((tile, values, evidence),))
                else:
                    outputs = _iter_reused_group_outputs(
                        provider, sensor, group=group, member_plans=member_plans,
                        source_tile_shape=tuple(source_tile_shape_row_col), angles=angles,
                        resource_policy=resource_policy,
                    )
                emitted = 0
                for tile, values, evidence in outputs:
                    r0, r1, c0, c1 = tile
                    if (
                        values.shape != (r1 - r0, c1 - c0)
                        or not np.isfinite(values).all() or np.any(values < 0)
                    ):
                        raise ValueError("Optical sensor tile returned invalid mean electrons.")
                    if (
                        evidence["provider"]["source_identity"] != plan["source_identity"]
                        or evidence["full_optical_state_sha256"]
                        != plan["full_optical_state_sha256"]
                        or evidence["sensor_execution"]["sensor_tile_bounds_row_col_exclusive"]
                        != list(tile)
                    ):
                        raise ValueError("Optical tile execution lost model or global bounds.")
                    mean[r0:r1, c0:c1] = values
                    line = _json_bytes(evidence) + b"\n"
                    tile_file.write(line.decode())
                    tile_digest.update(line)
                    tile_count += 1
                    if emitted == 0:
                        optical_execution = evidence["optical_execution"]
                        source_tiles_processed += int(optical_execution["source_tiles_processed"])
                        sparse_convolutions += int(optical_execution["sparse_convolutions"])
                        generated_kernels += int(optical_execution["generated_kernel_count"])
                    emitted += 1
                    if tile_file.tell() > resource_policy.max_total_output_bytes:
                        raise ValueError("Tile evidence exceeds declared total output budget.")
                if emitted != member_count:
                    raise ValueError("Optical reuse group returned an incomplete sensor tile set.")
                tile_index += member_count
        mean.flush()
        del mean
        if (
            provider.source_identity != plan["source_identity"]
            or _optical_state_fingerprint(provider.optics) != plan["full_optical_state_sha256"]
            or _sensor_identity(sensor) != plan["sensor_model_state_sha256"]
            or sensor.data
        ):
            raise ValueError("Provider or sensor model mutated before readout.")
        mean_readonly = np.load(mean_path, mmap_mode="r")
        readout_result = compute_out_of_core_readout(
            sensor, mean_readonly, temporary / "readout", seed=seed,
            row_chunk_rows=row_chunk_rows, policy=resource_policy.readout,
        )
        del mean_readonly
        nested_path = temporary / "readout" / "metadata.json"
        nested = json.loads(nested_path.read_text(encoding="utf-8"))
        nested["preflight"]["output_directory"] = str(destination / "readout")
        nested_path.write_text(
            json.dumps(nested, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        metadata = {
            "schema": SCHEMA,
            "status": "diagnostic_complete_sensor_raw_executed",
            "preflight": plan,
            "source_identity": plan["source_identity"],
            "full_optical_state_sha256": plan["full_optical_state_sha256"],
            "sensor_model_state_sha256": plan["sensor_model_state_sha256"],
            "sensor_tile_execution_count": tile_count,
            "sensor_tile_admitted_plans_verified_before_optics": True,
            "sensor_tile_geometry_halo_preflight_count": tile_count,
            "sensor_tile_geometry_halo_preflight_repeated_during_optics": False,
            "optical_reuse_group_count": len(group_plans),
            "optical_work_counter_aggregation": "once_per_reuse_group_or_singleton",
            "optical_source_tiles_processed_actual": source_tiles_processed,
            "optical_sparse_convolutions_actual": sparse_convolutions,
            "optical_generated_kernels_actual": generated_kernels,
            "sensor_tile_execution_jsonl_sha256": tile_digest.hexdigest(),
            "mean_electrons_npy_sha256": _sha256_file(mean_path),
            "mean_electrons_content_sha256": readout_result["mean_input_content_sha256"],
            "readout_metadata_sha256": _sha256_file(nested_path),
            "readout_sensor_capture": nested["sensor_capture"],
            "files": {
                "mean_electrons": "mean_electrons.npy",
                "tile_execution": "tile_execution.jsonl",
                "readout_metadata": "readout/metadata.json",
                "raw_dn": "readout/raw_dn.npy",
                "reference_dn": "readout/reference_dn.npy",
                "volts": "readout/volts.npy",
            },
            "full_scene_or_oi_materialized": False,
            "full_sensor_mean_or_raw_materialized_in_ram": False,
            "native_seed_bit_parity": False,
            "hard_process_rss_limit_enforced": False,
            "physical_scene_halo_qualified": False,
            "physical_fov_qualified": False,
            "product_full_frame_qualified": False,
        }
        (temporary / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        actual_output_bytes = sum(
            path.stat().st_size for path in temporary.rglob("*") if path.is_file()
        )
        if actual_output_bytes > resource_policy.max_total_output_bytes:
            raise ValueError("Actual artifacts exceed declared total output budget.")
        if destination.exists():
            raise ValueError("Output directory appeared during capture; refusing overwrite.")
        os.rename(temporary, destination)
        committed = True
        return {
            **metadata,
            "output_directory": str(destination),
            "actual_output_bytes": actual_output_bytes,
            "mean_electrons_path": str(destination / "mean_electrons.npy"),
            "raw_dn_path": str(destination / "readout" / "raw_dn.npy"),
            "reference_dn_path": str(destination / "readout" / "reference_dn.npy"),
            "volts_path": str(destination / "readout" / "volts.npy"),
            "metadata_path": str(destination / "metadata.json"),
        }
    finally:
        if not committed:
            shutil.rmtree(temporary)
