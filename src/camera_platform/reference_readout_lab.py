"""Reference readout experiments using existing electron-to-RAW and CDS engines.

These are authored behavioral configurations, not product circuit modes. Each
configuration performs fresh acquisition from the pinned mean electron rate;
common random-number seeds pair comparisons without implying physical DCG.
"""

from __future__ import annotations

import hashlib
import math
from copy import deepcopy

import numpy as np

from . import electrical
from . import reference_sensor_modes as core
from .reference_isp_lab import temporal_noise_power_spectrum

RECIPES = ("gain_budget", "noise_structure", "adc_headroom")


def default_settings():
    return {
        "gain_total": 8.0,
        "post_gain_noise_e": 4.0,
        "cds_rms_e": 2.0,
        "cds_correlation": 0.8,
        "column_fraction": 0.75,
        "adc_bits": [10, 12, 14],
    }


def validate_settings(settings):
    defaults = default_settings()
    if not isinstance(settings, dict) or set(settings) - set(defaults):
        raise ValueError("Readout lab settings require only declared gain, CDS and ADC fields.")
    result = {**defaults, **deepcopy(settings)}
    for key, low, high in (
        ("gain_total", 1, 32), ("post_gain_noise_e", 0, 100),
        ("cds_rms_e", 0, 100), ("cds_correlation", -1, 1),
        ("column_fraction", 0, 1),
    ):
        result[key] = electrical._number(result[key], key, low, high)
    bits = result["adc_bits"]
    if (not isinstance(bits, list) or not 1 <= len(bits) <= 3
            or any(type(value) is not int or not 8 <= value <= 16 for value in bits)
            or len(set(bits)) != len(bits)):
        raise ValueError("adc_bits requires 1..3 distinct integers in 8..16.")
    return result


def _cds(settings, fraction, correlation):
    rms = settings["cds_rms_e"]
    profile = electrical.default_cds_noise_profile()
    profile["reset_common_sigma_e"] = rms
    for axis, weight in (("pixel", 1 - fraction), ("row", 0), ("column", fraction)):
        sigma = rms * math.sqrt(weight / 2)
        profile[axis] = {
            "reset_sigma_e": sigma, "signal_sigma_e": sigma,
            "sample_correlation": correlation,
        }
    profile["source"] = {
        "kind": "estimated",
        "description": (
            "Authored Reference readout comparison at 25 C. Pixel and column variants "
            "have equal uncorrelated CDS marginal RMS; correlated CDS retains sample "
            "RMS and changes reset/signal correlation. Not a measured sensor circuit."
        ),
    }
    return electrical.validate_cds_noise_profile(profile)


def _configurations(base, recipe, settings):
    result = []
    if recipe == "gain_budget":
        total = settings["gain_total"]
        for identity, analog, digital in (
            ("analog_gain", total, 1.0), ("digital_gain", 1.0, total),
            ("balanced_gain", math.sqrt(total), math.sqrt(total)),
        ):
            model = deepcopy(base)
            model["digital_gain"] = digital
            model["post_gain_noise_v"] = (
                settings["post_gain_noise_e"] * base["conversion_gain_v_per_e"]
            )
            result.append((identity, analog, model, None))
    elif recipe == "noise_structure":
        for identity, fraction, rho in (
            ("pixel_iid", 0.0, 0.0),
            ("column_noise", settings["column_fraction"], 0.0),
            ("cds_correlated", settings["column_fraction"], settings["cds_correlation"]),
        ):
            model = deepcopy(base)
            model.update(read_noise_e=0.0, post_gain_noise_v=0.0, digital_gain=1.0)
            result.append((identity, 1.0, model, _cds(settings, fraction, rho)))
    else:
        black_fraction = base["adc"]["black_level_dn"] / (2**base["adc"]["bit_depth"] - 1)
        for bits in settings["adc_bits"]:
            model = deepcopy(base)
            model["digital_gain"] = 1.0
            model["adc"]["bit_depth"] = bits
            model["adc"]["black_level_dn"] = black_fraction * (2**bits - 1)
            result.append((f"adc_{bits}bit", 1.0, model, None))
    return result


