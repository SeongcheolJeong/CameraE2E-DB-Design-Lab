#!/usr/bin/env python3
"""Source-frozen 2D periodic, uniformly illuminated OCL spectral experiment.

Each solve contains one homogeneous CFA period. It estimates planar optical
absorption sensitivity, not a 3D Bayer response, charge collection, product QE,
spatial transfer kernel, or pupil-integrated response. The legacy finite-beam
worker is deliberately independent and remains unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np

SCHEMA = "camera_ocl_periodic2d_task_v1"
COLORS = ("red", "green", "blue")
ROLES = ("air", "active_si", "dti", "substrate", "passivation", "cfa", "lens")


def validate(task):
    if task.get("schema") != SCHEMA or task.get("dimension") != 2:
        raise ValueError("A versioned periodic 2D task is required")
    if task.get("source_model") != "periodic_homogeneous_cfa_plane_wave":
        raise ValueError("A uniform plane-wave source is required")
    if task.get("cfa_channel") not in COLORS or task.get("polarization") not in ("s", "p"):
        raise ValueError("Unknown CFA channel or polarization")
    if task.get("variant") not in ("reviewed", "ocl_shift"):
        raise ValueError("Only paired reviewed / OCL shift structures are supported")
    for key, lo, hi in (("wavelength_nm", 450., 650.), ("angle_deg", -40., 40.),
                        ("resolution", 30, 200), ("pml_um", .5, 2.),
                        ("after_source_time", 20., 300.)):
        value = task[key]
        if isinstance(value, bool) or not np.isfinite(value) or not lo <= value <= hi:
            raise ValueError("Invalid " + key)
    if int(task["resolution"]) != task["resolution"]:
        raise ValueError("Resolution must be an integer")
    if task["structure"]["report_code"] != "DEF-2206-805":
        raise ValueError("Only the reviewed IMX662 geometry is supported")
    if not np.isclose(task["rb_cfa_thickness_um"], .475, rtol=0, atol=1e-12):
        raise ValueError("The declared R/B thickness proxy is 0.475 um")
    if not np.isclose(task["ocl_shift_pitch"], .1, rtol=0, atol=1e-12):
        raise ValueError("The declared OCL shift is +0.1 pixel pitch")
    return task


def classify_point(task, x, y):
    """Fast scalar material callback matching the diagnostic role masks."""
    g = task["structure"]["geometry"]
    pitch = g["pitch_um"]
    dx = (x + pitch / 2) % pitch - pitch / 2
    if y < 0:
        boundary = pitch / 2 - abs(dx)
        if -g["dti_depth_um"] <= y and boundary < g["dti_width_um"] / 2:
            return ("silicon" if boundary < g["dti_width_um"] / 2 - g["dti_oxide_liner_um"]
                    else "passivation"), "dti"
        return "silicon", "active_si" if y >= -g["si_um"] else "substrate"
    if y < g["passivation_um"]:
        return "passivation", "passivation"
    cfa_top = g["passivation_um"] + g["cfa_um"]
    if y < cfa_top:
        if task["cfa_channel"] != "green" and y < cfa_top - task["rb_cfa_thickness_um"]:
            return "passivation", "passivation"
        return task["cfa_channel"], "cfa"
    shift = task["ocl_shift_pitch"] * pitch if task["variant"] == "ocl_shift" else 0.
    dxx = (x - shift + pitch / 2) % pitch - pitch / 2
    if (dxx / g["ocl_semiaxis_um"]) ** 2 + ((y-cfa_top) / g["ocl_height_um"]) ** 2 <= 1.:
        return "lens", "lens"
    return "air", "air"


def classify_arrays(task, x, y):
    """Return exact material and role labels; coordinates are physical microns."""
    g = task["structure"]["geometry"]
    pitch = g["pitch_um"]
    X, Y = np.broadcast_arrays(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    material = np.full(X.shape, "air", dtype="U12")
    role = np.full(X.shape, ROLES.index("air"), dtype=np.uint8)

    def assign(mask, name, label):
        material[mask] = name
        role[mask] = ROLES.index(label)

    dx = (X + pitch / 2) % pitch - pitch / 2
    boundary = pitch / 2 - np.abs(dx)
    assign(Y < 0, "silicon", "active_si")
    assign(Y < -g["si_um"], "silicon", "substrate")
    trench = ((Y < 0) & (Y >= -g["dti_depth_um"])
              & (boundary < g["dti_width_um"] / 2))
    assign(trench, "passivation", "dti")
    assign(trench & (boundary < g["dti_width_um"] / 2 - g["dti_oxide_liner_um"]),
           "silicon", "dti")
    cfa_top = g["passivation_um"] + g["cfa_um"]
    assign((Y >= 0) & (Y < g["passivation_um"]), "passivation", "passivation")
    cfa = (Y >= g["passivation_um"]) & (Y < cfa_top)
    color = task["cfa_channel"]
    assign(cfa, color, "cfa")
    if color != "green":
        assign(cfa & (Y < cfa_top - task["rb_cfa_thickness_um"]), "passivation", "passivation")
    shift = task["ocl_shift_pitch"] * pitch if task["variant"] == "ocl_shift" else 0.
    dxx = (X - shift + pitch / 2) % pitch - pitch / 2
    cap = ((dxx / g["ocl_semiaxis_um"]) ** 2
           + ((Y - cfa_top) / g["ocl_height_um"]) ** 2 <= 1.) & (Y >= cfa_top)
    assign(cap, "lens", "lens")
    return material, role


def material_values(task):
    """No wavelength extrapolation or material substitution is allowed."""
    wave = task["wavelength_nm"]
    values = {}
    for name, table in task["structure"]["materials"].items():
        axis = np.asarray(table["wavelength_nm"], dtype=float)
        if axis.ndim != 1 or len(axis) < 2 or np.any(np.diff(axis) <= 0):
            raise ValueError("Material wavelength axis must increase")
        if not axis[0] <= wave <= axis[-1]:
            raise ValueError("Material extrapolation forbidden")
        n = float(np.interp(wave, axis, table["n"]))
        k = float(np.interp(wave, axis, table["k"]))
        if not np.isfinite([n, k]).all() or k < 0 or n*n-k*k <= 0:
            raise ValueError("Unsupported or nonpassive material")
        values[name] = {"n": n, "k": k}
    for required in ("silicon", "passivation", "lens", *COLORS):
        if required not in values:
            raise ValueError("Missing material " + required)
    return values


def solve(task, output):
    import meep as mp
    from scipy.ndimage import map_coordinates

    validate(task)
    mp.verbosity(0)
    started = time.monotonic()
    g = task["structure"]["geometry"]
    wave, resolution, pml = task["wavelength_nm"], task["resolution"], task["pml_um"]
    pitch, freq = g["pitch_um"], 1000. / wave
    low = -g["si_um"]
    high = g["passivation_um"] + g["cfa_um"] + g["ocl_height_um"] + .5
    bottom, top = low - .4 - pml, high + .8 + pml
    cy = (bottom + top) / 2
    nk = material_values(task)
    media = {}
    for name, pair in nk.items():
        n, k = pair["n"], pair["k"]
        # Exactly reproduces the supplied complex epsilon at the monitored
        # wavelength. The pulse bandwidth is not a material validation claim.
        media[name] = mp.Medium(epsilon=n*n-k*k+2*n*k,
            E_susceptibilities=[mp.DrudeSusceptibility(frequency=freq, gamma=freq,
                                                      sigma=4*n*k)] if k > 0 else [])

    def point(x=0., y=0.):
        return mp.Vector3(x, y-cy)

    def material(p):
        name, _ = classify_point(task, p.x, p.y+cy)
        return mp.air if name == "air" else media[name]

    kx = freq * np.sin(np.deg2rad(task["angle_deg"]))
    source = mp.Source(mp.GaussianSource(freq, fwidth=.1*freq),
        component=mp.Ez if task["polarization"] == "s" else mp.Ex,
        center=point(y=high+.35), size=mp.Vector3(pitch, 0),
        amp_func=lambda p: np.exp(2j*np.pi*kx*p.x))
    size = mp.Vector3(pitch, high-low)

    def build(stack):
        sim = mp.Simulation(cell_size=mp.Vector3(pitch, top-bottom), dimensions=2,
            boundary_layers=[mp.PML(pml, direction=mp.Y)], sources=[source],
            resolution=resolution, k_point=mp.Vector3(kx), force_complex_fields=True,
            material_function=material if stack else None, extra_materials=list(media.values()))
        monitors = {label: sim.add_flux(freq, 0, 1, mp.FluxRegion(center=point(y=y),
            size=mp.Vector3(pitch, 0), direction=mp.Y, weight=-1))
            for label, y in (("top", high), ("bottom", low))}
        fields = sim.add_dft_fields([mp.Ex, mp.Ey, mp.Ez, mp.Dx, mp.Dy, mp.Dz],
            freq, 0, 1, center=point(y=(high+low)/2), size=size, yee_grid=True) if stack else None
        return sim, monitors, fields

    ref, rm, _ = build(False)
    ref.run(until_after_sources=task["after_source_time"])
    incident = float(mp.get_fluxes(rm["top"])[0])
    subtraction = ref.get_flux_data(rm["top"])
    if not np.isfinite(incident) or incident <= 0:
        raise ValueError("No positive incident reference power")
    ref.reset_meep()
    sim, monitors, fields = build(True)
    sim.load_minus_flux_data(monitors["top"], subtraction)
    sim.run(until_after_sources=task["after_source_time"])
    reflect = -float(mp.get_fluxes(monitors["top"])[0])/incident
    transmit = float(mp.get_fluxes(monitors["bottom"])[0])/incident
    x, y, _, _ = sim.get_array_metadata(dft_cell=fields)
    x, y = np.asarray(x), np.asarray(y)+cy
    axes_display = (x, y)
    signed_density = np.zeros((len(x), len(y)))
    layers = dict.fromkeys(ROLES, 0.)
    negative = 0.
    native = {}
    for e, d in ((mp.Ex, mp.Dx), (mp.Ey, mp.Dy), (mp.Ez, mp.Dz)):
        E, D = sim.get_dft_array(fields, e, 0), sim.get_dft_array(fields, d, 0)
        if not np.any(E):
            continue
        dims, lo, hi = sim.get_array_slice_dimensions(e, center=point(y=(high+low)/2), size=size)
        axes = [np.linspace(getattr(lo, key), getattr(hi, key), int(dims[i]))
                for i, key in enumerate(("x", "y"))]
        axes[1] += cy
        if E.shape != tuple(len(a) for a in axes):
            raise ValueError("Yee field coordinate mismatch")
        density = 2*np.pi*freq*np.imag(np.conj(E)*D)/incident
        quadrature = np.ones(E.shape)
        for axis, (coords, (lower, upper)) in enumerate(zip(axes, ((-pitch/2, pitch/2), (low, high)))):
            overlap = np.maximum(0, np.minimum(coords+.5/resolution, upper)-np.maximum(coords-.5/resolution, lower))
            shape = [1, 1]
            shape[axis] = len(coords)
            quadrature *= overlap.reshape(shape)
        negative += float(np.sum(np.minimum(density, 0)*quadrature))
        power = np.maximum(density, 0)*quadrature
        _, roles = classify_arrays(task, axes[0][:, None], axes[1][None, :])
        for i, role in enumerate(ROLES):
            layers[role] += float(power[roles == i].sum())
        coords = np.meshgrid(*[(a-b[0])*resolution for a, b in zip(axes_display, axes)], indexing="ij")
        signed_density += map_coordinates(density, coords, order=1, mode="nearest", prefilter=False)
        native[f"component_{e}_density"] = density.astype(np.float32)
        for axis, values in enumerate(axes):
            native[f"component_{e}_axis_{axis}"] = values
    _, roles = classify_arrays(task, x[:, None], y[None, :])
    absorption = sum(layers.values())
    residual = 1-reflect-transmit-absorption
    passive = min(reflect, transmit, *layers.values(), negative) >= -1e-5
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    arrays = output.with_suffix(".npz")
    np.savez_compressed(arrays, x_um=x, y_um=y, absorption_density=np.maximum(signed_density, 0).astype(np.float32),
        signed_absorption_density=signed_density.astype(np.float32), role=roles, **native)
    result = dict(schema="camera_ocl_periodic2d_sample_v1", task_id=task["id"],
        structure_revision=task["structure"]["revision"], worker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        dimension=2, source_model=task["source_model"], cfa_channel=task["cfa_channel"],
        wavelength_nm=wave, angle_deg=task["angle_deg"], polarization=task["polarization"],
        variant=task["variant"], resolution=resolution, control=task.get("control", "mesh"),
        after_source_time=task["after_source_time"], pml_um=pml,
        material_nk=nk, cfa_thickness_um=g["cfa_um"] if task["cfa_channel"] == "green" else task["rb_cfa_thickness_um"],
        ocl_shift_um=task["ocl_shift_pitch"]*pitch if task["variant"] == "ocl_shift" else 0.,
        R=reflect, T=transmit, side_outflow=0., absorption=absorption,
        active_si_absorption=layers["active_si"], layer_absorption=layers,
        energy_residual=residual, incident_power=incident, pixel_absorption=[layers["active_si"]],
        negative_dissipation_integral=negative, passive=passive,
        support_tail_fraction=None, support_tail_applicability="not_applicable_periodic_uniform_input",
        camera_response_eligible=False, measured_accuracy=False,
        array_sha256=hashlib.sha256(arrays.read_bytes()).hexdigest(),
        elapsed_s=time.monotonic()-started, meep_version=mp.__version__,
        numerical_valid=bool(passive and np.isfinite([residual, *layers.values()]).all() and abs(residual) <= .01))
    output.write_text(json.dumps(result, sort_keys=True, allow_nan=False))
    sim.reset_meep()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("output")
    args = parser.parse_args()
    solve(json.loads(Path(args.task).read_text()), args.output)
