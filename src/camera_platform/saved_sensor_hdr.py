"""Bounded static RAW bracket from one pinned CameraE2E sensor run.

This adapter reuses pyisetcam's frozen sensor response and digital RAW capture.
The zero-FPN sensitivity mode uses the platform's scalar RAW HDR merger. The
optional native-FPN mode keeps the saved PRNU/DSNU/column pattern and inverts
its per-pixel gain and post-well bias before merging the original RAW codes.
Those maps are known from the saved simulation model, not independently
measured calibration. Neither mode validates temporal PWM/LFM, motion HDR or
measured camera performance.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.assets import AssetStore
from pyisetcam.camera import camera_create, camera_get, camera_set
from pyisetcam.scene import scene_adjust_luminance, scene_calculate_luminance
from pyisetcam.sensor_capture import resample_sensor_capture
from pyisetcam.sensor_comparison import _physical_geometry
from pyisetcam.sensor_simulation_db import (
    _digest,
    sensor_simulation_db_config,
    sensor_simulation_db_get,
)
from pyisetcam.system_faca import camerae2e_run_scenario

from . import hdr
from .offline_assets import offline_asset_identity, offline_asset_store
from .saved_sensor_quality import _hdr_chart_scene, _hdr_native_rois
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camerae2e_saved_sensor_hdr_bracket_v1"
FPN_OVERRIDE = "zero_fpn_override"
FPN_NATIVE = "model_known_native_fpn"
_FPN_FIELDS = (
    "prnu_fraction", "dsnu_e", "column_prnu_fraction", "column_dsnu_e"
)


def _number(value: Any, name: str, low: float, high: float) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].")
    return number


def _exposures(value: Sequence[float]) -> list[float]:
    if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= 3:
        raise ValueError("Declare two or three independent bracket exposures.")
    selected = [_number(item, "exposure_s", 1e-6, 1.0) for item in value]
    if selected != sorted(set(selected)):
        raise ValueError("Bracket exposure_s must be distinct and ascending.")
    return selected


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixed_pattern_settings(sensor: Any) -> dict[str, Any]:
    if sensor.fields.get("sensor_output_mode") != "digital_raw":
        raise ValueError("Saved HDR requires a linear digital_raw capture.")
    pixel = sensor.fields["pixel"]
    original = {
        "prnu_fraction": float(pixel.get("prnu_sigma", 0.0)),
        "dsnu_v": float(pixel.get("dsnu_sigma_v", 0.0)),
        "column_fpn": [float(v) for v in sensor.fields.get("column_fpn", [0.0, 0.0])],
    }
    if len(original["column_fpn"]) != 2 or not all(
        math.isfinite(v) and v >= 0 for v in [
            original["prnu_fraction"], original["dsnu_v"], *original["column_fpn"]
        ]
    ):
        raise ValueError("Saved FPN settings are outside the supported finite domain.")
    return original


def _disable_fixed_pattern(sensor: Any) -> tuple[Any, dict[str, Any]]:
    """Remove only the FPN terms that scalar RAW HDR cannot calibrate."""
    selected = sensor.clone()
    original = _fixed_pattern_settings(selected)
    for name in (
        "offset_fpn_image", "gain_fpn_image", "column_offset_fpn", "column_gain_fpn"
    ):
        if name in selected.fields:
            raise ValueError("Saved HDR cannot discard a previously generated FPN map.")
    pixel = selected.fields["pixel"]
    pixel["prnu_sigma"] = 0.0
    pixel["dsnu_sigma_v"] = 0.0
    selected.fields["column_fpn"] = [0.0, 0.0]
    selected.fields["noise_flag"] = 2
    return selected, original


def _calibration(capture: Mapping[str, Any], shape: tuple[int, int]) -> dict[str, Any]:
    readout = capture["readout"]
    exposure = float(capture["exposure_time_s"])
    dn_per_e = float(capture["dn_per_electron"])
    black = float(capture["black_level_dn"])
    white = float(capture["white_level_dn"])
    full_well = float(readout["full_well_e"])
    if not all(math.isfinite(v) for v in (exposure, dn_per_e, black, white, full_well)):
        raise ValueError("Capture readout calibration must be finite.")
    return {
        "dn_per_e": dn_per_e,
        "black_level_dn": black,
        "white_level_dn": white,
        "full_well_e": full_well,
        "dark_e": np.full(shape, float(readout["dark_current_e_per_s"]) * exposure),
        "read_noise_variance_e2": float(readout["read_noise_e"]) ** 2,
        "quantization_variance_e2": 1.0 / (12.0 * dn_per_e**2),
        "linear_ceiling_dn": min(white, black + full_well * dn_per_e),
        "linear_floor_dn": 0.0,
    }


def _map_sha(value: np.ndarray) -> str:
    """Hash a model-known map with a portable float64 byte encoding."""
    return hashlib.sha256(np.ascontiguousarray(value, dtype="<f8").tobytes()).hexdigest()


def _native_fpn_calibration(
    captured: Any, scalar: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Recover the exact saved-pattern maps used by digital RAW capture.

    These are oracle calibration coefficients from the simulation, not a dark
    frame or flat-field measurement. No scene photoelectron truth is consumed.
    """
    fields = captured.fields
    shape = tuple(np.asarray(captured.data["raw_dn"]).shape)
    pixel = fields["pixel"]
    conversion = float(pixel["conversion_gain_v_per_electron"])
    column_sigmas = np.asarray(fields.get("column_fpn", [0.0, 0.0]), dtype=float)
    if not math.isfinite(conversion) or conversion <= 0 or column_sigmas.shape != (2,):
        raise ValueError("Native FPN requires a finite conversion gain and two column sigmas.")

    def value(name: str, expected_shape: tuple[int, ...], sigma: float, center: float):
        stored = fields.get(name)
        if stored is None:
            if sigma != 0:
                raise ValueError(f"Native FPN map {name} was not generated by RAW capture.")
            return np.full(expected_shape, center, dtype=np.float64)
        result = np.asarray(stored, dtype=np.float64)
        if result.shape != expected_shape or not np.isfinite(result).all():
            raise ValueError(f"Native FPN map {name} is not finite or has the wrong shape.")
        return result.copy()

    pixel_offset_v = value(
        "offset_fpn_image", shape, float(pixel.get("dsnu_sigma_v", 0.0)), 0.0
    )
    pixel_gain = value("gain_fpn_image", shape, float(pixel.get("prnu_sigma", 0.0)), 1.0)
    column_offset_v = value(
        "column_offset_fpn", (shape[1],), float(column_sigmas[0]), 0.0
    )
    column_gain = value("column_gain_fpn", (shape[1],), float(column_sigmas[1]), 1.0)
    gain = pixel_gain * column_gain[None, :]
    bias_e = (pixel_offset_v * column_gain[None, :] + column_offset_v[None, :]) / conversion
    if not np.isfinite(gain).all() or np.any(gain <= 0) or not np.isfinite(bias_e).all():
        raise ValueError("Native FPN has nonpositive response gain or nonfinite bias.")
    maps = {
        "pixel_gain": pixel_gain,
        "pixel_offset_e": pixel_offset_v / conversion,
        "column_gain": column_gain,
        "column_offset_e": column_offset_v / conversion,
        "response_gain": gain,
        "post_well_bias_e": bias_e,
    }
    return {**scalar, "response_gain": gain, "post_well_bias_e": bias_e}, maps


