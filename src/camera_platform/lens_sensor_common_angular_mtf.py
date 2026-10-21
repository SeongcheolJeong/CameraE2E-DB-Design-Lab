"""Bounded edge diagnostic on one shared object-angle area and sample grid.

The inputs are retained, already processed images. This interpolation does not
repair missing lens diffraction, radiometry, angular pixel response, or
physical validation. It only removes the unequal-ROI-area confounder from a
simulation screening diagnostic.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy.ndimage import gaussian_filter
from scipy.spatial import Delaunay, QhullError

from pyisetcam.sensor_comparison import _edge_metrics

SLOTS = ("L1S1", "L1S2", "L2S1", "L2S2")
_PAIRS = (
    ("L1S1", "L2S1", "lens_only"),
    ("L1S2", "L2S2", "lens_only"),
    ("L1S1", "L1S2", "sensor_only"),
    ("L2S1", "L2S2", "sensor_only"),
)


def _model_point_cell_footprint(
    cases: Mapping[str, Mapping[str, Any]],
    native_diagnostic: Mapping[str, Any],
) -> dict[str, Any]:
    """Quantify unequal 550 nm geometric sample cells in the scored ROIs.

    Central differences of the saved point-center map give a local angular
    Jacobian. Its cell area is a geometry proxy; the simulator's sensor
    integration kernel, color-dependent PSF and ISP receptive field are not
    represented by that Jacobian.
    """
    result: dict[str, Any] = {
        "available": False,
        "basis": "550nm_strict_raytrace_point_center_jacobian_cell_proxy",
        "method": "central_difference_interior_of_each_native_quality_roi",
        "per_slot": {},
        "max_to_min_median_cell_area_ratio": None,
        "max_to_min_median_row_side_ratio": None,
        "max_to_min_median_col_side_ratio": None,
        "proxy_within_one_percent_all_axes_and_area": False,
        "actual_pixel_integration_or_psf_footprint_compared": False,
        "reason": None,
    }
    if native_diagnostic.get("available") is not True:
        result["reason"] = "Native quality ROIs are unavailable."
        return result
    for slot in SLOTS:
        try:
            conditions = cases[slot]["conditions"]
            band = conditions["scene_wavelength_nm"].index(550.0)
            point = conditions["native_output_angular_grid"]["distortion_aware_point_center"]
            coordinates = np.asarray(
                point["coordinates_deg_row_col_by_wavelength"], dtype=float
            )[band]
            (r0, r1), (c0, c1) = native_diagnostic["per_slot"][slot][
                "native_roi_row_col_exclusive"
            ]
            crop = coordinates[r0:r1, c0:c1]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            result["reason"] = f"{slot} lacks a usable 550 nm point-center ROI: {exc}"
            return result
        if crop.ndim != 3 or crop.shape[-1] != 2 or min(crop.shape[:2]) < 3:
            result["reason"] = f"{slot} lacks an interior point-center cell."
            return result
        if not np.isfinite(crop).all():
            result["reason"] = f"{slot} has nonfinite point-center coordinates."
            return result
        row_vector = (crop[2:, 1:-1] - crop[:-2, 1:-1]) / 2.0
        col_vector = (crop[1:-1, 2:] - crop[1:-1, :-2]) / 2.0
        signed_area = (
            row_vector[..., 0] * col_vector[..., 1]
            - row_vector[..., 1] * col_vector[..., 0]
        )
        row_side = np.linalg.norm(row_vector, axis=-1)
        col_side = np.linalg.norm(col_vector, axis=-1)
        if (
            not np.isfinite(signed_area).all()
            or np.any(signed_area <= 0)
            or np.any(row_side <= 0)
            or np.any(col_side <= 0)
        ):
            result["reason"] = f"{slot} has a folded or degenerate model cell."
            return result
        result["per_slot"][slot] = {
            "interior_cell_count": int(signed_area.size),
            "median_cell_area_square_degrees": float(np.median(signed_area)),
            "minimum_cell_area_square_degrees": float(np.min(signed_area)),
            "maximum_cell_area_square_degrees": float(np.max(signed_area)),
            "median_row_side_deg": float(np.median(row_side)),
            "median_col_side_deg": float(np.median(col_side)),
        }
    def ratio(key: str) -> float:
        values = [result["per_slot"][slot][key] for slot in SLOTS]
        return float(max(values) / min(values))

    area_ratio = ratio("median_cell_area_square_degrees")
    row_ratio = ratio("median_row_side_deg")
    col_ratio = ratio("median_col_side_deg")
    result.update({
        "available": True,
        "max_to_min_median_cell_area_ratio": area_ratio,
        "max_to_min_median_row_side_ratio": row_ratio,
        "max_to_min_median_col_side_ratio": col_ratio,
        "proxy_within_one_percent_all_axes_and_area": all(
            value <= 1.01 + 1e-12 for value in (area_ratio, row_ratio, col_ratio)
        ),
    })
    return result


def _sample_on_grid(
    angular: np.ndarray,
    rgb: np.ndarray,
    valid: np.ndarray,
    query: np.ndarray,
    common_step_deg_row_col: tuple[float, float],
    native_roi_row_col_exclusive: tuple[int, int, int, int],
    *,
    boundary_mode: str = "reflect",
) -> tuple[np.ndarray, float] | None:
    """Area-filter and barycentrically sample one distortion-aware point map.

    A 550 nm point-center map is only a geometric approximation of the actual
    broadband pixel footprint. The caller checks its chromatic and local-scale
    limits before this operation.
    """
    row_step = float(np.median(np.diff(angular[..., 0], axis=0)))
    col_step = float(np.median(np.diff(angular[..., 1], axis=1)))
    if not all(math.isfinite(v) and v > 0 for v in (row_step, col_step)):
        return None
    # Downsampling must suppress frequencies above the shared output Nyquist.
    sigma = np.asarray([
        0.5 * math.sqrt(max((output_step / native_step) ** 2 - 1.0, 0.0))
        for output_step, native_step in zip(
            common_step_deg_row_col, (row_step, col_step), strict=True
        )
    ])
    weighted_full = gaussian_filter(
        np.where(valid[..., None], rgb, 0.0),
        sigma=(float(sigma[0]), float(sigma[1]), 0.0), mode=boundary_mode,
    )
    support_full = gaussian_filter(
        valid.astype(float), sigma=tuple(float(value) for value in sigma),
        mode=boundary_mode,
    )
    r0, r1, c0, c1 = native_roi_row_col_exclusive
    weighted = weighted_full[r0:r1, c0:c1]
    support = support_full[r0:r1, c0:c1]
    # An invalid ISP pixel inside an antialiasing footprint is not filled in.
    if not np.isfinite(weighted).all() or not np.isfinite(support).all():
        return None
    try:
        triangulation = Delaunay(angular.reshape(-1, 2))
    except QhullError:
        return None
    simplex = triangulation.find_simplex(query)
    if np.any(simplex < 0):
        return None
    vertices = triangulation.simplices[simplex]
    if np.any(support.reshape(-1)[vertices] < 0.999):
        return None
    transform = triangulation.transform[simplex]
    distance = query - transform[:, 2]
    first_two = np.einsum("nij,nj->ni", transform[:, :2], distance)
    weights = np.concatenate((first_two, 1.0 - first_two.sum(axis=1, keepdims=True)), axis=1)
    if np.any(weights < -1e-9):
        return None
    sample = np.sum(weighted.reshape(-1, 3)[vertices] * weights[..., None], axis=1)
    return sample, float(max(sigma))


def _sample_all(
    cases: Mapping[str, Mapping[str, Any]], images: Mapping[str, np.ndarray],
    valid_masks: Mapping[str, np.ndarray], native_diagnostic: Mapping[str, Any],
    query: np.ndarray, shape: tuple[int, int], steps: tuple[float, float],
) -> tuple[dict[str, np.ndarray], dict[str, float]] | None:
    sampled: dict[str, np.ndarray] = {}
    smoothing: dict[str, float] = {}
    for slot in SLOTS:
        conditions = cases[slot]["conditions"]
        point = conditions["native_output_angular_grid"]["distortion_aware_point_center"]
        band = conditions["scene_wavelength_nm"].index(550.0)
        (r0, r1), (c0, c1) = native_diagnostic["per_slot"][slot][
            "native_roi_row_col_exclusive"
        ]
        angular = np.asarray(point["coordinates_deg_row_col_by_wavelength"], dtype=float)[
            band, r0:r1, c0:c1
        ]
        sample = _sample_on_grid(
            angular, np.asarray(images[slot], dtype=float),
            np.asarray(valid_masks[slot], dtype=bool), query, steps,
            (r0, r1, c0, c1),
        )
        if sample is None:
            return None
        sampled[slot], smoothing[slot] = sample[0].reshape(*shape, 3), sample[1]
    return sampled, smoothing


def _finish_metric(
    result: dict[str, Any], sampled: Mapping[str, np.ndarray],
    smoothing: Mapping[str, float], grid: dict[str, Any], normal_step: float,
) -> dict[str, Any]:
    edges = {slot: _edge_metrics(sampled[slot]) for slot in SLOTS}
    if any(not edges[slot]["available"] for slot in SLOTS):
        result["reasons"].append("Shared-grid ISO edge estimator is unavailable.")
        return result
    common_nyquist = 0.5 / normal_step
    for slot in SLOTS:
        frequency = float(edges[slot]["mtf50_cycles_per_output_pixel"] / normal_step)
        result["per_slot"][slot] = {
            "available": True,
            "mtf50_cycles_per_degree_object_angle": frequency,
            "iso_method": edges[slot]["method"],
            "antialias_sigma_native_pixels_max": smoothing[slot],
        }
    result["grid"] = grid
    result["angular_roi_area_matched"] = True
    result["angular_sample_grid_matched"] = True
    result["common_nyquist_cycles_per_degree_object_angle"] = common_nyquist
    if any(
        result["per_slot"][slot]["mtf50_cycles_per_degree_object_angle"] >= common_nyquist
        for slot in SLOTS
    ):
        result["reasons"].append("One edge MTF50 reaches the common angular Nyquist.")
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


def _evaluate_common_grid_edge(
    cases: Mapping[str, Mapping[str, Any]],
    images: Mapping[str, np.ndarray],
    valid_masks: Mapping[str, np.ndarray],
    comparison: Mapping[str, Any],
    native_diagnostic: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare four simulated edges on identical angular coordinates and area.

    This metric remains a model sensitivity check. It cannot establish a
    product ordering while optical and pixel-angle physics are unvalidated.
    """
    result: dict[str, Any] = {
        "available": False,
        "method": "antialiased_barycentric_550nm_point_map_common_angular_grid_v1",
        "unit": "cycles/degree_object_angle",
        "scope": "central_simulated_broadband_edge_sensitivity_only",
        "angular_roi_area_matched": False,
        "angular_sample_grid_matched": False,
        "area_basis": "550nm_geometric_pixel_center_coordinates_only",
        "pixel_integrated_effective_area_matched": False,
        "model_550nm_point_cell_footprint": _model_point_cell_footprint(
            cases, native_diagnostic
        ),
        "product_ranking_allowed": False,
        "grid": None,
        "per_slot": {},
        "pairs": [],
        "reasons": [],
        "physics_limits": [
            "geometric_psf_without_diffraction_or_sampling_validation",
            "lens_radiometry_unknown_unity_assumption",
            "550nm_point_centers_not_broadband_pixel_integrated_footprints",
            "sensor_cra_angular_response_not_established",
        ],
    }
    if native_diagnostic.get("available") is not True:
        result["reasons"].append("Native angular edge prerequisite is unavailable.")
        return result
    if result["model_550nm_point_cell_footprint"]["available"] is not True:
        result["reasons"].append(
            "The 550 nm geometric point-cell footprint is invalid: "
            + str(result["model_550nm_point_cell_footprint"]["reason"])
        )
        return result
    overlap = comparison["common_550nm_point_center_roi"]
    low = np.asarray((overlap["row_angle_deg"][0], overlap["column_angle_deg"][0]), dtype=float)
    high = np.asarray((overlap["row_angle_deg"][1], overlap["column_angle_deg"][1]), dtype=float)
    if not np.isfinite(low).all() or not np.isfinite(high).all() or np.any(high <= low):
        result["reasons"].append("Common angular overlap is invalid.")
        return result
    center = (low + high) / 2.0
    max_step = max(
        max(native_diagnostic["per_slot"][slot]["mean_angular_step_deg_row_col"])
        for slot in SLOTS
    )
    if not math.isfinite(max_step) or max_step <= 0:
        result["reasons"].append("Native angular sampling is invalid.")
        return result

    # Search for the largest central square covered by every map. Inward-only
    # shrinking prevents interpolation outside any native point-center hull.
    span = float(np.min(high - low))
    for shrink in (1.0, 0.98, 0.96, 0.94, 0.92, 0.90, 0.88, 0.86):
        extent = span * shrink
        count = min(32, int(math.floor(extent / max_step)) + 1)
        if count < 16:
            continue
        step = extent / (count - 1)
        if step + 1e-12 < max_step:
            continue
        row_axis = np.linspace(center[0] - extent / 2, center[0] + extent / 2, count)
        col_axis = np.linspace(center[1] - extent / 2, center[1] + extent / 2, count)
        yy, xx = np.meshgrid(row_axis, col_axis, indexing="ij")
        query = np.column_stack((yy.ravel(), xx.ravel()))
        sampled_all = _sample_all(
            cases, images, valid_masks, native_diagnostic, query, (count, count),
            (step, step),
        )
        if sampled_all is None:
            continue
        sampled, smoothing = sampled_all
        grid = {
            "shape_row_col": [count, count],
            "row_angle_deg": [float(row_axis[0]), float(row_axis[-1])],
            "column_angle_deg": [float(col_axis[0]), float(col_axis[-1])],
            "step_deg_row_col": [step, step],
            "area_square_degrees": extent * extent,
            "grid_geometry": "uniform_central_square",
            "source_map_wavelength_nm": 550.0,
            "interpolation": "piecewise_linear_inside_all_four_point_center_hulls",
            "downsampling": "gaussian_antialias_before_interpolation",
            "filter_boundary": "reflect_full_retained_isp_frame",
        }
        # The authored source edge is x = y/12 in angular coordinates.
        return _finish_metric(result, sampled, smoothing, grid, step)

    # A 16-sample native ROI may have no room for a *new* 16-sample uniform
    # square after convex-hull margins. Its exact vetted point centers can be
    # the shared output coordinates instead, with all other cameras sampled at
    # those same points. This never extrapolates or manufactures finer samples.
    step_by_slot = {
        slot: tuple(native_diagnostic["per_slot"][slot]["mean_angular_step_deg_row_col"])
        for slot in SLOTS
    }
    required_step = tuple(max(step_by_slot[slot][axis] for slot in SLOTS) for axis in (0, 1))
    for reference in sorted(SLOTS, key=lambda slot: max(step_by_slot[slot]), reverse=True):
        if any(step_by_slot[reference][axis] + 1e-12 < required_step[axis] for axis in (0, 1)):
            continue
        conditions = cases[reference]["conditions"]
        point = conditions["native_output_angular_grid"]["distortion_aware_point_center"]
        band = conditions["scene_wavelength_nm"].index(550.0)
        (r0, r1), (c0, c1) = native_diagnostic["per_slot"][reference][
            "native_roi_row_col_exclusive"
        ]
        angular = np.asarray(point["coordinates_deg_row_col_by_wavelength"], dtype=float)[
            band, r0:r1, c0:c1
        ]
        shape = angular.shape[:2]
        if min(shape) < 16:
            continue
        sampled_all = _sample_all(
            cases, images, valid_masks, native_diagnostic,
            angular.reshape(-1, 2), shape, step_by_slot[reference],
        )
        if sampled_all is None:
            continue
        sampled, smoothing = sampled_all
        row_step, col_step = step_by_slot[reference]
        slope = row_step / (12.0 * col_step)
        normal_step = col_step * math.hypot(1.0, slope) / math.hypot(1.0, 1.0 / 12.0)
        corners = np.asarray((angular[0, 0], angular[0, -1],
                              angular[-1, -1], angular[-1, 0]))
        area = 0.5 * abs(float(np.dot(corners[:, 0], np.roll(corners[:, 1], -1))
                               - np.dot(corners[:, 1], np.roll(corners[:, 0], -1))))
        if not math.isfinite(area) or area <= 0:
            continue
        grid = {
            "shape_row_col": list(shape),
            "row_angle_deg": [float(angular[..., 0].min()), float(angular[..., 0].max())],
            "column_angle_deg": [float(angular[..., 1].min()), float(angular[..., 1].max())],
            "step_deg_row_col": [row_step, col_step],
            "area_square_degrees": area,
            "area_method": "four_corners_of_reference_point_center_grid",
            "grid_geometry": "vetted_native_reference_point_centers",
            "reference_slot": reference,
            "reference_native_roi_row_col_exclusive": [[r0, r1], [c0, c1]],
            "source_map_wavelength_nm": 550.0,
            "interpolation": "piecewise_linear_inside_all_four_point_center_hulls",
            "downsampling": "gaussian_antialias_before_interpolation",
            "filter_boundary": "reflect_full_retained_isp_frame",
        }
        return _finish_metric(result, sampled, smoothing, grid, normal_step)
    result["reasons"].append(
        "No central 16x16 common object-angle grid is covered by all four valid ISP images."
    )
    return result


