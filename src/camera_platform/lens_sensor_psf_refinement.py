"""Target-band diagnostics for independent geometric PSF grid refinement.

The pinned Lens DB contains geometric ray histograms.  Interpolating that
histogram cannot refine its source.  The opt-in pinned-prescription path
retraces and deposits the *same* admitted rays on a finer grid, which can
test histogram-bin sensitivity but cannot establish pupil-ray, diffraction,
spectral, or physical PSF convergence.  All outcomes remain inconclusive for
source-resolution qualification until an independent source refinement exists.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .lens_geometric_psf_rehistogram import (
    DEFAULT_MAX_PSF_BYTES,
    _target_side,
    rehistogram_pinned_geometric_psf,
)

SCHEMA = "camera_lens_sensor_psf_target_band_refinement_v1"
DEFAULT_MAX_ANALYSIS_BYTES = 128 * 1024**2
_PHASE_FLOOR = 1e-10


@dataclass(frozen=True)
class PinnedPSFBandPreparation:
    """One retrace's reusable optics and honest target-band evidence.

    ``optics`` exists only after the pinned source was reproduced.  Its PSF
    pitch must match the subsequently executed OI/convolution pitch exactly;
    an inconclusive source-resolution status does not prohibit a model-only
    comparison capture, but it cannot qualify physical or product MTF.
    """

    optics: dict[str, Any] | None
    evidence: dict[str, Any]


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite and positive.")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return number


def _nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite and nonnegative.")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and nonnegative.")
    return number


def _pitch(value: Sequence[float], name: str) -> tuple[float, float]:
    if len(value) != 2:
        raise ValueError(f"{name} requires row and column pitches.")
    return _positive(value[0], f"{name} row"), _positive(value[1], f"{name} column")


def _frequencies(values: Sequence[float]) -> tuple[float, ...]:
    if not 1 <= len(values) <= 64:
        raise ValueError("Declare 1..64 target-band frequencies.")
    frequencies = tuple(_positive(value, "Target frequency") for value in values)
    if any(high <= low for low, high in zip(frequencies, frequencies[1:], strict=False)):
        raise ValueError("Target-band frequencies must increase strictly.")
    return frequencies


def _directions(values: Sequence[float]) -> tuple[float, ...]:
    if not 1 <= len(values) <= 16:
        raise ValueError("Declare 1..16 image-plane directions.")
    directions = tuple(float(value) for value in values)
    if any(not math.isfinite(value) or not 0 <= value < 180 for value in directions):
        raise ValueError("Directions must be finite degrees in [0, 180).")
    if len(set(directions)) != len(directions):
        raise ValueError("Directions must be distinct.")
    return directions


def _histogram(values: Any, name: str) -> np.ndarray:
    histogram = np.asarray(values)
    if (
        histogram.ndim != 2 or min(histogram.shape) < 3
        or not (
            np.issubdtype(histogram.dtype, np.floating)
            or np.issubdtype(histogram.dtype, np.integer)
        )
        or not np.isfinite(histogram).all() or np.any(histogram < 0)
        or not math.isfinite(float(histogram.sum())) or histogram.sum() <= 0
    ):
        raise ValueError(f"{name} must be a finite nonnegative 2D PSF with positive mass.")
    return histogram


def _mass_fraction(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    number = _nonnegative(value, name)
    if number > 1:
        raise ValueError(f"{name} must be at most one.")
    return number


def _fingerprint(
    histogram: np.ndarray, pitch_um: tuple[float, float],
    frequencies: tuple[float, ...], directions: tuple[float, ...],
) -> dict[str, Any]:
    mass = float(np.sum(histogram, dtype=np.float64))
    normalized = np.asarray(histogram, dtype=np.float64) / mass
    y_um = ((histogram.shape[0] - 1) / 2 - np.arange(histogram.shape[0])) * pitch_um[0]
    x_um = (np.arange(histogram.shape[1]) - (histogram.shape[1] - 1) / 2) * pitch_um[1]
    profile_y = normalized.sum(axis=1)
    profile_x = normalized.sum(axis=0)
    cy = float(profile_y @ y_um)
    cx = float(profile_x @ x_um)
    rms_y = math.sqrt(float(profile_y @ ((y_um - cy) ** 2)))
    rms_x = math.sqrt(float(profile_x @ ((x_um - cx) ** 2)))
    otf = []
    for direction in directions:
        angle = math.radians(direction)
        for frequency in frequencies:
            fy = frequency * math.sin(angle)
            fx = frequency * math.cos(angle)
            # Matrix-vector contraction avoids an O(rows*cols) complex phase plane.
            row_phase = np.exp(-2j * math.pi * fy * y_um * 1e-3)
            col_phase = np.exp(-2j * math.pi * fx * x_um * 1e-3)
            value = complex(row_phase @ (normalized @ col_phase))
            magnitude = abs(value)
            otf.append({
                "direction_deg_from_x_right_toward_y_up": direction,
                "frequency_cycles_per_mm": frequency,
                "frequency_components_cycles_per_mm_y_x": [fy, fx],
                "complex_otf_real": float(value.real),
                "complex_otf_imag": float(value.imag),
                "magnitude": magnitude,
                "phase_rad": float(np.angle(value)) if magnitude > _PHASE_FLOOR else None,
            })
    return {
        "shape_row_col": list(histogram.shape),
        "pitch_um_row_col": list(pitch_um),
        "input_histogram_mass": mass,
        "centroid_um_y_up_x_right": [cy, cx],
        "rms_width_um_y_x": [rms_y, rms_x],
        "histogram_grid_nyquist_cycles_per_mm_row_col": [
            500 / pitch_um[0], 500 / pitch_um[1],
        ],
        "dc_normalized_complex_otf": otf,
    }


def _base_result(
    *, frequencies: tuple[float, ...], directions: tuple[float, ...],
    refinement_method: str, field_height_mm: float | None,
    wavelength_nm: float | None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "inconclusive_psf_source_refinement_unavailable",
        "histogram_grid_stability": "not_assessed",
        "refinement_method": refinement_method,
        "field_height_mm": field_height_mm,
        "wavelength_nm": wavelength_nm,
        "target_frequencies_cycles_per_mm": list(frequencies),
        "directions_deg_from_x_right_toward_y_up": list(directions),
        "frequency_coordinate": "image_plane_cycles_per_mm",
        "psf_semantics": "geometric_pre_pixel_ray_histogram",
        "psf_source_resolution_qualified": False,
        "pupil_ray_density_converged": False,
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
    }


def compare_psf_target_band(
    stored_histogram: Any,
    refined_histogram: Any | None,
    *,
    stored_pitch_um_row_col: Sequence[float],
    refined_pitch_um_row_col: Sequence[float] | None,
    target_frequencies_cycles_per_mm: Sequence[float],
    directions_deg: Sequence[float] = (0.0, 90.0),
    refinement_method: str = "stored_histogram_interpolation",
    field_height_mm: float | None = None,
    wavelength_nm: float | None = None,
    source_retained_ray_fraction: float | None = None,
    refined_retained_ray_fraction: float | None = None,
    admitted_ray_count: int | None = None,
    requested_pupil_ray_count: int | None = None,
    source_reproduction_verified: bool = False,
    same_admitted_intersections_verified: bool = False,
    max_complex_otf_abs_delta: float = 0.01,
    max_centroid_shift_um: float = 0.05,
    max_retained_ray_fraction_delta: float = 0.001,
    minimum_retained_ray_fraction: float = 0.999,
    minimum_admitted_rays: int = 64,
    max_analysis_bytes: int = DEFAULT_MAX_ANALYSIS_BYTES,
) -> dict[str, Any]:
    """Compare finite geometric PSF histograms at fixed physical frequencies.

    `pinned_prescription_retrace_same_rays` may validate bin-grid stability if
    a trusted caller supplies existing pinned-retrace evidence.  The method
    cannot validate the underlying ray population or wavefront.  Stored
    histogram interpolation is never eligible for an independent-grid claim.
    """
    frequencies = _frequencies(target_frequencies_cycles_per_mm)
    directions = _directions(directions_deg)
    if len(frequencies) * len(directions) > 64:
        raise ValueError("Limit target-band direction/frequency points to 64.")
    source_pitch = _pitch(stored_pitch_um_row_col, "Stored PSF pitch")
    if refinement_method not in {
        "stored_histogram_interpolation", "pinned_prescription_retrace_same_rays",
    }:
        raise ValueError("Unsupported PSF refinement method.")
    if field_height_mm is not None and (
        not math.isfinite(float(field_height_mm)) or float(field_height_mm) < 0
    ):
        raise ValueError("Field height must be finite and nonnegative.")
    if wavelength_nm is not None:
        _positive(wavelength_nm, "Wavelength")
    otf_tol = _nonnegative(max_complex_otf_abs_delta, "Complex OTF tolerance")
    centroid_tol = _nonnegative(max_centroid_shift_um, "Centroid tolerance")
    retained_tol = _nonnegative(
        max_retained_ray_fraction_delta, "Retained-fraction tolerance"
    )
    min_retained = _mass_fraction(
        minimum_retained_ray_fraction, "Minimum retained-ray fraction"
    )
    if min_retained is None:
        raise ValueError("Minimum retained-ray fraction is required.")
    if type(minimum_admitted_rays) is not int or minimum_admitted_rays < 1:
        raise ValueError("Minimum admitted rays must be a positive integer.")
    if type(max_analysis_bytes) is not int or not 1024 <= max_analysis_bytes <= 512*1024**2:
        raise ValueError("Analysis byte cap must be within [1024, 512 MiB].")

    result = _base_result(
        frequencies=frequencies, directions=directions,
        refinement_method=refinement_method,
        field_height_mm=field_height_mm, wavelength_nm=wavelength_nm,
    )
    source = _histogram(stored_histogram, "Stored PSF")
    if source.nbytes + source.size * 16 > max_analysis_bytes:
        result["status"] = "inconclusive_psf_analysis_resource_limit"
        result["resource_limit"] = {
            "stored_histogram_bytes": source.nbytes,
            "estimated_analysis_working_bytes": source.nbytes + source.size * 16,
            "max_analysis_bytes": max_analysis_bytes,
        }
        return result
    if refined_histogram is None:
        return result
    if refined_pitch_um_row_col is None:
        raise ValueError("Refined PSF pitch is required with a refined histogram.")
    refined_pitch = _pitch(refined_pitch_um_row_col, "Refined PSF pitch")
    refined = _histogram(refined_histogram, "Refined PSF")
    estimated_working_bytes = (
        source.nbytes + refined.nbytes + 16 * max(source.size, refined.size)
    )
    if estimated_working_bytes > max_analysis_bytes:
        result["status"] = "inconclusive_psf_analysis_resource_limit"
        result["resource_limit"] = {
            "stored_histogram_bytes": source.nbytes,
            "refined_histogram_bytes": refined.nbytes,
            "estimated_analysis_working_bytes": estimated_working_bytes,
            "max_analysis_bytes": max_analysis_bytes,
        }
        return result
    result["resource_use"] = {
        "stored_histogram_bytes": source.nbytes,
        "refined_histogram_bytes": refined.nbytes,
        "estimated_analysis_working_bytes": estimated_working_bytes,
        "max_analysis_bytes": max_analysis_bytes,
        "maximum_direction_frequency_points": 64,
    }
    if any(refined_pitch[index] >= source_pitch[index] for index in (0, 1)):
        result["status"] = "inconclusive_no_finer_psf_grid"
        return result

    for direction in directions:
        angle = math.radians(direction)
        for frequency in frequencies:
            fy, fx = frequency * math.sin(angle), frequency * math.cos(angle)
            if any(
                abs(component) >= 500 / pitch[index]
                for pitch in (source_pitch, refined_pitch)
                for index, component in ((0, fy), (1, fx))
            ):
                result["status"] = "inconclusive_target_outside_psf_grid_band"
                return result

    source_energy = _mass_fraction(source_retained_ray_fraction, "Stored retained-ray fraction")
    refined_energy = _mass_fraction(
        refined_retained_ray_fraction, "Refined retained-ray fraction"
    )
    if admitted_ray_count is not None and (
        type(admitted_ray_count) is not int or admitted_ray_count < 1
    ):
        raise ValueError("Admitted ray count must be a positive integer.")
    if requested_pupil_ray_count is not None and (
        type(requested_pupil_ray_count) is not int or requested_pupil_ray_count < 1
    ):
        raise ValueError("Requested pupil ray count must be a positive integer.")
    if (
        admitted_ray_count is not None and requested_pupil_ray_count is not None
        and admitted_ray_count > requested_pupil_ray_count
    ):
        raise ValueError("Admitted ray count exceeds requested pupil rays.")
    result["ray_support"] = {
        "source_retained_ray_fraction": source_energy,
        "refined_retained_ray_fraction": refined_energy,
        "admitted_ray_count": admitted_ray_count,
        "requested_pupil_ray_count": requested_pupil_ray_count,
        "minimum_admitted_rays": minimum_admitted_rays,
        "minimum_retained_ray_fraction": min_retained,
        "same_admitted_intersections_verified": same_admitted_intersections_verified,
        "source_reproduction_verified": source_reproduction_verified,
    }

    source_diagnostic = _fingerprint(source, source_pitch, frequencies, directions)
    refined_diagnostic = _fingerprint(refined, refined_pitch, frequencies, directions)
    source_otf = source_diagnostic["dc_normalized_complex_otf"]
    refined_otf = refined_diagnostic["dc_normalized_complex_otf"]
    complex_deltas = []
    magnitude_deltas = []
    phase_deltas = []
    for coarse, fine in zip(source_otf, refined_otf, strict=True):
        first = complex(coarse["complex_otf_real"], coarse["complex_otf_imag"])
        second = complex(fine["complex_otf_real"], fine["complex_otf_imag"])
        complex_deltas.append(abs(second - first))
        magnitude_deltas.append(abs(fine["magnitude"] - coarse["magnitude"]))
        if coarse["phase_rad"] is not None and fine["phase_rad"] is not None:
            raw = fine["phase_rad"] - coarse["phase_rad"]
            phase_deltas.append(abs(math.atan2(math.sin(raw), math.cos(raw))))
    centroid_shift = math.dist(
        source_diagnostic["centroid_um_y_up_x_right"],
        refined_diagnostic["centroid_um_y_up_x_right"],
    )
    energy_delta = (
        abs(refined_energy - source_energy)
        if source_energy is not None and refined_energy is not None else None
    )
    result.update({
        "stored_histogram": source_diagnostic,
        "refined_histogram": refined_diagnostic,
        "deltas": {
            "max_complex_otf_abs": max(complex_deltas),
            "max_mtf_magnitude_abs": max(magnitude_deltas),
            "max_wrapped_phase_rad": max(phase_deltas) if phase_deltas else None,
            "phase_defined_comparison_count": len(phase_deltas),
            "centroid_shift_um": centroid_shift,
            "retained_ray_fraction_abs": energy_delta,
        },
        "tolerances": {
            "max_complex_otf_abs": otf_tol,
            "max_centroid_shift_um": centroid_tol,
            "max_retained_ray_fraction_abs": retained_tol,
        },
        "otf_convention": "sum(normalized_bin_mass * exp(-2pi*i*(fx*x_right+fy*y_up)))",
        "histogram_bins_treated_as_point_mass_at_centers": True,
    })
    if refinement_method == "stored_histogram_interpolation":
        result["status"] = "inconclusive_stored_histogram_interpolation_only"
        return result
    if not source_reproduction_verified or not same_admitted_intersections_verified:
        result["status"] = "inconclusive_pinned_retrace_provenance_unverified"
        return result
    if (
        source_energy is None or refined_energy is None
        or admitted_ray_count is None or requested_pupil_ray_count is None
    ):
        result["status"] = "inconclusive_ray_support_unavailable"
        return result
    if (
        admitted_ray_count < minimum_admitted_rays
        or min(source_energy, refined_energy) < min_retained
    ):
        result["status"] = "inconclusive_ray_or_energy_support_limit"
        return result
    passed = (
        max(complex_deltas) <= otf_tol
        and centroid_shift <= centroid_tol
        and energy_delta is not None and energy_delta <= retained_tol
    )
    result["histogram_grid_stability"] = (
        "within_declared_tolerances" if passed else "difference_above_declared_tolerances"
    )
    result["status"] = (
        "inconclusive_psf_source_refinement_unavailable" if passed
        else "inconclusive_histogram_grid_difference"
    )
    result["independent_histogram_grid_comparison_executed"] = True
    result["same_ray_retrace_is_independent_source_refinement"] = False
    return result


def prepare_pinned_geometric_psf_target_band(
    revision_id: str,
    *,
    catalog_root: str | Path,
    target_pitch_um: float,
    field_height_mm: float,
    wavelength_nm: float,
    target_frequencies_cycles_per_mm: Sequence[float],
    directions_deg: Sequence[float] = (0.0, 90.0),
    max_total_psf_bytes: int = DEFAULT_MAX_PSF_BYTES,
    max_pupil_rays_per_node: int = 5000,
    soft_timeout_s: float = 120.0,
    max_analysis_bytes: int = DEFAULT_MAX_ANALYSIS_BYTES,
    **comparison_tolerances: Any,
) -> PinnedPSFBandPreparation:
    """Retrace a pinned prescription, then compare one exact field/band node.

    The underlying re-histogram function reproduces all pinned field/wavelength
    nodes and their success masks before returning.  This wrapper keeps the
    result model-only and inconclusive for source-ray convergence.
    """
    frequencies = _frequencies(target_frequencies_cycles_per_mm)
    directions = _directions(directions_deg)
    target_pitch = _positive(target_pitch_um, "Target PSF pitch")
    field = _nonnegative(field_height_mm, "Field height")
    wave = _positive(wavelength_nm, "Wavelength")
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("Declare a pinned lens revision ID.")
    if type(max_total_psf_bytes) is not int or not 1 <= max_total_psf_bytes <= 512*1024**2:
        raise ValueError("PSF output byte cap must be within [1, 512 MiB].")
    if type(max_analysis_bytes) is not int or not 1024 <= max_analysis_bytes <= 512*1024**2:
        raise ValueError("Analysis byte cap must be within [1024, 512 MiB].")
    if type(max_pupil_rays_per_node) is not int or not 1 <= max_pupil_rays_per_node <= 5000:
        raise ValueError("Pupil ray cap must be within [1, 5000].")
    if not 0 < _positive(soft_timeout_s, "Soft timeout") <= 300:
        raise ValueError("Soft timeout must be at most 300 seconds.")
    report = _base_result(
        frequencies=frequencies, directions=directions,
        refinement_method="pinned_prescription_retrace_same_rays",
        field_height_mm=field, wavelength_nm=wave,
    )
    report["lens_revision_id"] = revision_id

    # Read the pinned asset axes and shape before spending time on retracing.
    from .lens_catalog_bridge import LensCatalogBridgeError, _asset, _revision_source

    allowed_tolerances = {
        "max_complex_otf_abs_delta", "max_centroid_shift_um",
        "max_retained_ray_fraction_delta", "minimum_retained_ray_fraction",
        "minimum_admitted_rays",
    }
    unknown = set(comparison_tolerances) - allowed_tolerances
    if unknown:
        raise ValueError(f"Unsupported PSF comparison settings: {sorted(unknown)}")

    root = Path(catalog_root).expanduser().resolve()
    try:
        revision, snapshot, db_path, _db_sha = _revision_source(revision_id, root)
        _simulation_id, asset_path, _validation, _artifact = _asset(
            revision, snapshot, db_path, root
        )
    except LensCatalogBridgeError as exc:
        report["status"] = "inconclusive_pinned_psf_asset_unavailable"
        report["reason_code"] = exc.code
        report["reason"] = str(exc)
        return PinnedPSFBandPreparation(optics=None, evidence=report)
    with np.load(asset_path, allow_pickle=False) as asset:
        fields = np.asarray(asset["field_height_mm"], dtype=float)
        wavelengths = np.asarray(asset["wavelength_nm"], dtype=float)
        source_pitch_mm = np.asarray(asset["sample_spacing_mm"], dtype=float)
        source_psf = np.asarray(asset["psf_function"], dtype=np.float32)
        pupil_count = len(asset["pupil_coordinates"])
    if pupil_count > max_pupil_rays_per_node:
        report["status"] = "inconclusive_pupil_ray_resource_limit"
        report["requested_pupil_ray_count"] = pupil_count
        report["max_pupil_rays_per_node"] = max_pupil_rays_per_node
        return PinnedPSFBandPreparation(optics=None, evidence=report)
    field_indices = np.flatnonzero(np.isclose(fields, field, rtol=0, atol=1e-9))
    wave_indices = np.flatnonzero(np.isclose(wavelengths, wave, rtol=0, atol=1e-9))
    if len(field_indices) != 1 or len(wave_indices) != 1:
        report["status"] = "inconclusive_requested_field_or_wavelength_not_stored"
        report["available_field_heights_mm"] = fields.tolist()
        report["available_wavelengths_nm"] = wavelengths.tolist()
        return PinnedPSFBandPreparation(optics=None, evidence=report)
    source_pitch_um = [float(value * 1000) for value in source_pitch_mm]
    if target_pitch >= min(source_pitch_um):
        report["status"] = "inconclusive_no_finer_psf_grid"
        report["stored_psf_pitch_um_row_col"] = source_pitch_um
        return PinnedPSFBandPreparation(optics=None, evidence=report)
    try:
        target_side = _target_side(
            source_psf.shape[0], float(source_pitch_mm[0]), target_pitch * 1e-3
        )
    except ValueError as exc:
        report["status"] = "inconclusive_psf_grid_support_limit"
        report["reason"] = str(exc)
        return PinnedPSFBandPreparation(optics=None, evidence=report)
    estimated_output_bytes = (
        target_side**2 * len(fields) * len(wavelengths) * np.dtype("<f4").itemsize
    )
    report["preflight"] = {
        "target_grid_shape_row_col": [target_side, target_side],
        "estimated_total_psf_output_bytes": estimated_output_bytes,
        "max_total_psf_bytes": max_total_psf_bytes,
        "max_pupil_rays_per_node": max_pupil_rays_per_node,
        "soft_timeout_s": soft_timeout_s,
        "hard_process_rss_or_time_guard": False,
    }
    if estimated_output_bytes > max_total_psf_bytes:
        report["status"] = "inconclusive_psf_output_resource_limit"
        return PinnedPSFBandPreparation(optics=None, evidence=report)
    selected_source = source_psf[:, :, int(field_indices[0]), int(wave_indices[0])]
    estimated_analysis_working = (
        selected_source.nbytes + target_side**2 * 4
        + 16 * max(selected_source.size, target_side**2)
    )
    if estimated_analysis_working > max_analysis_bytes:
        report["status"] = "inconclusive_psf_analysis_resource_limit"
        report["estimated_analysis_working_bytes"] = estimated_analysis_working
        return PinnedPSFBandPreparation(optics=None, evidence=report)
    try:
        retraced = rehistogram_pinned_geometric_psf(
            revision_id, catalog_root=root, convolution_pitch_um=target_pitch,
            max_total_psf_bytes=max_total_psf_bytes,
            max_pupil_rays_per_node=max_pupil_rays_per_node,
            soft_timeout_s=soft_timeout_s,
        )
    except TimeoutError as exc:
        report["status"] = "inconclusive_psf_retrace_soft_timeout"
        report["reason"] = str(exc)
        return PinnedPSFBandPreparation(optics=None, evidence=report)
    except ImportError as exc:
        report["status"] = "inconclusive_psf_solver_unavailable"
        report["reason"] = str(exc)
        return PinnedPSFBandPreparation(optics=None, evidence=report)
    provenance = retraced.provenance
    nodes = [node for node in provenance["node_reports"] if (
        node["field_index"] == int(field_indices[0])
        and math.isclose(node["wavelength_nm"], wave, rel_tol=0, abs_tol=1e-9)
    )]
    if len(nodes) != 1:
        raise ValueError("Pinned retrace omitted the requested field/wavelength node.")
    node = nodes[0]
    refined_psf = np.asarray(
        retraced.optics["raytrace"]["psf"]["function"]
    )[:, :, int(field_indices[0]), int(wave_indices[0])]
    comparison = compare_psf_target_band(
        selected_source, refined_psf,
        stored_pitch_um_row_col=source_pitch_um,
        refined_pitch_um_row_col=[target_pitch, target_pitch],
        target_frequencies_cycles_per_mm=frequencies,
        directions_deg=directions,
        refinement_method="pinned_prescription_retrace_same_rays",
        field_height_mm=field, wavelength_nm=wave,
        source_retained_ray_fraction=node["source_grid_retained_ray_fraction"],
        refined_retained_ray_fraction=node["target_grid_retained_ray_fraction"],
        admitted_ray_count=node["admitted_ray_count"],
        requested_pupil_ray_count=node["requested_pupil_ray_count"],
        source_reproduction_verified=(
            provenance["worst_source_grid_max_abs_cell_error"]
            <= provenance["source_grid_reproduction_tolerance_max_abs"]
            and provenance["worst_source_grid_l1_mass_error"]
            <= provenance["source_grid_reproduction_tolerance_l1"]
        ),
        same_admitted_intersections_verified=(
            provenance["same_admitted_intersections_source_and_target"] is True
            and provenance["all_stored_field_wavelength_nodes_retraced"] is True
        ),
        max_analysis_bytes=max_analysis_bytes,
        **comparison_tolerances,
    )
    comparison["lens_revision_id"] = revision_id
    comparison["source_psf_asset_sha256"] = provenance["source_psf_asset_sha256"]
    comparison["derived_psf_signature_sha256"] = provenance[
        "derived_psf_signature_sha256"
    ]
    comparison["admitted_intersections_xy_mm_sha256"] = node[
        "admitted_intersections_xy_mm_sha256"
    ]
    comparison["preflight"] = report["preflight"]
    comparison["retrace_elapsed_seconds"] = provenance["elapsed_seconds"]
    return PinnedPSFBandPreparation(optics=retraced.optics, evidence=comparison)


def qualify_pinned_geometric_psf_target_band(
    revision_id: str,
    *,
    catalog_root: str | Path,
    target_pitch_um: float,
    field_height_mm: float,
    wavelength_nm: float,
    target_frequencies_cycles_per_mm: Sequence[float],
    directions_deg: Sequence[float] = (0.0, 90.0),
    max_total_psf_bytes: int = DEFAULT_MAX_PSF_BYTES,
    max_pupil_rays_per_node: int = 5000,
    soft_timeout_s: float = 120.0,
    max_analysis_bytes: int = DEFAULT_MAX_ANALYSIS_BYTES,
    **comparison_tolerances: Any,
) -> dict[str, Any]:
    """Return evidence only for callers that do not execute the derived optics."""
    return prepare_pinned_geometric_psf_target_band(
        revision_id,
        catalog_root=catalog_root,
        target_pitch_um=target_pitch_um,
        field_height_mm=field_height_mm,
        wavelength_nm=wavelength_nm,
        target_frequencies_cycles_per_mm=target_frequencies_cycles_per_mm,
        directions_deg=directions_deg,
        max_total_psf_bytes=max_total_psf_bytes,
        max_pupil_rays_per_node=max_pupil_rays_per_node,
        soft_timeout_s=soft_timeout_s,
        max_analysis_bytes=max_analysis_bytes,
        **comparison_tolerances,
    ).evidence
