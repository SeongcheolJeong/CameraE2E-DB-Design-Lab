"""Contracts for source-frozen 2D periodic OCL spectral sensitivity experiments.

All CFA materials are evaluated at every wavelength, including off-band light.
A valid absorption ratio does not promote these 2D samples to a 3D camera LUT.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np

from .registry import digest

TASK_SCHEMA = "camera_ocl_periodic2d_task_v1"
SAMPLE_SCHEMA = "camera_ocl_periodic2d_sample_v1"
SOURCE_MODEL = "periodic_homogeneous_cfa_plane_wave"
COLORS = ("red", "green", "blue")
WAVELENGTHS = (450., 550., 650.)
VARIANTS = ("reviewed", "ocl_shift")


def worker_path(workspace):
    return Path(workspace)/"simulations/fdtd_tcad/sensor_structure_periodic_spectral_worker.py"


def task_list(bundle, field_angles, *, resolutions=(60, 80), wavelengths=WAVELENGTHS,
              after_source_time=60., pml_um=.8, solver_sha256=None):
    """Build interleaved A/B tasks at exact wavelength-specific chief-ray angles.

    field_angles maps field names to {wavelength_nm: angle_deg}; JSON string
    wavelength keys are accepted, but missing or extrapolated nodes are not.
    """
    if not field_angles or not isinstance(field_angles, dict):
        raise ValueError("Explicit physical-field angle nodes are required")
    waves = np.asarray(wavelengths, dtype=float)
    meshes = np.asarray(resolutions, dtype=float)
    if (waves.ndim != 1 or len(set(waves)) != len(waves) or not np.isfinite(waves).all()
            or np.any((waves < 450) | (waves > 650))):
        raise ValueError("Wavelength nodes must be finite and within source support")
    if (meshes.ndim != 1 or len(set(meshes)) != len(meshes) or not len(meshes)
            or not np.isfinite(meshes).all() or np.any(meshes != np.floor(meshes))
            or np.any((meshes < 30) | (meshes > 200))):
        raise ValueError("Invalid mesh resolutions")
    result = []
    for res in sorted(meshes):
        for field, nodes in field_angles.items():
            by_wave = {float(key): float(value) for key, value in nodes.items()}
            for wave in waves:
                if wave not in by_wave or not np.isfinite(by_wave[wave]) or abs(by_wave[wave]) > 40:
                    raise ValueError("Missing or unsupported exact CRA wavelength node")
                for color in COLORS:
                    for pol in ("s", "p"):
                        for variant in VARIANTS:
                            task = dict(schema=TASK_SCHEMA, structure=copy.deepcopy(bundle),
                                source_model=SOURCE_MODEL, dimension=2, field=field,
                                wavelength_nm=float(wave), angle_deg=by_wave[wave], cfa_channel=color,
                                variant=variant, polarization=pol, resolution=int(res),
                                after_source_time=float(after_source_time), pml_um=float(pml_um),
                                rb_cfa_thickness_um=.475, ocl_shift_pitch=.1, control="mesh")
                            if solver_sha256 is not None:
                                task["worker_sha256"] = solver_sha256
                            task["id"] = digest(task)
                            result.append(task)
    return result


def validate_sample(task, path, *, worker_sha256=None):
    """Validate stored identities/arrays separately from numerical acceptance."""
    path = Path(path)
    sample = json.loads(path.read_text())
    if sample.get("schema") != SAMPLE_SCHEMA or sample.get("task_id") != task["id"]:
        raise ValueError("Spectral result identity mismatch")
    for key in ("wavelength_nm", "angle_deg", "cfa_channel", "variant", "polarization",
                "resolution", "source_model", "dimension", "control", "after_source_time", "pml_um"):
        if sample.get(key) != task.get(key):
            raise ValueError("Spectral sample coordinate mismatch: " + key)
    if sample.get("structure_revision") != task["structure"]["revision"]:
        raise ValueError("Structure revision mismatch")
    expected_worker = worker_sha256 or task.get("worker_sha256")
    if expected_worker and sample.get("worker_sha256") != expected_worker:
        raise ValueError("Worker revision mismatch")
    arrays = path.with_suffix(".npz")
    with arrays.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != sample.get("array_sha256"):
            raise ValueError("Numerical array checksum mismatch")
    power_names = ("R", "T", "side_outflow", "absorption", "active_si_absorption",
                   "energy_residual", "incident_power", "negative_dissipation_integral")
    if not np.isfinite([sample[key] for key in power_names]).all() or sample["incident_power"] <= 0:
        raise ValueError("Nonfinite or nonpositive normalized response")
    layers = sample["layer_absorption"]
    if not np.isfinite(list(layers.values())).all():
        raise ValueError("Nonfinite layer absorption")
    if not np.isclose(sum(layers.values()), sample["absorption"], rtol=0, atol=1e-9):
        raise ValueError("Total absorption does not match layers")
    if not np.isclose(layers["active_si"], sample["active_si_absorption"], rtol=0, atol=1e-9):
        raise ValueError("Active absorption does not match its layer")
    residual = 1-sample["R"]-sample["T"]-sample["side_outflow"]-sample["absorption"]
    if not np.isclose(residual, sample["energy_residual"], rtol=0, atol=1e-9):
        raise ValueError("Energy residual does not match powers")
    if sample["support_tail_fraction"] is not None or sample.get("camera_response_eligible") is not False:
        raise ValueError("Periodic 2D samples cannot claim finite-source support or 3D camera eligibility")
    with np.load(arrays, allow_pickle=False) as data:
        density, role = data["absorption_density"], data["role"]
        if density.shape != role.shape or density.ndim != 2 or not np.isfinite(density).all() or np.min(density) < 0:
            raise ValueError("Invalid absorption map")
    passive = min(sample["R"], sample["T"], *layers.values(), sample["negative_dissipation_integral"]) >= -1e-5
    sample["passive"] = bool(passive)
    sample["numerical_valid"] = bool(passive and abs(residual) <= .01)
    sample["field"] = task["field"]
    return sample


def ratios(samples):
    """Per-CFA and per-wavelength unpolarized sensitivity at each grid.

    Average s/p absorptions before division. Very weak baseline absorption is
    flagged and never represented as a reliable relative gain. Incomplete or
    invalid paired results are explicit; missing values cannot be successes.
    """
    groups = {}
    for sample in samples:
        key = (sample["field"], sample["wavelength_nm"], sample["angle_deg"],
               sample["cfa_channel"], sample["resolution"], sample.get("control", "mesh"))
        groups.setdefault(key, []).append(sample)
    rows = []
    expected = {(v, p) for v in VARIANTS for p in ("s", "p")}
    for key, values in sorted(groups.items()):
        index = {(s["variant"], s["polarization"]): s for s in values}
        complete = set(index) == expected and len(values) == 4
        valid = complete and all(s.get("numerical_valid") is True for s in values)
        row = dict(zip(("field", "wavelength_nm", "angle_deg", "cfa_channel", "resolution", "control"), key))
        row.update(complete=complete, numerical_valid=valid, camera_response_eligible=False,
                   gain=None, baseline_absorption=None, changed_absorption=None, weak_response=None)
        if complete:
            baseline = np.mean([index[("reviewed", p)]["active_si_absorption"] for p in ("s", "p")])
            changed = np.mean([index[("ocl_shift", p)]["active_si_absorption"] for p in ("s", "p")])
            row.update(baseline_absorption=float(baseline), changed_absorption=float(changed),
                       absolute_delta=float(changed-baseline), weak_response=bool(baseline < .01))
            if valid and baseline >= .01:
                row["gain"] = float(changed/baseline)
            row["polarized"] = [{"polarization": p,
                "baseline_absorption": index[("reviewed", p)]["active_si_absorption"],
                "changed_absorption": index[("ocl_shift", p)]["active_si_absorption"],
                "absolute_delta": index[("ocl_shift", p)]["active_si_absorption"]-index[("reviewed", p)]["active_si_absorption"]}
                for p in ("s", "p")]
        rows.append(row)
    return rows
