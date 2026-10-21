"""Cached ghost bundles -> native Reference photon/electron probes -> RAW/ISP."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

from . import reference_ghost as core
from . import reference_workshop_optics as optics
from . import reference_workshop_sensor as pixel
from .design_workshop_engine import write_json
from .registry import digest
from .virtual_lab_job import sha256


def load_prescription(workspace):
    # Reuse the exact reviewed DB material evaluator in its bounded 3-band
    # preflight chunks; do not interpolate a three-color dispersion proxy.
    chunks = [
        optics._preflight(workspace, optics.WAVE[i : i + 3].tolist())["psf_request"]["prescription"]
        for i in range(0, 21, 3)
    ]
    first = chunks[0]
    for rest in chunks[1:]:
        first["wavelength_nm"] += rest["wavelength_nm"]
        for a, b in zip(first["surfaces"], rest["surfaces"], strict=True):
            a["refractive_index_after"] += b["refractive_index_after"]
    return first


def png(path, array):
    Image.fromarray(np.uint8(np.clip(np.nan_to_num(array), 0, 1) * 255 + 0.5)).save(path)


def geometry_cache(prescription, theta, samples, root, progress):
    key = digest(
        {
            "prescription": prescription,
            "theta_deg": theta,
            "samples": samples,
            "code": sha256(Path(core.__file__)),
        }
    )
    root = Path(root) / key
    manifest = root / "manifest.json"
    if manifest.exists():
        metadata = json.loads(manifest.read_text())
        for name, checksum in metadata["files"].items():
            if sha256(root / name) != checksum:
                raise ValueError("Ghost cache hash changed: " + name)
        for row in metadata["bundles"]:
            with np.load(root / row["file"], allow_pickle=False) as data:
                yield {**row, **{k: data[k] for k in data.files}}
        return
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, bundle in enumerate(core.trace_bundles(prescription, theta, samples, progress)):
        name = f"bundle-{index:03d}.npz"
        # Invalid branches do not own physical positions or optical power.
        arrays = {k: v.copy() for k, v in bundle.items() if isinstance(v, np.ndarray)}
        for k in ("positions", "directions", "weights"):
            arrays[k][~arrays["valid"]] = 0
        np.savez_compressed(root / name, **arrays)
        row = {k: v for k, v in bundle.items() if not isinstance(v, np.ndarray)} | {"file": name}
        rows.append(row)
        yield {**bundle, "file": name}
    write_json(
        manifest,
        {"key": key, "bundles": rows, "files": {r["file"]: sha256(root / r["file"]) for r in rows}},
    )


def spectral_power(workspace):
    from .virtual_lab_engine import _spectra

    spectral = _spectra(workspace, "D65", 1000)
    waves = np.asarray(optics.WAVE)
    energy = np.asarray(spectral["white"]) * 6.62607015e-34 * 299792458 / (waves * 1e-9)
    return energy / energy.sum(), spectral


def direct_transmission(prescription, theta, samples):
    b = next(core.trace_bundles(prescription, theta, samples))
    t = b["triangles"]
    area = b["entrance_area_m2"]
    result = []
    for wi in range(len(prescription["wavelength_nm"])):
        valid = np.all(b["valid"][wi, t], axis=1)
        weights = b["weights"][wi, t].mean(axis=1)
        if not valid.any():
            raise ValueError("No supported direct pupil triangles for field")
        result.append((weights[valid] * area[valid, None]).sum(axis=0) / area[valid].sum())
    return np.asarray(result)


def map_angle(prescription, theta, phi, samples, response, power, fields, cache, progress):
    """Fixed 512² overview, independent actual 256² native receivers per field."""
    spec = {
        "p": prescription,
        "theta": theta,
        "phi": phi,
        "samples": samples,
        "response": response["table_sha256"],
        "fields": fields,
        "power": power.tolist(),
        "code": [sha256(Path(__file__)), sha256(Path(core.__file__))],
    }
    directory = Path(cache) / "maps" / digest(spec)
    if (directory / "manifest.json").exists():
        m = json.loads((directory / "manifest.json").read_text())
        if any(sha256(directory / k) != v for k, v in m["files"].items()):
            raise ValueError("Ghost map cache hash changed")
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    overview = np.zeros((2, 512, 512, 3))
    rates = {f["id"]: np.zeros((2, 256, 256, 3, 4)) for f in fields}
    phi = math.radians(phi)
    rotation = np.array([[math.cos(phi), -math.sin(phi)], [math.sin(phi), math.cos(phi)]])
    rows = []
    diagnostics = {}
    invalid_angular_power = 0.0
    subcell = 0.0
    waves = prescription["wavelength_nm"]
    for bi, b in enumerate(
        geometry_cache(prescription, theta, samples, Path(cache) / "rays", progress)
    ):
        if bi % 15 == 0:
            progress(f"Mapping {theta:g}° · path {bi}")
        ghost = int(b["pair"] is not None)
        layer = np.zeros((512, 512, 3))
        total = np.zeros(3)
        representative = None
        for wi, wave in enumerate(waves):
            valid = b["valid"][wi]
            xy = b["positions"][wi, :, :2] @ rotation.T
            if not valid.any():
                continue
            weights = b["weights"][wi]
            triangles = b["triangles"]
            good = np.all(valid[triangles], axis=1)
            total += (
                weights[triangles[good]].mean(axis=1) * b["entrance_area_m2"][good, None]
            ).sum(axis=0) * power[wi]
            raster, info = core.raster_triangles(
                xy,
                triangles,
                valid,
                weights * power[wi],
                b["entrance_area_m2"],
                [-13.66, 13.66, -13.66, 13.66],
                (512, 512),
            )
            layer += raster
            subcell += info["subcell_deposited_power"]
            angle = np.degrees(np.arccos(np.clip(b["directions"][wi, :, 2], -1, 1)))
            supported = valid & (angle <= 60)
            invalid_angular_power += float(weights[valid & ~supported].sum()) * power[wi]
            qe = np.zeros((len(valid), 4))
            if supported.any():
                qe[supported] = pixel.interpolate_qe(response, angle[supported], wave)
            values = (weights[:, :, None] * qe[:, None, :]).reshape(len(valid), 12)
            values *= power[wi] / (6.62607015e-34 * 299792458 / (wave * 1e-9))
            for f in fields:
                cx, cy = f["xy_mm"]
                half = 0.0029 * 256 / 2
                r, _ = core.raster_triangles(
                    xy,
                    triangles,
                    supported,
                    values,
                    b["entrance_area_m2"],
                    [cx - half, cx + half, cy - half, cy + half],
                    (256, 256),
                )
                rates[f["id"]][ghost] += r.reshape(256, 256, 3, 4) * (0.0029e-3) ** 2
            if wi == 10 and representative is None:
                ids = np.flatnonzero(valid)
                representative = {"pupil_index": int(ids[len(ids) // 2]), "wavelength_index": wi}
        overview[ghost] += layer
        rows.append(
            {
                "pair": b["pair"],
                "energy_m2": total.tolist(),
                "map": f"path-{bi:03d}.npz",
                "representative": representative,
                "ray_path_zx_mm": None
                if representative is None
                else core.representative_path(
                    prescription,
                    theta,
                    samples,
                    b["pair"],
                    representative["pupil_index"],
                    representative["wavelength_index"],
                ),
            }
        )
        np.savez_compressed(directory / f"path-{bi:03d}.npz", layer=layer.astype(np.float32))
        diagnostics = b["diagnostics"]
    np.save(directory / "overview.npy", overview.astype(np.float32))
    np.savez_compressed(directory / "rates.npz", **rates)
    metadata = {
        "theta_deg": theta,
        "azimuth_deg": math.degrees(phi),
        "pupil_samples": samples,
        "paths": rows,
        "diagnostics": diagnostics,
        "unsupported_angle_weight_diagnostic": invalid_angular_power,
        "overview_subcell_power_diagnostic": subcell,
        "overview_bounds_mm": [-13.66, 13.66, -13.66, 13.66],
        "receiver_quadrature": "2x2 per native receiving pixel; pupil piecewise-linear triangles",
        "sampling_convergence": "not_established",
        "native_pitch_um": 2.9,
    }
    write_json(directory / "maps.json", metadata)
    write_json(
        directory / "manifest.json",
        {
            "files": {
                p.name: sha256(p)
                for p in directory.iterdir()
                if p.is_file() and not p.name.startswith("._") and p.name != "manifest.json"
            }
        },
    )
    return directory


def background(workspace, source, capture, prescription, coating, field_id, chart, samples):
    """Explicit separable field/pupil-average spectral coating attenuation.

    Original optical PSF and angular QE retained; no claim of coating-induced PSF
    apodization. This approximation is separate from joint ghost transport.
    """
    from . import design_workshop_engine as shared
    from . import reference_workshop_engine as engine
    from .virtual_lab_engine import _spectra

    solution = optics.load_solution(Path(source) / "optics")
    field = next(f for f in solution["result"]["fields"] if f["id"] == field_id)
    transmission = direct_transmission(prescription, field["object_angle_deg"], samples)[:, coating]
    photons, _ = shared._scene(chart, field["geometry"], _spectra(workspace, "D65", 1000))
    response = json.loads((Path(source) / "pixel-response.json").read_text())
    result = engine._apply(solution, field, photons * transmission, response)
    return result["mean_electron_rate"], transmission


def acquire(rate, readout, model, calibration, gain_map, seed, frames=8, white=1.0, gamma=2.2):
    from . import electrical
    from .reference_sensor_modes import _transfer, process_electrons

    geometry = {"full_sensor_shape": list(rate.shape), "crop_origin": [0, 0]}
    slope, black, adcwhite = _transfer(model, 1.0)
    _, dark_rate, _ = electrical._patterns(model, list(rate.shape), [0, 0], rate.shape, 25.0)
    raws = []
    electrons = []
    linear = []
    display = []
    valid = []
    raw_valid = []
    well = []
    adc = []
    for i in range(frames):
        a = electrical.capture_electrons(
            rate * 0.004,
            0.004,
            model,
            analog_gain=1.0,
            temperature_c=25.0,
            geometry=geometry,
            noise_enabled=True,
            seed=seed + i,
        )["arrays"]
        raw = a["raw_dn"]
        e = (raw.astype(float) - black) / slope - dark_rate * 0.004
        ok = (raw > 0) & (raw < adcwhite) & ~a["well_saturated_mask"]
        p = process_electrons(
            e,
            calibration["color"]["matrix"],
            lsc_gain=gain_map,
            display_white=white,
            gamma=gamma,
            valid=ok,
        )
        raws.append(raw)
        raw_valid.append(ok)
        electrons.append(e)
        linear.append(p["linear"])
        display.append(p["display"])
        valid.append(p["valid"])
        well.append(a["well_saturated_mask"])
        adc.append(a["adc_high_mask"] | a["digital_high_mask"])
    return {
        "raw_frames": np.array(raws),
        "raw_valid_frames": np.array(raw_valid),
        "electron_frames": np.array(electrons),
        "linear_frames": np.array(linear),
        "display_frames": np.array(display),
        "valid_frames": np.array(valid),
        "well_saturated_frames": np.array(well),
        "adc_saturated_frames": np.array(adc),
    }


def measure(arrays, baseline=None, chart="uniform"):
    from .reference_sensor_measurements import measure as existing

    m = existing(
        arrays["electron_frames"],
        arrays["linear_frames"],
        arrays["valid_frames"],
        contribution_count=1,
        native_pitch_factor=1,
        chart=chart,
        well_saturated_frames=arrays["well_saturated_frames"],
        adc_saturated_frames=arrays["adc_saturated_frames"],
    )
    rgb = arrays["linear_frames"].mean(axis=0)
    y = rgb @ np.array([0.2126, 0.7152, 0.0722])
    inner = y[16:-16, 16:-16]
    lo, hi = np.percentile(inner, [10, 90])
    m["fixed_roi_percentile_contrast"] = float((hi - lo) / max(hi + lo, 1e-12))
    if baseline is not None:
        b = baseline["linear_frames"].mean(axis=0)
        m["linear_rgb_rmse_vs_direct_only"] = float(
            np.sqrt(np.mean((rgb[16:-16, 16:-16] - b[16:-16, 16:-16]) ** 2))
        )
    if chart == "color" and baseline is not None:
        from pyisetcam.metrics import delta_e_ab

        from .virtual_lab_metrics import LINEAR_SRGB_TO_XYZ

        common = arrays["valid_frames"].all(axis=0) & baseline["valid_frames"].all(axis=0)
        h, w = rgb.shape[:2]
        patches = []
        for r in range(4):
            for c in range(6):
                roi = (
                    slice(int((r + 0.25) * h / 4), int((r + 0.75) * h / 4)),
                    slice(int((c + 0.25) * w / 6), int((c + 0.75) * w / 6)),
                )
                if not common[roi].all():
                    patches.append(
                        {
                            "index": r * 6 + c,
                            "delta_e00": None,
                            "reason": "invalid_or_saturated_support",
                        }
                    )
                    continue
                x = rgb[roi].mean(axis=(0, 1)) @ LINEAR_SRGB_TO_XYZ.T
                y = b[roi].mean(axis=(0, 1)) @ LINEAR_SRGB_TO_XYZ.T
                de = float(
                    np.asarray(
                        delta_e_ab(
                            y[None, :], x[None, :], np.array([0.95047, 1.0, 1.08883]), "2000"
                        )
                    ).reshape(-1)[0]
                )
                patches.append({"index": r * 6 + c, "delta_e00": de})
        finite = [p["delta_e00"] for p in patches if p["delta_e00"] is not None]
        m["color_change"] = {
            "reference": "same-coating direct-only output, not measured color truth",
            "patches": patches,
            "mean_delta_e00": float(np.mean(finite)) if len(finite) == 24 else None,
            "valid_patches": len(finite),
        }
    return m
