"""Sequential exposure integration, electrical RAW capture and optional RAW HDR.

The legacy light waveform uniformly modulates one declared sensor-surface image.
An optional second, separately pinned image timeline remains unmodulated;
neither component is inferred from a combined image. All frame timing is
validated together before capture.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import numpy as np

from camera_platform import electrical, hdr
from camera_platform.periodic_light import (
    integrate_linear_basis,
    integrate_waveform_ns,
    phase_envelope_ns,
    validate_waveform,
)
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
    _scene_hash,
)
from camera_platform.temporal import _parse as _parse_temporal
from camera_platform.timing import (
    MAX_FRAME_PIXELS,
    MAX_FRAMES,
    MAX_KNOTS,
    row_windows,
    summarize_timing,
)

PLUGIN_ID = "hdr_bracket_v1"
ILLUMINATION_COMPONENT_SCHEMA = "camera_unmodulated_plus_modulated_illumination_v1"
PROGRAM_SCHEMA = "camera_capture_program_v1"
NONLINEAR_PROGRAM_SCHEMA = "camera_capture_program_v2"
CDS_PROGRAM_SCHEMA = "camera_capture_program_v3"
NONLINEAR_OBSERVATION = "sampled_nonlinear_raw_black_relative_v1"
NONLINEAR_PREVIEW = {
    "method": "calibrated_charge_fixed_color_v1",
    "normalization": "full_well_e",
    "isp_profile_role": "fixed_color_settings_only",
}


def validate_preview(value):
    if not isinstance(value, Mapping) or dict(value) != NONLINEAR_PREVIEW:
        raise ValueError("Nonlinear capture requires the explicit calibrated fixed-color preview")
    return dict(NONLINEAR_PREVIEW)

CAPTURE_EFFECTS = [
    *TEMPORAL_EFFECTS,
    "electrical_readout",
    "periodic_light_modulation",
    "raw_hdr_bracket_merge",
]
MAX_WORKING_ARRAY_BYTES = 1024 * 1024 * 1024
MAX_OUTPUT_ARRAY_BYTES = 128 * 1024 * 1024


def validate_capture_program(payload: Any) -> dict[str, Any]:
    """Normalize an explicit program; null disables waveform or HDR fusion."""
    if not isinstance(payload, Mapping):
        raise ValueError("Capture program requires an object")
    nonlinear = payload.get("schema") == NONLINEAR_PROGRAM_SCHEMA
    cds = payload.get("schema") == CDS_PROGRAM_SCHEMA
    required = {"schema", "electrical_model", "waveform", "hdr_merge"}
    if nonlinear:
        required.add("preview")
    if cds:
        required.add("cds_read_noise")
    if set(payload) != required:
        raise ValueError("Capture program fields differ from its declared schema")
    if (not isinstance(payload["schema"], str)
            or payload["schema"] not in {PROGRAM_SCHEMA, NONLINEAR_PROGRAM_SCHEMA,
                                         CDS_PROGRAM_SCHEMA}):
        raise ValueError("Unsupported capture program schema")
    if nonlinear:
        from . import nonlinear_hdr, nonlinear_readout

        validate_model = nonlinear_readout.validate_model
        validate_merge = nonlinear_hdr.validate_profile
    else:
        validate_model = electrical.validate_electrical_model
        validate_merge = hdr.validate_profile
    result = {
        "schema": payload["schema"],
        "electrical_model": validate_model(payload["electrical_model"]),
        "waveform": (
            None if payload["waveform"] is None else validate_waveform(payload["waveform"])
        ),
        "hdr_merge": (
            None if payload["hdr_merge"] is None else validate_merge(payload["hdr_merge"])
        ),
    }
    if cds:
        if (result["electrical_model"]["read_noise_e"]
                or result["electrical_model"]["post_gain_noise_v"]):
            raise ValueError("CDS profile replaces aggregate input and post-gain read noise")
        if result["hdr_merge"] is not None:
            raise ValueError("Correlated CDS read noise has no independent-pixel HDR merge")
        result["cds_read_noise"] = electrical.validate_cds_noise_profile(
            payload["cds_read_noise"]
        )
    if nonlinear:
        result["preview"] = validate_preview(payload["preview"])
    return result


def default_capture_program(legacy_readout: Mapping) -> dict[str, Any]:
    """Return explicit authored defaults; the caller must provide a static bracket."""
    return validate_capture_program(
        {
            "schema": PROGRAM_SCHEMA,
            "electrical_model": electrical.default_electrical_model(legacy_readout),
            "waveform": None,
            "hdr_merge": hdr.default_profile(),
        }
    )


def capture_effects(program: Any) -> list[str]:
    """Actual active stages for fixed Test/prediction correspondence."""
    selected = validate_capture_program(program)
    effects = [*TEMPORAL_EFFECTS, "electrical_readout"]
    if selected["schema"] == NONLINEAR_PROGRAM_SCHEMA:
        effects.append("nonlinear_charge_voltage_response")
    if selected["schema"] == CDS_PROGRAM_SCHEMA:
        effects.append("reset_signal_cds_native_row_column_correlated_read_noise")
    if selected["waveform"] is not None:
        effects.append("periodic_light_modulation")
    if selected["hdr_merge"] is not None:
        effects.append("raw_hdr_bracket_merge")
    return effects


def _raw_format(model):
    return {
        "cfa": "RGGB", "bit_depth": model["adc"]["bit_depth"],
        "black_level_dn": model["adc"]["black_level_dn"],
        "response_type": ("nonlinear_charge_voltage"
                          if model["schema"] == "camera_electrical_readout_v2" else "linear"),
        "packing": "unpacked", "layout": "bayer", "hdr_streams": 1,
    }


def _resources(parsed):
    base = parsed["base"]
    rows, cols = base["size"]
    pixels = int(rows * cols)
    source_pixels = int(np.prod(_collection_support_shape(base)))
    frames, scenes = len(parsed["control"]["frames"]), len(parsed["unique"])
    weight_bytes = frames * scenes * rows * 8
    site_count = len(base.get("channels", CHANNELS))
    rate_bytes = scenes * pixels * site_count * 8
    collection_bytes = (scenes * source_pixels * _collection_evidence_count(base) * 8
                        if "charge_collection" in base else 0)
    evidence = _native_response_arrays(base)
    full_pixels = int(np.prod(base["geometry"]["full_sensor_shape"]))
    working = (
        parsed["unique_spectral_samples"] * 8
        + rate_bytes + collection_bytes * 2
        + frames * pixels * 512
        + base["image"].nbytes * 8
        + full_pixels * 48
        + (full_pixels * 16 + frames * pixels * 24
           if parsed["program"]["schema"] == CDS_PROGRAM_SCHEMA else 0)
        + weight_bytes * 2
        + sum(a.nbytes for a in evidence.values()) * 2
    )
    result = {
        "cpu_threads": 1,
        "gpu_required": False,
        "execution": "in_process",
        "working_array_bytes_estimate": int(working),
        "estimate_kind": "conservative_array_budget_not_measured_peak",
        "frames": frames,
        "readouts": frames,
        "unique_scenes": scenes,
        "knots": len(parsed["timeline"]["knots"]),
        "output_size": list(base["size"]),
        "unique_spectral_samples": parsed["unique_spectral_samples"],
        "automatic_coarsening": False,
    }
    if base["field_radiometry_mode"] == SOURCE_RI_MODE:
        map_bytes = (base["source_subcells"].size if base["source_subcells"] is not None
                     else base["image"].size) * 8
        result["working_array_bytes_estimate"] += base["source_ri_extra_bytes"] + map_bytes
        result["source_position_ri_map_bytes"] = map_bytes
        result["source_position_ri_unique_scenes"] = scenes
    if parsed["program"]["waveform"] is not None and (
        parsed["program"]["waveform"]["kind"] == "pwm"
    ):
        result["working_array_bytes_estimate"] += frames * rows * 2 * 8
        result["pwm_phase_envelope_bytes"] = frames * rows * 2 * 8
    if base["field_psf"] is not None:
        result["working_array_bytes_estimate"] += (
            base["field_psf"]["working_array_bytes_estimate"] + base["kernels"].nbytes
        )
        result["field_psf_fft_work_units"] = scenes * base["field_psf"]["fft_work_units"]
    if "native_direction_work" in base:
        result["native_direction_weighted_samples"] = scenes * base["native_direction_work"]
    if (
        max([rate_bytes, weight_bytes, frames * pixels * site_count * 8,
             scenes * source_pixels * 8 if collection_bytes else 0,
             result.get("source_position_ri_map_bytes", 0),
             *(a.nbytes for a in evidence.values())]) > MAX_OUTPUT_ARRAY_BYTES
        or result["working_array_bytes_estimate"] > MAX_WORKING_ARRAY_BYTES
    ):
        raise ValueError("HDR capture exceeds bounded working/output array memory")
    return result


def _parse(request):
    if not isinstance(request, Mapping) or "capture_program" not in request:
        raise ValueError("HDR capture requires an explicit capture_program")
    program = validate_capture_program(request["capture_program"])
    unmodulated_input = request.get("unmodulated_scene_sequence")
    if unmodulated_input is not None and program["waveform"] is None:
        raise ValueError("A separate unmodulated scene requires an explicit light waveform")
    # Never validate/execute a one-frame surrogate: overlap, source coverage and
    # commands are cross-frame contracts even when HDR fusion is disabled.
    base_request = {k: v for k, v in request.items()
                    if k not in {"capture_program", "unmodulated_scene_sequence"}}
    source_ri = "field_radiometry_mode" in base_request
    parsed = _parse_temporal(base_request, allow_source_ri=source_ri)
    parsed["program"] = program
    if unmodulated_input is not None:
        unmodulated = _parse_temporal(
            {**base_request, "scene_sequence": unmodulated_input},
            allow_source_ri=source_ri,
        )
        for key in ("geometry", "size", "source_support", "capture_support"):
            if unmodulated["base"].get(key) != parsed["base"].get(key):
                raise ValueError(
                    "Unmodulated and modulated scenes require identical native geometry"
                )
        for key in ("wave", "pitch"):
            if not np.array_equal(unmodulated["base"][key], parsed["base"][key]):
                raise ValueError("Illumination components require identical wavelength/pitch")
        if (unmodulated["timeline"]["clock"] != parsed["timeline"]["clock"]
                or unmodulated["control"] != parsed["control"]):
            raise ValueError(
                "Unmodulated and modulated scenes require one exact capture clock/control"
            )
        if source_ri:
            modulated_subcells = parsed["base"]["source_subcells"]
            unmodulated_subcells = unmodulated["base"]["source_subcells"]
            if ((modulated_subcells is None) != (unmodulated_subcells is None)
                    or (modulated_subcells is not None
                        and modulated_subcells.shape != unmodulated_subcells.shape)):
                raise ValueError(
                    "Source-position RI illumination components require one exact source "
                    "cell/subcell grid"
                )
        parsed["unmodulated"] = unmodulated
    base, control = parsed["base"], parsed["control"]
    model = program["electrical_model"]
    if "charge_collection" in base and model["fixed_pattern"]["prnu_sigma"] != 0:
        raise ValueError("Charge collection requires PRNU=0; source-generation PRNU is not modeled")
    for field in ("bit_depth", "black_level_dn"):
        if model["adc"][field] != base["readout"][field]:
            raise ValueError("Electrical ADC RAW format differs from the fixed sensor readout")
    if int(np.prod(base["geometry"]["full_sensor_shape"])) > electrical.MAX_NATIVE_PIXELS:
        raise ValueError("Electrical fixed patterns exceed the bounded full native sensor domain")
    if base["seed"] + len(control["frames"]) - 1 > 2**32 - 1:
        raise ValueError("Per-frame electrical noise seed exceeds the uint32 domain")
    temperature = (model["reference_temperature_c"]
                   if base["temperature"] is None else base["temperature"])
    if program["schema"] == CDS_PROGRAM_SCHEMA:
        low, high = program["cds_read_noise"]["temperature_range_c"]
        if not low <= temperature <= high:
            raise ValueError("Temperature is outside the declared CDS noise domain")
    if program["schema"] == NONLINEAR_PROGRAM_SCHEMA:
        from .nonlinear_readout import validate_capture_conditions
    else:
        validate_capture_conditions = electrical.validate_capture_conditions
    for frame in control["frames"]:
        validate_capture_conditions(
            model, frame["applied"]["exposure_ns"] / 1e9,
            temperature_c=temperature, analog_gain=frame["applied"]["gain"],
        )
    waveform = program["waveform"]
    if waveform is not None and (
        waveform["clock_id"] != control["clock"]["clock_id"]
        or waveform["clock_id"] != parsed["timeline"]["clock"]["clock_id"]
    ):
        raise ValueError("Waveform clock_id must match the complete scene and control clock")
    if program["hdr_merge"] is not None:
        if len(control["frames"]) * int(np.prod(base["size"])) > hdr.MAX_FRAME_PIXELS:
            raise ValueError("HDR RAW bracket exceeds its total frame-pixel budget")
        if len(control["frames"]) < 2:
            raise ValueError("HDR merge requires at least two independently reset exposures")
        # Provenance descriptions may differ for the same declared optical image.
        # Compare validated numeric image and all optical metadata, retaining each
        # source separately in the output. Source labels cannot establish motion.
        for component in (parsed, *([parsed["unmodulated"]]
                                    if "unmodulated" in parsed else [])):
            identities = {
                _scene_hash(
                    {key: value for key, value in item["scene"].items()
                     if key not in {"source", "source_subcell_evidence"}},
                    np.asarray(item["scene"]["spectral_irradiance"], dtype=float),
                    source_ri=source_ri,
                )
                for item in component["unique"]
            }
            if len(identities) != 1:
                raise ValueError(
                    "HDR merge requires each optical component to be static; no motion alignment"
                )
        if (
            waveform is not None
            and waveform["kind"] == "pwm"
            and any(
                frame["applied"]["exposure_ns"] % waveform["period_ns"]
                for frame in control["frames"]
            )
        ):
            raise ValueError("PWM HDR merge requires whole-period exposure lengths for every frame")
    resources = _resources(parsed)
    if "unmodulated" in parsed:
        unmodulated = parsed["unmodulated"]
        second = _resources({**unmodulated, "program": program})
        if (parsed["unique_spectral_samples"] + unmodulated["unique_spectral_samples"]
                > MAX_UNIQUE_SPECTRAL_SAMPLES):
            raise ValueError("Combined illumination components exceed spectral sample budget")
        if (resources.get("field_psf_fft_work_units", 0)
                + second.get("field_psf_fft_work_units", 0) > MAX_FIELD_FFT_WORK
                or resources.get("native_direction_weighted_samples", 0)
                + second.get("native_direction_weighted_samples", 0)
                > MAX_NATIVE_DIRECTION_WORK
                or parsed["resources"].get("collection_routing_samples", 0)
                + unmodulated["resources"].get("collection_routing_samples", 0)
                > 512_000_000):
            raise ValueError("Combined illumination components exceed optical work budget")
        combined = (resources["working_array_bytes_estimate"]
                    + second["working_array_bytes_estimate"])
        if combined > MAX_WORKING_ARRAY_BYTES:
            raise ValueError("Combined illumination components exceed working-array budget")
        resources.update(
            working_array_bytes_estimate=combined,
            unique_scenes=resources["unique_scenes"] + second["unique_scenes"],
            unique_spectral_samples=(parsed["unique_spectral_samples"]
                                     + unmodulated["unique_spectral_samples"]),
            collection_routing_samples=(
                parsed["resources"].get("collection_routing_samples", 0)
                + unmodulated["resources"].get("collection_routing_samples", 0)
            ),
            illumination_components={
                "schema": ILLUMINATION_COMPONENT_SCHEMA,
                "modulated_unique_scenes": len(parsed["unique"]),
                "unmodulated_unique_scenes": len(unmodulated["unique"]),
            },
        )
        if source_ri:
            resources["source_position_ri"] = {
                "mode": SOURCE_RI_MODE,
                "component_map_bytes": {
                    "modulated": resources["source_position_ri_map_bytes"],
                    "unmodulated": second["source_position_ri_map_bytes"],
                },
                "source_grid_contract": "one_exact_native_cell_or_subcell_grid",
            }
    # The scalar dark mean cannot bound a seeded Gaussian DSNU map. Check the
    # exact native map only after all cheaper optical/output resource bounds,
    # but still before any optical work or per-frame readout.
    electrical._preflight_seeded_dark_charge(
        model, base["geometry"], base["size"], temperature,
        max(frame["applied"]["exposure_ns"] for frame in control["frames"]) / 1e9,
    )
    parsed["resources"] = resources
    return parsed


def _integration(parsed, *, unmodulated=False):
    control, timeline, base = parsed["control"], parsed["timeline"], parsed["base"]
    waveform = (None if unmodulated else parsed["program"]["waveform"]) or {
        "schema": "camera_light_waveform_v1",
        "kind": "constant",
        "clock_id": control["clock"]["clock_id"],
        "level": 1,
    }
    frames = control["frames"]
    weights = np.zeros((len(frames), len(parsed["unique"]), base["size"][0]), dtype=float)
    effective = np.zeros((len(frames), base["size"][0]), dtype=float)
    windows = []
    knots = [knot["time_ns"] for knot in timeline["knots"]]
    for frame_index, frame in enumerate(frames):
        window = row_windows(frame, control["mode"], base["geometry"], base["size"])
        windows.append(window)
        for row, (start, end) in enumerate(
            zip(window["row_start_ns"], window["row_end_ns"], strict=True)
        ):
            effective[frame_index, row] = integrate_waveform_ns(waveform, start, end) / 1e9
            for index, (left, right) in enumerate(zip(knots, knots[1:], strict=False)):
                w_left, w_right = integrate_linear_basis(
                    waveform, start, end, left, right, timeline["interpolation"]
                )
                weights[frame_index, parsed["scene_indices"][index], row] += w_left
                weights[frame_index, parsed["scene_indices"][index + 1], row] += w_right
        if not np.allclose(
            weights[frame_index].sum(axis=0), effective[frame_index], rtol=3e-14, atol=0
        ):
            raise ValueError("Modulated temporal basis does not cover every row exposure")
    exposures = np.array([frame["applied"]["exposure_ns"] / 1e9 for frame in frames])
    return weights, effective, effective / exposures[:, None], exposures, windows


class HDRBracketRuntime:
    """Bounded sequential RAW acquisition with a declared static-scene HDR boundary."""

    plugin_id = PLUGIN_ID

    def describe_capabilities(self):
        return {
            "plugin_id": self.plugin_id,
            "role": "runtime",
            "version": "1.0",
            "mode": "hdr_bracket",
            "available": True,
            "supports": [
                "analytic_uniform_constant_or_pwm_light",
                "explicit_unmodulated_and_modulated_spectral_components",
                "electrical_raw_readout",
                "opt_in_native_row_column_correlated_reset_signal_CDS_raw",
                "native_rolling_or_global_exposure",
                "independent_sequential_bracket",
                "calibrated_integer_raw_hdr",
                "per_frame_configured_isp",
                "native_four_site_absolute_response",
                "opt_in_source_position_relative_illumination_before_psf",
            ],
            "unsupported": [
                "shared_charge_dcg",
                "motion_alignment",
                "inferred_ambient_light",
                "vendor_lfm",
                "physical_validation",
                "correlated_CDS_HDR_merge",
                "automatic_exposure_control",
            ],
            "limits": {
                "max_frames": MAX_FRAMES,
                "max_knots": MAX_KNOTS,
                "max_frame_pixels": MAX_FRAME_PIXELS,
                "max_unique_spectral_samples": MAX_UNIQUE_SPECTRAL_SAMPLES,
                "max_full_native_pixels": electrical.MAX_NATIVE_PIXELS,
                "max_field_fft_work_units": MAX_FIELD_FFT_WORK,
                "max_working_array_bytes": MAX_WORKING_ARRAY_BYTES,
                "max_output_array_bytes": MAX_OUTPUT_ARRAY_BYTES,
                "max_native_direction_weighted_samples": MAX_NATIVE_DIRECTION_WORK,
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
        nonlinear = program["schema"] == NONLINEAR_PROGRAM_SCHEMA
        capture_electrons = electrical.capture_electrons
        merge_bracket = hdr.merge_raw_bracket
        if nonlinear:
            from . import nonlinear_hdr, nonlinear_readout
            from .nonlinear_isp import process_raw as process_nonlinear_raw

            capture_electrons = nonlinear_readout.capture_electrons
            merge_bracket = nonlinear_hdr.merge_raw_bracket
        site_names = list(base.get("channels", CHANNELS))
        frames = control["frames"]
        weights, effective, ratio, exposures, windows = _integration(parsed)
        pwm_phase_envelope = None
        if program["waveform"] is not None and program["waveform"]["kind"] == "pwm":
            row_bounds = np.array([
                phase_envelope_ns(program["waveform"], frame["applied"]["exposure_ns"])
                for frame in frames
            ], dtype=float) * 1e-9
            pwm_phase_envelope = (
                np.broadcast_to(row_bounds[:, 0, None], effective.shape).copy(),
                np.broadcast_to(row_bounds[:, 1, None], effective.shape).copy(),
            )
        rates, flux, radius, cfa, ri_map = TemporalSceneRuntime._optical_rates(parsed)
        source_ri = base["field_radiometry_mode"] == SOURCE_RI_MODE
        with np.errstate(over="ignore", invalid="ignore"):
            modulated_channels = np.einsum("fsr,srwc->frwc", weights, rates, optimize=False)
        unmodulated = parsed.get("unmodulated")
        unmodulated_weights = unmodulated_rates = unmodulated_flux = unmodulated_channels = None
        if unmodulated is not None:
            (unmodulated_weights, unmodulated_effective, _,
             unmodulated_exposures, unmodulated_windows) = _integration(
                unmodulated, unmodulated=True)
            if (not np.array_equal(unmodulated_exposures, exposures)
                    or not np.allclose(
                        unmodulated_effective, exposures[:, None], rtol=3e-14, atol=0)
                    or unmodulated_windows != windows):
                raise ValueError("Unmodulated component row timing differs from modulated capture")
            (unmodulated_rates, unmodulated_flux, unmodulated_radius,
             unmodulated_cfa, unmodulated_ri) = TemporalSceneRuntime._optical_rates(unmodulated)
            if (not np.array_equal(unmodulated_radius, radius)
                    or not np.array_equal(unmodulated_cfa, cfa)
                    or not np.array_equal(unmodulated_ri, ri_map)):
                raise ValueError("Illumination components differ in receiver optical geometry")
            if source_ri and not np.array_equal(
                parsed["source_ri_map"], unmodulated["source_ri_map"]
            ):
                raise ValueError(
                    "Source-position RI illumination components differ in fixed source map"
                )
            with np.errstate(over="ignore", invalid="ignore"):
                unmodulated_channels = np.einsum(
                    "fsr,srwc->frwc", unmodulated_weights, unmodulated_rates, optimize=False
                )
                channels = modulated_channels + unmodulated_channels
        else:
            channels = modulated_channels
        if (
            not np.isfinite(channels).all()
            or np.any(channels < 0)
            or np.any(channels > electrical.MAX_CHARGE_E)
        ):
            raise ValueError("Integrated photocharge exceeds the finite 1e13 electron domain")
        means = np.take_along_axis(channels, cfa[None, ..., None], axis=-1)[..., 0]
        model = program["electrical_model"]
        temperature = (
            model["reference_temperature_c"] if base["temperature"] is None else base["temperature"]
        )
        raw_format = _raw_format(model)
        captured, processed_arrays = {}, {}
        calibrations, metadata, isp_summaries, conditions, sampled_means = [], [], [], [], []
        for index, frame in enumerate(frames):
            applied = frame["applied"]
            capture = capture_electrons(
                means[index],
                exposures[index],
                model,
                analog_gain=applied["gain"],
                temperature_c=temperature,
                geometry={
                    key: base["geometry"][key] for key in ("full_sensor_shape", "crop_origin")
                },
                noise_enabled=base["noise_enabled"],
                seed=base["seed"] + index,
                **({"cds_profile": program["cds_read_noise"]}
                   if program["schema"] == CDS_PROGRAM_SCHEMA else {}),
            )
            calibration = capture["calibration"]
            calibrations.append(calibration)
            metadata.append({"frame_id": frame["frame_id"], **capture["summary"]})
            raw = capture["arrays"]["raw_dn"]
            profile = deepcopy(parsed["profiles"][applied["isp_model_id"]])
            if "wb_gains_rgb" in applied:
                profile["white_balance"]["gains_rgb"] = applied["wb_gains_rgb"]
            processed = (
                process_nonlinear_raw(raw, calibration, profile, geometry=base["geometry"])
                if nonlinear else process_raw(raw, raw_format, profile, geometry=base["geometry"])
            )
            isp_summaries.append(
                {
                    "frame_id": frame["frame_id"],
                    "isp_model_id": applied["isp_model_id"],
                    **processed["summary"],
                }
            )
            for key, array in processed["arrays"].items():
                if key != "rgb":
                    processed_arrays.setdefault(key, []).append(array[0])
            for key, array in capture["arrays"].items():
                captured.setdefault(key, []).append(array)
            # Observed ADC values, signed after declared dark correction. No
            # oracle/expected photocharge appears in this prediction or fusion.
            corrected = raw.astype(float) - raw_format["black_level_dn"]
            if not nonlinear:
                corrected -= calibration["dark_e"] * calibration["dn_per_e"]
            channel_means = [float(corrected[cfa == channel].mean())
                             for channel in range(len(site_names))]
            if not np.isfinite(channel_means).all():
                raise ValueError("Sampled RAW prediction must remain finite")
            sampled_means.append(channel_means)
            conditions.append(
                {
                    "frame_id": frame["frame_id"],
                    "conditions": {
                        "exposure_s": {"value": float(exposures[index]), "unit": "s"},
                        "gain": {"value": applied["gain"], "unit": "1"},
                        "temperature_c": {"value": temperature, "unit": "degC"},
                    },
                }
            )
        frame_ids = [frame["frame_id"] for frame in frames]
        capture_units = metadata[0]["array_units"]
        arrays = {
            "channel_photoelectron_rate_scenes": rates,
            "row_scene_integration_weights_s": weights,
            "channel_electrons": channels,
            **({
                "modulated_channel_electrons": modulated_channels,
                "unmodulated_channel_photoelectron_rate_scenes": unmodulated_rates,
                "row_unmodulated_integration_weights_s": unmodulated_weights,
                "unmodulated_channel_electrons": unmodulated_channels,
            } if unmodulated is not None else {}),
            "mean_electrons": means,
            "raw_frames": np.stack(captured.pop("raw_dn")),
            **{"electrical_" + key: np.stack(value) for key, value in captured.items()},
            "calibration_dark_e": np.stack([value["dark_e"] for value in calibrations]),
            "radius_map": radius,
            "cfa_channel_index": cfa,
            "relative_illumination_map": ri_map,
            **({"source_relative_illumination_map": parsed["source_ri_map"].copy()}
               if source_ri else {}),
            **({"unmodulated_source_relative_illumination_map":
                unmodulated["source_ri_map"].copy()}
               if source_ri and unmodulated is not None else {}),
            "psf_intensity": base["kernels"].copy(),
            **_field_psf_arrays(base),
            **_radial_response_arrays(base),
            **_native_response_arrays(base),
            **parsed.get("collection_rate_arrays", {}),
            **({"unmodulated_" + key: value for key, value in
                unmodulated.get("collection_rate_arrays", {}).items()}
               if unmodulated is not None else {}),
            "row_start_ns": np.array([w["row_start_ns"] for w in windows], dtype=np.int64),
            "row_end_ns": np.array([w["row_end_ns"] for w in windows], dtype=np.int64),
            "frame_start_ns": np.array(
                [frame["frame_start_ns"] for frame in frames], dtype=np.int64
            ),
            "waveform_row_effective_s": effective,
            "waveform_row_exposure_ratio": ratio,
            **({
                "waveform_row_all_phase_min_effective_s": pwm_phase_envelope[0],
                "waveform_row_all_phase_max_effective_s": pwm_phase_envelope[1],
            } if pwm_phase_envelope is not None else {}),
            **{key: np.stack(value) for key, value in processed_arrays.items()},
        }
        arrays["rgb"] = arrays["rgb_frames"][0].copy()
        array_units = {
            "channel_photoelectron_rate_scenes": "electron/pixel/s",
            "row_scene_integration_weights_s": "s",
            "channel_electrons": "electron/pixel",
            **({
                "modulated_channel_electrons": "electron/pixel",
                "unmodulated_channel_photoelectron_rate_scenes": "electron/pixel/s",
                "row_unmodulated_integration_weights_s": "s",
                "unmodulated_channel_electrons": "electron/pixel",
            } if unmodulated is not None else {}),
            "mean_electrons": "electron/pixel",
            "raw_frames": "DN",
            **{"electrical_" + key: capture_units[key] for key in captured},
            "calibration_dark_e": "electron/pixel",
            "radius_map": "1",
            "cfa_channel_index": "index",
            "relative_illumination_map": "1",
            **({"source_relative_illumination_map": "1"} if source_ri else {}),
            **({"unmodulated_source_relative_illumination_map": "1"}
               if source_ri and unmodulated is not None else {}),
            "psf_intensity": "1",
            "row_start_ns": "ns",
            "row_end_ns": "ns",
            "frame_start_ns": "ns",
            "waveform_row_effective_s": "s",
            "waveform_row_exposure_ratio": "1",
            **({
                "waveform_row_all_phase_min_effective_s": "s",
                "waveform_row_all_phase_max_effective_s": "s",
            } if pwm_phase_envelope is not None else {}),
            **isp_summaries[0]["array_units"],
            **{name: "mm" for name in _field_psf_arrays(base)},
            **_native_response_units(base),
            **{key: "electron/pixel/s" for key in parsed.get("collection_rate_arrays", {})},
            **({"unmodulated_" + key: "electron/pixel/s" for key in
                unmodulated.get("collection_rate_arrays", {})} if unmodulated is not None else {}),
        }
        if "field_reference_radius_mm" in base:
            array_units.update(sensor_radius_mm="mm", response_radius_map="1")
        hdr_summary = None
        if program["hdr_merge"] is not None:
            merged = merge_bracket(
                arrays["raw_frames"],
                exposures,
                calibrations,
                geometry=base["geometry"],
                profile=program["hdr_merge"],
            )
            arrays.update({"hdr_" + key: value for key, value in merged["arrays"].items()})
            arrays["rgb"] = merged["arrays"]["rgb"].copy()
            array_units.update(
                {"hdr_" + key: unit for key, unit in merged["summary"]["array_units"].items()}
            )
            hdr_summary = merged["summary"]
        if any(array.nbytes > MAX_OUTPUT_ARRAY_BYTES for array in arrays.values()):
            raise ValueError("Capture output exceeds the per-array storage bound")
        timing = summarize_timing(control, geometry=base["geometry"], frame_shape=base["size"])
        effects = with_sensor_effects(capture_effects(program), base)
        if unmodulated is not None:
            effects.append("independent_unmodulated_and_modulated_ideal_image_components")
        summary = {
            "schema_version": "1.0",
            "plugin_id": self.plugin_id,
            "source_kind": "simulated",
            "physical_validation": "not_established",
            "uncertainty": {"kind": "unknown"},
            "engine": "spectral PSF/RI/QE + analytic light/exposure + electrical RAW + ISP/HDR",
            "channels": site_names.copy(),
            "wavelength_nm": base["wave"].tolist(),
            "output_size": list(base["size"]),
            "frame_count": len(frames),
            "frames": [
                {**deepcopy(frame), "frame_index": index, "timing": timing["frames"][index]}
                for index, frame in enumerate(frames)
            ],
            "frame_ids": frame_ids,
            "geometry": base["geometry"],
            **_collection_geometry_summary(base, parsed.get("collection_rate_arrays")),
            **_source_ri_summary(base),
            **({"source_relative_illumination_map_scope":
                "same_fixed_source_grid_all_knots_and_components" if unmodulated is not None
                else "same_fixed_source_grid_all_knots"} if source_ri else {}),
            "pixel_pitch_um": base["pitch"],
            **_radial_response_summary(base),
            **_native_response_summary(base),
            "capture_program": program,
            "control_sequence": control,
            "timing": timing,
            "clock": control["clock"],
            "coverage": parsed["coverage"],
            "raw_format": raw_format,
            "conditions_per_frame": conditions,
            "included_effects": effects,
            "readout_count": len(frames),
            "capture_metadata": metadata,
            "calibrations": [
                {
                    **{k: v for k, v in cal.items() if k != "dark_e"},
                    "dark_e_array": "calibration_dark_e",
                    "frame_index": index,
                }
                for index, cal in enumerate(calibrations)
            ],
            "seed_sequence": [base["seed"] + index for index in range(len(frames))],
            "noise_enabled": base["noise_enabled"],
            "temperature_basis": (
                "electrical_model_reference_temperature"
                if base["temperature"] is None
                else "request_temperature"
            ),
            "isp_per_frame": isp_summaries,
            "array_units": array_units,
            "sampled_raw_dark_corrected_channel_mean_dn": sampled_means,
            "prediction": {
                "metric_id": "raw.channel_mean_dn_sequence",
                "unit": "DN",
                "values": np.asarray(sampled_means).reshape(-1).tolist(),
                "axes": [
                    {"name": "frame", "unit": "1", "values": frame_ids},
                    {"name": "channel", "unit": "1", "values": site_names.copy()},
                ],
                "reference_plane": "sensor_raw",
                "included_effects": effects.copy(),
                "conditions": {},
                "conditions_per_frame": conditions,
                "source_kind": "simulated",
                "method_and_version": "sampled_raw_dark_corrected_v1",
                "uncertainty": {"kind": "unknown"},
            },
            "waveform": {
                "specification": program["waveform"],
                "scope": ("modulated_component_only" if unmodulated is not None
                          else "uniform multiplicative modulation of the whole declared image"),
                "independent_ambient_component": unmodulated is not None,
                "on_time_correction": False,
                "effective_seconds_array": "waveform_row_effective_s",
                **({"effective_seconds_scope": "modulated_component_only"}
                   if unmodulated is not None else {}),
                "exposure_ratio_array": "waveform_row_exposure_ratio",
                **({
                    "all_phase_min_effective_seconds_array":
                        "waveform_row_all_phase_min_effective_s",
                    "all_phase_max_effective_seconds_array":
                        "waveform_row_all_phase_max_effective_s",
                    "all_phase_envelope_scope": (
                        "pointwise_native_row_window_extrema_over_every_global_phase_origin; "
                        "fixed_declared_period_duty_and_levels; modulated_component_only; "
                        "not_a_joint_row_extremum_or_driver_jitter_model; "
                        "not_a_bound_on_scene_weighted_charge_or_RAW"
                    ),
                    "phase_invariant_effective_seconds": bool(np.array_equal(
                        pwm_phase_envelope[0], pwm_phase_envelope[1])),
                } if pwm_phase_envelope is not None else {}),
            },
            "simulation_oracle": {
                "arrays": [
                    "mean_electrons",
                    "channel_electrons",
                    "electrical_mean_total_charge_e",
                    "electrical_well_saturated_mask",
                    "electrical_adc_low_mask",
                    "electrical_adc_high_mask",
                    "electrical_digital_low_mask",
                    "electrical_digital_high_mask",
                ],
                "used_by_hdr_merge": False,
                "used_by_prediction": False,
            },
            "hdr_merge": hdr_summary,
            "hdr_merge_applied": hdr_summary is not None,
            "main_preview": "hdr_rgb" if hdr_summary is not None else "rgb_frames[0]",
            "scene_interpolation": parsed["timeline"]["interpolation"],
            "scene_knots": [
                {
                    "time_ns": knot["time_ns"],
                    "scene_index": index,
                    "scene_content_hash": parsed["scene_hashes"][index],
                }
                for knot, index in zip(
                    parsed["timeline"]["knots"], parsed["scene_indices"], strict=True
                )
            ],
            "scene_sources": [item["scene"]["source"] for item in parsed["unique"]],
            "psf_source": base["psf_source"],
            "flux_diagnostics_per_scene": flux,
            "integration": "analytic waveform times declared linear/ZOH irradiance basis",
            "limitations": [
                ("Independent sequential reset/readout with within-frame native row/column "
                 "covariance; no cross-frame or shared-charge covariance."
                 if program["schema"] == CDS_PROGRAM_SCHEMA else
                 "Independent sequential reset/readout; no DCG, shared charge or covariance."),
                ("The unmodulated component follows its own timed ideal-image knots without "
                 "waveform multiplication; no ambient component is inferred from either image."
                 if unmodulated is not None else
                 "Waveform modulates the complete declared image; "
                 "no independent ambient inference."),
                "No PWM on-time correction, vendor LFM algorithm, motion alignment or deghosting.",
                "HDR requires a static declared scene and equal whole-cycle PWM average when used.",
                "Sampled RAW channel means retain clipping and noise; they are not expectations.",
                "Declared PSF/RI/QE optics retain their supplied domains; exterior image is zero.",
                "Field PSF and angular response are separable approximations, not a general lens.",
                "Fixed patterns and temperature dark-current scaling are model assumptions.",
                "Per-frame ISP and HDR preview are explicit separate display processing paths.",
                "No established physical validation, total uncertainty or hardware equivalence.",
            ],
        }
        if unmodulated is not None:
            summary["illumination_components"] = {
                "schema": ILLUMINATION_COMPONENT_SCHEMA,
                "reference_plane": "sensor_ideal_image_before_psf_ri",
                "modulated": {"scene_sequence": "scene_sequence",
                              "row_weights_array": "row_scene_integration_weights_s",
                              "channel_electrons_array": "modulated_channel_electrons"},
                "unmodulated": {"scene_sequence": "unmodulated_scene_sequence",
                                "row_weights_array": "row_unmodulated_integration_weights_s",
                                "channel_electrons_array": "unmodulated_channel_electrons"},
                "summation": (
                    "channel_electrons = modulated_channel_electrons "
                    "+ unmodulated_channel_electrons"
                ),
            }
            summary["unmodulated_scene_interpolation"] = unmodulated["timeline"]["interpolation"]
            summary["unmodulated_scene_knots"] = [
                {"time_ns": knot["time_ns"], "scene_index": index,
                 "scene_content_hash": unmodulated["scene_hashes"][index]}
                for knot, index in zip(unmodulated["timeline"]["knots"],
                                       unmodulated["scene_indices"], strict=True)
            ]
            summary["unmodulated_scene_sources"] = [
                item["scene"]["source"] for item in unmodulated["unique"]
            ]
            summary["unmodulated_flux_diagnostics_per_scene"] = unmodulated_flux
            summary["integration"] = (
                "independent exact row integration of waveform-modulated and unmodulated "
                "declared linear/ZOH irradiance bases"
            )
            if hdr_summary is not None:
                summary["hdr_rate_interpretation"] = (
                    "HDR RAW fusion estimates combined unmodulated plus PWM cycle-average "
                    "modulated rate over whole-period exposures; it does not infer separate "
                    "component rates from RAW"
                )
        if source_ri:
            summary["limitations"].append(
                "Source-position RI is fixed across declared knots; receiver angular QE "
                "remains a separable approximation, with no source-direction coupling."
            )
            if unmodulated is not None:
                summary.setdefault("array_geometry", {}).update(
                    unmodulated_source_relative_illumination_map=(
                        "source_support" if "source_support" in base else "native_scene_crop")
                )
                summary["source_position_ri_components"] = {
                    "source_grid_contract": "one_exact_native_cell_or_subcell_grid",
                    "modulated_map_array": "source_relative_illumination_map",
                    "unmodulated_map_array": "unmodulated_source_relative_illumination_map",
                    "maps_equal": True,
                }
        if pwm_phase_envelope is not None:
            summary["limitations"].append(
                "The all-phase PWM envelope bounds ideal waveform effective time for each "
                "row separately; it does not bound scene-weighted charge, RAW, driver jitter "
                "or phase drift."
            )
        if nonlinear:
            summary["sampled_raw_black_relative_channel_mean_dn"] = summary.pop(
                "sampled_raw_dark_corrected_channel_mean_dn")
            summary["prediction"]["method_and_version"] = NONLINEAR_OBSERVATION
            summary["prediction"]["dark_correction"] = "not_applied_to_raw_code_metric"
            summary["prediction"]["raw_format"] = deepcopy(raw_format)
            summary["preview_contract"] = deepcopy(program["preview"])
            summary["isp_profile_role"] = "fixed_color_settings_only"
            summary["limitations"] = [
                item for item in summary["limitations"]
                if item != ("Per-frame ISP and HDR preview are explicit separate "
                            "display processing paths.")
            ] + ["Per-frame preview decodes RAW and uses fixed ISP color settings only; "
                 "it does not execute the full configured ISP graph.",
                 "Observed RAW metric subtracts black only; nonlinear dark response remains."]
        if source_ri:
            summary["prediction"]["method_and_version"] += "_source_ri_v1"
        if base["field_psf"] is not None:
            summary["psf"] = _psf_summary(base)
        return {"arrays": arrays, "summary": summary}
