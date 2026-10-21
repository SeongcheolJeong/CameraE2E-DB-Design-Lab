"""RAW-observed electrical and effective-PSF calibration on frozen DB local probes.

Each retained field gets its own virtual local sensor. Its center coordinate is
not a mapping of a lens direction onto a physical full sensor. The DB broadband
Gr PSF replaces spatial response once; it is never applied to saved blurred RAW.
"""

from __future__ import annotations

import hashlib
import io
from copy import deepcopy
from pathlib import Path

import numpy as np
from PIL import Image

from . import calibration_db_optics_engine as optical
from . import calibration_lab_engine as lab
from . import calibration_lab_raw as observer
from . import reference_isp, virtual_lab_metrics
from .calibration_db_holdout import reference_raw
from .registry import canonical, digest

FIELDS = ("center", "outer-ne")
ROLES = ("training", "observation", "replicate", "before", "after")
ELECTRICAL = ("black_level_dn", "dn_per_e", "read_noise_e", "dark_current_e_per_s")
SCOPE = [
    "Synthetic independent acquisitions using a shared optical/ISP model; not physical validation.",
    "Original DB D65 450-650nm Gr broadband sampled PSF is the before spatial operator.",
    "Each field is a separate local 128x128 virtual sensor; no physical sensor coordinate mapping.",
    "The Gr response is a shared-RGB effective blur proxy, not a full spectral camera replay.",
    "Ideal new edge charts receive exactly one effective PSF; saved blurred rates are not reused.",
    "Dark/PTC identify black, DN/e, read noise and dark current; full well and FPN stay fixed.",
    "PTC comparison uses identical authored electron doses; no mean brightness adjustment.",
    "MTF uses two cardinal RAW Gr edges, at most 0.25 cycles/native pixel.",
    "PSF fit assumes an isotropic zero-phase Gaussian; physical PSF is not uniquely identified.",
    "Kernel OTF, RAW SFR and fixed-reference-ISP SFR are separate results.",
    "No QE or CCM fitting is performed here; the retained DB ISP transform remains fixed.",
]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical(value), encoding="utf-8")


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def profiles(prepared):
    """Retain actual DB sampled kernels; author a separate known synthetic target."""
    base = lab.default_profile()
    projection = prepared["projection"]
    base["native_shape"] = base["render_shape"] = [128, 128]
    base["pixel_pitch_um"] = projection["pixel_pitch_um"]
    base["electronics"] = deepcopy(projection["electronics"])
    base["conditions"].update(gain=projection["conditions"]["gain"], temperature_c=25.0)
    converted = optical.project_optical_profile(
        prepared, base, field_mapping_mode="reference_model"
    )
    if not converted["can_apply"]:
        raise ValueError("DB sampled PSF transfer failed qualification")
    base["conditions"].update(converted["profile_patch"]["conditions"])
    nodes = {n["source_field_id"]: n for n in converted["profile_patch"]["blur"]["field_nodes"]}
    result = {}
    for fid in FIELDS:
        p = deepcopy(base)
        p["blur"] = deepcopy(converted["profile_patch"]["blur"])
        n = deepcopy(nodes[fid])
        n.update(field_xy=[0.0, 0.0], field_coordinate_domain="sensor_normalized")
        p["blur"].update(
            field_nodes=[n],
            field_coordinate_domain="sensor_normalized",
            field_mapping_kind="independent_local_virtual_probe",
            field_mapping_mode="local_probe",
            initialization_only=False,
        )
        # Absolute electron dose for a unit neutral, from the retained DB field DC.
        dc = np.asarray(prepared["dc"][fid]["uniform_planes"], float)
        p["reference_white_e"] = float(
            dc[..., 1].mean() / 0.18 * prepared["spec"]["exposure_ms"] / 1000
        )
        p["db_local_probe"] = {
            "source_field_id": fid,
            "source_geometry": next(
                f["geometry"] for f in prepared["optical"]["fields"] if f["id"] == fid
            ),
            "physical_sensor_mapping": "not_established",
            "scope": SCOPE,
        }
        result[fid] = lab.validate_profile(p)
    return result, {k: v for k, v in converted.items() if k != "derived_files"}