def _grid_coordinates(
    cases: Mapping[str, Mapping[str, Any]], grid: Mapping[str, Any] | None,
) -> np.ndarray | None:
    if grid is None:
        return None
    height, width = grid["shape_row_col"]
    if grid["grid_geometry"] == "uniform_central_square":
        rows = np.linspace(*grid["row_angle_deg"], height)
        cols = np.linspace(*grid["column_angle_deg"], width)
        yy, xx = np.meshgrid(rows, cols, indexing="ij")
        coordinates = np.stack((yy, xx), axis=-1)
    else:
        conditions = cases[grid["reference_slot"]]["conditions"]
        band = conditions["scene_wavelength_nm"].index(550.0)
        point = conditions["native_output_angular_grid"]["distortion_aware_point_center"]
        (r0, r1), (c0, c1) = grid["reference_native_roi_row_col_exclusive"]
        coordinates = np.asarray(point["coordinates_deg_row_col_by_wavelength"])[
            band, r0:r1, c0:c1
        ]
    return coordinates


def _scored_point_roi(
    cases: Mapping[str, Mapping[str, Any]], grid: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Describe the actual center-coordinate crop consumed by _edge_metrics.

    ISO edge estimation discards one eighth of the resampled frame on each
    side. Neither this crop nor its bounding box proves physical input support.
    """
    coordinates = _grid_coordinates(cases, grid)
    if coordinates is None:
        return None
    height, width = coordinates.shape[:2]
    ry, rx = max(1, height // 8), max(1, width // 8)
    scored = coordinates[ry:-ry, rx:-rx]
    return {
        "coordinate_basis": "550nm_geometric_point_centers_only",
        "resampled_frame_shape_row_col": [height, width],
        "scored_roi_row_col_exclusive": [[ry, height - ry], [rx, width - rx]],
        "scored_shape_row_col": list(scored.shape[:2]),
        "row_angle_deg": [float(scored[..., 0].min()), float(scored[..., 0].max())],
        "column_angle_deg": [float(scored[..., 1].min()), float(scored[..., 1].max())],
        "boundary_corners_deg_row_col": scored[
            [0, 0, -1, -1], [0, -1, -1, 0]
        ].tolist(),
        "physical_input_support": "not_established",
    }


def _model_input_support_diagnostic(
    cases: Mapping[str, Mapping[str, Any]],
    images: Mapping[str, np.ndarray],
    valid_masks: Mapping[str, np.ndarray],
    native_diagnostic: Mapping[str, Any],
    result: Mapping[str, Any],
    isp_profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Measure saved-array boundary effects without inferring optical support.

    The quality study has a finite 3x3 demosaic and optionally a finite 3x3
    sharpen after pointwise stages. Its stored profile and implementation must
    match the per-case summary before any ISP halo is credited. This does not
    bound the upstream geometric PSF, pixel integration, or scene padding.
    """
    diagnostic: dict[str, Any] = {
        "schema": "camera_lens_sensor_model_input_support_diagnostic_v1",
        "scope": "scored_550nm_point_center_resampling_and_fixed_isp_only",
        "available": False,
        "per_slot": {},
        "all_scored_resampling_boundaries_independent": None,
        "all_fixed_isp_crop_boundaries_outside_scored_halo": None,
        "all_scored_point_centers_within_source_sample_centers": None,
        "all_recorded_global_psf_downstream_upper_bounds_fit_scene": None,
        "optical_psf_input_support_verified": False,
        "pixel_integration_input_support_verified": False,
        "source_scene_halo_verified": False,
        "sampling_and_edge_estimator_convergence_verified": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "reasons": [],
    }
    coordinates = _grid_coordinates(cases, result["grid"])
    if coordinates is None or native_diagnostic.get("available") is not True:
        diagnostic["reasons"].append("No scored common coordinate grid is available.")
        return diagnostic
    height, width = coordinates.shape[:2]
    ry, rx = max(1, height // 8), max(1, width // 8)
    query = np.asarray(coordinates[ry:-ry, rx:-rx], dtype=float).reshape(-1, 2)
    if not query.size or not np.isfinite(query).all():
        diagnostic["reasons"].append("The scored coordinate grid is invalid.")
        return diagnostic
    profile_halo: int | None = None
    if isp_profile is not None:
        from . import isp_v2

        try:
            selected = isp_v2.validate_profile(dict(isp_profile))
        except (KeyError, TypeError, ValueError):
            selected = None
        if selected is not None and (
            selected["calibration_id"] is None
            and selected["spatial_lsc"]["mode"] == "bypass"
            and selected["raw_denoise"]["mode"] == "bypass"
        ):
            sharpen = selected["linear_sharpen"]
            profile_halo = 1 + int(
                sharpen["mode"] != "bypass"
                and sharpen["amount"] > 0
                and sharpen["max_delta"] > 0
            )

    for slot in SLOTS:
        try:
            condition = cases[slot]["conditions"]
            point = condition["native_output_angular_grid"]["distortion_aware_point_center"]
            waves = condition["scene_wavelength_nm"]
            bands = np.asarray(point["coordinates_deg_row_col_by_wavelength"], dtype=float)
            band = waves.index(550.0)
            (r0, r1), (c0, c1) = native_diagnostic["per_slot"][slot][
                "native_roi_row_col_exclusive"
            ]
            native = bands[band, r0:r1, c0:c1]
            frame = np.asarray(images[slot], dtype=float)
            valid = np.asarray(valid_masks[slot], dtype=bool)
            if (
                bands.ndim != 4 or bands.shape[0] != len(waves)
                or bands.shape[1:3] != frame.shape[:2]
                or frame.shape[-1] != 3 or valid.shape != frame.shape[:2]
                or not np.isfinite(bands).all()
            ):
                raise ValueError("saved angular map or RGB shape differs")
            triangulation = Delaunay(native.reshape(-1, 2))
            simplex = triangulation.find_simplex(query)
            if np.any(simplex < 0):
                raise ValueError("scored coordinates leave the native point-center hull")
            vertices = triangulation.simplices[simplex]
            native_rows = vertices // native.shape[1] + r0
            native_cols = vertices % native.shape[1] + c0
            margin = int(np.min(np.stack((
                native_rows, native_cols,
                frame.shape[0] - 1 - native_rows,
                frame.shape[1] - 1 - native_cols,
            ))))
            row_step = float(np.median(np.diff(native[..., 0], axis=0)))
            col_step = float(np.median(np.diff(native[..., 1], axis=1)))
            step = result["grid"]["step_deg_row_col"]
            sigma = [
                0.5 * math.sqrt(max((output / source) ** 2 - 1.0, 0.0))
                for output, source in zip(step, (row_step, col_step), strict=True)
            ]
            # scipy.ndimage.gaussian_filter uses radius round(4*sigma) by default.
            radius = [int(4.0 * value + 0.5) for value in sigma]
            sampled_reflect = _sample_on_grid(
                native, frame, valid, query, tuple(step), (r0, r1, c0, c1)
            )
            sampled_constant = _sample_on_grid(
                native, frame, valid, query, tuple(step), (r0, r1, c0, c1),
                boundary_mode="constant",
            )
            if sampled_reflect is None or sampled_constant is None:
                maximum_difference = None
                resampling_boundary_independent = False
            else:
                maximum_difference = float(np.max(np.abs(
                    sampled_reflect[0] - sampled_constant[0]
                )))
                resampling_boundary_independent = (
                    margin >= max(radius) and maximum_difference <= 1e-12
                )
            source_support = point.get("input_scene_support") or {}
            declared_support = np.asarray(
                source_support.get("source_sample_center_support_deg_row_col", []),
                dtype=float,
            )
            if (
                source_support.get("status")
                == "all_model_point_centers_within_source_samples"
                and declared_support.shape == (2,)
                and np.all(np.isfinite(declared_support))
                and np.all(declared_support > 0)
            ):
                sampled_centers = bands[:, native_rows, native_cols, :]
                maximum_abs = np.max(np.abs(sampled_centers), axis=(1, 2))
                source_margin = (declared_support[None, :] - maximum_abs).tolist()
                point_centers_within_source = bool(np.all(
                    declared_support[None, :] - maximum_abs >= -1e-12
                ))
            else:
                source_margin = None
                point_centers_within_source = None
            fixed_isp_identity = (
                cases[slot].get("isp_implementation") == "camera_platform_float_isp_v2"
                and profile_halo is not None
                and cases[slot].get("isp_crop_full_frame_halo_native_pixels")
                == profile_halo
            )
            # The bounded source RAW keeps the largest exact nonzero PSF
            # kernel across its whole wavelength/field/azimuth execution. It
            # is a conservative *global* support bound, not a local or
            # wavelength-specific energy measurement at this edge ROI.
            source_halo: dict[str, Any] = {
                "status": "not_recorded",
                "recorded_global_upper_bound_fits_source_scene": None,
                "scored_local_input_support_verified": None,
            }
            source_model = condition.get("model_psf_source_support")
            if (
                isinstance(source_model, Mapping)
                and source_model.get("status") == "global_exact_nonzero_bound_recorded"
            ):
                shape = source_model["scene_samples_row_col"]
                spacing = float(source_model["oi_sample_spacing_um"])
                pitch = float(source_model["sensor_pixel_pitch_um"])
                psf_shape = source_model[
                    "largest_exact_nonzero_centered_kernel_row_col"
                ]
                if (
                    not isinstance(shape, list) or len(shape) != 2
                    or not isinstance(psf_shape, list) or len(psf_shape) != 2
                    or not math.isfinite(spacing) or spacing <= 0
                    or not math.isfinite(pitch) or pitch <= 0
                    or source_model.get("executed_wavelength_nm") != waves
                    or not all(type(v) is int and v > 0 for v in shape + psf_shape)
                    or not math.isclose(
                        pitch, float(condition["sensor_pixel_pitch_um"]), rel_tol=1e-9
                    )
                ):
                    raise ValueError("Source PSF/scene halo metadata differs from this case.")
                samples_per_native = pitch / spacing
                sensor_center = (np.asarray(frame.shape[:2], dtype=float) - 1.0) / 2.0
                vertex_center_offset = np.asarray([
                    np.max(np.abs((native_rows - sensor_center[0]) * samples_per_native)),
                    np.max(np.abs((native_cols - sensor_center[1]) * samples_per_native)),
                ])
                source_half = (np.asarray(shape, dtype=float) - 1.0) / 2.0
                available = source_half - vertex_center_offset
                psf_radius = (np.asarray(psf_shape, dtype=float) - 1.0) / 2.0
                # The saved RAW lacks executed mosaic spatial-integration
                # operator details (subpixel count, OCL group and aperture).
                # The global maximum gives a sufficient PSF bound when it fits.
                # When it does not fit, local scored support remains unknown.
                # Unrecorded mosaic integration prevents full certification.
                downstream_native_halo = np.asarray(radius, dtype=float)
                if fixed_isp_identity:
                    downstream_native_halo += profile_halo
                required = (
                    psf_radius + downstream_native_halo * samples_per_native
                )
                recorded_bound_fits = bool(np.all(available >= required))
                source_halo = {
                    "status": "global_bound_evaluated",
                    "wavelength_nm": waves,
                    "per_wavelength_local_psf_support_recorded": False,
                    "scene_samples_row_col": shape,
                    "source_half_span_samples_row_col": source_half.tolist(),
                    "scored_vertex_max_distance_from_scene_center_samples_row_col": (
                        vertex_center_offset.tolist()
                    ),
                    "scored_vertex_available_halo_samples_row_col": available.tolist(),
                    "executed_global_max_nonzero_psf_radius_samples_row_col": (
                        psf_radius.tolist()
                    ),
                    "sensor_mosaic_spatial_integration_support": "not_recorded",
                    "scored_antialias_radius_native_pixels_row_col": radius,
                    "fixed_isp_radius_native_pixels": (
                        profile_halo if fixed_isp_identity else None
                    ),
                    "required_recorded_global_upper_bound_halo_samples_row_col": (
                        required.tolist()
                    ),
                    "recorded_global_upper_bound_margin_samples_row_col": (
                        available - required
                    ).tolist(),
                    "recorded_global_upper_bound_fits_source_scene": recorded_bound_fits,
                    "scored_local_input_support_verified": None,
                    "interpretation": (
                        "This maximum over all generated kernels is an upper bound "
                        "on local modeled PSF support. A nonfitting bound cannot "
                        "establish local truncation; a fitting bound cannot certify "
                        "unrecorded mosaic pixel integration."
                    ),
                }
            diagnostic["per_slot"][slot] = {
                "source_wavelength_nm": waves,
                "scored_vertex_min_native_frame_border_margin_pixels": margin,
                "scored_antialias_sigma_native_pixels_row_col": sigma,
                "scored_antialias_radius_native_pixels_row_col": radius,
                "scored_reflect_vs_zero_boundary_max_rgb_difference": maximum_difference,
                "scored_resampling_boundary_independent": resampling_boundary_independent,
                "fixed_quality_isp_halo_upper_bound_native_pixels": (
                    profile_halo if fixed_isp_identity else None
                ),
                "fixed_isp_crop_boundary_outside_scored_halo": (
                    margin >= profile_halo if fixed_isp_identity else None
                ),
                "scored_point_center_to_scene_sample_center_margin_deg_by_wavelength_row_col": (
                    source_margin
                ),
                "scored_point_centers_within_source_sample_centers": (
                    point_centers_within_source
                ),
                "point_center_margin_is_optical_pixel_support": False,
                "executed_psf_pixel_source_scene_halo": source_halo,
            }
        except (KeyError, IndexError, TypeError, ValueError, QhullError) as exc:
            diagnostic["reasons"].append(f"{slot}: {exc}")
            return diagnostic
    diagnostic["available"] = True
    diagnostic["all_scored_resampling_boundaries_independent"] = all(
        diagnostic["per_slot"][slot]["scored_resampling_boundary_independent"]
        for slot in SLOTS
    )
    isp_values = [
        diagnostic["per_slot"][slot]["fixed_isp_crop_boundary_outside_scored_halo"]
        for slot in SLOTS
    ]
    diagnostic["all_fixed_isp_crop_boundaries_outside_scored_halo"] = (
        all(isp_values) if all(value is not None for value in isp_values) else None
    )
    source_values = [
        diagnostic["per_slot"][slot]["scored_point_centers_within_source_sample_centers"]
        for slot in SLOTS
    ]
    diagnostic["all_scored_point_centers_within_source_sample_centers"] = (
        all(source_values) if all(value is not None for value in source_values) else None
    )
    halo_values = [
        diagnostic["per_slot"][slot]["executed_psf_pixel_source_scene_halo"][
            "recorded_global_upper_bound_fits_source_scene"
        ]
        for slot in SLOTS
    ]
    diagnostic["all_recorded_global_psf_downstream_upper_bounds_fit_scene"] = (
        all(halo_values) if all(value is not None for value in halo_values) else None
    )
    diagnostic["reasons"].extend((
        "When recorded, the RAW job supplies a global PSF nonzero extent, not the wavelength- "
        "and ROI-specific optical kernel or its encircled energy.",
        "The modeled source-scene halo has no validated physical pixel aperture, "
        "diffraction, or independent boundary-extension sensitivity.",
        "One retained capture and one source sampling do not establish MTF estimator convergence.",
    ))
    return diagnostic


def _physical_mtf_comparability(
    cases: Mapping[str, Mapping[str, Any]], result: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed when a common coordinate ROI lacks qualified input support.

    This quality-job contract retains center maps and processed RGB, but no
    wavelength-dependent optical/pixel/ISP support closure. Center coincidence,
    valid RGB masks, or equal Jacobian areas cannot supply that missing proof.
    Different pixel apertures and PSFs are allowed measurands, not in themselves
    comparability failures. No external boolean can upgrade this diagnostic.
    """
    coordinate_matched = (
        result["angular_roi_area_matched"] is True
        and result["angular_sample_grid_matched"] is True
    )
    missing = [
        "broadband_object_coordinate_support_unqualified",
        "per_system_optical_pixel_isp_input_support_unqualified",
        "resampling_boundary_extension_independence_unqualified",
    ]
    if not coordinate_matched:
        missing.insert(0, "common_point_center_roi_unavailable")
    if result["available"] is not True:
        missing.insert(0, "common_grid_edge_metric_unavailable")
    return {
        "schema": "camera_lens_sensor_physical_mtf_comparability_v1",
        "status": "not_established",
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "model_diagnostic_available": result["available"],
        "model_diagnostic_authority": "processed_image_model_sensitivity_only",
        "common_point_center_roi_matched": coordinate_matched,
        "scored_point_center_roi": _scored_point_roi(cases, result["grid"]),
        "broadband_object_coordinate_support_verified": None,
        "per_system_optical_pixel_isp_input_support_verified": None,
        "resampling_boundary_extension_independence_verified": None,
        "resampling_boundary_rule": (
            result["grid"]["filter_boundary"] if result["grid"] else None
        ),
        "equal_native_pixel_apertures_or_psf_shapes_required": False,
        "point_cell_jacobian_role": "sampling_geometry_only_not_support_qualification",
        "unmet_requirements": missing,
        "required_evidence": [
            "common_object_coordinate_scored_roi_across_contributing_wavelengths",
            "each_system_own_optical_pixel_and_isp_input_support_coverage",
            "qualified_source_halo_and_crop_or_boundary_extension_sensitivity",
            "sampling_and_edge_estimator_convergence_on_that_common_roi",
        ],
    }


def evaluate_common_grid_edge(
    cases: Mapping[str, Mapping[str, Any]],
    images: Mapping[str, np.ndarray],
    valid_masks: Mapping[str, np.ndarray],
    comparison: Mapping[str, Any],
    native_diagnostic: Mapping[str, Any],
    *,
    isp_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Retain the numeric model diagnostic and separately qualify its scope."""
    result = _evaluate_common_grid_edge(cases, images, valid_masks, comparison, native_diagnostic)
    result["physical_mtf_comparability"] = _physical_mtf_comparability(cases, result)
    result["model_input_support_diagnostic"] = _model_input_support_diagnostic(
        cases, images, valid_masks, native_diagnostic, result, isp_profile,
    )
    return result
