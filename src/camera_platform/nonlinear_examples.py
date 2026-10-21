"""Explicit authored starting examples, never migration or physical calibration."""

from copy import deepcopy

from . import dcg_electrical, electrical, nonlinear_dcg, nonlinear_hdr, nonlinear_readout


def _source():
    return {"kind": "estimated", "description": (
        "Authored example: first-half slope uses the legacy scale; second-half slope is 0.6x. "
        "Legacy noise sets a numerical scale for declared conversion-output voltage noise. "
        "Curve, noise plane/covariance and condition range are assumptions, "
        "not measured calibration."
    )}


def response_curve(capacity_e, first_slope_v_per_e, analog_gain):
    gain_range = [min(.25, analog_gain), max(8., analog_gain)]
    voltage = capacity_e * first_slope_v_per_e
    return {
        "schema": "camera_charge_voltage_response_v1",
        "units": {"charge": "electron", "voltage": "V"},
        "charge_e": [0., capacity_e / 2., capacity_e],
        "signal_voltage_v": [0., voltage / 2., voltage * .8],
        "valid_domain": {"temperature_c": [25., 25.], "analog_gain": gain_range},
        "signed_inverse": {"minimum_voltage_v": -voltage, "method": "origin_tangent"},
        "source": _source(),
    }


def single_model(legacy_readout):
    legacy = electrical.default_electrical_model(legacy_readout)
    curve = response_curve(legacy["full_well_e"], legacy["conversion_gain_v_per_e"],
                           legacy_readout["gain"])
    common = {k: deepcopy(v) for k, v in legacy.items() if k in nonlinear_readout.MODEL_KEYS}
    return nonlinear_readout.validate_model({
        **common, "schema": nonlinear_readout.SCHEMA, "charge_voltage_response": curve,
        "read_noise": {
            "schema": nonlinear_readout.NOISE_SCHEMA, "plane": nonlinear_readout.NOISE_PLANE,
            "includes": nonlinear_readout.NOISE_INCLUDES,
            "variance_v2": (legacy["read_noise_e"] * legacy["conversion_gain_v_per_e"])**2,
            "temperature_range_c": [25., 25.],
            "analog_gain_range": curve["valid_domain"]["analog_gain"].copy(),
            "source": _source(),
        }, "source": _source(),
    })


def shared_model(legacy_readout):
    legacy = dcg_electrical.default_model(legacy_readout)
    result = deepcopy(legacy)
    result["schema"] = nonlinear_dcg.SCHEMA
    slopes = []
    for path in result["paths"]:
        slope = path.pop("conversion_gain_v_per_e")
        slopes.append(slope)
        path["charge_voltage_response"] = response_curve(
            min(result["shared_charge_capacity_e"], path["input_linear_ceiling_e"]),
            slope, path["analog_gain"])
    covariance = [[legacy["read_noise"]["covariance_e2"][i][j] * slopes[i] * slopes[j]
                   for j in range(2)] for i in range(2)]
    result["read_noise"] = {
        "schema": nonlinear_dcg.NOISE_SCHEMA, "stream_ids": ["HCG", "LCG"],
        "plane": nonlinear_readout.NOISE_PLANE, "includes": nonlinear_readout.NOISE_INCLUDES,
        "covariance_v2": covariance, "temperature_range_c": [25., 25.], "source": _source(),
    }
    result["source"] = _source()
    return nonlinear_dcg.validate_model(result)


def capture_program(legacy_readout, mode):
    from . import dcg_runtime, hdr_runtime, reference_hdr, reference_hdr_runtime

    preview = dict(hdr_runtime.NONLINEAR_PREVIEW)
    if mode == "shared_charge_nonlinear":
        return dcg_runtime.validate_capture_program({
            "schema": "camera_dcg_capture_program_v2",
            "electrical_model": shared_model(legacy_readout),
            "merge_profile": nonlinear_dcg.default_profile(), "preview": preview,
        })
    if mode not in {"independent_bracket_nonlinear", "reference_guarded_nonlinear"}:
        raise ValueError("Unknown authored nonlinear example acquisition mode")
    reference = mode == "reference_guarded_nonlinear"
    module = reference_hdr_runtime if reference else hdr_runtime
    return module.validate_capture_program({
        "schema": ("camera_reference_hdr_capture_program_v2" if reference
                   else "camera_capture_program_v2"),
        "electrical_model": single_model(legacy_readout), "waveform": None,
        "hdr_merge": (reference_hdr.default_profile_v2() if reference
                      else nonlinear_hdr.default_profile()), "preview": preview,
    })
