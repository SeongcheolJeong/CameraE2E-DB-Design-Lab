"""Static-scene shared-charge acquisitions with an explicit two-readout axis.

Recorded control frames identify independently reset acquisitions. Each one
integrates light only once and reads that charge through the declared two paths.
This behavioral model is not a hardware DCG or physical-validation claim.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import numpy as np

from camera_platform import dcg, dcg_electrical, electrical, hdr
from camera_platform.runtime import (
    CHANNELS,
    RuntimeRequestError,
    _issue,
    charge_collection_capabilities,
    with_sensor_effects,
)
from camera_platform.spatial import (
    MAX_FIELD_FFT_WORK,
    SOURCE_RI_MODE,
    _field_psf_arrays,
    _native_response_arrays,
    _native_response_summary,
    _native_response_units,
    _psf_summary,
    _radial_response_arrays,
    _radial_response_summary,
    _source_ri_summary,
)
from camera_platform.temporal import (
    MAX_NATIVE_DIRECTION_WORK,
    MAX_UNIQUE_SPECTRAL_SAMPLES,
    TEMPORAL_EFFECTS,
    TemporalSceneRuntime,
    _collection_evidence_count,
    _collection_geometry_summary,
    _collection_support_shape,
    _integration_weights,
    _scene_hash,
)
from camera_platform.temporal import _parse as _parse_temporal
from camera_platform.timing import MAX_FRAME_PIXELS, MAX_FRAMES, MAX_KNOTS, summarize_timing

PLUGIN_ID = "shared_charge_dcg_v1"
PROGRAM_SCHEMA = "camera_dcg_capture_program_v1"
NONLINEAR_PROGRAM_SCHEMA = "camera_dcg_capture_program_v2"
NONLINEAR_OBSERVATION = "sampled_nonlinear_shared_charge_raw_black_relative_v1"
MAX_WORKING_ARRAY_BYTES = 1024 * 1024 * 1024
MAX_OUTPUT_ARRAY_BYTES = 128 * 1024 * 1024
READOUT_IDS = ["HCG", "LCG"]
CAPTURE_EFFECTS = [*TEMPORAL_EFFECTS, "shared_charge_two_path_readout", "raw_dcg_covariance_merge"]


def validate_capture_program(payload: Any) -> dict[str, Any]:
    """Validate a separate program; null merge retains two original RAW paths."""
    if not isinstance(payload, Mapping):
        raise ValueError("DCG capture program requires an object")
    nonlinear = payload.get("schema") == NONLINEAR_PROGRAM_SCHEMA
    required = {"schema", "electrical_model", "merge_profile"}
    if nonlinear:
        required.add("preview")
    if set(payload) != required:
        raise ValueError("DCG program fields differ from its declared schema")
    if (not isinstance(payload["schema"], str)
            or payload["schema"] not in {PROGRAM_SCHEMA, NONLINEAR_PROGRAM_SCHEMA}):
        raise ValueError("Unsupported DCG capture program schema")
    if nonlinear:
        from . import nonlinear_dcg
        from .hdr_runtime import validate_preview

        validate_model = nonlinear_dcg.validate_model
        validate_merge = nonlinear_dcg.validate_profile
    else:
        validate_model = dcg_electrical.validate_model
        validate_merge = dcg.validate_profile
    result = {
        "schema": payload["schema"],
        "electrical_model": validate_model(payload["electrical_model"]),
        "merge_profile": None if payload["merge_profile"] is None
        else validate_merge(payload["merge_profile"]),
    }
    if nonlinear:
        result["preview"] = validate_preview(payload["preview"])
    return result


def default_capture_program(legacy_readout):
    """Explicit illustrative behavioral model, not a measured device preset."""
    return validate_capture_program(
        {
            "schema": PROGRAM_SCHEMA,
            "electrical_model": dcg_electrical.default_model(legacy_readout),
            "merge_profile": dcg.default_profile(),
        }
    )


def capture_effects(program):
    selected = validate_capture_program(program)
    effects = [*TEMPORAL_EFFECTS, "shared_charge_two_path_readout"]
    if selected["schema"] == NONLINEAR_PROGRAM_SCHEMA:
        effects.append("nonlinear_charge_voltage_response")
    if selected["merge_profile"] is not None:
        effects.append("raw_dcg_covariance_merge")
    return effects


def observation_effects(program):
    """Stages affecting the sampled RAW metric, before the optional merge."""
    return [effect for effect in capture_effects(program) if effect != "raw_dcg_covariance_merge"]


def _raw_formats(model):
    return [
        {
            "cfa": "RGGB",
            "bit_depth": path["adc"]["bit_depth"],
            "black_level_dn": path["adc"]["black_level_dn"],
            "response_type": (
                "nonlinear_charge_voltage" if model["schema"] == "camera_shared_charge_readout_v2"
                else "linear"
            ),
            "packing": "unpacked",
            "layout": "bayer",
            "hdr_streams": 1,
        }
        for path in model["paths"]
    ]


def _resources(parsed):
    base = parsed["base"]
    rows, cols = base["size"]
    pixels = int(rows * cols)
    source_pixels = int(np.prod(_collection_support_shape(base)))
    acquisitions, scenes = len(parsed["control"]["frames"]), len(parsed["unique"])
    sites = len(base.get("channels", CHANNELS))
    rate_bytes = scenes * pixels * sites * 8
    collection_bytes = (scenes * source_pixels * _collection_evidence_count(base) * 8
                        if "charge_collection" in base else 0)
    weight_bytes = acquisitions * scenes * rows * 8
    evidence = _native_response_arrays(base)
    full_pixels = int(np.prod(base["geometry"]["full_sensor_shape"]))
    # Includes retained + stacking copies of both paths, read covariance/merge
    # intermediates, separate ISP processing, and full-native fixed-pattern maps.
    working = (
        parsed["unique_spectral_samples"] * 8
        + rate_bytes + collection_bytes * 2
        + acquisitions * pixels * 1024
        + base["image"].nbytes * 8
        + full_pixels * 48
        + weight_bytes * 2
        + sum(a.nbytes for a in evidence.values()) * 2
    )
    source_ri_map_bytes = 0
    if base["field_radiometry_mode"] == SOURCE_RI_MODE:
        # Every static knot shares one source grid, but its irradiance is
        # weighted independently before the PSF and two-path readout.
        source_ri_map_bytes = (
            base["source_subcells"].size if base["source_subcells"] is not None
            else base["image"].size
        ) * 8
        working += base["source_ri_extra_bytes"] + source_ri_map_bytes
    result = {
        "cpu_threads": 1,
        "gpu_required": False,
        "execution": "in_process",
        "working_array_bytes_estimate": int(working),
        "estimate_kind": "conservative_array_budget_not_measured_peak",
        "acquisitions": acquisitions,
        "readouts_per_acquisition": 2,
        "readouts": acquisitions * 2,
        "unique_scenes": scenes,
        "knots": len(parsed["timeline"]["knots"]),
        "output_size": list(base["size"]),
        "unique_spectral_samples": parsed["unique_spectral_samples"],
        "automatic_coarsening": False,
    }
    if source_ri_map_bytes:
        result["source_position_ri_map_bytes"] = source_ri_map_bytes
        result["source_position_ri_unique_scenes"] = scenes
    if base["field_psf"] is not None:
        result["working_array_bytes_estimate"] += (
            base["field_psf"]["working_array_bytes_estimate"] + base["kernels"].nbytes
        )
        result["field_psf_fft_work_units"] = scenes * base["field_psf"]["fft_work_units"]
    if "native_direction_work" in base:
        result["native_direction_weighted_samples"] = scenes * base["native_direction_work"]
    largest = max(
        [
            rate_bytes,
            weight_bytes,
            scenes * source_pixels * 8 if collection_bytes else 0,
            acquisitions * pixels * max(48, sites * 8),
            source_ri_map_bytes,
            *(a.nbytes for a in evidence.values()),
        ]
    )
    if largest > MAX_OUTPUT_ARRAY_BYTES or working > MAX_WORKING_ARRAY_BYTES:
        raise ValueError("DCG capture exceeds bounded working/output array memory")
    if result["working_array_bytes_estimate"] > MAX_WORKING_ARRAY_BYTES:
        raise ValueError("DCG field optics exceed bounded working array memory")
    return result


def _parse(request):
    if not isinstance(request, Mapping) or "capture_program" not in request:
        raise ValueError("DCG capture requires an explicit capture_program")
    program = validate_capture_program(request["capture_program"])
    parsed = _parse_temporal(
        {k: v for k, v in request.items() if k != "capture_program"},
        allow_source_ri=True,
    )
    parsed["program"] = program
    base, control, model = parsed["base"], parsed["control"], program["electrical_model"]
    if "charge_collection" in base and model["fixed_pattern"]["prnu_sigma"] != 0:
        raise ValueError("Charge collection requires PRNU=0; source-generation PRNU is not modeled")
    if any(frame["applied"]["gain"] != 1 for frame in control["frames"]):
        raise ValueError("DCG applied acquisition gain must be 1; path gains are fixed in program")
    temperature = (
        model["reference_temperature_c"] if base["temperature"] is None else base["temperature"]
    )
    validate_conditions = dcg_electrical.validate_capture_conditions
    if program["schema"] == NONLINEAR_PROGRAM_SCHEMA:
        from .nonlinear_dcg import validate_capture_conditions as validate_conditions
    for frame in control["frames"]:
        validate_conditions(
            model, frame["applied"]["exposure_ns"] * 1e-9, temperature
        )
    if any(path["adc"]["bit_depth"] != base["readout"]["bit_depth"] for path in model["paths"]):
        raise ValueError("DCG path ADC bit depth differs from the fixed sensor RAW format")
    if int(np.prod(base["geometry"]["full_sensor_shape"])) > dcg_electrical.MAX_NATIVE_PIXELS:
        raise ValueError("DCG fixed patterns exceed the bounded full native sensor domain")
    if int(np.prod(base["size"])) > dcg_electrical.MAX_PIXELS:
        raise ValueError("DCG acquisition exceeds its native pixel budget")
    if base["seed"] + len(control["frames"]) - 1 > 2**32 - 1:
        raise ValueError("Per-acquisition noise seed exceeds the uint32 domain")
    # Shared-charge paths use one seeded native dark map. Its DSNU tail can
    # overflow after temperature scaling even when the scalar dark mean fits.
    electrical._preflight_seeded_dark_charge(
        model, base["geometry"], base["size"], temperature,
        max(frame["applied"]["exposure_ns"] for frame in control["frames"]) / 1e9,
    )
    source_ri = base["field_radiometry_mode"] == SOURCE_RI_MODE
    identities = {
        _scene_hash(
            {key: value for key, value in item["scene"].items()
             if key not in {"source", "source_subcell_evidence"}},
            np.asarray(item["scene"]["spectral_irradiance"], dtype=float),
            source_ri=source_ri,
        )
        for item in parsed["unique"]
    }
    if len(identities) != 1:
        raise ValueError(
            "DCG requires one static unique optical image; no waveform or motion model"
        )
    parsed["resources"] = _resources(parsed)
    return parsed


def _linear_preview(rate, valid, geometry, profile):
    """Reuse the unchanged HDR float-display math, never its independent fusion."""
    geom, pattern = hdr._geometry(geometry, rate.shape)
    selected = hdr.validate_profile(profile)
    linear, rgb_valid, preview = hdr._rgb(rate, valid, pattern, selected)
    return {
        "merged_linear_rgb": linear,
        "merged_rgb_valid_mask": rgb_valid,
        "merged_rgb": preview,
    }, {
        "profile": selected,
        "geometry": geom,
        "scope": "display only; no RAW re-quantization and no HDR bracket fusion",
        "rgb_validity": "all nine reflected 3x3 Bayer samples valid",
        "negative_linear_values": "preserved; only preview clamps negative values",
        "physical_colorimetry": "not_established",
    }


class SharedChargeDCGRuntime:
    """One charge integration and two declared gain paths per acquisition."""

    plugin_id = PLUGIN_ID

    def describe_capabilities(self):
        return {
            "plugin_id": self.plugin_id,
            "role": "runtime",
            "version": "1.0",
            "mode": "shared_charge_dcg",
            "available": True,
            "supports": [
                "static_spectral_image",
                "native_rolling_or_global_exposure",
                "shared_charge_two_gain_paths",
                "declared_read_noise_covariance",
                "separate_original_integer_raw_streams",
                "raw_only_covariance_merge",
                "native_four_site_absolute_response",
                "source_position_relative_illumination_opt_in",
                "per_path_configured_isp",
            ],
            "unsupported": [
                "motion",
                "waveform",
                "independent_bracket_fusion",
                "lofic",
                "charge_transfer_circuit",
                "closed_loop_ae_awb",
                "raw_replay",
                "physical_validation",
                "vendor_dcg_equivalence",
            ],
            "limits": {
                "max_acquisitions": MAX_FRAMES,
                "readouts_per_acquisition": 2,
                "max_knots": MAX_KNOTS,
                "max_frame_pixels": MAX_FRAME_PIXELS,
                "max_unique_spectral_samples": MAX_UNIQUE_SPECTRAL_SAMPLES,
                "max_full_native_pixels": dcg_electrical.MAX_NATIVE_PIXELS,
                "max_field_fft_work_units": MAX_FIELD_FFT_WORK,
                "max_native_direction_weighted_samples": MAX_NATIVE_DIRECTION_WORK,
                "max_working_array_bytes": MAX_WORKING_ARRAY_BYTES,
                "max_output_array_bytes": MAX_OUTPUT_ARRAY_BYTES,
            },
            "charge_collection": charge_collection_capabilities(),
            "physical_validation": "not_established",
        }

    def validate_request(self, request):
        try:
            _parse(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return getattr(exc, "issues", [_issue("request", str(exc))])
        return []

    def _checked(self, request):
        try:
            return _parse(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise RuntimeRequestError(
                getattr(exc, "issues", [_issue("request", str(exc))])
            ) from exc

    def estimate_resources(self, request):
        return self._checked(request)["resources"]

    def execute(self, request):
        parsed = self._checked(request)
        try:
            return self._execute(parsed)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise RuntimeRequestError(
                getattr(exc, "issues", [_issue("request", str(exc))])
            ) from exc

    def _execute(self, parsed):
        from camera_platform.isp import process_raw

        base, control, program = parsed["base"], parsed["control"], parsed["program"]
        frames, model = control["frames"], program["electrical_model"]
        nonlinear = program["schema"] == NONLINEAR_PROGRAM_SCHEMA
        capture_shared = dcg_electrical.capture_shared_electrons
        merge_shared = dcg.merge_shared_charge_raw
        if nonlinear:
            from . import nonlinear_dcg
            from .nonlinear_isp import process_raw as process_nonlinear_raw

            capture_shared = nonlinear_dcg.capture_shared_electrons
            merge_shared = nonlinear_dcg.merge_shared_charge_raw
        sites = list(base.get("channels", CHANNELS))
        weights, windows = _integration_weights(parsed)
        rates, flux, radius, cfa, ri_map = TemporalSceneRuntime._optical_rates(parsed)
        with np.errstate(over="ignore", invalid="ignore"):
            channels = np.einsum("asr,srwc->arwc", weights, rates, optimize=False)
        if (
            not np.isfinite(channels).all()
            or np.any(channels < 0)
            or np.any(channels > dcg_electrical.MAX_CHARGE_E)
        ):
            raise ValueError("Integrated photocharge exceeds the finite electron domain")
        means = np.take_along_axis(channels, cfa[None, ..., None], axis=-1)[..., 0]
        exposures = [frame["applied"]["exposure_ns"] * 1e-9 for frame in frames]
        temperature = (
            model["reference_temperature_c"] if base["temperature"] is None else base["temperature"]
        )
        formats = _raw_formats(model)
        geometry = {key: base["geometry"][key] for key in ("full_sensor_shape", "crop_origin")}
        captured, merged_arrays, path_arrays = {}, {}, {}
        path_array_units = {}
        metadata, calibrations, isp_summaries, merge_summaries = [], [], [], []
        path_previews, predictions, conditions, display_summaries = [], [], [], []
        preview_profile = hdr.default_profile()
        for index, frame in enumerate(frames):
            acquisition_id, applied = frame["frame_id"], frame["applied"]
            capture = capture_shared(
                means[index],
                exposures[index],
                model,
                temperature_c=temperature,
                geometry=geometry,
                noise_enabled=base["noise_enabled"],
                seed=base["seed"] + index,
            )
            raw, calibration = capture["arrays"]["raw_dn"], capture["calibration"]
            calibrations.append(calibration)
            metadata.append({"acquisition_id": acquisition_id, **capture["summary"]})
            for key, array in capture["arrays"].items():
                captured.setdefault(key, []).append(array)
            profile = deepcopy(parsed["profiles"][applied["isp_model_id"]])
            if "wb_gains_rgb" in applied:
                profile["white_balance"]["gains_rgb"] = applied["wb_gains_rgb"]
            previews, per_path_isp, sampled = [], [], []
            acquisition_path_arrays = {}
            for path_index, fmt in enumerate(formats):
                cal = (nonlinear_dcg.calibration_for_path(calibration, path_index)
                       if nonlinear else calibration["paths"][path_index])
                processed = (
                    process_nonlinear_raw(raw[path_index], cal, profile, geometry=base["geometry"])
                    if nonlinear else process_raw(
                        raw[path_index], fmt, profile, geometry=base["geometry"])
                )
                previews.append(processed["arrays"]["rgb"])
                if nonlinear:
                    for key, array in processed["arrays"].items():
                        if key not in {"rgb", "rgb_frames"}:
                            name = "path_" + key.removesuffix("_frames") + "_groups"
                            acquisition_path_arrays.setdefault(name, []).append(array[0])
                            path_array_units[name] = processed["summary"]["array_units"][key]
                per_path_isp.append(
                    {
                        "readout_id": READOUT_IDS[path_index],
                        "isp_model_id": applied["isp_model_id"],
                        **processed["summary"],
                    }
                )
                corrected = raw[path_index].astype(float) - fmt["black_level_dn"]
                if not nonlinear:
                    corrected -= calibration["dark_e"] * cal["dn_per_e"]
                sampled.append(
                    [float(corrected[cfa == channel].mean()) for channel in range(len(sites))]
                )
            if not np.isfinite(sampled).all():
                raise ValueError("Sampled DCG RAW prediction must remain finite")
            path_previews.append(np.stack(previews))
            for key, values in acquisition_path_arrays.items():
                path_arrays.setdefault(key, []).append(np.stack(values))
            predictions.append(sampled)
            isp_summaries.append({"acquisition_id": acquisition_id, "paths": per_path_isp})
            conditions.append(
                {
                    "acquisition_id": acquisition_id,
                    "conditions": {
                        "exposure_s": {"value": exposures[index], "unit": "s"},
                        "gain": {"value": 1, "unit": "1"},
                        "temperature_c": {"value": temperature, "unit": "degC"},
                    },
                }
            )
            if program["merge_profile"] is not None:
                # Only actual codes + declared calibration: no simulator truth arrays.
                merged = merge_shared(raw, calibration, program["merge_profile"])
                merge_summaries.append({"acquisition_id": acquisition_id, **merged["summary"]})
                display, display_summary = _linear_preview(
                    merged["arrays"]["merged_rate"],
                    merged["arrays"]["valid_mask"],
                    base["geometry"],
                    preview_profile,
                )
                display_summaries.append({"acquisition_id": acquisition_id, **display_summary})
                for key, array in {**merged["arrays"], **display}.items():
                    merged_arrays.setdefault(key, []).append(array)
        acquisitions = [frame["frame_id"] for frame in frames]
        units = metadata[0]["array_units"]
        arrays = {
            "channel_photoelectron_rate_scenes": rates,
            "row_scene_integration_weights_s": weights,
            "channel_electrons": channels,
            "mean_electrons": means,
            "raw_groups": np.stack(captured.pop("raw_dn")),
            "rgb_groups": np.stack(path_previews),
            **{key: np.stack(values) for key, values in path_arrays.items()},
            **{"electrical_" + key: np.stack(value) for key, value in captured.items()},
            "calibration_dark_e": np.stack([cal["dark_e"] for cal in calibrations]),
            "radius_map": radius,
            "cfa_channel_index": cfa,
            "relative_illumination_map": ri_map,
            "psf_intensity": base["kernels"].copy(),
            **_field_psf_arrays(base),
            **_radial_response_arrays(base),
            **_native_response_arrays(base),
            **parsed.get("collection_rate_arrays", {}),
            "row_start_ns": np.array([w["row_start_ns"] for w in windows], dtype=np.int64),
            "row_end_ns": np.array([w["row_end_ns"] for w in windows], dtype=np.int64),
            "acquisition_start_ns": np.array([f["frame_start_ns"] for f in frames], dtype=np.int64),
            **{key: np.stack(value) for key, value in merged_arrays.items()},
        }
        if base["field_radiometry_mode"] == SOURCE_RI_MODE:
            arrays["source_relative_illumination_map"] = parsed["source_ri_map"].copy()
        merge_applied = program["merge_profile"] is not None
        arrays["rgb"] = (
            arrays["merged_rgb"][0] if merge_applied else arrays["rgb_groups"][0, 0]
        ).copy()
        array_units = {
            "channel_photoelectron_rate_scenes": "electron/pixel/s",
            "row_scene_integration_weights_s": "s",
            "channel_electrons": "electron/pixel",
            "mean_electrons": "electron/pixel",
            "raw_groups": "DN",
            "rgb_groups": "1",
            "rgb": "1",
            **path_array_units,
            **{"electrical_" + key: units[key] for key in captured},
            "calibration_dark_e": "electron/pixel",
            "radius_map": "1",
            "cfa_channel_index": "index",
            "relative_illumination_map": "1",
            "psf_intensity": "1",
            "row_start_ns": "ns",
            "row_end_ns": "ns",
            "acquisition_start_ns": "ns",
            **{name: "mm" for name in _field_psf_arrays(base)},
            **_native_response_units(base),
            **{key: "electron/pixel/s" for key in parsed.get("collection_rate_arrays", {})},
        }
        if base["field_radiometry_mode"] == SOURCE_RI_MODE:
            array_units["source_relative_illumination_map"] = "1"
        if "field_reference_radius_mm" in base:
            array_units.update(sensor_radius_mm="mm", response_radius_map="1")
        if merge_applied:
            array_units.update(merge_summaries[0]["array_units"])
            array_units.update(
                merged_linear_rgb="electron/pixel/s",
                merged_rgb="1",
                merged_rgb_valid_mask="boolean",
            )
        if any(array.nbytes > MAX_OUTPUT_ARRAY_BYTES for array in arrays.values()):
            raise ValueError("DCG output exceeds the per-array storage bound")
        timing = summarize_timing(control, geometry=base["geometry"], frame_shape=base["size"])
        effects = with_sensor_effects(capture_effects(program), base)
        summary = {
            "schema_version": "1.0",
            "plugin_id": self.plugin_id,
            "source_kind": "simulated",
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "engine": "spectral PSF/RI/QE + integrated shared charge + two RAW readouts",
            "channels": sites,
            "wavelength_nm": base["wave"].tolist(),
            "output_size": list(base["size"]),
            "acquisition_count": len(frames),
            "acquisition_ids": acquisitions,
            "readout_ids": READOUT_IDS.copy(),
            "readouts_per_acquisition": 2,
            "readout_count": 2 * len(frames),
            "raw_axes": ["acquisition", "readout", "row", "column"],
            "acquisitions": [
                {
                    "acquisition_id": f["frame_id"],
                    "acquisition_index": i,
                    "shared_charge_group_id": f["frame_id"],
                    "recorded_frame": deepcopy(f),
                    "exposure_s": exposures[i],
                    "readout_ids": READOUT_IDS.copy(),
                    "readouts": [{"stream_id": stream} for stream in READOUT_IDS],
                    "timing": timing["frames"][i],
                }
                for i, f in enumerate(frames)
            ],
            "geometry": base["geometry"],
            **_collection_geometry_summary(base, parsed.get("collection_rate_arrays")),
            "pixel_pitch_um": base["pitch"],
            **_source_ri_summary(base),
            **({"source_relative_illumination_map_scope": "same_fixed_source_grid_all_knots"}
               if base["field_radiometry_mode"] == SOURCE_RI_MODE else {}),
            **_radial_response_summary(base),
            **_native_response_summary(base),
            "capture_program": program,
            "control_sequence": control,
            "timing": timing,
            "clock": control["clock"],
            "coverage": parsed["coverage"],
            "raw_formats": formats,
            "conditions_per_acquisition": conditions,
            "included_effects": effects,
            "capture_metadata": metadata,
            "calibrations": [
                {
                    **{k: v for k, v in cal.items() if k != "dark_e"},
                    "dark_e_array": "calibration_dark_e",
                    "acquisition_index": i,
                }
                for i, cal in enumerate(calibrations)
            ],
            "seed_sequence": [base["seed"] + i for i in range(len(frames))],
            "noise_enabled": base["noise_enabled"],
            "temperature_basis": "electrical_model_reference_temperature"
            if base["temperature"] is None
            else "request_temperature",
            "isp_per_acquisition": isp_summaries,
            "array_units": array_units,
            "sampled_raw_dark_corrected_channel_mean_dn": predictions,
            "prediction": {
                "metric_id": "raw.channel_mean_dn_shared_charge",
                "unit": "DN",
                "values": np.asarray(predictions).reshape(-1).tolist(),
                "axes": [
                    {"name": "acquisition", "unit": "1", "values": acquisitions},
                    {"name": "readout", "unit": "1", "values": READOUT_IDS.copy()},
                    {"name": "channel", "unit": "1", "values": sites.copy()},
                ],
                "reference_plane": "sensor_raw",
                "included_effects": with_sensor_effects(observation_effects(program), base),
                "conditions": {},
                "conditions_per_acquisition": conditions,
                "raw_formats": deepcopy(formats),
                "source_kind": "simulated",
                "method_and_version": "sampled_shared_charge_raw_dark_corrected_v1",
                "uncertainty": {"kind": "unknown"},
            },
            "simulation_oracle": {
                "arrays": [
                    "mean_electrons",
                    "channel_electrons",
                    *["electrical_" + key for key in captured if key != "adc_dn"],
                ],
                "used_by_merge": False,
                "used_by_prediction": False,
            },
            "dcg_merge": merge_summaries if merge_applied else None,
            "merge_applied": merge_applied,
            "merged_preview": display_summaries if merge_applied else None,
            "main_preview": "merged_rgb[0]" if merge_applied else "rgb_groups[0,0]",
            "scene_interpolation": parsed["timeline"]["interpolation"],
            "scene_knots": [
                {
                    "time_ns": k["time_ns"],
                    "scene_index": i,
                    "scene_content_hash": parsed["scene_hashes"][i],
                }
                for k, i in zip(parsed["timeline"]["knots"], parsed["scene_indices"], strict=True)
            ],
            "scene_sources": [item["scene"]["source"] for item in parsed["unique"]],
            "psf_source": base["psf_source"],
            "flux_diagnostics_per_scene": flux,
            "integration": "existing analytic static-scene temporal basis before shared charge",
            "limitations": [
                "Ideal nondestructive two paths read one charge; not vendor DCG or LOFIC.",
                "Acquisitions reset independently; paths within one acquisition share shot noise.",
                "No waveform, moving scene, charge transfer, closed loop or legacy RAW replay.",
                "Applied acquisition gain must equal one; model path gains are fixed.",
                "RAW censor guards are estimates, not clipping truth or recovery guarantees.",
                "Read covariance and rounding bounds do not establish total uncertainty.",
                "Path previews use configured ISP; float preview uses declared display tuning.",
                "No physical validation, hardware equivalence or colorimetry is established.",
            ],
        }
        if nonlinear:
            summary["sampled_raw_black_relative_channel_mean_dn"] = summary.pop(
                "sampled_raw_dark_corrected_channel_mean_dn")
            summary["prediction"]["method_and_version"] = NONLINEAR_OBSERVATION
            summary["prediction"]["dark_correction"] = "not_applied_to_raw_code_metric"
            summary["preview_contract"] = deepcopy(program["preview"])
            summary["isp_profile_role"] = "fixed_color_settings_only"
            summary["limitations"] = [
                item for item in summary["limitations"]
                if item != ("Path previews use configured ISP; float preview "
                            "uses declared display tuning.")
            ] + ["Path previews decode RAW and use fixed ISP color settings only; "
                 "they do not execute the full configured ISP graph.",
                 "Observed RAW metric subtracts black only; nonlinear dark response remains."]
        if base["field_radiometry_mode"] == SOURCE_RI_MODE:
            summary["prediction"]["method_and_version"] += "_source_ri_v1"
            summary["limitations"].append(
                "Fixed source-position RI precedes the conditional unit-sum PSF; "
                "receiver angular QE and electrical PRNU remain separable approximations."
            )
        if base["field_psf"] is not None:
            summary["psf"] = _psf_summary(base)
        return {"arrays": arrays, "summary": summary}
