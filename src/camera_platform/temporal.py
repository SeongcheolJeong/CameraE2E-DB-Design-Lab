"""Exact declared-timeline exposure integration before one RAW readout per frame.

The timeline interpolates supplied *ideal-image irradiance*, not object geometry.
Optics are fixed and linear before clipping, so integrating their collected
photoelectron rate at every declared knot is equivalent to integrating this
declared image model. Commands are evidence; only recorded applied settings drive
capture. Electronic transport timing and closed-loop control are not simulated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import numpy as np

from camera_platform.runtime import (
    CHANNELS,
    RuntimeRequestError,
    _issue,
    charge_collection_capabilities,
    with_sensor_effects,
)
from camera_platform.spatial import (
    EFFECTS,
    MAX_FIELD_FFT_WORK,
    SOURCE_RI_MODE,
    _capture,
    _field_psf_arrays,
    _native_response_arrays,
    _native_response_summary,
    _native_response_units,
    _psf_summary,
    _radial_response_arrays,
    _radial_response_summary,
    _source_ri_summary,
    photoelectron_rate,
)
from camera_platform.spatial import _parse as _parse_spatial
from camera_platform.timing import (
    MAX_FRAME_PIXELS,
    MAX_FRAMES,
    MAX_KNOTS,
    row_windows,
    summarize_timing,
    validate_control_sequence,
    validate_timed_scene,
    validate_timing_coverage,
)

PLUGIN_ID = "temporal_scene_v1"
MAX_UNIQUE_SPECTRAL_SAMPLES = 64_000_000
MAX_WORKING_ARRAY_BYTES = 1024 * 1024 * 1024
MAX_OUTPUT_ARRAY_BYTES = 128 * 1024 * 1024
MAX_NATIVE_DIRECTION_WORK = 128_000_000
TEMPORAL_EFFECTS = [*EFFECTS, "temporal_exposure_integration"]


def _scene_hash(scene: Mapping[str, Any], image: np.ndarray, *, source_ri=False) -> str:
    """Stable numeric content identity; original input encodings remain registry artifacts."""
    # The historical no-mode JSON-list identity is kept byte-for-byte. Native
    # ndarray subcells are not JSON serializable, so hash their canonical
    # numeric shape/bytes; selected source RI has always used that identity.
    canonical_subcells = source_ri or isinstance(
        scene.get("source_subcell_irradiance"), np.ndarray
    )
    metadata = {key: value for key, value in scene.items()
                if key != "spectral_irradiance"
                and (not canonical_subcells or key != "source_subcell_irradiance")}
    digest = hashlib.sha256(json.dumps(
        {"scene": metadata, "shape": list(image.shape), "dtype": "little_float64"},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode())
    digest.update(np.ascontiguousarray(image, dtype="<f8").tobytes())
    if canonical_subcells and "source_subcell_irradiance" in scene:
        subcells = np.ascontiguousarray(scene["source_subcell_irradiance"], dtype="<f8")
        digest.update(json.dumps({"source_subcell_shape": list(subcells.shape)},
                                 sort_keys=True, separators=(",", ":")).encode())
        digest.update(subcells.tobytes())
    return digest.hexdigest()


def _parse(request: Mapping[str, Any], *, allow_source_ri: bool = False) -> dict[str, Any]:
    from camera_platform.isp import validate_profile

    required = {"wavelength_nm", "pixel_pitch_um", "readout", "lens",
                "psf", "scene_sequence", "control_sequence",
                "isp_profiles"}
    native = isinstance(request, Mapping) and "native_pixel_response" in request
    required |= {"native_pixel_response"} if native else {"qe", "sensor_angular_response"}
    optional = {"temperature_c", "seed", "noise_enabled", "charge_collection"}
    if allow_source_ri:
        optional.add("field_radiometry_mode")
    if (not isinstance(request, Mapping) or not required <= set(request)
            or set(request) - required - optional):
        raise ValueError("Unknown or missing temporal fields; applied frame settings are required")
    if ("field_radiometry_mode" in request
            and request["field_radiometry_mode"] != SOURCE_RI_MODE):
        raise ValueError("Temporal field_radiometry_mode supports only source_position_ri_v1")
    sequence = request["scene_sequence"]
    if not isinstance(sequence, Mapping) or not isinstance(sequence.get("knots"), list):
        raise ValueError("scene_sequence requires a declared knot list")
    if not 2 <= len(sequence["knots"]) <= MAX_KNOTS:
        raise ValueError("Scene sequence requires 2..128 knots")
    # Timing validates a small proxy: avoid deepcopying up to 64M image samples
    # multiple times. Every actual scene is independently spatially validated below.
    proxy_knots = []
    for knot in sequence["knots"]:
        if (not isinstance(knot, Mapping) or set(knot) != {"time_ns", "scene"}
                or not isinstance(knot["scene"], Mapping)):
            raise ValueError("Expanded knots require exactly time_ns and scene")
        proxy_knots.append({"time_ns": knot["time_ns"], "scene": {}})
    proxy = validate_timed_scene({**sequence, "knots": proxy_knots}, expanded=True)
    base = {key: value for key, value in request.items()
            if key not in {"scene_sequence", "control_sequence", "isp_profiles"}}
    unique, hashes, indices, known = [], [], [], {}
    total_samples = 0
    inputs = None
    for knot in sequence["knots"]:
        spatial = {**base, "scene": knot["scene"]}
        parsed = _parse_spatial(spatial, rate_only=True)
        if inputs is None:
            inputs = parsed
        elif (parsed["geometry"] != inputs["geometry"] or parsed["size"] != inputs["size"]):
            raise ValueError("Every scene must share exact native geometry and image shape")
        elif "field_radiometry_mode" in request and (
            (parsed["source_subcells"] is None) != (inputs["source_subcells"] is None)
            or (parsed["source_subcells"] is not None and
                parsed["source_subcells"].shape != inputs["source_subcells"].shape)
        ):
            raise ValueError("Source-position RI requires one source cell/subcell grid "
                             "across all temporal knots")
        content_hash = _scene_hash(
            knot["scene"], parsed["image"],
            source_ri=request.get("field_radiometry_mode") == SOURCE_RI_MODE)
        if content_hash not in known:
            total_samples += parsed["image"].size
            if "field_radiometry_mode" in request and parsed["source_subcells"] is not None:
                total_samples += parsed["source_subcells"].size
            if total_samples > MAX_UNIQUE_SPECTRAL_SAMPLES:
                raise ValueError("Unique scene input exceeds 64000000 spectral samples")
            known[content_hash] = len(unique)
            hashes.append(content_hash)
            unique.append(spatial)
        indices.append(known[content_hash])
    if ("charge_collection" in inputs
            and len(unique) * inputs["collection_routing_work"] > 512_000_000):
        raise ValueError("Charge collection exceeds 512M timeline routing samples")
    if (inputs["field_psf"] is not None
            and len(unique) * inputs["field_psf"]["fft_work_units"] > MAX_FIELD_FFT_WORK):
        raise ValueError("Temporal field PSF exceeds the 500000000 FFT work-unit budget "
                         "across unique scenes")
    if len(unique) * inputs.get("native_direction_work", 0) > MAX_NATIVE_DIRECTION_WORK:
        raise ValueError("Temporal native direction/EQE integration exceeds 128M weighted "
                         "samples across unique scenes")
    geometry, shape = inputs["geometry"], inputs["size"]
    control = validate_control_sequence(request["control_sequence"],
                                        geometry=geometry, frame_shape=shape)
    coverage = validate_timing_coverage(proxy, control, geometry=geometry,
                                        frame_shape=shape, expanded=True)
    profiles = request["isp_profiles"]
    if not isinstance(profiles, Mapping):
        raise ValueError("isp_profiles must resolve every referenced ISP model ID")
    referenced = {frame["applied"]["isp_model_id"] for frame in control["frames"]}
    referenced |= {frame["commanded"]["isp_model_id"] for frame in control["frames"]
                   if "commanded" in frame}
    if set(profiles) != referenced:
        raise ValueError("isp_profiles must exactly resolve applied and commanded ISP model IDs")
    profiles = {key: validate_profile(value) for key, value in profiles.items()}
    parsed = {"base": inputs, "unique": unique, "scene_hashes": hashes, "scene_indices": indices,
            "timeline": proxy, "control": control, "coverage": coverage, "profiles": profiles,
            "unique_spectral_samples": total_samples}
    parsed["resources"] = _resources(parsed)
    return parsed


def _resources(parsed):
    """Bound the actual site axis before any unique-scene rate allocation."""
    base = parsed["base"]
    pixels = int(np.prod(base["size"]))
    source_pixels = int(np.prod(_collection_support_shape(base)))
    rows = base["size"][0]
    frames, scenes = len(parsed["control"]["frames"]), len(parsed["unique"])
    sites = len(base.get("channels", CHANNELS))
    rate_bytes = pixels * scenes * sites * 8
    collection_bytes = (source_pixels * scenes * _collection_evidence_count(base) * 8
                        if "charge_collection" in base else 0)
    weight_bytes = frames * scenes * rows * 8
    evidence = _native_response_arrays(base)
    result = {
        "cpu_threads": 1, "gpu_required": False, "execution": "in_process",
        "working_array_bytes_estimate": int(
            parsed["unique_spectral_samples"] * 8 + rate_bytes + collection_bytes * 2
            + pixels * frames * (224 + (sites - 3) * 16)
            + base["image"].nbytes * 8 + weight_bytes * 2
            + sum(a.nbytes for a in evidence.values()) * 2),
        "estimate_kind": "array_budget_not_measured_peak", "frames": frames,
        "output_size": list(base["size"]), "unique_scenes": scenes,
        "knots": len(parsed["timeline"]["knots"]),
        "unique_spectral_samples": parsed["unique_spectral_samples"],
        "readouts": frames, "automatic_coarsening": False,
    }
    if base["field_radiometry_mode"] == SOURCE_RI_MODE:
        # Each unique image is weighted transiently; one immutable spatial RI
        # map is retained because all knots share the exact source grid.
        map_bytes = (base["source_subcells"].size if base["source_subcells"] is not None
                     else base["image"].size) * 8
        result["working_array_bytes_estimate"] += base["source_ri_extra_bytes"] + map_bytes
        result["source_position_ri_map_bytes"] = map_bytes
        result["source_position_ri_unique_scenes"] = scenes
    if "charge_collection" in base:
        result["collection_routing_samples"] = scenes * base["collection_routing_work"]
    if base["field_psf"] is not None:
        result["working_array_bytes_estimate"] += (
            base["field_psf"]["working_array_bytes_estimate"] + base["kernels"].nbytes)
        result["field_psf_node_convolutions"] = scenes * base["field_psf"]["convolutions"]
        result["field_psf_fft_work_units"] = scenes * base["field_psf"]["fft_work_units"]
    if "native_direction_work" in base:
        result["native_direction_weighted_samples"] = scenes * base["native_direction_work"]
    if (max([rate_bytes, weight_bytes, pixels * frames * sites * 8,
             source_pixels * scenes * 8 if collection_bytes else 0,
             result.get("source_position_ri_map_bytes", 0),
             *(a.nbytes for a in evidence.values())]) > MAX_OUTPUT_ARRAY_BYTES
            or result["working_array_bytes_estimate"] > MAX_WORKING_ARRAY_BYTES):
        raise ValueError("Temporal capture exceeds bounded working/output array memory")
    return result


def _collection_support_shape(base):
    """Shape of source-addressed carrier ledgers, distinct from receiver RAW."""
    return base.get("source_support", {}).get("shape", base["size"])


def _collection_evidence_count(base):
    return (3 + int(base.get("partial_source_collection", False))
            + int("capture_slice" in base))


def _collection_geometry_summary(base, collection_arrays):
    result = {}
    if collection_arrays and "source_support" in base:
        result = {
            "source_support": base["source_support"],
            "capture_crop": base["capture_support"],
            "array_geometry": {
                "channel_photoelectron_rate_scenes": "capture_crop",
                **{name: "source_support" for name in collection_arrays},
            },
        }
    if base["field_radiometry_mode"] == SOURCE_RI_MODE:
        result.setdefault("array_geometry", {})["source_relative_illumination_map"] = (
            "source_support" if "source_support" in base else "native_scene_crop")
        if "source_support" in base:
            result.setdefault("source_support", base["source_support"])
            result.setdefault("capture_crop", base["capture_support"])
    return result


def _integration_weights(parsed: dict[str, Any]) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Analytic integral of each declared temporal basis in each native row window."""
    control, timeline, base = parsed["control"], parsed["timeline"], parsed["base"]
    knots = [knot["time_ns"] for knot in timeline["knots"]]
    frames = control["frames"]
    weights = np.zeros((len(frames), len(parsed["unique"]), base["size"][0]), dtype=float)
    windows = []
    linear = timeline["interpolation"] == "linear_irradiance"
    for frame_index, frame in enumerate(frames):
        window = row_windows(frame, control["mode"], base["geometry"], base["size"])
        windows.append(window)
        for index, (left, right) in enumerate(zip(knots, knots[1:], strict=False)):
            for row, (start, end) in enumerate(zip(
                window["row_start_ns"], window["row_end_ns"], strict=True
            )):
                low, high = max(start, left), min(end, right)
                if high <= low:
                    continue
                duration = high - low  # Integer subtraction before conversion to seconds.
                right_seconds = (duration * ((high - left) + (low - left))
                                 / (2 * (right - left)) * 1e-9 if linear else 0.0)
                left_seconds = duration * 1e-9 - right_seconds
                weights[frame_index, parsed["scene_indices"][index], row] += left_seconds
                weights[frame_index, parsed["scene_indices"][index + 1], row] += right_seconds
        expected = frame["applied"]["exposure_ns"] * 1e-9
        if not np.allclose(weights[frame_index].sum(axis=0), expected, rtol=2e-14, atol=0):
            raise RuntimeError("Temporal integration basis did not cover every row exposure")
    return weights, windows


