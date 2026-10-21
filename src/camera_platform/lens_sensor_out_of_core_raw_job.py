"""Persist a saved Lens DB x Sensor DB disk-backed RAW diagnostic.

The full optical image, mean-electron plane, and RAW arrays are never held in
RAM by this job.  The declared sensor geometry is a virtual centered crop;
success establishes a numerical RAW calculation, not a product FOV or MTF.
"""

from __future__ import annotations

import math
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from .lens_sensor_out_of_core_optical_raw import (
    OpticalDiskRawResourcePolicy,
    compute_optical_to_disk_raw,
    preflight_optical_to_disk_raw,
)
from .lens_sensor_out_of_core_readout import OutOfCoreReadoutPolicy
from .lens_sensor_streamed_analytic_capture import AnalyticCaptureResourcePolicy
from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_out_of_core_raw_diagnostic_v1"
JOB_KIND = "lens_sensor_out_of_core_raw"
WORKER_TIMEOUT_S = 600
_MAX_SENSOR_SIDE = 256
_MAX_SENSOR_CELLS = 65_536
_MAX_SCENE_SIDE = 4096
_MAX_SENSOR_TILES = 256
_MAX_SOURCE_TILE_SIDE = 256
_WAVELENGTHS_NM = np.array([450.0, 550.0, 650.0])
_ANGLES_DEG = np.arange(0.0, 361.0, 10.0)

LIMITATION = (
    "Saved geometric raytrace PSF and saved db_estimate_v1 sensor response/readout "
    "produce a disk-backed numerical RAW for a user-declared virtual centered "
    "crop and analytic infinity-conjugate Scene. Lens absolute transmission and "
    "relative illumination remain unknown unity placeholders. Diffraction, "
    "validated angular pixel response, mount, product active mode, physical FOV/MTF, "
    "Scene sampling convergence, and measured accuracy are not established. "
    "Coordinate-keyed readout follows the native physical order but deliberately "
    "does not reproduce the native NumPy seed bit-for-bit. Resource admission is "
    "a numerical policy estimate and disk snapshot, not a hard RSS or disk reservation."
)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "저장 렌즈 × 센서 디스크 기반 RAW 수치 진단",
        "lens_revision_id": "",
        "sensor_revision": {"identifier": "", "run_id": ""},
        "virtual_sensor_geometry": {
            "kind": "user_declared_virtual_centered_crop",
            "mode_id": "",
            "rows": 32,
            "cols": 32,
            "pixel_pitch_um": None,
            "optical_axis_offset_um_row_col": [0.0, 0.0],
        },
        "analytic_scene": {
            "kind": "continuous_slanted_edge_d65",
            "object_distance_m": "infinity",
            "shape_row_col": [1024, 1024],
            "horizontal_fov_deg": 1.0,
            "bright_luminance_cd_m2": 1000.0,
            "edge_offset_deg": 0.0,
        },
        "capture": {"exposure_s": 0.004, "noise_flag": 0, "seed": 7},
        "sensor_tile_shape_row_col": [16, 16],
        "source_tile_shape_row_col": [64, 64],
        "readout_row_chunk_rows": 16,
        "reuse_optical_groups": False,
        "accept_approximate_lens_radiometry": False,
        "acknowledge_virtual_geometry_and_limits": False,
    }


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number in [{minimum}, {maximum}].")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a finite number in [{minimum}, {maximum}]."
        ) from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} must be a finite number in [{minimum}, {maximum}].")
    return result


