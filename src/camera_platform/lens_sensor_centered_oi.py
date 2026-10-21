"""Crop a computed optical image without shifting a fixed native RAW grid.

This is a sensor-stage optimization. The caller must already have a globally
correct optical image, including distortion and the complete PSF source halo.
It does not make a cropped Scene or a locally recomputed PSF equivalent to a
full optical calculation.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from pyisetcam.sensor import _sample2space, _sensor_ocl_group_proxy
from pyisetcam.types import OpticalImage, Sensor

SCHEMA = "camerae2e_centered_optical_roi_sensor_stage_v1"


def _positive(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive.")
    return float(value)


def _shape(value: Any, name: str) -> tuple[int, int]:
    if (
        not isinstance(value, (tuple, list)) or len(value) != 2
        or any(type(v) is not int or v < 2 for v in value)
    ):
        raise ValueError(f"{name} must be two integer dimensions of at least two.")
    return value[0], value[1]


def _sensor_contract(sensor: Sensor) -> tuple[tuple[int, int], np.ndarray, int]:
    if (
        sensor.fields.get("sensor_output_mode") != "digital_raw"
        or sensor.fields.get("mosaic") is not True
        or str(sensor.fields.get("response_type", "linear")).lower() != "linear"
        or sensor.fields.get("sensor_compute_method")
        or sensor.fields.get("auto_exposure") is not False
    ):
        raise ValueError("A fixed-exposure linear mosaic digital RAW sensor is required.")
    exposure = np.asarray(sensor.fields.get("integration_time"), dtype=float)
    if exposure.size != 1 or exposure.ndim > 1 or not math.isfinite(float(exposure)):
        raise ValueError("Sensor exposure must be one fixed finite scalar.")
    if float(exposure) <= 0:
        raise ValueError("Sensor exposure must be positive for crop equivalence.")
    size = _shape(sensor.fields.get("size"), "Sensor size")
    pixel = sensor.fields.get("pixel")
    if not isinstance(pixel, dict):
        raise ValueError("Sensor pixel geometry is unavailable.")
    spacing = np.asarray(pixel.get("size_m"), dtype=float)
    if spacing.shape != (2,) or not np.isfinite(spacing).all() or np.any(spacing <= 0):
        raise ValueError("Sensor pixel size must be two finite positive meters.")
    # The current sensor integrator places centers using pixel.size_m rather
    # than size+gap. A nonzero gap needs a separate global placement contract.
    if any(
        float(pixel.get(key, 0.0)) != 0.0
        for key in ("height_gap_m", "width_gap_m")
    ):
        raise ValueError("Sensor pixel gaps need a separate spatial placement contract.")
    # The default route has a local spectral QE followed by a known spatial
    # integration stencil. Optional LUT and OCL branches can introduce other
    # spatial support or a different OI response; this ROI contract has not
    # established their required source halo.
    for key in ("fdtd_sensor", "tcad_sensor"):
        config = sensor.fields.get(key)
        if config is not None and not (
            isinstance(config, dict) and config.get("enabled") is False
        ):
            raise ValueError(f"Active or unknown {key} response is unsupported for OI crop.")
    for key in ("physical_estimate", "fdtd_estimate"):
        if sensor.fields.get(key) is not None:
            raise ValueError(f"Active or unknown {key} response is unsupported for OI crop.")
    stored_ocl = sensor.fields.get("ocl_group_proxy")
    if stored_ocl is not None:
        if not isinstance(stored_ocl, dict):
            raise ValueError("Unknown OCL group proxy is unsupported for OI crop.")
        try:
            normalized_ocl = _sensor_ocl_group_proxy(sensor)
        except (TypeError, ValueError) as exc:
            raise ValueError("Unknown OCL group proxy is unsupported for OI crop.") from exc
        if normalized_ocl["mode"] != "off":
            raise ValueError("Active OCL group proxy is unsupported for OI crop.")
    samples = sensor.fields.get("n_samples_per_pixel", 1)
    if type(samples) is not int or samples < 1 or samples % 2 == 0:
        raise ValueError("Sensor subpixel sample count must be positive and odd.")
    return size, spacing, samples


def _oi_contract(oi: OpticalImage) -> tuple[tuple[int, int], float, float]:
    photons = np.asarray(oi.data.get("photons"))
    wave = np.asarray(oi.fields.get("wave"), dtype=float)
    if (
        photons.ndim != 3 or min(photons.shape[:2]) < 2
        or wave.ndim != 1 or photons.shape[2] != wave.size
        or not np.isfinite(photons).all() or not np.isfinite(wave).all()
    ):
        raise ValueError("Computed optical photons and wavelength axis are required.")
    if oi.fields.get("crop") is True:
        raise ValueError("A previously cropped OI has no verified global optical origin.")
    rows, cols = photons.shape[:2]
    width = _positive(oi.fields.get("width_m"), "Full OI width")
    height = _positive(oi.fields.get("height_m"), "Full OI height")
    spacing = _positive(oi.fields.get("sample_spacing_m"), "Full OI sample spacing")
    distance = _positive(oi.fields.get("image_distance_m"), "Full OI image distance")
    if not (
        math.isclose(width / cols, spacing, rel_tol=1e-10, abs_tol=1e-15)
        and math.isclose(height / rows, spacing, rel_tol=1e-10, abs_tol=1e-15)
    ):
        raise ValueError("Full OI width, height and sample spacing disagree.")
    for key, extent in (("fov_deg", width), ("vfov_deg", height)):
        expected = math.degrees(2.0 * math.atan(extent / (2.0 * distance)))
        actual = oi.fields.get(key)
        if (
            type(actual) not in (int, float) or not math.isfinite(actual)
            or not math.isclose(float(actual), expected, rel_tol=1e-9, abs_tol=1e-10)
        ):
            raise ValueError(f"Full OI {key} disagrees with physical extent and image distance.")
    return (rows, cols), spacing, distance


def _required_axis(
    full_count: int, sensor_count: int, oi_spacing: float,
    sensor_spacing: float, subpixel_samples: int,
) -> dict[str, Any]:
    source = _sample2space(np.arange(full_count, dtype=float), oi_spacing)
    step = 1.0 / subpixel_samples
    target_samples = np.arange(0.0, sensor_count, step, dtype=float) + step / 2.0
    target = _sample2space(target_samples, sensor_spacing)
    if target[0] < source[0] or target[-1] > source[-1]:
        raise ValueError("Full OI does not cover the sensor's modeled subpixel centers.")
    kernel_size = max(1, int(np.ceil(sensor_spacing / max(oi_spacing, 1e-12))))
    coordinates = (target - source[0]) / (source[1] - source[0])
    first_interpolation = int(np.floor(float(coordinates.min())))
    last_interpolation = int(np.ceil(float(coordinates.max())))
    # scipy.signal.convolve2d(mode='same') at output i depends on input
    # [i - K//2, i + (K-1)//2]. Include both interpolation brackets.
    required_first = first_interpolation - kernel_size // 2
    required_last = last_interpolation + (kernel_size - 1) // 2
    if required_first < 0 or required_last >= full_count:
        raise ValueError("Full OI lacks the sensor interpolation/convolution source halo.")
    center = (full_count - 1.0) / 2.0
    minimum = int(math.ceil(2 * max(
        center - required_first, required_last - center
    ) + 1 - 1e-12))
    minimum = max(minimum, int(math.ceil(sensor_count * sensor_spacing / oi_spacing - 1e-12)))
    if (full_count - minimum) % 2:
        minimum += 1
    return {
        "sensor_target_centers_m": [float(target[0]), float(target[-1])],
        "required_full_oi_source_indices_inclusive": [required_first, required_last],
        "gaussian_kernel_samples": kernel_size,
        "minimum_centered_oi_samples": minimum,
    }


def preflight_centered_oi_crop(
    full_oi: OpticalImage, sensor: Sensor, roi_shape_row_col: tuple[int, int] | list[int]
) -> dict[str, Any]:
    """Require the exact full-grid origin and all modeled sensor dependencies."""
    full_shape, spacing, distance = _oi_contract(full_oi)
    sensor_shape, pixel_size, samples = _sensor_contract(sensor)
    roi_shape = _shape(roi_shape_row_col, "OI ROI shape")
    if any(roi > full for roi, full in zip(roi_shape, full_shape, strict=True)):
        raise ValueError("OI ROI exceeds the full computed optical image.")
    if any((full - roi) % 2 for roi, full in zip(roi_shape, full_shape, strict=True)):
        raise ValueError("Centered OI ROI must have the same odd/even parity as the full OI.")
    origin = tuple((full - roi) // 2 for roi, full in zip(roi_shape, full_shape, strict=True))
    axes = [
        _required_axis(full, target, spacing, float(pixel), samples)
        for full, target, pixel in zip(full_shape, sensor_shape, pixel_size, strict=True)
    ]
    for axis, (full, roi, start, sensor_pixels, pixel) in enumerate(zip(
        full_shape, roi_shape, origin, sensor_shape, pixel_size, strict=True
    )):
        required_first, required_last = axes[axis]["required_full_oi_source_indices_inclusive"]
        if start > required_first or start + roi - 1 < required_last:
            raise ValueError(
                f"Centered OI ROI omits sensor interpolation/convolution halo on axis {axis}."
            )
        if roi * spacing + 1e-15 < sensor_pixels * float(pixel):
            raise ValueError(f"Centered OI ROI misses the active pixel-area extent on axis {axis}.")
        if not (0 <= start and start + roi <= full):
            raise ValueError("Centered OI ROI origin lies outside the global optical grid.")
    return {
        "schema": SCHEMA,
        "status": "sensor_stage_crop_preflight_passed",
        "full_oi_shape_row_col": list(full_shape),
        "roi_shape_row_col": list(roi_shape),
        "roi_origin_in_full_oi_row_col": list(origin),
        "roi_stop_exclusive_in_full_oi_row_col": [
            start + count for start, count in zip(origin, roi_shape, strict=True)
        ],
        "minimum_centered_shape_row_col": [
            axis["minimum_centered_oi_samples"] for axis in axes
        ],
        "sample_spacing_m": spacing,
        "image_distance_m": distance,
        "sensor_shape_row_col": list(sensor_shape),
        "sensor_pixel_size_m_row_col": pixel_size.tolist(),
        "sensor_subpixel_samples_per_pixel": samples,
        "sensor_dependency_by_axis": axes,
        "global_optical_calculation_verified": False,
        "full_scene_memory_reduced": False,
        "same_noise_realization_requires_same_sensor_and_seed": True,
        "optional_spatial_responses": "absent_or_explicitly_disabled",
    }


def centered_oi_crop_for_sensor(
    full_oi: OpticalImage, sensor: Sensor, roi_shape_row_col: tuple[int, int] | list[int]
) -> tuple[OpticalImage, dict[str, Any]]:
    """Return a sensor-only OI crop with full-grid spacing and center intact."""
    preflight = preflight_centered_oi_crop(full_oi, sensor, roi_shape_row_col)
    row0, col0 = preflight["roi_origin_in_full_oi_row_col"]
    rows, cols = preflight["roi_shape_row_col"]
    spacing = preflight["sample_spacing_m"]
    distance = preflight["image_distance_m"]
    cropped = full_oi.clone()
    cropped.data["photons"] = np.asarray(full_oi.data["photons"])[
        row0:row0 + rows, col0:col0 + cols, :
    ].copy()
    depth = full_oi.fields.get("depth_map_m")
    if depth is not None:
        depth_array = np.asarray(depth)
        if depth_array.shape != tuple(preflight["full_oi_shape_row_col"]):
            raise ValueError("Full OI depth map does not match the optical photon array.")
        cropped.fields["depth_map_m"] = depth_array[
            row0:row0 + rows, col0:col0 + cols
        ].copy()
    cropped.fields.update({
        "rows": rows, "cols": cols,
        "width_m": cols * spacing,
        "height_m": rows * spacing,
        "sample_spacing_m": spacing,
        "image_distance_m": distance,
        "fov_deg": math.degrees(2.0 * math.atan(cols * spacing / (2.0 * distance))),
        "vfov_deg": math.degrees(2.0 * math.atan(rows * spacing / (2.0 * distance))),
    })
    for key in ("illuminance", "mean_illuminance", "psf_struct", "psf_streaming"):
        cropped.fields.pop(key, None)
    cropped.metadata["centered_oi_sensor_stage"] = dict(preflight)
    return cropped, preflight
