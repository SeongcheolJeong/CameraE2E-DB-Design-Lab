"""Bounded normal-incidence planar optical characterization through a fixed Meep CLI.

Import and request validation do not load Meep. This file also runs directly under
the separately configured Meep Python 3.10 environment. Outputs are optical power
fractions; no collected-electron QE or electrical sensor parameters are inferred.
"""

from __future__ import annotations

import argparse
import atexit
import copy
import hashlib
import json
import math
import platform
import sys
from pathlib import Path

SCHEMA = "meep_planar_stack_v1"
ALGORITHM = "meep_planar_optical_absorption_v1"
MAX_REQUEST_BYTES = 256 * 1024
MAX_CELL_STEPS = 100_000_000
PASSIVITY_TOLERANCE = 1e-6
EFFECTS = ["planar_layer_interference", "material_optical_absorption"]


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _keys(value, expected, path):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"{path} requires exactly {sorted(expected)}")


def _number(value, minimum, maximum, path):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(path + " requires a finite number; no coercion")
    if not minimum <= value <= maximum:
        raise ValueError(f"{path} must be in [{minimum}, {maximum}]")
    return float(value)


def _text(value, path):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError(path + " requires a nonempty string of at most 1024 characters")


def request_template():
    return {
        "schema": SCHEMA,
        "wavelength_nm": [550.0],
        "ambient_index": 1.0,
        "incidence_deg": 0.0,
        "polarization": "linear_x",
        "layers": [
            {
                "name": "Declared synthetic lossy dielectric",
                "thickness_nm": 200.0,
                "optical_constants": {"n": 2.0, "k": 0.1},
                "provenance": {
                    "source_kind": "estimated",
                    "source_uri": "fixture://planar-dielectric-n2-k0.1",
                    "license": "MIT",
                    "method_and_version": "declared_uniform_material_v1",
                },
            }
        ],
        "simulation": {
            "resolution_px_per_um": 80,
            "pml_thickness_um": 0.8,
            "air_gap_um": 1.0,
            "after_source_time": 40.0,
            "source_bandwidth_fraction": 0.2,
            "convergence_tolerance": 0.01,
        },
    }


def validate_request(request):
    """Return a frozen-by-copy validated request without importing the backend."""
    _keys(request, request_template(), "request")
    if request["schema"] != SCHEMA:
        raise ValueError("Unsupported optical characterization schema")
    if _number(request["ambient_index"], 1, 1, "ambient_index") != 1:
        raise ValueError("Only air on both sides is supported")
    _number(request["incidence_deg"], 0, 0, "incidence_deg")
    if request["polarization"] != "linear_x":
        raise ValueError("Only normal-incidence linear_x polarization is supported")
    waves = request["wavelength_nm"]
    if not isinstance(waves, list) or not 1 <= len(waves) <= 5:
        raise ValueError("Explicit wavelength_nm requires 1 to 5 samples")
    for wave in waves:
        _number(wave, 350, 1100, "wavelength_nm")
    if any(b <= a for a, b in zip(waves, waves[1:], strict=False)):
        raise ValueError("Wavelength samples must be strictly increasing")
    layers = request["layers"]
    if not isinstance(layers, list) or not 1 <= len(layers) <= 8:
        raise ValueError("Planar stack requires 1 to 8 explicit layers")
    indices, thicknesses, names = [], [], []
    for i, layer in enumerate(layers):
        prefix = f"layers[{i}]"
        _keys(layer, {"name", "thickness_nm", "optical_constants", "provenance"}, prefix)
        _text(layer["name"], prefix + ".name")
        names.append(layer["name"])
        thicknesses.append(_number(layer["thickness_nm"], 20, 5000, prefix + ".thickness_nm"))
        p = layer["provenance"]
        _keys(
            p,
            {"source_kind", "source_uri", "license", "method_and_version"},
            prefix + ".provenance",
        )
        for key, value in p.items():
            _text(value, prefix + ".provenance." + key)
        if p["source_kind"] not in {"design", "measured", "literature", "estimated"}:
            raise ValueError(
                "Layer provenance must state design, measured, literature or estimated"
            )
        constants = layer["optical_constants"]
        if isinstance(constants, dict) and set(constants) == {"n", "k"}:
            ns, ks = [constants["n"]] * len(waves), [constants["k"]] * len(waves)
        else:
            _keys(constants, {"wavelength_nm", "n", "k"}, prefix + ".optical_constants")
            if constants["wavelength_nm"] != waves:
                raise ValueError("Material coordinates must exactly match requested wavelengths")
            ns, ks = constants["n"], constants["k"]
            if not all(isinstance(v, list) and len(v) == len(waves) for v in (ns, ks)):
                raise ValueError("Material n and k must cover every requested wavelength")
        for n, k in zip(ns, ks, strict=True):
            _number(n, 1, 6, prefix + ".n")
            _number(k, 0, 2, prefix + ".k")
            if n * n - k * k <= 0:
                raise ValueError(
                    "Only passive positive-real-permittivity dielectrics are supported"
                )
        indices.append(list(ns))
    if len(set(names)) != len(names):
        raise ValueError("Layer names must be unique")
    if sum(thicknesses) > 10_000:
        raise ValueError("Total layer thickness exceeds 10 um")
    s = request["simulation"]
    _keys(s, request_template()["simulation"], "simulation")
    resolution = _number(s["resolution_px_per_um"], 16, 400, "resolution_px_per_um")
    if not resolution.is_integer():
        raise ValueError("Grid resolution must be an integer")
    _number(s["pml_thickness_um"], 0.3, 3, "pml_thickness_um")
    _number(s["air_gap_um"], 0.6, 3, "air_gap_um")
    _number(s["after_source_time"], 20, 200, "after_source_time")
    _number(s["source_bandwidth_fraction"], 0.1, 0.3, "source_bandwidth_fraction")
    _number(s["convergence_tolerance"], 0.0001, 0.02, "convergence_tolerance")
    if min(thicknesses) * 0.001 * resolution < 4:
        raise ValueError("Each layer needs at least four baseline grid cells")
    for ns in indices:
        if min(w * 0.001 * resolution / n for w, n in zip(waves, ns, strict=True)) < 8:
            raise ValueError("Every material wavelength needs at least eight baseline grid cells")
    result = copy.deepcopy(request)
    if estimate_resources(result)["estimated_cell_steps"] > MAX_CELL_STEPS:
        raise ValueError("Bounded characterization exceeds the cell-step budget")
    json.dumps(result, allow_nan=False)
    return result


