"""Build an opt-in, content-addressed geometric PSF candidate from a pinned lens.

This deliberately does not update the Lens DB or silently replace its PSF.
The candidate has the source asset's physical histogram support, pitch, field
and wavelength axes. Every source node must reproduce before *any* denser
pupil is traced. A denser geometric ray histogram is neither a diffractive PSF
nor evidence of physical camera MTF accuracy.
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

from .lens_catalog_bridge import _asset, _revision_source
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

SCHEMA = "camera_pinned_dense_geometric_psf_asset_candidate_v1"
DEFAULT_MAX_OUTPUT_PSF_BYTES = 128 * 1024**2
MAX_TOTAL_REQUESTED_RAYS = 500_000
MAX_WORKING_ARRAY_BYTES = 256 * 1024**2
SOURCE_DIRECTION_REPRODUCTION_MAX_ABS_TOLERANCE = 1e-7


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trace_point_direction(
    model: Any, pupil: Any, field: Any, wavelength_nm: float,
) -> tuple[np.ndarray, np.ndarray]:
    from rayoptics.raytr.trace import trace_base

    ray, _op_delta, _wavelength = trace_base(
        model, pupil, field, wavelength_nm,
        check_apertures=True, apply_vignetting=False,
    )
    point = np.asarray(ray[-1][0], dtype=float).reshape(3)
    direction = np.asarray(ray[-1][1], dtype=float).reshape(3)
    norm = float(np.linalg.norm(direction))
    if not np.isfinite(point).all() or not np.isfinite(direction).all() or norm <= 0:
        raise ValueError("Ray has an invalid image intersection or direction.")
    return point[:2].copy(), direction / norm


def _trace_pupil(
    model: Any, field: Any, wavelength_nm: float, pupil: np.ndarray,
    chief_point: np.ndarray, deadline: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    points: list[np.ndarray] = []
    directions = np.full((len(pupil), 3), np.nan, dtype=float)
    mask = np.zeros(len(pupil), dtype=bool)
    for index, location in enumerate(pupil):
        if time.monotonic() > deadline:
            raise TimeoutError("Dense geometric PSF candidate exceeded its soft deadline.")
        try:
            point, direction = _trace_point_direction(model, location, field, wavelength_nm)
        except Exception as exc:
            if not type(exc).__name__.startswith("Trace"):
                raise
            continue
        mask[index] = True
        points.append(point - chief_point)
        directions[index] = direction
    if not points:
        raise ValueError("No rays reached the image plane for a stored PSF node.")
    return np.asarray(points, dtype=float), directions, mask


def _complex_pairs(values: np.ndarray) -> list[list[float]]:
    return [[float(value.real), float(value.imag)] for value in values]


def _read_source(
    revision_id: str, catalog_root: Path,
) -> tuple[dict[str, Any], dict[str, np.ndarray], Path, Path]:
    revision, snapshot, db_path, db_sha = _revision_source(revision_id, catalog_root)
    simulation_id, asset_path, validation, _artifact = _asset(
        revision, snapshot, db_path, catalog_root
    )
    with np.load(asset_path, allow_pickle=False) as asset:
        arrays = {name: np.asarray(asset[name]).copy() for name in asset.files}
    fields = np.asarray(arrays["field_height_mm"], dtype=float)
    waves = np.asarray(arrays["wavelength_nm"], dtype=float)
    psf = np.asarray(arrays["psf_function"])
    if (
        fields.ndim != 1 or waves.ndim != 1
        or not 2 <= len(fields) <= 7 or not 1 <= len(waves) <= 5
        or psf.ndim != 4 or psf.shape[2:] != (len(fields), len(waves))
        or psf.shape[0] != psf.shape[1]
        or psf.dtype != np.float32
    ):
        raise ValueError("Pinned source PSF axis or float32 array contract differs.")
    identity = {
        "lens_revision_id": revision_id,
        "lens_revision_content_sha256": revision["content_sha256"],
        "simulation_id": simulation_id,
        "lens_db_sha256": db_sha,
        "source_psf_asset_sha256": validation["asset_sha256"],
        "source_prescription_sha256": validation["source_prescription_sha256"],
        "source_psf_array_sha256": _array_sha(psf, "<f4"),
    }
    return identity, arrays, db_path, asset_path


def _validate_request(
    pupil_samples: int, max_total_requested_pupil_rays: int,
    max_output_psf_bytes: int, soft_timeout_s: float,
) -> None:
    if type(pupil_samples) is not int or not 3 <= pupil_samples <= 257:
        raise ValueError("Dense pupil samples must be an integer from 3 through 257.")
    if (
        type(max_total_requested_pupil_rays) is not int
        or not 1 <= max_total_requested_pupil_rays <= MAX_TOTAL_REQUESTED_RAYS
    ):
        raise ValueError("Requested ray budget must be an integer within 500,000.")
    if (
        type(max_output_psf_bytes) is not int
        or not 1 <= max_output_psf_bytes <= DEFAULT_MAX_OUTPUT_PSF_BYTES
    ):
        raise ValueError("Output PSF byte budget must be within 128 MiB.")
    if (
        isinstance(soft_timeout_s, bool) or not math.isfinite(float(soft_timeout_s))
        or not 0 < float(soft_timeout_s) <= 300
    ):
        raise ValueError("Soft timeout must be positive and at most 300 seconds.")


def build_pinned_dense_geometric_psf_candidate(
    revision_id: str,
    *,
    catalog_root: str | Path,
    output_dir: str | Path,
    pupil_samples: int = 145,
    target_frequency_cycles_per_mm_row_col: tuple[tuple[float, ...], tuple[float, ...]] = (
        (100.0, 250.0, 400.0, 500.0, 800.0),
        (100.0, 250.0, 400.0, 500.0, 800.0),
    ),
    max_total_requested_pupil_rays: int = 400_000,
    max_output_psf_bytes: int = DEFAULT_MAX_OUTPUT_PSF_BYTES,
    soft_timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Reproduce every stored node, then trace and save a denser candidate.

    The ray/array/time budgets are admission and soft-loop guards, not a hard
    process RSS or wall-clock limit. Run the build inside a guarded worker for
    untrusted or larger catalog assets. Only a complete all-node build writes
    an asset; any source mismatch or unsupported physical window fails closed.
    """
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("Choose one pinned Lens DB revision ID.")
    _validate_request(
        pupil_samples, max_total_requested_pupil_rays,
        max_output_psf_bytes, soft_timeout_s,
    )
    targets = _targets(target_frequency_cycles_per_mm_row_col)
    root = Path(catalog_root).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    if destination == root or root in destination.parents:
        raise ValueError("Candidate output must be outside the production Lens catalog.")
    started = time.monotonic()
    deadline = started + float(soft_timeout_s)
    builder_path = Path(__file__).resolve()
    candidate_builder_file_sha = _file_sha(builder_path)
    identity, source, db_path, asset_path = _read_source(revision_id, root)
    fields = np.asarray(source["field_height_mm"], dtype=float)
    waves = np.asarray(source["wavelength_nm"], dtype=float)
    source_psf = np.asarray(source["psf_function"], dtype=np.float32)
    source_pupil = np.asarray(source["pupil_coordinates"], dtype=float)
    source_pitch = np.asarray(source["sample_spacing_mm"], dtype=float)
    if (
        source_pitch.shape != (2,) or not np.isfinite(source_pitch).all()
        or np.any(source_pitch <= 0)
        or not np.isclose(source_pitch[0], source_pitch[1], atol=1e-15, rtol=0)
    ):
        raise ValueError("Pinned source PSF physical grid is invalid.")
    dense_pupil = _pupil_points(pupil_samples)
    node_count = len(fields) * len(waves)
    requested_rays = node_count * (len(source_pupil) + len(dense_pupil))
    output_bytes = int(source_psf.nbytes)
    # Candidate output, dense directions, original loaded arrays, and source
    # trace buffers; this is an allocation policy, not a measured peak RSS.
    estimated_arrays_bytes = int(
        sum(value.nbytes for value in source.values()) + output_bytes
        + node_count * len(dense_pupil) * (3 * 8 + 8 + 1)
        + node_count * len(source_pupil) * (2 * 8 + 3 * 8 + 1)
    )
    if requested_rays > max_total_requested_pupil_rays:
        raise ValueError(
            f"All-node candidate needs {requested_rays} pupil rays, above declared "
            f"budget {max_total_requested_pupil_rays}."
        )
    if output_bytes > max_output_psf_bytes or estimated_arrays_bytes > MAX_WORKING_ARRAY_BYTES:
        raise ValueError("Candidate exceeds the declared PSF or working-array byte policy.")
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
        len(fields), len(waves), len(source_pupil), 3
    ):
        raise ValueError("Pinned source direction array shape differs.")

    node_reports: list[dict[str, Any]] = []
    source_otfs: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}
    source_admitted: dict[tuple[int, int], float] = {}
    traced_chiefs = np.empty_like(source["chief_points_mm"], dtype=float)
    traced_chief_directions = np.empty_like(source["chief_direction_cosines"], dtype=float)
    # Complete source reproduction first. The dense loop is unreachable when
    # even one pinned field/wavelength node fails its source checks.
    for fi, field in enumerate(base.model["optical_spec"]["fov"].fields):
        for wi, wavelength in enumerate(waves):
            if time.monotonic() > deadline:
                raise TimeoutError("Source reproduction exceeded the soft deadline.")
            chief, chief_direction = _trace_point_direction(
                base.model, [0.0, 0.0], field, float(wavelength)
            )
            traced_chiefs[fi, wi] = chief
            traced_chief_directions[fi, wi] = chief_direction
            if (
                not np.allclose(chief, source["chief_points_mm"][fi, wi], atol=1e-9, rtol=0)
                or not np.allclose(
                    chief_direction, source["chief_direction_cosines"][fi, wi],
                    atol=1e-9, rtol=0,
                )
            ):
                raise ValueError(f"Pinned chief ray did not reproduce at node ({fi},{wi}).")
            points, directions, success = _trace_pupil(
                base.model, field, float(wavelength), source_pupil, chief, deadline
            )
            histogram, retained = _histogram(
                points, source_psf.shape[0], float(source_pitch[0]),
                min_retained=1e-12,
            )
            delta = np.abs(histogram - source_psf[:, :, fi, wi])
            saved_mask = source["pupil_weights"][fi, wi] > 0
            direction_error = float(np.max(np.abs(
                directions[success] - source["image_direction_cosines"][fi, wi, success]
            )))
            if (
                not np.array_equal(success, saved_mask)
                or len(points) != int(source["success_counts"][fi, wi])
                or retained < minimum_retained
                or float(delta.max()) > SOURCE_REPRODUCTION_MAX_ABS_TOLERANCE
                or float(delta.sum()) > SOURCE_REPRODUCTION_L1_TOLERANCE
                or direction_error > SOURCE_DIRECTION_REPRODUCTION_MAX_ABS_TOLERANCE
            ):
                raise ValueError(f"Pinned source PSF did not reproduce at node ({fi},{wi}).")
            source_otfs[fi, wi] = _direct_ray_axis_otf(points, targets)
            source_admitted[fi, wi] = len(points) / len(source_pupil)
            node_reports.append({
                "field_index": fi,
                "field_height_mm": float(fields[fi]),
                "wavelength_index": wi,
                "wavelength_nm": float(wavelength),
                "source_reproduced_before_dense_trace": True,
                "source_requested_rays": len(source_pupil),
                "source_admitted_rays": len(points),
                "source_admitted_ray_fraction": source_admitted[fi, wi],
                "source_fixed_support_retained_ray_fraction": float(retained),
                "source_histogram_max_abs_cell_error": float(delta.max()),
                "source_histogram_l1_mass_error": float(delta.sum()),
                "source_ray_direction_max_abs_error": direction_error,
                "source_admitted_intersections_xy_mm_sha256": _array_sha(points, "<f8"),
            })

    derived = np.empty_like(source_psf)
    dense_count = np.zeros((len(fields), len(waves)), dtype=np.int32)
    dense_directions = np.full(
        (len(fields), len(waves), len(dense_pupil), 3), np.nan, dtype=float
    )
    dense_weights = np.zeros((len(fields), len(waves), len(dense_pupil)), dtype=float)
    dense_retained = np.zeros((len(fields), len(waves)), dtype=float)
    for node in node_reports:
        fi, wi = node["field_index"], node["wavelength_index"]
        field = base.model["optical_spec"]["fov"].fields[fi]
        points, directions, success = _trace_pupil(
            base.model, field, float(waves[wi]), dense_pupil,
            traced_chiefs[fi, wi], deadline,
        )
        histogram, retained = _histogram(
            points, source_psf.shape[0], float(source_pitch[0]),
            min_retained=1e-12,
        )
        if retained < minimum_retained:
            raise ValueError(
                f"Denser rays escaped pinned physical PSF support at node ({fi},{wi})."
            )
        derived[:, :, fi, wi] = histogram.astype(np.float32)
        dense_count[fi, wi] = len(points)
        dense_directions[fi, wi] = directions
        dense_weights[fi, wi, success] = 1.0 / len(points)
        dense_retained[fi, wi] = retained
        dense_otf = _direct_ray_axis_otf(points, targets)
        source_otf = source_otfs[fi, wi]
        row_delta = np.abs(dense_otf[0] - source_otf[0])
        col_delta = np.abs(dense_otf[1] - source_otf[1])
        node.update({
            "dense_requested_rays": len(dense_pupil),
            "dense_admitted_rays": len(points),
            "dense_admitted_ray_fraction": len(points) / len(dense_pupil),
            "dense_fixed_support_retained_ray_fraction": float(retained),
            "dense_admitted_intersections_xy_mm_sha256": _array_sha(points, "<f8"),
            "source_complex_otf_row_y_up": _complex_pairs(source_otf[0]),
            "source_complex_otf_col_x_right": _complex_pairs(source_otf[1]),
            "dense_complex_otf_row_y_up": _complex_pairs(dense_otf[0]),
            "dense_complex_otf_col_x_right": _complex_pairs(dense_otf[1]),
            "source_vs_dense_complex_otf_abs_delta_row_y_up": row_delta.tolist(),
            "source_vs_dense_complex_otf_abs_delta_col_x_right": col_delta.tolist(),
            "source_vs_dense_max_complex_otf_abs_delta": float(max(
                row_delta.max(), col_delta.max()
            )),
            "source_vs_dense_admitted_fraction_delta": abs(
                len(points) / len(dense_pupil) - source_admitted[fi, wi]
            ),
        })
    if not np.isfinite(derived).all() or np.any(derived < 0):
        raise ValueError("Derived candidate PSF contains invalid values.")
    if _file_sha(asset_path) != identity["source_psf_asset_sha256"]:
        raise ValueError("Pinned source PSF changed during candidate generation.")
    if _file_sha(db_path) != identity["lens_db_sha256"]:
        raise ValueError("Pinned lens source DB changed during candidate generation.")
    if _file_sha(builder_path) != candidate_builder_file_sha:
        raise ValueError("Candidate builder source changed during generation.")
    candidate_array_sha = _array_sha(derived, "<f4")
    source_quality_json_sha = hashlib.sha256(
        str(source["quality_json"]).encode("utf-8")
    ).hexdigest()
    source_optics_json_sha = hashlib.sha256(
        str(source["optics_json"]).encode("utf-8")
    ).hexdigest()
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "all_stored_nodes_candidate_generated_model_only",
        "scope": "pinned_geometric_ray_histogram_all_stored_field_wavelength_nodes",
        **identity,
        "candidate_psf_array_sha256": candidate_array_sha,
        "candidate_builder_file_sha256": candidate_builder_file_sha,
        "source_quality_json_sha256": source_quality_json_sha,
        "source_optics_json_sha256": source_optics_json_sha,
        "source_psf_shape_row_col_field_wave": list(source_psf.shape),
        "candidate_psf_shape_row_col_field_wave": list(derived.shape),
        "fixed_physical_histogram_pitch_mm_row_col": source_pitch.tolist(),
        "fixed_physical_histogram_support_shape_row_col": list(source_psf.shape[:2]),
        "field_height_mm": fields.tolist(),
        "wavelength_nm": waves.tolist(),
        "source_pupil_samples": int(settings["pupil_samples"]),
        "dense_pupil_samples": pupil_samples,
        "target_frequency_cycles_per_mm_row_col": [
            targets[0].tolist(), targets[1].tolist(),
        ],
        "node_count": node_count,
        "node_reports": node_reports,
        "all_stored_field_wavelength_nodes_completed": True,
        "source_reproduction_completed_before_any_dense_trace": True,
        "source_geometry_and_relative_illumination_arrays_copied_unchanged": True,
        "dense_ray_directions_and_cra_recomputed": True,
        "source_reproduction_tolerances": {
            "histogram_max_abs_cell": SOURCE_REPRODUCTION_MAX_ABS_TOLERANCE,
            "histogram_l1_mass": SOURCE_REPRODUCTION_L1_TOLERANCE,
            "ray_direction_max_abs": SOURCE_DIRECTION_REPRODUCTION_MAX_ABS_TOLERANCE,
        },
        "source_vs_dense_worst_complex_otf_abs_delta": max(
            node["source_vs_dense_max_complex_otf_abs_delta"] for node in node_reports
        ),
        "minimum_dense_fixed_support_retained_ray_fraction": float(dense_retained.min()),
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
        "optical_psf_semantics": "geometric_pre_pixel_ray_histogram",
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "lens_radiometry_validated": False,
        "pupil_density_convergence_qualified_all_nodes": False,
        "scene_sampling_qualified": False,
        "system_mtf_or_product_ranking_allowed": False,
    }
    quality = {
        "schema": SCHEMA,
        "candidate_only": True,
        "source_psf_asset_sha256": identity["source_psf_asset_sha256"],
        "source_quality_json_sha256": source_quality_json_sha,
        "source_optics_json_sha256": source_optics_json_sha,
        "source_builder_generator_sha256": settings["generator_sha256"],
        "candidate_builder_file_sha256": candidate_builder_file_sha,
        "optical_psf_semantics": "geometric_pre_pixel_ray_histogram",
        "pupil_sampling_convergence_validated": False,
        "histogram_sampling_convergence_validated": False,
        "lens_radiometry_validated": False,
        "radiometry_status": "unknown",
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "physical_validation": False,
    }
    candidate_settings = dict(settings)
    candidate_settings["model"] = SCHEMA
    candidate_settings["pupil_samples"] = pupil_samples
    candidate_settings["source_builder_generator_sha256"] = settings["generator_sha256"]
    candidate_settings["candidate_builder_file_sha256"] = candidate_builder_file_sha
    if math.isinf(float(candidate_settings["object_distance_m"])):
        candidate_settings["object_distance_m"] = "positive_infinity"
    candidate_settings.pop("generator_sha256", None)
    archive = dict(source)
    # The source optics JSON describes the *old* 65-ray pupil and is unsafe to
    # reuse as candidate authority. Consumers must read candidate_metadata_json
    # and explicitly bind the separate, pinned source optics themselves.
    archive.pop("optics_json", None)
    archive.update({
        "psf_function": derived,
        "success_counts": dense_count,
        "requested_counts": np.full_like(dense_count, len(dense_pupil)),
        "pupil_coordinates": dense_pupil,
        "pupil_weights": dense_weights,
        "pupil_throughput_fraction": dense_count / len(dense_pupil),
        "image_direction_cosines": dense_directions,
        "chief_points_mm": traced_chiefs,
        "chief_direction_cosines": traced_chief_directions,
        "chief_ray_angle_deg": np.degrees(np.arctan2(
            np.linalg.norm(traced_chief_directions[:, :, :2], axis=2),
            np.abs(traced_chief_directions[:, :, 2]),
        )),
        "retained_energy_fraction": dense_retained,
        "quality_json": np.asarray(json.dumps(quality, sort_keys=True, allow_nan=False)),
        "build_settings_json": np.asarray(json.dumps(
            candidate_settings, sort_keys=True, allow_nan=False
        )),
        "candidate_metadata_json": np.asarray(json.dumps(
            manifest, sort_keys=True, allow_nan=False
        )),
    })
    destination.mkdir(parents=True, exist_ok=True)
    descriptor = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".dense-geometric-psf-", suffix=".npz", dir=destination
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
        if "temporary_name" in locals():
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
            prefix=".dense-geometric-psf-report-", suffix=".json", dir=destination
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


