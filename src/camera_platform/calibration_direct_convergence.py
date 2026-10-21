"""Physical DB PSF sampling/support qualification and located Bayer captures.

Slanted edges use exact discrete PSF projection at rational slope 7/100. A CDF
executes the same convolution without a large 2-D FFT. Native square-pixel area
integration follows optical convolution once. No detector convolution is folded
into the source optical PSF, and no Gaussian approximation is introduced.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np

from . import calibration_db_core_engine as core
from . import calibration_direct_optics as optics
from . import calibration_lab_raw as observer
from . import calibration_psf_convergence as legacy
from . import reference_isp, virtual_lab_metrics
from .registry import digest

POLICY = "direct_db_polychromatic_psf_convergence_v1"
GRID = [(q, r) for q in (1, 2, 4, 8, 16) for r in (8, 16, 32, 48, 64)]
REFERENCES = [(8, 64), (16, 64), (16, 96)]
LIMITS = {
    "mtf_max_abs": 0.005,
    "mtf50_relative": 0.01,
    "discarded_mass_to_reference": 0.001,
    "reference_minimum_retained_energy": 0.95,
}
PHASES = (0.0, 0.25)
ISP_HALO = 8
PHOTON_SCALE = 4.0


def setting(q, r):
    if type(q) is not int or q not in (1, 2, 4, 8, 16):
        raise ValueError("Direct PSF q must be 1,2,4,8,16")
    if type(r) is not int or r not in (8, 16, 32, 48, 64, 96):
        raise ValueError("Direct PSF support radius must be 8,16,32,48,64,96 native pixels")
    return {"samples_per_pixel": q, "radius_native_px": r}


def key(s):
    return f"dq{s['samples_per_pixel']}-r{s['radius_native_px']}"


def sampled_kernel(mass, s):
    q, r = s["samples_per_pixel"], s["radius_native_px"]
    setting(q, r)
    center = optics.REFERENCE_RADIUS * optics.REFERENCE_Q
    if mass.shape != (2 * center + 1, 2 * center + 1, 3) or mass.dtype.kind != "f":
        raise ValueError("Direct PSF must retain the full independently propagated reference grid")
    step = optics.REFERENCE_Q // q
    span = r * optics.REFERENCE_Q
    result = mass[
        center - span : center + span + 1 : step, center - span : center + span + 1 : step
    ].copy()
    result *= step**2
    if not np.isfinite(result).all() or np.any(result < 0) or np.any(result.sum(axis=(0, 1)) <= 0):
        raise ValueError("Direct kernel has invalid optical energy")
    return result


def cost(s, extra=0):
    q, r = s["samples_per_pixel"], s["radius_native_px"]
    cells = (2 * r * q + 1) ** 2
    pixels = (128 + 2 * (ISP_HALO + extra)) ** 2
    runtime_bytes = int(cells * (12 + 4 + 16) + pixels * q * q * 16 + 8 * (214 * r * q + 2) * 3)
    reference_bytes = (2 * optics.REFERENCE_RADIUS * optics.REFERENCE_Q + 1) ** 2 * 12
    return {
        "runtime_working_bytes_estimate": runtime_bytes,
        "reference_resident_bytes": reference_bytes,
        "kernel_shape": [2 * r * q + 1] * 2,
        "pitch_px": 1 / q,
        "operation_proxy": int(3 * (cells + pixels * q * q)),
        "working_bytes_estimate": runtime_bytes + reference_bytes,
        "method": (
            "three spectral-kernel projections plus exact CDF lookups and receiver area integration"
        ),
    }


def optical_curves(k, q):
    x = np.linspace(0, 0.5, 201)
    axis = (np.arange(k.shape[0]) - k.shape[0] // 2) / q
    result = {}
    for ch, name in enumerate(("R", "G", "B")):
        for angle, dimension in ((0, 0), (90, 1)):
            marginal = k[..., ch].sum(axis=dimension, dtype=np.float64)
            otf = np.exp(-2j * np.pi * x[:, None] * axis) @ marginal / marginal.sum()
            result[f"{name}-{angle}"] = {"x": x.tolist(), "y": np.abs(otf).tolist()}
    return result


def projection_cdf(k, angle):
    """Exact discrete projection: optical x right, array y down, rational slant."""
    n = k.shape[0]
    r = n // 2
    axis = np.arange(-r, r + 1, dtype=np.int32)
    indices = (
        100 * axis[None, :] - 7 * axis[:, None]
        if angle == 0
        else 100 * axis[:, None] + 7 * axis[None, :]
    ) + 107 * r
    result = []
    for ch in range(3):
        hist = np.bincount(indices.ravel(), weights=k[..., ch].ravel(), minlength=214 * r + 1)
        result.append(np.r_[0.0, np.cumsum(hist, dtype=np.float64) / hist.sum()])
    return result, 107 * r


def edge_charge(g, c, s, angle, phase, cdf, offset, extra=0):
    from .calibration_unified_engine import remosaic

    q = s["samples_per_pixel"]
    h = ISP_HALO + extra
    n = 128 + 2 * h
    origin = [o - h for o in c["origin_row_col"]]
    if any(o < 0 or o + n > full for o, full in zip(origin, g["full_sensor_shape"], strict=True)):
        raise ValueError("Receiving RAW/ISP halo leaves the active sensor")
    # Coordinates in half-subpixel units avoid phase/crop-origin floating drift.
    y = (2 * q * (origin[0] - c["center_active_row_col"][0]) + 2 * np.arange(n * q) + 1 - q).astype(
        np.int64
    )
    x = (2 * q * (origin[1] - c["center_active_row_col"][1]) + 2 * np.arange(n * q) + 1 - q).astype(
        np.int64
    )
    numerator = (
        100 * x[None, :] - 7 * y[:, None] if angle == 0 else 100 * y[:, None] + 7 * x[None, :]
    )
    indices = (numerator - int(200 * q * phase) - 1) // 2 + offset + 1
    planes = []
    for ch in range(3):
        high = 0.1 + 0.7 * np.take(cdf[ch], np.clip(indices, 0, len(cdf[ch]) - 1))
        # Exactly one receiving square-pixel area integral, after optical convolution.
        native = high.reshape(n, q, n, q).mean(axis=(1, 3)) * g["white_e_rgb"][ch] * PHOTON_SCALE
        planes.append(native)
    return remosaic(np.stack(planes, axis=-1)), origin


def capture(profile, g, c, s, angle, phase, cdf, offset, extra=0):
    charge, origin = edge_charge(g, c, s, angle, phase, cdf, offset, extra)
    local = profile["fields"]["center"]
    raw = core.lab._capture_electrical(
        local,
        core.lab._electrical_model(local),
        {},
        charge,
        profile["conditions"]["exposure_ms"] / 1000,
        0,
        noise_enabled=False,
    )["arrays"]["raw_dn"].astype(np.uint16)
    linear = reference_isp.process(
        raw,
        {**profile["electronics"], "gain": profile["conditions"]["gain"]},
        profile["isp"]["electron_to_output_matrix"],
        profile["isp_tuning"],
    )[0]
    h = ISP_HALO + extra
    meta = core.metadata(
        local,
        {"id": c["id"], "field": c["id"], "chart": "mtf", "orientation": angle},
        "observation",
        0,
        profile["conditions"]["exposure_ms"] / 1000,
    )
    meta["geometry"] = {
        "full_sensor_shape": g["full_sensor_shape"],
        "crop_origin": c["origin_row_col"],
    }
    meta["analysis"].pop("field_xy", None)
    meta["conditions"]["incident_photons_per_pixel"] = (
        g["incident_photons_per_pixel"] * PHOTON_SCALE
    )
    meta["conditions"]["illumination_scope"] = (
        "normalized D65 photon dose, not absolute DB field irradiance"
    )
    return raw[h:-h, h:-h].copy(), linear[h:-h, h:-h].copy(), raw, meta, origin


def curves(raw, linear, meta, angle):
    buf = io.BytesIO()
    np.save(buf, raw, allow_pickle=False)
    obs, quality, _ = observer.analyze_npy(meta, buf.getvalue())
    if quality["not_fit_reasons"]:
        raise ValueError("Direct RAW edge is not measurable: " + str(quality["not_fit_reasons"]))
    sfr = virtual_lab_metrics.slanted_edge_metrics(
        linear[..., 1].T if angle == 90 else linear[..., 1]
    )
    if sfr["status"] != "ok":
        raise ValueError("Direct ISP edge is not measurable")
    return {
        "raw": {"x": obs["frequency_cy_px"], "y": obs["mtf"]},
        "isp": {"x": sfr["frequency"]["cycles_per_pixel"], "y": sfr["sfr"]},
    }, obs["field_xy"]


def compare_curve(a, b):
    result = legacy.compare_curve(a, b)
    # Sharp physical lenses may never reach MTF50 inside the Bayer Gr band.
    # Keep the measured-band curve gate; explicitly report both out-of-band values.
    if (
        result.get("reason") == "mtf50_unavailable"
        and result.get("mtf50") is None
        and result.get("reference_mtf50") is None
        and min(a["y"]) > 0.5
        and min(b["y"]) > 0.5
    ):
        result.update(
            passed=result["max_abs"] <= LIMITS["mtf_max_abs"],
            reason="both_mtf50_above_measurable_band",
            mtf50_check="out_of_band",
        )
    return result


def compare(a, b):
    checks = []
    for fid, reference in b["fields"].items():
        current = a["fields"][fid]
        if current.get("status") != "measured":
            checks.append(
                {
                    "field": fid,
                    "stage": "source",
                    "chart": "all",
                    "passed": False,
                    "reason": current.get("reason", "not_measured"),
                }
            )
            continue
        for name, ref in reference["kernel_curves"].items():
            checks.append(
                {
                    "field": fid,
                    "stage": "optical",
                    "chart": name,
                    **compare_curve(current["kernel_curves"][name], ref),
                }
            )
        for name, ref in reference["captures"].items():
            for stage, curve in ref["curves"].items():
                checks.append(
                    {
                        "field": fid,
                        "stage": stage,
                        "chart": name,
                        **compare_curve(current["captures"][name]["curves"][stage], curve),
                    }
                )
        residual = np.asarray(current["energy_rgb"]) / np.asarray(reference["energy_rgb"]) - 1
        checks.append(
            {
                "field": fid,
                "stage": "mass",
                "chart": "relative_to_finite_reference",
                "max_abs": float(np.max(abs(residual))),
                "passed": bool(np.max(abs(residual)) <= LIMITS["discarded_mass_to_reference"]),
            }
        )
    return {"passed": all(row["passed"] for row in checks), "checks": checks}


def _minimum_q(root, field):
    values = []
    for wave in field["wavelengths"]:
        with np.load(root / "direct-optics" / wave["rays_file"], allow_pickle=False) as saved:
            span = float(np.max(np.ptp(saved["fine_q"], axis=0)) * 1.04)
        values.append(2 * span * 2.9 / (wave["wavelength_nm"] * 0.001))
    return max(values)


def qualify(profile, source, root, progress=None):
    root = Path(root)
    g = profile["sensor_crops"]
    optics.validate(g)
    settings = [setting(q, r) for q, r in dict.fromkeys([*REFERENCES, *GRID])]
    evidence = {
        key(s): {"id": key(s), "setting": s, "fields": {}, "operation_proxy": 0} for s in settings
    }
    source_fields = {f["field"]: f for f in source["fields"]}
    for c in g["crops"]:
        fid = c["id"]
        source_field = source_fields[fid]
        path = root / "direct-optics" / source_field["file"]
        if optics.sha(path) != source_field["sha256"]:
            raise ValueError("Direct field PSF bytes differ from frozen source")
        with np.load(path, allow_pickle=False) as saved:
            mass = saved["mass_rgb"]
        required_q = _minimum_q(root, source_field)
        for s in settings:
            row = evidence[key(s)]
            estimates = cost(s)
            data = {"status": "refused", "cost": estimates, "minimum_point_sampling_q": required_q}
            row["fields"][fid] = data
            row["operation_proxy"] += estimates["operation_proxy"]
            if s["samples_per_pixel"] < required_q:
                data["reason"] = "optical_intensity_nyquist_not_satisfied"
                continue
            if estimates["working_bytes_estimate"] > optics.MAX_WORKING_BYTES:
                data["reason"] = "working_memory_budget_exceeded"
                continue
            if progress:
                progress(f"직접 PSF 수렴 · {fid} · {key(s)} · RAW/ISP 두 방향·두 위상")
            k = sampled_kernel(mass, s)
            data.update(
                status="measured",
                energy_rgb=k.sum(axis=(0, 1), dtype=np.float64).tolist(),
                kernel_curves=optical_curves(k, s["samples_per_pixel"]),
                captures={},
            )
            folder = root / "direct-convergence" / key(s) / fid
            folder.mkdir(parents=True, exist_ok=True)
            for angle in (0, 90):
                cdf, offset = projection_cdf(k, angle)
                for phase in PHASES:
                    cid = f"edge-{angle}-phase-{int(phase * 100):02d}"
                    raw, linear, halo, meta, origin = capture(
                        profile, g, c, s, angle, phase, cdf, offset
                    )
                    measured, xy = curves(raw, linear, meta, angle)
                    name = folder / (cid + ".npz")
                    np.savez_compressed(name, raw=raw, linear=linear, raw_with_isp_halo=halo)
                    data["captures"][cid] = {
                        "file": str(name.relative_to(root)),
                        "sha256": optics.sha(name),
                        "curves": measured,
                        "metadata": meta,
                        "observed_sensor_normalized_field_xy": xy,
                        "raw_halo_origin_row_col": origin,
                        "isp_halo_native_px": ISP_HALO,
                    }
    reference = evidence[key(setting(16, 96))]
    # Every reference field must exist before any comparison/selection is permitted.
    if any(f["status"] != "measured" for f in reference["fields"].values()):
        raise ValueError("Direct reference grid exceeds physical/resource preflight")
    refchecks = [
        {
            "axis": "sampling",
            "from": "dq8-r64",
            "to": "dq16-r64",
            **compare(evidence["dq8-r64"], evidence["dq16-r64"]),
        },
        {
            "axis": "support",
            "from": "dq16-r64",
            "to": "dq16-r96",
            **compare(evidence["dq16-r64"], reference),
        },
    ]
    reference_pass = all(r["passed"] for r in refchecks)
    candidates = []
    for q, r in GRID:
        row = evidence[key(setting(q, r))]
        row["comparison"] = compare(row, reference)
        row["passed"] = reference_pass and row["comparison"]["passed"]
        candidates.append(row)
    valid = [c for c in candidates if c["passed"]]
    selected = (
        min(
            valid,
            key=lambda c: (
                c["operation_proxy"],
                c["setting"]["samples_per_pixel"],
                c["setting"]["radius_native_px"],
            ),
        )
        if valid
        else None
    )
    parity = []
    applied = []
    if selected:
        for c in g["crops"]:
            with np.load(
                root / "direct-optics" / source_fields[c["id"]]["file"], allow_pickle=False
            ) as z:
                k = sampled_kernel(z["mass_rgb"], selected["setting"])
            path = root / "direct-convergence" / (c["id"] + "-selected-kernel.npz")
            np.savez_compressed(path, mass_rgb=k)
            applied.append(
                {
                    "field": c["id"],
                    "file": str(path.relative_to(root)),
                    "sha256": optics.sha(path),
                    "setting": selected["setting"],
                    "source_kind": "direct_prescription_diffraction",
                    "included_effects": g["psf_included_effects"],
                    "pixel_aperture_included": False,
                }
            )
            for angle in (0, 90):
                cdf, offset = projection_cdf(k, angle)
                for phase in PHASES:
                    a = capture(profile, g, c, selected["setting"], angle, phase, cdf, offset)
                    b = capture(
                        profile, g, c, selected["setting"], angle, phase, cdf, offset, extra=16
                    )
                    raw_error = int(np.max(abs(a[0].astype(int) - b[0].astype(int))))
                    isp_error = float(np.max(abs(a[1] - b[1])))
                    parity.append(
                        {
                            "field": c["id"],
                            "angle": angle,
                            "phase": phase,
                            "raw_max_abs_dn": raw_error,
                            "isp_max_abs": isp_error,
                            "passed": raw_error == 0 and isp_error <= 1e-12,
                        }
                    )
    parity_pass = bool(parity) and all(c["passed"] for c in parity)
    result = {
        "schema": POLICY,
        "status": "passed" if selected and parity_pass else "not_converged",
        "geometry_sha256": g["geometry_sha256"],
        "geometry": g,
        "source": source,
        "limits": LIMITS,
        "reference_pass": reference_pass,
        "reference_id": reference["id"],
        "reference_checks": refchecks,
        "references": [evidence[key(setting(*s))] for s in REFERENCES],
        "candidates": candidates,
        "selected": None
        if selected is None
        else {
            "id": selected["id"],
            "setting": selected["setting"],
            "operation_proxy": selected["operation_proxy"],
            "cost_by_field": {f: v["cost"] for f, v in selected["fields"].items()},
        },
        "applied_kernels": applied,
        "crop_parity": {"passed": parity_pass, "checks": parity},
        "cost_method": (
            "sum of exact discrete kernel projec"
            "tion and native-area CDF lookup cou"
            "nts; finite candidate grid"
        ),
        "energy_scope": (
            "discarded mass relative to independ"
            "ently propagated finite ±96 pixel r"
            "eference; unknown energy outside th"
            "at window retained explicitly"
        ),
        "mtf50_scope": (
            "both above measured band is reported out_of_band; curve tolerance still applies"
        ),
        "capture_photons_per_pixel": g["incident_photons_per_pixel"] * PHOTON_SCALE,
        "noise_enabled": False,
        "pixel_aperture_application_count": 1,
        "physical_validation": "not_established",
    }
    optics.save(root / "direct-convergence" / "result.json", result)
    return result


def applied_profile(profile, result):
    from copy import deepcopy

    p = deepcopy(profile)
    p["sensor_crops"]["numerical_application"] = {
        "policy": POLICY,
        "status": result["status"],
        "selected": result["selected"],
        "kernels": result["applied_kernels"],
        "source_geometry_sha256": p["sensor_crops"]["geometry_sha256"],
        "capture_photons_per_pixel": result["capture_photons_per_pixel"],
    }
    # Application is not part of the frozen source-geometry contract.
    p.pop("profile_sha256", None)
    p["profile_sha256"] = digest(p)
    return p


def capture_applied(profile, root, field_id, angle=0, phase=0.0):
    """Execute the selected kernel of a verified active camera profile directly."""
    if profile.get("profile_sha256") != digest(
        {k: v for k, v in profile.items() if k != "profile_sha256"}
    ):
        raise ValueError("Applied camera profile hash differs")
    g = profile["sensor_crops"]
    optics.validate(g)
    application = g.get("numerical_application", {})
    if application.get("status") != "passed" or application.get("policy") != POLICY:
        raise ValueError("Direct PSF application has not passed")
    if angle not in (0, 90) or phase not in PHASES:
        raise ValueError("Select a qualified edge orientation and phase")
    if application["capture_photons_per_pixel"] != g["incident_photons_per_pixel"] * PHOTON_SCALE:
        raise ValueError("Applied direct-edge photon dose differs")
    rows = [r for r in application["kernels"] if r["field"] == field_id]
    crops = [c for c in g["crops"] if c["id"] == field_id]
    if len(rows) != 1 or len(crops) != 1:
        raise ValueError("Applied profile has no unique direct PSF for this sensor crop")
    relative = Path(rows[0]["file"])
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(base):
        raise ValueError("Applied kernel must be inside the immutable result")
    if optics.sha(path) != rows[0]["sha256"]:
        raise ValueError("Applied direct PSF kernel bytes differ")
    s = rows[0]["setting"]
    setting(s["samples_per_pixel"], s["radius_native_px"])
    with np.load(path, allow_pickle=False) as saved:
        k = saved["mass_rgb"]
    expected = 2 * s["samples_per_pixel"] * s["radius_native_px"] + 1
    if k.shape != (expected, expected, 3) or not np.isfinite(k).all() or np.any(k < 0):
        raise ValueError("Applied direct PSF has invalid shape or mass")
    cdf, offset = projection_cdf(k, angle)
    return capture(profile, g, crops[0], s, angle, phase, cdf, offset)
