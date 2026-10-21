"""Source-pinned patent virtual optical bench, with explicit geometric/diffraction modes.

The geometric profile retraces the actual edited prescription. It is NOT a
fallback PSF for a refused diffraction calculation. Receiver pixel binning is
applied once; reciprocal radiometry and absolute angular QE remain separate.
"""

from __future__ import annotations

import copy
import inspect
import json
import math
import time
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from . import db_design_radiometry, lens_factory, prescription_psf, reference_exit_pupil
from . import design_workshop_optics as shared
from .lens_db_wavefront_bridge import APERTURE_HYPOTHESIS, db_wavefront_preflight

SCHEMA = "camera_reference_workshop_optics_v1"
IRCF_SCHEMA = "camera_reference_ircf_optics_v1"
REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"
CSV_SHA256 = "f3f4790fc4135501c2921ceb532cb6af764b991e3df5fe9c805953deae5779bd"
WAVE = np.arange(450.0, 651.0, 10.0)
MAX_HEIGHT_MM = 13.66
REFERENCE_FOCAL_MM = 35.14


def template(workspace=None):
    return {
        "schema": SCHEMA,
        "reference_revision_id": REVISION,
        "optical_profile": "geometric_retrace",
        "controls": {"curvature_scale": 1.0, "sensor_shift_um": 0.0},
        "pixel_pitch_um": 2.9,
        "native_shape_row_col": [256, 256],
        "wavelength_nm": WAVE.tolist(),
        "pupil_samples": 48,
        "kernel_size": 129,
        "subpixel_samples": 5,
    }


def normalize(specification, *, source_context=None):
    data = template()
    if not isinstance(specification, dict) or set(specification) - set(data):
        raise ValueError("Unknown reference optical specification fields")
    supplied = copy.deepcopy(specification)
    controls = {**data["controls"], **supplied.pop("controls", {})}
    data.update(supplied)
    data["controls"] = controls
    expected_revision = REVISION if source_context is None else source_context["revision_id"]
    if (
        data["schema"] not in {SCHEMA, IRCF_SCHEMA}
        or data["reference_revision_id"] != expected_revision
    ):
        raise ValueError("Select the pinned p0014 intermediate reference")
    if data["optical_profile"] not in {
        "geometric_retrace",
        "wavefront",
        reference_exit_pupil.PROFILE,
    }:
        raise ValueError("Choose explicit geometric_retrace, wavefront or exit_pupil_diffraction")
    if set(controls) != {"curvature_scale", "sensor_shift_um"}:
        raise ValueError("Only first finite curvature and sensor Z are editable")
    for name, bounds in (("curvature_scale", (0.995, 1.005)), ("sensor_shift_um", (-20, 20))):
        v = controls[name]
        if type(v) not in (int, float) or not math.isfinite(v) or not bounds[0] <= v <= bounds[1]:
            raise ValueError(f"{name} must lie within {bounds}")
        controls[name] = float(v)
    if data["pixel_pitch_um"] != 2.9 or data["native_shape_row_col"] != [256, 256]:
        raise ValueError("Reference bench preserves 2.9um pitch and 256x256 native ROIs")
    if data["schema"] == IRCF_SCHEMA:
        supported = [np.arange(400.0, 1001.0, step).tolist() for step in (10.0, 5.0)]
        if data["wavelength_nm"] not in supported:
            raise ValueError("IRCF spectral axis requires 400–1000nm in exact10 or5nm steps")
        if data["optical_profile"] != "geometric_retrace":
            raise ValueError("IRCF profile supports explicit geometric retrace only")
    elif data["wavelength_nm"] != WAVE.tolist():
        raise ValueError("Reference spectral axis is exactly450–650nm in10nm bins")
    if (
        type(data["pupil_samples"]) is not int
        or not 24 <= data["pupil_samples"] <= 96
        or data["pupil_samples"] % 2
    ):
        raise ValueError("pupil_samples requires an even24..96")
    if (
        type(data["kernel_size"]) is not int
        or not 17 <= data["kernel_size"] <= 257
        or data["kernel_size"] % 2 != 1
    ):
        raise ValueError("Use odd geometric kernel_size17..257")
    if data["subpixel_samples"] != 5:
        raise ValueError("Wavefront receiver subpixel setting is fixed at5")
    return data


def _preflight(workspace, waves, *, source_context=None):
    p = db_wavefront_preflight(
        REVISION if source_context is None else source_context["revision_id"],
        catalog_root=Path(workspace) / "camerae2e_db",
        reviewed_csv_sha256=CSV_SHA256 if source_context is None else source_context["csv_sha256"],
        aperture_hypothesis=APERTURE_HYPOTHESIS,
        wavelength_nm=list(waves),
        field_x_mm=[-0.001, 0.001],
        field_y_mm=[-0.001, 0.001],
        pixel_pitch_um=2.9,
        pupil_samples=54,
        kernel_size=17,
        subpixel_samples=5,
    )
    if p["status"] != "research_request_ready":
        raise ValueError("Pinned reference conversion refused: " + json.dumps(p["blockers"]))
    if source_context is not None and "operating_f_number" in source_context:
        p = _operating_pupil(workspace, p, source_context)
    return p


def _paraxial_values(request, wavelength_index):
    model = prescription_psf._model(request, wavelength_index)
    fod = model["analysis_results"]["parax_data"].fod
    focal, radius = float(fod.efl), float(abs(fod.enp_radius))
    if not (math.isfinite(focal) and math.isfinite(radius) and focal > 0 and radius > 0):
        raise ValueError("Operating pupil requires positive finite paraxial values")
    return {
        "wavelength_nm": request["prescription"]["wavelength_nm"][wavelength_index],
        "effective_focal_length_mm": focal,
        "entrance_pupil_radius_mm": radius,
        "effective_f_number": focal / (2 * radius),
        "status": "computed_paraxial_only",
        "not_diffraction": True,
        "physical_validation": "not_established",
    }


