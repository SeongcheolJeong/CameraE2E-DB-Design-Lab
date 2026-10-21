"""Source-bound, exact spectral/field diffraction for nine native sensor crops.

No PSF field interpolation or Gaussian surrogate is used. The source request,
stop, detector plane, spectral samples and chief-image anchors are immutable.
Nested output grids are decimated from directly propagated optical point samples;
this never upsamples a native pixel-integrated PSF. Receiver integration belongs
to the downstream capture, once. Cache nodes are hash checked and resumable.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import shutil
import time
from copy import deepcopy
from pathlib import Path

import numpy as np
from scipy.optimize import brentq

from . import db_design_optics as db
from . import db_design_sensor_timing as timing
from . import prescription_psf
from . import reference_exit_pupil as pupil
from . import reference_workshop_optics as optics
from .registry import canonical, digest

SCHEMA = "calibration_direct_db_sensor_crops_v1"
REFERENCE_Q = 16
REFERENCE_RADIUS = 96
PHOTONS_PER_PIXEL = 8000.0
MAX_WORKING_BYTES = 1024**3


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            h.update(block)
    return h.hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(canonical(value))
    tmp.replace(path)


def backend():
    return {
        "generator_sha256": sha(__file__),
        "exit_pupil_sha256": sha(pupil.__file__),
        "prescription_sha256": sha(prescription_psf.__file__),
        "rayoptics": importlib.metadata.version("rayoptics"),
        "prysm": importlib.metadata.version("prysm"),
        "numpy": np.__version__,
    }


def geometry_digest(g):
    return digest({k: v for k, v in g.items() if k not in {"geometry_sha256", "numerical_application"}})


def validate(g):
    if g.get("schema") != SCHEMA or g.get("geometry_sha256") != geometry_digest(g):
        raise ValueError("Direct sensor geometry changed")
    if g["backend"] != backend():
        raise ValueError("Direct optics generator/backend differs from frozen request")
    if g["wavelength_nm"] != np.arange(450.0, 651.0, 10.0).tolist() or len(g["crops"]) != 9:
        raise ValueError("Nine direct fields and 21 exact spectral nodes required")
    for c in g["crops"]:
        if "node" in c or "interpolation_weight" in c or c["shape_row_col"] != [128, 128]:
            raise ValueError("Direct source cannot contain interpolated PSF nodes")
        if any(
            type(o) is not int or o % 2 or o < 0 or o + 128 > n
            for o, n in zip(c["origin_row_col"], g["full_sensor_shape"], strict=True)
        ):
            raise ValueError("Direct crop must preserve native active-array RGGB coordinates")


def bind(profile, prepared, workspace):
    from rayoptics.raytr import trace
    from rayoptics.raytr.opticalspec import Field

    sensor = prepared["candidate"]["source_identities"]["sensor"]
    if (
        sensor["revision_id"] != timing.REVISION
        or sensor["content_sha256"] != timing.REVISION_SHA
        or profile["pixel_pitch_um"] != 2.9
    ):
        raise ValueError("Direct crops require pinned IMX462 structural sensor")
    dimensions = timing.profile(workspace)
    key = prepared["candidate"]["lens_key"]
    current = db.source_metadata(workspace, key)
    original = prepared["candidate"]["source_identities"]["lens"]["source"]
    for name in (
        "revision_id",
        "revision_content_sha256",
        "reviewed_csv_sha256",
        "prescription_sha256",
        "applied_prescription_sha256",
        "pupil_condition",
        "paraxial",
    ):
        if current.get(name) != original.get(name):
            raise ValueError("Direct lens differs from frozen DB source: " + name)
    context = db._source(key)
    spec = db.template(key, optical_profile="exit_pupil_diffraction")
    waves = np.arange(450.0, 651.0, 10.0).tolist()
    requests = [optics._request(workspace, spec, waves[start:start+3], source_context=context)
                for start in range(0, len(waves), 3)]
    request = requests[3]
    model = prescription_psf._model(request, 1)

    def chief(angle):
        f = Field(y=angle, fov=model.optical_spec["fov"])
        pupil.aim_at_stop(model, f, 550.0)
        ray, _, _ = trace.trace_base(
            model,
            [0.0, 0.0],
            f,
            550.0,
            apply_vignetting=False,
            check_apertures=True,
            pt_inside_fuzz=0.0,
        )
        prescription_psf._validate_physical_ray(
            ray, model, request["prescription"]["surfaces"], "direct crop chief"
        )
        return np.asarray(ray[-1][0][:2], float)

    axis = dimensions["optical_axis_active_row_col"]
    labels = (("nw", "n", "ne"), ("w", "center", "e"), ("sw", "s", "se"))
    crops = []
    for ri, row in enumerate((64, 484, 904)):
        for ci, col in enumerate((64, 904, 1744)):
            center = [row + 63.5, col + 63.5]
            anchor = list(axis) if (ri, ci) == (1, 1) else center[:]
            xy = [(anchor[1] - axis[1]) * 0.0029, -(anchor[0] - axis[0]) * 0.0029]
            radius = float(np.linalg.norm(xy))
            angle = (
                0.0
                if radius == 0
                else brentq(
                    lambda a, radius=radius: np.linalg.norm(chief(a)) - radius,
                    0.0,
                    12.0,
                    xtol=1e-10,
                )
            )
            point = chief(angle)
            if abs(np.linalg.norm(point) - radius) > 1e-7:
                raise ValueError("Retraced chief misses declared sensor anchor")
            crops.append(
                {
                    "id": "crop-" + labels[ri][ci],
                    "origin_row_col": [row, col],
                    "shape_row_col": [128, 128],
                    "center_active_row_col": center,
                    "optical_anchor_active_row_col": anchor,
                    "image_xy_mm": xy,
                    "object_angle_deg": float(angle),
                    "meridional_anchor_xy_mm": point.tolist(),
                    "azimuth_deg": math.degrees(math.atan2(xy[1], xy[0])) if radius else 0.0,
                    "chief_anchor_residual_mm": float(abs(np.linalg.norm(point) - radius)),
                }
            )
    qe = np.asarray(profile["spectral_response"]["center"], float)
    if not all(np.array_equal(qe, np.asarray(v)) for v in profile["spectral_response"].values()):
        raise ValueError("Direct crop adapter requires angle-independent replacement QE")
    illumination = np.asarray(prepared["spectral"]["white"], float)
    weights = illumination[:, None] * qe[:, [0, 1, 3]]
    weights[:, 1] = illumination * qe[:, 1:3].mean(axis=1)
    white_e = PHOTONS_PER_PIXEL * weights.sum(axis=0) / illumination.sum()
    weights /= weights.sum(axis=0)
    g = {
        "schema": SCHEMA,
        "full_sensor_shape": dimensions["active_shape_row_col"],
        "pixel_pitch_um": 2.9,
        "optical_axis_active_row_col": axis,
        "dimension_source": dimensions["dimension_source"],
        "sensor_revision_id": sensor["revision_id"],
        "sensor_revision_sha256": sensor["content_sha256"],
        "cfa_phase_provenance": "assumed RGGB at active origin; even located crops and halos",
        "source_geometry_sha256": dimensions["profile_sha256"],
        "crops": crops,
        "coordinate_convention": ("native pixel centers at integer active row/col; "
                                  "optical x right and y up"),
        "lens_source": current,
        "lens_key": key,
        "requests": requests,
        "backend": backend(),
        "wavelength_nm": waves,
        "reference_q": REFERENCE_Q,
        "reference_radius_native_px": REFERENCE_RADIUS,
        "spectral_weights_rgb": weights.tolist(),
        "white_e_rgb": white_e.tolist(),
        "incident_photons_per_pixel": PHOTONS_PER_PIXEL,
        "radiometry_scope": ("normalized D65 spectral photon-dose edge; "
                             "absolute field irradiance/shading not identified"),
        "psf_source": "direct_prescription_reference_sphere_scalar_diffraction",
        "psf_included_effects": ["lens_aberrations", "diffraction", "lateral_chromatic_shift"],
        "pixel_aperture_application_count": 1,
        "charge_diffusion": "zero_not_separately_identified",
        "interpolation": "none_between_fields_or_wavelengths",
        "full_frame_allocated": False,
        "local_stationary_psf": True,
        "physical_validation": "not_established",
    }
    g["geometry_sha256"] = geometry_digest(g)
    validate(g)
    p = deepcopy(profile)
    p["sensor_crops"] = g
    p.pop("profile_sha256", None)
    p["profile_sha256"] = digest(p)
    return p


def _trace_record(trace):
    return {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in trace.items()}


def _qualified_wave(request, wi, crop, folder):
    """Same unchanged physical gates as reference_exit_pupil.node, with retained rays."""
    wave = request["prescription"]["wavelength_nm"][wi]
    attempts = []
    for n in (24, 48, 96):
        traces = [
            pupil.trace_pupil(
                request,
                wi,
                crop["object_angle_deg"],
                np.asarray(crop["meridional_anchor_xy_mm"]),
                count,
                crop["azimuth_deg"],
            )
            for count in (n, 2 * n)
        ]
        # Fixed 129 native-pixel physical qualification, unchanged from DB optics.
        span = max(float(np.max(np.ptp(t["q"], axis=0)) * 1.04) for t in traces)
        grid = max(
            256, 2 ** math.ceil(math.log2(math.ceil(129 * 2.9 * span / (0.8 * wave * 0.001)) + 1))
        )
        ps = [pupil.regular_pupil(t, grid) for t in traces]
        fine = pupil.regular_pupil(traces[-1], 2 * grid)
        masses = [pupil.propagate(v, wave, 129, 2.9, 11) for v in ps]
        masses += [pupil.propagate(fine, wave, 129, 2.9, s) for s in (5, 11)]
        energy = np.asarray([v.sum() for v in masses])
        normalized = [v / e for v, e in zip(masses, energy, strict=True)]
        changes = [float(np.abs(v - normalized[-1]).sum()) for v in normalized[:-1]]
        error = pupil.interpolation_error(*traces, wave)
        phase = pupil.phase_grid_step(fine, wave)
        passed = (
            np.isfinite(energy).all()
            and energy.min() >= 0.95
            and energy.max() <= 1.001
            and max(changes) <= 0.03
            and phase <= 0.25
            and error["maximum_abs_waves"] <= 0.02
        )
        attempts.append(
            {
                "pupil_samples": [n, 2 * n],
                "direction_grid_samples": [grid, 2 * grid],
                "retained_energy": energy.tolist(),
                "kernel_l1_changes": changes,
                "maximum_neighbor_opd_waves": phase,
                "unwrapped_opd_interpolation": error,
                "passed": bool(passed),
                "wavelength_nm": wave,
            }
        )
        if passed:
            np.savez_compressed(
                folder / "qualified-rays.npz",
                **{
                    f"{role}_{key}": value
                    for role, t in zip(("coarse", "fine"), traces, strict=True)
                    for key, value in t.items()
                    if isinstance(value, np.ndarray)
                },
            )
            save(
                folder / "qualification.json",
                {
                    "status": "passed",
                    "attempts": attempts,
                    "ray_metadata": [
                        {
                            k: v
                            for k, v in _trace_record(t).items()
                            if not isinstance(t[k], np.ndarray)
                        }
                        for t in traces
                    ],
                    "ray_arrays_sha256": sha(folder / "qualified-rays.npz"),
                    "limits": {
                        "kernel_l1": 0.03,
                        "minimum_retained_energy": 0.95,
                        "maximum_retained_energy": 1.001,
                        "phase_step_waves": 0.25,
                        "opd_error_waves": 0.02,
                    },
                },
            )
            return traces[-1], fine, attempts[-1]
    raise ValueError("Direct prescription pupil qualification failed: " + canonical(attempts))


def _reference_from_trace(trace, wave):
    span = float(np.max(np.ptp(trace["q"], axis=0)) * 1.04)
    extent_um = (2 * REFERENCE_RADIUS + 1 / REFERENCE_Q) * 2.9
    minimum = math.ceil(extent_um * span / (0.8 * wave * 0.001))
    grid = max(512, 2 ** math.ceil(math.log2(minimum + 1)))
    p = pupil.regular_pupil(trace, grid)
    # Pupil is independently qualified; direction/phase criterion remains explicit.
    if pupil.phase_grid_step(p, wave) > 0.25:
        raise ValueError("Wide direct PSF direction grid phase criterion failed")
    size = 2 * REFERENCE_RADIUS * REFERENCE_Q + 1
    mass = pupil.propagate(p, wave, size, 2.9 / REFERENCE_Q, 1)
    if not np.isfinite(mass).all() or np.any(mass < 0) or not 0.95 <= mass.sum() <= 1.001:
        raise ValueError("Wide direct PSF energy is outside unchanged physical bounds")
    return mass, {
        "direction_grid_samples": grid,
        "retained_energy": float(mass.sum()),
        "point_pitch_um": 2.9 / REFERENCE_Q,
        "point_shape_row_col": list(mass.shape),
        "intensity_nyquist_pass": True,
        "output_cell_area_integrated": False,
    }


def generate(g, workspace, progress=None):
    """Generate each field and exact spectral node once, retaining resumable evidence."""
    validate(g)
    cache = Path(workspace) / ".cache" / "calibration-direct-psf" / g["geometry_sha256"]
    cache.mkdir(parents=True, exist_ok=True)
    save(cache / "geometry.json", g)
    records = []
    for ci, c in enumerate(g["crops"]):
        field = cache / c["id"]
        field.mkdir(exist_ok=True)
        if (field / "result.json").exists():
            row = json.loads((field / "result.json").read_text())
            if sha(field / "spectral-psf.npz") != row["sha256"]:
                raise ValueError("Completed direct optical cache changed")
            records.append(row)
            continue
        size = 2 * REFERENCE_RADIUS * REFERENCE_Q + 1
        summed = np.zeros((size, size, 3), dtype=np.float64)
        diagnostics = []
        started = time.perf_counter()
        for wi, wave in enumerate(g["wavelength_nm"]):
            if progress:
                progress(f"DB 직접 PSF · {ci + 1}/9 {c['id']} · {wi + 1}/21 {wave:g}nm")
            folder = field / f"wave-{int(wave)}"
            folder.mkdir(exist_ok=True)
            qualification = folder / "qualification.json"
            if qualification.exists():
                saved = json.loads(qualification.read_text())
                if (
                    saved["status"] != "passed"
                    or sha(folder / "qualified-rays.npz") != saved["ray_arrays_sha256"]
                ):
                    raise ValueError("Retained direct pupil ray cache changed")
                with np.load(folder / "qualified-rays.npz", allow_pickle=False) as z:
                    trace = {k.removeprefix("fine_"): z[k] for k in z if k.startswith("fine_")}
                trace.update(saved["ray_metadata"][-1])
                diag = saved["attempts"][-1]
            else:
                trace, _, diag = _qualified_wave(g["requests"][wi//3], wi%3, c, folder)
            mass, reference = _reference_from_trace(trace, wave)
            from prysm.fttools import mdft

            mdft.clear()
            for ch, weight in enumerate(g["spectral_weights_rgb"][wi]):
                summed[..., ch] += mass * weight
            diagnostics.append(
                {
                    "wavelength_nm": wave,
                    "physical_qualification": diag,
                    "reference": reference,
                    "qualification_file": str(qualification.relative_to(cache)),
                    "qualification_sha256": sha(qualification),
                    "rays_file": str((folder / "qualified-rays.npz").relative_to(cache)),
                    "rays_sha256": sha(folder / "qualified-rays.npz"),
                }
            )
        output = field / "spectral-psf.npz"
        # Float32 storage is explicitly bounded; all accumulation was float64.
        del mass
        cast = summed.astype(np.float32)
        storage_error = max(
            float(np.max(np.abs(cast[y : y + 32].astype(float) - summed[y : y + 32])))
            for y in range(0, size, 32)
        )
        if storage_error > 1e-7:
            raise ValueError("Stored direct PSF precision exceeded declared absolute mass bound")
        np.savez_compressed(output, mass_rgb=cast)
        row = {
            "field": c["id"],
            "file": str(output.relative_to(cache)),
            "sha256": sha(output),
            "wavelengths": diagnostics,
            "shape_row_col_channels": list(cast.shape),
            "energy_rgb": cast.sum(axis=(0, 1), dtype=np.float64).tolist(),
            "elapsed_seconds": time.perf_counter() - started,
            "storage_dtype": "float32",
            "storage_max_abs_mass_error": storage_error,
        }
        save(field / "result.json", row)
        records.append(row)
    result = {
        "schema": SCHEMA,
        "geometry_sha256": g["geometry_sha256"],
        "fields": records,
        "backend": g["backend"],
        "status": "passed",
    }
    save(cache / "result.json", result)
    return cache, result


def retain(g, workspace, root, progress=None):
    cache, result = generate(g, workspace, progress)
    destination = Path(root) / "direct-optics"
    destination.mkdir(parents=True, exist_ok=True)
    for p in cache.rglob("*"):
        if p.is_file() and not p.name.endswith(".tmp"):
            target = destination / p.relative_to(cache)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
    for row in result["fields"]:
        if sha(destination / row["file"]) != row["sha256"]:
            raise ValueError("Retained optical field differs from source cache")
    return result
