"""Noise-free off-axis sensor tiles on CameraE2E's global optical grid.

This adapter retains the native spectral current, Gaussian prefilter, linear
interpolation, CFA selection, and photodetector integration.  Only their
coordinates are made explicit: ``sensor_compute`` otherwise recenters every
OI and sensor array and cannot evaluate an arbitrary off-center crop.

The result is a photoelectron *mean*, before dark current, random or fixed
noise, gain, full-well clipping, and ADC conversion.  In particular a seed
cannot make independently sampled noisy tiles identical to a full RAW frame.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.signal import convolve2d

from pyisetcam.sensor import (
    _ELEMENTARY_CHARGE_C,
    _gaussian_kernel,
    _interp2_linear_constant_zero,
    _sensor_apply_physics_response,
    _sensor_pd_array,
    _signal_current_density,
)
from pyisetcam.sensor_capture import _readout_values
from pyisetcam.types import OpticalImage, Sensor

from .lens_sensor_centered_oi import _sensor_contract

SCHEMA = "camerae2e_offaxis_sensor_mean_tile_v1"


def _shape(value: Any, name: str, *, minimum: int) -> tuple[int, int]:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or any(type(item) is not int or item < minimum for item in value)
    ):
        raise ValueError(f"{name} requires two integer dimensions >= {minimum}.")
    return value[0], value[1]


def _bounds(
    value: Any, full_shape: tuple[int, int], name: str, *, minimum: int
) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 4
        or any(type(item) is not int for item in value)
    ):
        raise ValueError(f"{name} requires four integer half-open bounds.")
    r0, r1, c0, c1 = value
    if not (
        0 <= r0 < r1 <= full_shape[0]
        and 0 <= c0 < c1 <= full_shape[1]
        and r1 - r0 >= minimum
        and c1 - c0 >= minimum
    ):
        raise ValueError(f"{name} lies outside its global grid or is too small.")
    return r0, r1, c0, c1


def _require_unity_etendue(sensor: Sensor, sensor_shape: tuple[int, int]) -> None:
    vignetting = sensor.fields.get("vignetting", 0)
    if isinstance(vignetting, str):
        allowed = vignetting.strip().lower().replace(" ", "") in {
            "skip",
            "off",
            "none",
            "disabled",
            "false",
            "",
        }
    else:
        allowed = type(vignetting) in (int, float) and vignetting == 0
    if not allowed:
        raise ValueError("Active or unknown sensor vignetting is unsupported for off-axis tiles.")
    stored = sensor.fields.get("etendue")
    if stored is not None:
        values = np.asarray(stored)
        if values.ndim == 0:
            try:
                scalar = float(values)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Nonunity or unknown sensor etendue is unsupported for off-axis tiles."
                ) from exc
            if math.isfinite(scalar) and scalar == 1:
                return
            raise ValueError(
                "Nonunity or unknown sensor etendue is unsupported for off-axis tiles."
            )
        if (
            values.shape != sensor_shape
            or not np.issubdtype(values.dtype, np.number)
            or np.iscomplexobj(values)
        ):
            raise ValueError(
                "Nonunity or unknown sensor etendue is unsupported for off-axis tiles."
            )
        # A previously computed sensor may retain a full unity map. Validate
        # it without constructing full-size dtype or boolean temporary arrays.
        # This scan is O(full sensor pixels) in time; callers can avoid it by
        # retaining the original uncomputed sensor with etendue=None.
        for row in range(0, sensor_shape[0], 256):
            for col in range(0, sensor_shape[1], 256):
                chunk = np.asarray(values[row : row + 256, col : col + 256], dtype=float)
                if not np.isfinite(chunk).all() or not np.all(chunk == 1):
                    raise ValueError(
                        "Nonunity or unknown sensor etendue is unsupported for off-axis tiles."
                    )


def _global_target_axis(
    full_count: int, first_pixel: int, last_pixel: int, pitch_m: float, samples: int
) -> np.ndarray:
    step = 1.0 / samples
    # Native _sample2space subtracts the mean of the full subpixel axis,
    # which is full_count / 2.  Generate only this tile's subpixel samples.
    indices = np.arange(first_pixel * samples, last_pixel * samples, dtype=float)
    return ((indices * step + step / 2.0) - full_count / 2.0) * pitch_m


def _global_source_axis(full_count: int, first: int, last: int, spacing_m: float) -> np.ndarray:
    # Native _sample2space on [0, full_count) subtracts (full_count-1)/2.
    indices = np.arange(first, last, dtype=float)
    return (indices - (full_count - 1.0) / 2.0) * spacing_m


def _required_source_axis(
    full_source_count: int, target_axis: np.ndarray, spacing_m: float, kernel_size: int
) -> tuple[int, int]:
    coordinates = target_axis / spacing_m + (full_source_count - 1.0) / 2.0
    # scipy.signal.convolve2d(mode='same') at index i consumes
    # [i-K//2, i+(K-1)//2].  Interpolation additionally consumes its lower
    # and upper sample.  Values outside the *full* OI are zero in native code.
    first = int(np.floor(float(np.min(coordinates)))) - kernel_size // 2
    last = int(np.ceil(float(np.max(coordinates)))) + (kernel_size - 1) // 2
    return max(first, 0), min(last, full_source_count - 1)


def _preflight_grid(
    sensor: Sensor,
    full_oi_shape_row_col: tuple[int, int],
    sensor_tile_bounds_row_col_exclusive: tuple[int, int, int, int],
    oi_sample_spacing_m: float,
) -> tuple[dict[str, Any], tuple[np.ndarray, np.ndarray]]:
    full_shape = _shape(full_oi_shape_row_col, "Full OI shape", minimum=2)
    sensor_shape, pixel_pitch, samples = _sensor_contract(sensor)
    _require_unity_etendue(sensor, sensor_shape)
    readout = sensor.fields.get("sensor_readout")
    if not isinstance(readout, dict):
        raise ValueError("A configured digital RAW readout is required.")
    try:
        _readout_values(readout)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Digital RAW readout is incomplete or unsupported.") from exc
    sensor_bounds = _bounds(
        sensor_tile_bounds_row_col_exclusive,
        sensor_shape,
        "Sensor tile",
        minimum=1,
    )
    spacing = float(oi_sample_spacing_m)
    if not math.isfinite(spacing) or spacing <= 0:
        raise ValueError("OI sample spacing must be finite and positive.")
    pattern = np.asarray(sensor.fields.get("pattern"))
    if (
        pattern.ndim != 2
        or min(pattern.shape) < 1
        or not np.issubdtype(pattern.dtype, np.integer)
        or np.any(pattern < 1)
    ):
        raise ValueError("A nonempty positive integer CFA site pattern is required.")

    target_axes = tuple(
        _global_target_axis(full_count, start, stop, float(pitch), samples)
        for full_count, start, stop, pitch in zip(
            sensor_shape,
            (sensor_bounds[0], sensor_bounds[2]),
            (sensor_bounds[1], sensor_bounds[3]),
            pixel_pitch,
            strict=True,
        )
    )
    kernel_shape = tuple(
        max(1, int(np.ceil(float(pitch) / max(spacing, 1e-12)))) for pitch in pixel_pitch
    )
    dependencies = tuple(
        _required_source_axis(full_count, target, spacing, kernel_size)
        for full_count, target, kernel_size in zip(
            full_shape, target_axes, kernel_shape, strict=True
        )
    )
    for axis, (first, last) in enumerate(dependencies):
        if first > last:
            raise ValueError(f"Sensor tile axis {axis} lies outside the modeled full OI.")
    preflight = {
        "schema": SCHEMA,
        "status": "noise_free_sensor_tile_preflight_passed",
        "global_oi_shape_row_col": list(full_shape),
        "sensor_global_shape_row_col": list(sensor_shape),
        "sensor_tile_bounds_row_col_exclusive": list(sensor_bounds),
        "required_global_oi_indices_inclusive_by_axis": [
            list(dependency) for dependency in dependencies
        ],
        "gaussian_kernel_shape_row_col": list(kernel_shape),
        "oi_sample_spacing_m": spacing,
        "sensor_pixel_pitch_m_row_col": pixel_pitch.tolist(),
        "cfa_phase_origin_sensor_row_col": [sensor_bounds[0], sensor_bounds[2]],
        "sensor_subpixel_samples_per_pixel": samples,
        "sensor_center_alignment": "global_oi_center_equals_global_sensor_center",
        "readout_executed": False,
        "noise_and_fixed_pattern_executed": False,
        "full_frame_execution_qualified": False,
    }
    return preflight, target_axes


def preflight_fixed_sensor_tile_mean_electrons(
    sensor: Sensor,
    *,
    full_oi_shape_row_col: tuple[int, int],
    sensor_tile_bounds_row_col_exclusive: tuple[int, int, int, int],
    oi_sample_spacing_m: float,
) -> dict[str, Any]:
    """Return the exact global OI dependency interval before optical work.

    The interval is inclusive and clipped to the true full OI boundaries;
    native convolution uses zero outside that full grid.  A caller should
    request an optical ROI covering both axes' intervals and at least two OI
    samples on each axis.  Coordinate arrays scale only with the requested
    sensor tile, independent of either full virtual grid's dimensions.
    """
    preflight, _ = _preflight_grid(
        sensor,
        full_oi_shape_row_col,
        sensor_tile_bounds_row_col_exclusive,
        oi_sample_spacing_m,
    )
    return preflight


def compute_fixed_sensor_tile_mean_electrons(
    sensor: Sensor,
    optical_roi_photons: np.ndarray,
    *,
    full_oi_shape_row_col: tuple[int, int],
    optical_roi_bounds_row_col_exclusive: tuple[int, int, int, int],
    sensor_tile_bounds_row_col_exclusive: tuple[int, int, int, int],
    wavelength_nm: np.ndarray,
    oi_sample_spacing_m: float,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Integrate one global sensor tile using a globally located optical ROI.

    ``full_oi_shape_row_col`` describes the actual post-PSF optical image,
    including any strict raytrace padding.  Its center and the center of the
    full sensor are the same physical point, as in native ``sensor_compute``.
    The caller must supply photons computed for the declared *global* OI
    indices, including every source sample required by the sensor halo.

    Supported: fixed-exposure linear mosaic ``digital_raw`` sensor; native
    spectral/QE and pixel-area operations; no active optional spatial physics,
    OCL group proxy, sensor vignetting, or nonunity etendue.  Only the mean
    photoelectrons are returned; the readout is deliberately not executed.
    """
    preflight, target_axes = _preflight_grid(
        sensor,
        full_oi_shape_row_col,
        sensor_tile_bounds_row_col_exclusive,
        oi_sample_spacing_m,
    )
    full_oi_shape = tuple(preflight["global_oi_shape_row_col"])
    sensor_bounds = tuple(preflight["sensor_tile_bounds_row_col_exclusive"])
    pixel_pitch = np.asarray(preflight["sensor_pixel_pitch_m_row_col"], dtype=float)
    samples = int(preflight["sensor_subpixel_samples_per_pixel"])
    kernel_shape = tuple(preflight["gaussian_kernel_shape_row_col"])
    dependencies = tuple(
        tuple(axis) for axis in preflight["required_global_oi_indices_inclusive_by_axis"]
    )
    oi_bounds = _bounds(
        optical_roi_bounds_row_col_exclusive,
        full_oi_shape,
        "Optical ROI",
        minimum=2,
    )
    photons = np.asarray(optical_roi_photons)
    wave = np.asarray(wavelength_nm, dtype=float)
    if (
        photons.ndim != 3
        or photons.shape[:2] != (oi_bounds[1] - oi_bounds[0], oi_bounds[3] - oi_bounds[2])
        or photons.shape[2] != wave.size
        or not np.issubdtype(photons.dtype, np.number)
        or np.iscomplexobj(photons)
        or not np.isfinite(photons).all()
        or wave.ndim != 1
        or wave.size < 1
        or not np.isfinite(wave).all()
        or np.any(np.diff(wave) <= 0)
    ):
        raise ValueError("Optical ROI requires finite real photons and a matching wavelength axis.")
    exposure = float(np.asarray(sensor.fields["integration_time"], dtype=float).reshape(-1)[0])
    pattern = np.asarray(sensor.fields.get("pattern"), dtype=int)
    for axis, (lower, upper, required) in enumerate(
        zip(
            (oi_bounds[0], oi_bounds[2]),
            (oi_bounds[1], oi_bounds[3]),
            dependencies,
            strict=True,
        )
    ):
        if lower > required[0] or upper <= required[1]:
            raise ValueError(
                f"Optical ROI omits sensor interpolation/Gaussian halo on axis {axis}."
            )

    roi_oi = OpticalImage(fields={"wave": wave.copy()}, data={"photons": photons})
    current_density = _signal_current_density(roi_oi, sensor)
    if pattern.max() > current_density.shape[2]:
        raise ValueError("CFA pattern refers to a missing spectral filter channel.")
    tile_shape = (
        sensor_bounds[1] - sensor_bounds[0],
        sensor_bounds[3] - sensor_bounds[2],
    )
    row_sites = (
        np.floor(
            (1.0 / samples)
            * np.arange(sensor_bounds[0] * samples, sensor_bounds[1] * samples, dtype=float)
        ).astype(int)
        % pattern.shape[0]
    )
    col_sites = (
        np.floor(
            (1.0 / samples)
            * np.arange(sensor_bounds[2] * samples, sensor_bounds[3] * samples, dtype=float)
        ).astype(int)
        % pattern.shape[1]
    )
    cfa = pattern[row_sites[:, None], col_sites[None, :]]
    kernel = _gaussian_kernel(kernel_shape, kernel_shape[0] / 4.0)
    source_axes = (
        _global_source_axis(
            full_oi_shape[0], oi_bounds[0], oi_bounds[1], preflight["oi_sample_spacing_m"]
        ),
        _global_source_axis(
            full_oi_shape[1], oi_bounds[2], oi_bounds[3], preflight["oi_sample_spacing_m"]
        ),
    )
    sampled_current_density = np.zeros(cfa.shape, dtype=float)
    for channel in range(current_density.shape[2]):
        plane = convolve2d(
            np.asarray(current_density[:, :, channel], dtype=float),
            kernel,
            mode="same",
        )
        sampled = _interp2_linear_constant_zero(
            plane, source_axes[0], source_axes[1], target_axes[0], target_axes[1]
        )
        sampled_current_density += (cfa == channel + 1) * sampled

    pixel_area = float(np.prod(pixel_pitch))
    if samples == 1:
        signal_current = (
            sampled_current_density * float(sensor.fields["pixel"]["fill_factor"]) * pixel_area
        )
    else:
        pd_array = _sensor_pd_array(sensor, 1.0 / samples)
        photodetector = np.tile(pd_array, tile_shape)
        small_current = sampled_current_density * photodetector
        box = pixel_area * (np.ones((samples, samples), dtype=float) / float(samples**2))
        blurred = convolve2d(small_current, box, mode="same")
        start = samples // 2
        signal_current = blurred[start::samples, start::samples]

    signal_current = _sensor_apply_physics_response(sensor, signal_current)
    conversion_gain = float(sensor.fields["pixel"]["conversion_gain_v_per_electron"])
    if not math.isfinite(conversion_gain) or conversion_gain <= 0:
        raise ValueError("Pixel conversion gain must be finite and positive.")
    # Match sensor_compute_noise_free's voltage arithmetic and its temporary
    # 1e6-V anti-saturation swing before converting back to electrons.
    volts = signal_current * (exposure * conversion_gain / _ELEMENTARY_CHARGE_C)
    mean = np.clip(volts, 0.0, 1.0e6) / conversion_gain
    if mean.shape != tile_shape or not np.isfinite(mean).all():
        raise ValueError("Computed sensor tile has an invalid shape or nonfinite means.")

    evidence = {
        **preflight,
        "status": "noise_free_mean_electrons_computed",
        "optical_roi_bounds_row_col_exclusive": list(oi_bounds),
        "exposure_time_s": exposure,
        "optical_source_provenance_verified": False,
    }
    return np.asarray(mean, dtype=float), evidence