def configurations(request):
    baseline = copy.deepcopy(request["simulation"])
    result = [{"name": "baseline", **baseline}]
    for name, key, factor in (
        ("resolution_refined", "resolution_px_per_um", 2),
        ("pml_refined", "pml_thickness_um", 1.5),
        ("time_refined", "after_source_time", 2),
    ):
        result.append({"name": name, **baseline, key: baseline[key] * factor})
    return result


def estimate_resources(request):
    thickness = sum(layer["thickness_nm"] for layer in request["layers"]) * 0.001
    total = 0
    max_cells = 0
    for c in configurations(request):
        cells = math.ceil(
            (thickness + 2 * c["air_gap_um"] + 2 * c["pml_thickness_um"])
            * c["resolution_px_per_um"]
        )
        max_cells = max(max_cells, cells)
        for wave in request["wavelength_nm"]:
            source_duration = 10 * wave * 0.001 / c["source_bandwidth_fraction"]
            steps = math.ceil(
                (source_duration + c["after_source_time"]) * 2 * c["resolution_px_per_um"]
            )
            total += 2 * cells * steps  # empty reference + material stack
    return {
        "dimensions": 1,
        "simulation_count": len(request["wavelength_nm"]) * 8,
        "max_cells": max_cells,
        "estimated_cell_steps": total,
    }


def _backend():
    import meep as mp

    # Keep the fixed CLI's stdout machine-readable; scientific diagnostics are retained below.
    atexit.unregister(mp.report_elapsed_time)
    mp.verbosity(0)
    if mp.__version__ != "1.33.0" or mp.with_mpi() or mp.count_processors() != 1:
        raise ValueError("This characterized adapter requires serial Meep 1.33.0")
    return mp


def backend_probe():
    import numpy as np

    mp = _backend()
    return {
        "available": True,
        "algorithm": ALGORITHM,
        "meep_version": mp.__version__,
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "mpi_enabled": bool(mp.with_mpi()),
        "processor_count": mp.count_processors(),
        "dimensions": 1,
        "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "meep_python_module_sha256": hashlib.sha256(Path(mp.__file__).read_bytes()).hexdigest(),
        "meep_compiled_extension_sha256": hashlib.sha256(
            Path(mp._meep.__file__).read_bytes()
        ).hexdigest(),
    }


def _optical_constants(layer, wave_index):
    material = layer["optical_constants"]
    if "wavelength_nm" in material:
        return material["n"][wave_index], material["k"][wave_index]
    return material["n"], material["k"]


