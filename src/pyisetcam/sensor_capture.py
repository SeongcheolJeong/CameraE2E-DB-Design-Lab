"""Explicit digital RAW capture contract for CameraE2E.

The original sensor API retains its inverse analog-gain convention. This
opt-in adapter uses physical amplification, electron-domain readout parameters,
and a digital black pedestal. It supports a single linear mosaic exposure.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from .types import OpticalImage, Sensor, SessionContext
from .utils import param_format

_DEFAULT_READOUT = {
    "gain": 1.0,
    "full_well_e": 10000.0,
    "read_noise_e": 10.0,
    "dark_current_e_per_s": 10.0,
    "bit_depth": 12,
    "black_level_dn": 64.0,
}
_CAPTURE_ARRAY_KEYS = ("mean_electrons", "reference_dn", "raw_dn")


def _readout_values(values: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(values) - set(_DEFAULT_READOUT)
    if unknown:
        raise ValueError(f"Unknown digital RAW readout parameters: {sorted(unknown)}")
    result = dict(values)
    for key in _DEFAULT_READOUT:
        value = float(result[key])
        if not np.isfinite(value):
            raise ValueError(f"Digital RAW {key} must be finite.")
        result[key] = value
    bits = result["bit_depth"]
    if bits != int(bits) or not 1 <= bits <= 24:
        raise ValueError("Digital RAW bit_depth must be an integer in [1, 24].")
    result["bit_depth"] = int(bits)
    for key in ("gain", "full_well_e"):
        if result[key] <= 0:
            raise ValueError(f"Digital RAW {key} must be positive.")
    for key in ("read_noise_e", "dark_current_e_per_s", "black_level_dn"):
        if result[key] < 0:
            raise ValueError(f"Digital RAW {key} must be nonnegative.")
    if result["black_level_dn"] >= 2 ** result["bit_depth"] - 1:
        raise ValueError("Digital RAW black_level_dn must be below the ADC white level.")
    return result


def _require_single_linear_capture(sensor: Sensor) -> float:
    if param_format(sensor.fields.get("response_type", "linear")) != "linear":
        raise ValueError("Digital RAW requires linear response; use legacy mode for log sensors.")
    if not sensor.fields.get("mosaic", True):
        raise ValueError("Digital RAW v1 requires a mosaic sensor.")
    if sensor.fields.get("sensor_compute_method"):
        raise ValueError("Digital RAW v1 does not support binning/HDR; use legacy mode.")
    exposure = np.asarray(sensor.fields.get("integration_time", 0.0), dtype=float)
    if exposure.ndim > 1 or exposure.size != 1:
        raise ValueError("Digital RAW v1 requires one scalar exposure; use legacy mode for HDR.")
    value = float(exposure.reshape(-1)[0])
    if not np.isfinite(value) or value < 0:
        raise ValueError("Digital RAW exposure must be finite and nonnegative.")
    return value


def configure_e2e_sensor(
    sensor: Sensor,
    *,
    mode: str = "digital_raw",
    readout: Mapping[str, Any] | None = None,
) -> Sensor:
    """Return a configured clone, preserving cached mean charge on reconfiguration.

    Readout gain is physical amplification. On first configuration its default
    is the reciprocal of the legacy analog-gain divisor. Unspecified readout
    parameters use the declared assumptions above; they are not measured data.
    Existing marked settings survive partial updates. ``mode='legacy'`` selects
    the original low-level computation explicitly.
    """

    selected_mode = str(mode).lower().replace("-", "_")
    if selected_mode not in {"digital_raw", "legacy"}:
        raise ValueError("Sensor output mode must be 'digital_raw' or 'legacy'.")
    updated = sensor.clone()
    updated.fields["sensor_output_mode"] = selected_mode
    if selected_mode == "legacy":
        if readout:
            raise ValueError("Digital RAW readout parameters cannot be used in legacy mode.")
        updated.fields.pop("sensor_readout", None)
        updated.metadata.pop("sensor_capture", None)
        for key in _CAPTURE_ARRAY_KEYS:
            updated.data.pop(key, None)
        return updated

    _require_single_linear_capture(updated)
    existing = sensor.fields.get("sensor_readout", {})
    values = dict(_DEFAULT_READOUT)
    if sensor.fields.get("sensor_output_mode") == "digital_raw":
        values.update(existing)
    else:
        legacy_gain = float(sensor.fields.get("analog_gain", 1.0))
        if not np.isfinite(legacy_gain) or legacy_gain <= 0:
            raise ValueError("Legacy analog gain must be finite and positive.")
        values["gain"] = 1.0 / legacy_gain
    values.update(dict(readout or {}))
    values = _readout_values(values)
    updated.fields["sensor_readout"] = values
    updated.fields["analog_gain"] = 1.0 / values["gain"]
    updated.fields["analog_offset"] = 0.0
    updated.fields["nbits"] = values["bit_depth"]
    updated.fields["zero_level"] = values["black_level_dn"]
    updated.fields["quantization"] = "linear"
    pixel = updated.fields["pixel"]
    voltage_swing = float(pixel["voltage_swing"])
    if not np.isfinite(voltage_swing) or voltage_swing <= 0:
        raise ValueError("Digital RAW voltage swing must be finite and positive.")
    conversion_gain = voltage_swing / values["full_well_e"]
    pixel["conversion_gain_v_per_electron"] = conversion_gain
    pixel["read_noise_v"] = values["read_noise_e"] * conversion_gain
    pixel["dark_voltage_v_per_sec"] = values["dark_current_e_per_s"] * conversion_gain
    return updated


def _fixed_pattern_components(
    sensor: Sensor, shape: tuple[int, int]
) -> tuple[np.ndarray | float, np.ndarray | float]:
    """Return charge gain and signed readout bias, persisting configured maps.

    Pixel/column gain retain their existing multiplicative response convention.
    DSNU maps in this digital path are fixed readout bias, in input-referred
    volts, not dark-current variation. Bias cannot create or remove stored well
    charge and must be added after the physical well capacity is enforced.
    """

    rows, cols = shape
    pixel = sensor.fields["pixel"]
    conversion_gain = float(pixel["conversion_gain_v_per_electron"])
    column_sigmas = np.asarray(sensor.fields.get("column_fpn", [0.0, 0.0]), dtype=float)
    if column_sigmas.shape != (2,) or not np.all(np.isfinite(column_sigmas)):
        raise ValueError("Digital RAW column FPN requires two finite offset/gain sigmas.")
    specs = (
        ("offset_fpn_image", (rows, cols), float(pixel.get("dsnu_sigma_v", 0)), 0.0),
        ("gain_fpn_image", (rows, cols), float(pixel.get("prnu_sigma", 0)), 1.0),
        ("column_offset_fpn", (cols,), float(column_sigmas[0]), 0.0),
        ("column_gain_fpn", (cols,), float(column_sigmas[1]), 1.0),
    )
    streams = np.random.SeedSequence(sensor.fields.get("fixed_pattern_seed", 0)).spawn(4)
    maps = []
    for (name, shape, sigma, center), stream in zip(specs, streams, strict=True):
        if not np.isfinite(sigma) or sigma < 0:
            raise ValueError(f"Digital RAW {name} sigma must be finite and nonnegative.")
        stored = sensor.fields.get(name)
        if stored is not None:
            values = np.asarray(stored, dtype=float)
            if values.shape != shape or not np.all(np.isfinite(values)):
                raise ValueError(f"Digital RAW {name} must be finite and match the sensor shape.")
        elif sigma > 0:
            values = np.random.default_rng(stream).normal(center, sigma, size=shape)
            sensor.fields[name] = values.copy()
        else:
            # No fabricated spatial-pattern array for an unspecified/zero model.
            values = center
        maps.append(values)
    offset, gain, column_offset, column_gain = maps
    bias_e = (offset * column_gain + column_offset) / conversion_gain
    return gain * column_gain, bias_e


def resample_sensor_capture(sensor: Sensor, *, seed: int | None) -> Sensor:
    """Draw a RAW frame from cached photoelectron means without recomputing optics.

    ``mean_electrons`` excludes dark charge, fixed patterns, gain and random
    noise. ``reference_dn`` is the continuous, clipped expectation-chain output
    before ADC rounding, including mean dark charge and enabled fixed patterns.
    Multiplicative PRNU precedes well clipping; signed DSNU/readout offsets
    follow well clipping, then read noise and the digital black pedestal.
    Flags 0/-1 disable noise; 1/-2 retain shot noise; 2 also adds read noise.
    """

    if sensor.fields.get("sensor_output_mode") != "digital_raw":
        raise ValueError("resample_sensor_capture requires a digital_raw sensor.")
    updated = configure_e2e_sensor(sensor)
    exposure = _require_single_linear_capture(updated)
    mean = np.asarray(updated.data.get("mean_electrons"), dtype=float)
    if mean.shape != tuple(updated.fields["size"]) or not np.all(np.isfinite(mean)):
        raise ValueError("Digital RAW requires a finite mean_electrons image matching sensor size.")
    if np.any(mean < 0):
        raise ValueError("Digital RAW mean_electrons must be nonnegative.")
    readout = updated.fields["sensor_readout"]
    flag = int(updated.fields.get("noise_flag", 2))
    if flag not in {-2, -1, 0, 1, 2}:
        raise ValueError("Digital RAW noise flag must be -2, -1, 0, 1, or 2.")
    rng = np.random.default_rng(seed)
    total_mean = mean + readout["dark_current_e_per_s"] * exposure
    electrons = rng.poisson(total_mean).astype(float) if flag in {-2, 1, 2} else total_mean.copy()
    expected = total_mean.copy()
    bias_e = 0.0
    if flag in {1, 2}:
        response_gain, bias_e = _fixed_pattern_components(updated, mean.shape)
        electrons *= response_gain
        expected *= response_gain
    full_well = readout["full_well_e"]
    well_fraction = float(np.mean(electrons >= full_well))
    electrons = np.clip(electrons, 0.0, full_well)
    expected = np.clip(expected, 0.0, full_well)
    electrons += bias_e
    expected += bias_e
    if flag == 2 and readout["read_noise_e"] > 0:
        electrons += rng.normal(0.0, readout["read_noise_e"], size=mean.shape)
    gain = readout["gain"]
    white = 2 ** readout["bit_depth"] - 1
    black = readout["black_level_dn"]
    dn_per_e = (white - black) * gain / full_well
    continuous_dn = black + dn_per_e * electrons
    raw = np.clip(np.rint(continuous_dn), 0, white).astype(np.int32)
    updated.data["mean_electrons"] = mean.copy()
    updated.data["reference_dn"] = np.clip(black + dn_per_e * expected, 0.0, white)
    updated.data["raw_dn"] = raw
    updated.data["dv"] = raw.copy()
    # Signed ADC-input signal voltage relative to the digital black pedestal;
    # retaining it is diagnostic only. ISP processing consumes raw_dn.
    conversion_gain = float(updated.fields["pixel"]["conversion_gain_v_per_electron"])
    updated.data["volts"] = electrons * gain * conversion_gain
    updated.data["channel_volts"] = None
    previous = dict(updated.metadata.get("sensor_capture", {}))
    updated.metadata["sensor_capture"] = {
        **previous,
        "schema": "camerae2e_sensor_capture_v1",
        "mode": "digital_raw",
        "readout": dict(readout),
        "white_level_dn": white,
        "black_level_dn": black,
        "dn_per_electron": dn_per_e,
        "exposure_time_s": exposure,
        "noise_seed": seed,
        "fixed_pattern_seed": updated.fields.get("fixed_pattern_seed", 0),
        "noise_flag": flag,
        "fixed_pattern_order": "response_gain_then_well_clip_then_signed_bias_then_read_noise",
        "dsnu_definition": "fixed_readout_bias_per_capture_not_dark_current_variation",
        "units": {
            "mean_electrons": "photoelectrons/pixel before dark, FPN, noise and gain",
            "reference_dn": "continuous DN before rounding, clipped to ADC range",
            "raw_dn": "integer DN including digital black pedestal",
            "volts": "signed post-gain ADC-input signal V relative to digital black pedestal",
        },
        "saturation": {
            "well_fraction": well_fraction,
            "adc_white_fraction": float(np.mean(raw == white)),
            "adc_zero_fraction": float(np.mean(raw == 0)),
        },
        "provenance": "model-derived capture; readout values are configured assumptions",
    }
    return updated


def normalized_sensor_dn(sensor: Sensor) -> np.ndarray:
    """Return signed, once-black-subtracted RAW normalized by white-minus-black."""

    if sensor.fields.get("sensor_output_mode") != "digital_raw":
        raise ValueError("normalized_sensor_dn requires a digital_raw sensor.")
    if sensor.data.get("raw_dn") is None:
        raise ValueError("Digital RAW sensor has no computed raw_dn.")
    values = np.asarray(sensor.data["raw_dn"], dtype=float)
    readout = _readout_values(sensor.fields["sensor_readout"])
    black = readout["black_level_dn"]
    white = 2 ** readout["bit_depth"] - 1
    return (values - black) / (white - black)


def compute_sensor_capture(
    sensor: Sensor,
    oi: OpticalImage,
    *,
    seed: int | None = None,
    session: SessionContext | None = None,
) -> Sensor:
    """Internal dispatch: compute optical mean once, then sample the RAW readout."""

    from .sensor import sensor_compute_noise_free
    from .session import track_session_object

    configured = configure_e2e_sensor(sensor)
    exposure = _require_single_linear_capture(configured)
    if exposure == 0 and not configured.fields.get("auto_exposure", False):
        mean = np.zeros(configured.fields["size"], dtype=float)
    else:
        optical = configured.clone()
        optical.fields.pop("sensor_output_mode", None)
        optical.fields.pop("sensor_readout", None)
        optical.metadata.pop("sensor_capture", None)
        optical = sensor_compute_noise_free(optical, oi, seed=seed)
        conversion_gain = float(optical.fields["pixel"]["conversion_gain_v_per_electron"])
        mean = np.asarray(optical.data["volts"], dtype=float) / conversion_gain
        configured.fields["integration_time"] = optical.fields["integration_time"]
        configured.fields["auto_exposure"] = False
    configured.data["mean_electrons"] = mean
    selected_seed = configured.fields.get("noise_seed", 0) if seed is None else seed
    result = resample_sensor_capture(configured, seed=selected_seed)
    return track_session_object(session, result)
