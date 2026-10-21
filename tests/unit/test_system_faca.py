from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pyisetcam import (
    camera_create,
    camera_get,
    camerae2e_faca_report,
    camerae2e_run_scenario,
    camerae2e_run_sweep,
    sensor_get,
)
from pyisetcam.fdtd_sensor import FDTDSensorLUT


def test_camerae2e_run_scenario_returns_stage_metrics_and_report() -> None:
    result = camerae2e_run_scenario(
        {
            "name": "unit_faca",
            "scene": {"type": "uniform ee", "args": [8]},
            "sensor": {"noise_flag": 0},
        },
        seed=11,
        include_arrays=False,
    )
    report = camerae2e_faca_report(result)

    assert result["schema_version"] == "camerae2e_faca_scenario_v1"
    assert report["name"] == "unit_faca"
    assert report["stage_summaries"]["sensor_raw"]["available"] is True
    assert report["metrics"]["color"]["rgb_mean"] is not None
    assert report["parameter_lineage"][0]["path"] == "sensor.noise_flag"
    assert "readiness_tiers" in report["artifact_lineage"]["summary"]


def test_camerae2e_run_sweep_records_axis_values() -> None:
    sweep = camerae2e_run_sweep(
        {
            "name": "unit_sweep",
            "scene": {"type": "uniform ee", "args": [8]},
            "sensor": {"noise_flag": 0},
        },
        {"sensor.integration_time": [0.002, 0.004]},
        seed=20,
        include_arrays=False,
    )

    assert sweep["schema_version"] == "camerae2e_faca_sweep_v1"
    assert sweep["case_count"] == 2
    assert sweep["cases"][0]["axis_values"]["sensor.integration_time"] == 0.002
    assert sweep["cases"][1]["seed"] == 21


def test_camerae2e_run_scenario_can_include_hw_isp_control_summary() -> None:
    result = camerae2e_run_scenario(
        {
            "name": "unit_hw_faca",
            "scene": {"type": "uniform ee", "args": [8]},
            "sensor": {"noise_flag": 0},
            "hw_isp": {"enabled": True, "nframes": 2},
        },
        seed=3,
        include_arrays=False,
    )
    report = camerae2e_faca_report(result)

    assert report["metrics"]["control"]["frame_count"] == 2
    assert report["stage_summaries"]["ip_result"]["available"] is True


def test_camerae2e_run_sweep_enables_hw_isp_delay_axes() -> None:
    sweep = camerae2e_run_sweep(
        {
            "name": "unit_hw_delay_sweep",
            "scene": {"type": "uniform ee", "args": [8]},
            "sensor": {"noise_flag": 0},
        },
        {"hw_isp.ae_apply_delay_frames": [0, 2]},
        seed=7,
        include_arrays=False,
    )

    assert sweep["case_count"] == 2
    assert sweep["cases"][0]["metrics"]["control"]["frame_count"] == 3
    assert sweep["cases"][0]["scenario"]["hw_isp"]["enabled"] is True
    assert sweep["cases"][1]["axis_values"]["hw_isp.ae_apply_delay_frames"] == 2


def _raw(result):
    return np.asarray(sensor_get(camera_get(result["camera"], "sensor"), "volts"))


@pytest.mark.parametrize("use_hw_isp", [False, True])
def test_scenario_seed_replays_and_changes_temporal_noise(use_hw_isp) -> None:
    scenario = {
        "scene": {"type": "uniform ee", "args": [8]},
        "sensor": {"noise_flag": 2, "integration_time": 0.001},
        "hw_isp": {"enabled": use_hw_isp, "nframes": 2, "seed": 999},
    }
    first = camerae2e_run_scenario(scenario, seed=11, include_arrays=False)
    replay = camerae2e_run_scenario(scenario, seed=11, include_arrays=False)
    other = camerae2e_run_scenario(scenario, seed=12, include_arrays=False)
    np.testing.assert_array_equal(_raw(first), _raw(replay))
    assert not np.array_equal(_raw(first), _raw(other))
    if use_hw_isp:
        frames = first["hw_isp_sequence"].frames
        assert not np.array_equal(frames[0].sensor.data["volts"], frames[1].sensor.data["volts"])


def test_disabled_fdtd_matches_unattached_not_enabled_response() -> None:
    lut = FDTDSensorLUT(
        source_path=Path("synthetic.json"), schema="test", mode="test",
        wavelengths_nm=np.array([550.0]),
        summary_rows=[
            {"case": "center", "wavelength_nm": 550.0, "total_response": 1.0},
            {"case": "edge", "wavelength_nm": 550.0, "cra_x_deg": 20.0,
             "total_response": 0.5},
        ],
    )
    base = {
        "scene": {"type": "uniform ee", "args": [8]},
        "sensor": {"noise_flag": 0, "integration_time": 0.001},
    }
    physics = {"lut": lut, "mode": "qe", "case": "edge"}
    plain = camerae2e_run_scenario(base, include_arrays=False)
    disabled = camerae2e_run_scenario(
        {**base, "fdtd": {**physics, "enabled": False}}, include_arrays=False,
    )
    enabled = camerae2e_run_scenario(
        {**base, "fdtd": {**physics, "enabled": True}}, include_arrays=False,
    )
    np.testing.assert_array_equal(_raw(plain), _raw(disabled))
    # Optical attenuation affects generated photocharge, not the dark offset
    # included in the new readout's mean output voltage.
    plain_charge = camera_get(plain["camera"], "sensor").data["mean_electrons"]
    enabled_charge = camera_get(enabled["camera"], "sensor").data["mean_electrons"]
    np.testing.assert_allclose(enabled_charge, plain_charge * 0.5, rtol=1e-6)
    assert disabled["parameter_lineage"][-1]["status"] == "disabled"


@pytest.mark.parametrize("layer,key", [("fdtd", "lut"), ("tcad", "db")])
def test_disabled_physics_skips_missing_files_and_disables_existing_camera(layer, key) -> None:
    camera = camera_create()
    camera.fields["sensor"].fields[f"{layer}_sensor"] = {
        "enabled": True, key: "/missing/physics-artifact.json",
    }
    result = camerae2e_run_scenario(
        {"scene": {"type": "uniform ee", "args": [8]}, "sensor": {"noise_flag": 0},
         layer: {"enabled": False, key: "/missing/physics-artifact.json"}},
        camera=camera, include_arrays=False,
    )
    attached = sensor_get(camera_get(result["camera"], "sensor"), f"{layer} sensor")
    assert attached["enabled"] is False
    assert np.all(np.isfinite(_raw(result)))
    assert camera.fields["sensor"].fields[f"{layer}_sensor"]["enabled"] is True
