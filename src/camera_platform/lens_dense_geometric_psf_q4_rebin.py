"""Rebin verified dense geometric rays to quarter source pitch without retracing.

The parent v2 candidate has already verified every source field/wavelength node
and persisted the admitted dense ray intersections.  This module changes only
the histogram grid.  A passing numerical ray/bin OTF gap remains a geometric
model diagnostic, never a camera or physical MTF qualification.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import numpy as np

from .lens_dense_geometric_psf_candidate import _file_sha
from .lens_dense_geometric_psf_candidate_v2 import (
    SCHEMA as PARENT_SCHEMA,
)
from .lens_dense_geometric_psf_candidate_v2 import (
    STATUS as PARENT_STATUS,
)
from .lens_dense_geometric_psf_candidate_v2 import (
    _complex_pairs,
    _max_complex_delta,
    _otf_pairs,
)
from .lens_geometric_psf_rehistogram import _array_sha, _histogram
from .lens_geometric_pupil_density_diagnostic import _direct_ray_axis_otf, _targets

SCHEMA = "camera_pinned_dense_geometric_psf_q4_rebin_asset_v1"
STATUS = "all_stored_nodes_q4_histogram_candidate_generated_model_only"
REFINEMENT_FACTOR = 4
MAX_TARGET_SIDE = 2049
MAX_OUTPUT_PSF_BYTES = 256 * 1024**2
MAX_ESTIMATED_WORKING_ARRAY_BYTES = 512 * 1024**2
MAX_ARCHIVE_BYTES = 384 * 1024**2
MAX_DECODED_ARCHIVE_BYTES = 320 * 1024**2
MAX_ARCHIVE_MEMBERS = 16
MAX_REPORT_BYTES = 2 * 1024**2


def _support(parent: dict[str, Any]) -> tuple[int, float, list[float]]:
    source_shape = parent.get("source_psf_shape_row_col_field_wave")
    source_pitch = np.asarray(parent.get("source_histogram_pitch_mm_row_col"), dtype=float)
    if (
        not isinstance(source_shape, list) or len(source_shape) != 4
        or source_shape[0] != source_shape[1]
        or not 8 <= source_shape[0] <= 512
        or source_pitch.shape != (2,) or not np.isfinite(source_pitch).all()
        or np.any(source_pitch <= 0)
        or not np.isclose(source_pitch[0], source_pitch[1], rtol=0, atol=1e-15)
    ):
        raise ValueError("Pinned q2 parent has invalid source PSF support.")
    target_side = (source_shape[0] - 1) * REFINEMENT_FACTOR + 1
    target_pitch = float(source_pitch[0]) / REFINEMENT_FACTOR
    support = [(side - 1) * float(pitch) for side, pitch in zip(
        source_shape[:2], source_pitch, strict=True,
    )]
    if (
        target_side > MAX_TARGET_SIDE
        or not math.isclose(
            (target_side - 1) * target_pitch, support[0], rel_tol=0, abs_tol=1e-12,
        )
    ):
        raise ValueError("q4 sample-center support exceeds policy or differs from source.")
    return target_side, target_pitch, support


def _validate_parent(
    parent: dict[str, Any], arrays: dict[str, np.ndarray],
    *, parent_run_id: str, parent_asset_sha256: str,
) -> tuple[int, float, list[float], tuple[np.ndarray, np.ndarray]]:
    if (
        parent.get("schema") != PARENT_SCHEMA
        or parent.get("status") != PARENT_STATUS
        or parent.get("all_stored_field_wavelength_nodes_completed") is not True
        or parent.get("candidate_asset_sha256") != parent_asset_sha256
        or not isinstance(parent_run_id, str) or not parent_run_id.startswith("run-")
        or "dense_admitted_intersections_xy_mm" not in arrays
        or "dense_node_offsets" not in arrays
    ):
        raise ValueError("Choose one verified, complete q2 candidate Run and asset SHA.")
    side, pitch, support = _support(parent)
    source_shape = parent["source_psf_shape_row_col_field_wave"]
    parent_shape = parent["candidate_psf_shape_row_col_field_wave"]
    if (
        parent_shape[:2] != [(n - 1) * 2 + 1 for n in source_shape[:2]]
        or parent_shape[2:] != source_shape[2:]
        or not np.allclose(
            parent["candidate_histogram_pitch_mm_row_col"],
            np.asarray(parent["source_histogram_pitch_mm_row_col"]) / 2,
            rtol=0, atol=1e-15,
        )
        or parent.get("physical_sample_center_support_mm_row_col") != support
        or parent.get("node_count") != source_shape[2] * source_shape[3]
    ):
        raise ValueError("Parent q2 physical grid or axes differ from pinned source.")
    points = np.asarray(arrays["dense_admitted_intersections_xy_mm"])
    offsets = np.asarray(arrays["dense_node_offsets"])
    if (
        points.ndim != 2 or points.shape[1] != 2 or points.dtype != np.float64
        or not np.isfinite(points).all()
        or _array_sha(points, "<f8") != parent.get("dense_intersections_array_sha256")
        or offsets.shape != (parent["node_count"] + 1,) or offsets.dtype != np.int64
        or offsets[0] != 0 or offsets[-1] != len(points)
        or np.any(np.diff(offsets) <= 0)
    ):
        raise ValueError("Parent q2 admitted-ray arrays differ from verified lineage.")
    return side, pitch, support, (points, offsets)


def build_q4_geometric_psf_rebin(
    parent_manifest: dict[str, Any],
    parent_arrays: dict[str, np.ndarray],
    *,
    parent_run_id: str,
    parent_asset_sha256: str,
    output_dir: str | Path,
    max_output_psf_bytes: int = MAX_OUTPUT_PSF_BYTES,
    max_estimated_working_array_bytes: int = MAX_ESTIMATED_WORKING_ARRAY_BYTES,
    soft_timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Create an immutable q4 numerical candidate from verified q2 rays.

    The caller must use the owner-bound v2 Job loader. This function checks the
    supplied ray hashes and lineage again, but cannot itself authenticate a Run.
    """
    if (
        type(max_output_psf_bytes) is not int
        or not 1 <= max_output_psf_bytes <= MAX_OUTPUT_PSF_BYTES
        or type(max_estimated_working_array_bytes) is not int
        or not 1 <= max_estimated_working_array_bytes <= MAX_ESTIMATED_WORKING_ARRAY_BYTES
        or type(soft_timeout_s) not in (int, float)
        or not math.isfinite(float(soft_timeout_s))
        or not 0 < float(soft_timeout_s) <= 300
    ):
        raise ValueError("Declare bounded q4 output, working-array and soft-time policies.")
    started = time.monotonic()
    deadline = started + float(soft_timeout_s)
    side, pitch, support, (points, offsets) = _validate_parent(
        parent_manifest, parent_arrays,
        parent_run_id=parent_run_id, parent_asset_sha256=parent_asset_sha256,
    )
    source_shape = parent_manifest["source_psf_shape_row_col_field_wave"]
    shape = (side, side, source_shape[2], source_shape[3])
    output_bytes = math.prod(shape) * 4
    # Count decoded parent arrays because the verified loader currently retains
    # them while rebinning. Float64 deposit scratch is bounded to one node.
    estimated_bytes = int(
        sum(value.nbytes for value in parent_arrays.values()) + output_bytes
        + side * side * 8 + points.nbytes + offsets.nbytes
    )
    if output_bytes > max_output_psf_bytes:
        raise ValueError("q4 PSF exceeds the declared output-byte policy before allocation.")
    if estimated_bytes > max_estimated_working_array_bytes:
        raise ValueError("q4 exceeds the declared working-array policy before allocation.")
    targets = _targets(parent_manifest["target_frequency_cycles_per_mm_row_col"])
    tolerance = float(parent_manifest["maximum_histogram_complex_otf_abs_delta_allowed"])
    settings = json.loads(str(parent_arrays["build_settings_json"]))
    minimum_retained = float(settings["min_retained_energy"])
    if (
        not math.isfinite(tolerance) or not 0 < tolerance <= 0.1
        or not math.isfinite(minimum_retained) or not 0 < minimum_retained <= 1
        or targets[0][-1] >= 0.5 / pitch or targets[1][-1] >= 0.5 / pitch
    ):
        raise ValueError("Parent frequency, tolerance or support-retention contract differs.")
    derived = np.empty(shape, dtype=np.float32)
    reports: list[dict[str, Any]] = []
    for index, parent_node in enumerate(parent_manifest["node_reports"]):
        if time.monotonic() > deadline:
            raise TimeoutError("q4 geometric rebin exceeded its soft deadline.")
        fi, wi = divmod(index, source_shape[3])
        subset = points[offsets[index]:offsets[index + 1]]
        if (
            parent_node.get("field_index") != fi
            or parent_node.get("wavelength_index") != wi
            or parent_node.get("dense_admitted_rays") != len(subset)
            or parent_node.get("dense_admitted_intersections_xy_mm_sha256")
            != _array_sha(subset, "<f8")
        ):
            raise ValueError("Parent node order or admitted intersections differ.")
        histogram, retained = _histogram(
            subset, side, pitch, min_retained=1e-12,
        )
        if retained < minimum_retained:
            raise ValueError("q4 rays escaped the pinned physical PSF support.")
        derived[:, :, fi, wi] = histogram.astype(np.float32)
        direct = tuple(_complex_pairs(axis) for axis in _direct_ray_axis_otf(subset, targets))
        for actual, key in zip(
            direct,
            ("direct_ray_complex_otf_row_y_up", "direct_ray_complex_otf_col_x_right"),
            strict=True,
        ):
            if not np.allclose(actual, parent_node[key], rtol=0, atol=1e-10):
                raise ValueError("Parent direct-ray OTF differs from persisted rays.")
        binned = _otf_pairs(derived[:, :, fi, wi], pitch, targets)
        gap = _max_complex_delta(direct, binned)
        reports.append({
            "field_index": fi,
            "field_height_mm": parent_node["field_height_mm"],
            "wavelength_index": wi,
            "wavelength_nm": parent_node["wavelength_nm"],
            "admitted_rays": len(subset),
            "admitted_intersections_xy_mm_sha256": _array_sha(subset, "<f8"),
            "q4_fixed_support_retained_ray_fraction": float(retained),
            "direct_ray_complex_otf_row_y_up": direct[0],
            "direct_ray_complex_otf_col_x_right": direct[1],
            "q2_binned_vs_direct_max_complex_otf_abs_delta": (
                parent_node["target_pitch_binned_vs_direct_max_complex_otf_abs_delta"]
            ),
            "q4_binned_complex_otf_row_y_up": binned[0],
            "q4_binned_complex_otf_col_x_right": binned[1],
            "q4_binned_vs_direct_max_complex_otf_abs_delta": gap,
        })
    if not np.isfinite(derived).all() or np.any(derived < 0):
        raise ValueError("q4 PSF contains invalid values.")
    worst = max(node["q4_binned_vs_direct_max_complex_otf_abs_delta"] for node in reports)
    within = worst <= tolerance
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "status": STATUS,
        "scope": "same_dense_admitted_rays_rebinned_to_quarter_source_pitch_all_nodes",
        "parent_candidate_run_id": parent_run_id,
        "parent_candidate_asset_sha256": parent_asset_sha256,
        "parent_candidate_psf_array_sha256": parent_manifest["candidate_psf_array_sha256"],
        "parent_dense_intersections_array_sha256": (
            parent_manifest["dense_intersections_array_sha256"]
        ),
        "lens_revision_id": parent_manifest["lens_revision_id"],
        "lens_revision_content_sha256": parent_manifest["lens_revision_content_sha256"],
        "simulation_id": parent_manifest["simulation_id"],
        "lens_db_sha256": parent_manifest["lens_db_sha256"],
        "source_psf_asset_sha256": parent_manifest["source_psf_asset_sha256"],
        "source_prescription_sha256": parent_manifest["source_prescription_sha256"],
        "source_psf_array_sha256": parent_manifest["source_psf_array_sha256"],
        "candidate_psf_array_sha256": _array_sha(derived, "<f4"),
        "source_psf_shape_row_col_field_wave": source_shape,
        "parent_q2_psf_shape_row_col_field_wave": (
            parent_manifest["candidate_psf_shape_row_col_field_wave"]
        ),
        "candidate_psf_shape_row_col_field_wave": list(shape),
        "source_histogram_pitch_mm_row_col": (
            parent_manifest["source_histogram_pitch_mm_row_col"]
        ),
        "parent_q2_histogram_pitch_mm_row_col": (
            parent_manifest["candidate_histogram_pitch_mm_row_col"]
        ),
        "candidate_histogram_pitch_mm_row_col": [pitch, pitch],
        "physical_sample_center_support_mm_row_col": support,
        "histogram_refinement_factor": REFINEMENT_FACTOR,
        "field_height_mm": parent_manifest["field_height_mm"],
        "wavelength_nm": parent_manifest["wavelength_nm"],
        "target_frequency_cycles_per_mm_row_col": (
            parent_manifest["target_frequency_cycles_per_mm_row_col"]
        ),
        "node_count": len(reports),
        "node_reports": reports,
        "all_stored_field_wavelength_nodes_completed": True,
        "same_parent_dense_rays_reused_without_raytrace": True,
        "minimum_q4_fixed_support_retained_ray_fraction": min(
            node["q4_fixed_support_retained_ray_fraction"] for node in reports
        ),
        "parent_q2_worst_complex_otf_abs_gap": (
            parent_manifest["dense_target_pitch_binned_vs_direct_worst_complex_otf_abs_delta"]
        ),
        "q4_worst_complex_otf_abs_gap": worst,
        "maximum_histogram_complex_otf_abs_delta_allowed": tolerance,
        "histogram_target_status": (
            "numeric_gap_within_declared_tolerance_model_only" if within
            else "inconclusive_target_histogram_gap"
        ),
        "histogram_gap_within_declared_tolerance_model_only": within,
        "output_psf_float32_bytes": output_bytes,
        "estimated_working_array_bytes_not_peak_rss": estimated_bytes,
        "limits": {
            "max_output_psf_bytes": max_output_psf_bytes,
            "max_estimated_working_array_bytes": max_estimated_working_array_bytes,
            "soft_timeout_s": float(soft_timeout_s),
            "hard_process_rss_or_time_guard": False,
        },
        "optical_psf_semantics": "geometric_pre_pixel_ray_histogram",
        "diffraction_included": False,
        "pixel_aperture_included": False,
        "pupil_density_convergence_qualified_all_nodes": False,
        "scene_or_executed_convolution_sampling_qualified": False,
        "lens_radiometry_validated": False,
        "system_mtf_or_product_ranking_allowed": False,
    }
    archive = {
        "psf_function": derived,
        "field_height_mm": np.asarray(parent_arrays["field_height_mm"]).copy(),
        "wavelength_nm": np.asarray(parent_arrays["wavelength_nm"]).copy(),
        "source_sample_spacing_mm": np.asarray(
            parent_arrays["source_sample_spacing_mm"]
        ).copy(),
        "sample_spacing_mm": np.asarray([pitch, pitch]),
        "dense_admitted_intersections_xy_mm": points.copy(),
        "dense_node_offsets": offsets.copy(),
        "quality_json": np.asarray(json.dumps({
            "schema": SCHEMA,
            "candidate_only": True,
            "same_parent_dense_rays_reused_without_raytrace": True,
            "diffraction_included": False,
            "pixel_aperture_included": False,
            "physical_validation": False,
        }, sort_keys=True)),
        "candidate_metadata_json": np.asarray(json.dumps(
            manifest, sort_keys=True, allow_nan=False,
        )),
    }
    destination = Path(output_dir).expanduser().resolve()
    descriptor = None
    temporary_name = None
    destination.mkdir(parents=True, exist_ok=True)
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".q4-geometric-rebin-", suffix=".npz", dir=destination,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            np.savez_compressed(handle, **archive)
            handle.flush()
            os.fsync(handle.fileno())
        temporary = Path(temporary_name)
        archive_sha = _file_sha(temporary)
        archive_path = destination / f"{archive_sha}.npz"
        if archive_path.exists():
            if _file_sha(archive_path) != archive_sha:
                raise ValueError("Existing q4 content-addressed archive is corrupt.")
            temporary.unlink()
        else:
            os.replace(temporary, archive_path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        archive_path.unlink(missing_ok=True)
        raise ValueError("q4 archive exceeds compressed byte policy.")
    manifest["candidate_asset_sha256"] = archive_sha
    manifest["candidate_asset_file"] = archive_path.name
    manifest["candidate_asset_path"] = str(archive_path)
    report_path = destination / f"{archive_sha}.json"
    payload = json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("q4 report exceeds byte policy.")
    if report_path.exists() and report_path.read_text(encoding="utf-8") != payload:
        raise ValueError("Existing q4 content-addressed report differs.")
    if not report_path.exists():
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".q4-geometric-rebin-report-", suffix=".json", dir=destination,
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


def load_q4_geometric_psf_rebin(
    report_path: str | Path,
    *,
    expected_parent_run_id: str,
    expected_parent_asset_sha256: str,
    expected_lens_revision_id: str,
    expected_source_psf_asset_sha256: str,
    candidate_asset_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Recompute q4 histograms/OTF from bundled rays before accepting an asset."""
    path = Path(report_path).expanduser().resolve()
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("q4 report exceeds byte policy.")
    report = json.loads(path.read_text(encoding="utf-8"))
    relocated = candidate_asset_path is not None
    archive_path = (
        Path(candidate_asset_path).expanduser().resolve() if relocated
        else (path.parent / report["candidate_asset_file"]).resolve()
    )
    if (
        report.get("schema") != SCHEMA or report.get("status") != STATUS
        or report.get("parent_candidate_run_id") != expected_parent_run_id
        or report.get("parent_candidate_asset_sha256") != expected_parent_asset_sha256
        or report.get("lens_revision_id") != expected_lens_revision_id
        or report.get("source_psf_asset_sha256") != expected_source_psf_asset_sha256
        or report.get("same_parent_dense_rays_reused_without_raytrace") is not True
        or report.get("all_stored_field_wavelength_nodes_completed") is not True
        or report.get("histogram_refinement_factor") != REFINEMENT_FACTOR
        or report.get("candidate_asset_file") != f"{report.get('candidate_asset_sha256')}.npz"
        or (not relocated and archive_path.parent != path.parent)
        or archive_path.stat().st_size > MAX_ARCHIVE_BYTES
        or _file_sha(archive_path) != report.get("candidate_asset_sha256")
    ):
        raise ValueError("q4 archive identity, owner lineage or checksum differs.")
    with zipfile.ZipFile(archive_path) as zipped:
        members = zipped.infolist()
        if (
            not 1 <= len(members) <= MAX_ARCHIVE_MEMBERS
            or any(not entry.filename.endswith(".npy") for entry in members)
            or sum(entry.file_size for entry in members) > MAX_DECODED_ARCHIVE_BYTES
        ):
            raise ValueError("q4 decoded archive exceeds member or byte policy.")
    with np.load(archive_path, allow_pickle=False) as source:
        arrays = {name: np.asarray(source[name]).copy() for name in source.files}
    embedded = json.loads(str(arrays["candidate_metadata_json"]))
    without_file = {key: value for key, value in report.items() if key not in (
        "candidate_asset_sha256", "candidate_asset_file", "candidate_asset_path",
        "report_path",
    )}
    source_shape = report.get("source_psf_shape_row_col_field_wave")
    target_shape = report.get("candidate_psf_shape_row_col_field_wave")
    source_pitch = np.asarray(report.get("source_histogram_pitch_mm_row_col"), dtype=float)
    target_pitch = np.asarray(report.get("candidate_histogram_pitch_mm_row_col"), dtype=float)
    if (
        embedded != without_file
        or not isinstance(source_shape, list) or len(source_shape) != 4
        or not isinstance(target_shape, list) or len(target_shape) != 4
        or source_shape[0] != source_shape[1]
        or not 8 <= source_shape[0] <= 512
        or target_shape[:2] != [(n - 1) * REFINEMENT_FACTOR + 1 for n in source_shape[:2]]
        or target_shape[2:] != source_shape[2:]
        or source_pitch.shape != (2,) or target_pitch.shape != (2,)
        or not np.isfinite(source_pitch).all() or not np.isfinite(target_pitch).all()
        or np.any(source_pitch <= 0) or np.any(target_pitch <= 0)
        or not np.allclose(source_pitch / target_pitch, REFINEMENT_FACTOR,
                           rtol=0, atol=1e-9)
        or report.get("physical_sample_center_support_mm_row_col") != [
            (side - 1) * pitch
            for side, pitch in zip(source_shape[:2], source_pitch, strict=True)
        ]
        or not np.array_equal(arrays["source_sample_spacing_mm"], source_pitch)
        or not np.array_equal(arrays["sample_spacing_mm"], target_pitch)
        or list(arrays["psf_function"].shape) != target_shape
        or arrays["psf_function"].dtype != np.float32
        or _array_sha(arrays["psf_function"], "<f4")
        != report.get("candidate_psf_array_sha256")
        or not np.isfinite(arrays["psf_function"]).all()
        or np.any(arrays["psf_function"] < 0)
        or not np.allclose(
            np.sum(arrays["psf_function"], axis=(0, 1), dtype=np.float64),
            1.0, rtol=0, atol=1e-5,
        )
        or not np.array_equal(arrays["field_height_mm"], report["field_height_mm"])
        or not np.array_equal(arrays["wavelength_nm"], report["wavelength_nm"])
        or report.get("output_psf_float32_bytes") != math.prod(target_shape) * 4
        or report["output_psf_float32_bytes"] > MAX_OUTPUT_PSF_BYTES
    ):
        raise ValueError("q4 PSF array, pitch, axes, support or embedded lineage differs.")
    points = arrays["dense_admitted_intersections_xy_mm"]
    offsets = arrays["dense_node_offsets"]
    nodes = report["node_reports"]
    if (
        points.ndim != 2 or points.shape[1] != 2 or points.dtype != np.float64
        or not np.isfinite(points).all()
        or _array_sha(points, "<f8") != report["parent_dense_intersections_array_sha256"]
        or offsets.shape != (len(nodes) + 1,) or offsets.dtype != np.int64
        or offsets[0] != 0 or offsets[-1] != len(points)
        or np.any(np.diff(offsets) <= 0)
        or len(nodes) != target_shape[2] * target_shape[3]
    ):
        raise ValueError("q4 admitted-ray evidence or node count differs.")
    targets = _targets(report["target_frequency_cycles_per_mm_row_col"])
    tolerance = float(report["maximum_histogram_complex_otf_abs_delta_allowed"])
    worst = 0.0
    minimum_retained = 1.0
    for index, node in enumerate(nodes):
        fi, wi = divmod(index, target_shape[3])
        subset = points[offsets[index]:offsets[index + 1]]
        if (
            node.get("field_index") != fi or node.get("wavelength_index") != wi
            or node.get("admitted_rays") != len(subset)
            or node.get("admitted_intersections_xy_mm_sha256") != _array_sha(subset, "<f8")
        ):
            raise ValueError("q4 ray segment or node order differs.")
        histogram, retained = _histogram(
            subset, target_shape[0], float(target_pitch[0]), min_retained=1e-12,
        )
        if (
            not np.array_equal(histogram.astype(np.float32),
                               arrays["psf_function"][:, :, fi, wi])
            or not math.isclose(retained, node["q4_fixed_support_retained_ray_fraction"],
                                rel_tol=0, abs_tol=1e-12)
        ):
            raise ValueError("q4 ray deposition differs from stored PSF.")
        direct = tuple(_complex_pairs(axis) for axis in _direct_ray_axis_otf(subset, targets))
        binned = _otf_pairs(arrays["psf_function"][:, :, fi, wi],
                            float(target_pitch[0]), targets)
        if (
            not np.allclose(direct[0], node["direct_ray_complex_otf_row_y_up"],
                            rtol=0, atol=1e-10)
            or not np.allclose(direct[1], node["direct_ray_complex_otf_col_x_right"],
                               rtol=0, atol=1e-10)
            or not np.allclose(binned[0], node["q4_binned_complex_otf_row_y_up"],
                               rtol=0, atol=1e-10)
            or not np.allclose(binned[1], node["q4_binned_complex_otf_col_x_right"],
                               rtol=0, atol=1e-10)
        ):
            raise ValueError("q4 complex OTF differs from persisted ray/bin data.")
        gap = _max_complex_delta(direct, binned)
        if not math.isclose(gap, node["q4_binned_vs_direct_max_complex_otf_abs_delta"],
                            rel_tol=0, abs_tol=1e-10):
            raise ValueError("q4 node frequency gap differs.")
        worst = max(worst, gap)
        minimum_retained = min(minimum_retained, retained)
    within = worst <= tolerance
    if (
        not math.isfinite(tolerance) or not 0 < tolerance <= 0.1
        or not math.isclose(worst, report["q4_worst_complex_otf_abs_gap"],
                            rel_tol=0, abs_tol=1e-10)
        or not math.isclose(
            minimum_retained, report["minimum_q4_fixed_support_retained_ray_fraction"],
            rel_tol=0, abs_tol=1e-10,
        )
        or report.get("histogram_gap_within_declared_tolerance_model_only") is not within
        or report.get("histogram_target_status") != (
            "numeric_gap_within_declared_tolerance_model_only" if within
            else "inconclusive_target_histogram_gap"
        )
        or report.get("pupil_density_convergence_qualified_all_nodes") is not False
        or report.get("system_mtf_or_product_ranking_allowed") is not False
    ):
        raise ValueError("q4 declared frequency decision or claim differs.")
    return report, arrays