def _merge_native_fpn(
    raw: np.ndarray,
    exposures: Sequence[float],
    calibrations: Sequence[Mapping[str, Any]],
    *,
    geometry: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge original integer RAW through model-known per-pixel FPN inverse."""
    if raw.dtype != np.dtype("int32") or raw.ndim != 3 or raw.shape[0] != len(exposures):
        raise ValueError("Native FPN HDR requires aligned int32 RAW bracket frames.")
    input_sha256 = hashlib.sha256(raw.tobytes(order="C")).hexdigest()
    count, rows, cols = raw.shape
    if len(calibrations) != count or rows % 2 or cols % 2:
        raise ValueError("Native FPN HDR requires one calibrated even native mosaic per frame.")
    profile = hdr.validate_profile(hdr.default_profile())
    resolved_geometry, pattern = hdr._geometry(geometry, (rows, cols))
    rates = np.empty(raw.shape, dtype=np.float64)
    variances = np.empty_like(rates)
    valid = np.empty(raw.shape, dtype=bool)
    summaries: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for index, (cal, exposure) in enumerate(zip(calibrations, exposures, strict=True)):
        scalar = {key: cal[key] for key in hdr.CALIBRATION_FIELDS}
        normalized, dark = hdr._calibration(scalar, (rows, cols))
        gain = np.asarray(cal["response_gain"], dtype=np.float64)
        bias = np.asarray(cal["post_well_bias_e"], dtype=np.float64)
        if (
            gain.shape != (rows, cols)
            or bias.shape != (rows, cols)
            or not np.isfinite(gain).all()
            or not np.isfinite(bias).all()
            or np.any(gain <= 0)
        ):
            raise ValueError("Native FPN calibration has invalid gain or bias maps.")
        dn_per_e = normalized["dn_per_e"]
        black = normalized["black_level_dn"]
        white = normalized["white_level_dn"]
        full_well = normalized["full_well_e"]
        readvar = normalized["read_noise_variance_e2"]
        quantvar = normalized["quantization_variance_e2"]
        if np.any(raw[index] < 0) or np.any(raw[index] > white):
            raise ValueError("Native FPN RAW lies outside its saved ADC range.")
        with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
            observed_e = (raw[index].astype(np.float64) - black) / dn_per_e
            input_e = (observed_e - bias) / gain
            rates[index] = (input_e - dark) / exposure
            variances[index] = (
                np.maximum(input_e, 0) + (readvar + quantvar) / gain**2
            ) / exposure**2
            floor_dn = np.clip(black + dn_per_e * bias, 0.0, white)
            ceiling_dn = np.clip(black + dn_per_e * (full_well + bias), 0.0, white)
            guard_dn = max(
                profile["saturation_margin_dn"],
                profile["read_noise_guard_sigma"] * math.sqrt(readvar) * dn_per_e,
            )
        if (
            not np.isfinite(rates[index]).all()
            or not np.isfinite(variances[index]).all()
            or not np.isfinite(floor_dn).all()
            or not np.isfinite(ceiling_dn).all()
        ):
            raise ValueError("Native FPN inverse exceeds its numerical domain.")
        lower = raw[index] <= floor_dn + guard_dn
        upper = raw[index] >= ceiling_dn - guard_dn
        unresolvable = ceiling_dn - floor_dn <= 2 * guard_dn
        undefined = variances[index] <= 0
        valid[index] = ~(lower | upper | unresolvable | undefined)
        summaries.append(
            {
                **normalized,
                "dark_e_shape": [rows, cols],
                "dark_e_sha256": _map_sha(dark),
                "response_gain_sha256": _map_sha(gain),
                "post_well_bias_e_sha256": _map_sha(bias),
                "calibration_source": "saved_simulation_model_known_maps_not_measured",
            }
        )
        diagnostics.append(
            {
                "frame_index": index,
                "guard_dn": float(guard_dn),
                "lower_censored_pixels": int(lower.sum()),
                "upper_censored_pixels": int(upper.sum()),
                "unresolvable_calibration_pixels": int(unresolvable.sum()),
                "undefined_variance_pixels": int(undefined.sum()),
                "usable_pixels": int(valid[index].sum()),
            }
        )
    usable_count = valid.sum(axis=0)
    output_valid = usable_count > 0
    minimum = np.min(np.where(valid, variances, np.inf), axis=0)
    reciprocal = np.zeros_like(rates)
    np.divide(minimum[None, ...], variances, out=reciprocal, where=valid)
    weights = np.zeros_like(rates)
    np.divide(reciprocal, reciprocal.sum(axis=0)[None, ...], out=weights,
              where=output_valid[None, ...])
    merged_rate = np.sum(weights * rates, axis=0)
    estimated_variance = np.sum(weights**2 * variances, axis=0)
    merged_rate[~output_valid] = np.nan
    estimated_variance[~output_valid] = np.nan
    unknown = usable_count < 2
    consistent = ~unknown
    for first in range(count):
        for second in range(first + 1, count):
            joint = valid[first] & valid[second]
            threshold = profile["consistency_sigma"] * np.hypot(
                np.sqrt(variances[first]), np.sqrt(variances[second])
            )
            difference = np.abs(rates[first] - rates[second])
            if not np.isfinite(threshold[joint]).all() or not np.isfinite(difference[joint]).all():
                raise ValueError("Native FPN temporal consistency exceeds its numerical domain.")
            consistent &= ~joint | (difference <= threshold)
    if (
        not np.isfinite(merged_rate[output_valid]).all()
        or not np.isfinite(estimated_variance[output_valid]).all()
    ):
        raise ValueError("Native FPN merge exceeds its numerical domain.")
    linear, rgb_valid, preview = hdr._rgb(merged_rate, output_valid, pattern, profile)
    if hashlib.sha256(raw.tobytes(order="C")).hexdigest() != input_sha256:
        raise RuntimeError("Original RAW input changed during native FPN fusion.")
    arrays = {
        "exposure_rate": rates,
        "exposure_estimated_variance": variances,
        "exposure_valid_mask": valid,
        "weights": weights,
        "merged_rate": merged_rate,
        "estimated_variance": estimated_variance,
        "valid_mask": output_valid,
        "temporal_consistency_mask": consistent,
        "temporal_consistency_unknown_mask": unknown,
        "linear_rgb": linear,
        "linear_rgb_valid_mask": rgb_valid,
        "rgb": preview,
    }
    summary = {
        "schema": "camera_hdr_native_fpn_merge_result_v1",
        "method": "inverse_variance_raw_per_pixel_model_fpn",
        "profile": profile,
        "geometry": resolved_geometry,
        "frames": count,
        "exposure_s": list(exposures),
        "input_shape": list(raw.shape),
        "input_dtype": raw.dtype.str,
        "input_array_bytes_sha256": input_sha256,
        "input_hash_scope": "C-order original RAW array bytes; source retained by caller",
        "calibrations": summaries,
        "frame_diagnostics": diagnostics,
        "valid_pixels": int(output_valid.sum()),
        "invalid_pixels": int((~output_valid).sum()),
        "temporal_consistent_pixels": int(consistent.sum()),
        "temporal_inconsistent_pixels": int((~consistent & ~unknown).sum()),
        "temporal_consistency_unknown_pixels": int(unknown.sum()),
        "single_usable_exposure_pixels": int((usable_count == 1).sum()),
        "linear_rgb_valid_pixels": int(rgb_valid.sum()),
        "rate_estimator": (
            "(((raw-black)/dn_per_e-post_well_bias_e)/response_gain-dark_e)/exposure_s"
        ),
        "variance_estimator": (
            "(max(input_e,0)+(read_var+quant_var)/response_gain**2)/exposure_s**2"
        ),
        "estimated_variance_scope": "plug-in independent shot/read/quantization variance only",
        "calibration_source": "saved_simulation_model_known_maps_not_measured",
        "calibration_independence": False,
        "censoring": "per-pixel post-well-bias floor and full-well/ADC ceiling with RAW DN guard",
        "weight_normalization": "per-pixel sum=1 over usable exposures; all-invalid weights=0",
        "consistency_test": "all usable pairs: abs(rate_i-rate_j)<=sigma*sqrt(var_i+var_j)",
        "invalid_policy": "NaN radiometric values with false mask; preview alone uses black",
        "demosaic": "pyisetcam.ip._ie_bilinear; Gr/Gb collapsed in preview only",
        "physical_validation": "not_established",
        "uncertainty": {"kind": "unknown"},
        "limitations": [
            "Perfect knowledge of saved model FPN maps is assumed, not measured calibration.",
            "FPN gain is inverted only where positive; unresolvable RAW spans are censored.",
            "No correlated-noise model, motion alignment, PWM correction or deghosting.",
        ],
    }
    return {"arrays": arrays, "summary": summary}


def evaluate_saved_sensor_hdr_bracket(
    identifier: str,
    *,
    run_id: str,
    root: str | Path,
    workspace: str | Path,
    output_dir: str | Path,
    exposure_s: Sequence[float] = (0.001, 0.016),
    luminance_cd_m2: float = 1000.0,
    output_size: Sequence[int] = (32, 32),
    seed: int = 7,
    fixed_pattern_mode: str = FPN_OVERRIDE,
) -> dict[str, Any]:
    """Capture original bracket RAW, then fuse its observable codes in rate units."""
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("A nonempty saved sensor identifier is required.")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("An explicit saved sensor run_id is required.")
    if fixed_pattern_mode not in {FPN_OVERRIDE, FPN_NATIVE}:
        raise ValueError("Saved HDR requires zero_fpn_override or model_known_native_fpn.")
    exposures = _exposures(exposure_s)
    luminance = _number(luminance_cd_m2, "luminance_cd_m2", 0.01, 10000.0)
    if (
        not isinstance(output_size, (list, tuple))
        or len(output_size) != 2
        or any(type(v) is not int or v != 32 for v in output_size)
    ):
        raise ValueError("Saved HDR strip chart currently requires 32x32 output_size.")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    if not isinstance(output_dir, (str, Path)):
        raise ValueError("output_dir must be a filesystem path.")
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Saved HDR output_dir is not empty: {destination}")

    inspection = inspect_sensor_revision(identifier, root=root, run_id=run_id)
    if not inspection["executable"]:
        raise ValueError(f"Saved sensor run cannot execute: {inspection['reason']}")
    revision = inspection["revision"]
    if revision["model"] not in {"db_estimate_v1", "physical_estimate_v1"}:
        raise ValueError("Saved HDR requires a pinned DB/physical estimate snapshot.")
    saved = sensor_simulation_db_get(revision["report_code"], root=root, run_id=run_id)
    metadata = saved["metadata"]
    if _digest(metadata["config"]) != revision["config_sha256"]:
        raise ValueError("Saved HDR config hash differs from the selected run.")
    settings = metadata.get("simulation_settings") or {}
    if settings.get("fnumber") != 4.0 or settings.get("focal_length_m") != 0.004:
        raise ValueError("Saved HDR requires the saved F/4, 4 mm ideal optics contract.")
    configuration = sensor_simulation_db_config(
        revision["report_code"], root=root, run_id=run_id
    )
    scenario = configuration["scenario"]
    if scenario.get("sensor_output_mode") != "digital_raw":
        raise ValueError("Saved HDR requires the digital_raw output contract.")
    cfa = metadata.get("native_cfa") or {}
    if (
        cfa.get("pattern") != [[1, 2], [3, 4]]
        or cfa.get("filter_names") != ["R", "Gr", "Gb", "B"]
    ):
        raise ValueError("Saved HDR currently requires native R/Gr/Gb/B Bayer CFA.")
    profile = metadata.get("readout_profile") or {}
    saved_fpn = profile.get("fixed_pattern") or {}
    if set(saved_fpn) != set(_FPN_FIELDS):
        raise ValueError("Saved HDR requires explicit four-field FPN provenance.")
    model = metadata["model_snapshot"]
    pitch_m = float(model["pixel_pitch_um"]) * 1e-6
    geometry = _physical_geometry([pitch_m], (32, 32))
    scene_fov = float(np.degrees(2 * np.arctan(
        1.2 * geometry["common_extent_m"][1] / (2 * 0.004)
    )))
    source_identity = json.dumps(inspection, sort_keys=True, separators=(",", ":"), allow_nan=False)
    asset_identity = offline_asset_identity(workspace=workspace, source=root)
    assets = offline_asset_store(workspace=workspace, source=root)
    original_raw = []
    captures = []
    calibrations = []
    observed_fpn = []
    native_maps = []
    native_map_hashes = []
    scene_contract = None
    raw_shape = None
    with AssetStore.bound_default(assets):
        for index, exposure in enumerate(exposures):
            scene, current_scene = _hdr_chart_scene((32, 32), scene_fov, assets)
            scene_adjust_luminance(scene, luminance, asset_store=assets)
            luminance_map = scene_calculate_luminance(scene, asset_store=assets)
            for level in current_scene["levels"]:
                y0, x0, y1, x1 = level["source_roi_half_open"]
                region = np.s_[math.ceil(y0):math.floor(y1), math.ceil(x0):math.floor(x1)]
                level["scene_luminance_cd_m2"] = float(np.mean(luminance_map[region]))
            if scene_contract is None:
                scene_contract = current_scene
            elif current_scene != scene_contract:
                raise ValueError("Static HDR bracket scene changed between exposures.")
            camera = camera_create(asset_store=assets)
            camera_set(camera, "optics fnumber", 4.0)
            camera_set(camera, "optics focal length", 0.004)
            camera_set(camera, "ip render scale", False)
            camera_set(camera, "sensor size", geometry["native_sizes"][0])
            current_scenario = copy.deepcopy(scenario)
            current_scenario["sensor"] = {
                **current_scenario.get("sensor", {}),
                "size": geometry["native_sizes"][0],
                "integration_time": exposure,
                "noise_flag": 0,
            }
            current_scenario["sensor_resize"] = False
            computed = camerae2e_run_scenario(
                current_scenario,
                scene=scene,
                camera=camera,
                asset_store=assets,
                sensor_output_mode="digital_raw",
                include_arrays=False,
                seed=seed,
            )
            cached = camera_get(computed["camera"], "sensor")
            binding = cached.metadata.get("sensor_simulation_db") or {}
            if (
                binding.get("run_id") != run_id
                or binding.get("result_sha256") != revision["result_sha256"]
                or cached.fields["sensor_readout"] != model["readout"]
            ):
                raise ValueError("Executed HDR capture lost its saved sensor/readout binding.")
            if fixed_pattern_mode == FPN_OVERRIDE:
                selected, original_fpn = _disable_fixed_pattern(cached)
            else:
                selected = cached.clone()
                original_fpn = _fixed_pattern_settings(selected)
                selected.fields["noise_flag"] = 2
            frame_seed = int(np.random.SeedSequence([seed, index]).generate_state(1)[0])
            captured = resample_sensor_capture(selected, seed=frame_seed)
            raw = np.asarray(captured.data["raw_dn"])
            if raw.dtype != np.dtype("int32") or raw.ndim != 2:
                raise ValueError("Saved HDR capture did not produce native integer RAW.")
            if raw_shape is None:
                raw_shape = raw.shape
            elif raw.shape != raw_shape:
                raise ValueError("Saved HDR bracket native geometry changed between frames.")
            info = captured.metadata["sensor_capture"]
            if (
                info["noise_flag"] != 2
                or info["exposure_time_s"] != exposure
                or info["readout"] != model["readout"]
            ):
                raise ValueError("Saved HDR readout changed during bracket capture.")
            original_raw.append(raw.copy())
            captures.append({
                "index": index,
                "exposure_s": exposure,
                "noise_seed": frame_seed,
                "capture": info,
                "original_raw_sha256": hashlib.sha256(raw.tobytes()).hexdigest(),
            })
            scalar_calibration = _calibration(info, raw.shape)
            if fixed_pattern_mode == FPN_NATIVE:
                native_calibration, maps = _native_fpn_calibration(captured, scalar_calibration)
                hashes = {name: _map_sha(value) for name, value in maps.items()}
                if native_map_hashes and hashes != native_map_hashes[0]:
                    raise ValueError("Saved native FPN maps changed across bracket exposures.")
                calibrations.append(native_calibration)
                native_maps.append(maps)
                native_map_hashes.append(hashes)
                captures[-1]["model_fpn_map_sha256"] = hashes
            else:
                calibrations.append(scalar_calibration)
            observed_fpn.append(original_fpn)
    assert scene_contract is not None and raw_shape is not None
    if any(value != observed_fpn[0] for value in observed_fpn):
        raise ValueError("Saved FPN profile changed across the bracket.")
    native_rois = _hdr_native_rois(scene_contract, (32, 32), raw_shape)
    for level, roi in zip(scene_contract["levels"], native_rois, strict=True):
        level["native_roi_half_open"] = roi
    raw_bracket = np.stack(original_raw, axis=0)
    native_geometry = {
        "kind": "native_sensor",
        "full_sensor_shape": list(raw_shape),
        "crop_origin": [0, 0],
    }
    if fixed_pattern_mode == FPN_NATIVE:
        merged = _merge_native_fpn(
            raw_bracket, exposures, calibrations, geometry=native_geometry
        )
    else:
        merged = hdr.merge_raw_bracket(
            raw_bracket, exposures, calibrations,
            geometry=native_geometry, profile=hdr.default_profile(),
        )
    if merged["summary"]["input_array_bytes_sha256"] != hashlib.sha256(
        raw_bracket.tobytes()
    ).hexdigest():
        raise ValueError("HDR merger did not consume the original bracket RAW bytes.")
    if json.dumps(inspect_sensor_revision(identifier, root=root, run_id=run_id),
                  sort_keys=True, separators=(",", ":"), allow_nan=False) != source_identity:
        raise ValueError("Saved HDR source changed during the bracket.")
    if offline_asset_identity(workspace=workspace, source=root) != asset_identity:
        raise ValueError("Pinned upstream assets changed during the bracket.")
    level_results = []
    rate = merged["arrays"]["merged_rate"]
    valid = merged["arrays"]["valid_mask"]
    for level, (y0, x0, y1, x1) in zip(scene_contract["levels"], native_rois, strict=True):
        values = rate[y0:y1, x0:x1]
        mask = valid[y0:y1, x0:x1]
        level_results.append({
            "index": level["index"],
            "scene_luminance_cd_m2": level["scene_luminance_cd_m2"],
            "native_roi_half_open": [y0, x0, y1, x1],
            "valid_fraction": float(np.mean(mask)),
            "mean_merged_rate_e_per_pixel_s": (
                float(np.mean(values[mask])) if np.any(mask) else None
            ),
        })
    destination.mkdir(parents=True, exist_ok=True)
    npz = destination / "saved-sensor-hdr.npz"
    retained_arrays = {
        "original_raw_dn": raw_bracket,
        **{f"hdr_{key}": value for key, value in merged["arrays"].items()},
    }
    if native_maps:
        retained_arrays.update(
            {f"model_fpn_{key}": value for key, value in native_maps[0].items()}
        )
    np.savez_compressed(npz, **retained_arrays)
    native_mode = fixed_pattern_mode == FPN_NATIVE
    executed_fpn = (
        {key: float(saved_fpn[key]) for key in _FPN_FIELDS}
        if native_mode else {key: 0.0 for key in _FPN_FIELDS}
    )
    report = {
        "schema": SCHEMA,
        "status": "completed",
        "revision": revision,
        "upstream_assets": asset_identity,
        "source_boundary": {
            "saved_sensor_simulation_run_used": True,
            "sensor_model_kind": model["kind"],
            "input_manifest_sha256": saved["input_digest"],
            "saved_arrays_sha256": saved.get("arrays_sha256"),
            "lens_model": "shared_ideal_f4_4mm_not_catalog_lens_revision",
            "native_cfa": "R/Gr/Gb/B Bayer; merged RAW rate retains four mosaic sites",
            "preview_cfa": "existing HDR preview collapses Gr/Gb to one green plane",
            "readout_profile": (
                "saved_scalar_and_model_known_native_fpn_maps"
                if native_mode else "saved_scalar_values_with_explicit_zero_fpn_override"
            ),
            "physical_validation": "not_established",
        },
        "controls": {
            "exposure_s": exposures,
            "luminance_cd_m2": luminance,
            "output_size": [32, 32],
            "seed": seed,
            "capture_timing": "sequential_independent_reset_static_scene_no_timestamps",
            "fixed_pattern_mode": fixed_pattern_mode,
            "saved_fixed_pattern": {key: float(saved_fpn[key]) for key in _FPN_FIELDS},
            "executed_fixed_pattern": executed_fpn,
            "saved_pixel_fpn_before_override": observed_fpn[0],
            "saved_readout_scalar_values": model["readout"],
            "readout_overrides": (
                {} if native_mode
                else {"fixed_pattern": {key: 0.0 for key in _FPN_FIELDS}}
            ),
            "model_fpn_calibration": (
                {
                    "source": "saved_simulation_model_known_maps_not_measured",
                    "independent_calibration": False,
                    "map_sha256": native_map_hashes[0],
                }
                if native_mode else None
            ),
        },
        "scene_contract": scene_contract,
        "captures": captures,
        "fusion": merged["summary"],
        "levels": level_results,
        "artifacts": {"npz": npz.name, "npz_sha256": _sha(npz)},
        "verification": {
            "implementation_verification": "runtime_completed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
            "lfm_evaluated": False,
            "motion_or_deghosting_evaluated": False,
        },
        "limitation": (
            "Static independently reset exposures from one pinned saved sensor model. "
            + (
                "Saved PRNU/DSNU/column FPN remain active, and fusion assumes exact "
                "per-pixel maps from the simulation model rather than independent calibration. "
                if native_mode else
                "Saved PRNU/DSNU/column FPN are disabled explicitly for scalar HDR fusion. "
            )
            + "The merged rate and preview are model estimates, not temporal PWM/LFM, "
            "rolling shutter, motion alignment or measured performance. "
            "The preview collapses the native Gr/Gb planes to green."
        ),
    }
    json_path = destination / "saved-sensor-hdr.json"
    json_path.write_text(json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return {**report, "artifacts": {"json": str(json_path), "npz": str(npz)},
            "artifact_sha256": {"json": _sha(json_path), "npz": _sha(npz)}}
