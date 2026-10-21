"""Matched physical-crop comparisons using the explicitly estimated DB model.

Optics are computed once per sensor/scene. Photoelectron means are linear in
luminance, integration time and effective efficiency before well clipping.
All stochastic captures pass through the same fixed linear E2E ISP and spatial
resampling; spatial texture is never substituted for temporal noise.
"""

from __future__ import annotations

import base64
import copy
import html
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import affine_transform, gaussian_filter

from .assets import AssetStore
from .camera import camera_create, camera_get, camera_set
from .ip import _sensor_space_from_data, ip_compute
from .iso import iso12233
from .metrics import delta_e_ab
from .scene import (
    macbeth_ideal_color,
    scene_adjust_illuminant,
    scene_adjust_luminance,
    scene_create,
    scene_set,
)
from .system_faca import camerae2e_run_scenario
from .utils import linear_to_srgb

DEFAULT_SENSOR_IDS = ("DEF-1807-801", "DEF-2011-804", "DEF-2206-805")
_BASE_ASSUMPTIONS = {
    "effective_efficiency": 0.75,
    "full_well_e": 10000.0,
    "read_noise_e": 10.0,
    "dark_current_e_per_s": 10.0,
}
_ASSUMPTION_BOUNDS = {
    "effective_efficiency": (0.5, 1.0),
    "full_well_e": (5000.0, 20000.0),
    "read_noise_e": (3.0, 20.0),
    "dark_current_e_per_s": (1.0, 100.0),
}
_SCENE_NAMES = ("uniform", "macbeth_d65", "slanted_edge")
_ARRAY_DTYPES = {
    "rgb_reference": "float32",
    "rgb_frame": "float32",
    "rgb_mean": "float32",
    "rgb_variance": "float32",
    "luma_variance": "float32",
    "raw_dn_frame": "int32",
    "raw_dn_mean": "float32",
    "raw_dn_variance": "float32",
    "mean_photoelectrons": "float32",
}
_COLOR_EVALUATION = {
    "contract": "in_sample_calibration_fit_residual",
    "calibration": {
        "method": "sensor_to_target_matrix least-squares MCC fit to XYZ",
        "surface_asset": "macbethChart.mat",
        "surface_count": 24,
        "illuminant": "D65",
        "sensor_response": "db_estimate_v1 EQE",
    },
    "evaluation": {
        "scene": "macbeth_d65",
        "surface_asset": "macbethChart.mat",
        "surface_count": 24,
        "illuminant": "D65",
        "input": "inner-patch XYZ means of temporal-mean capture/ISP output",
        "normalization": "white_patch_Y",
        "metric": "CIE76",
    },
    "held_out_surfaces": False,
    "independent_color_accuracy": False,
    "interpretation": (
        "Calibration and evaluation reuse the same 24 Macbeth reflectances under D65. "
        "The in-sample residual includes capture, optics and ISP effects; it does not "
        "measure generalization to other surfaces or illuminants."
    ),
}


def sensor_comparison_assumptions(sweep: bool = True) -> list[dict[str, Any]]:
    """Return the declared baseline, eight one-at-a-time changes and two corners."""
    result = [{"name": "baseline", **_BASE_ASSUMPTIONS}]
    if not sweep:
        return result
    for key, (low, high) in _ASSUMPTION_BOUNDS.items():
        for suffix, value in (("low", low), ("high", high)):
            result.append({"name": f"{key}_{suffix}", **_BASE_ASSUMPTIONS, key: value})
    result.extend(
        [
            {
                "name": "optimistic_corner",
                "effective_efficiency": 1.0,
                "full_well_e": 20000.0,
                "read_noise_e": 3.0,
                "dark_current_e_per_s": 1.0,
            },
            {
                "name": "pessimistic_corner",
                "effective_efficiency": 0.5,
                "full_well_e": 5000.0,
                "read_noise_e": 20.0,
                "dark_current_e_per_s": 100.0,
            },
        ]
    )
    return result


def _positive_sequence(values: Sequence[float], name: str) -> tuple[float, ...]:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or not array.size or not np.all(np.isfinite(array)) or np.any(array <= 0):
        raise ValueError(f"{name} must be a nonempty sequence of finite positive values.")
    if np.unique(array).size != array.size:
        raise ValueError(f"{name} must not contain duplicate values.")
    return tuple(float(value) for value in array)


def _physical_geometry(pitches_m: Sequence[float], output_size: tuple[int, int]) -> dict[str, Any]:
    pitches = np.asarray(pitches_m, dtype=float)
    if pitches.size == 0 or np.any(~np.isfinite(pitches)) or np.any(pitches <= 0):
        raise ValueError("Pixel pitches must be finite and positive.")
    # An explicitly modelled crop, not a claim to use an unverified full die size.
    extent = np.asarray(output_size, dtype=float) * float(np.max(pitches))
    sizes = []
    for pitch in pitches:
        # Two native pixels on every side provide interpolation/ISP context.
        size = (2 * np.ceil((extent / pitch + 4) / 2)).astype(int)
        sizes.append(size.tolist())
    return {
        "common_extent_m": extent.tolist(),
        "native_sizes": sizes,
        "output_size": list(output_size),
        "output_pitch_m": float(np.max(pitches)),
        "crop_origin": "center",
        "cfa_origin_preserved": True,
    }


def _physical_resample(
    image: np.ndarray, pitch_m: float, extent_m: Sequence[float], output_size: tuple[int, int]
) -> np.ndarray:
    """Sample a centered physical crop with antialiasing before downsampling."""
    image = np.asarray(image, dtype=float)
    scale = np.asarray(extent_m, dtype=float) / (np.asarray(output_size) * pitch_m)
    offset = (np.asarray(image.shape[:2]) - 1) / 2 - scale * (np.asarray(output_size) - 1) / 2
    if np.allclose(scale, 1.0, rtol=0, atol=1e-12) and np.allclose(
        offset, np.rint(offset), rtol=0, atol=1e-12
    ):
        top, left = np.rint(offset).astype(int)
        bottom, right = top + output_size[0], left + output_size[1]
        if top >= 0 and left >= 0 and bottom <= image.shape[0] and right <= image.shape[1]:
            # Equal native/output pitch: exact physical crop, no interpolation work.
            return image[top:bottom, left:right].copy()
    sigma = 0.5 * np.sqrt(np.maximum(scale * scale - 1.0, 0.0))
    if image.ndim == 2:
        smooth = gaussian_filter(image, sigma, mode="reflect")
        return affine_transform(
            smooth,
            np.diag(scale),
            offset=offset,
            output_shape=output_size,
            order=1,
            mode="reflect",
            prefilter=False,
        )
    return np.stack(
        [
            _physical_resample(image[..., channel], pitch_m, extent_m, output_size)
            for channel in range(image.shape[2])
        ],
        axis=-1,
    )


