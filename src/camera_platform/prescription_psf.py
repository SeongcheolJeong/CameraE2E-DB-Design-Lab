"""Prescription OPD and bounded, paraxial, uniform-exit-pupil field PSFs.

The ray trace determines OPD and clipping. Amplitude transport and diffraction
remain an explicitly declared approximation, not a physical-camera validation.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import time

import numpy as np

from . import finite_conjugate, lens_factory, optics_surfaces, pupil_transmission

FACTORY_ID = "rayoptics_prescription_field_psf_v1"
ASPHERE_FACTORY_ID = "rayoptics_prescription_even_asphere_field_psf_v4"
FINITE_FACTORY_ID = "rayoptics_prescription_finite_planar_field_psf_v5"
MAX_RAYS = 400_000
# A reviewed 28-surface, 54x108 pupil trace can take almost 100 seconds on an
# otherwise idle host. Keep a bounded research budget with margin for normal
# worker contention; numerical convergence and pupil gates are unchanged.
MAX_SECONDS = 180


def request_template(version=1):
    """Authored narrow-field fixture shared by the radiometry and OPD factories.

    Clear apertures enclose this fixture's transmitted bundles; they are physical
    inputs, not a numerical aperture crop applied after tracing. Neither factory's
    convergence or approximation-domain tolerance is relaxed for the example.
    """
    prescription = lens_factory.request_template(version=version)
    prescription["wavelength_nm"] = [500, 600]
    for surface in prescription["surfaces"]:
        surface["refractive_index_after"] = [
            surface["refractive_index_after"][1],
            surface["refractive_index_after"][3],
        ]
    prescription["surfaces"][0]["semi_diameter_mm"] = 0.5
    for surface in prescription["surfaces"][1:]:
        surface["semi_diameter_mm"] = 0.8
    prescription["surfaces"][-1]["thickness_mm"] = 18.79797133828165
    prescription["image_height_mm"] = [0.0, 0.375, 0.75]
    prescription["sampling"].update(polar_rings=44, azimuth_samples=44)
    if version == 4:
        # An authored nonzero departure on this fixture's narrower aperture;
        # this changes the physical prescription, never the acceptance gates.
        front = next(s for s in prescription["surfaces"] if s["profile"] == "even_asphere")
        front["asphere"]["coefficients"] = [8e-4, 0.0, 0.0]
    if version >= 2:
        prescription["stop_transmission"] = pupil_transmission.uniform_transmission(prescription)
    if version == 5:
        # Authored 500 mm conjugate, near the two sampled colours' paraxial
        # image distances. These are fixture inputs, never an autofocus step.
        prescription["surfaces"][-1]["thickness_mm"] = 19.592456537208645
        prescription["object_conjugate"].update(
            distance_mm=500.0, reference_magnification=-0.04104715527949275
        )
    return {
        "schema": f"prescription_field_psf_v{version}",
        "prescription": prescription,
        "object_conjugate": "finite_planar_object" if version == 5 else "infinity_plane_wave",
        "reference_focal_length_mm": 19.812067271756955,
        "field_x_mm": [-0.5, 0.0, 0.5],
        "field_y_mm": [-0.5, 0.0, 0.5],
        "pixel_pitch_um": 3.0,
        "pupil_samples": 32,
        "kernel_size": 41,
        "subpixel_samples": 3,
        "max_kernel_l1_change": 0.03,
        "minimum_retained_energy": 0.95,
        "maximum_exit_pupil_mapping_fraction": 0.03,
        "maximum_ray_cone_deg": 8.0,
    }


def validate_request(request):
    if not isinstance(request, dict) or set(request) != set(request_template()):
        raise ValueError("Declare the exact prescription_field_psf_v1, v2, v3, v4 or v5 fields")
    data = copy.deepcopy(request)
    finite = data["schema"] == "prescription_field_psf_v5"
    if (
        data["schema"] not in ("prescription_field_psf_v1", "prescription_field_psf_v2",
                               "prescription_field_psf_v3", "prescription_field_psf_v4",
                               "prescription_field_psf_v5")
        or data["object_conjugate"] != (
            "finite_planar_object" if finite else "infinity_plane_wave"
        )
    ):
        raise ValueError("Declare infinity_plane_wave for v1–v4 or finite_planar_object for v5")
    data["prescription"] = lens_factory.validate_wavefront_prescription(data["prescription"])
    if data["prescription"]["schema"] != "lens_prescription_v" + data["schema"][-1]:
        raise ValueError("PSF and embedded prescription request versions must match")
    for key in ("field_x_mm", "field_y_mm"):
        axis = np.asarray(data[key])
        if (
            not isinstance(data[key], list)
            or any(type(v) not in (int, float) for v in data[key])
            or axis.ndim != 1
            or axis.dtype.kind not in "iuf"
            or not 2 <= len(axis) <= 9
            or not np.isfinite(axis).all()
            or not np.all(np.diff(axis) > 0)
            or axis[0] > 0
            or axis[-1] < 0
            or np.max(np.abs(axis)) > 20
        ):
            raise ValueError(
                f"{key} requires 2..9 finite increasing sensor-centered mm nodes enclosing zero"
            )
    for key, low, high in (
        ("reference_focal_length_mm", 0.5, 500),
        ("pixel_pitch_um", 0.2, 20),
        ("max_kernel_l1_change", 1e-6, 0.03),
        ("minimum_retained_energy", 0.95, 0.9999),
        ("maximum_exit_pupil_mapping_fraction", 1e-6, 0.05),
        ("maximum_ray_cone_deg", 0.1, 10),
    ):
        if type(data[key]) not in (int, float) or not low <= data[key] <= high:
            raise ValueError(f"{key} must be finite and in [{low},{high}]")
    for key, low, high in (
        ("pupil_samples", 16, 128),
        ("kernel_size", 3, 65),
        ("subpixel_samples", 3, 9),
    ):
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise ValueError(f"{key} must be an integer in [{low},{high}]")
    if (
        data["pupil_samples"] % 2
        or data["kernel_size"] % 2 != 1
        or data["subpixel_samples"] % 2 != 1
    ):
        raise ValueError("Use even pupil samples and odd kernel/subpixel samples")
    fields = len(data["field_y_mm"]) * len(data["field_x_mm"])
    if fields > 25:
        raise ValueError("Field PSF supports at most 25 field nodes")
    waves = len(data["prescription"]["wavelength_nm"])
    if 5 * data["pupil_samples"] ** 2 * fields * waves > MAX_RAYS:
        raise ValueError("Prescription wavefront exceeds the bounded ray budget")
    if (
        4
        * waves
        * fields
        * (data["kernel_size"] * (2 * data["subpixel_samples"] + 1)) ** 2
        * (2 * data["pupil_samples"])
        > 2_000_000_000
    ):
        raise ValueError("Prescription PSF exceeds the bounded DFT budget")
    return data


def _model(data, wavelength_index):
    """One exact monochromatic model avoids interpolation and stale spectral aiming."""
    from opticalglass.opticalmedium import ConstantIndex
    from rayoptics.optical.opticalmodel import OpticalModel
    from rayoptics.raytr.opticalspec import FieldSpec, PupilSpec, WvlSpec

    prescription = data["prescription"]
    wavelength = prescription["wavelength_nm"][wavelength_index]
    model = OpticalModel()
    model.radius_mode = True
    seq, osp = model.seq_model, model.optical_spec
    finite = data["schema"] == "prescription_field_psf_v5"
    # The finite object is a physical plane before the first authored vertex.
    seq.gaps[0].thi = prescription["object_conjugate"]["distance_mm"] if finite else 1e10
    seq.do_apertures = False  # Never let update_model resize authored clear apertures.
    stop = next(s for s in prescription["surfaces"] if s["is_stop"])
    osp["pupil"] = PupilSpec(osp, key=["object", "epd"], value=2 * stop["semi_diameter_mm"])
    osp["fov"] = FieldSpec(
        osp, key=["object", "height" if finite else "angle"],
        value=1, flds=[0.0, 1.0], is_relative=False
    )
    osp["wvls"] = WvlSpec([(wavelength, 1.0)], ref_wl=0)
    for surface in prescription["surfaces"]:
        seq.add_surface(
            [
                surface["radius_mm"] or 0.0,
                surface["thickness_mm"],
                ConstantIndex(surface["refractive_index_after"][wavelength_index], surface["id"]),
            ],
            sd=surface["semi_diameter_mm"],
        )
        if data["schema"] in ("prescription_field_psf_v4", "prescription_field_psf_v5"):
            # add_surface inserts before the image plane. Both forward OPD and
            # reciprocal radiometry use this same finite physical surface.
            seq.ifcs[-2].profile = optics_surfaces.profile(surface)
        if surface["is_stop"]:
            seq.set_stop()
    model.update_model()
    axial = model["ar"]["parax_data"].ax_ray
    stop_height = abs(axial[seq.stop_surface][0])
    if not np.isfinite(stop_height) or stop_height <= 1e-12:
        raise ValueError("Declared stop has no finite paraxial entrance-pupil mapping")
    osp["pupil"].value *= stop["semi_diameter_mm"] / stop_height
    model.update_model()
    expected = [surface["semi_diameter_mm"] for surface in prescription["surfaces"]]
    if [surface.max_aperture for surface in seq.ifcs[1:-1]] != expected:
        raise ValueError("Backend changed the authored clear apertures")
    return model


def _finite_pupil_trace(model, field, wavelength, normalized_pupil):
    """Trace from the physical finite point to an explicit entrance-pupil aim.

    RayOptics 0.9.8's relative-pupil launch reconstructs a finite object-height
    point with the opposite sign. Its explicit ``aim pt`` path uses obj_coords
    directly, as does chief-ray aiming. Do not route these rays through the
    backend's relative-pupil chief helper.
    """
    from rayoptics.raytr import trace

    aim = field.aim_info + model.optical_spec["pupil"].value * 0.5 * normalized_pupil
    return trace.trace_safe(
        model, aim, field, wavelength, None, "summary", pupil_type="aim pt",
        apply_vignetting=False, check_apertures=True, pt_inside_fuzz=0.0,
        use_named_tuples=True,
    )


def _finite_reference(model, wavelength, object_y_mm, sensor_y_mm):
    from rayoptics.raytr import trace, waveabr
    from rayoptics.raytr.opticalspec import Field

    field = Field(y=object_y_mm, fov=model.optical_spec["fov"])
    field.aim_info = trace.aim_chief_ray(model, field, wavelength)
    result = _finite_pupil_trace(model, field, wavelength, np.zeros(2))
    if result.err is not None or result.pkg is None:
        raise ValueError("No complete finite-object chief ray through the declared stop")
    chief_ray = result.pkg
    paraxial = model["ar"]["parax_data"]
    # 0.9.8's fod.exp_dist combines the actual image-plane ray height with
    # the paraxial conjugate distance. Under defocus it moves the axial pupil.
    # Intersect the paraxial stop-center ray from the last physical surface;
    # this physical exit-pupil location cannot depend on the sensor gap.
    pupil_height, pupil_slope = paraxial.pr_ray[-2][:2]
    if not np.isfinite([pupil_height, pupil_slope]).all() or abs(pupil_slope) < 1e-14:
        raise ValueError("Finite scalar PSF requires a finite physical exit pupil")
    exit_distance = -pupil_height / pupil_slope
    exit_segment = waveabr.transfer_to_exit_pupil(
        model.seq_model.ifcs[-2], (chief_ray.ray[-2][0], chief_ray.ray[-2][1]), exit_distance
    )
    chief = chief_ray, exit_segment
    reference = waveabr.calculate_reference_sphere(
        model, field, wavelength, 0.0, chief, image_pt_2d=[0.0, sensor_y_mm]
    )
    return field, reference, chief


def _validate_physical_ray(ray, model, surfaces, label):
    """Check the complete physical path before it becomes OPD evidence.

    A profile solver's signed local root is not the distance between surfaces:
    RayOptics can translate the ray origin before intersection. The completed
    ray must nevertheless have finite geometry and nonnegative physical legs.
    """
    if ray is None or len(ray) != len(model.seq_model.ifcs):
        raise ValueError(f"Incomplete {label} prescription ray trace")
    for segment in ray:
        if len(segment) != 4:
            raise ValueError(f"Malformed {label} prescription ray segment")
        point, direction, distance, normal = segment
        if (
            any(np.shape(v) != (3,) for v in (point, direction, normal))
            or np.shape(distance) != ()
            or not np.isfinite(np.r_[point, direction, distance, normal]).all()
            or distance < -1e-8
            or abs(float(np.linalg.norm(direction)) - 1.0) > 1e-10
            or np.linalg.norm(normal) == 0
        ):
            raise ValueError(f"Nonfinite, malformed or backwards {label} prescription ray")
    for index, surface in enumerate(surfaces, start=1):
        optics_surfaces.validate_intersection(surface, ray[index][0])


def _wavefront(data, model, wavelength, x_mm, y_mm, samples, deadline):
    from rayoptics.raytr import trace, waveabr
    from rayoptics.raytr.opticalspec import Field

    radial = math.hypot(x_mm, y_mm)
    # RayOptics' exit-pupil routine assumes the meridional YZ plane.
    # Trace there, rotating *both* pupil and physical geometry explicitly.
    er = np.array([0.0, 1.0]) if radial == 0 else np.array([x_mm, y_mm]) / radial
    et = np.array([er[1], -er[0]])
    rotation = np.column_stack((et, er))
    finite = data["schema"] == "prescription_field_psf_v5"
    if finite:
        object_xy = finite_conjugate.object_xy_mm(
            np.array([x_mm, y_mm]), data["prescription"]["object_conjugate"]
        )
        object_y = radial / data["prescription"]["object_conjugate"]["reference_magnification"]
        fld, reference, chief = _finite_reference(model, wavelength, object_y, radial)
        expected_origin = np.array([0.0, object_y, 0.0])
        origin_error = float(np.linalg.norm(chief[0].ray[0][0] - expected_origin))
        if origin_error > 1e-9:
            raise ValueError("Finite chief ray did not start at the declared physical object point")
    else:
        angle = math.degrees(math.atan2(radial, data["reference_focal_length_mm"]))
        fld = Field(y=angle, fov=model.optical_spec["fov"])
        reference, chief = trace.setup_pupil_coords(
            model, fld, wavelength, 0.0, image_pt=[0.0, radial]
        )
    physical_surfaces = (
        data["prescription"]["surfaces"]
        if data["schema"] in ("prescription_field_psf_v4", "prescription_field_psf_v5")
        else ()
    )
    if physical_surfaces:
        _validate_physical_ray(chief[0].ray, model, physical_surfaces, "chief")
    stop_point = chief[0].ray[model.seq_model.stop_surface][0]
    if np.linalg.norm(stop_point[:2]) > 1e-7:
        raise ValueError("Chief ray aiming did not reach the declared stop center")
    fod = model["ar"]["parax_data"].fod
    radius = float(fod.exp_radius)
    distance = float(reference[2])
    if not np.isfinite([radius, distance]).all() or radius <= 0 or distance <= 0:
        raise ValueError("No positive finite exit pupil/reference sphere for scalar propagation")
    axis = (np.arange(samples) - samples // 2) * 2.4 / samples
    xx, yy = np.meshgrid(axis, axis)
    opd = np.zeros((samples, samples))
    admitted = np.zeros((samples, samples), dtype=bool)
    ray_sensor = np.zeros((samples, samples, 2))
    exit_xy = np.zeros((samples, samples, 2))
    transmission = data["prescription"].get("stop_transmission")
    if transmission is not None:
        stop = next(s for s in data["prescription"]["surfaces"] if s["is_stop"])
        stop_xy = np.zeros((samples, samples, 2))
        power_transmittance = np.zeros((samples, samples))
    departure = 0.0
    cone = 0.0
    failures = {"TraceRayBlockedError": 0, "TraceMissedSurfaceError": 0, "TraceTIRError": 0}
    for iy in range(samples):
        if time.monotonic() > deadline:
            raise ValueError("Prescription wavefront exceeded its compute deadline")
        for ix in range(samples):
            local_pupil = rotation.T @ np.array([xx[iy, ix], yy[iy, ix]])
            if finite:
                result = _finite_pupil_trace(model, fld, wavelength, local_pupil)
            else:
                result = trace.trace_safe(
                    model,
                    local_pupil,
                    fld,
                    wavelength,
                    None,
                    "summary",
                    apply_vignetting=False,
                    check_apertures=True,
                    pt_inside_fuzz=0.0,
                )
            if result.err is not None:
                category = type(result.err).__name__
                if category not in failures:
                    raise ValueError(f"Unexpected ray failure: {category}")
                failures[category] += 1
                continue
            if result.pkg is None or len(result.pkg[0]) != len(model.seq_model.ifcs):
                raise ValueError("Incomplete prescription ray trace")
            ray = result.pkg[0]
            if finite:
                error = float(np.linalg.norm(ray[0][0] - expected_origin))
                origin_error = max(origin_error, error)
                if error > 1e-9:
                    raise ValueError("Finite pupil ray did not start at the declared object point")
            if any(
                not np.isfinite(np.r_[segment[0], segment[1], segment[2]]).all()
                or segment[2] < -1e-8
                for segment in ray
            ):
                raise ValueError("Nonfinite or backwards prescription ray")
            if physical_surfaces:
                _validate_physical_ray(ray, model, physical_surfaces, "sampled")
            value = waveabr.wave_abr_full_calc(
                fod, fld, wavelength, 0.0, result.pkg, chief, reference
            )
            pt, direction = ray[-2][:2]
            projected = pt + (chief[1][0][2] - pt[2]) / direction[2] * direction - chief[1][0]
            error = np.linalg.norm(projected[:2] - radius * local_pupil) / radius
            departure = max(departure, float(error))
            cone = max(
                cone, math.degrees(math.acos(float(np.clip(direction @ reference[1], -1, 1))))
            )
            if not np.isfinite(value):
                raise ValueError("Nonfinite prescription OPD")
            opd[iy, ix] = value * 1e6  # mm -> nm; never central-wavelength waves.
            admitted[iy, ix] = True
            ray_sensor[iy, ix] = rotation @ ray[-1][0][:2]
            exit_xy[iy, ix] = rotation @ projected[:2]
            if transmission is not None:
                hit = ray[model.seq_model.stop_surface][0]
                power_transmittance[iy, ix] = pupil_transmission.power_at_stop(
                    transmission,
                    wavelength,
                    hit,
                    stop["semi_diameter_mm"],
                )
                stop_xy[iy, ix] = rotation @ hit[:2]
    if not admitted.any():
        raise ValueError("No admitted pupil rays at a requested field/wavelength")
    border = bool(
        admitted[0].any() or admitted[-1].any() or admitted[:, 0].any() or admitted[:, -1].any()
    )
    reference_angle = math.degrees(math.acos(float(np.clip(reference[1][2], -1, 1))))
    phase_step = 0.0
    for dimension in (0, 1):
        valid_neighbors = np.take(admitted, range(samples - 1), axis=dimension) & np.take(
            admitted, range(1, samples), axis=dimension
        )
        jumps = np.abs(np.diff(opd, axis=dimension))[valid_neighbors] / wavelength
        phase_step = max(phase_step, float(jumps.max()) if jumps.size else 0.0)
    result = {
        "opd_nm": opd,
        "admitted": admitted,
        "ray_sensor_xy_mm": ray_sensor,
        "exit_pupil_xy_mm": exit_xy,
        "exit_pupil_radius_mm": radius,
        "reference_sphere_radius_mm": distance,
        "chief_sensor_xy_mm": rotation @ chief[0].ray[-1][0][:2],
        "reference_image_xy_mm": np.array([x_mm, y_mm]),
        "exit_pupil_mapping_fraction": departure,
        "ray_cone_deg": cone,
        "reference_axis_deg": reference_angle,
        "pupil_boundary_truncated": border,
        "maximum_neighbor_opd_waves": phase_step,
        "propagation_distance_over_exit_pupil_diameter": distance / (2 * radius),
        "failure_counts": failures,
    }
    if transmission is not None:
        result.update(
            stop_intersection_xy_mm=stop_xy,
            stop_power_transmittance=power_transmittance,
            pupil_transmitted_power_fraction=float(power_transmittance.sum() / admitted.sum()),
        )
    if finite:
        result.update(
            object_origin_xy_mm=object_xy,
            maximum_object_origin_residual_mm=origin_error,
            object_distance_from_first_vertex_mm=data["prescription"]["object_conjugate"][
                "distance_mm"
            ],
        )
    return result


def _propagate(data, wavefront, wavelength, subpixels):
    from prysm.propagation import focus_fixed_sampling

    aperture = wavefront["admitted"]
    samples = aperture.shape[0]
    dx = 2.4 * wavefront["exit_pupil_radius_mm"] / samples
    distance = wavefront["reference_sphere_radius_mm"]
    f_number = distance / (2 * wavefront["exit_pupil_radius_mm"])
    pitch = data["pixel_pitch_um"]
    if pitch / subpixels > wavelength * 1e-3 * f_number / 2:
        raise ValueError("Receiving subpixels exceed the scalar diffraction Nyquist spacing")
    period_um = wavelength * 1e-3 * distance / dx
    if data["kernel_size"] * pitch >= 0.8 * period_um:
        raise ValueError("Finite PSF support approaches periodic pupil-grid image replicas")
    # RayOptics returns chief OPL minus ray OPL; Prysm's forward DFT needs
    # physical pupil phase. The opposite sign mirrors distortion/lateral color.
    power = aperture
    if "stop_transmission" in data["prescription"]:
        if "stop_power_transmittance" not in wavefront:
            raise ValueError("Masked diffraction requires actual traced stop transmission")
        power = aperture * wavefront["stop_power_transmittance"]
        if (
            power.shape != aperture.shape
            or not np.isfinite(power).all()
            or np.any((power < 0) | (power > 1))
        ):
            raise ValueError("Invalid traced stop transmission")
    transmitted = power.sum()
    if transmitted == 0:
        return np.zeros((data["kernel_size"],) * 2)
    pupil = np.sqrt(power) * np.exp(-2j * np.pi * wavefront["opd_nm"] / wavelength)
    field = focus_fixed_sampling(
        pupil, dx, distance, wavelength * 1e-3, pitch / subpixels, data["kernel_size"] * subpixels
    )
    # Conditional on transmitted pupil energy. Absolute E/L comes only from
    # reciprocal radiometry and must never be inferred from this pupil-grid mean.
    sampled = np.abs(field) ** 2 / transmitted
    if not np.isfinite(sampled).all():
        raise ValueError("Nonfinite scalar diffraction result")
    size = data["kernel_size"]
    return sampled.reshape(size, subpixels, size, subpixels).sum(axis=(1, 3))


def characterize(request):
    data = validate_request(request)
    if (
        importlib.metadata.version("rayoptics") != "0.9.8"
        or importlib.metadata.version("prysm") != "0.21.1"
    ):
        raise ValueError("Prescription adapter requires verified RayOptics 0.9.8 and Prysm 0.21.1")
    deadline = time.monotonic() + MAX_SECONDS
    waves = data["prescription"]["wavelength_nm"]
    n, s = data["pupil_samples"], data["subpixel_samples"]
    shape = (len(waves), len(data["field_y_mm"]), len(data["field_x_mm"]))
    mass = np.zeros((4, *shape, data["kernel_size"], data["kernel_size"]))
    wavefronts = {}
    geometry = []
    entrance_diameters, effective_focal_lengths = [], []
    for wi, wavelength in enumerate(waves):
        model = _model(data, wi)
        fod = model["ar"]["parax_data"].fod
        entrance_diameters.append(float(2 * fod.enp_radius))
        effective_focal_lengths.append(float(fod.efl))
        for yi, y in enumerate(data["field_y_mm"]):
            for xi, x in enumerate(data["field_x_mm"]):
                for refinement, count in enumerate((n, 2 * n)):
                    wf = _wavefront(data, model, wavelength, x, y, count, deadline)
                    for reception, subpixels in enumerate((s, 2 * s + 1)):
                        mass[refinement + 2 * reception, wi, yi, xi] = _propagate(
                            data, wf, wavelength, subpixels
                        )
                    wavefronts[(refinement, wi, yi, xi)] = wf
                    geometry.append(
                        {
                            "wavelength_nm": wavelength,
                            "field_y_mm": y,
                            "field_x_mm": x,
                            "pupil_samples": count,
                            **{
                                key: value
                                for key, value in wf.items()
                                if not isinstance(value, np.ndarray)
                            },
                        }
                    )
    retained = mass.sum(axis=(-1, -2))
    kernels = np.divide(
        mass,
        retained[..., None, None],
        out=np.zeros_like(mass),
        where=retained[..., None, None] > 0,
    )
    delta = np.abs(kernels[:-1] - kernels[-1]).sum(axis=(-1, -2))
    mapping = max(g["exit_pupil_mapping_fraction"] for g in geometry)
    cone = max(g["ray_cone_deg"] for g in geometry)
    reference_angle = max(g["reference_axis_deg"] for g in geometry)
    phase_step = max(g["maximum_neighbor_opd_waves"] for g in geometry)
    numerical = bool(
        np.isfinite(kernels).all()
        and np.max(delta) <= data["max_kernel_l1_change"]
        and retained.min() >= data["minimum_retained_energy"]
        and retained.max() <= 1.001
        and phase_step <= 0.25
    )
    approximation = bool(
        mapping <= data["maximum_exit_pupil_mapping_fraction"]
        and cone <= data["maximum_ray_cone_deg"]
        and reference_angle <= 2.0
        and not any(g["pupil_boundary_truncated"] for g in geometry)
    )
    accepted = numerical and approximation
    summary = {
        "schema": "prescription_field_psf_characterization_v1",
        "factory_id": FACTORY_ID,
        "status": "PROPOSED" if accepted else "REFUSED",
        "source_kind": "simulated",
        "approval_status": "unapproved",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "normalized_request": data,
        "request_sha256": hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
        "backend": {"rayoptics": "0.9.8", "prysm": "0.21.1"},
        "method_and_version": FACTORY_ID,
        "opd_convention": "chief_optical_path_minus_ray_optical_path",
        "propagation_phase": "exp(-2*pi*i*opd_nm/wavelength_nm)",
        "included_effects": ["lens_psf", "pixel_aperture"],
        "numerical_agreement": numerical,
        "approximation_domain_checks": approximation,
        "max_kernel_l1_change_observed": float(delta.max()),
        "minimum_retained_energy_observed": float(retained.min()),
        "maximum_retained_energy_observed": float(retained.max()),
        "maximum_exit_pupil_mapping_fraction_observed": mapping,
        "maximum_ray_cone_deg_observed": cone,
        "maximum_reference_axis_deg_observed": reference_angle,
        "maximum_neighbor_opd_waves_observed": phase_step,
        "reference_focal_length_mm": data["reference_focal_length_mm"],
        "reference_wavelength_nm": waves[len(waves) // 2],
        "entrance_pupil_diameter_mm_by_wavelength": entrance_diameters,
        "effective_focal_length_mm_by_wavelength": effective_focal_lengths,
        "geometric_reference_f_number": data["reference_focal_length_mm"]
        / entrance_diameters[len(waves) // 2],
        "configurations": [
            {"pupil_samples": p, "subpixel_samples": q}
            for p, q in ((n, s), (2 * n, s), (n, 2 * s + 1), (2 * n, 2 * s + 1))
        ],
        "geometry": geometry,
        "assumptions": [
            "Infinity plane-wave object; centered spherical/plane prescription, "
            "explicit per-wavelength real indices and apertures",
            "Fixed authored sensor plane and shared ideal rectilinear source positions; "
            "no automatic focus or spectral recentering",
            "Paraxial exit-pupil mapping and uniform admitted pupil amplitude; "
            "pupil amplitude transport is not solved",
            "Scalar Fraunhofer diffraction; image plane treated perpendicular to reference axis, "
            "bounded to two degrees",
            "Unit-sum conditional PSFs; ideal-lossless absolute throughput "
            "and relative illumination are separate effects",
        ],
        "limitations": [
            "Mapping/cone bounds are approximation-domain checks, "
            "not prediction-error bounds or physical validation",
            "No vector polarization, Fresnel/coating transmission, stray light, "
            "finite conjugates or pupil-amplitude transport",
            "Finite kernel tails are reported and renormalized; "
            "numerical refinement is not total prediction uncertainty",
            "Field interpolation is linear in source position; "
            "intermediate fields are not independently optically validated",
            "Normalized source-position PSF scatter combined with receiving-position "
            "reciprocal RI is an approximation; full radiometric transport and its "
            "source-to-image Jacobian are not established",
        ],
        "issues": []
        if accepted
        else [
            {
                "code": "PRESCRIPTION_PSF_INCONCLUSIVE",
                "message": "Numerical agreement and declared approximation-domain checks "
                "are both required",
            }
        ],
    }
    arrays = {
        "psf_bin_energy": mass,
        "psf_kernel": kernels[-1],
        "retained_energy_fraction": retained,
        "wavelength_nm": np.asarray(waves),
        "field_y_mm": np.asarray(data["field_y_mm"]),
        "field_x_mm": np.asarray(data["field_x_mm"]),
    }
    for refinement, label in ((0, "coarse"), (1, "fine")):
        keys = (
            "opd_nm",
            "admitted",
            "ray_sensor_xy_mm",
            "exit_pupil_xy_mm",
            "chief_sensor_xy_mm",
            "reference_image_xy_mm",
        )
        if "stop_transmission" in data["prescription"]:
            keys += (
                "stop_intersection_xy_mm",
                "stop_power_transmittance",
                "pupil_transmitted_power_fraction",
            )
        if data["schema"] == "prescription_field_psf_v5":
            keys += ("object_origin_xy_mm",)
        for key in keys:
            arrays[f"{label}_{key}"] = np.asarray(
                [
                    wavefronts[(refinement, wi, yi, xi)][key]
                    for wi in range(shape[0])
                    for yi in range(shape[1])
                    for xi in range(shape[2])
                ]
            ).reshape((*shape, *np.asarray(wavefronts[(refinement, 0, 0, 0)][key]).shape))
    candidate = {
        "schema": "field_intensity_psf_v1",
        "wavelength_nm": waves,
        "field_y_mm": data["field_y_mm"],
        "field_x_mm": data["field_x_mm"],
        "field_coordinates": "sensor_center_yx",
        "interpolation": "bilinear_source_position",
        "values": kernels[-1].tolist(),
        "sample_pitch_um": data["pixel_pitch_um"],
        "normalization": "unit_sum",
        "centering": "array_center",
        "spatial_model": "field_grid",
        "boundary": "zero_outside_supplied_image",
        "sampling_model": "pixel_integrated_optical_impulse",
        "source": {
            "kind": "simulated",
            "description": "Prescription OPD with uniform-amplitude paraxial "
            "exit-pupil diffraction; unverified physical accuracy",
        },
    }
    if "stop_transmission" in data["prescription"]:
        summary.update(
            schema="prescription_field_psf_characterization_v" + data["schema"][-1],
            request_sha256=pupil_transmission.digest(data),
            prescription_sha256=pupil_transmission.digest(data["prescription"]),
            stop_transmission_sha256=pupil_transmission.digest(
                data["prescription"]["stop_transmission"]
            ),
            pupil_energy_normalization="conditional_on_admitted_transmitted_power",
            pupil_transmitted_power_fraction_basis=(
                "Mean power T on the geometrically admitted paraxial pupil grid; "
                "diagnostic only, not irradiance/radiance or its ratio"
            ),
        )
        summary["assumptions"][2] = (
            "Paraxial exit-pupil mapping with amplitude sqrt(T) from the actual explicit "
            "stop intersection; general pupil amplitude transport is not solved"
        )
        summary["assumptions"][4] = (
            "Unit-sum conditional transmitted PSFs; absolute masked throughput and "
            "relative illumination are separate reciprocal radiometry effects"
        )
        candidate["source"]["description"] = (
            "Prescription OPD with passive stop sqrt(T) amplitude on the paraxial exit pupil; "
            "conditional unit-sum PSF, unverified physical accuracy"
        )
    if data["schema"] in ("prescription_field_psf_v4", "prescription_field_psf_v5"):
        summary.update(
            factory_id=ASPHERE_FACTORY_ID,
            method_and_version=ASPHERE_FACTORY_ID,
            surface_geometry=optics_surfaces.certify_prescription(data["prescription"]["surfaces"]),
        )
        summary["assumptions"][0] = (
            "Infinity plane-wave object; centered plane/spherical/even-asphere prescription "
            "with signed vertex radius, conic constant and explicit A4/A6/A8/A10 in mm units; "
            "per-wavelength real indices and finite authored apertures"
        )
        summary["limitations"].append(
            "Centered rotationally symmetric even aspheres only; no freeform terms, "
            "tilt, decenter or A2 power term. Certified surface geometry and safeguarded "
            "ray intersections do not establish scalar diffraction prediction accuracy"
        )
        candidate["source"]["description"] = (
            "Centered even-asphere prescription OPD with passive stop sqrt(T) amplitude "
            "on the paraxial exit pupil; scalar conditional unit-sum PSF, "
            "unverified physical accuracy"
        )
    if data["schema"] == "prescription_field_psf_v5":
        summary.update(
            factory_id=FINITE_FACTORY_ID,
            method_and_version=FINITE_FACTORY_ID,
            object_conjugate=copy.deepcopy(data["prescription"]["object_conjugate"]),
            field_mapping={
                "input_coordinates": "ideal_sensor_chart_xy_mm",
                "object_coordinates": "physical_planar_object_xy_mm",
                "mapping": "object_xy=ideal_sensor_xy/reference_magnification",
                "wavelength_mapping": "one_authored_magnification_shared_by_all_wavelengths",
                "reference_focal_length_role": "reference_f_number_only_not_field_mapping",
                "sensor_plane": "authored_last_surface_gap_no_automatic_focus",
            },
            maximum_object_origin_residual_mm_observed=max(
                g["maximum_object_origin_residual_mm"] for g in geometry
            ),
        )
        summary["assumptions"][0] = (
            "Finite planar object at the authored distance before the first surface vertex; "
            "centered plane/spherical/even-asphere prescription, explicit real indices "
            "and apertures"
        )
        summary["assumptions"][1] = (
            "Fixed authored sensor plane and signed object-to-ideal-sensor chart magnification "
            "shared by all wavelengths; no automatic focus or chromatic recentering"
        )
        summary["limitations"][1] = (
            "No vector polarization, Fresnel/coating transmission, stray light or pupil-amplitude "
            "transport; no arbitrary object depths, tilted planes or 3D scene projection"
        )
        summary["limitations"].append(
            "Finite defocus is already traced into OPD; applying an additional downstream "
            "defocus kernel would count it twice. Strong defocus may fail finite support, "
            "phase sampling or the unchanged paraxial mapping gates"
        )
        candidate["source"]["description"] = (
            "Finite planar-object prescription OPD at the fixed authored sensor; passive stop "
            "sqrt(T) amplitude on the paraxial exit pupil, conditional scalar unit-sum PSF; "
            "unverified physical accuracy"
        )
    return {
        "summary": summary,
        "arrays": arrays,
        "model_candidate": candidate if accepted else None,
    }
