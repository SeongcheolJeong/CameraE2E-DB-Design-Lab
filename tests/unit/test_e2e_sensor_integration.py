"""Verify physical RAW survives the public CameraE2E and HW control boundaries."""

import numpy as np
import pytest

from pyisetcam import (
    camera_create,
    camera_get,
    camerae2e_run_scenario,
    hw_isp_config,
    hw_isp_simulate_sequence,
    image_sensor_db_config,
    scene_create,
    sensor_set,
)


def _run(*, readout=None, gain=1.0, mode="digital_raw"):
    return camerae2e_run_scenario({
        "scene": {"type": "uniform ee", "args": [8]},
        "sensor": {"noise_flag": 0, "integration_time": 0.001, "analog_gain": gain},
        "readout": {"black_level_dn": 0, **(readout or {})} if mode != "legacy" else {},
    }, sensor_output_mode=mode, seed=12)


def test_public_faca_exposes_digital_raw_and_original_charge():
    first = _run(gain=1)
    second = _run(gain=2)
    a, b = [camera_get(r["camera"], "sensor") for r in (first, second)]
    np.testing.assert_array_equal(a.data["mean_electrons"], b.data["mean_electrons"])
    np.testing.assert_allclose(b.data["reference_dn"], 2 * a.data["reference_dn"])
    assert b.data["raw_dn"].mean() > a.data["raw_dn"].mean()
    assert first["stages"]["sensor_raw"]["unit"] == "DN"
    assert first["stages"]["sensor_mean_electrons"]["unit"] == "e-/pixel"
    assert first["sensor_capture"]


def test_public_faca_adc_changes_actual_isp_image():
    coarse = _run(readout={"bit_depth": 2})
    fine = _run(readout={"bit_depth": 12})
    coarse_ip = camera_get(coarse["camera"], "ip")
    fine_ip = camera_get(fine["camera"], "ip")
    assert not np.array_equal(coarse_ip.data["result"], fine_ip.data["result"])
    assert np.count_nonzero(coarse_ip.data["input"]) == 0


def test_public_faca_legacy_is_explicit():
    result = _run(mode="legacy")
    sensor = camera_get(result["camera"], "sensor")
    assert "raw_dn" not in sensor.data
    assert result["stages"]["sensor_raw"]["unit"] == "V"


def test_db_model_changes_signal_and_explicit_readout_wins():
    sensors = []
    for code in ("DEF-1807-801", "DEF-2011-804"):
        config = image_sensor_db_config(code)
        result = camerae2e_run_scenario({
            **config["scenario"],
            "scene": {"type": "uniform ee", "args": [8]},
            "sensor": {"noise_flag": 0, "integration_time": 0.001},
            "readout": {"full_well_e": 20000, "read_noise_e": 3},
        })
        sensor = camera_get(result["camera"], "sensor")
        sensors.append(sensor)
        assert sensor.fields["sensor_readout"]["full_well_e"] == 20000
        assert sensor.fields["sensor_readout"]["read_noise_e"] == 3
        assert result["sensor_estimate"]
    assert not np.allclose(sensors[0].data["mean_electrons"], sensors[1].data["mean_electrons"])


def test_hw_ae_gain_increases_codes_when_exposure_is_capped():
    camera = camera_create()
    camera.fields["sensor"] = sensor_set(camera.fields["sensor"], "noise flag", 0)
    config = hw_isp_config(
        fps=30, exposure_time_us=100,
        control_path={
            "apply_to_image": True, "awb_enabled": False,
            "ae_apply_delay_frames": 1, "max_exposure_fraction": 0.003,
            "min_exposure_time_us": 100, "min_analog_gain": 1, "max_analog_gain": 16,
        },
    )
    sequence = hw_isp_simulate_sequence(
        camera, scene_create("uniform ee", 8), config, nframes=4,
    )
    gains = [f.sensor.fields["sensor_readout"]["gain"] for f in sequence.frames]
    codes = [f.sensor.data["raw_dn"].mean() for f in sequence.frames]
    assert gains[-1] > gains[0]
    assert codes[-1] > codes[0]
    for frame in sequence.frames:
        applied = frame.controls_applied["applied_controls"]
        assert applied["analog_gain"] == frame.sensor.fields["sensor_readout"]["gain"]


def test_unknown_output_mode_is_rejected():
    with pytest.raises(ValueError, match="sensor_output_mode"):
        camerae2e_run_scenario(sensor_output_mode="guess")


def test_camera_parameter_aliases_use_physical_readout_and_explicit_precedence():
    result = camerae2e_run_scenario({
        "scene": {"type": "uniform ee", "args": [8]},
        "parameters": {"sensor.nbits": 8, "sensor.black level": 20, "sensor.analog gain": 2},
        "sensor": {"noise_flag": 0, "integration_time": 0.001},
    }, include_arrays=False)
    readout = camera_get(result["camera"], "sensor").fields["sensor_readout"]
    assert readout["bit_depth"] == 8
    assert readout["black_level_dn"] == 20
    assert readout["gain"] == 2
    explicit = camerae2e_run_scenario({
        "scene": {"type": "uniform ee", "args": [8]},
        "parameters": {"sensor.analog gain": 2},
        "sensor": {"analog_gain": 3, "noise_flag": 0},
        "readout": {"gain": 4},
    }, include_arrays=False)
    assert camera_get(explicit["camera"], "sensor").fields["sensor_readout"]["gain"] == 4


def test_model_readout_assumption_survives_scenario_loading():
    scenario = image_sensor_db_config("DEF-2011-804", assumptions={"gain": 2})["scenario"]
    result = camerae2e_run_scenario({
        **scenario, "scene": {"type": "uniform ee", "args": [8]},
        "sensor": {"noise_flag": 0},
    }, include_arrays=False)
    assert camera_get(result["camera"], "sensor").fields["sensor_readout"]["gain"] == 2