class _Moments:
    """Online temporal moments; unbiased sample variance across captures."""

    def __init__(self) -> None:
        self.count = 0
        self.mean: np.ndarray | None = None
        self.m2: np.ndarray | None = None

    def add(self, image: np.ndarray) -> None:
        image = np.asarray(image, dtype=float)
        self.count += 1
        if self.mean is None:
            self.mean, self.m2 = image.copy(), np.zeros_like(image)
        else:
            difference = image - self.mean
            self.mean += difference / self.count
            self.m2 += difference * (image - self.mean)

    @property
    def variance(self) -> np.ndarray:
        if self.count < 2:
            raise ValueError("At least two temporal samples are required.")
        return self.m2 / (self.count - 1)


def _make_scene(
    kind: str, output_size: tuple[int, int], fov_deg: float, store: AssetStore,
    *, source_size: tuple[int, int] | None = None,
) -> tuple[Any, dict[str, Any]]:
    # 20% extra scene field keeps sensor interpolation context inside the scene.
    if source_size is None:
        height, width = (max(32, 2 * value) for value in output_size)
    else:
        if (
            not isinstance(source_size, tuple) or len(source_size) != 2
            or any(type(value) is not int or value < max(32, 2 * output_size[index])
                   for index, value in enumerate(source_size))
        ):
            raise ValueError(
                "source_size must contain two integer scene dimensions "
                "at least twice the output size."
            )
        height, width = source_size
    scene = scene_create("uniform ee", [height, width], asset_store=store)
    scene_adjust_illuminant(scene, "D65", asset_store=store)
    geometry: dict[str, Any] = {"scene_to_crop_scale": 1.2}
    if kind == "macbeth_d65":
        patch = max(2, min(int(height / 1.2 * 0.8 / 4), int(width / 1.2 * 0.8 / 6)))
        chart = scene_create("macbeth d65", patch, asset_store=store)
        source = np.asarray(chart.data["photons"], dtype=float)
        canvas = np.broadcast_to(
            np.mean(source, axis=(0, 1)) * 0.25, (height, width, source.shape[2])
        ).copy()
        top, left = (height - source.shape[0]) // 2, (width - source.shape[1]) // 2
        canvas[top : top + source.shape[0], left : left + source.shape[1]] = source
        scene_set(scene, "photons", canvas)
        geometry["patch_boxes"] = []
        for col in range(6):
            for row in range(4):
                # Inner 40% of each patch excludes optical/demosaic edge mixing.
                bounds = np.array(
                    [
                        top + (row + 0.3) * patch,
                        left + (col + 0.3) * patch,
                        top + (row + 0.7) * patch,
                        left + (col + 0.7) * patch,
                    ]
                )
                center = np.array([height, width, height, width]) / 2
                factor = np.tile(np.array(output_size) / np.array([height, width]) * 1.2, 2)
                bounds = (bounds - center) * factor + np.tile(np.array(output_size) / 2, 2)
                geometry["patch_boxes"].append(bounds.tolist())
    elif kind == "slanted_edge":
        yy, xx = np.indices((height, width), dtype=float)
        target = np.where(xx - width / 2 > (yy - height / 2) / 12, 1.0, 0.02)
        scene_set(scene, "photons", np.asarray(scene.data["photons"]) * target[..., None])
    elif kind != "uniform":
        raise ValueError(f"Unknown comparison scene: {kind}")
    scene_set(scene, "fov", fov_deg)
    scene_adjust_luminance(scene, 1.0, asset_store=store)
    return scene, geometry


def _fixed_isp_matrices(sensor: Any, ip: Any, store: AssetStore) -> tuple[np.ndarray, np.ndarray]:
    computed = ip_compute(ip, sensor, asset_store=store)
    if not np.allclose(computed.fields["illuminant_correction_matrix"], np.eye(3)):
        raise ValueError("Comparison requires fixed 'none' illuminant correction.")
    return (
        np.asarray(computed.fields["sensor_conversion_matrix"], dtype=float),
        np.asarray(computed.fields["ics2display"], dtype=float),
    )


def _linear_isp(
    sensor: Any, dn: np.ndarray, matrices: tuple[np.ndarray, np.ndarray]
) -> tuple[np.ndarray, np.ndarray]:
    readout = sensor.fields["sensor_readout"]
    white = float(2 ** int(readout["bit_depth"]) - 1)
    normalized = (np.asarray(dn, dtype=float) - float(readout["black_level_dn"])) / (
        white - float(readout["black_level_dn"])
    )
    sensor_space = _sensor_space_from_data(sensor, normalized, "bilinear")
    xyz = np.maximum(sensor_space @ matrices[0], 0.0)
    rgb = np.maximum(xyz @ matrices[1], 0.0)
    return rgb, xyz


def _patch_means(image: np.ndarray, boxes: Sequence[Sequence[float]]) -> np.ndarray:
    patches = []
    for top, left, bottom, right in boxes:
        y0, y1 = max(0, int(np.floor(top))), min(image.shape[0], int(np.ceil(bottom)))
        x0, x1 = max(0, int(np.floor(left))), min(image.shape[1], int(np.ceil(right)))
        if y1 <= y0 or x1 <= x0:
            raise ValueError("Output crop is too small to sample all Macbeth patches.")
        patches.append(np.mean(image[y0:y1, x0:x1], axis=(0, 1)))
    return np.asarray(patches)