def _operating_pupil(workspace, preflight, context):
    """Derive one physical stop at the patent reference wavelength, then freeze it.

    A tabulated effective aperture is not necessarily the operating iris of a
    zoom configuration. Preserve that source and adjust only the explicit stop
    in the execution prescription. Forward PSF and reciprocal radiometry both
    consume this same prescription, including its hard stop aperture.
    """
    target = context["operating_f_number"]
    wavelength = context["f_number_reference_wavelength_nm"]
    if type(target) not in (int, float) or not math.isfinite(target) or target <= 0:
        raise ValueError("Operating F-number must be finite and positive")
    original_context = {k: v for k, v in context.items() if k != "operating_f_number"}
    reference = _preflight(
        workspace, sorted({550.0, float(wavelength)}), source_context=original_context
    )["psf_request"]
    wi = reference["prescription"]["wavelength_nm"].index(wavelength)
    before = _paraxial_values(reference, wi)
    source_stop = next(s for s in reference["prescription"]["surfaces"] if s["is_stop"])
    original_radius = source_stop["semi_diameter_mm"]
    radius = original_radius * before["effective_f_number"] / target
    if radius > original_radius * (1 + 1e-12):
        raise ValueError("Requested operating aperture exceeds the authored stop aperture")
    source_stop["semi_diameter_mm"] = radius
    after = _paraxial_values(reference, wi)
    if not math.isclose(after["effective_f_number"], target, rel_tol=1e-9):
        raise ValueError("Derived operating stop does not reproduce the requested F-number")
    p = copy.deepcopy(preflight)
    stop = next(s for s in p["psf_request"]["prescription"]["surfaces"] if s["is_stop"])
    if not math.isclose(stop["semi_diameter_mm"], original_radius, rel_tol=1e-12):
        raise ValueError("Monochromatic batches disagree on the source stop aperture")
    stop["semi_diameter_mm"] = radius
    p["source_stop_paraxial"] = p["paraxial_translation_diagnostic"]
    waves = p["psf_request"]["prescription"]["wavelength_nm"]
    p["paraxial_translation_diagnostic"] = _paraxial_values(
        p["psf_request"], min(range(len(waves)), key=lambda i: abs(waves[i] - 550))
    )
    p["pupil_condition"] = {
        "mode": "patent_f_number_at_reference_wavelength",
        "requested_f_number": target,
        "reference_wavelength_nm": wavelength,
        "effective_f_number": after["effective_f_number"],
        "effective_focal_length_mm": after["effective_focal_length_mm"],
        "entrance_pupil_radius_mm": after["entrance_pupil_radius_mm"],
        "source_stop_semi_diameter_mm": original_radius,
        "applied_stop_semi_diameter_mm": radius,
        "source_stop_f_number_at_reference_wavelength": before["effective_f_number"],
        "stop_surface_id": stop["id"],
        "derivation": "source stop radius * source F-number / requested F-number",
        "scope": "One physical stop across all wavelengths and fields; forward and reciprocal",
        "source_url": context.get("f_number_source_url"),
        "physical_validation": "not_established",
    }
    p["assumptions"] = [*p["assumptions"], "operating_stop_derived_from_patent_f_number"]
    return p


def source_metadata(workspace, *, source_context=None):
    p = _preflight(workspace, [450.0, 550.0, 650.0], source_context=source_context)
    return {
        "id": "US10310255B2-p0014-intermediate" if source_context is None else source_context["id"],
        "name": "US10310255B2 · p0014 · intermediate"
        if source_context is None
        else source_context["name"],
        **p["source"],
        "image_height_mm": MAX_HEIGHT_MM
        if source_context is None
        else source_context["image_height_mm"],
        "reference_focal_length_mm": REFERENCE_FOCAL_MM
        if source_context is None
        else source_context["focal_length_mm"],
        "surfaces": p["psf_request"]["prescription"]["surfaces"],
        "paraxial": p["paraxial_translation_diagnostic"],
        "assumptions": p["assumptions"],
        "limitations": p["limitations"],
        "physical_validation": "not_established",
        "source_rights": "Internal research; source redistribution not established",
        **(
            {
                "pupil_condition": p["pupil_condition"],
                "source_stop_paraxial": p["source_stop_paraxial"],
                "applied_prescription_sha256": shared._sha(p["psf_request"]["prescription"]),
            }
            if "pupil_condition" in p
            else {}
        ),
    }


def field_layout(workspace=None):
    rows = [
        ("center", "중심", 0.0, 0.0),
        ("mid-right", "중간 우측", 0.5, 0.0),
        ("mid-top", "중간 상단", 0.5, 90.0),
        ("mid-left", "중간 좌측", 0.5, 180.0),
        ("mid-bottom", "중간 하단", 0.5, 270.0),
        ("outer-ne", "주변 우상", 0.85, 45.0),
        ("outer-nw", "주변 좌상", 0.85, 135.0),
        ("outer-sw", "주변 좌하", 0.85, 225.0),
        ("outer-se", "주변 우하", 0.85, 315.0),
    ]
    return [
        {
            "id": name,
            "label": label,
            "normalized_radius": radius,
            "azimuth_deg": azimuth,
            "x": radius * math.cos(math.radians(azimuth)),
            "y": radius * math.sin(math.radians(azimuth)),
            "baseline_reference_xy_mm": [
                radius * MAX_HEIGHT_MM * math.cos(math.radians(azimuth)),
                radius * MAX_HEIGHT_MM * math.sin(math.radians(azimuth)),
            ],
            "object_angle_deg": math.degrees(
                math.atan2(radius * MAX_HEIGHT_MM, REFERENCE_FOCAL_MM)
            ),
        }
        for name, label, radius, azimuth in rows
    ]


def geometry(spec, *, source_context=None):
    halo = spec["kernel_size"] // 2
    focal = REFERENCE_FOCAL_MM if source_context is None else source_context["focal_length_mm"]
    return {
        "native_shape_row_col": [256, 256],
        "input_shape_row_col": [256 + 2 * halo] * 2,
        "crop_bounds_row_col": [halo, halo + 256, halo, halo + 256],
        "pixel_pitch_um": 2.9,
        "receiver_sampling": "pixel_integrated_optical_impulse",
        "source_sampling": "native_cell_center_with_optical_halo",
        "field_approximation": "local_shift_invariant_at_declared_field_node",
        "product_mode": False,
        "cfa_origin": {
            "local_row": 0,
            "local_col": 0,
            "pattern": "RGGB",
            "scope": "independent_virtual_bench_ROI",
        },
        "reference_focal_length_mm": focal,
        "horizontal_fov_deg": math.degrees(2 * math.atan(256 * 2.9 / 2000 / focal)),
    }


def _backend():
    output = shared._backend()
    output["adapter_sha256"] = shared._file_sha(__file__)
    output["shared_adapter_sha256"] = shared._file_sha(shared.__file__)
    output["exit_pupil_adapter_sha256"] = shared._file_sha(reference_exit_pupil.__file__)
    output["dense_radiometry_adapter_sha256"] = shared._file_sha(db_design_radiometry.__file__)
    return output


