"""Sensor-stage equivalence for a globally located strict optical ROI."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from camera_platform.lens_sensor_roi_capture import (
    capture_fixed_sensor_from_optical_roi,
    capture_fixed_sensor_from_strict_optical_roi,
)
from pyisetcam.sensor import sensor_compute, sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor
from pyisetcam.types import OpticalImage

WORKSPACE = Path(__file__).resolve().parents[2]
LENS_REVISION = "6c48e967-74b4-5901-8186-ba6fafe4797b"


def _oi_fields(shape: tuple[int, int], spacing: float, wave: np.ndarray) -> dict:
    distance = 0.035
    rows, cols = shape
    return {
        "wave": wave,
        "width_m": cols * spacing,
        "height_m": rows * spacing,
        "sample_spacing_m": spacing,
        "image_distance_m": distance,
        "fov_deg": math.degrees(2 * math.atan(cols * spacing / (2 * distance))),
        "vfov_deg": math.degrees(2 * math.atan(rows * spacing / (2 * distance))),
    }


def _synthetic_pair():
    wave = np.array([450.0, 550.0, 650.0])
    spacing = 1e-6
    fields = _oi_fields((64, 64), spacing, wave)
    photons = 1e12 + 1e12 * np.random.default_rng(9).random((64, 64, wave.size))
    full_oi = OpticalImage(fields=fields, data={"photons": photons})
    sensor = sensor_create("default", {"size": (16, 16), "wave": wave})
    sensor = sensor_set(sensor, "pixel size same fill factor", 2.8e-6)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", 2)
    sensor = configure_e2e_sensor(sensor)
    bounds = [9, 55, 9, 55]
    evidence = {
        "mode": "strict_bounded_source_tiles_optical_roi",
        "global_grid_shape_row_col": [64, 64],
        "output_roi_row_col": bounds,
        "global_field_coordinates_preserved": True,
        "sensor_or_isp_executed": False,
        "full_source_cube_allocated_by_operator": False,
        "caller_source_cube_residency": "present",
        "caller_source_memory_qualified": True,
        "scene_halo_qualified": False,
        "sample_spacing_m": spacing,
        "wavelength_nm": wave.tolist(),
    }
    return sensor, full_oi, photons[9:55, 9:55], evidence


def test_centered_optical_roi_reuses_native_pixel_and_digital_raw_ops():
    sensor, full_oi, roi, optical_evidence = _synthetic_pair()
    full = sensor_compute(sensor, full_oi, seed=71)
    captured, evidence = capture_fixed_sensor_from_strict_optical_roi(
        sensor, roi, full_oi_fields=full_oi.fields,
        optical_roi_evidence=optical_evidence, seed=71,
    )
    # Local and global center coordinates can differ by last-bit floating
    # rounding even though the modeled sensor support is identical.
    np.testing.assert_allclose(
        captured.data["mean_electrons"], full.data["mean_electrons"],
        rtol=2e-15, atol=2e-15,
    )
    np.testing.assert_array_equal(captured.data["raw_dn"], full.data["raw_dn"])
    assert evidence["full_oi_photon_array_allocated_by_adapter"] is False
    assert evidence["scene_halo_qualified"] is False
    assert evidence["full_frame_execution_qualified"] is False
    assert captured.metadata["strict_optical_roi_sensor_capture"] == evidence


def test_provider_optical_roi_record_can_continue_without_full_source_cube():
    sensor, full_oi, roi, optical_evidence = _synthetic_pair()
    provider_evidence = {
        **optical_evidence,
        "caller_source_cube_residency": "unknown",
        "caller_source_memory_qualified": False,
        "source_identity": "bounded_test_tile_provider",
    }
    full = sensor_compute(sensor, full_oi, seed=23)
    captured, evidence = capture_fixed_sensor_from_strict_optical_roi(
        sensor, roi, full_oi_fields=full_oi.fields,
        optical_roi_evidence=provider_evidence, seed=23,
    )
    np.testing.assert_allclose(
        captured.data["mean_electrons"], full.data["mean_electrons"],
        rtol=2e-15, atol=2e-15,
    )
    np.testing.assert_array_equal(captured.data["raw_dn"], full.data["raw_dn"])
    assert evidence["full_source_cube_allocated_by_optical_operator"] is False
    assert evidence["caller_source_cube_residency"] == "unknown"
    assert evidence["caller_source_memory_qualified"] is False
    assert evidence["full_frame_execution_qualified"] is False


@pytest.mark.parametrize("change,match", [
    ({"output_roi_row_col": [8, 54, 9, 55]}, "Off-center"),
    ({"output_roi_row_col": [10, 54, 10, 54]}, "halo"),
    ({"sample_spacing_m": 1.01e-6}, "spacing"),
    ({"global_field_coordinates_preserved": False}, "global field"),
    ({"mode": "unknown"}, "strict optical ROI"),
])
def test_incompatible_optical_roi_is_rejected(change, match):
    sensor, full_oi, roi, evidence = _synthetic_pair()
    evidence = {**evidence, **change}
    if change.get("output_roi_row_col") == [10, 54, 10, 54]:
        roi = roi[1:-1, 1:-1]
    with pytest.raises(ValueError, match=match):
        capture_fixed_sensor_from_strict_optical_roi(
            sensor, roi, full_oi_fields=full_oi.fields,
            optical_roi_evidence=evidence, seed=71,
        )


def test_pinned_p0014_optical_roi_and_saved_def_sensor_match_full_sensor_capture():
    root = WORKSPACE / "camerae2e_db"
    if not (root / "catalog.sqlite").is_file():
        pytest.skip("Pinned lens and sensor DB unavailable")
    from camera_platform.lens_catalog_bridge import _asset, _revision_source
    from camera_platform.offline_assets import offline_asset_store
    from pyisetcam.camera import camera_create, camera_get
    from pyisetcam.image_sensor_db import image_sensor_db_reconcile_scenario
    from pyisetcam.lens_patents import lens_patent_raytrace_optics
    from pyisetcam.optics import (
        _image_distance_m,
        _radiance_to_irradiance,
        _raytrace_expected_kernel_shape,
        _raytrace_geometry,
        _strict_raytrace_apply_psf_bounded,
        _strict_raytrace_apply_psf_bounded_roi,
        _strict_raytrace_apply_psf_bounded_roi_provider,
        oi_compute,
    )
    from pyisetcam.sensor_simulation_db import sensor_simulation_db_config
    from pyisetcam.system_faca import (
        _apply_physics_and_sensor_overrides,
        _configure_scenario_readout,
    )
    from pyisetcam.types import Scene

    run_id = "360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601"
    scenario = sensor_simulation_db_config(
        "DEF-2512-801", root=root, run_id=run_id
    )["scenario"]
    scenario = {
        **scenario,
        "sensor": {
            **scenario.get("sensor", {}),
            "size": [16, 16], "integration_time": 0.004, "noise_flag": 2,
        },
    }
    scenario = image_sensor_db_reconcile_scenario(scenario)
    scenario["sensor_output_mode"] = "digital_raw"
    assets = offline_asset_store(workspace=WORKSPACE)
    camera, _ = _apply_physics_and_sensor_overrides(
        camera_create(asset_store=assets), scenario, assets
    )
    sensor = _configure_scenario_readout(
        camera_get(camera, "sensor"), scenario, "digital_raw"
    )
    assert sensor.metadata["sensor_simulation_db"]["run_id"] == run_id
    assert sensor.fields["fdtd_sensor"]["enabled"] is False
    assert sensor.fields["tcad_sensor"]["enabled"] is False

    revision, snapshot, lens_db, _ = _revision_source(LENS_REVISION, root)
    simulation_id, psf_path, check, _ = _asset(revision, snapshot, lens_db, root)
    optics = lens_patent_raytrace_optics(
        simulation_id, db_path=lens_db, psf_dir=psf_path.parent,
        allow_approximate_radiometry=True,
    )
    assert optics["raytrace"]["asset_sha256"] == check["asset_sha256"]
    wave = np.array([450.0])
    spacing = 1.151213027e-6
    cube = 1e13 + 1e12 * np.random.default_rng(19).random((32, 32, 1))
    kernel_shape = _raytrace_expected_kernel_shape(optics, spacing)
    padding = tuple(side // 2 + 2 for side in kernel_shape)
    angles = np.arange(0.0, 361.0, 90.0)
    full_padded, _ = _strict_raytrace_apply_psf_bounded(
        cube, optics, wave, spacing, padding, angles
    )
    full_photons = full_padded[
        padding[0]:padding[0] + 32, padding[1]:padding[1] + 32
    ]
    roi, optical_evidence = _strict_raytrace_apply_psf_bounded_roi(
        cube, optics, wave, spacing, padding, angles,
        output_roi_row_col=(2, 30, 2, 30),
        source_tile_shape_row_col=(16, 16),
    )
    fields = _oi_fields((32, 32), spacing, wave)
    full = sensor_compute(sensor, OpticalImage(
        fields=fields, data={"photons": full_photons}
    ), seed=77)
    captured, evidence = capture_fixed_sensor_from_strict_optical_roi(
        sensor, roi, full_oi_fields=fields,
        optical_roi_evidence=optical_evidence, seed=77,
    )
    np.testing.assert_allclose(
        captured.data["mean_electrons"], full.data["mean_electrons"],
        rtol=1e-12, atol=1e-18,
    )
    np.testing.assert_array_equal(captured.data["raw_dn"], full.data["raw_dn"])
    assert evidence["full_source_cube_allocated_by_optical_operator"] is False
    assert evidence["caller_source_cube_residency"] == "present"
    assert evidence["full_frame_execution_qualified"] is False

    provider_roi, provider_optical_evidence = (
        _strict_raytrace_apply_psf_bounded_roi_provider(
            (32, 32), lambda r0, r1, c0, c1: cube[r0:r1, c0:c1],
            optics, wave, spacing, padding, angles,
            output_roi_row_col=(2, 30, 2, 30),
            source_identity="pinned_p0014_test_callback",
            source_tile_shape_row_col=(16, 16),
        )
    )
    provider_capture, provider_evidence = capture_fixed_sensor_from_strict_optical_roi(
        sensor, provider_roi, full_oi_fields=fields,
        optical_roi_evidence=provider_optical_evidence, seed=77,
    )
    np.testing.assert_allclose(
        provider_capture.data["mean_electrons"], full.data["mean_electrons"],
        rtol=1e-12, atol=1e-18,
    )
    np.testing.assert_array_equal(provider_capture.data["raw_dn"], full.data["raw_dn"])
    assert provider_evidence["caller_source_cube_residency"] == "unknown"
    assert provider_evidence["caller_source_memory_qualified"] is False
    assert provider_evidence["full_frame_execution_qualified"] is False

    # The real CameraE2E OI passed to a sensor is padded. The provider's ROI
    # coordinates, in contrast, describe the unpadded Scene/geometry grid.
    object_distance = float(optics["raytrace"].get("object_distance_m", np.inf))
    scene = Scene(fields={
        "wave": wave,
        "distance_m": object_distance,
        "fov_deg": 1.0,
        "vfov_deg": 1.0,
        "rows": 32,
        "cols": 32,
    }, data={"photons": cube})
    image_distance = _image_distance_m(optics, scene)
    scene_fov = math.degrees(2 * math.atan(32 * spacing / (2 * image_distance)))
    scene.fields["fov_deg"] = scene_fov
    scene.fields["vfov_deg"] = scene_fov
    full_oi = oi_compute(OpticalImage(fields={
        "optics": optics,
        "psf_compute_mode": "bounded_streaming",
        "psf_sample_angles_deg": angles,
    }), scene, crop=False)
    actual_spacing = full_oi.fields["sample_spacing_m"]
    actual_padding = tuple(full_oi.fields["padding_pixels"])
    distorted = _raytrace_geometry(
        _radiance_to_irradiance(cube, optics, scene), wave, optics, scene
    )
    actual_roi, actual_optical_evidence = _strict_raytrace_apply_psf_bounded_roi_provider(
        (32, 32), lambda r0, r1, c0, c1: distorted[r0:r1, c0:c1],
        optics, wave, actual_spacing, actual_padding, angles,
        output_roi_row_col=(2, 30, 2, 30),
        source_identity="pinned_p0014_computed_oi_test_callback",
        source_tile_shape_row_col=(16, 16),
    )
    actual_full_capture = sensor_compute(sensor, full_oi, seed=77)
    actual_roi_capture, actual_evidence = capture_fixed_sensor_from_optical_roi(
        actual_roi, (32, 32), (2, 30, 2, 30), optics, wave,
        actual_spacing, full_oi.fields["image_distance_m"], sensor,
        actual_optical_evidence, seed=77,
    )
    np.testing.assert_allclose(
        actual_roi_capture.data["mean_electrons"],
        actual_full_capture.data["mean_electrons"],
        rtol=1e-12, atol=1e-18,
    )
    np.testing.assert_array_equal(
        actual_roi_capture.data["raw_dn"], actual_full_capture.data["raw_dn"]
    )
    assert actual_evidence["full_oi_shape_row_col"] == list(full_oi.data["photons"].shape[:2])
    assert actual_evidence["strict_psf_padding_row_col"] == list(actual_padding)
    assert actual_evidence["geometry_psf_signature_verified"] is True
    assert actual_evidence["geometry_psf_signature_scope"] == ["geometry", "psf"]
    assert actual_evidence["full_optical_response_signature_verified"] is False
    assert actual_evidence["caller_source_cube_residency"] == "unknown"
    assert actual_evidence["full_frame_execution_qualified"] is False