def _pair(value: Any, name: str, minimum: int, maximum: int) -> list[int]:
    if (
        not isinstance(value, list) or len(value) != 2
        or any(type(item) is not int or not minimum <= item <= maximum for item in value)
    ):
        raise ValueError(f"{name} requires two integers in [{minimum}, {maximum}].")
    return value


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    template = default_spec()
    if not isinstance(payload, dict) or set(payload) not in (
        set(template), set(template) - {"reuse_optical_groups"}
    ):
        raise ValueError("Declare exactly the disk-backed lens x sensor RAW fields.")
    if type(payload.get("reuse_optical_groups", False)) is not bool:
        raise ValueError("reuse_optical_groups must be an explicit boolean opt-in.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported disk-backed RAW schema.")
    if (
        payload["accept_approximate_lens_radiometry"] is not True
        or payload["acknowledge_virtual_geometry_and_limits"] is not True
    ):
        raise ValueError("Explicit radiometry and virtual-geometry acknowledgements are required.")
    if not isinstance(payload["name"], str) or not 1 <= len(payload["name"].strip()) <= 160:
        raise ValueError("Name requires 1..160 characters.")
    lens_id = payload["lens_revision_id"]
    if not isinstance(lens_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", lens_id):
        raise ValueError("Choose a Lens DB revision UUID, never a local path.")
    selected = payload["sensor_revision"]
    if (
        not isinstance(selected, dict)
        or set(selected) != {"identifier", "run_id"}
        or not isinstance(selected["identifier"], str)
        or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", selected["identifier"])
        or not isinstance(selected["run_id"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", selected["run_id"])
    ):
        raise ValueError("Choose one saved Sensor DB report code and run hash, never a path.")
    virtual = payload["virtual_sensor_geometry"]
    if not isinstance(virtual, dict) or set(virtual) != set(template["virtual_sensor_geometry"]):
        raise ValueError("Declare the complete virtual sensor geometry.")
    if virtual["kind"] != "user_declared_virtual_centered_crop":
        raise ValueError("Only an explicitly virtual centered sensor crop is supported.")
    if (
        not isinstance(virtual["mode_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", virtual["mode_id"])
    ):
        raise ValueError("Virtual mode_id requires 1..64 safe identifier characters.")
    rows, cols = _pair(
        [virtual["rows"], virtual["cols"]], "virtual shape", 8, _MAX_SENSOR_SIDE
    )
    if rows * cols > _MAX_SENSOR_CELLS:
        raise ValueError("Virtual crop exceeds the disk-backed diagnostic cell policy.")
    pitch = _number(virtual["pixel_pitch_um"], "pixel_pitch_um", 0.1, 100.0)
    offset = virtual["optical_axis_offset_um_row_col"]
    if (
        not isinstance(offset, list) or len(offset) != 2
        or any(type(item) not in (int, float) or float(item) != 0.0 for item in offset)
    ):
        raise ValueError("Current stitched sensor origin requires an explicit [0, 0] offset.")
    scene = payload["analytic_scene"]
    if not isinstance(scene, dict) or set(scene) != set(template["analytic_scene"]):
        raise ValueError("Declare the complete analytic Scene.")
    if scene["kind"] != "continuous_slanted_edge_d65":
        raise ValueError("Only a continuous D65 slanted-edge Scene is supported.")
    if scene["object_distance_m"] != "infinity":
        raise ValueError("This angular analytic Scene requires an infinity-conjugate lens.")
    _pair(scene["shape_row_col"], "analytic_scene.shape_row_col", 32, _MAX_SCENE_SIDE)
    fov = _number(scene["horizontal_fov_deg"], "horizontal_fov_deg", 0.001, 30.0)
    luminance = _number(scene["bright_luminance_cd_m2"], "bright_luminance_cd_m2", 0.01, 10000.0)
    edge = _number(scene["edge_offset_deg"], "edge_offset_deg", -15.0, 15.0)
    if abs(edge) > fov / 2:
        raise ValueError("Edge offset must lie within the declared Scene half-FOV.")
    capture = payload["capture"]
    if not isinstance(capture, dict) or set(capture) != set(template["capture"]):
        raise ValueError("Declare fixed exposure, noise flag and seed.")
    exposure = _number(capture["exposure_s"], "exposure_s", 1e-6, 1.0)
    if type(capture["noise_flag"]) is not int or capture["noise_flag"] not in {0, 2}:
        raise ValueError("noise_flag must be 0 or 2.")
    if type(capture["seed"]) is not int or not 0 <= capture["seed"] < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    tile = _pair(payload["sensor_tile_shape_row_col"], "sensor_tile_shape_row_col", 2, 64)
    if tile[0] > rows or tile[1] > cols:
        raise ValueError("Sensor tile cannot exceed the virtual crop.")
    _pair(
        payload["source_tile_shape_row_col"], "source_tile_shape_row_col",
        16, _MAX_SOURCE_TILE_SIDE,
    )
    chunks = payload["readout_row_chunk_rows"]
    if type(chunks) is not int or not 1 <= chunks <= 256:
        raise ValueError("readout_row_chunk_rows must be 1..256.")
    if chunks >= rows:
        raise ValueError(
            "readout_row_chunk_rows must be smaller than full sensor rows "
            "to preserve the out-of-core claim."
        )
    value = deepcopy(payload)
    value.setdefault("reuse_optical_groups", False)
    value["name"] = value["name"].strip()
    value["lens_revision_id"] = lens_id.lower()
    value["virtual_sensor_geometry"]["pixel_pitch_um"] = pitch
    value["virtual_sensor_geometry"]["optical_axis_offset_um_row_col"] = [0.0, 0.0]
    value["analytic_scene"]["horizontal_fov_deg"] = fov
    value["analytic_scene"]["bright_luminance_cd_m2"] = luminance
    value["analytic_scene"]["edge_offset_deg"] = edge
    value["capture"]["exposure_s"] = exposure
    return value


def _db_root(service: Any) -> Path:
    return Path(service.workspace) / "camerae2e_db"


def _tiles(shape: tuple[int, int], tile: list[int]) -> tuple[tuple[int, int, int, int], ...]:
    blocks = tuple(
        (r, min(shape[0], r + tile[0]), c, min(shape[1], c + tile[1]))
        for r in range(0, shape[0], tile[0])
        for c in range(0, shape[1], tile[1])
    )
    if not 2 <= len(blocks) <= _MAX_SENSOR_TILES:
        raise ValueError(
            f"Out-of-core crop requires 2..{_MAX_SENSOR_TILES} complete sensor tiles."
        )
    return blocks


def _resource_policy() -> OpticalDiskRawResourcePolicy:
    """Server admission policy; these numbers are not physical hardware limits."""
    return OpticalDiskRawResourcePolicy(
        optical_tile=AnalyticCaptureResourcePolicy(
            estimated_working_budget_bytes=512 * 1024**2,
            max_geometry_query_cells=64_000_000,
            max_source_tiles=4096,
            max_output_roi_cells=1_048_576,
            max_source_tile_cells=65_536,
        ),
        readout=OutOfCoreReadoutPolicy(
            max_sensor_cells=_MAX_SENSOR_CELLS,
            max_row_chunk_cells=_MAX_SENSOR_CELLS,
            max_transient_bytes=64 * 1024**2,
            max_output_bytes=8 * 1024**2,
            max_resident_input_bytes=256 * 1024**2,
            min_free_after_output_bytes=256 * 1024**2,
        ),
        max_sensor_tiles=_MAX_SENSOR_TILES,
        max_mean_disk_bytes=4 * 1024**2,
        max_total_output_bytes=32 * 1024**2,
        max_combined_working_bytes=1024**3,
        min_free_after_output_bytes=256 * 1024**2,
    )


def _execution_inputs(service: Any, spec: dict[str, Any]):
    from .lens_sensor_scene_geometry_tile import (
        d65_analytic_bright_photons,
        make_analytic_strict_geometry_provider,
    )
    from .lens_sensor_streamed_raw_job import _models

    lens, inspected, optics, sensor = _models(service, spec)
    scene = spec["analytic_scene"]
    bright = d65_analytic_bright_photons(
        workspace=service.workspace,
        horizontal_fov_deg=scene["horizontal_fov_deg"],
        luminance_cd_m2=scene["bright_luminance_cd_m2"],
        wavelengths_nm=_WAVELENGTHS_NM,
    )
    provider = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=tuple(scene["shape_row_col"]),
        scene_horizontal_fov_deg=scene["horizontal_fov_deg"],
        wavelengths_nm=_WAVELENGTHS_NM,
        bright_side_photons=bright,
        analytic_slanted_edge_offset_deg=scene["edge_offset_deg"],
        max_tile_cells=65_536,
    )
    shape = (
        spec["virtual_sensor_geometry"]["rows"],
        spec["virtual_sensor_geometry"]["cols"],
    )
    tiles = _tiles(shape, spec["sensor_tile_shape_row_col"])
    return lens, inspected, provider, sensor, tiles, _resource_policy()


def _stable_preflight(plan: dict[str, Any]) -> dict[str, Any]:
    """Remove only changing private path/disk observations from admission."""
    result = deepcopy(plan)
    result.pop("output_directory", None)
    result.pop("observed_free_disk_bytes_before_output", None)
    return result


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current
    from .offline_assets import offline_asset_identity

    spec = specification(payload)
    lens, inspected, provider, sensor, tiles, policy = _execution_inputs(service, spec)
    native_pitch_um = spec["virtual_sensor_geometry"]["pixel_pitch_um"]
    samples_per_native_pixel = float(native_pitch_um / provider.oi_pitch_um)
    if samples_per_native_pixel < 1.0 - 1e-12:
        raise ValueError(
            "Declared analytic Scene is coarser than the saved sensor pixel pitch; "
            "increase Scene samples or narrow its fixed physical FOV."
        )
    probe_target = service.registry.directory / (".diskraw_admission_" + digest(spec))
    plan = preflight_optical_to_disk_raw(
        provider, sensor, probe_target,
        sensor_tiles_row_col_exclusive=tiles,
        source_tile_shape_row_col=tuple(spec["source_tile_shape_row_col"]),
        sample_angles_deg=_ANGLES_DEG,
        seed=spec["capture"]["seed"],
        row_chunk_rows=spec["readout_row_chunk_rows"],
        resource_policy=policy,
        reuse_optical_groups=spec["reuse_optical_groups"],
    )
    if plan["status"] != "ready_under_declared_policy":
        raise ValueError(f"resource_blocked: {plan['status']}: {plan['reason']}")
    return {
        "name": spec["name"],
        "job_kind": JOB_KIND,
        "plugin_id": SCHEMA,
        "specification": spec,
        "source_identities": {
            "lens": lens["identity"],
            "sensor": inspected["revision"],
            "analytic_scene_sha256": provider.source_identity,
            "optical_state_sha256": provider.optics_state_sha256,
            "sensor_model_state_sha256": plan["sensor_model_state_sha256"],
        },
        "upstream_assets": offline_asset_identity(
            workspace=service.workspace, source=_db_root(service)
        ),
        "sampling": {
            "optical_convolution_sample_pitch_um": float(provider.oi_pitch_um),
            "native_sensor_pixel_pitch_um": native_pitch_um,
            "input_scene_samples_per_native_pixel": samples_per_native_pixel,
            "minimum_one_sample_per_pixel_met": True,
            "sampling_convergence": "not_assessed",
        },
        "resource_preflight": _stable_preflight(plan),
        "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def _checked_npy(path: Path, *, shape: tuple[int, int], dtype: np.dtype) -> None:
    mapped = np.load(path, mmap_mode="r", allow_pickle=False)
    try:
        if mapped.shape != shape or mapped.dtype != dtype:
            raise IntegrityError(f"Stored {path.name} has an unexpected shape or dtype.")
    finally:
        del mapped


def _work_root(state_dir: Path, identity: str) -> Path:
    """Return a job-owned scratch root without accepting a client file path."""
    if not re.fullmatch(r"run-[0-9a-f]{32}", identity):
        raise IntegrityError("Disk-backed scratch requires an exact run identity.")
    parent = state_dir / "out_of_core_work"
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise IntegrityError("Disk-backed scratch parent is not a real directory.")
    return parent / identity


def cleanup_work_root(state_dir: Path, identity: str) -> bool:
    """Remove only this run's real scratch directory after its child is dead."""
    root = _work_root(state_dir, identity)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise IntegrityError("Disk-backed run scratch is not a real directory.")
    if not root.exists():
        return False
    shutil.rmtree(root)
    return True


def _reject_private_paths(path: Path, *, workspace: Path, state_dir: Path) -> None:
    """Inspect line evidence in bounded chunks before making it downloadable."""
    needles = [str(workspace).encode(), str(state_dir).encode()]
    overlap = max(map(len, needles)) - 1
    previous = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            data = previous + chunk
            if any(needle in data for needle in needles):
                raise IntegrityError("Private workspace or state path leaked into tile evidence.")
            previous = data[-overlap:] if overlap else b""


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .offline_assets import offline_asset_identity

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Disk-backed RAW job is not running or was cancelled.")
    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen lens, sensor, Scene, assets, policy or code changed.")
    spec = fresh["specification"]
    lens, inspected, provider, sensor, tiles, policy = _execution_inputs(service, spec)
    if (
        lens["identity"] != fresh["source_identities"]["lens"]
        or inspected["revision"] != fresh["source_identities"]["sensor"]
        or provider.source_identity != fresh["source_identities"]["analytic_scene_sha256"]
        or provider.optics_state_sha256 != fresh["source_identities"]["optical_state_sha256"]
    ):
        raise IntegrityError("Pinned model changed before disk-backed execution.")
    run_root = _work_root(service.registry.directory, job["id"])
    run_root.parent.mkdir(mode=0o700, exist_ok=True)
    if run_root.parent.is_symlink():
        raise IntegrityError("Disk-backed scratch parent became a symlink.")
    run_root.mkdir(mode=0o700, exist_ok=False)
    destination = run_root / "capture"
    try:
        report = compute_optical_to_disk_raw(
            provider, sensor, destination,
            sensor_tiles_row_col_exclusive=tiles,
            source_tile_shape_row_col=tuple(spec["source_tile_shape_row_col"]),
            sample_angles_deg=_ANGLES_DEG,
            seed=spec["capture"]["seed"],
            row_chunk_rows=spec["readout_row_chunk_rows"],
            resource_policy=policy,
            reuse_optical_groups=spec["reuse_optical_groups"],
        )
        if _stable_preflight(report["preflight"]) != fresh["resource_preflight"]:
            raise IntegrityError("Executed numeric preflight differs from frozen admission.")
        if canonical(prepare(service, spec)) != canonical(fresh):
            raise IntegrityError("Pinned sources or policy changed during disk-backed capture.")
        if offline_asset_identity(
            workspace=service.workspace, source=_db_root(service)
        ) != fresh["upstream_assets"]:
            raise IntegrityError("Pinned upstream assets changed during disk-backed capture.")
        if service.registry.get_run(job["id"])["status"] != "running":
            raise RuntimeError("Disk-backed RAW job was cancelled during execution.")
        shape = (
            spec["virtual_sensor_geometry"]["rows"],
            spec["virtual_sensor_geometry"]["cols"],
        )
        files = {
            "mean_electrons": (
                Path(report["mean_electrons_path"]), np.dtype("float64"), "electron"
            ),
            "raw_dn": (Path(report["raw_dn_path"]), np.dtype("int32"), "DN"),
            "reference_dn": (Path(report["reference_dn_path"]), np.dtype("float64"), "DN"),
            "volts": (Path(report["volts_path"]), np.dtype("float64"), "V"),
        }
        artifacts = {}
        for name, (path, dtype, unit) in files.items():
            _checked_npy(path, shape=shape, dtype=dtype)
            descriptor = service.registry.store_file(
                path, f"disk-{name}.npy", "application/octet-stream"
            )
            descriptor.update(dtype=str(dtype), shape=list(shape), unit=unit)
            artifacts[name] = descriptor
        tiles_file = destination / "tile_execution.jsonl"
        _reject_private_paths(
            tiles_file, workspace=Path(service.workspace), state_dir=service.registry.directory
        )
        tile_descriptor = service.registry.store_file(
            tiles_file, "disk-tile-execution.jsonl", "application/x-ndjson"
        )
        artifacts["tile_execution"] = tile_descriptor
        execution = {
            "status": report["status"],
            "preflight": _stable_preflight(report["preflight"]),
            "sensor_tile_execution_count": report["sensor_tile_execution_count"],
            "optical_reuse_group_count": report["optical_reuse_group_count"],
            "optical_work_counter_aggregation": report["optical_work_counter_aggregation"],
            "optical_source_tiles_processed_actual": (
                report["optical_source_tiles_processed_actual"]
            ),
            "optical_sparse_convolutions_actual": (
                report["optical_sparse_convolutions_actual"]
            ),
            "optical_generated_kernels_actual": report["optical_generated_kernels_actual"],
            "sensor_tile_execution_jsonl_sha256": report["sensor_tile_execution_jsonl_sha256"],
            "mean_electrons_npy_sha256": report["mean_electrons_npy_sha256"],
            "mean_electrons_content_sha256": report["mean_electrons_content_sha256"],
            "readout_sensor_capture": report["readout_sensor_capture"],
            "actual_output_bytes": report["actual_output_bytes"],
            "full_scene_or_oi_materialized": report["full_scene_or_oi_materialized"],
            "full_sensor_mean_or_raw_materialized_in_ram": (
                report["full_sensor_mean_or_raw_materialized_in_ram"]
            ),
            "native_seed_bit_parity": report["native_seed_bit_parity"],
            "hard_process_rss_limit_enforced": report["hard_process_rss_limit_enforced"],
        }
        if (
            artifacts["mean_electrons"]["sha256"] != report["mean_electrons_npy_sha256"]
            or artifacts["tile_execution"]["sha256"]
            != report["sensor_tile_execution_jsonl_sha256"]
            or execution["full_sensor_mean_or_raw_materialized_in_ram"] is not False
            or execution["native_seed_bit_parity"] is not False
        ):
            raise IntegrityError("Disk-backed artifact or memory contract differs.")
        summary = {
            "schema": SCHEMA,
            "status": "completed_numerical_raw_diagnostic",
            "specification": spec,
            "source_identities": fresh["source_identities"],
            "upstream_assets": fresh["upstream_assets"],
            "sampling": fresh["sampling"],
            "resource_preflight": fresh["resource_preflight"],
            "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
            "execution": execution,
            "array_artifacts": artifacts,
            "verification": {
                "implementation_verification": "strict_optical_tiles_and_disk_readout_executed",
                "cross_tool_comparison": "not_performed",
                "physical_validation": "not_established",
            },
            "physical_fov_qualified": False,
            "product_full_frame_qualified": False,
            "limitation": LIMITATION,
        }
        serialized = canonical(summary)
        if (
            str(service.workspace) in serialized
            or str(service.registry.directory) in serialized
        ):
            raise IntegrityError("Private workspace or state path leaked into result.")
        result_artifact = service.registry.store_blob(
            serialized.encode(), "disk-raw-diagnostic-result.json", "application/json"
        )
        return {"summary": summary, "artifacts": [*artifacts.values(), result_artifact]}
    finally:
        cleanup_work_root(service.registry.directory, job["id"])


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a disk-backed RAW diagnostic.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        for key in (
            "specification", "source_identities", "upstream_assets", "sampling",
            "resource_preflight", "default_worker_timeout_seconds",
        ):
            if canonical(summary[key]) != canonical(job[key]):
                raise IntegrityError(f"Stored disk-backed RAW {key} differs from frozen request.")
        if (
            summary.get("schema") != SCHEMA
            or summary.get("execution", {}).get("preflight") != job["resource_preflight"]
            or summary.get("physical_fov_qualified") is not False
            or summary.get("product_full_frame_qualified") is not False
        ):
            raise IntegrityError("Stored disk-backed RAW execution contract differs.")
        if job["status"] == "succeeded":
            shape = (
                job["specification"]["virtual_sensor_geometry"]["rows"],
                job["specification"]["virtual_sensor_geometry"]["cols"],
            )
            for key, dtype in (
                ("mean_electrons", np.dtype("float64")),
                ("raw_dn", np.dtype("int32")),
                ("reference_dn", np.dtype("float64")),
                ("volts", np.dtype("float64")),
            ):
                descriptor = summary["array_artifacts"][key]
                if descriptor not in job["artifacts"]:
                    raise IntegrityError(f"Stored {key} artifact link is missing.")
                path = service.registry.blob_path(descriptor["sha256"])
                _checked_npy(path, shape=shape, dtype=dtype)
            tile_descriptor = summary["array_artifacts"]["tile_execution"]
            if tile_descriptor not in job["artifacts"]:
                raise IntegrityError("Stored optical tile evidence link is missing.")
            service.registry.blob_path(tile_descriptor["sha256"])
            result = [
                item for item in job["artifacts"]
                if item["name"] == "disk-raw-diagnostic-result.json"
            ]
            if (
                len(result) != 1
                or service.registry.blob_path(result[0]["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Stored disk-backed RAW summary differs from result artifact.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-disk-raw/template", tags=["lens_sensor_disk_raw"])
    def template(q: str = ""):
        from .lens_catalog_bridge import lens_revision_catalog
        from .sensor_db_bridge import sensor_revision_catalog

        db_root = _db_root(service)
        return {
            "specification": default_spec(),
            "lens_catalog": lens_revision_catalog(catalog_root=db_root, query=q or None),
            "sensor_catalog": sensor_revision_catalog(root=db_root, query=q or None),
            "resource_policy": _resource_policy().as_dict(),
            "maximum_sensor_shape_row_col": [_MAX_SENSOR_SIDE, _MAX_SENSOR_SIDE],
            "maximum_scene_shape_row_col": [_MAX_SCENE_SIDE, _MAX_SCENE_SIDE],
            "default_worker_timeout_seconds": WORKER_TIMEOUT_S,
            "resource_policy_is_hardware_limit": False,
            "limitation": LIMITATION,
        }

    @app.get("/api/v1/lens-sensor-disk-raw/sensor-option", tags=["lens_sensor_disk_raw"])
    def sensor_option(identifier: str, run_id: str):
        from .sensor_db_bridge import inspect_sensor_revision

        if (
            not re.fullmatch(r"[A-Za-z0-9-]{1,64}", identifier)
            or not re.fullmatch(r"[0-9a-f]{64}", run_id)
        ):
            raise ValueError("Choose a saved sensor catalog identifier and run hash.")
        inspected = inspect_sensor_revision(identifier, root=_db_root(service), run_id=run_id)
        response = inspected.get("native_response") or {}
        supported = bool(
            inspected.get("executable")
            and response.get("kind") == "db_estimate_v1"
            and type(response.get("pixel_pitch_um")) in (int, float)
        )
        return {
            "sensor_revision": inspected["revision"],
            "supported_disk_raw_diagnostic": supported,
            "native_response_kind": response.get("kind"),
            "pixel_pitch_um": float(response["pixel_pitch_um"]) if supported else None,
            "saved_native_cfa": response.get("native_cfa") if supported else None,
            "active_product_mode_inferred": False,
            "physical_validation": "not_established",
        }

    @app.post("/api/v1/lens-sensor-disk-raw", status_code=202, tags=["lens_sensor_disk_raw"])
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-disk-raw", tags=["lens_sensor_disk_raw"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-disk-raw/{identity}", tags=["lens_sensor_disk_raw"])
    def detail(identity: str):
        return get(service, identity)

    @app.post("/api/v1/lens-sensor-disk-raw/{identity}/cancel", tags=["lens_sensor_disk_raw"])
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get("/api/v1/lens-sensor-disk-raw/{identity}/export", tags=["lens_sensor_disk_raw"])
    def export(identity: str):
        return Response(
            canonical(get(service, identity)), media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="disk-raw-diagnostic.json"'},
        )
