"""Bounded image-quality diagnostics for one pinned sensor-simulation run.

This path executes the immutable sensor model and native readout selected by an
explicit ``run_id``. It uses the F/4, 4 mm ideal optics recorded by the saved
simulation, then evaluates static D65 uniform, slanted-edge, Macbeth or HDR
strip-chart scenes. The strip chart diagnoses one exposure; it does not combine
exposures or model HDR/LFM readout.
Temporal SNR comes from independent RAW captures, edge MTF50 from the temporal
mean of fixed linear RGB, and Macbeth CIE76 from an in-sample D65 calibration
fit. The results describe this modeled camera chain, not intrinsic sensor MTF,
independent color accuracy, measured product performance or a lens DB revision.

``output_dir`` must be new or empty. The function writes a source report and
numeric NPZ evidence there; it never modifies the sensor-simulation database.
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
from pyisetcam.scene import (
    macbeth_ideal_color,
    scene_adjust_luminance,
    scene_calculate_luminance,
    scene_create,
    scene_set,
)
from pyisetcam.sensor_capture import resample_sensor_capture
from pyisetcam.sensor_comparison import (
    _COLOR_EVALUATION,
    _color_metrics,
    _edge_metrics,
    _fixed_isp_matrices,
    _linear_isp,
    _make_scene,
    _Moments,
    _physical_geometry,
    _physical_resample,
    _snr_metrics,
)
from pyisetcam.sensor_simulation_db import (
    _digest,
    sensor_simulation_db_config,
    sensor_simulation_db_get,
)
from pyisetcam.system_faca import camerae2e_run_scenario

from .offline_assets import offline_asset_identity, offline_asset_store
from .sensor_db_bridge import inspect_sensor_revision

SCHEMA = "camerae2e_saved_sensor_quality_v1"
_SCENES = frozenset({"uniform", "slanted_edge", "macbeth_d65", "hdr_chart_d65"})
_LUMA_WEIGHTS = np.array([0.213, 0.715, 0.072], dtype=float)
_FIXED_FNUMBER = 4.0
_FIXED_FOCAL_LENGTH_M = 0.004
_HDR_DYNAMIC_RANGE = 1000.0
_HDR_LEVELS = 6
_HDR_COLUMNS_PER_LEVEL = 8


def _bounded_real(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{name} must be a finite number in [{low}, {high}].")
    return number


def _bounded_int(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}].")
    return value


def _metric(value: Any, unit: str, *, reason: str) -> dict[str, Any]:
    if value is None or not math.isfinite(float(value)):
        return {"available": False, "unit": unit, "reason": reason}
    return {"available": True, "value": float(value), "unit": unit}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding_matches(sensor: Any, revision: Mapping[str, Any]) -> bool:
    binding = sensor.metadata.get("sensor_simulation_db")
    return bool(
        isinstance(binding, Mapping)
        and binding.get("run_id") == revision["run_id"]
        and binding.get("report_code") == revision["report_code"]
        and binding.get("result_sha256") == revision["result_sha256"]
    )


def _hdr_chart_scene(
    output_size: tuple[int, int], fov_deg: float, store: AssetStore
) -> tuple[Any, dict[str, Any]]:
    """Place the existing D65 strip chart inside the comparison field of view."""
    scene, _ = _make_scene("uniform", output_size, fov_deg, store)
    chart = scene_create(
        "hdr chart",
        _HDR_DYNAMIC_RANGE,
        _HDR_LEVELS,
        _HDR_COLUMNS_PER_LEVEL,
        asset_store=store,
    )
    canvas_shape = np.asarray(scene.data["photons"]).shape
    chart_photons = np.asarray(chart.data["photons"], dtype=float)
    chart_rows, chart_cols = chart_photons.shape[:2]
    canvas_rows, canvas_cols = canvas_shape[:2]
    if chart_photons.shape[2] != canvas_shape[2] or not np.array_equal(
        chart.fields["wave"], scene.fields["wave"]
    ):
        raise ValueError("HDR chart and saved quality D65 canvas have different wavelength grids.")
    if chart_rows > canvas_rows or chart_cols > canvas_cols:
        raise ValueError("HDR chart does not fit inside the bounded comparison scene.")
    top, left = (canvas_rows - chart_rows) // 2, (canvas_cols - chart_cols) // 2
    # Match the background to the darkest strip, not a new illuminant or a
    # fabricated response. The chart generator supplies every spectral value.
    dark_spectrum = np.mean(chart_photons[:, -_HDR_COLUMNS_PER_LEVEL :, :], axis=(0, 1))
    photons = np.broadcast_to(dark_spectrum, canvas_shape).copy()
    photons[top : top + chart_rows, left : left + chart_cols] = chart_photons
    scene_set(scene, "photons", photons)
    scene_set(scene, "fov", fov_deg)
    factors = np.asarray(output_size, dtype=float) / np.asarray(
        [canvas_rows, canvas_cols], dtype=float
    ) * 1.2
    levels = []
    reflectances = np.logspace(0.0, -np.log10(_HDR_DYNAMIC_RANGE), _HDR_LEVELS)
    for index, reflectance in enumerate(reflectances):
        source_roi = [
            top + chart_rows * 0.2,
            left + (index + 0.2) * _HDR_COLUMNS_PER_LEVEL,
            top + chart_rows * 0.8,
            left + (index + 0.8) * _HDR_COLUMNS_PER_LEVEL,
        ]
        output_roi = [
            (source_roi[0] - canvas_rows / 2) * factors[0] + output_size[0] / 2,
            (source_roi[1] - canvas_cols / 2) * factors[1] + output_size[1] / 2,
            (source_roi[2] - canvas_rows / 2) * factors[0] + output_size[0] / 2,
            (source_roi[3] - canvas_cols / 2) * factors[1] + output_size[1] / 2,
        ]
        levels.append(
            {
                "index": index,
                "declared_reflectance": float(reflectance),
                "source_roi_half_open": [float(value) for value in source_roi],
                "output_roi_half_open": [float(value) for value in output_roi],
            }
        )
    return scene, {
        "generator": {
            "api": "pyisetcam.scene_create",
            "name": "hdr chart",
            "args": [_HDR_DYNAMIC_RANGE, _HDR_LEVELS, _HDR_COLUMNS_PER_LEVEL],
            "illuminant": "D65",
            "background": "chart_darkest_strip_mean_spectrum",
            "scene_to_crop_scale": 1.2,
        },
        "scene_horizontal_fov_deg": float(fov_deg),
        "canvas_size": [canvas_rows, canvas_cols],
        "chart_size": [chart_rows, chart_cols],
        "chart_origin": [top, left],
        "levels": levels,
    }


def _hdr_native_rois(
    scene_geometry: Mapping[str, Any],
    output_size: tuple[int, int],
    native_size: tuple[int, int],
) -> list[list[int]]:
    offsets = [
        (native - output) // 2
        for native, output in zip(native_size, output_size, strict=True)
    ]
    rois = []
    for level in scene_geometry["levels"]:
        y0, x0, y1, x1 = level["output_roi_half_open"]
        roi = [
            math.ceil(y0) + offsets[0],
            math.ceil(x0) + offsets[1],
            math.floor(y1) + offsets[0],
            math.floor(x1) + offsets[1],
        ]
        if (
            roi[0] < 0
            or roi[1] < 0
            or roi[2] > native_size[0]
            or roi[3] > native_size[1]
            or roi[2] <= roi[0]
            or roi[3] <= roi[1]
        ):
            raise ValueError("HDR chart strip has no interior native RAW pixels.")
        rois.append(roi)
    return rois


def _scene_metrics(
    kind: str,
    *,
    cached: Any,
    matrices: tuple[np.ndarray, np.ndarray] | None,
    pitch_m: float,
    geometry: Mapping[str, Any],
    output_size: tuple[int, int],
    scene_geometry: Mapping[str, Any],
    ideal: np.ndarray | None,
    frames: int,
    frame_seeds: list[int],
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Compute metrics from one optical solve and actual independent captures."""
    dark = cached.clone()
    dark.data["mean_electrons"] = np.zeros_like(cached.data["mean_electrons"])
    dark = resample_sensor_capture(dark, seed=frame_seeds[0])
    raw_signal = np.asarray(cached.data["reference_dn"], dtype=float) - np.asarray(
        dark.data["reference_dn"], dtype=float
    )
    raw_moments = _Moments()
    rgb_moments, xyz_moments, luma_moments = _Moments(), _Moments(), _Moments()
    saturation: dict[str, list[float]] = {}
    hdr_rois = (
        [level["native_roi_half_open"] for level in scene_geometry["levels"]]
        if kind == "hdr_chart_d65"
        else []
    )
    hdr_white_counts = np.zeros(len(hdr_rois), dtype=np.int64)
    hdr_zero_counts = np.zeros(len(hdr_rois), dtype=np.int64)
    hdr_raw_frames: list[np.ndarray] = []
    white_dn = 2 ** int(cached.fields["sensor_readout"]["bit_depth"]) - 1
    first_raw: np.ndarray | None = None
    first_rgb: np.ndarray | None = None
    reference_rgb = dark_rgb = None
    if matrices is not None:
        reference_rgb_native, _ = _linear_isp(cached, cached.data["reference_dn"], matrices)
        dark_rgb_native, _ = _linear_isp(dark, dark.data["reference_dn"], matrices)
        reference_rgb = _physical_resample(
            reference_rgb_native, pitch_m, geometry["common_extent_m"], output_size
        )
        dark_rgb = _physical_resample(
            dark_rgb_native, pitch_m, geometry["common_extent_m"], output_size
        )
    for frame_seed in frame_seeds:
        captured = resample_sensor_capture(cached, seed=frame_seed)
        raw = np.asarray(captured.data["raw_dn"])
        if raw.dtype != np.dtype("int32") or raw.shape != tuple(cached.fields["size"]):
            raise ValueError("Saved sensor quality capture returned unsupported RAW data.")
        raw_moments.add(raw)
        if first_raw is None:
            first_raw = raw.copy()
        if kind == "hdr_chart_d65":
            hdr_raw_frames.append(raw.copy())
        for index, (y0, x0, y1, x1) in enumerate(hdr_rois):
            region = raw[y0:y1, x0:x1]
            hdr_white_counts[index] += int(np.count_nonzero(region == white_dn))
            hdr_zero_counts[index] += int(np.count_nonzero(region == 0))
        for key, value in captured.metadata["sensor_capture"]["saturation"].items():
            saturation.setdefault(key, []).append(float(value))
        if matrices is not None:
            rgb_native, xyz_native = _linear_isp(captured, raw, matrices)
            rgb = _physical_resample(
                rgb_native, pitch_m, geometry["common_extent_m"], output_size
            )
            xyz = _physical_resample(
                xyz_native, pitch_m, geometry["common_extent_m"], output_size
            )
            rgb_moments.add(rgb)
            xyz_moments.add(xyz)
            luma_moments.add(rgb @ _LUMA_WEIGHTS)
            if first_rgb is None:
                first_rgb = rgb.copy()
    assert first_raw is not None and raw_moments.mean is not None
    saturated = {key: float(np.mean(values)) for key, values in saturation.items()}
    ranking_eligible = all(value <= 0.001 for value in saturated.values())
    raw_snr = _metric(
        _snr_metrics(raw_signal, raw_moments.variance)["snr_db"],
        "dB",
        reason="No positive temporal RAW signal-to-noise ratio could be estimated.",
    )
    raw_snr["ranking_eligible"] = ranking_eligible
    if not ranking_eligible:
        raw_snr["interpretation"] = "Clipped capture: diagnostic SNR only."
    metrics: dict[str, Any] = {
        "native_raw_snr_db": raw_snr,
        "mean_photoelectrons": float(np.mean(cached.data["mean_electrons"])),
        "saturation_fractions": saturated,
        "frame_count": frames,
    }
    arrays: dict[str, np.ndarray] = {
        "raw_dn_frame": first_raw.astype(np.int32, copy=False),
        "raw_dn_mean": np.asarray(raw_moments.mean, dtype=np.float32),
        "raw_dn_variance": np.asarray(raw_moments.variance, dtype=np.float32),
        "mean_photoelectrons": np.asarray(cached.data["mean_electrons"], dtype=np.float32),
        "raw_reference_dn": np.asarray(cached.data["reference_dn"], dtype=np.float32),
        "raw_dark_reference_dn": np.asarray(dark.data["reference_dn"], dtype=np.float32),
    }
    if kind == "hdr_chart_d65":
        arrays["hdr_chart_raw_dn_frames"] = np.stack(hdr_raw_frames, axis=0)
        mean_electrons = np.asarray(cached.data["mean_electrons"], dtype=float)
        reference_dn = np.asarray(cached.data["reference_dn"], dtype=float)
        dark_reference_dn = np.asarray(dark.data["reference_dn"], dtype=float)
        readout = cached.fields["sensor_readout"]
        dark_charge = float(readout["dark_current_e_per_s"]) * float(
            cached.fields["integration_time"]
        )
        full_well = float(readout["full_well_e"])
        hdr_levels = []
        for index, (level, (y0, x0, y1, x1)) in enumerate(
            zip(scene_geometry["levels"], hdr_rois, strict=True)
        ):
            region = np.s_[y0:y1, x0:x1]
            count = (y1 - y0) * (x1 - x0)
            hdr_levels.append(
                {
                    "index": index,
                    "declared_reflectance": level["declared_reflectance"],
                    "scene_luminance_cd_m2": level["scene_luminance_cd_m2"],
                    "native_roi_half_open": [y0, x0, y1, x1],
                    "native_pixel_count": count,
                    "mean_photoelectrons_per_pixel": float(np.mean(mean_electrons[region])),
                    "mean_reference_dn": float(np.mean(reference_dn[region])),
                    "mean_dark_subtracted_reference_dn": float(
                        np.mean(reference_dn[region] - dark_reference_dn[region])
                    ),
                    "mean_raw_dn": float(np.mean(raw_moments.mean[region])),
                    "sampled_adc_white_fraction": float(
                        hdr_white_counts[index] / (frames * count)
                    ),
                    "sampled_adc_zero_fraction": float(hdr_zero_counts[index] / (frames * count)),
                    "mean_charge_above_well_fraction": float(
                        np.mean(mean_electrons[region] + dark_charge >= full_well)
                    ),
                }
            )
        metrics["hdr_chart_response"] = {
            "available": True,
            "contract": "single_exposure_static_d65_strip_chart_response",
            "input_dynamic_range_ratio": _HDR_DYNAMIC_RANGE,
            "levels_bright_to_dark": hdr_levels,
            "sampled_well_clip_fraction": {
                "available": False,
                "reason": "The capture retains only whole-frame sampled well clipping fractions.",
            },
            "whole_frame_sampled_well_clip_fraction": saturated["well_fraction"],
            "mean_charge_above_well_fraction_method": (
                "Pre-readout mean photoelectrons plus mean dark charge, without "
                "stochastic shot noise or fixed-pattern gain."
            ),
            "hdr_fusion_performed": False,
            "lfm_evaluated": False,
        }
        for field in (
            "mean_photoelectrons_per_pixel",
            "mean_reference_dn",
            "mean_dark_subtracted_reference_dn",
            "mean_raw_dn",
            "sampled_adc_white_fraction",
            "sampled_adc_zero_fraction",
            "mean_charge_above_well_fraction",
        ):
            arrays[f"hdr_chart_{field}"] = np.asarray(
                [level[field] for level in hdr_levels], dtype=np.float32
            )
    if matrices is None:
        unavailable = {"available": False, "reason": "Saved native CFA has no standard RGB ISP."}
        metrics["common_output_snr_db"] = unavailable.copy()
        if kind == "slanted_edge":
            metrics["edge_mtf50"] = unavailable.copy()
        if kind == "macbeth_d65":
            metrics["macbeth_cie76_mean"] = unavailable.copy()
        return metrics, arrays
    assert reference_rgb is not None and dark_rgb is not None
    assert rgb_moments.mean is not None and xyz_moments.mean is not None
    output_signal = (reference_rgb - dark_rgb) @ _LUMA_WEIGHTS
    output_snr = _metric(
        _snr_metrics(output_signal, luma_moments.variance)["snr_db"],
        "dB",
        reason="No positive temporal output signal-to-noise ratio could be estimated.",
    )
    output_snr["ranking_eligible"] = ranking_eligible
    if not ranking_eligible:
        output_snr["interpretation"] = "Clipped capture: diagnostic SNR only."
    metrics["common_output_snr_db"] = output_snr
    arrays.update(
        rgb_frame=np.asarray(first_rgb, dtype=np.float32),
        rgb_mean=np.asarray(rgb_moments.mean, dtype=np.float32),
        rgb_variance=np.asarray(rgb_moments.variance, dtype=np.float32),
        xyz_mean=np.asarray(xyz_moments.mean, dtype=np.float32),
        xyz_variance=np.asarray(xyz_moments.variance, dtype=np.float32),
        luma_variance=np.asarray(luma_moments.variance, dtype=np.float32),
        rgb_reference=np.asarray(reference_rgb, dtype=np.float32),
    )
    if kind == "slanted_edge":
        edge = _edge_metrics(rgb_moments.mean)
        metrics["edge_mtf50"] = (
            {
                "available": True,
                "value": edge["mtf50_cycles_per_output_pixel"],
                "unit": "cycles/output_pixel",
                "method": edge["method"],
            }
            if edge["available"]
            else {"available": False, "reason": edge["reason"]}
        )
        arrays["edge_frequency_cycles_per_output_pixel"] = np.asarray(
            edge.get("frequency_cycles_per_output_pixel", []), dtype=np.float32
        )
        arrays["edge_luminance_mtf"] = np.asarray(edge.get("luminance_mtf", []), dtype=np.float32)
    if kind == "macbeth_d65":
        assert ideal is not None
        color = _color_metrics(xyz_moments.mean, scene_geometry["patch_boxes"], ideal)
        metrics["macbeth_cie76_mean"] = (
            {
                "available": True,
                "value": color["mean_delta_e"],
                "unit": "CIE76_delta_e",
                "evaluation_contract": _COLOR_EVALUATION["contract"],
                "independent_color_accuracy": False,
            }
            if color["available"]
            else {"available": False, "reason": color["reason"]}
        )
        if color["available"]:
            arrays["macbeth_patch_delta_e"] = np.asarray(color["patch_delta_e"], dtype=np.float32)
    return metrics, arrays


