"""Hypothesis-driven OCL sensitivity, deliberately separate from qualified 3D QE.

The finite-beam central absorption ratio is NOT a uniform-illumination gain.
This adapter asks what a camera would do IF that response multiplier applied.
It neither exports QE nor interprets the absorption vector as a spatial kernel.
"""
from __future__ import annotations

import copy
import numpy as np

from . import electrical
from .reference_sensor_modes import process_electrons

PROFILES = ("green_only", "achromatic_shared")
SITES = ((0, 0), (0, 1), (1, 0), (1, 1))


def response_evidence(summary):
    if summary.get("specification", {}).get("dimension") != 2:
        raise ValueError("This sensitivity demonstration requires an explicit 2D source")
    assessment = summary["trend_assessment"]
    result = []
    for angle in (0., 20.):
        rows = [r for r in assessment["decision_summary"]
                if r["variant"] == "ocl_shift" and r["wavelength_nm"] == 550.
                and r["angle_deg"] == angle]
        by_metric = {r["metric"]: r for r in rows}
        central = by_metric.get("central_absorption", {})
        if (central.get("status") != "magnitude_stable"
                or central.get("delta_unit") != "relative_fraction"):
            raise ValueError("Both exact angle nodes need stable central absorption differences")
        anchors = [s for s in summary["samples"] if s.get("control", "mesh") == "mesh"
                   and s["resolution"] == 80 and s["wavelength_nm"] == 550.
                   and s["angle_deg"] == angle and s["variant"] in ("reviewed", "ocl_shift")]
        keys = {(s["variant"], s["polarization"]) for s in anchors}
        if len(anchors) != 4 or keys != {(v, p) for v in ("reviewed", "ocl_shift") for p in ("s", "p")}:
            raise ValueError("Exact 80-cell s/p source anchors required")
        for s in anchors:
            pixels = np.asarray(s["pixel_absorption"], dtype=float)
            if (pixels.ndim != 1 or not len(pixels) % 2 or not np.isfinite(pixels).all()
                    or np.any(pixels < 0) or pixels[len(pixels)//2] <= 0
                    or not np.isfinite([s["energy_residual"], s["support_tail_fraction"]]).all()
                    or not s["passive"] or abs(s["energy_residual"]) > .01
                    or not 0 <= s["support_tail_fraction"] <= .015):
                raise ValueError("Numerically invalid source anchor")
        means = {}
        for variant in ("reviewed", "ocl_shift"):
            selected = [s for s in anchors if s["variant"] == variant]
            means[variant] = float(np.mean([s["pixel_absorption"][len(s["pixel_absorption"]) // 2]
                                           for s in selected]))
        ratio = means["ocl_shift"] / means["reviewed"]
        if not np.isclose(ratio, 1 + central["delta"], rtol=1e-9, atol=1e-12):
            raise ValueError("Assessment and stored absorption arrays disagree")
        result.append(dict(angle_deg=angle, wavelength_nm=550., central_gain=ratio,
            total_active_si_gain=1 + by_metric["total_absorption"]["delta"],
            total_active_si_classification=by_metric["total_absorption"]["status"],
            neighbor_delta_pp=100 * by_metric["neighbor_fraction"]["delta"],
            central_observed_sensitivity=central["sensitivity"],
            classification=central["status"], anchor_task_ids=[s["task_id"] for s in anchors],
            polarization_notes=("0° p central response is below the 1% meaningful threshold; "
                                "20° central response is magnitude-stable for s and p."),
            meaning="Finite Gaussian beam central redistribution; not uniform-scene QE"))
    return result


def project_rate(rate, gain, profile):
    values = np.asarray(rate, dtype=float)
    if (values.ndim != 2 or any(n % 2 for n in values.shape) or min(values.shape) < 4
            or not np.isfinite(values).all() or np.any(values < 0)):
        raise ValueError("Finite nonnegative even-shaped RGGB electron rate required")
    if profile not in PROFILES or not np.isfinite(gain) or not 0 < gain < 2:
        raise ValueError("Explicit supported projection profile and positive bounded gain required")
    projected = values.copy()
    if profile == "green_only":
        projected[0::2, 1::2] *= gain
        projected[1::2, 0::2] *= gain
    else:
        projected *= gain
    return projected


def acquire(rate, model, seed, frames=8, exposure_s=.004, geometry=None):
    """Fresh independently reset readouts; paired seeds and persistent FPN in A/B."""
    rate = project_rate(rate, 1., "green_only")
    if type(frames) is not int or not 2 <= frames <= 32:
        raise ValueError("Temporal noise needs 2..32 independent frames")
    captured = [electrical.capture_electrons(rate * exposure_s, exposure_s, copy.deepcopy(model),
        analog_gain=1., temperature_c=25.,
        geometry=geometry or {"full_sensor_shape": list(rate.shape), "crop_origin": [0, 0]},
        noise_enabled=True, seed=seed + i) for i in range(frames)]
    arrays = [c["arrays"] for c in captured]
    calibration = captured[0]["calibration"]
    raw = np.stack([a["raw_dn"] for a in arrays])
    well = np.stack([a["well_saturated_mask"] for a in arrays])
    adc = np.stack([a["adc_low_mask"] | a["adc_high_mask"] |
                    a["digital_low_mask"] | a["digital_high_mask"] for a in arrays])
    electrons = ((raw.astype(float) - calibration["black_level_dn"]) /
                 calibration["dn_per_e"] - calibration["dark_e"])
    valid = (~well & ~adc & (raw > calibration["linear_floor_dn"])
             & (raw < calibration["linear_ceiling_dn"]))
    variance = (arrays[0]["mean_total_charge_e"] + calibration["read_noise_variance_e2"]
                + calibration["quantization_variance_e2"])
    return dict(raw_frames=raw, electron_frames=electrons, valid_frames=valid,
        well_frames=well, adc_frames=adc, variance_e2=variance, calibration=calibration,
        expected_photo_e=rate * exposure_s,
        photo_response_map=arrays[0]["photo_response_map"],
        capture_summary=captured[0]["summary"])


def process(capture, matrix, lsc_gain, display_white=1.):
    outputs = [process_electrons(frame, matrix, lsc_gain=lsc_gain,
        display_white=display_white, gamma=2.2, demosaic="bilinear",
        valid=capture["valid_frames"][i], variance_e2=capture["variance_e2"])
        for i, frame in enumerate(capture["electron_frames"])]
    # Same noiseless bypass is also retained to distinguish a deterministic color
    # change from the sampling noise of eight acquired frames.
    expected = process_electrons(capture["expected_photo_e"], matrix, lsc_gain=lsc_gain,
                                display_white=display_white, gamma=2.2, demosaic="bilinear")
    return dict(linear=np.stack([o["linear"] for o in outputs]),
                display=outputs[0]["display"], valid=np.stack([o["valid"] for o in outputs]),
                expected_linear=expected["linear"],
                corrected_variance_e2=outputs[0]["lsc_mosaic_variance_e2_per_contributor"])


def measure(capture, processed, common_raw, common_rgb):
    signal, noise, snr, expected, model_noise = [], [], [], [], []
    variance = np.var(capture["electron_frames"], axis=0, ddof=1)
    means = np.mean(capture["electron_frames"], axis=0)
    for r, c in SITES:
        mask = common_raw[r::2, c::2]
        if not np.any(mask):
            signal.append(None); noise.append(None); snr.append(None)
            expected.append(None); model_noise.append(None)
            continue
        m = float(means[r::2, c::2][mask].mean())
        n = float(np.sqrt(variance[r::2, c::2][mask].mean()))
        signal.append(m); noise.append(n); snr.append(m / n if n else None)
        expected.append(float(capture["expected_photo_e"][r::2, c::2][mask].mean()))
        model_noise.append(float(np.sqrt(capture["variance_e2"][r::2, c::2][mask].mean())))
    linear = processed["linear"]
    rgb_noise = np.sqrt(np.var(linear, axis=0, ddof=1)[common_rgb].mean(axis=0)).tolist() if common_rgb.any() else [None]*3
    luma = linear @ np.array([.2126, .7152, .0722])
    luma_noise = float(np.sqrt(np.var(luma, axis=0, ddof=1)[common_rgb].mean())) if common_rgb.any() else None
    return dict(site_mean_e=signal, site_expected_photo_e=expected,
        site_temporal_noise_e=noise, site_snr=snr, site_model_noise_e=model_noise,
        linear_rgb_temporal_noise=rgb_noise, luma_temporal_noise=luma_noise,
        well_saturation_fraction=float(capture["well_frames"][:, 8:-8, 8:-8].mean()),
        adc_saturation_fraction=float(capture["adc_frames"][:, 8:-8, 8:-8].mean()),
        raw_valid_fraction=float(common_raw[8:-8, 8:-8].mean()),
        rgb_valid_fraction=float(common_rgb[8:-8, 8:-8].mean()),
        noise_definition="sqrt(mean(per-pixel temporal sample variance)), ddof=1; eight repeats",
        snr_scope="uniform chart only for homogeneous signal interpretation; no spatial texture as noise")


def compare(a, b, pa, pb, ma, mb, common_rgb, chart, light):
    from pyisetcam.metrics import delta_e_ab
    from .virtual_lab_metrics import LINEAR_SRGB_TO_XYZ
    def pct(left, right):
        return [None if x is None or y is None or x == 0 else 100 * (y / x - 1)
                for x, y in zip(left, right, strict=True)]
    result = dict(signal_change_pct=pct(ma["site_expected_photo_e"], mb["site_expected_photo_e"]),
        noise_change_pct=pct(ma["site_temporal_noise_e"], mb["site_temporal_noise_e"]),
        snr_change_pct=pct(ma["site_snr"], mb["site_snr"]))
    if chart == "color":
        patches, repeats = [], []
        h, w = common_rgb.shape
        for row in range(4):
            for col in range(6):
                y0, y1 = row*h//4 + 8, (row+1)*h//4 - 8
                x0, x1 = col*w//6 + 8, (col+1)*w//6 - 8
                valid = common_rgb[y0:y1, x0:x1]
                if valid.mean() < .95:
                    patches.append(None); repeats.append(None); continue
                def xyz(image):
                    return image[y0:y1, x0:x1][valid].mean(axis=0) @ LINEAR_SRGB_TO_XYZ.T
                # Declared illuminant scale, shared by A/B; never fit from images.
                white = np.array([.95047, 1., 1.08883]) * light
                patches.append(float(delta_e_ab(xyz(pa["expected_linear"]),
                    xyz(pb["expected_linear"]), white, "2000")))
                repeats.append(float(delta_e_ab(xyz(pa["linear"].mean(axis=0)),
                    xyz(pb["linear"].mean(axis=0)), white, "2000")))
        result.update(color_shift_delta_e00_patches=patches,
            color_shift_delta_e00_mean=float(np.mean(patches)) if all(v is not None for v in patches) else None,
            acquired_color_shift_delta_e00_patches=repeats,
            color_shift_definition="A/B appearance shift, not color accuracy; fixed D65 white scaled by declared light; noiseless electron-domain diagnostic")
    return result