def authored_truth(baselines, modules):
    result = deepcopy(baselines)
    for fi, fid in enumerate(FIELDS):
        p = result[fid]
        if "electronics" in modules:
            e = p["electronics"]
            e.update(
                black_level_dn=e["black_level_dn"] + 8,
                dn_per_e=e["dn_per_e"] * 1.15,
                read_noise_e=max(2.0, e["read_noise_e"] * 0.55),
                dark_current_e_per_s=max(10.0, e["dark_current_e_per_s"] * 1.6),
            )
        if "optics" in modules:
            p["blur"] = {
                **lab.default_profile()["blur"],
                "field_nodes": [
                    {
                        "field_xy": [0.0, 0.0],
                        "sigma_x_px": 1.35 + fi * 0.45,
                        "sigma_y_px": 1.35 + fi * 0.45,
                        "theta_deg": 0.0,
                    }
                ],
            }
        lab.validate_profile(p)
    return result


def specs(modules):
    result = []
    if "electronics" in modules:
        result += [
            {"id": "dark", "chart": "dark", "field": "center", "orientation": 0},
            {"id": "ptc", "chart": "ptc", "field": "center", "orientation": 0},
        ]
    if "optics" in modules:
        result += [
            {"id": f"{fid}-mtf-{angle}", "chart": "mtf", "field": fid, "orientation": angle}
            for fid in FIELDS
            for angle in (0, 90)
        ]
    return result


