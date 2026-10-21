"""Continuous spectral OI -> real time-integrated RAW -> native HDR diagnostic."""

import numpy as np

from camera_platform.road_hdr_fusion import calibrated_rate, merge
from camera_platform.saved_sensor_hdr import _calibration, _native_fpn_calibration
from pyisetcam import (
    TemporalCaptureConfig,
    configure_e2e_sensor,
    sensor_compute_temporal,
    sensor_create,
    sensor_set,
)
from pyisetcam.types import OpticalImage


def temporal_pair(velocity):
    shape = (32, 96)
    sensor = sensor_create("bayer (rggb)", {"size": shape, "wave": np.array([450.0, 550.0, 650.0])})
    sensor = configure_e2e_sensor(
        sensor, readout={"read_noise_e": 1.0, "dark_current_e_per_s": 0.0}
    )
    sensor = sensor_set(sensor, "noise flag", 2)
    x = np.arange(shape[1])
    y = np.arange(shape[0])[:, None]

    # A continuous narrow object above textured static spectral background.
    def oi_at(t):
        background = (1 + 0.2 * np.sin(x * 0.7)[None, :] + 0.1 * np.cos(y * 0.5)) * 1e14
        object_rate = np.exp(-0.5 * ((x - 35 - velocity * t) / 1.5) ** 2)[None, :] * 1e15
        return OpticalImage(
            fields={
                "wave": np.array([450.0, 550.0, 650.0]),
                "width_m": shape[1] * sensor.fields["pixel"]["size_m"][1],
            },
            data={"photons": np.repeat((background + object_rate)[..., None], 3, axis=-1)},
        )

    outputs = []
    means = []
    for exposure, start, seed in ((0.001, 0.012, 11), (0.008, 0.0, 12)):
        current = sensor_set(sensor, "integration time", exposure)
        captured = sensor_compute_temporal(
            current,
            oi_at,
            seed=seed,
            timing=TemporalCaptureConfig(
                frame_start_s=start, rolling_shutter=False, samples_per_exposure=33
            ),
        )
        raw = captured.data["raw_dn"]
        scalar = _calibration(captured.metadata["sensor_capture"], shape)
        cal, _ = _native_fpn_calibration(captured, scalar)
        outputs.append(
            calibrated_rate(
                raw,
                exposure,
                cal,
                {"kind": "native_sensor", "full_sensor_shape": list(shape), "crop_origin": [0, 0]},
            )
        )
        means.append(captured.data["mean_electrons"] / exposure)
    return outputs, means


def test_continuous_spectral_blur_is_not_recovered_by_registration():
    still, mean_still = temporal_pair(0.0)
    moving, mean_moving = temporal_pair(800.0)
    result = merge(moving[0], moving[1], profile="local", tile_side=16)
    # Long exposure really integrated a moving object, not a resized static RAW.
    assert not np.allclose(mean_moving[0], mean_moving[1])
    assert np.allclose(mean_still[0], mean_still[1], rtol=1e-5)
    assert np.all(~result["valid"] | moving[0]["valid"])
    assert result["summary"]["motion"]["ground_truth_used"] is False
