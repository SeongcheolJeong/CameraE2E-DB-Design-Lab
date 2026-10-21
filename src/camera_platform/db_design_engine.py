"""Selected DB prescriptions and frozen pixels in the existing native Reference bench.

Local charts share sensor-plane coordinates, not camera FOV. This is a controlled
engineering comparison with immutable source provenance, never product ranking.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import numpy as np

from . import db_design_optics as optics
from . import db_design_sensor as sensors
from . import design_workshop_engine as shared
from . import reference_workshop_engine as reference
from . import reference_workshop_sensor as pixel
from .registry import digest
from .virtual_lab_engine import XYZ_TO_RGB, _spectra
from .virtual_lab_job import sha256

FIELDS = ("center", "outer-ne")
CHARTS = ("resolution", "color", "uniform", "texture")


def calibration(solution, cell, response, spectral):
    """Fit once on the reference center spectrum, then freeze for every candidate."""
    center = next(f for f in solution["result"]["fields"] if f["id"] == "center")
    photons = np.broadcast_to(spectral["white"], (*center["geometry"]["input_shape_row_col"], 21))
    applied = apply(solution, "center", photons, response)
    sites = applied["center_electron_rate_per_band"] * 0.004
    rgb = sites[:, [0, 1, 3]].copy()
    rgb[:, 1] = sites[:, 1:3].mean(axis=1)
    target = spectral["xyz_per_photon"] * spectral["white"][:, None] / 1000
    scale = float(np.max(rgb))
    x, white = rgb / scale, rgb.sum(axis=0) / scale
    lhs = np.block([[x.T @ x, white[:, None]], [white[None, :], np.zeros((1, 1))]])
    matrix = np.linalg.solve(lhs, np.vstack([x.T @ target, target.sum(axis=0)]))[:3]
    color = {
        "matrix": (matrix / scale @ XYZ_TO_RGB.T).tolist(),
        "reference_exposure_ms": 4,
        "reference_luminance_cd_m2": 1000,
        "baseline_cell_sha256": cell["cell_sha256"],
        "baseline_optical_revision": solution["result"]["revision"],
        "method": "baseline_center_pupil_spectral_fit_with_white_constraint",
        "per_candidate_fit": False,
    }
    result = {"color": color, "lsc": None, "baseline_only": True}
    result["identity"] = digest(result)
    return result


def apply(solution, field_id, photons, response):
    return optics.apply_field(
        solution,
        field_id,
        photons,
        angular_response=lambda theta, phi, wave: pixel.absolute_angular_response(
            response, theta, phi, wave
        ),
    )


def camera(workspace, directory, spec, solution, cell, calibrations, progress):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    if {f["id"] for f in solution["result"]["fields"] if f["id"] in FIELDS} != set(FIELDS):
        raise ValueError("Both requested DB fields are required; no partial success")
    response = pixel.build_response(cell, wavelength_nm=shared.WAVE)
    spectral = _spectra(workspace, "D65", spec["luminance_cd_m2"])
    for filename, data in (
        ("pixel-cell.json", cell),
        ("pixel-response.json", response),
        ("calibration.json", calibrations),
        ("optical-response.json", solution["result"]),
    ):
        shared.write_json(root / filename, data)
    fields, captures = [], {}
    for fi, field in enumerate(f for f in solution["result"]["fields"] if f["id"] in FIELDS):
        if field["status"] != "succeeded":
            raise ValueError("Selected DB field is not executable: " + json.dumps(field["issues"]))
        field_root = root / "fields" / field["id"]
        field_root.mkdir(parents=True, exist_ok=True)
        records = {}
        for kind in CHARTS:
            progress(field["id"] + " · " + kind + " · optical/pixel → native RAW")
            photons, _ = shared._scene(kind, field["geometry"], spectral)
            applied = apply(solution, field["id"], photons, response)
            rate = applied["mean_electron_rate"]
            if rate.shape != (256, 256) or not np.isfinite(rate).all() or np.any(rate < 0):
                raise ValueError("DB capture requires finite native 256x256 electron rates")
            folder = field_root / kind
            folder.mkdir()
            np.save(folder / "mean-electron-rate.npy", rate)
            np.save(folder / "planes-electron-rate.npy", applied["planes_electron_rate"])
            truth = shared._reference_image(kind, rate.shape, spectral)
            np.save(folder / "source-reference-linear.npy", truth.astype(np.float32))
            frames = []
            for repeat in range(spec["frames"]):
                seed = spec["seed"] + fi * 32 + repeat
                captured = shared._capture(rate, cell["electrical_model"], spec, seed)
                path = folder / f"raw-{repeat:02d}.npy"
                np.save(path, captured["arrays"]["raw_dn"])
                frames.append({
                    "file": path.name, "sha256": sha256(path), "seed": seed,
                    "saturation_fractions": captured["summary"]["saturation_fractions"],
                })
                if repeat == 0:
                    shared.write_json(folder / "capture.json", captured["summary"])
            records[kind] = {
                "raw": frames,
                "rate_sha256": sha256(folder / "mean-electron-rate.npy"),
                "planes_sha256": sha256(folder / "planes-electron-rate.npy"),
                "source_reference_sha256": sha256(folder / "source-reference-linear.npy"),
                "optical_pixel_contract": applied["metadata"],
                "response_cache_reused": False,
            }
        capture = {
            "schema": "camera_reference_field_capture_v1",
            "configuration": spec,
            "geometry": field["geometry"],
            "captures": records,
            "readout": {**cell["readout"], "gain": spec["gain"]},
            "calibration": calibrations["color"],
            "scene_reference": {
                "reference_xyz": spectral["reference_xyz"].tolist(),
                "white_xyz": spectral["white_xyz"].tolist(),
                "asset": spectral["asset"],
                "chromatic_adaptation": spectral["chromatic_adaptation"],
            },
        }
        shared.write_json(field_root / "capture.json", capture)
        captures[field["id"]] = capture
        rendered = reference._render_field(
            field_root, capture, spec, field, response, cell, calibrations
        )
        rendered["comparison_scope"] = "same_native_sensor_plane_chart_not_matched_camera_FOV"
        dn_per_e = (
            (2 ** cell["readout"]["bit_depth"] - 1 - cell["readout"]["black_level_dn"])
            * spec["gain"]
            / cell["readout"]["full_well_e"]
        )
        rendered["metrics"]["temporal_noise_e"] = (
            rendered["metrics"]["temporal_noise_dn"] / dn_per_e
        )
        rendered["tests"]["noise"]["electron_conversion"] = {
            "dn_per_e": dn_per_e,
            "definition": "input-referred Green temporal RAW noise including quantization",
        }
        fields.append(rendered)
    shared.write_json(
        root / "capture.json",
        {
            "configuration": spec,
            "fields": captures,
            "calibration_sha256": sha256(root / "calibration.json"),
        },
    )
    result = reference._summary(root, spec, solution, cell, calibrations, fields)
    result["calibration"].update(lsc_fixed_baseline=False, lsc_applied=False)
    diffraction = all(f["optics"].get("diffraction_included") for f in fields)
    result["limitations"] = [
        "Local native 256x256 sensor-plane charts; different focal lengths give different FOVs.",
        "Same field object directions, chart pixel scale, exposure, ISP and baseline CCM.",
        "Native source-cell-center approximation; sampled PSF transfer is not continuous "
        "lens-only MTF or validation of continuous scene sampling to Nyquist.",
        (
            "Scalar aberrated diffraction PSF with pupil-averaged planar pixel response; "
            "local stationary field, not vector electromagnetic pixel transport."
            if diffraction
            else "Geometric PSF with pupil-averaged planar pixel response; diffraction excluded."
        ),
        "Saved structure and camera-proxy readout do not establish a measured product model.",
        "Common FPN-off policy; stored PRNU/DSNU values are preserved as unapplied metadata.",
        "No per-candidate color fit, auto-brightness or product ranking.",
    ]
    shared.write_json(root / "render.json", result)
    return result


def run(workspace, directory, request, snapshot, progress, *, check=lambda: None):
    from .reference_isp import default_profile
    from .reference_workshop_job import defaults

    started = time.monotonic()
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    cache = root.parent.parent / "db-design-cache"
    solutions, optical_paths = {}, {}
    for key in request["lens_keys"]:
        profile = (
            "exit_pupil_diffraction"
            if request.get("comparison_mode") in {"aperture_diffraction", "aperture_sweep"}
            and key != "reference"
            else "geometric_retrace"
        )
        folder = cache / optics.cache_key(workspace, key, optical_profile=profile)
        optics.solve(workspace, key, folder, on_progress=progress, optical_profile=profile)
        solutions[key] = optics.load_solution(folder)
        optical_paths[key] = folder
        check()
    cells = {key: sensors.build_cell(workspace, key) for key in request["sensor_keys"]}
    baseline_cell = cells["reference_imx462"]
    response = pixel.build_response(baseline_cell, wavelength_nm=shared.WAVE)
    spectral = _spectra(workspace, "D65", 1000)
    fixed = calibration(solutions["reference"], baseline_cell, response, spectral)
    candidates = []
    for lens_key in request["lens_keys"]:
        for sensor_key in request["sensor_keys"]:
            identity = f"candidate-{len(candidates):02d}"
            folder = root / "candidates" / identity
            lens, sensor = snapshot["lenses"][lens_key], snapshot["sensors"][sensor_key]
            label = lens["label"] + " × " + sensor["label"]
            spec = defaults()
            spec.update(
                name=label,
                frames=8,
                seed=7,
                exposure_ms=4,
                gain=1,
                luminance_cd_m2=1000,
                chart_suite="classic_v1",
            )
            spec["isp"] = default_profile()
            spec["lens"]["optical_profile"] = solutions[lens_key]["result"]["specification"][
                "optical_profile"
            ]
            spec["db_selection"] = {"lens_key": lens_key, "sensor_key": sensor_key}
            progress(label + " · 8 independent captures")
            result = camera(
                workspace, folder, spec, solutions[lens_key], cells[sensor_key], fixed, progress
            )
            shutil.copytree(optical_paths[lens_key], folder / "optics")
            item = {
                "id": identity,
                "status": "succeeded",
                "label": label,
                "lens_key": lens_key,
                "sensor_key": sensor_key,
                "prefix": f"candidates/{identity}/",
                "result": result,
                "source_identities": {"lens": lens, "sensor": sensor},
                "parameter_sources": lens.get("parameter_sources", [])
                + sensor.get("parameter_sources", []),
            }
            shared.write_json(folder / "sources.json", item["source_identities"])
            shared.write_json(folder / "result.json", item)
            candidates.append(item)
            check()
    output = {
        "schema": "camera_db_design_study_result_v1",
        "configuration": request,
        "comparison_mode": request.get("comparison_mode", "lens_sensor"),
        "candidates": candidates,
        "baseline_candidate_id": "candidate-00",
        "capture_count": len(candidates) * len(FIELDS) * len(CHARTS) * 8,
        "raw_shape_row_col": [256, 256],
        "raw_preserved": True,
        "physical_validation": "not_established",
        "product_ranking_allowed": False,
        "comparison_scope": "same_native_sensor_plane_charts_common_reference_CCM",
        "calibration": fixed,
        "performance": {"elapsed_seconds": time.monotonic() - started},
    }
    if request.get("comparison_mode") == "aperture_sweep":
        output["aperture_sweep"] = aperture_comparison(root, candidates, request)
        shared.write_json(root / "aperture-selection.json", output["aperture_sweep"])
    return output


def aperture_comparison(root, candidates, request):
    """Compare only the same prescription's iris settings on retained captures."""
    from . import db_design_aperture as evaluation

    rows = []
    selected = [c for c in candidates if c["lens_key"] != "reference"]
    if len(selected) != len(request["f_numbers"]):
        raise ValueError("Every declared aperture requires a completed candidate")
    for candidate, requested in zip(selected, request["f_numbers"], strict=True):
        source = candidate["source_identities"]["lens"]["source"]
        condition = source.get("pupil_condition")
        applied = (
            condition["effective_f_number"]
            if condition else source["paraxial"]["effective_f_number"]
        )
        fields = []
        for field in candidate["result"]["fields"]:
            measured = evaluation.measure_field(
                Path(root) / candidate["prefix"] / "fields" / field["id"], field
            )
            fields.append({"field_id": field["id"], **measured})
        rows.append({
            "candidate_id": candidate["id"], "f_number": applied,
            "requested_f_number": requested,
            "aperture_condition": condition or {
                "mode": "source_stop", "effective_f_number": applied,
                "reference_wavelength_nm": 550,
                "note": "F/2.75 label retains original physical stop; actual F-number is reported",
            },
            "fields": fields,
        })
    open_fields = {f["field_id"]: f for f in rows[0]["fields"]}
    for row in rows:
        for field in row["fields"]:
            reference_signal = open_fields[field["field_id"]]["mean_signal_e"]
            field["relative_signal"] = (
                field["mean_signal_e"] / reference_signal
                if field["mean_signal_e"] is not None and reference_signal is not None
                and reference_signal > 0 else None
            )
        for name, metric, aggregate in (
            ("worst_field_snr_db", "snr_db", min),
            ("worst_field_sfr50_cpp", "processed_sfr50_cpp", min),
            ("min_relative_signal", "relative_signal", min),
            ("max_saturation_fraction", "saturation_fraction", max),
        ):
            values = [f.get(metric) for f in row["fields"]]
            row[name] = aggregate(values) if all(v is not None for v in values) else None
    return {
        "schema": "camera_db_aperture_sweep_result_v1",
        "rows": rows,
        "baseline_candidate_id": rows[0]["candidate_id"],
        "selection": evaluation.assess(rows, request["selection_policy"]),
        "scope": (
            "Fixed 4ms/gain1/native charts and common ISP; selection within this sampled model only"
        ),
        "signal_reference": (
            "Same-field uniform Green photoelectrons at the lowest requested F-number"
        ),
        "sfr_scope": "Processed chart SFR50, not continuous lens-only MTF or Nyquist qualification",
        "physical_validation": "not_established",
    }
