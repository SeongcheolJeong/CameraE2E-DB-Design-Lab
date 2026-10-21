"""Bounded, source-bound image-quality diagnostics for a saved 2 x 2 RAW matrix.

Every input is an already completed Lens DB x saved Sensor DB RAW job. This
module runs ISP on those exact frames; it never re-runs optics or capture.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from pyisetcam.dataset import _sha256_array
from pyisetcam.sensor_comparison import _edge_metrics

from . import isp_v2
from .registry import canonical

SCHEMA = "camerae2e_lens_sensor_quality_study_v1"
SCHEMA_V2 = "camerae2e_lens_sensor_quality_study_v2"
SLOTS = ("L1S1", "L1S2", "L2S1", "L2S2")
_PAIRS = (
    ("L1S1", "L2S1", "lens_only"),
    ("L1S2", "L2S2", "lens_only"),
    ("L1S1", "L1S2", "sensor_only"),
    ("L2S1", "L2S2", "sensor_only"),
)
_FOV_REL_TOL = 1e-6
_FOV_ABS_TOL_DEG = 1e-9
_POINT_GRID_STATUS = "model_distortion_aware_pixel_centers_only"
_POINT_GRID_MODEL = "strict_raytrace_radial_geometry_inverse_piecewise_linear"


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite numeric evidence.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite numeric evidence.")
    return result


def _executed_psf_source_support(
    report: Mapping[str, Any], scene: Mapping[str, Any],
    lens: Mapping[str, Any], native_grid: Mapping[str, Any] | None,
    pixel_pitch_um: float,
) -> dict[str, Any]:
    """Carry the bounded RAW run's PSF/scene sampling into a quality study.

    The run records one maximum nonzero kernel extent over every generated
    wavelength/field/azimuth kernel.  It does not retain local per-band kernel
    energies.  A failure of this conservative bound means that support is
    unqualified, not that every scored pixel lost that much energy.
    """
    unavailable = {"status": "not_recorded", "reason": (
        "The source RAW does not retain an executed bounded-PSF extent and "
        "matched source-scene sampling contract."
    )}
    resource = report.get("resource_preflight")
    execution = lens.get("psf_execution")
    angular = scene.get("angular_grid_contract")
    if not (
        isinstance(resource, Mapping) and isinstance(execution, Mapping)
        and isinstance(angular, Mapping) and isinstance(native_grid, Mapping)
        and resource.get("scope") == "central_bounded_strict_raytrace"
        and execution.get("mode") == "strict_bounded_streaming"
    ):
        return unavailable
    preview_resource = report.get("preview", {}).get("resource_preflight")
    if canonical(resource) != canonical(preview_resource):
        raise ValueError("Executed PSF resource differs from frozen RAW preflight.")
    scene_shape = angular.get("source_shape")
    if (
        not isinstance(scene_shape, list) or len(scene_shape) != 2
        or any(type(value) is not int or value < 2 for value in scene_shape)
        or scene_shape != [resource.get("scene_samples_row"), resource.get("scene_samples_col")]
        or execution.get("wavelength_nm") != scene.get("wavelength_nm")
        or execution.get("kernel_shape_row_col")
        != resource.get("resampled_kernel_samples_row_col")
    ):
        raise ValueError("Executed PSF wavelength/kernel axes differ from source scene.")
    full = execution["kernel_shape_row_col"]
    nonzero = execution.get("largest_exact_nonzero_centered_kernel_row_col")
    if (
        not isinstance(full, list) or len(full) != 2
        or any(type(value) is not int or value < 1 or value % 2 != 1 for value in full)
        or not isinstance(nonzero, list) or len(nonzero) != 2
        or any(
            type(value) is not int or value < 1 or value % 2 != 1
            or value > full[axis]
            for axis, value in enumerate(nonzero)
        )
    ):
        raise ValueError("Executed bounded PSF has no valid exact-nonzero extent.")
    spacing_um = _finite_number(
        resource.get("scene_sample_spacing_um"), "source OI sample spacing"
    )
    image_distance_m = _finite_number(
        native_grid.get("image_distance_m"), "source image distance"
    )
    horizontal_fov = _finite_number(
        angular.get("horizontal_fov_deg"), "source horizontal FOV"
    )
    if (
        spacing_um <= 0 or image_distance_m <= 0 or pixel_pitch_um <= 0
        or horizontal_fov <= 0
        or not math.isclose(
            spacing_um,
            2.0 * image_distance_m * math.tan(math.radians(horizontal_fov) / 2.0)
            * 1e6 / scene_shape[1],
            rel_tol=1e-6,
        )
    ):
        raise ValueError("Source OI sample spacing differs from executed FOV geometry.")
    return {
        "status": "global_exact_nonzero_bound_recorded",
        "basis": "executed_bounded_psf_run_metadata_and_frozen_scene_preflight",
        "scene_samples_row_col": scene_shape,
        "oi_sample_spacing_um": spacing_um,
        "executed_wavelength_nm": execution["wavelength_nm"],
        "resampled_kernel_samples_row_col": full,
        "largest_exact_nonzero_centered_kernel_row_col": nonzero,
        "per_wavelength_local_kernel_energy": "not_recorded",
        "sensor_pixel_pitch_um": pixel_pitch_um,
        "sensor_sampling_support": (
            "not_recorded; digital_raw uses the mosaic spatial-integration path"
        ),
    }


def _point_center_map(
    grid: Mapping[str, Any], size: list[int], wavelengths: Any,
) -> np.ndarray | None:
    """Check the saved raytrace point map before using it as a diagnostic.

    These coordinates are object-space ray geometry at pixel centers. They are
    not a pixel-integrated or PSF-weighted sampling grid.
    """
    point = grid.get("distortion_aware_point_center")
    if point is None:
        return None
    if (
        not isinstance(point, Mapping)
        or point.get("status") != _POINT_GRID_STATUS
        or point.get("model") != _POINT_GRID_MODEL
        or point.get("coordinate_convention")
        != "row_positive_down_col_positive_right_degrees"
        or point.get("sensor_shape") != size
        or point.get("cross_sensor_common_output_grid") != "not_established"
        or point.get("pixel_integration_and_psf_effective_grid") != "not_established"
        or point.get("wavelength_nm") != wavelengths
        or not isinstance(wavelengths, list)
        or not wavelengths
    ):
        raise ValueError("Saved distortion-aware point-center map has an invalid contract.")
    try:
        coordinates = np.asarray(point["coordinates_deg_row_col_by_wavelength"], dtype=float)
        minimum = np.asarray(
            point["min_adjacent_angular_step_deg_row_col_by_wavelength"], dtype=float
        )
        maximum = np.asarray(
            point["max_adjacent_angular_step_deg_row_col_by_wavelength"], dtype=float
        )
        sample_size = np.asarray(point["sensor_sample_size_m_row_col"], dtype=float)
        declared_pitch = np.asarray(grid["pixel_pitch_m_row_col"], dtype=float)
        source_support = point["input_scene_support"]
        supported_angles = np.asarray(
            source_support["source_sample_center_support_deg_row_col"], dtype=float
        )
        reported_max_angles = np.asarray(
            source_support["max_abs_input_angle_deg_row_col_by_wavelength"], dtype=float
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Saved distortion-aware point-center map is malformed.") from exc
    bands = len(wavelengths)
    if (
        coordinates.shape != (bands, size[0], size[1], 2)
        or minimum.shape != (bands, 2)
        or maximum.shape != (bands, 2)
        or sample_size.shape != (2,)
        or declared_pitch.shape != (2,)
        or supported_angles.shape != (2,)
        or reported_max_angles.shape != (bands, 2)
        or not np.isfinite(coordinates).all()
        or not np.isfinite(minimum).all()
        or not np.isfinite(maximum).all()
        or not np.isfinite(sample_size).all()
        or not np.isfinite(declared_pitch).all()
        or not np.isfinite(supported_angles).all()
        or not np.isfinite(reported_max_angles).all()
        or not np.all((minimum > 0) & (minimum <= maximum))
        or not np.all(sample_size > 0)
        or not np.all(supported_angles > 0)
        or not np.allclose(sample_size, declared_pitch, rtol=1e-9, atol=1e-15)
        or source_support.get("status") != "all_model_point_centers_within_source_samples"
        or not math.isclose(
            _finite_number(point.get("image_distance_m"), "point-map image distance"),
            _finite_number(grid.get("image_distance_m"), "grid image distance"),
            rel_tol=1e-9,
        )
    ):
        raise ValueError("Saved distortion-aware point-center map has invalid dimensions.")
    observed_max_angles = np.max(np.abs(coordinates), axis=(1, 2))
    if not (
        np.allclose(reported_max_angles, observed_max_angles, rtol=1e-9, atol=1e-12)
        and np.all(reported_max_angles <= supported_angles[None, :] + 1e-9)
    ):
        raise ValueError("Saved point-center angles exceed the declared input scene support.")
    # A radial model should preserve the signs and ordering of both detector axes.
    row_steps = np.diff(coordinates[..., 0], axis=1)
    column_steps = np.diff(coordinates[..., 1], axis=2)
    if not (np.all(row_steps > 0) and np.all(column_steps > 0)):
        raise ValueError("Saved distortion-aware point-center coordinates are not monotone.")
    observed_minimum = np.stack(
        (row_steps.min(axis=(1, 2)), column_steps.min(axis=(1, 2))), axis=1
    )
    observed_maximum = np.stack(
        (row_steps.max(axis=(1, 2)), column_steps.max(axis=(1, 2))), axis=1
    )
    if not (
        np.allclose(minimum, observed_minimum, rtol=1e-9, atol=1e-12)
        and np.allclose(maximum, observed_maximum, rtol=1e-9, atol=1e-12)
    ):
        raise ValueError("Saved distortion-aware point-center step bounds differ from coordinates.")
    return coordinates


def _condition(report: Mapping[str, Any], checked: Mapping[str, Any]) -> dict[str, Any]:
    preview = report["preview"]
    scene, capture, lens = preview["scene"], preview["capture"], preview["lens_model"]
    spec = report["specification"]
    if (
        scene.get("kind") != "slanted_edge_d65"
        or preview.get("scope") != "approximate_geometric_central_field_raw_preview"
        or lens.get("absolute_radiometry") != "unknown_unity_transmission_assumption"
        or lens.get("relative_illumination") != "unknown_unity_assumption"
        or lens.get("psf_sampling_validation") != "not_established"
    ):
        raise ValueError("Quality study requires one central approximate slanted-edge RAW.")
    field = lens.get("field_psf_selection") or {}
    if field.get("mode") != "centered_scene_full_raytrace":
        raise ValueError("Quality study requires a central full-raytrace field selection.")
    fov = _finite_number(scene.get("horizontal_fov_deg"), "horizontal_fov_deg")
    if fov <= 0:
        raise ValueError("horizontal_fov_deg must be positive.")
    size = capture.get("sensor_size")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(type(v) is not int or not 8 <= v <= 32 for v in size)
    ):
        raise ValueError("The source must retain an 8..32 pixel sensor crop.")
    if (
        spec.get("scene_kind") != "slanted_edge"
        or spec.get("sensor_size") != size
        or spec.get("luminance_cd_m2") != scene.get("luminance_cd_m2")
        or spec.get("exposure_s") != capture.get("exposure_s")
        or spec.get("noise_flag") != capture.get("noise_flag")
        or spec.get("seed") != capture.get("seed")
        or spec.get("lens_revision_id")
        != report["source_identities"]["lens"].get("revision_id")
        or (spec.get("sensor_revision") or {}).get("run_id")
        != report["source_identities"]["sensor"].get("run_id")
    ):
        raise ValueError(
            "Source scene, capture or DB selection differs from its frozen specification."
        )
    angular_scene = scene.get("angular_grid_contract")
    native_output = capture.get("native_output_angular_grid")
    if report["schema"] in {
        "camerae2e_lens_sensor_raw_preview_study_v4",
        "camerae2e_lens_sensor_raw_preview_study_v5",
    }:
        if (
            spec.get("scene_horizontal_fov_deg") != fov
            or not isinstance(angular_scene, dict)
            or angular_scene.get("mode") != "explicit_common_input_scene"
            or angular_scene.get("horizontal_fov_deg") != fov
            or angular_scene.get("native_output_grid_alignment") != "not_established"
            or angular_scene.get("source_shape")
            != (
                spec.get("scene_samples_row_col")
                if report["schema"] == "camerae2e_lens_sensor_raw_preview_study_v5"
                else [max(32, 2 * size[0]), max(32, 2 * size[1])]
            )
            or not isinstance(angular_scene.get("center_angular_step_deg_row_col"), list)
            or len(angular_scene["center_angular_step_deg_row_col"]) != 2
            or any(
                _finite_number(value, "source angular center step") <= 0
                for value in angular_scene["center_angular_step_deg_row_col"]
            )
            or not isinstance(native_output, dict)
            or native_output.get("sensor_resize") is not False
            or native_output.get("status")
            != "paraxial_center_only_full_distortion_map_not_established"
            or not isinstance(native_output.get("center_angular_step_deg_row_col"), list)
            or len(native_output["center_angular_step_deg_row_col"]) != 2
            or any(
                _finite_number(value, "native angular center step") <= 0
                for value in native_output["center_angular_step_deg_row_col"]
            )
        ):
            raise ValueError("Common input scene or native output grid evidence is invalid.")
        _point_center_map(native_output, size, scene.get("wavelength_nm"))
    pixel_pitch_um = _finite_number(
        preview["sensor_model"].get("pixel_pitch_um"), "pixel pitch"
    )
    psf_source_support = _executed_psf_source_support(
        report, scene, lens, native_output, pixel_pitch_um,
    )
    condition = {
        "source_schema": report["schema"],
        "scene_kind": scene["kind"],
        "scene_generator": scene.get("generator"),
        "scene_geometry": scene.get("geometry"),
        "scene_wavelength_nm": scene.get("wavelength_nm"),
        "luminance_cd_m2": _finite_number(scene.get("luminance_cd_m2"), "luminance"),
        "object_distance_m": scene.get("object_distance_m"),
        "horizontal_fov_deg": fov,
        "angular_scene_grid_contract": angular_scene,
        "native_output_angular_grid": native_output,
        "sensor_size": size,
        "exposure_s": _finite_number(capture.get("exposure_s"), "exposure"),
        "noise_flag": capture.get("noise_flag"),
        "seed": capture.get("seed"),
        "lens_scope": preview["scope"],
        "field_psf_mode": field["mode"],
        "psf_compute_mode": field.get("psf_compute_mode", "precomputed"),
        "native_cfa_contract": checked["native_cfa_contract"],
        "upstream_assets": report.get("upstream_assets"),
        "lens_focal_length_m": _finite_number(lens.get("focal_length_m"), "focal length"),
        "lens_f_number": _finite_number(lens.get("f_number"), "f-number"),
        "sensor_pixel_pitch_um": pixel_pitch_um,
    }
    if psf_source_support["status"] != "not_recorded":
        condition["model_psf_source_support"] = psf_source_support
    return condition


def _require_crossed_identities(cases: Mapping[str, Mapping[str, Any]]) -> None:
    if set(cases) != set(SLOTS):
        raise ValueError("Declare exactly L1S1, L1S2, L2S1 and L2S2.")
    lens = {key: canonical(cases[key]["binding"]["lens_identity"]) for key in SLOTS}
    sensor = {key: canonical(cases[key]["binding"]["saved_sensor_revision"]) for key in SLOTS}
    if not (
        lens["L1S1"] == lens["L1S2"]
        and lens["L2S1"] == lens["L2S2"]
        and lens["L1S1"] != lens["L2S1"]
        and sensor["L1S1"] == sensor["L2S1"]
        and sensor["L1S2"] == sensor["L2S2"]
        and sensor["L1S1"] != sensor["L1S2"]
    ):
        raise ValueError("Source jobs must form two distinct Lens revisions x two Sensor runs.")


def _edge_diagnostic(rgb: np.ndarray, valid: np.ndarray, saturated: bool) -> dict[str, Any]:
    if saturated:
        return {"available": False, "reason": "Source capture is clipped."}
    rows, cols = rgb.shape[:2]
    ry, rx = max(1, rows // 8), max(1, cols // 8)
    if not bool(np.all(valid[ry:-ry, rx:-rx])):
        return {"available": False, "reason": "The centered ISP edge ROI has invalid pixels."}
    edge = _edge_metrics(rgb)
    if not edge["available"]:
        return {"available": False, "reason": edge["reason"]}
    return {
        "available": True,
        "value": edge["mtf50_cycles_per_output_pixel"],
        "unit": "cycles/output_pixel",
        "method": edge["method"],
        "input": "single_retained_frame_linear_post_sharpen_rgb",
        "interpretation": "simulation screening diagnostic, not measured system MTF",
    }


def _comparison(cases: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    reference = cases["L1S1"]["conditions"]
    common_keys = (
        "source_schema", "scene_kind", "scene_generator", "scene_geometry",
        "scene_wavelength_nm", "luminance_cd_m2", "object_distance_m",
        "angular_scene_grid_contract",
        "sensor_size", "exposure_s", "noise_flag", "seed", "lens_scope",
        "field_psf_mode", "psf_compute_mode", "native_cfa_contract", "upstream_assets",
    )
    mismatched = [
        key for key in common_keys
        if any(canonical(cases[slot]["conditions"][key]) != canonical(reference[key])
               for slot in SLOTS[1:])
    ]
    fovs = {slot: cases[slot]["conditions"]["horizontal_fov_deg"] for slot in SLOTS}
    native_grids = {
        slot: cases[slot]["conditions"]["native_output_angular_grid"] for slot in SLOTS
    }
    point_maps = {
        slot: (
            _point_center_map(
                native_grids[slot], cases[slot]["conditions"]["sensor_size"],
                cases[slot]["conditions"]["scene_wavelength_nm"],
            ) if isinstance(native_grids[slot], Mapping) else None
        )
        for slot in SLOTS
    }
    point_map_available = {slot: point_maps[slot] is not None for slot in SLOTS}
    common_point_roi: dict[str, Any] = {"status": "unavailable"}
    if all(point_map_available.values()):
        # 550 nm maps expose geometric overlap in object-angle space only.
        # No processed image is resampled or scored on this ROI here.
        reference_waves = reference["scene_wavelength_nm"]
        if 550.0 in reference_waves:
            band = reference_waves.index(550.0)
            extents = {
                slot: (
                    np.min(point_maps[slot][band], axis=(0, 1)),
                    np.max(point_maps[slot][band], axis=(0, 1)),
                )
                for slot in SLOTS
            }
            low = np.max(np.stack([extents[slot][0] for slot in SLOTS]), axis=0)
            high = np.min(np.stack([extents[slot][1] for slot in SLOTS]), axis=0)
            nonempty = bool(np.all(high > low))
            counts = {}
            for slot in SLOTS:
                coordinates = point_maps[slot][band]
                counts[slot] = int(np.count_nonzero(
                    (coordinates[..., 0] >= low[0]) & (coordinates[..., 0] <= high[0])
                    & (coordinates[..., 1] >= low[1]) & (coordinates[..., 1] <= high[1])
                )) if nonempty else 0
            common_point_roi = {
                "status": "point_center_geometry_overlap_only",
                "wavelength_nm": 550.0,
                "nonempty": nonempty,
                "row_angle_deg": [float(low[0]), float(high[0])] if nonempty else None,
                "column_angle_deg": [float(low[1]), float(high[1])] if nonempty else None,
                "native_pixel_centers_in_overlap_by_slot": counts,
                "effective_psf_pixel_grid_and_comparable_mtf": "not_established",
            }
    source_coarser_than_native: dict[str, bool | None] = {}
    source_step_over_min_distorted_step: dict[str, float | None] = {}
    for slot in SLOTS:
        conditions = cases[slot]["conditions"]
        scene_grid = conditions["angular_scene_grid_contract"]
        output_grid = native_grids[slot]
        if not isinstance(scene_grid, dict) or not isinstance(output_grid, dict):
            source_coarser_than_native[slot] = None
            source_step_over_min_distorted_step[slot] = None
            continue
        source_steps = scene_grid.get("center_angular_step_deg_row_col")
        output_steps = output_grid.get("center_angular_step_deg_row_col")
        if (
            not isinstance(source_steps, list) or len(source_steps) != 2
            or not isinstance(output_steps, list) or len(output_steps) != 2
        ):
            source_coarser_than_native[slot] = None
            source_step_over_min_distorted_step[slot] = None
            continue
        if point_map_available[slot]:
            minimum = np.asarray(
                output_grid["distortion_aware_point_center"]["min_adjacent_angular_step_deg_row_col_by_wavelength"],
                dtype=float,
            )
            source_step_over_min_distorted_step[slot] = float(np.max(
                np.asarray(source_steps, dtype=float)[None, :] / minimum
            ))
            source_coarser_than_native[slot] = (
                source_step_over_min_distorted_step[slot] > 1.0 + _FOV_REL_TOL
            )
        else:
            source_step_over_min_distorted_step[slot] = None
            source_coarser_than_native[slot] = any(
                source_step > native_step * (1.0 + _FOV_REL_TOL)
                for source_step, native_step in zip(source_steps, output_steps, strict=True)
            )
    # Point-center ray coordinates are not the effective angular footprint of
    # finite pixels and a field-dependent, wavelength-dependent PSF. The four
    # original RAWs still have different grids and are not jointly resampled.
    matched_native_output_grid = False
    matched_fov = all(
        math.isclose(value, fovs["L1S1"], rel_tol=_FOV_REL_TOL,
                     abs_tol=_FOV_ABS_TOL_DEG)
        for value in fovs.values()
    )
    available = all(cases[slot]["edge_mtf50"]["available"] for slot in SLOTS)
    reasons = [f"Mismatched source condition: {key}." for key in mismatched]
    if not matched_fov:
        reasons.append(
            "The executed horizontal fields of view differ; common angular sampling "
            "and ROI comparison are not established for cycles/output-pixel deltas."
        )
    if not matched_native_output_grid:
        reasons.append(
            "Distortion-aware point-center maps describe geometry only; common "
            "PSF/pixel-integrated native RAW output footprints remain unestablished. "
            "This gate blocks native cycles/output-pixel pairwise deltas; separately "
            "evaluated angular-grid diagnostics have their own limits."
            if all(point_map_available.values()) else
            "A common native RAW output angular grid is not established: at least one "
            "capture lacks a distortion-aware point-center map."
        )
    if any(value is True for value in source_coarser_than_native.values()):
        reasons.append(
            "The common input scene's center angular sample spacing is coarser than "
            "at least one native RAW pixel-center step (distortion-aware where available, "
            "otherwise paraxial); source sampling may limit the edge metric."
        )
    if not available:
        reasons.append("At least one source has no valid single-frame edge MTF50.")
    eligible = not reasons
    pairs = []
    for left, right, axis in _PAIRS:
        item: dict[str, Any] = {"left": left, "right": right, "changed_axis": axis,
                                "edge_mtf50_delta_available": eligible}
        if eligible:
            item["right_minus_left_cycles_per_output_pixel"] = (
                cases[right]["edge_mtf50"]["value"] - cases[left]["edge_mtf50"]["value"]
            )
        else:
            item["reason"] = (
                "Native cycles/output-pixel pairwise gate did not pass; separately "
                "evaluated angular-grid diagnostics have their own limits."
            )
        pairs.append(item)
    return {
        "matched_source_controls": not mismatched,
        "mismatched_control_fields": mismatched,
        "matched_horizontal_fov": matched_fov,
        "matched_native_output_angular_grid": matched_native_output_grid,
        "native_output_angular_grid_by_slot": native_grids,
        "distortion_aware_point_center_map_by_slot": point_map_available,
        "common_550nm_point_center_roi": common_point_roi,
        "source_step_over_min_distorted_step_by_slot": source_step_over_min_distorted_step,
        "input_scene_coarser_than_native_output_by_slot": source_coarser_than_native,
        "horizontal_fov_deg_by_slot": fovs,
        "horizontal_fov_tolerance": {"relative": _FOV_REL_TOL,
                                     "absolute_deg": _FOV_ABS_TOL_DEG},
        "edge_mtf50_diagnostic_delta_allowed": eligible,
        "temporal_snr_delta_allowed": False,
        "product_ranking_allowed": False,
        "pairwise_diagnostic_deltas": pairs,
        "reasons": reasons,
        "scope": (
            "matched-input simulated ISP edge sensitivity only; no product ranking"
            if eligible else
            "native cycles/output-pixel pairwise gate only; angular diagnostics "
            "are evaluated separately"
        ),
    }


def _matched_angular_edge(
    cases: Mapping[str, Mapping[str, Any]],
    images: Mapping[str, np.ndarray],
    valid_masks: Mapping[str, np.ndarray],
    comparison: Mapping[str, Any],
) -> dict[str, Any]:
    """Estimate central edge response inside a shared object-angle overlap.

    Each retained frame already contains the executed Lens PSF convolution,
    sensor regrid and ISP.  ISO 12233 operates on *native* output samples; the
    strict raytrace inverse converts its frequency axis to object angle. Native
    rectangles and their ISO edge crops have different angular extents. This
    deliberately does not interpolate four RAW arrays onto an invented common
    pixel grid.  Sampling and chromatic geometry must pass explicit bounds.
    """
    result: dict[str, Any] = {
        "available": False,
        "method": "native_iso12233_edge_with_strict_raytrace_object_angle_scale_v1",
        "unit": "cycles/degree_object_angle",
        "scope": "central_simulated_broadband_edge_sensitivity_only",
        "angular_roi_area_matched": False,
        "angular_roi_contract": "shared_overlap_native_rectangles_different_extents",
        "product_ranking_allowed": False,
        "per_slot": {},
        "pairs": [],
        "reasons": [],
        "requirements": {
            "minimum_native_roi_shape": [16, 16],
            "maximum_source_step_over_native_step": 0.5,
            "maximum_local_angular_step_variation_fraction": 0.02,
            "maximum_chromatic_center_displacement_native_steps": 0.1,
        },
    }
    reasons: list[str] = result["reasons"]
    if not comparison["matched_source_controls"] or not comparison["matched_horizontal_fov"]:
        reasons.append("Four captures do not share the frozen input scene and controls.")
    if any(cases[slot]["conditions"]["source_schema"] !=
           "camerae2e_lens_sensor_raw_preview_study_v5" for slot in SLOTS):
        reasons.append("A dense common-input scene v5 is required.")
    if any(cases[slot]["conditions"]["noise_flag"] != 0 for slot in SLOTS):
        reasons.append("One noisy frame per camera cannot establish the edge response.")
    if any(not comparison["distortion_aware_point_center_map_by_slot"][slot]
           for slot in SLOTS):
        reasons.append("Every capture needs its executed strict-raytrace angular map.")
    source_ratio = comparison["source_step_over_min_distorted_step_by_slot"]
    if any(source_ratio[slot] is None or source_ratio[slot] > 0.5 for slot in SLOTS):
        reasons.append(
            "The common input scene is not at least twice as dense as every native sample step."
        )
    overlap = comparison["common_550nm_point_center_roi"]
    if overlap.get("status") != "point_center_geometry_overlap_only" or not overlap.get("nonempty"):
        reasons.append("No shared central object-angle ROI is established.")
    if reasons:
        return result

    low = np.asarray((overlap["row_angle_deg"][0], overlap["column_angle_deg"][0]))
    high = np.asarray((overlap["row_angle_deg"][1], overlap["column_angle_deg"][1]))
    native_nyquist: dict[str, float] = {}
    for slot in SLOTS:
        conditions = cases[slot]["conditions"]
        point = conditions["native_output_angular_grid"]["distortion_aware_point_center"]
        waves = conditions["scene_wavelength_nm"]
        band = waves.index(550.0)
        angular = np.asarray(point["coordinates_deg_row_col_by_wavelength"], dtype=float)
        centers = angular[band]
        inside = np.all((centers >= low - 1e-12) & (centers <= high + 1e-12), axis=-1)
        row_indices = np.flatnonzero(inside[:, inside.shape[1] // 2])
        col_indices = np.flatnonzero(inside[inside.shape[0] // 2, :])
        # A radial distortion can make the rectangular all-inside crop smaller
        # than the point count. This is intentional; the metric uses one exact
        # rectangular ROI without edge extrapolation.
        if row_indices.size < 16 or col_indices.size < 16:
            result["per_slot"][slot] = {
                "available": False,
                "reason": "Shared angular ROI has fewer than 16 native samples per axis.",
                "all_inside_native_rows_cols": [int(row_indices.size), int(col_indices.size)],
            }
            continue
        r0, r1 = int(row_indices[0]), int(row_indices[-1]) + 1
        c0, c1 = int(col_indices[0]), int(col_indices[-1]) + 1
        while not np.all(inside[r0:r1, c0:c1]) and r1 - r0 >= 16 and c1 - c0 >= 16:
            # Trim the axis with more excess samples, symmetrically about the
            # optical center; never fill an uncovered corner by interpolation.
            if r1 - r0 >= c1 - c0:
                r0 += 1
                r1 -= 1
            else:
                c0 += 1
                c1 -= 1
        if r1 - r0 < 16 or c1 - c0 < 16 or not np.all(inside[r0:r1, c0:c1]):
            result["per_slot"][slot] = {
                "available": False,
                "reason": (
                    "The common angular ROI has fewer than 16 fully covered "
                    "native samples per axis."
                ),
            }
            continue
        crop = centers[r0:r1, c0:c1]
        row_step = np.diff(crop[..., 0], axis=0)
        col_step = np.diff(crop[..., 1], axis=1)
        row_mean, col_mean = float(np.mean(row_step)), float(np.mean(col_step))
        variation = max(
            float(np.max(np.abs(row_step / row_mean - 1.0))),
            float(np.max(np.abs(col_step / col_mean - 1.0))),
        )
        chromatic = float(np.max(np.linalg.norm(
            angular[:, r0:r1, c0:c1] - crop[None, ...], axis=-1
        )) / min(row_mean, col_mean))
        if variation > 0.02 or chromatic > 0.1:
            result["per_slot"][slot] = {
                "available": False,
                "reason": (
                    "Local distortion or chromatic center displacement exceeds "
                    "the bounded angular-scale contract."
                ),
                "local_angular_step_variation_fraction": variation,
                "chromatic_center_displacement_native_steps": chromatic,
            }
            continue
        rgb = np.asarray(images[slot][r0:r1, c0:c1], dtype=float)
        valid = np.asarray(valid_masks[slot][r0:r1, c0:c1], dtype=bool)
        ry, rx = max(1, rgb.shape[0] // 8), max(1, rgb.shape[1] // 8)
        if not bool(np.all(valid[ry:-ry, rx:-rx])) or any(
            cases[slot]["saturation_fractions"][name] > 0.001
            for name in ("adc_white_fraction", "adc_zero_fraction", "well_fraction")
        ):
            result["per_slot"][slot] = {
                "available": False,
                "reason": "The shared edge ROI has invalid ISP pixels or clipped RAW.",
            }
            continue
        edge = _edge_metrics(rgb)
        if not edge["available"]:
            result["per_slot"][slot] = {"available": False, "reason": edge["reason"]}
            continue
        # The authored edge is x = y/12 in source sample coordinates. The
        # explicit common scene maps this to tan(theta_col)=tan(theta_row)/12.
        # Convert one ISO output-normal pixel into its object-angle distance.
        pixel_edge_slope = row_mean / (12.0 * col_mean)
        normal_step = col_mean * math.hypot(1.0, pixel_edge_slope) / math.hypot(1.0, 1.0 / 12.0)
        frequency = float(edge["mtf50_cycles_per_output_pixel"] / normal_step)
        native_nyquist[slot] = 0.5 / normal_step
        result["per_slot"][slot] = {
            "available": True,
            "mtf50_cycles_per_degree_object_angle": frequency,
            "native_nyquist_cycles_per_degree_object_angle": native_nyquist[slot],
            "native_roi_row_col_exclusive": [[r0, r1], [c0, c1]],
            "native_roi_shape": [r1 - r0, c1 - c0],
            "mean_angular_step_deg_row_col": [row_mean, col_mean],
            "edge_normal_angular_step_deg": normal_step,
            "local_angular_step_variation_fraction": variation,
            "chromatic_center_displacement_native_steps": chromatic,
            "iso_method": edge["method"],
        }
    if not all(result["per_slot"].get(slot, {}).get("available") for slot in SLOTS):
        reasons.append("At least one capture lacks a supported common-angle edge estimate.")
        return result
    common_nyquist = min(native_nyquist.values())
    result["common_nyquist_cycles_per_degree_object_angle"] = common_nyquist
    if any(
        result["per_slot"][slot]["mtf50_cycles_per_degree_object_angle"] >= common_nyquist
        for slot in SLOTS
    ):
        reasons.append("At least one MTF50 lies above the shared native angular Nyquist limit.")
        return result
    result["available"] = True
    result["pairs"] = [
        {
            "left": left, "right": right, "changed_axis": axis,
            "right_minus_left_cycles_per_degree_object_angle": (
                result["per_slot"][right]["mtf50_cycles_per_degree_object_angle"]
                - result["per_slot"][left]["mtf50_cycles_per_degree_object_angle"]
            ),
        }
        for left, right, axis in _PAIRS
    ]
    return result


def evaluate_matrix(
    inputs: Mapping[str, Mapping[str, Any]], *, profile: dict[str, Any],
    schema: str = SCHEMA,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Process four verified saved RAW frames without deriving temporal SNR."""
    if schema not in {SCHEMA, SCHEMA_V2}:
        raise ValueError("Unsupported Lens x Sensor quality-study schema.")
    if set(inputs) != set(SLOTS):
        raise ValueError("Declare exactly L1S1, L1S2, L2S1 and L2S2.")
    selected = isp_v2.validate_profile(profile)
    cases: dict[str, dict[str, Any]] = {}
    arrays: dict[str, np.ndarray] = {}
    images: dict[str, np.ndarray] = {}
    valid_masks: dict[str, np.ndarray] = {}
    for slot in SLOTS:
        source = inputs[slot]
        report, raw, checked, binding = (
            source[key] for key in ("report", "raw", "checked", "binding")
        )
        if (
            not isinstance(raw, np.ndarray)
            or raw.dtype != np.dtype("int32")
            or raw.ndim != 2
            or checked["raw_content_sha256"] != _sha256_array(raw)
            or binding["source_raw_content_sha256"] != checked["raw_content_sha256"]
            or canonical(report["source_identities"]["lens"])
            != canonical(binding["lens_identity"])
            or canonical(report["source_identities"]["sensor"])
            != canonical(binding["saved_sensor_revision"])
            or canonical(checked["profile"]) != canonical(selected)
        ):
            raise ValueError("One matrix input lost its saved RAW, source, or ISP binding.")
        conditions = _condition(report, checked)
        if list(raw.shape) != conditions["sensor_size"]:
            raise ValueError("Source RAW shape differs from executed sensor crop.")
        result = isp_v2.process_raw(
            raw, checked["raw_format"], selected, geometry=checked["geometry"]
        )
        rgb = np.asarray(result["arrays"]["sharpened_rgb_frames"][0], dtype=float)
        valid = np.asarray(result["arrays"]["sharpen_valid_mask_frames"][0], dtype=bool)
        if rgb.shape != (*raw.shape, 3) or valid.shape != raw.shape:
            raise ValueError("ISP output shape differs from retained RAW crop.")
        saturation = report["preview"]["capture"].get("saturation") or {}
        fractions = {
            key: _finite_number(saturation.get(key), key)
            for key in ("adc_white_fraction", "adc_zero_fraction", "well_fraction")
        }
        if any(not 0 <= value <= 1 for value in fractions.values()):
            raise ValueError("Source saturation fractions are invalid.")
        edge = _edge_diagnostic(rgb, valid, any(value > 0.001 for value in fractions.values()))
        arrays[f"{slot}_raw_dn"] = np.ascontiguousarray(raw)
        arrays[f"{slot}_linear_isp_rgb"] = rgb.astype(np.float32)
        arrays[f"{slot}_valid_rgb_mask"] = valid
        # Recompute the optional angular estimate from the same float32 array
        # that is sealed in the NPZ, so readback can verify it byte-for-byte.
        images[slot] = arrays[f"{slot}_linear_isp_rgb"]
        valid_masks[slot] = valid
        cases[slot] = {
            "source_job_id": binding["source_lens_sensor_job_id"],
            "source_binding": binding,
            "conditions": conditions,
            "raw_content_sha256": checked["raw_content_sha256"],
            "native_raw_mean_dn": float(np.mean(raw)),
            "saturation_fractions": fractions,
            "valid_rgb_pixel_fraction": float(np.mean(valid)),
            "edge_mtf50": edge,
            "temporal_snr_db": {
                "available": False,
                "reason": (
                    "Only one retained frame exists for this Lens x Sensor source; "
                    "independent seeds are required for temporal variance."
                ),
            },
            "metric_scope": "single-frame simulated ISP diagnostic",
            "isp_implementation": result["summary"]["implementation"]["id"],
            "isp_crop_full_frame_halo_native_pixels": result["summary"][
                "crop_full_frame_comparison_halo_native_pixels"
            ],
            "binding": binding,
        }
    _require_crossed_identities(cases)
    comparison = _comparison(cases)
    angular_edge = _matched_angular_edge(cases, images, valid_masks, comparison)
    comparison["matched_object_angle_edge_mtf50"] = angular_edge
    if schema == SCHEMA_V2:
        from .lens_sensor_common_angular_mtf import evaluate_common_grid_edge

        comparison["common_grid_object_angle_edge_mtf50"] = evaluate_common_grid_edge(
            cases, images, valid_masks, comparison, angular_edge, isp_profile=selected,
        )
    for slot in SLOTS:
        cases[slot]["matched_object_angle_edge_mtf50"] = angular_edge["per_slot"].get(
            slot, {"available": False, "reason": "; ".join(angular_edge["reasons"])}
        )
    for item in cases.values():
        del item["binding"]
    return {
        "schema": schema,
        "status": "completed",
        "source_kind": "four_completed_lens_sensor_raw_jobs",
        "source_slots": list(SLOTS),
        "isp_profile": selected,
        "cases": cases,
        "comparison": comparison,
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
        },
        "limitation": (
            "Exact retained single-frame RAW is replayed through fixed ISP v2. Lens "
            "transmission/relative illumination and PSF sampling are not validated. "
            "Native cycles/pixel MTF50 is individual only. The optional matched "
            "object-angle MTF50 compares central simulated edge response when "
            "all angular and sampling gates pass; it is not measured accuracy. "
            + (
                "The v2 common-grid diagnostic uses the same object-angle area and "
                "antialiased sample grid for all four images; missing optical and "
                "pixel-angle physics still precludes a product ranking. "
                if schema == SCHEMA_V2 else ""
            ) +
            "Temporal SNR, color accuracy and product ranking are not established."
        ),
    }, arrays
