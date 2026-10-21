"""Bounded camera runtime plugins with explicit physical interfaces.

The common input is on-axis spectral irradiance at the sensor entrance in
W m^-2 nm^-1. It already includes lens transmission and aperture throughput.
``qe[channel, wavelength]`` is collected electrons per incident photon,
including CFA/microlens/charge collection. Never multiply those effects again.

Response-DB v1 is a static, rotationally symmetric flat-field pilot, not a
general scene renderer. Lens angular weights are discrete *power fractions*,
already integrated over angular bins (no extra sin(theta) factor). Sensor
``relative_response[channel,wavelength,theta]`` is relative to the supplied
normal-incidence QE. All axes have explicit units in their field names.

Results separate a JSON-compatible summary from NumPy artifact arrays. No
runtime result establishes physical validation or quantified model accuracy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import numpy as np

PLANCK_J_S = 6.62607015e-34
LIGHT_M_S = 299792458.0
CHANNELS = ["R", "G", "B"]
READOUT_KEYS = {
    "full_well_e",
    "read_noise_e",
    "dark_current_e_per_s",
    "gain",
    "bit_depth",
    "black_level_dn",
}


def sensor_response_fields(payload):
    """Choose one optical-to-charge contract, never multiply both responses."""
    if "native_pixel_response" in payload:
        if {"qe", "sensor_angular_response"} & set(payload):
            raise ValueError("Native absolute site EQE replaces qe and sensor_angular_response")
        response = payload["native_pixel_response"]
        generation = isinstance(response, Mapping) and response.get("schema") == (
            "camera_native_generation_response_v1")
        if generation or "charge_collection" in payload:
            from .charge_collection import validate_pair

            if "charge_collection" not in payload:
                raise ValueError("Generated-charge response requires explicit charge_collection")
            response, collection = validate_pair(response, payload["charge_collection"])
            return {"native_pixel_response": response, "charge_collection": collection}
        return {"native_pixel_response": payload["native_pixel_response"]}
    if "charge_collection" in payload:
        raise ValueError("Charge collection cannot be applied to already-collected legacy QE")
    return {"qe": payload["qe"], "sensor_angular_response": payload["sensor_angular_response"]}


def with_sensor_effects(effects, payload):
    """Retain the old ledger exactly unless separate generation/collection is active."""
    if "charge_collection" not in payload:
        return list(effects)
    result = []
    for effect in effects:
        result.extend(["sensor_charge_generation", "sensor_charge_collection"]
                      if effect == "sensor_effective_qe" else [effect])
    return result


def charge_collection_capabilities():
    return {
        "generation_schema": "camera_native_generation_response_v1",
        "collection_schema": "camera_charge_collection_v1",
        "geometry": "complete_native_sensor_only; each side even and in [8,512]",
        "crop_halo": "not_implemented; partial-sensor requests refused",
        "timing": "instantaneous source-to-destination rate before receiver row exposure",
        "statistics": "single_destination_poisson_thinning",
        "electrical_prnu": "requires_zero",
        "max_offset_pixels": 4, "max_offsets": 81,
        "max_generated_band_rate_e_per_pixel_s_nm": 1e13,
        "max_single_scene_routing_samples": 128_000_000,
        "max_timeline_routing_samples": 512_000_000,
        "physical_validation": "not_established",
    }


class RuntimeRequestError(ValueError):
    """A structured request refusal suitable for an API validation response."""

    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = issues
        super().__init__("; ".join(f"{x['field']}: {x['message']}" for x in issues))


class RuntimePlugin(Protocol):
    def describe_capabilities(self) -> dict[str, Any]: ...
    def validate_request(self, request: Mapping[str, Any]) -> list[dict[str, Any]]: ...
    def estimate_resources(self, request: Mapping[str, Any]) -> dict[str, Any]: ...
    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


def _issue(field: str, message: str, code: str = "INVALID_INPUT") -> dict[str, str]:
    return {"code": code, "field": field, "message": message, "severity": "error"}


def _array(value: Any, field: str, ndim: int) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=float)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a numeric array") from exc
    if result.ndim != ndim or not np.isfinite(result).all():
        raise ValueError(f"{field} must be a finite {ndim}-D array")
    return result


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number, not boolean")
    result = float(value)
    if not np.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{field} must be in [{minimum}, {maximum}]")
    return result


def _axis(value: Any, field: str, low: float, high: float, maximum: int) -> np.ndarray:
    result = _array(value, field, 1)
    if not 2 <= result.size <= maximum or not np.all(np.diff(result) > 0):
        raise ValueError(f"{field} requires 2..{maximum} strictly increasing samples")
    if result[0] < low or result[-1] > high:
        raise ValueError(f"{field} must stay within [{low}, {high}]")
    return result


def _common(request: Mapping[str, Any]) -> dict[str, Any]:
    wave = _axis(request["wavelength_nm"], "wavelength_nm", 200, 2500, 2048)
    irradiance = _array(request["spectral_irradiance_w_m2_nm"], "irradiance", 1)
    if irradiance.shape != wave.shape or np.any(irradiance < 0):
        raise ValueError("irradiance must be nonnegative and match wavelength_nm")
    native = "native_pixel_response" in request
    if native:
        from .native_pixel_response import validate_response

        selected = sensor_response_fields(request)
        optical = {"native_pixel_response": validate_response(request["native_pixel_response"])}
        if "charge_collection" in selected:
            optical["charge_collection"] = selected["charge_collection"]
        response = optical["native_pixel_response"]
        if response["wavelength_nm"] != wave.tolist():
            raise ValueError("Native EQE and runtime wavelength coordinates must match exactly")
        if response["pixel_pitch_um"] != request["pixel_pitch_um"]:
            raise ValueError("Native EQE and sensor pixel pitch must match exactly")
        if request.get("parameter_uncertainty"):
            raise ValueError("Native site/azimuth uncertainty propagation is not implemented")
        optical["channels"] = response["site_names"].copy()
    else:
        if "charge_collection" in request:
            raise ValueError("Charge collection cannot be applied to already-collected legacy QE")
        qe = _array(request["qe"], "qe", 2)
        if qe.shape != (3, wave.size) or np.any((qe < 0) | (qe > 1)):
            raise ValueError("qe must have shape [3, wavelength] with values in [0, 1]")
        optical = {"qe": qe, "channels": CHANNELS.copy()}
    plane = request.get("irradiance_reference_plane", "sensor_surface_on_axis")
    if plane != "sensor_surface_on_axis":
        raise ValueError("irradiance_reference_plane must be sensor_surface_on_axis")
    pitch = _number(request["pixel_pitch_um"], "pixel_pitch_um", 0.1, 1000)
    exposure = _number(request["exposure_s"], "exposure_s", 0, 3600)
    readout = dict(request["readout"])
    if set(readout) != READOUT_KEYS:
        raise ValueError(f"readout requires exactly {sorted(READOUT_KEYS)}")
    bounds = {
        "full_well_e": (1, 1e9),
        "read_noise_e": (0, 1e6),
        "dark_current_e_per_s": (0, 1e9),
        "gain": (1e-6, 1e6),
        "bit_depth": (1, 24),
        "black_level_dn": (0, 2**24 - 2),
    }
    for key, (low, high) in bounds.items():
        readout[key] = _number(readout[key], f"readout.{key}", low, high)
    if readout["bit_depth"] != int(readout["bit_depth"]):
        raise ValueError("bit_depth must be an integer")
    readout["bit_depth"] = int(readout["bit_depth"])
    if readout["black_level_dn"] >= 2 ** readout["bit_depth"] - 1:
        raise ValueError("black_level_dn must be below the ADC white level")
    temperature = request.get("temperature_c")
    if temperature is not None:
        temperature = _number(temperature, "temperature_c", -273.15, 1000)
    photon_density = irradiance * wave * 1e-9 / (PLANCK_J_S * LIGHT_M_S)
    incident = np.trapezoid(photon_density, x=wave) * (pitch * 1e-6) ** 2 * exposure
    if not np.isfinite(incident) or incident > 1e12:
        raise ValueError("incident photons/pixel exceed the 1e12 bounded-runtime limit")
    return {
        "wave": wave,
        "irradiance": irradiance,
        **optical,
        "pitch": pitch,
        "exposure": exposure,
        "readout": readout,
        "photon_density": photon_density,
        "temperature": temperature,
    }


def _integrate(inputs: dict[str, Any], effective_qe: np.ndarray) -> np.ndarray:
    return (
        np.trapezoid(effective_qe * inputs["photon_density"], x=inputs["wave"], axis=-1)
        * (inputs["pitch"] * 1e-6) ** 2
        * inputs["exposure"]
    )


def _nullable(values: np.ndarray) -> list[Any]:
    if values.ndim > 1:
        return [_nullable(row) for row in values]
    return [float(x) if np.isfinite(x) else None for x in values]


def _summary(
    inputs: dict[str, Any],
    electrons: np.ndarray,
    fields: np.ndarray,
    plugin_id: str,
    effects: list[str],
) -> dict[str, Any]:
    readout = inputs["readout"]
    dark = readout["dark_current_e_per_s"] * inputs["exposure"]
    capacity = min(readout["full_well_e"], readout["full_well_e"] / readout["gain"])
    linear = electrons + dark < capacity
    theoretical_noise = np.sqrt(electrons + dark + readout["read_noise_e"] ** 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        snr = np.where(
            linear & (electrons > 0) & (theoretical_noise > 0),
            20 * np.log10(electrons / theoretical_noise),
            np.nan,
        )
    white = 2 ** readout["bit_depth"] - 1
    dn_per_e = (white - readout["black_level_dn"]) * readout["gain"] / readout["full_well_e"]
    # Expected black/dark-corrected continuous RAW, accounting for clipping.
    signal = (np.minimum(electrons + dark, capacity) - min(dark, capacity)) * dn_per_e
    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = (signal[:, [0, 3]] / signal[:, 1:3].mean(axis=1, keepdims=True)
                  if "native_pixel_response" in inputs else signal[:, [0, 2]] / signal[:, 1:2])
    conditions = {
        "exposure_s": {"value": inputs["exposure"], "unit": "s"},
        "gain": {"value": readout["gain"], "unit": "1"},
    }
    if inputs["temperature"] is not None:
        conditions["temperature_c"] = {"value": inputs["temperature"], "unit": "degC"}
    return {
        "schema_version": "1.0",
        "plugin_id": plugin_id,
        "source_kind": "simulated",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "channels": inputs.get("channels", CHANNELS),
        "field_radius": fields.tolist(),
        "channel_electrons": electrons.tolist(),
        "channel_ratios": _nullable(ratios),
        "channel_reference_signal_dn": signal.tolist(),
        "theoretical_snr_db": _nullable(snr),
        "saturation_margin_e": (capacity - electrons - dark).tolist(),
        "linear_unsaturated": bool(linear.all()),
        "readout": readout,
        "conditions": conditions,
        "integration": "trapezoidal over wavelength_nm; irradiance density per nm",
        "irradiance_reference_plane": "sensor_surface_on_axis",
        "included_effects": effects,
        "units": {
            "channel_electrons": "electron/pixel",
            "channel_ratios": "1",
            "channel_reference_signal_dn": "DN",
            "saturation_margin_e": "electron/pixel",
            "theoretical_snr_db": "dB",
        },
        "prediction": {
            "metric_id": "raw.channel_ratio",
            "unit": "1",
            "values": _nullable(ratios.reshape(-1)),
            "axes": [
                {"name": "field_radius", "unit": "1", "values": fields.tolist()},
                {"name": "channel_ratio", "unit": "1", "values": ["R/G", "B/G"]},
            ],
            "reference_plane": "sensor_raw",
            "included_effects": effects,
            "conditions": conditions,
            "source_kind": "simulated",
            "method_and_version": plugin_id + ":continuous_dark_corrected_expectation",
            "uncertainty": {"kind": "unknown"},
        },
        "limitations": [
            "No physical accuracy or model uncertainty has been established.",
            "Input QE is effective collected-electron QE, not optical absorption alone.",
            "Irradiance already includes on-axis lens transmission/aperture throughput.",
            "Theoretical SNR is an unsaturated Poisson/read-noise estimate, not an EMVA result.",
            "Prediction ratios use continuous expected RAW after matched dark subtraction; "
            "ADC rounding and sampling uncertainty are not included in that prediction.",
            "Temperature is recorded only; readout/QE parameters must already match it.",
        ],
    }


class QuickEstimateRuntime:
    plugin_id = "quick_estimate_v1"

    def describe_capabilities(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "role": "runtime",
            "version": "1.0",
            "mode": "quick_estimate",
            "available": True,
            "supports": [
                "spectral_photoelectrons",
                "theoretical_snr",
                "saturation_margin",
                "conditional_input_bound_propagation",
            ],
            "unsupported": ["scene_rendering", "temporal_dynamics", "physical_validation"],
            "input_plane": "sensor_surface_on_axis",
            "channels": CHANNELS,
            "native_response": {
                "schema": "camera_native_pixel_response_v1",
                "sites": ["R", "Gr", "Gb", "B"],
                "angular_condition": "normal_incidence_only",
                "uncertainty_propagation": "not_implemented",
            },
            "physical_validation": "not_established",
        }

    def _parse(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if "charge_collection" in request:
            raise ValueError("Quick estimate has no native spatial collection geometry; "
                             "use a full-sensor Response DB or spatial capture")
        return _common(request)

    def validate_request(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            self._parse(request)
            from .uncertainty import validate_request

            validate_request(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            code = "OUT_OF_DOMAIN" if str(exc).startswith("OUT_OF_DOMAIN:") else "INVALID_INPUT"
            return [_issue("request", str(exc), code)]
        return []

    def _checked(self, request: Mapping[str, Any]) -> dict[str, Any]:
        issues = self.validate_request(request)
        if issues:
            raise RuntimeRequestError(issues)
        return self._parse(request)

    def estimate_resources(self, request: Mapping[str, Any]) -> dict[str, Any]:
        inputs = self._checked(request)
        return {
            "cpu_threads": 1,
            "gpu_required": False,
            "working_array_bytes_estimate": int(inputs["wave"].size * 8 * 12),
            "estimate_kind": "array_budget_not_measured_peak",
            "execution": "in_process",
        }

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        inputs = self._checked(request)
        if "native_pixel_response" in inputs:
            from .native_pixel_response import evaluate

            qe = evaluate(inputs["native_pixel_response"], inputs["wave"], 0.0, 0.0).T
        else:
            qe = inputs["qe"]
        electrons = _integrate(inputs, qe)[None, :]
        summary = _summary(
            inputs,
            electrons,
            np.array([0.0]),
            self.plugin_id,
            ["sensor_effective_qe", "mean_dark_charge", "readout_clipping"],
        )
        from .uncertainty import attach, propagate

        if "native_pixel_response" in inputs:
            from .registry import digest

            summary["native_pixel_response"] = {
                "sha256": digest(inputs["native_pixel_response"]),
                "site_names": inputs["channels"],
                "angular_condition": "normal_incidence_only_quick_estimate",
                "ratio_green_definition": "arithmetic_mean_of_Gr_and_Gb_expectations",
                "uncertainty": {"kind": "unknown"},
            }
        else:
            attach(summary, propagate(request, inputs, response_db=False))
        return {"summary": summary, "arrays": {"field_channel_electrons": electrons}}


class ResponseDBRuntime(QuickEstimateRuntime):
    plugin_id = "response_db_v1"

    def describe_capabilities(self) -> dict[str, Any]:
        import importlib.util

        return {
            "plugin_id": self.plugin_id,
            "role": "runtime",
            "version": "1.0",
            "mode": "response_db",
            "available": importlib.util.find_spec("pyisetcam") is not None,
            "availability_note": "Module discovery only; execution checks engine/assets.",
            "supports": [
                "static_flat_field",
                "discrete_incident_angle_distribution",
                "radial_color_shading",
                "digital_raw",
                "bilinear_sensor_rgb",
                "independent_noise_frames",
                "temporal_noise_estimate",
                "conditional_input_bound_propagation",
            ],
            "unsupported": [
                "general_scene",
                "psf",
                "distortion",
                "polarization",
                "hdr",
                "rolling_shutter_motion",
                "physical_validation",
            ],
            "limits": {"max_output_side": 512, "max_frames": 16, "max_frame_pixels": 4194304},
            "native_response": {
                "schema": "camera_native_pixel_response_v1",
                "sites": ["R", "Gr", "Gb", "B"],
                "requires": ["joint_directional_response", "explicit_native_crop"],
                "theta_only_legacy_azimuth": "unsupported",
                "inter_site_redistribution": "unsupported",
                "max_weighted_direction_samples": 128_000_000,
            },
            "charge_collection": charge_collection_capabilities(),
            "physical_validation": "not_established",
        }

    def _parse(self, request: Mapping[str, Any], *,
               _validated_charge_collection_support: bool = False) -> dict[str, Any]:
        inputs = _common(request)
        if "native_pixel_response" in inputs:
            return _parse_native_response(
                request, inputs,
                validated_charge_collection_support=_validated_charge_collection_support)
        lens, sensor = request["lens"], request["sensor_angular_response"]
        if "field_reference_radius_mm" in lens:
            if type(lens["field_reference_radius_mm"]) not in (int, float):
                raise ValueError("lens.field_reference_radius_mm requires a finite real number")
            inputs["field_reference_radius_mm"] = _number(
                lens["field_reference_radius_mm"], "lens.field_reference_radius_mm", 1e-12, 1000000
            )
        field = _axis(lens["field_radius"], "lens.field_radius", 0, 1, 256)
        if field[0] != 0 or field[-1] != 1:
            raise ValueError("lens.field_radius must cover the complete normalized radius [0,1]")
        theta = _axis(lens["theta_deg"], "lens.theta_deg", 0, 89.9, 181)
        sensor_theta = _axis(sensor["theta_deg"], "sensor.theta_deg", 0, 89.9, 181)
        if sensor_theta[0] != 0:
            raise ValueError("sensor.theta_deg must include the normal-incidence reference at zero")
        if theta[0] < sensor_theta[0] or theta[-1] > sensor_theta[-1]:
            raise ValueError("OUT_OF_DOMAIN: lens angles exceed sensor angular response coverage")
        spectral_lens = "wavelength_nm" in lens
        if spectral_lens and lens["wavelength_nm"] != inputs["wave"].tolist():
            raise ValueError("Lens and sensor wavelength coordinates must match exactly")
        weights = _array(lens["angular_weights"], "lens.angular_weights", 3 if spectral_lens else 2)
        shape = (
            (inputs["wave"].size, field.size, theta.size)
            if spectral_lens
            else (field.size, theta.size)
        )
        if weights.shape != shape or np.any(weights < 0):
            raise ValueError("angular_weights must match declared wavelength/field/theta axes")
        if not np.allclose(weights.sum(axis=-1), 1, rtol=0, atol=1e-8):
            raise ValueError(
                "angular_weights must sum to 1 for every field; no automatic normalization"
            )
        ri = _array(
            lens["relative_illumination"], "lens.relative_illumination", 2 if spectral_lens else 1
        )
        ri_shape = (inputs["wave"].size, field.size) if spectral_lens else field.shape
        if ri.shape != ri_shape or np.any(ri < 0) or not np.all(ri[..., 0] == 1):
            raise ValueError(
                "relative_illumination must match wavelength/field, "
                "be nonnegative and normalize on axis"
            )
        if not spectral_lens:
            weights = np.broadcast_to(weights, (inputs["wave"].size, *weights.shape))
            ri = np.broadcast_to(ri, (inputs["wave"].size, *ri.shape))
        response = _array(
            sensor.get("relative_response", sensor.get("response")), "sensor.relative_response", 3
        )
        if response.shape != (3, inputs["wave"].size, sensor_theta.size) or np.any(response < 0):
            raise ValueError("relative_response must be nonnegative [3,wavelength,sensor_theta]")
        if np.any(response * inputs["qe"][..., None] > 1 + 1e-12):
            raise ValueError("Effective angular QE exceeds one collected electron per photon")
        if not np.allclose(response[..., 0], 1, rtol=0, atol=1e-8):
            raise ValueError("relative_response must equal one at normal incidence")
        size = _array(request.get("output_size", [64, 64]), "output_size", 1)
        if size.shape != (2,) or np.any(size != size.astype(int)):
            raise ValueError("output_size requires integer [rows,columns]")
        if np.any((size < 8) | (size > 512)) or np.any(size % 2):
            raise ValueError("output_size sides must be even integers in [8,512] for RGGB")
        frames = _number(request.get("frames", 4), "frames", 1, 16)
        seed = _number(request.get("seed", 0), "seed", 0, 2**32 - 17)
        if frames != int(frames) or seed != int(seed):
            raise ValueError("frames and seed must be integers")
        if np.prod(size) * frames > 4194304:
            raise ValueError("Run exceeds the 4194304 frame-pixel workload limit")
        if "noise_enabled" in request and not isinstance(request["noise_enabled"], bool):
            raise ValueError("noise_enabled must be boolean")
        inputs.update(
            field=field,
            theta=theta,
            sensor_theta=sensor_theta,
            weights=weights,
            ri=ri,
            response=response,
            size=tuple(int(x) for x in size),
            frames=int(frames),
            seed=int(seed),
            noise_enabled=request.get("noise_enabled", True),
        )
        return inputs

    def estimate_resources(self, request: Mapping[str, Any]) -> dict[str, Any]:
        inputs = self._checked(request)
        pixels = int(np.prod(inputs["size"]))
        estimate = {
            "cpu_threads": 1,
            "gpu_required": False,
            "working_array_bytes_estimate": pixels * (inputs["frames"] * 8 + 256),
            "estimate_kind": "array_budget_not_measured_peak",
            "execution": "in_process",
            "frames": inputs["frames"],
            "output_size": list(inputs["size"]),
        }
        if "native_pixel_response" in inputs:
            from .spatial import _native_response_arrays

            # Native flat fields use the spatial receiver/readout operator,
            # including its bounded angular interpolation scratch buffers.
            estimate["working_array_bytes_estimate"] = (
                64 * 1024 * 1024
                + pixels * (inputs["frames"] * 160 + 128 + len(inputs["wave"]) * 64)
                + 3 * sum(a.nbytes for a in _native_response_arrays(inputs).values())
            )
            estimate["native_direction_weighted_samples"] = inputs["native_direction_work"]
        return estimate

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        inputs = self._checked(request)
        if "native_pixel_response" in inputs:
            return _execute_native_response(request, inputs)
        # Interpolate only inside the supplied angular domain, then integrate
        # power-fraction weights. There is no CRA-only or RGB-to-spectrum fallback.
        response = np.stack(
            [
                np.interp(inputs["theta"], inputs["sensor_theta"], row)
                for row in inputs["response"].reshape(-1, inputs["sensor_theta"].size)
            ]
        ).reshape(3, inputs["wave"].size, inputs["theta"].size)
        effective = (
            np.einsum("wfa,cwa->fcw", inputs["weights"], response)
            * inputs["qe"]
            * inputs["ri"].T[:, None, :]
        )
        electrons = _integrate(inputs, effective)
        dark_charge = inputs["readout"]["dark_current_e_per_s"] * inputs["exposure"]
        if np.max(electrons) + dark_charge > 1e13:
            raise RuntimeRequestError([_issue("request", "Charge exceeds bounded sampler domain")])
        rows, cols = inputs["size"]
        yy, xx = np.meshgrid(
            np.arange(rows) - (rows - 1) / 2, np.arange(cols) - (cols - 1) / 2, indexing="ij"
        )
        radius = np.hypot(xx, yy) / np.hypot((cols - 1) / 2, (rows - 1) / 2)
        channel_map = np.stack(
            [np.interp(radius, inputs["field"], electrons[:, c]) for c in range(3)], axis=-1
        )
        cfa = np.tile(np.array([[0, 1], [1, 2]]), (rows // 2, cols // 2))
        mean = np.take_along_axis(channel_map, cfa[..., None], axis=-1)[..., 0]
        raw, rgb, capture_metadata = _engine_capture(inputs, mean)
        summary = _summary(
            inputs,
            electrons,
            inputs["field"],
            self.plugin_id,
            [
                "lens_relative_illumination",
                "lens_incident_angle_distribution",
                "sensor_effective_qe",
                "sensor_angular_response",
                "mean_dark_charge",
                "readout_clipping",
            ],
        )
        if "field_reference_radius_mm" in inputs:
            summary["field_reference_radius_mm"] = inputs["field_reference_radius_mm"]
            summary["field_radius_meaning"] = (
                "physical image height / field_reference_radius_mm; representative raster "
                "covers the lens response domain, not the selected native sensor extent"
            )
        from .uncertainty import attach, propagate

        attach(summary, propagate(request, inputs, response_db=True))
        variance = raw.astype(float).var(axis=0, ddof=1) if inputs["frames"] > 1 else None
        noise_dn = float(np.sqrt(variance.mean())) if variance is not None else None
        max_clip = max(max(item["saturation"].values()) for item in capture_metadata)
        dn_per_e = capture_metadata[0]["dn_per_electron"]
        signal_dn = float(mean.mean() * dn_per_e)
        snr_eligible = noise_dn is not None and noise_dn > 0 and max_clip <= 0.001
        snr = 20 * np.log10(signal_dn / noise_dn) if snr_eligible and signal_dn > 0 else None
        summary.update(
            engine="pyisetcam.configure_e2e_sensor/resample_sensor_capture/ip_compute",
            cfa="RGGB",
            seed_sequence=[inputs["seed"] + i for i in range(inputs["frames"])],
            output_size=list(inputs["size"]),
            frames=inputs["frames"],
            spatial_sampling={
                "kind": "representative_normalized_field_raster",
                "field_radius_one": "raster_corner",
                "charge_area": "one_native_pixel_from_pixel_pitch_um",
                "contiguous_native_sensor_capture": False,
            },
            raw_temporal_noise_dn=noise_dn,
            raw_temporal_snr_db=snr,
            temporal_snr_definition=(
                "mean expected photo-signal DN / sqrt(mean per-pixel temporal variance)"
            ),
            saturation_fraction=max_clip,
            capture_metadata=capture_metadata,
            isp={
                "demosaic": "bilinear",
                "awb": "off",
                "lens_shading_correction": "off",
                "color_matrix": "identity",
                "frame_max_normalization": False,
                "rgb_meaning": "sensor RGB with sRGB transfer curve; not colorimetric sRGB",
            },
        )
        summary["limitations"].extend(
            [
                "Static radially symmetric flat field; no PSF, scene geometry, motion, or HDR.",
                "Lens tables either declare the exact sensor wavelength grid or explicitly "
                "use wavelength-independent incident-angle weights and relative illumination.",
                "Polarization, azimuth, crosstalk, PRNU and DSNU are not modeled by this adapter.",
                "Angular and radial interpolation are linear and have unquantified model error.",
                "The raster samples normalized sensor field with native-pixel charge; it is not "
                "a contiguous native-resolution capture and cannot establish spatial resolution.",
                "RGB is uncorrected sensor-channel visualization; judge shading in RAW ratios.",
                "Temporal noise uses independent samples; saturation above 0.1% withholds SNR.",
            ]
        )
        arrays = {
            "field_channel_electrons": electrons,
            "mean_electrons": mean,
            "radius_map": radius,
            "raw_frames": raw,
            "rgb": rgb,
        }
        if variance is not None:
            arrays["raw_temporal_variance_dn2"] = variance
        return {"summary": summary, "arrays": arrays}


def _parse_native_response(request, inputs, *, validated_charge_collection_support=False):
    """Validate the joint-direction contract before any image-sized work."""
    from .lens_direction_response import validate_directional_response

    lens = request["lens"]
    if "directional_response" not in lens:
        raise ValueError("Native site EQE requires joint Lens directions; theta marginals lose phi")
    directional = validate_directional_response(lens["directional_response"])
    field = _axis(lens["field_radius"], "lens.field_radius", 0, 1, 256)
    if (directional["wavelength_nm"] != inputs["wave"].tolist()
            or lens.get("wavelength_nm") != inputs["wave"].tolist()
            or directional["field_radius"] != field.tolist()
            or field[0] != 0 or field[-1] != 1):
        raise ValueError("Joint Lens and sensor wavelength/field coordinates must match exactly")
    # The v3 Lens carries its legacy theta marginal for legacy receivers. It is
    # not consumed by this operator, but contradictory/malformed axes or power
    # are still rejected rather than silently ignored.
    theta_axis = _axis(lens["theta_deg"], "lens.theta_deg", 0, 89.9, 181)
    marginal = _array(lens["angular_weights"], "lens.angular_weights", 3)
    if (marginal.shape != (len(inputs["wave"]), len(field), len(theta_axis))
            or np.any(marginal < 0)
            or not np.allclose(marginal.sum(axis=-1), 1, atol=1e-8, rtol=0)):
        raise ValueError("Lens theta marginal must retain its declared normalized shape")
    if lens.get("field_reference_radius_mm") != directional["field_reference_radius_mm"]:
        raise ValueError("Joint Lens and radial response require the same physical reference")
    ri = _array(lens["relative_illumination"], "lens.relative_illumination", 2)
    if (ri.shape != (inputs["wave"].size, field.size) or np.any(ri < 0)
            or not np.all(ri[:, 0] == 1)):
        raise ValueError("Native Lens RI must be nonnegative [wavelength,field], on-axis one")
    response = inputs["native_pixel_response"]
    directions = np.asarray(directional["directions_xyz"], dtype=float)
    power = np.asarray(directional["conditional_weights"], dtype=float)
    active = power.any(axis=0)
    theta = np.degrees(np.arccos(np.clip(directions[..., 2], -1, 1)))
    if np.any(theta[active] > response["theta_deg"][-1] + 1e-10):
        raise ValueError("OUT_OF_DOMAIN: joint Lens rays exceed native EQE theta coverage")
    size = _array(request.get("output_size", [64, 64]), "output_size", 1)
    if (size.shape != (2,) or np.any(size != size.astype(int))
            or np.any((size < 8) | (size > 512)) or np.any(size % 2)):
        raise ValueError("output_size sides must be even integers in [8,512]")
    frames = _number(request.get("frames", 4), "frames", 1, 16)
    seed = _number(request.get("seed", 0), "seed", 0, 2**32 - 17)
    if frames != int(frames) or seed != int(seed) or np.prod(size) * frames > 4194304:
        raise ValueError("Native frames/seed require bounded integers and workload")
    if "noise_enabled" in request and not isinstance(request["noise_enabled"], bool):
        raise ValueError("noise_enabled must be boolean")
    for key, minimum in (("full_sensor_shape", 2), ("crop_origin", 0)):
        value = request.get(key)
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or any(type(x) is not int or not minimum <= x <= 1_000_000 for x in value)):
            raise ValueError(f"Native Response DB requires explicit bounded integer {key}")
    full, origin = request["full_sensor_shape"], request["crop_origin"]
    if any(o + s > f for o, s, f in zip(origin, size, full, strict=True)):
        raise ValueError("Native response crop must lie inside full_sensor_shape")
    if ("charge_collection" in inputs
            and (tuple(origin) != (0, 0) or tuple(size) != tuple(full))
            and not validated_charge_collection_support):
        raise ValueError("Charge collection requires a full-sensor native image; partial crops "
                         "need validated optical and carrier halo support")
    if "charge_collection" in inputs:
        routing_work = (int(np.prod(size)) * len(inputs["wave"])
                        * len(inputs["charge_collection"]["destination_offsets_rc"]))
        if routing_work > 128_000_000:
            raise ValueError("Charge collection exceeds 128M per-scene routing samples")
        inputs["collection_routing_work"] = routing_work
    furthest = [max(abs(o - (f-1)/2), abs(o+s-1-(f-1)/2))
                for o, s, f in zip(origin, size, full, strict=True)]
    if np.hypot(*furthest) * inputs["pitch"] / 1000 > directional["field_reference_radius_mm"]:
        raise ValueError("OUT_OF_DOMAIN: native receiver exceeds Lens physical radial coverage")
    expanded = active.sum(axis=1).astype(int)
    expanded[0] *= len(directional["on_axis_rotation_phi_deg"])
    work = int(expanded.sum()) * int(np.prod(size)) * len(inputs["wave"])
    if work > 128_000_000:
        raise ValueError("Native direction/EQE integration exceeds 128M weighted samples; "
                         "use a smaller native crop or an independently qualified response grid")
    inputs.update(field=field, ri=ri, directional_response=directional,
                  field_reference_radius_mm=directional["field_reference_radius_mm"],
                  size=tuple(int(x) for x in size), frames=int(frames), seed=int(seed),
                  noise_enabled=request.get("noise_enabled", True), native_direction_work=work)
    inputs.update(full=tuple(full), origin=tuple(origin))
    return inputs


def _execute_native_response(request, inputs):
    # A flat-field response calculation can use the same native receiver operator
    # as spatial/temporal captures. The identity kernel represents an explicitly
    # uniform flat field, not an inferred optical PSF or resolution prediction.
    from .isp import default_profile
    from .spatial import SpatialSceneRuntime

    full = request.get("full_sensor_shape")
    origin = request.get("crop_origin")
    if full is None or origin is None:
        raise ValueError("Native Response DB requires explicit full_sensor_shape and crop_origin")
    spatial = {key: request[key] for key in (
        "wavelength_nm", "native_pixel_response", "pixel_pitch_um", "readout", "lens",
        "exposure_s")}
    spatial.update({key: request[key]
                    for key in ("temperature_c", "frames", "seed", "noise_enabled",
                                "charge_collection")
                    if key in request})
    source = {"kind": "estimated", "description": "Declared uniform flat field; no spatial PSF"}
    spatial.update(isp_profile=default_profile(), scene={
        "schema": "spectral_ideal_image_v1", "wavelength_nm": inputs["wave"].tolist(),
        "unit": "W/m^2/nm", "reference_plane": "sensor_ideal_image_before_psf_ri",
        "spectral_irradiance": np.broadcast_to(
            inputs["irradiance"], (*inputs["size"], len(inputs["wave"]))).tolist(),
        "sampling": {"kind": "native_sensor", "full_sensor_shape": full,
                     "crop_origin": origin, "pixel_pitch_um": inputs["pitch"]},
        "included_effects": ["on_axis_lens_throughput"], "source": source,
    }, psf={
        "schema": "intensity_psf_v1", "wavelength_nm": inputs["wave"].tolist(),
        "values": [[[1.0]] for _ in inputs["wave"]], "sample_pitch_um": inputs["pitch"],
        "normalization": "unit_sum", "centering": "array_center",
        "spatial_model": "locally_shift_invariant", "boundary": "zero_outside_supplied_image",
        "sampling_model": "pixel_integrated_optical_impulse", "source": source,
    })
    output = SpatialSceneRuntime().execute(spatial)
    summary = output["summary"]
    summary.update(plugin_id="response_db_v1", flat_field_assumption="uniform_ideal_irradiance",
                   spatial_resolution_prediction=False)
    effects = [e for e in summary["included_effects"] if e not in {"lens_psf", "pixel_aperture"}]
    summary["included_effects"] = effects
    # Keep site means and native RAW intact. The explicit R/G,B/G metric combines
    # the two observed green-site means only here, never before readout.
    means = np.asarray(summary["native_cfa_channel_mean_dn"])
    green = means[1:3].mean()
    ratios = [float(means[k] / green) if green > 0 else None for k in (0, 3)]
    summary["prediction"].update(metric_id="raw.channel_ratio", unit="1", values=ratios,
                                 axes=[{"name": "channel_ratio", "unit": "1",
                                        "values": ["R/G", "B/G"]}], included_effects=effects)
    summary["native_pixel_response"]["ratio_green_definition"] = "mean_of_Gr_and_Gb_site_means"
    return output


def _engine_capture(inputs: dict[str, Any], mean: np.ndarray) -> tuple[Any, Any, Any]:
    # Public engine entry points are intentionally imported only at execution.
    from pyisetcam.ip import ip_compute, ip_create, ip_set
    from pyisetcam.sensor import sensor_create, sensor_set
    from pyisetcam.sensor_capture import configure_e2e_sensor, resample_sensor_capture

    sensor = sensor_create("bayer-rggb", {"size": inputs["size"], "wave": inputs["wave"]})
    sensor = sensor_set(sensor, "pixel size", inputs["pitch"] * 1e-6)
    sensor = sensor_set(sensor, "integration time", inputs["exposure"])
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", 2 if inputs["noise_enabled"] else 0)
    sensor = sensor_set(sensor, "filter spectra", inputs["qe"].T)
    sensor.fields["pixel"]["prnu_sigma"] = 0.0
    sensor.fields["pixel"]["dsnu_sigma_v"] = 0.0
    sensor.fields["column_fpn"] = [0.0, 0.0]
    sensor = configure_e2e_sensor(sensor, readout=inputs["readout"])
    sensor.data["mean_electrons"] = mean
    captures = [
        resample_sensor_capture(sensor, seed=inputs["seed"] + index)
        for index in range(inputs["frames"])
    ]
    processor = ip_create(sensor=captures[0])
    processor = ip_set(processor, "conversion method sensor", "none")
    processor.fields["render"]["scale"] = False
    processed = ip_compute(processor, captures[0])
    rgb = np.asarray(processed.data["srgb"], dtype=float)
    if rgb.shape != (*mean.shape, 3) or not np.isfinite(rgb).all():
        raise RuntimeError("CameraE2E ISP produced an invalid RGB artifact")
    return (
        np.stack([capture.data["raw_dn"] for capture in captures]),
        rgb,
        [capture.metadata["sensor_capture"] for capture in captures],
    )


RUNTIMES: dict[str, RuntimePlugin] = {
    "quick_estimate_v1": QuickEstimateRuntime(),
    "response_db_v1": ResponseDBRuntime(),
}


def get_plugin(plugin_id: str) -> RuntimePlugin:
    if plugin_id == "electrical_control_v1":
        from .electrical_control_runtime import ElectricalControlRuntime

        return ElectricalControlRuntime()
    if plugin_id == "retained_charge_v1":
        from .retained_charge_runtime import RetainedChargeRuntime

        return RetainedChargeRuntime()
    if plugin_id == "reference_hdr_v1":
        from .reference_hdr_runtime import ReferenceHDRRuntime

        return ReferenceHDRRuntime()
    if plugin_id == "shared_charge_dcg_v1":
        from .dcg_runtime import SharedChargeDCGRuntime

        return SharedChargeDCGRuntime()
    if plugin_id == "hdr_bracket_v1":
        from .hdr_runtime import HDRBracketRuntime

        return HDRBracketRuntime()
    if plugin_id == "closed_loop_scene_v1":
        from .closed_loop import ClosedLoopSceneRuntime

        return ClosedLoopSceneRuntime()
    if plugin_id == "temporal_scene_v1":
        from .temporal import TemporalSceneRuntime

        return TemporalSceneRuntime()
    if plugin_id == "spatial_scene_v1":
        from .spatial import SpatialSceneRuntime

        return SpatialSceneRuntime()
    canonical = {"quick_estimate": "quick_estimate_v1", "response_db": "response_db_v1"}.get(
        plugin_id, plugin_id
    )
    if canonical not in RUNTIMES:
        raise RuntimeRequestError(
            [_issue("plugin_id", f"Unsupported runtime {plugin_id}", "UNSUPPORTED_RUNTIME")]
        )
    return RUNTIMES[canonical]


def list_plugins() -> list[dict[str, Any]]:
    from .closed_loop import ClosedLoopSceneRuntime
    from .dcg_runtime import SharedChargeDCGRuntime
    from .electrical_control_runtime import ElectricalControlRuntime
    from .hdr_runtime import HDRBracketRuntime
    from .reference_hdr_runtime import ReferenceHDRRuntime
    from .retained_charge_runtime import RetainedChargeRuntime
    from .spatial import SpatialSceneRuntime
    from .temporal import TemporalSceneRuntime

    return [plugin.describe_capabilities() for plugin in RUNTIMES.values()] + [
        SharedChargeDCGRuntime().describe_capabilities(),
        SpatialSceneRuntime().describe_capabilities(),
        TemporalSceneRuntime().describe_capabilities(),
        ClosedLoopSceneRuntime().describe_capabilities(),
        HDRBracketRuntime().describe_capabilities(),
        ReferenceHDRRuntime().describe_capabilities(),
        RetainedChargeRuntime().describe_capabilities(),
        ElectricalControlRuntime().describe_capabilities(),
    ]


def run_plugin(plugin_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
    return get_plugin(plugin_id).execute(request)