def cache_key(workspace, specification, *, source_context=None):
    source = source_metadata(workspace, source_context=source_context)
    return shared._sha(
        {
            "specification": normalize(specification, source_context=source_context),
            "backend": _backend(),
            "reciprocal_radiometry": {
                "profile": (source_context or {}).get("reciprocal_profile", "lens_factory_default"),
                "base_sampling": (source_context or {}).get(
                    "reciprocal_sampling", {"polar_rings": 40, "azimuth_samples": 28}
                ),
            },
            "source": source["prescription_sha256"],
            **({"located_fields": source_context["located_fields"]}
               if source_context is not None and "located_fields" in source_context else {}),
            **(
                {
                    "pupil_condition": source["pupil_condition"],
                    "applied_prescription": source["applied_prescription_sha256"],
                }
                if "pupil_condition" in source
                else {}
            ),
        }
    )


def _request(workspace, spec, waves, *, source_context=None):
    request = copy.deepcopy(
        _preflight(workspace, waves, source_context=source_context)["psf_request"]
    )
    first = next(s for s in request["prescription"]["surfaces"] if s["radius_mm"] not in (None, 0))
    # Curvature, not radius, scales; modifying a radius directly would reverse the control.
    first["radius_mm"] /= spec["controls"]["curvature_scale"]
    request["prescription"]["surfaces"][-1]["thickness_mm"] += (
        spec["controls"]["sensor_shift_um"] / 1000
    )
    return request


def _rotate(points, azimuth):
    # Solver meridional coordinate is +Y. Field layout uses azimuth from +X.
    angle = math.radians(azimuth - 90.0)
    rotation = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    return np.asarray(points) @ rotation.T


def _histogram(points, size, pitch_um):
    """Integrate geometric delta-ray mass over square receiving pixels exactly once."""
    half = (size - 1) / 2
    edges = (np.arange(size + 1) - half - 0.5) * pitch_um / 1000
    counts, _, _ = np.histogram2d(-points[:, 1], points[:, 0], bins=(edges, edges))
    retained = float(counts.sum() / len(points))
    if retained < 0.999:
        raise ValueError(
            f"Geometric PSF retains {retained:.6f}; required0.999, enlarge explicit kernel"
        )
    return counts / counts.sum(), retained


def _rays(request, wavelength_index, radii, samples):
    from rayoptics.raytr import trace
    from rayoptics.raytr.opticalspec import Field

    wave = request["prescription"]["wavelength_nm"][wavelength_index]
    model = prescription_psf._model(request, wavelength_index)
    axis = np.linspace(-1.0, 1.0, samples)
    pupil = np.asarray([(x, y) for y in axis for x in axis if x * x + y * y <= 1])
    output = []
    for radius in radii:
        field = Field(
            y=math.degrees(math.atan2(radius, REFERENCE_FOCAL_MM)), fov=model.optical_spec["fov"]
        )
        field.aim_info = trace.aim_chief_ray(model, field, wave)
        ray, _, _ = trace.trace_base(
            model, [0.0, 0.0], field, wave, check_apertures=True, apply_vignetting=False
        )
        prescription_psf._validate_physical_ray(
            ray, model, request["prescription"]["surfaces"], "geometric chief"
        )
        chief = np.asarray(ray[-1][0][:2])
        stop_residual = float(np.linalg.norm(ray[model.seq_model.stop_surface][0][:2]))
        if stop_residual > 1e-5:
            raise ValueError("Geometric chief aiming exceeds10nm stop-center residual")
        points, directions, rejected = [], [], {}
        for xy in pupil:
            try:
                path, _, _ = trace.trace_base(
                    model,
                    xy,
                    field,
                    wave,
                    check_apertures=True,
                    apply_vignetting=False,
                    pt_inside_fuzz=0.0,
                )
                prescription_psf._validate_physical_ray(
                    path, model, request["prescription"]["surfaces"], "geometric pupil"
                )
            except Exception as exc:
                name = type(exc).__name__
                if not name.startswith("Trace"):
                    raise
                rejected[name] = rejected.get(name, 0) + 1
                continue
            points.append(np.asarray(path[-1][0][:2]))
            directions.append(np.asarray(path[-1][1]))
        if not points:
            raise ValueError("No admitted geometric pupil rays")
        output.append(
            {
                "points": np.asarray(points),
                "directions": np.asarray(directions),
                "chief": chief,
                "chief_direction": np.asarray(ray[-1][1]),
                "requested_rays": len(pupil),
                "rejected": rejected,
                "stop_aim_residual_mm": stop_residual,
                "effective_focal_length_mm": float(model["ar"]["parax_data"].fod.efl),
            }
        )
    return output


def _picture(directory, reference, fields, psf, reference_index=10):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 3, figsize=(9, 8))
    for ax in axes.flat:
        ax.set_visible(False)
    for ax, field, kernel in zip(
        list(axes.flat)[: len(fields)], fields, psf[reference_index], strict=True
    ):
        ax.set_visible(True)
        ax.imshow(np.log10(np.maximum(kernel, 1e-8)), cmap="magma", vmin=-8, vmax=0)
        ax.set_title(field["id"], fontsize=9)
    diffracted = any(f.get("diffraction_included") for f in fields)
    label = "aberration + diffraction" if diffracted else "geometric · diffraction excluded"
    fig.suptitle("550 nm receiver-pixel PSF · " + label)
    fig.tight_layout()
    fig.savefig(directory / "psf.png", dpi=120)
    plt.close(fig)
    for fi, field in enumerate(fields):
        if field["status"] != "succeeded":
            field["images"] = {}
            continue
        name = "psf-" + field["id"] + ".png"
        fig, ax = plt.subplots(figsize=(4, 3))
        im = ax.imshow(
            np.log10(np.maximum(psf[reference_index, fi], 1e-8)),
            cmap="magma",
            vmin=-8,
            vmax=0,
        )
        ax.set_title(field["id"] + " · 550nm " + label, fontsize=9)
        fig.colorbar(im, ax=ax, label="log10(pixel probability)")
        fig.tight_layout()
        fig.savefig(directory / name, dpi=120)
        plt.close(fig)
        field["images"] = {"psf": name, "layout": "layout.png"}
    fig, ax = plt.subplots(figsize=(11, 4))
    z = 0.0
    from . import optics_surfaces

    for s in reference["surfaces"]:
        radius = np.linspace(-s["semi_diameter_mm"], s["semi_diameter_mm"], 80)
        zz = [z + optics_surfaces.sag(s, abs(r)) for r in radius]
        ax.plot(zz, radius, color="teal", linewidth=0.8)
        z += s["thickness_mm"]
    ax.axvline(z, color="black", linestyle="--", label="sensor")
    ax.set(xlabel="axial coordinate (mm)", ylabel="height (mm)", title=reference["name"])
    fig.tight_layout()
    fig.savefig(directory / "layout.png", dpi=130)
    plt.close(fig)
    return {"psf": "psf.png", "layout": "layout.png"}


