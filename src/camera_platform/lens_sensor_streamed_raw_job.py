"""Persist a bounded Lens DB x saved Sensor DB stitched-RAW diagnostic.

The user declares a *virtual* centered sensor crop and analytic Scene.  The
stored geometric lens PSF and saved sensor response/readout are reused, but
neither this declaration nor successful computation establishes a product
active mode, physical FOV, image quality, or a peak-memory guarantee.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from .registry import IntegrityError, canonical, digest

SCHEMA = "camerae2e_lens_sensor_streamed_raw_diagnostic_v1"
JOB_KIND = "lens_sensor_streamed_raw"
LIMITATION = (
    "An infinity-conjugate analytic Scene and user-declared virtual centered crop "
    "reuse a pinned geometric Lens DB PSF "
    "and a saved Sensor DB model/readout to produce integer RAW. Lens absolute "
    "transmission and relative illumination are unknown unity placeholders. "
    "Diffraction, validated angular pixel response, product active mode and mount, "
    "physical MTF/FOV, sampling convergence, and measured accuracy are not "
    "established. Readout still materializes full sensor arrays. Admission is a "
    "numerical policy estimate, not an enforced peak-RSS limit or product claim."
)
_MAX_SENSOR_SIDE = 16
_MAX_SCENE_SIDE = 512
_MAX_SENSOR_TILES = 16
_MAX_TILE_SIDE = 128
_WORKING_BUDGET_BYTES = 512 * 1024**2
_WAVELENGTHS_NM = np.array([450.0, 550.0, 650.0])
_ANGLES_DEG = np.arange(0.0, 361.0, 10.0)


def default_spec() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "name": "저장 렌즈 × 센서 타일 RAW 수치 진단",
        "lens_revision_id": "",
        "sensor_revision": {"identifier": "", "run_id": ""},
        "virtual_sensor_geometry": {
            "kind": "user_declared_virtual_centered_crop",
            "mode_id": "",
            "rows": 8,
            "cols": 8,
            "pixel_pitch_um": None,
            "optical_axis_offset_um_row_col": [0.0, 0.0],
        },
        "analytic_scene": {
            "kind": "continuous_slanted_edge_d65",
            "object_distance_m": "infinity",
            "shape_row_col": [512, 512],
            "horizontal_fov_deg": 0.6,
            "bright_luminance_cd_m2": 1000.0,
            "edge_offset_deg": 0.0,
        },
        "capture": {"exposure_s": 0.004, "noise_flag": 0, "seed": 7},
        "sensor_tile_shape_row_col": [4, 4],
        "source_tile_shape_row_col": [64, 64],
        "accept_approximate_lens_radiometry": False,
        "acknowledge_virtual_geometry_and_limits": False,
    }


def _number(value: Any, name: str, low: float, high: float) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].") from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].")
    return result


def _pair(value: Any, name: str, low: int, high: int) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(item) is not int or not low <= item <= high for item in value)
    ):
        raise ValueError(f"{name} requires two integers in [{low}, {high}].")
    return value


def specification(payload: dict[str, Any]) -> dict[str, Any]:
    example = default_spec()
    if not isinstance(payload, dict) or set(payload) != set(example):
        raise ValueError("Declare exactly the streamed lens x sensor RAW fields.")
    if payload["schema"] != SCHEMA:
        raise ValueError("Unsupported streamed RAW schema.")
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
    if not isinstance(virtual, dict) or set(virtual) != set(example["virtual_sensor_geometry"]):
        raise ValueError("Declare the complete virtual sensor geometry.")
    if virtual["kind"] != "user_declared_virtual_centered_crop":
        raise ValueError("Only an explicitly virtual centered sensor crop is supported.")
    if (
        not isinstance(virtual["mode_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", virtual["mode_id"])
    ):
        raise ValueError("Virtual mode_id requires 1..64 safe identifier characters.")
    for axis in ("rows", "cols"):
        if type(virtual[axis]) is not int or not 8 <= virtual[axis] <= _MAX_SENSOR_SIDE:
            raise ValueError(f"Virtual {axis} must be 8..{_MAX_SENSOR_SIDE} pixels.")
    _number(virtual["pixel_pitch_um"], "virtual pixel_pitch_um", 0.1, 100.0)
    offset = virtual["optical_axis_offset_um_row_col"]
    if (
        not isinstance(offset, list)
        or len(offset) != 2
        or any(type(item) not in (int, float) or float(item) != 0.0 for item in offset)
    ):
        raise ValueError("Current stitched sensor origin requires an explicit [0, 0] offset.")
    scene = payload["analytic_scene"]
    if not isinstance(scene, dict) or set(scene) != set(example["analytic_scene"]):
        raise ValueError("Declare the complete analytic Scene.")
    if scene["kind"] != "continuous_slanted_edge_d65":
        raise ValueError("Only a continuous D65 slanted-edge Scene is supported.")
    if scene["object_distance_m"] != "infinity":
        raise ValueError("This angular analytic Scene requires an infinity-conjugate lens.")
    _pair(scene["shape_row_col"], "analytic_scene.shape_row_col", 32, _MAX_SCENE_SIDE)
    fov = _number(scene["horizontal_fov_deg"], "horizontal_fov_deg", 0.001, 30.0)
    _number(scene["bright_luminance_cd_m2"], "bright_luminance_cd_m2", 0.01, 10000.0)
    edge = _number(scene["edge_offset_deg"], "edge_offset_deg", -15.0, 15.0)
    if abs(edge) > fov / 2:
        raise ValueError("Edge offset must lie within the declared Scene half-FOV.")
    capture = payload["capture"]
    if not isinstance(capture, dict) or set(capture) != set(example["capture"]):
        raise ValueError("Declare fixed exposure, noise flag and seed.")
    _number(capture["exposure_s"], "exposure_s", 1e-6, 1.0)
    if type(capture["noise_flag"]) is not int or capture["noise_flag"] not in {0, 2}:
        raise ValueError("noise_flag must be 0 or 2.")
    if type(capture["seed"]) is not int or not 0 <= capture["seed"] < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    tile = _pair(payload["sensor_tile_shape_row_col"], "sensor_tile_shape_row_col", 2, 8)
    if any(tile[axis] > virtual["rows" if axis == 0 else "cols"] for axis in (0, 1)):
        raise ValueError("Sensor tile cannot exceed the virtual crop.")
    _pair(payload["source_tile_shape_row_col"], "source_tile_shape_row_col", 16, _MAX_TILE_SIDE)
    result = deepcopy(payload)
    result["name"] = result["name"].strip()
    result["lens_revision_id"] = lens_id.lower()
    result["sensor_revision"]["identifier"] = selected["identifier"].strip()
    result["virtual_sensor_geometry"]["pixel_pitch_um"] = float(virtual["pixel_pitch_um"])
    result["virtual_sensor_geometry"]["optical_axis_offset_um_row_col"] = [0.0, 0.0]
    result["analytic_scene"]["horizontal_fov_deg"] = fov
    result["analytic_scene"]["bright_luminance_cd_m2"] = float(scene["bright_luminance_cd_m2"])
    result["analytic_scene"]["edge_offset_deg"] = edge
    result["capture"]["exposure_s"] = float(capture["exposure_s"])
    return result


def _db_root(service: Any) -> Path:
    return Path(service.workspace) / "camerae2e_db"


def _tiles(shape: tuple[int, int], tile: list[int]) -> tuple[tuple[int, int, int, int], ...]:
    blocks = tuple(
        (r, min(shape[0], r + tile[0]), c, min(shape[1], c + tile[1]))
        for r in range(0, shape[0], tile[0])
        for c in range(0, shape[1], tile[1])
    )
    if len(blocks) > _MAX_SENSOR_TILES:
        raise ValueError(f"Virtual sensor requires more than {_MAX_SENSOR_TILES} tiles.")
    return blocks


def _models(service: Any, spec: dict[str, Any]):
    """Load only pinned local DB models; keep local paths out of job records."""
    from pyisetcam.camera import camera_create, camera_get
    from pyisetcam.image_sensor_db import image_sensor_db_reconcile_scenario
    from pyisetcam.lens_patents import lens_patent_raytrace_optics
    from pyisetcam.sensor_simulation_db import sensor_simulation_db_config
    from pyisetcam.system_faca import (
        _apply_physics_and_sensor_overrides,
        _configure_scenario_readout,
    )

    from .lens_catalog_bridge import _asset, _revision_source, geometric_lens_preview
    from .lens_sensor_centered_oi import _sensor_contract
    from .offline_assets import offline_asset_store
    from .sensor_db_bridge import inspect_sensor_revision

    db_root = _db_root(service)
    lens = geometric_lens_preview(spec["lens_revision_id"], catalog_root=db_root)
    selected = spec["sensor_revision"]
    inspected = inspect_sensor_revision(
        selected["identifier"], root=db_root, run_id=selected["run_id"]
    )
    if not inspected["executable"] or inspected["native_response"]["kind"] != "db_estimate_v1":
        raise ValueError("Only an executable saved db_estimate_v1 sensor is supported.")
    pitch = float(inspected["native_response"]["pixel_pitch_um"])
    declared = spec["virtual_sensor_geometry"]["pixel_pitch_um"]
    if not math.isclose(declared, pitch, rel_tol=1e-9, abs_tol=1e-12):
        raise ValueError("Virtual pixel pitch must equal the pinned saved sensor pitch.")
    configuration = sensor_simulation_db_config(
        inspected["revision"]["report_code"], root=db_root,
        run_id=inspected["revision"]["run_id"],
    )
    saved = configuration["scenario"]
    if (
        saved.get("sensor_output_mode") != "digital_raw"
        or saved.get("sensor_illumination") is not None
    ):
        raise ValueError("Saved sensor must have a non-angular digital_raw contract.")
    revision, snapshot, db_path, _ = _revision_source(spec["lens_revision_id"], db_root)
    simulation_id, psf_path, validation, _ = _asset(revision, snapshot, db_path, db_root)
    optics = lens_patent_raytrace_optics(
        simulation_id, db_path=db_path, psf_dir=psf_path.parent,
        allow_approximate_radiometry=True,
    )
    ray = optics["raytrace"]
    if (
        not math.isinf(float(ray.get("object_distance_m", math.nan)))
        or lens["model"]["object_distance_m"] != spec["analytic_scene"]["object_distance_m"]
    ):
        raise ValueError("Pinned lens PSF conjugate differs from the declared analytic Scene.")
    if (
        ray.get("asset_sha256") != lens["identity"]["asset_sha256"]
        or ray.get("source_prescription_sha256") != validation["source_prescription_sha256"]
        or ray.get("psf_kind") != "geometric"
        or ray.get("diffraction_included") is not False
        or ray.get("radiometry_assumption_accepted") is not True
        or ray.get("radiometry_status") == "validated"
    ):
        raise ValueError("Loaded lens differs from pinned unknown-radiometry geometric PSF.")
    for table in ("geometry", "relative_illumination", "psf"):
        if not np.array_equal(np.asarray(ray[table]["wavelength_nm"], float), _WAVELENGTHS_NM):
            raise ValueError("Lens tables must use exactly the supported wavelength axis.")
    transmission = optics.get("transmittance", {})
    relative = ray["relative_illumination"]
    if (
        transmission.get("status") != "unknown"
        or not np.array_equal(np.asarray(transmission.get("scale")), np.ones(3))
        or relative.get("status") != "unknown"
        or not np.array_equal(
            np.asarray(relative.get("function")),
            np.ones_like(np.asarray(relative.get("function"))),
        )
    ):
        raise ValueError("Lens radiometry must be its declared unity placeholder.")
    assets = offline_asset_store(workspace=service.workspace, source=db_root)
    virtual = spec["virtual_sensor_geometry"]
    config = image_sensor_db_reconcile_scenario({
        **saved,
        "sensor": {
            **saved.get("sensor", {}),
            "size": [virtual["rows"], virtual["cols"]],
            "integration_time": spec["capture"]["exposure_s"],
            "noise_flag": spec["capture"]["noise_flag"],
        },
        "sensor_resize": False,
    })
    config["sensor_output_mode"] = "digital_raw"
    with assets.bound_default(assets):
        camera = camera_create(asset_store=assets)
        camera, _ = _apply_physics_and_sensor_overrides(camera, config, assets)
        sensor = _configure_scenario_readout(camera_get(camera, "sensor"), config, "digital_raw")
    shape, pixel, _ = _sensor_contract(sensor)
    if shape != (virtual["rows"], virtual["cols"]) or not np.allclose(
        pixel * 1e6, [declared, declared], rtol=1e-9, atol=1e-12
    ):
        raise ValueError("Loaded sensor geometry differs from the declared virtual crop.")
    binding = sensor.metadata.get("sensor_simulation_db") or {}
    expected = inspected["revision"]
    if any(binding.get(key) != expected[key] for key in ("run_id", "report_code", "result_sha256")):
        raise ValueError("Configured sensor differs from pinned saved run.")
    return lens, inspected, optics, sensor


def _execution_inputs(service: Any, spec: dict[str, Any]):
    from .lens_sensor_scene_geometry_tile import (
        d65_analytic_bright_photons,
        make_analytic_strict_geometry_provider,
    )
    from .lens_sensor_streamed_analytic_capture import AnalyticCaptureResourcePolicy
    from .lens_sensor_streamed_full_raw import StitchedRawResourcePolicy

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
        max_tile_cells=_MAX_TILE_SIDE**2,
    )
    sensor_shape = (
        spec["virtual_sensor_geometry"]["rows"],
        spec["virtual_sensor_geometry"]["cols"],
    )
    tiles = _tiles(sensor_shape, spec["sensor_tile_shape_row_col"])
    policy = StitchedRawResourcePolicy(
        optical_tile=AnalyticCaptureResourcePolicy(
            estimated_working_budget_bytes=_WORKING_BUDGET_BYTES,
            max_geometry_query_cells=2_000_000,
            max_source_tiles=256,
            max_output_roi_cells=_MAX_SENSOR_SIDE**2,
            max_source_tile_cells=_MAX_TILE_SIDE**2,
        ),
        estimated_working_budget_bytes=_WORKING_BUDGET_BYTES,
        max_full_sensor_cells=_MAX_SENSOR_SIDE**2,
        max_sensor_tiles=_MAX_SENSOR_TILES,
    )
    return lens, inspected, provider, sensor, tiles, policy


def prepare(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    from .imaging import _current
    from .lens_sensor_streamed_full_raw import preflight_streamed_full_raw
    from .offline_assets import offline_asset_identity

    spec = specification(payload)
    lens, inspected, provider, sensor, tiles, policy = _execution_inputs(service, spec)
    native_pitch_um = spec["virtual_sensor_geometry"]["pixel_pitch_um"]
    samples_per_native_pixel = float(native_pitch_um / provider.oi_pitch_um)
    if samples_per_native_pixel < 1.0 - 1e-12:
        raise ValueError(
            "Declared analytic Scene is coarser than the saved sensor pixel pitch; "
            "increase its sample count or narrow its fixed physical FOV."
        )
    plan = preflight_streamed_full_raw(
        provider,
        sensor,
        sensor_tiles_row_col_exclusive=tiles,
        source_tile_shape_row_col=tuple(spec["source_tile_shape_row_col"]),
        sample_angles_deg=_ANGLES_DEG,
        resource_policy=policy,
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
            "scene_grid_convergence": "not_assessed",
        },
        "resource_preflight": plan,
        "environment": _current(service),
        "limitation": LIMITATION,
    }


def submit(service: Any, payload: dict[str, Any]) -> dict[str, Any]:
    frozen = prepare(service, payload)
    return service.registry.submit(frozen, digest(frozen))


def _npy(array: np.ndarray) -> bytes:
    stream = io.BytesIO()
    np.save(stream, array, allow_pickle=False)
    return stream.getvalue()


def execute(service: Any, job: dict[str, Any]) -> dict[str, Any]:
    from .lens_sensor_streamed_full_raw import compute_streamed_full_raw
    from .offline_assets import offline_asset_identity

    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Streamed RAW job is not running or was cancelled.")
    fresh = prepare(service, job["specification"])
    if any(canonical(job.get(key)) != canonical(value) for key, value in fresh.items()):
        raise IntegrityError("Frozen lens, sensor, Scene, assets, resource policy or code changed.")
    spec = fresh["specification"]
    lens, inspected, provider, sensor, tiles, policy = _execution_inputs(service, spec)
    if (
        lens["identity"] != fresh["source_identities"]["lens"]
        or inspected["revision"] != fresh["source_identities"]["sensor"]
        or provider.source_identity != fresh["source_identities"]["analytic_scene_sha256"]
        or provider.optics_state_sha256 != fresh["source_identities"]["optical_state_sha256"]
    ):
        raise IntegrityError("Pinned model changed before stitched execution.")
    captured, report = compute_streamed_full_raw(
        provider,
        sensor,
        sensor_tiles_row_col_exclusive=tiles,
        source_tile_shape_row_col=tuple(spec["source_tile_shape_row_col"]),
        sample_angles_deg=_ANGLES_DEG,
        resource_policy=policy,
        seed=spec["capture"]["seed"],
    )
    if canonical(report["preflight"]) != canonical(fresh["resource_preflight"]):
        raise IntegrityError("Executed numeric preflight differs from frozen admission.")
    raw = np.asarray(captured.data["raw_dn"])
    mean = np.asarray(captured.data["mean_electrons"])
    expected_shape = (
        spec["virtual_sensor_geometry"]["rows"],
        spec["virtual_sensor_geometry"]["cols"],
    )
    if (
        raw.dtype != np.dtype("int32")
        or raw.shape != expected_shape
        or mean.shape != expected_shape
        or not np.isfinite(mean).all()
    ):
        raise ValueError("Native readout did not return the declared RAW/mean contract.")
    if canonical(prepare(service, spec)) != canonical(fresh):
        raise IntegrityError("Pinned sources or code changed during stitched capture.")
    if (
        offline_asset_identity(workspace=service.workspace, source=_db_root(service))
        != fresh["upstream_assets"]
    ):
        raise IntegrityError("Pinned upstream assets changed during stitched capture.")
    if service.registry.get_run(job["id"])["status"] != "running":
        raise RuntimeError("Streamed RAW job was cancelled during execution.")
    artifacts = {}
    for kind, array, unit in (("raw", raw, "DN"), ("mean_electrons", mean, "electron")):
        descriptor = service.registry.store_blob(
            _npy(array), f"streamed-{kind}.npy", "application/octet-stream"
        )
        descriptor.update(dtype=str(array.dtype), shape=list(array.shape), unit=unit)
        artifacts[kind] = descriptor
    summary = {
        "schema": SCHEMA,
        "status": "completed_diagnostic",
        "specification": spec,
        "source_identities": fresh["source_identities"],
        "upstream_assets": fresh["upstream_assets"],
        "sampling": fresh["sampling"],
        "resource_preflight": fresh["resource_preflight"],
        "execution": report,
        "native_capture": {
            "noise_seed": captured.metadata.get("sensor_capture", {}).get("noise_seed"),
            "shape_row_col": list(raw.shape),
            "raw_dtype": "int32",
            "mean_electrons_dtype": str(mean.dtype),
        },
        "array_artifacts": artifacts,
        "verification": {
            "implementation_verification": "executed_native_readout_once",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "physical_fov_qualified": False,
        "product_full_frame_qualified": False,
        "hard_process_rss_limit_enforced": False,
        "limitation": LIMITATION,
    }
    result = service.registry.store_blob(
        canonical(summary).encode(), "streamed-raw-diagnostic-result.json", "application/json"
    )
    return {"summary": summary, "artifacts": [*artifacts.values(), result]}


def get(service: Any, identity: str) -> dict[str, Any]:
    job = service.registry.get_run(identity)
    if job.get("job_kind") != JOB_KIND:
        raise ValueError("Selected job is not a streamed RAW diagnostic.")
    summary = (job.get("result") or {}).get("summary")
    if summary is not None:
        for key in (
            "specification", "source_identities", "upstream_assets", "sampling",
            "resource_preflight",
        ):
            if canonical(summary[key]) != canonical(job[key]):
                raise IntegrityError(f"Stored streamed RAW {key} differs from frozen request.")
        if (
            summary.get("schema") != SCHEMA
            or summary.get("execution", {}).get("preflight") != job["resource_preflight"]
            or summary.get("physical_fov_qualified") is not False
            or summary.get("product_full_frame_qualified") is not False
        ):
            raise IntegrityError("Stored streamed RAW execution contract differs.")
        if job["status"] == "succeeded":
            shape = (
                job["specification"]["virtual_sensor_geometry"]["rows"],
                job["specification"]["virtual_sensor_geometry"]["cols"],
            )
            for key, dtype in (("raw", np.dtype("int32")), ("mean_electrons", np.dtype("float64"))):
                descriptor = summary["array_artifacts"][key]
                if descriptor not in job["artifacts"]:
                    raise IntegrityError(f"Stored {key} artifact link is missing.")
                payload = service.registry.blob_path(descriptor["sha256"]).read_bytes()
                if hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
                    raise IntegrityError(f"Stored {key} artifact hash differs.")
                array = np.load(io.BytesIO(payload), allow_pickle=False)
                if array.dtype != dtype or array.shape != shape:
                    raise IntegrityError(f"Stored {key} artifact array contract differs.")
            result = [item for item in job["artifacts"]
                      if item["name"] == "streamed-raw-diagnostic-result.json"]
            if (
                len(result) != 1
                or service.registry.blob_path(result[0]["sha256"]).read_bytes()
                != canonical(summary).encode()
            ):
                raise IntegrityError("Stored streamed RAW summary differs from result artifact.")
    return job


def list_records(service: Any) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in ("id", "name", "status", "created_at", "error")}
        for item in service.registry.list_runs() if item.get("job_kind") == JOB_KIND
    ]


def install_routes(app: Any, service: Any) -> None:
    from fastapi.responses import Response

    @app.get("/api/v1/lens-sensor-streamed-raw/template", tags=["lens_sensor_streamed_raw"])
    def template(q: str = ""):
        from .lens_catalog_bridge import lens_revision_catalog
        from .sensor_db_bridge import sensor_revision_catalog

        db_root = _db_root(service)
        return {
            "specification": default_spec(),
            "lens_catalog": lens_revision_catalog(catalog_root=db_root, query=q or None),
            "sensor_catalog": sensor_revision_catalog(root=db_root, query=q or None),
            "limitation": LIMITATION,
        }

    @app.get(
        "/api/v1/lens-sensor-streamed-raw/sensor-option",
        tags=["lens_sensor_streamed_raw"],
    )
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
            "supported_streamed_raw_diagnostic": supported,
            "native_response_kind": response.get("kind"),
            "pixel_pitch_um": (
                float(response["pixel_pitch_um"]) if supported else None
            ),
            "saved_native_cfa": response.get("native_cfa") if supported else None,
            "physical_validation": "not_established",
            "active_product_mode_inferred": False,
        }

    @app.post(
        "/api/v1/lens-sensor-streamed-raw", status_code=202,
        tags=["lens_sensor_streamed_raw"],
    )
    def create(payload: dict):
        return submit(service, payload)

    @app.get("/api/v1/lens-sensor-streamed-raw", tags=["lens_sensor_streamed_raw"])
    def listing():
        return list_records(service)

    @app.get("/api/v1/lens-sensor-streamed-raw/{identity}", tags=["lens_sensor_streamed_raw"])
    def detail(identity: str):
        return get(service, identity)

    @app.post(
        "/api/v1/lens-sensor-streamed-raw/{identity}/cancel",
        tags=["lens_sensor_streamed_raw"],
    )
    def cancel(identity: str):
        get(service, identity)
        return service.registry.cancel(identity)

    @app.get(
        "/api/v1/lens-sensor-streamed-raw/{identity}/export",
        tags=["lens_sensor_streamed_raw"],
    )
    def export(identity: str):
        return Response(
            canonical(get(service, identity)),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="streamed-raw-diagnostic.json"'},
        )
