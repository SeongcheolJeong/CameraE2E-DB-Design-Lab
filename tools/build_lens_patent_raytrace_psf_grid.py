#!/usr/bin/env python3
"""Build RayOptics-derived PSF grids for Lens_Patent_DB rows.

Run this with the RayOptics Python environment, for example:

    /Users/seongcheoljeong/RayOptics/rayoptics-env/bin/python3.12 \
        tools/build_lens_patent_raytrace_psf_grid.py --limit 1

The output is intentionally separate from the SQLite catalog because PSF arrays
can grow quickly. Each generated `.npz` file contains CameraE2E-compatible
raytrace arrays plus enough metadata for `pyisetcam.lens_patents` to load them.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sqlite3
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import rayoptics
from rayoptics.elem import profiles
from rayoptics.environment import FieldSpec, OpticalModel, PupilSpec, WvlSpec
from rayoptics.raytr.trace import trace_base

CAMERAE2E_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CAMERAE2E_ROOT / "src"))
from pyisetcam.lens_patents import lens_patent_default_db_path  # noqa: E402
from pyisetcam.lens_validation import validate_lens_prescription  # noqa: E402

CODE_VERSION = "rayoptics_geometric_psf_contract_v2"
DEFAULT_CAMERA_DB = (
    CAMERAE2E_ROOT
    / "src"
    / "pyisetcam"
    / "data"
    / "lens_patents"
    / "lens_patent_simulation_v6.sqlite"
)
DEFAULT_LENS_DB_ROOT = Path.home() / "Lens_Patent_DB"
DEFAULT_EXPORT_ROOT = DEFAULT_LENS_DB_ROOT / "exports" / "prescriptions_expanded_v6"
DEFAULT_OUT_DIR = CAMERAE2E_ROOT / "reports" / "lens_psf_v2" / "raytrace_psf_v2"

PSF_PRESETS: dict[str, dict[str, Any]] = {
    "debug": {
        "field_count": 3,
        "wavelengths": "550",
        "pupil_samples": 13,
        "psf_size": 64,
    },
    "standard": {
        "field_count": 5,
        "wavelengths": "450,550,650",
        "pupil_samples": 49,
        "psf_size": 384,
    },
    "production": {
        "field_count": 5,
        "wavelengths": "450,550,650",
        "pupil_samples": 65,
        "psf_size": 512,
    },
    "golden": {
        "field_count": 7,
        "wavelengths": "450,500,550,600,650",
        "pupil_samples": 97,
        "psf_size": 768,
    },
}


@dataclass(frozen=True)
class Candidate:
    simulation_id: str
    lens_id: str
    company: str
    publication_number: str
    example_label: str
    configuration: str
    f_number: float
    focal_length_mm: float
    image_height_mm: float | None
    half_field_deg: float | None
    source_surfaces_csv: str
    optics_json: dict[str, Any]


@dataclass
class TraceBundle:
    psf_function: np.ndarray
    field_height_mm: np.ndarray
    wavelength_nm: np.ndarray
    sample_spacing_mm: np.ndarray
    geometry_function: np.ndarray
    relative_illumination_function: np.ndarray
    success_counts: np.ndarray
    requested_counts: np.ndarray
    chief_points_mm: np.ndarray
    notes: list[str]
    pupil_throughput_fraction: np.ndarray
    image_direction_cosines: np.ndarray
    pupil_weights: np.ndarray
    pupil_coordinates: np.ndarray
    chief_direction_cosines: np.ndarray
    chief_ray_angle_deg: np.ndarray
    retained_energy_fraction: np.ndarray
    quality: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=sorted(PSF_PRESETS), default="debug")
    parser.add_argument("--list-presets", action="store_true")
    parser.add_argument("--camerae2e-db", type=Path, default=lens_patent_default_db_path())
    parser.add_argument("--export-root", type=Path)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--simulation-id")
    parser.add_argument("--company")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--field-count", type=int)
    parser.add_argument("--wavelengths")
    parser.add_argument("--pupil-samples", type=int)
    parser.add_argument("--psf-size", type=int)
    parser.add_argument("--sample-spacing-um", type=float)
    parser.add_argument("--min-success-fraction", type=float, default=0.25)
    parser.add_argument("--reference-wavelength-nm", type=float, default=550.0)
    parser.add_argument("--object-distance-m", type=float, default=math.inf,
                        help="Actual object conjugate; required for close/1m configurations.")
    parser.add_argument("--efl-relative-tolerance", type=float, default=0.05)
    parser.add_argument("--min-retained-energy", type=float, default=0.999)
    parser.add_argument("--sensor-pitch-um", type=float,
                        help="Optional target: require PSF spacing <= half this pitch.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--reset-manifest", action="store_true")
    parser.add_argument("--include-non-ready", action="store_true")
    args = parser.parse_args()
    if args.export_root is None:
        version = re.search(r"_v(\d+)", args.camerae2e_db.name)
        args.export_root = DEFAULT_LENS_DB_ROOT / "exports" / f"prescriptions_expanded_v{version.group(1) if version else '6'}"
    if not args.list_presets:
        apply_preset_defaults(args)
    return args


def main() -> int:
    args = parse_args()
    if args.list_presets:
        print(json.dumps(PSF_PRESETS, indent=2, sort_keys=True))
        return 0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    candidates = load_candidates(args)
    manifest_path = args.out_dir / "manifest.json"
    manifest_rows_by_id = {} if args.reset_manifest else load_existing_manifest_rows(manifest_path)
    build_settings = effective_build_settings(args)
    for candidate in candidates:
        output_path = args.out_dir / f"{safe_id(candidate.simulation_id)}.npz"
        row: dict[str, Any] = {
            "simulation_id": candidate.simulation_id,
            "lens_id": candidate.lens_id,
            "company": candidate.company,
            "publication_number": candidate.publication_number,
            "example_label": candidate.example_label,
            "configuration": candidate.configuration,
            "file": output_path.name,
            "status": "pending",
            "contract_version": 2,
            "preset": args.preset,
            "built_at": datetime.now(UTC).isoformat(),
        }
        try:
            reviewed_path = args.camerae2e_db.parent / "reviewed_prescriptions" / candidate.source_surfaces_csv
            source_path = reviewed_path if reviewed_path.exists() else args.export_root / candidate.source_surfaces_csv
            surfaces = read_csv(source_path)
            preflight = validate_source(candidate, surfaces, args.object_distance_m)
            if output_path.exists() and not args.overwrite:
                with np.load(output_path, allow_pickle=False) as existing:
                    previous = json.loads(str(existing["build_settings_json"]))
                    metadata = json.loads(str(existing["optics_json"]))["raytrace"]
                    if (metadata.get("contract_version") != 2
                            or metadata.get("source_prescription_sha256") != preflight["prescription_sha256"]
                            or previous != build_settings):
                        raise ValueError("Existing asset is legacy or stale; choose a new output directory or explicitly rebuild with --overwrite.")
                    row.update({
                        "status": "generated", "cache_reused": True,
                        "psf_shape": list(existing["psf_function"].shape),
                        "field_height_mm": existing["field_height_mm"].tolist(),
                        "wavelength_nm": existing["wavelength_nm"].tolist(),
                        "sample_spacing_mm": existing["sample_spacing_mm"].tolist(),
                        "success_min": int(existing["success_counts"].min()),
                        "success_max": int(existing["success_counts"].max()),
                        "quality": metadata.get("quality", {}),
                    })
                manifest_rows_by_id[candidate.simulation_id] = row
                continue
            bundle = build_raytrace_psf_bundle(
                candidate,
                surfaces,
                wavelengths_nm=parse_wavelengths(args.wavelengths),
                field_count=args.field_count,
                pupil_samples=args.pupil_samples,
                psf_size=args.psf_size,
                sample_spacing_um=args.sample_spacing_um,
                min_success_fraction=args.min_success_fraction,
                reference_wavelength_nm=args.reference_wavelength_nm,
                object_distance_m=args.object_distance_m,
                efl_relative_tolerance=args.efl_relative_tolerance,
                min_retained_energy=args.min_retained_energy,
                sensor_pitch_um=args.sensor_pitch_um,
            )
            bundle.quality["source_file"] = str(source_path.resolve())
            bundle.quality["source_file_sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
            optics_json = build_raytrace_optics_json(candidate, bundle, output_path.name)
            np.savez_compressed(
                output_path,
                psf_function=bundle.psf_function.astype(np.float32),
                field_height_mm=bundle.field_height_mm.astype(np.float64),
                wavelength_nm=bundle.wavelength_nm.astype(np.float64),
                sample_spacing_mm=bundle.sample_spacing_mm.astype(np.float64),
                geometry_function=bundle.geometry_function.astype(np.float64),
                relative_illumination_function=bundle.relative_illumination_function.astype(np.float64),
                success_counts=bundle.success_counts.astype(np.int32),
                requested_counts=bundle.requested_counts.astype(np.int32),
                chief_points_mm=bundle.chief_points_mm.astype(np.float64),
                pupil_throughput_fraction=bundle.pupil_throughput_fraction,
                image_direction_cosines=bundle.image_direction_cosines,
                pupil_weights=bundle.pupil_weights,
                pupil_coordinates=bundle.pupil_coordinates,
                chief_direction_cosines=bundle.chief_direction_cosines,
                chief_ray_angle_deg=bundle.chief_ray_angle_deg,
                retained_energy_fraction=bundle.retained_energy_fraction,
                quality_json=np.array(json.dumps(bundle.quality, sort_keys=True)),
                optics_json=np.array(json.dumps(optics_json, sort_keys=True)),
                build_settings_json=np.array(json.dumps(build_settings, sort_keys=True)),
            )
            row.update(
                {
                    "status": "generated",
                    "psf_shape": list(bundle.psf_function.shape),
                    "sample_spacing_mm": bundle.sample_spacing_mm.tolist(),
                    "field_height_mm": bundle.field_height_mm.tolist(),
                    "wavelength_nm": bundle.wavelength_nm.tolist(),
                    "success_min": int(np.min(bundle.success_counts)),
                    "success_max": int(np.max(bundle.success_counts)),
                    "notes": bundle.notes,
                    "quality": bundle.quality,
                }
            )
        except Exception as exc:  # noqa: BLE001 - manifest should record all per-lens failures.
            row.update(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                }
            )
        manifest_rows_by_id[candidate.simulation_id] = row

    manifest_rows = sorted(
        manifest_rows_by_id.values(),
        key=lambda row: (str(row.get("company", "")), str(row.get("simulation_id", ""))),
    )
    write_manifest(manifest_path, args, manifest_rows)
    print(json.dumps(summarize_manifest(manifest_rows), indent=2, sort_keys=True))
    return 1 if any(row.get("status") == "failed" for row in manifest_rows_by_id.values()) else 0


def load_existing_manifest_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    rows = payload.get("rows", [])
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        simulation_id = row.get("simulation_id")
        if simulation_id:
            result[str(simulation_id)] = dict(row)
    return result


def merge_existing_row(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return incoming
    if existing.get("status") == "generated":
        return existing
    merged = dict(existing)
    merged.update(incoming)
    return merged


def apply_preset_defaults(args: argparse.Namespace) -> None:
    preset = PSF_PRESETS[str(args.preset)]
    for key, value in preset.items():
        if getattr(args, key) is None:
            setattr(args, key, value)

    if int(args.field_count) < 1:
        raise ValueError("--field-count must be positive.")
    if int(args.pupil_samples) < 1:
        raise ValueError("--pupil-samples must be positive.")
    if int(args.psf_size) < 8:
        raise ValueError("--psf-size must be at least 8.")
    if args.sample_spacing_um is not None and float(args.sample_spacing_um) <= 0.0:
        raise ValueError("--sample-spacing-um must be positive when supplied.")
    parse_wavelengths(str(args.wavelengths))
    if not 0 < args.min_success_fraction <= 1:
        raise ValueError("--min-success-fraction must be in (0,1].")
    if not 0 < args.min_retained_energy <= 1:
        raise ValueError("--min-retained-energy must be in (0,1].")
    if args.reference_wavelength_nm <= 0 or not math.isfinite(args.reference_wavelength_nm):
        raise ValueError("Reference wavelength must be finite and positive.")
    if args.object_distance_m <= 0 or math.isnan(args.object_distance_m):
        raise ValueError("Object distance must be positive or infinity.")
    if not 0 < args.efl_relative_tolerance <= 0.25:
        raise ValueError("EFL tolerance must be in (0,0.25].")
    if args.sensor_pitch_um is not None and args.sensor_pitch_um <= 0:
        raise ValueError("Sensor pitch must be positive.")


def effective_build_settings(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "preset": str(args.preset),
        "field_count": int(args.field_count),
        "wavelengths": parse_wavelengths(str(args.wavelengths)).tolist(),
        "pupil_samples": int(args.pupil_samples),
        "psf_size": int(args.psf_size),
        "sample_spacing_um": args.sample_spacing_um,
        "min_success_fraction": float(args.min_success_fraction),
        "model": CODE_VERSION,
        "reference_wavelength_nm": args.reference_wavelength_nm,
        "object_distance_m": args.object_distance_m,
        "efl_relative_tolerance": args.efl_relative_tolerance,
        "min_retained_energy": args.min_retained_energy,
        "sensor_pitch_um": args.sensor_pitch_um,
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "rayoptics_version": rayoptics.__version__,
        "opticalglass_version": version("opticalglass"),
        "numpy_version": np.__version__,
    }


def load_candidates(args: argparse.Namespace) -> list[Candidate]:
    con = sqlite3.connect(f"file:{args.camerae2e_db.resolve()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    clauses = []
    params: list[Any] = []
    if not args.include_non_ready:
        clauses.append("simulation_status = 'camerae2e_ready'")
    if args.simulation_id:
        clauses.append("simulation_id = ?")
        params.append(args.simulation_id)
    if args.company:
        clauses.append("lower(company) = lower(?)")
        params.append(args.company)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = "" if args.simulation_id else "LIMIT ?"
    if not args.simulation_id:
        params.append(max(int(args.limit), 1))
    rows = con.execute(
        f"""
        SELECT *
        FROM simulation_results
        {where}
        ORDER BY company, publication_number, example_label, configuration
        {limit_sql}
        """,
        params,
    ).fetchall()
    con.close()
    candidates: list[Candidate] = []
    for row in rows:
        if row["f_number"] is None or row["focal_length_mm"] is None:
            continue
        candidates.append(
            Candidate(
                simulation_id=str(row["simulation_id"]),
                lens_id=str(row["lens_id"]),
                company=str(row["company"]),
                publication_number=str(row["publication_number"]),
                example_label=str(row["example_label"]),
                configuration=str(row["configuration"]),
                f_number=float(row["f_number"]),
                focal_length_mm=float(row["focal_length_mm"]),
                image_height_mm=none_or_float(row["image_height_mm"]),
                half_field_deg=none_or_float(row["half_field_deg"]),
                source_surfaces_csv=str(row["source_surfaces_csv"]),
                optics_json=json.loads(str(row["optics_json"])),
            )
        )
    return candidates


def validate_source(candidate: Candidate, surfaces: list[dict[str, str]], object_distance_m: float) -> dict[str, Any]:
    """Reject unresolved prescriptions before both generation and cache reuse."""
    result = validate_lens_prescription(
        surfaces, focal_length_mm=candidate.focal_length_mm,
        f_number=candidate.f_number, configuration=candidate.configuration,
    )
    errors = [issue for issue in result["issues"] if issue["severity"] == "error"]
    if errors:
        raise ValueError("Prescription validation failed: " + "; ".join(
            f"{issue['code']}: {issue['message']}" for issue in errors
        ))
    if object_distance_m <= 0 or math.isnan(object_distance_m):
        raise ValueError("Object conjugate must be positive or infinity.")
    if re.search(r"close|near|macro|(?<![a-z])\d+(?:\.\d+)?\s*(?:m|cm)(?![a-z])", candidate.configuration, re.I) and not math.isfinite(object_distance_m):
        raise ValueError("Finite-conjugate configuration requires --object-distance-m.")
    stops = [row for row in surfaces if row.get("surface_kind", "").strip().lower() == "stop"]
    if len(stops) != 1:
        raise ValueError("Exactly one explicit aperture stop is required; no stop proxy is generated.")
    for row in surfaces:
        kind = row.get("surface_kind", "").strip().lower()
        if kind == "object" or is_image_context(row):
            continue
        if positive(strict_number(row.get("effective_aperture"))) is None:
            raise ValueError(f"S{row.get('surface_label')}: physical clear aperture is unknown.")
        surface_data(row)
    return result


def is_image_context(row: dict[str, str]) -> bool:
    """Only a nonnumeric terminal caption is omitted from the sequential path.

    A numbered 'image' row with a known gap may be the cover-glass exit face;
    even zero-gap explicit interfaces must be retained to preserve refraction.
    """
    if row.get("surface_kind", "").strip().lower() != "image":
        return False
    caption = str(row.get("surface_label", "")).strip().lower() in {"image", "image plane", "ima", "im"}
    terminal_gap = str(row.get("thickness", "")).strip().lower() in {"", "plane"}
    terminal_gap = terminal_gap or (caption and strict_number(row.get("thickness")) == 0)
    return (terminal_gap
            and strict_number(row.get("radius")) in {None, 0.0}
            and strict_number(row.get("nd")) in {None, 1.0})


def build_raytrace_psf_bundle(
    candidate: Candidate,
    surfaces: list[dict[str, str]],
    *,
    wavelengths_nm: np.ndarray,
    field_count: int,
    pupil_samples: int,
    psf_size: int,
    sample_spacing_um: float | None,
    min_success_fraction: float,
    reference_wavelength_nm: float = 550.0,
    object_distance_m: float = math.inf,
    efl_relative_tolerance: float = 0.05,
    min_retained_energy: float = 0.999,
    sensor_pitch_um: float | None = None,
) -> TraceBundle:
    source = validate_source(candidate, surfaces, object_distance_m)
    wavelengths_nm = parse_wavelengths(",".join(map(str, wavelengths_nm)))
    model, field_height_mm = build_rayoptics_model(
        candidate, surfaces, field_count, wavelengths_nm,
        reference_wavelength_nm=reference_wavelength_nm,
        object_distance_m=object_distance_m,
        efl_relative_tolerance=efl_relative_tolerance,
    )
    pupil_points = pupil_disk_samples(pupil_samples)
    if len(pupil_points) < 5:
        raise ValueError("At least five pupil samples are required.")
    fields = model["optical_spec"]["fov"].fields
    n_field, n_wave, n_pupil = len(fields), len(wavelengths_nm), len(pupil_points)
    requested_counts = np.full((n_field, n_wave), n_pupil, dtype=int)
    success_counts = np.zeros((n_field, n_wave), dtype=int)
    chief_points = np.zeros((n_field, n_wave, 2))
    chief_directions = np.zeros((n_field, n_wave, 3))
    directions = np.full((n_field, n_wave, n_pupil, 3), np.nan)
    weights = np.zeros((n_field, n_wave, n_pupil))
    geometry = np.zeros((n_field, n_wave))
    point_sets = [[np.empty((0, 2)) for _ in range(n_wave)] for _ in range(n_field)]
    rejected_reasons: dict[str, int] = {}
    for fi, field in enumerate(fields):
        for wi, wavelength in enumerate(wavelengths_nm):
            chief_point, chief_direction = trace_image_ray(model, [0., 0.], field, float(wavelength))
            chief_points[fi, wi] = chief_point
            chief_directions[fi, wi] = chief_direction
            geometry[fi, wi] = float(np.linalg.norm(chief_point))
            points = []
            for pi, pupil in enumerate(pupil_points):
                try:
                    point, direction = trace_image_ray(model, pupil, field, float(wavelength))
                except Exception as exc:
                    key = type(exc).__name__
                    # Only optical tracing failures represent lost pupil samples.
                    if not key.startswith("Trace"):
                        raise
                    rejected_reasons[key] = rejected_reasons.get(key, 0) + 1
                    continue
                points.append(point - chief_point)
                directions[fi, wi, pi] = direction
                weights[fi, wi, pi] = 1.0
            success_counts[fi, wi] = len(points)
            if not points:
                raise ValueError(f"No admitted rays at field {fi}, wavelength {wavelength}.")
            weights[fi, wi] /= len(points)
            point_sets[fi][wi] = np.asarray(points)
    throughput = success_counts / requested_counts
    if float(throughput.min()) < min_success_fraction:
        raise ValueError(f"Insufficient aperture-checked rays: minimum fraction {throughput.min():.4g} < {min_success_fraction}.")
    if np.any(np.diff(geometry, axis=0) <= 0):
        raise ValueError("Geometry is not strictly increasing; mapping is invalid or outside supported field.")
    spacing_mm = choose_sample_spacing(point_sets, psf_size, sample_spacing_um)
    if sensor_pitch_um is not None and spacing_mm * 1000 > sensor_pitch_um / 2:
        raise ValueError("PSF spacing is too coarse for the specified sensor pitch; increase grid size or choose a finer supported spacing.")
    psf = np.zeros((psf_size, psf_size, n_field, n_wave))
    retained = np.zeros((n_field, n_wave))
    for fi in range(n_field):
        for wi in range(n_wave):
            psf[:, :, fi, wi], retained[fi, wi] = histogram_psf(
                point_sets[fi][wi], psf_size, spacing_mm,
                min_retained_energy=min_retained_energy, return_retained=True,
            )
    fod = model["analysis_results"]["parax_data"].fod
    quality = {
        "source_prescription_sha256": source["prescription_sha256"],
        "source_validation": source,
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "code_version": CODE_VERSION,
        "rayoptics_version": rayoptics.__version__,
        "numpy_version": np.__version__,
        "opticalglass_version": version("opticalglass"),
        "material_dispersion_model": "opticalglass ModelGlass Buchdahl2 approximation from nd/Vd",
        "material_dispersion_validated": False,
        "system_scope": "centered rotationally symmetric transmitting sequential lens",
        "focus_plane": "prescription image plane; no best-focus optimization",
        "actual_efl_mm": float(fod.efl),
        "nominal_efl_mm": candidate.focal_length_mm,
        "efl_relative_error": abs(float(fod.efl) / candidate.focal_length_mm - 1),
        "effective_f_number": float(abs(fod.fno)),
        "entrance_pupil_radius_mm": float(abs(fod.enp_radius)),
        "image_plane_last_gap_mm": float(model["seq_model"].gaps[-1].thi),
        "magnification": float(fod.m),
        "reference_wavelength_nm": reference_wavelength_nm,
        "object_distance_m": object_distance_m,
        "min_pupil_throughput_fraction": float(throughput.min()),
        "rejected_ray_reasons": rejected_reasons,
        "min_retained_energy_fraction": float(retained.min()),
        "sample_spacing_um": spacing_mm * 1000,
        "sensor_pitch_um": sensor_pitch_um,
        "sampling_validated_for_sensor": sensor_pitch_um is not None,
        "pupil_sampling_convergence_validated": False,
        "spectral_sampling_convergence_validated": False,
        "psf_kind": "geometric",
        "diffraction_included": False,
        "radiometry_status": "unknown",
    }
    return TraceBundle(
        psf_function=psf, field_height_mm=field_height_mm,
        wavelength_nm=wavelengths_nm, sample_spacing_mm=np.array([spacing_mm, spacing_mm]),
        geometry_function=geometry, relative_illumination_function=np.ones_like(geometry),
        success_counts=success_counts, requested_counts=requested_counts,
        chief_points_mm=chief_points,
        notes=["Aperture-checked geometric pupil-ray PSF; diffraction is not included.",
               "RI and absolute transmittance are unknown; unity arrays are compatibility placeholders.",
               "Pupil weights are conditional uniform geometric quadrature, not calibrated radiometric weights.",
               "Pupil and spectral sampling convergence have not been established."],
        pupil_throughput_fraction=throughput, image_direction_cosines=directions,
        pupil_weights=weights, pupil_coordinates=pupil_points,
        chief_direction_cosines=chief_directions,
        chief_ray_angle_deg=np.degrees(np.arctan2(np.linalg.norm(chief_directions[:, :, :2], axis=2), np.abs(chief_directions[:, :, 2]))),
        retained_energy_fraction=retained, quality=quality,
    )


def build_rayoptics_model(
    candidate: Candidate,
    surfaces: list[dict[str, str]],
    field_count: int,
    wavelengths_nm: np.ndarray,
    *,
    reference_wavelength_nm: float = 550.0,
    object_distance_m: float = math.inf,
    efl_relative_tolerance: float = 0.05,
) -> tuple[OpticalModel, np.ndarray]:
    validate_source(candidate, surfaces, object_distance_m)
    if not math.isfinite(reference_wavelength_nm) or reference_wavelength_nm <= 0:
        raise ValueError("Reference wavelength must be positive and finite.")
    max_field_height = candidate.image_height_mm
    if max_field_height is None and candidate.half_field_deg is not None:
        max_field_height = candidate.focal_length_mm * math.tan(
            math.radians(candidate.half_field_deg)
        )
    if max_field_height is None or max_field_height <= 0:
        raise ValueError("Physical field height or half field angle is required; no field proxy is generated.")
    field_height_mm = np.linspace(
        0.0,
        float(max_field_height),
        max(int(field_count), 1),
        dtype=float,
    )
    relative_fields = field_height_mm / max(float(max_field_height), 1e-12)

    model = OpticalModel()
    model.radius_mode = True
    seq_model = model["seq_model"]
    optical_spec = model["optical_spec"]
    seq_model.do_apertures = False
    seq_model.gaps[0].thi = 1.0e10 if math.isinf(object_distance_m) else object_distance_m * 1000.0
    optical_spec["pupil"] = PupilSpec(
        optical_spec,
        key=["image", "f/#"],
        value=float(candidate.f_number),
    )
    optical_spec["fov"] = FieldSpec(
        optical_spec,
        key=["image", "height"],
        value=float(max_field_height),
        flds=relative_fields.tolist(),
        is_relative=True,
    )
    # v2 raster convention is a +x radial field with image y pointing upward.
    for field in optical_spec["fov"].fields:
        field.x, field.y = field.y, 0.0
    model_wavelengths = sorted(set(float(w) for w in wavelengths_nm) | {reference_wavelength_nm})
    optical_spec["wvls"] = WvlSpec(
        [(w, 1.0) for w in model_wavelengths],
        ref_wl=model_wavelengths.index(reference_wavelength_nm),
    )

    stop_surface = None
    for row in surfaces:
        surface_kind = str(row.get("surface_kind", "")).strip().lower()
        if surface_kind == "object" or is_image_context(row):
            continue
        surf_data = surface_data(row)
        semi_diameter = float(strict_number(row.get("effective_aperture"))) / 2.0
        seq_model.add_surface(surf_data, sd=semi_diameter)
        surface_index = int(seq_model.cur_surface)
        apply_asphere(seq_model.ifcs[surface_index], row)
        if surface_kind == "stop":
            stop_surface = surface_index

    if stop_surface is not None:
        seq_model.cur_surface = int(stop_surface)
        seq_model.set_stop()
    model.update_model()
    fod = model["analysis_results"]["parax_data"].fod
    if not math.isfinite(float(fod.efl)) or float(fod.efl) <= 0:
        raise ValueError("Retraced model has no positive finite effective focal length.")
    if abs(float(fod.efl) / candidate.focal_length_mm - 1) > efl_relative_tolerance:
        raise ValueError(f"Retraced EFL {float(fod.efl):.6g} mm disagrees with nominal {candidate.focal_length_mm:.6g} mm.")
    pupil_radius = float(abs(fod.enp_radius))
    if not math.isfinite(pupil_radius) or not 0 < pupil_radius < 10 * candidate.focal_length_mm:
        raise ValueError("Retraced entrance pupil is physically invalid.")
    return model, field_height_mm


def surface_data(row: dict[str, str]) -> list[Any]:
    raw_radius = str(row.get("radius", "")).strip().lower()
    plane = raw_radius in {"inf", "+inf", "-inf", "infinity", "+infinity", "-infinity", "infinite", "∞"}
    radius = 0.0 if plane else strict_number(row.get("radius"))
    thickness = strict_number(row.get("thickness"))
    if radius is None or thickness is None:
        raise ValueError(f"S{row.get('surface_label')}: unresolved radius or thickness.")
    if thickness < 0:
        raise ValueError("Negative gaps/reflected paths require a different sequential adapter.")
    data: list[Any] = [radius, thickness]
    nd = strict_number(row.get("nd"))
    vd = strict_number(row.get("vd"))
    if str(row.get("nd", "")).strip() and nd is None:
        raise ValueError("Unresolved material refractive index.")
    material = str(row.get("material", "")).strip()
    if nd is not None and nd > 1.0:
        if vd is None or vd <= 0:
            raise ValueError("Dispersive material requires a measured nd/Vd pair; no Vd proxy is generated.")
        data.extend([nd, vd])
    elif nd is not None and nd != 1.0:
        raise ValueError("Invalid material refractive index.")
    elif material and material.lower() != "air":
        raise ValueError("Named material requires explicit validated catalog/dispersion data.")
    return data


def apply_asphere(ifc: Any, row: dict[str, str]) -> None:
    coefficients = {}
    for key, raw in row.items():
        if not re.fullmatch(r"a\d+", key.lower()) or str(raw).strip() == "":
            continue
        value = strict_number(raw)
        if value is None:
            raise ValueError(f"Unresolved aspheric coefficient {key}.")
        if value != 0:
            order = int(key[1:])
            if not 2 <= order <= 20:
                raise ValueError(f"Unsupported aspheric polynomial order {order}.")
            coefficients[order] = value
    conic = strict_number(row.get("conic"))
    if str(row.get("conic", "")).strip() and conic is None:
        raise ValueError("Unresolved conic coefficient.")
    if conic is None:
        conic = strict_number(row.get("k"))
    if not coefficients:
        if conic is not None and hasattr(ifc, "profile"):
            ifc.profile = profiles.mutate_profile(ifc.profile, "Conic")
            ifc.profile.cc = conic
        return

    radial = any(order % 2 for order in coefficients)
    ifc.profile = profiles.mutate_profile(ifc.profile, "RadialPolynomial" if radial else "EvenPolynomial")
    if conic is not None:
        ifc.profile.cc = conic
    size = max(coefficients) if radial else max(coefficients) // 2
    ifc.profile.coefs = [0.0] * size
    for order, value in coefficients.items():
        ifc.profile.coefs[order - 1 if radial else order // 2 - 1] = value
    ifc.profile.update()


def trace_image_point(
    model: OpticalModel,
    pupil: Any,
    field: Any,
    wavelength_nm: float,
) -> np.ndarray:
    return trace_image_ray(model, pupil, field, wavelength_nm)[0]


def trace_image_ray(model: OpticalModel, pupil: Any, field: Any, wavelength_nm: float) -> tuple[np.ndarray, np.ndarray]:
    ray, _op_delta, _wavelength = trace_base(
        model, pupil, field, wavelength_nm, check_apertures=True,
        apply_vignetting=False,
    )
    point = np.asarray(ray[-1][0], dtype=float).reshape(3)
    direction = np.asarray(ray[-1][1], dtype=float).reshape(3)
    norm = float(np.linalg.norm(direction))
    if not np.isfinite(point).all() or not np.isfinite(direction).all() or norm <= 0:
        raise ValueError("Ray contains invalid image intersection or direction.")
    return point[:2].copy(), direction / norm


def pupil_disk_samples(samples: int) -> np.ndarray:
    axis = np.linspace(-1.0, 1.0, max(int(samples), 1), dtype=float)
    points = []
    for y in axis:
        for x in axis:
            if (x * x + y * y) <= 1.0 + 1e-12:
                points.append([x, y])
    return np.asarray(points, dtype=float)


def choose_sample_spacing(
    point_sets: list[list[np.ndarray]],
    psf_size: int,
    sample_spacing_um: float | None,
) -> float:
    if sample_spacing_um is not None:
        return float(sample_spacing_um) / 1000.0
    max_extent = 0.0
    for row in point_sets:
        for points in row:
            if points.size:
                max_extent = max(max_extent, float(np.max(np.abs(points))))
    if max_extent <= 0.0:
        return 0.001
    return max_extent / max((psf_size / 2.0) * 0.85, 1.0)


def histogram_psf(
    points: np.ndarray, psf_size: int, sample_spacing_mm: float,
    *, min_retained_energy: float = 0.999, return_retained: bool = False,
) -> Any:
    """Deposit geometric ray mass on pixel centers (+x columns, +y up).

    Bilinear deposition keeps the centroid at the continuous ray coordinate.
    Normalization is conditional on admitted rays; radiometric throughput is
    deliberately separate. Missing or out-of-support rays never become deltas.
    """
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) == 0:
        raise ValueError("Cannot construct a PSF without image intersections.")
    if not np.isfinite(points).all() or not math.isfinite(sample_spacing_mm) or sample_spacing_mm <= 0:
        raise ValueError("PSF coordinates and spacing must be finite.")
    if psf_size < 2:
        raise ValueError("PSF grid must be at least 2 by 2.")
    center = (psf_size - 1) / 2.0
    cols = points[:, 0] / sample_spacing_mm + center
    rows = center - points[:, 1] / sample_spacing_mm
    # Reject enormous coordinates before integer conversion can overflow.
    near = (rows >= -1) & (rows <= psf_size) & (cols >= -1) & (cols <= psf_size)
    r, c = rows[near], cols[near]
    r0, c0 = np.floor(r).astype(int), np.floor(c).astype(int)
    dr, dc = r - r0, c - c0
    kernel = np.zeros((psf_size, psf_size))
    for ry, cy, mass in ((0, 0, (1-dr)*(1-dc)), (1, 0, dr*(1-dc)),
                         (0, 1, (1-dr)*dc), (1, 1, dr*dc)):
        ri, ci = r0 + ry, c0 + cy
        valid = (ri >= 0) & (ri < psf_size) & (ci >= 0) & (ci < psf_size)
        np.add.at(kernel, (ri[valid], ci[valid]), mass[valid])
    total = float(kernel.sum())
    retained = total / len(points)
    if total <= 0 or retained < min_retained_energy:
        raise ValueError(f"PSF support retains only {retained:.6g} of admitted ray energy (required {min_retained_energy}).")
    kernel /= total
    return (kernel, retained) if return_retained else kernel


def build_raytrace_optics_json(
    candidate: Candidate,
    bundle: TraceBundle,
    file_name: str,
) -> dict[str, Any]:
    base = dict(candidate.optics_json)
    base.update(
        {
            "model": "raytrace",
            "compute_method": "opticspsf",
            "offaxis_method": "skip",
            "focal_length_m": float(candidate.focal_length_mm) / 1000.0,
            "nominal_focal_length_m": float(candidate.focal_length_mm) / 1000.0,
            "f_number": float(candidate.f_number),
            "transmittance": {
                "wave": bundle.wavelength_nm.tolist(),
                "scale": [1.0] * int(bundle.wavelength_nm.size),
                "status": "unknown",
                "provenance": "unity compatibility placeholder; absolute spectral transmission is uncalibrated",
            },
            "raytrace": {
                "contract_version": 2,
                "coordinate_convention": "pixel_centers",
                "psf_reference_axis": "+x",
                "row_axis": "y_up",
                "physics_policy": "strict",
                "psf_kind": "geometric",
                "diffraction_included": False,
                "radiometry_status": "unknown",
                "source_prescription_sha256": bundle.quality["source_prescription_sha256"],
                "generator_sha256": bundle.quality["generator_sha256"],
                "code_version": CODE_VERSION,
                "quality": bundle.quality,
                "pupil_ray_data": {
                    "axes": ["field_height", "wavelength", "pupil_sample", "xyz"],
                    "direction_basis": "image Cartesian: +x radial, +y upward, +z propagation",
                    "blocked_direction_encoding": "NaN with zero pupil weight",
                    "weights": "conditional uniform geometric quadrature over admitted rays",
                    "throughput": "admitted pupil sample fraction; not calibrated irradiance or relative illumination",
                },
                "program": "RayOptics",
                "lens_file": candidate.source_surfaces_csv,
                "reference_wavelength_nm": bundle.quality["reference_wavelength_nm"],
                "object_distance_m": bundle.quality["object_distance_m"],
                "magnification": bundle.quality["magnification"],
                "f_number": float(candidate.f_number),
                "effective_focal_length_m": bundle.quality["actual_efl_mm"] / 1000.0,
                "effective_f_number": bundle.quality["effective_f_number"],
                "max_fov_deg": (
                    None
                    if candidate.half_field_deg is None
                    else float(candidate.half_field_deg) * 2.0
                ),
                "geometry": {
                    "field_coordinate": "ideal_image_height",
                    "interpolation": "linear",
                    "function": "__npz__:geometry_function",
                    "field_height_mm": bundle.field_height_mm.tolist(),
                    "wavelength_nm": bundle.wavelength_nm.tolist(),
                },
                "relative_illumination": {
                    "field_coordinate": "ideal_image_height",
                    "interpolation": "linear",
                    "status": "unknown",
                    "provenance": "unity compatibility placeholder; pupil trace success is not radiometric relative illumination",
                    "function": "__npz__:relative_illumination_function",
                    "field_height_mm": bundle.field_height_mm.tolist(),
                    "wavelength_nm": bundle.wavelength_nm.tolist(),
                },
                "psf": {
                    "field_coordinate": "ideal_image_height",
                    "coordinate_convention": "pixel_centers",
                    "psf_reference_axis": "+x",
                    "row_axis": "y_up",
                    "function": "__npz__:psf_function",
                    "field_height_mm": bundle.field_height_mm.tolist(),
                    "wavelength_nm": bundle.wavelength_nm.tolist(),
                    "sample_spacing_mm": bundle.sample_spacing_mm.tolist(),
                },
                "computation": {
                    "psf_spacing_m": float(bundle.sample_spacing_mm[0]) / 1000.0,
                },
                "blocks_per_field_height": 4,
                "name": (
                    f"{candidate.company} {candidate.publication_number} "
                    f"{candidate.configuration}"
                ),
            },
            "lens_patent_raytrace_psf": {
                "file": file_name,
                "simulation_id": candidate.simulation_id,
                "model": CODE_VERSION,
                "contract_version": 2,
                "notes": bundle.notes,
            },
        }
    )
    return base


def write_manifest(path: Path, args: argparse.Namespace, rows: list[dict[str, Any]]) -> None:
    payload = {
        "schema": "camerae2e_lens_patent_raytrace_psf_manifest_v2",
        "contract_version": 2,
        "built_at": datetime.now(UTC).isoformat(),
        "camerae2e_db": str(args.camerae2e_db),
        "export_root": str(args.export_root),
        "settings": effective_build_settings(args),
        "available_presets": PSF_PRESETS,
        "rows": rows,
        "summary": summarize_manifest(rows),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def summarize_manifest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"total": len(rows)}
    by_company: dict[str, dict[str, int]] = {}
    for row in rows:
        key = str(row.get("status", "unknown"))
        summary[key] = int(summary.get(key, 0)) + 1
        company = str(row.get("company", "unknown") or "unknown")
        company_summary = by_company.setdefault(company, {"total": 0})
        company_summary["total"] += 1
        company_summary[key] = int(company_summary.get(key, 0)) + 1
    summary["by_company"] = by_company
    return summary


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_wavelengths(value: str) -> np.ndarray:
    wavelengths = [float(part.strip()) for part in value.split(",") if part.strip()]
    if not wavelengths:
        raise ValueError("At least one wavelength is required.")
    if any(not math.isfinite(w) or w <= 0 for w in wavelengths):
        raise ValueError("Wavelengths must be positive and finite.")
    return np.asarray(sorted(set(wavelengths)), dtype=float)


def strict_number(value: Any) -> float | None:
    """Parse a whole numeric field; never recover digits from unresolved text."""
    text = str(value if value is not None else "").strip()
    try:
        result = float(text.replace("−", "-").replace("D", "E").replace("d", "e"))
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def numeric(value: Any) -> float | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    lowered = text.lower().replace("∞", "infinity")
    if lowered in {"inf", "+inf", "infinite", "infinity", "-inf", "-infinity"}:
        return None
    match = re.search(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?", text)
    if match is None:
        return None
    try:
        parsed = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def positive(value: float | None) -> float | None:
    if value is None or value <= 0.0 or not math.isfinite(value):
        return None
    return value


def none_or_float(value: Any) -> float | None:
    if value is None:
        return None
    parsed = numeric(value)
    return None if parsed is None else float(parsed)


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


if __name__ == "__main__":
    raise SystemExit(main())
