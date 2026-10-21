"""Physical temporal capture through HW timing, transport and AE controls."""

import numpy as np
import pytest

from pyisetcam import (
    camera_create,
    configure_e2e_sensor,
    hw_isp_config,
    hw_isp_simulate_frame,
    hw_isp_simulate_sequence,
    scene_create,
    sensor_get,
    sensor_set,
)


@pytest.fixture
def small_capture(asset_store):
    camera = camera_create(asset_store=asset_store)
    sensor = sensor_set(camera.fields["sensor"], "size", [8, 8])
    camera.fields["sensor"] = sensor_set(sensor, "noise flag", 0)
    return camera, scene_create("uniform ee", 8, asset_store=asset_store)


def _ramp(base, slope=10.0):
    def scene_at(seconds):
        scene = base.clone()
        scene.data["photons"] = np.asarray(base.data["photons"]) * (1.0 + slope * seconds)
        return scene

    return scene_at


def _config(**kwargs):
    return hw_isp_config(
        exposure_time_us=4000,
        line_time_us=1000,
        hidden_lines_top=2,
        temporal_samples_per_exposure=4,
        control_path={"ae_enabled": False, "awb_enabled": False},
        **kwargs,
    )


def test_callable_ramp_integrates_each_rolling_row_and_global_shutter(small_capture, asset_store):
    camera, base = small_capture
    common = {"asset_store": asset_store, "sensor_resize": False}
    constant = hw_isp_simulate_frame(camera, lambda _: base, _config(), **common)
    rolling = hw_isp_simulate_frame(camera, _ramp(base), _config(rolling_shutter=True), **common)
    global_frame = hw_isp_simulate_frame(
        camera, _ramp(base), _config(rolling_shutter=False), **common
    )
    reference = constant.sensor.data["mean_electrons"]

    for frame in (rolling, global_frame):
        timeline = frame.timeline
        row_mid_s = np.array(
            [
                (timeline.row_exposure_start_us(row) + timeline.row_exposure_end_us(row)) * 0.5e-6
                for row in range(8)
            ]
        )
        expected = reference * (1.0 + 10.0 * row_mid_s[:, None])
        # The optical pipeline stores intermediate photon responses at float32 precision.
        np.testing.assert_allclose(frame.sensor.data["mean_electrons"], expected, rtol=5e-7)
        assert (
            frame.camera.metadata["hw_isp"]["timeline"]["rolling_shutter"]
            == timeline.rolling_shutter
        )
        capture = frame.sensor.metadata["temporal_capture"]
        for key, method in (
            ("row_exposure_start_s", timeline.row_exposure_start_us),
            ("row_exposure_end_s", timeline.row_exposure_end_us),
        ):
            np.testing.assert_allclose(capture[key], [method(row) * 1e-6 for row in range(8)])

    assert rolling.timeline.row_exposure_start_us(0) == 2000
    assert rolling.timeline.row_exposure_start_us(7) == 9000
    assert (
        global_frame.timeline.row_exposure_start_us(0)
        == global_frame.timeline.row_exposure_start_us(7)
        == 0
    )
    assert global_frame.timeline.row_start_us(7) - global_frame.timeline.row_start_us(0) == 7000
    assert not np.array_equal(rolling.sensor.data["raw_dn"], global_frame.sensor.data["raw_dn"])


def test_callable_uses_queue_stalled_absolute_exposure_time(small_capture, asset_store):
    camera, base = small_capture
    config = _config(
        fps=10,
        jitter_us_std=123,
        transport={"request_queue_depth": 1, "max_buffers": 1},
    )
    previous = {"app_visible_times_us": [200000.0]}
    common = {
        "asset_store": asset_store,
        "sensor_resize": False,
        "frame_id": 1,
        "previous_state": previous,
    }
    reference = hw_isp_simulate_frame(camera, lambda _: base, config, **common)
    result = hw_isp_simulate_frame(camera, _ramp(base), config, **common)
    timeline = result.timeline
    assert timeline.timestamps_us["exposure_start"] == 200000.0
    assert timeline.queue_stall_us == 200000.0 - timeline.timestamps_us["requested"]
    assert timeline.timestamps_us["requested"] != 100000.0
    midpoint = np.array([(timeline.row_exposure_start_us(row) + 2000) * 1e-6 for row in range(8)])
    np.testing.assert_allclose(
        result.sensor.data["mean_electrons"],
        reference.sensor.data["mean_electrons"] * (1 + 10 * midpoint[:, None]),
        rtol=5e-7,
    )


def test_manual_temporal_exposure_matches_pixels_controls_and_timeline(small_capture, asset_store):
    camera, base = small_capture
    config = hw_isp_config(
        exposure_time_us=50,
        rolling_shutter=False,
        temporal_samples_per_exposure=4,
        control_path={"apply_to_image": False},
    )
    frames = hw_isp_simulate_sequence(
        camera,
        lambda _: base,
        config,
        exposure_times=[50, 200],
        nframes=2,
        asset_store=asset_store,
        sensor_resize=False,
    ).frames
    for frame, exposure in zip(frames, [50, 200], strict=True):
        assert frame.controls_applied["exposure_time_us"] == exposure
        assert frame.timeline.timestamps_us["exposure_time_us"] == exposure
        assert float(sensor_get(frame.sensor, "exposure duration")) == pytest.approx(
            exposure * 1e-6
        )
        assert (
            frame.timeline.row_exposure_end_us(7) - frame.timeline.row_exposure_start_us(7)
            == exposure
        )
    np.testing.assert_allclose(
        frames[1].sensor.data["mean_electrons"], 4 * frames[0].sensor.data["mean_electrons"]
    )