def _wavefront_diagnostic(request, radius):
    try:
        wi = request["prescription"]["wavelength_nm"].index(550.0)
        model = prescription_psf._model(request, wi)
        wf = prescription_psf._wavefront(
            request, model, 550.0, radius, 0.0, 16, time.monotonic() + 30
        )
        return {
            "status": "probe_only",
            "reference_axis_deg": wf["reference_axis_deg"],
            "maximum_reference_axis_deg": 2.0,
            "diffraction_accepted": False,
            "reason": "Low-resolution feasibility probe, not a converged diffraction solve",
        }
    except (ValueError, RuntimeError) as exc:
        return {"status": "refused", "reason": str(exc), "diffraction_accepted": False}


def _diffracted_psfs(workspace, spec, directory, fields, ray_rows, progress, source_context):
    """Exact spectral nodes, independently gated and resumable per field/wavelength."""
    shape = (len(spec["wavelength_nm"]), len(fields), spec["kernel_size"], spec["kernel_size"])
    actual, limited = np.zeros(shape), np.zeros(shape)
    diagnostics = [[] for _ in fields]
    waves_all = spec["wavelength_nm"]
    for start in range(0, len(waves_all), 3):
        waves = waves_all[start : start + 3]
        if len(waves) < 2:
            waves = sorted(set([550.0, *waves]))
        request = _request(workspace, spec, waves, source_context=source_context)
        for wi, wave in enumerate(waves):
            out_wi = waves_all.index(wave)
            for fi, field in enumerate(fields):
                ri = (field["pupil_node_index"] if "pupil_node_index" in field
                      else [0.0, 0.5, 0.85].index(field["normalized_radius"]))
                anchor = ray_rows[550.0][ri]["chief"]
                signature = shared._sha(
                    {
                        "request": request,
                        "wavelength_nm": wave,
                        "field": field["id"],
                        "angle": field["object_angle_deg"],
                        "anchor": anchor.tolist(),
                        "sampling": spec,
                        "backend": _backend(),
                    }
                )
                base = directory / ("exit-pupil-" + signature)
                manifest, array_path = base.with_suffix(".json"), base.with_suffix(".npz")
                if manifest.exists():
                    diag = json.loads(manifest.read_text())
                    if shared._file_sha(array_path) != diag["arrays_sha256"]:
                        raise ValueError("Reference-sphere diffraction node cache changed")
                    with np.load(array_path, allow_pickle=False) as saved:
                        kernel, zero = saved["psf"], saved["phase_zero_psf"]
                else:
                    attempts = []
                    for n in sorted({spec["pupil_samples"], max(64, spec["pupil_samples"]), 96}):
                        progress(
                            f"Aberration + diffraction: {wave:g}nm {field['id']} pupil{n}→{2 * n}"
                        )
                        kernel, zero, diag = reference_exit_pupil.node(
                            request,
                            wi,
                            field["object_angle_deg"],
                            anchor,
                            field["azimuth_deg"],
                            n,
                            spec["kernel_size"],
                            spec["pixel_pitch_um"],
                        )
                        attempts.append(copy.deepcopy(diag))
                        if diag["status"] == "PROPOSED":
                            break
                    diag["adaptive_attempts"] = attempts
                    np.savez_compressed(array_path, psf=kernel, phase_zero_psf=zero)
                    diag["arrays_sha256"] = shared._file_sha(array_path)
                    shared._write(manifest, diag)
                diagnostics[fi].append(diag)
                actual[out_wi, fi], limited[out_wi, fi] = kernel, zero
                if diag["status"] != "PROPOSED":
                    raise ValueError(
                        f"Diffraction refused at {wave:g}nm {field['id']}: "
                        + json.dumps(diag, allow_nan=False)
                    )
    return actual, limited, diagnostics


def _located_reciprocal(directory, request, backend, dense_profile, progress):
    """Partition physical heights into on-axis/located pairs at unchanged precision.

    Factory requests require at least two wavelengths and an on-axis height.
    Three heights exceed the public per-request budget for this prescription.
    Actual direction samples are retained; unequal histogram theta axes are not
    merged. An inconsistent common on-axis quadrature is explicitly refused.
    """
    batches, summaries = [], []
    for height in request["image_height_mm"][1:]:
        part = copy.deepcopy(request)
        part.update(image_height_mm=[0.0, height], field_radius=[0.0, 1.0])
        summary, arrays = shared._batch(
            directory,
            "located-dense-reciprocal" if dense_profile else "located-reciprocal",
            db_design_radiometry.request(part) if dense_profile else part,
            backend,
            db_design_radiometry.characterize if dense_profile else lens_factory.characterize,
            progress,
        )
        summaries.append({**summary, "located_image_height_mm": height,
                          "partition": "on_axis_plus_one_located_field_at_unchanged_sampling"})
        if summary["status"] != "PROPOSED":
            raise ValueError("Located reciprocal radiometry refused at image height "
                             + str(height) + "mm: " + json.dumps(summary.get("convergence", {})))
        batches.append(arrays)
    first = batches[0]
    for arrays in batches[1:]:
        for name, axis in (("irradiance_per_radiance_sr", 1),
                           ("directional_conditional_weights", 1),
                           ("image_direction_cosines", 0)):
            reference = np.take(first[name], 0, axis=axis)
            actual = np.take(arrays[name], 0, axis=axis)
            if reference.shape != actual.shape or not np.allclose(reference, actual, rtol=1e-12, atol=1e-14):
                raise ValueError("Located reciprocal partitions disagree on common on-axis " + name)
    combined = {}
    for name, axis in (("irradiance_per_radiance_sr", 1),
                       ("directional_conditional_weights", 1),
                       ("image_direction_cosines", 0)):
        rows = [np.take(first[name], [0], axis=axis)]
        rows.extend(np.take(arrays[name], [1], axis=axis) for arrays in batches)
        combined[name] = np.concatenate(rows, axis=axis)
    return summaries, combined


