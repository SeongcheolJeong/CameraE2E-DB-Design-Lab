"""Scalar reference-sphere angular spectrum on the fixed, flat sensor plane.

This is a separate model from prescription_psf's paraxial exit-pupil FFT.
Actual traced reference-sphere intersections determine the Fourier directions;
the phase is RayOptics chief-minus-ray OPD. Uniform incident entrance-pupil
flux is mapped with its area Jacobian, then conditional PSF energy is normalized.
Absolute throughput is supplied only by the unchanged reciprocal radiometry.
Polarization, coating amplitude and vector high-NA propagation are not modeled.
"""

from __future__ import annotations

import math
import time

import numpy as np
from scipy.interpolate import CloughTocher2DInterpolator
from scipy.spatial import Delaunay

from . import prescription_psf

PROFILE = "exit_pupil_diffraction"


def aim_at_stop(model, field, wave):
    """Verify chief aiming, and bracket the meridional root when Newton fails.

    RayOptics' scalar iteration can return an unconverged aim. A trace error
    beyond the stop can also hide the already-computed stop coordinate. Use
    that coordinate, never an assumed zero residual, and verify the final ray.
    The optical prescription and physical apertures are not changed.
    """
    from rayoptics.raytr import trace
    from rayoptics.raytr.traceerror import TraceError
    from scipy.optimize import brentq

    if field.x != 0 or model.optical_spec['fov'].is_wide_angle:
        raise ValueError("Verified stop aiming currently requires a meridional centered field")
    stop = model.seq_model.stop_surface
    tolerance = 1e-6  # mm; accommodates finite representation of infinity.

    def residual(y):
        try:
            pkg = trace.trace_base(model, [0., float(y)], field, wave,
                pupil_type='aim pt', apply_vignetting=False, check_apertures=False)
        except TraceError as exc:
            if exc.surf <= stop:
                raise ValueError('Chief aiming ray cannot reach the stop') from exc
            pkg = exc.ray_pkg
        value = float(pkg[0][stop][0][1])
        if not math.isfinite(value):
            raise ValueError('Nonfinite chief stop residual')
        return value

    original = np.asarray(trace.aim_chief_ray(model, field, wave), float)
    try:
        before = residual(original[1])
    except ValueError:
        before = None
    method = 'verified_backend_aim'
    y = float(original[1])
    if before is None or abs(before) > tolerance:
        method = 'bracketed_stop_intercept'
        radius = model.optical_spec['pupil'].value / 2
        previous = None
        root = None
        # Start close to the paraxial aim; broaden without relying on a
        # numerical derivative of rays launched from the distant object plane.
        for scale in (.01, .05, .25, 1., 2., 4.):
            previous = None
            for value in np.linspace(-scale * radius, scale * radius, 17):
                try:
                    r = residual(value)
                except ValueError:
                    previous = None
                    continue
                if abs(r) <= tolerance:
                    root = float(value)
                    break
                if previous is not None and r * previous[1] < 0:
                    root = brentq(residual, previous[0], value, xtol=1e-10)
                    break
                previous = (value, r)
            if root is not None:
                break
        if root is None:
            raise ValueError('Cannot bracket a physical chief ray through the stop center')
        y = root
    field.aim_info = np.array([0., y])
    after = residual(y)
    if abs(after) > tolerance:
        raise ValueError('Chief ray failed the verified stop-center tolerance')
    ray, _, _ = trace.trace_base(model, [0., 0.], field, wave,
        apply_vignetting=False, check_apertures=True, pt_inside_fuzz=0.)
    if np.linalg.norm(ray[stop][0][:2]) > tolerance:
        raise ValueError('Relative-pupil chief differs from the verified stop intercept')
    return dict(method=method, original_aim_xy_mm=original.tolist(),
                original_stop_residual_mm=before, aim_xy_mm=field.aim_info.tolist(),
                stop_residual_mm=after, stop_residual_limit_mm=tolerance)


