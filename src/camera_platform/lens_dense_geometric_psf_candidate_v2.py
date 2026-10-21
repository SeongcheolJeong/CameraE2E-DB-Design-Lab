"""Opt-in dense geometric PSF at an explicitly finer physical histogram pitch.

This is a numerical ray/histogram diagnostic.  It never replaces the pinned
Lens DB asset, and agreement with admitted rays does not establish diffraction,
pixel-aperture, camera MTF, or product accuracy.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from .lens_dense_geometric_psf_candidate import (
    DEFAULT_MAX_OUTPUT_PSF_BYTES,
    MAX_WORKING_ARRAY_BYTES,
    SOURCE_DIRECTION_REPRODUCTION_MAX_ABS_TOLERANCE,
    _file_sha,
    _read_source,
    _trace_point_direction,
    _trace_pupil,
    _validate_request,
)
from .lens_geometric_psf_rehistogram import (
    SOURCE_REPRODUCTION_L1_TOLERANCE,
    SOURCE_REPRODUCTION_MAX_ABS_TOLERANCE,
    _array_sha,
    _histogram,
    _pupil_points,
)
from .lens_geometric_pupil_density_diagnostic import (
    _direct_ray_axis_otf,
    _load_pinned_node,
    _targets,
)
from .lens_sensor_psf_frequency_diagnostic import psf_axis_target_frequency_diagnostic

SCHEMA = "camera_pinned_dense_geometric_psf_asset_candidate_v2"
STATUS = "all_stored_nodes_fine_histogram_candidate_generated_model_only"
REFINEMENT_FACTOR = 2
MAXIMUM_HISTOGRAM_OTF_TOLERANCE = 0.1


def _complex_pairs(values: np.ndarray) -> list[list[float]]:
    return [[float(z.real), float(z.imag)] for z in values]


def _otf_pairs(
    histogram: np.ndarray, pitch_mm: float, targets: tuple[np.ndarray, np.ndarray],
) -> tuple[list[list[float]], list[list[float]]]:
    result = psf_axis_target_frequency_diagnostic(
        histogram,
        pitch_um_row_col=(pitch_mm * 1000, pitch_mm * 1000),
        target_frequency_cycles_per_mm_row_col=targets,
    )
    if result["status"] != "numeric_supplied_histogram_axis_otf_only":
        raise ValueError("Declared frequency exceeds the source or target histogram Nyquist.")
    axes = result["axis_otf"]
    return tuple(  # type: ignore[return-value]
        [[entry["complex_otf_real"], entry["complex_otf_imag"]] for entry in axes[key]]
        for key in ("row_y_up", "col_x_right")
    )


def _max_complex_delta(
    left: tuple[list[list[float]], list[list[float]]],
    right: tuple[list[list[float]], list[list[float]]],
) -> float:
    return max(
        abs(complex(*a) - complex(*b))
        for left_axis, right_axis in zip(left, right, strict=True)
        for a, b in zip(left_axis, right_axis, strict=True)
    )


def _fine_grid(
    source_side: int, source_pitch_mm: float, requested_pitch_um: float,
) -> tuple[int, float]:
    if (
        isinstance(requested_pitch_um, bool)
        or not math.isfinite(float(requested_pitch_um))
        or float(requested_pitch_um) <= 0
        or not 8 <= source_side <= 1024
    ):
        raise ValueError("Declare a finite positive finer histogram pitch and bounded source grid.")
    target_pitch_mm = float(requested_pitch_um) * 1e-3
    if not math.isclose(
        source_pitch_mm / target_pitch_mm, REFINEMENT_FACTOR,
        rel_tol=0, abs_tol=1e-9,
    ):
        raise ValueError("v2 target histogram pitch must be exactly half the pinned source pitch.")
    target_side = (source_side - 1) * REFINEMENT_FACTOR + 1
    if target_side > 2049 or not math.isclose(
        (target_side - 1) * target_pitch_mm,
        (source_side - 1) * source_pitch_mm,
        rel_tol=0, abs_tol=1e-12,
    ):
        raise ValueError("Fine PSF sample-center support does not match the source support.")
    return target_side, target_pitch_mm


def build_pinned_dense_geometric_psf_candidate_v2(
    revision_id: str,
    *,
    catalog_root: str | Path,
    output_dir: str | Path,
    target_histogram_pitch_um: float,
    pupil_samples: int = 145,
    target_frequency_cycles_per_mm_row_col: tuple[tuple[float, ...], tuple[float, ...]] = (
        (100.0, 250.0, 400.0), (100.0, 250.0, 400.0),
    ),
    maximum_histogram_complex_otf_abs_delta: float = 0.01,
    max_total_requested_pupil_rays: int = 400_000,
    max_output_psf_bytes: int = DEFAULT_MAX_OUTPUT_PSF_BYTES,
    soft_timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Reproduce all source nodes, then trace each dense pupil once for both grids."""
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("Choose one pinned Lens DB revision ID.")
    _validate_request(
        pupil_samples, max_total_requested_pupil_rays, max_output_psf_bytes, soft_timeout_s,
    )
    if (
        isinstance(maximum_histogram_complex_otf_abs_delta, bool)
        or not math.isfinite(float(maximum_histogram_complex_otf_abs_delta))
        or not 0 < float(maximum_histogram_complex_otf_abs_delta)
        <= MAXIMUM_HISTOGRAM_OTF_TOLERANCE
    ):
        raise ValueError("Declare a positive absolute complex-OTF tolerance at most 0.1.")
    targets = _targets(target_frequency_cycles_per_mm_row_col)
    root = Path(catalog_root).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    if destination == root or root in destination.parents:
        raise ValueError("Candidate output must be outside the production Lens catalog.")
    started = time.monotonic()
    deadline = started + float(soft_timeout_s)
    builder_sha = _file_sha(Path(__file__).resolve())
    identity, source, db_path, asset_path = _read_source(revision_id, root)
    fields = np.asarray(source["field_height_mm"], dtype=float)
    waves = np.asarray(source["wavelength_nm"], dtype=float)
    source_psf = np.asarray(source["psf_function"], dtype=np.float32)
    source_pupil = np.asarray(source["pupil_coordinates"], dtype=float)
    source_pitch = np.asarray(source["sample_spacing_mm"], dtype=float)
    if (
        source_pitch.shape != (2,) or not np.isfinite(source_pitch).all()
        or np.any(source_pitch <= 0)
        or not np.isclose(source_pitch[0], source_pitch[1], rtol=0, atol=1e-15)
    ):
        raise ValueError("Pinned source PSF physical grid is invalid.")
    target_side, target_pitch = _fine_grid(
        source_psf.shape[0], float(source_pitch[0]), target_histogram_pitch_um,
    )
    if targets[0][-1] >= 0.5 / source_pitch[0] or targets[1][-1] >= 0.5 / source_pitch[1]:
        raise ValueError("Declared frequency must be below the source histogram Nyquist.")
    dense_pupil = _pupil_points(pupil_samples)
    node_count = len(fields) * len(waves)
    requested_rays = node_count * (len(source_pupil) + len(dense_pupil))
    output_shape = (target_side, target_side, len(fields), len(waves))
    output_bytes = math.prod(output_shape) * np.dtype("<f4").itemsize
    intersection_bytes = node_count * len(dense_pupil) * 2 * 8
    estimated_arrays_bytes = int(
        sum(value.nbytes for value in source.values()) + output_bytes
        + intersection_bytes
        + node_count * len(dense_pupil) * (3 * 8 + 8 + 1)
        + node_count * len(source_pupil) * (2 * 8 + 3 * 8 + 1)
        + target_side**2 * 8 + source_psf.shape[0]**2 * 8
    )
    if requested_rays > max_total_requested_pupil_rays:
        raise ValueError(
            f"All-node candidate needs {requested_rays} pupil rays, above declared "
            f"budget {max_total_requested_pupil_rays}."
        )
    if output_bytes > max_output_psf_bytes or estimated_arrays_bytes > MAX_WORKING_ARRAY_BYTES:
        raise ValueError("Fine candidate exceeds the declared PSF or working-array byte policy.")
    base = _load_pinned_node(revision_id, root, 0, float(waves[0]))
    if (
        base.source_psf_asset_sha256 != identity["source_psf_asset_sha256"]
        or base.source_prescription_sha256 != identity["source_prescription_sha256"]
        or base.lens_db_sha256 != identity["lens_db_sha256"]
        or not np.array_equal(base.source_pupil, source_pupil)
        or len(base.model["optical_spec"]["fov"].fields) != len(fields)
    ):
        raise ValueError("Pinned model, axes, source pupil, or lineage changed.")
    settings = json.loads(str(source["build_settings_json"]))
    minimum_retained = float(settings["min_retained_energy"])
    if pupil_samples <= int(settings["pupil_samples"]):
        raise ValueError("Candidate pupil must be denser than the pinned source pupil.")
    if source["pupil_weights"].shape != (len(fields), len(waves), len(source_pupil)):
        raise ValueError("Pinned source pupil mask shape differs.")
    if source["image_direction_cosines"].shape != (
        len(fields), len(waves), len(source_pupil), 3,
    ):
        raise ValueError("Pinned source direction array shape differs.")

    node_reports: list[dict[str, Any]] = []
    chiefs = np.empty_like(source["chief_points_mm"], dtype=float)
    chief_directions = np.empty_like(source["chief_direction_cosines"], dtype=float)
    # The dense loop remains unreachable until every original node reproduces.
    for fi, field in enumerate(base.model["optical_spec"]["fov"].fields):
        for wi, wave in enumerate(waves):
            if time.monotonic() > deadline:
                raise TimeoutError("Source reproduction exceeded the soft deadline.")
            chief, direction = _trace_point_direction(base.model, [0., 0.], field, float(wave))
            chiefs[fi, wi] = chief
            chief_directions[fi, wi] = direction
            if (
                not np.allclose(chief, source["chief_points_mm"][fi, wi], rtol=0, atol=1e-9)
                or not np.allclose(
                    direction, source["chief_direction_cosines"][fi, wi], rtol=0, atol=1e-9,
                )
            ):
                raise ValueError(f"Pinned chief ray did not reproduce at node ({fi},{wi}).")
            points, directions, success = _trace_pupil(
                base.model, field, float(wave), source_pupil, chief, deadline,
            )
            histogram, retained = _histogram(
                points, source_psf.shape[0], float(source_pitch[0]), min_retained=1e-12,
            )
            delta = np.abs(histogram - source_psf[:, :, fi, wi])
            direction_error = float(np.max(np.abs(
                directions[success] - source["image_direction_cosines"][fi, wi, success]
            )))
            if (
                not np.array_equal(success, source["pupil_weights"][fi, wi] > 0)
                or len(points) != int(source["success_counts"][fi, wi])
                or retained < minimum_retained
                or float(delta.max()) > SOURCE_REPRODUCTION_MAX_ABS_TOLERANCE
                or float(delta.sum()) > SOURCE_REPRODUCTION_L1_TOLERANCE
                or direction_error > SOURCE_DIRECTION_REPRODUCTION_MAX_ABS_TOLERANCE
            ):
                raise ValueError(f"Pinned source PSF did not reproduce at node ({fi},{wi}).")
            node_reports.append({
                "field_index": fi, "field_height_mm": float(fields[fi]),
                "wavelength_index": wi, "wavelength_nm": float(wave),
                "source_reproduced_before_dense_trace": True,
                "source_requested_rays": len(source_pupil),
                "source_admitted_rays": len(points),
                "source_fixed_support_retained_ray_fraction": float(retained),
                "source_histogram_max_abs_cell_error": float(delta.max()),
                "source_histogram_l1_mass_error": float(delta.sum()),
                "source_ray_direction_max_abs_error": direction_error,
                "source_admitted_intersections_xy_mm_sha256": _array_sha(points, "<f8"),
            })

    derived = np.empty(output_shape, dtype=np.float32)
    dense_count = np.zeros((len(fields), len(waves)), dtype=np.int32)
    dense_directions = np.full(
        (len(fields), len(waves), len(dense_pupil), 3), np.nan, dtype=float,
    )
    dense_weights = np.zeros((len(fields), len(waves), len(dense_pupil)), dtype=float)
    dense_retained = np.zeros((len(fields), len(waves)), dtype=float)
    intersections: list[np.ndarray] = []
    offsets = [0]
    for node in node_reports:
        fi, wi = node["field_index"], node["wavelength_index"]
        field = base.model["optical_spec"]["fov"].fields[fi]
        points, directions, success = _trace_pupil(
            base.model, field, float(waves[wi]), dense_pupil, chiefs[fi, wi], deadline,
        )
        coarse, coarse_retained = _histogram(
            points, source_psf.shape[0], float(source_pitch[0]), min_retained=1e-12,
        )
        fine, fine_retained = _histogram(
            points, target_side, target_pitch, min_retained=1e-12,
        )
        if min(coarse_retained, fine_retained) < minimum_retained:
            raise ValueError(
                f"Denser rays escaped pinned physical PSF support at node ({fi},{wi})."
            )
        # Diagnose the exact stored float32 masses, not only a transient float64 grid.
        derived[:, :, fi, wi] = fine.astype(np.float32)
        direct = tuple(_complex_pairs(axis) for axis in _direct_ray_axis_otf(points, targets))
        coarse_otf = _otf_pairs(coarse.astype(np.float32), float(source_pitch[0]), targets)
        fine_otf = _otf_pairs(derived[:, :, fi, wi], target_pitch, targets)
        dense_count[fi, wi] = len(points)
        dense_directions[fi, wi] = directions
        dense_weights[fi, wi, success] = 1.0 / len(points)
        dense_retained[fi, wi] = fine_retained
        intersections.append(points)
        offsets.append(offsets[-1] + len(points))
        node.update({
            "dense_requested_rays": len(dense_pupil),
            "dense_admitted_rays": len(points),
            "dense_admitted_ray_fraction": len(points) / len(dense_pupil),
            "dense_source_pitch_retained_ray_fraction": float(coarse_retained),
            "dense_target_pitch_retained_ray_fraction": float(fine_retained),
            "dense_admitted_intersections_xy_mm_sha256": _array_sha(points, "<f8"),
            "direct_ray_complex_otf_row_y_up": direct[0],
            "direct_ray_complex_otf_col_x_right": direct[1],
            "source_pitch_binned_complex_otf_row_y_up": coarse_otf[0],
            "source_pitch_binned_complex_otf_col_x_right": coarse_otf[1],
            "target_pitch_binned_complex_otf_row_y_up": fine_otf[0],
            "target_pitch_binned_complex_otf_col_x_right": fine_otf[1],
            "source_pitch_binned_vs_direct_max_complex_otf_abs_delta": (
                _max_complex_delta(direct, coarse_otf)
            ),
            "target_pitch_binned_vs_direct_max_complex_otf_abs_delta": (
                _max_complex_delta(direct, fine_otf)
            ),
        })
    if not np.isfinite(derived).all() or np.any(derived < 0):
        raise ValueError("Fine candidate PSF contains invalid values.")
    if (
        _file_sha(asset_path) != identity["source_psf_asset_sha256"]
        or _file_sha(db_path) != identity["lens_db_sha256"]
        or _file_sha(Path(__file__).resolve()) != builder_sha
    ):
        raise ValueError("Pinned source or candidate builder changed during generation.")
    joined = np.concatenate(intersections, axis=0).astype("<f8", copy=False)
    offset_array = np.asarray(offsets, dtype="<i8")
    max_gap = max(
        node["target_pitch_binned_vs_direct_max_complex_otf_abs_delta"]
        for node in node_reports
    )
    max_source_gap = max(
        node["source_pitch_binned_vs_direct_max_complex_otf_abs_delta"]
        for node in node_reports
    )
    within = max_gap <= maximum_histogram_complex_otf_abs_delta
    manifest: dict[str, Any] = {
        "schema": SCHEMA, "status": STATUS,
        "scope": "pinned_geometric_ray_histogram_all_stored_field_wavelength_nodes",
        **identity,
        "candidate_builder_file_sha256": builder_sha,
        "source_quality_json_sha256": hashlib.sha256(
            str(source["quality_json"]).encode("utf-8")
        ).hexdigest(),
        "source_optics_json_sha256": hashlib.sha256(
            str(source["optics_json"]).encode("utf-8")
        ).hexdigest(),
        "candidate_psf_array_sha256": _array_sha(derived, "<f4"),
        "dense_intersections_array_sha256": _array_sha(joined, "<f8"),
        "source_psf_shape_row_col_field_wave": list(source_psf.shape),
        "candidate_psf_shape_row_col_field_wave": list(derived.shape),
        "source_histogram_pitch_mm_row_col": source_pitch.tolist(),
        "candidate_histogram_pitch_mm_row_col": [target_pitch, target_pitch],
        "physical_sample_center_support_mm_row_col": [
            (source_psf.shape[0] - 1) * float(source_pitch[0]),
            (source_psf.shape[1] - 1) * float(source_pitch[1]),
        ],
        "histogram_refinement_factor": REFINEMENT_FACTOR,
        "field_height_mm": fields.tolist(), "wavelength_nm": waves.tolist(),
        "source_pupil_samples": int(settings["pupil_samples"]),
        "dense_pupil_samples": pupil_samples,
        "target_frequency_cycles_per_mm_row_col": [
            targets[0].tolist(), targets[1].tolist(),
        ],
        "node_count": node_count, "node_reports": node_reports,
        "all_stored_field_wavelength_nodes_completed": True,
        "source_reproduction_completed_before_any_dense_trace": True,
        "source_geometry_and_relative_illumination_arrays_copied_unchanged": True,
        "dense_ray_directions_and_cra_recomputed": True,
        "dense_source_pitch_binned_vs_direct_worst_complex_otf_abs_delta": max_source_gap,
        "dense_target_pitch_binned_vs_direct_worst_complex_otf_abs_delta": max_gap,
        "maximum_histogram_complex_otf_abs_delta_allowed": float(
            maximum_histogram_complex_otf_abs_delta
        ),
        "target_histogram_gap_within_declared_tolerance_model_only": within,
        "histogram_target_status": (
            "numeric_gap_within_declared_tolerance_model_only" if within
            else "inconclusive_target_histogram_gap"
        ),
        "minimum_dense_source_pitch_retained_ray_fraction": min(
            node["dense_source_pitch_retained_ray_fraction"] for node in node_reports
        ),
        "minimum_dense_target_pitch_retained_ray_fraction": float(dense_retained.min()),
        "requested_pupil_rays": requested_rays,
        "output_psf_float32_bytes": output_bytes,
        "estimated_working_array_bytes_not_peak_rss": estimated_arrays_bytes,
        "limits": {
            "max_total_requested_pupil_rays": max_total_requested_pupil_rays,
            "max_output_psf_bytes": max_output_psf_bytes,
            "max_working_array_bytes": MAX_WORKING_ARRAY_BYTES,
            "soft_timeout_s": float(soft_timeout_s),
            "hard_process_rss_or_time_guard": False,
        },
        "source_reproduction_tolerances": {
            "histogram_max_abs_cell": SOURCE_REPRODUCTION_MAX_ABS_TOLERANCE,
            "histogram_l1_mass": SOURCE_REPRODUCTION_L1_TOLERANCE,
            "ray_direction_max_abs": SOURCE_DIRECTION_REPRODUCTION_MAX_ABS_TOLERANCE,
        },
        "optical_psf_semantics": "geometric_pre_pixel_ray_histogram",
        "diffraction_included": False, "pixel_aperture_included": False,
        "pupil_density_convergence_qualified_all_nodes": False,
        "scene_sampling_qualified": False,
        "lens_radiometry_validated": False,
        "system_mtf_or_product_ranking_allowed": False,
    }
    quality = {
        "schema": SCHEMA, "candidate_only": True,
        "source_psf_asset_sha256": identity["source_psf_asset_sha256"],
        "candidate_builder_file_sha256": builder_sha,
        "optical_psf_semantics": "geometric_pre_pixel_ray_histogram",
        "pupil_sampling_convergence_validated": False,
        "histogram_sampling_convergence_validated": False,
        "radiometry_status": "unknown", "diffraction_included": False,
        "pixel_aperture_included": False, "physical_validation": False,
    }
    candidate_settings = dict(settings)
    candidate_settings.update({
        "model": SCHEMA, "pupil_samples": pupil_samples,
        "source_builder_generator_sha256": settings["generator_sha256"],
        "candidate_builder_file_sha256": builder_sha,
        "target_histogram_pitch_um": float(target_histogram_pitch_um),
        "histogram_refinement_factor": REFINEMENT_FACTOR,
    })
    if math.isinf(float(candidate_settings["object_distance_m"])):
        candidate_settings["object_distance_m"] = "positive_infinity"
    candidate_settings.pop("generator_sha256", None)
    archive = dict(source)
    archive.pop("optics_json", None)
    archive.update({
        "source_sample_spacing_mm": source_pitch.copy(),
        "sample_spacing_mm": np.asarray([target_pitch, target_pitch]),
        "psf_function": derived,
        "dense_admitted_intersections_xy_mm": joined,
        "dense_node_offsets": offset_array,
        "success_counts": dense_count,
        "requested_counts": np.full_like(dense_count, len(dense_pupil)),
        "pupil_coordinates": dense_pupil,
        "pupil_weights": dense_weights,
        "pupil_throughput_fraction": dense_count / len(dense_pupil),
        "image_direction_cosines": dense_directions,
        "chief_points_mm": chiefs,
        "chief_direction_cosines": chief_directions,
        "chief_ray_angle_deg": np.degrees(np.arctan2(
            np.linalg.norm(chief_directions[:, :, :2], axis=2),
            np.abs(chief_directions[:, :, 2]),
        )),
        "retained_energy_fraction": dense_retained,
        "quality_json": np.asarray(json.dumps(quality, sort_keys=True, allow_nan=False)),
        "build_settings_json": np.asarray(json.dumps(
            candidate_settings, sort_keys=True, allow_nan=False,
        )),
        "candidate_metadata_json": np.asarray(json.dumps(
            manifest, sort_keys=True, allow_nan=False,
        )),
    })
    destination.mkdir(parents=True, exist_ok=True)
    descriptor = None
    temporary_name = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".dense-geometric-psf-v2-", suffix=".npz", dir=destination,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            np.savez_compressed(handle, **archive)
            handle.flush()
            os.fsync(handle.fileno())
        temporary = Path(temporary_name)
        candidate_asset_sha = _file_sha(temporary)
        candidate_path = destination / f"{candidate_asset_sha}.npz"
        if candidate_path.exists():
            if _file_sha(candidate_path) != candidate_asset_sha:
                raise ValueError("Existing candidate content-addressed path is corrupt.")
            temporary.unlink()
        else:
            os.replace(temporary, candidate_path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    manifest["candidate_asset_sha256"] = candidate_asset_sha
    manifest["candidate_asset_path"] = str(candidate_path)
    manifest["candidate_asset_file"] = candidate_path.name
    report_path = destination / f"{candidate_asset_sha}.json"
    payload = json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if report_path.exists() and report_path.read_text(encoding="utf-8") != payload:
        raise ValueError("Existing candidate report differs at content-addressed path.")
    if not report_path.exists():
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".dense-geometric-psf-v2-report-", suffix=".json", dir=destination,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, report_path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    manifest["report_path"] = str(report_path)
    manifest["elapsed_seconds"] = time.monotonic() - started
    return manifest


def load_pinned_dense_geometric_psf_candidate_v2(
    report_path: str | Path,
    *,
    expected_lens_revision_id: str,
    expected_source_psf_asset_sha256: str,
    candidate_asset_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Verify a complete v2 artifact, including physical support and ray/bin OTF."""
    path = Path(report_path).expanduser().resolve()
    report = json.loads(path.read_text(encoding="utf-8"))
    relocated = candidate_asset_path is not None
    asset = (
        Path(candidate_asset_path).expanduser().resolve() if relocated
        else (path.parent / report["candidate_asset_file"]).resolve()
    )
    if (
        report.get("schema") != SCHEMA or report.get("status") != STATUS
        or report.get("all_stored_field_wavelength_nodes_completed") is not True
        or report.get("source_reproduction_completed_before_any_dense_trace") is not True
        or report.get("lens_revision_id") != expected_lens_revision_id
        or report.get("source_psf_asset_sha256") != expected_source_psf_asset_sha256
        or (not relocated and asset.parent != path.parent)
        or report.get("candidate_asset_file") != f"{report.get('candidate_asset_sha256')}.npz"
        or asset.stat().st_size > DEFAULT_MAX_OUTPUT_PSF_BYTES * 3
        or _file_sha(asset) != report.get("candidate_asset_sha256")
    ):
        raise ValueError("Fine candidate identity or archive integrity differs.")
    with np.load(asset, allow_pickle=False) as archive:
        arrays = {name: np.asarray(archive[name]).copy() for name in archive.files}
    embedded = json.loads(str(arrays["candidate_metadata_json"]))
    without_file = {key: value for key, value in report.items() if key not in (
        "candidate_asset_sha256", "candidate_asset_path", "candidate_asset_file", "report_path",
    )}
    source_shape = report.get("source_psf_shape_row_col_field_wave")
    target_shape = report.get("candidate_psf_shape_row_col_field_wave")
    source_pitch = np.asarray(report.get("source_histogram_pitch_mm_row_col"), dtype=float)
    target_pitch = np.asarray(report.get("candidate_histogram_pitch_mm_row_col"), dtype=float)
    if (
        "optics_json" in arrays or embedded != without_file
        or not isinstance(source_shape, list) or len(source_shape) != 4
        or not isinstance(target_shape, list) or len(target_shape) != 4
        or source_shape[2:] != target_shape[2:]
        or target_shape[:2] != [(n - 1) * REFINEMENT_FACTOR + 1 for n in source_shape[:2]]
        or report.get("histogram_refinement_factor") != REFINEMENT_FACTOR
        or source_pitch.shape != (2,) or target_pitch.shape != (2,)
        or not np.isfinite(source_pitch).all() or not np.isfinite(target_pitch).all()
        or np.any(source_pitch <= 0) or np.any(target_pitch <= 0)
        or not np.allclose(source_pitch / target_pitch, REFINEMENT_FACTOR, rtol=0, atol=1e-9)
        or not np.allclose(
            (np.asarray(source_shape[:2]) - 1) * source_pitch,
            (np.asarray(target_shape[:2]) - 1) * target_pitch,
            rtol=0, atol=1e-12,
        )
        or not isinstance(report.get("physical_sample_center_support_mm_row_col"), list)
        or not np.allclose(
            report.get("physical_sample_center_support_mm_row_col"),
            (np.asarray(source_shape[:2]) - 1) * source_pitch,
            rtol=0, atol=1e-12,
        )
        or not np.array_equal(arrays["source_sample_spacing_mm"], source_pitch)
        or not np.array_equal(arrays["sample_spacing_mm"], target_pitch)
        or list(arrays["psf_function"].shape) != target_shape
        or arrays["psf_function"].dtype != np.float32
        or not np.array_equal(arrays["field_height_mm"], report["field_height_mm"])
        or not np.array_equal(arrays["wavelength_nm"], report["wavelength_nm"])
        or _array_sha(arrays["psf_function"], "<f4")
        != report.get("candidate_psf_array_sha256")
        or not np.isfinite(arrays["psf_function"]).all()
        or np.any(arrays["psf_function"] < 0)
        or not np.allclose(
            np.sum(arrays["psf_function"], axis=(0, 1), dtype=np.float64),
            1.0, rtol=0, atol=1e-5,
        )
    ):
        raise ValueError("Fine candidate PSF, pitch, support or embedded lineage differs.")
    points = arrays["dense_admitted_intersections_xy_mm"]
    offsets = arrays["dense_node_offsets"]
    nodes = report["node_reports"]
    if (
        points.ndim != 2 or points.shape[1] != 2 or points.dtype != np.float64
        or not np.isfinite(points).all()
        or _array_sha(points, "<f8") != report.get("dense_intersections_array_sha256")
        or offsets.shape != (len(nodes) + 1,)
        or offsets.dtype != np.int64
        or offsets[0] != 0 or offsets[-1] != len(points)
        or np.any(np.diff(offsets) <= 0)
        or len(nodes) != target_shape[2] * target_shape[3]
    ):
        raise ValueError("Fine candidate admitted-ray evidence differs.")
    targets = _targets(report["target_frequency_cycles_per_mm_row_col"])
    max_source_gap = 0.0
    max_target_gap = 0.0
    for index, node in enumerate(nodes):
        fi, wi = divmod(index, target_shape[3])
        subset = points[offsets[index]:offsets[index + 1]]
        if (
            node.get("field_index") != fi or node.get("wavelength_index") != wi
            or node.get("dense_admitted_rays") != len(subset)
            or node.get("dense_admitted_intersections_xy_mm_sha256")
            != _array_sha(subset, "<f8")
        ):
            raise ValueError("Fine candidate node ordering or admitted rays differ.")
        coarse, coarse_retained = _histogram(
            subset, source_shape[0], float(source_pitch[0]), min_retained=1e-12,
        )
        fine, fine_retained = _histogram(
            subset, target_shape[0], float(target_pitch[0]), min_retained=1e-12,
        )
        if (
            not np.array_equal(fine.astype(np.float32), arrays["psf_function"][:, :, fi, wi])
            or not math.isclose(
                coarse_retained, node["dense_source_pitch_retained_ray_fraction"],
                rel_tol=0, abs_tol=1e-12,
            )
            or not math.isclose(
                fine_retained, node["dense_target_pitch_retained_ray_fraction"],
                rel_tol=0, abs_tol=1e-12,
            )
        ):
            raise ValueError("Fine candidate ray deposition or support differs.")
        direct = tuple(_complex_pairs(axis) for axis in _direct_ray_axis_otf(subset, targets))
        coarse_otf = _otf_pairs(coarse.astype(np.float32), float(source_pitch[0]), targets)
        fine_otf = _otf_pairs(arrays["psf_function"][:, :, fi, wi], float(target_pitch[0]), targets)
        for actual, prefix in ((direct, "direct_ray"), (coarse_otf, "source_pitch_binned"),
                               (fine_otf, "target_pitch_binned")):
            if (
                not np.allclose(actual[0], node[f"{prefix}_complex_otf_row_y_up"],
                                rtol=0, atol=1e-10)
                or not np.allclose(actual[1], node[f"{prefix}_complex_otf_col_x_right"],
                                   rtol=0, atol=1e-10)
            ):
                raise ValueError("Fine candidate node complex OTF differs.")
        source_gap = _max_complex_delta(direct, coarse_otf)
        target_gap = _max_complex_delta(direct, fine_otf)
        if (
            not math.isclose(
                source_gap, node["source_pitch_binned_vs_direct_max_complex_otf_abs_delta"],
                rel_tol=0, abs_tol=1e-10,
            )
            or not math.isclose(
                target_gap, node["target_pitch_binned_vs_direct_max_complex_otf_abs_delta"],
                rel_tol=0, abs_tol=1e-10,
            )
        ):
            raise ValueError("Fine candidate frequency gap differs.")
        max_source_gap = max(max_source_gap, source_gap)
        max_target_gap = max(max_target_gap, target_gap)
    tolerance = float(report["maximum_histogram_complex_otf_abs_delta_allowed"])
    within = max_target_gap <= tolerance
    if (
        not math.isfinite(tolerance) or not 0 < tolerance <= MAXIMUM_HISTOGRAM_OTF_TOLERANCE
        or not math.isclose(
            max_source_gap,
            report["dense_source_pitch_binned_vs_direct_worst_complex_otf_abs_delta"],
            rel_tol=0, abs_tol=1e-10,
        )
        or not math.isclose(
            max_target_gap,
            report["dense_target_pitch_binned_vs_direct_worst_complex_otf_abs_delta"],
            rel_tol=0, abs_tol=1e-10,
        )
        or report["target_histogram_gap_within_declared_tolerance_model_only"] is not within
        or report.get("histogram_target_status") != (
            "numeric_gap_within_declared_tolerance_model_only" if within
            else "inconclusive_target_histogram_gap"
        )
    ):
        raise ValueError("Fine candidate declared frequency decision differs.")
    return report, arrays