def _simulate_pair(mp, request, config, wave_index):
    frequency = 1000.0 / request["wavelength_nm"][wave_index]
    thickness = sum(layer["thickness_nm"] for layer in request["layers"]) * 0.001
    gap, pml = config["air_gap_um"], config["pml_thickness_um"]
    start, end = -thickness / 2, thickness / 2
    source_z, reflect_z, transmit_z = start - 0.7 * gap, start - 0.35 * gap, end + 0.35 * gap
    boundaries = [start]
    geometry, material_values = [], []
    position = start
    for layer in request["layers"]:
        i = len(material_values)
        n, k = _optical_constants(layer, wave_index)
        eps_real, eps_imag = n * n - k * k, 2 * n * k
        conductivity = 2 * math.pi * frequency * eps_imag / eps_real
        length = layer["thickness_nm"] * 0.001
        geometry.append(
            mp.Block(
                center=mp.Vector3(z=position + length / 2),
                size=mp.Vector3(mp.inf, mp.inf, length),
                material=mp.Medium(epsilon=eps_real, D_conductivity=conductivity),
            )
        )
        material_values.append(
            {
                "name": request["layers"][i]["name"],
                "n": n,
                "k": k,
                "epsilon_real": eps_real,
                "epsilon_imag_at_frequency": eps_imag,
                "D_conductivity": conductivity,
            }
        )
        position += length
        boundaries.append(position)
    # The outer flux planes stay in homogeneous air, away from Yee-grid interface
    # interpolation. Internal planes partition layers; their convergence is checked.
    boundaries[0] = start - 0.15 * gap
    boundaries[-1] = end + 0.15 * gap

    def build(objects):
        sim = mp.Simulation(
            dimensions=1,
            cell_size=mp.Vector3(z=thickness + 2 * gap + 2 * pml),
            resolution=config["resolution_px_per_um"],
            Courant=0.5,
            boundary_layers=[mp.PML(pml)],
            geometry=objects,
            sources=[
                mp.Source(
                    mp.GaussianSource(
                        frequency=frequency, fwidth=frequency * config["source_bandwidth_fraction"]
                    ),
                    component=mp.Ex,
                    center=mp.Vector3(z=source_z),
                )
            ],
        )
        monitors = [
            sim.add_flux(frequency, 0, 1, mp.FluxRegion(center=mp.Vector3(z=z), direction=mp.Z))
            for z in [reflect_z, transmit_z] + boundaries
        ]
        return sim, monitors

    reference, rmon = build([])
    try:
        reference.run(until_after_sources=config["after_source_time"])
        incident = float(mp.get_fluxes(rmon[0])[0])
        normalization_flux = [float(mp.get_fluxes(m)[0]) for m in rmon]
        incident_data = reference.get_flux_data(rmon[0])
        if not math.isfinite(incident) or incident <= 0:
            raise ValueError("Empty normalization has no finite positive incident flux")
    finally:
        reference.reset_meep()
    sim, monitors = build(geometry)
    try:
        sim.load_minus_flux_data(monitors[0], incident_data)
        sim.run(until_after_sources=config["after_source_time"])
        raw = [float(mp.get_fluxes(m)[0]) for m in monitors]
    finally:
        sim.reset_meep()
    if not all(math.isfinite(value) for value in raw + normalization_flux):
        raise ValueError("Nonfinite flux cannot be promoted to an optical response")
    reflectance, transmittance = -raw[0] / incident, raw[1] / incident
    layer_absorption = [(a - b) / incident for a, b in zip(raw[2:], raw[3:], strict=False)]
    absorption = sum(layer_absorption)
    return {
        "R": reflectance,
        "T": transmittance,
        "A": absorption,
        "layer_absorption": layer_absorption,
        "energy_residual": reflectance + transmittance + absorption - 1,
        "normalization_residual": max(abs(value / incident - 1) for value in normalization_flux),
        "incident_flux": incident,
        "reference_flux": normalization_flux,
        "stack_flux": raw,
        "monitor_z_um": [reflect_z, transmit_z] + boundaries,
        "physical_stack_bounds_z_um": [start, end],
        "source_z_um": source_z,
        "material_values": material_values,
        "frequency_1_per_um": frequency,
    }


