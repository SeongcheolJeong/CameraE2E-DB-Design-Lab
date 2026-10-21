"""Independent temporal image-formation invariants and public API contracts."""

import numpy as np
import pytest

from pyisetcam import (
    TemporalCaptureConfig,
    camera_compute,
    camera_compute_sequence,
    camera_compute_temporal,
    camera_create,
    configure_e2e_sensor,
    scene_create,
    scene_set,
    sensor_compute,
    sensor_compute_temporal,
    sensor_create,
    sensor_set,
)
from pyisetcam.types import OpticalImage


@pytest.fixture
def optical_input():
    sensor = sensor_create("monochrome", {"size": (12, 96), "wave": np.array([550.0])})
    sensor = sensor_set(sensor, "integration time", 0.02)
    sensor = sensor_set(sensor, "noise flag", 0)
    sensor = configure_e2e_sensor(sensor, readout={"dark_current_e_per_s": 0})
    oi = OpticalImage(
        fields={
            "wave": np.array([550.0]),
            "width_m": 96 * sensor.fields["pixel"]["size_m"][1],
        },
        data={"photons": np.full((12, 96, 1), 1e15)},
    )
    return sensor, oi


@pytest.mark.parametrize("rolling", [False, True])
@pytest.mark.parametrize("exposure", [0.003, 0.02])
def test_constant_scene_matches_static_electrons_and_raw(optical_input, rolling, exposure):
    sensor, oi = optical_input
    sensor = sensor_set(sensor, "integration time", exposure)
    expected = sensor_compute(sensor, oi, seed=7)
    actual = sensor_compute_temporal(
        sensor,
        lambda t: oi,
        seed=7,
        timing=TemporalCaptureConfig(
            frame_start_s=1.0,
            line_time_s=0.0007,
            hidden_lines_top=3,
            rolling_shutter=rolling,
            samples_per_exposure=7,
        ),
    )
    np.testing.assert_allclose(
        actual.data["mean_electrons"], expected.data["mean_electrons"], rtol=1e-13
    )
    np.testing.assert_array_equal(actual.data["raw_dn"], expected.data["raw_dn"])
    assert "temporal_capture" not in sensor.metadata


@pytest.mark.parametrize("rolling", [False, True])
def test_linear_light_ramp_matches_analytic_row_integrals(optical_input, rolling):
    sensor, oi = optical_input
    base = sensor_compute(sensor, oi).data["mean_electrons"]
    timing = TemporalCaptureConfig(
        frame_start_s=0.13,
        line_time_s=0.0013,
        hidden_lines_top=2,
        rolling_shutter=rolling,
        samples_per_exposure=3,
    )

    def ramp(t):
        current = oi.clone()
        current.data["photons"] *= 1.0 + 5.0 * t
        return current

    actual = sensor_compute_temporal(sensor, ramp, timing=timing)
    starts = np.full(12, 0.13)
    if rolling:
        starts += (2 + np.arange(12)) * 0.0013
    # Integral of a + b*t over [start, start+E] is E*(a+b*(start+E/2)).
    expected = base * (1 + 5 * (starts + 0.02 / 2))[:, None]
    # The inherited spectral-current calculation uses float32 before spatial
    # integration; allow its rounding while checking the analytic time integral.
    np.testing.assert_allclose(actual.data["mean_electrons"], expected, rtol=1e-7)
    np.testing.assert_allclose(actual.metadata["temporal_capture"]["row_exposure_start_s"], starts)


def _moments(image):
    x = np.arange(image.shape[1])
    mass = np.sum(image, axis=1)
    centroid = np.sum(image * x, axis=1) / mass
    variance = np.sum(image * (x[None, :] - centroid[:, None]) ** 2, axis=1) / mass
    return centroid, variance, mass


def test_moving_gaussian_has_physical_skew_blur_and_energy(optical_input):
    sensor, oi = optical_input
    velocity, sigma, origin = 200.0, 2.0, 30.0

    def moving(t):
        current = oi.clone()
        x = np.arange(96)
        current.data["photons"] *= np.exp(-0.5 * ((x - origin - velocity * t) / sigma) ** 2)[
            None, :, None
        ]
        return current

    moments = {}
    for exposure in (0.005, 0.02):
        for rolling in (False, True):
            actual = sensor_compute_temporal(
                sensor_set(sensor, "integration time", exposure),
                moving,
                timing=TemporalCaptureConfig(
                    line_time_s=0.001,
                    rolling_shutter=rolling,
                    samples_per_exposure=64,
                ),
            )
            centroid, variance, mass = _moments(actual.data["mean_electrons"])
            starts = np.arange(12) * 0.001 if rolling else np.zeros(12)
            np.testing.assert_allclose(
                centroid, origin + velocity * (starts + exposure / 2), atol=1e-8
            )
            np.testing.assert_allclose(
                variance, sigma**2 + (velocity * exposure) ** 2 / 12, atol=0.001
            )
            moments[exposure, rolling] = centroid, variance, mass
    assert moments[0.02, True][0][-1] - moments[0.02, True][0][0] == pytest.approx(2.2)
    assert np.ptp(moments[0.02, False][0]) < 1e-10
    assert np.mean(moments[0.02, False][1]) > np.mean(moments[0.005, False][1])
    np.testing.assert_allclose(moments[0.02, True][2], 4 * moments[0.005, True][2], rtol=1e-7)


