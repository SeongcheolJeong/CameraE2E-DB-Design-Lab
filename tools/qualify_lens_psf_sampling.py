#!/usr/bin/env python3
"""Re-trace one pinned Lens DB field/band and re-histogram identical rays.

This produces an independent target-pitch reference for the existing geometric
PSF interpolation. It does not replace the production PSF or qualify physical
MTF, diffraction, lens radiometry, or a camera product.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from scipy.ndimage import map_coordinates, rotate

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from camera_platform.lens_catalog_bridge import _asset, _revision_source  # noqa: E402
from pyisetcam.optics import _raytrace_psf_grid, _raytrace_psf_support_axes  # noqa: E402
from tools.build_lens_patent_raytrace_psf_grid import (  # noqa: E402
    CODE_VERSION,
    build_rayoptics_model,
    histogram_psf,
    load_candidates,
    pupil_disk_samples,
    read_csv,
    trace_image_ray,
    validate_source,
)

SCHEMA = "camerae2e_pinned_lens_trace_once_psf_pitch_reference_v1"
FREQUENCIES_CYCLES_PER_MM = (100.0, 250.0, 500.0)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _points_sha(points_mm: np.ndarray) -> str:
    values = np.ascontiguousarray(points_mm, dtype="<f8")
    digest = hashlib.sha256()
    digest.update(str(values.shape).encode())
    digest.update(values.tobytes())
    return digest.hexdigest()


def _metrics(hist: np.ndarray, pitch_mm: float) -> dict[str, Any]:
    values = np.asarray(hist, dtype=float)
    if values.ndim != 2 or np.any(values < 0) or not np.isfinite(values).all():
        raise ValueError("Histogram must be a finite nonnegative plane.")
    values = values / values.sum()
    row = ((values.shape[0] - 1) / 2 - np.arange(values.shape[0])) * pitch_mm
    col = (np.arange(values.shape[1]) - (values.shape[1] - 1) / 2) * pitch_mm
    yy, xx = np.meshgrid(row, col, indexing="ij")
    cx, cy = float(np.sum(values * xx)), float(np.sum(values * yy))
    radial = np.hypot(xx - cx, yy - cy).ravel()
    order = np.argsort(radial)
    radius80 = float(radial[order][np.searchsorted(np.cumsum(values.ravel()[order]), 0.8)])
    x_profile = values.sum(axis=0)
    y_profile = values.sum(axis=1)
    return {
        "centroid_um_xy": [cx * 1000, cy * 1000],
        "rms_width_um_xy": [
            math.sqrt(float(np.sum(values * (xx - cx) ** 2))) * 1000,
            math.sqrt(float(np.sum(values * (yy - cy) ** 2))) * 1000,
        ],
        "encircled_energy_80_radius_um": radius80 * 1000,
        "mtf_x_at_cycles_per_mm": {
            f"{frequency:g}": float(abs(np.sum(
                x_profile * np.exp(-2j * np.pi * frequency * col)
            ))) for frequency in FREQUENCIES_CYCLES_PER_MM
        },
        "mtf_y_at_cycles_per_mm": {
            f"{frequency:g}": float(abs(np.sum(
                y_profile * np.exp(-2j * np.pi * frequency * row)
            ))) for frequency in FREQUENCIES_CYCLES_PER_MM
        },
    }


def _interpolate_stored(
    stored: np.ndarray, source_pitch_mm: float, target_pitch_mm: float,
    azimuth_deg: float,
) -> tuple[np.ndarray, float]:
    table = {
        "function": stored[:, :, None, None],
        "sample_spacing_mm": [source_pitch_mm, source_pitch_mm],
        "coordinate_convention": "pixel_centers",
    }
    source_x, source_y = _raytrace_psf_support_axes(table)
    target_x, target_y = _raytrace_psf_grid(target_pitch_mm, table)
    row, col = np.meshgrid(
        (target_y - source_y[0]) / (source_y[1] - source_y[0]),
        (target_x - source_x[0]) / (source_x[1] - source_x[0]),
        indexing="ij",
    )
    rotated = (
        stored if azimuth_deg == 0 else rotate(
            stored, azimuth_deg, reshape=False, order=1,
            mode="constant", cval=0.0, prefilter=False,
        )
    )
    sampled = map_coordinates(
        rotated, [row, col], order=1, mode="constant", cval=0.0,
        prefilter=False,
    )
    if sampled.sum() <= 0:
        raise ValueError("Stored PSF interpolation missed all ray mass.")
    return sampled / sampled.sum(), float(rotated.sum() / stored.sum())


def _rotate_ray_points(points_mm: np.ndarray, azimuth_deg: float) -> np.ndarray:
    angle = math.radians(azimuth_deg)
    cosine, sine = math.cos(angle), math.sin(angle)
    transform = np.array([[cosine, -sine], [sine, cosine]])
    return np.asarray(points_mm, dtype=float) @ transform.T


def compare_same_ray_histograms(
    points_mm: np.ndarray, stored_histogram: np.ndarray,
    *, source_pitch_mm: float, target_pitch_mm: float,
    min_retained_energy: float, azimuth_deg: float = 0.0,
) -> dict[str, Any]:
    """Reference vs current interpolation on the same physical PSF support."""
    stored = np.asarray(stored_histogram, dtype=float)
    points = np.asarray(points_mm, dtype=float)
    if (
        stored.ndim != 2 or stored.shape[0] != stored.shape[1]
        or stored.shape[0] < 8 or points.ndim != 2 or points.shape[1] != 2
        or not np.isfinite(points).all() or not np.isfinite(stored).all()
        or np.any(stored < 0) or stored.sum() <= 0
        or not 0 < source_pitch_mm < math.inf
        or not 0 < target_pitch_mm < math.inf
        or not 0 < min_retained_energy <= 1
        or not math.isfinite(azimuth_deg) or not 0 <= azimuth_deg < 360
    ):
        raise ValueError("Declare finite PSF/ray inputs, pitches and retained-energy threshold.")
    source_side = int(stored.shape[0])
    # Match the source center-to-center support, including even→odd changes.
    source_half_mm = (source_side - 1) * source_pitch_mm / 2
    target_radius = math.ceil(source_half_mm / target_pitch_mm)
    target_side = 2 * target_radius + 1
    if target_side > 2049:
        raise ValueError("Target PSF exceeds the 2049-sample diagnostic limit.")
    source_rehist, source_retained = histogram_psf(
        points, source_side, source_pitch_mm,
        min_retained_energy=1e-12, return_retained=True,
    )
    rotated_points = _rotate_ray_points(points, azimuth_deg)
    target_rehist, target_retained = histogram_psf(
        rotated_points, target_side, target_pitch_mm,
        min_retained_energy=1e-12, return_retained=True,
    )
    interpolated, rotation_retained = _interpolate_stored(
        stored, source_pitch_mm, target_pitch_mm, azimuth_deg,
    )
    if interpolated.shape != target_rehist.shape:
        raise ValueError("Reference and current interpolation do not share target support.")
    max_source_cell_error = float(np.max(np.abs(source_rehist - stored)))
    source_reproduced = max_source_cell_error <= 2e-7
    reference = _metrics(target_rehist, target_pitch_mm)
    current = _metrics(interpolated, target_pitch_mm)
    return {
        "point_set_sha256": _points_sha(points),
        "azimuth_deg_from_positive_x": azimuth_deg,
        "rotated_point_set_sha256": _points_sha(rotated_points),
        "same_successful_ray_points_for_source_and_target": True,
        "source_psf_samples_row_col": list(stored.shape),
        "target_psf_samples_row_col": list(target_rehist.shape),
        "source_pitch_um": source_pitch_mm * 1000,
        "target_pitch_um": target_pitch_mm * 1000,
        "source_center_to_center_half_span_um": source_half_mm * 1000,
        "target_center_to_center_half_span_um": target_radius * target_pitch_mm * 1000,
        "source_rehistogram_retained_ray_fraction": source_retained,
        "target_rehistogram_retained_ray_fraction": target_retained,
        "required_retained_ray_fraction": min_retained_energy,
        "strict_rotation_retained_stored_mass_before_unit_sum": rotation_retained,
        "source_rehistogram_max_abs_cell_error_vs_pinned_float32": max_source_cell_error,
        "source_rehistogram_matches_pinned_asset": source_reproduced,
        "target_reference": reference,
        "current_stored_histogram_interpolation": current,
        "target_reference_vs_interpolation_max_abs_cell_error": float(np.max(
            np.abs(target_rehist - interpolated)
        )),
        "target_reference_vs_interpolation_l1_mass_difference": float(np.sum(
            np.abs(target_rehist - interpolated)
        )),
        "target_reference_vs_interpolation_metric_delta": {
            "centroid_um_xy": [a - b for a, b in zip(
                current["centroid_um_xy"], reference["centroid_um_xy"], strict=True
            )],
            "rms_width_um_xy": [a - b for a, b in zip(
                current["rms_width_um_xy"], reference["rms_width_um_xy"], strict=True
            )],
            "encircled_energy_80_radius_um": (
                current["encircled_energy_80_radius_um"]
                - reference["encircled_energy_80_radius_um"]
            ),
            "mtf_x_at_cycles_per_mm": {
                key: current["mtf_x_at_cycles_per_mm"][key] - value
                for key, value in reference["mtf_x_at_cycles_per_mm"].items()
            },
            "mtf_y_at_cycles_per_mm": {
                key: current["mtf_y_at_cycles_per_mm"][key] - value
                for key, value in reference["mtf_y_at_cycles_per_mm"].items()
            },
        },
        "reference_valid_for_pinned_source": (
            source_reproduced and source_retained >= min_retained_energy
            and target_retained >= min_retained_energy
        ),
        "production_psf_resampling_qualified": False,
        "production_psf_replaced": False,
        "physical_mtf_comparison_allowed": False,
    }


def qualify_revision(
    revision_id: str, *, catalog_root: Path, field_index: int,
    wavelength_nm: float, target_pitch_um: float, max_traces: int = 5000,
    max_seconds: float = 120.0, azimuth_deg: float = 0.0,
) -> dict[str, Any]:
    """Trace one pinned field/band, then compare saved interpolation to ray truth."""
    started = time.monotonic()
    if (
        not revision_id or type(field_index) is not int or field_index < 0
        or not math.isfinite(wavelength_nm) or not math.isfinite(target_pitch_um)
        or target_pitch_um <= 0 or type(max_traces) is not int
        or not 1 <= max_traces <= 5000 or not 0 < max_seconds <= 300
        or not math.isfinite(azimuth_deg) or not 0 <= azimuth_deg < 360
    ):
        raise ValueError("Declare a field, band, target pitch, <=5000 traces and <=300 s.")
    root = catalog_root.expanduser().resolve()
    revision, snapshot, db_path, db_sha = _revision_source(revision_id, root)
    simulation_id, psf_path, validation, _ = _asset(revision, snapshot, db_path, root)
    with np.load(psf_path, allow_pickle=False) as asset:
        settings = json.loads(str(asset["build_settings_json"]))
        quality = json.loads(str(asset["quality_json"]))
        fields = np.asarray(asset["field_height_mm"], dtype=float).copy()
        waves = np.asarray(asset["wavelength_nm"], dtype=float).copy()
        pupil_saved = np.asarray(asset["pupil_coordinates"], dtype=float).copy()
        weights = np.asarray(asset["pupil_weights"], dtype=float).copy()
        success_saved = np.asarray(asset["success_counts"], dtype=int).copy()
        chiefs_saved = np.asarray(asset["chief_points_mm"], dtype=float).copy()
        pitch = np.asarray(asset["sample_spacing_mm"], dtype=float).copy()
        if field_index >= len(fields):
            raise ValueError("Requested field is outside pinned PSF coverage.")
        matching_waves = np.flatnonzero(np.isclose(waves, wavelength_nm, rtol=0, atol=1e-9))
        if len(matching_waves) != 1:
            raise ValueError("Requested wavelength must be a stored PSF band.")
        wave_index = int(matching_waves[0])
        stored = np.asarray(asset["psf_function"][:, :, field_index, wave_index], dtype=float)
    generator = ROOT / "tools" / "build_lens_patent_raytrace_psf_grid.py"
    if (
        settings.get("model") != CODE_VERSION
        or settings.get("generator_sha256") != _sha(generator)
        or quality.get("source_prescription_sha256") != validation["source_prescription_sha256"]
        or len(pitch) != 2 or not np.isclose(pitch[0], pitch[1], rtol=0, atol=1e-15)
        or not np.isfinite(pitch).all() or np.any(pitch <= 0)
        or weights.shape != (len(fields), len(waves), len(pupil_saved))
        or chiefs_saved.shape != (len(fields), len(waves), 2)
        or len(pupil_saved) > max_traces
    ):
        raise ValueError("Pinned generator, source, square pitch or pupil contract differs.")
    requested_target_side = 2 * math.ceil(
        (stored.shape[0] - 1) * pitch[0] / (2 * target_pitch_um * 1e-3)
    ) + 1
    if requested_target_side > 2049:
        raise ValueError("Target PSF exceeds the 2049-sample diagnostic limit.")
    candidates = load_candidates(SimpleNamespace(
        camerae2e_db=db_path, include_non_ready=False,
        simulation_id=simulation_id, company=None, limit=1,
    ))
    if len(candidates) != 1:
        raise ValueError("Pinned Lens DB simulation is not uniquely camera-ready.")
    candidate = candidates[0]
    csv_root = db_path.parent / "reviewed_prescriptions"
    csv_path = (csv_root / candidate.source_surfaces_csv).resolve()
    if not csv_path.is_relative_to(csv_root.resolve()) or not csv_path.is_file():
        raise ValueError("Pinned reviewed prescription is unavailable inside Lens DB.")
    if _sha(csv_path) != quality.get("source_file_sha256"):
        raise ValueError("Reviewed prescription bytes differ from pinned PSF build.")
    surfaces = read_csv(csv_path)
    source = validate_source(candidate, surfaces, float(settings["object_distance_m"]))
    if source["prescription_sha256"] != validation["source_prescription_sha256"]:
        raise ValueError("Retraced prescription differs from pinned Lens revision.")
    model, traced_fields = build_rayoptics_model(
        candidate, surfaces, field_count=int(settings["field_count"]),
        wavelengths_nm=np.asarray(settings["wavelengths"], dtype=float),
        reference_wavelength_nm=float(settings["reference_wavelength_nm"]),
        object_distance_m=float(settings["object_distance_m"]),
        efl_relative_tolerance=float(settings["efl_relative_tolerance"]),
    )
    if not np.allclose(fields, traced_fields, rtol=0, atol=1e-9):
        raise ValueError("Retraced field coordinates differ from pinned asset.")
    pupil = pupil_disk_samples(int(settings["pupil_samples"]))
    if not np.array_equal(pupil, pupil_saved):
        raise ValueError("Retraced pupil locations differ from pinned asset.")
    field = model["optical_spec"]["fov"].fields[field_index]
    chief, _ = trace_image_ray(model, [0.0, 0.0], field, wavelength_nm)
    if not np.allclose(chief, chiefs_saved[field_index, wave_index], rtol=0, atol=1e-9):
        raise ValueError("Retraced chief ray differs from pinned asset.")
    points: list[np.ndarray] = []
    accepted_mask = np.zeros(len(pupil), dtype=bool)
    for index, pupil_location in enumerate(pupil):
        if time.monotonic() - started > max_seconds:
            raise TimeoutError("Selected field/band exceeded the declared tracing time limit.")
        try:
            point, _ = trace_image_ray(model, pupil_location, field, wavelength_nm)
        except Exception as exc:
            if not type(exc).__name__.startswith("Trace"):
                raise
            continue
        accepted_mask[index] = True
        points.append(point - chief)
    if (
        len(points) != int(success_saved[field_index, wave_index])
        or not np.array_equal(accepted_mask, weights[field_index, wave_index] > 0)
    ):
        raise ValueError("Retraced successful-ray mask differs from pinned PSF asset.")
    result = compare_same_ray_histograms(
        np.asarray(points), stored, source_pitch_mm=float(pitch[0]),
        target_pitch_mm=target_pitch_um * 1e-3,
        min_retained_energy=float(settings["min_retained_energy"]),
        azimuth_deg=azimuth_deg,
    )
    return {
        "schema": SCHEMA,
        "status": (
            "independent_target_pitch_reference_available"
            if result["reference_valid_for_pinned_source"] else "inconclusive_source_reproduction"
        ),
        "lens_revision_id": revision_id,
        "lens_revision_content_sha256": revision["content_sha256"],
        "lens_db_sha256": db_sha,
        "simulation_id": simulation_id,
        "psf_asset_sha256": _sha(psf_path),
        "reviewed_prescription_sha256": _sha(csv_path),
        "generator_sha256": _sha(generator),
        "field_index": field_index,
        "field_height_mm": float(fields[field_index]),
        "wavelength_nm": wavelength_nm,
        "azimuth_deg_from_positive_x": azimuth_deg,
        "traced_pupil_ray_count": len(pupil),
        "successful_ray_count": len(points),
        "max_traces": max_traces,
        "max_seconds": max_seconds,
        "time_bound_semantics": (
            "soft_trace_loop_deadline; model construction and an individual ray trace "
            "are not process-killed"
        ),
        "elapsed_seconds": time.monotonic() - started,
        "same_ray_histogram_comparison": result,
        "qualification_scope": (
            "one pinned geometric Lens DB field/wavelength/azimuth and one target pitch; "
            "no diffraction, source Scene, sensor aperture, "
            "physical MTF or product validation"
        ),
        "physical_mtf_comparison_allowed": False,
        "production_psf_resampling_qualified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision-id", required=True)
    parser.add_argument("--catalog-root", type=Path, default=ROOT / "camerae2e_db")
    parser.add_argument("--field-index", type=int, default=0)
    parser.add_argument("--wavelength-nm", type=float, default=550.0)
    parser.add_argument("--azimuth-deg", type=float, default=0.0)
    parser.add_argument("--target-pitch-um", type=float, required=True)
    parser.add_argument("--max-traces", type=int, default=5000)
    parser.add_argument("--max-seconds", type=float, default=120.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = qualify_revision(
        args.revision_id, catalog_root=args.catalog_root,
        field_index=args.field_index, wavelength_nm=args.wavelength_nm,
        target_pitch_um=args.target_pitch_um, max_traces=args.max_traces,
        max_seconds=args.max_seconds, azimuth_deg=args.azimuth_deg,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