def solve(workspace, specification, output_dir, *, on_progress=None, source_context=None):
    spec = normalize(specification, source_context=source_context)
    spectral_axis = np.asarray(spec["wavelength_nm"], dtype=float)
    reference_index = spec["wavelength_nm"].index(550.0)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    progress = on_progress or (lambda message: None)
    reference = source_metadata(workspace, source_context=source_context)
    revision = cache_key(workspace, spec, source_context=source_context)
    if (directory / "result.json").exists():
        result = load_solution(directory)["result"]
        if result["revision"] != revision:
            raise ValueError("Optical cache revision changed")
        return result
    started = time.monotonic()
    located = (source_context or {}).get("located_fields")
    fields = copy.deepcopy(located) if located is not None else field_layout()
    fractions = [f["normalized_radius"] for f in fields] if located is not None else [0.0, 0.5, 0.85]
    if located is not None:
        if ([f.get("id") for f in fields] != ["center", "outer-ne"]
                or any(not math.isfinite(f["object_reference_radius_mm"])
                       or f["object_reference_radius_mm"] < 0 for f in fields)):
            raise ValueError("Located optics require two explicit center/peripheral physical fields")
    elif source_context is not None:
        # Bounded DB comparison uses two physical field directions. This
        # reduces batch work without lowering the pupil quadrature or gates.
        fields = [f for f in fields if f["id"] in {"center", "outer-ne"}]
        fractions = [0.0, 0.85]
    radii = (sorted({0.0, *(f["object_reference_radius_mm"] for f in fields)}) if located is not None
             else [fraction * MAX_HEIGHT_MM for fraction in fractions])
    for f in fields:
        f.update(geometry=geometry(spec, source_context=source_context), status="failed", issues=[])
        if source_context is not None:
            f["pupil_node_index"] = (radii.index(f["object_reference_radius_mm"]) if located is not None
                                     else fractions.index(f["normalized_radius"]))
    if located is not None:
        for f in fields:
            f["geometry"].update(copy.deepcopy(f["sensor_placement"]))
    overview = _request(workspace, spec, [450.0, 550.0, 650.0], source_context=source_context)
    diagnostic_radii = (
        radii
        if source_context is None
        else [r * source_context["focal_length_mm"] / REFERENCE_FOCAL_MM for r in radii]
    )
    diagnostic = [_wavefront_diagnostic(overview, r) for r in diagnostic_radii]
    if spec["optical_profile"] == "wavefront":
        for f in fields:
            f["issues"] = [
                "Full spectral reference-field diffraction is not established; "
                "explicit geometric profile is available"
            ]
            f["wavefront_diagnostic"] = diagnostic[f["pupil_node_index"]] if located is not None else diagnostic[fractions.index(f["normalized_radius"])]
        result = {
            "schema": spec["schema"],
            "revision": revision,
            "reference": reference,
            "specification": spec,
            "fields": fields,
            "status": "REFUSED",
            "images": {},
            "arrays_file": "optics.npz",
        }
        np.savez_compressed(directory / "optics.npz", wavelength_nm=spectral_axis)
        result["arrays_sha256"] = shared._file_sha(directory / "optics.npz")
        shared._write(directory / "result.json", result)
        return result
    ray_rows = {}
    # Independent exact monochromatic models; no spectral interpolation.
    for start in range(0, len(spectral_axis), 3):
        waves = spectral_axis[start : start + 3].tolist()
        if len(waves) < 2:
            waves = sorted(set([550.0, *waves]))
        request = _request(workspace, spec, waves, source_context=source_context)
        label = "geometric-" + shared._sha(
            {
                "request": request,
                "samples": spec["pupil_samples"],
                "field_radii_mm": radii,
                "ray_adapter": inspect.getsource(_rays),
                "prescription_backend": shared._file_sha(prescription_psf.__file__),
            }
        )
        saved = directory / (label + ".npz")
        manifest = directory / (label + ".json")
        if manifest.exists():
            meta = json.loads(manifest.read_text())
            if shared._file_sha(saved) != meta["sha256"]:
                raise ValueError("Geometric ray cache changed")
            with np.load(saved, allow_pickle=False) as loaded:
                arrays = {k: loaded[k] for k in loaded.files}
        else:
            progress(f"Actual prescription geometric retrace: {waves}nm")
            arrays, meta = {}, {"waves": waves, "traces": {}}
            for wi, wave in enumerate(waves):
                for ri, row in enumerate(_rays(request, wi, radii, spec["pupil_samples"])):
                    prefix = f"w{int(wave)}_r{ri}"
                    for key in ("points", "directions", "chief", "chief_direction"):
                        arrays[prefix + "_" + key] = row.pop(key)
                    meta["traces"][prefix] = row
            np.savez_compressed(saved, **arrays)
            meta["sha256"] = shared._file_sha(saved)
            shared._write(manifest, meta)
        for wave in waves:
            ray_rows[wave] = []
            for ri in range(len(radii)):
                prefix = f"w{int(wave)}_r{ri}"
                ray_rows[wave].append(
                    {
                        **meta["traces"][prefix],
                        **{
                            k: arrays[prefix + "_" + k]
                            for k in ("points", "directions", "chief", "chief_direction")
                        },
                    }
                )
    # Use actual changed chief-image locations for reciprocal pixel radiometry.
    heights = [float(np.linalg.norm(r["chief"])) for r in ray_rows[550.0]]
    ray_summaries, ray_arrays = [], {}
    for start in range(0, len(spectral_axis), 2):
        waves = spectral_axis[start : start + 2].tolist()
        if len(waves) == 1:
            waves = [550.0, waves[0]]
        request = _request(workspace, spec, waves, source_context=source_context)["prescription"]
        request["image_height_mm"] = heights
        request["field_radius"] = [h / heights[-1] for h in heights]
        request["sampling"].update(
            (source_context or {}).get(
                "reciprocal_sampling", {"polar_rings": 40, "azimuth_samples": 28}
            )
        )
        dense_radiometry = (source_context or {}).get("reciprocal_profile")
        if dense_radiometry not in (None, db_design_radiometry.PROFILE):
            raise ValueError("Unknown explicit reciprocal radiometry profile")
        if located is not None:
            summaries, arrays = _located_reciprocal(
                directory, request, _backend(), dense_radiometry, progress
            )
            ray_summaries.extend(summaries)
        else:
            summary, arrays = shared._batch(
                directory,
                "dense-reciprocal-radiometry" if dense_radiometry else "reciprocal-radiometry",
                db_design_radiometry.request(request) if dense_radiometry else request,
                _backend(),
                db_design_radiometry.characterize if dense_radiometry else lens_factory.characterize,
                progress,
            )
            ray_summaries.append(summary)
        for wi, wave in enumerate(waves):
            ray_arrays[wave] = {
                "irradiance": arrays["irradiance_per_radiance_sr"][wi],
                "conditional": arrays["directional_conditional_weights"][wi],
                "directions": arrays["image_direction_cosines"],
            }
    psf = np.zeros((len(spectral_axis), len(fields), spec["kernel_size"], spec["kernel_size"]))
    chief = np.zeros((len(spectral_axis), len(fields), 2))
    cra = np.zeros((len(spectral_axis), len(fields)))
    retained = np.ones((len(spectral_axis), len(fields)))
    throughput = np.zeros((len(spectral_axis), len(fields)))
    combined = {"wavelength_nm": spectral_axis}
    radiometry_ok = all(s["status"] == "PROPOSED" for s in ray_summaries)
    for fi, field in enumerate(fields):
        ri = (field["pupil_node_index"] if "pupil_node_index" in field
              else [0.0, 0.5, 0.85].index(field["normalized_radius"]))
        try:
            # Shared 550-nm chief defines this ROI; chromatic shifts remain in kernels.
            origin = ray_rows[550.0][ri]["chief"]
            for wi, wave in enumerate(spectral_axis):
                row = ray_rows[wave][ri]
                points = _rotate(row["points"] - origin, field["azimuth_deg"])
                psf[wi, fi], retained[wi, fi] = _histogram(
                    points, spec["kernel_size"], spec["pixel_pitch_um"]
                )
                chief[wi, fi] = _rotate(row["chief"], field["azimuth_deg"])
                cra[wi, fi] = math.degrees(math.acos(np.clip(row["chief_direction"][2], -1, 1)))
                throughput[wi, fi] = len(row["points"]) / row["requested_rays"]
            if not radiometry_ok:
                raise ValueError(
                    "Reciprocal radiometry did not meet unchanged Factory convergence gates"
                )
            field["status"] = "succeeded"
        except ValueError as exc:
            field["issues"].append(str(exc))
        radius = field["normalized_radius"] * MAX_HEIGHT_MM
        if source_context is not None:
            radius = (
                math.tan(math.radians(field["object_angle_deg"]))
                * source_context["focal_length_mm"]
            )
            field["geometry"].update(
                reference_focal_length_mm=source_context["focal_length_mm"],
                horizontal_fov_deg=math.degrees(
                    2 * math.atan(256 * 2.9 / 2000 / source_context["focal_length_mm"])
                ),
                comparison_grid="common_field_directions_native_pixel_probes_not_matched_angular_ROI",
            )
            field["native_angular_step_deg"] = math.degrees(
                math.atan(2.9 / 1000 / source_context["focal_length_mm"])
            )
            field["field_selection_basis"] = "fixed_p0014_reference_object_angle"
            if located is not None:
                field["field_selection_basis"] = "chief_ray_solved_to_declared_inside_ROI_optical_anchor"
                if not np.allclose(chief[reference_index, fi], field["target_image_xy_mm"], atol=1e-7, rtol=0):
                    raise ValueError("Retraced chief does not match the located sensor optical anchor")
        field.update(
            image_xy_mm=chief[reference_index, fi].tolist(),
            chief_ray_angle_deg=float(cra[reference_index, fi]),
            relative_illumination=float(
                ray_arrays[550.0]["irradiance"][ri] / ray_arrays[550.0]["irradiance"][0]
            ),
            irradiance_per_radiance_sr=float(ray_arrays[550.0]["irradiance"][ri]),
            distortion_percent=0.0
            if radius == 0
            else 100 * (float(np.linalg.norm(chief[reference_index, fi])) / radius - 1),
            mtf=shared._mtf(psf[reference_index, fi], 2.9, -math.radians(field["azimuth_deg"])),
            wavefront_diagnostic=diagnostic[ri],
            minimum_retained_energy_fraction=float(retained[:, fi].min()),
            pupil_throughput_fraction=float(throughput[:, fi].min()),
            psf_kind="geometric_receiver_pixel_integrated",
            diffraction_included=False,
            sampling_convergence="not_established",
            source_revision_id=REVISION
            if source_context is None
            else source_context["revision_id"],
        )
    if spec["optical_profile"] == reference_exit_pupil.PROFILE:
        geometric_psf = psf.copy()
        psf, limited, wavefront_rows = _diffracted_psfs(
            workspace, spec, directory, fields, ray_rows, progress, source_context
        )
        combined.update(geometric_psf=geometric_psf, phase_zero_diffraction_psf=limited)
        for fi, field in enumerate(fields):
            azimuth = -math.radians(field["azimuth_deg"])
            field.update(
                mtf=shared._mtf(psf[reference_index, fi], 2.9, azimuth),
                mtf_reference_wavelength_nm=550.0,
                mtf_comparison={
                    "geometric": shared._mtf(geometric_psf[reference_index, fi], 2.9, azimuth),
                    "aberrated_diffraction": shared._mtf(psf[reference_index, fi], 2.9, azimuth),
                    "diffraction_limited": shared._mtf(limited[reference_index, fi], 2.9, azimuth),
                },
                diffraction_included=True,
                psf_kind="reference_sphere_scalar_diffraction_receiver_pixel_integrated",
                sampling_convergence="passed_pupil_phase_interpolation_direction_grid_receiver_refinement",
                wavefront_diagnostic={"status": "accepted", "nodes": wavefront_rows[fi]},
                minimum_retained_energy_fraction=min(
                    min(d["retained_energy"]) for d in wavefront_rows[fi]
                ),
            )
            retained[:, fi] = [min(d["retained_energy"]) for d in wavefront_rows[fi]]
    if (source_context or {}).get("reciprocal_profile"):
        spectral_weights, spectral_directions = db_design_radiometry.spectral_directions(
            ray_arrays, spectral_axis
        )
        combined["directions_xyz_per_wavelength"] = spectral_directions
        direction_grid = spectral_directions[list(spectral_axis).index(550.0)]
    else:
        spectral_weights = np.asarray([ray_arrays[w]["conditional"] for w in spectral_axis])
        direction_grid = ray_arrays[550.0]["directions"]
    combined.update(
        psf=psf,
        chief_sensor_xy_mm=chief,
        chief_ray_angle_deg=cra,
        retained_energy_fraction=retained,
        pupil_throughput_fraction=throughput,
        image_height_mm=np.asarray(heights),
        irradiance_per_radiance_sr=np.asarray([ray_arrays[w]["irradiance"] for w in spectral_axis]),
        conditional_weights=spectral_weights,
        directions_xyz=direction_grid,
    )
    if spec["schema"] == IRCF_SCHEMA:
        # The reciprocal grid is geometric and wavelength-independent today.
        # Store each returned grid explicitly so future backend changes cannot
        # silently apply 550nm directions to another wavelength's weights.
        combined["directions_xyz_per_wavelength"] = np.asarray(
            [ray_arrays[w]["directions"] for w in spectral_axis]
        )
        combined["spectral_bin_width_nm"] = spectral_widths(spectral_axis)
    np.savez_compressed(directory / "optics.npz", **combined)
    modified_reference = {**reference, "surfaces": overview["prescription"]["surfaces"]}
    result = {
        "schema": spec["schema"],
        "revision": revision,
        "reference": reference,
        "backend": _backend(),
        "specification": spec,
        "fields": fields,
        "status": "PROPOSED" if all(f["status"] == "succeeded" for f in fields) else "PARTIAL",
        "geometry": geometry(spec, source_context=source_context),
        "prescription": overview["prescription"],
        "arrays_file": "optics.npz",
        "arrays_sha256": shared._file_sha(directory / "optics.npz"),
        "images": _picture(directory, modified_reference, fields, psf, reference_index),
        "diagnostics": {"radiometry": ray_summaries, "wavefront_probes": diagnostic},
        "elapsed_s": time.monotonic() - started,
        "limitations": [
            "Geometric ray PSF; diffraction excluded, pupil-sampling convergence not established",
            "Axisymmetric meridional rays rotated to declared fixed baseline object directions",
            "Local field-node PSF/RI over256x256 ROI; "
            "no full-camera image or within-ROI distortion warp",
            "Source-cell center approximation; "
            "geometric receiver binning applies pixel aperture once",
            "Separable receiver-side pupil integration, "
            "not joint landing-position/direction transport",
            "Patent effective apertures and nd/Vd dispersion are declared research hypotheses",
        ],
    }
    if (source_context or {}).get("reciprocal_profile"):
        result["direction_basis"] = "per_wavelength_adaptive_reciprocal_grid_zero_power_padding"
    if spec["schema"] == IRCF_SCHEMA:
        result["limitations"].extend(
            [
                "400–1000nm uses declared nd/Vd dispersion hypotheses, not measured NIR lens data",
                "IRCF is a zero-thickness unpolarized transmission mask in final air gap, "
                "parallel to sensor; no refraction, focus shift, coating reflection or ghosts",
                "Pupil-averaged transmission changes signal/color but does not reweight "
                "the separate geometric landing-position PSF",
            ]
        )
        result["spectral_integration"] = "trapezoid_400_1000_nm"
        result["direction_basis"] = "per_wavelength_reciprocal_sensor_plane_grid"
    if spec["optical_profile"] == reference_exit_pupil.PROFILE:
        result["limitations"] = [
            "Scalar reference-sphere angular spectrum on the fixed flat sensor; "
            "actual ray OPD and directions replace paraxial exit-pupil mapping",
            "Uniform incident entrance-pupil flux transported with scalar area Jacobian; "
            "no polarization, coating-amplitude or vector diffraction model",
            "Conditional normalized PSF; absolute irradiance comes once from reciprocal radiometry",
            "Same authored sensor plane and 550nm chief anchor at every wavelength; "
            "no spectral refocus or chromatic recentering",
            "Receiver pixel integration once; local field-node PSF over256x256 native ROI",
            "Separable pupil-averaged sensor angular QE, not joint coherent landing-angle response",
            "Phase-zero curve removes aberration on the same admitted pupil; "
            "not a separate ideal circular lens or product performance claim",
            "Patent effective-aperture and nd/Vd material hypotheses; "
            "physical validation not established",
        ]
        result["diagnostics"]["diffraction_nodes"] = wavefront_rows
    shared._write(directory / "result.json", result)
    return result


