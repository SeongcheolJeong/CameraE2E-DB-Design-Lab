"""Approximate geometric Lens DB × frozen Sensor DB integer RAW preview.

The existing pyisetcam ray-trace optics and saved sensor snapshot do the
calculation.  Current patent PSFs have unknown absolute transmission and
relative illumination; accepting those placeholders is mandatory.  A RAW
array from this path is a numerical sensitivity preview, never a calibrated
camera prediction or a product ranking.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import fftconvolve

from pyisetcam.camera import camera_compute, camera_create, camera_get, camera_set
from pyisetcam.image_sensor_db import image_sensor_db_reconcile_scenario
from pyisetcam.lens_patents import lens_patent_raytrace_optics
from pyisetcam.optics import (
    DEFAULT_RAYTRACE_ANGLE_STEP_DEG,
    _strict_raytrace_cache_signature,
    oi_calculate_illuminance,
    oi_calculate_irradiance,
    rt_geometry,
    rt_psf_grid,
    rt_psf_interp,
)
from pyisetcam.scene import scene_adjust_luminance, scene_get, scene_interpolate_w, scene_set
from pyisetcam.sensor import sensor_get
from pyisetcam.sensor_comparison import _make_scene
from pyisetcam.sensor_simulation_db import sensor_simulation_db_config
from pyisetcam.system_faca import (
    _apply_physics_and_sensor_overrides,
    _configure_scenario_readout,
    camerae2e_run_scenario,
)

from .lens_catalog_bridge import _asset, _revision_source, geometric_lens_preview
from .lens_sensor_angular_geometry import strict_raytrace_pixel_center_angles
from .offline_assets import offline_asset_identity, offline_asset_store
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camerae2e_lens_sensor_geometric_raw_preview_v1"
WAVELENGTHS_NM = (450.0, 550.0, 650.0)
# Development-only interactive ceiling, bracketed by installed proof: a
# 2.36-GiB stack completed, while 4.61/6.07-GiB stacks exceeded 120 seconds.
# It is a resource policy, not a wall-time prediction or a physics criterion.
CENTRAL_PSF_STACK_BUDGET_BYTES = 3 * 1024**3
DIRECT_PSF_REHISTOGRAM_BUDGET_BYTES = 192 * 1024**2
LIMITATION = (
    "Actual integer RAW from the existing pyisetcam geometric raytrace optics "
    "and a frozen saved DB sensor snapshot. Lens spectral transmission and "
    "relative illumination are explicitly assumed unity because the current "
    "lens asset has no validated radiometry. The geometric PSF omits "
    "diffraction and has no validated pixel-integrated sampling. The D65 scene "
    "is resampled to 450/550/650 nm. This is an approximate central-field "
    "numerical preview, not calibrated photon throughput, independent image "
    "quality, measured accuracy, or a product ranking."
)
LOCAL_FIELD_LIMITATION = (
    "A selected stored lens field PSF is applied as a shift-invariant local patch "
    "to the same frozen DB sensor/readout. This is a geometric PSF-only field "
    "sensitivity experiment. The patch is centered on the sensor, and its "
    "radiance is extended at the patch border for convolution; it does not "
    "simulate the full off-axis image location, differential distortion, "
    "chief-ray-angle-dependent sensor response, field illumination or absolute "
    "transmission. It is not a calibrated RAW prediction or product ranking."
)
BOUNDED_LIMITATION = (
    LIMITATION
    + " The strict ray-trace PSF is generated and cropped on demand to avoid "
    "the complete precomputed field/angle/wavelength stack. This changes the "
    "execution strategy, not the geometric optical model or its validation status."
)
RADIANCE_SCENE_LIMITATION = (
    "Actual integer RAW from existing pyisetcam geometric raytrace optics and a "
    "frozen saved DB sensor snapshot. The input is a hash-pinned, user-declared "
    "object-side spectral radiance array, not sensor-plane irradiance or "
    "independently verified ground truth. Its FOV and conjugate are matched "
    "to the saved lens PSF; energy-density samples are linearly interpolated "
    "at 450/550/650 nm and converted to photons without luminance normalization. "
    "Lens transmission and relative illumination are assumed unity because "
    "the stored lens asset has no validated radiometry. Bounded strict PSF "
    "streaming changes execution strategy, not its geometric optical model. "
    "Diffraction, pixel-integrated PSF sampling, CRA-dependent sensor response "
    "and product accuracy remain unvalidated. This 8..32-pixel numerical "
    "sensitivity preview is not a calibrated RAW prediction or product ranking."
)
DIRECT_PSF_REHISTOGRAM_LIMITATION = (
    BOUNDED_LIMITATION
    + " All stored field/wavelength geometric PSF nodes are re-traced from the "
    "pinned prescription and directly re-histogrammed at the declared optical "
    "convolution pitch. This is an opt-in numerical diagnostic of the saved "
    "finite-pupil ray model; source-grid reproduction and derived-kernel hashes "
    "are recorded. It does not establish pupil-density convergence, off-axis "
    "rotation accuracy, diffraction, absolute radiometry, scene halo support, "
    "physical MTF or product ranking."
)


def _number(value: Any, name: str, low: float, high: float) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be finite and within [{low}, {high}].")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be finite and within [{low}, {high}].") from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be finite and within [{low}, {high}].")
    return result


def _scene_samples(
    value: tuple[int, int] | list[int] | None,
    sensor_size: tuple[int, int] | list[int],
    *, max_samples: int = 256,
) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, (tuple, list)) or len(value) != 2
        or any(
            type(item) is not int
            or not max(32, 2 * sensor_size[index]) <= item <= max_samples
            for index, item in enumerate(value)
        )
    ):
        raise ValueError(
            "scene_samples_row_col requires two integers from twice the sensor crop "
            f"(minimum 32) through {max_samples}."
        )
    return int(value[0]), int(value[1])


def central_psf_resource_preflight(
    lens_model: dict[str, Any], sensor_pitch_um: float, sensor_size: tuple[int, int] | list[int],
    *, scene_horizontal_fov_deg: float | None = None,
    scene_samples_row_col: tuple[int, int] | list[int] | None = None,
    max_scene_samples: int = 256,
) -> dict[str, Any]:
    """Bound the exact strict-raytrace kernel stack before queuing a RAW job.

    The estimate follows ``_make_scene`` and pyisetcam's pixel-center PSF
    resampling: 37 azimuths, every stored field, and three requested bands.
    The full field stack is currently built even for a near-axis small scene.
    """
    if (
        not isinstance(sensor_size, (tuple, list))
        or len(sensor_size) != 2
        or any(type(value) is not int or not 8 <= value <= 32 for value in sensor_size)
    ):
        raise ValueError("sensor_size requires two integer dimensions from 8 to 32.")
    pitch_um = _number(sensor_pitch_um, "pixel_pitch_um", 0.1, 100.0)
    scene_samples = _scene_samples(
        scene_samples_row_col, sensor_size, max_samples=max_scene_samples
    )
    if scene_samples is not None and scene_horizontal_fov_deg is None:
        raise ValueError("Explicit scene samples require an explicit common scene FOV.")
    try:
        source_rows, source_cols = lens_model["psf_samples_row_col"]
        source_pitch_row_mm, source_pitch_col_mm = lens_model["sample_spacing_mm_row_col"]
        field_count = len(lens_model["field_samples_mm"])
        nominal_focal_mm = float(lens_model["nominal_focal_length_mm"])
        effective_focal_mm = float(lens_model["effective_focal_length_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "Lens PSF resource metadata is unavailable; central RAW cannot be queued."
        ) from exc
    if (
        type(source_rows) is not int
        or type(source_cols) is not int
        or source_rows < 1
        or source_cols < 1
        or field_count < 2
        or not all(
            math.isfinite(value) and value > 0
            for value in (
                source_pitch_row_mm, source_pitch_col_mm, nominal_focal_mm, effective_focal_mm
            )
        )
    ):
        raise ValueError("Lens PSF resource metadata is invalid; central RAW cannot be queued.")
    explicit_fov = (
        None if scene_horizontal_fov_deg is None else
        _number(scene_horizontal_fov_deg, "scene_horizontal_fov_deg", 0.001, 30.0)
    )
    if explicit_fov is not None:
        minimum_fov = math.degrees(2.0 * math.atan(
            1.2 * sensor_size[1] * pitch_um / (2.0 * effective_focal_mm * 1000.0)
        ))
        if explicit_fov < minimum_fov:
            raise ValueError(
                "scene_horizontal_fov_deg does not cover the native sensor crop "
                f"with 20% scene padding (minimum {minimum_fov:.9g} deg)."
            )
    # `_make_scene` uses max(32, 2 * width); raytrace image distance is its
    # effective focal length, while the requested FOV uses nominal focal length.
    def sampled_kernel(width: int) -> tuple[int, float, int, int]:
        cols = scene_samples[1] if scene_samples is not None else max(32, 2 * width)
        if explicit_fov is None:
            extent_mm = 1.2 * width * pitch_um / 1000.0
            spacing_mm = extent_mm * effective_focal_mm / nominal_focal_mm / cols
        else:
            spacing_mm = (
                2.0 * effective_focal_mm * math.tan(math.radians(explicit_fov) / 2.0)
                / cols
            )
        rows = 2 * math.ceil((source_rows - 1) * source_pitch_row_mm / (2 * spacing_mm)) + 1
        columns = 2 * math.ceil((source_cols - 1) * source_pitch_col_mm / (2 * spacing_mm)) + 1
        return cols, spacing_mm, rows, columns

    scene_cols, sample_spacing_mm, kernel_rows, kernel_cols = sampled_kernel(sensor_size[1])
    angle_count = len(
        np.arange(
            0.0,
            360.0 + DEFAULT_RAYTRACE_ANGLE_STEP_DEG,
            DEFAULT_RAYTRACE_ANGLE_STEP_DEG,
        )
    )
    stack_bytes = angle_count * field_count * len(WAVELENGTHS_NM) * kernel_rows * kernel_cols * 4
    suggested_virtual_size = None
    if stack_bytes > CENTRAL_PSF_STACK_BUDGET_BYTES and explicit_fov is None:
        for side in range(max(sensor_size) + 1, 33):
            _, _, candidate_rows, candidate_cols = sampled_kernel(side)
            candidate_bytes = (
                angle_count * field_count * len(WAVELENGTHS_NM)
                * candidate_rows * candidate_cols * 4
            )
            if candidate_bytes <= CENTRAL_PSF_STACK_BUDGET_BYTES:
                suggested_virtual_size = [side, side]
                break
    result = {
        "status": (
            "within_development_budget"
            if stack_bytes <= CENTRAL_PSF_STACK_BUDGET_BYTES else "resource_blocked"
        ),
        "scope": "central_full_raytrace",
        "source_psf_samples_row_col": [source_rows, source_cols],
        "resampled_kernel_samples_row_col": [kernel_rows, kernel_cols],
        "scene_samples_col": scene_cols,
        "scene_samples_row": (
            scene_samples[0] if scene_samples is not None else max(32, 2 * sensor_size[0])
        ),
        "scene_sample_spacing_um": sample_spacing_mm * 1000.0,
        "azimuth_samples": angle_count,
        "stored_fields": field_count,
        "wavelength_samples": len(WAVELENGTHS_NM),
        "estimated_float32_stack_bytes": stack_bytes,
        "estimated_float32_stack_gib": stack_bytes / 1024**3,
        "minimum_stack_plus_float64_copy_gib": 3 * stack_bytes / 1024**3,
        "development_stack_budget_gib": CENTRAL_PSF_STACK_BUDGET_BYTES / 1024**3,
        "suggested_distinct_virtual_sensor_size": suggested_virtual_size,
        "execution_guaranteed": False,
    }
    if explicit_fov is not None:
        result["scene_horizontal_fov_deg"] = explicit_fov
        result["scene_fov_source"] = "explicit_common_input_scene"
        result["minimum_covering_scene_horizontal_fov_deg"] = minimum_fov
    return result


def require_central_psf_budget(resource: dict[str, Any]) -> None:
    if resource["status"] == "resource_blocked":
        alternate = resource["suggested_distinct_virtual_sensor_size"]
        size_advice = (
            f"A distinct virtual sensor_size={alternate} fits this stack limit but "
            "changes scene FOV and sampling. "
            if alternate is not None else
            "No supported virtual sensor_size fits this stack limit. "
        )
        raise ValueError(
            "resource_blocked: central full-raytrace PSF requests "
            f"{resource['estimated_float32_stack_gib']:.2f} GiB float32 stack "
            f"(at least {resource['minimum_stack_plus_float64_copy_gib']:.2f} GiB "
            "including the solver's float64 copy), above the observed "
            f"{resource['development_stack_budget_gib']:.0f} GiB development limit. "
            "Use explicit local_field_psf scope for a different PSF-only patch "
            f"question. {size_advice}Neither is an equivalent central RAW result."
        )


def bounded_psf_resource_preflight(
    lens_model: dict[str, Any], sensor_pitch_um: float, sensor_size: tuple[int, int] | list[int],
    *, scene_horizontal_fov_deg: float | None = None,
    scene_samples_row_col: tuple[int, int] | list[int] | None = None,
    max_scene_samples: int = 256,
) -> dict[str, Any]:
    """Apply a heuristic development budget to strict streaming PSF execution."""
    legacy = central_psf_resource_preflight(
        lens_model, sensor_pitch_um, sensor_size,
        scene_horizontal_fov_deg=scene_horizontal_fov_deg,
        scene_samples_row_col=scene_samples_row_col,
        max_scene_samples=max_scene_samples,
    )
    rows, cols = legacy["resampled_kernel_samples_row_col"]
    single_float64 = rows * cols * 8
    source_rows, source_cols = legacy["source_psf_samples_row_col"]
    source_workspace = (
        4 * source_rows * source_cols * legacy["stored_fields"]
        * legacy["wavelength_samples"] * 8
    )
    # Four cached float32 kernels, interpolation coordinates, rotated/resampled
    # temporaries and FFT workspace can coexist. A 64x heuristic understated
    # observed P0014 peak RSS, so use 128x. This remains an admission policy,
    # not a proven memory upper bound or execution-time guarantee.
    estimated_working = 128 * single_float64 + source_workspace
    result = {
        "status": (
            "within_development_budget"
            if estimated_working <= CENTRAL_PSF_STACK_BUDGET_BYTES else "resource_blocked"
        ),
        "scope": "central_bounded_strict_raytrace",
        "resampled_kernel_samples_row_col": [rows, cols],
        "legacy_complete_float32_stack_gib": legacy["estimated_float32_stack_gib"],
        "estimated_source_psf_workspace_gib": source_workspace / 1024**3,
        "estimated_bounded_working_gib": estimated_working / 1024**3,
        "development_working_budget_gib": CENTRAL_PSF_STACK_BUDGET_BYTES / 1024**3,
        "max_cached_kernels": 4,
        "memory_estimate_kind": "heuristic_128x_kernel_plus_4x_source_psf_not_measured_rss",
        "execution_guaranteed": False,
    }
    if scene_horizontal_fov_deg is not None:
        result["scene_horizontal_fov_deg"] = legacy["scene_horizontal_fov_deg"]
        result["scene_fov_source"] = legacy["scene_fov_source"]
        result["scene_samples_col"] = legacy["scene_samples_col"]
        result["scene_samples_row"] = legacy["scene_samples_row"]
        result["scene_sample_spacing_um"] = legacy["scene_sample_spacing_um"]
        result["minimum_covering_scene_horizontal_fov_deg"] = (
            legacy["minimum_covering_scene_horizontal_fov_deg"]
        )
    return result


def require_bounded_psf_budget(resource: dict[str, Any]) -> None:
    if resource["status"] == "resource_blocked":
        raise ValueError(
            "resource_blocked: bounded strict ray-trace PSF estimates "
            f"{resource['estimated_bounded_working_gib']:.2f} GiB working memory, "
            f"above the {resource['development_working_budget_gib']:.0f} GiB "
            "development policy. Select a different lens, sampling or bounded virtual window."
        )


def direct_rehistogram_resource_preflight(
    lens_model: dict[str, Any], sensor_pitch_um: float,
    sensor_size: tuple[int, int] | list[int], *,
    scene_horizontal_fov_deg: float,
    scene_samples_row_col: tuple[int, int] | list[int],
) -> dict[str, Any]:
    """Admit only a small, explicit optical grid for finite-ray diagnostics."""
    result = bounded_psf_resource_preflight(
        lens_model, sensor_pitch_um, sensor_size,
        scene_horizontal_fov_deg=scene_horizontal_fov_deg,
        scene_samples_row_col=scene_samples_row_col,
    )
    rows, cols = result["resampled_kernel_samples_row_col"]
    field_count = len(lens_model["field_samples_mm"])
    derived_bytes = rows * cols * field_count * len(WAVELENGTHS_NM) * 8
    result = {
        **result,
        "scope": "central_bounded_direct_same_ray_rehistogram_diagnostic",
        "derived_psf_float64_bytes": derived_bytes,
        "derived_psf_budget_bytes": DIRECT_PSF_REHISTOGRAM_BUDGET_BYTES,
        "field_wavelength_nodes_required": field_count * len(WAVELENGTHS_NM),
        "status": (
            "within_development_budget"
            if result["status"] == "within_development_budget"
            and derived_bytes <= DIRECT_PSF_REHISTOGRAM_BUDGET_BYTES
            else "resource_blocked"
        ),
        "execution_guaranteed": False,
        "hard_memory_or_wall_time_bound": False,
    }
    return result


def require_direct_rehistogram_budget(resource: dict[str, Any]) -> None:
    if resource["status"] != "within_development_budget":
        raise ValueError(
            "resource_blocked: direct same-ray geometric PSF re-histogram "
            f"requires {resource['derived_psf_float64_bytes'] / 1024**2:.1f} MiB "
            "for all field/wavelength kernels, beyond the opt-in diagnostic "
            f"budget of {resource['derived_psf_budget_bytes'] / 1024**2:.0f} MiB "
            "or the bounded strict convolution budget. Use a coarser declared "
            "optical grid or a separately supervised research process."
        )


def _local_field_oi(
    camera: Any,
    scene: Any,
    optics: dict[str, Any],
    *,
    field_index: int,
    field_azimuth_deg: float,
) -> tuple[Any, dict[str, Any]]:
    """Use an exact stored field PSF on a small, centered optical patch.

    Only the PSF varies with selected field. Ray-trace geometry and sensor
    angular response are deliberately not projected to that off-axis pose.
    ``oi_calculate_irradiance`` supplies the no-distortion irradiance and
    ``rt_geometry`` the physical sample pitch; ``rt_psf_interp`` supplies the
    existing solver's normalized PSF sampling.
    """
    oi = rt_geometry(camera_get(camera, "oi"), scene)
    cube = np.asarray(oi_calculate_irradiance(scene, optics), dtype=float)
    waves = np.asarray(oi.fields["wave"], dtype=float)
    positions = np.asarray(optics["raytrace"]["geometry"]["function"], dtype=float)
    x_grid_m, y_grid_m, _ = rt_psf_grid(oi)
    if cube.ndim != 3 or cube.shape[-1] != waves.size or x_grid_m.size == 0:
        raise ValueError("Local field optical patch has no sampled irradiance or PSF support.")
    if positions.shape != (
        len(optics["raytrace"]["psf"]["field_height_mm"]), waves.size
    ):
        raise ValueError("Lens geometry and PSF field/wavelength axes do not match.")
    local = np.empty_like(cube)
    for band, wavelength_nm in enumerate(waves):
        distorted_height_m = float(positions[field_index, band]) / 1e3
        kernel = rt_psf_interp(
            optics,
            field_height_m=distorted_height_m,
            field_angle_deg=field_azimuth_deg,
            wavelength_nm=float(wavelength_nm),
            x_grid_m=x_grid_m,
            y_grid_m=y_grid_m,
        )
        if kernel.ndim != 2 or not np.all(np.isfinite(kernel)) or np.any(kernel < 0):
            raise ValueError("The selected field PSF has invalid sampled energy.")
        # The scene generator describes a local chart patch. Extending its
        # boundary avoids turning a finite crop into a false dark surround.
        pad_y, pad_x = kernel.shape[0] // 2, kernel.shape[1] // 2
        padded = np.pad(cube[:, :, band], ((pad_y, pad_y), (pad_x, pad_x)), mode="edge")
        filtered = fftconvolve(padded, kernel, mode="same")
        local[:, :, band] = np.maximum(
            filtered[pad_y : pad_y + cube.shape[0], pad_x : pad_x + cube.shape[1]], 0.0
        )
    computed = oi.clone()
    computed.data["photons"] = local
    oi_calculate_illuminance(computed)
    field_mm = float(optics["raytrace"]["psf"]["field_height_mm"][field_index])
    selection = {
        "mode": "local_stored_field_psf",
        "stored_field_index": field_index,
        "ideal_image_height_mm": field_mm,
        "azimuth_deg_from_positive_x": field_azimuth_deg,
        "distorted_image_height_mm_by_wavelength": positions[field_index].tolist(),
        "wavelength_nm": waves.tolist(),
        "sample_spacing_um": float(oi.fields["sample_spacing_m"]) * 1e6,
        "boundary_model": "edge_extended_local_radiance_patch",
        "distortion_at_selected_field": "not_applied",
        "relative_illumination_at_selected_field": "unknown_unity_assumption",
        "sensor_angular_response_at_selected_field": "not_applied",
    }
    computed.metadata["local_field_psf"] = selection.copy()
    return computed, selection


def preview_lens_sensor_raw(
    lens_revision_id: str,
    sensor_identifier: str,
    *,
    sensor_run_id: str,
    root: str | Path,
    workspace: str | Path,
    scene_kind: str = "slanted_edge",
    sensor_size: tuple[int, int] = (16, 16),
    luminance_cd_m2: float | None = 1000.0,
    exposure_s: float = 0.004,
    noise_flag: int = 0,
    seed: int = 7,
    accept_approximate_lens_radiometry: bool = False,
    field_psf_index: int | None = None,
    field_azimuth_deg: float = 0.0,
    psf_compute_mode: str = "precomputed",
    geometric_psf_sampling: str = "stored_interpolated",
    scene_horizontal_fov_deg: float | None = None,
    scene_samples_row_col: tuple[int, int] | list[int] | None = None,
    radiance_scene: tuple[dict[str, Any], np.ndarray] | None = None,
    record_sampling_evidence: bool = False,
    include_mean_electron_values: bool = False,
    analytic_slanted_edge_offset_deg: float | None = None,
) -> dict[str, Any]:
    """Calculate a pinned central RAW or opt-in local-field PSF RAW preview.

    Only saved ``db_estimate_v1`` models are supported.  The physical estimate
    has a separate angular illumination contract and cannot silently inherit
    the saved run's F/4 pupil when a different Lens DB prescription is chosen.
    """
    if accept_approximate_lens_radiometry is not True:
        raise ValueError("Explicit acceptance of approximate lens radiometry is required.")
    if not isinstance(lens_revision_id, str) or not lens_revision_id.strip():
        raise ValueError("Choose an explicit Lens DB revision ID.")
    if not isinstance(sensor_identifier, str) or not sensor_identifier.strip():
        raise ValueError("Choose an explicit saved sensor identifier.")
    if not isinstance(sensor_run_id, str) or not sensor_run_id.strip():
        raise ValueError("Choose an explicit saved sensor run ID.")
    imported_radiance = scene_kind == "registered_object_radiance"
    if scene_kind not in {"uniform", "slanted_edge", "registered_object_radiance"}:
        raise ValueError("scene_kind must be uniform, slanted_edge or registered_object_radiance.")
    if analytic_slanted_edge_offset_deg is not None:
        if scene_kind != "slanted_edge" or scene_horizontal_fov_deg is None:
            raise ValueError("Analytic edge offset requires an explicit-FOV slanted_edge scene.")
        _number(analytic_slanted_edge_offset_deg, "analytic_slanted_edge_offset_deg", -1.0, 1.0)
    if imported_radiance:
        from .lens_sensor_radiance_scene import metadata_contract

        if (
            radiance_scene is None or not isinstance(radiance_scene, tuple)
            or len(radiance_scene) != 2 or luminance_cd_m2 is not None
            or scene_horizontal_fov_deg is not None or scene_samples_row_col is not None
        ):
            raise ValueError(
                "Registered object radiance needs its verified record and array, "
                "without luminance scaling or duplicate FOV/sampling controls."
            )
        radiance_record, radiance_array = radiance_scene
        metadata_contract({key: radiance_record[key] for key in {
            "schema", "name", "revision", "source_kind", "source_uri", "license",
            "method_and_version", "physical_instance", "parents", "wavelength_nm",
            "unit", "reference_plane", "sampling_convention", "horizontal_fov_deg",
            "object_distance_m", "included_effects",
        }})
        if (
            not isinstance(radiance_array, np.ndarray)
            or list(radiance_array.shape) != radiance_record.get("shape")
            or radiance_array.shape[2] != len(radiance_record["wavelength_nm"])
        ):
            raise ValueError("Registered radiance scene array differs from its record.")
    elif radiance_scene is not None:
        raise ValueError("A registered radiance scene requires its dedicated scene_kind.")
    if (
        not isinstance(sensor_size, (tuple, list))
        or len(sensor_size) != 2
        or any(type(item) is not int or not 8 <= item <= 32 for item in sensor_size)
    ):
        raise ValueError("sensor_size requires two integer dimensions from 8 to 32.")
    if type(noise_flag) is not int or noise_flag not in {0, 2}:
        raise ValueError("noise_flag must be 0 or 2.")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    if field_psf_index is not None and (type(field_psf_index) is not int or field_psf_index < 0):
        raise ValueError("field_psf_index must be a nonnegative stored PSF field index.")
    azimuth = _number(field_azimuth_deg, "field_azimuth_deg", 0.0, 360.0)
    if azimuth == 360.0:
        raise ValueError("field_azimuth_deg must be less than 360 degrees.")
    if field_psf_index is None and azimuth != 0.0:
        raise ValueError("field_azimuth_deg requires an explicit field_psf_index.")
    if psf_compute_mode not in {"precomputed", "bounded_streaming"}:
        raise ValueError("psf_compute_mode must be precomputed or bounded_streaming.")
    if geometric_psf_sampling not in {
        "stored_interpolated", "direct_same_ray_rehistogram",
    }:
        raise ValueError("Unsupported geometric PSF sampling method.")
    direct_rehistogram = geometric_psf_sampling == "direct_same_ray_rehistogram"
    if direct_rehistogram and (
        field_psf_index is not None or psf_compute_mode != "bounded_streaming"
        or scene_horizontal_fov_deg is None or scene_samples_row_col is None
        or imported_radiance
    ):
        raise ValueError(
            "Direct same-ray PSF re-histogram requires an explicit-FOV, "
            "explicit-grid central bounded analytic scene."
        )
    if field_psf_index is not None and psf_compute_mode != "precomputed":
        raise ValueError("Local field PSF already uses a separate bounded patch path.")
    luminance = (
        None if imported_radiance else
        _number(luminance_cd_m2, "luminance_cd_m2", 0.01, 10000.0)
    )
    exposure = _number(exposure_s, "exposure_s", 1e-6, 1.0)
    explicit_fov = (
        float(radiance_record["horizontal_fov_deg"]) if imported_radiance else
        None if scene_horizontal_fov_deg is None else
        _number(scene_horizontal_fov_deg, "scene_horizontal_fov_deg", 0.001, 30.0)
    )
    if explicit_fov is not None and (
        field_psf_index is not None or psf_compute_mode != "bounded_streaming"
    ):
        raise ValueError(
            "Explicit common input scene requires central bounded strict PSF execution."
        )
    size = tuple(sensor_size)
    scene_samples = _scene_samples(
        tuple(radiance_array.shape[:2]) if imported_radiance else scene_samples_row_col,
        size,
    )
    if scene_samples is not None and explicit_fov is None:
        raise ValueError("Explicit scene samples require an explicit common scene FOV.")
    db_root = Path(root).expanduser().resolve()
    lens = geometric_lens_preview(lens_revision_id, catalog_root=db_root, wavelength_nm=550.0)
    inspection = inspect_sensor_revision(sensor_identifier, root=db_root, run_id=sensor_run_id)
    if not inspection["executable"]:
        raise ValueError(f"Saved sensor run cannot execute: {inspection['reason']}")
    if inspection["native_response"]["kind"] != "db_estimate_v1":
        raise ValueError(
            "Lens × sensor RAW preview currently supports db_estimate_v1 only; "
            "physical/FDTD responses need a matched lens pupil and CRA contract."
        )
    resource_preflight = None
    if field_psf_index is None:
        if direct_rehistogram:
            resource_preflight = direct_rehistogram_resource_preflight(
                lens["model"], inspection["native_response"]["pixel_pitch_um"], size,
                scene_horizontal_fov_deg=explicit_fov,
                scene_samples_row_col=scene_samples,
            )
            require_direct_rehistogram_budget(resource_preflight)
        elif psf_compute_mode == "bounded_streaming":
            resource_preflight = bounded_psf_resource_preflight(
                lens["model"], inspection["native_response"]["pixel_pitch_um"], size,
                scene_horizontal_fov_deg=explicit_fov,
                scene_samples_row_col=scene_samples,
            )
            require_bounded_psf_budget(resource_preflight)
        else:
            resource_preflight = central_psf_resource_preflight(
                lens["model"], inspection["native_response"]["pixel_pitch_um"], size,
                scene_horizontal_fov_deg=explicit_fov,
                scene_samples_row_col=scene_samples,
            )
            require_central_psf_budget(resource_preflight)
    selected = inspection["revision"]
    configuration = sensor_simulation_db_config(
        selected["report_code"], root=db_root, run_id=selected["run_id"]
    )
    saved_scenario = configuration["scenario"]
    if saved_scenario.get("sensor_output_mode") != "digital_raw":
        raise ValueError("The saved sensor run must have a digital_raw contract.")
    if saved_scenario.get("sensor_illumination") is not None:
        raise ValueError("Saved angular illumination cannot be reused with a new lens.")

    # The catalog bridge resolves only the hash-pinned local source and PSF.
    revision, snapshot, lens_db, source_sha = _revision_source(lens_revision_id, db_root)
    simulation_id, psf_path, validation, _ = _asset(
        revision, snapshot, lens_db, db_root
    )
    optics = lens_patent_raytrace_optics(
        simulation_id,
        db_path=lens_db,
        psf_dir=psf_path.parent,
        allow_approximate_radiometry=True,
    )
    ray = optics["raytrace"]
    field_heights_mm = np.asarray(ray["psf"]["field_height_mm"], dtype=float)
    if field_psf_index is not None and field_psf_index >= field_heights_mm.size:
        raise ValueError("field_psf_index exceeds the stored lens PSF field samples.")
    if (
        ray.get("asset_sha256") != lens["identity"]["asset_sha256"]
        or ray.get("source_prescription_sha256") != validation["source_prescription_sha256"]
        or ray.get("psf_kind") != "geometric"
        or ray.get("diffraction_included") is not False
        or ray.get("radiometry_assumption_accepted") is not True
    ):
        raise ValueError("Loaded lens optics differ from the pinned geometric preview.")
    for table_name in ("geometry", "relative_illumination", "psf"):
        table_waves = np.asarray(ray[table_name]["wavelength_nm"], dtype=float)
        if not np.allclose(table_waves, WAVELENGTHS_NM, rtol=0, atol=1e-9):
            raise ValueError("Lens optical tables must contain exactly 450/550/650 nm.")
    if ray.get("radiometry_status") == "validated":
        raise ValueError("This approximation contract expects an unknown-radiometry lens.")
    transmission = optics.get("transmittance", {})
    relative = ray["relative_illumination"]
    if (
        transmission.get("status") != "unknown"
        or not np.array_equal(np.asarray(transmission.get("scale")), np.ones(3))
        or relative.get("status") != "unknown"
        or not np.array_equal(
            np.asarray(relative["function"]),
            np.ones_like(np.asarray(relative["function"])),
        )
    ):
        raise ValueError("Lens radiometry is not the declared unity-placeholder contract.")
    derived_psf_provenance = None
    expected_derived_optics_signature = None
    if direct_rehistogram:
        from .lens_geometric_psf_rehistogram import rehistogram_pinned_geometric_psf

        derived = rehistogram_pinned_geometric_psf(
            lens_revision_id,
            catalog_root=db_root,
            convolution_pitch_um=resource_preflight["scene_sample_spacing_um"],
            max_total_psf_bytes=DIRECT_PSF_REHISTOGRAM_BUDGET_BYTES,
        )
        optics = derived.optics
        derived_psf_provenance = derived.provenance
        if (
            optics.get("raytrace", {}).get("asset_sha256")
            != lens["identity"]["asset_sha256"]
            or optics["raytrace"]["psf"].get("derived_kernel_sha256")
            != derived_psf_provenance.get("derived_psf_signature_sha256")
            or derived_psf_provenance.get("source_psf_asset_sha256")
            != lens["identity"]["asset_sha256"]
        ):
            raise ValueError("Derived PSF lost its pinned source or derived identity.")
        expected_derived_optics_signature = _strict_raytrace_cache_signature(optics)
    pitch_m = _number(
        inspection["native_response"]["pixel_pitch_um"], "pixel_pitch_um", 0.1, 100.0
    ) * 1e-6
    focal_m = _number(optics["focal_length_m"], "focal_length_m", 1e-5, 1.0)
    fnumber = _number(optics["f_number"], "lens_f_number", 0.1, 100.0)
    horizontal_extent_m = 1.2 * size[1] * pitch_m
    horizontal_fov_deg = (
        explicit_fov if explicit_fov is not None else
        math.degrees(2.0 * math.atan(horizontal_extent_m / (2.0 * focal_m)))
    )
    if horizontal_fov_deg > float(ray["max_fov_deg"]):
        raise ValueError("Sensor crop exceeds the saved lens PSF field of view.")
    object_distance_m = float(ray["object_distance_m"])
    if not object_distance_m > 0:
        raise ValueError("Lens object distance is invalid.")
    if imported_radiance:
        declared_distance = radiance_record["object_distance_m"]
        if not (
            (declared_distance == "infinity" and math.isinf(object_distance_m))
            or (
                type(declared_distance) in (int, float)
                and math.isfinite(object_distance_m)
                and math.isclose(
                    float(declared_distance), object_distance_m,
                    rel_tol=1e-9, abs_tol=1e-9,
                )
            )
        ):
            raise ValueError(
                "Registered scene object_distance_m differs from the saved lens "
                "raytrace PSF conjugate; reproject or select a matching lens."
            )
    assets = offline_asset_store(workspace=workspace, source=db_root)
    source_assets = offline_asset_identity(workspace=workspace, source=db_root)
    with assets.bound_default(assets):
        if imported_radiance:
            from .lens_sensor_radiance_scene import scene_from_radiance, source_identity

            scene = scene_from_radiance(radiance_record, radiance_array, assets=assets)
            scene_geometry = {
                "scene_to_crop_scale": None,
                "native_crop_scale": "depends_on_executed_lens_and_sensor",
                "source_asset": source_identity(radiance_record),
            }
        else:
            scene, scene_geometry = _make_scene(
                "uniform" if analytic_slanted_edge_offset_deg is not None else scene_kind,
                size, horizontal_fov_deg, assets, source_size=scene_samples
            )
            if analytic_slanted_edge_offset_deg is not None:
                # Fix the bright-side physical radiance *before* rasterizing
                # the edge. Normalizing each edge raster to the same frame
                # mean would change the scene radiance as N or phase changes.
                scene_interpolate_w(scene, WAVELENGTHS_NM, asset_store=assets)
                scene_adjust_luminance(scene, luminance, asset_store=assets)
                rows, cols = np.asarray(scene.data["photons"]).shape[:2]
                row, col = np.indices((rows, cols), dtype=float)
                # Regenerate the same continuous angular half-plane at every
                # resolution; no interpolation of a prior raster is involved.
                step = 2 * math.tan(math.radians(horizontal_fov_deg) / 2) / cols
                offset = math.tan(math.radians(analytic_slanted_edge_offset_deg))
                edge = ((col + 0.5 - cols / 2) - (row + 0.5 - rows / 2) / 12) * step
                mask = np.where(edge > offset, 1.0, 0.02)
                scene_set(scene, "photons", np.asarray(scene.data["photons"]) * mask[..., None])
                scene_geometry = {
                    **scene_geometry,
                    "analytic_source": "continuous_tangent_plane_slanted_half_plane_v1",
                    "edge_offset_deg": analytic_slanted_edge_offset_deg,
                }
        if explicit_fov is not None:
            scene_geometry = {
                **scene_geometry,
                "scene_to_crop_scale": None,
                "native_crop_scale": "depends_on_executed_lens_and_sensor",
            }
            scene_rows, scene_cols = np.asarray(scene.data["photons"]).shape[:2]
            source_hfov_deg = float(scene.fields["fov_deg"])
            source_vfov_deg = float(scene.fields["vfov_deg"])
            scene_grid_contract = {
                "mode": "explicit_common_input_scene",
                "source_shape": [scene_rows, scene_cols],
                "horizontal_fov_deg": source_hfov_deg,
                "vertical_fov_deg": source_vfov_deg,
                "center_angular_step_deg_row_col": [
                    math.degrees(math.atan(
                        2.0 * math.tan(math.radians(source_vfov_deg) / 2.0) / scene_rows
                    )),
                    math.degrees(math.atan(
                        2.0 * math.tan(math.radians(source_hfov_deg) / 2.0) / scene_cols
                    )),
                ],
                "native_output_grid_alignment": "not_established",
            }
        if not imported_radiance:
            if analytic_slanted_edge_offset_deg is None:
                scene_interpolate_w(scene, WAVELENGTHS_NM, asset_store=assets)
                scene_adjust_luminance(scene, luminance, asset_store=assets)
            scene_set(scene, "distance", object_distance_m)
        else:
            if not np.array_equal(np.asarray(scene.fields["wave"]), WAVELENGTHS_NM):
                raise ValueError(
                    "Registered radiance scene did not reach the lens wavelength grid."
                )
            if not math.isclose(
                float(scene.fields["distance_m"]), object_distance_m,
                rel_tol=1e-9, abs_tol=1e-9,
            ) and not (
                math.isinf(float(scene.fields["distance_m"]))
                and math.isinf(object_distance_m)
            ):
                raise ValueError("Registered radiance scene conjugate changed during adaptation.")
        camera = camera_create(asset_store=assets)
        camera_set(camera, "optics", optics)
        if psf_compute_mode == "bounded_streaming":
            camera.fields["oi"].fields["psf_compute_mode"] = "bounded_streaming"
        scenario = {
            **saved_scenario,
            "name": "lens_sensor_geometric_raw_preview",
            "sensor": {
                **saved_scenario.get("sensor", {}),
                "size": list(size),
                "integration_time": exposure,
                "noise_flag": noise_flag,
            },
            "sensor_resize": False,
        }
        if field_psf_index is None:
            result = camerae2e_run_scenario(
                scenario,
                scene=scene,
                camera=camera,
                asset_store=assets,
                sensor_output_mode="digital_raw",
                include_arrays=False,
                seed=seed,
            )
            computed = result["camera"]
            field_selection = {
                "mode": "centered_scene_full_raytrace",
                "psf_compute_mode": psf_compute_mode,
                "stored_field_support_mm": field_heights_mm.tolist(),
                "note": "The small centered scene samples only the near-axis portion of the field.",
            }
        else:
            # Reuse FACA's sensor model/readout configurators without first
            # computing a full central-field optical image that is discarded.
            local_config = image_sensor_db_reconcile_scenario(dict(scenario))
            local_config["sensor_output_mode"] = "digital_raw"
            local_camera, _ = _apply_physics_and_sensor_overrides(
                camera, local_config, assets
            )
            local_camera.fields["sensor"] = _configure_scenario_readout(
                camera_get(local_camera, "sensor"), local_config, "digital_raw"
            )
            local_sensor = camera_get(local_camera, "sensor")
            image_sensor_db_reconcile_scenario(
                local_config,
                actual_physics={
                    "fdtd": local_sensor.fields.get("fdtd_sensor"),
                    "tcad": local_sensor.fields.get("tcad_sensor"),
                },
            )
            local_oi, field_selection = _local_field_oi(
                camera, scene, optics,
                field_index=field_psf_index,
                field_azimuth_deg=azimuth,
            )
            local_camera.fields["oi"] = local_oi
            computed = camera_compute(
                local_camera, "oi", asset_store=assets, seed=seed
            )
    sensor = camera_get(computed, "sensor")
    oi = camera_get(computed, "oi")
    binding = sensor.metadata.get("sensor_simulation_db")
    if (
        not isinstance(binding, dict)
        or binding.get("run_id") != selected["run_id"]
        or binding.get("report_code") != selected["report_code"]
        or binding.get("result_sha256") != selected["result_sha256"]
    ):
        raise ValueError("Executed sensor differs from the selected saved run.")
    computed_optics = oi.fields["optics"]
    if (
        computed_optics.get("model") != "raytrace"
        or computed_optics.get("raytrace", {}).get("asset_sha256")
        != lens["identity"]["asset_sha256"]
    ):
        raise ValueError("Executed optics differ from the selected Lens DB revision.")
    if psf_compute_mode == "bounded_streaming":
        streaming = oi.fields.get("psf_streaming")
        if (
            not isinstance(streaming, dict)
            or streaming.get("mode") != "strict_bounded_streaming"
            or streaming.get("complete_precomputed_stack") is not False
            or streaming.get("peak_cached_kernels", 5) > 4
        ):
            raise ValueError("Combined capture did not use bounded strict PSF execution.")
    if direct_rehistogram:
        actual_oi_pitch_um = float(oi.fields["sample_spacing_m"]) * 1e6
        if not math.isclose(
            actual_oi_pitch_um, resource_preflight["scene_sample_spacing_um"],
            rel_tol=1e-8, abs_tol=1e-9,
        ):
            raise ValueError("Direct-ray PSF pitch differs from executed optical pitch.")
        if (
            computed_optics["raytrace"].get("geometric_psf_rehistogram", {}).get(
                "derived_psf_signature_sha256"
            )
            != derived_psf_provenance["derived_psf_signature_sha256"]
        ):
            raise ValueError("Executed optics lost the derived PSF signature.")
        if (
            _strict_raytrace_cache_signature(computed_optics)
            != expected_derived_optics_signature
        ):
            raise ValueError("Derived optics table changed during RAW capture.")
        if (
            oi.fields["psf_streaming"].get("source_signature")
            != expected_derived_optics_signature
        ):
            raise ValueError("Strict solver did not execute the derived PSF table.")
    raw = np.asarray(sensor.data["raw_dn"])
    electrons = np.asarray(sensor.data["mean_electrons"], dtype=float)
    reference = np.asarray(sensor.data["reference_dn"], dtype=float)
    capture = sensor.metadata.get("sensor_capture", {})
    if (
        raw.dtype != np.dtype("int32")
        or raw.shape != size
        or electrons.shape != size
        or reference.shape != size
        or not np.all(np.isfinite(electrons))
        or not np.all(np.isfinite(reference))
    ):
        raise ValueError("Combined capture returned an unsupported RAW or electron array.")
    native_output_grid = None
    if explicit_fov is not None:
        image_distance_m = _number(oi.fields.get("image_distance_m"), "image_distance_m", 1e-5, 1.0)
        pitch_row_m = _number(sensor_get(sensor, "deltay"), "executed_pixel_pitch_y_m", 1e-8, 1e-3)
        pitch_col_m = _number(sensor_get(sensor, "deltax"), "executed_pixel_pitch_x_m", 1e-8, 1e-3)
        effective_focal_m = _number(
            computed_optics["raytrace"].get("effective_focal_length_m"),
            "executed_effective_focal_length_m", 1e-5, 1.0,
        )
        if not (
            math.isclose(pitch_row_m, pitch_m, rel_tol=1e-9, abs_tol=1e-15)
            and math.isclose(pitch_col_m, pitch_m, rel_tol=1e-9, abs_tol=1e-15)
            and math.isclose(image_distance_m, effective_focal_m, rel_tol=1e-9)
        ):
            raise ValueError("Executed sensor pitch or raytrace image distance changed.")
        # The digital-RAW regrid places its sample centers using pixel.size_m,
        # whereas sensor_get(deltax/y) includes pixel gaps.  This preview's
        # current pitch contract is only sound when the two are identical.
        pixel_sample_size_m = np.asarray(sensor.fields["pixel"]["size_m"], dtype=float)
        if (
            pixel_sample_size_m.shape != (2,)
            or not np.all(np.isfinite(pixel_sample_size_m))
            or not np.allclose(
                pixel_sample_size_m, [pitch_row_m, pitch_col_m], rtol=1e-9, atol=1e-15
            )
        ):
            raise ValueError(
                "Executed RAW sample-center spacing differs from the declared sensor pitch."
            )
        point_center_grid = strict_raytrace_pixel_center_angles(
            computed_optics["raytrace"],
            sensor_shape=size,
            sensor_sample_size_m_row_col=pixel_sample_size_m.tolist(),
            image_distance_m=image_distance_m,
            wavelength_nm=WAVELENGTHS_NM,
        )
        point_angles = np.asarray(
            point_center_grid["coordinates_deg_row_col_by_wavelength"], dtype=float
        )
        source_rows, source_cols = scene_grid_contract["source_shape"]
        source_center_support_deg_row_col = [
            math.degrees(math.atan(
                math.tan(math.radians(scene_grid_contract["vertical_fov_deg"]) / 2.0)
                * (1.0 - 1.0 / source_rows)
            )),
            math.degrees(math.atan(
                math.tan(math.radians(scene_grid_contract["horizontal_fov_deg"]) / 2.0)
                * (1.0 - 1.0 / source_cols)
            )),
        ]
        maximum_angles = np.max(np.abs(point_angles), axis=(1, 2)).tolist()
        if any(
            component > support + 1e-9
            for row_col in maximum_angles
            for component, support in zip(
                row_col, source_center_support_deg_row_col, strict=True
            )
        ):
            raise ValueError(
                "The executed distortion-aware pixel centers exceed the common input "
                "scene sample-center support. Widen the declared scene FOV."
            )
        point_center_grid["input_scene_support"] = {
            "status": "all_model_point_centers_within_source_samples",
            "max_abs_input_angle_deg_row_col_by_wavelength": maximum_angles,
            "source_sample_center_support_deg_row_col": source_center_support_deg_row_col,
        }
        native_output_grid = {
            "status": "paraxial_center_only_full_distortion_map_not_established",
            "status_note": (
                "The legacy scalar FOV and center step below are paraxial. A separate "
                "strict-raytrace distortion map now locates each model pixel center by "
                "wavelength; finite pixel integration, PSF-effective support and a "
                "shared cross-sensor angular output grid remain unestablished."
            ),
            "sensor_resize": False,
            "pixel_pitch_m_row_col": [pitch_row_m, pitch_col_m],
            "image_distance_m": image_distance_m,
            "center_angular_step_deg_row_col": [
                math.degrees(math.atan(pitch_row_m / image_distance_m)),
                math.degrees(math.atan(pitch_col_m / image_distance_m)),
            ],
            "horizontal_fov_deg": math.degrees(2.0 * math.atan(
                size[1] * pitch_col_m / (2.0 * image_distance_m)
            )),
            "vertical_fov_deg": math.degrees(2.0 * math.atan(
                size[0] * pitch_row_m / (2.0 * image_distance_m)
            )),
            "distortion_aware_point_center": point_center_grid,
        }
    if geometric_lens_preview(lens_revision_id, catalog_root=db_root, wavelength_nm=550.0)[
        "identity"
    ] != lens["identity"]:
        raise ValueError("Lens source changed during combined RAW capture.")
    if inspect_sensor_revision(
        sensor_identifier, root=db_root, run_id=sensor_run_id
    )["revision"] != selected:
        raise ValueError("Sensor source changed during combined RAW capture.")
    if offline_asset_identity(workspace=workspace, source=db_root) != source_assets:
        raise ValueError("Pinned upstream assets changed during combined RAW capture.")
    sampling_evidence = None
    if record_sampling_evidence or direct_rehistogram:
        from .lens_sensor_sampling_evidence import (
            executed_mosaic_integration_evidence,
            optical_psf_sampling_evidence,
        )

        actual_oi_pitch_um = float(oi.fields["sample_spacing_m"]) * 1e6
        expected_oi_pitch_um = (
            resource_preflight or {}
        ).get("scene_sample_spacing_um")
        if expected_oi_pitch_um is not None and not math.isclose(
            actual_oi_pitch_um, expected_oi_pitch_um, rel_tol=1e-8, abs_tol=1e-9
        ):
            raise ValueError("Executed optical convolution pitch differs from preflight.")
        sampling_evidence = {
            "source_shape_row_col": list(np.asarray(scene.data["photons"]).shape[:2]),
            "source_wavelength_nm": list(WAVELENGTHS_NM),
            "optical_psf": optical_psf_sampling_evidence(
                computed_optics,
                convolution_pitch_um=actual_oi_pitch_um,
                scene_shape_row_col=tuple(np.asarray(scene.data["photons"]).shape[:2]),
                wavelengths_nm=WAVELENGTHS_NM,
            ),
            "mosaic_integration": executed_mosaic_integration_evidence(sensor, oi),
            "source_kind": (
                "declared_raster" if imported_radiance else
                "analytic_continuous_half_plane" if analytic_slanted_edge_offset_deg is not None
                else "pyisetcam_generated_pattern"
            ),
            "product_mtf_validated": False,
        }
    return {
        "schema": SCHEMA,
        "status": "completed",
        "scope": (
            "approximate_geometric_central_field_raw_preview"
            if field_psf_index is None else "approximate_geometric_local_field_psf_raw_preview"
        ),
        "physical_validation": "not_established",
        "lens_identity": lens["identity"],
        "sensor_revision": selected,
        "upstream_assets": source_assets,
        "resource_preflight": resource_preflight,
        "scene": {
            "kind": "registered_object_radiance" if imported_radiance else f"{scene_kind}_d65",
            "luminance_cd_m2": luminance,
            **({
                "luminance_semantics": "bright_side_reference_before_edge_mask",
                "actual_mean_luminance_cd_m2": float(scene_get(
                    scene, "mean luminance", asset_store=assets
                )),
            } if analytic_slanted_edge_offset_deg is not None else {}),
            **({
                "actual_mean_luminance_cd_m2": float(scene_get(
                    scene, "mean luminance", asset_store=assets
                )),
                "source_asset": source_identity(radiance_record),
                "source_wavelength_nm": radiance_record["wavelength_nm"],
                "source_unit": radiance_record["unit"],
                "source_reference_plane": radiance_record["reference_plane"],
                "spectral_conversion": "linear_energy_density_then_energy_to_quanta_once",
            } if imported_radiance else {}),
            "wavelength_nm": list(WAVELENGTHS_NM),
            "horizontal_fov_deg": horizontal_fov_deg,
            "object_distance_m": "infinity" if math.isinf(object_distance_m) else object_distance_m,
            "generator": (
                "camera_platform.lens_sensor_radiance_scene.scene_from_radiance"
                if imported_radiance else "pyisetcam.sensor_comparison._make_scene"
            ),
            "geometry": scene_geometry,
            **({"angular_grid_contract": scene_grid_contract} if explicit_fov is not None else {}),
            **({"sampling_evidence": sampling_evidence} if sampling_evidence is not None else {}),
        },
        "lens_model": {
            "focal_length_m": focal_m,
            "f_number": fnumber,
            "psf_kind": "geometric",
            "absolute_radiometry": "unknown_unity_transmission_assumption",
            "relative_illumination": "unknown_unity_assumption",
            "diffraction_included": False,
            "psf_sampling_validation": "not_established",
            "psf_source_sampling": geometric_psf_sampling,
            **({"direct_same_ray_rehistogram": derived_psf_provenance}
               if derived_psf_provenance is not None else {}),
            "source_db_sha256": source_sha,
            "field_psf_selection": field_selection,
            "psf_execution": (
                oi.fields.get("psf_streaming")
                if psf_compute_mode == "bounded_streaming" else "complete_precomputed_stack"
            ),
        },
        "sensor_model": {
            "kind": "db_estimate_v1",
            "saved_snapshot_used": True,
            "pixel_pitch_um": pitch_m * 1e6,
            "source_scope_policy": inspection["native_response"]["source_scope_policy"],
            "readout_classification": inspection["readout"]["classification"],
            "effective_readout": capture["readout"],
        },
        "capture": {
            "exposure_s": exposure,
            "noise_flag": noise_flag,
            "seed": seed,
            "sensor_size": list(size),
            "saturation": capture.get("saturation", {}),
            **(
                {"native_output_angular_grid": native_output_grid}
                if explicit_fov is not None else {}
            ),
        },
        "raw": {
            "dtype": "int32",
            "unit": "DN",
            "shape": list(raw.shape),
            "values": raw.tolist(),
            "min_dn": int(raw.min()),
            "max_dn": int(raw.max()),
        },
        "mean_electrons": {
            "unit": "electron/pixel",
            "mean": float(electrons.mean()),
            "min": float(electrons.min()),
            "max": float(electrons.max()),
            **({"values": electrons.tolist()} if include_mean_electron_values else {}),
        },
        "reference_dn": {
            "unit": "continuous_DN",
            "mean": float(reference.mean()),
        },
        "limitation": (
            LOCAL_FIELD_LIMITATION if field_psf_index is not None else
            DIRECT_PSF_REHISTOGRAM_LIMITATION if direct_rehistogram else
            RADIANCE_SCENE_LIMITATION if imported_radiance else
            BOUNDED_LIMITATION if psf_compute_mode == "bounded_streaming" else LIMITATION
        ),
    }