def acquisition_seed(seed, role, spec, level, repeat):
    ci = ("dark", "ptc", "mtf").index(spec["chart"])
    return (
        seed
        + ROLES.index(role) * 100000
        + FIELDS.index(spec["field"]) * 10000
        + ci * 1000
        + (spec["orientation"] // 90) * 500
        + level * 50
        + repeat
    )


def metadata(profile, spec, role, seed, exposure):
    shape = 64 if spec["chart"] in {"dark", "ptc"} else 128
    origin = (128 - shape) // 2
    return {
        "id": f"{role}-{spec['id']}",
        "chart": spec["chart"],
        "role": "training" if role == "training" else "final_validation",
        "source_kind": "synthetic",
        "measurement_domain": "camera_raw",
        "session_id": f"core-{seed}-{role}",
        "acquisition_id": f"core-{seed}-{role}-{spec['id']}",
        "conditions": {**profile["conditions"], "exposure_s": exposure},
        "raw_format": {
            "cfa": "RGGB",
            "bit_depth": profile["electronics"]["bit_depth"],
            "black_level_dn": profile["electronics"]["black_level_dn"],
            "response_type": "linear",
        },
        "geometry": {"full_sensor_shape": [128, 128], "crop_origin": [origin, origin]},
        "analysis": {
            "channel": "G1",
            "orientation_deg": spec["orientation"],
            "field_xy": [0.0, 0.0],
        },
        "observations": {},
        "source_field_id": spec["field"],
    }


def acquire(profile, baseline, prepared, spec, role, seed, root, progress=None):
    """Training/reference use independent NumPy readout, predictions electrical.capture."""
    directory = root / "acquisitions" / spec["id"] / role
    directory.mkdir(parents=True, exist_ok=False)
    exposure = prepared["spec"]["exposure_ms"] / 1000
    meta = metadata(baseline, spec, role, seed, exposure)
    chart = spec["chart"]
    if chart == "dark":
        x = np.array([0.001, 0.01, 0.04, 0.1, 0.3, 0.8, 2.5]) * (1 if role == "training" else 1.07)
        meta["analysis"]["exposure_s"] = x.tolist()
        charges, exposures, repeats = [np.zeros((64, 64)) for _ in x], x, 32
        x_unit = "s"
    elif chart == "ptc":
        x = np.array([8.0, 24.0, 60.0, 140.0, 300.0, 600.0, 1000.0]) * (
            1 if role == "training" else 1.07
        )
        charges, exposures, repeats = [np.full((64, 64), v) for v in x], [exposure] * len(x), 32
        x_unit = "authored photoelectrons/pixel"
        meta["analysis"].pop("exposure_s", None)
    else:
        x, x_unit = np.array([0.0]), "cycles/native pixel"
        m = {
            **meta,
            "observations": {"orientation_deg": spec["orientation"], "field_xy": [0.0, 0.0]},
        }
        scene, _ = lab._scene(profile, m, (128, 128))
        planes, kernel = lab._effective_blur(scene, profile["blur"]["field_nodes"][0])
        np.save(directory / "effective-kernel.npy", kernel, allow_pickle=False)
        rr, cc = np.indices((128, 128))
        channel = np.where(rr % 2 == 0, np.where(cc % 2 == 0, 0, 1), np.where(cc % 2 == 0, 1, 2))
        charges = [
            np.take_along_axis(planes, channel[..., None], axis=2)[..., 0]
            * profile["reference_white_e"]
        ]
        exposures, repeats = [exposure], 16
    e = profile["electronics"]
    readout = {
        k: e[k]
        for k in (
            "full_well_e",
            "read_noise_e",
            "dark_current_e_per_s",
            "bit_depth",
            "black_level_dn",
            "dn_per_e",
        )
    }
    readout["gain"] = profile["conditions"]["gain"]
    model = lab._electrical_model(profile)
    frames, seeds = [], []
    for li, (charge, t) in enumerate(zip(charges, exposures, strict=True)):
        stack = []
        for ri in range(repeats):
            stream = acquisition_seed(seed, role, spec, li, ri)
            seeds.append(stream)
            if role in {"training", "observation", "replicate"}:
                raw = reference_raw(charge / t, readout, t, stream)
            else:
                raw = lab._capture_electrical(profile, model, meta["geometry"], charge, t, stream)[
                    "arrays"
                ]["raw_dn"]
            stack.append(raw)
        frames.append(stack)
    raw = np.asarray(frames, dtype=np.uint16)
    data = io.BytesIO()
    np.save(data, raw, allow_pickle=False)
    (directory / "raw.npy").write_bytes(data.getvalue())
    observed, quality, hashes = observer.analyze_npy(meta, data.getvalue())
    if quality["not_fit_reasons"] or not observed:
        raise ValueError(
            f"{spec['id']} {role}: RAW observer rejected: {quality['not_fit_reasons']}"
        )
    measured = {
        **meta,
        "observations": observed,
        "quality": quality,
        "raw_sha256": checksum(data.getvalue()),
    }
    save(directory / "measurement.json", measured)
    # Same retained DB Reference ISP; readout calibration follows each applied model.
    means = raw.astype(float).mean(axis=1)
    linears = [
        np.mean(
            [
                reference_isp.process(
                    frame, readout, prepared["calibration"]["matrix"], prepared["spec"]["isp"]
                )[0]
                for frame in level
            ],
            axis=0,
        )
        for level in raw
    ]
    np.save(directory / "linear.npy", np.asarray(linears), allow_pickle=False)
    black, span = (0.0, 128.0) if chart == "dark" else (0.0, 1024.0)
    Image.fromarray(np.rint(np.clip((means[-1] - black) / span, 0, 1) * 255).astype(np.uint8)).save(
        directory / "raw.png"
    )
    Image.fromarray(np.rint(np.clip(linears[-1], 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)).save(
        directory / "isp.png"
    )
    metrics = (
        {"raw_mean_dn": observed["mean_dn"], "raw_variance_dn2": observed["variance_dn2"]}
        if chart != "mtf"
        else {}
    )
    if chart == "mtf":
        linear = linears[-1][..., 1]
        if spec["orientation"] == 90:
            linear = linear.T
        isp = virtual_lab_metrics.slanted_edge_metrics(linear)
        if isp["status"] != "ok":
            raise ValueError("ISP SFR could not be measured")
        metrics.update(
            raw_sfr={"x": observed["frequency_cy_px"], "y": observed["mtf"]},
            isp_sfr={"x": isp["frequency"]["cycles_per_pixel"], "y": isp["sfr"]},
        )
    else:
        metrics["isp_mean_rgb"] = [im[4:-4, 4:-4].mean(axis=(0, 1)).tolist() for im in linears]
    return {
        "measurement": measured,
        "metrics": metrics,
        "raw": str((directory / "raw.npy").relative_to(root)),
        "linear": str((directory / "linear.npy").relative_to(root)),
        "raw_sha256": measured["raw_sha256"],
        "frames": len(seeds),
        "seeds": seeds,
        "frame_hashes": hashes,
        "images": {k: str((directory / (k + ".png")).relative_to(root)) for k in ("raw", "isp")},
        "x": x.tolist(),
        "x_unit": x_unit,
        "readout": readout,
        "raw_display_range_dn": [black, black + span],
    }


def compare(series, key):
    values = {r: np.asarray(s["metrics"][key], float) for r, s in series.items() if r != "training"}
    return residuals(values)


def residuals(values):
    result = {"observation": values["observation"].tolist()}
    for role in ("before", "after", "replicate"):
        delta = values[role] - values["observation"]
        result[role] = {
            "values": values[role].tolist(),
            "residual": delta.tolist(),
            "rmse": float(np.sqrt(np.mean(delta**2))),
            "max_abs": float(np.max(np.abs(delta))),
        }
    b, a = result["before"]["rmse"], result["after"]["rmse"]
    result["reduction_percent"] = None if b == 0 else float(100 * (1 - a / b))
    return result


def sfr_comparison(series, key):
    curves = {r: s["metrics"][key] for r, s in series.items() if r != "training"}
    upper = min(0.25, *(max(c["x"]) for c in curves.values()))
    lower = max(min(c["x"]) for c in curves.values())
    if upper <= lower:
        raise ValueError("SFR curves have no shared observed frequency band")
    x = np.linspace(lower, upper, 41)
    result = residuals({r: np.interp(x, c["x"], c["y"]) for r, c in curves.items()})
    return {
        **result,
        "x": x.tolist(),
        "unit": "1",
        "frequency_unit": "cycles/native pixel",
        "scope": "shared measured frequency band; no extrapolation",
    }


def recover(baselines, candidates, truth):
    rows = []
    for key in ELECTRICAL:
        b, a, t = (
            profiles["center"]["electronics"][key] for profiles in (baselines, candidates, truth)
        )
        rows.append(
            {
                "parameter": "electronics." + key,
                "before": b,
                "after": a,
                "truth": t,
                "absolute_error": abs(a - t),
                "source": "known_synthetic_truth_only",
            }
        )
    for fid in FIELDS:
        for key in ("sigma_x_px", "sigma_y_px"):
            n = candidates[fid]["blur"]["field_nodes"][0]
            t = truth[fid]["blur"]["field_nodes"][0]
            if n.get("family") == "sampled_native_kernel_v1":
                continue
            rows.append(
                {
                    "parameter": fid + ".effective_psf." + key,
                    "before": None,
                    "after": n[key],
                    "truth": t[key],
                    "absolute_error": abs(n[key] - t[key]),
                    "source": "assumed_isotropic_zero_phase_gaussian_only",
                }
            )
    return rows


def execute(prepared, output, seed, modules, progress=None):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    baselines, conversion = profiles(prepared)
    truth = authored_truth(baselines, modules)
    protocol = {
        "schema": "camera_db_core_protocol_v1",
        "generator": "db_local_core_v1",
        "seed": seed,
        "modules": modules,
        "roles": list(ROLES),
        "scope": SCOPE,
        "source_identity": prepared["source_identity"],
        "specification": prepared["spec"],
        "candidate_frozen_before_holdout": True,
        "holdout_refit": False,
        "thresholds": {
            "dark.raw_mean_dn": 0.5,
            "ptc.raw_mean_dn": 2.0,
            "ptc.raw_variance_dn2": 5.0,
            "mtf.raw_sfr": 0.08,
            "mtf.isp_sfr": 0.08,
        },
        "reference_sampler": "independent NumPy Poisson/read-noise/ADC",
        "local_coordinate_policy": "separate virtual sensor per retained DB field",
    }
    save(root / "protocol.json", protocol)
    save(root / "baseline-profiles.json", baselines)
    save(root / "authored-truth.json", truth)
    save(root / "db-optical-projection.json", conversion)
    tasks, acquired = specs(modules), {}
    for spec in tasks:
        if progress:
            progress("학습 RAW 취득 · " + spec["id"])
        acquired[spec["id"]] = {
            "training": acquire(
                truth[spec["field"]],
                baselines[spec["field"]],
                prepared,
                spec,
                "training",
                seed,
                root,
            )
        }
    candidates, fits = {}, {}
    for fid in FIELDS:
        if progress:
            progress("Dark/PTC → RAW MTF 파라미터 추정 · " + fid)
        training = [
            acquired[s["id"]]["training"]["measurement"]
            for s in tasks
            if s["chart"] != "mtf" or s["field"] == fid
        ]
        fit = lab.fit_profile(baselines[fid], training, max_iterations=100)
        if any(d["status"] == "not_converged" for d in fit["diagnostics"]):
            raise ValueError("Calibration did not converge")
        candidates[fid], fits[fid] = (
            fit["profile"],
            {k: v for k, v in fit.items() if k != "profile"},
        )
    candidate_sha = digest(candidates)
    save(root / "candidate-profiles.json", candidates)
    save(root / "fit.json", fits)
    comparisons = []
    for spec in tasks:
        fid, series = spec["field"], acquired[spec["id"]]
        for role, profile in (
            ("observation", truth[fid]),
            ("replicate", truth[fid]),
            ("before", baselines[fid]),
            ("after", candidates[fid]),
        ):
            if progress:
                progress("고정 후보 독립 비교 · " + spec["id"] + " · " + role)
            series[role] = acquire(profile, baselines[fid], prepared, spec, role, seed, root)
        metrics = (
            {
                key: compare(series, key)
                for key in ("raw_mean_dn", "raw_variance_dn2", "isp_mean_rgb")
            }
            if spec["chart"] != "mtf"
            else {key: sfr_comparison(series, key) for key in ("raw_sfr", "isp_sfr")}
        )
        comparisons.append({**spec, "series": series, "metrics": metrics})
    if digest(candidates) != candidate_sha:
        raise ValueError("Holdout changed the frozen candidate")
    streams = [v for group in acquired.values() for role in group.values() for v in role["seeds"]]
    if len(streams) != len(set(streams)):
        raise ValueError("Acquisition streams overlap")
    checks = []
    for c in comparisons:
        for key, metric in c["metrics"].items():
            name = c["chart"] + "." + key
            if name in protocol["thresholds"]:
                limit = protocol["thresholds"][name]
                checks.append(
                    {
                        "chart": c["id"],
                        "metric": key,
                        "after_rmse": metric["after"]["rmse"],
                        "threshold": limit,
                        "passed": metric["after"]["rmse"] <= limit,
                    }
                )
    validation = {
        "numerical_tolerance_pass": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "source": "predeclared_synthetic_demo_thresholds",
        "physical_validation": "not_established",
    }
    return {
        "schema": "camera_db_core_comparison_v1",
        "status": "synthetic_holdout_completed",
        "modules": modules,
        "protocol": protocol,
        "scope": SCOPE,
        "comparisons": comparisons,
        "baseline_profiles": baselines,
        "candidate_profiles": candidates,
        "candidate_sha256": candidate_sha,
        "fit": fits,
        "parameter_recovery": recover(baselines, candidates, truth),
        "raw_frames": len(streams),
        "independent_streams": len(set(streams)),
        "refit_performed": False,
        "validation": validation,
        "physical_validation": "not_established",
    }