def evaluate_saved_sensor_quality(
    identifier: str,
    *,
    run_id: str,
    output_dir: str | Path,
    root: str | Path | None = None,
    workspace: str | Path | None = None,
    scenes: Sequence[str] = ("uniform", "slanted_edge", "macbeth_d65"),
    luminance_cd_m2: float = 1000.0,
    exposure_s: float = 0.004,
    frames: int = 4,
    output_size: tuple[int, int] = (32, 32),
    seed: int = 0,
) -> dict[str, Any]:
    """Evaluate bounded quality diagnostics from exactly one saved run."""
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("identifier must be a nonempty sensor ID or report code.")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("An explicit nonempty saved run_id is required.")
    if isinstance(scenes, (str, bytes)) or not isinstance(scenes, Sequence):
        raise ValueError("scenes must be a nonempty sequence of supported scene names.")
    if (
        not 1 <= len(scenes) <= 3
        or any(not isinstance(scene, str) for scene in scenes)
        or len(set(scenes)) != len(scenes)
        or set(scenes) - _SCENES
    ):
        raise ValueError(f"scenes must be distinct values from {sorted(_SCENES)}.")
    if not isinstance(output_size, (tuple, list)) or len(output_size) != 2:
        raise ValueError("output_size requires two integer dimensions from 16 to 64.")
    size = (
        _bounded_int(output_size[0], "output rows", 16, 64),
        _bounded_int(output_size[1], "output columns", 16, 64),
    )
    if "hdr_chart_d65" in scenes and min(size) < 32:
        raise ValueError("hdr_chart_d65 requires at least 32 output pixels on each axis.")
    nframes = _bounded_int(frames, "frames", 2, 8)
    base_seed = _bounded_int(seed, "seed", 0, 2**32 - 1)
    luminance = _bounded_real(luminance_cd_m2, "luminance_cd_m2", 0.01, 10000.0)
    exposure = _bounded_real(exposure_s, "exposure_s", 1e-6, 1.0)
    if not isinstance(output_dir, (str, Path)):
        raise ValueError("output_dir must be a new or empty directory path.")
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(f"Saved quality output_dir is not empty: {destination}")

    inspection = inspect_sensor_revision(identifier, root=root, run_id=run_id)
    if not inspection["executable"]:
        raise ValueError(f"Saved sensor revision cannot execute: {inspection['reason']}")
    revision = inspection["revision"]
    if revision["run_id"] != run_id:
        raise ValueError("Selected saved run_id differs from the request.")
    saved = sensor_simulation_db_get(revision["report_code"], root=root, run_id=run_id)
    row, metadata = saved["db_entry"], saved["metadata"]
    if (
        row["result_sha256"] != revision["result_sha256"]
        or row["config_sha256"] != revision["config_sha256"]
        or _digest(metadata["config"]) != row["config_sha256"]
    ):
        raise ValueError("Saved sensor quality source changed or config hash mismatched.")
    settings = metadata.get("simulation_settings") or {}
    if (
        settings.get("fnumber") != _FIXED_FNUMBER
        or settings.get("focal_length_m") != _FIXED_FOCAL_LENGTH_M
    ):
        raise ValueError("Saved run lacks the fixed F/4, 4 mm optics contract.")
    configuration = sensor_simulation_db_config(revision["report_code"], root=root, run_id=run_id)
    if metadata["model_snapshot"]["kind"] == "physical_estimate_v1":
        illumination = configuration["scenario"].get("sensor_illumination")
        if not isinstance(illumination, Mapping) or not illumination.get("directions"):
            raise ValueError("Saved physical run lacks its F/4 angular illumination.")
    descriptor = configuration["scenario"]["sensor_model"]
    if descriptor["simulation_db"]["result_sha256"] != revision["result_sha256"]:
        raise ValueError("Saved sensor descriptor differs from the selected revision.")
    model = metadata["model_snapshot"]
    pitch_m = float(model["pixel_pitch_um"]) * 1e-6
    geometry = _physical_geometry([pitch_m], size)
    scene_fov = float(
        np.degrees(
            2 * np.arctan(1.2 * geometry["common_extent_m"][1] / (2 * _FIXED_FOCAL_LENGTH_M))
        )
    )
    geometry["scene_horizontal_fov_deg"] = scene_fov
    geometry["common_horizontal_fov_deg"] = float(
        np.degrees(2 * np.arctan(geometry["common_extent_m"][1] / (2 * _FIXED_FOCAL_LENGTH_M)))
    )
    asset_identity = offline_asset_identity(workspace=workspace, source=root)
    assets = offline_asset_store(workspace=workspace, source=root)
    arrays: dict[str, np.ndarray] = {}
    cases: list[dict[str, Any]] = []
    isp_supported = metadata.get("isp", {}).get("available") is True
    with AssetStore.bound_default(assets):
        ideal = (
            macbeth_ideal_color("D65", "XYZ", asset_store=assets)
            if "macbeth_d65" in scenes
            else None
        )
        for scene_index, kind in enumerate(scenes):
            scene, scene_geometry = (
                _hdr_chart_scene(size, scene_fov, assets)
                if kind == "hdr_chart_d65"
                else _make_scene(kind, size, scene_fov, assets)
            )
            scene_adjust_luminance(scene, luminance, asset_store=assets)
            if kind == "hdr_chart_d65":
                luminance_map = scene_calculate_luminance(scene, asset_store=assets)
                for level in scene_geometry["levels"]:
                    y0, x0, y1, x1 = level["source_roi_half_open"]
                    source_roi = np.s_[
                        math.ceil(y0) : math.floor(y1), math.ceil(x0) : math.floor(x1)
                    ]
                    level["scene_luminance_cd_m2"] = float(np.mean(luminance_map[source_roi]))
            camera = camera_create(asset_store=assets)
            camera_set(camera, "optics fnumber", _FIXED_FNUMBER)
            camera_set(camera, "optics focal length", _FIXED_FOCAL_LENGTH_M)
            camera_set(camera, "ip render scale", False)
            camera_set(camera, "sensor size", geometry["native_sizes"][0])
            scenario = copy.deepcopy(configuration["scenario"])
            if scenario.get("sensor_output_mode") != "digital_raw":
                raise ValueError("Saved quality requires a digital_raw sensor run.")
            scenario["sensor"] = {
                **scenario.get("sensor", {}),
                "size": geometry["native_sizes"][0],
                "integration_time": exposure,
                "noise_flag": 2,
            }
            scenario["sensor_resize"] = False
            computed = camerae2e_run_scenario(
                scenario,
                scene=scene,
                camera=camera,
                asset_store=assets,
                sensor_output_mode="digital_raw",
                include_arrays=False,
                seed=base_seed,
            )
            cached = camera_get(computed["camera"], "sensor")
            if not _binding_matches(cached, revision):
                raise ValueError("Executed sensor differs from the selected saved run.")
            saved_readout = model["readout"]
            effective_readout = dict(cached.fields["sensor_readout"])
            if any(effective_readout[key] != value for key, value in saved_readout.items()):
                raise ValueError("Executed readout differs from the saved readout snapshot.")
            if cached.fields.get("fixed_pattern_seed") != configuration["scenario"]["sensor"].get(
                "fixed_pattern_seed", 0
            ):
                raise ValueError("Executed fixed-pattern seed differs from the saved run.")
            if kind == "hdr_chart_d65":
                rois = _hdr_native_rois(
                    scene_geometry, size, tuple(int(value) for value in cached.fields["size"])
                )
                for level, roi in zip(scene_geometry["levels"], rois, strict=True):
                    level["native_roi_half_open"] = roi
            matrices = (
                _fixed_isp_matrices(cached, camera_get(computed["camera"], "ip"), assets)
                if isp_supported
                else None
            )
            frame_seeds = [
                int(np.random.SeedSequence([base_seed, scene_index, frame]).generate_state(1)[0])
                for frame in range(nframes)
            ]
            metrics, case_arrays = _scene_metrics(
                kind,
                cached=cached,
                matrices=matrices,
                pitch_m=pitch_m,
                geometry=geometry,
                output_size=size,
                scene_geometry=scene_geometry,
                ideal=ideal,
                frames=nframes,
                frame_seeds=frame_seeds,
            )
            prefix = f"case_{scene_index:03d}"
            for key, value in case_arrays.items():
                if value.dtype.hasobject or not np.all(np.isfinite(value)):
                    raise ValueError(f"Saved quality array {key} is invalid.")
                arrays[f"{prefix}_{key}"] = value
            case = {
                "scene": kind,
                "luminance_cd_m2": luminance,
                "exposure_s": exposure,
                "array_prefix": prefix,
                "frame_seeds": frame_seeds,
                "native_size": geometry["native_sizes"][0],
                "readout": effective_readout,
                "metrics": metrics,
            }
            if kind == "hdr_chart_d65":
                case["scene_contract"] = scene_geometry
            cases.append(case)
    if inspect_sensor_revision(identifier, root=root, run_id=run_id) != inspection:
        raise ValueError("Saved sensor source changed during quality evaluation.")
    if offline_asset_identity(workspace=workspace, source=root) != asset_identity:
        raise ValueError("Pinned upstream assets changed during quality evaluation.")
    color_evaluation = copy.deepcopy(_COLOR_EVALUATION)
    color_evaluation["calibration"]["sensor_response"] = (
        f"saved {model['kind']} spectral QE from pinned sensor-simulation run"
    )
    color_evaluation["calibration"]["saved_run_id"] = revision["run_id"]
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "completed",
        "revision": revision,
        "source_boundary": {
            "saved_sensor_simulation_run_used": True,
            "sensor_model_kind": model["kind"],
            "input_manifest_sha256": saved["input_digest"],
            "saved_arrays_sha256": saved.get("arrays_sha256"),
            "lens_model": "shared_ideal_f4_4mm_not_catalog_lens_revision",
            "physical_validation": "not_established",
            "estimate_only": True,
        },
        "upstream_assets": asset_identity,
        "model_provenance": model.get("provenance", {}),
        "readout_provenance": inspection["readout"],
        "source_scope_policy": inspection["native_response"]["source_scope_policy"],
        "controls": {
            "scenes": list(scenes),
            "frames": nframes,
            "seed": base_seed,
            "luminance_cd_m2": luminance,
            "exposure_s": exposure,
            "output_size": list(size),
            "fnumber": _FIXED_FNUMBER,
            "focal_length_m": _FIXED_FOCAL_LENGTH_M,
            "saved_fixed_pattern_seed": configuration["scenario"]["sensor"].get(
                "fixed_pattern_seed", 0
            ),
            "readout_overrides": {},
        },
        "geometry": geometry,
        "method": {
            "temporal_noise": (
                "Unbiased per-pixel variance from independent RAW frames; "
                "static FPN is retained but not counted as temporal noise."
            ),
            "raw_signal": "Illuminated minus dark deterministic reference DN.",
            "output_signal": (
                "Illuminated minus dark fixed-linear-RGB luminance; common physical crop."
            ),
            "edge": (
                "ISO 12233 first half-contrast crossing on temporal-mean linear RGB, "
                "cycles/output pixel."
            ),
            "color_evaluation": color_evaluation,
            "snr_clip_exclusion": (
                "Any well/ADC white/ADC zero fraction above 0.001 makes SNR diagnostic only."
            ),
            "hdr_chart_response": (
                "Six D65 strips from pyisetcam.scene_create('hdr chart') at a declared "
                "1000:1 scene ratio; per-strip native RAW and clipping are single-exposure "
                "diagnostics, not HDR fusion or LFM validation."
            ),
        },
        "limitations": [
            "Saved sensor estimate and ideal optics are not measured product validation.",
            (
                "MTF50 includes fixed optics, sensor sampling, demosaic and output crop; "
                "it is not intrinsic sensor MTF."
            ),
            (
                "Macbeth/D65 evaluation reuses the calibration surfaces and is an "
                "in-sample residual, not independent color accuracy."
            ),
            (
                "Static crop excludes full die resolution, rolling shutter, motion, HDR "
                "and lens DB prescriptions."
            ),
            (
                "The HDR strip-chart scene tests one exposure and does not implement "
                "HDR readout or LFM."
            ),
        ],
        "cases": cases,
        "sample_count": len(cases) * nframes,
        "optical_solve_count": len(cases),
    }
    destination.mkdir(parents=True, exist_ok=True)
    npz_path = destination / "saved_quality_arrays.npz"
    with npz_path.open("wb") as stream:
        np.savez_compressed(stream, **arrays)
    report["artifacts"] = {
        "storage": "local_output_dir",
        "npz_name": npz_path.name,
        "npz_sha256": _sha256(npz_path),
    }
    report_path = destination / "saved_quality_report.json"
    report_path.write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        **report,
        "artifacts": {"json": str(report_path), "npz": str(npz_path)},
        "artifact_sha256": {"json": _sha256(report_path), "npz": _sha256(npz_path)},
    }