def trace_pupil(request, wi, object_angle_deg, anchor_xy_mm, samples, azimuth_deg):
    from rayoptics.raytr import trace, waveabr
    from rayoptics.raytr.opticalspec import Field

    wave = request["prescription"]["wavelength_nm"][wi]
    if request["object_conjugate"] != "infinity_plane_wave":
        raise ValueError("Reference-sphere profile requires an infinity plane-wave source")
    transmission = request["prescription"].get("stop_transmission")
    if transmission is not None and not np.all(
        np.asarray(transmission["power_transmittance"]) == 1
    ):
        raise ValueError(
            "Reference-sphere profile currently requires uniform unit stop transmission"
        )
    model = prescription_psf._model(request, wi)
    field = Field(y=object_angle_deg, fov=model.optical_spec["fov"])
    aiming = aim_at_stop(model, field, wave)
    chief = trace.trace_chief_ray(model, field, wave, 0.0)
    sphere = trace.calculate_reference_sphere(model, field, wave, 0.0, chief,
        image_pt_2d=np.asarray(anchor_xy_mm).tolist())
    surfaces = request["prescription"]["surfaces"]
    center = np.r_[anchor_xy_mm, surfaces[-1]["thickness_mm"]]
    radius = sphere[2]
    axis = (np.arange(samples) - samples // 2) * 2.4 / samples
    angle = math.radians(azimuth_deg - 90)
    rotation = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    positions, directions, phase, entrance, landing = [], [], [], [], []
    admitted = np.zeros((samples, samples), bool)
    opd_grid = np.full((samples, samples), np.nan)
    deadline = time.monotonic() + 180
    failures = {}

    def trace_at(x, y):
        result = trace.trace_safe(
            model,
            [x, y],
            field,
            wave,
            None,
            "summary",
            apply_vignetting=False,
            check_apertures=True,
            pt_inside_fuzz=0.0,
        )
        if result.err is not None:
            name = type(result.err).__name__
            if name not in {"TraceRayBlockedError", "TraceMissedSurfaceError", "TraceTIRError"}:
                raise ValueError("Unexpected reference-sphere ray failure: " + name)
            failures[name] = failures.get(name, 0) + 1
            return None
        return result

    def record(result, x, y):
        ray = result.pkg[0]
        prescription_psf._validate_physical_ray(ray, model, surfaces, "reference sphere")
        point, direction = ray[-2][:2]
        delta = point - center
        discriminant = (direction @ delta) ** 2 - delta @ delta + radius**2
        if discriminant <= 0:
            raise ValueError("A transmitted ray misses the reference sphere")
        distance = -direction @ delta - math.sqrt(discriminant)
        hit = point + distance * direction
        unit = (center - hit) / radius
        if not np.isfinite(unit).all() or unit[2] <= 0:
            raise ValueError("Reference sphere has a non-forward angular spectrum")
        opd = (
            waveabr.wave_abr_full_calc(
                model["ar"]["parax_data"].fod, field, wave, 0.0, result.pkg, chief, sphere
            )
            * 1e6
        )
        if not np.isfinite(opd):
            raise ValueError("Nonfinite reference-sphere OPD")
        positions.append(rotation @ -unit[:2])
        directions.append(unit)
        phase.append(opd)
        entrance.append([x, y])
        landing.append(rotation @ (ray[-1][0][:2] - anchor_xy_mm))
        return opd

    for iy, y in enumerate(axis):
        if time.monotonic() > deadline:
            raise ValueError("Reference-sphere trace exceeded its 180s node budget")
        for ix, x in enumerate(axis):
            result = trace_at(x, y)
            if result is None:
                continue
            opd_grid[iy, ix] = record(result, x, y)
            admitted[iy, ix] = True
    interior_count = len(positions)
    if trace_at(0., 0.) is None:
        raise ValueError('Verified pupil center is blocked; boundary search cannot start')
    # Resolve the hard pupil edge independently of the coarse interior lattice.
    # Restrict this model to star-convex admitted pupils; do not bridge holes.
    for theta in np.linspace(0, 2 * np.pi, samples * 4, endpoint=False):
        if time.monotonic() > deadline:
            raise ValueError("Reference-sphere trace exceeded its 180s node budget")
        ux, uy = math.cos(theta), math.sin(theta)
        low, high = 0.0, 1.2
        if trace_at(high * ux, high * uy) is not None:
            raise ValueError("Admitted pupil exceeds the boundary-search window")
        for _ in range(13):
            mid = (low + high) / 2
            if trace_at(mid * ux, mid * uy) is None:
                high = mid
            else:
                low = mid
        for fraction in (0.25, 0.5, 0.75):
            if trace_at(low * fraction * ux, low * fraction * uy) is None:
                raise ValueError("Non-star-convex pupil needs another propagation model")
        x, y = low * ux, low * uy
        record(trace_at(x, y), x, y)
    if not positions:
        raise ValueError("No transmitted reference-sphere rays")
    if admitted[0].any() or admitted[-1].any() or admitted[:, 0].any() or admitted[:, -1].any():
        raise ValueError("Entrance-pupil sampling window truncates the admitted pupil")
    jumps = [np.abs(np.diff(opd_grid, axis=i)) / wave for i in (0, 1)]
    return {
        "q": np.asarray(positions),
        "opd_nm": np.asarray(phase),
        "entrance_xy": np.asarray(entrance),
        "landing_xy_mm": np.asarray(landing),
        "reference_sphere_direction_xyz": np.asarray(directions),
        "samples": samples,
        "step": 2.4 / samples,
        "interior_count": interior_count,
        "maximum_neighbor_opd_waves": float(max(np.nanmax(v) for v in jumps)),
        "opd_rms_waves": float(np.std(phase[:interior_count]) / wave),
        "reference_sphere_radius_mm": float(radius),
        "reference_axis_deg": float(math.degrees(math.acos(sphere[1][2]))),
        "failures": failures,
        "chief_aiming": aiming,
        "chief_sensor_xy_mm": rotation @ chief[0].ray[-1][0][:2],
        "anchor_xy_mm": rotation @ anchor_xy_mm,
    }


def regular_pupil(traced, grid_samples):
    """Map incident-cell flux to direction space with an explicit area Jacobian.

    Triangles may join only neighboring samples of the entrance lattice. This
    prevents a convex hull from filling clipped holes or untraced pupil gaps.
    OPD is interpolated as an unwrapped path, never as wrapped complex phase.
    """
    q = traced["q"]
    center = (q.max(axis=0) + q.min(axis=0)) / 2
    span = float(np.max(q.max(axis=0) - q.min(axis=0)) * 1.04)
    spacing = span / grid_samples
    axis = (np.arange(grid_samples) - grid_samples // 2) * spacing
    xx, yy = np.meshgrid(axis + center[0], axis + center[1])
    target = np.column_stack((xx.ravel(), yy.ravel()))
    triangles = Delaunay(q)
    simplex = triangles.find_simplex(target)
    inside = simplex >= 0
    selected = simplex[inside]
    vertices = triangles.simplices[selected]
    transform = triangles.transform[selected]
    bary = np.einsum("nij,nj->ni", transform[:, :2], target[inside] - transform[:, 2])
    bary = np.column_stack((bary, 1 - bary.sum(axis=1)))
    tri_q = q[vertices]
    tri_e = traced["entrance_xy"][vertices]

    def area(v):
        a, b = v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]
        return (a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]) / 2

    area_q, area_e = area(tri_q), area(tri_e)
    edge = np.max(
        np.stack(
            [np.linalg.norm(tri_e[:, a] - tri_e[:, b], axis=1) for a, b in ((0, 1), (1, 2), (2, 0))]
        ),
        axis=0,
    )
    neighbors = edge <= 1.5 * traced["step"]
    if np.any((area_e * area_q <= 0) & neighbors):
        raise ValueError("Reference-sphere angular mapping folds the incident pupil")
    area_q, area_e = np.abs(area_q), np.abs(area_e)
    valid = (area_q > 1e-14) & (area_e > 1e-14) & neighbors
    power, phase = np.zeros(target.shape[0]), np.zeros(target.shape[0])
    locations = np.flatnonzero(inside)[valid]
    power[locations] = area_e[valid] / area_q[valid]
    phase[locations] = CloughTocher2DInterpolator(triangles, traced["opd_nm"])(target[locations])
    if not np.isfinite(power).all() or power.sum() <= 0 or not np.isfinite(phase).all():
        raise ValueError("Invalid entrance-to-direction pupil Jacobian")
    return {
        "power": power.reshape(grid_samples, grid_samples),
        "opd_nm": phase.reshape(grid_samples, grid_samples),
        "spacing": spacing,
    }


