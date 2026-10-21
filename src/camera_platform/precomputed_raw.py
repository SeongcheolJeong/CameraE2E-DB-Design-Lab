"""Read out an already computed Bayer electron image without asset lookup.

The spatial runtime owns optical integration and CFA routing before this
boundary. The temporary pyisetcam Sensor here is used only for its digital RAW
readout contract; it is never exposed as a spectral Sensor model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

from pyisetcam.sensor import _sensor_base
from pyisetcam.sensor_capture import configure_e2e_sensor, resample_sensor_capture

_CFA_PATTERNS = {
    "rggb": ((1, 2), (2, 3)),
    "grbg": ((2, 1), (3, 2)),
    "gbrg": ((2, 3), (1, 2)),
    "bggr": ((3, 2), (2, 1)),
}


def capture_precomputed_raw(
    mean_electrons: Any,
    *,
    wavelength_nm: Any,
    pixel_pitch_m: float,
    exposure_s: float,
    cfa_phase: str,
    readout: Mapping[str, Any],
    noise_enabled: bool,
    frames: int,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    """Sample digital RAW from source-calculated mean charge without filter data."""

    values = np.asarray(mean_electrons)
    if (values.ndim != 2 or values.dtype.kind not in "uif"
            or not all(values.shape) or not np.isfinite(values).all()
            or np.any(values < 0)):
        raise ValueError("Precomputed charge requires a nonempty finite nonnegative 2-D image.")
    wave = np.asarray(wavelength_nm)
    if (wave.ndim != 1 or wave.dtype.kind not in "uif" or wave.size < 1
            or not np.isfinite(wave).all() or np.any(wave <= 0)
            or np.any(np.diff(wave) <= 0)):
        raise ValueError("Precomputed charge requires increasing positive wavelengths.")
    pitch = float(pixel_pitch_m)
    exposure = float(exposure_s)
    if not np.isfinite(pitch) or pitch <= 0:
        raise ValueError("Precomputed charge pixel pitch must be positive and finite.")
    if not np.isfinite(exposure) or exposure < 0:
        raise ValueError("Precomputed charge exposure must be nonnegative and finite.")
    if cfa_phase not in _CFA_PATTERNS:
        raise ValueError("Precomputed charge CFA phase must be rggb, grbg, gbrg, or bggr.")
    if type(noise_enabled) is not bool:
        raise ValueError("Precomputed charge noise_enabled must be boolean.")
    if type(frames) is not int or not 1 <= frames <= 16:
        raise ValueError("Precomputed charge frames must be an integer in [1, 16].")
    if type(seed) is not int or seed < 0:
        raise ValueError("Precomputed charge seed must be a nonnegative integer.")

    size = tuple(int(axis) for axis in values.shape)
    sensor = _sensor_base(
        "precomputed-charge-" + cfa_phase,
        wave.astype(float, copy=False),
        size,
        {"size_m": np.array([pitch, pitch], dtype=float)},
    )
    sensor.fields.update(
        pattern=np.asarray(_CFA_PATTERNS[cfa_phase], dtype=int),
        integration_time=exposure,
        auto_exposure=False,
        noise_flag=2 if noise_enabled else 0,
        column_fpn=[0.0, 0.0],
    )
    sensor.fields["pixel"].update(prnu_sigma=0.0, dsnu_sigma_v=0.0)
    sensor = configure_e2e_sensor(sensor, readout=readout)
    sensor.data["mean_electrons"] = values.astype(float, copy=True)
    captures = [resample_sensor_capture(sensor, seed=seed + index)
                for index in range(frames)]
    return (
        np.stack([capture.data["raw_dn"] for capture in captures]),
        captures[0].data["reference_dn"].copy(),
        [capture.metadata["sensor_capture"] for capture in captures],
    )
