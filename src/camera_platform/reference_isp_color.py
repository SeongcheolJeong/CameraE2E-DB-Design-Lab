"""Model-derived, white-anchored color fitting on preserved Reference optics.

Munsell reflectances alone determine the fit. Macbeth is a withheld model test;
neither evaluation RAW nor displayed images are read to estimate this matrix.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from pyisetcam.metrics import delta_e_ab

from . import reference_workshop_optics as optics
from . import reference_workshop_sensor as pixel
from .offline_assets import offline_asset_identity, offline_asset_store
from .registry import digest
from .virtual_lab_engine import XYZ_TO_RGB, _spectra

SCHEMA = "camera_reference_isp_color_calibration_v1"
TRAIN_FILE = "MunsellSamples_Vhrel.mat"
HOLDOUT_FILE = "macbethChart.mat"
WAVE = np.arange(450.0, 651.0, 10.0)
REFERENCE_EXPOSURE_S = 0.004


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _model_digest(value):
    # Pixel model contracts use the original JSON ASCII serialization.
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def fit_white_constrained(sensor_samples, target_samples, white_sensor, white_target):
    """Least-squares row-vector transform with an exact declared white anchor.

    Inputs may have any consistent linear signal units. No sample-dependent
    normalization, clipping, weighting or held-out tuning is performed.
    """
    x, y, ws, wt = (
        np.asarray(a, dtype=float)
        for a in (sensor_samples, target_samples, white_sensor, white_target)
    )
    if (
        x.ndim != 2
        or x.shape[1] != 3
        or y.shape != x.shape
        or x.shape[0] < 3
        or ws.shape != (3,)
        or wt.shape != (3,)
        or not all(np.isfinite(a).all() for a in (x, y, ws, wt))
        or np.any(ws <= 0)
        or np.linalg.matrix_rank(x) < 3
    ):
        raise ValueError("Color fitting requires finite full-rank Nx3 samples and a positive white")
    scale = float(np.max(np.abs(x)))
    a, w = x / scale, ws / scale
    lhs = np.block([[a.T @ a, w[:, None]], [w[None, :], np.zeros((1, 1))]])
    rhs = np.vstack([a.T @ y, wt])
    matrix = np.linalg.solve(lhs, rhs)[:3] / scale
    if not np.isfinite(matrix).all() or not np.allclose(ws @ matrix, wt, rtol=1e-10, atol=1e-10):
        raise ValueError("Color fit did not retain its declared white anchor")
    return matrix


def reflectance_sets(workspace):
    """Return the fixed, disjoint local training and withheld assets."""
    assets = offline_asset_store(workspace=workspace, source=Path(workspace) / "camerae2e_db")
    result = {}
    for role, name, count in (("training", TRAIN_FILE, 64), ("holdout", HOLDOUT_FILE, 24)):
        wave, values = assets.load_reflectances(name, wave_nm=WAVE)
        values = np.asarray(values, float)
        if (
            values.shape != (21, count)
            or not np.array_equal(wave, WAVE)
            or not np.isfinite(values).all()
            or np.any((values < 0) | (values > 1))
        ):
            raise ValueError("Pinned color reflectance asset changed shape, range or wavelength")
        relative = "data/surfaces/reflectances/" + name
        result[role] = {
            "values": values,
            "provenance": {
                "file": relative,
                "sha256": _sha(assets.resolve(relative)),
                "indices_one_based": list(range(1, count + 1)),
                "wavelength_nm": WAVE.tolist(),
                "resampled_content_sha256": digest(values.tolist()),
            },
        }
    differences = np.max(
        np.abs(result["training"]["values"].T[:, None] - result["holdout"]["values"].T[None]),
        axis=-1,
    )
    if np.any(differences <= 1e-12):
        raise ValueError("Color training and withheld reflectances contain identical spectra")
    result["asset_identity"] = offline_asset_identity(workspace=workspace)
    return result


def fit_basis(electrons_per_band_rgb, target_xyz_per_band, training_reflectances):
    """Fit electron-to-XYZ using only the supplied training reflectances."""
    electrons = np.asarray(electrons_per_band_rgb, float)
    targets = np.asarray(target_xyz_per_band, float)
    training = np.asarray(training_reflectances, float)
    if (
        electrons.shape != (21, 3)
        or targets.shape != (21, 3)
        or training.ndim != 2
        or training.shape[0] != 21
        or not all(np.isfinite(a).all() for a in (electrons, targets, training))
        or np.any(electrons < 0)
        or np.any((training < 0) | (training > 1))
    ):
        raise ValueError(
            "Color basis requires 21 bands and nonnegative electron/reflectance inputs"
        )
    sensor_samples, target_samples = training.T @ electrons, training.T @ targets
    matrix = fit_white_constrained(
        sensor_samples, target_samples, electrons.sum(axis=0), targets.sum(axis=0)
    )
    return matrix, sensor_samples, target_samples


def _source(source_dir):
    root = Path(source_dir).resolve()
    names = [
        "capture.json",
        "pixel-cell.json",
        "pixel-response.json",
        "optical-response.json",
        "optics/result.json",
        "optics/optics.npz",
    ]
    hashes = {name: _sha(root / name) for name in names}
    capture, cell, response, optical = (json.loads((root / name).read_text()) for name in names[:4])
    solution = optics.load_solution(root / "optics")
    if digest(solution["result"]) != digest(optical):
        raise ValueError("Preserved optical response differs from its solved source")
    for record, key in ((cell, "cell_sha256"), (response, "table_sha256")):
        if record.get(key) != _model_digest({k: v for k, v in record.items() if k != key}):
            raise ValueError("Preserved pixel source hash changed")
    if response["cell_sha256"] != cell["cell_sha256"] or not np.array_equal(
        response["wavelength_nm"], WAVE
    ):
        raise ValueError(
            "Preserved pixel response differs from the selected cell or wavelength support"
        )
    center = next((f for f in optical["fields"] if f["id"] == "center"), None)
    if center is None or center["status"] != "succeeded":
        raise ValueError("Color fitting requires a successful preserved optical center")
    actual = capture["fields"]["center"]
    expected_readout = {**cell["readout"], "gain": capture["configuration"]["gain"]}
    if actual["readout"] != expected_readout:
        raise ValueError("Preserved center RAW readout differs from its source pixel")
    return root, hashes, capture, cell, response, solution, center


def fit_calibration(workspace, source_dir):
    """Return a content-addressed calibration; never edit or reacquire source RAW."""
    root, hashes, capture, cell, response, solution, center = _source(source_dir)
    reflectances = reflectance_sets(workspace)
    spectral = _spectra(workspace, "D65", 1000)
    shape = center["geometry"]["input_shape_row_col"]
    applied = optics.apply_field(
        solution,
        "center",
        np.broadcast_to(spectral["white"], (*shape, len(WAVE))),
        angular_response=lambda theta, phi, wave: pixel.absolute_angular_response(
            response, theta, phi, wave
        ),
    )
    sites = np.asarray(applied["center_electron_rate_per_band"], float) * REFERENCE_EXPOSURE_S
    electrons = sites[:, [0, 1, 3]].copy()
    electrons[:, 1] = sites[:, 1:3].mean(axis=1)
    target = spectral["white"][:, None] * spectral["xyz_per_photon"] / 1000
    xyz_matrix, train_x, train_y = fit_basis(electrons, target, reflectances["training"]["values"])
    matrix = xyz_matrix @ XYZ_TO_RGB.T
    holdout = reflectances["holdout"]["values"]
    expected, predicted = holdout.T @ target, (holdout.T @ electrons) @ xyz_matrix
    differences = np.asarray(delta_e_ab(expected, predicted, target.sum(axis=0), "2000"))
    baseline = np.asarray(capture["fields"]["center"]["calibration"]["matrix"], float)
    column_norm = np.linalg.norm(matrix, axis=0)
    data = {
        "schema": SCHEMA,
        "matrix": matrix.tolist(),
        "matrix_xyz": xyz_matrix.tolist(),
        "input_unit": "electrons",
        "output_unit": "signed_linear_srgb",
        "matrix_convention": "row_vector_electrons_at_reference_exposure @ matrix",
        "reference_exposure_s": REFERENCE_EXPOSURE_S,
        "reference_luminance_cd_m2": 1000,
        "illuminant": "D65",
        "source_binding": {
            "files_sha256": hashes,
            "cell_sha256": cell["cell_sha256"],
            "pixel_response_sha256": response["table_sha256"],
            "optical_revision": solution["result"]["revision"],
            "readout_sha256": digest(cell["readout"]),
            "cfa_thickness_scale": cell["cfa_thickness_scale"],
            "baseline_matrix_sha256": digest(baseline.tolist()),
            "center_field_id": "center",
            "actual_pupil_response": True,
        },
        "training": {**reflectances["training"]["provenance"], "role": "fit_only"},
        "holdout": {
            **reflectances["holdout"]["provenance"],
            "role": "withheld_model_evaluation",
            "used_in_fit": False,
            "delta_e00_mean": float(differences.mean()),
            "delta_e00_max": float(differences.max()),
            "delta_e00": differences.tolist(),
            "reference_xyz": expected.tolist(),
            "predicted_xyz": predicted.tolist(),
            "meaning": "ideal spectral model; acquired chart evaluation is reported separately",
        },
        "assets": reflectances["asset_identity"],
        "basis": {
            "wavelength_nm": WAVE.tolist(),
            "site_order": ["R", "Gr", "Gb", "B"],
            "electrons_per_band_sites": sites.tolist(),
            "electrons_per_band_rgb": electrons.tolist(),
            "target_xyz_per_band": target.tolist(),
            "white_electrons_rgb": electrons.sum(axis=0).tolist(),
            "white_xyz": target.sum(axis=0).tolist(),
            "chromatic_adaptation": spectral["chromatic_adaptation"],
            "integration": (
                "illumination, band width, pupil QE, radiometry and pixel area already included"
            ),
        },
        "fit": {
            "method": "munsell64_center_pupil_white_constrained_least_squares_v1",
            "training_residual_rms_xyz": float(
                np.sqrt(np.mean((train_x @ xyz_matrix - train_y) ** 2))
            ),
            "training_condition_number": float(np.linalg.cond(train_x)),
            "matrix_condition_number": float(np.linalg.cond(matrix)),
            "white_anchor_residual_xyz": (
                electrons.sum(axis=0) @ xyz_matrix - target.sum(axis=0)
            ).tolist(),
            "evaluation_raw_used": False,
            "frame_normalization": False,
            "field_policy": "one_center_matrix_applied_unchanged_to_all_fields",
        },
        "diagnostics": {
            "matrix_column_l2_norm": column_norm.tolist(),
            "column_norm_ratio_to_frozen_spectral_matrix": (
                column_norm / np.maximum(np.linalg.norm(baseline, axis=0), 1e-30)
            ).tolist(),
            "noise_meaning": (
                "coefficient norms only; measure actual output noise from repeated RAW"
            ),
        },
        "physical_validation": "not_established",
    }
    if hashes != {name: _sha(root / name) for name in hashes}:
        raise ValueError("Preserved Reference source changed during color fitting")
    data["id"] = "reference-isp-color-" + digest(data)
    return data


def verify_calibration(calibration, source_dir=None):
    """Verify the complete calibration and, optionally, its preserved source."""
    data = {key: value for key, value in calibration.items() if key != "id"}
    if data.get("schema") != SCHEMA or calibration.get("id") != "reference-isp-color-" + digest(
        data
    ):
        raise ValueError("Reference color calibration content changed")
    if source_dir is not None:
        root = Path(source_dir).resolve()
        for name, expected in data["source_binding"]["files_sha256"].items():
            path = (root / name).resolve()
            if not path.is_relative_to(root) or _sha(path) != expected:
                raise ValueError("Reference color calibration source changed")
    return calibration


def validate_binding(calibration, source_dir):
    """Allow different RAW only for the exact fitted optical/pixel/readout model."""
    verify_calibration(calibration)
    _, hashes, _, cell, response, solution, _ = _source(source_dir)
    binding = calibration["source_binding"]
    actual = {
        "cell_sha256": cell["cell_sha256"],
        "pixel_response_sha256": response["table_sha256"],
        "optical_revision": solution["result"]["revision"],
        "readout_sha256": digest(cell["readout"]),
    }
    if any(binding[key] != value for key, value in actual.items()):
        raise ValueError(
            "Reference color calibration belongs to a different optical/pixel/readout model"
        )
    arrays_hash = solution["result"]["arrays_sha256"]
    if arrays_hash != binding["files_sha256"]["optics/optics.npz"]:
        raise ValueError("Reference color calibration optical arrays changed")
    for name in ("pixel-cell.json", "pixel-response.json", "optical-response.json"):
        if hashes[name] != binding["files_sha256"][name]:
            raise ValueError("Reference color calibration model source changed")
    return calibration
