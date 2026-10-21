#!/usr/bin/env python3
"""Resource-bounded Meep producer for sensor-level optical response LUTs.

The worker deliberately supports Python 3.10 without importing pyisetcam. It
reads a frozen canonical material JSON. No measured electrical QE is implied.
A coarse process check is never promoted into a validated production LUT.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import shlex
import shutil
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

SCHEMA = "camerae2e_fdtd_response_v1"
SITE_NAMES = ["R", "Gr", "Gb", "B"]
SITE_CHANNELS = ["red", "green", "green", "blue"]
LOCAL_LIMITS = {"concurrent_jobs": 1, "cores": 4, "memory_gib": 24.0, "wall_seconds": 7200.0}
DEFAULT_MEEP_PYTHON = "/Users/seongcheoljeong/FDTD/.meep-env/bin/python"
GATES = ("energy_balance", "spatial", "time", "pml", "interpolation")


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def _regular_axis(start, stop, step, name):
    step = float(step)
    if not np.isfinite(step) or step <= 0:
        raise ValueError(f"{name} step must be finite and positive.")
    count = (stop - start) / step
    if not np.isclose(count, round(count), atol=1e-9):
        raise ValueError(f"{name} step must divide its covered interval exactly.")
    return [float(value) for value in np.linspace(start, stop, int(round(count)) + 1)]


def direction_grid(step_deg, phi_step_deg=45):
    """Normal is one direction; full periodic azimuth rings cover [0,360)."""
    theta = _regular_axis(0, 40, step_deg, "theta")
    phi = _regular_axis(0, 360, phi_step_deg, "phi")[:-1]
    return [(0.0, 0.0)] + [(t, p) for t in theta[1:] for p in phi]


def spatial_levels(resolution):
    if not np.isfinite(resolution) or resolution != int(resolution) or resolution < 4:
        raise ValueError("Production resolution must be an integer >=4 with distinct mesh levels.")
    levels = [int(resolution * factor) for factor in (0.75, 1.0, 1.25)]
    if len(set(levels)) != 3:
        raise ValueError("Spatial convergence requires three distinct mesh levels.")
    return levels


def material_nk(material, wavelength_nm):
    wave = np.asarray(material["wavelength_nm"], dtype=float)
    n, k = np.asarray(material["n"], dtype=float), np.asarray(material["k"], dtype=float)
    if (
        wave.ndim != 1
        or wave.size < 2
        or n.shape != wave.shape
        or k.shape != wave.shape
        or not np.all(np.isfinite(np.concatenate([wave, n, k])))
        or np.any(np.diff(wave) <= 0)
        or np.any(n <= 0)
        or np.any(k < 0)
    ):
        raise ValueError("Invalid canonical n,k table.")
    if not np.isfinite(wavelength_nm) or not wave[0] <= wavelength_nm <= wave[-1]:
        raise ValueError("Wavelength is outside canonical material coverage.")
    return complex(np.interp(wavelength_nm, wave, n), np.interp(wavelength_nm, wave, k))


def geometry_from_bundle(bundle):
    source = bundle["geometry_um"]
    geometry = {
        key: float(source[key])
        for key in (
            "pitch",
            "lens_height",
            "lens_edge_gap",
            "cfa_thickness",
            "passivation_thickness",
            "si_thickness",
        )
    }
    if any(not np.isfinite(value) or value < 0 for value in geometry.values()):
        raise ValueError("Geometry must be finite and nonnegative.")
    if geometry["pitch"] <= 0 or geometry["si_thickness"] <= 0:
        raise ValueError("Pitch and silicon thickness must be positive.")
    if 2 * geometry["lens_edge_gap"] >= geometry["pitch"]:
        raise ValueError("Microlens edge gap leaves no aperture.")
    geometry.update(
        cell_pixels=[2, 2],
        cfa_sites=SITE_NAMES,
        lens_shape="upper_half_ellipsoid",
        exit_medium="air",
        lens_shift_um=[float(v) for v in source.get("lens_shift_um", [0.0, 0.0])],
        cfa_thickness_by_channel_um={
            c: float(bundle["channels"][c]["thickness_um"]) for c in ("red", "green", "blue")
        },
        cfa_thickness_rule="fixed CFA top; channel passivation complements CFA thickness",
        source_model="periodic monochromatic plane wave",
    )
    if len(geometry["lens_shift_um"]) != 2 or not np.all(np.isfinite(geometry["lens_shift_um"])):
        raise ValueError("Lens shift must be a finite x,y pair.")
    for thickness in geometry["cfa_thickness_by_channel_um"].values():
        if (
            not np.isfinite(thickness)
            or not 0 < thickness < geometry["cfa_thickness"] + geometry["passivation_thickness"]
        ):
            raise ValueError(
                "Channel CFA thickness must leave positive passivation at fixed CFA top."
            )
    return geometry


def estimate_resources(geometry, resolution, pml_um=0.6):
    length = (
        geometry["si_thickness"]
        + geometry["passivation_thickness"]
        + geometry["cfa_thickness"]
        + geometry["lens_height"]
        + 2.0
        + 2 * pml_um
    )
    shape = [
        math.ceil(2 * geometry["pitch"] * resolution),
        math.ceil(length * resolution),
        math.ceil(2 * geometry["pitch"] * resolution),
    ]
    cells = math.prod(shape)
    # Conservative complex-field + material + DFT-field allowance, not measured RSS.
    rss_gib = cells * 512 / 1024**3 + 0.5
    return {
        "grid_shape_estimate": shape,
        "cell_count_estimate": cells,
        "rss_gib_estimate": rss_gib,
        "bytes_per_cell_assumption": 512,
        "requires_external_resources": rss_gib > LOCAL_LIMITS["memory_gib"],
        "runtime_estimate_seconds": None,
        "runtime_estimate_status": "requires measured pilot throughput",
    }


def _job(
    bundle_path,
    bundle,
    geometry,
    role,
    wave,
    theta,
    phi,
    polarization,
    resolution,
    after_source_time=40.0,
    pml_um=0.6,
    channel="green",
):
    body = {
        "role": role,
        "sensor_id": bundle["sensor_id"],
        "report_code": bundle["report_code"],
        "bundle_path": str(bundle_path),
        "bundle_file_sha256": file_hash(bundle_path),
        "material_bundle_sha256": bundle["material_bundle_sha256"],
        "geometry": geometry,
        "geometry_sha256": bundle["geometry_sha256"],
        "solver_geometry_sha256": canonical_hash(geometry),
        "solver_code_sha256": file_hash(__file__),
        "wavelength_nm": float(wave),
        "solver_path": str(bundle_path.parents[1] / "runner_snapshot.py"),
        "theta_deg": float(theta),
        "phi_deg": float(phi),
        "polarization": polarization,
        "resolution_px_per_um": int(resolution),
        "after_source_time": after_source_time,
        "pml_um": pml_um,
        "planar_channel": channel,
        "planar_include_lens": False,
    }
    body["job_sha256"] = canonical_hash(body)
    body["job_id"] = body["job_sha256"][:20]
    body["resource_estimate"] = estimate_resources(geometry, resolution, pml_um)
    if role == "planar_calibration":
        body["resource_estimate"].update(rss_gib_estimate=0.25, requires_external_resources=False)
    return body


def plan_campaign(
    bundle_paths,
    output_dir,
    *,
    resolution=80,
    sampling="final",
    wavelength_step_nm=None,
    theta_step_deg=None,
    phi_step_deg=None,
):
    """Freeze all real sensor bundles and create resumable, content-addressed jobs."""
    levels = spatial_levels(resolution)
    if sampling not in {"initial", "final", "refined"}:
        raise ValueError("Unknown sampling preset.")
    defaults = {"initial": (50, 10, 45), "final": (10, 5, 45), "refined": (5, 2.5, 11.25)}[sampling]
    dw, dt, dp = [
        defaults[i] if v is None else float(v)
        for i, v in enumerate((wavelength_step_nm, theta_step_deg, phi_step_deg))
    ]
    if dw < 5 or dt < 2.5 or dp < 11.25:
        raise ValueError(
            "Supported minimum steps are wavelength 5 nm, theta 2.5 deg, phi 11.25 deg."
        )
    waves = _regular_axis(400, 700, dw, "wavelength")
    directions = direction_grid(dt, dp)
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshot = output / "runner_snapshot.py"
    if snapshot.exists() and file_hash(snapshot) != file_hash(__file__):
        raise ValueError("Frozen solver changed; use a new campaign directory.")
    snapshot.write_bytes(Path(__file__).read_bytes())
    bundles = [json.loads(Path(path).read_text()) for path in bundle_paths]
    bundles.sort(
        key=lambda b: (
            {"DEF-2011-804": 0, "DEF-1807-801": 1, "DEF-2206-805": 2}.get(b["report_code"], 3),
            b["report_code"],
        )
    )
    jobs, records = [], []
    for bundle in bundles:
        geometry = geometry_from_bundle(bundle)
        bundle_path = output / "bundles" / (bundle["report_code"] + ".json")
        if bundle_path.exists() and json.loads(bundle_path.read_text()) != bundle:
            raise ValueError("Frozen material bundle changed; use a new campaign directory.")
        write_json(bundle_path, bundle)

        def create(
            role,
            wave,
            theta,
            phi,
            pol,
            res,
            bundle_path=bundle_path,
            bundle=bundle,
            geometry=geometry,
            **kw,
        ):
            return _job(bundle_path, bundle, geometry, role, wave, theta, phi, pol, res, **kw)

        # These are explicit wiring/calibration jobs, excluded from production samples.
        for wave in (400, 550, 700):
            for theta, pol in [(0, "s"), (40, "s"), (40, "p")]:
                jobs.append(
                    create("planar_calibration", wave, theta, 0, pol, 120, after_source_time=80.0)
                )
        for pol in ("s", "p"):
            jobs.append(create("3d_process_check", 550, 0, 0, pol, 6, after_source_time=12.0))
        for wave in waves:
            for theta, phi in directions:
                for pol in ("s", "p"):
                    jobs.append(create("production", wave, theta, phi, pol, resolution))
        anchors = [
            (wave, theta, phi)
            for wave in (400, 550, 700)
            for theta, phi in [(0, 0), (40, 0), (40, 45)]
        ]
        for wave, theta, phi in anchors:
            for pol in ("s", "p"):
                for level in levels:
                    jobs.append(create("spatial", wave, theta, phi, pol, level))
                for duration in (40.0, 80.0):
                    jobs.append(
                        create(
                            "time", wave, theta, phi, pol, resolution, after_source_time=duration
                        )
                    )
                for pml in (0.6, 0.9):
                    jobs.append(create("pml", wave, theta, phi, pol, resolution, pml_um=pml))
        # Off-grid validation anchors test the eventual interpolant, not its own nodes.
        # Midpoints track the actual axes, including the refined 5/2.5/11.25 grid.
        validation_points = []
        for fraction in (0.1, 0.5, 0.9):
            wi = min(int(fraction * (len(waves) - 1)), len(waves) - 2)
            wave = (waves[wi] + waves[wi + 1]) / 2
            ti = min(int(fraction * 40 / dt), int(40 / dt) - 1)
            theta = (ti + 0.5) * dt
            phi = (int(fraction * 360 / dp) + 0.5) * dp % 360
            validation_points.extend([(wave, 0, 0), (wave, theta, phi)])
        for wave, theta, phi in validation_points:
            for pol in ("s", "p"):
                jobs.append(create("interpolation", wave, theta, phi, pol, resolution))
        records.append(
            {
                "sensor_id": bundle["sensor_id"],
                "report_code": bundle["report_code"],
                "bundle_path": str(bundle_path),
                "geometry": geometry,
                "material_bundle_sha256": bundle["material_bundle_sha256"],
                "resource_estimate": estimate_resources(geometry, resolution),
            }
        )
    manifest = {
        "schema": "camerae2e_fdtd_campaign_v1",
        "estimate_only": True,
        "sampling": sampling,
        "axes": {
            "wavelength_nm": waves,
            "theta_deg": sorted({t for t, p in directions}),
            "phi_deg": sorted({p for t, p in directions}),
        },
        "sampling_steps": {"wavelength_step_nm": dw, "theta_step_deg": dt, "phi_step_deg": dp},
        "output_dir": str(output),
        "local_limits": LOCAL_LIMITS,
        "solver_python": DEFAULT_MEEP_PYTHON,
        "solver_code_sha256": file_hash(__file__),
        "solver_snapshot": str(snapshot),
        "sensors": records,
        "jobs": jobs,
        "production_jobs": sum(j["role"] == "production" for j in jobs),
        "reference_runs": "Each job includes a separate empty-cell incident reference.",
        "completion": "pending; a manifest is not a completed or validated LUT",
        "sensitivity": (
            "Run tools/prepare_sensor_fdtd_sensitivity.py for immutable "
            "CFA/lens variant bundles and anchor jobs; execution remains "
            "pending."
        ),
    }
    manifest["manifest_sha256"] = canonical_hash(manifest)
    path = output / "campaign.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError("Campaign content changed; choose a new output directory.")
    write_json(path, manifest)
    return manifest


def _plane_positions(geometry, pml):
    stack = sum(
        geometry[k]
        for k in ("si_thickness", "passivation_thickness", "cfa_thickness", "lens_height")
    )
    bottom = -stack / 2
    silicon_top = bottom + geometry["si_thickness"]
    pass_top = silicon_top + geometry["passivation_thickness"]
    cfa_top = pass_top + geometry["cfa_thickness"]
    lens_top = cfa_top + geometry["lens_height"]
    return {
        "bottom": bottom,
        "silicon_top": silicon_top,
        "pass_top": pass_top,
        "cfa_top": cfa_top,
        "lens_top": lens_top,
        "length": stack + 2 + 2 * pml,
        "source": lens_top + 0.65,
        "reflection": lens_top + 0.3,
        "transmission": bottom - 0.3,
    }


def planar_tmm(
    bundle, wavelength_nm, channel="green", include_lens=False, angle_deg=0, polarization="s"
):
    """Independent coherent transfer-matrix reference using tangential admittance."""
    g = geometry_from_bundle(bundle)
    entries = [
        ("cfa", bundle["channels"][channel], g["cfa_thickness"]),
        ("passivation", bundle["materials"]["passivation"], g["passivation_thickness"]),
        ("silicon", bundle["materials"]["silicon"], g["si_thickness"]),
    ]
    if include_lens:
        entries.insert(0, ("lens", bundle["materials"]["lens"], g["lens_height"]))
    beta = np.sin(np.deg2rad(angle_deg))
    q0 = np.sqrt(1 - beta * beta)
    y0 = q0 if polarization == "s" else 1 / q0
    matrices = []
    total = np.eye(2, dtype=complex)
    for name, material, thickness in entries:
        n = material_nk(material, wavelength_nm)
        q = np.sqrt(n * n - beta * beta + 0j)
        admittance = q if polarization == "s" else n * n / q
        phase = 2 * np.pi * q * thickness / (wavelength_nm / 1000)
        matrix = np.array(
            [
                [np.cos(phase), -1j * np.sin(phase) / admittance],
                [-1j * admittance * np.sin(phase), np.cos(phase)],
            ]
        )
        matrices.append((name, matrix))
        total = total @ matrix
    boundary = total @ np.array([1, y0])
    reflection_amplitude = (y0 * boundary[0] - boundary[1]) / (y0 * boundary[0] + boundary[1])
    transmission_amplitude = 2 * y0 / (y0 * boundary[0] + boundary[1])
    vector = np.array([1 + reflection_amplitude, y0 * (1 - reflection_amplitude)])
    layers = {"lens": 0.0}
    for name, matrix in matrices:
        before = float(np.real(vector[0] * np.conj(vector[1])))
        vector = np.linalg.solve(matrix, vector)
        after = float(np.real(vector[0] * np.conj(vector[1])))
        layers[name] = float((before - after) / y0)
    return {
        "reflection": float(abs(reflection_amplitude) ** 2),
        "transmission": float(abs(transmission_amplitude) ** 2),
        "layer_absorption": layers,
        "geometry": "CFA + passivation + finite silicon, air/air; curved lens excluded",
    }


def periodic_lens_centers(geometry):
    """Periodic copies move lens centers only; collection regions remain fixed."""
    pitch = geometry["pitch"]
    width = 2 * pitch
    radius = (pitch - 2 * geometry["lens_edge_gap"]) / 2
    dx, dz = geometry["lens_shift_um"]
    centers = []
    for x, z in [
        (-pitch / 2, -pitch / 2),
        (pitch / 2, -pitch / 2),
        (-pitch / 2, pitch / 2),
        (pitch / 2, pitch / 2),
    ]:
        x = (x + dx + pitch) % width - pitch
        z = (z + dz + pitch) % width - pitch
        for ix in (-1, 0, 1):
            for iz in (-1, 0, 1):
                a, b = x + ix * width, z + iz * width
                if abs(a) < pitch + radius and abs(b) < pitch + radius:
                    centers.append((a, b))
    return centers


def regional_integrals(weighted, x, z):
    """Split center-boundary quadrature samples equally; avoid odd-grid site bias."""

    def negative_share(axis):
        axis = np.asarray(axis, dtype=float)
        return np.where(np.isclose(axis, 0, rtol=0, atol=1e-12), 0.5, (axis < 0).astype(float))

    nx, nz = negative_share(x), negative_share(z)
    return [
        float(np.sum(weighted * xm[:, None, None] * zm[None, None, :]))
        for xm, zm in [(nx, nz), (1 - nx, nz), (nx, 1 - nz), (1 - nx, 1 - nz)]
    ]


def validate_job_identity(job):
    body = {
        key: value
        for key, value in job.items()
        if key not in {"job_sha256", "job_id", "resource_estimate"}
    }
    if canonical_hash(body) != job["job_sha256"] or job["job_id"] != job["job_sha256"][:20]:
        raise ValueError("Job identity hash does not match immutable descriptor.")


def _meep_worker(job, bundle_override=None):
    import meep as mp

    mp.verbosity(0)
    validate_job_identity(job)
    bundle_path = Path(bundle_override or job["bundle_path"])
    if (
        file_hash(bundle_path) != job["bundle_file_sha256"]
        or file_hash(__file__) != job["solver_code_sha256"]
    ):
        raise ValueError("Hash-locked job does not match material bundle or solver code.")
    bundle = json.loads(bundle_path.read_text())
    g = job["geometry"]
    pos = _plane_positions(g, job["pml_um"])
    planar = job["role"] == "planar_calibration"
    planar_1d = planar and job["theta_deg"] == 0
    freq = 1000 / job["wavelength_nm"]
    theta, phi = np.deg2rad([job["theta_deg"], job["phi_deg"]])
    kx, kz = freq * np.sin(theta) * np.cos(phi), freq * np.sin(theta) * np.sin(phi)
    materials = {}
    for name, material in {**bundle["materials"], **bundle["channels"]}.items():
        n = material_nk(material, job["wavelength_nm"])
        epsilon = n.real**2 - n.imag**2
        if epsilon <= 0:
            raise ValueError(
                "Canonical material requires dispersive fitting; epsilon is not positive."
            )
        materials[name] = mp.Medium(
            epsilon=epsilon, D_conductivity=2 * np.pi * freq * (2 * n.real * n.imag) / epsilon
        )
    pitch, width = g["pitch"], 2 * g["pitch"]
    if planar_1d:
        cell = mp.Vector3(0, 0, pos["length"])
        source_center = mp.Vector3(0, 0, pos["source"])
        sources = [
            mp.Source(
                mp.GaussianSource(freq, fwidth=0.1 * freq), component=mp.Ex, center=source_center
            )
        ]
        boundary = [mp.PML(job["pml_um"])]
        kpoint = False
    elif planar:
        # A 2D Bloch-periodic slab is sufficient to test oblique s and p launches.
        width = 1.0
        cell = mp.Vector3(width, pos["length"], 0)
        sources = [
            mp.Source(
                mp.GaussianSource(freq, fwidth=0.1 * freq),
                component=mp.Ez if job["polarization"] == "s" else mp.Ex,
                center=mp.Vector3(0, pos["source"], 0),
                size=mp.Vector3(width, 0, 0),
                amp_func=lambda point: np.exp(2j * np.pi * kx * point.x),
            )
        ]
        boundary = [mp.PML(job["pml_um"], direction=mp.Y)]
        kpoint = mp.Vector3(kx, 0, 0)
    else:
        cell = mp.Vector3(width, pos["length"], width)
        # Tangential surface-current components launch the specified s/p wave.
        amplitudes = (
            [-np.sin(phi), np.cos(phi)]
            if job["polarization"] == "s"
            else [np.cos(phi), np.sin(phi)]
        )
        sources = []
        for component, amplitude in zip([mp.Ex, mp.Ez], amplitudes, strict=True):
            if abs(amplitude) > 1e-12:
                sources.append(
                    mp.Source(
                        mp.GaussianSource(freq, fwidth=0.1 * freq),
                        component=component,
                        amplitude=amplitude,
                        center=mp.Vector3(0, pos["source"], 0),
                        size=mp.Vector3(width, 0, width),
                        amp_func=lambda point: np.exp(2j * np.pi * (kx * point.x + kz * point.z)),
                    )
                )
        boundary = [mp.PML(job["pml_um"], direction=mp.Y)]
        kpoint = mp.Vector3(kx, 0, kz)

    def block(lo, hi, material, x=0, z=0, sx=None, sz=None):
        if planar_1d:
            return mp.Block(
                center=mp.Vector3(0, 0, (hi + lo) / 2),
                size=mp.Vector3(mp.inf, mp.inf, hi - lo),
                material=material,
            )
        return mp.Block(
            center=mp.Vector3(x, (hi + lo) / 2, z),
            size=mp.Vector3(sx or mp.inf, hi - lo, sz or mp.inf),
            material=material,
        )

    objects = []
    if planar:
        if job["planar_include_lens"]:
            objects.append(block(pos["cfa_top"], pos["lens_top"], materials["lens"]))
        objects.append(block(pos["pass_top"], pos["cfa_top"], materials[job["planar_channel"]]))
    else:
        # Full lower halves are overwritten by CFA/passivation/Si blocks below.
        diameter = pitch - 2 * g["lens_edge_gap"]
        if g["lens_height"] > 0:
            for x, z in periodic_lens_centers(g):
                objects.append(
                    mp.Ellipsoid(
                        center=mp.Vector3(x, pos["cfa_top"], z),
                        size=mp.Vector3(diameter, 2 * g["lens_height"], diameter),
                        material=materials["lens"],
                    )
                )
        for (x, z), channel in zip(
            [
                (-pitch / 2, -pitch / 2),
                (pitch / 2, -pitch / 2),
                (-pitch / 2, pitch / 2),
                (pitch / 2, pitch / 2),
            ],
            SITE_CHANNELS,
            strict=True,
        ):
            local_bottom = pos["cfa_top"] - g["cfa_thickness_by_channel_um"][channel]
            objects.append(
                block(local_bottom, pos["cfa_top"], materials[channel], x, z, pitch, pitch)
            )
            objects.append(
                block(
                    pos["silicon_top"], local_bottom, materials["passivation"], x, z, pitch, pitch
                )
            )
    if planar:
        objects.append(block(pos["silicon_top"], pos["pass_top"], materials["passivation"]))
    objects.append(block(pos["bottom"], pos["silicon_top"], materials["silicon"]))

    def build(with_stack):
        options = {
            "cell_size": cell,
            "boundary_layers": boundary,
            "sources": sources,
            "resolution": job["resolution_px_per_um"],
            "geometry": objects if with_stack else [],
            "force_complex_fields": True,
        }
        if not planar_1d:
            options["k_point"] = kpoint
        sim = mp.Simulation(**options)

        def monitor(coordinate):
            center = mp.Vector3(0, 0, coordinate) if planar_1d else mp.Vector3(0, coordinate, 0)
            size = mp.Vector3() if planar_1d else mp.Vector3(width, 0, 0 if planar else width)
            direction = mp.Z if planar_1d else mp.Y
            return sim.add_flux(
                freq, 0, 1, mp.FluxRegion(center=center, size=size, direction=direction, weight=-1)
            )

        monitors = {
            name: monitor(pos[name])
            for name in ["reflection", "cfa_top", "pass_top", "silicon_top", "transmission"]
        }
        # Finite Si bottom lies above transmission air; lossless air contributes zero.
        monitors["reflected"] = monitor(pos["reflection"])
        fields = None
        if with_stack and not planar:
            fields = sim.add_dft_fields(
                [mp.Ex, mp.Ey, mp.Ez],
                freq,
                0,
                1,
                center=mp.Vector3(0, (pos["bottom"] + pos["silicon_top"]) / 2, 0),
                size=mp.Vector3(width, g["si_thickness"], width),
                yee_grid=False,
            )
        return sim, monitors, fields

    start = time.monotonic()
    reference, ref_monitors, _ = build(False)
    reference.run(until_after_sources=job["after_source_time"])
    incident = float(mp.get_fluxes(ref_monitors["reflection"])[0])
    subtraction = reference.get_flux_data(ref_monitors["reflected"])
    if not np.isfinite(incident) or incident <= 0:
        raise ValueError("Empty-cell incident reference has no positive down-going power.")
    reference.reset_meep()
    sim, monitors, fields = build(True)
    sim.load_minus_flux_data(monitors["reflected"], subtraction)
    sim.run(until_after_sources=job["after_source_time"])
    flux = {key: float(mp.get_fluxes(value)[0]) / incident for key, value in monitors.items()}
    reflection, transmission = -flux["reflected"], flux["transmission"]
    layers = {
        "lens": flux["reflection"] - flux["cfa_top"],
        "cfa": flux["cfa_top"] - flux["pass_top"],
        "passivation": flux["pass_top"] - flux["silicon_top"],
        "silicon": flux["silicon_top"] - flux["transmission"],
    }
    raw_layers = dict(layers)
    variable_cfa = not planar and len(set(g["cfa_thickness_by_channel_um"].values())) > 1
    if variable_cfa:
        if material_nk(bundle["materials"]["passivation"], job["wavelength_nm"]).imag != 0:
            raise ValueError(
                "Variable CFA thickness currently requires lossless passivation for "
                "separate layer power."
            )
        # Nominal horizontal interface cuts the varied CFA. Its raw fluxes are retained;
        # lossless passivation contributes exactly zero material dissipation.
        layers["cfa"] = flux["cfa_top"] - flux["silicon_top"]
        layers["passivation"] = 0.0
    result = {
        "schema": "camerae2e_fdtd_solver_sample_v1",
        "raw_horizontal_plane_layer_absorption": raw_layers,
        "layer_separation_method": "lossless passivation with channel CFA boundary"
        if variable_cfa
        else "horizontal boundary flux differences",
        "job_id": job["job_id"],
        "job_sha256": job["job_sha256"],
        "role": job["role"],
        "wavelength_nm": job["wavelength_nm"],
        "theta_deg": job["theta_deg"],
        "phi_deg": job["phi_deg"],
        "polarization": job["polarization"],
        "reflection": reflection,
        "transmission": transmission,
        "layer_absorption": layers,
        "energy_balance_residual": reflection + transmission + sum(layers.values()) - 1,
        "incident_cell_power_meep": incident,
        "meep_version": mp.__version__,
        "worker_wall_seconds": time.monotonic() - start,
        "numerical_settings": {
            key: job[key] for key in ["resolution_px_per_um", "after_source_time", "pml_um"]
        },
        "estimate_only": True,
        "quantitative_ready": False,
    }
    if planar:
        expected = planar_tmm(
            bundle,
            job["wavelength_nm"],
            job["planar_channel"],
            angle_deg=job["theta_deg"],
            polarization=job["polarization"],
        )
        errors = [
            abs(reflection - expected["reflection"]),
            abs(transmission - expected["transmission"]),
        ]
        errors += [abs(layers[key] - expected["layer_absorption"][key]) for key in layers]
        result["independent_tmm"] = expected
        result["tmm_max_abs_error"] = float(max(errors))
        result["tmm_check_tolerance"] = 0.01
        result["tmm_check_passed"] = bool(max(errors) <= 0.01)
    else:
        components = [
            np.asarray(sim.get_dft_array(fields, component, 0))
            for component in [mp.Ex, mp.Ey, mp.Ez]
        ]
        intensity = sum(np.abs(component) ** 2 for component in components)
        x, _, z, weights = sim.get_array_metadata(dft_cell=fields)
        weighted = intensity * np.asarray(weights)
        regions = regional_integrals(weighted, x, z)
        total = sum(regions)
        if not np.isfinite(total) or total <= 0:
            raise ValueError("No finite Si volume field response.")
        result["absorption_cell_fraction"] = [
            layers["silicon"] * value / total for value in regions
        ]
        result["regional_normalization"] = (
            "Si |E|² volume fractions weighted by Meep integration "
            "weights, scaled to independently monitored total Si layer absorption"
        )
        result["region_volume_weights_raw"] = regions
        silicon_n = material_nk(bundle["materials"]["silicon"], job["wavelength_nm"])
        eps_imag = 2 * silicon_n.real * silicon_n.imag
        dissipative = np.asarray(regions) * np.pi * freq * eps_imag / incident
        result["dissipative_integral_diagnostic"] = {
            "formula": "pi*f*imag(epsilon)*integral(abs(E_DFT)^2 dV) / incident_DFT_flux",
            "raw_site_cell_fraction": dissipative.tolist(),
            "raw_total_cell_fraction": float(dissipative.sum()),
            "flux_total_cell_fraction": layers["silicon"],
            "raw_minus_flux": float(dissipative.sum() - layers["silicon"]),
            "flux_to_raw_scale": float(layers["silicon"] / dissipative.sum())
            if dissipative.sum() > 0
            else None,
            "validated": False,
            "scope": (
                "Unscaled dissipative diagnostic; DFT quadrature and boundary integration "
                "require convergence evidence. Primary sites preserve flux total using "
                "relative uniform-loss Si volume integrals."
            ),
        }

    sim.reset_meep()
    return result


@contextmanager
def _local_job_lock(output_dir):
    with (Path(output_dir) / ".local_worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another local campaign job is running.") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_job(
    manifest_path,
    job_id=None,
    *,
    role="planar_calibration",
    solver_python=None,
    max_wall_seconds=120,
    max_rss_gib=24,
    cores=4,
):
    """Execute exactly one hash-locked job with hard process-group resource limits."""
    import psutil

    if not 0 < max_wall_seconds <= LOCAL_LIMITS["wall_seconds"]:
        raise ValueError("Local wall limit must be positive and no more than two hours.")
    if not 0 < max_rss_gib <= LOCAL_LIMITS["memory_gib"] or not 1 <= cores <= 4:
        raise ValueError("Local resource policy permits at most 24 GiB and four cores.")
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest["solver_code_sha256"] != file_hash(manifest["solver_snapshot"]):
        raise ValueError("Frozen solver code changed after campaign planning.")
    candidates = [
        job
        for job in manifest["jobs"]
        if (job["job_id"] == job_id if job_id else job["role"] == role)
    ]
    if not candidates:
        raise ValueError("No matching campaign job.")
    for job in candidates:
        destination = Path(manifest["output_dir"]) / "jobs" / job["job_id"]
        result_path = destination / "result.json"
        if result_path.exists():
            saved = json.loads(result_path.read_text())
            if saved.get("job_sha256") != job["job_sha256"]:
                raise ValueError("Existing result has a different job hash.")
            if job_id:
                return saved
            continue
        break
    else:
        return {"status": "all_selected_jobs_already_completed"}
    if job["resource_estimate"]["rss_gib_estimate"] > max_rss_gib:
        return {
            "status": "external_resources_required",
            "job_id": job["job_id"],
            "resource_estimate": job["resource_estimate"],
        }
    destination.mkdir(parents=True, exist_ok=True)
    job_path = destination / "job.json"
    write_json(job_path, job)
    environment = os.environ.copy()
    environment.update(
        {
            name: str(cores)
            for name in [
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
                "MKL_NUM_THREADS",
            ]
        }
    )
    command = [
        solver_python or manifest["solver_python"],
        job["solver_path"],
        "worker",
        "--job",
        str(job_path),
        "--output",
        str(result_path),
    ]
    start = time.monotonic()
    peak = 0
    termination = None
    with _local_job_lock(manifest["output_dir"]), (destination / "worker.log").open("w") as log:
        process = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, env=environment, start_new_session=True
        )
        watched = psutil.Process(process.pid)
        while process.poll() is None:
            try:
                members = [watched] + watched.children(recursive=True)
                rss = sum(member.memory_info().rss for member in members if member.is_running())
                peak = max(peak, rss)
            except psutil.Error:
                pass
            if peak > max_rss_gib * 1024**3:
                termination = "memory_limit"
            if time.monotonic() - start > max_wall_seconds:
                termination = "wall_time_limit"
            if termination:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                break
            time.sleep(0.2)
        process.wait()
    observation = {
        "job_id": job["job_id"],
        "status": termination
        or (
            "completed_unverified" if process.returncode == 0 and result_path.exists() else "failed"
        ),
        "returncode": process.returncode,
        "wall_seconds": time.monotonic() - start,
        "peak_rss_bytes": peak,
        "peak_rss_gib": peak / 1024**3,
        "cores_limit": cores,
        "wall_seconds_limit": max_wall_seconds,
        "rss_gib_limit": max_rss_gib,
        "command": command,
        "result_path": str(result_path),
        "log_path": str(destination / "worker.log"),
    }
    write_json(destination / "resource_observation.json", observation)
    return observation


def prepare_external(manifest_path, output_dir):
    """Copy an executable offline bundle; launch remains an explicit external action.

    Original descriptors and IDs stay unchanged. The worker resolves relocated
    material files using --bundle-dir, verifies their hashes, and emits original
    job IDs so import requires no provenance rewrite.
    """
    manifest = json.loads(Path(manifest_path).read_text())
    output = Path(output_dir).resolve()
    existing_manifest = output / "campaign.json"
    if existing_manifest.exists() and json.loads(existing_manifest.read_text()) != manifest:
        raise ValueError("External campaign changed; choose a new output directory.")
    output.mkdir(parents=True, exist_ok=True)
    if file_hash(manifest["solver_snapshot"]) != manifest["solver_code_sha256"]:
        raise ValueError("Frozen solver snapshot hash mismatch.")
    snapshot = output / "runner_snapshot.py"
    if snapshot.exists() and file_hash(snapshot) != manifest["solver_code_sha256"]:
        raise ValueError("External bundle already contains different solver code.")
    shutil.copyfile(manifest["solver_snapshot"], snapshot)
    write_json(output / "campaign.json", manifest)
    unique_bundles = {job["bundle_path"]: job["bundle_file_sha256"] for job in manifest["jobs"]}
    for original, expected_hash in unique_bundles.items():
        if file_hash(original) != expected_hash:
            raise ValueError("Source material bundle hash changed before external preparation.")
        path = output / "bundles" / Path(original).name
        path.parent.mkdir(exist_ok=True)
        if path.exists() and file_hash(path) != expected_hash:
            raise ValueError("Relocated bundle filename collision with different content.")
        shutil.copyfile(original, path)
    (output / "results").mkdir(exist_ok=True)
    launcher = output / "run_job.sh"
    launcher.write_text("""#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
