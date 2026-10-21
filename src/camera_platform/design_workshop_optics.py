"""Editable, narrow-field prescription optics for the design workshop.

Actual RayOptics/Prysm characterizations remain subject to their existing gates.
This adapter does not convert the wide parametric lens into a prescription lens.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import time
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from . import lens_factory, optics_surfaces, prescription_psf, pupil_transmission

SCHEMA = "camera_design_workshop_optics_v1"
WAVE = np.arange(450.0, 651.0, 10.0)
REFERENCE_WAVE = 550.0
CONTROL_LIMITS = {
    "front_radius_mm": (10.0, 100.0),
    "rear_radius_mm": (-100.0, -10.0),
    "glass_thickness_mm": (0.5, 6.0),
    "stop_gap_mm": (0.05, 5.0),
    "stop_radius_mm": (0.2, 0.7),
    "sensor_shift_um": (-100.0, 100.0),
    "conic_constant": (-2.0, 1.0),
    "a4_mm3": (-0.01, 0.01),
}


def _sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temp.replace(path)


def default_specification(pixel_pitch_um=3.45):
    return {
        "schema": SCHEMA,
        "controls": {
            "front_radius_mm": 20.0,
            "rear_radius_mm": -20.0,
            "glass_thickness_mm": 3.0,
            "stop_gap_mm": 1.0,
            "stop_radius_mm": 0.5,
            "sensor_shift_um": 0.0,
            "conic_constant": -0.5,
            "a4_mm3": 8e-4,
        },
        "pixel_pitch_um": pixel_pitch_um,
        "native_shape_row_col": None,
        "field_x_mm": [-0.5, 0.0, 0.5],
        "field_y_mm": [-0.5, 0.0, 0.5],
        "wavelength_nm": WAVE.tolist(),
        "pupil_samples": 40,
        "kernel_size": 41,
        "subpixel_samples": 3,
    }


def normalize(specification):
    supplied = copy.deepcopy(specification)
    result = default_specification()
    if not isinstance(supplied, dict) or set(supplied) - set(result):
        raise ValueError("Unknown workshop optical specification fields")
    controls = {**result["controls"], **supplied.pop("controls", {})}
    result.update(supplied)
    result["controls"] = controls
    if result["schema"] != SCHEMA or set(controls) != set(CONTROL_LIMITS):
        raise ValueError("Unsupported workshop prescription controls")
    for key, (low, high) in CONTROL_LIMITS.items():
        value = controls[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must lie within [{low}, {high}]")
        controls[key] = float(value)
    if result["wavelength_nm"] != WAVE.tolist():
        raise ValueError("Workshop optics requires exact 450–650 nm in 10 nm steps")
    pitch = result["pixel_pitch_um"]
    if type(pitch) not in (int, float) or not 0.2 <= pitch <= 20:
        raise ValueError("pixel_pitch_um must be within 0.2..20")
    for axis in ("field_x_mm", "field_y_mm"):
        values = np.asarray(result[axis], dtype=float)
        if (
            values.shape != (3,)
            or not np.isfinite(values).all()
            or values[1] != 0
            or not 0 < values[2] <= 0.5
            or values[0] != -values[2]
        ):
            raise ValueError("Use three symmetric field nodes within ±0.5 mm")
        result[axis] = values.tolist()
    for key in ("pupil_samples", "kernel_size", "subpixel_samples"):
        if type(result[key]) is not int:
            raise ValueError(f"{key} requires an integer")
    if not 16 <= result["pupil_samples"] <= 48 or result["pupil_samples"] % 2:
        raise ValueError("Use even pupil_samples in 16..48")
    _request(result, [500.0, 550.0, 600.0], validate=True)
    geometry(result)
    return result


def _request(spec, waves, *, validate=False):
    request = prescription_psf.request_template(4)
    prescription = request["prescription"]
    control = spec["controls"]
    prescription["wavelength_nm"] = list(waves)
    prescription["sampling"].update(polar_rings=24, azimuth_samples=24)
    # These are authored dispersion samples, not inferred commercial glass data.
    glass = np.interp(waves, [450, 500, 550, 600, 650], [1.525, 1.521, 1.518, 1.516, 1.514])
    for surface in prescription["surfaces"]:
        surface["refractive_index_after"] = (
            glass.tolist() if surface["id"] == "synthetic-front" else [1.0] * len(waves)
        )
    stop, front, rear = prescription["surfaces"]
    stop.update(thickness_mm=control["stop_gap_mm"], semi_diameter_mm=control["stop_radius_mm"])
    front.update(radius_mm=control["front_radius_mm"], thickness_mm=control["glass_thickness_mm"])
    front["asphere"].update(
        conic_constant=control["conic_constant"], coefficients=[control["a4_mm3"], 0.0, 0.0]
    )
    rear.update(
        radius_mm=control["rear_radius_mm"],
        thickness_mm=18.79797133828165 + control["sensor_shift_um"] / 1000,
    )
    prescription["stop_transmission"] = pupil_transmission.uniform_transmission(prescription)
    request.update(
        {
            key: copy.deepcopy(spec[key])
            for key in (
                "field_x_mm",
                "field_y_mm",
                "pixel_pitch_um",
                "pupil_samples",
                "kernel_size",
                "subpixel_samples",
            )
        }
    )
    return prescription_psf.validate_request(request) if validate else request


def geometry(spec):
    pitch = float(spec["pixel_pitch_um"])
    halo = int(spec["kernel_size"]) // 2
    maximum = [int(2000 * spec[key][-1] / pitch) // 2 * 2 for key in ("field_y_mm", "field_x_mm")]
    requested = spec["native_shape_row_col"]
    if requested is None:
        cols = maximum[1] - 2 * halo
        rows = min(maximum[0] - 2 * halo, int(cols * 9 / 16) // 2 * 2)
    else:
        if (
            not isinstance(requested, list)
            or len(requested) != 2
            or any(type(v) is not int or v < 8 or v % 2 for v in requested)
        ):
            raise ValueError("native_shape_row_col requires positive even rows and columns")
        rows, cols = requested
    if min(rows, cols) < 8 or rows + 2 * halo > maximum[0] or cols + 2 * halo > maximum[1]:
        raise ValueError("Requested native crop plus optical halo leaves solved field support")
    focal = prescription_psf.request_template(4)["reference_focal_length_mm"]
    return {
        "native_shape_row_col": [rows, cols],
        "input_shape_row_col": [rows + 2 * halo, cols + 2 * halo],
        "crop_bounds_row_col": [halo, halo + rows, halo, halo + cols],
        "pixel_pitch_um": pitch,
        "horizontal_fov_deg": math.degrees(2 * math.atan(cols * pitch / 2000 / focal)),
        "reference_focal_length_mm": focal,
        "source_sampling": "native cell-centered radiance with explicit PSF halo",
        "receiver_sampling": "square pixel aperture already integrated into PSF",
        "not_120_degree_or_product_mode": True,
    }


def wavelength_batches(spec):
    """Symmetric odd-sized PSF batches retain precisely the same 550 nm reference."""
    maximum = min(15, prescription_psf.MAX_RAYS // (5 * spec["pupil_samples"] ** 2 * 9))
    pairs = (maximum - 1) // 2
    if pairs < 1:
        raise ValueError("Pupil samples leave no bounded symmetric spectral batch")
    low = WAVE[WAVE < REFERENCE_WAVE]
    return [
        sorted([REFERENCE_WAVE, *part.tolist(), *(1100 - part).tolist()])
        for part in (low[i : i + pairs] for i in range(0, len(low), pairs))
    ]


def _backend():
    modules = (lens_factory, prescription_psf, optics_surfaces, pupil_transmission)
    return {
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("rayoptics", "prysm", "numpy", "scipy")
        },
        "source_sha256": {Path(m.__file__).name: _file_sha(m.__file__) for m in modules},
        "adapter_sha256": _file_sha(__file__),
    }


def _batch(directory, label, request, backend, function, progress):
    identity = _sha({"request": request, "backend": backend, "kind": label})
    target = directory / "batches" / identity
    summary_path, arrays_path = target / "summary.json", target / "arrays.npz"
    if summary_path.exists():
        document = json.loads(summary_path.read_text())
        if document["arrays_sha256"] != _file_sha(arrays_path):
            raise ValueError("Optical batch cache arrays changed")
        with np.load(arrays_path, allow_pickle=False) as loaded:
            return document["summary"], {key: loaded[key] for key in loaded.files}
    waves = request.get("wavelength_nm", request.get("prescription", {}).get("wavelength_nm"))
    progress(f"{label}: {waves} nm")
    result = function(request)
    target.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(arrays_path, **result["arrays"])
    _write(
        summary_path,
        {
            "identity": identity,
            "arrays_sha256": _file_sha(arrays_path),
            "summary": result["summary"],
        },
    )
    return result["summary"], result["arrays"]


def _mtf(kernel, pitch_um, angle):
    yy, xx = np.indices(kernel.shape, dtype=float)
    xx -= (kernel.shape[1] - 1) / 2
    yy -= (kernel.shape[0] - 1) / 2
    frequency = np.linspace(0, 0.5, 65)
    curves = []
    for direction in (angle, angle + math.pi / 2):
        coordinate = xx * math.cos(direction) + yy * math.sin(direction)
        curves.append(
            np.abs(
                np.sum(
                    kernel[None] * np.exp(-2j * np.pi * frequency[:, None, None] * coordinate),
                    axis=(1, 2),
                )
            ).tolist()
        )
    return {
        "cycles_per_native_pixel": frequency.tolist(),
        "cycles_per_mm": (frequency * 1000 / pitch_um).tolist(),
        "radial": curves[0],
        "tangential": curves[1],
        "includes_receiver_pixel_aperture": True,
    }


def _pictures(directory, request, arrays):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    images = {}
    surfaces = request["prescription"]["surfaces"]
    positions = np.r_[0, np.cumsum([s["thickness_mm"] for s in surfaces])]
    fig, ax = plt.subplots(figsize=(9, 3))
    for position, surface in zip(positions, surfaces, strict=False):
        rr = np.linspace(-surface["semi_diameter_mm"], surface["semi_diameter_mm"], 101)
        zz = [position + optics_surfaces.sag(surface, abs(r)) for r in rr]
        ax.plot(zz, rr, label=surface["id"])
    ax.axvline(positions[-1], color="black", linestyle="--", label="fixed sensor")
    ax.set(
        xlabel="axial position (mm)",
        ylabel="height (mm)",
        title="Authored aspheric singlet — exact prescription",
    )
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(directory / "layout.png", dpi=140)
    plt.close(fig)
    images["layout"] = "layout.png"
    fig, axes = plt.subplots(3, 3, figsize=(7, 6))
    for yi in range(3):
        for xi in range(3):
            admitted = arrays["admitted"][10, yi, xi]
            points = arrays["ray_sensor_xy_mm"][10, yi, xi][admitted]
            center = arrays["chief_sensor_xy_mm"][10, yi, xi]
            offset = (points - center) * 1000
            axes[yi, xi].scatter(offset[:, 0], offset[:, 1], s=1, alpha=0.2)
            axes[yi, xi].set_title(
                f"({request['field_x_mm'][xi]}, {request['field_y_mm'][yi]}) mm",
                fontsize=8,
            )
            axes[yi, xi].set_aspect("equal", adjustable="datalim")
    fig.suptitle("550 nm traced sensor intercepts, relative to chief ray (µm)")
    fig.tight_layout()
    fig.savefig(directory / "rays.png", dpi=140)
    plt.close(fig)
    images["rays"] = "rays.png"
    for key, title, cmap in (
        ("psf", "550 nm pixel-integrated PSF (log display)", "magma"),
        ("opd_nm", "550 nm admitted pupil OPD (nm)", "coolwarm"),
    ):
        data = arrays[key][10, 1, 1]
        if key == "psf":
            data = np.log10(np.maximum(data, 1e-10))
        else:
            data = np.where(arrays["admitted"][10, 1, 1], data, np.nan)
        fig, ax = plt.subplots(figsize=(4, 3))
        graphic = ax.imshow(data, origin="upper", cmap=cmap)
        ax.set_title(title, fontsize=9)
        fig.colorbar(graphic, ax=ax)
        fig.tight_layout()
        fig.savefig(directory / (key + ".png"), dpi=140)
        plt.close(fig)
        images["opd" if key == "opd_nm" else key] = key + ".png"
    return images


def _chief_diagnostics(request):
    from rayoptics.raytr import trace
    from rayoptics.raytr.opticalspec import Field

    model = prescription_psf._model(request, 1)
    output = []
    for y in request["field_y_mm"]:
        for x in request["field_x_mm"]:
            radius = math.hypot(x, y)
            angle = math.degrees(math.atan2(radius, request["reference_focal_length_mm"]))
            field = Field(y=angle, fov=model.optical_spec["fov"])
            _, chief = trace.setup_pupil_coords(
                model, field, REFERENCE_WAVE, 0.0, image_pt=[0, radius]
            )
            direction = np.asarray(chief[0].ray[-1][1])
            output.append(
                {
                    "field_y_mm": y,
                    "field_x_mm": x,
                    "object_angle_deg": angle,
                    "chief_ray_angle_deg": math.degrees(math.acos(np.clip(direction[2], -1, 1))),
                    "wavelength_nm": REFERENCE_WAVE,
                }
            )
    return output


def solve(specification, output_dir, *, on_progress=None):
    spec = normalize(specification)
    progress = on_progress or (lambda message: None)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    backend = _backend()
    revision = _sha({"specification": spec, "backend": backend})
    completed = directory / "result.json"
    if completed.exists():
        solution = load_solution(directory)
        if solution["result"]["revision"] != revision:
            raise ValueError(
                "Use a new optical revision directory after prescription/backend changes"
            )
        return solution["result"]
    started = time.monotonic()
    summaries, rows, ray_rows = [], {}, {}
    for waves in wavelength_batches(spec):
        request = _request(spec, waves, validate=True)
        summary, arrays = _batch(
            directory, "OPD/PSF", request, backend, prescription_psf.characterize, progress
        )
        summaries.append(summary)
        for i, wave in enumerate(waves):
            current = {
                key: arrays[name][i]
                for key, name in (
                    ("psf", "psf_kernel"),
                    ("opd_nm", "fine_opd_nm"),
                    ("admitted", "fine_admitted"),
                    ("ray_sensor_xy_mm", "fine_ray_sensor_xy_mm"),
                    ("chief_sensor_xy_mm", "fine_chief_sensor_xy_mm"),
                )
            }
            current["effective_focal_length_mm"] = summary[
                "effective_focal_length_mm_by_wavelength"
            ][i]
            if wave in rows and not np.allclose(
                rows[wave]["psf"], current["psf"], rtol=0, atol=1e-12
            ):
                raise ValueError("Shared reference wavelength changed between spectral batches")
            rows[wave] = current
    # Reciprocal radiometry has an independent, smaller ray budget. No wavelength
    # reference enters this calculation; all batches use the identical geometry.
    ray_wave = WAVE.tolist()
    ray_batches = [ray_wave[i : i + 2] for i in range(0, len(ray_wave), 2)]
    if len(ray_batches[-1]) == 1:
        ray_batches[-1] = sorted([REFERENCE_WAVE, *ray_batches[-1]])
    for waves in ray_batches:
        request = _request(spec, waves)["prescription"]
        request["sampling"].update(polar_rings=44, azimuth_samples=44)
        summary, arrays = _batch(
            directory, "RI/directions", request, backend, lens_factory.characterize, progress
        )
        summaries.append(summary)
        for i, wave in enumerate(waves):
            ray_rows[wave] = {
                "irradiance_per_radiance_sr": arrays["irradiance_per_radiance_sr"][i],
                "angular_weights": arrays["angular_weights"][i],
                "conditional_weights": arrays.get(
                    "directional_conditional_weights",
                    np.zeros_like(arrays["accepted_rays"], dtype=float),
                )[i],
            }
        ray_axes = {
            "directions_xyz": arrays["image_direction_cosines"],
            "theta_deg": arrays["theta_deg"],
            "image_height_mm": np.asarray(request["image_height_mm"]),
            "on_axis_rotation_phi_deg": arrays.get(
                "on_axis_rotation_phi_deg", np.arange(32) * 360 / 32
            ),
        }
    combined = {key: np.asarray([rows[wave][key] for wave in WAVE]) for key in rows[450.0]}
    combined.update(
        {key: np.asarray([ray_rows[wave][key] for wave in WAVE]) for key in ray_rows[450.0]}
    )
    combined.update(ray_axes)
    combined["center_solid_angle_sr"] = combined["irradiance_per_radiance_sr"][:, 0]
    combined.update(
        wavelength_nm=WAVE,
        field_x_mm=np.asarray(spec["field_x_mm"]),
        field_y_mm=np.asarray(spec["field_y_mm"]),
    )
    np.savez_compressed(directory / "optics.npz", **combined)
    diagnostic = []
    for yi, y in enumerate(spec["field_y_mm"]):
        for xi, x in enumerate(spec["field_x_mm"]):
            diagnostic.append(
                {
                    "field_y_mm": y,
                    "field_x_mm": x,
                    "wavelength_nm": REFERENCE_WAVE,
                    **_mtf(combined["psf"][10, yi, xi], spec["pixel_pitch_um"], math.atan2(y, x)),
                }
            )
    # A single Factory prescription is limited to sixteen bands. This compact
    # overview remains a valid three-band prescription; the solved union and
    # every exact batch request are preserved separately rather than forging a
    # nonconforming 21-band Factory request.
    full_request = _request(spec, [450.0, 550.0, 650.0])
    result = {
        "schema": SCHEMA,
        "revision": revision,
        "specification": spec,
        "backend": backend,
        "status": "PROPOSED" if all(s["status"] == "PROPOSED" for s in summaries) else "REFUSED",
        "geometry": geometry(spec),
        "prescription": full_request["prescription"],
        "prescription_overview_wavelength_nm": [450.0, 550.0, 650.0],
        "solved_wavelength_nm": WAVE.tolist(),
        "reference_wavelength_nm": REFERENCE_WAVE,
        "spectral_batches_nm": wavelength_batches(spec),
        "arrays_file": "optics.npz",
        "arrays_sha256": _file_sha(directory / "optics.npz"),
        "images": _pictures(directory, full_request, combined),
        "diagnostics": {
            "mtf": diagnostic,
            "chief_rays": _chief_diagnostics(full_request),
            "batch_summaries": [
                {k: s[k] for k in s if k not in {"normalized_request", "geometry"}}
                for s in summaries
            ],
        },
        "elapsed_s": time.monotonic() - started,
        "limitations": [
            "Authored synthetic glass: explicitly sampled piecewise-linear dispersion; "
            "not a vendor lens",
            "Narrow-field scalar paraxial PSF; existing convergence/domain gates are unchanged",
            "Pixel aperture already included; no second receiving-area box filter is permitted",
            "Source-center PSF and receiver radiometry/QE are separable; "
            "joint landing-position/angle transport is not solved",
            "Native source-cell center approximation; no inferred high-frequency input detail",
            "Ideal uncoated interfaces and authored unity stop power; "
            "no measured transmission or physical validation",
        ],
    }
    _write(completed, result)
    return result


def load_solution(output_dir):
    directory = Path(output_dir)
    result = json.loads((directory / "result.json").read_text())
    path = directory / result["arrays_file"]
    if _file_sha(path) != result["arrays_sha256"]:
        raise ValueError("Optical solution array hash changed")
    with np.load(path, allow_pickle=False) as loaded:
        arrays = {key: loaded[key] for key in loaded.files}
    return {"result": result, "arrays": arrays}


def _weights(axis, coordinates):
    upper = np.clip(np.searchsorted(axis, coordinates, side="right"), 1, len(axis) - 1)
    fraction = (coordinates - axis[upper - 1]) / (axis[upper] - axis[upper - 1])
    result = np.zeros((len(axis), len(coordinates)))
    result[upper - 1, np.arange(len(coordinates))] = 1 - fraction
    result[upper, np.arange(len(coordinates))] = fraction
    return result


def apply_spectral_radiance(
    solution, photons, *, pixel_qe, wavelength_nm=None, angular_response=None
):
    """Object radiance photons/s/m²/sr/nm to receiver e-/s, with one aperture.

    angular_response, when supplied, returns ABSOLUTE QE (...,4) and replaces
    pixel_qe. Its arguments are theta_deg, azimuth_deg, wavelength_nm. The first
    implementation supports azimuth-invariant pixel structure responses.
    """
    result, arrays = solution["result"], solution["arrays"]
    if result["status"] != "PROPOSED":
        raise ValueError("Refused optical characterizations cannot generate camera evidence")
    geom = result["geometry"]
    photons = np.asarray(photons, dtype=float)
    if (
        photons.shape != (*geom["input_shape_row_col"], len(WAVE))
        or not np.isfinite(photons).all()
        or np.min(photons) < 0
    ):
        raise ValueError(
            "Finite nonnegative native spectral radiance with the declared halo is required"
        )
    if wavelength_nm is not None and not np.array_equal(wavelength_nm, WAVE):
        raise ValueError("Spectral input must use exact solved wavelength coordinates")
    qe = np.asarray(pixel_qe, dtype=float)
    if qe.shape != (len(WAVE), 4) or not np.isfinite(qe).all() or np.min(qe) < 0 or np.max(qe) > 1:
        raise ValueError("pixel_qe requires absolute [wavelength,R,Gr,Gb,B] efficiencies in0..1")
    h, w = photons.shape[:2]
    pitch = geom["pixel_pitch_um"] / 1000
    yy = (np.arange(h) - (h - 1) / 2) * pitch
    xx = (np.arange(w) - (w - 1) / 2) * pitch
    wy, wx = _weights(arrays["field_y_mm"], yy), _weights(arrays["field_x_mm"], xx)
    radius = np.hypot(yy[:, None], xx[None])
    bounds = geom["crop_bounds_row_col"]
    r0, r1, c0, c1 = bounds
    spectral = np.empty((r1 - r0, c1 - c0, len(WAVE)))
    planes = np.zeros((*spectral.shape[:2], 4))
    center_effective_qe = np.empty((len(WAVE), 4))
    center_electron_rate_per_band = np.empty((len(WAVE), 4))
    for wi, wave in enumerate(WAVE):
        blurred = np.zeros((h, w))
        for yi in range(3):
            for xi in range(3):
                weighted = photons[..., wi] * wy[yi, :, None] * wx[xi, None, :]
                blurred += fftconvolve(weighted, arrays["psf"][wi, yi, xi], mode="same")
        blurred = np.maximum(blurred, 0)
        radiometry = np.interp(
            radius, arrays["image_height_mm"], arrays["irradiance_per_radiance_sr"][wi]
        )
        irradiance = blurred * radiometry
        spectral[..., wi] = irradiance[r0:r1, c0:c1]
        if angular_response is None:
            effective = np.broadcast_to(qe[wi], (*radius.shape, 4))
        else:
            # Current pixel structure model is rotationally invariant; evaluate
            # actual admitted directions, retaining exact power/angle pairing.
            nodes = []
            for fi, directions in enumerate(arrays["directions_xyz"]):
                weight = arrays["conditional_weights"][wi, fi]
                admitted = weight > 0
                direction = directions[admitted]
                theta = np.degrees(np.arccos(np.clip(direction[:, 2], -1, 1)))
                phi = np.degrees(np.arctan2(direction[:, 1], direction[:, 0])) % 360
                response = np.asarray(angular_response(theta, phi, float(wave)))
                if (
                    response.shape != (len(theta), 4)
                    or not np.isfinite(response).all()
                    or np.min(response) < 0
                    or np.max(response) > 1
                ):
                    raise ValueError("Angular callback must return absolute finite four-site QE")
                rotated = np.asarray(angular_response(theta, (phi + 90) % 360, float(wave)))
                if not np.allclose(response, rotated, rtol=1e-10, atol=1e-12):
                    raise ValueError("Workshop adapter requires azimuth-invariant pixel QE")
                nodes.append(np.sum(response * weight[admitted, None], axis=0))
            nodes = np.asarray(nodes)
            effective = np.stack(
                [np.interp(radius, arrays["image_height_mm"], nodes[:, site]) for site in range(4)],
                axis=-1,
            )
        band_rate = (
            spectral[..., wi, None]
            * effective[r0:r1, c0:c1]
            * 10
            * (geom["pixel_pitch_um"] * 1e-6) ** 2
        )
        planes += band_rate
        cy, cx = planes.shape[0] // 2, planes.shape[1] // 2
        center_effective_qe[wi] = effective[r0 + cy, c0 + cx]
        center_electron_rate_per_band[wi] = band_rate[cy, cx]
    # Crop halo parity is explicit: RGGB is defined at the sensor output origin.
    sites = np.array([[0, 1], [2, 3]])
    rr, cc = np.indices(planes.shape[:2])
    mosaic = np.take_along_axis(planes, sites[rr % 2, cc % 2][..., None], axis=-1)[..., 0]
    return {
        "mean_electron_rate": mosaic,
        "planes_electron_rate": planes,
        "spectral_irradiance": spectral,
        "center_effective_qe": center_effective_qe,
        "center_electron_rate_per_band": center_electron_rate_per_band,
        "metadata": {
            "unit": "electrons/s",
            "optical_revision": result["revision"],
            "geometry": geom,
            "pixel_aperture_applications": 1,
            "angular_response": "absolute_QE_replaces_normal_QE"
            if angular_response
            else "normal_QE",
            "angular_scope": (
                "rotationally_invariant_pixel_response; receiver-side separable pupil power average"
            ),
            "spectral_bin_width_nm": 10,
            "source_input_unit": "photons/s/m2/sr/nm",
        },
    }


def through_focus(specification, offsets_um, output_dir, *, on_progress=None):
    spec = normalize(specification)
    if not isinstance(offsets_um, (list, tuple)) or not 1 <= len(offsets_um) <= 9:
        raise ValueError("Choose one to nine explicit sensor-position offsets")
    results = []
    for offset in offsets_um:
        child = copy.deepcopy(spec)
        child["controls"]["sensor_shift_um"] += float(offset)
        solved = solve(
            child, Path(output_dir) / ("focus-" + _sha(child)[:16]), on_progress=on_progress
        )
        results.append(
            {
                "offset_um": float(offset),
                "revision": solved["revision"],
                "status": solved["status"],
                "mtf": solved["diagnostics"]["mtf"],
            }
        )
    document = {
        "schema": "camera_design_workshop_through_focus_v1",
        "reference_wavelength_nm": REFERENCE_WAVE,
        "points": results,
        "sensor_plane_actually_retraced": True,
    }
    _write(Path(output_dir) / "through-focus.json", document)
    return document
