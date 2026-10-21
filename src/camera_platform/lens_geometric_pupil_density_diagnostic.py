"""Bounded geometric ray-density sensitivity for one pinned Lens DB PSF node.

The saved source pupil is retraced and must reproduce its pinned ray mask,
chief ray, and geometric histogram before any denser pupil is compared.  Each
denser level uses the same prescription, field, wavelength, chief reference,
and physical histogram support.  OTF is evaluated directly from independently
traced ray intersections, not from an interpolated histogram.  Agreement is a
numerical statement about this geometric model only: it excludes diffraction,
pixel aperture, source accuracy, and physical/system MTF validation.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .lens_catalog_bridge import _asset, _revision_source
from .lens_geometric_psf_rehistogram import (
    BUILDER_MODEL,
    SOURCE_REPRODUCTION_L1_TOLERANCE,
    SOURCE_REPRODUCTION_MAX_ABS_TOLERANCE,
    SUPPORTED_BUILDER_GENERATOR_SHA256,
    _array_sha,
    _candidate,
    _histogram,
    _model,
    _pupil_points,
    _reviewed_surfaces,
    _trace,
)

SCHEMA = "camera_pinned_geometric_pupil_density_diagnostic_v2"
_MAX_LEVELS = 5
_MAX_PUPIL_SAMPLES = 257
_MAX_TARGET_FREQUENCIES_PER_AXIS = 64
_MAX_TOTAL_RAYS = 50_000


@dataclass(frozen=True)
class _PinnedNode:
    revision_id: str
    simulation_id: str
    lens_db_sha256: str
    source_psf_asset_sha256: str
    source_prescription_sha256: str
    builder_generator_sha256: str
    model: Any
    field: Any
    field_index: int
    field_height_mm: float
    wavelength_nm: float
    source_pupil_samples: int
    source_pupil: np.ndarray
    source_success_mask: np.ndarray
    source_success_count: int
    saved_chief_mm_xy: np.ndarray
    source_histogram: np.ndarray
    source_pitch_mm: float
    minimum_retained_fraction: float


def _positive_number(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return number


def _targets(values: Sequence[Sequence[float]]) -> tuple[np.ndarray, np.ndarray]:
    if len(values) != 2:
        raise ValueError("Declare row and column target-frequency sequences.")
    result = tuple(np.asarray(axis, dtype=float) for axis in values)
    if any(
        axis.ndim != 1 or not 1 <= axis.size <= _MAX_TARGET_FREQUENCIES_PER_AXIS
        or not np.isfinite(axis).all() or axis[0] <= 0
        or np.any(np.diff(axis) <= 0)
        for axis in result
    ):
        raise ValueError("Target frequencies must be bounded, finite, positive and increasing.")
    return result  # type: ignore[return-value]


def _load_pinned_node(
    revision_id: str, catalog_root: str | Path, field_index: int, wavelength_nm: float,
) -> _PinnedNode:
    root = Path(catalog_root).expanduser().resolve()
    revision, snapshot, db_path, db_sha = _revision_source(revision_id, root)
    simulation_id, asset_path, validation, _artifact = _asset(
        revision, snapshot, db_path, root
    )
    with np.load(asset_path, allow_pickle=False) as asset:
        settings = json.loads(str(asset["build_settings_json"]))
        quality = json.loads(str(asset["quality_json"]))
        fields = np.asarray(asset["field_height_mm"], dtype=float).copy()
        waves = np.asarray(asset["wavelength_nm"], dtype=float).copy()
        source_pupil = np.asarray(asset["pupil_coordinates"], dtype=float).copy()
        source_masks = np.asarray(asset["pupil_weights"], dtype=float).copy()
        source_counts = np.asarray(asset["success_counts"], dtype=int).copy()
        chiefs = np.asarray(asset["chief_points_mm"], dtype=float).copy()
        source_pitch = np.asarray(asset["sample_spacing_mm"], dtype=float).copy()
        source_psf = np.asarray(asset["psf_function"], dtype=np.float32).copy()
    if (
        settings.get("model") != BUILDER_MODEL
        or settings.get("generator_sha256") not in SUPPORTED_BUILDER_GENERATOR_SHA256
        or quality.get("generator_sha256") != settings.get("generator_sha256")
        or quality.get("source_prescription_sha256") != validation["source_prescription_sha256"]
        or not 2 <= len(fields) <= 7 or not 1 <= len(waves) <= 5
        or not np.isfinite(fields).all() or not np.isfinite(waves).all()
        or np.any(waves <= 0)
        or not np.array_equal(waves, np.asarray(settings.get("wavelengths"), dtype=float))
        or source_psf.ndim != 4 or source_psf.shape[2:] != (len(fields), len(waves))
        or source_psf.shape[0] != source_psf.shape[1]
        or source_pitch.shape != (2,) or not np.isfinite(source_pitch).all()
        or np.any(source_pitch <= 0)
        or not np.isclose(source_pitch[0], source_pitch[1], rtol=0, atol=1e-15)
        or source_pupil.ndim != 2 or source_pupil.shape[1] != 2
        or not np.isfinite(source_pupil).all()
        or source_masks.shape != (len(fields), len(waves), len(source_pupil))
        or source_counts.shape != (len(fields), len(waves))
        or chiefs.shape != (len(fields), len(waves), 2)
        or type(settings.get("pupil_samples")) is not int
        or not 3 <= settings["pupil_samples"] <= _MAX_PUPIL_SAMPLES
    ):
        raise ValueError("Pinned PSF source/builder axes or pupil contract differs.")
    if type(field_index) is not int or not 0 <= field_index < len(fields):
        raise ValueError("Choose one exact stored field index.")
    selected_wave = _positive_number(wavelength_nm, "Wavelength")
    matching_wave = np.flatnonzero(waves == selected_wave)
    if matching_wave.size != 1:
        raise ValueError("Choose one exact stored wavelength, without interpolation.")
    wave_index = int(matching_wave[0])
    if not np.array_equal(_pupil_points(settings["pupil_samples"]), source_pupil):
        raise ValueError("Pinned source pupil positions differ from saved builder recipe.")
    candidate = _candidate(db_path, simulation_id)
    surfaces, _csv_path, prescription_sha = _reviewed_surfaces(
        candidate, db_path, quality["source_file_sha256"],
        float(settings["object_distance_m"]),
    )
    if prescription_sha != validation["source_prescription_sha256"]:
        raise ValueError("Reviewed prescription differs from pinned PSF source.")
    model, traced_fields = _model(candidate, surfaces, settings)
    if not np.allclose(fields, traced_fields, rtol=0, atol=1e-9):
        raise ValueError("Rebuilt field coordinates differ from pinned PSF asset.")
    minimum_retained = float(settings["min_retained_energy"])
    if not math.isfinite(minimum_retained) or not 0 < minimum_retained <= 1:
        raise ValueError("Pinned fixed-support retained-energy threshold is invalid.")
    return _PinnedNode(
        revision_id=revision_id,
        simulation_id=simulation_id,
        lens_db_sha256=db_sha,
        source_psf_asset_sha256=validation["asset_sha256"],
        source_prescription_sha256=prescription_sha,
        builder_generator_sha256=settings["generator_sha256"],
        model=model,
        field=model["optical_spec"]["fov"].fields[field_index],
        field_index=field_index,
        field_height_mm=float(fields[field_index]),
        wavelength_nm=selected_wave,
        source_pupil_samples=settings["pupil_samples"],
        source_pupil=source_pupil,
        source_success_mask=source_masks[field_index, wave_index] > 0,
        source_success_count=int(source_counts[field_index, wave_index]),
        saved_chief_mm_xy=chiefs[field_index, wave_index],
        source_histogram=source_psf[:, :, field_index, wave_index],
        source_pitch_mm=float(source_pitch[0]),
        minimum_retained_fraction=minimum_retained,
    )


def _trace_level(
    node: _PinnedNode, pupil: np.ndarray, chief: np.ndarray, deadline: float,
) -> tuple[np.ndarray, np.ndarray]:
    points: list[np.ndarray] = []
    success = np.zeros(len(pupil), dtype=bool)
    for index, location in enumerate(pupil):
        if time.monotonic() > deadline:
            raise TimeoutError("Geometric pupil-density diagnostic exceeded its soft deadline.")
        try:
            point = _trace(node.model, location, node.field, node.wavelength_nm)
        except Exception as exc:
            if not type(exc).__name__.startswith("Trace"):
                raise
            continue
        success[index] = True
        points.append(np.asarray(point, dtype=float) - chief)
    if not points:
        raise ValueError("No admitted rays at the selected pupil-density level.")
    intersections = np.asarray(points, dtype=float)
    if intersections.shape != (len(points), 2) or not np.isfinite(intersections).all():
        raise ValueError("Retraced pupil intersections are invalid.")
    return intersections, success


def _direct_ray_axis_otf(
    points_mm_xy: np.ndarray, targets: tuple[np.ndarray, np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    # Coordinate y is positive upward, opposite histogram row index.
    row_y = points_mm_xy[:, 1]
    col_x = points_mm_xy[:, 0]
    return (
        np.asarray([np.mean(np.exp(-2j * math.pi * f * row_y)) for f in targets[0]]),
        np.asarray([np.mean(np.exp(-2j * math.pi * f * col_x)) for f in targets[1]]),
    )


def diagnose_pinned_geometric_pupil_density(
    revision_id: str,
    *,
    catalog_root: str | Path,
    field_index: int,
    wavelength_nm: float,
    higher_pupil_samples: Sequence[int],
    target_frequency_cycles_per_mm_row_col: Sequence[Sequence[float]],
    maximum_complex_otf_delta: float = 0.01,
    maximum_admitted_fraction_delta: float = 0.02,
    max_total_requested_pupil_rays: int = 30_000,
    soft_timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Retest a pinned node at two or more independently denser pupil grids.

    A stable dense reference requires two consecutive comparisons *between*
    higher pupil grids within declared tolerance, unchanged fixed source-window
    support, and a bounded difference in admitted pupil fraction.  Only then
    is the pinned source pupil compared directly with the finest stable ray
    reference.  Both decisions concern this geometric ray model alone; neither
    qualifies saved-PSF histogram sampling or camera/system MTF.
    The timeout is soft; callers must provide process-level resource isolation.
    """
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("Choose an explicit pinned Lens revision ID.")
    targets = _targets(target_frequency_cycles_per_mm_row_col)
    tolerance = _positive_number(maximum_complex_otf_delta, "Complex OTF tolerance")
    fraction_tolerance = _positive_number(
        maximum_admitted_fraction_delta, "Admitted-fraction tolerance"
    )
    deadline_seconds = _positive_number(soft_timeout_s, "Soft timeout")
    if tolerance > 0.1 or fraction_tolerance > 0.1 or deadline_seconds > 300:
        raise ValueError("Diagnostic tolerances cannot exceed 10% or timeout 300 s.")
    if (
        type(max_total_requested_pupil_rays) is not int
        or not 1 <= max_total_requested_pupil_rays <= _MAX_TOTAL_RAYS
        or not isinstance(higher_pupil_samples, (list, tuple))
        or not 2 <= len(higher_pupil_samples) <= _MAX_LEVELS - 1
        or any(type(level) is not int or not 3 <= level <= _MAX_PUPIL_SAMPLES
               for level in higher_pupil_samples)
        or any(b <= a for a, b in zip(
            higher_pupil_samples, higher_pupil_samples[1:], strict=False
        ))
    ):
        raise ValueError("Declare two to four increasing bounded higher pupil grids.")
    started = time.monotonic()
    node = _load_pinned_node(revision_id, catalog_root, field_index, wavelength_nm)
    if higher_pupil_samples[0] <= node.source_pupil_samples:
        raise ValueError("Every new pupil grid must be denser than the pinned source.")
    pupil_levels = [node.source_pupil] + [
        _pupil_points(level) for level in higher_pupil_samples
    ]
    counts = [len(pupil) for pupil in pupil_levels]
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "inconclusive",
        "scope": "selected_pinned_geometric_ray_node_only",
        "lens_revision_id": node.revision_id,
        "simulation_id": node.simulation_id,
        "lens_db_sha256": node.lens_db_sha256,
        "source_psf_asset_sha256": node.source_psf_asset_sha256,
        "source_prescription_sha256": node.source_prescription_sha256,
        "builder_generator_sha256": node.builder_generator_sha256,
        "field_index": node.field_index,
        "field_height_mm": node.field_height_mm,
        "wavelength_nm": node.wavelength_nm,
        "fixed_histogram_support_shape_row_col": list(node.source_histogram.shape),
        "fixed_histogram_support_pitch_um": node.source_pitch_mm * 1000,
        "fixed_histogram_minimum_retained_ray_fraction": node.minimum_retained_fraction,
        "target_frequency_cycles_per_mm_row_col": [
            targets[0].tolist(), targets[1].tolist(),
        ],
        "source_pupil_samples": node.source_pupil_samples,
        "higher_pupil_samples": list(higher_pupil_samples),
        "requested_pupil_ray_counts_by_level": counts,
        "total_requested_pupil_rays": sum(counts),
        "max_total_requested_pupil_rays": max_total_requested_pupil_rays,
        "maximum_complex_otf_delta_allowed": tolerance,
        "maximum_admitted_fraction_delta_allowed": fraction_tolerance,
        "soft_timeout_s": deadline_seconds,
        "soft_timeout_is_hard_process_limit": False,
        "required_consecutive_refinement_passes": 2,
        "dense_refinement_baseline": "first_higher_pupil_grid",
        "complex_otf_convention": "mean(exp(-2pi*i*f*y_up_or_x_right))_over_admitted_rays",
        "psf_histogram_interpolation_used_for_otf": False,
        "pinned_source_pupil_density_adequacy_status": (
            "not_evaluated_without_stable_dense_reference"
        ),
        "pinned_source_pupil_density_adequate_at_declared_targets": None,
        "pinned_source_pupil_vs_stable_dense_ray_reference": None,
        "saved_psf_histogram_sampling_qualified": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "steps": [],
    }
    if sum(counts) > max_total_requested_pupil_rays:
        result["status"] = "inconclusive_pupil_ray_budget"
        return result
    deadline = started + deadline_seconds
    try:
        if time.monotonic() > deadline:
            raise TimeoutError("Pinned source loading exceeded the soft deadline.")
        chief = _trace(node.model, [0.0, 0.0], node.field, node.wavelength_nm)
        if not np.allclose(chief, node.saved_chief_mm_xy, rtol=0, atol=1e-9):
            raise ValueError("Retraced chief ray differs from pinned source.")
        previous_otf = None
        previous_admitted_fraction = None
        source_otf = None
        source_admitted_fraction = None
        consecutive_passes = 0
        for index, pupil in enumerate(pupil_levels):
            points, success = _trace_level(node, pupil, chief, deadline)
            admitted_fraction = len(points) / len(pupil)
            histogram, retained = _histogram(
                points, node.source_histogram.shape[0], node.source_pitch_mm,
                min_retained=1e-12,
            )
            if index == 0:
                difference = np.abs(histogram - node.source_histogram)
                if (
                    not np.array_equal(success, node.source_success_mask)
                    or len(points) != node.source_success_count
                    or retained < node.minimum_retained_fraction
                    or float(np.max(difference)) > SOURCE_REPRODUCTION_MAX_ABS_TOLERANCE
                    or float(np.sum(difference)) > SOURCE_REPRODUCTION_L1_TOLERANCE
                ):
                    raise ValueError(
                        "Retraced pinned source pupil does not reproduce saved PSF node."
                    )
            otf = _direct_ray_axis_otf(points, targets)
            step: dict[str, Any] = {
                "pupil_samples": (
                    node.source_pupil_samples if index == 0
                    else higher_pupil_samples[index - 1]
                ),
                "requested_pupil_rays": len(pupil),
                "admitted_pupil_rays": len(points),
                "admitted_pupil_fraction": admitted_fraction,
                "fixed_support_retained_ray_fraction": retained,
                "pupil_coordinates_sha256": _array_sha(pupil, "<f8"),
                "admitted_intersections_xy_mm_sha256": _array_sha(points, "<f8"),
                "centroid_um_row_y_up_col_x_right": [
                    float(np.mean(points[:, 1]) * 1000),
                    float(np.mean(points[:, 0]) * 1000),
                ],
                "complex_otf_row_y_up": [[float(z.real), float(z.imag)] for z in otf[0]],
                "complex_otf_col_x_right": [[float(z.real), float(z.imag)] for z in otf[1]],
            }
            if index == 0:
                step["source_histogram_reproduced"] = True
                step["source_histogram_max_abs_cell_error"] = float(np.max(difference))
                step["source_histogram_l1_mass_error"] = float(np.sum(difference))
                source_otf = otf
                source_admitted_fraction = admitted_fraction
            result["steps"].append(step)
            if retained < node.minimum_retained_fraction:
                result["status"] = "inconclusive_fixed_physical_support"
                return result
            if previous_otf is not None:
                max_delta = max(
                    float(np.max(np.abs(current - previous)))
                    for current, previous in zip(otf, previous_otf, strict=True)
                )
                admitted_delta = abs(admitted_fraction - previous_admitted_fraction)
                passed = max_delta <= tolerance and admitted_delta <= fraction_tolerance
                step["maximum_complex_otf_delta_from_previous"] = max_delta
                step["admitted_pupil_fraction_delta_from_previous"] = admitted_delta
                step["declared_numeric_tolerances_met"] = passed
                step["comparison_role"] = (
                    "source_to_first_higher_grid_baseline" if index == 1
                    else "dense_refinement"
                )
                consecutive_passes = (
                    consecutive_passes + 1 if index >= 2 and passed else 0
                )
                step["consecutive_passes"] = consecutive_passes
                if consecutive_passes == 2:
                    assert source_otf is not None and source_admitted_fraction is not None
                    row_delta = float(np.max(np.abs(otf[0] - source_otf[0])))
                    col_delta = float(np.max(np.abs(otf[1] - source_otf[1])))
                    source_delta = max(row_delta, col_delta)
                    source_admitted_delta = abs(
                        admitted_fraction - source_admitted_fraction
                    )
                    source_adequate = (
                        source_delta <= tolerance
                        and source_admitted_delta <= fraction_tolerance
                    )
                    result["pinned_source_pupil_vs_stable_dense_ray_reference"] = {
                        "source_pupil_samples": node.source_pupil_samples,
                        "dense_reference_pupil_samples": step["pupil_samples"],
                        "maximum_complex_otf_delta_row_y_up": row_delta,
                        "maximum_complex_otf_delta_col_x_right": col_delta,
                        "maximum_complex_otf_delta": source_delta,
                        "admitted_pupil_fraction_delta": source_admitted_delta,
                        "declared_numeric_tolerances_met": source_adequate,
                    }
                    result["pinned_source_pupil_density_adequate_at_declared_targets"] = (
                        source_adequate
                    )
                    result["pinned_source_pupil_density_adequacy_status"] = (
                        "adequate_at_declared_targets_geometric_model_only"
                        if source_adequate else
                        "inadequate_at_declared_targets_geometric_model_only"
                    )
                    result["status"] = (
                        "dense_geometric_pupil_reference_stable_at_declared_targets_model_only"
                    )
                    return result
            else:
                step["consecutive_passes"] = 0
            previous_otf = otf
            previous_admitted_fraction = admitted_fraction
    except TimeoutError:
        result["status"] = "inconclusive_soft_timeout"
        return result
    result["status"] = "inconclusive_refinement_limit"
    return result
