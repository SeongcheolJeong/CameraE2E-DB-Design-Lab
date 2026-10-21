#!/usr/bin/env python3
"""Freeze executable CFA/material/lens sensitivity jobs without running Meep.

Anchor results are screened only after all baseline/variant anchors finish.
A >5% native-site response or angular-ratio change requires spectral/angular
grid expansion; an anchor screen is never a validated production response.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "simulations/fdtd_tcad/run_sensor_response.py"
CHANNELS = ("red", "green", "blue")
DIRECTIONS = ((0, 0), (20, 0), (20, 90), (35, 0), (35, 90))
COLLECTION_EFFICIENCIES = (0.5, 0.75, 1.0)
EXPANSION_THRESHOLD = 0.05


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _immutable_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != content:
        raise ValueError(f"Frozen artifact changed: {path}; use a new output directory.")
    if not path.exists():
        path.write_bytes(content)


def _immutable_json(path: Path, value: Any) -> None:
    _immutable_bytes(path, (json.dumps(value, indent=2, allow_nan=False) + "\n").encode())


def _load_runner(path: Path):
    spec = importlib.util.spec_from_file_location("frozen_sensitivity_runner", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def _rehash_bundle(bundle: dict[str, Any]) -> None:
    for material in [*bundle["channels"].values(), *bundle["materials"].values()]:
        wave, n, k = (np.asarray(material[key], dtype=float) for key in ("wavelength_nm", "n", "k"))
        if (
            wave.ndim != 1
            or wave.size < 2
            or n.shape != wave.shape
            or k.shape != wave.shape
            or not np.all(np.isfinite([wave, n, k]))
            or np.any(np.diff(wave) <= 0)
            or np.any(n <= 0)
            or np.any(k < 0)
        ):
            raise ValueError("Sensitivity bundles require finite, passive, ordered n/k tables.")
        material["material_sha256"] = _digest(
            {key: material[key] for key in ("wavelength_nm", "n", "k")}
        )
    bundle["material_bundle_sha256"] = _digest(
        {
            "channels": {
                key: value["material_sha256"] for key, value in bundle["channels"].items()
            },
            "materials": {
                key: value["material_sha256"] for key, value in bundle["materials"].items()
            },
        }
    )
    bundle["geometry_sha256"] = _digest(bundle["geometry_um"])


def sensitivity_variants(bundle: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return baseline plus 24 one-at-a-time, explicitly assumed optical variants.

    Channel-specific CFA thickness varies within its documented range. Material
    perturbations affect one RGB channel at a time. Collection efficiency is a
    later multiplier and is deliberately absent from the optical variant list.
    """

    variants = [("baseline", copy.deepcopy(bundle))]

    def add(name, parameter, value, mutate, unit):
        changed = copy.deepcopy(bundle)
        mutate(changed)
        changed["sensitivity"] = {
            "variant_id": name,
            "parameter": parameter,
            "value": value,
            "unit": unit,
            "method": "one_at_a_time",
            "estimate_only": True,
            "measured_accuracy_verified": False,
            "baseline_material_bundle_sha256": bundle["material_bundle_sha256"],
            "baseline_geometry_sha256": bundle["geometry_sha256"],
            "collection_efficiency": "postprocessing only; no optical rerun",
            "geometry_convention": {
                "cfa_thickness": "fixed common CFA top/lens plane; per-site passivation complement",
                "lens_height": "height alone varies; underlying CFA/passivation remains fixed",
                "lens_shift": "periodic lens centers move; native Si regions remain fixed",
            },
        }
        _rehash_bundle(changed)
        variants.append((name, changed))

    for channel in CHANNELS:
        material = bundle["channels"][channel]
        bounds = np.asarray(material["thickness_range_um"], dtype=float)
        if bounds.shape != (2,) or not np.all(np.isfinite(bounds)) or not 0 < bounds[0] < bounds[1]:
            raise ValueError(f"{channel} CFA thickness needs a positive ordered sensitivity range.")
        for label, thickness in zip(("low", "high"), bounds, strict=True):

            def change_thickness(changed, channel=channel, thickness=float(thickness)):
                changed["channels"][channel]["thickness_um"] = thickness
                changed["channels"][channel]["thickness_source"] = {
                    "value": thickness,
                    "source_kind": "sensitivity_assumption",
                    "basis": "documented canonical channel thickness_range_um endpoint",
                }
                changed["geometry_um"]["cfa_thickness_by_channel_um"] = {
                    key: float(item["thickness_um"]) for key, item in changed["channels"].items()
                }

            add(
                f"{channel}_thickness_{label}",
                f"channels.{channel}.thickness_um",
                float(thickness),
                change_thickness,
                "um",
            )
        for scale in (0.8, 1.2):

            def change_k(changed, channel=channel, scale=scale):
                changed["channels"][channel]["k"] = (
                    np.asarray(changed["channels"][channel]["k"]) * scale
                ).tolist()

            add(
                f"{channel}_k_{scale:.1f}",
                f"channels.{channel}.k_multiplier",
                scale,
                change_k,
                "ratio",
            )
        for offset in (-0.05, 0.05):

            def change_n(changed, channel=channel, offset=offset):
                values = np.asarray(changed["channels"][channel]["n"]) + offset
                if not np.all(np.isfinite(values)) or np.any(values <= 0):
                    raise ValueError("CFA n perturbation must remain finite and positive.")
                changed["channels"][channel]["n"] = values.tolist()

            add(
                f"{channel}_n_{offset:+.2f}",
                f"channels.{channel}.n_offset",
                offset,
                change_n,
                "refractive index",
            )
    for scale in (0.9, 1.1):

        def change_height(changed, scale=scale):
            changed["geometry_um"]["lens_height"] *= scale

        add(
            f"lens_height_{scale:.1f}",
            "geometry_um.lens_height_multiplier",
            scale,
            change_height,
            "ratio",
        )
    for axis, index in (("x", 0), ("y", 1)):
        for fraction in (-0.05, 0.05):
            shift = fraction * float(bundle["pixel_pitch_um"])

            def change_shift(changed, index=index, shift=shift):
                values = [0.0, 0.0]
                values[index] = shift
                changed["geometry_um"]["lens_shift_um"] = values

            add(
                f"lens_shift_{axis}_{fraction:+.2f}pitch",
                f"geometry_um.lens_shift_{axis}",
                shift,
                change_shift,
                "um",
            )
    return variants