def _calibration_record(calibration):
    result = {k: v for k, v in calibration.items() if k != "dark_e"}
    dark = np.asarray(calibration["dark_e"], dtype="<f8")
    result["dark_e"] = {
        "array": "dark_e", "dtype": "<f8", "shape": list(dark.shape),
        "sha256": hashlib.sha256(dark.tobytes(order="C")).hexdigest(),
    }
    return result


def readout_modes(
    mean_rate, readout, *, recipe, settings, exposure_s=0.004, light_scale=1.0,
    frames=8, seed=42, electrical_model=None, geometry=None, noise_enabled=True,
    temperature_c=25.0,
):
    """Acquire independently reset RAW per mode, with paired simulation seeds.

    Returned signed electrons use actual capture calibration, not legacy DN/e.
    Variance is a pre-clipping marginal model with approximate quantization;
    CDS spatial covariance is not flattened into an independent-pixel claim.
    """
    if recipe not in RECIPES:
        raise ValueError("Unknown Reference readout recipe.")
    settings = validate_settings(settings)
    seed = electrical._integer(seed, "seed", 0, 2**32 - 1)
    if type(noise_enabled) is not bool:
        raise ValueError("noise_enabled must be boolean.")
    if isinstance(temperature_c, (bool, np.bool_)) or temperature_c != 25:
        raise ValueError("Reference readout recipes are fixed at the authored 25 C condition.")
    for name, value in (("exposure_s", exposure_s), ("light_scale", light_scale)):
        electrical._number(value, name, 0, 3600 if name == "exposure_s" else 1e6)
    photo, base, geometry = core._inputs(
        mean_rate, readout, frames, exposure_s, light_scale, electrical_model, geometry
    )
    if max(photo.shape) > 256:
        raise ValueError("Reference readout lab supports at most native 256x256 probes.")
    modes = {}
    for identity, analog, authored, cds_profile in _configurations(base, recipe, settings):
        model = electrical.validate_electrical_model(authored)
        captures = [electrical.capture_electrons(
            photo, exposure_s, model, analog_gain=analog, temperature_c=temperature_c,
            geometry=geometry, noise_enabled=noise_enabled, seed=(seed + i) % 2**32,
            cds_profile=cds_profile,
        ) for i in range(frames)]
        arrays = [capture["arrays"] for capture in captures]
        calibration = captures[0]["calibration"]
        raw = np.stack([a["raw_dn"] for a in arrays])
        adc_raw = np.stack([a["adc_dn"] for a in arrays])
        well = np.stack([a["well_saturated_mask"] for a in arrays])
        adc = np.stack([a["adc_low_mask"] | a["adc_high_mask"] for a in arrays])
        digital = np.stack([a["digital_low_mask"] | a["digital_high_mask"] for a in arrays])
        valid = (~well & ~adc & ~digital
                 & (raw > calibration["linear_floor_dn"])
                 & (raw < calibration["linear_ceiling_dn"]))
        electrons = (raw.astype(float) - calibration["black_level_dn"]) / calibration["dn_per_e"]
        electrons -= calibration["dark_e"]
        mean = arrays[0]["mean_total_charge_e"]
        variance = (mean if noise_enabled else np.zeros_like(mean)) + (
            calibration["read_noise_variance_e2"] + calibration["quantization_variance_e2"]
        )
        input_read = model["read_noise_e"]**2 if noise_enabled else 0.0
        post_read = ((model["post_gain_noise_v"] / (
            model["conversion_gain_v_per_e"] * analog))**2 if noise_enabled else 0.0)
        quantization = captures[0]["summary"]["quantization_variance_model"]
        budget = {
            "photo_shot_e2_mean": float(np.mean(photo * arrays[0]["photo_response_map"]))
            if noise_enabled else 0.0,
            "dark_shot_e2_mean": float(np.mean(calibration["dark_e"])) if noise_enabled else 0.0,
            "input_read_e2": input_read,
            "post_gain_read_e2": post_read,
            "cds_residual_e2": calibration["read_noise_variance_e2"] if cds_profile else 0.0,
            "adc_quantization_e2": quantization["adc_input_e2"],
            "digital_quantization_e2": quantization["digital_rounding_input_e2"],
            "units": "input_referred_electron_squared",
            "scope": "pre-clipping marginal model; quantization uniform-error approximation",
        }
        metadata = {
            "id": identity, "recipe": recipe, "units": "electron/native_pixel",
            "contribution_count": 1, "cfa": "RGGB", "native_geometry": geometry,
            "exposure_s": float(exposure_s), "light_scale": float(light_scale),
            "noise_enabled": noise_enabled, "temperature_c": float(temperature_c),
            "seed": seed, "frame_seeds": [(seed + i) % 2**32 for i in range(frames)],
            "seed_policy": "common random numbers across configurations; not simultaneous DCG",
            "raw_reused": False, "product_mode": False, "physical_validation": "not_established",
            "analog_gain": analog, "digital_gain": model["digital_gain"],
            "bit_depth": model["adc"]["bit_depth"],
            "conversion_gain_v_per_e": model["conversion_gain_v_per_e"],
            "post_gain_noise_v": model["post_gain_noise_v"],
            "post_gain_noise_e_at_unity_analog": (
                model["post_gain_noise_v"] / model["conversion_gain_v_per_e"]
            ),
            "dn_per_e": calibration["dn_per_e"],
            "black_level_dn": calibration["black_level_dn"],
            "white_level_dn": calibration["white_level_dn"],
            "electrical_model": model, "calibration": _calibration_record(calibration),
            "cds_profile": cds_profile, "noise_budget": budget,
            "fixed_pattern": deepcopy(model["fixed_pattern"]),
            "adc_pedestal_policy": "constant black voltage fraction and ADC full-scale voltage",
            "validity_basis": "simulator stage clipping plus strict calibrated RAW bounds",
            "digital_mask_definition": (
                "digital_saturated may include ADC rails; digital_added requires an interior "
                "integer ADC code, excluding continuous ADC clipping and endpoint rounding"
            ),
            "well_mask_basis": "simulator charge truth",
            "dark_correction": "subtract model expected dark; dark shot noise remains",
            "variance_scope": (
                "pre-clipping marginal shot/read plus uniform quantization approximation; "
                "CDS row/column covariance is retained by acquisition, not this scalar map"
            ),
            "processing_order": captures[0]["summary"]["processing_order"],
            "limitations": captures[0]["summary"]["limitations"],
        }
        mode = core._payload(electrons, variance, valid, well, adc, metadata, raw)
        mode.update(
            adc_frames=adc_raw, digital_saturated_frames=digital,
            digital_added_saturated_frames=(
                digital & (adc_raw > 0) & (adc_raw < calibration["white_level_dn"])
            ),
            dark_e=calibration["dark_e"], photo_mean_e=photo.copy(),
            mean_total_charge_e=mean,
        )
        if cds_profile:
            for key in (
                "cds_reset_sample_noise_e", "cds_signal_sample_noise_e", "cds_residual_noise_e"
            ):
                mode[key] = np.stack([a[key] for a in arrays])
        modes[identity] = mode
    return {
        "modes": modes, "readout": base, "settings": settings, "recipe": recipe,
        "geometry": {"native_shape": list(photo.shape), "output_shape": list(photo.shape),
                     "cfa": "RGGB", "contributing_pixels_per_output_site": 1},
        "diagnostics": {
            "comparison": "fixed mean optical/pixel response; newly acquired RAW for each mode",
            "gain_semantics": (
                "conversion V/e, analog before ADC, digital after ADC; no added photons"
            ),
            "new_capture_count": len(modes) * frames,
        },
    }


