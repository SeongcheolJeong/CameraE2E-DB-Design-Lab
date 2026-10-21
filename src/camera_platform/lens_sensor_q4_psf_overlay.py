"""Opt-in q4 geometric PSF overlay for one strict CameraE2E raytrace Lens.

The caller must first use the owner-bound q4 Job ``load_verified_rebin``.
This second boundary checks that the loaded array belongs to the *source*
optics supplied for capture. Only the pre-pixel PSF and its physical sample
spacing are replaced; no radiometry, geometry, direction, or Lens revision is
qualified by this operation.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

import numpy as np

from .lens_dense_geometric_psf_q4_rebin import (
    MAX_OUTPUT_PSF_BYTES,
    REFINEMENT_FACTOR,
)
from .lens_dense_geometric_psf_q4_rebin import (
    SCHEMA as Q4_SCHEMA,
)
from .lens_dense_geometric_psf_q4_rebin import (
    STATUS as Q4_STATUS,
)
from .lens_geometric_psf_rehistogram import _array_sha


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _axes(value: Any, count: int) -> np.ndarray:
    result = np.asarray(value, dtype=float)
    if result.shape != (count,) or not np.isfinite(result).all():
        raise ValueError("q4 PSF axes or pitch are invalid.")
    return result


def optics_with_q4_geometric_psf_candidate(
    source_optics: dict[str, Any],
    candidate_report: Mapping[str, Any],
    candidate_arrays: Mapping[str, np.ndarray],
) -> dict[str, Any]:
    """Return strict raytrace optics with a separately identified q4 PSF.

    ``candidate_report`` and ``candidate_arrays`` must come from the q4 Job's
    owner-bound ``load_verified_rebin``. This function does not authenticate a
    Run itself. It rejects a wrong source or numerical grid before an optics
    object can be used by CameraE2E.
    """
    ray = source_optics.get("raytrace")
    if source_optics.get("model") != "raytrace" or not isinstance(ray, dict):
        raise ValueError("A loaded strict raytrace Lens model is required.")
    source_table = ray.get("psf")
    if (
        ray.get("contract_version") != 2
        or ray.get("physics_policy") != "strict"
        or ray.get("psf_kind") != "geometric"
        or ray.get("diffraction_included") is not False
        or not isinstance(source_table, dict)
        or source_table.get("coordinate_convention") != "pixel_centers"
    ):
        raise ValueError("Only strict pre-pixel geometric Lens PSFs are supported.")

    source_sha = ray.get("asset_sha256")
    candidate_sha = candidate_report.get("candidate_asset_sha256")
    parent_sha = candidate_report.get("parent_candidate_asset_sha256")
    parent_run_id = candidate_report.get("parent_candidate_run_id")
    source_identity = source_optics.get("lens_patent_raytrace_psf")
    if (
        not _sha(source_sha) or not _sha(candidate_sha) or candidate_sha == source_sha
        or not _sha(parent_sha) or not isinstance(parent_run_id, str)
        or re.fullmatch(r"run-[0-9a-f]{32}", parent_run_id) is None
        or not _sha(ray.get("source_prescription_sha256"))
        or not _sha(candidate_report.get("lens_revision_content_sha256"))
        or not _sha(candidate_report.get("lens_db_sha256"))
        or not _sha(candidate_report.get("parent_candidate_psf_array_sha256"))
        or not _sha(candidate_report.get("parent_dense_intersections_array_sha256"))
        or candidate_report.get("schema") != Q4_SCHEMA
        or candidate_report.get("status") != Q4_STATUS
        or candidate_report.get("source_psf_asset_sha256") != source_sha
        or candidate_report.get("source_prescription_sha256")
        != ray.get("source_prescription_sha256")
        or not isinstance(source_identity, Mapping)
        or source_identity.get("simulation_id") != candidate_report.get("simulation_id")
        or candidate_report.get("same_parent_dense_rays_reused_without_raytrace") is not True
        or candidate_report.get("all_stored_field_wavelength_nodes_completed") is not True
        or candidate_report.get("histogram_refinement_factor") != REFINEMENT_FACTOR
        or candidate_report.get("optical_psf_semantics")
        != "geometric_pre_pixel_ray_histogram"
        or candidate_report.get("diffraction_included") is not False
        or candidate_report.get("pixel_aperture_included") is not False
        or candidate_report.get("pupil_density_convergence_qualified_all_nodes") is not False
        or candidate_report.get("scene_or_executed_convolution_sampling_qualified") is not False
        or candidate_report.get("lens_radiometry_validated") is not False
        or candidate_report.get("system_mtf_or_product_ranking_allowed") is not False
    ):
        raise ValueError("q4 candidate/source or pinned parent lineage differs.")
    tolerance = candidate_report.get("maximum_histogram_complex_otf_abs_delta_allowed")
    worst = candidate_report.get("q4_worst_complex_otf_abs_gap")
    if (
        type(tolerance) not in (int, float) or type(worst) not in (int, float)
        or not math.isfinite(float(tolerance)) or not math.isfinite(float(worst))
        or not 0 <= float(worst) <= float(tolerance) <= 0.1
        or candidate_report.get("histogram_gap_within_declared_tolerance_model_only") is not True
        or candidate_report.get("histogram_target_status")
        != "numeric_gap_within_declared_tolerance_model_only"
    ):
        raise ValueError("q4 numerical histogram gap did not meet its declared target.")

    source_function = np.asarray(source_table.get("function"))
    candidate_function = np.asarray(candidate_arrays.get("psf_function"))
    if (
        source_function.ndim != 4 or source_function.shape[0] != source_function.shape[1]
        or not 8 <= source_function.shape[0] <= 512
        or source_function.dtype.kind not in "fiu"
        or not np.isfinite(source_function).all() or np.any(source_function < 0)
        or list(source_function.shape)
        != candidate_report.get("source_psf_shape_row_col_field_wave")
        or _array_sha(source_function, "<f4")
        != candidate_report.get("source_psf_array_sha256")
    ):
        raise ValueError("Source Lens PSF array differs from pinned q4 lineage.")
    fields = _axes(source_table.get("field_height_mm"), source_function.shape[2])
    waves = _axes(source_table.get("wavelength_nm"), source_function.shape[3])
    source_pitch = _axes(source_table.get("sample_spacing_mm"), 2)
    if (
        np.any(source_pitch <= 0)
        or not np.isclose(source_pitch[0], source_pitch[1], rtol=0, atol=1e-15)
        or not np.array_equal(fields, candidate_report.get("field_height_mm"))
        or not np.array_equal(waves, candidate_report.get("wavelength_nm"))
        or not np.array_equal(
            source_pitch, candidate_report.get("source_histogram_pitch_mm_row_col")
        )
        or not np.array_equal(
            source_pitch, candidate_arrays.get("source_sample_spacing_mm")
        )
        or not np.array_equal(fields, candidate_arrays.get("field_height_mm"))
        or not np.array_equal(waves, candidate_arrays.get("wavelength_nm"))
    ):
        raise ValueError("q4 source pitch, field or wavelength differs from Lens PSF.")

    expected_shape = (
        (source_function.shape[0] - 1) * REFINEMENT_FACTOR + 1,
        (source_function.shape[1] - 1) * REFINEMENT_FACTOR + 1,
        *source_function.shape[2:],
    )
    expected_parent_shape = (
        (source_function.shape[0] - 1) * 2 + 1,
        (source_function.shape[1] - 1) * 2 + 1,
        *source_function.shape[2:],
    )
    candidate_pitch = _axes(candidate_arrays.get("sample_spacing_mm"), 2)
    parent_pitch = _axes(candidate_report.get("parent_q2_histogram_pitch_mm_row_col"), 2)
    declared_support = _axes(candidate_report.get("physical_sample_center_support_mm_row_col"), 2)
    support = (np.asarray(source_function.shape[:2]) - 1) * source_pitch
    if (
        candidate_function.shape != expected_shape
        or candidate_function.dtype != np.float32
        or candidate_function.nbytes > MAX_OUTPUT_PSF_BYTES
        or list(expected_shape) != candidate_report.get("candidate_psf_shape_row_col_field_wave")
        or list(expected_parent_shape)
        != candidate_report.get("parent_q2_psf_shape_row_col_field_wave")
        or not np.allclose(candidate_pitch, source_pitch / REFINEMENT_FACTOR,
                           rtol=0, atol=1e-15)
        or not np.array_equal(
            candidate_pitch, candidate_report.get("candidate_histogram_pitch_mm_row_col")
        )
        or not np.allclose(parent_pitch, source_pitch / 2, rtol=0, atol=1e-15)
        or not np.allclose(declared_support, support, rtol=0, atol=1e-12)
        or not np.allclose((np.asarray(expected_shape[:2]) - 1) * candidate_pitch,
                           support, rtol=0, atol=1e-12)
        or not np.isfinite(candidate_function).all()
        or np.any(candidate_function < 0)
        or _array_sha(candidate_function, "<f4")
        != candidate_report.get("candidate_psf_array_sha256")
        or not np.allclose(
            np.sum(candidate_function, axis=(0, 1), dtype=np.float64),
            1.0, rtol=0, atol=1e-5,
        )
        or candidate_report.get("node_count")
        != source_function.shape[2] * source_function.shape[3]
        or candidate_report.get("output_psf_float32_bytes") != candidate_function.nbytes
    ):
        raise ValueError("q4 PSF array, pitch, physical support or mass differs.")

    planes = np.array(candidate_function, copy=True)
    planes.setflags(write=False)
    pitch = np.array(candidate_pitch, copy=True)
    pitch.setflags(write=False)
    candidate_table = {**source_table, "function": planes, "sample_spacing_mm": pitch}
    candidate_ray = {
        **ray,
        "psf": candidate_table,
        "asset_sha256": candidate_sha,
        "source_psf_asset_sha256": source_sha,
        "q4_geometric_psf_candidate": {
            "schema": Q4_SCHEMA,
            "candidate_asset_sha256": candidate_sha,
            "parent_candidate_run_id": parent_run_id,
            "parent_candidate_asset_sha256": parent_sha,
            "source_psf_asset_sha256": source_sha,
            "component_scope": "geometric_psf_function_and_sample_spacing_only",
            "source_geometry_radiometry_and_direction_fields_retained": True,
            "histogram_gap_within_declared_tolerance_model_only": True,
            "diffraction_included": False,
            "pixel_aperture_included": False,
            "pupil_density_convergence_qualified_all_nodes": False,
            "scene_or_executed_convolution_sampling_qualified": False,
            "physical_mtf_comparison_allowed": False,
            "product_ranking_allowed": False,
        },
    }
    return {**source_optics, "raytrace": candidate_ray}