def load_solution(output_dir):
    directory = Path(output_dir)
    result = json.loads((directory / "result.json").read_text())
    path = directory / result["arrays_file"]
    if shared._file_sha(path) != result["arrays_sha256"]:
        raise ValueError("Optical solution array hash changed")
    with np.load(path, allow_pickle=False) as loaded:
        return {"result": result, "arrays": {k: loaded[k] for k in loaded.files}}


def spectral_widths(wavelength_nm):
    """Trapezoidal weights over a closed spectral interval, without extrapolation."""
    wave = np.asarray(wavelength_nm, float)
    if wave.ndim != 1 or len(wave) < 2 or not np.isfinite(wave).all() or np.any(np.diff(wave) <= 0):
        raise ValueError("Spectral integration requires a finite increasing axis")
    widths = np.empty_like(wave)
    widths[0], widths[-1] = (wave[1] - wave[0]) / 2, (wave[-1] - wave[-2]) / 2
    widths[1:-1] = (wave[2:] - wave[:-2]) / 2
    return widths


def pupil_samples(solution, field_id, wavelength_index):
    """Return admitted sensor-plane AOI, azimuth and original photon weights.

    These directions also describe incidence on a parallel zero-thickness filter
    in the final air gap. They are not object-space field angles.
    """
    result, arrays = solution["result"], solution["arrays"]
    field = next((f for f in result["fields"] if f["id"] == field_id), None)
    if field is None or field["status"] != "succeeded":
        raise ValueError("Pupil response requires a successful optical field")
    ri = (field["pupil_node_index"] if "pupil_node_index" in field
          else [0.0, 0.5, 0.85].index(field["normalized_radius"]))
    weights = arrays["conditional_weights"][wavelength_index, ri]
    admitted = weights > 0
    if not np.any(admitted):
        raise ValueError("Pupil response requires admitted positive photon weights")
    directions = (
        arrays["directions_xyz_per_wavelength"][wavelength_index, ri, admitted]
        if "directions_xyz_per_wavelength" in arrays
        else arrays["directions_xyz"][ri, admitted]
    )
    theta = np.degrees(np.arccos(np.clip(directions[:, 2], -1, 1)))
    phi = (np.degrees(np.arctan2(directions[:, 1], directions[:, 0])) + field["azimuth_deg"]) % 360
    return theta, phi, weights[admitted]