class TemporalSceneRuntime:
    """Bounded image-plane temporal capture from recorded applied camera settings."""

    plugin_id = PLUGIN_ID

    def describe_capabilities(self) -> dict[str, Any]:
        import importlib.util

        return {
            "plugin_id": self.plugin_id, "role": "runtime", "version": "1.0",
            "mode": "temporal_scene",
            "available": importlib.util.find_spec("pyisetcam") is not None,
            "supports": ["declared_spectral_image_timeline", "exact_linear_or_zoh_integration",
                         "native_rolling_or_global_exposure", "recorded_applied_settings",
                         "one_physical_readout_per_frame", "per_frame_configured_isp",
                         "supplied_field_psf", "bilinear_source_position_psf",
                         "source_position_relative_illumination_opt_in",
                         "native_four_site_absolute_response"],
            "unsupported": ["scene3d", "occlusion", "geometric_motion_interpolation",
                            "closed_loop_ae_awb", "hdr", "electronic_transport_simulation",
                            "depth_dependent_psf", "general_distortion_map", "depth_reprojection",
                            "temperature_dynamics", "physical_validation"],
            "limits": {"max_frames": MAX_FRAMES, "max_knots": MAX_KNOTS,
                       "max_frame_pixels": MAX_FRAME_PIXELS,
                       "max_unique_spectral_samples": MAX_UNIQUE_SPECTRAL_SAMPLES,
                       "max_field_fft_work_units": MAX_FIELD_FFT_WORK,
                       "max_working_array_bytes": MAX_WORKING_ARRAY_BYTES,
                       "max_output_array_bytes": MAX_OUTPUT_ARRAY_BYTES,
                       "max_native_direction_weighted_samples": MAX_NATIVE_DIRECTION_WORK},
            "charge_collection": charge_collection_capabilities(),
            "physical_validation": "not_established",
        }

    def validate_request(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            _parse(request, allow_source_ri=True)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return getattr(exc, "issues", [_issue("request", str(exc))])
        return []

    def _checked(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return _parse(request, allow_source_ri=True)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            issues = getattr(exc, "issues", [_issue("request", str(exc))])
            raise RuntimeRequestError(issues) from exc

    def estimate_resources(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._checked(request)["resources"]

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any]:
        parsed = self._checked(request)
        return self._execute_parsed(parsed, self._optical_rates(parsed))

    def prepare_capture(self, request: Mapping[str, Any]) -> TemporalCaptureSession:
        """Prepare immutable optics once for causal frame-by-frame controllers."""
        parsed = self._checked(request)
        return TemporalCaptureSession(parsed, self._optical_rates(parsed))

    @staticmethod
    def _optical_rates(parsed):
        base = parsed["base"]
        sites = len(base.get("channels", CHANNELS))
        rates = np.empty((len(parsed["unique"]), *base["size"], sites), dtype=float)
        flux = []
        collection_arrays = {}
        collection_shape = _collection_support_shape(base)
        source_ri_map = None
        for index, scene_request in enumerate(parsed["unique"]):
            response = photoelectron_rate(scene_request)
            rates[index] = response["arrays"]["channel_photoelectron_rate"]
            diagnostics = dict(response["summary"]["flux_diagnostics"])
            if base["field_radiometry_mode"] == SOURCE_RI_MODE:
                candidate = response["arrays"]["source_relative_illumination_map"]
                if source_ri_map is None:
                    source_ri_map = candidate.copy()
                elif not np.array_equal(source_ri_map, candidate):
                    raise RuntimeError("Temporal source-position RI maps differ across knots")
            if "charge_collection" in base:
                diagnostics["charge_collection"] = response["summary"]["charge_collection"]
                for key in ("collection_generated_rate", "collection_intrinsic_loss_rate",
                            "collection_off_sensor_loss_rate",
                            "collection_outside_support_collected_rate",
                            "source_support_collected_photoelectron_rate"):
                    if key not in response["arrays"]:
                        continue
                    if key + "_scenes" not in collection_arrays:
                        collection_arrays[key + "_scenes"] = np.empty(
                            (len(parsed["unique"]), *collection_shape), dtype=float)
                    collection_arrays[key + "_scenes"][index] = response["arrays"][key]
            flux.append(diagnostics)
        if collection_arrays:
            parsed["collection_rate_arrays"] = collection_arrays
        if source_ri_map is not None:
            parsed["source_ri_map"] = source_ri_map
        radius = response["arrays"]["radius_map"]
        cfa = response["arrays"]["cfa_channel_index"]
        ri_map = response["arrays"]["relative_illumination_map"]
        return rates, flux, radius, cfa, ri_map

    def _execute_parsed(self, parsed, optics):
        from camera_platform.isp import process_raw

        base, control = parsed["base"], parsed["control"]
        site_names = list(base.get("channels", CHANNELS))
        site_count = len(site_names)
        weights, windows = _integration_weights(parsed)
        rates, flux, radius, cfa, ri_map = optics
        # [frame, scene, row] × [scene, row, column, channel]. The optical
        # response is linear; clipping and dark/random charge are applied later.
        with np.errstate(over="ignore", invalid="ignore"):
            channels = np.einsum("fsr,srwc->frwc", weights, rates, optimize=False)
        exposures = np.array([f["applied"]["exposure_ns"] * 1e-9 for f in control["frames"]])
        dark = exposures * base["readout"]["dark_current_e_per_s"]
        if (not np.isfinite(channels).all() or np.any(channels < 0)
                or np.any(channels.max(axis=(1, 2, 3)) + dark > 1e13)):
            raise RuntimeRequestError([_issue("exposure", "Integrated charge exceeds 1e13 domain")])
        means = np.take_along_axis(channels, cfa[None, ..., None], axis=-1)[..., 0]
        readout = base["readout"]
        raw_format = {
            "cfa": "RGGB", "bit_depth": readout["bit_depth"],
            "black_level_dn": readout["black_level_dn"], "response_type": "linear",
            "packing": "unpacked", "layout": "bayer", "hdr_streams": 1,
        }
        raw_frames, references, channel_refs, metadata, isp_summaries = [], [], [], [], []
        processed_arrays: dict[str, list[np.ndarray]] = {}
        expected_means, sampled_means, conditions, unsaturated = [], [], [], []
        for index, frame in enumerate(control["frames"]):
            applied = frame["applied"]
            frame_readout = {**readout, "gain": applied["gain"]}
            frame_inputs = {**base, "exposure": exposures[index], "frames": 1,
                            "seed": base["seed"] + index, "readout": frame_readout}
            raw, reference, capture = _capture(frame_inputs, means[index])
            profile = parsed["profiles"][applied["isp_model_id"]]
            if "wb_gains_rgb" in applied:
                profile = deepcopy(profile)
                profile["white_balance"]["gains_rgb"] = applied["wb_gains_rgb"]
            processed = process_raw(raw, raw_format, profile,
                                    geometry=base["geometry"])
            white, black = 2 ** readout["bit_depth"] - 1, readout["black_level_dn"]
            scale = (white - black) * applied["gain"] / readout["full_well_e"]
            baseline = np.clip(black + min(dark[index], readout["full_well_e"]) * scale, 0, white)
            channel_ref = np.clip(
                black + np.minimum(channels[index] + dark[index], readout["full_well_e"])
                * scale, 0, white
            ) - baseline
            expected_means.append([float(channel_ref[..., channel][cfa == channel].mean())
                                   for channel in range(site_count)])
            sampled_means.append([float((raw[0].astype(float) - baseline)[cfa == channel].mean())
                                  for channel in range(site_count)])
            capacity = min(readout["full_well_e"], readout["full_well_e"] / applied["gain"])
            unsaturated.append(bool((channels[index] + dark[index] < capacity).all()))
            raw_frames.append(raw[0])
            references.append(reference)
            channel_refs.append(channel_ref)
            metadata.append({"frame_id": frame["frame_id"], **capture[0]})
            isp_summaries.append({"frame_id": frame["frame_id"],
                                  "isp_model_id": applied["isp_model_id"],
                                  **processed["summary"]})
            for key, array in processed["arrays"].items():
                if key != "rgb":
                    processed_arrays.setdefault(key, []).append(array[0])
            condition = {"exposure_s": {"value": float(exposures[index]), "unit": "s"},
                         "gain": {"value": applied["gain"], "unit": "1"}}
            if base["temperature"] is not None:
                condition["temperature_c"] = {"value": base["temperature"], "unit": "degC"}
            conditions.append({"frame_id": frame["frame_id"], "conditions": condition})
        frame_ids = [frame["frame_id"] for frame in control["frames"]]
        arrays = {
            "channel_photoelectron_rate_scenes": rates,
            "row_scene_integration_weights_s": weights,
            "channel_electrons": channels, "mean_electrons": means,
            "channel_reference_signal_dn": np.stack(channel_refs),
            "reference_dn": np.stack(references), "raw_frames": np.stack(raw_frames),
            "radius_map": radius, "cfa_channel_index": cfa, "relative_illumination_map": ri_map,
            "psf_intensity": base["kernels"].copy(),
            **_field_psf_arrays(base),
            **_radial_response_arrays(base),
            **_native_response_arrays(base),
            **parsed.get("collection_rate_arrays", {}),
            "row_start_ns": np.array([w["row_start_ns"] for w in windows], dtype=np.int64),
            "row_end_ns": np.array([w["row_end_ns"] for w in windows], dtype=np.int64),
            "frame_start_ns": np.array([f["frame_start_ns"] for f in control["frames"]],
                                       dtype=np.int64),
            **{key: np.stack(value) for key, value in processed_arrays.items()},
        }
        if base["field_radiometry_mode"] == SOURCE_RI_MODE:
            arrays["source_relative_illumination_map"] = parsed["source_ri_map"].copy()
        arrays["rgb"] = arrays["rgb_frames"][0].copy()
        if any(array.nbytes > MAX_OUTPUT_ARRAY_BYTES for array in arrays.values()):
            raise ValueError("Temporal capture output exceeds the per-array storage bound")
        array_units = {
            "channel_photoelectron_rate_scenes": "electron/pixel/s",
            "row_scene_integration_weights_s": "s", "channel_electrons": "electron/pixel",
            "mean_electrons": "electron/pixel", "channel_reference_signal_dn": "DN",
            "reference_dn": "DN", "raw_frames": "DN", "radius_map": "1",
            "cfa_channel_index": "index", "relative_illumination_map": "1", "psf_intensity": "1",
            "row_start_ns": "ns", "row_end_ns": "ns", "frame_start_ns": "ns",
            **isp_summaries[0]["array_units"],
            **{name: "mm" for name in _field_psf_arrays(base)},
            **_native_response_units(base),
            **{key: "electron/pixel/s" for key in parsed.get("collection_rate_arrays", {})},
        }
        if base["field_radiometry_mode"] == SOURCE_RI_MODE:
            array_units["source_relative_illumination_map"] = "1"
        summary = {
            "schema_version": "1.0", "plugin_id": self.plugin_id, "source_kind": "simulated",
            "physical_validation": "not_established", "uncertainty": {"kind": "unknown"},
            "engine": "spectral PSF/RI/QE + analytic exposure + pyisetcam readout + ISP",
            "channels": site_names.copy(), "wavelength_nm": base["wave"].tolist(),
            "output_size": list(base["size"]), "frames": len(frame_ids), "frame_ids": frame_ids,
            "geometry": base["geometry"], "pixel_pitch_um": base["pitch"],
            **_collection_geometry_summary(base, parsed.get("collection_rate_arrays")),
            **_source_ri_summary(base),
            **({"source_relative_illumination_map_scope": "same_fixed_source_grid_all_knots"}
               if base["field_radiometry_mode"] == SOURCE_RI_MODE else {}),
            **_radial_response_summary(base),
            **_native_response_summary(base),
            "seed_sequence": [base["seed"] + index for index in range(len(frame_ids))],
            "noise_enabled": base["noise_enabled"],
            "scene_interpolation": parsed["timeline"]["interpolation"],
            "scene_knots": [{"time_ns": knot["time_ns"], "scene_index": index,
                             "scene_content_hash": parsed["scene_hashes"][index]}
                            for knot, index in zip(parsed["timeline"]["knots"],
                                                   parsed["scene_indices"], strict=True)],
            "scene_sources": [item["scene"]["source"] for item in parsed["unique"]],
            "psf_source": base["psf_source"], "flux_diagnostics_per_scene": flux,
            "control_sequence": control, "timing": summarize_timing(
                control, geometry=base["geometry"], frame_shape=base["size"]),
            "coverage": parsed["coverage"], "raw_format": raw_format,
            "conditions_per_frame": conditions,
            "included_effects": with_sensor_effects(TEMPORAL_EFFECTS, base),
            "native_cfa_channel_mean_dn": expected_means,
            "sampled_raw_dark_corrected_channel_mean_dn": sampled_means,
            "linear_unsaturated_per_frame": unsaturated, "capture_metadata": metadata,
            "readout_count": len(frame_ids), "readout_parameters": {
                key: value for key, value in readout.items() if key != "gain"},
            "fixed_pattern_noise": {"prnu_sigma": 0, "dsnu_sigma_v": 0, "column_fpn": [0, 0]},
            "isp_per_frame": isp_summaries, "array_units": array_units,
            "prediction": {
                "metric_id": "raw.channel_mean_dn_sequence", "unit": "DN",
                "values": np.asarray(expected_means).reshape(-1).tolist(),
                "axes": [{"name": "frame", "unit": "1", "values": frame_ids},
                         {"name": "channel", "unit": "1", "values": site_names.copy()}],
                "reference_plane": "sensor_raw",
                "included_effects": with_sensor_effects(TEMPORAL_EFFECTS, base),
                "conditions": {}, "conditions_per_frame": conditions, "source_kind": "simulated",
                "method_and_version": "continuous_expected_temporal_native_cfa_dark_corrected_v1",
                "uncertainty": {"kind": "unknown"},
            },
            "integration": "Analytic declared irradiance basis; wavelength trapezoid per nm",
            "temporal_noise_estimation": "not_computed; frame variance includes scene motion",
            "limitations": [
                "Exact for the declared linear/ZOH image timeline, not exact geometric motion.",
                "Spectral image inputs already contain on-axis aperture/transmission throughput.",
                ("Fixed source-position RI precedes conditional unit-sum PSF; "
                 "receiver angular QE remains separable."
                 if base["field_radiometry_mode"] == SOURCE_RI_MODE else
                 "Fixed LSI pixel-integrated PSF precedes output-position RI/QE."
                 if base["field_psf"] is None else
                 "Fixed source-position field PSF precedes output-position RI/QE "
                 "as a separable approximation; intermediate field response is unverified."),
                "Supplied wavelength/field kernel offsets remain fixed without recentering; "
                "their geometric or chromatic lens interpretation requires source evidence.",
                "Native image exterior is zero; cropped-out content is unknown.",
                "One dark/noise/well/ADC readout per frame after exposure integration.",
                "PRNU, DSNU and column fixed patterns are explicitly zero throughout the sequence.",
                "Applied settings are fixed across each frame; no mid-frame register changes.",
                "Recorded command/transport times are retained, never synthesized or fitted.",
                "No 3D, occlusion, separate general distortion-map warp, depth reprojection, "
                "HDR, closed-loop AE/AWB or physical validation.",
                "Temperature is fixed metadata; QE/readout must already correspond to it.",
                "RAW expectations differ from ADC realizations; motion variance is not SNR.",
            ],
        }
        if base["field_radiometry_mode"] == SOURCE_RI_MODE:
            summary["prediction"]["method_and_version"] = (
                "continuous_expected_temporal_native_cfa_dark_corrected_source_ri_v1")
            summary["limitations"].append(
                "Source-position RI is fixed across declared knots; receiver angular QE "
                "remains a separable approximation, with no source-direction coupling.")
        if base["field_psf"] is not None:
            summary["psf"] = _psf_summary(base)
        if "field_reference_radius_mm" in base:
            summary["array_units"].update(sensor_radius_mm="mm", response_radius_map="1")
        return {"summary": summary, "arrays": arrays}


class TemporalCaptureSession:
    """Prepared rates with one independent physical readout per explicit frame.

    The caller supplies the complete already-applied frame and, when referenced,
    its issued command. This class does not schedule or infer controller actions.
    """

    def __init__(self, parsed, optics):
        # Rates have already absorbed the ideal images. Retain only the source
        # identities needed by frame summaries, and snapshot the small mutable
        # contracts so caller edits cannot alter later frames in this session.
        self._parsed = deepcopy({
            **parsed,
            "base": {k: v for k, v in parsed["base"].items() if k != "image"},
            "unique": [{"scene": {"source": item["scene"]["source"]}}
                       for item in parsed["unique"]],
        })
        rates, flux, radius, cfa, ri_map = optics
        for array in (rates, radius, cfa, ri_map):
            array.flags.writeable = False
        for array in self._parsed.get("collection_rate_arrays", {}).values():
            array.flags.writeable = False
        if "source_ri_map" in self._parsed:
            self._parsed["source_ri_map"].flags.writeable = False
        self._optics = rates, deepcopy(flux), radius, cfa, ri_map

    @property
    def geometry(self):
        return deepcopy(self._parsed["base"]["geometry"])

    @property
    def frame_shape(self):
        return list(self._parsed["base"]["size"])

    def capture_frame(self, frame, *, seed_index: int, command=None):
        if type(seed_index) is not int or not 0 <= seed_index < 64:
            raise ValueError("Frame seed index must be an integer in [0,63]")
        item = deepcopy(frame)
        if command is not None:
            item["commanded"] = deepcopy(command)
        parsed = self._parsed
        control = validate_control_sequence(
            {**parsed["control"], "frames": [item]}, geometry=self.geometry,
            frame_shape=self.frame_shape,
        )
        coverage = validate_timing_coverage(
            parsed["timeline"], control, geometry=self.geometry,
            frame_shape=self.frame_shape, expanded=True,
        )
        checked_frame = control["frames"][0]
        ids = {checked_frame["applied"]["isp_model_id"]}
        if "commanded" in checked_frame:
            ids.add(checked_frame["commanded"]["isp_model_id"])
        if not ids <= set(parsed["profiles"]):
            raise ValueError("Frame references an ISP absent from the prepared capture")
        selected = {**parsed, "control": control, "coverage": coverage,
                    "base": {**parsed["base"], "seed": parsed["base"]["seed"] + seed_index}}
        output = TemporalSceneRuntime()._execute_parsed(selected, self._optics)
        output["summary"] = deepcopy(output["summary"])
        for key in ("channel_photoelectron_rate_scenes", "radius_map",
                    "cfa_channel_index", "relative_illumination_map",
                    *parsed.get("collection_rate_arrays", {})):
            # A view of a read-only base cannot accidentally be made writable.
            output["arrays"][key] = output["arrays"][key].view()
        return output


def request_template() -> dict[str, Any]:
    """Authored moving step, explicitly synthetic; no captured settings or hardware claim."""
    from camera_platform.isp import default_profile
    from camera_platform.spatial import request_template as spatial_template

    spatial = spatial_template()
    scene = spatial.pop("scene")
    for key in ("exposure_s", "frames", "isp_profile"):
        spatial.pop(key, None)
    clock = {"clock_id": "authored-relative-clock", "unit": "ns", "reference": "relative",
             "origin": "Synthetic fixture start; not a hardware clock observation"}
    second = deepcopy(scene)
    image = np.full((64, 64, 5), 1e-5)
    image[:, 40:] *= 4
    second["spectral_irradiance"] = image.tolist()
    return {**spatial,
            "scene_sequence": {"schema": "timed_spectral_scene_v1", "clock": clock,
                "interpolation": "linear_irradiance", "knots": [
                    {"time_ns": 0, "scene": scene}, {"time_ns": 20_000_000, "scene": second}]},
            "control_sequence": {"schema": "recorded_control_sequence_v1", "clock": clock,
                "mode": {"shutter": "global", "line_time_ns": 0, "hidden_top_rows": 0,
                         "readout_basis": "full_sensor_rows"},
                "frames": [{"frame_id": f"synthetic-frame-{index}",
                    "frame_start_ns": index * 10_000_000,
                    "applied": {"applied_at_ns": 0, "exposure_ns": 4_000_000, "gain": 1,
                                "isp_model_id": "authored-isp", "command_id": None}}
                           for index in range(2)]},
            "isp_profiles": {"authored-isp": default_profile()}}