def prepare_sensitivity(
    bundle_paths: list[Path],
    output_dir: Path,
    *,
    resolution: int = 80,
    runner_path: Path = RUNNER,
) -> dict[str, Any]:
    """Create immutable bundles and a runner-compatible 30-anchor-per-variant manifest."""

    if int(resolution) != resolution or resolution <= 0:
        raise ValueError("Resolution must be a positive integer.")
    if not bundle_paths:
        raise ValueError("Sensitivity preparation requires at least one canonical sensor bundle.")
    output = Path(output_dir).expanduser().resolve()
    snapshot = output / "runner_snapshot.py"
    runner_bytes = Path(runner_path).read_bytes()
    compile(runner_bytes, str(runner_path), "exec")
    _immutable_bytes(snapshot, runner_bytes)
    runner = _load_runner(snapshot)
    records, jobs = [], []
    seen = set()
    for source in sorted(Path(path).expanduser().resolve() for path in bundle_paths):
        canonical = json.loads(source.read_text())
        code = canonical["report_code"]
        if code in seen:
            raise ValueError(f"Duplicate canonical sensor report: {code}.")
        seen.add(code)
        checked = copy.deepcopy(canonical)
        _rehash_bundle(checked)
        if any(
            checked[key] != canonical[key]
            for key in (
                "material_bundle_sha256",
                "geometry_sha256",
            )
        ):
            raise ValueError(
                f"Canonical {code} hashes disagree with actual material/geometry data."
            )
        for variant_id, bundle in sensitivity_variants(canonical):
            path = output / "bundles" / f"{code}__{variant_id}.json"
            _immutable_json(path, bundle)
            geometry = runner.geometry_from_bundle(bundle)
            if variant_id.endswith("thickness_low") or variant_id.endswith("thickness_high"):
                expected = bundle["geometry_um"]["cfa_thickness_by_channel_um"]
                if geometry.get("cfa_thickness_by_channel_um") != expected:
                    raise ValueError("Runner does not implement channel-specific CFA thickness.")
            if (
                variant_id.startswith("lens_shift")
                and geometry.get("lens_shift_um") != (bundle["geometry_um"]["lens_shift_um"])
            ):
                raise ValueError("Runner does not implement the requested microlens shift.")
            variant_jobs = []
            for wavelength in (450, 550, 650):
                for theta, phi in DIRECTIONS:
                    for pol in ("s", "p"):
                        job = runner._job(
                            path,
                            bundle,
                            geometry,
                            "sensitivity_baseline"
                            if variant_id == "baseline"
                            else "sensitivity_anchor",
                            wavelength,
                            theta,
                            phi,
                            pol,
                            int(resolution),
                        )
                        jobs.append(job)
                        variant_jobs.append(job["job_id"])
            records.append(
                {
                    "sensor_id": bundle["sensor_id"],
                    "report_code": code,
                    "variant_id": variant_id,
                    "source_bundle": str(source),
                    "source_bundle_sha256": _file_hash(source),
                    "bundle_path": str(path),
                    "bundle_file_sha256": _file_hash(path),
                    "material_bundle_sha256": bundle["material_bundle_sha256"],
                    "geometry_sha256": bundle["geometry_sha256"],
                    "solver_geometry_sha256": runner.canonical_hash(geometry),
                    "assumption": bundle.get("sensitivity", {"variant_id": "baseline"}),
                    "geometry_assumptions": {
                        "cfa_thickness_rule": geometry.get("cfa_thickness_rule"),
                        "lens_shift_rule": "periodic centers, fixed Si collection regions",
                    },
                    "job_ids": variant_jobs,
                }
            )
    manifest = {
        "schema": "camerae2e_fdtd_sensitivity_campaign_v1",
        "estimate_only": True,
        "measured_accuracy_verified": False,
        "output_dir": str(output),
        "solver_python": runner.DEFAULT_MEEP_PYTHON,
        "solver_snapshot": str(snapshot),
        "solver_code_sha256": _file_hash(snapshot),
        "local_limits": runner.LOCAL_LIMITS,
        "preparer_sha256": _file_hash(Path(__file__)),
        "resolution_px_per_um": int(resolution),
        "variants": records,
        "jobs": jobs,
        "production_jobs": 0,
        "anchors": {
            "wavelength_nm": [450, 550, 650],
            "theta_phi_deg": DIRECTIONS,
            "polarization": ["s", "p"],
        },
        "collection_efficiency": {
            "values": COLLECTION_EFFICIENCIES,
            "optical_jobs": 0,
            "application": "per-pixel QE = 4 * absorption_cell_fraction * eta",
        },
        "screening": {
            "expansion_threshold": EXPANSION_THRESHOLD,
            "metric": "maximum native-site relative response or relative angular-ratio change",
            "denominator_floor": 1e-12,
            "expansion": (
                "Required variants need 400:10:700 nm and validated angular grid; "
                "anchors are insufficient."
            ),
        },
        "completion": "pending; no sensitivity results are implied by planned jobs",
        "execute_one_job": [
            sys.executable,
            str(snapshot),
            "run",
            "--manifest",
            str(output / "sensitivity_campaign.json"),
            "--role",
            "sensitivity_anchor",
        ],
    }
    manifest["manifest_sha256"] = _digest(manifest)
    _immutable_json(output / "sensitivity_campaign.json", manifest)
    return manifest


