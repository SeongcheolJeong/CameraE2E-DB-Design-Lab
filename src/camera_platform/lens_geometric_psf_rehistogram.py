"""Opt-in, revision-bound geometric PSF re-histogramming for Lens DB RAW.

The pinned v2 PSF stores bilinearly deposited ray histograms, not the ray
intersections. Rebuild the pinned prescription, retrace every stored field and
wavelength, reproduce each source histogram and success mask, then deposit the
same admitted intersections directly on a declared optical-image grid. The
result remains a geometric pre-pixel PSF. The ordinary Lens DB/strict optical
path is unchanged, including its off-axis rotation and source-boundary limits.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import map_coordinates, rotate

from pyisetcam.lens_patents import lens_patent_raytrace_optics
from pyisetcam.lens_validation import validate_lens_prescription

from .lens_catalog_bridge import _asset, _revision_source

SCHEMA = "camera_pinned_geometric_psf_rehistogram_v1"
BUILDER_MODEL = "rayoptics_geometric_psf_contract_v2"
SUPPORTED_BUILDER_GENERATOR_SHA256 = frozenset({
    "8c5cb4ae8b84b06160d06b06f32e1a860259be7ba2f2bba4ae9ffbfe6fb42f16",
})
SOURCE_REPRODUCTION_MAX_ABS_TOLERANCE = 2e-7
SOURCE_REPRODUCTION_L1_TOLERANCE = 1e-4
SOURCE_METRIC_CENTROID_RMS_TOLERANCE_UM = 1e-3
SOURCE_METRIC_MTF_TOLERANCE = 1e-4
MTF_CYCLES_PER_MM = (100.0, 250.0, 500.0)
DEFAULT_MAX_PSF_BYTES = 128 * 1024**2
MAX_TARGET_SIDE = 2049


@dataclass(frozen=True)
class GeometricRehistogramResult:
    optics: dict[str, Any]
    provenance: dict[str, Any]


@dataclass(frozen=True)
class _Candidate:
    simulation_id: str
    configuration: str
    f_number: float
    focal_length_mm: float
    image_height_mm: float | None
    half_field_deg: float | None
    source_surfaces_csv: str


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _array_sha(array: np.ndarray, dtype: str) -> str:
    values = np.ascontiguousarray(array, dtype=dtype)
    digest = hashlib.sha256()
    digest.update(str(values.shape).encode("ascii"))
    digest.update(values.dtype.str.encode("ascii"))
    digest.update(values.tobytes())
    return digest.hexdigest()


def _json_sha(value: dict[str, Any]) -> str:
    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: normalize(entry) for key, entry in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(entry) for entry in item]
        if isinstance(item, float) and math.isinf(item):
            return "positive_infinity" if item > 0 else "negative_infinity"
        if isinstance(item, float) and math.isnan(item):
            raise ValueError("Cannot hash NaN in pinned build settings.")
        return item

    encoded = json.dumps(
        normalize(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mass_metrics(histogram: np.ndarray, pitch_mm: float) -> dict[str, Any]:
    """Discrete spot/OTF diagnostics; these do not imply physical MTF."""
    values = np.asarray(histogram, dtype=float)
    if values.ndim != 2 or not np.isfinite(values).all() or np.any(values < 0):
        raise ValueError("Source PSF metrics require a finite nonnegative plane.")
    total = float(values.sum())
    if total <= 0:
        raise ValueError("Source PSF metrics require positive mass.")
    values = values / total
    y = ((values.shape[0] - 1) / 2 - np.arange(values.shape[0])) * pitch_mm
    x = (np.arange(values.shape[1]) - (values.shape[1] - 1) / 2) * pitch_mm
    yy, xx = np.meshgrid(y, x, indexing="ij")
    cx = float(np.sum(values * xx))
    cy = float(np.sum(values * yy))
    rms_x = math.sqrt(float(np.sum(values * (xx - cx) ** 2)))
    rms_y = math.sqrt(float(np.sum(values * (yy - cy) ** 2)))
    radius = np.hypot(xx - cx, yy - cy).ravel()
    order = np.argsort(radius)
    cum_mass = np.cumsum(values.ravel()[order])
    ee80 = float(radius[order[min(np.searchsorted(cum_mass, 0.8), len(order) - 1)]])
    profile_x = values.sum(axis=0)
    profile_y = values.sum(axis=1)
    return {
        "centroid_um_xy": [cx * 1000, cy * 1000],
        "rms_width_um_xy": [rms_x * 1000, rms_y * 1000],
        "ee80_radius_um": ee80 * 1000,
        "sampled_grid_nyquist_cycles_per_mm": 1.0 / (2.0 * pitch_mm),
        "mtf_frequencies_cycles_per_mm": list(MTF_CYCLES_PER_MM),
        "mtf_frequency_status": [
            "within_grid_nyquist" if f <= 1.0 / (2.0 * pitch_mm)
            else "aliased_discrete_response"
            for f in MTF_CYCLES_PER_MM
        ],
        "mtf_x": [
            float(abs(np.sum(profile_x * np.exp(-2j * math.pi * f * x))))
            for f in MTF_CYCLES_PER_MM
        ],
        "mtf_y": [
            float(abs(np.sum(profile_y * np.exp(-2j * math.pi * f * y))))
            for f in MTF_CYCLES_PER_MM
        ],
    }


def _metric_deltas(source: dict[str, Any], rebuilt: dict[str, Any]) -> dict[str, Any]:
    supported = [
        index for index, frequency in enumerate(MTF_CYCLES_PER_MM)
        if frequency <= min(
            source["sampled_grid_nyquist_cycles_per_mm"],
            rebuilt["sampled_grid_nyquist_cycles_per_mm"],
        )
    ]
    return {
        "centroid_um_max_abs": max(abs(a-b) for a, b in zip(
            source["centroid_um_xy"], rebuilt["centroid_um_xy"], strict=True
        )),
        "rms_width_um_max_abs": max(abs(a-b) for a, b in zip(
            source["rms_width_um_xy"], rebuilt["rms_width_um_xy"], strict=True
        )),
        "ee80_radius_um_abs": abs(source["ee80_radius_um"] - rebuilt["ee80_radius_um"]),
        "mtf_max_abs": max(
            abs(source[key][index] - rebuilt[key][index])
            for key in ("mtf_x", "mtf_y") for index in supported
        ) if supported else None,
        "mtf_frequencies_compared_cycles_per_mm": [
            MTF_CYCLES_PER_MM[index] for index in supported
        ],
    }


def _strict_style_interpolation(
    source: np.ndarray, source_pitch_mm: float, target_pitch_mm: float,
    target_side: int, *, azimuth_deg: float = 0.0,
) -> tuple[np.ndarray, float]:
    """Mirror strict rotation/resampling for a diagnostic comparison only."""
    source_side = source.shape[0]
    source_x0 = -(source_side - 1) * source_pitch_mm / 2
    source_y0 = (source_side - 1) * source_pitch_mm / 2
    target_axis = (np.arange(target_side) - (target_side - 1) / 2) * target_pitch_mm
    cols = (target_axis - source_x0) / source_pitch_mm
    rows = (target_axis[::-1] - source_y0) / -source_pitch_mm
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    rotated = (
        source if azimuth_deg == 0 else rotate(
            source, azimuth_deg, reshape=False, order=1,
            mode="constant", cval=0.0, prefilter=False,
        )
    )
    sampled = map_coordinates(
        rotated, [rr, cc], order=1, mode="constant", cval=0.0,
        prefilter=False,
    )
    mass = float(sampled.sum())
    if not math.isfinite(mass) or mass <= 0:
        raise ValueError("Strict-style PSF interpolation missed all ray mass.")
    return sampled / mass, float(rotated.sum() / source.sum())


def _rotated_points(points_mm: np.ndarray, azimuth_deg: float) -> np.ndarray:
    angle = math.radians(azimuth_deg)
    cosine, sine = math.cos(angle), math.sin(angle)
    return np.asarray(points_mm, dtype=float) @ np.array([
        [cosine, -sine], [sine, cosine],
    ]).T


def _number(value: Any) -> float | None:
    try:
        result = float(str(value if value is not None else "").strip()
                           .replace("−", "-").replace("D", "E").replace("d", "e"))
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def _positive(value: float | None) -> bool:
    return value is not None and value > 0


def _is_image_context(row: dict[str, str]) -> bool:
    if row.get("surface_kind", "").strip().lower() != "image":
        return False
    caption = str(row.get("surface_label", "")).strip().lower() in {
        "image", "image plane", "ima", "im",
    }
    terminal_gap = str(row.get("thickness", "")).strip().lower() in {"", "plane"}
    terminal_gap = terminal_gap or (caption and _number(row.get("thickness")) == 0)
    return (
        terminal_gap and _number(row.get("radius")) in {None, 0.0}
        and _number(row.get("nd")) in {None, 1.0}
    )


def _surface_data(row: dict[str, str]) -> list[Any]:
    raw_radius = str(row.get("radius", "")).strip().lower()
    plane = raw_radius in {
        "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity", "infinite", "∞",
    }
    radius = 0.0 if plane else _number(row.get("radius"))
    thickness = _number(row.get("thickness"))
    if radius is None or thickness is None:
        raise ValueError(f"S{row.get('surface_label')}: unresolved radius or thickness.")
    if thickness < 0:
        raise ValueError("Negative gaps/reflected paths need another sequential adapter.")
    data: list[Any] = [radius, thickness]
    nd = _number(row.get("nd"))
    vd = _number(row.get("vd"))
    if str(row.get("nd", "")).strip() and nd is None:
        raise ValueError("Unresolved material refractive index.")
    material = str(row.get("material", "")).strip()
    if nd is not None and nd > 1.0:
        if vd is None or vd <= 0:
            raise ValueError("Dispersive material needs an explicit nd/Vd pair.")
        data.extend([nd, vd])
    elif nd is not None and nd != 1.0:
        raise ValueError("Invalid material refractive index.")
    elif material and material.lower() != "air":
        raise ValueError("Named material lacks explicit validated dispersion data.")
    return data


def _apply_asphere(ifc: Any, row: dict[str, str], profiles: Any) -> None:
    coefficients = {}
    for key, raw in row.items():
        if not re.fullmatch(r"a\d+", key.lower()) or str(raw).strip() == "":
            continue
        value = _number(raw)
        if value is None:
            raise ValueError(f"Unresolved aspheric coefficient {key}.")
        if value != 0:
            order = int(key[1:])
            if not 2 <= order <= 20:
                raise ValueError(f"Unsupported aspheric polynomial order {order}.")
            coefficients[order] = value
    conic = _number(row.get("conic"))
    if str(row.get("conic", "")).strip() and conic is None:
        raise ValueError("Unresolved conic coefficient.")
    if conic is None:
        conic = _number(row.get("k"))
    if not coefficients:
        if conic is not None and hasattr(ifc, "profile"):
            ifc.profile = profiles.mutate_profile(ifc.profile, "Conic")
            ifc.profile.cc = conic
        return
    radial = any(order % 2 for order in coefficients)
    ifc.profile = profiles.mutate_profile(
        ifc.profile, "RadialPolynomial" if radial else "EvenPolynomial"
    )
    if conic is not None:
        ifc.profile.cc = conic
    size = max(coefficients) if radial else max(coefficients) // 2
    ifc.profile.coefs = [0.0] * size
    for order, value in coefficients.items():
        ifc.profile.coefs[order - 1 if radial else order // 2 - 1] = value
    ifc.profile.update()


def _candidate(db_path: Path, simulation_id: str) -> _Candidate:
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM simulation_results WHERE simulation_id=? "
            "AND simulation_status='camerae2e_ready'", (simulation_id,),
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("Pinned simulation is not uniquely camera-ready.")
    row = rows[0]
    if row["f_number"] is None or row["focal_length_mm"] is None:
        raise ValueError("Pinned simulation lacks optical first-order properties.")
    return _Candidate(
        simulation_id=simulation_id,
        configuration=str(row["configuration"]),
        f_number=float(row["f_number"]),
        focal_length_mm=float(row["focal_length_mm"]),
        image_height_mm=(None if row["image_height_mm"] is None
                         else float(row["image_height_mm"])),
        half_field_deg=(None if row["half_field_deg"] is None
                        else float(row["half_field_deg"])),
        source_surfaces_csv=str(row["source_surfaces_csv"]),
    )


def _reviewed_surfaces(
    candidate: _Candidate, db_path: Path, expected_file_sha256: str,
    object_distance_m: float,
) -> tuple[list[dict[str, str]], Path, str]:
    csv_root = db_path.parent / "reviewed_prescriptions"
    csv_path = csv_root / candidate.source_surfaces_csv
    if (
        csv_root.is_symlink() or csv_path.is_symlink()
        or not csv_path.resolve().is_relative_to(csv_root.resolve())
        or not csv_path.is_file()
    ):
        raise ValueError("Pinned reviewed prescription path is unavailable or unsafe.")
    csv_sha = _sha(csv_path)
    if csv_sha != expected_file_sha256:
        raise ValueError("Reviewed prescription bytes differ from pinned PSF build.")
    with csv_path.open(newline="", encoding="utf-8") as handle:
        surfaces = list(csv.DictReader(handle))
    validation = validate_lens_prescription(
        surfaces, focal_length_mm=candidate.focal_length_mm,
        f_number=candidate.f_number, configuration=candidate.configuration,
    )
    errors = [item for item in validation["issues"] if item["severity"] == "error"]
    if errors:
        raise ValueError("Reviewed prescription fails the pinned structural validation.")
    if object_distance_m <= 0 or math.isnan(object_distance_m):
        raise ValueError("Object conjugate must be positive or infinity.")
    if (
        re.search(
            r"close|near|macro|(?<![a-z])\d+(?:\.\d+)?\s*(?:m|cm)(?![a-z])",
            candidate.configuration, re.I,
        ) and not math.isfinite(object_distance_m)
    ):
        raise ValueError("Finite-conjugate configuration needs a finite object distance.")
    stops = [r for r in surfaces if r.get("surface_kind", "").strip().lower() == "stop"]
    if len(stops) != 1:
        raise ValueError("Exactly one explicit aperture stop is required.")
    for row in surfaces:
        if row.get("surface_kind", "").strip().lower() == "object" or _is_image_context(row):
            continue
        if not _positive(_number(row.get("effective_aperture"))):
            raise ValueError("Physical clear aperture is unknown.")
        _surface_data(row)
    return surfaces, csv_path, validation["prescription_sha256"]


def _model(
    candidate: _Candidate, surfaces: list[dict[str, str]], settings: dict[str, Any],
) -> tuple[Any, np.ndarray]:
    try:
        from rayoptics.elem import profiles
        from rayoptics.environment import FieldSpec, OpticalModel, PupilSpec, WvlSpec
    except ImportError as exc:
        raise ValueError("Opt-in re-histogram requires the factory-rayoptics extra.") from exc

    object_distance_m = float(settings["object_distance_m"])
    reference = float(settings["reference_wavelength_nm"])
    waves = np.asarray(settings["wavelengths"], dtype=float)
    if not math.isfinite(reference) or reference <= 0:
        raise ValueError("Pinned reference wavelength is invalid.")
    max_height = candidate.image_height_mm
    if max_height is None and candidate.half_field_deg is not None:
        max_height = candidate.focal_length_mm * math.tan(math.radians(candidate.half_field_deg))
    if max_height is None or max_height <= 0:
        raise ValueError("Pinned simulation lacks a physical field extent.")
    heights = np.linspace(0.0, max_height, int(settings["field_count"]), dtype=float)
    model = OpticalModel()
    model.radius_mode = True
    sequence = model["seq_model"]
    spec = model["optical_spec"]
    sequence.do_apertures = False
    sequence.gaps[0].thi = 1e10 if math.isinf(object_distance_m) else object_distance_m * 1000
    spec["pupil"] = PupilSpec(spec, key=["image", "f/#"], value=candidate.f_number)
    spec["fov"] = FieldSpec(
        spec, key=["image", "height"], value=max_height,
        flds=(heights / max(max_height, 1e-12)).tolist(), is_relative=True,
    )
    for field in spec["fov"].fields:
        field.x, field.y = field.y, 0.0
    model_waves = sorted(set(float(w) for w in waves) | {reference})
    spec["wvls"] = WvlSpec(
        [(w, 1.0) for w in model_waves], ref_wl=model_waves.index(reference)
    )
    stop_surface = None
    for row in surfaces:
        kind = str(row.get("surface_kind", "")).strip().lower()
        if kind == "object" or _is_image_context(row):
            continue
        data = _surface_data(row)
        semi_diameter = float(_number(row.get("effective_aperture"))) / 2
        sequence.add_surface(data, sd=semi_diameter)
        index = int(sequence.cur_surface)
        _apply_asphere(sequence.ifcs[index], row, profiles)
        if kind == "stop":
            stop_surface = index
    if stop_surface is not None:
        sequence.cur_surface = stop_surface
        sequence.set_stop()
    model.update_model()
    fod = model["analysis_results"]["parax_data"].fod
    tolerance = float(settings["efl_relative_tolerance"])
    if (
        not math.isfinite(float(fod.efl)) or float(fod.efl) <= 0
        or abs(float(fod.efl) / candidate.focal_length_mm - 1) > tolerance
    ):
        raise ValueError("Rebuilt effective focal length differs from pinned configuration.")
    pupil_radius = float(abs(fod.enp_radius))
    if not math.isfinite(pupil_radius) or not 0 < pupil_radius < 10 * candidate.focal_length_mm:
        raise ValueError("Rebuilt entrance pupil is physically invalid.")
    return model, heights


def _pupil_points(samples: int) -> np.ndarray:
    axis = np.linspace(-1.0, 1.0, samples, dtype=float)
    return np.asarray(
        [[x, y] for y in axis for x in axis if x*x + y*y <= 1.0 + 1e-12],
        dtype=float,
    )


def _trace(model: Any, pupil: Any, field: Any, wavelength_nm: float) -> np.ndarray:
    from rayoptics.raytr.trace import trace_base

    ray, _op_delta, _wavelength = trace_base(
        model, pupil, field, wavelength_nm,
        check_apertures=True, apply_vignetting=False,
    )
    point = np.asarray(ray[-1][0], dtype=float).reshape(3)
    direction = np.asarray(ray[-1][1], dtype=float).reshape(3)
    norm = float(np.linalg.norm(direction))
    if not np.isfinite(point).all() or not np.isfinite(direction).all() or norm <= 0:
        raise ValueError("Ray has invalid image intersection or direction.")
    return point[:2].copy()


def _histogram(
    points_mm: np.ndarray, side: int, pitch_mm: float, *, min_retained: float,
) -> tuple[np.ndarray, float]:
    """Match v2 builder's bilinear geometric-ray deposition exactly."""
    points = np.asarray(points_mm, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) == 0:
        raise ValueError("No finite admitted intersections for a PSF node.")
    if not np.isfinite(points).all() or side < 2 or not math.isfinite(pitch_mm) or pitch_mm <= 0:
        raise ValueError("Invalid geometric PSF points or grid.")
    center = (side - 1) / 2
    cols = points[:, 0] / pitch_mm + center
    rows = center - points[:, 1] / pitch_mm
    near = (rows >= -1) & (rows <= side) & (cols >= -1) & (cols <= side)
    r, c = rows[near], cols[near]
    r0, c0 = np.floor(r).astype(int), np.floor(c).astype(int)
    dr, dc = r - r0, c - c0
    kernel = np.zeros((side, side), dtype=float)
    for ry, cx, mass in (
        (0, 0, (1-dr)*(1-dc)), (1, 0, dr*(1-dc)),
        (0, 1, (1-dr)*dc), (1, 1, dr*dc),
    ):
        ri, ci = r0 + ry, c0 + cx
        valid = (ri >= 0) & (ri < side) & (ci >= 0) & (ci < side)
        np.add.at(kernel, (ri[valid], ci[valid]), mass[valid])
    total = float(kernel.sum())
    retained = total / len(points)
    if total <= 0 or retained < min_retained:
        raise ValueError(
            f"PSF grid retains {retained:.6g} ray mass; required {min_retained:.6g}."
        )
    return kernel / total, retained