def interpolation_error(coarse, fine, wave_nm):
    """Independent finer physical rays validate interpolation of unwrapped OPD."""
    interp = CloughTocher2DInterpolator(coarse["q"], coarse["opd_nm"])
    values = interp(fine["q"])
    valid = np.isfinite(values)
    residual = (values[valid] - fine["opd_nm"][valid]) / wave_nm
    return {
        "maximum_abs_waves": float(np.max(np.abs(residual))),
        "rms_waves": float(np.sqrt(np.mean(residual**2))),
        "fine_ray_coverage_fraction": float(valid.mean()),
        "maximum_abs_waves_limit": 0.02,
        "coverage_note": "New boundary rays are assessed by full PSF refinement",
    }


def phase_grid_step(pupil, wave_nm):
    valid = pupil["power"] > 0
    step = 0.0
    for axis in (0, 1):
        left = np.take(valid, range(valid.shape[axis] - 1), axis=axis)
        right = np.take(valid, range(1, valid.shape[axis]), axis=axis)
        changes = np.abs(np.diff(pupil["opd_nm"], axis=axis))[left & right] / wave_nm
        step = max(step, float(changes.max()) if changes.size else 0.0)
    return step


def propagate(pupil, wave_nm, size, pitch_um, subpixels, *, phase_zero=False):
    from prysm.propagation import focus_fixed_sampling

    spacing = pupil["spacing"]
    period_um = wave_nm * 1e-3 / spacing
    if size * pitch_um >= 0.8 * period_um:
        raise ValueError("Direction-grid PSF support approaches periodic image replicas")
    support = np.argwhere(pupil["power"] > 0)
    extent = np.max(np.ptp(support, axis=0)) * spacing
    if pitch_um / subpixels > wave_nm * 1e-3 / (2 * extent):
        raise ValueError("Receiver samples exceed angular-spectrum intensity Nyquist spacing")
    phase = 0 if phase_zero else pupil["opd_nm"]
    field = np.sqrt(pupil["power"]) * np.exp(-2j * np.pi * phase / wave_nm)
    # q is dimensionless. A notional propagation distance of 1mm and pupil
    # spacing q*1mm gives the exact exp(-2pi*i*q*x/lambda) flat-plane transform.
    focused = focus_fixed_sampling(
        field, spacing, 1.0, wave_nm * 1e-3, pitch_um / subpixels, size * subpixels
    )
    mass = (
        (np.abs(focused) ** 2 / pupil["power"].sum())
        .reshape(size, subpixels, size, subpixels)
        .sum(axis=(1, 3))
    )
    return mass[::-1].copy()  # physical +Y is image row-up; apply exactly once.


