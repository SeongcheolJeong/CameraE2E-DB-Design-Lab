"""Bounded native RAW ISP v2; explicit calibration hints are not uncertainty.

The fixed graph preserves signed floating intermediates and acquired RAW bytes.
It does not acquire pixels, infer calibration, or alter the legacy ISP backend.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from . import isp

SCHEMA = "camera_isp_profile_v2"
IMPLEMENTATION = "camera_platform_float_isp_v2"
GRAPH = [
    "black_level",
    "spatial_lsc",
    "raw_denoise",
    "demosaic",
    "white_balance",
    "ccm",
    "linear_sharpen",
    "tone",
]
SITES = ["R", "Gr", "Gb", "B"]
MAX_OUTPUT_ARRAY_BYTES = 128 * 1024 * 1024
MAX_WORKING_ARRAY_BYTES = 1024 * 1024 * 1024
SPATIAL = np.array([[1.0, 2.0, 1.0], [2.0, 4.0, 2.0], [1.0, 2.0, 1.0]])


def default_profile(calibration_id=None):
    result = isp.default_profile()
    result.update(
        schema=SCHEMA,
        graph=GRAPH.copy(),
        calibration_id=calibration_id,
        disabled_effects=["auto_white_balance", "hdr_merge", "temporal_denoise"],
        spatial_lsc={"mode": "bypass"},
        raw_denoise={"mode": "bypass", "strength": 0.5, "range_floor_dn2": 1.0},
        linear_sharpen={"mode": "bypass", "amount": 0.25, "threshold": 0.01, "max_delta": 0.1},
    )
    return result


def _common_profile(profile):
    result = isp.default_profile()
    for key in ("precision", "black_level", "demosaic", "white_balance", "ccm", "tone"):
        result[key] = copy.deepcopy(profile[key])
    return isp.validate_profile(result)


def validate_profile(profile):
    expected = default_profile()
    isp._exact(profile, set(expected), "profile")
    for key in ("schema", "graph", "precision", "disabled_effects"):
        if profile[key] != expected[key]:
            isp._refuse("Unsupported or reordered " + key, "profile." + key)
    common = _common_profile(profile)
    result = copy.deepcopy(expected)
    for key in ("black_level", "demosaic", "white_balance", "ccm", "tone"):
        result[key] = common[key]
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
            "calibrated_same_site_filter_v1",
            {"strength": (0, 1), "range_floor_dn2": (0, 1e12)},
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
    if result["raw_denoise"]["range_floor_dn2"] <= 0:
        isp._refuse("Range denominator floor must be positive DN squared", "raw_denoise")
    if identity is None and any(
        result[key]["mode"] != "bypass" for key in ("spatial_lsc", "raw_denoise")
    ):
        isp._refuse("LSC or denoise requires an immutable calibration ID", "calibration_id")
    return result


def describe_capabilities():
    return {
        "implementation_id": IMPLEMENTATION,
        "profile_schema": SCHEMA,
        "graph": GRAPH.copy(),
        "precision": "float64",
        "supports": [
            "native_single_stream_integer_rggb",
            "four_native_crop_phases",
            "signed_intermediates",
            "native_four_site_grid_v1",
            "calibrated_same_site_filter_v1",
            "bounded_detail_v1",
            "retained_raw_replay",
            "fixed_v1_baseline",
        ],
        "unsupported": [
            "representative_raster",
            "paired_dcg_raw",
            "float_radiometric_input",
            "automatic_calibration",
            "temporal_denoise",
            "hardware_latency",
            "physical_color_accuracy",
            "exact_post_filter_covariance",
        ],
        "limits": {
            "max_frame_pixels": isp.MAX_FRAME_PIXELS,
            "max_frames": isp.MAX_FRAMES,
            "max_output_array_bytes": MAX_OUTPUT_ARRAY_BYTES,
            "max_working_array_bytes": MAX_WORKING_ARRAY_BYTES,
        },
        "physical_validation": "not_established",
    }


def validate_inputs(raw, raw_format, profile, *, geometry, calibration=None):
    """Preflight native format, numeric calibration/domain and conservative memory.

    Identity/provenance and acquisition-domain binding of the calibration record
    remain the caller's responsibility. ``calibration`` is its numeric payload.
    """
    selected = validate_profile(profile)
    if not isinstance(geometry, Mapping) or geometry.get("kind") != "native_sensor":
        isp._refuse("ISP v2 requires explicit native sensor geometry", "geometry")
    frames, fmt, geom, pattern = isp._input(raw, raw_format, geometry)
    numeric = None
    if selected["calibration_id"] is not None:
        if calibration is None:
            isp._refuse("The selected calibration numeric payload is missing", "calibration")
        from .isp_calibration import validate_numeric_payload

        numeric = validate_numeric_payload(calibration)
    elif calibration is not None:
        isp._refuse("Unbound calibration payload is unsupported", "calibration")
    count, rows, cols = frames.shape
    norm = fmt["white_level_dn"] - fmt["black_level_dn"]
    for stage, section in (("spatial_lsc", "lsc"), ("raw_denoise", "noise")):
        if selected[stage]["mode"] != "bypass" and (numeric is None or numeric[section] is None):
            isp._refuse("Active stage requires its calibration section", "calibration." + section)
    if selected["spatial_lsc"]["mode"] != "bypass":
        grid = numeric["lsc"]
        for axis, nodes in enumerate((grid["row_nodes"], grid["column_nodes"])):
            origin, full, size = (
                geom["crop_origin"][axis],
                geom["full_sensor_shape"][axis],
                (rows, cols)[axis],
            )
            if (
                nodes[0] < 0
                or nodes[-1] > full - 1
                or nodes[0] > origin
                or nodes[-1] < origin + size - 1
            ):
                isp._refuse(
                    "LSC grid must cover native pixel centers without extrapolation",
                    "calibration.lsc",
                    "OUT_OF_DOMAIN",
                )
    if selected["raw_denoise"]["mode"] != "bypass":
        floor = selected["raw_denoise"]["range_floor_dn2"] / norm / norm
        if not np.isfinite(floor) or floor <= 0:
            isp._refuse("Normalized range floor exceeds the numerical domain", "raw_denoise")
    # Includes retained stages, validity, v1 baseline and its intermediates,
    # bounded sublattice padding/accumulators and RGB sharpening workspace.
    calibration_bytes = 0 if numeric is None else len(str(numeric).encode()) * 4
    working = int(frames.size * 640 + rows * cols * 64 + calibration_bytes)
    largest = int(max(frames.size * 3 * 8, rows * cols * 3 * 8))
    if working > MAX_WORKING_ARRAY_BYTES or largest > MAX_OUTPUT_ARRAY_BYTES:
        isp._refuse("ISP v2 exceeds working/output array bounds", "raw.shape", "RESOURCE_LIMIT")
    return {
        "frames": frames,
        "format": fmt,
        "geometry": geom,
        "pattern": pattern,
        "profile": selected,
        "calibration": numeric,
        "resources": {
            "working_array_bytes_estimate": working,
            "largest_output_array_bytes": largest,
            "estimate_kind": "conservative_array_budget_not_measured_peak",
            "automatic_coarsening": False,
        },
    }


def _site_map(shape, origin):
    tile = np.roll(
        np.array([[0, 1], [2, 3]], dtype=np.int8), (-origin[0] % 2, -origin[1] % 2), axis=(0, 1)
    )
    return np.tile(tile, (shape[0] // 2, shape[1] // 2)), tile


def _lsc_map(grid, geometry, site):
    """Bilinear native-center interpolation; zero-weight invalid nodes do not contribute."""
    rows, cols = site.shape
    axes = [np.asarray(grid[name], dtype=float) for name in ("row_nodes", "column_nodes")]
    coordinates = [
        np.arange(size) + origin
        for size, origin in zip((rows, cols), geometry["crop_origin"], strict=True)
    ]
    indices = [
        np.clip(np.searchsorted(nodes, coord, side="right") - 1, 0, len(nodes) - 2)
        for nodes, coord in zip(axes, coordinates, strict=True)
    ]
    fractions = [
        (coord - nodes[index]) / (nodes[index + 1] - nodes[index])
        for coord, nodes, index in zip(coordinates, axes, indices, strict=True)
    ]
    values, valid_nodes = np.asarray(grid["gains"]), np.asarray(grid["valid_mask"])
    result, valid = np.zeros(site.shape), np.ones(site.shape, dtype=bool)
    for dy in range(2):
        for dx in range(2):
            weight = (fractions[0] if dy else 1 - fractions[0])[:, None] * (
                fractions[1] if dx else 1 - fractions[1]
            )[None, :]
            at = (indices[0][:, None] + dy, indices[1][None, :] + dx, site)
            result += weight * values[at]
            valid &= (weight == 0) | valid_nodes[at]
    return np.where(valid, result, 1.0), valid


def _neighbors(value):
    padded = np.pad(value, ((0, 0), (1, 1), (1, 1)), mode="reflect")
    rows, cols = value.shape[1:]
    for dy in range(3):
        for dx in range(3):
            yield padded[:, dy : dy + rows, dx : dx + cols], SPATIAL[dy, dx]


def _stencil_valid(valid):
    result = np.ones(valid.shape, dtype=bool)
    for neighbor, _ in _neighbors(valid):
        result &= neighbor
    return result


def same_site_denoise(value, variance, valid, strength, floor):
    """Pure F×H×W filter; callers validate units, masks and four-site geometry.

    Variance and ``floor`` use the square of ``value``'s units. The operation
    preserves signed samples and never mixes the four Bayer sublattices.
    """
    result = value.copy()
    if strength == 0:
        return result
    for dy in range(2):
        for dx in range(2):
            center, hint, allowed = (a[:, dy::2, dx::2] for a in (value, variance, valid))
            numerator, denominator = np.zeros(center.shape), np.zeros(center.shape)
            for (neighbor, spatial), (other_hint, _), (other_valid, _) in zip(
                _neighbors(center), _neighbors(hint), _neighbors(allowed), strict=True
            ):
                weight = spatial * np.exp(
                    -np.square(neighbor - center) / (2 * np.maximum(hint + other_hint, floor))
                )
                weight *= other_valid
                numerator += weight * neighbor
                denominator += weight
            average = np.divide(numerator, denominator, out=center.copy(), where=denominator > 0)
            # Invalid centers remain the original value with a false mask: no repair claim.
            result[:, dy::2, dx::2] = np.where(
                allowed, center + strength * (average - center), center
            )
    return result


def _denoise(value, variance, valid, strength, floor):
    """Compatibility entry point for the charge-domain ISP."""
    return same_site_denoise(value, variance, valid, strength, floor)


def bounded_detail_delta(guide, applied_mask, *, amount, threshold, max_delta):
    """Pure bounded detail in the caller's linear units, with explicit support."""
    blur = sum(value * weight / 16 for value, weight in _neighbors(guide))
    detail = guide - blur
    return np.where(
        applied_mask,
        np.sign(detail)
        * np.minimum(max_delta, amount * np.maximum(np.abs(detail) - threshold, 0)),
        0.0,
    )


