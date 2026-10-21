"""Fail-closed same-physical-Scene grid-refinement study.

An evaluator may use the real Lens x Sensor bridge or a small analytic test
fixture. It must re-render a continuous Scene at every requested grid; a
resized earlier raster does not recover the original optical input. Expanding
the source halo at fixed angular pitch is a separate experiment.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.iso import iso12233

SCHEMA = "camera_lens_sensor_scene_sampling_convergence_v1"


def initial_scene_grid_candidate(
    *, horizontal_fov_deg: float, image_distance_m: float,
    original_psf_pitch_um: float, native_pixel_pitch_um: float,
    minimum_samples: int = 32,
) -> dict[str, Any]:
    """Return a starting experiment only; no pitch ratio proves convergence."""
    values = (
        horizontal_fov_deg, image_distance_m, original_psf_pitch_um,
        native_pixel_pitch_um,
    )
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("FOV, image distance and both physical pitches must be positive.")
    extent_um = 2 * image_distance_m * 1e6 * math.tan(math.radians(horizontal_fov_deg) / 2)
    # A two-samples-per-original-PSF-or-pixel pitch starting point is only a
    # search seed. The independent optical/Scene convergence gates decide.
    desired_pitch = min(original_psf_pitch_um, native_pixel_pitch_um) / 2
    minimum = max(minimum_samples, math.ceil(extent_um / desired_pitch))
    samples = 1 << (minimum - 1).bit_length()
    return {
        "candidate_square_samples_row_col": [samples, samples],
        "starting_pitch_um": extent_um / samples,
        "rule": "two_samples_per_smaller_original_psf_or_native_pixel_pitch_search_seed",
        "universal_sufficiency_claim": False,
        "source_halo_qualified": False,
        "psf_resampling_qualified": False,
    }


def run_scene_sampling_convergence(
    evaluator: Callable[[int, float], dict[str, Any]],
    *, first_samples: int, max_samples: int, subpixel_shift_deg: float,
    mean_electron_relative_tolerance: float,
    edge_mtf50_relative_tolerance: float,
    shift_response_relative_tolerance: float,
) -> dict[str, Any]:
    """Double grid density at fixed FOV until all declared model metrics settle.

    ``evaluator(samples, edge_offset_deg)`` must run noise-off capture of the
    *same* physical analytic Scene at offsets zero and ``subpixel_shift_deg``.
    Returned mean-electron arrays are pre-readout; edge MTF is a declared
    numerical metric on a fixed native ROI. These gates never certify a lens.
    """
    if (
        type(first_samples) is not int or type(max_samples) is not int
        or first_samples < 16 or max_samples < first_samples
        or not math.isfinite(subpixel_shift_deg) or subpixel_shift_deg <= 0
        or any(
            not math.isfinite(value) or value <= 0
            for value in (
                mean_electron_relative_tolerance, edge_mtf50_relative_tolerance,
                shift_response_relative_tolerance,
            )
        )
    ):
        raise ValueError("Declare finite positive refinement bounds, shift and tolerances.")
    samples = first_samples
    previous = None
    consecutive_passes = 0
    steps: list[dict[str, Any]] = []
    attempted_grids: list[list[int]] = []
    invariant = None
    while samples <= max_samples:
        attempted_grids.append([samples, samples])
        captures = [evaluator(samples, offset) for offset in (0.0, subpixel_shift_deg)]
        if any(not isinstance(item, dict) for item in captures):
            raise ValueError("Sampling evaluator must return capture dictionaries.")
        current_invariant = tuple(captures[0].get(key) for key in (
            "source_kind", "scene_horizontal_fov_deg", "lens_identity",
            "sensor_identity", "exposure_s", "roi", "psf_sampling_model",
        ))
        if (
            current_invariant != tuple(captures[1].get(key) for key in (
                "source_kind", "scene_horizontal_fov_deg", "lens_identity",
                "sensor_identity", "exposure_s", "roi", "psf_sampling_model",
            ))
            or any(value is None for value in current_invariant)
        ):
            raise ValueError("Subpixel captures do not share declared physical conditions.")
        if invariant is None:
            invariant = current_invariant
        elif current_invariant != invariant:
            raise ValueError("Grid refinement changed physical Scene, model, ROI or exposure.")
        if captures[0]["source_kind"] != "analytic_continuous":
            return {
                "schema": SCHEMA, "status": "sampling_of_declared_raster_only",
                "steps": steps, "attempted_grids": attempted_grids,
                "physical_mtf_comparison_allowed": False,
            }
        if any(item.get("noise_flag") != 0 for item in captures):
            raise ValueError("Convergence requires noise-off mean-electron captures.")
        pair = [np.asarray(item["mean_electrons"], dtype=float) for item in captures]
        if (
            pair[0].ndim != 2 or pair[0].shape != pair[1].shape
            or any(not np.isfinite(array).all() for array in pair)
        ):
            raise ValueError("Mean-electron arrays must be finite and share a native grid.")
        mtf = [float(item["edge_mtf50"]) for item in captures]
        if any(not math.isfinite(value) or value <= 0 for value in mtf):
            return {
                "schema": SCHEMA, "status": "inconclusive_invalid_edge_metric",
                "steps": steps, "attempted_grids": attempted_grids,
                "reason": "Edge MTF50 must be finite and strictly positive.",
                "physical_mtf_comparison_allowed": False,
            }
        contrast = [
            float(np.percentile(array, 90) - np.percentile(array, 10))
            / max(float(np.max(np.abs(array))), 1e-12)
            for array in pair
        ]
        if min(contrast) < 0.05:
            return {
                "schema": SCHEMA, "status": "inconclusive_invalid_edge_metric",
                "steps": steps, "attempted_grids": attempted_grids,
                "reason": "Native mean-electron edge contrast is below 5%.",
                "physical_mtf_comparison_allowed": False,
            }
        step = {
            "scene_samples_row_col": [samples, samples],
            "scene_horizontal_fov_deg": captures[0]["scene_horizontal_fov_deg"],
            "mean_electrons_pre_readout": True,
            "edge_mtf50_numeric_raw_mosaic_proxy": mtf,
            "edge_estimator": "iso12233_on_pre_readout_Bayer_mosaic_numeric_proxy",
            "normalized_native_edge_contrast": contrast,
            "subpixel_shift_deg": subpixel_shift_deg,
            "source_halo_verified": all(
                item.get("source_halo_verified") is True for item in captures
            ),
            "psf_resampling_verified": all(
                item.get("psf_resampling_verified") is True for item in captures
            ),
        }
        if previous is not None:
            prev_pair, prev_mtf = previous
            scale = max(
                1e-12, float(np.max(np.abs(pair[0]))),
                float(np.max(np.abs(prev_pair[0]))),
            )
            electron_delta = float(np.max(np.abs(pair[0] - prev_pair[0]))) / scale
            shift_delta = float(np.max(np.abs(
                (pair[1] - pair[0]) - (prev_pair[1] - prev_pair[0])
            ))) / scale
            mtf_delta = max(
                abs(mtf[index] - prev_mtf[index]) / max(
                    1e-12, abs(mtf[index]), abs(prev_mtf[index])
                ) for index in (0, 1)
            )
            step["relative_change_from_previous"] = {
                "mean_electrons": electron_delta,
                "edge_mtf50": mtf_delta,
                "subpixel_shift_response": shift_delta,
            }
            numerical_pass = (
                electron_delta <= mean_electron_relative_tolerance
                and mtf_delta <= edge_mtf50_relative_tolerance
                and shift_delta <= shift_response_relative_tolerance
            )
            step["declared_numerical_tolerance_met"] = numerical_pass
            consecutive_passes = consecutive_passes + 1 if numerical_pass else 0
            step["consecutive_passes"] = consecutive_passes
            steps.append(step)
            if consecutive_passes >= 2:
                return {
                    "schema": SCHEMA,
                    "status": "numeric_sampling_converged_input_support_inconclusive",
                    "steps": steps,
                    "attempted_grids": attempted_grids,
                    "tolerances": {
                        "mean_electrons": mean_electron_relative_tolerance,
                        "edge_mtf50": edge_mtf50_relative_tolerance,
                        "subpixel_shift_response": shift_response_relative_tolerance,
                    },
                    "physical_mtf_comparison_allowed": False,
                    "required_consecutive_refinement_passes": 2,
                    "note": (
                        "Caller-declared halo or PSF booleans are recorded diagnostics, "
                        "not independent qualification evidence."
                    ),
                }
        else:
            step["consecutive_passes"] = 0
            steps.append(step)
        previous = (pair, mtf)
        samples *= 2
    return {
        "schema": SCHEMA, "status": "inconclusive_resource_limit",
        "steps": steps, "attempted_grids": attempted_grids,
        "max_samples": max_samples,
        "required_consecutive_refinement_passes": 2,
        "physical_mtf_comparison_allowed": False,
    }


def adaptive_lens_sensor_raw_preview(
    lens_revision_id: str, sensor_identifier: str, *, sensor_run_id: str,
    root: str | Path, workspace: str | Path,
    scene_horizontal_fov_deg: float, sensor_size: tuple[int, int] = (32, 32),
    luminance_cd_m2: float = 1000.0, exposure_s: float = 0.004,
    max_scene_samples: int = 256,
    geometric_psf_sampling: str = "stored_interpolated",
    mean_electron_relative_tolerance: float = 0.01,
    edge_mtf50_relative_tolerance: float = 0.02,
    shift_response_relative_tolerance: float = 0.02,
) -> dict[str, Any]:
    """Run the real bounded RAW bridge at adaptive analytic-Scene resolutions.

    This intentionally remains inside the legacy 256 cap. The halo is not
    qualified for current stored wide PSFs, so even a numerically stable run
    cannot unlock physical MTF comparison. The 1024 experiment has a separate
    process-resource preflight and remains blocked until supervised execution.
    """
    from .lens_catalog_bridge import geometric_lens_preview
    from .lens_sensor_raw_bridge import preview_lens_sensor_raw
    from .sensor_db_bridge import inspect_sensor_revision

    if type(max_scene_samples) is not int or not 32 <= max_scene_samples <= 256:
        raise ValueError("Adaptive in-process RAW is bounded to 32..256 scene samples.")
    if geometric_psf_sampling not in {
        "stored_interpolated", "direct_same_ray_rehistogram",
    }:
        raise ValueError("Adaptive RAW requires a supported geometric PSF sampling method.")
    if sensor_size[0] != sensor_size[1]:
        raise ValueError("Adaptive analytic edge currently requires a square sensor crop.")
    lens = geometric_lens_preview(lens_revision_id, catalog_root=Path(root))
    sensor = inspect_sensor_revision(sensor_identifier, root=Path(root), run_id=sensor_run_id)
    if not sensor["executable"] or sensor["native_response"]["kind"] != "db_estimate_v1":
        raise ValueError("Adaptive RAW requires an executable saved db_estimate_v1 sensor.")
    model = lens["model"]
    pitch_um = float(sensor["native_response"]["pixel_pitch_um"])
    focal_m = float(model["effective_focal_length_mm"]) * 1e-3
    start = initial_scene_grid_candidate(
        horizontal_fov_deg=scene_horizontal_fov_deg,
        image_distance_m=focal_m,
        original_psf_pitch_um=min(model["sample_spacing_mm_row_col"]) * 1000,
        native_pixel_pitch_um=pitch_um,
        minimum_samples=max(32, 2 * sensor_size[0]),
    )
    requested_first = start["candidate_square_samples_row_col"][0]
    first = requested_first
    pitch_seed_unmet = requested_first > max_scene_samples // 2
    if pitch_seed_unmet:
        # Run a clearly under-resolved diagnostic ladder instead of returning
        # an empty study. Never promote it to a qualified optical sampling result.
        first = max(32, 2 * sensor_size[0])
        if first > max_scene_samples:
            return {
                "schema": SCHEMA,
                "status": "inconclusive_resource_limit",
                "attempted_grids": [],
                "starting_candidate": start,
                "starting_pitch_bound_unmet": True,
                "max_scene_samples": max_scene_samples,
                "geometric_psf_sampling": geometric_psf_sampling,
                "physical_mtf_comparison_allowed": False,
            }
    shift_deg = math.degrees(math.atan(pitch_um * 1e-6 / focal_m)) / 2

    def evaluate(samples: int, offset_deg: float) -> dict[str, Any]:
        report = preview_lens_sensor_raw(
            lens_revision_id, sensor_identifier, sensor_run_id=sensor_run_id,
            root=root, workspace=workspace, scene_kind="slanted_edge",
            sensor_size=sensor_size, luminance_cd_m2=luminance_cd_m2,
            exposure_s=exposure_s, noise_flag=0, seed=7,
            accept_approximate_lens_radiometry=True,
            psf_compute_mode="bounded_streaming",
            geometric_psf_sampling=geometric_psf_sampling,
            scene_horizontal_fov_deg=scene_horizontal_fov_deg,
            scene_samples_row_col=(samples, samples),
            record_sampling_evidence=True, include_mean_electron_values=True,
            analytic_slanted_edge_offset_deg=offset_deg,
        )
        electrons = np.asarray(report["mean_electrons"]["values"], dtype=float)
        edge = iso12233(electrons, delta_x=1.0)
        sampling = report["scene"]["sampling_evidence"]
        if sampling["source_kind"] != "analytic_continuous_half_plane":
            raise ValueError("Adaptive RAW did not re-render the analytic edge.")
        return {
            "source_kind": "analytic_continuous",
            "scene_horizontal_fov_deg": report["scene"]["horizontal_fov_deg"],
            "lens_identity": report["lens_identity"],
            "sensor_identity": report["sensor_revision"],
            "exposure_s": report["capture"]["exposure_s"],
            "roi": [0, sensor_size[0], 0, sensor_size[1]],
            "psf_sampling_model": (
                sampling["optical_psf"]["sampling_model"] + ":" + geometric_psf_sampling
            ),
            "noise_flag": 0,
            "mean_electrons": electrons,
            "edge_mtf50": float(edge.mtf50),
            "source_halo_verified": False,
            "psf_resampling_verified": False,
        }

    try:
        study = run_scene_sampling_convergence(
            evaluate, first_samples=first, max_samples=max_scene_samples,
            subpixel_shift_deg=shift_deg,
            mean_electron_relative_tolerance=mean_electron_relative_tolerance,
            edge_mtf50_relative_tolerance=edge_mtf50_relative_tolerance,
            shift_response_relative_tolerance=shift_response_relative_tolerance,
        )
    except (ValueError, MemoryError, OverflowError) as exc:
        study = {
            "schema": SCHEMA,
            "status": "inconclusive_runtime_or_metric_blocked",
            "blocker": str(exc),
            "physical_mtf_comparison_allowed": False,
        }
    study["starting_candidate"] = start
    study["geometric_psf_sampling"] = geometric_psf_sampling
    study["starting_pitch_bound_unmet"] = pitch_seed_unmet
    study["edge_estimator"] = "iso12233_on_pre_readout_Bayer_mosaic_numeric_proxy"
    study["declared_research_tolerances"] = {
        "mean_electrons_relative": mean_electron_relative_tolerance,
        "raw_mosaic_edge_mtf50_relative": edge_mtf50_relative_tolerance,
        "subpixel_shift_response_relative": shift_response_relative_tolerance,
    }
    study["same_physical_scene"] = {
        "source": "continuous_tangent_plane_slanted_half_plane_v1",
        "horizontal_fov_deg": scene_horizontal_fov_deg,
        "exposure_s": exposure_s,
        "luminance_cd_m2": luminance_cd_m2,
        "native_sensor_size": list(sensor_size),
        "controlled_subpixel_shift_deg": shift_deg,
    }
    study["input_halo_qualified"] = False
    study["full_field_psf_resampling_qualified"] = False
    return study
