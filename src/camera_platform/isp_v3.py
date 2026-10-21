"""Bounded advanced ISP over calibrated signed photocharge from retained RAW.

Source readout variance is a local tuning estimate, not output covariance.
This graph never invents linear DN or resamples acquisition noise.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import numpy as np

from . import electrical, isp, isp_calibration, isp_v2, nonlinear_isp, nonlinear_readout

SCHEMA = "camera_isp_profile_v3"
IMPLEMENTATION = "camera_platform_float_isp_v3"
INPUT_SCHEMA = "camera_calibrated_isp_input_v1"
CALIBRATION_SCHEMA = "camera_charge_isp_calibration_numeric_v1"
GRAPH = [
    "charge_decode",
    "spatial_lsc",
    "raw_denoise",
    "demosaic",
    "white_balance",
    "ccm",
    "linear_sharpen",
    "tone",
]
SITES = isp_v2.SITES.copy()
MAX_OUTPUT_ARRAY_BYTES = 128 * 1024 * 1024
MAX_WORKING_ARRAY_BYTES = 1024 * 1024 * 1024


def default_profile(base=None, calibration_id=None):
    """Start with v1 fixed color settings and every new filter bypassed."""
    common = isp.validate_profile(isp.default_profile() if base is None else base)
    result = {
        key: copy.deepcopy(common[key])
        for key in ("precision", "demosaic", "white_balance", "ccm", "tone")
    }
    result.update(
        schema=SCHEMA,
        graph=GRAPH.copy(),
        calibration_id=calibration_id,
        disabled_effects=["auto_white_balance", "hdr_merge", "temporal_denoise"],
        charge_decode={
            "schema": "camera_nonlinear_raw_decode_v1",
            "method": "nonlinear_raw_interval_decode_v1",
            "guard_sigma": 3.0,
            "saturation_margin_dn": 1.0,
        },
        spatial_lsc={"mode": "bypass"},
        raw_denoise={"mode": "bypass", "strength": 0.5, "range_floor_e2": 1.0},
        linear_sharpen={"mode": "bypass", "amount": 0.25, "threshold": 0.01, "max_delta": 0.1},
    )
    return result


def _common_profile(profile):
    common = isp.default_profile()
    for key in ("precision", "demosaic", "white_balance", "ccm", "tone"):
        common[key] = copy.deepcopy(profile[key])
    return isp.validate_profile(common)


def validate_profile(profile):
    expected = default_profile()
    isp._exact(profile, set(expected), "profile")
    for key in ("schema", "graph", "precision", "disabled_effects"):
        if profile[key] != expected[key]:
            isp._refuse("Unsupported or reordered " + key, "profile." + key)
    result = copy.deepcopy(expected)
    common = _common_profile(profile)
    for key in ("demosaic", "white_balance", "ccm", "tone"):
        result[key] = common[key]
    decode = isp._exact(
        profile["charge_decode"], set(expected["charge_decode"]), "profile.charge_decode"
    )
    for key in ("schema", "method"):
        if decode[key] != expected["charge_decode"][key]:
            isp._refuse("Unsupported charge decoder", "profile.charge_decode." + key)
    for key in ("guard_sigma", "saturation_margin_dn"):
        result["charge_decode"][key] = isp._number(
            decode[key], 0, 1e6, "profile.charge_decode." + key
        )
    identity = profile["calibration_id"]
    if identity is not None and (
        not isinstance(identity, str)
        or not identity.strip()
        or identity != identity.strip()
        or len(identity) > 256
    ):
        isp._refuse("Calibration ID must be null or a nonblank immutable ID", "calibration_id")
    result["calibration_id"] = identity
    for stage, mode, bounds in (
        ("spatial_lsc", "native_four_site_grid_v1", {}),
        (
            "raw_denoise",
            "local_charge_variance_filter_v1",
            {"strength": (0, 1), "range_floor_e2": (0, 1e26)},
        ),
        (
            "linear_sharpen",
            "bounded_detail_v1",
            {"amount": (0, 1), "threshold": (0, 16), "max_delta": (0, 1)},
        ),
    ):
        config = isp._exact(profile[stage], {"mode", *bounds}, "profile." + stage)
        if config["mode"] not in ("bypass", mode):
            isp._refuse("Unsupported stage mode", "profile." + stage)
        result[stage] = {
            "mode": config["mode"],
            **{
                key: isp._number(config[key], *limit, "profile." + stage + "." + key)
                for key, limit in bounds.items()
            },
        }
    if result["raw_denoise"]["range_floor_e2"] <= 0:
        isp._refuse("Range denominator floor must be positive electrons squared", "raw_denoise")
    if identity is None and result["spatial_lsc"]["mode"] != "bypass":
        isp._refuse("LSC requires an immutable charge calibration ID", "calibration_id")
    return result


def validate_numeric_calibration(calibration):
    """Validate only the charge-basis grid; provenance remains caller verified."""
    isp._exact(calibration, {"schema", "sites", "lsc"}, "calibration")
    if calibration["schema"] != CALIBRATION_SCHEMA or calibration["sites"] != SITES:
        isp._refuse("Charge LSC requires its versioned four-site calibration", "calibration")
    # Reuse the bounded interpolation shape, never the legacy DN noise contract.
    parsed = isp_calibration.validate_numeric_payload(
        {
            "schema": isp_calibration.SCHEMA,
            "sites": SITES,
            "lsc": calibration["lsc"],
            "noise": None,
        }
    )
    return {"schema": CALIBRATION_SCHEMA, "sites": SITES.copy(), "lsc": parsed["lsc"]}


_MOSAIC_FLOATS = [
    "black_subtracted_normalized_frames",
    "lsc_normalized_frames",
    "denoised_normalized_frames",
    "sharpen_delta_frames",
    "decoded_total_charge_e_frames",
    "calibrated_photocharge_e_frames",
    "decoded_charge_low_e_frames",
    "decoded_charge_high_e_frames",
    "decoded_inverse_slope_e_per_v_frames",
    "decoded_read_variance_e2_frames",
    "decoded_shot_variance_estimate_e2_frames",
    "decoded_stochastic_variance_estimate_e2_frames",
    "decoded_quantization_bound_e_frames",
    "photocharge_low_normalized_frames",
    "photocharge_high_normalized_frames",
    "quantization_bound_normalized_frames",
    "noise_variance_hint_e2_frames",
    "noise_variance_hint_normalized_frames",
    "lsc_quantization_bound_normalized_frames",
]
_RGB_FLOATS = [
    "demosaiced_rgb_frames",
    "white_balanced_rgb_frames",
    "ccm_rgb_frames",
    "sharpened_rgb_frames",
    "rgb_frames",
    "baseline_rgb_frames",
    "baseline_ccm_rgb_frames",
]
_MASKS = [
    "raw_valid_mask_frames",
    "raw_code_valid_mask_frames",
    "lsc_valid_mask_frames",
    "noise_hint_valid_mask_frames",
    "denoise_valid_mask_frames",
    "demosaic_valid_mask_frames",
    "linear_valid_mask_frames",
    "sharpen_stencil_valid_mask_frames",
    "sharpen_applied_mask_frames",
    "sharpen_valid_mask_frames",
    "rgb_valid_mask_frames",
    "baseline_rgb_valid_mask_frames",
    "quantization_knot_crossing_mask_frames",
    "lower_censored_mask_frames",
    "upper_censored_mask_frames",
    "impossible_code_mask_frames",
    "inverse_outside_domain_mask_frames",
]


def output_array_descriptors(shape):
    """Exact names/shapes/dtypes of emitted arrays, for storage preflight."""
    count, rows, cols = shape
    descriptors = []
    for names, dimensions, dtype in (
        (_MOSAIC_FLOATS, [count, rows, cols], "<f8"),
        (_RGB_FLOATS, [count, rows, cols, 3], "<f8"),
        (_MASKS, [count, rows, cols], "|b1"),
        (["rgb", "baseline_rgb"], [rows, cols, 3], "<f8"),
        (["lsc_gain_map"], [rows, cols], "<f8"),
        (["lsc_calibration_valid_mask"], [rows, cols], "|b1"),
        (["native_site_index"], [rows, cols], "<i8"),
        (["native_site_index_tile"], [2, 2], "<i8"),
    ):
        for name in names:
            descriptors.append({"name": name, "shape": dimensions.copy(), "dtype": dtype})
    return descriptors


def validate_inputs(raw, calibrations, profile, *, geometry, normalization_e, calibration=None):
    """Preflight before decoder allocation; bind each frame to its calibration."""
    selected = validate_profile(profile)
    if (
        type(raw) is not np.ndarray
        or raw.dtype.kind not in "iu"
        or raw.dtype.itemsize > 4
        or raw.ndim != 3
    ):
        isp._refuse("ISP v3 requires integer RAW [F,H,W], at most 32 bits", "raw")
    count, rows, cols = raw.shape
    if not (
        1 <= count <= isp.MAX_FRAMES
        and 2 <= rows <= isp.MAX_SIDE
        and 2 <= cols <= isp.MAX_SIDE
        and rows % 2 == 0
        and cols % 2 == 0
        and rows * cols <= electrical.MAX_PIXELS
        and raw.size <= isp.MAX_FRAME_PIXELS
    ):
        isp._refuse(
            "RAW exceeds bounded even native frame dimensions", "raw.shape", "RESOURCE_LIMIT"
        )
    norm = isp._number(normalization_e, 0, electrical.MAX_CHARGE_E, "normalization_e")
    if norm <= 0:
        isp._refuse("Fixed electron normalization must be positive", "normalization_e")
    floor = selected["raw_denoise"]["range_floor_e2"] / norm / norm
    if not np.isfinite(floor) or floor <= 0:
        isp._refuse("Normalized range floor exceeds the numerical domain", "raw_denoise")
    descriptors = output_array_descriptors(raw.shape)
    output_bytes = [int(np.prod(d["shape"])) * np.dtype(d["dtype"]).itemsize for d in descriptors]
    # Retained stages + decoder workspace + source dark copies + filters + baseline.
    working = int(raw.size * 896 + rows * cols * 96)
    largest = max(output_bytes)
    if working > MAX_WORKING_ARRAY_BYTES or largest > MAX_OUTPUT_ARRAY_BYTES:
        isp._refuse("ISP v3 exceeds working/output array bounds", "raw.shape", "RESOURCE_LIMIT")
    if type(calibrations) is not list or len(calibrations) != count:
        isp._refuse("Exactly one bound nonlinear calibration per frame is required", "calibrations")
    parsed = []
    geom = pattern = None
    for index, item in enumerate(calibrations):
        cal, geom, pattern = nonlinear_isp._input(raw[index], item, geometry)
        white = 2 ** cal["model"]["adc"]["bit_depth"] - 1
        if np.any(raw[index] < 0) or np.any(raw[index] > white):
            isp._refuse("RAW frame lies outside its calibrated ADC code domain", "raw")
        parsed.append(cal)
    numeric = None
    if selected["calibration_id"] is not None:
        if calibration is None:
            isp._refuse("The selected charge calibration payload is missing", "calibration")
        numeric = validate_numeric_calibration(calibration)
    elif calibration is not None:
        isp._refuse("Unbound charge calibration payload is unsupported", "calibration")
    if selected["spatial_lsc"]["mode"] != "bypass":
        if numeric["lsc"] is None:
            isp._refuse("Active LSC requires its charge calibration grid", "calibration.lsc")
        for axis, nodes in enumerate((numeric["lsc"]["row_nodes"], numeric["lsc"]["column_nodes"])):
            origin, full, size = (
                geom["crop_origin"][axis],
                geom["full_sensor_shape"][axis],
                (rows, cols)[axis],
            )
            if nodes[-1] > full - 1 or nodes[0] > origin or nodes[-1] < origin + size - 1:
                isp._refuse(
                    "LSC must cover native pixel centers without extrapolation",
                    "calibration.lsc",
                    "OUT_OF_DOMAIN",
                )
    calibration_bytes = 0 if numeric is None else len(str(numeric).encode()) * 4
    working += calibration_bytes
    if working > MAX_WORKING_ARRAY_BYTES:
        isp._refuse("ISP v3 calibration exceeds working bounds", "calibration", "RESOURCE_LIMIT")
    return {
        "frames": raw,
        "calibrations": parsed,
        "profile": selected,
        "geometry": geom,
        "pattern": pattern,
        "normalization_e": norm,
        "calibration": numeric,
        "resources": {
            "working_array_bytes_estimate": working,
            "largest_output_array_bytes": largest,
            "output_array_payload_bytes": sum(output_bytes),
            "output_array_descriptors": descriptors,
            "estimate_kind": "conservative_array_budget_not_measured_peak",
            "automatic_coarsening": False,
        },
    }


def _masked(value, valid):
    result = value.copy()
    result[~valid] = np.nan
    if not np.isfinite(result[valid]).all():
        isp._refuse("Nonfinite valid calibrated ISP intermediate", "result", "NUMERICAL_FAILURE")
    return result


def _color(mosaic, valid, pattern, site, selected):
    from pyisetcam.ip import _ie_bilinear

    result = np.empty((*mosaic.shape, 3), dtype=np.float64)
    rgb_index = np.take([0, 1, 1, 2], site)
    for frame in range(len(mosaic)):
        planes = np.zeros((*mosaic.shape[-2:], 3))
        for channel in range(3):
            samples = (rgb_index == channel) & valid[frame]
            planes[..., channel][samples] = mosaic[frame][samples]
        result[frame] = _ie_bilinear(planes, pattern)
    rgb_valid = isp_v2._stencil_valid(valid)
    demosaiced = _masked(result, rgb_valid)
    balanced = _masked(demosaiced * np.asarray(selected["white_balance"]["gains_rgb"]), rgb_valid)
    corrected = _masked(balanced @ np.asarray(selected["ccm"]["matrix_rgb"]).T, rgb_valid)
    return demosaiced, balanced, corrected, rgb_valid


def _display(value, valid, selected):
    from pyisetcam.utils import linear_to_srgb

    clipped = np.clip(np.where(valid[..., None], value, 0.0), 0.0, 1.0)
    return linear_to_srgb(clipped) if selected["tone"]["curve"] == "srgb" else clipped


def process_raw(raw, calibrations, profile, *, geometry, normalization_e, calibration=None):
    """Replay immutable nonlinear RAW; physical NaNs are separate from display black."""
    parsed = validate_inputs(
        raw,
        calibrations,
        profile,
        geometry=geometry,
        normalization_e=normalization_e,
        calibration=calibration,
    )
    selected, geom, norm = parsed["profile"], parsed["geometry"], parsed["normalization_e"]
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    options = {
        key: selected["charge_decode"][key] for key in ("guard_sigma", "saturation_margin_dn")
    }
    decoded = [
        nonlinear_readout.decode_raw(frame, cal, **options)
        for frame, cal in zip(raw, parsed["calibrations"], strict=True)
    ]

    def stack(key):
        return np.stack([item["arrays"][key] for item in decoded])

    valid = stack("valid_mask")
    total, read_var, shot_var = (
        stack(key) for key in ("total_charge_e", "read_variance_e2", "shot_variance_estimate_e2")
    )
    variance, bound = stack("stochastic_variance_estimate_e2"), stack("quantization_bound_e")
    low, high = stack("charge_low_e"), stack("charge_high_e")
    dark = np.stack([cal["dark_e"] for cal in parsed["calibrations"]])
    photo = _masked(total - dark, valid)
    normalized = _masked(photo / norm, valid)
    low_norm, high_norm = _masked((low - dark) / norm, valid), _masked((high - dark) / norm, valid)
    bound_norm = _masked(bound / norm, valid)
    decode_summaries = [item["summary"] for item in decoded]
    inverse_slope = stack("inverse_slope_e_per_v")
    decoder_masks = {
        key + "_frames": stack(key)
        for key in (
            "quantization_knot_crossing_mask",
            "lower_censored_mask",
            "upper_censored_mask",
            "impossible_code_mask",
            "inverse_outside_domain_mask",
        )
    }
    decoded.clear()
    del dark
    site, tile = isp_v2._site_map(raw.shape[-2:], geom["crop_origin"])
    site, tile = site.astype("<i8"), tile.astype("<i8")
    lsc_applied = selected["spatial_lsc"]["mode"] != "bypass"
    gain, grid_valid = (
        isp_v2._lsc_map(parsed["calibration"]["lsc"], geom, site)
        if lsc_applied
        else (np.ones(raw.shape[-2:]), np.ones(raw.shape[-2:], bool))
    )
    lsc_valid = valid & grid_valid
    lsc = _masked(normalized * gain, lsc_valid)
    hint_e2 = _masked(variance * np.square(gain), lsc_valid)
    hint = _masked(hint_e2 / norm / norm, lsc_valid)
    lsc_bound = _masked(bound_norm * gain, lsc_valid)
    denoise = selected["raw_denoise"]
    denoise_applied = denoise["mode"] != "bypass" and denoise["strength"] > 0
    # Mask BEFORE arithmetic: zero times NaN otherwise poisons denominator and
    # silently bypasses filtering even at valid centers next to missing samples.
    denoised = (
        _masked(
            isp_v2._denoise(
                np.where(lsc_valid, lsc, 0.0),
                np.where(lsc_valid, hint, 0.0),
                lsc_valid,
                denoise["strength"],
                denoise["range_floor_e2"] / norm / norm,
            ),
            lsc_valid,
        )
        if denoise_applied
        else lsc.copy()
    )
    demosaiced, balanced, corrected, rgb_valid = _color(
        denoised, lsc_valid, parsed["pattern"], site, selected
    )
    sharpen = selected["linear_sharpen"]
    sharpen_applied = (
        sharpen["mode"] != "bypass" and sharpen["amount"] > 0 and sharpen["max_delta"] > 0
    )
    stencil_valid = isp_v2._stencil_valid(rgb_valid)
    applied_mask = stencil_valid if sharpen_applied else np.zeros(raw.shape, bool)
    delta = np.zeros(raw.shape)
    if sharpen_applied:
        guide = np.where(rgb_valid[..., None], corrected, 0.0).mean(axis=-1)
        blur = sum(value * weight / 16 for value, weight in isp_v2._neighbors(guide))
        detail = guide - blur
        delta = np.where(
            applied_mask,
            np.sign(detail)
            * np.minimum(
                sharpen["max_delta"],
                sharpen["amount"] * np.maximum(np.abs(detail) - sharpen["threshold"], 0),
            ),
            0.0,
        )
    sharpened = _masked(corrected + delta[..., None], rgb_valid)
    output = _display(sharpened, rgb_valid, selected)
    # The same decoder, fixed electron normalization and color settings define
    # the baseline. In particular, a DCG path ceiling is not its normalizer.
    _, _, baseline_ccm, baseline_valid = _color(
        normalized, valid, parsed["pattern"], site, selected
    )
    baseline = _display(baseline_ccm, baseline_valid, selected)
    arrays = {
        "black_subtracted_normalized_frames": normalized,
        "lsc_normalized_frames": lsc,
        "denoised_normalized_frames": denoised,
        "demosaiced_rgb_frames": demosaiced,
        "white_balanced_rgb_frames": balanced,
        "ccm_rgb_frames": corrected,
        "sharpened_rgb_frames": sharpened,
        "sharpen_delta_frames": _masked(delta, rgb_valid),
        "rgb_frames": output,
        "rgb": output[0].copy(),
        "baseline_rgb_frames": baseline,
        "baseline_rgb": baseline[0].copy(),
        "baseline_ccm_rgb_frames": baseline_ccm,
        "lsc_gain_map": gain,
        "native_site_index": site,
        "native_site_index_tile": tile,
        "decoded_total_charge_e_frames": total,
        "calibrated_photocharge_e_frames": photo,
        "decoded_charge_low_e_frames": low,
        "decoded_charge_high_e_frames": high,
        "decoded_inverse_slope_e_per_v_frames": inverse_slope,
        "decoded_read_variance_e2_frames": read_var,
        "decoded_shot_variance_estimate_e2_frames": shot_var,
        "decoded_stochastic_variance_estimate_e2_frames": variance,
        "decoded_quantization_bound_e_frames": bound,
        "photocharge_low_normalized_frames": low_norm,
        "photocharge_high_normalized_frames": high_norm,
        "quantization_bound_normalized_frames": bound_norm,
        "noise_variance_hint_e2_frames": hint_e2,
        "noise_variance_hint_normalized_frames": hint,
        "lsc_quantization_bound_normalized_frames": lsc_bound,
        "raw_valid_mask_frames": valid,
        "raw_code_valid_mask_frames": valid.copy(),
        "lsc_calibration_valid_mask": grid_valid,
        "lsc_valid_mask_frames": lsc_valid,
        "noise_hint_valid_mask_frames": lsc_valid.copy(),
        "denoise_valid_mask_frames": lsc_valid.copy(),
        "demosaic_valid_mask_frames": rgb_valid,
        "linear_valid_mask_frames": rgb_valid.copy(),
        "sharpen_stencil_valid_mask_frames": stencil_valid,
        "sharpen_applied_mask_frames": applied_mask.copy(),
        "sharpen_valid_mask_frames": rgb_valid.copy(),
        "rgb_valid_mask_frames": rgb_valid.copy(),
        "baseline_rgb_valid_mask_frames": baseline_valid,
        **decoder_masks,
    }
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        isp._refuse("RAW input changed during processing", "raw", "INTEGRITY_FAILURE")
    descriptors = parsed["resources"]["output_array_descriptors"]
    if set(arrays) != {d["name"] for d in descriptors} or any(
        list(arrays[d["name"]].shape) != d["shape"]
        or arrays[d["name"]].dtype.str != d["dtype"]
        or arrays[d["name"]].nbytes > MAX_OUTPUT_ARRAY_BYTES
        for d in descriptors
    ):
        isp._refuse("ISP v3 output differs from its resource forecast", "result", "RESOURCE_LIMIT")
    units = {key: "boolean" if value.dtype.kind == "b" else "1" for key, value in arrays.items()}
    for key in arrays:
        if "_e2_" in key:
            units[key] = "electron^2"
        elif "_e_per_v_" in key:
            units[key] = "electron/V"
        elif "_e_frames" in key:
            units[key] = "electron"
    units.update(
        native_site_index="index",
        native_site_index_tile="index",
        noise_variance_hint_normalized_frames="1^2",
    )
    input_summary = {
        "schema": INPUT_SCHEMA,
        "normalization_e": norm,
        "signal": "(decoded total charge - calibrated expected dark)/normalization_e",
        "normalization_source": "explicit fixed scalar; never frame maximum or per-path ceiling",
        "variance": "decoder local inverse-slope read plus total-charge plug-in shot estimate",
        "dark_noise": "expected dark subtraction retains dark shot variance",
        "quantization": "decoder charge preimage bounds with numerical allowance; "
        "no quantization variance",
        "geometry": geom,
        "frames": len(raw),
        "input_array_bytes_sha256": before,
        "frame_calibrations": [
            {"frame_index": index, **item} for index, item in enumerate(decode_summaries)
        ],
        "array_units": {
            key: units[key]
            for key in arrays
            if key.startswith(("decoded_", "calibrated_", "photocharge_", "quantization_"))
        },
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
    }
    stage_data = [
        ("black_subtracted_normalized_frames", "raw_valid_mask_frames", True),
        ("lsc_normalized_frames", "lsc_valid_mask_frames", lsc_applied),
        ("denoised_normalized_frames", "denoise_valid_mask_frames", denoise_applied),
        ("demosaiced_rgb_frames", "demosaic_valid_mask_frames", True),
        ("white_balanced_rgb_frames", "demosaic_valid_mask_frames", True),
        ("ccm_rgb_frames", "demosaic_valid_mask_frames", True),
        ("sharpened_rgb_frames", "sharpen_valid_mask_frames", sharpen_applied),
        ("rgb_frames", "rgb_valid_mask_frames", True),
    ]
    backend = nonlinear_isp._implementation()
    backend["id"] = IMPLEMENTATION
    for name in ("isp_v3.py", "isp_v2.py", "isp_calibration.py"):
        backend["source_sha256"]["camera_platform/" + name] = hashlib.sha256(
            Path(__file__).with_name(name).read_bytes()
        ).hexdigest()
    summary = {
        "schema_version": "camera_isp_replay_result_v3",
        "implementation": backend,
        "profile": selected,
        "profile_sha256": isp._digest(selected),
        "executed_graph": GRAPH.copy(),
        "calibrated_input": input_summary,
        "normalization_e": norm,
        "geometry": geom,
        "frames": len(raw),
        "output_size": list(raw.shape[-2:]),
        "input_shape": list(raw.shape),
        "input_dtype": raw.dtype.str,
        "input_array_bytes_sha256": before,
        "input_hash_scope": "C-order selected RAW frame bytes; "
        "original container/topology retained by caller",
        "site_names": SITES.copy(),
        "native_site_index_tile": tile.tolist(),
        "local_cfa_rgb_indices": (parsed["pattern"] - 1).tolist(),
        "array_units": units,
        "resources": parsed["resources"],
        "stage_outputs": {
            stage: {
                "array": array,
                "validity": mask,
                "unit": "1",
                "applied": applied,
                "version": "1",
                "configuration_sha256": isp._digest(selected[stage]),
            }
            for stage, (array, mask, applied) in zip(GRAPH, stage_data, strict=True)
        },
        "calibration_id": selected["calibration_id"],
        "calibration_numeric_sha256": None
        if parsed["calibration"] is None
        else isp._digest(parsed["calibration"]),
        "baseline": {
            "array": "baseline_rgb_frames",
            "linear_array": "baseline_ccm_rgb_frames",
            "validity": "baseline_rgb_valid_mask_frames",
            "same_original_raw": True,
            "same_decode_and_normalization": True,
            "same_wb_ccm_tone": True,
            "new_stages_applied": False,
        },
        "normalization": input_summary["signal"],
        "frame_max_normalization": False,
        "linear_negative_policy": "preserved until tone",
        "tone_input_clipping": [0.0, 1.0],
        "invalid_policy": "NaN physical outputs, finite masked work buffers, black display pixels",
        "boundary": "reflect local crop; same-site planes independently",
        "crop_full_frame_comparison_halo_native_pixels": (2 if denoise_applied else 0)
        + 1
        + (1 if sharpen_applied else 0),
        "noise_hint": {
            "formula": "decoder stochastic variance_e2*lsc_gain^2/normalization_e^2",
            "range_floor_unit": "post-LSC-equivalent electron^2 before normalization",
            "meaning": "pre-filter local tuning estimate; not propagated output covariance",
            "quantization_variance": "not_included",
            "uncertainty": {"kind": "unknown"},
        },
        "denoise": {
            "stencil": "3x3 same native site",
            "spatial_kernel": isp_v2.SPATIAL.tolist(),
            "invalid_neighbors": "excluded before arithmetic",
            "temporal_mixing": False,
        },
        "sharpen": {
            "guide": "arithmetic mean of linear sensor RGB; not photopic luminance",
            "delta": "sign(detail)*min(max_delta,amount*max(abs(detail)-threshold,0))",
            "invalid_stencil": "bypass; preserve center validity",
        },
        "quantization_scope": "input and post-LSC decoder evidence only; "
        "no post-filter bound or latent-charge confidence claim",
        "rgb_meaning": "fixed matrix-transformed sensor RGB; colorimetric accuracy not established",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "limitations": [
            "No acquisition, noise resampling, HDR/DCG fusion, "
            "calibration fitting or temporal filtering.",
            "Caller binds preserved source topology and charge-LSC provenance/domain.",
            "Local inverse-slope variance is approximate near nonlinear knots; "
            "calibration uncertainty is unknown.",
            "Denoise/sharpen do not establish output covariance "
            "or post-filter quantization intervals.",
            "LSC does not restore throughput or photons; sharpen response is not Lens MTF.",
        ],
    }
    return {"arrays": arrays, "summary": summary}
