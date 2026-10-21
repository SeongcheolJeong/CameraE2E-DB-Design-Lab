"""Time-resolved optical integration followed by one physical RAW readout.

Callbacks take simulation time in seconds. They supply instantaneous spectral
radiance/irradiance, not already exposed images. Adjacent sampled electron rates
are linearly interpolated and integrated over each row's own exposure window.
Only two sampled sensor images are retained, regardless of sequence length.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil

import numpy as np

from .assets import AssetStore
from .ip import ip_compute
from .optics import oi_compute, oi_get
from .sensor import sensor_compute_noise_free, sensor_get, sensor_set, sensor_set_size_to_fov
from .sensor_capture import (
    _require_single_linear_capture,
    configure_e2e_sensor,
    resample_sensor_capture,
)
from .types import Camera, OpticalImage, Scene, Sensor


@dataclass(frozen=True)
class TemporalCaptureConfig:
    """Exposure timing in seconds; frame start precedes hidden rolling rows.

    Global shutter exposes all active rows at ``frame_start_s``. Rolling shutter
    starts row r at frame_start_s + (hidden_lines_top + r) * line_time_s.
    Sampling spacing is at most exposure / samples_per_exposure. Increasing
    this value resolves faster motion/flicker; the cap raises rather than
    silently degrading accuracy. Sequential readout is modeled by HW ISP.
    """

    frame_start_s: float = 0.0
    line_time_s: float = 0.0
    rolling_shutter: bool = True
    hidden_lines_top: int = 0
    samples_per_exposure: int = 16
    max_scene_samples: int = 4096

    def __post_init__(self) -> None:
        for name in ("frame_start_s", "line_time_s"):
            if not np.isfinite(getattr(self, name)):
                raise ValueError(f"Temporal {name} must be finite.")
        if self.line_time_s < 0:
            raise ValueError("Temporal line_time_s must be nonnegative.")
        if not isinstance(self.rolling_shutter, (bool, np.bool_)):
            raise ValueError("Temporal rolling_shutter must be a boolean.")
        for name, minimum in (
            ("hidden_lines_top", 0),
            ("samples_per_exposure", 1),
            ("max_scene_samples", 2),
        ):
            value = getattr(self, name)
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
                raise ValueError(f"Temporal {name} must be an integer >= {minimum}.")
            if value < minimum:
                raise ValueError(f"Temporal {name} must be an integer >= {minimum}.")


def _validate_photons(value: Scene | OpticalImage, expected_type: type) -> np.ndarray:
    if not isinstance(value, expected_type):
        raise TypeError(f"Temporal callback must return a {expected_type.__name__}.")
    photons = np.asarray(value.data.get("photons"), dtype=float)
    wave = np.asarray(value.fields.get("wave"), dtype=float)
    if photons.ndim != 3 or wave.ndim != 1 or photons.shape[2] != wave.size:
        raise ValueError("Temporal input requires an H x W x wavelength photon cube.")
    if not all(photons.shape) or not np.all(np.isfinite(photons)) or np.any(photons < 0):
        raise ValueError("Temporal photon rates must be finite, nonnegative and nonempty.")
    if not np.all(np.isfinite(wave)) or np.any(wave <= 0) or np.any(np.diff(wave) <= 0):
        raise ValueError("Temporal wavelengths must be positive and strictly increasing.")
    return photons


def _row_offsets(rows: int, timing: TemporalCaptureConfig) -> np.ndarray:
    if not timing.rolling_shutter:
        return np.zeros(rows, dtype=float)
    return (timing.hidden_lines_top + np.arange(rows, dtype=float)) * timing.line_time_s


def sensor_compute_temporal(
    sensor: Sensor,
    oi_at: Callable[[float], OpticalImage],
    *,
    timing: TemporalCaptureConfig | None = None,
    seed: int | None = None,
) -> Sensor:
    """Integrate instantaneous optical images and produce a digital RAW capture.

    The sensor must have a fixed scalar exposure (seconds); AE must be resolved
    before capture. Existing CFA/QE, spatial integration and attached sensor
    physics participate at every time sample. Dark charge, noise, FPN, full-well
    clipping, gain and ADC run once after integration. This is a linear mosaic
    capture, not staggered HDR or temporal denoising.
    """

    if not callable(oi_at):
        raise TypeError("oi_at must be callable with simulation time in seconds.")
    resolved = TemporalCaptureConfig() if timing is None else timing
    if not isinstance(resolved, TemporalCaptureConfig):
        raise TypeError("timing must be a TemporalCaptureConfig.")
    configured = configure_e2e_sensor(sensor)
    if configured.fields.get("auto_exposure", False):
        raise ValueError("Temporal capture requires fixed exposure; resolve AE before capture.")
    exposure = _require_single_linear_capture(configured)
    shape = tuple(configured.fields["size"])
    starts = _row_offsets(shape[0], resolved)
    ends = starts + exposure
    absolute_starts = starts + resolved.frame_start_s
    absolute_ends = ends + resolved.frame_start_s
    if not np.all(np.isfinite(absolute_ends)):
        raise ValueError("Temporal row exposure times must be finite.")
    if exposure > 0 and (
        np.any(absolute_ends <= absolute_starts)
        or (
            resolved.rolling_shutter
            and resolved.line_time_s > 0
            and np.any(np.diff(absolute_starts) <= 0)
        )
    ):
        raise ValueError("Temporal timestamp precision is insufficient; rebase the time origin.")
    total = np.zeros(shape, dtype=float)
    sample_count = 0
    spacing = 0.0

    if exposure > 0:
        span = float(ends[-1] - starts[0])
        intervals_float = span / exposure * resolved.samples_per_exposure
        if not np.isfinite(intervals_float) or intervals_float > resolved.max_scene_samples - 1:
            raise ValueError(
                "Temporal capture exceeds max_scene_samples; increase the explicit cap, "
                "reduce sensor rows/readout span, or choose a coarser samples_per_exposure."
            )
        intervals = max(1, int(ceil(intervals_float)))
        times = np.linspace(starts[0], ends[-1], intervals + 1)
        if np.any(np.diff(times + resolved.frame_start_s) <= 0):
            raise ValueError(
                "Temporal timestamp precision is insufficient; rebase the time origin."
            )
        spacing = float(times[1] - times[0])
        if spacing <= 0:
            raise ValueError("Temporal exposure is too small to resolve at this readout time.")
        optical_sensor = configured.clone()
        optical_sensor.fields.pop("sensor_output_mode", None)
        optical_sensor.fields.pop("sensor_readout", None)
        optical_sensor.metadata.pop("sensor_capture", None)
        # Compute an un-clipped, noise-free exposure at each instant. Weight by
        # overlap / exposure below, avoiding large one-second charge intermediates.
        optical_sensor = sensor_set(optical_sensor, "integration time", exposure)
        reference: tuple[tuple[int, ...], np.ndarray, np.ndarray] | None = None

        def sample(relative_time: float) -> np.ndarray:
            nonlocal reference, sample_count
            oi = oi_at(float(resolved.frame_start_s + relative_time))
            photons = _validate_photons(oi, OpticalImage)
            physical_size = np.array([oi_get(oi, "width"), oi_get(oi, "height")], dtype=float)
            if not np.all(np.isfinite(physical_size)) or np.any(physical_size <= 0):
                raise ValueError(
                    "Temporal optical image physical size must be finite and positive."
                )
            if reference is None:
                reference = photons.shape, np.asarray(oi.fields["wave"]).copy(), physical_size
            elif (
                photons.shape != reference[0]
                or not np.array_equal(oi.fields["wave"], reference[1])
                or not np.array_equal(physical_size, reference[2])
            ):
                raise ValueError(
                    "Temporal optical image shape, wavelength grid and physical size "
                    "must stay fixed."
                )
            computed = sensor_compute_noise_free(optical_sensor, oi)
            mean = np.asarray(computed.data["volts"], dtype=float) / float(
                computed.fields["pixel"]["conversion_gain_v_per_electron"]
            )
            if mean.shape != shape or not np.all(np.isfinite(mean)) or np.any(mean < 0):
                raise ValueError(
                    "Temporal optical response must be finite nonnegative sensor charge."
                )
            sample_count += 1
            return mean

        previous = sample(float(times[0]))
        for left, right in zip(times[:-1], times[1:], strict=True):
            current = sample(float(right))
            lower = np.maximum(starts, left)
            upper = np.minimum(ends, right)
            duration = np.maximum(upper - lower, 0.0)
            # Exact integral of the piecewise-linear sampled response, including
            # fractional intervals at each individual row's exposure boundaries.
            weight_right = np.where(
                duration > 0,
                duration * ((upper + lower) / 2.0 - left) / (right - left),
                0.0,
            )
            weight_left = duration - weight_right
            total += (
                previous * (weight_left / exposure)[:, None]
                + current * (weight_right / exposure)[:, None]
            )
            previous = current

    configured.data["mean_electrons"] = total
    configured.metadata["temporal_capture"] = {
        "schema": "camerae2e_temporal_capture_v1",
        "frame_start_s": float(resolved.frame_start_s),
        "exposure_time_s": exposure,
        "rolling_shutter": bool(resolved.rolling_shutter),
        "line_time_s": float(resolved.line_time_s),
        "hidden_lines_top": int(resolved.hidden_lines_top),
        "row_exposure_start_s": absolute_starts.tolist(),
        "row_exposure_end_s": absolute_ends.tolist(),
        "samples_per_exposure": int(resolved.samples_per_exposure),
        "sample_count": sample_count,
        "max_sample_spacing_s": spacing,
        "quadrature": "piecewise_linear_response_exact_row_window_integral",
        "readouts_per_capture": 1,
        "input": "instantaneous_spectral_photon_rate",
    }
    selected_seed = configured.fields.get("noise_seed", 0) if seed is None else seed
    return resample_sensor_capture(configured, seed=selected_seed)


def camera_compute_temporal(
    camera: Camera,
    scene_at: Callable[[float], Scene],
    *,
    timing: TemporalCaptureConfig | None = None,
    seed: int | None = None,
    asset_store: AssetStore | None = None,
    sensor_resize: bool = True,
) -> Camera:
    """Render changing scenes through optics, row exposure integration and ISP.

    ``scene_at(t_seconds)`` must be a pure function returning instantaneous
    spectral Scene data with fixed shape, wavelength grid and angular FOV.
    Motion/occlusion/illumination are supplied by this callback. The retained OI
    is a labeled representative midpoint snapshot; RAW is time-integrated.
    """

    if not callable(scene_at):
        raise TypeError("scene_at must be callable with simulation time in seconds.")
    resolved = TemporalCaptureConfig() if timing is None else timing
    if not isinstance(resolved, TemporalCaptureConfig):
        raise TypeError("timing must be a TemporalCaptureConfig.")
    working = camera.clone()
    initial = scene_at(float(resolved.frame_start_s))
    initial_photons = _validate_photons(initial, Scene)
    initial = initial.clone()
    sensor = configure_e2e_sensor(working.fields["sensor"])
    oi = working.fields["oi"]
    if sensor_resize:
        hfov, vfov = float(initial.fields["fov_deg"]), float(initial.fields["vfov_deg"])
        shfov = float(sensor_get(sensor, "fov horizontal", initial, oi))
        svfov = float(sensor_get(sensor, "fov vertical", initial, oi))
        if (
            abs((hfov - shfov) / max(shfov, 1e-12)) > 0.01
            or abs((vfov - svfov) / max(svfov, 1e-12)) > 0.01
        ):
            sensor = sensor_set_size_to_fov(sensor, (hfov, vfov), oi)

    def optical_at(time_s: float) -> OpticalImage:
        scene = scene_at(time_s)
        photons = _validate_photons(scene, Scene)
        if (
            photons.shape != initial_photons.shape
            or not np.array_equal(scene.fields["wave"], initial.fields["wave"])
            or any(
                scene.fields.get(key) != initial.fields.get(key) for key in ("fov_deg", "vfov_deg")
            )
        ):
            raise ValueError("Temporal Scene shape, wavelength grid and FOV must stay fixed.")
        return oi_compute(oi.clone(), scene.clone())

    captured = sensor_compute_temporal(sensor, optical_at, timing=resolved, seed=seed)
    metadata = copy.deepcopy(captured.metadata["temporal_capture"])
    starts = metadata["row_exposure_start_s"]
    reference_time = (starts[0] + starts[-1]) / 2.0 + metadata["exposure_time_s"] / 2.0
    representative_oi = optical_at(reference_time)
    metadata["reference_time_s"] = reference_time
    metadata["optical_image_role"] = "representative_midpoint_snapshot_not_integrated_RAW"
    captured.metadata["temporal_capture"] = copy.deepcopy(metadata)
    representative_oi.metadata["temporal_capture"] = copy.deepcopy(metadata)
    ip = ip_compute(working.fields["ip"], captured, asset_store=asset_store)
    ip.metadata["temporal_capture"] = copy.deepcopy(metadata)
    working.fields.update(sensor=captured, oi=representative_oi, ip=ip)
    working.data["result"] = ip.data.get("result")
    working.metadata["temporal_capture"] = metadata
    return working