def load_pinned_dense_geometric_psf_candidate(
    report_path: str | Path,
    *,
    expected_lens_revision_id: str,
    expected_source_psf_asset_sha256: str,
    candidate_asset_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Integrity-check an explicit candidate for an opt-in consumer."""
    path = Path(report_path).expanduser().resolve()
    report = json.loads(path.read_text(encoding="utf-8"))
    relocated = candidate_asset_path is not None
    asset = (
        Path(candidate_asset_path).expanduser().resolve()
        if relocated else (path.parent / report["candidate_asset_file"]).resolve()
    )
    if (
        report.get("schema") != SCHEMA
        or report.get("status") != "all_stored_nodes_candidate_generated_model_only"
        or report.get("all_stored_field_wavelength_nodes_completed") is not True
        or report.get("lens_revision_id") != expected_lens_revision_id
        or report.get("source_psf_asset_sha256") != expected_source_psf_asset_sha256
        or (not relocated and asset.parent != path.parent)
        or report.get("candidate_asset_file") != f"{report.get('candidate_asset_sha256')}.npz"
        or (not relocated and asset.stem != report.get("candidate_asset_sha256"))
        or asset.stat().st_size > DEFAULT_MAX_OUTPUT_PSF_BYTES * 3
        or _file_sha(asset) != report["candidate_asset_sha256"]
    ):
        raise ValueError("Dense geometric candidate identity or archive integrity differs.")
    with np.load(asset, allow_pickle=False) as archive:
        arrays = {name: np.asarray(archive[name]).copy() for name in archive.files}
    if "optics_json" in arrays:
        raise ValueError("Dense candidate contains stale source optics JSON metadata.")
    embedded = json.loads(str(arrays["candidate_metadata_json"]))
    quality = json.loads(str(arrays["quality_json"]))
    if (
        embedded.get("candidate_psf_array_sha256") != report["candidate_psf_array_sha256"]
        or embedded.get("source_psf_asset_sha256") != expected_source_psf_asset_sha256
        or embedded.get("candidate_builder_file_sha256")
        != report.get("candidate_builder_file_sha256")
        or quality.get("schema") != SCHEMA
        or quality.get("candidate_only") is not True
        or quality.get("candidate_builder_file_sha256")
        != report.get("candidate_builder_file_sha256")
        or quality.get("source_psf_asset_sha256")
        != expected_source_psf_asset_sha256
        or quality.get("diffraction_included") is not False
        or quality.get("pixel_aperture_included") is not False
        or _array_sha(arrays["psf_function"], "<f4")
        != report["candidate_psf_array_sha256"]
        or list(arrays["psf_function"].shape)
        != report["source_psf_shape_row_col_field_wave"]
        or not np.array_equal(arrays["field_height_mm"], report["field_height_mm"])
        or not np.array_equal(arrays["wavelength_nm"], report["wavelength_nm"])
        or not np.array_equal(
            arrays["sample_spacing_mm"],
            report["fixed_physical_histogram_pitch_mm_row_col"],
        )
    ):
        raise ValueError("Dense geometric candidate PSF or embedded lineage differs.")
    return report, arrays