def _profile_rms(residuals, mask, *, axis):
    counts = mask.sum(axis=axis)
    summed = np.where(mask[None], residuals, 0).sum(axis=axis + 1)
    means = np.divide(summed, counts[None], out=np.zeros_like(summed), where=counts[None] > 0)
    values = np.std(means, axis=0, ddof=1)
    return [float(v) if count else None for v, count in zip(values, counts, strict=True)]


def _ptc_points(frames, valid, chart):
    result = {
        "status": "unavailable", "points": [],
        "scope": (
            "per-CFA-site temporal variance points; not fitted gain or EMVA certification"
        ),
        "variance_units": "electron_squared", "mean_units": "electron/native_pixel",
    }
    if chart not in {"uniform", "dark", "dynamic_range"}:
        result["reason"] = "PTC_requires_declared_uniform_dark_or_step_regions"
        return result
    if len(frames) < 2:
        result["reason"] = "at_least_two_independent_captures_required"
        return result
    h, w = frames.shape[1:]
    margin = min(8, max(0, min(h, w) // 8))
    rois = [(chart, (margin, h - margin, margin, w - margin))]
    if chart == "dynamic_range":
        rois = []
        for index in range(16):
            c0, c1 = index * w // 16, (index + 1) * w // 16
            inset = max(1, (c1 - c0) // 4)
            rois.append((f"step-{index:02d}", (margin, h - margin, c0 + inset, c1 - inset)))
    for label, (r0, r1, c0, c1) in rois:
        for site, (r, c) in zip(core.SITE_NAMES, core.SITES, strict=True):
            rows, cols = np.arange(r0 + (r - r0) % 2, r1, 2), np.arange(c0 + (c - c0) % 2, c1, 2)
            sample = frames[:, rows[:, None], cols]
            supported = valid[:, rows[:, None], cols]
            available = bool(sample.size and supported.all() and np.isfinite(sample).all())
            result["points"].append({
                "id": f"{label}-{site}", "site": site, "roi": [r0, r1, c0, c1],
                "status": "ok" if available else "unavailable",
                "reason": None if available else "measurement_ROI_missing_or_invalid_capture",
                "valid_fraction": float(supported.mean()) if supported.size else 0.0,
                "mean_electrons": float(sample.mean()) if available else None,
                "temporal_variance_e2": float(np.var(sample, axis=0, ddof=1).mean())
                if available else None,
                "pixel_count": int(len(rows) * len(cols)),
            })
    valid_count = sum(point["status"] == "ok" for point in result["points"])
    result.update(status="ok" if valid_count == len(result["points"]) else "partial"
                  if valid_count else "unavailable", valid_point_count=valid_count)
    return result


def mode_diagnostics(mode, *, chart=None):
    """Measure temporal residuals; never treat the scene's spatial texture as noise."""
    frames = np.asarray(mode["electron_frames"], dtype=float)
    valid = np.asarray(mode["valid_frames"], dtype=bool)
    common = valid.all(axis=0) & np.isfinite(frames).all(axis=0)
    repeats = len(frames)
    residuals = frames - frames.mean(axis=0, keepdims=True)
    result = {
        "schema": "camera_reference_readout_diagnostics_v1", "chart": chart,
        "repeat_count": repeats, "valid_fraction": float(common.mean()),
        "noise_budget": mode["metadata"]["noise_budget"],
        "mean_electrons": float(frames[:, common].mean()) if common.any() else None,
        "temporal_noise_e": float(np.sqrt(np.var(frames[:, common], axis=0, ddof=1).mean()))
        if repeats > 1 and common.any() else None,
        "row_temporal_rms_e": _profile_rms(residuals, common, axis=1) if repeats > 1 else [],
        "column_temporal_rms_e": _profile_rms(residuals, common, axis=0) if repeats > 1 else [],
        "profile_definition": (
            "temporal RMS of row/column average residual on all-repeat-valid sites"
        ),
        "support_note": (
            "profiles and NPS condition on common valid support; PTC rejects invalid ROI"
        ),
        "digital_saturation_fraction": float(mode["digital_saturated_frames"].mean()),
        "digital_added_saturation_fraction": float(mode["digital_added_saturated_frames"].mean()),
        "ptc": _ptc_points(frames, valid, chart),
    }
    # Same-site Gr plane avoids conflating a periodic CFA variance pattern with
    # the measured spatial noise structure. Convert its two-pitch grid to native.
    green = frames[:, 0::2, 1::2]
    spectrum = temporal_noise_power_spectrum(
        np.repeat(green[..., None], 3, axis=-1), common[0::2, 1::2]
    )
    spectrum["frequency_cycles_per_pixel"] = [v / 2 for v in spectrum["frequency_cycles_per_pixel"]]
    spectrum["radial_power"] = [v * 4 if v is not None else None for v in spectrum["radial_power"]]
    spectrum.update(
        units="electron_squared_per_(cycles_per_native_pixel)^2",
        signal="signed_calibrated_Gr_electrons_before_ISP", site="Gr", site_stride_native=2,
        radial_support=[0.0, 0.25], roi_coordinate_system="Gr_site_plane_row_col",
        integration_note="full site-grid 2D density integral; native frequency density scaled by 4",
    )
    result["noise_power_spectrum"] = spectrum
    return result
