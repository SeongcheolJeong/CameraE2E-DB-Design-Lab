"""Fixed-grid Lens x Sensor sensitivity to an opt-in dense geometric PSF.

Only the geometric, pre-pixel PSF planes change.  Scene, optical geometry,
radiometry, sensor response and pixel integration stay fixed.  A candidate
must first be loaded and integrity-checked by the dense-PSF asset loader.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

import numpy as np

from .lens_dense_geometric_psf_candidate import SCHEMA as CANDIDATE_SCHEMA
from .lens_geometric_psf_rehistogram import _array_sha
from .lens_sensor_scene_geometry_tile import _optical_state_fingerprint

SCHEMA = "camera_lens_sensor_dense_geometric_psf_fixed_grid_sensitivity_v1"


def optics_with_dense_geometric_psf_candidate(
    source_optics: dict[str, Any],
    candidate_report: Mapping[str, Any],
    candidate_arrays: Mapping[str, np.ndarray],
) -> dict[str, Any]:
    """Overlay verified all-node PSF *shape* while preserving other optics.

    The returned optics deliberately has a new PSF asset identity.  Its other
    pupil/direction and radiometry fields remain pinned to the source, so this
    is a controlled shape-sensitivity model, not a promoted Lens DB revision.
    """
    ray = source_optics.get("raytrace")
    if not isinstance(ray, dict) or source_optics.get("model") != "raytrace":
        raise ValueError("A loaded strict raytrace Lens model is required.")
    source_table = ray.get("psf")
    if (
        ray.get("contract_version") != 2
        or ray.get("psf_kind") != "geometric"
        or ray.get("diffraction_included") is not False
        or not isinstance(source_table, dict)
    ):
        raise ValueError("Only strict pre-pixel geometric Lens PSFs are supported.")
    source_sha = ray.get("asset_sha256")
    candidate_sha = candidate_report.get("candidate_asset_sha256")
    if (
        candidate_report.get("schema") != CANDIDATE_SCHEMA
        or candidate_report.get("status")
        != "all_stored_nodes_candidate_generated_model_only"
        or candidate_report.get("all_stored_field_wavelength_nodes_completed") is not True
        or candidate_report.get("optical_psf_semantics")
        != "geometric_pre_pixel_ray_histogram"
        or candidate_report.get("diffraction_included") is not False
        or candidate_report.get("pixel_aperture_included") is not False
        or candidate_report.get("source_psf_asset_sha256") != source_sha
        or not isinstance(candidate_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", candidate_sha) is None
        or not isinstance(source_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_sha) is None
    ):
        raise ValueError("Candidate lineage or pre-pixel geometric contract differs.")
    for key, report_key in (
        ("field_height_mm", "field_height_mm"),
        ("wavelength_nm", "wavelength_nm"),
        ("sample_spacing_mm", "fixed_physical_histogram_pitch_mm_row_col"),
    ):
        if key not in candidate_arrays or not np.array_equal(
            np.asarray(candidate_arrays[key]),
            np.asarray(source_table[key]),
        ) or not np.array_equal(
            np.asarray(candidate_arrays[key]),
            np.asarray(candidate_report[report_key]),
        ):
            raise ValueError(f"Candidate {key} differs from the pinned Lens PSF.")
    source_function = np.asarray(source_table["function"])
    candidate_function = np.asarray(candidate_arrays.get("psf_function"))
    if (
        source_function.ndim != 4
        or candidate_function.shape != source_function.shape
        or candidate_function.dtype != np.float32
        or list(candidate_function.shape)
        != candidate_report.get("candidate_psf_shape_row_col_field_wave")
        or not np.isfinite(candidate_function).all()
        or np.any(candidate_function < 0)
        or _array_sha(source_function, "<f4")
        != candidate_report.get("source_psf_array_sha256")
        or _array_sha(candidate_function, "<f4")
        != candidate_report.get("candidate_psf_array_sha256")
    ):
        raise ValueError("Candidate PSF shape, dtype or values differ from the source.")
    mass = np.sum(candidate_function, axis=(0, 1), dtype=np.float64)
    if not np.allclose(mass, 1.0, rtol=0, atol=1e-5):
        raise ValueError("Every candidate PSF node must have unit geometric mass.")
    # The verified loader checks the file and embedded array hashes.  Copy the
    # planes so a later caller mutation cannot change an in-flight comparison.
    planes = np.array(candidate_function, copy=True)
    planes.setflags(write=False)
    candidate_table = dict(source_table)
    candidate_table["function"] = planes
    candidate_ray = dict(ray)
    candidate_ray["psf"] = candidate_table
    candidate_ray["asset_sha256"] = candidate_sha
    candidate_ray["source_psf_asset_sha256"] = source_sha
    candidate_ray["geometric_pupil_density_candidate"] = {
        "schema": CANDIDATE_SCHEMA,
        "candidate_asset_sha256": candidate_sha,
        "source_psf_asset_sha256": source_sha,
        "component_scope": "geometric_psf_function_only",
        "source_radiometry_and_direction_fields_retained": True,
        "all_stored_field_wavelength_nodes_completed": True,
        "pixel_aperture_included": False,
        "diffraction_included": False,
        "physical_mtf_comparison_allowed": False,
    }
    result = dict(source_optics)
    result["raytrace"] = candidate_ray
    if _optical_state_fingerprint(result) == _optical_state_fingerprint(source_optics):
        raise ValueError("Candidate did not change the optical state.")
    return result


def compare_fixed_grid_dense_geometric_psf(
    source_optics: dict[str, Any],
    sensor: Any,
    candidate_report: Mapping[str, Any],
    candidate_arrays: Mapping[str, np.ndarray],
    *,
    runner_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Run source and candidate at one identical Scene/OI grid and ROI.

    The caller supplies the registered model's already validated runner
    arguments.  A single grid gives PSF-source sensitivity, never grid
    convergence or a Bayer/system MTF qualification.
    """
    from .lens_sensor_registered_roi_frequency_execution import (
        run_registered_roi_frequency_sampling,
    )

    if runner_kwargs.get("max_refinements") != 0:
        raise ValueError("Dense PSF sensitivity requires one fixed Scene grid.")
    candidate_optics = optics_with_dense_geometric_psf_candidate(
        source_optics, candidate_report, candidate_arrays
    )
    source_before = _optical_state_fingerprint(source_optics)
    candidate_before = _optical_state_fingerprint(candidate_optics)
    source_result, source_means = run_registered_roi_frequency_sampling(
        source_optics, sensor, return_transient_means=True, **runner_kwargs
    )
    candidate_result, candidate_means = run_registered_roi_frequency_sampling(
        candidate_optics, sensor, return_transient_means=True, **runner_kwargs
    )
    if (
        _optical_state_fingerprint(source_optics) != source_before
        or _optical_state_fingerprint(candidate_optics) != candidate_before
    ):
        raise ValueError("An optical model changed during the fixed-grid comparison.")
    source_step = source_result["steps"][-1] if source_result["steps"] else None
    candidate_step = candidate_result["steps"][-1] if candidate_result["steps"] else None
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "inconclusive_fixed_grid_capture_unavailable",
        "source_psf_asset_sha256": candidate_report["source_psf_asset_sha256"],
        "candidate_psf_asset_sha256": candidate_report["candidate_asset_sha256"],
        "source_optical_state_sha256": source_before,
        "candidate_optical_state_sha256": candidate_before,
        "candidate_component_scope": "geometric_psf_function_only",
        "source_radiometry_and_direction_fields_retained": True,
        "source_capture_status": source_result["status"],
        "candidate_capture_status": candidate_result["status"],
        "source_step": source_step,
        "candidate_step": candidate_step,
        "same_scene_and_sensor_inputs": False,
        "scene_sampling_converged": False,
        "pupil_density_converged_all_nodes": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
    }
    if source_means is None or candidate_means is None:
        return result
    if (
        source_result["sensor_identity"] != candidate_result["sensor_identity"]
        or source_result["fixed_physical_input"]["wavelength_nm"]
        != candidate_result["fixed_physical_input"]["wavelength_nm"]
        or source_result["fixed_physical_input"]["bright_side_photons"]
        != candidate_result["fixed_physical_input"]["bright_side_photons"]
        or source_step["source_identity_by_phase"]
        != candidate_step["source_identity_by_phase"]
        or source_step["scene_shape_row_col"] != candidate_step["scene_shape_row_col"]
        or not math.isclose(
            source_step["oi_pitch_um"], candidate_step["oi_pitch_um"],
            rel_tol=0, abs_tol=1e-12,
        )
        or any(a.shape != b.shape for a, b in zip(
            source_means, candidate_means, strict=True
        ))
    ):
        raise ValueError("Candidate and source did not use identical Scene/Sensor inputs.")
    scale = max(
        1e-12,
        *(float(np.max(np.abs(array))) for array in (*source_means, *candidate_means)),
    )
    mean_delta = max(
        float(np.max(np.abs(candidate - source))) / scale
        for source, candidate in zip(source_means, candidate_means, strict=True)
    )
    source_phase = source_means[1] - source_means[0]
    candidate_phase = candidate_means[1] - candidate_means[0]
    phase_delta = float(np.max(np.abs(candidate_phase - source_phase))) / scale
    signal_valid = (
        source_step.get("mean_phase_signal_valid") is True
        and candidate_step.get("mean_phase_signal_valid") is True
    )
    result.update({
        "status": (
            "numeric_dense_geometric_psf_fixed_grid_sensitivity_model_only"
            if signal_valid else "inconclusive_edge_signal_support"
        ),
        "same_scene_and_sensor_inputs": True,
        "edge_signal_valid_for_both_optics": signal_valid,
        "scene_shape_row_col": source_step["scene_shape_row_col"],
        "scene_oi_pitch_um": source_step["oi_pitch_um"],
        "source_mean_electrons_sha256_by_phase": (
            source_step["mean_electrons_sha256_by_phase"]
        ),
        "candidate_mean_electrons_sha256_by_phase": (
            candidate_step["mean_electrons_sha256_by_phase"]
        ),
        "full_scale_electrons_denominator": scale,
        "mean_electron_full_scale_normalized_max_delta": mean_delta,
        "shift_response_full_scale_normalized_max_delta": phase_delta,
        "local_signal_relative_error_assessed": False,
    })
    return result