def _anchor_key(job: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(job[key] for key in ("wavelength_nm", "theta_deg", "phi_deg", "polarization"))


def _completed_response(job: dict[str, Any], output: Path) -> np.ndarray | None:
    path = output / "jobs" / job["job_id"] / "result.json"
    if not path.exists():
        return None
    result = json.loads(path.read_text())
    if result.get("job_sha256") != job["job_sha256"]:
        raise ValueError("Sensitivity result does not match its immutable input job.")
    if any(
        result.get(key) != job[key]
        for key in (
            "wavelength_nm",
            "theta_deg",
            "phi_deg",
            "polarization",
        )
    ):
        raise ValueError("Sensitivity result axes disagree with its input job.")
    response = np.asarray(result.get("absorption_cell_fraction"), dtype=float)
    if response.shape != (4,) or not np.all(np.isfinite(response)) or np.any(response < -1e-10):
        raise ValueError("Sensitivity result needs four finite nonnegative native responses.")
    residual = float(result.get("energy_balance_residual", float("nan")))
    if not np.isfinite(residual) or abs(residual) > 0.01:
        raise ValueError("Sensitivity anchor energy conservation has not passed 1%.")
    layers = result.get("layer_absorption", {})
    if "silicon" in layers and not np.isclose(
        layers["silicon"], np.sum(response), rtol=0, atol=1e-6
    ):
        raise ValueError("Sensitivity silicon absorption must equal the native-site sum.")
    fractions = np.asarray(
        [
            result.get("reflection"),
            result.get("transmission"),
            *[value for key, value in layers.items() if key != "silicon"],
        ],
        dtype=float,
    )
    if (
        not np.all(np.isfinite(fractions))
        or np.any(fractions < -1e-10)
        or np.any(fractions > 1 + 1e-10)
    ):
        raise ValueError("Sensitivity anchor needs finite passive R/T/layer fractions.")
    actual_residual = float(np.sum(response) + np.sum(fractions) - 1)
    if abs(actual_residual) > 0.01 or not np.isclose(actual_residual, residual, rtol=0, atol=1e-6):
        raise ValueError("Sensitivity reported energy residual disagrees with actual fractions.")
    return np.maximum(response, 0)


def _electron_response_bounds(responses: dict[tuple[Any, ...], np.ndarray]) -> dict[str, Any]:
    optical = np.stack(list(responses.values()))
    return {
        "native_sites": ["R", "Gr", "Gb", "B"],
        "unit": "electrons per photon incident on one pixel-equivalent area",
        "scope": "minimum/maximum over evaluated wavelength/angle/polarization anchors",
        "formula": "4 * absorption_cell_fraction * collection_efficiency",
        "by_collection_efficiency": [
            {
                "collection_efficiency": eta,
                "minimum_per_site": (4 * eta * np.min(optical, axis=0)).tolist(),
                "maximum_per_site": (4 * eta * np.max(optical, axis=0)).tolist(),
            }
            for eta in COLLECTION_EFFICIENCIES
        ],
    }


def assess_sensitivity(manifest_path: Path) -> dict[str, Any]:
    """Screen completed optical anchors; missing runs never become zero sensitivity."""

    manifest = json.loads(Path(manifest_path).read_text())
    expected_hash = manifest.pop("manifest_sha256")
    if _digest(manifest) != expected_hash:
        raise ValueError("Sensitivity manifest hash does not match its contents.")
    if _file_hash(Path(manifest["solver_snapshot"])) != manifest["solver_code_sha256"]:
        raise ValueError("Sensitivity solver snapshot changed after preparation.")
    jobs = {job["job_id"]: job for job in manifest["jobs"]}
    output = Path(manifest["output_dir"])
    baseline = {}
    for variant in manifest["variants"]:
        if variant["variant_id"] == "baseline":
            baseline[variant["report_code"]] = {
                _anchor_key(jobs[job_id]): _completed_response(jobs[job_id], output)
                for job_id in variant["job_ids"]
            }
    reports = []
    for variant in manifest["variants"]:
        if variant["variant_id"] == "baseline":
            continue
        reference = baseline[variant["report_code"]]
        responses = {
            _anchor_key(jobs[job_id]): _completed_response(jobs[job_id], output)
            for job_id in variant["job_ids"]
        }
        missing = sum(
            reference[key] is None or response is None for key, response in responses.items()
        )
        row = {
            "sensor_id": variant["sensor_id"],
            "report_code": variant["report_code"],
            "variant_id": variant["variant_id"],
            "missing_anchor_pairs": missing,
            "quantitative_ready": False,
            "measured_accuracy_verified": False,
        }
        if missing:
            row.update(status="pending_anchor_jobs", expansion_required=None)
        else:
            maximum_response, maximum_ratio = 0.0, 0.0
            for key, response in responses.items():
                base = reference[key]
                maximum_response = max(
                    maximum_response,
                    float(np.max(np.abs(response - base) / np.maximum(np.abs(base), 1e-12))),
                )
                if key[1] > 0:
                    normal_key = (key[0], 0.0, 0.0, key[3])
                    ratio = response / np.maximum(responses[normal_key], 1e-12)
                    base_ratio = base / np.maximum(reference[normal_key], 1e-12)
                    maximum_ratio = max(
                        maximum_ratio,
                        float(
                            np.max(
                                np.abs(ratio - base_ratio) / np.maximum(np.abs(base_ratio), 1e-12)
                            )
                        ),
                    )
            expand = max(maximum_response, maximum_ratio) > EXPANSION_THRESHOLD
            row.update(
                status="expansion_required" if expand else "anchor_screen_complete_unverified",
                expansion_required=expand,
                max_relative_response_change=maximum_response,
                max_relative_angular_ratio_change=maximum_ratio,
                electron_response_bounds=_electron_response_bounds(responses),
            )
        reports.append(row)
    return {
        "schema": "camerae2e_fdtd_sensitivity_screen_v1",
        "manifest_sha256": expected_hash,
        "estimate_only": True,
        "quantitative_ready": False,
        "reason": "Anchor screening does not replace convergence or full-grid validation.",
        "variants": reports,
        "pending_variants": sum(row["status"] == "pending_anchor_jobs" for row in reports),
        "expansion_required_count": sum(row["expansion_required"] is True for row in reports),
        "collection_efficiency": manifest["collection_efficiency"],
        "completed_baseline_electron_bounds": [
            {"report_code": code, **_electron_response_bounds(responses)}
            for code, responses in baseline.items()
            if all(response is not None for response in responses.values())
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--bundles", nargs="+", type=Path)
    prepare.add_argument(
        "--output-dir", type=Path, default=ROOT / "reports/fdtd_sensor_e2e/sensitivity"
    )
    prepare.add_argument("--resolution", type=int, default=80)
    assess = sub.add_parser("assess")
    assess.add_argument("--manifest", type=Path, required=True)
    assess.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.action == "prepare":
        paths = args.bundles or sorted(
            (ROOT / "reports/fdtd_sensor_e2e/materials").glob("def_*.json")
        )
        result = prepare_sensitivity(paths, args.output_dir, resolution=args.resolution)
        print(
            json.dumps(
                {
                    "manifest": str(args.output_dir / "sensitivity_campaign.json"),
                    "variants_including_baselines": len(result["variants"]),
                    "anchor_jobs": len(result["jobs"]),
                    "executed_jobs": 0,
                },
                indent=2,
            )
        )
    else:
        result = assess_sensitivity(args.manifest)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
