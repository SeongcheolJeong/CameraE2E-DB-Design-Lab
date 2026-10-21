"""Bounded saved-sensor PWM phase sweep with exact two-level row integration.

This is an authored time-varying illumination sensitivity experiment. The
original saved readout, including FPN, remains active. It is not a measurement
of LED flicker mitigation, sensor HDR mode, exposure control or motion.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from pyisetcam.assets import AssetStore
from pyisetcam.camera import camera_create, camera_get, camera_set
from pyisetcam.scene import scene_adjust_luminance, scene_create, scene_set
from pyisetcam.sensor_capture import resample_sensor_capture
from pyisetcam.sensor_simulation_db import (
    _digest,
    sensor_simulation_db_config,
    sensor_simulation_db_get,
)
from pyisetcam.system_faca import camerae2e_run_scenario

from .offline_assets import offline_asset_identity, offline_asset_store
from .saved_sensor_hdr import _number
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camerae2e_saved_sensor_pwm_lfm_v1"
PHASE_FRACTIONS = (0.0, 0.25, 0.5, 0.75)
SENSOR_SIZE = (8, 8)
LINE_TIME_S = 20e-6
MAX_PWM_CYCLES_PER_EXPOSURE = 32
MAX_LINEARITY_ERROR = 1e-6
MIN_BLACK_RELATIVE_SIGNAL_DN = 1.0
MAX_ELIGIBLE_RAIL_FRACTION = 0.001


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _on_fraction(start: np.ndarray, exposure: float, period: float, duty: float,
                 phase: float) -> np.ndarray:
    """Exact on-time of the declared ideal square wave per exposure window."""
    high = period * duty

    def primitive(t: np.ndarray) -> np.ndarray:
        phase_time = t + phase
        cycles = np.floor(phase_time / period)
        return cycles * high + np.minimum(phase_time - cycles * period, high)

    fraction = (primitive(start + exposure) - primitive(start)) / exposure
    if not np.isfinite(fraction).all() or np.any(fraction < -1e-10) or np.any(
        fraction > 1 + 1e-10
    ):
        raise ValueError("Analytic PWM integral left its finite phase domain.")
    return np.clip(fraction, 0, 1)


def _phase_modulation(phase_cases: list[dict[str, Any]], baseline_capture: dict[str, Any],
                      black_dn: float) -> dict[str, Any]:
    """Refuse a normalized LFM proxy when floor/ceiling hides the response."""
    means = [float(item["mean_raw_dn"]) for item in phase_cases]
    absolute_range = float(max(means) - min(means))
    signal = float(np.mean(means) - black_dn)
    captures = [item["sensor_capture"] for item in phase_cases] + [baseline_capture]
    rails = {
        key: max(float(item["saturation"][key]) for item in captures)
        for key in ("well_fraction", "adc_white_fraction", "adc_zero_fraction")
    }
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in rails.values()):
        raise ValueError("PWM capture saturation fractions are invalid.")
    if not math.isfinite(signal) or not math.isfinite(absolute_range):
        raise ValueError("PWM phase RAW mean is not finite.")
    if any(value > MAX_ELIGIBLE_RAIL_FRACTION for value in rails.values()):
        status = "unavailable_clipped_or_rail_limited"
    elif signal <= MIN_BLACK_RELATIVE_SIGNAL_DN:
        status = "unavailable_below_black_relative_signal_floor"
    else:
        status = "eligible"
    return {
        "phase_mean_dn_range": absolute_range,
        "mean_phase_black_relative_dn": signal,
        "maximum_rail_fractions": rails,
        "phase_modulation_status": status,
        "phase_modulation_fraction_black_relative": (
            absolute_range / signal if status == "eligible" else None
        ),
    }


def evaluate_saved_sensor_pwm_lfm(
    identifier: str,
    *,
    run_id: str,
    root: str | Path,
    workspace: str | Path,
    output_dir: str | Path,
    exposure_s: tuple[float, float] | list[float] = (0.001, 0.004),
    pwm_period_s: float = 0.002,
    duty_cycle: float = 0.5,
    off_to_on_ratio: float = 0.02,
    on_luminance_cd_m2: float = 1000.0,
    seed: int = 7,
) -> dict[str, Any]:
    """Compare four PWM phases at two exposures against constant duty-equivalent light."""
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("A saved sensor identifier is required.")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("An explicit saved sensor run_id is required.")
    if not isinstance(exposure_s, (tuple, list)) or len(exposure_s) != 2:
        raise ValueError("Declare exactly two ascending exposure_s values.")
    exposures = [_number(v, "exposure_s", 0.0005, 0.02) for v in exposure_s]
    if not exposures[0] < exposures[1]:
        raise ValueError("PWM exposure_s values must be distinct and ascending.")
    period = _number(pwm_period_s, "pwm_period_s", 0.0005, 0.02)
    duty = _number(duty_cycle, "duty_cycle", 0.05, 0.95)
    low = _number(off_to_on_ratio, "off_to_on_ratio", 0.0, 0.5)
    luminance = _number(on_luminance_cd_m2, "on_luminance_cd_m2", 0.01, 10000.0)
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a uint32 integer.")
    if exposures[-1] / period > MAX_PWM_CYCLES_PER_EXPOSURE:
        raise ValueError("PWM exposure exceeds the bounded 32-cycle integration domain.")
    if not isinstance(output_dir, (str, Path)):
        raise ValueError("output_dir must be a filesystem path.")
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Saved LFM output_dir is not empty: {destination}")

    inspection = inspect_sensor_revision(identifier, root=root, run_id=run_id)
    if not inspection["executable"]:
        raise ValueError(f"Saved sensor run cannot execute: {inspection['reason']}")
    revision = inspection["revision"]
    if revision["model"] not in {"db_estimate_v1", "physical_estimate_v1"}:
        raise ValueError("Saved LFM needs a pinned DB/physical estimate snapshot.")
    saved = sensor_simulation_db_get(revision["report_code"], root=root, run_id=run_id)
    metadata = saved["metadata"]
    if _digest(metadata["config"]) != revision["config_sha256"]:
        raise ValueError("Saved LFM config hash differs from the selected run.")
    settings = metadata.get("simulation_settings") or {}
    if settings.get("fnumber") != 4.0 or settings.get("focal_length_m") != 0.004:
        raise ValueError("Saved LFM requires saved F/4, 4 mm ideal optics.")
    scenario = sensor_simulation_db_config(
        revision["report_code"], root=root, run_id=run_id
    )["scenario"]
    if scenario.get("sensor_output_mode") != "digital_raw":
        raise ValueError("Saved LFM requires digital_raw output.")
    model = metadata["model_snapshot"]
    saved_fpn = (metadata.get("readout_profile") or {}).get("fixed_pattern") or {}
    if set(saved_fpn) != {
        "prnu_fraction", "dsnu_e", "column_prnu_fraction", "column_dsnu_e"
    } or any(
        type(value) not in (int, float) or not math.isfinite(float(value)) or value < 0
        for value in saved_fpn.values()
    ):
        raise ValueError("Saved LFM requires explicit finite FPN parameter provenance.")
    asset_identity = offline_asset_identity(workspace=workspace, source=root)
    assets = offline_asset_store(workspace=workspace, source=root)
    source_identity = json.dumps(inspection, sort_keys=True, separators=(",", ":"), allow_nan=False)
    raw_frames = []
    constant_frames = []
    integrated_arrays = []
    cases = []
    with AssetStore.bound_default(assets):
        base_scene = scene_create("uniform ee", [12, 12], asset_store=assets)
        scene_adjust_luminance(base_scene, luminance, asset_store=assets)
        on_photons = np.asarray(base_scene.data["photons"], dtype=float).copy()

        def scaled_scene(scale: float):
            return scene_set(base_scene.clone(), "photons", on_photons * scale)

        def static_capture(exposure: float, scale: float, noise: int, static_seed: int):
            camera = camera_create(asset_store=assets)
            camera_set(camera, "optics fnumber", 4.0)
            camera_set(camera, "optics focal length", 0.004)
            camera_set(camera, "ip render scale", False)
            camera_set(camera, "sensor size", list(SENSOR_SIZE))
            current = copy.deepcopy(scenario)
            current["sensor"] = {
                **current.get("sensor", {}),
                "size": list(SENSOR_SIZE),
                "integration_time": exposure,
                "noise_flag": noise,
            }
            current["sensor_resize"] = False
            result = camerae2e_run_scenario(
                current, scene=scaled_scene(scale), camera=camera,
                asset_store=assets, sensor_output_mode="digital_raw",
                include_arrays=False, seed=static_seed,
            )
            sensor = camera_get(result["camera"], "sensor")
            binding = sensor.metadata.get("sensor_simulation_db") or {}
            if (binding.get("run_id") != run_id
                    or binding.get("result_sha256") != revision["result_sha256"]
                    or sensor.fields["sensor_readout"] != model["readout"]):
                raise ValueError("Temporal setup lost the saved sensor/readout binding.")
            return result["camera"], sensor

        mean_scale = duty + (1 - duty) * low
        for exp_index, exposure in enumerate(exposures):
            frame_seed = int(np.random.SeedSequence([seed, exp_index]).generate_state(1)[0])
            high_camera, high_sensor = static_capture(exposure, 1.0, 0, frame_seed)
            _, low_sensor = static_capture(exposure, low, 0, frame_seed)
            _, constant_sensor = static_capture(exposure, mean_scale, 2, frame_seed)
            constant_raw = np.asarray(constant_sensor.data["raw_dn"])
            if constant_raw.dtype != np.dtype("int32"):
                raise ValueError("Duty-equivalent baseline did not produce integer RAW.")
            constant_frames.append(constant_raw.copy())
            high_mean = np.asarray(high_sensor.data["mean_electrons"], dtype=float)
            low_mean = np.asarray(low_sensor.data["mean_electrons"], dtype=float)
            constant_mean = np.asarray(constant_sensor.data["mean_electrons"], dtype=float)
            linear_midpoint = low_mean + (high_mean - low_mean) * duty
            linearity_error = float(
                np.linalg.norm(constant_mean - linear_midpoint)
                / max(np.linalg.norm(constant_mean), 1e-12)
            )
            if not math.isfinite(linearity_error) or linearity_error > MAX_LINEARITY_ERROR:
                raise ValueError(
                    "Saved response is not linear across the authored PWM on/off levels."
                )
            capture_sensor = camera_get(high_camera, "sensor").clone()
            if any(name in capture_sensor.fields for name in (
                "offset_fpn_image", "gain_fpn_image",
                "column_offset_fpn", "column_gain_fpn",
            )):
                raise ValueError("Saved LFM expects model-generated virtual-window FPN maps.")
            capture_sensor.fields["noise_flag"] = 2
            phase_frames = []
            phase_integrated = []
            phase_cases = []
            for fraction in PHASE_FRACTIONS:
                phase = fraction * period
                starts = np.arange(SENSOR_SIZE[0], dtype=float) * LINE_TIME_S
                fractions = _on_fraction(starts, exposure, period, duty, phase)
                integrated = low_mean + (high_mean - low_mean) * fractions[:, None]
                selected = capture_sensor.clone()
                selected.data["mean_electrons"] = integrated.copy()
                sensor = resample_sensor_capture(selected, seed=frame_seed)
                binding = sensor.metadata.get("sensor_simulation_db") or {}
                if (binding.get("run_id") != run_id
                        or binding.get("result_sha256") != revision["result_sha256"]
                        or sensor.fields["sensor_readout"] != model["readout"]):
                    raise ValueError("Temporal capture lost the saved sensor/readout binding.")
                raw = np.asarray(sensor.data["raw_dn"])
                mean = np.asarray(sensor.data["mean_electrons"], dtype=float)
                if not np.array_equal(mean, integrated):
                    raise ValueError("PWM readout changed analytically integrated photoelectrons.")
                if raw.dtype != np.dtype("int32") or raw.shape != constant_raw.shape:
                    raise ValueError("Temporal PWM did not produce matching integer RAW.")
                phase_frames.append(raw.copy())
                phase_integrated.append(mean.copy())
                phase_cases.append({
                    "phase_fraction_of_period": fraction,
                    "phase_s": phase,
                    "noise_seed": frame_seed,
                    "raw_sha256": hashlib.sha256(raw.tobytes()).hexdigest(),
                    "mean_raw_dn": float(np.mean(raw)),
                    "row_mean_dn_range": float(np.ptp(np.mean(raw, axis=1))),
                    "row_timing": {
                        "schema": "camerae2e_analytic_pwm_row_timing_v1",
                        "frame_start_s": 0.0,
                        "rolling_shutter": True,
                        "line_time_s": LINE_TIME_S,
                        "row_exposure_start_s": starts.tolist(),
                        "row_exposure_end_s": (starts + exposure).tolist(),
                        "ideal_pwm_on_fraction_per_row": fractions.tolist(),
                        "method": "exact_piecewise_constant_row_overlap",
                    },
                    "sensor_capture": sensor.metadata["sensor_capture"],
                })
            raw_frames.append(np.stack(phase_frames))
            integrated_arrays.append(np.stack(phase_integrated))
            black = float(model["readout"]["black_level_dn"])
            modulation = _phase_modulation(
                phase_cases, constant_sensor.metadata["sensor_capture"], black
            )
            cases.append({
                "exposure_s": exposure,
                "source_binding": {
                    "run_id": revision["run_id"],
                    "report_code": revision["report_code"],
                    "result_sha256": revision["result_sha256"],
                    "config_sha256": revision["config_sha256"],
                },
                "integration_method": "analytic_endpoint_temporal_integration",
                "endpoint_linearity_relative_l2_error": linearity_error,
                "constant_duty_equivalent_scale": mean_scale,
                "constant_raw_sha256": hashlib.sha256(constant_raw.tobytes()).hexdigest(),
                "constant_mean_raw_dn": float(np.mean(constant_raw)),
                **modulation,
                "maximum_row_banding_dn_range": max(
                    item["row_mean_dn_range"] for item in phase_cases
                ),
                "phases": phase_cases,
            })
    if json.dumps(inspect_sensor_revision(identifier, root=root, run_id=run_id),
                  sort_keys=True, separators=(",", ":"), allow_nan=False) != source_identity:
        raise ValueError("Saved sensor source changed during PWM sweep.")
    if offline_asset_identity(workspace=workspace, source=root) != asset_identity:
        raise ValueError("Pinned upstream assets changed during PWM sweep.")
    destination.mkdir(parents=True, exist_ok=True)
    npz = destination / "saved-sensor-lfm.npz"
    np.savez_compressed(
        npz,
        original_pwm_raw_dn=np.stack(raw_frames),
        original_constant_raw_dn=np.stack(constant_frames),
        integrated_mean_electrons=np.stack(integrated_arrays),
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
            "saved_fpn_parameters_applied_to_generated_virtual_window_map": True,
            "saved_fpn_parameter_provenance": "saved_simulation_profile_assumptions",
            "sensor_readout_profile": inspection["readout"]["profile"],
            "sensor_readout_classification": inspection["readout"]["classification"],
            "measured_fpn_map_reused": False,
            "fpn_physical_validation": "not_established",
            "lens_model": "shared_ideal_f4_4mm_not_catalog_lens_revision",
            "physical_validation": "not_established",
        },
        "controls": {
            "exposure_s": exposures,
            "pwm_period_s": period,
            "duty_cycle": duty,
            "off_to_on_ratio": low,
            "on_luminance_cd_m2": luminance,
            "phase_fractions_of_period": list(PHASE_FRACTIONS),
            "sensor_size": list(SENSOR_SIZE),
            "rolling_shutter": True,
            "line_time_s": LINE_TIME_S,
            "timing_provenance": "authored 20 us/line assumption, not saved sensor timing",
            "crop_provenance": (
                "authored 8x8 virtual sensor window; location in saved native array unknown"
            ),
            "frame_start_s": 0.0,
            "maximum_pwm_cycles_per_exposure": MAX_PWM_CYCLES_PER_EXPOSURE,
            "seed": seed,
            "noise_seed_policy": (
                "same seed within each exposure across four phases and constant-light "
                "reference for paired sensitivity; these are not independent repeat captures"
            ),
            "saved_readout": model["readout"],
            "saved_fixed_pattern": saved_fpn,
            "fixed_pattern_seed": capture_sensor.fields.get("fixed_pattern_seed", 0),
        },
        "scene_contract": {
            "api": "pyisetcam.scene_create('uniform ee')",
            "illumination": "on/off scalar modulation of one static spectral scene",
            "pwm_waveform": "ideal discontinuous periodic rectangular illumination",
            "spatial_motion": False,
            "spectrum": "uniform equal-energy (EE), not D65",
            "integration_method": "analytic_endpoint_temporal_integration",
            "computation": (
                "pyisetcam runs the saved spectral sensor and ideal optics at on, off, "
                "and duty-equivalent constant light. Exact PWM row overlap mixes only "
                "the validated linear endpoint mean-electron images before each "
                "pyisetcam resample_sensor_capture readout. The temporal sampling engine "
                "is not run for each phase."
            ),
        },
        "cases": cases,
        "linearity_check": {
            "independent_point": "separately computed duty-equivalent constant illumination",
            "equation": "mean_midpoint == low_mean + duty*(high_mean-low_mean)",
            "relative_l2_error_by_exposure": [
                case["endpoint_linearity_relative_l2_error"] for case in cases
            ],
            "maximum_accepted_relative_l2_error": MAX_LINEARITY_ERROR,
            "status": "passed",
        },
        "comparison": {
            "metric": "phase_modulation_fraction_black_relative",
            "definition": (
                "(max_phase_mean_raw_dn-min_phase_mean_raw_dn)/"
                "(mean_phase_raw_dn-black_dn)"
            ),
            "values_by_exposure": [
                case["phase_modulation_fraction_black_relative"] for case in cases
            ],
            "absolute_dn_range_by_exposure": [case["phase_mean_dn_range"] for case in cases],
            "eligibility": (
                "ratio unavailable if black-relative mean <=1 DN or any PWM/constant "
                "well, ADC-white or ADC-zero rail fraction exceeds 0.001"
            ),
            "smaller_means": (
                "for eligible cases only, less modeled phase-dependent RAW variation "
                "for this authored PWM"
            ),
            "automatic_product_selection": False,
        },
        "artifacts": {"npz": npz.name, "npz_sha256": _sha(npz)},
        "verification": {
            "implementation_verification": "runtime_completed",
            "endpoint_linearity_check": "passed",
            "cross_tool_comparison": "not_performed",
            "physical_validation": "not_established",
            "hdr_fusion_performed": False,
        },
        "limitation": (
            "This is a finite phase sweep under an authored ideal square-wave light and "
            "fixed camera settings. One seed is reused across phases for paired sensitivity, "
            "so phase outcomes are not independent captures. The 20 us row timing and 8x8 "
            "virtual sensor window are authored assumptions, not saved sensor specifications. "
            "Saved FPN parameter assumptions generate fixed maps on this virtual window; "
            "no measured FPN map is reused or physically validated. Exact two-level row "
            "overlap follows a separate midpoint linearity check. "
            "It is a model sensitivity diagnostic, "
            "not physical LED flicker mitigation, immunity across untested phases, "
            "closed-loop AE, rolling vendor HDR, or measured sensor performance."
        ),
    }
    json_path = destination / "saved-sensor-lfm.json"
    json_path.write_text(json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return {**report, "artifacts": {"json": str(json_path), "npz": str(npz)},
            "artifact_sha256": {"json": _sha(json_path), "npz": _sha(npz)}}