: "${MEEP_PYTHON:=python3}"
: "${OMP_NUM_THREADS:=4}"
export OMP_NUM_THREADS
exec "$MEEP_PYTHON" "$HERE/runner_snapshot.py" worker \\
  --manifest "$HERE/campaign.json" --job-id "$1" \\
  --bundle-dir "$HERE/bundles" --output "$HERE/results/$1.json"
""")
    launcher.chmod(0o755)
    job_rows = []
    for job in manifest["jobs"]:
        validate_job_identity(job)
        job_rows.append(
            {
                "job_id": job["job_id"],
                "role": job["role"],
                "report_code": job["report_code"],
                "estimated_rss_gib": job["resource_estimate"]["rss_gib_estimate"],
                "command": "./run_job.sh " + shlex.quote(job["job_id"]),
                "status": "pending_external_execution",
            }
        )
    (output / "jobs.jsonl").write_text("".join(json.dumps(row) + "\n" for row in job_rows))
    instructions = (
        "This package provisions nothing and starts no jobs. Copy the entire directory "
        "to an existing compute server. Use an installed compatible Meep Python via "
        "MEEP_PYTHON; request scheduler memory above each jobs.jsonl estimate. Run one "
        "selected command under your scheduler. Local 24 GiB guard is deliberately "
        "absent from this explicit external worker path. Keep campaign.json, "
        "runner_snapshot.py and bundles unchanged. Copy results/*.json back and use "
        "import-results with the original local campaign. Execution completion never "
        "implies validated optical response.\n"
    )
    (output / "README.txt").write_text(instructions)
    value = {
        "schema": "camerae2e_fdtd_external_bundle_v1",
        "manifest_sha256": manifest["manifest_sha256"],
        "solver_code_sha256": manifest["solver_code_sha256"],
        "job_count": len(job_rows),
        "status": "pending_external_execution",
        "portable_path_resolution": (
            "--bundle-dir relocates files without changing hash-locked original job descriptors"
        ),
        "result_identity": "Original job_id and job_sha256, verified on import",
    }
    write_json(output / "external_bundle.json", value)
    return value


def import_results(manifest_path, results_dir):
    manifest = json.loads(Path(manifest_path).read_text())
    by_id = {job["job_id"]: job for job in manifest["jobs"]}
    imported = 0
    for path in sorted(Path(results_dir).glob("*.json")):
        sample = json.loads(path.read_text())
        job = by_id.get(sample.get("job_id"))
        if job is None or sample.get("job_sha256") != job["job_sha256"]:
            raise ValueError("Imported result has unknown or changed job identity.")
        if sample.get("schema") != "camerae2e_fdtd_solver_sample_v1":
            raise ValueError("Imported result is not a solver sample.")
        for key in ("wavelength_nm", "theta_deg", "phi_deg", "polarization", "role"):
            if sample.get(key) != job[key]:
                raise ValueError("Imported result coordinates do not match job.")
        destination = Path(manifest["output_dir"]) / "jobs" / job["job_id"] / "result.json"
        if destination.exists() and json.loads(destination.read_text()) != sample:
            raise ValueError("Imported result conflicts with an existing immutable result.")
        write_json(destination, sample)
        imported += 1
    return {
        "imported": imported,
        "quantitative_ready": False,
        "note": "Strict assembly and runtime gates remain required.",
    }


def campaign_status(manifest_path):
    manifest = json.loads(Path(manifest_path).read_text())
    roles = {}
    for job in manifest["jobs"]:
        counts = roles.setdefault(job["role"], {"planned": 0, "completed": 0, "pending": 0})
        counts["planned"] += 1
        path = Path(manifest["output_dir"]) / "jobs" / job["job_id"] / "result.json"
        if path.exists() and json.loads(path.read_text()).get("job_sha256") == job["job_sha256"]:
            counts["completed"] += 1
        else:
            counts["pending"] += 1
    return {
        "schema": "camerae2e_fdtd_campaign_status_v1",
        "roles": roles,
        "production_complete": roles.get("production", {}).get("pending", 1) == 0,
        "quantitative_ready": False,
        "reason": "Convergence and interpolation gates require evidence.",
    }


def _sample_vector(sample):
    return np.asarray(
        [
            *sample.get("absorption_cell_fraction", []),
            sample["reflection"],
            sample["transmission"],
            *sample["layer_absorption"].values(),
        ],
        dtype=float,
    )


def _relative_error(first, second):
    """Maximum per-component error; never normalize by a different strong channel."""
    first, second = np.asarray(first, dtype=float), np.asarray(second, dtype=float)
    return float(np.max(np.abs(first - second) / np.maximum(np.abs(second), 1e-12)))


def response_errors(first, second, first_center=None, second_center=None):
    """Mixed criterion is applied to native response, before electrical efficiency."""
    a, b = 4 * np.asarray(first, dtype=float)[:4], 4 * np.asarray(second, dtype=float)[:4]
    if a.shape != (4,) or b.shape != (4,) or not np.all(np.isfinite([a, b])):
        raise ValueError("Convergence comparison requires four finite site responses.")
    difference = np.abs(a - b)
    strong = np.abs(b) >= 0.01
    relative = np.divide(difference, np.abs(b), out=np.zeros(4), where=strong)
    weak = np.where(strong, 0, difference)
    result = {
        "max_relative_error": float(relative.max()),
        "max_weak_response_abs_error": float(weak.max()),
        "native_response_first": a.tolist(),
        "native_response_reference": b.tolist(),
        "strong_response_mask": strong.tolist(),
        "max_angular_ratio_relative_error": None,
        "max_angular_ratio_weak_response_abs_error": None,
    }
    if first_center is not None and second_center is not None:
        ac, bc = (
            4 * np.asarray(first_center, dtype=float)[:4],
            4 * np.asarray(second_center, dtype=float)[:4],
        )
        if not np.all(np.isfinite([ac, bc])):
            raise ValueError("Normal reference response must be finite.")
        stable = strong & (np.abs(bc) >= 0.01) & (np.abs(ac) >= 0.01)
        ar = np.divide(a, ac, out=np.zeros(4), where=stable)
        br = np.divide(b, bc, out=np.zeros(4), where=stable)
        ratio_error = np.divide(np.abs(ar - br), np.abs(br), out=np.zeros(4), where=stable)
        result.update(
            max_angular_ratio_relative_error=float(ratio_error.max()),
            max_angular_ratio_weak_response_abs_error=float(np.where(stable, 0, difference).max()),
            stable_angular_ratio_mask=stable.tolist(),
        )
    return result


def _comparison_evidence(comparisons, complete, **extra):
    keys = (
        "max_relative_error",
        "max_angular_ratio_relative_error",
        "max_weak_response_abs_error",
        "max_angular_ratio_weak_response_abs_error",
    )
    available = (
        complete
        and bool(comparisons)
        and all(row[key] is not None for row in comparisons for key in keys)
    )
    evidence = {
        key: max(row[key] for row in comparisons)
        if comparisons and all(row[key] is not None for row in comparisons)
        else None
        for key in keys
    }
    evidence.update(
        policy="native_response_mixed_v1",
        sample_count=len(comparisons),
        tolerance=0.03,
        weak_response_threshold=0.01,
        weak_response_absolute_tolerance=0.001,
        comparisons=comparisons,
        all_required_jobs_completed=complete,
        **extra,
    )
    passed = bool(
        available
        and evidence[keys[0]] <= 0.03
        and evidence[keys[1]] <= 0.03
        and evidence[keys[2]] <= 0.001
        and evidence[keys[3]] <= 0.001
    )
    return evidence, passed


def assemble_response(manifest_path, report_code):
    """Assemble only real sample files; absent or failed checks stay unverified."""
    manifest = json.loads(Path(manifest_path).read_text())
    record = next(record for record in manifest["sensors"] if record["report_code"] == report_code)
    bundle = json.loads(Path(record["bundle_path"]).read_text())
    jobs = [job for job in manifest["jobs"] if job["report_code"] == report_code]
    loaded = {}
    for job in jobs:
        path = Path(manifest["output_dir"]) / "jobs" / job["job_id"] / "result.json"
        if path.exists():
            sample = json.loads(path.read_text())
            if sample.get("job_sha256") != job["job_sha256"]:
                raise ValueError("Result does not match hash-locked job.")
            loaded[job["job_id"]] = sample
    production = [job for job in jobs if job["role"] == "production"]
    samples = [loaded[job["job_id"]] for job in production if job["job_id"] in loaded]
    all_samples = len(samples) == len(production)
    energy = []
    passive = True
    for sample in samples:
        silicon = sum(sample["absorption_cell_fraction"])
        total = (
            sample["reflection"] + sample["transmission"] + sum(sample["layer_absorption"].values())
        )
        energy.append(abs(total - 1))
        passive &= bool(
            np.all(np.isfinite(_sample_vector(sample)))
            and np.all(_sample_vector(sample) >= -1e-10)
            and abs(silicon - sample["layer_absorption"]["silicon"]) <= 1e-10
        )
    gates = {key: False for key in GATES}
    evidence = {
        "energy_balance": {
            "max_abs_error": max(energy) if energy else None,
            "sample_count": len(samples),
            "tolerance": 0.01,
            "passive": passive,
            "passivity_roundoff_tolerance": 1e-10,
        }
    }
    gates["energy_balance"] = all_samples and passive and bool(energy) and max(energy) <= 0.01
    for role, setting in [
        ("spatial", "resolution_px_per_um"),
        ("time", "after_source_time"),
        ("pml", "pml_um"),
    ]:
        selected = [job for job in jobs if job["role"] == role]
        complete = bool(selected) and all(job["job_id"] in loaded for job in selected)
        if len({job["job_id"] for job in selected}) != len(selected):
            complete = False
        groups = {}
        centers = {}
        for job in selected:
            if job["job_id"] not in loaded:
                continue
            key = (job["wavelength_nm"], job["theta_deg"], job["phi_deg"], job["polarization"])
            groups.setdefault(key, []).append(job)
            if job["theta_deg"] == 0:
                centers[(job["wavelength_nm"], job["polarization"], job[setting])] = loaded[
                    job["job_id"]
                ]
        comparisons = []
        for (wave, theta, phi, pol), group in groups.items():
            group.sort(key=lambda item: item[setting])
            for first, second in zip(group, group[1:], strict=False):
                a, b = loaded[first["job_id"]], loaded[second["job_id"]]
                ac = centers.get((wave, pol, first[setting]))
                bc = centers.get((wave, pol, second[setting]))
                if first[setting] >= second[setting] or first["job_id"] == second["job_id"]:
                    complete = False
                    continue
                comparison = response_errors(
                    a["absorption_cell_fraction"],
                    b["absorption_cell_fraction"],
                    None if ac is None else ac["absorption_cell_fraction"],
                    None if bc is None else bc["absorption_cell_fraction"],
                )
                comparison.update(
                    wavelength_nm=wave,
                    theta_deg=theta,
                    phi_deg=phi,
                    polarization=pol,
                    settings=[first[setting], second[setting]],
                )
                comparisons.append(comparison)
        expected_levels = 3 if role == "spatial" else 2
        complete &= bool(groups) and all(
            len(group) == expected_levels and len({j[setting] for j in group}) == expected_levels
            for group in groups.values()
        )
        evidence[role], gates[role] = _comparison_evidence(
            comparisons, complete, required_job_count=len(selected)
        )
    # Interpolation is evaluated against off-grid FDTD, never against the grid itself.
    checks = [job for job in jobs if job["role"] == "interpolation"]
    complete = all_samples and all(job["job_id"] in loaded for job in checks)
    comparisons = []
    if complete:
        grid = {}
        for sample in samples:
            key = (sample["wavelength_nm"], sample["theta_deg"], sample["phi_deg"])
            grid.setdefault(key, []).append(_sample_vector(sample))
        grid = {key: np.mean(values, axis=0) for key, values in grid.items()}
        waves = sorted({key[0] for key in grid})
        thetas = sorted({key[1] for key in grid})
        phis = sorted({key[2] for key in grid})
        periodic_phis = phis + [phis[0] + 360]

        def bracket(axis, value):
            hi = int(np.searchsorted(axis, value))
            hi = min(hi, len(axis) - 1)
            lo = max(0, hi - 1)
            if value == axis[hi]:
                return [(axis[hi], 1.0)]
            weight = (value - axis[lo]) / (axis[hi] - axis[lo])
            return [(axis[lo], 1 - weight), (axis[hi], weight)]

        def interpolate(wave, theta, phi):
            terms = []
            for w, ww in bracket(waves, wave):
                for t, tw in bracket(thetas, theta):
                    if t == 0:
                        terms.append(grid[(w, 0.0, 0.0)] * ww * tw)
                        continue
                    for p, pw in bracket(periodic_phis, phi % 360):
                        terms.append(grid[(w, t, float(p % 360))] * ww * tw * pw)
            return sum(terms)

        observed = {}
        for job in checks:
            key = (job["wavelength_nm"], job["theta_deg"], job["phi_deg"])
            observed.setdefault(key, []).append(_sample_vector(loaded[job["job_id"]]))
        observed = {key: np.mean(values, axis=0) for key, values in observed.items()}
        for (wave, theta, phi), actual in observed.items():
            predicted = interpolate(wave, theta, phi)
            actual_center = observed[(wave, 0.0, 0.0)][:4]
            predicted_center = interpolate(wave, 0, 0)[:4]
            comparison = response_errors(predicted, actual, predicted_center, actual_center)
            comparison.update(wavelength_nm=wave, theta_deg=theta, phi_deg=phi)
            comparisons.append(comparison)
    evidence["interpolation"], gates["interpolation"] = _comparison_evidence(comparisons, complete)
    calibration_jobs = [job for job in jobs if job["role"] == "planar_calibration"]
    calibration = [loaded[j["job_id"]] for j in calibration_jobs if j["job_id"] in loaded]
    # An incomplete or normal-only calibration cohort cannot validate oblique production.
    required_calibration = {
        (w, t, p) for w in (400.0, 550.0, 700.0) for t, p in [(0.0, "s"), (40.0, "s"), (40.0, "p")]
    }
    covered = {(j["wavelength_nm"], j["theta_deg"], j["polarization"]) for j in calibration_jobs}
    planar_pass = bool(
        required_calibration <= covered
        and len(calibration) == len(calibration_jobs)
        and all(
            s.get("tmm_check_passed", False) and s.get("tmm_max_abs_error", 1) <= 0.01
            for s in calibration
        )
    )
    ready = all(gates.values()) and planar_pass
    return {
        "schema": SCHEMA,
        "sensor_id": record["sensor_id"],
        "report_code": report_code,
        "pixel_pitch_um": record["geometry"]["pitch"],
        "cell_pixels": [2, 2],
        "site_names": SITE_NAMES,
        "material_bundle_sha256": bundle["material_bundle_sha256"],
        "geometry_sha256": bundle["geometry_sha256"],
        "solver_geometry_sha256": canonical_hash(record["geometry"]),
        "solver_code_sha256": manifest["solver_code_sha256"],
        "estimate_only": True,
        "azimuth_periodic": True,
        "samples": samples,
        "expected_sample_count": len(production),
        "completed_sample_count": len(samples),
        "provenance": {
            "canonical_material_bundle": record["bundle_path"],
            "material_sources": bundle["provenance"],
            "solver_geometry": record["geometry"],
            "axes": {
                "x": "columns increasing, Meep x",
                "y": "rows increasing, Meep z",
                "propagation": "toward negative Meep y",
            },
            "normalization": "whole 2x2 cell incident normal power in an empty reference cell",
            "regional_response": "full-pixel Si absorption, not split-PD collection current",
            "electrical_collection": "not included; optical absorption is not external QE",
        },
        "validation": {
            "status": "passed" if ready else "unverified",
            "policy": "native_response_mixed_v1",
            "planar_calibration_completed": len(calibration),
            "planar_calibration_required": len(calibration_jobs),
            "refinement": {
                "status": "required_refinement"
                if complete and not gates["interpolation"]
                else "pending_evidence"
                if not complete
                else "passed",
                "next_steps": {
                    key: max(floor, value / 2)
                    for key, value, floor in zip(
                        ("wavelength_step_nm", "theta_step_deg", "phi_step_deg"),
                        [
                            manifest.get("sampling_steps", {}).get(key, default)
                            for key, default in [
                                ("wavelength_step_nm", 10),
                                ("theta_step_deg", 5),
                                ("phi_step_deg", 45),
                            ]
                        ],
                        (5, 2.5, 11.25),
                        strict=True,
                    )
                },
                "action": (
                    "Use plan with explicit step options in a new directory; if already at "
                    "minimum steps and failing, validation remains failed and further "
                    "numerical/model investigation is required."
                ),
            },
            "gates": gates,
            "evidence": evidence,
            "planar_tmm_passed": planar_pass,
            "scope": (
                "minimum wavelength and maximum angle convergence anchors; off-grid interpolation"
            ),
            "missing_production_samples": len(production) - len(samples),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--bundles", nargs="+", required=True)
    plan.add_argument("--output-dir", required=True)
    plan.add_argument("--resolution", type=int, default=80)
    plan.add_argument("--sampling", choices=["initial", "final", "refined"], default="final")
    for name in ("wavelength-step-nm", "theta-step-deg", "phi-step-deg"):
        plan.add_argument("--" + name, type=float)
    external = sub.add_parser("prepare-external")
    external.add_argument("--manifest", required=True)
    external.add_argument("--output-dir", required=True)
    imports = sub.add_parser("import-results")
    imports.add_argument("--manifest", required=True)
    imports.add_argument("--results-dir", required=True)
    run = sub.add_parser("run")
    run.add_argument("--manifest", required=True)
    run.add_argument("--job-id")
    run.add_argument("--role", default="planar_calibration")
    run.add_argument("--max-wall-seconds", type=float, default=120)
    run.add_argument("--max-rss-gib", type=float, default=24)
    run.add_argument("--cores", type=int, default=4)
    run.add_argument("--solver-python")
    status = sub.add_parser("status")
    status.add_argument("--manifest", required=True)
    merge = sub.add_parser("merge")
    merge.add_argument("--manifest", required=True)
    merge.add_argument("--report-code", required=True)
    merge.add_argument("--output", required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--job")
    worker.add_argument("--manifest")
    worker.add_argument("--job-id")
    worker.add_argument("--bundle-dir")
    worker.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.action == "plan":
        value = plan_campaign(
            args.bundles,
            args.output_dir,
            resolution=args.resolution,
            sampling=args.sampling,
            wavelength_step_nm=args.wavelength_step_nm,
            theta_step_deg=args.theta_step_deg,
            phi_step_deg=args.phi_step_deg,
        )
        value = {
            "manifest": str(Path(args.output_dir).resolve() / "campaign.json"),
            "production_jobs": value["production_jobs"],
            "jobs": len(value["jobs"]),
        }
    elif args.action == "prepare-external":
        value = prepare_external(args.manifest, args.output_dir)
    elif args.action == "import-results":
        value = import_results(args.manifest, args.results_dir)
    elif args.action == "run":
        value = run_job(
            args.manifest,
            args.job_id,
            role=args.role,
            solver_python=args.solver_python,
            max_wall_seconds=args.max_wall_seconds,
            max_rss_gib=args.max_rss_gib,
            cores=args.cores,
        )
    elif args.action == "merge":
        value = assemble_response(args.manifest, args.report_code)
        write_json(args.output, value)
    elif args.action == "worker":
        if args.job:
            job = json.loads(Path(args.job).read_text())
        elif args.manifest and args.job_id:
            manifest = json.loads(Path(args.manifest).read_text())
            job = next(j for j in manifest["jobs"] if j["job_id"] == args.job_id)
        else:
            parser.error("worker requires --job or --manifest with --job-id")
        override = (
            Path(args.bundle_dir) / Path(job["bundle_path"]).name if args.bundle_dir else None
        )
        value = _meep_worker(job, override)
        write_json(args.output, value)
    else:
        value = campaign_status(args.manifest)
    print(json.dumps(value, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