def assess_results(request, rows):
    """Assess signed, finite outputs without repairing failed or unconverged physics."""
    configs = configurations(request)
    if len(rows) != len(configs) or any(len(r) != len(request["wavelength_nm"]) for r in rows):
        raise ValueError("Output coordinates do not match the frozen execution plan")
    tolerance = request["simulation"]["convergence_tolerance"]
    checks = []
    for ci, group in enumerate(rows):
        for wi, row in enumerate(group):
            values = [row[k] for k in ("R", "T", "A", "energy_residual", "normalization_residual")]
            values += row["layer_absorption"]
            if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
                raise ValueError("Nonfinite or nonnumeric optical results")
            if len(row["layer_absorption"]) != len(request["layers"]):
                raise ValueError("Layer absorption axis mismatch")
            checks.append(
                {
                    "configuration": configs[ci]["name"],
                    "wavelength_nm": request["wavelength_nm"][wi],
                    "energy_balance_pass": abs(row["energy_residual"]) <= tolerance,
                    "normalization_pass": row["normalization_residual"] <= tolerance,
                    "passivity_pass": all(
                        -PASSIVITY_TOLERANCE <= v <= 1 + PASSIVITY_TOLERANCE
                        for v in values[:3] + row["layer_absorption"]
                    ),
                    "negative_values_retained": any(
                        v < 0 for v in values[:3] + row["layer_absorption"]
                    ),
                }
            )
    differences = []
    for ci, group in enumerate(rows[1:], start=1):
        for wi, row in enumerate(group):
            baseline = rows[0][wi]
            delta = {key: abs(row[key] - baseline[key]) for key in ("R", "T", "A")}
            delta["max_layer_absorption"] = max(
                abs(a - b)
                for a, b in zip(row["layer_absorption"], baseline["layer_absorption"], strict=True)
            )
            differences.append(
                {
                    "configuration": configs[ci]["name"],
                    "wavelength_nm": request["wavelength_nm"][wi],
                    "absolute_differences": delta,
                    "pass": max(delta.values()) <= tolerance,
                }
            )
    physics_pass = all(
        c["energy_balance_pass"] and c["normalization_pass"] and c["passivity_pass"] for c in checks
    )
    converged = all(c["pass"] for c in differences)
    return {
        "status": "NUMERICALLY_CONVERGED" if physics_pass and converged else "INCONCLUSIVE",
        "numerical_checks_pass": physics_pass,
        "convergence_checks_pass": converged,
        "checks": checks,
        "convergence": differences,
        "absolute_tolerance": tolerance,
        "passivity_tolerance": PASSIVITY_TOLERANCE,
        "interpretation": (
            "Independent two-level resolution, PML and time checks; "
            "no physical accuracy or uncertainty bound implied"
        ),
    }


def run_characterization(request):
    request = validate_request(request)
    backend = backend_probe()
    mp = _backend()
    rows = [
        [_simulate_pair(mp, request, config, wi) for wi in range(len(request["wavelength_nm"]))]
        for config in configurations(request)
    ]
    assessment = assess_results(request, rows)
    return {
        "summary": {
            "schema": SCHEMA,
            "algorithm": ALGORITHM,
            **assessment,
            "backend": backend,
            "request": request,
            "request_hash": _hash(request),
            "source_kind": "simulated",
            "physical_validation_pass": False,
            "approval_status": "unapproved",
            "uncertainty": {"kind": "unknown"},
            "metric": "optical.absorbed_power_fraction",
            "unit": "1",
            "reference_plane": "air_before_planar_stack",
            "propagation": "+z",
            "electric_field_polarization": "x",
            "included_effects": EFFECTS,
            "excluded_effects": [
                "microlens_focusing",
                "lateral_optical_crosstalk",
                "charge_generation_yield",
                "carrier_transport",
                "charge_collection",
                "readout",
                "isp",
            ],
            "resources": estimate_resources(request),
            "limitations": [
                "Planar layers, normal incidence, air on both sides, linear x polarization",
                "Separate narrowband conductivity models match n,k only at each target frequency",
                "Numerical convergence does not establish measured material accuracy",
                "Optical absorption is not collected-electron QE; no sensor model is produced",
            ],
        },
        "arrays": {
            "configuration": [c["name"] for c in configurations(request)],
            "wavelength_nm": request["wavelength_nm"],
            "layer": [layer["name"] for layer in request["layers"]],
            "configurations": configurations(request),
            "results": rows,
            "result_axis_order": ["configuration", "wavelength"],
            "flux_units": "Meep power per unit transverse area, relative source amplitude",
            "flux_monitor_order": ["reflection_subtracted_or_reference", "transmission"]
            + [f"layer_boundary_{i}" for i in range(len(request["layers"]) + 1)],
        },
        "model_candidate": None,
    }


def _read_json(path):
    if path.stat().st_size > MAX_REQUEST_BYTES:
        raise ValueError("Request JSON exceeds 256 KiB")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError("Nonfinite JSON constant: " + value)

    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    try:
        if args.probe:
            if args.request or args.output:
                raise ValueError("Probe does not accept request or output")
            print(json.dumps(backend_probe(), allow_nan=False))
            return
        if not args.request or not args.output:
            raise ValueError("Both --request and --output are required")
        result = run_characterization(_read_json(args.request))
        # Exclusive create: the supervisor owns output paths; old evidence is never overwritten.
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
        print(json.dumps({"status": result["summary"]["status"], "output": str(args.output)}))
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