def _pupil_effective(solution, field_id, wi, wave, angular_response):
    theta, phi, weights = pupil_samples(solution, field_id, wi)
    response = np.asarray(angular_response(theta, phi, float(wave)))
    if (
        response.shape != (len(theta), 4)
        or not np.isfinite(response).all()
        or np.min(response) < 0
        or np.max(response) > 1
    ):
        raise ValueError("Angular response must be finite absolute four-site QE in0..1")
    rotated = np.asarray(angular_response(theta, (phi + 90) % 360, float(wave)))
    if not np.allclose(response, rotated, rtol=1e-10, atol=1e-12):
        raise ValueError("Reference bench currently requires azimuth-invariant pixel response")
    # Keep original incident-photon weights. Renormalizing after a filter mask
    # would erase its transmission loss.
    return np.sum(response * weights[:, None], axis=0)


def effective_pupil_response(solution, field_id, angular_response):
    """Absolute [wavelength,site] response using the exact apply_field weighting."""
    waves = np.asarray(solution["arrays"].get("wavelength_nm", WAVE), float)
    return np.asarray(
        [
            _pupil_effective(solution, field_id, wi, wave, angular_response)
            for wi, wave in enumerate(waves)
        ]
    )


def apply_field(
    solution,
    field_id,
    photons,
    angular_response=None,
    *,
    pixel_qe=None,
    return_spectral=False,
    spectral_groups=None,
):
    """Apply retained optical transport and absolute receiver response once.

    ``spectral_groups`` maps names to boolean wavelength masks and accumulates
    group planes without a second convolution or retaining a full spectral cube.
    ``return_spectral`` additionally returns [wavelength,row,col,site] planes.
    """
    result, arrays = solution["result"], solution["arrays"]
    extended = result.get("schema") == IRCF_SCHEMA
    wave_axis = np.asarray(arrays.get("wavelength_nm", WAVE), float)
    if extended:
        if wave_axis.tolist() not in [np.arange(400.0, 1001.0, s).tolist() for s in (10, 5)]:
            raise ValueError("IRCF response requires declared400–1000nm axis")
        widths = spectral_widths(wave_axis)
        if not np.array_equal(arrays.get("spectral_bin_width_nm"), widths):
            raise ValueError("IRCF spectral integration weights differ from the declared axis")
    else:
        if not np.array_equal(wave_axis, WAVE):
            raise ValueError("Legacy Reference optical response requires exact21-band axis")
        widths = np.full(len(WAVE), 10.0)
    fi = next((i for i, f in enumerate(result["fields"]) if f["id"] == field_id), None)
    if fi is None:
        raise ValueError("Unknown optical field")
    field = result["fields"][fi]
    if field["status"] != "succeeded":
        raise ValueError("Refused field cannot generate RAW: " + "; ".join(field["issues"]))
    g = field["geometry"]
    photons = np.asarray(photons, dtype=float)
    if (
        photons.shape != (*g["input_shape_row_col"], len(wave_axis))
        or not np.isfinite(photons).all()
        or (photons < 0).any()
    ):
        raise ValueError(
            f"Require finite nonnegative exact{len(wave_axis)}-band native radiance "
            "with declared halo"
        )
    if angular_response is None:
        qe = np.asarray(pixel_qe)
        if (
            qe.shape != (len(wave_axis), 4)
            or not np.isfinite(qe).all()
            or np.min(qe) < 0
            or np.max(qe) > 1
        ):
            raise ValueError("Require absolute four-site QE or angular_response")
    r0, r1, c0, c1 = g["crop_bounds_row_col"]
    planes = np.zeros((r1 - r0, c1 - c0, 4))
    center = np.zeros((len(wave_axis), 4))
    spectral = np.empty((len(wave_axis), *planes.shape)) if return_spectral else None
    if spectral_groups is not None and not isinstance(spectral_groups, dict):
        raise ValueError("Spectral groups require named boolean wavelength masks")
    masks, groups = {}, {}
    for name, mask in (spectral_groups or {}).items():
        mask = np.asarray(mask)
        if (
            not isinstance(name, str)
            or not name
            or mask.dtype != bool
            or mask.shape != wave_axis.shape
        ):
            raise ValueError("Spectral groups require named boolean wavelength masks")
        masks[name], groups[name] = mask, np.zeros_like(planes)
    ri = (field["pupil_node_index"] if "pupil_node_index" in field
          else [0.0, 0.5, 0.85].index(field["normalized_radius"]))
    for wi, wave in enumerate(wave_axis):
        if angular_response is not None:
            effective = _pupil_effective(solution, field_id, wi, wave, angular_response)
        else:
            effective = qe[wi]
        irradiance = np.maximum(
            fftconvolve(photons[..., wi], arrays["psf"][wi, fi], mode="same"), 0
        )[r0:r1, c0:c1]
        rate = (
            irradiance[..., None]
            * arrays["irradiance_per_radiance_sr"][wi, ri]
            * effective
            * widths[wi]
            * (2.9e-6) ** 2
        )
        planes += rate
        center[wi] = rate[planes.shape[0] // 2, planes.shape[1] // 2]
        if spectral is not None:
            spectral[wi] = rate
        for name, mask in masks.items():
            if mask[wi]:
                groups[name] += rate
    rr, cc = np.indices(planes.shape[:2])
    sites = np.array([[0, 1], [2, 3]])[rr % 2, cc % 2]
    raw = np.take_along_axis(planes, sites[..., None], axis=-1)[..., 0]
    output = {
        "mean_electron_rate": raw,
        "planes_electron_rate": planes,
        "center_electron_rate_per_band": center,
        "metadata": {
            "unit": "electrons/s",
            "optical_revision": result["revision"],
            "field_id": field_id,
            "geometry": g,
            "pixel_aperture_applications": 1,
            "angular_response": "absolute_QE_replaces_normal_QE",
            "diffraction_included": bool(field.get("diffraction_included", False)),
            "source_input_unit": "photons/s/m2/sr/nm",
            "spectral_bin_width_nm": widths.tolist() if extended else 10,
        },
    }
    if extended:
        output["metadata"].update(
            wavelength_nm=wave_axis.tolist(),
            spectral_integration="trapezoid_400_1000_nm",
            direction_basis="per_wavelength_reciprocal_sensor_plane_grid",
        )
    if spectral is not None:
        output["spectral_planes_electron_rate"] = spectral
    if spectral_groups is not None:
        output["group_planes_electron_rate"] = groups
        output["metadata"]["spectral_groups"] = {
            name: wave_axis[mask].tolist() for name, mask in masks.items()
        }
    return output
