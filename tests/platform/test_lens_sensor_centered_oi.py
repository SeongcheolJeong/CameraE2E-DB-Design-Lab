"""A central optical photon crop must preserve global sensor sampling."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from camera_platform.lens_sensor_centered_oi import (
    centered_oi_crop_for_sensor,
    preflight_centered_oi_crop,
)
from pyisetcam.optics import oi_crop
from pyisetcam.sensor import sensor_compute, sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor
from pyisetcam.types import OpticalImage

WORKSPACE = Path(__file__).resolve().parents[2]


def _pair() -> tuple[OpticalImage, object]:
    waves = np.array([450.0, 550.0, 650.0])
    spacing = 1e-6
    rows = cols = 64
    image_distance = 0.035
    photons = (1e12 + 1e12 * np.random.default_rng(51).random((rows, cols, 3)))
    fov = math.degrees(2 * math.atan(cols * spacing / (2 * image_distance)))
    oi = OpticalImage(
        fields={
            "wave": waves, "width_m": cols * spacing, "height_m": rows * spacing,
            "sample_spacing_m": spacing, "image_distance_m": image_distance,
            "fov_deg": fov, "vfov_deg": fov,
            "optics": {"focal_length_m": 0.033},
        },
        data={"photons": photons},
    )
    sensor = sensor_create("default", {"size": (16, 16), "wave": waves})
    sensor = sensor_set(sensor, "pixel size same fill factor", 2.8e-6)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = configure_e2e_sensor(
        sensor, readout={"dark_current_e_per_s": 0.0, "read_noise_e": 0.0}
    )
    return oi, sensor


def test_minimal_safe_crop_preserves_fixed_16x16_raw_and_electrons():
    oi, sensor = _pair()
    crop, evidence = centered_oi_crop_for_sensor(oi, sensor, (46, 46))
    assert evidence["roi_origin_in_full_oi_row_col"] == [9, 9]
    assert evidence["minimum_centered_shape_row_col"] == [46, 46]
    assert evidence["full_scene_memory_reduced"] is False
    assert evidence["global_optical_calculation_verified"] is False
    assert crop.fields["sample_spacing_m"] == oi.fields["sample_spacing_m"]
    assert crop.fields["width_m"] == 46e-6
    assert crop.fields["height_m"] == 46e-6
    full_capture = sensor_compute(sensor, oi, seed=12)
    crop_capture = sensor_compute(sensor, crop, seed=12)
    np.testing.assert_allclose(
        crop_capture.data["mean_electrons"],
        full_capture.data["mean_electrons"], rtol=1e-12, atol=1e-10,
    )
    np.testing.assert_array_equal(crop_capture.data["raw_dn"], full_capture.data["raw_dn"])


def test_parity_and_source_halo_both_gate_a_centered_crop():
    oi, sensor = _pair()
    with pytest.raises(ValueError, match="odd/even parity"):
        preflight_centered_oi_crop(oi, sensor, (51, 51))
    with pytest.raises(ValueError, match="interpolation/convolution halo"):
        preflight_centered_oi_crop(oi, sensor, (44, 44))
    # The smaller even crop is physically narrower than the modeled 16-pixel
    # sensor active area, independently of the interpolation stencil.
    with pytest.raises(ValueError):
        preflight_centered_oi_crop(oi, sensor, (42, 42))


def test_oi_crop_does_not_preserve_raytrace_effective_focal_spacing():
    oi, sensor = _pair()
    # oi_crop's illuminance update needs a standard visible wavelength axis.
    wave = np.arange(400.0, 701.0, 10.0)
    wide = oi.clone()
    wide.fields["wave"] = wave
    wide.data["photons"] = np.ones((64, 64, wave.size), dtype=float) * 1e12
    legacy = oi_crop(wide, [7, 7, 51, 51])
    assert legacy.data["photons"].shape[:2] == (52, 52)
    assert legacy.fields["sample_spacing_m"] / oi.fields["sample_spacing_m"] == pytest.approx(
        0.035 / 0.033
    )
    safe, _ = centered_oi_crop_for_sensor(oi, sensor, (52, 52))
    assert safe.fields["sample_spacing_m"] == oi.fields["sample_spacing_m"]


def test_metadata_and_fixed_exposure_must_remain_consistent():
    oi, sensor = _pair()
    wrong = oi.clone()
    wrong.fields["width_m"] *= 1.01
    with pytest.raises(ValueError, match="width, height and sample spacing"):
        preflight_centered_oi_crop(wrong, sensor, (46, 46))
    auto = sensor.clone()
    auto.fields["auto_exposure"] = True
    with pytest.raises(ValueError, match="fixed-exposure"):
        preflight_centered_oi_crop(oi, auto, (46, 46))
    prior_crop = oi.clone()
    prior_crop.fields["crop"] = True
    with pytest.raises(ValueError, match="global optical origin"):
        preflight_centered_oi_crop(prior_crop, sensor, (46, 46))


@pytest.mark.parametrize("field, config", [
    ("fdtd_sensor", {"enabled": True, "mode": "qe+field+crosstalk"}),
    ("fdtd_sensor", {"mode": "qe+field+crosstalk"}),
    ("fdtd_sensor", "unknown"),
    ("tcad_sensor", {"enabled": True, "mode": "collection"}),
    ("tcad_sensor", {"mode": "collection"}),
    ("physical_estimate", {"kind": "physical_estimate_v1"}),
    ("fdtd_estimate", {"kind": "fdtd_estimate_v1"}),
    ("ocl_group_proxy", {"mode": "uniform", "shape": [2, 2], "equalization": 1.0}),
    ("ocl_group_proxy", "unknown"),
])
def test_active_or_unknown_spatial_sensor_response_is_rejected(field, config):
    oi, sensor = _pair()
    sensor.fields[field] = config
    with pytest.raises(ValueError, match=field if field != "ocl_group_proxy" else "OCL"):
        preflight_centered_oi_crop(oi, sensor, (46, 46))


def test_explicitly_disabled_spatial_response_is_allowed():
    oi, sensor = _pair()
    sensor.fields["fdtd_sensor"] = {"enabled": False, "mode": "qe+field+crosstalk"}
    sensor.fields["tcad_sensor"] = {"enabled": False, "mode": "collection"}
    sensor.fields["ocl_group_proxy"] = {
        "mode": "off", "shape": [2, 2], "equalization": 1.0
    }
    evidence = preflight_centered_oi_crop(oi, sensor, (46, 46))
    assert evidence["optional_spatial_responses"] == "absent_or_explicitly_disabled"


def test_even_but_off_center_slice_changes_the_modeled_raw():
    oi, sensor = _pair()
    centered, _ = centered_oi_crop_for_sensor(oi, sensor, (46, 46))
    shifted = centered.clone()
    shifted.data["photons"] = np.asarray(oi.data["photons"])[10:56, 9:55, :].copy()
    centered_mean = sensor_compute(sensor, centered, seed=3).data["mean_electrons"]
    shifted_mean = sensor_compute(sensor, shifted, seed=3).data["mean_electrons"]
    assert np.max(np.abs(centered_mean - shifted_mean)) > 0.01


def test_pinned_gc50f6_saved_sensor_matches_on_a_computed_oi_crop():
    if not (WORKSPACE / "camerae2e_db/catalog.sqlite").is_file():
        pytest.skip("Pinned saved sensor DB is unavailable")
    from camera_platform.offline_assets import offline_asset_store
    from pyisetcam.camera import camera_create, camera_get
    from pyisetcam.image_sensor_db import image_sensor_db_reconcile_scenario
    from pyisetcam.sensor_simulation_db import sensor_simulation_db_config
    from pyisetcam.system_faca import (
        _apply_physics_and_sensor_overrides,
        _configure_scenario_readout,
    )

    run_id = "360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601"
    scenario = sensor_simulation_db_config(
        "DEF-2512-801", root=WORKSPACE / "camerae2e_db", run_id=run_id
    )["scenario"]
    scenario = {
        **scenario,
        "sensor": {
            **scenario.get("sensor", {}),
            "size": [16, 16], "integration_time": 0.004, "noise_flag": 0,
        },
    }
    scenario = image_sensor_db_reconcile_scenario(scenario)
    scenario["sensor_output_mode"] = "digital_raw"
    assets = offline_asset_store(workspace=WORKSPACE)
    camera, _ = _apply_physics_and_sensor_overrides(
        camera_create(asset_store=assets), scenario, assets
    )
    sensor = _configure_scenario_readout(camera_get(camera, "sensor"), scenario, "digital_raw")
    assert sensor.metadata["sensor_simulation_db"]["run_id"] == run_id
    assert sensor.fields["fdtd_sensor"]["enabled"] is False
    assert sensor.fields["tcad_sensor"]["enabled"] is False
    wave = np.asarray(sensor.fields["wave"], dtype=float)
    spacing = 0.25e-6
    image_distance = 0.035
    photons = 1e13 + 1e12 * np.random.default_rng(9).random((64, 64, wave.size))
    fov = math.degrees(2 * math.atan(64 * spacing / (2 * image_distance)))
    oi = OpticalImage(
        fields={
            "wave": wave, "width_m": 64 * spacing, "height_m": 64 * spacing,
            "sample_spacing_m": spacing, "image_distance_m": image_distance,
            "fov_deg": fov, "vfov_deg": fov,
        },
        data={"photons": photons},
    )
    cropped, evidence = centered_oi_crop_for_sensor(oi, sensor, (48, 48))
    assert evidence["minimum_centered_shape_row_col"] == [40, 40]
    full_capture = sensor_compute(sensor, oi, seed=77)
    crop_capture = sensor_compute(sensor, cropped, seed=77)
    np.testing.assert_allclose(
        crop_capture.data["mean_electrons"], full_capture.data["mean_electrons"],
        rtol=1e-12, atol=1e-10,
    )
    np.testing.assert_array_equal(crop_capture.data["raw_dn"], full_capture.data["raw_dn"])
