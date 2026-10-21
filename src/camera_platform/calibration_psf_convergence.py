"""Bounded numerical qualification of frozen effective PSFs, before holdout.

This is discretization qualification, not another physical-parameter fit.
The finite search, reference refinement, metric bands and cost proxy are pinned.
"""

from __future__ import annotations

import io
import math
import time
from copy import deepcopy

import numpy as np
from scipy.fft import next_fast_len
from scipy.signal import fftconvolve

from . import calibration_db_core_engine as core
from . import calibration_lab_raw as observer
from . import calibration_sensor_crops as crops
from . import reference_isp, virtual_lab_metrics
from .calibration_db_optics_engine import transfer_mtf
from .registry import digest

POLICY = "effective_psf_sensor_crop_convergence_v2"
LIMITS = {"mtf_max_abs": 0.005, "mtf50_relative": 0.01, "tail_energy_bound": 0.001}
GRID = [(q, t) for q in (1, 2, 4, 8) for t in (3, 4, 5, 6)]
REFERENCES = [(8, 6), (16, 6), (16, 8)]
PHASES = (0.0, 0.25)
MAX_WORK_BYTES = 1024**3


def setting(q, t):
    if type(q) is not int or q not in (1, 2, 4, 8, 16):
        raise ValueError("PSF samples per pixel must be one of 1, 2, 4, 8, 16")
    if type(t) is not int or t not in (3, 4, 5, 6, 8):
        raise ValueError("PSF support must be one of 3, 4, 5, 6, 8 sigma")
    return {"samples_per_pixel": q, "support_sigma": t}


def key(s):
    return f"q{s['samples_per_pixel']}-s{s['support_sigma']}"


def parameters(node, s):
    setting(s["samples_per_pixel"], s["support_sigma"])
    if node.get("family", "centered_elliptical_gaussian_v1") != "centered_elliptical_gaussian_v1":
        raise ValueError("Numerical refinement requires an analytic effective Gaussian PSF")
    sx, sy = float(node["sigma_x_px"]), float(node["sigma_y_px"])
    if not all(math.isfinite(v) and 0.03 <= v <= 8 for v in (sx, sy)):
        raise ValueError("PSF sigma outside bounded numerical search")
    angle = float(node["theta_deg"])
    if not math.isfinite(angle):
        raise ValueError("PSF angle must be finite")
    q, t = s["samples_per_pixel"], s["support_sigma"]
    return sx, sy, angle, q, math.ceil(t * max(sx, sy) * q)


def cost(node, s, shape=(128, 128)):
    *_, q, r = parameters(node, s)
    # Actual edge-padded linear FFT extent: input + two halos + kernel - 1.
    fy, fx = (next_fast_len(n * q + 4 * r, real=True) for n in shape)
    area = fy * fx
    working = (shape[0] * shape[1] * q * q * 3 + area * 8 + (2 * r + 1) ** 2) * 8
    return {
        "kernel_shape": [2 * r + 1, 2 * r + 1],
        "pitch_px": 1 / q,
        "fft_shape": [fy, fx],
        "working_bytes_estimate": int(working),
        "fft_work_proxy": float(3 * area * math.log2(area)),
        # Union bound of the two Gaussian marginals, before renormalization.
        "tail_energy_bound": min(
            1.0, 2 * math.erfc((r / q) / max(node["sigma_x_px"], node["sigma_y_px"]) / math.sqrt(2))
        ),
    }


def kernel(node, s):
    sx, sy, angle, q, r = parameters(node, s)
    x = np.arange(-r, r + 1, dtype=float) / q
    y, x = np.meshgrid(x, x, indexing="ij")
    a = np.radians(angle)
    u, v = x * np.cos(a) + y * np.sin(a), -x * np.sin(a) + y * np.cos(a)
    k = np.exp(-0.5 * ((u / sx) ** 2 + (v / sy) ** 2))
    return k / k.sum()


