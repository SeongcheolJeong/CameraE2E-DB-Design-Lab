"""Absolute/relative spectral scan identification and frozen-QE RAW validation.

No normalization, gain, white balance, CCM, or QE is fitted during validation.
Calibrated incident dose is defined at the sensor surface, after lens/filters.
Radiometric calibration declarations are retained, not independently certified.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import numpy as np

CHANNELS = ["R", "G", "B"]
CONDITIONS = {"camera_id", "mode_id", "gain", "temperature_c"}
PROVENANCE = {
    "calibration_id",
    "session_id",
    "acquisition_id",
    "instrument_id",
    "source_sha256",
    "parent_profile_sha256",
    "baseline_reason",
}
EFFECTS = {"cfa", "microlens", "photodiode", "sensor_stack"}
SCAN_FIELDS = {
    "schema",
    "wavelength_nm",
    "channels",
    "mean_dn",
    "dark_dn",
    "dn_per_e",
    "exposure_s",
    "bandwidth_nm",
    "adc_white_dn",
    "reference_plane",
    "conditions",
    "source_kind",
    "provenance",
    "included_effects",
    "incident_photons_per_pixel",
    "photon_flux_per_second",
    "radiometry",
    "clipped_fraction",
    "dark_clipped_fraction",
}
PROFILE_FIELDS = {
    "schema",
    "response_kind",
    "wavelength_nm",
    "channels",
    "qe",
    "relative_spectral_response",
    "normalization",
    "dn_per_e",
    "adc_white_dn",
    "bandwidth_nm",
    "reference_plane",
    "conditions",
    "exposure_range_s",
    "included_effects",
    "source_kind",
    "provenance",
    "radiometry",
    "scan_sha256",
    "method",
    "physical_validation",
    "limitations",
    "profile_sha256",
}
RAW_FIELDS = {
    "schema",
    "wavelength_nm",
    "photon_spectral_dose",
    "photon_dose_unit",
    "measured_raw_rgb_dn",
    "black_level_dn",
    "dn_per_e",
    "adc_white_dn",
    "exposure_s",
    "conditions",
    "source_kind",
    "provenance",
    "reference_plane",
    "dose_includes",
    "radiometry",
    "normalization",
    "tolerance",
    "clipped_fraction",
    "patch_ids",
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def profile_sha256(profile):
    """Hash a profile excluding its own hash; use to mark an authored baseline revision."""
    return _hash({key: value for key, value in profile.items() if key != "profile_sha256"})


def _object(value, allowed, required, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    if set(value) - allowed:
        raise ValueError(f"{name} has unknown fields: {sorted(set(value) - allowed)}")
    if required - set(value):
        raise ValueError(f"{name} is missing: {sorted(required - set(value))}")
    _canonical(value)
    return deepcopy(value)


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise ValueError(f"{name} requires nonempty bounded text")
    return value


def _pin(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError(f"{name} requires a lowercase SHA256")
    return value


def _array(value, name, shape=None, *, low=None, high=None):
    raw = np.asarray(value)
    if raw.dtype.kind not in "iuf" or not np.isfinite(raw).all():
        raise ValueError(f"{name} requires finite numeric values")
    result = raw.astype(float)
    if shape is not None and result.shape != tuple(shape):
        raise ValueError(f"{name} requires shape {tuple(shape)}")
    if low is not None and np.any(result < low):
        raise ValueError(f"{name} must be >= {low}")
    if high is not None and np.any(result > high):
        raise ValueError(f"{name} must be <= {high}")
    return result


def _positive(value, name):
    result = _array(value, name, ())
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return float(result)


def _conditions(value):
    result = _object(value, CONDITIONS, CONDITIONS, "conditions")
    for key in ("camera_id", "mode_id"):
        _text(result[key], key)
    _positive(result["gain"], "gain")
    _array(result["temperature_c"], "temperature_c", (), low=-100, high=150)
    return result


def _provenance(value):
    required = {"calibration_id", "session_id", "acquisition_id", "instrument_id"}
    result = _object(value, PROVENANCE, required, "provenance")
    for key, item in result.items():
        (_pin if key.endswith("sha256") else _text)(item, "provenance." + key)
    return result


def _source_kind(value):
    if not isinstance(value, str) or value not in {"synthetic", "measured"}:
        raise ValueError("source_kind must be synthetic or measured")
    return value


def _effects(value):
    if (
        not isinstance(value, list)
        or any(not isinstance(v, str) for v in value)
        or len(value) != len(set(value))
        or set(value) - EFFECTS
        or not {"cfa", "photodiode"} <= set(value)
    ):
        raise ValueError(
            "included_effects must explicitly include cfa and photodiode; no lens/filter"
        )
    return value


def _wavelength(value):
    result = _array(value, "wavelength_nm", low=100, high=3000)
    if result.ndim != 1 or not 2 <= len(result) <= 2048 or np.any(np.diff(result) <= 0):
        raise ValueError("wavelength_nm requires 2..2048 strictly increasing wavelengths")
    return result


def _bandwidth(value, wave):
    width = _array(value, "bandwidth_nm")
    if width.ndim == 0:
        width = np.full(wave.shape, float(width))
    if width.shape != wave.shape or np.any(width <= 0) or np.any(wave - width / 2 <= 0):
        raise ValueError(
            "bandwidth_nm requires positive scalar or one positive bandwidth per wavelength"
        )
    if np.any(wave[:-1] + width[:-1] / 2 > wave[1:] - width[1:] / 2 + 1e-10):
        raise ValueError("Scan wavelength passbands overlap; independent QE samples are required")
    return width


def _unclipped(value, shape, name):
    fractions = _array(value, name, shape, low=0, high=1)
    if np.any(fractions > 0):
        raise ValueError(
            f"{name}: saturated/clipped observations cannot establish linear QE or RAW response"
        )


def _radiometry(value, *, relative=False, dose=False):
    allowed = {"kind", "calibration_id", "unit", "source_spectrum_corrected"}
    required = {"kind", "calibration_id", "unit"}
    result = _object(value, allowed, required, "radiometry")
    _text(result["calibration_id"], "radiometry.calibration_id")
    if relative:
        if result["kind"] != "relative_source_corrected" or result["unit"] != "relative_photons/s":
            raise ValueError(
                "Relative scans require corrected relative photon flux, not RAW spectrum"
            )
        if result.get("source_spectrum_corrected") is not True:
            raise ValueError("Relative scans require explicit source spectrum correction")
    else:
        unit = "photons/pixel/nm" if dose else "photons/pixel"
        if result["kind"] != "absolute_calibrated" or result["unit"] != unit:
            raise ValueError(
                "Absolute response requires calibrated sensor-surface photon radiometry"
            )
        if "source_spectrum_corrected" in result:
            raise ValueError(
                "Absolute photon dose already declares its calibrated spectral radiometry"
            )
    return result


def derive_qe(scan):
    """Derive absolute band-averaged QE or explicitly normalized relative response.

    ``incident_photons_per_pixel`` is the calibrated dose integrated over each
    non-overlapping scan passband. Relative flux is corrected for the source
    spectrum and multiplied by each acquisition's exposure, then each response
    channel is normalized to its own maximum. It can never predict absolute DN.
    """
    required = SCAN_FIELDS - {"incident_photons_per_pixel", "photon_flux_per_second"}
    data = _object(scan, SCAN_FIELDS, required, "scan")
    if data["schema"] != "camera_qe_scan_v1" or data["channels"] != CHANNELS:
        raise ValueError("Scan requires camera_qe_scan_v1 with channels R,G,B")
    if data["reference_plane"] != "sensor_surface":
        raise ValueError("QE input photons must be referenced to sensor_surface")
    absolute = "incident_photons_per_pixel" in data
    if absolute == ("photon_flux_per_second" in data):
        raise ValueError("Provide exactly one absolute dose or source-corrected relative flux")
    wave = _wavelength(data["wavelength_nm"])
    width = _bandwidth(data["bandwidth_nm"], wave)
    shape = (len(wave), 3)
    mean = _array(data["mean_dn"], "mean_dn", shape, low=0)
    dark = _array(data["dark_dn"], "dark_dn", shape, low=0)
    white = _positive(data["adc_white_dn"], "adc_white_dn")
    if np.any(mean >= white) or np.any(dark >= white):
        raise ValueError("Scan mean or dark DN reaches saturation")
    _unclipped(data["clipped_fraction"], shape, "clipped_fraction")
    _unclipped(data["dark_clipped_fraction"], shape, "dark_clipped_fraction")
    exposure = _array(data["exposure_s"], "exposure_s", (len(wave),), low=1e-9, high=3600)
    gain = _positive(data["dn_per_e"], "dn_per_e")
    signal = (mean - dark) / gain
    if np.any(signal < 0):
        raise ValueError(
            "Dark-subtracted scan signal must be nonnegative; no clipping-to-zero is applied"
        )
    radiometry = _radiometry(data["radiometry"], relative=not absolute)
    photons = _array(
        data["incident_photons_per_pixel"] if absolute else data["photon_flux_per_second"],
        "incident photon dose/flux",
        (len(wave),),
        low=0,
    )
    if np.any(photons <= 0):
        raise ValueError("Incident photon dose/flux must be strictly positive at every wavelength")
    if not absolute:
        photons = photons * exposure
    response = signal / photons[:, None]
    if absolute and np.any(response > 1):
        raise ValueError("Absolute QE must lie in [0,1]; verify photon calibration and DN/e units")
    if not absolute:
        maximum = response.max(axis=0)
        if np.any(maximum <= 0):
            raise ValueError("Every relative response channel needs a positive maximum")
        response = response / maximum
    result = {
        "schema": "camera_qe_profile_v1",
        "response_kind": "absolute_qe" if absolute else "relative_spectral_response",
        "wavelength_nm": wave.tolist(),
        "channels": CHANNELS.copy(),
        "qe" if absolute else "relative_spectral_response": response.tolist(),
        "normalization": "none" if absolute else "per_channel_max",
        "dn_per_e": gain,
        "adc_white_dn": white,
        "bandwidth_nm": width.tolist(),
        "reference_plane": "sensor_surface",
        "conditions": _conditions(data["conditions"]),
        "exposure_range_s": [float(exposure.min()), float(exposure.max())],
        "included_effects": _effects(data["included_effects"]),
        "source_kind": _source_kind(data["source_kind"]),
        "provenance": _provenance(data["provenance"]),
        "radiometry": radiometry,
        "scan_sha256": _hash(scan),
        "method": "absolute_photon_scan_v1" if absolute else "relative_source_corrected_scan_v1",
        "physical_validation": "not_established",
        "limitations": [
            "Band-averaged samples represented at scan centers; no monochromator deconvolution.",
            "Piecewise-linear spectral interpolation only inside the sampled center band.",
            "CFA and declared sensor effects are included; lens/filter transmission is not QE.",
            "Radiometry/instrument declarations are preserved, not independently authenticated.",
            "Mean dark subtraction does not separately identify sensor quantum-yield mechanisms.",
            *(
                []
                if absolute
                else [
                    "Per-channel relative normalization discards absolute QE and channel balance."
                ]
            ),
        ],
    }
    result["profile_sha256"] = profile_sha256(result)
    return result


def _profile(profile):
    required = PROFILE_FIELDS - {"qe", "relative_spectral_response"}
    result = _object(profile, PROFILE_FIELDS, required, "profile")
    _pin(result["profile_sha256"], "profile_sha256")
    if result["profile_sha256"] != profile_sha256(result):
        raise ValueError("QE profile hash mismatch; source profile was changed")
    if result["schema"] != "camera_qe_profile_v1" or result["channels"] != CHANNELS:
        raise ValueError("Unsupported spectral profile schema/channels")
    if result["response_kind"] != "absolute_qe":
        raise ValueError(
            "Absolute RAW prediction requires absolute_qe; relative response is unsupported"
        )
    if (
        "qe" not in result
        or "relative_spectral_response" in result
        or result["normalization"] != "none"
    ):
        raise ValueError("Absolute QE cannot carry relative normalization")
    if result["reference_plane"] != "sensor_surface":
        raise ValueError("QE profile must refer to sensor_surface")
    wave = _wavelength(result["wavelength_nm"])
    _bandwidth(result["bandwidth_nm"], wave)
    qe = _array(result["qe"], "qe", (len(wave), 3), low=0, high=1)
    _conditions(result["conditions"])
    _effects(result["included_effects"])
    _source_kind(result["source_kind"])
    _provenance(result["provenance"])
    _radiometry(result["radiometry"])
    _pin(result["scan_sha256"], "scan_sha256")
    _positive(result["dn_per_e"], "dn_per_e")
    _positive(result["adc_white_dn"], "adc_white_dn")
    bounds = _array(result["exposure_range_s"], "exposure_range_s", (2,), low=1e-9, high=3600)
    if bounds[0] > bounds[1]:
        raise ValueError("Exposure bounds must be ordered")
    if result["physical_validation"] != "not_established":
        raise ValueError("This engine cannot establish a physical validation release status")
    if result["method"] == "authored_baseline_v1":
        if result["source_kind"] != "synthetic":
            raise ValueError("An authored baseline is synthetic, not a measured QE revision")
        _pin(result["provenance"].get("parent_profile_sha256"), "parent_profile_sha256")
        _text(result["provenance"].get("baseline_reason"), "baseline_reason")
    elif result["method"] != "absolute_photon_scan_v1":
        raise ValueError("Unsupported absolute QE derivation method")
    return result, wave, qe


def validate_raw(profile, payload):
    """Compare frozen QE predictions with measured RAW patch RGB DN, without fitting.

    Photon dose is density [photons/pixel/nm], already integrated over exposure
    and transported through the lens/filters to the sensor surface. Trapezoidal
    integration uses every QE knot and refuses narrower or extrapolated bands.
    """
    p, qe_wave, qe = _profile(profile)
    before = profile_sha256(profile)
    data = _object(payload, RAW_FIELDS, RAW_FIELDS - {"patch_ids"}, "RAW validation")
    if data["schema"] != "camera_spectral_raw_validation_v1":
        raise ValueError("Unsupported RAW validation schema")
    if (
        data["reference_plane"] != "sensor_surface"
        or data["photon_dose_unit"] != "photons/pixel/nm"
    ):
        raise ValueError("RAW prediction requires sensor_surface photons/pixel/nm")
    if data["normalization"] != "none":
        raise ValueError(
            "Validation requires normalization='none'; fitted gain/WB/CCM are forbidden"
        )
    if data["dose_includes"] != ["lens", "filters"]:
        raise ValueError("Declare dose_includes=['lens','filters']; they must not be applied twice")
    _radiometry(data["radiometry"], dose=True)
    _source_kind(data["source_kind"])
    _provenance(data["provenance"])
    if _conditions(data["conditions"]) != p["conditions"]:
        raise ValueError("RAW camera/mode/gain/temperature conditions differ from the QE profile")
    if _positive(data["dn_per_e"], "dn_per_e") != p["dn_per_e"]:
        raise ValueError(
            "RAW DN/e differs from the frozen scan calibration; gain fitting is forbidden"
        )
    if _positive(data["adc_white_dn"], "adc_white_dn") != p["adc_white_dn"]:
        raise ValueError("RAW ADC white level differs from the frozen scan")
    exposure = _positive(data["exposure_s"], "exposure_s")
    if not p["exposure_range_s"][0] <= exposure <= p["exposure_range_s"][1]:
        raise ValueError("RAW exposure is outside the declared scan exposure range")
    wave = _wavelength(data["wavelength_nm"])
    if not np.isclose(wave[0], qe_wave[0], atol=1e-9, rtol=0) or not np.isclose(
        wave[-1], qe_wave[-1], atol=1e-9, rtol=0
    ):
        raise ValueError(
            "Spectral dose must cover the complete QE center band; extrapolation is forbidden"
        )
    if any(not np.any(np.isclose(wave, knot, atol=1e-9, rtol=0)) for knot in qe_wave):
        raise ValueError("Spectral dose quadrature must retain every QE wavelength knot")
    photons = _array(data["photon_spectral_dose"], "photon_spectral_dose", low=0)
    if (
        photons.ndim != 2
        or not 1 <= len(photons) <= 1024
        or photons.shape[1] != len(wave)
        or not np.any(photons > 0)
    ):
        raise ValueError(
            "Photon spectral dose requires 1..1024 patches by wavelength with positive dose"
        )
    count = len(photons)
    measured = _array(data["measured_raw_rgb_dn"], "measured_raw_rgb_dn", (count, 3), low=0)
    white = p["adc_white_dn"]
    if np.any(measured >= white):
        raise ValueError("Measured RAW reaches ADC saturation")
    _unclipped(data["clipped_fraction"], (count, 3), "clipped_fraction")
    black = _array(data["black_level_dn"], "black_level_dn", low=0, high=white - 1)
    if black.ndim == 0:
        black = np.repeat(black, 3)
    if black.shape != (3,):
        raise ValueError("black_level_dn requires a scalar or three channel values")
    tolerance = _object(
        data["tolerance"],
        {"channel_rmse_dn", "max_abs_dn", "ratio_abs"},
        {"channel_rmse_dn", "max_abs_dn"},
        "tolerance",
    )
    channel_limits = _array(tolerance["channel_rmse_dn"], "channel_rmse_dn", (3,), low=0)
    maximum_limit = float(_array(tolerance["max_abs_dn"], "max_abs_dn", (), low=0))
    ratio_limit = (
        None
        if "ratio_abs" not in tolerance
        else float(_array(tolerance["ratio_abs"], "ratio_abs", (), low=0))
    )
    patch_ids = data.get("patch_ids", [f"patch-{i:02d}" for i in range(count)])
    if not isinstance(patch_ids, list) or len(patch_ids) != count:
        raise ValueError("patch_ids must be unique and match patch count")
    for identity in patch_ids:
        _text(identity, "patch_id")
    if len(set(patch_ids)) != count:
        raise ValueError("patch_ids must be unique and match patch count")
    sampled = np.column_stack([np.interp(wave, qe_wave, qe[:, c]) for c in range(3)])
    electrons = np.trapezoid(photons[:, :, None] * sampled[None, :, :], wave, axis=1)
    predicted = black + p["dn_per_e"] * electrons
    if not np.isfinite(predicted).all() or np.any(predicted >= white):
        raise ValueError("Frozen QE predicts ADC saturation; linear comparison is invalid")
    residual = predicted - measured
    channel_rmse = np.sqrt(np.mean(residual**2, axis=0))
    maximum = float(np.max(np.abs(residual)))
    patches, ratio_errors, unavailable = [], [], 0
    for index, identity in enumerate(patch_ids):
        a, b = predicted[index] - black, measured[index] - black
        ratios = {
            key: None
            for key in (
                "predicted_rg",
                "measured_rg",
                "error_rg",
                "predicted_bg",
                "measured_bg",
                "error_bg",
            )
        }
        if a[1] > 0 and b[1] > 0:
            for name, channel in (("rg", 0), ("bg", 2)):
                ratios["predicted_" + name] = float(a[channel] / a[1])
                ratios["measured_" + name] = float(b[channel] / b[1])
                ratios["error_" + name] = ratios["predicted_" + name] - ratios["measured_" + name]
                ratio_errors.append(abs(ratios["error_" + name]))
        else:
            unavailable += 1
        patches.append(
            {
                "patch_id": identity,
                "predicted_raw_rgb_dn": predicted[index].tolist(),
                "measured_raw_rgb_dn": measured[index].tolist(),
                "residual_dn": residual[index].tolist(),
                "rmse_dn": float(np.sqrt(np.mean(residual[index] ** 2))),
                "max_abs_dn": float(np.max(np.abs(residual[index]))),
                "ratios": ratios,
            }
        )
    ratio_max = max(ratio_errors) if ratio_errors else None
    checks = [
        {
            "id": f"rmse_{channel}_dn",
            "status": "pass" if value <= limit else "fail",
            "value": float(value),
            "limit": float(limit),
        }
        for channel, value, limit in zip(CHANNELS, channel_rmse, channel_limits, strict=True)
    ]
    checks.append(
        {
            "id": "max_abs_dn",
            "status": "pass" if maximum <= maximum_limit else "fail",
            "value": maximum,
            "limit": maximum_limit,
        }
    )
    checks.append(
        {
            "id": "signal_rg_bg_ratio",
            "status": "not_evaluated"
            if ratio_limit is None
            else "pass"
            if unavailable == 0 and ratio_max is not None and ratio_max <= ratio_limit
            else "fail",
            "value": ratio_max,
            "limit": ratio_limit,
        }
    )
    passed = all(row["status"] != "fail" for row in checks)
    after = profile_sha256(profile)
    if before != after:
        raise ValueError("QE profile mutated during validation")
    return {
        "schema": "camera_spectral_raw_validation_result_v1",
        "status": "pass" if passed else "fail",
        "numerical_tolerance_pass": passed,
        "physical_validation": "not_established",
        "channels": CHANNELS.copy(),
        "patch_ids": patch_ids,
        "unit": "DN",
        "wavelength_nm": wave.tolist(),
        "predicted_electrons": electrons.tolist(),
        "predicted_raw_rgb_dn": predicted.tolist(),
        "measured_raw_rgb_dn": measured.tolist(),
        "residual_dn": residual.tolist(),
        "patches": patches,
        "metrics": {
            "channel_rmse_dn": channel_rmse.tolist(),
            "channel_bias_dn": residual.mean(axis=0).tolist(),
            "overall_rmse_dn": float(np.sqrt(np.mean(residual**2))),
            "max_abs_dn": maximum,
            "ratio_max_abs": ratio_max,
            "ratio_unavailable_patches": unavailable,
        },
        "checks": checks,
        "qe_profile_sha256_before": before,
        "qe_profile_sha256_after": after,
        "qe_unchanged": True,
        "payload_sha256": _hash(payload),
        "normalization": "none",
        "source_kind": data["source_kind"],
        "qe_source_kind": p["source_kind"],
        "scope": "Frozen CFA-inclusive QE and sensor-surface photon dose; linear RAW DN only",
        "limitations": [
            "Lens/filter transport is already included in photon dose and is not reapplied.",
            "Dose is exposure-integrated; exposure is checked, not multiplied into dose again.",
            "No fitted gain, per-patch normalization, white balance, CCM or QE updates.",
            "R/G and B/G use black-subtracted signals; nonpositive green makes ratios unavailable.",
            "Numerical pass neither authenticates radiometry nor validates physical hardware.",
        ],
    }