def process_raw(raw, raw_format, profile, *, geometry, calibration=None):
    """Execute the fixed graph; all stage samples remain finite with separate masks."""
    from pyisetcam.ip import _ie_bilinear
    from pyisetcam.utils import linear_to_srgb

    parsed = validate_inputs(raw, raw_format, profile, geometry=geometry, calibration=calibration)
    frames, fmt, geom, selected, numeric = (
        parsed[key] for key in ("frames", "format", "geometry", "profile", "calibration")
    )
    count, rows, cols = frames.shape
    before = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    norm = fmt["white_level_dn"] - fmt["black_level_dn"]
    signal = frames.astype(float) - fmt["black_level_dn"]
    blc = signal / norm
    raw_valid = (frames > 0) & (frames < fmt["white_level_dn"])
    site, tile = _site_map((rows, cols), geom["crop_origin"])
    lsc_applied = selected["spatial_lsc"]["mode"] != "bypass"
    gain, grid_valid = (
        _lsc_map(numeric["lsc"], geom, site)
        if lsc_applied
        else (np.ones((rows, cols)), np.ones((rows, cols), dtype=bool))
    )
    lsc = blc * gain if lsc_applied else blc.copy()
    lsc_valid = raw_valid & grid_valid
    denoise_config = selected["raw_denoise"]
    denoise_configured = denoise_config["mode"] != "bypass"
    denoise_applied = denoise_configured and denoise_config["strength"] > 0
    hint_dn2, hint = np.zeros(frames.shape), np.zeros(frames.shape)
    hint_valid = np.zeros(frames.shape, dtype=bool)
    if denoise_configured:
        noise = numeric["noise"]
        lower, upper = noise["signal_interval_dn"]
        hint_valid = lsc_valid & (signal >= lower) & (signal <= upper)
        hint_dn2 = np.where(
            hint_valid,
            np.asarray(noise["slope_dn"])[site] * np.maximum(signal, 0)
            + np.asarray(noise["intercept_dn2"])[site],
            0.0,
        )
        hint = hint_dn2 * np.square(gain) / norm / norm
    denoise_valid = hint_valid if denoise_applied else lsc_valid.copy()
    denoised = (
        same_site_denoise(
            lsc,
            hint,
            denoise_valid,
            denoise_config["strength"],
            denoise_config["range_floor_dn2"] / norm / norm,
        )
        if denoise_applied
        else lsc.copy()
    )
    demosaiced = np.empty((count, rows, cols, 3))
    rgb_index = np.take([0, 1, 1, 2], site)
    for index in range(count):
        planes = np.zeros((rows, cols, 3))
        for channel in range(3):
            pixels = rgb_index == channel
            planes[..., channel][pixels] = denoised[index][pixels]
        demosaiced[index] = _ie_bilinear(planes, parsed["pattern"])
    rgb_valid = _stencil_valid(denoise_valid)
    balanced = demosaiced * np.asarray(selected["white_balance"]["gains_rgb"])
    corrected = balanced @ np.asarray(selected["ccm"]["matrix_rgb"]).T
    sharpen = selected["linear_sharpen"]
    sharpen_applied = (
        sharpen["mode"] != "bypass" and sharpen["amount"] > 0 and sharpen["max_delta"] > 0
    )
    stencil_valid = _stencil_valid(rgb_valid)
    applied_mask = stencil_valid if sharpen_applied else np.zeros(frames.shape, dtype=bool)
    delta = np.zeros(frames.shape)
    if sharpen_applied:
        guide = corrected.mean(axis=-1)
        delta = bounded_detail_delta(
            guide,
            applied_mask,
            amount=sharpen["amount"],
            threshold=sharpen["threshold"],
            max_delta=sharpen["max_delta"],
        )
    sharpened = corrected + delta[..., None] if sharpen_applied else corrected.copy()
    clipped = np.clip(sharpened, 0.0, 1.0)
    output = linear_to_srgb(clipped) if selected["tone"]["curve"] == "srgb" else clipped
    baseline = isp.process_raw(raw, raw_format, _common_profile(selected), geometry=geom)
    arrays = {
        "black_subtracted_normalized_frames": blc,
        "lsc_normalized_frames": lsc,
        "denoised_normalized_frames": denoised,
        "demosaiced_rgb_frames": demosaiced,
        "white_balanced_rgb_frames": balanced,
        "ccm_rgb_frames": corrected,
        "sharpened_rgb_frames": sharpened,
        "sharpen_delta_frames": delta,
        "rgb_frames": output,
        "rgb": output[0].copy(),
        "baseline_rgb_frames": baseline["arrays"]["rgb_frames"],
        "baseline_rgb": baseline["arrays"]["rgb"],
        "lsc_gain_map": gain,
        "native_site_index": site,
        "native_site_index_tile": tile,
        "noise_variance_hint_dn2_frames": hint_dn2,
        "noise_variance_hint_normalized_frames": hint,
        "raw_code_valid_mask_frames": raw_valid,
        "lsc_calibration_valid_mask": grid_valid,
        "lsc_valid_mask_frames": lsc_valid,
        "noise_hint_valid_mask_frames": hint_valid,
        "denoise_valid_mask_frames": denoise_valid,
        "demosaic_valid_mask_frames": rgb_valid,
        "sharpen_stencil_valid_mask_frames": stencil_valid,
        "sharpen_applied_mask_frames": applied_mask,
        "sharpen_valid_mask_frames": rgb_valid,
        "rgb_valid_mask_frames": rgb_valid,
    }
    baseline_summary = baseline["summary"]
    del baseline
    if not all(np.isfinite(value).all() for value in arrays.values()):
        isp._refuse("ISP v2 produced a nonfinite intermediate", "result", "NUMERICAL_FAILURE")
    if any(value.nbytes > MAX_OUTPUT_ARRAY_BYTES for value in arrays.values()):
        isp._refuse("ISP v2 output exceeds storage bound", "result", "RESOURCE_LIMIT")
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != before:
        isp._refuse("RAW input changed during processing", "raw", "INTEGRITY_FAILURE")
    units = {key: "boolean" if value.dtype.kind == "b" else "1" for key, value in arrays.items()}
    units.update(
        native_site_index="index",
        native_site_index_tile="index",
        noise_variance_hint_dn2_frames="DN^2",
        noise_variance_hint_normalized_frames="1^2",
    )
    stages = dict(
        zip(
            GRAPH,
            [
                ("black_subtracted_normalized_frames", "raw_code_valid_mask_frames", True),
                ("lsc_normalized_frames", "lsc_valid_mask_frames", lsc_applied),
                ("denoised_normalized_frames", "denoise_valid_mask_frames", denoise_applied),
                ("demosaiced_rgb_frames", "demosaic_valid_mask_frames", True),
                ("white_balanced_rgb_frames", "demosaic_valid_mask_frames", True),
                ("ccm_rgb_frames", "demosaic_valid_mask_frames", True),
                ("sharpened_rgb_frames", "sharpen_valid_mask_frames", sharpen_applied),
                ("rgb_frames", "rgb_valid_mask_frames", True),
            ],
            strict=True,
        )
    )
    backend = copy.deepcopy(baseline_summary["implementation"])
    backend.update(id=IMPLEMENTATION)
    for name in ("isp_v2.py", "isp_calibration.py"):
        backend["source_sha256"]["camera_platform/" + name] = hashlib.sha256(
            Path(__file__).with_name(name).read_bytes()
        ).hexdigest()
    summary = {
        "schema_version": "camera_isp_replay_result_v2",
        "implementation": backend,
        "profile": selected,
        "profile_sha256": isp._digest(selected),
        "raw_format": fmt,
        "geometry": geom,
        "input_shape": list(raw.shape),
        "input_dtype": raw.dtype.str,
        "input_array_bytes_sha256": before,
        "input_hash_scope": baseline_summary["input_hash_scope"],
        "frames": count,
        "output_size": [rows, cols],
        "site_names": SITES.copy(),
        "native_site_index_tile": tile.tolist(),
        "local_cfa_rgb_indices": (parsed["pattern"] - 1).tolist(),
        "array_units": units,
        "resources": parsed["resources"],
        "stage_outputs": {
            stage: {
                "array": a,
                "validity": mask,
                "unit": "1",
                "applied": applied,
                "version": "1",
                "configuration_sha256": isp._digest(selected[stage]),
            }
            for stage, (a, mask, applied) in stages.items()
        },
        "calibration_id": selected["calibration_id"],
        "calibration_numeric_sha256": None if numeric is None else isp._digest(numeric),
        "baseline": {
            "implementation_id": isp.IMPLEMENTATION,
            "profile": baseline_summary["profile"],
            "profile_sha256": baseline_summary["profile_sha256"],
            "array": "baseline_rgb_frames",
            "same_original_raw": True,
            "same_wb_ccm_tone": True,
            "new_stages_applied": False,
        },
        "normalization": baseline_summary["normalization"],
        "black_subtractions": 1,
        "frame_max_normalization": False,
        "linear_negative_policy": "preserved until tone",
        "tone_input_clipping": [0.0, 1.0],
        "boundary": "reflect local crop; same-site planes independently",
        "crop_full_frame_comparison_halo_native_pixels": (2 if denoise_applied else 0)
        + 1
        + (1 if sharpen_applied else 0),
        "mask_scope": "RAW endpoints, calibration domain and stencil validity; "
        "not sensor saturation truth",
        "lsc_invalid_policy": "invalid interpolation support bypasses gain and remains invalid",
        "noise_hint": {
            "source_reference": "black_subtracted_dn_before_lsc",
            "formula": "(slope_dn*max(raw_dn-black_dn,0)+intercept_dn2)*lsc_gain^2/normalization^2",
            "range_floor_unit": "post-LSC-equivalent DN^2 before normalization",
            "range_weight": "exp(-delta_normalized^2/(2*max(v_i+v_j,range_floor_dn2/N^2)))",
            "invalid_policy": "out-of-domain hint is masked with zero placeholder; "
            "center bypasses denoise",
            "meaning": "filter tuning hint, not propagated output covariance",
            "uncertainty": {"kind": "unknown"},
        },
        "denoise": {
            "spatial_kernel": SPATIAL.tolist(),
            "stencil": "3x3 same native site",
            "center_policy": "valid center retained; invalid center unchanged and invalid",
            "invalid_neighbors": "excluded",
            "mixing": "center+strength*(weighted_average-center)",
        },
        "sharpen": {
            "guide": "arithmetic mean of linear sensor RGB; not photopic luminance",
            "blur_kernel": (SPATIAL / 16).tolist(),
            "delta": "sign(detail)*min(max_delta,amount*max(abs(detail)-threshold,0))",
            "rgb_application": "same bounded signed delta on all three channels",
            "invalid_stencil": "bypass; preserve center validity",
            "threshold_unit": "normalized linear RGB",
        },
        "frame_diagnostics": [
            {
                "frame_index": i,
                "raw_endpoint_pixels": int((~raw_valid[i]).sum()),
                "valid_rgb_pixels": int(rgb_valid[i].sum()),
                "denoise_changed_pixels": int((denoised[i] != lsc[i]).sum()),
                "sharpen_changed_pixels": int((delta[i] != 0).sum()),
                "tone_low_clipped_samples": int((sharpened[i] < 0).sum()),
                "tone_high_clipped_samples": int((sharpened[i] > 1).sum()),
            }
            for i in range(count)
        ],
        "rgb_meaning": baseline_summary["rgb_meaning"],
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "limitations": [
            "No acquisition, calibration fitting, temporal state, DCG fusion or hardware timing.",
            "Calibration provenance and acquisition-domain binding must be verified by the caller.",
            "LSC scales observed codes and noise; "
            "it does not restore optical throughput or photons.",
            "Data-dependent denoise and sharpen do not establish "
            "exact output covariance or total uncertainty.",
            "Invalid samples remain finite with separate masks; "
            "no missing-information recovery is claimed.",
            "Crop reflection cannot reproduce unavailable outside pixels; "
            "compare interiors using the declared halo.",
            "Processed edge/sharpen response is not Lens MTF or verified sensor colorimetry.",
        ],
    }
    return {"arrays": arrays, "summary": summary}