def test_sample_count_does_not_add_read_noise_or_change_fpn(optical_input):
    sensor, oi = optical_input
    sensor = sensor_set(sensor, "noise flag", 2)
    sensor = sensor_set(sensor, "prnu level", 1.0)
    sensor = configure_e2e_sensor(sensor, readout={"read_noise_e": 7, "dark_current_e_per_s": 10})
    static = sensor_compute(sensor, oi, seed=123)
    frames = [
        sensor_compute_temporal(
            sensor,
            lambda t: oi,
            seed=123,
            timing=TemporalCaptureConfig(
                samples_per_exposure=n,
                rolling_shutter=False,
            ),
        )
        for n in (2, 64)
    ]
    for frame in frames:
        np.testing.assert_array_equal(frame.data["raw_dn"], static.data["raw_dn"])
        np.testing.assert_array_equal(
            frame.fields["gain_fpn_image"], static.fields["gain_fpn_image"]
        )
        assert frame.metadata["temporal_capture"]["readouts_per_capture"] == 1
    changed_seed = sensor_compute_temporal(sensor, lambda t: oi, seed=124)
    assert not np.array_equal(changed_seed.data["raw_dn"], frames[0].data["raw_dn"])
    np.testing.assert_array_equal(
        changed_seed.fields["gain_fpn_image"], frames[0].fields["gain_fpn_image"]
    )


def test_flicker_banding_converges_to_analytic_exposure_average(optical_input):
    sensor, oi = optical_input
    sensor = sensor_set(sensor, "integration time", 0.003)
    base = sensor_compute(sensor, oi).data["mean_electrons"]
    frequency, amplitude = 100.0, 0.8

    def flicker(t):
        current = oi.clone()
        current.data["photons"] *= 1 + amplitude * np.cos(2 * np.pi * frequency * t)
        return current

    starts = 0.002 + np.arange(12) * 0.0015
    expected = (
        base
        * (
            1
            + amplitude
            * np.sinc(frequency * 0.003)
            * np.cos(2 * np.pi * frequency * (starts + 0.003 / 2))
        )[:, None]
    )
    errors = []
    for samples in (4, 64):
        actual = sensor_compute_temporal(
            sensor,
            flicker,
            timing=TemporalCaptureConfig(
                frame_start_s=0.002,
                line_time_s=0.0015,
                samples_per_exposure=samples,
            ),
        )
        errors.append(float(np.max(np.abs(actual.data["mean_electrons"] - expected))))
    assert errors[1] < errors[0] / 100
    np.testing.assert_allclose(actual.data["mean_electrons"], expected, rtol=0.0002)
    assert np.ptp(actual.data["mean_electrons"].mean(axis=1)) > np.mean(base)


def test_full_well_is_applied_after_time_integration(optical_input):
    sensor, oi = optical_input
    base = sensor_compute(sensor, oi).data["mean_electrons"][0, 0]
    oi.data["photons"] *= 1000 / base
    sensor = configure_e2e_sensor(sensor, readout={"full_well_e": 600, "black_level_dn": 0})

    def ramp(t):
        current = oi.clone()
        current.data["photons"] *= t / 0.02
        return current

    captured = sensor_compute_temporal(
        sensor, ramp, timing=TemporalCaptureConfig(rolling_shutter=False)
    )
    # Endpoint would saturate a 20ms independent capture; its integrated ramp is
    # only 500e and must survive. Clipping each sub-image gives a different value.
    np.testing.assert_allclose(captured.data["mean_electrons"], 500, rtol=1e-7)
    np.testing.assert_allclose(captured.data["raw_dn"], round(4095 * 500 / 600), atol=1)