def blur(scene, node, s):
    q = s["samples_per_pixel"]
    if scene.ndim != 3 or scene.shape[2] != 3 or any(n % q for n in scene.shape[:2]):
        raise ValueError("PSF scene must be a three-channel integer sampling grid")
    if (
        cost(node, s, tuple(n // q for n in scene.shape[:2]))["working_bytes_estimate"]
        > MAX_WORK_BYTES
    ):
        raise ValueError("PSF refinement exceeds the 1 GiB estimated working-memory bound")
    k = kernel(node, s)
    r = k.shape[0] // 2
    planes = []
    # Sequential channels bound peak memory; crop only after padded convolution.
    for c in range(3):
        padded = np.pad(scene[..., c], r, mode="edge")
        image = fftconvolve(padded, k, mode="same")[r:-r, r:-r]
        planes.append(image[q // 2 :: q, q // 2 :: q].copy())
    return np.maximum(0, np.stack(planes, axis=-1))


def edge_scene(s, orientation, phase=0.0, shape=(128, 128)):
    q = s["samples_per_pixel"]
    # All q sample the same native centers (n + .5), including q=1.
    y = (np.arange(shape[0] * q) / q + 0.5 - (q // 2) / q - shape[0] / 2)[:, None]
    x = (np.arange(shape[1] * q) / q + 0.5 - (q // 2) / q - shape[1] / 2)[None, :]
    a = np.radians(orientation)
    across, along = x * np.cos(a) + y * np.sin(a), y * np.cos(a) - x * np.sin(a)
    mono = 0.1 + 0.7 * (across > 0.07 * along + phase)
    return np.repeat(mono[..., None], 3, axis=-1)


def image_charge(profile, prepared, task, phase=0.0):
    """Same renderer used both in qualification and final noisy acquisitions."""
    from . import calibration_unified_engine as engine

    s, fid = profile["psf_numerics"]["setting"], task["field"]
    q = s["samples_per_pixel"]
    qe = np.asarray(profile["spectral_response"][fid])
    response = (
        np.asarray(profile["spectral_dc_gain"][fid])[:, None]
        * qe
        * prepared["spec"]["exposure_ms"]
        / 1000
    )
    rgb = response[:, [0, 1, 3]].copy()
    rgb[:, 1] = response[:, 1:3].mean(axis=1)
    node = profile["fields"][fid]["blur"]["field_nodes"][0]
    # Reject before allocating the supersampled scene.
    if cost(node, s)["working_bytes_estimate"] > MAX_WORK_BYTES:
        raise ValueError("PSF refinement exceeds the 1 GiB estimated working-memory bound")
    if task["chart"] == "macbeth":
        patterns = engine.color.charts._patterns("color", (128 * q, 128 * q))
        scene = (np.asarray(prepared["spectral"]["patches"]) @ rgb)[patterns]
    elif task["chart"] == "mtf":
        scene = edge_scene(s, task["orientation"], phase)
        scene *= np.asarray(prepared["spectral"]["white"]) @ rgb
    else:
        raise ValueError("Spatial refinement supports MTF and Macbeth charts")
    return engine.remosaic(blur(scene, node, s))


def with_numerics(profile, s):
    p = deepcopy(profile)
    p["psf_numerics"] = {"policy": POLICY, "setting": dict(s), "native_center_phase_px": 0.5}
    p.pop("profile_sha256", None)
    p["profile_sha256"] = digest(p)
    return p


def crossing(x, y):
    x, y = np.asarray(x), np.asarray(y)
    ix = np.flatnonzero((y[:-1] >= 0.5) & (y[1:] < 0.5))
    if not len(ix):
        return None
    i = ix[0]
    return float(x[i] + (0.5 - y[i]) * (x[i + 1] - x[i]) / (y[i + 1] - y[i]))


def compare_curve(a, b):
    """No extrapolation or invented MTF50 for out-of-band crossings."""
    x, y, rx, ry = (np.asarray(v, float) for v in (a["x"], a["y"], b["x"], b["y"]))
    if (
        any(v.ndim != 1 or len(v) < 3 or not np.isfinite(v).all() for v in (x, y, rx, ry))
        or len(x) != len(y)
        or len(rx) != len(ry)
        or np.any(np.diff(x) <= 0)
        or np.any(np.diff(rx) <= 0)
    ):
        return {"passed": False, "reason": "invalid_curve"}
    if x[0] > rx[0] + 1e-12 or x[-1] < rx[-1] - 1e-12:
        return {"passed": False, "reason": "candidate_band_does_not_cover_reference"}
    residual = np.interp(rx, x, y) - ry
    ca, cb = crossing(x, y), crossing(rx, ry)
    relative = None if ca is None or cb is None or cb <= 0 else abs(ca - cb) / cb
    maximum = float(np.max(np.abs(residual)))
    return {
        "max_abs": maximum,
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "mtf50": ca,
        "reference_mtf50": cb,
        "mtf50_relative": relative,
        "passed": bool(
            maximum <= LIMITS["mtf_max_abs"]
            and relative is not None
            and relative <= LIMITS["mtf50_relative"]
        ),
        "reason": "mtf50_unavailable" if relative is None else None,
    }


def compare(a, b):
    checks = []
    for fid, data in b["fields"].items():
        for angle, reference in data["kernel_curves"].items():
            checks.append(
                {
                    "field": fid,
                    "chart": f"kernel-{angle}",
                    "stage": "kernel",
                    **compare_curve(a["fields"][fid]["kernel_curves"][angle], reference),
                }
            )
        for cid, capture in data["captures"].items():
            for stage, reference in capture["curves"].items():
                checks.append(
                    {
                        "field": fid,
                        "chart": cid,
                        "stage": stage,
                        **compare_curve(
                            a["fields"][fid]["captures"][cid]["curves"][stage], reference
                        ),
                    }
                )
    return {"passed": all(c["passed"] for c in checks), "checks": checks}


def measure(profile, prepared, s, root, progress):
    p = crops.expand(with_numerics(profile, s))
    output = {"id": key(s), "setting": s, "fields": {}, "profile_sha256": p["profile_sha256"]}
    for fid in p["fields"]:
        if progress:
            progress(f"PSF 수렴 · {key(s)} · {fid} · RAW/ISP 두 방향·두 위치")
        node = p["fields"][fid]["blur"]["field_nodes"][0]
        located = fid.startswith("crop-")
        estimates = crops.cost(node, s) if located else cost(node, s)
        if estimates["working_bytes_estimate"] > MAX_WORK_BYTES:
            raise ValueError(
                "PSF reference exceeds working-memory budget; no unverified selection"
            )
        folder = root / "convergence" / key(s) / fid
        folder.mkdir(parents=True, exist_ok=False)
        k = kernel(node, s)
        np.save(folder / "kernel.npy", k, allow_pickle=False)
        frequency = np.linspace(0, 0.5, 201)
        mtfs = transfer_mtf(k, frequency, [0, 90], spacing=1 / s["samples_per_pixel"])
        data = {
            "cost": estimates,
            "kernel": str((folder / "kernel.npy").relative_to(root)),
            "kernel_curves": {
                str(angle): {"x": frequency.tolist(), "y": m.tolist()}
                for angle, m in zip((0, 90), mtfs, strict=True)
            },
            "captures": {},
        }
        readout = {**p["electronics"], "gain": prepared["spec"]["gain"]}
        model = core.lab._electrical_model(p["fields"][fid])
        start = time.perf_counter()
        for angle in (0, 90):
            for phase in PHASES:
                cid = f"edge-{angle}-phase-{int(phase * 100):02d}"
                task = {"id": cid, "field": fid, "chart": "mtf", "orientation": angle}
                if located:
                    raw, linear, meta, placement, raw_halo = crops.capture(
                        p, prepared, fid, s, angle, phase
                    )
                else:
                    charge = image_charge(p, prepared, task, phase)
                    raw = core.lab._capture_electrical(
                        p["fields"][fid],
                        model,
                        {},
                        charge,
                        prepared["spec"]["exposure_ms"] / 1000,
                        0,
                        noise_enabled=False,
                    )["arrays"]["raw_dn"].astype(np.uint16)
                    linear = reference_isp.process(
                        raw, readout, p["isp"]["electron_to_output_matrix"], p["isp_tuning"]
                    )[0]
                    meta = core.metadata(
                        p["fields"][fid],
                        task,
                        "observation",
                        0,
                        prepared["spec"]["exposure_ms"] / 1000,
                    )
                buf = io.BytesIO()
                np.save(buf, raw, allow_pickle=False)
                obs, quality, _ = observer.analyze_npy(meta, buf.getvalue())
                if quality["not_fit_reasons"]:
                    raise ValueError(
                        "Convergence RAW MTF unavailable: " + str(quality["not_fit_reasons"])
                    )
                image = linear[..., 1].T if angle == 90 else linear[..., 1]
                sfr = virtual_lab_metrics.slanted_edge_metrics(image)
                if sfr["status"] != "ok":
                    raise ValueError("Convergence ISP MTF unavailable")
                filename = folder / (cid + ".npz")
                np.savez_compressed(
                    filename,
                    raw=raw,
                    linear=linear,
                    **({"raw_with_isp_halo": raw_halo} if located else {}),
                )
                data["captures"][cid] = {
                    "metadata": meta,
                    "sensor_placement": placement if located else None,
                    "file": str(filename.relative_to(root)),
                    "sha256": core.checksum(filename.read_bytes()),
                    "curves": {
                        "raw": {"x": obs["frequency_cy_px"], "y": obs["mtf"]},
                        "isp": {"x": sfr["frequency"]["cycles_per_pixel"], "y": sfr["sfr"]},
                    },
                }
        data["elapsed_seconds"] = time.perf_counter() - start
        output["fields"][fid] = data
    output["fft_work_proxy"] = sum(f["cost"]["fft_work_proxy"] for f in output["fields"].values())
    output["tail_energy_pass"] = all(
        f["cost"]["tail_energy_bound"] <= LIMITS["tail_energy_bound"]
        for f in output["fields"].values()
    )
    return output


def select(candidates, references_pass):
    valid = (
        [c for c in candidates if c["comparison"]["passed"] and c["tail_energy_pass"]]
        if references_pass
        else []
    )
    return (
        min(
            valid,
            key=lambda c: (
                c["fft_work_proxy"],
                c["setting"]["samples_per_pixel"],
                c["setting"]["support_sigma"],
            ),
        )
        if valid
        else None
    )


def qualify(profile, prepared, root, progress=None):
    if profile.get("sensor_crops", {}).get("psf_source") == (
        "direct_prescription_reference_sphere_scalar_diffraction"
    ):
        profile = deepcopy(profile)
        profile.pop("sensor_crops")
    evidence = {}
    # Establish the reference independently before choosing a candidate.
    for q, t in [*REFERENCES, *GRID]:
        s = setting(q, t)
        if key(s) not in evidence:
            evidence[key(s)] = measure(profile, prepared, s, root, progress)
    reference = evidence[key(setting(16, 8))]
    ref_checks = [
        {
            "axis": "sampling",
            "from": "q8-s6",
            "to": "q16-s6",
            **compare(evidence["q8-s6"], evidence["q16-s6"]),
        },
        {
            "axis": "support",
            "from": "q16-s6",
            "to": "q16-s8",
            **compare(evidence["q16-s6"], reference),
        },
    ]
    reference_pass = all(c["passed"] for c in ref_checks) and reference["tail_energy_pass"]
    candidates = []
    for q, t in GRID:
        c = evidence[key(setting(q, t))]
        c["comparison"] = compare(c, reference)
        c["passed"] = reference_pass and c["comparison"]["passed"] and c["tail_energy_pass"]
        candidates.append(c)
    selected = select(candidates, reference_pass)
    crop_parity = None
    if selected and profile.get("sensor_crops"):
        if progress:
            progress("센서 Crop · 확장 캔버스/광학 halo/ISP halo/CFA 일치 검사")
        crop_parity = crops.parity(profile, prepared, selected["setting"], root)
        if not crop_parity["passed"]:
            selected = None
    result = {
        "sensor_crops": profile.get("sensor_crops"),
        "crop_parity": crop_parity,
        "schema": POLICY,
        "status": "passed" if selected else "not_converged",
        "limits": LIMITS,
        "frozen_physical_profile_sha256": profile["profile_sha256"],
        "reference_id": reference["id"],
        "reference_checks": ref_checks,
        "reference_pass": reference_pass,
        "selected": None
        if selected is None
        else {
            "id": selected["id"],
            "setting": selected["setting"],
            "fft_work_proxy": selected["fft_work_proxy"],
            "cost_by_field": {f: v["cost"] for f, v in selected["fields"].items()},
        },
        "candidates": candidates,
        "references": [evidence[key(setting(*s))] for s in REFERENCES],
        "cost_method": (
            "sum of all tested fields: channel count * padded FFT cells * log2(cells);"
            " native 128x128 crops plus ISP halo"
            "; one factored mono FFT for crops, "
            "three for legacy RGB; timing inform"
            "ational only"
        ),
        "search_domain": {
            "samples_per_pixel": [1, 2, 4, 8],
            "support_sigma": [3, 4, 5, 6],
            "edge_orientation_deg": [0, 90],
            "edge_phase_native_px": list(PHASES),
        },
        "scope": [
            (
                "Frozen fitted effective Gaussian PSF; no physical param"
                "eter refit or holdout-data selection."
            ),
            (
                "Deterministic integer RAW with noise disabled, same rea"
                "dout and ISP at every resolution."
            ),
            (
                "RAW Gr 0-0.25 cycles/native pixel; ISP G and kernel 0-0"
                ".5. Missing MTF50 fails closed."
            ),
            (
                "Two retained virtual probes plus ni"
                "ne globally located sensor crops wh"
                "en geometry is bound; "
                "declared radial effective-model interpolation, not nine physical measurements."
            ),
            (
                "Original DB native pixel-integrated kernel remains unch"
                "anged; subpixel source sampling not recovered."
            ),
            (
                "Least estimated FFT work among this finite passing grid"
                ", not a universal hardware-cost optimum."
            ),
            (
                "Legacy probes use edge replication;"
                " located crops evaluate global scen"
                "e halos before native "
                "RAW/ISP cropping. Local stationary PSF, no long-range flare qualification."
            ),
        ],
        "noise_enabled": False,
        "max_estimated_working_bytes": MAX_WORK_BYTES,
    }
    core.save(root / "convergence" / "result.json", result)
    return result