def node(request, wi, object_angle_deg, anchor_xy_mm, azimuth_deg, samples, size, pitch_um):
    wave = request["prescription"]["wavelength_nm"][wi]
    traces = [
        trace_pupil(request, wi, object_angle_deg, anchor_xy_mm, n, azimuth_deg)
        for n in (samples, 2 * samples)
    ]
    span = max(np.max(np.ptp(t["q"], axis=0)) * 1.04 for t in traces)
    minimum_grid = math.ceil(size * pitch_um * span / (0.8 * wave * 1e-3))
    grid = max(256, 2 ** math.ceil(math.log2(minimum_grid + 1)))
    pupils = [regular_pupil(t, grid) for t in traces]
    fine_pupil = regular_pupil(traces[-1], 2 * grid)
    # Receiver sampling has an explicit bound; the 5x result is compared to11x.
    masses = [propagate(p, wave, size, pitch_um, 11) for p in pupils]
    masses += [propagate(fine_pupil, wave, size, pitch_um, s) for s in (5, 11)]
    energy = np.asarray([m.sum() for m in masses])
    if not np.isfinite(energy).all() or np.any(energy <= 0):
        raise ValueError("Nonfinite or empty propagated PSF")
    kernels = [m / e for m, e in zip(masses, energy, strict=True)]
    changes = [float(np.abs(k - kernels[-1]).sum()) for k in kernels[:-1]]
    phase_step = phase_grid_step(fine_pupil, wave)
    error = interpolation_error(*traces, wave)
    accepted = (
        max(changes) <= 0.03
        and energy.min() >= 0.95
        and energy.max() <= 1.001
        and phase_step <= 0.25
        and error["maximum_abs_waves"] <= 0.02
    )
    limited = propagate(fine_pupil, wave, size, pitch_um, 11, phase_zero=True)
    if not np.isfinite(limited).all() or limited.sum() <= 0:
        raise ValueError("Invalid phase-zero diffraction diagnostic")
    axis = (np.arange(size) - size // 2) * pitch_um
    centroid = np.array([(kernels[-1].sum(0) * axis).sum(), -(kernels[-1].sum(1) * axis).sum()])
    diagnostic = {
        "status": "PROPOSED" if accepted else "REFUSED",
        "wavelength_nm": wave,
        "pupil_samples": [samples, 2 * samples],
        "direction_grid_samples": [grid, 2 * grid],
        "receiver_subpixels": [5, 11],
        "kernel_l1_changes": changes,
        "maximum_kernel_l1_change": 0.03,
        "retained_energy": energy.tolist(),
        "minimum_retained_energy": 0.95,
        "maximum_neighbor_opd_waves": phase_step,
        "maximum_neighbor_opd_waves_limit": 0.25,
        "traced_neighbor_opd_waves": traces[-1]["maximum_neighbor_opd_waves"],
        "unwrapped_opd_interpolation": error,
        "reference_axis_deg": traces[-1]["reference_axis_deg"],
        "opd_rms_waves": traces[-1]["opd_rms_waves"],
        "phase_zero_retained_energy": float(limited.sum()),
        "fixed_sensor_reference_xy_mm": traces[-1]["anchor_xy_mm"].tolist(),
        "geometric_centroid_um": (
            traces[-1]["landing_xy_mm"][: traces[-1]["interior_count"]].mean(axis=0) * 1000
        ).tolist(),
        "diffraction_centroid_um": centroid.tolist(),
        "scalar_model": "reference_sphere_direction_spectrum_flux_conserving_v1",
    }
    return kernels[-1], limited / limited.sum(), diagnostic