def test_zero_exposure_is_one_dark_readout_without_optical_samples(optical_input):
    sensor, _ = optical_input
    sensor = sensor_set(sensor, "integration time", 0)
    sensor = sensor_set(sensor, "noise flag", 2)

    def forbidden(t):
        raise AssertionError("Zero exposure has no incident charge to sample.")

    result = sensor_compute_temporal(sensor, forbidden, seed=5)
    assert not np.any(result.data["mean_electrons"])
    assert np.std(result.data["raw_dn"]) > 0
    assert result.metadata["temporal_capture"]["sample_count"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"line_time_s": -1},
        {"line_time_s": np.nan},
        {"frame_start_s": np.inf},
        {"samples_per_exposure": 0},
        {"samples_per_exposure": 1.5},
        {"max_scene_samples": 1},
        {"hidden_lines_top": -1},
        {"rolling_shutter": "false"},
    ],
)
def test_invalid_timing_rejected(kwargs):
    with pytest.raises(ValueError):
        TemporalCaptureConfig(**kwargs)


def test_sample_cap_is_explicit_before_sampling(optical_input):
    sensor, _ = optical_input

    def forbidden(t):
        raise AssertionError("Sample limit must be checked before optical work.")

    with pytest.raises(ValueError, match="max_scene_samples"):
        sensor_compute_temporal(
            sensor,
            forbidden,
            timing=TemporalCaptureConfig(
                line_time_s=0.1,
                max_scene_samples=32,
            ),
        )


def test_input_contract_and_auto_exposure_fail_loudly(optical_input):
    sensor, oi = optical_input
    with pytest.raises(TypeError, match="OpticalImage"):
        sensor_compute_temporal(sensor, lambda t: np.zeros((12, 96)))
    with pytest.raises(ValueError, match="resolve AE"):
        sensor_compute_temporal(sensor_set(sensor.clone(), "auto exposure", True), lambda t: oi)
    oi.data["photons"][0, 0, 0] = -1
    with pytest.raises(ValueError, match="nonnegative"):
        sensor_compute_temporal(sensor, lambda t: oi)


@pytest.mark.parametrize("dimension", ["width_m", "height_m"])
def test_optical_geometry_changes_rejected(optical_input, dimension):
    sensor, oi = optical_input

    def resizing(t):
        current = oi.clone()
        if t > 0:
            current.fields[dimension] = 2 * current.fields.get(dimension, 1e-5)
        return current

    with pytest.raises(ValueError, match="must stay fixed"):
        sensor_compute_temporal(sensor, resizing)


@pytest.mark.parametrize("dimension", ["width_m", "height_m"])
@pytest.mark.parametrize("size", [0.0, -0.01, np.nan, np.inf])
def test_invalid_optical_physical_size_rejected(optical_input, dimension, size):
    sensor, oi = optical_input
    oi.fields[dimension] = size
    with pytest.raises(ValueError, match="physical size"):
        sensor_compute_temporal(sensor, lambda t: oi)


@pytest.mark.parametrize("start", [1e16, 1e14])
def test_unrepresentable_exposure_or_sample_times_rejected(optical_input, start):
    sensor, _ = optical_input

    def forbidden(t):
        raise AssertionError("Collapsed sampling timestamps must fail before integration.")

    with pytest.raises(ValueError, match="timestamp precision"):
        sensor_compute_temporal(
            sensor, forbidden, timing=TemporalCaptureConfig(frame_start_s=start)
        )


def test_camera_constant_temporal_matches_static_raw_and_isp():
    camera = camera_create()
    camera.fields["sensor"] = configure_e2e_sensor(
        sensor_set(camera.fields["sensor"], "integration time", 0.002)
    )
    scene = scene_set(scene_create("macbeth", 4), "fov", 2.0)
    static = camera_compute(camera.clone(), scene, seed=5)
    temporal = camera_compute_temporal(
        camera,
        lambda t: scene,
        seed=5,
        timing=TemporalCaptureConfig(
            samples_per_exposure=3,
            line_time_s=1e-5,
        ),
    )
    np.testing.assert_allclose(
        temporal.fields["sensor"].data["mean_electrons"],
        static.fields["sensor"].data["mean_electrons"],
        rtol=1e-12,
    )
    np.testing.assert_array_equal(
        temporal.fields["sensor"].data["raw_dn"], static.fields["sensor"].data["raw_dn"]
    )
    np.testing.assert_array_equal(temporal.data["result"], static.data["result"])
    assert (
        temporal.fields["ip"].metadata["temporal_capture"] == temporal.metadata["temporal_capture"]
    )
    assert "temporal_capture" not in camera.metadata


def test_basic_camera_sequence_has_replayable_independent_frame_noise():
    camera = camera_create()
    scene = scene_set(scene_create("macbeth", 4), "fov", 2.0)
    _, images = camera_compute_sequence(
        camera, scenes=scene, exposuretimes=0.002, nframes=3, seed=7
    )
    _, replay = camera_compute_sequence(
        camera, scenes=scene, exposuretimes=0.002, nframes=3, seed=7
    )
    for first, second in zip(images, replay, strict=True):
        np.testing.assert_array_equal(first, second)
    assert not np.array_equal(images[0], images[1])
    assert not np.array_equal(images[1], images[2])