def _color_metrics(
    xyz: np.ndarray, boxes: Sequence[Sequence[float]], ideal: np.ndarray
) -> dict[str, Any]:
    actual = _patch_means(xyz, boxes)
    if actual[3, 1] <= 0:
        return {"available": False, "reason": "No positive white-patch signal."}
    actual = actual * ideal[3, 1] / actual[3, 1]
    error = delta_e_ab(actual, ideal, ideal[3], "1976")
    return {
        "available": True,
        "metric": "CIE76",
        "normalization": "white_patch_Y",
        "mean_delta_e": float(np.mean(error)),
        "max_delta_e": float(np.max(error)),
        "patch_delta_e": error.tolist(),
    }


def _edge_metrics(rgb: np.ndarray) -> dict[str, Any]:
    if min(rgb.shape[:2]) < 16 or np.ptp(rgb) <= 0:
        return {"available": False, "reason": "Slanted-edge image is too small or has no contrast."}
    # Center ROI avoids ISP border artifacts. Same output pixels for every sensor.
    ry, rx = max(1, rgb.shape[0] // 8), max(1, rgb.shape[1] // 8)
    try:
        roi = rgb[ry:-ry, rx:-rx]
        result = iso12233(roi / np.max(roi), delta_x=1.0)
        frequencies = np.asarray(result.freq, dtype=float).reshape(-1)
        curve = np.asarray(result.mtf, dtype=float)
        curve = curve[:, -1] if curve.ndim == 2 else curve.reshape(-1)
        valid = np.isfinite(frequencies) & np.isfinite(curve) & (frequencies <= result.nyquistf)
        frequencies, curve = frequencies[valid], curve[valid]
        crossing = np.flatnonzero((curve[:-1] >= 0.5) & (curve[1:] < 0.5))
        if not crossing.size:
            return {"available": False, "reason": "No MTF50 crossing below output Nyquist."}
        index = int(crossing[0])
        value = float(
            frequencies[index]
            + (frequencies[index + 1] - frequencies[index])
            * (0.5 - curve[index])
            / (curve[index + 1] - curve[index])
        )
    except (ValueError, np.linalg.LinAlgError, FloatingPointError) as error:
        return {"available": False, "reason": str(error)}
    if not np.isfinite(value):
        return {"available": False, "reason": "MTF50 crossing could not be estimated."}
    return {
        "available": True,
        "mtf50_cycles_per_output_pixel": value,
        "method": "iso12233_curve_first_half_contrast_crossing_linear_interpolation",
        "input": "temporal_mean_linear_RGB",
        "frequency_cycles_per_output_pixel": frequencies.tolist(),
        "luminance_mtf": curve.tolist(),
    }


def _snr_metrics(signal: np.ndarray, variance: np.ndarray) -> dict[str, Any]:
    mean_signal = float(np.mean(signal))
    rms_noise = float(np.sqrt(np.mean(variance)))
    ratio = mean_signal / rms_noise if rms_noise > 0 else None
    return {
        "mean_signal": mean_signal,
        "temporal_noise_rms": rms_noise,
        "snr_linear": ratio,
        "snr_db": float(20 * np.log10(ratio)) if ratio is not None and ratio > 0 else None,
    }


def _uncertainty_summary(cases: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for case in cases:
        key = (case["scene"], case["luminance_cd_m2"], case["exposure_s"])
        groups.setdefault(key, []).append(case)
    summaries = []
    for (scene, luminance, exposure), entries in groups.items():
        by_sensor: dict[str, list[float]] = {}
        by_assumption: dict[str, list[tuple[str, float]]] = {}
        excluded = []
        for item in entries:
            if not item["metrics"].get("snr_ranking_eligible", True):
                excluded.append(
                    {
                        "sensor_id": item["sensor_id"],
                        "assumption": item["assumption"],
                        "reason": "well or ADC clipping fraction exceeds 0.001",
                    }
                )
                continue
            value = item["metrics"]["common_output"]["snr_db"]
            if value is not None:
                by_sensor.setdefault(item["sensor_id"], []).append(value)
                by_assumption.setdefault(item["assumption"], []).append((item["sensor_id"], value))
        all_sensors = {item["sensor_id"] for item in entries}
        incomplete = [
            name
            for name, values in by_assumption.items()
            if {key for key, _ in values} != all_sensors
        ]
        orders = {
            name: [key for key, _ in sorted(values, key=lambda x: (-x[1], x[0]))]
            for name, values in by_assumption.items()
            if name not in incomplete
        }
        intervals = {key: [min(values), max(values)] for key, values in by_sensor.items()}
        distinct_orders = {tuple(value) for value in orders.values()}
        summaries.append(
            {
                "scene": scene,
                "luminance_cd_m2": luminance,
                "exposure_s": exposure,
                "snr_db_assumption_range": intervals,
                "ranking_changes_under_shared_assumptions": len(distinct_orders) > 1,
                "orders_by_shared_assumption": orders,
                "excluded_from_ranking": excluded,
                "incomplete_shared_assumptions": incomplete,
                "interpretation": (
                    "Sensitivity bounds, not confidence intervals or measured product ranking."
                ),
            }
        )
    return summaries


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, (np.integer, np.floating)):
        return _jsonable(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _annotate_color_evaluation(report: dict[str, Any]) -> None:
    """Label both fresh and re-rendered metrics without modifying their numbers."""
    report["method"]["color"] = (
        "In-sample Macbeth/D65 calibration fit residual (CIE76), white-patch Y normalized; "
        "same surfaces as the MCC matrix fit, not independent color accuracy"
    )
    report["method"]["color_evaluation"] = copy.deepcopy(_COLOR_EVALUATION)
    limitation = (
        "Macbeth/D65 color evaluation reuses the MCC calibration surfaces: "
        "in-sample fit residual, not independent color accuracy."
    )
    if limitation not in report["limitations"]:
        report["limitations"].append(limitation)
    for case in report["cases"]:
        if "color" in case["metrics"]:
            case["metrics"]["color"].update(
                {
                    "evaluation_contract": _COLOR_EVALUATION["contract"],
                    "held_out_surfaces": False,
                    "independent_color_accuracy": False,
                    "provenance": "method.color_evaluation",
                }
            )


def camerae2e_compare_sensors(
    sensor_ids: Sequence[str] | None = None,
    *,
    root: str | Path | None = None,
    output_dir: str | Path | None = None,
    nframes: int = 32,
    luminances: Sequence[float] = (1, 10, 100),
    exposures: Sequence[float] = (0.001, 0.004, 0.016),
    output_size: tuple[int, int] = (128, 128),
    scene_types: Sequence[str] = _SCENE_NAMES,
    assumption_sweep: bool = True,
    seed: int = 0,
    fnumber: float = 4.0,
    focal_length_m: float = 0.004,
    readout: Mapping[str, Any] | None = None,
    asset_store: AssetStore | None = None,
) -> dict[str, Any]:
    """Compare DB estimates with fixed controls and independent temporal samples.

    ``output_size`` is a common physical sensor crop at the largest selected
    pitch, subsequently resampled to common output pixels. Full resolution, FPS,
    rolling-shutter motion and measured calibration are outside this experiment.
    ``readout`` may set gain, bit_depth and black_level_dn; uncertain physical
    readout parameters are controlled by the documented eleven assumption sets.
    NPZ stores baseline moments and representative frames, never full bursts.
    """
    # Imports are local so lightweight report/geometry utilities have no model I/O.
    from .image_sensor_db import image_sensor_db_config, image_sensor_db_get
    from .sensor_capture import configure_e2e_sensor, resample_sensor_capture
    from .sensor_estimate import image_sensor_db_model, image_sensor_db_model_inventory

    if isinstance(nframes, bool) or int(nframes) != nframes or nframes < 2:
        raise ValueError("nframes must be an integer of at least two.")
    if isinstance(seed, bool) or int(seed) != seed or seed < 0:
        raise ValueError("seed must be a nonnegative integer.")
    if len(output_size) != 2 or any(
        isinstance(v, bool) or int(v) != v or v < 8 for v in output_size
    ):
        raise ValueError("output_size must contain two integer dimensions of at least eight.")
    output_size = tuple(int(value) for value in output_size)
    luminances = _positive_sequence(luminances, "luminances")
    exposures = _positive_sequence(exposures, "exposures")
    _positive_sequence([fnumber], "fnumber")
    _positive_sequence([focal_length_m], "focal_length_m")
    scene_types = tuple(scene_types)
    if (
        not scene_types
        or len(set(scene_types)) != len(scene_types)
        or set(scene_types) - set(_SCENE_NAMES)
    ):
        raise ValueError(f"scene_types must be distinct values from {_SCENE_NAMES}.")
    identifiers = list(DEFAULT_SENSOR_IDS if sensor_ids is None else sensor_ids)
    if not identifiers or isinstance(sensor_ids, str):
        raise ValueError("sensor_ids must be a nonempty sequence of sensor IDs or report codes.")
    controls = {"gain": 1.0, "bit_depth": 12, "black_level_dn": 64.0}
    if readout:
        unknown = set(readout) - set(controls)
        if unknown:
            raise ValueError(
                "Use the assumption sweep for physical readout parameters; "
                f"unsupported controls: {sorted(unknown)}"
            )
        controls.update(readout)
    store = asset_store or AssetStore.default()
    inventory = image_sensor_db_model_inventory(root=root)
    models, selected, skipped = [], [], []
    for identifier in identifiers:
        record = image_sensor_db_get(identifier, root=root)
        match = next(
            (item for item in inventory["records"] if item["sensor_id"] == record["sensor_id"]),
            None,
        )
        if match is None or not match["available"]:
            skipped.append(
                {
                    "sensor_id": record["sensor_id"],
                    "code": record["code"],
                    "reason": match["reason"] if match else "Not in model inventory.",
                }
            )
            continue
        if record["sensor_id"] in [item["sensor_id"] for item in selected]:
            raise ValueError("sensor_ids resolve to duplicate catalog records.")
        model = image_sensor_db_model(identifier, root=root, assumptions=_BASE_ASSUMPTIONS)
        models.append(model)
        selected.append(
            {
                "sensor_id": record["sensor_id"],
                "code": record["code"],
                "device_name": record["device_name"],
                "pixel_pitch_um": model["pixel_pitch_um"],
                "model_provenance": model["provenance"],
                "optical_model": {
                    key: model[key]
                    for key in (
                        "kind",
                        "wavelength_nm",
                        "eqe",
                        "filter_names",
                        "cfa_pattern",
                        "active_si_thickness_um",
                        "cfa_transmission",
                        "silicon_absorption",
                    )
                },
                "estimate_only": True,
            }
        )
    selected_ids = {record["sensor_id"] for record in selected}
    for item in inventory["records"]:
        item["comparison_status"] = (
            "selected"
            if item["sensor_id"] in selected_ids
            else "not_selected"
            if item["available"]
            else "excluded"
        )
        item["comparison_skip_reason"] = (
            None
            if item["sensor_id"] in selected_ids
            else "Available model; not selected for simulation."
            if item["available"]
            else item["reason"]
        )
    assumptions = sensor_comparison_assumptions(assumption_sweep)
    report: dict[str, Any] = {
        "schema_version": "camerae2e_sensor_comparison_v1",
        "estimate_only": True,
        "comparison_contract": "db_estimate_v1_fixed_controls_common_physical_crop",
        "inventory": inventory,
        "selected_sensors": selected,
        "skipped_sensors": skipped,
        "assumptions": assumptions,
        "cases": [],
        "artifacts": {},
        "controls": {
            "nframes": int(nframes),
            "seed": int(seed),
            "luminances_cd_m2": luminances,
            "exposures_s": exposures,
            "scene_types": scene_types,
            "readout": controls,
            "fnumber": fnumber,
            "focal_length_m": focal_length_m,
            "auto_exposure": False,
            "white_balance": "fixed_D65_matrix",
        },
        "method": {
            "mean_cache": "one optical solve per sensor/scene at 1 cd/m2 and 1 ms",
            "mean_scaling": (
                "photoelectrons * luminance * exposure/0.001 * efficiency/0.75, before clipping"
            ),
            "isp": (
                "actual E2E fixed DN normalization, bilinear demosaic, cached D65 matrices; "
                "no frame max normalization"
            ),
            "spatial_resampling": (
                "centered physical crop, Gaussian antialias, bilinear sampling after demosaic"
            ),
            "temporal_noise": (
                "unbiased per-pixel variance across independent captures; "
                "RGB luma weights [0.213,0.715,0.072]"
            ),
            "array_storage": (
                "baseline conditions only: one frame, temporal mean, variance, noiseless reference"
            ),
            "array_dtypes": dict(_ARRAY_DTYPES),
            "raw_array_units": {
                "raw_dn_frame": "DN",
                "raw_dn_mean": "DN",
                "raw_dn_variance": "DN squared",
            },
            "color": (
                "CIE76 on Macbeth patch means, white-patch Y normalized; "
                "modeled D65 matrix, not measured calibration"
            ),
            "uncertainty": (
                "shared one-at-a-time assumptions and two corners; not confidence intervals"
            ),
            "snr_rank_exclusion": "any native well/ADC white/ADC zero fraction above 0.001",
        },
        "limitations": [
            "All outputs are DB estimates, not sensor measurements or calibrated product rankings.",
            "FWC, read noise, dark current and effective efficiency are shared assumptions.",
            "Static crops exclude full die resolution, FPS, HDR modes, shutter timing and motion.",
            "No AE controller is used in fixed comparisons; any AE smoke is reported separately.",
        ],
        "optical_solve_count": 0,
    }
    arrays: dict[str, np.ndarray] = {}
    if selected:
        pitches = [float(model["pixel_pitch_um"]) * 1e-6 for model in models]
        geometry = _physical_geometry(pitches, output_size)
        report["geometry"] = geometry
        fov = np.degrees(2 * np.arctan(geometry["common_extent_m"][1] / (2 * focal_length_m)))
        scene_fov = np.degrees(
            2 * np.arctan(1.2 * geometry["common_extent_m"][1] / (2 * focal_length_m))
        )
        geometry["common_horizontal_fov_deg"] = float(fov)
        geometry["scene_horizontal_fov_deg"] = float(scene_fov)
        scenes = {
            kind: _make_scene(kind, output_size, float(scene_fov), store) for kind in scene_types
        }
        ideal = (
            macbeth_ideal_color("D65", "XYZ", asset_store=store)
            if "macbeth_d65" in scene_types
            else None
        )
        luma_weights = np.array([0.213, 0.715, 0.072])
        for sensor_index, (record, _model, pitch) in enumerate(
            zip(selected, models, pitches, strict=True)
        ):
            camera = camera_create(asset_store=store)
            camera_set(camera, "optics fnumber", fnumber)
            camera_set(camera, "optics focal length", focal_length_m)
            camera_set(camera, "ip render scale", False)
            camera_set(camera, "sensor size", geometry["native_sizes"][sensor_index])
            for scene_index, kind in enumerate(scene_types):
                scene, scene_geometry = scenes[kind]
                scenario = copy.deepcopy(
                    image_sensor_db_config(
                        record["sensor_id"], root=root, assumptions=_BASE_ASSUMPTIONS,
                        model="db_estimate_v1",
                    )["scenario"]
                )
                scenario["sensor"] = {
                    **scenario.get("sensor", {}),
                    "integration_time": 0.001,
                    "noise_flag": 2,
                }
                scenario["sensor_resize"] = False
                scenario["readout"] = {
                    **controls,
                    **{
                        key: value
                        for key, value in _BASE_ASSUMPTIONS.items()
                        if key != "effective_efficiency"
                    },
                }
                base_result = camerae2e_run_scenario(
                    scenario,
                    scene=scene,
                    camera=camera,
                    asset_store=store,
                    sensor_output_mode="digital_raw",
                    include_arrays=False,
                    seed=int(seed),
                )
                report["optical_solve_count"] += 1
                cached = camera_get(base_result["camera"], "sensor")
                matrices = _fixed_isp_matrices(
                    cached, camera_get(base_result["camera"], "ip"), store
                )
                record.setdefault(
                    "isp_provenance",
                    {
                        "sensor_input": camera_get(base_result["camera"], "ip").metadata.get(
                            "sensor_input", {}
                        ),
                        "sensor_to_xyz_matrix": matrices[0].tolist(),
                        "xyz_to_display_matrix": matrices[1].tolist(),
                    },
                )
                base_mean = np.asarray(cached.data["mean_electrons"], dtype=float).copy()
                record.setdefault("capture_provenance", base_result.get("sensor_estimate", {}))
                for luminance in luminances:
                    for exposure in exposures:
                        for assumption in assumptions:
                            current = configure_e2e_sensor(
                                cached.clone(),
                                readout={
                                    **controls,
                                    **{
                                        key: value
                                        for key, value in assumption.items()
                                        if key not in {"name", "effective_efficiency"}
                                    },
                                },
                            )
                            current.fields["integration_time"] = float(exposure)
                            current.data["mean_electrons"] = (
                                base_mean
                                * luminance
                                * exposure
                                / 0.001
                                * (
                                    assumption["effective_efficiency"]
                                    / _BASE_ASSUMPTIONS["effective_efficiency"]
                                )
                            )
                            current = resample_sensor_capture(current, seed=int(seed))
                            reference_rgb_native, _ = _linear_isp(
                                current, current.data["reference_dn"], matrices
                            )
                            reference_rgb = _physical_resample(
                                reference_rgb_native,
                                pitch,
                                geometry["common_extent_m"],
                                output_size,
                            )
                            # Dark reference follows the same ISP as the illuminated reference.
                            signal_current = current.clone()
                            signal_current.data["mean_electrons"] = np.zeros_like(base_mean)
                            signal_current = resample_sensor_capture(signal_current, seed=int(seed))
                            dark_rgb_native, _ = _linear_isp(
                                signal_current, signal_current.data["reference_dn"], matrices
                            )
                            dark_rgb = _physical_resample(
                                dark_rgb_native, pitch, geometry["common_extent_m"], output_size
                            )
                            rgb_moments, xyz_moments, luma_moments, raw_moments = (
                                _Moments() for _ in range(4)
                            )
                            saturation: dict[str, list[float]] = {}
                            representative = raw_representative = None
                            for frame in range(int(nframes)):
                                # Common random numbers reduce jitter between assumption sets.
                                frame_seed = int(
                                    np.random.SeedSequence(
                                        [int(seed), sensor_index, scene_index, frame]
                                    ).generate_state(1)[0]
                                )
                                capture = resample_sensor_capture(current, seed=frame_seed)
                                rgb_native, xyz_native = _linear_isp(
                                    capture, capture.data["raw_dn"], matrices
                                )
                                rgb = _physical_resample(
                                    rgb_native, pitch, geometry["common_extent_m"], output_size
                                )
                                xyz = _physical_resample(
                                    xyz_native, pitch, geometry["common_extent_m"], output_size
                                )
                                rgb_moments.add(rgb)
                                xyz_moments.add(xyz)
                                luma_moments.add(rgb @ luma_weights)
                                raw_moments.add(capture.data["raw_dn"])
                                if frame == 0:
                                    representative, raw_representative = (
                                        rgb.copy(),
                                        np.asarray(capture.data["raw_dn"]).copy(),
                                    )
                                for key, value in (
                                    capture.metadata["sensor_capture"].get("saturation", {}).items()
                                ):
                                    saturation.setdefault(key, []).append(float(value))
                            photo_signal = current.data["mean_electrons"]
                            capture_readout = current.fields["sensor_readout"]
                            metrics = {
                                "native_raw": {
                                    **_snr_metrics(
                                        current.data["reference_dn"]
                                        - signal_current.data["reference_dn"],
                                        raw_moments.variance,
                                    ),
                                    "signal_units": (
                                        "illuminated minus dark deterministic clipped reference DN"
                                    ),
                                    "mean_photoelectrons": float(np.mean(photo_signal)),
                                    "temporal_mean_dn": float(np.mean(raw_moments.mean)),
                                },
                                "common_output": {
                                    **_snr_metrics(
                                        (reference_rgb - dark_rgb) @ luma_weights,
                                        luma_moments.variance,
                                    ),
                                    "signal_units": "fixed linear output luminance",
                                    "temporal_mean": float(np.mean(luma_moments.mean)),
                                    "bias_vs_noiseless_reference": float(
                                        np.mean(luma_moments.mean - reference_rgb @ luma_weights)
                                    ),
                                },
                                "saturation": {
                                    key: float(np.mean(values))
                                    for key, values in saturation.items()
                                },
                                "expected_photo_well_fraction": float(
                                    np.mean(photo_signal >= assumption["full_well_e"])
                                ),
                            }
                            metrics["snr_ranking_eligible"] = all(
                                value <= 0.001 for value in metrics["saturation"].values()
                            )
                            metrics["snr_interpretation"] = (
                                "unsaturated temporal SNR"
                                if metrics["snr_ranking_eligible"]
                                else "clipped signal: diagnostic SNR only, excluded from ranking"
                            )
                            if kind == "macbeth_d65":
                                metrics["color"] = _color_metrics(
                                    xyz_moments.mean, scene_geometry["patch_boxes"], ideal
                                )
                            if kind == "slanted_edge":
                                metrics["edge"] = _edge_metrics(rgb_moments.mean)
                            index = len(report["cases"])
                            case = {
                                "case_id": f"case_{index:04d}",
                                "sensor_id": record["sensor_id"],
                                "code": record["code"],
                                "scene": kind,
                                "luminance_cd_m2": luminance,
                                "exposure_s": exposure,
                                "assumption": assumption["name"],
                                "readout": capture_readout,
                                "metrics": metrics,
                            }
                            if assumption["name"] == "baseline":
                                prefix = case["case_id"]
                                case["array_prefix"] = prefix
                                for name, data in {
                                    "rgb_reference": reference_rgb,
                                    "rgb_frame": representative,
                                    "rgb_mean": rgb_moments.mean,
                                    "rgb_variance": rgb_moments.variance,
                                    "luma_variance": luma_moments.variance,
                                    "raw_dn_frame": raw_representative,
                                    "raw_dn_mean": raw_moments.mean,
                                    "raw_dn_variance": raw_moments.variance,
                                    "mean_photoelectrons": photo_signal,
                                }.items():
                                    arrays[f"{prefix}_{name}"] = np.asarray(
                                        data, dtype=_ARRAY_DTYPES[name]
                                    )
                            report["cases"].append(case)
    report["case_count"] = len(report["cases"])
    report["sample_count"] = len(report["cases"]) * int(nframes)
    report["uncertainty_summary"] = _uncertainty_summary(report["cases"])
    _annotate_color_evaluation(report)
    report = _jsonable(report)
    if output_dir is not None:
        _write_report(report, arrays, Path(output_dir))
    return report


def _png_uri(rgb: np.ndarray, scale: float) -> str:
    from PIL import Image

    buffer = io.BytesIO()
    rendered = np.uint8(np.clip(linear_to_srgb(np.maximum(rgb * scale, 0)), 0, 1) * 255 + 0.5)
    Image.fromarray(rendered).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _metric_plots(report: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Render scientific comparison curves directly from the reported numbers."""
    if not report["cases"]:
        return []
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def encode(figure: Any) -> str:
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", dpi=130, bbox_inches="tight")
        plt.close(figure)
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    scenes = report["controls"]["scene_types"]
    luminances = report["controls"]["luminances_cd_m2"]
    exposures = sorted(report["controls"]["exposures_s"])
    x = np.asarray(exposures) * 1000
    colors = plt.get_cmap("tab10")
    figure, axes = plt.subplots(
        len(scenes),
        len(luminances),
        squeeze=False,
        figsize=(4.6 * len(luminances), 3.1 * len(scenes)),
    )
    for row, scene in enumerate(scenes):
        for column, luminance in enumerate(luminances):
            axis = axes[row, column]
            for sensor_index, sensor in enumerate(report["selected_sensors"]):
                means, low, high = [], [], []
                for exposure in exposures:
                    matched = [
                        case
                        for case in report["cases"]
                        if (
                            case["sensor_id"] == sensor["sensor_id"]
                            and case["scene"] == scene
                            and case["luminance_cd_m2"] == luminance
                            and case["exposure_s"] == exposure
                            and case["metrics"]["snr_ranking_eligible"]
                        )
                    ]
                    values = [
                        case["metrics"]["common_output"]["snr_db"]
                        for case in matched
                        if case["metrics"]["common_output"]["snr_db"] is not None
                    ]
                    baseline = next(
                        (
                            case["metrics"]["common_output"]["snr_db"]
                            for case in matched
                            if case["assumption"] == "baseline"
                        ),
                        None,
                    )
                    means.append(np.nan if baseline is None else baseline)
                    low.append(min(values) if values else np.nan)
                    high.append(max(values) if values else np.nan)
                color = colors(sensor_index % 10)
                axis.plot(x, means, "o-", color=color, label=sensor["code"])
                axis.fill_between(x, low, high, alpha=0.15, color=color)
            axis.set(
                xscale="log",
                title=f"{scene} · {luminance:g} cd/m²",
                xlabel="Exposure (ms)",
                ylabel="Common-output SNR (dB)",
            )
            axis.grid(alpha=0.25)
            axis.legend(fontsize=8)
    figure.suptitle("Fixed-control baseline SNR; shaded assumption ranges (clipping excluded)")
    figure.tight_layout()
    plots = [("Matched output SNR and sensitivity ranges", encode(figure))]
    quality = []
    if "macbeth_d65" in scenes:
        quality.append(("macbeth_d65", "color", "mean_delta_e", "In-sample CIE76 residual"))
    if "slanted_edge" in scenes:
        quality.append(("slanted_edge", "edge", "mtf50_cycles_per_output_pixel", "MTF50 (cy/px)"))
    if quality:
        figure, axes = plt.subplots(1, len(quality), squeeze=False, figsize=(6 * len(quality), 3.7))
        luminance = max(luminances)
        for axis, (scene, metric, key, title) in zip(axes[0], quality, strict=True):
            for sensor_index, sensor in enumerate(report["selected_sensors"]):
                values = []
                for exposure in exposures:
                    match = next(
                        (
                            case
                            for case in report["cases"]
                            if (
                                case["sensor_id"] == sensor["sensor_id"]
                                and case["scene"] == scene
                                and case["luminance_cd_m2"] == luminance
                                and case["exposure_s"] == exposure
                                and case["assumption"] == "baseline"
                            )
                        ),
                        None,
                    )
                    value = match["metrics"].get(metric, {}).get(key) if match else None
                    values.append(np.nan if value is None else value)
                axis.plot(x, values, "o-", color=colors(sensor_index % 10), label=sensor["code"])
            axis.set(
                xscale="log",
                xlabel="Exposure (ms)",
                ylabel=title,
                title=f"{title} · {luminance:g} cd/m² · baseline",
            )
            axis.grid(alpha=0.25)
            axis.legend(fontsize=8)
        figure.tight_layout()
        plots.append(("In-sample color residual and slanted-edge estimates", encode(figure)))
    return plots


def _write_report(
    report: dict[str, Any],
    arrays: Mapping[str, np.ndarray],
    directory: Path,
    *,
    write_arrays: bool = True,
) -> None:
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": str(directory / "comparison.json"),
        "npz": str(directory / "comparison_arrays.npz"),
        "html": str(directory / "index.html"),
    }
    _annotate_color_evaluation(report)
    report["artifacts"] = paths
    if write_arrays:
        np.savez_compressed(paths["npz"], **arrays)
    elif not Path(paths["npz"]).is_file():
        raise FileNotFoundError("Refreshing report text requires the existing comparison arrays.")
    Path(paths["json"]).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    cards = []
    baseline = [case for case in report["cases"] if case["assumption"] == "baseline"]
    for case in baseline:
        prefix = case.get("array_prefix")
        if not prefix:
            continue
        peers = [
            item
            for item in baseline
            if all(item[key] == case[key] for key in ("scene", "luminance_cd_m2", "exposure_s"))
        ]
        maximum = max(
            float(np.quantile(arrays[f"{item['array_prefix']}_rgb_reference"], 0.995))
            for item in peers
        )
        scale = 0.85 / max(maximum, np.finfo(float).tiny)
        ref, frame = (
            _png_uri(arrays[f"{prefix}_{name}"], scale) for name in ("rgb_reference", "rgb_frame")
        )
        noise = np.sqrt(np.maximum(arrays[f"{prefix}_luma_variance"], 0))
        noise_max = max(
            float(np.max(np.sqrt(arrays[f"{item['array_prefix']}_luma_variance"])))
            for item in peers
        )
        noise_uri = _png_uri(
            np.repeat((noise / max(noise_max, np.finfo(float).tiny))[..., None], 3, axis=2), 1
        )
        snr = case["metrics"]["common_output"]["snr_db"]
        snr_label = "unavailable" if snr is None else f"{snr:.2f} dB"
        label = (
            f"{case['code']} · {case['scene']} · {case['luminance_cd_m2']:g} cd/m² · "
            f"{case['exposure_s'] * 1000:g} ms"
        )
        clipped = (
            " Clipped capture; excluded from SNR ranking."
            if not case["metrics"]["snr_ranking_eligible"]
            else ""
        )
        cards.append(
            f'<article data-scene="{html.escape(case["scene"])}"><h3>{html.escape(label)}</h3>'
            f'<div class="images"><figure><img src="{ref}">'
            "<figcaption>Noiseless reference</figcaption></figure>"
            f'<figure><img src="{frame}"><figcaption>One capture</figcaption></figure>'
            f'<figure><img src="{noise_uri}">'
            "<figcaption>Temporal noise map</figcaption></figure></div>"
            f"<p>Common-output SNR: {snr_label}.{clipped} Shared display gain: {scale:.5g}; "
            f"noise maximum: {noise_max:.5g}.</p></article>"
        )
    rows = []
    for case in report["cases"]:
        metrics = case["metrics"]
        values = [
            case["code"],
            case["scene"],
            case["luminance_cd_m2"],
            case["exposure_s"],
            case["assumption"],
            metrics["native_raw"]["mean_photoelectrons"],
            metrics["common_output"]["snr_db"],
            metrics["saturation"].get("well_fraction"),
            metrics["saturation"].get("adc_white_fraction"),
            metrics.get("color", {}).get("mean_delta_e"),
            metrics.get("edge", {}).get("mtf50_cycles_per_output_pixel"),
        ]
        rows.append(
            "<tr>"
            + "".join(
                "<td>"
                + html.escape(
                    f"{v:.4g}" if isinstance(v, float) else str(v if v is not None else "—")
                )
                + "</td>"
                for v in values
            )
            + "</tr>"
        )
    inventory = report["inventory"]
    options = "".join(f"<option>{name}</option>" for name in report["controls"]["scene_types"])
    columns = [
        "Sensor",
        "Scene",
        "cd/m²",
        "s",
        "Assumption",
        "Mean e−",
        "Output SNR dB",
        "Well sat.",
        "ADC white",
        "In-sample CIE76",
        "MTF50 cy/px",
    ]
    headers = "".join(f"<th>{name}</th>" for name in columns)
    detail_keys = [
        "comparison_contract",
        "controls",
        "method",
        "assumptions",
        "limitations",
        "uncertainty_summary",
    ]
    details = html.escape(json.dumps({key: report[key] for key in detail_keys}, indent=2))
    plots = "".join(
        f'<h2>{html.escape(title)}</h2><img class="plot" src="{uri}">'
        for title, uri in _metric_plots(report)
    )
    bands = []
    for item in report["uncertainty_summary"]:
        for sensor_id, (low, high) in item["snr_db_assumption_range"].items():
            code = next(
                record["code"]
                for record in report["selected_sensors"]
                if record["sensor_id"] == sensor_id
            )
            changed = item["ranking_changes_under_shared_assumptions"]
            bands.append(
                f"<tr><td>{html.escape(code)}</td><td>{item['scene']}</td>"
                f"<td>{item['luminance_cd_m2']:g}</td>"
                f"<td>{item['exposure_s'] * 1000:g}</td>"
                f"<td>{low:.2f}–{high:.2f}</td><td>{changed}</td></tr>"
            )
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<title>DB sensor estimates</title><style>
body{{font:15px system-ui;margin:32px auto;max-width:1200px;padding:0 20px;
color:#172b3a;background:#f5f7fa}}h1{{font-size:30px}}
.notice,article{{background:white;border:1px solid #d5dde4;border-radius:10px;
padding:18px;margin:18px 0}}.notice{{border-left:5px solid #bc7200}}
.images{{display:flex;gap:16px}}figure{{margin:0;flex:1}}
img{{width:100%;max-width:240px;image-rendering:auto}}figcaption{{font-size:12px}}
img.plot{{max-width:100%;border-radius:8px;background:white}}
table{{border-collapse:collapse;width:100%;font-size:12px}}
td,th{{padding:7px;text-align:right;border-bottom:1px solid #ddd}}
th{{position:sticky;top:0;background:#e7edf3}}.table{{overflow:auto;max-height:650px}}
button,select,input{{font:inherit;padding:6px}}pre{{white-space:pre-wrap;font-size:12px}}
</style><h1>Image sensor comparison · DB estimates</h1><div class="notice">
<strong>Estimate only; no measured product ranking.</strong>
<p>Identical optics, physical crop, scene luminance, exposure and output size.
Shared assumptions are swept independently. No frame-dependent brightness
normalization or AE is used.</p><p>{inventory["record_count"]} catalog records;
{inventory["available_count"]} modelable; {inventory["excluded_count"]} excluded with reasons.
{report["optical_solve_count"]} optical solves, {report["case_count"]} cases,
{report["sample_count"]} temporal samples.</p>
<a href="comparison.json">JSON with full provenance and inventory</a> ·
<a href="comparison_arrays.npz">Baseline arrays (NPZ)</a></div>
{plots}
<p><strong>Color evaluation scope:</strong> the D65 MCC matrix is fitted to the same
24 Macbeth reflectances used by the chart evaluation. CIE76 is an in-sample calibration
fit residual including capture and ISP effects, not independent color accuracy.
No held-out surfaces or illuminants are evaluated.</p>
<h2>Baseline images and temporal noise</h2><p>Reference and capture share one display
gain across sensors for each optical condition. Noise maps share a scale across those
sensors. These view gains never enter metrics.</p><label>Scene
<select id="scene"><option value="all">All</option>{options}</select></label>
{"".join(cards)}<h2>Sensitivity to unknown parameters</h2>
<p>Ranges span shared assumption sets and exclude clipped captures. Order changes refer
only to matched, unsaturated cases under shared assumptions. Each real sensor can have
different electrical parameters; these ranges are not confidence intervals.</p>
<div class="table"><table><thead><tr><th>Sensor</th><th>Scene</th><th>cd/m²</th>
<th>ms</th><th>SNR range (dB)</th><th>Shared order changes</th></tr></thead>
<tbody>{"".join(bands)}</tbody></table></div>
<h2>All assumption sets</h2><input id="search" placeholder="Filter sensor or assumption">
<div class="table"><table><thead><tr>{headers}</tr></thead>
<tbody id="metrics">{"".join(rows)}</tbody></table></div><details>
<summary>Comparison contract, assumptions and limitations</summary><pre>{details}</pre></details>
<script>
document.querySelector('#scene').onchange=e=>document.querySelectorAll('article').forEach(
a=>a.hidden=e.target.value!=='all'&&a.dataset.scene!==e.target.value);
document.querySelector('#search').oninput=e=>document.querySelectorAll('#metrics tr').forEach(
r=>r.hidden=!r.textContent.toLowerCase().includes(e.target.value.toLowerCase()));
</script></html>"""
    Path(paths["html"]).write_text(document, encoding="utf-8")
