"""Explicit PSF/native-pixel response to independent-reset electrical RAW/ISP.

The supplied ideal-plane scene already contains on-axis throughput ONCE.
No F-number scaling, model registration, auto-exposure or ISP is implicit.
PSF and joint ray/EQE integration reuse the spatial engine; collected rate is
integrated once and passed to the electrical engine. No charge/trap history.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy

import numpy as np

from . import electrical, isp, spatial
from .registry import digest


def evaluate_response(
    rate_request: dict, electrical_model: dict, *, exposure_s: float,
    analog_gain: float, temperature_c: float = 25.0,
    noise_enabled: bool = False, seed: int = 713,
    isp_profile: dict | None = None,
) -> dict:
    """Return pre-readout photocharge, integer RAW and optional basic ISP stages.

    rate_request follows spatial.photoelectron_rate, without exposure/frames.
    Each invocation is an independent-reset exposure, not a temporal capture.
    Readout gain never multiplies the optical input or collected photocharge.
    An ISP profile processes only the acquired integer RAW; it cannot alter
    photocharge or recapture the scene.
    """
    if (type(exposure_s) not in (int, float) or not np.isfinite(exposure_s)
            or not 0 < exposure_s <= 3600):
        raise ValueError("exposure_s must be positive finite seconds")
    if (type(analog_gain) not in (int, float) or not np.isfinite(analog_gain)
            or not 1e-6 <= analog_gain <= 1e6):
        raise ValueError("analog_gain must be a finite positive readout gain")
    if type(noise_enabled) is not bool or type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("Require boolean noise mode and integer seed")
    model = electrical.validate_electrical_model(electrical_model)
    # Reject an incompatible readout before decoding a potentially large PSF
    # and running the optical integration. The electrical stage checks the
    # same conditions again against the actual incident charge.
    electrical.validate_capture_conditions(model, exposure_s, analog_gain, temperature_c)
    selected_isp = isp.validate_profile(isp_profile) if isp_profile is not None else None
    request = deepcopy(rate_request)
    optical = spatial.photoelectron_rate(request)
    upstream = optical["arrays"]
    geometry = optical["summary"]["geometry"]
    photo = upstream["photoelectron_rate"] * exposure_s
    readout = electrical.capture_electrons(
        photo, exposure_s, model, analog_gain=analog_gain,
        temperature_c=temperature_c,
        geometry={"full_sensor_shape": geometry["full_sensor_shape"],
                  "crop_origin": geometry["crop_origin"]},
        noise_enabled=noise_enabled, seed=seed,
    )
    image = np.asarray(request["scene"]["spectral_irradiance"], dtype="<f8")
    arrays = {
        **upstream, "incident_spectral_irradiance": image.copy(),
        "mean_photocharge_e": photo,
        "channel_mean_photocharge_e": upstream["channel_photoelectron_rate"] * exposure_s,
        **readout["arrays"],
    }
    processed = None
    if selected_isp is not None:
        adc = model["adc"]
        raw_format = {
            "cfa": "RGGB", "bit_depth": adc["bit_depth"],
            "black_level_dn": adc["black_level_dn"], "response_type": "linear",
            "packing": "unpacked", "layout": "bayer", "hdr_streams": 1,
        }
        processed = isp.process_raw(
            arrays["raw_dn"], raw_format, selected_isp,
            geometry={"kind": "native_sensor", **geometry},
        )
        arrays.update(processed["arrays"])
    sites = upstream.get("native_site_index", upstream["cfa_channel_index"])
    names = ["R", "Gr", "Gb", "B"] if "native_pixel_response" in request else ["R", "G", "B"]
    black = model["adc"]["black_level_dn"]
    metrics = []
    for index, name in enumerate(names):
        mask = sites == index
        if mask.any():
            metrics.append({
                "site": name, "pixel_count": int(mask.sum()),
                "mean_photocharge_e": float(photo[mask].mean()),
                "mean_black_relative_raw_dn": float((arrays["raw_dn"][mask] - black).mean()),
                "well_saturated_fraction": float(arrays["well_saturated_mask"][mask].mean()),
                "adc_high_fraction": float(arrays["adc_high_mask"][mask].mean()),
            })
    units = {**optical["summary"]["array_units"], **readout["summary"]["array_units"],
             "incident_spectral_irradiance": "W/m^2/nm", "mean_photocharge_e": "electron/pixel",
             "channel_mean_photocharge_e": "electron/pixel"}
    if processed is not None:
        units.update(processed["summary"]["array_units"])
    source_ri = request.get("field_radiometry_mode") == spatial.SOURCE_RI_MODE
    response_identity = {
        "psf": digest(request["psf"]), "lens": digest(request["lens"]),
        "pixel_response": digest(request.get("native_pixel_response", {
            "qe": request.get("qe"), "angular": request.get("sensor_angular_response")})),
        "electrical_model": digest(model),
        "scene_array_sha256": hashlib.sha256(image.tobytes(order="C")).hexdigest(),
        "scene_array_encoding": "little_endian_float64_C", "scene_shape": list(image.shape),
        "scene_sampling": digest(request["scene"]["sampling"]),
        "scene_source": digest(request["scene"]["source"]),
        "wavelength_nm": digest(request["wavelength_nm"]),
    }
    if source_ri:
        response_identity["field_radiometry_mode"] = spatial.SOURCE_RI_MODE
    if "charge_collection" in request:
        # Generation EQE and source-to-well transport are independently
        # replaceable pixel components. A routing change can alter integer RAW
        # without changing the generation response or any optical component.
        response_identity["charge_collection"] = digest(request["charge_collection"])
    if "source_subcell_irradiance" in request["scene"]:
        subcells = np.asarray(request["scene"]["source_subcell_irradiance"], dtype="<f8")
        response_identity["source_subcell_array_sha256"] = hashlib.sha256(
            subcells.tobytes(order="C")
        ).hexdigest()
        response_identity["source_subcell_array_encoding"] = "little_endian_float64_C"
        response_identity["source_subcell_shape"] = list(subcells.shape)
        response_identity["source_subcell_evidence"] = digest(
            request["scene"]["source_subcell_evidence"]
        )
    optical_order = (
        ["supplied_ideal_irradiance_with_throughput", "source_relative_illumination",
         "supplied_pixel_integrated_psf_once", "receiver_direction_weighted_EQE"]
        if source_ri else
        ["supplied_ideal_irradiance_with_throughput", "supplied_pixel_integrated_psf_once",
         "relative_illumination", "joint_direction_weighted_EQE"]
    )
    return {"arrays": arrays, "summary": {
        "method_and_version": ("explicit_response_chain_v2" if processed is not None
                               else "explicit_response_chain_v1"),
        "source_kind": "simulated", "physical_validation": "not_established",
        "exposure_s": exposure_s, "analog_gain": analog_gain,
        "noise_enabled": noise_enabled, "seed": seed, "geometry": geometry,
        "site_metrics": metrics, "array_units": units,
        "response_identity": response_identity,
        "optical": optical["summary"], "electrical": readout["summary"],
        "processing_order": [*optical_order, "wavelength_and_pixel_area",
            "exposure_once", "independent_reset_electrical_readout",
            *(["basic_software_isp_from_acquired_integer_RAW"] if processed is not None else [])],
        **({"isp": processed["summary"]} if processed is not None else {}),
        "calibration": {k: v for k, v in readout["calibration"].items()
                        if not isinstance(v, np.ndarray)},
        "limits": ["No registered-model applicability or physical accuracy is granted.",
                   "PSF/directions use the spatial engine's separable approximation.",
                   "Gain cannot undo upstream full-well saturation.",
                   ("No temporal trap, auto-exposure or HDR is applied."
                    if processed is not None else
                    "No temporal trap, auto-exposure, HDR or ISP is applied.")],
    }}
