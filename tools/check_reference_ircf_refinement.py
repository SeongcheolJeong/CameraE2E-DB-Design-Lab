#!/usr/bin/env python3
"""Retrace the patent lens at5 nm and compare representative spectral integrals.

This is a bounded uniform-patch quadrature diagnostic. It does not certify
spatial PSF sampling, chart boundaries, empirical NIR QE or a physical product.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np

from camera_platform import reference_ircf_engine as engine
from camera_platform import reference_ircf_optics as ircf_optics
from camera_platform import reference_ircf_spectra as spectra
from camera_platform import reference_workshop_optics as optics
from camera_platform.virtual_lab_engine import XYZ_TO_RGB
from pyisetcam.metrics import delta_e_ab

FIELDS = ("center", "outer-ne")
FILTERS = ("baseline", "nir_leak_1pct")
LIGHTS = ("day", "ill_a")


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evaluate(workspace, solution, step_nm, fixed_matrices=None, fixed_lsc=None):
    wave = spectra.wavelength_grid(step_nm)
    assets = spectra.load_assets(workspace, wave)
    pixel = spectra.build_pixel_response(workspace, wave)
    fields = [f for f in solution["result"]["fields"] if f["id"] in FIELDS]
    if {f["id"] for f in fields} != set(FIELDS):
        raise ValueError("Refinement requires both requested optical fields")
    rejected = {f["id"]: f.get("issues", []) for f in fields if f["status"] != "succeeded"}
    if rejected:
        raise ValueError("Refinement refused unsupported fields: " + json.dumps(rejected))
    matrices, lsc, calibration = engine.calibrations(
        solution, assets, pixel["response"], fields, FILTERS, 1.8
    )
    cases = {}
    for field in fields:
        for light_id in LIGHTS:
            light = assets["illuminants"][light_id]["photons_per_m2_sr_s_nm"]
            reference = (
                assets["macbeth_reflectance"].T @ (light[:, None] * assets["xyz_per_photon"]) / 1000
            )
            white = light @ assets["xyz_per_photon"] / 1000
            for filter_id in FILTERS:
                basis, _ = engine.spectral_response(
                    solution, field, assets, pixel["response"], filter_id, "pupil", 1.8
                )
                band_electrons = basis * light[:, None] * engine.EXPOSURE
                patch_electrons = assets["macbeth_reflectance"].T @ band_electrons
                gain = np.array([lsc[field["id"]][r, c] for r, c in engine.SITE_POSITIONS])
                row = {
                    "field_id": field["id"],
                    "illuminant": light_id,
                    "filter_id": filter_id,
                    "uniform_electrons_sites": band_electrons.sum(axis=0),
                    "nir_fraction": float(band_electrons[wave > 700].sum() / band_electrons.sum()),
                    "patch_electrons_sites": patch_electrons,
                    "reference_xyz": reference,
                    "white_xyz": white,
                    "color": {},
                }
                for key, selected in (("baseline", "baseline"), ("refit", filter_id)):
                    rgb = engine.rgb_sites(patch_electrons * gain) @ matrices[selected]
                    predicted = rgb @ np.linalg.inv(XYZ_TO_RGB.T)
                    error = delta_e_ab(reference, predicted, white, "2000")
                    row["color"][key] = {
                        "predicted_xyz": predicted,
                        "delta_e00": error,
                        "delta_e00_mean": float(np.mean(error)),
                        "delta_e00_max": float(np.max(error)),
                    }
                    if fixed_matrices is not None:
                        fixed_gain = np.array(
                            [fixed_lsc[field["id"]][r, c] for r, c in engine.SITE_POSITIONS]
                        )
                        fixed_rgb = (
                            engine.rgb_sites(patch_electrons * fixed_gain)
                            @ fixed_matrices[selected]
                        )
                        fixed_xyz = fixed_rgb @ np.linalg.inv(XYZ_TO_RGB.T)
                        fixed_error = delta_e_ab(reference, fixed_xyz, white, "2000")
                        row["color"][key]["fixed_10nm_calibration_predicted_xyz"] = fixed_xyz
                        row["color"][key]["fixed_10nm_calibration_delta_e00_mean"] = float(
                            np.mean(fixed_error)
                        )
                cases["/".join((field["id"], light_id, filter_id))] = row
    return cases, matrices, lsc, calibration


def compare(coarse, fine):
    results = {}
    for key in coarse:
        a, b = coarse[key], fine[key]
        ea, eb = np.asarray(a["uniform_electrons_sites"]), np.asarray(b["uniform_electrons_sites"])
        pa, pb = np.asarray(a["patch_electrons_sites"]), np.asarray(b["patch_electrons_sites"])
        row = {
            "uniform_electrons_relative_change_sites": (eb - ea) / np.maximum(np.abs(eb), 1e-30),
            "uniform_electrons_max_relative_change": float(
                np.max(np.abs(eb - ea) / np.maximum(np.abs(eb), 1e-30))
            ),
            "patch_electrons_max_relative_change": float(
                np.max(np.abs(pb - pa) / np.maximum(np.abs(pb), 1e-30))
            ),
            "nir_fraction_change_percentage_points": (b["nir_fraction"] - a["nir_fraction"]) * 100,
            "color": {},
        }
        for profile in ("baseline", "refit"):
            ac, bc = a["color"][profile], b["color"][profile]
            between = delta_e_ab(ac["predicted_xyz"], bc["predicted_xyz"], b["white_xyz"], "2000")
            fixed = delta_e_ab(
                ac["predicted_xyz"],
                bc["fixed_10nm_calibration_predicted_xyz"],
                b["white_xyz"],
                "2000",
            )
            row["color"][profile] = {
                "mean_delta_e00_change": bc["delta_e00_mean"] - ac["delta_e00_mean"],
                "max_patch_delta_e00_between_grid_predictions": float(np.max(between)),
                "mean_patch_delta_e00_between_grid_predictions": float(np.mean(between)),
                "max_patch_delta_e00_fixed_10nm_calibration": float(np.max(fixed)),
            }
        results[key] = row
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--cache", type=Path, default=Path("/Volumes/T7/CameraE2E/reference-workshop-cache")
    )
    parser.add_argument("--coarse-folder", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("outputs/reference-ircf-lab-20261012/refinement.json")
    )
    args = parser.parse_args()
    start = time.monotonic()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    last = 0

    def progress(value):
        nonlocal last
        now = time.monotonic()
        if now - last > 20:
            print(
                json.dumps(
                    {"stage": "5nm independent optical solve", "progress": value},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            last = now

    fine_solution, fine_folder = ircf_optics.solve(
        args.workspace, args.cache, step_nm=5, on_progress=progress
    )
    fine_seconds = time.monotonic() - start
    coarse_folder = args.coarse_folder
    if coarse_folder is None:
        coarse_folder = (
            args.cache
            / "ircf-optics"
            / optics.cache_key(args.workspace, ircf_optics.specification(10))
        )
    deadline = time.monotonic() + 7200
    while not (coarse_folder / "result.json").is_file():
        if time.monotonic() > deadline:
            raise TimeoutError("The separate10 nm physical optical solution is not complete")
        print("Waiting for completed10 nm optical cache", flush=True)
        time.sleep(10)
    coarse_solution = optics.load_solution(coarse_folder)
    coarse, matrices, lsc, calibration10 = evaluate(args.workspace, coarse_solution, 10)
    fine, _, _, calibration5 = evaluate(args.workspace, fine_solution, 5, matrices, lsc)
    differences = compare(coarse, fine)
    rankings = []
    for field in FIELDS:
        for light in LIGHTS:
            for ccm in ("baseline", "refit"):
                names = ["/".join((field, light, filter_id)) for filter_id in FILTERS]
                order10 = sorted(
                    FILTERS,
                    key=lambda f: coarse["/".join((field, light, f))]["color"][ccm][
                        "delta_e00_mean"
                    ],
                )
                order5 = sorted(
                    FILTERS,
                    key=lambda f: fine["/".join((field, light, f))]["color"][ccm]["delta_e00_mean"],
                )
                rankings.append(
                    {
                        "field_id": field,
                        "illuminant": light,
                        "ccm": ccm,
                        "delta_e00_order_10nm": order10,
                        "delta_e00_order_5nm": order5,
                        "order_changed": order10 != order5,
                        "cases": names,
                    }
                )
    report = {
        "schema": "camera_reference_ircf_refinement_v1",
        "status": "completed",
        "scope": (
            "uniform and24 independently uniform Macbeth patch spectra; "
            "no spatial chart-edge metric"
        ),
        "case_count": len(differences),
        "exposure_s": engine.EXPOSURE,
        "fine_optics_elapsed_s": fine_seconds,
        "elapsed_s": time.monotonic() - start,
        "coarse_optics": {
            "folder": str(coarse_folder),
            "sha256": file_sha(coarse_folder / "result.json"),
            "arrays_sha256": file_sha(coarse_folder / "optics.npz"),
        },
        "fine_optics": {
            "folder": str(fine_folder),
            "sha256": file_sha(fine_folder / "result.json"),
            "arrays_sha256": file_sha(fine_folder / "optics.npz"),
        },
        "physical_rays_independently_solved": True,
        "fine_optics_interpolated_from_coarse": False,
        "calibration_policy": (
            "same visible-only Munsell64 white constraint, recomputed pergrid; "
            "optional fixed10 nm calibration diagnostic"
        ),
        "coarse": coarse,
        "fine": fine,
        "differences": differences,
        "calibration_10nm": calibration10,
        "calibration_5nm": calibration5,
        "filter_color_order": rankings,
        "summary": {
            "max_uniform_electrons_relative_change": max(
                r["uniform_electrons_max_relative_change"] for r in differences.values()
            ),
            "max_patch_electrons_relative_change": max(
                r["patch_electrons_max_relative_change"] for r in differences.values()
            ),
            "max_patch_color_difference_delta_e00": max(
                v["max_patch_delta_e00_between_grid_predictions"]
                for r in differences.values()
                for v in r["color"].values()
            ),
            "color_order_changed_conditions": sum(r["order_changed"] for r in rankings),
        },
        "limitations": [
            "This checks5 versus10 nm integration for eight declared conditions; "
            "no broader convergence claim.",
            "No optical geometry, image sampling, ray-count or measured-model convergence claim.",
            "NIR sensor response and filter angle dependence remain the declared proxies.",
            "No per-patch readout clipping/noise; diagnostics are means before readout.",
        ],
        "code_sha256": {
            str(Path(module.__file__).relative_to(args.workspace)): file_sha(module.__file__)
            for module in (engine, spectra, ircf_optics, optics)
        },
    }
    args.output.write_text(
        json.dumps(engine.json_value(report), ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps({"output": str(args.output), **report["summary"]}, ensure_ascii=False),
        flush=True,
    )


if __name__ == "__main__":
    main()