def test_temporal_ae_delay_changes_integrated_exposure(small_capture, asset_store):
    camera, base = small_capture
    config = hw_isp_config(
        exposure_time_us=1000,
        rolling_shutter=False,
        temporal_samples_per_exposure=4,
        control_path={"apply_to_image": True, "ae_apply_delay_frames": 2, "awb_enabled": False},
    )
    frames = hw_isp_simulate_sequence(
        camera,
        lambda _: base,
        config,
        nframes=4,
        asset_store=asset_store,
        sensor_resize=False,
    ).frames
    requested = frames[0].controls_applied["requested_controls"]["ae"]
    assert frames[2].controls_applied["ae_stats_frame"] == 0
    assert frames[2].controls_applied["exposure_time_us"] == requested["exposure_time_us"]
    assert requested["exposure_time_us"] != frames[0].controls_applied["exposure_time_us"]
    for frame in frames:
        exposure = frame.controls_applied["exposure_time_us"]
        assert frame.timeline.timestamps_us["exposure_time_us"] == exposure
        assert float(sensor_get(frame.sensor, "exposure duration")) == pytest.approx(
            exposure * 1e-6
        )
    ratio = (
        frames[2].controls_applied["exposure_time_us"]
        / frames[0].controls_applied["exposure_time_us"]
    )
    np.testing.assert_allclose(
        frames[2].sensor.data["mean_electrons"], ratio * frames[0].sensor.data["mean_electrons"]
    )


def test_temporal_manual_exposure_and_gain_survive_image_applied_awb(small_capture, asset_store):
    camera, base = small_capture
    camera.fields["sensor"] = configure_e2e_sensor(camera.fields["sensor"], readout={"gain": 4})
    config = hw_isp_config(
        exposure_time_us=50,
        rolling_shutter=False,
        temporal_samples_per_exposure=4,
        control_path={"apply_to_image": True, "ae_enabled": False, "awb_apply_delay_frames": 2},
    )
    frames = hw_isp_simulate_sequence(
        camera,
        lambda _: base,
        config,
        exposure_times=[50, 200, 50],
        nframes=3,
        asset_store=asset_store,
        sensor_resize=False,
    ).frames
    for frame, exposure in zip(frames, [50, 200, 50], strict=True):
        assert frame.controls_applied["exposure_time_us"] == exposure
        assert frame.controls_applied["analog_gain"] == 4
        assert frame.sensor.fields["sensor_readout"]["gain"] == 4
        assert float(sensor_get(frame.sensor, "exposure duration")) == pytest.approx(
            exposure * 1e-6
        )
        assert frame.timeline.timestamps_us["exposure_time_us"] == exposure
        capture = frame.sensor.metadata["temporal_capture"]
        assert capture["exposure_time_s"] == pytest.approx(exposure * 1e-6)
        np.testing.assert_allclose(
            np.subtract(capture["row_exposure_end_s"], capture["row_exposure_start_s"]),
            exposure * 1e-6,
        )
    np.testing.assert_allclose(
        frames[1].sensor.data["mean_electrons"], 4 * frames[0].sensor.data["mean_electrons"]
    )
    np.testing.assert_allclose(
        frames[2].sensor.data["mean_electrons"], frames[0].sensor.data["mean_electrons"]
    )
    requested_awb = frames[0].controls_applied["requested_controls"]["awb"]["wb_gains_rgb"]
    assert not np.allclose(requested_awb, [1, 1, 1])
    assert frames[2].controls_applied["awb_stats_frame"] == 0
    np.testing.assert_allclose(frames[2].controls_applied["wb_gains_rgb"], requested_awb)


def test_temporal_sequence_noise_is_repeatable_but_varies_by_frame(small_capture, asset_store):
    camera, base = small_capture
    camera.fields["sensor"] = sensor_set(camera.fields["sensor"], "noise flag", 2)
    config = _config(rolling_shutter=False, seed=123)
    kwargs = {"nframes": 3, "asset_store": asset_store, "sensor_resize": False}
    a = hw_isp_simulate_sequence(camera, lambda _: base, config, **kwargs)
    b = hw_isp_simulate_sequence(camera, lambda _: base, config, **kwargs)
    for first, second in zip(a.frames, b.frames, strict=True):
        np.testing.assert_array_equal(first.sensor.data["raw_dn"], second.sensor.data["raw_dn"])
    assert not np.array_equal(a.frames[0].sensor.data["raw_dn"], a.frames[1].sensor.data["raw_dn"])


def test_hw_temporal_sample_cap_is_enforced(small_capture, asset_store):
    camera, base = small_capture
    with pytest.raises(ValueError, match="max_scene_samples"):
        hw_isp_simulate_frame(
            camera,
            lambda _: base,
            _config(temporal_max_scene_samples=2),
            asset_store=asset_store,
            sensor_resize=False,
        )


def test_callable_legacy_mode_is_rejected(small_capture, asset_store):
    camera, base = small_capture
    with pytest.raises(ValueError, match="sensor_output_mode='digital_raw'"):
        hw_isp_simulate_frame(
            camera,
            lambda _: base,
            _config(),
            sensor_output_mode="legacy",
            asset_store=asset_store,
            sensor_resize=False,
        )


@pytest.mark.parametrize("name", ["temporal_samples_per_exposure", "temporal_max_scene_samples"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True, float("inf")])
def test_temporal_sampling_limits_require_positive_integers(name, value):
    with pytest.raises(ValueError, match=name):
        hw_isp_config(**{name: value})


def test_temporal_sample_cap_requires_two_samples():
    with pytest.raises(ValueError, match="temporal_max_scene_samples"):
        hw_isp_config(temporal_max_scene_samples=1)