def _target_side(source_side: int, source_pitch_mm: float, target_pitch_mm: float) -> int:
    if source_side < 8 or source_side > MAX_TARGET_SIDE:
        raise ValueError("Pinned PSF has an unsupported source side length.")
    half_span = (source_side - 1) * source_pitch_mm / 2
    side = 2 * math.ceil(half_span / target_pitch_mm) + 1
    if side > MAX_TARGET_SIDE:
        raise ValueError("Target PSF exceeds the 2049-sample optical diagnostic limit.")
    return side


def rehistogram_pinned_geometric_psf(
    revision_id: str, *, catalog_root: str | Path, convolution_pitch_um: float,
    max_total_psf_bytes: int = DEFAULT_MAX_PSF_BYTES,
    max_pupil_rays_per_node: int = 5000,
    soft_timeout_s: float = 120.0,
) -> GeometricRehistogramResult:
    """Build every exact stored field/band PSF from the same pinned ray set.

    The returned optics are an opt-in derived *geometric pre-pixel* response
    for an OI whose executed pitch exactly matches ``convolution_pitch_um``.
    The caller must compare that executed pitch and retain this provenance.
    This only caps output PSF bytes/rays and checks a soft trace-loop deadline;
    it has no independent hard process RSS or wall-time guard.
    """
    started = time.monotonic()
    if (
        not isinstance(revision_id, str) or not revision_id
        or not math.isfinite(convolution_pitch_um) or convolution_pitch_um <= 0
        or type(max_total_psf_bytes) is not int or not 1 <= max_total_psf_bytes <= 512*1024**2
        or type(max_pupil_rays_per_node) is not int
        or not 1 <= max_pupil_rays_per_node <= 5000
        or not math.isfinite(soft_timeout_s) or not 0 < soft_timeout_s <= 300
    ):
        raise ValueError("Declare a pinned lens, positive pitch and bounded PSF/ray/time limits.")
    root = Path(catalog_root).expanduser().resolve()
    revision, snapshot, db_path, db_sha = _revision_source(revision_id, root)
    simulation_id, asset_path, validation, _artifact = _asset(
        revision, snapshot, db_path, root
    )
    optics = lens_patent_raytrace_optics(
        simulation_id, db_path=db_path, psf_dir=asset_path.parent,
        allow_approximate_radiometry=True,
    )
    ray = optics["raytrace"]
    if (
        ray.get("asset_sha256") != validation["asset_sha256"]
        or ray.get("source_prescription_sha256") != validation["source_prescription_sha256"]
        or ray.get("psf_kind") != "geometric"
        or ray.get("diffraction_included") is not False
        or ray.get("radiometry_status") == "validated"
    ):
        raise ValueError("Loaded optics differ from pinned geometric Lens DB authority.")
    with np.load(asset_path, allow_pickle=False) as asset:
        settings = json.loads(str(asset["build_settings_json"]))
        quality = json.loads(str(asset["quality_json"]))
        fields = np.asarray(asset["field_height_mm"], dtype=float).copy()
        waves = np.asarray(asset["wavelength_nm"], dtype=float).copy()
        pupil_saved = np.asarray(asset["pupil_coordinates"], dtype=float).copy()
        weights_saved = np.asarray(asset["pupil_weights"], dtype=float).copy()
        successes_saved = np.asarray(asset["success_counts"], dtype=int).copy()
        chiefs_saved = np.asarray(asset["chief_points_mm"], dtype=float).copy()
        source_pitch = np.asarray(asset["sample_spacing_mm"], dtype=float).copy()
        source_psf = np.asarray(asset["psf_function"], dtype=np.float32).copy()
    n_field, n_wave = len(fields), len(waves)
    if (
        settings.get("model") != BUILDER_MODEL
        or settings.get("generator_sha256") not in SUPPORTED_BUILDER_GENERATOR_SHA256
        or quality.get("generator_sha256") != settings.get("generator_sha256")
        or quality.get("source_prescription_sha256") != validation["source_prescription_sha256"]
        or source_psf.ndim != 4 or source_psf.shape[0] != source_psf.shape[1]
        or source_psf.shape[2:] != (n_field, n_wave)
        or not 2 <= n_field <= 7 or not 1 <= n_wave <= 5
        or not np.array_equal(waves, np.asarray(settings.get("wavelengths"), dtype=float))
        or not np.isfinite(waves).all() or np.any(waves <= 0)
        or len(source_pitch) != 2 or not np.isfinite(source_pitch).all()
        or np.any(source_pitch <= 0)
        or not np.isclose(source_pitch[0], source_pitch[1], rtol=0, atol=1e-15)
        or not np.allclose(fields, np.asarray(ray["psf"]["field_height_mm"]), rtol=0, atol=1e-9)
        or not np.array_equal(waves, np.asarray(ray["psf"]["wavelength_nm"]))
        or weights_saved.shape != (n_field, n_wave, len(pupil_saved))
        or successes_saved.shape != (n_field, n_wave)
        or chiefs_saved.shape != (n_field, n_wave, 2)
        or pupil_saved.shape != (len(pupil_saved), 2)
        or not np.isfinite(pupil_saved).all()
        or len(pupil_saved) > max_pupil_rays_per_node
    ):
        raise ValueError("Pinned PSF axes, pupil, source or build contract differs.")
    if not np.array_equal(source_psf, np.asarray(ray["psf"]["function"], dtype=np.float32)):
        raise ValueError("Loaded optics PSF array differs from pinned asset bytes.")
    original_computation_pitch_m = ray.get("computation", {}).get("psf_spacing_m")
    if (
        original_computation_pitch_m is None
        or not math.isclose(
            float(original_computation_pitch_m), float(source_pitch[0]) * 1e-3,
            rel_tol=0, abs_tol=1e-15,
        )
    ):
        raise ValueError("Loaded optics computation spacing differs from pinned PSF grid.")
    target_pitch_mm = convolution_pitch_um * 1e-3
    side = _target_side(source_psf.shape[0], float(source_pitch[0]), target_pitch_mm)
    output_bytes = side * side * n_field * n_wave * np.dtype("<f4").itemsize
    if output_bytes > max_total_psf_bytes:
        raise ValueError("Derived PSF array exceeds the declared output-byte cap.")
    candidate = _candidate(db_path, simulation_id)
    surfaces, csv_path, prescription_sha = _reviewed_surfaces(
        candidate, db_path, quality["source_file_sha256"],
        float(settings["object_distance_m"]),
    )
    if prescription_sha != validation["source_prescription_sha256"]:
        raise ValueError("Reviewed prescription content differs from pinned revision.")
    model, traced_fields = _model(candidate, surfaces, settings)
    if not np.allclose(fields, traced_fields, rtol=0, atol=1e-9):
        raise ValueError("Rebuilt field coordinates differ from pinned PSF asset.")
    pupil = _pupil_points(int(settings["pupil_samples"]))
    if not np.array_equal(pupil, pupil_saved):
        raise ValueError("Rebuilt pupil locations differ from pinned PSF asset.")
    derived = np.empty((side, side, n_field, n_wave), dtype=np.float32)
    node_reports: list[dict[str, Any]] = []
    worst_source_error = 0.0
    worst_source_l1 = 0.0
    azimuth_45_diagnostic = None
    minimum_retained = float(settings["min_retained_energy"])
    for field_index, field in enumerate(model["optical_spec"]["fov"].fields):
        for wave_index, wavelength in enumerate(waves):
            if time.monotonic() - started > soft_timeout_s:
                raise TimeoutError("Opt-in PSF re-histogram exceeded its soft trace deadline.")
            chief = _trace(model, [0.0, 0.0], field, float(wavelength))
            if not np.allclose(chief, chiefs_saved[field_index, wave_index], rtol=0, atol=1e-9):
                raise ValueError("Rebuilt chief ray differs from pinned PSF asset.")
            points = []
            mask = np.zeros(len(pupil), dtype=bool)
            for pupil_index, location in enumerate(pupil):
                if time.monotonic() - started > soft_timeout_s:
                    raise TimeoutError("Opt-in PSF re-histogram exceeded its soft trace deadline.")
                try:
                    point = _trace(model, location, field, float(wavelength))
                except Exception as exc:
                    if not type(exc).__name__.startswith("Trace"):
                        raise
                    continue
                mask[pupil_index] = True
                points.append(point - chief)
            if (
                len(points) != int(successes_saved[field_index, wave_index])
                or not np.array_equal(mask, weights_saved[field_index, wave_index] > 0)
            ):
                raise ValueError("Retraced successful-ray mask differs from pinned PSF asset.")
            points_array = np.asarray(points, dtype=float)
            source_rebuilt, source_retained = _histogram(
                points_array, source_psf.shape[0], float(source_pitch[0]),
                min_retained=minimum_retained,
            )
            target_rebuilt, target_retained = _histogram(
                points_array, side, target_pitch_mm, min_retained=minimum_retained,
            )
            source_delta = np.abs(source_rebuilt - source_psf[:, :, field_index, wave_index])
            source_error = float(source_delta.max())
            source_l1 = float(source_delta.sum())
            source_metrics = _mass_metrics(
                source_psf[:, :, field_index, wave_index], float(source_pitch[0])
            )
            rebuilt_metrics = _mass_metrics(source_rebuilt, float(source_pitch[0]))
            metric_deltas = _metric_deltas(source_metrics, rebuilt_metrics)
            if (
                source_error > SOURCE_REPRODUCTION_MAX_ABS_TOLERANCE
                or source_l1 > SOURCE_REPRODUCTION_L1_TOLERANCE
                or metric_deltas["centroid_um_max_abs"]
                    > SOURCE_METRIC_CENTROID_RMS_TOLERANCE_UM
                or metric_deltas["rms_width_um_max_abs"]
                    > SOURCE_METRIC_CENTROID_RMS_TOLERANCE_UM
                or metric_deltas["ee80_radius_um_abs"]
                    > float(source_pitch[0] * 1000)
                or metric_deltas["mtf_max_abs"] is None
                or metric_deltas["mtf_max_abs"] > SOURCE_METRIC_MTF_TOLERANCE
            ):
                raise ValueError("Retraced source-grid PSF does not reproduce pinned histogram.")
            worst_source_error = max(worst_source_error, source_error)
            worst_source_l1 = max(worst_source_l1, source_l1)
            derived[:, :, field_index, wave_index] = target_rebuilt.astype(np.float32)
            current_interpolated, _ = _strict_style_interpolation(
                source_psf[:, :, field_index, wave_index], float(source_pitch[0]),
                target_pitch_mm, side,
            )
            direct_target_metrics = _mass_metrics(target_rebuilt, target_pitch_mm)
            current_target_metrics = _mass_metrics(current_interpolated, target_pitch_mm)
            if (
                field_index == n_field - 1
                and wave_index == int(np.argmin(np.abs(waves - 550.0)))
            ):
                direct_rotated, direct_rotated_retained = _histogram(
                    _rotated_points(points_array, 45.0), side, target_pitch_mm,
                    min_retained=1e-12,
                )
                v5_rotated, v5_rotation_mass_ratio = _strict_style_interpolation(
                    source_psf[:, :, field_index, wave_index], float(source_pitch[0]),
                    target_pitch_mm, side, azimuth_deg=45.0,
                )
                v8_rotated, v8_rotation_mass_ratio = _strict_style_interpolation(
                    derived[:, :, field_index, wave_index], target_pitch_mm,
                    target_pitch_mm, side, azimuth_deg=45.0,
                )
                direct_rotated_metrics = _mass_metrics(direct_rotated, target_pitch_mm)
                azimuth_45_diagnostic = {
                    "field_index": field_index,
                    "wavelength_nm": float(wavelength),
                    "azimuth_deg": 45.0,
                    "direct_target_retained_ray_fraction": float(direct_rotated_retained),
                    "v5_stored_interpolation_pre_unit_sum_mass_ratio": (
                        v5_rotation_mass_ratio
                    ),
                    "v8_derived_rotation_pre_unit_sum_mass_ratio": (
                        v8_rotation_mass_ratio
                    ),
                    "direct_vs_v5_stored_interpolation_l1_mass_difference": float(np.sum(
                        np.abs(direct_rotated - v5_rotated)
                    )),
                    "direct_vs_v8_derived_rotation_l1_mass_difference": float(np.sum(
                        np.abs(direct_rotated - v8_rotated)
                    )),
                    "direct_vs_v5_stored_interpolation_metric_deltas_abs": _metric_deltas(
                        direct_rotated_metrics,
                        _mass_metrics(v5_rotated, target_pitch_mm),
                    ),
                    "direct_vs_v8_derived_rotation_metric_deltas_abs": _metric_deltas(
                        direct_rotated_metrics,
                        _mass_metrics(v8_rotated, target_pitch_mm),
                    ),
                    "qualified": False,
                }
            packed = np.packbits(mask, bitorder="little")
            node_reports.append({
                "field_index": field_index,
                "field_height_mm": float(fields[field_index]),
                "wavelength_nm": float(wavelength),
                "admitted_ray_count": len(points),
                "requested_pupil_ray_count": len(pupil),
                "success_mask_packbits_little_hex": packed.tobytes().hex(),
                "success_mask_sha256": _array_sha(packed, "|u1"),
                "admitted_intersections_xy_mm_sha256": _array_sha(points_array, "<f8"),
                "source_grid_max_abs_cell_error": source_error,
                "source_grid_l1_mass_error": source_l1,
                "source_grid_metric_deltas": metric_deltas,
                "direct_target_metrics": direct_target_metrics,
                "strict_source_interpolation_target_metrics": current_target_metrics,
                "direct_vs_strict_target_l1_mass_difference": float(np.sum(
                    np.abs(target_rebuilt - current_interpolated)
                )),
                "direct_vs_strict_target_metric_deltas_abs": _metric_deltas(
                    direct_target_metrics, current_target_metrics
                ),
                "source_grid_retained_ray_fraction": float(source_retained),
                "target_grid_retained_ray_fraction": float(target_retained),
            })
    original_array_sha = _array_sha(source_psf, "<f4")
    derived_array_sha = _array_sha(derived, "<f4")
    derived_signature = _json_sha({
        "schema": SCHEMA,
        "source_psf_asset_sha256": validation["asset_sha256"],
        "source_psf_array_sha256": original_array_sha,
        "source_prescription_sha256": prescription_sha,
        "lens_revision_content_sha256": revision["content_sha256"],
        "builder_generator_sha256": settings.get("generator_sha256"),
        "builder_settings_sha256": _json_sha(settings),
        "source_pitch_mm_row_col": source_pitch.tolist(),
        "target_pitch_mm_row_col": [target_pitch_mm, target_pitch_mm],
        "field_height_mm": fields.tolist(),
        "wavelength_nm": waves.tolist(),
        "derived_psf_array_sha256": derived_array_sha,
        "node_success_mask_hashes": [node["success_mask_sha256"] for node in node_reports],
        "node_admitted_intersection_hashes": [
            node["admitted_intersections_xy_mm_sha256"] for node in node_reports
        ],
    })
    try:
        import rayoptics
        runtime_rayoptics = rayoptics.__version__
        runtime_opticalglass = version("opticalglass")
    except ImportError:
        runtime_rayoptics, runtime_opticalglass = None, None
    provenance = {
        "schema": SCHEMA,
        "status": "opt_in_geometric_target_pitch_reference_available",
        "lens_revision_id": revision_id,
        "lens_revision_content_sha256": revision["content_sha256"],
        "lens_db_sha256": db_sha,
        "simulation_id": simulation_id,
        "source_prescription_sha256": prescription_sha,
        "reviewed_prescription_file_sha256": _sha(csv_path),
        "source_psf_asset_sha256": validation["asset_sha256"],
        "source_psf_array_sha256": original_array_sha,
        "derived_psf_array_sha256": derived_array_sha,
        "derived_psf_signature_sha256": derived_signature,
        "builder_generator_sha256": settings.get("generator_sha256"),
        "builder_model": BUILDER_MODEL,
        "builder_runtime_versions": {
            "rayoptics": settings.get("rayoptics_version"),
            "opticalglass": settings.get("opticalglass_version"),
            "numpy": settings.get("numpy_version"),
        },
        "current_runtime_versions": {
            "rayoptics": runtime_rayoptics,
            "opticalglass": runtime_opticalglass,
            "numpy": np.__version__,
        },
        "source_psf_pitch_um_row_col": [float(v * 1000) for v in source_pitch],
        "source_computation_psf_spacing_m": float(original_computation_pitch_m),
        "declared_optical_convolution_pitch_um": convolution_pitch_um,
        "derived_psf_pitch_um_row_col": [convolution_pitch_um] * 2,
        "derived_computation_psf_spacing_m": convolution_pitch_um * 1e-6,
        "source_psf_shape_row_col": list(source_psf.shape[:2]),
        "derived_psf_shape_row_col": [side, side],
        "field_wavelength_node_count": len(node_reports),
        "node_reports": node_reports,
        "worst_source_grid_max_abs_cell_error": worst_source_error,
        "worst_source_grid_l1_mass_error": worst_source_l1,
        "selected_off_axis_45_degree_rotation_diagnostic": azimuth_45_diagnostic,
        "source_grid_reproduction_tolerance_max_abs": SOURCE_REPRODUCTION_MAX_ABS_TOLERANCE,
        "source_grid_reproduction_tolerance_l1": SOURCE_REPRODUCTION_L1_TOLERANCE,
        "source_grid_metric_centroid_rms_tolerance_um": (
            SOURCE_METRIC_CENTROID_RMS_TOLERANCE_UM
        ),
        "source_grid_metric_mtf_tolerance": SOURCE_METRIC_MTF_TOLERANCE,
        "output_psf_float32_bytes": output_bytes,
        "limits": {
            "max_total_psf_bytes": max_total_psf_bytes,
            "max_pupil_rays_per_node": max_pupil_rays_per_node,
            "max_target_side": MAX_TARGET_SIDE,
            "soft_timeout_s": soft_timeout_s,
            "hard_process_rss_or_time_guard": False,
        },
        "elapsed_seconds": time.monotonic() - started,
        "same_admitted_intersections_source_and_target": True,
        "all_stored_field_wavelength_nodes_retraced": True,
        "optical_psf_semantics": "geometric_pre_pixel_ray_histogram",
        "sensor_pixel_aperture_included": False,
        "azimuth_rotation_qualified": False,
        "scene_halo_qualified": False,
        "diffraction_included": False,
        "lens_radiometry_validated": False,
        "physical_mtf_comparison_allowed": False,
        "product_ranking_allowed": False,
    }
    derived_psf_table = dict(ray["psf"])
    derived_psf_table["function"] = derived
    derived_psf_table["sample_spacing_mm"] = np.asarray(
        [target_pitch_mm, target_pitch_mm], dtype=float
    )
    derived_psf_table["coordinate_convention"] = "pixel_centers"
    derived_psf_table["derived_kernel_sha256"] = derived_signature
    derived_psf_table["derivation"] = {
        "schema": SCHEMA,
        "source_psf_asset_sha256": validation["asset_sha256"],
        "source_psf_array_sha256": original_array_sha,
        "derived_psf_array_sha256": derived_array_sha,
        "derived_psf_signature_sha256": derived_signature,
        "declared_optical_convolution_pitch_um": convolution_pitch_um,
        "all_stored_field_wavelength_nodes_retraced": True,
        "azimuth_rotation_qualified": False,
        "physical_mtf_comparison_allowed": False,
    }
    derived_ray = dict(ray)
    derived_ray["psf"] = derived_psf_table
    derived_ray["computation"] = {
        **ray.get("computation", {}),
        "psf_spacing_m": convolution_pitch_um * 1e-6,
    }
    derived_ray["geometric_psf_rehistogram"] = provenance
    derived_optics = dict(optics)
    derived_optics["raytrace"] = derived_ray
    return GeometricRehistogramResult(optics=derived_optics, provenance=provenance)
