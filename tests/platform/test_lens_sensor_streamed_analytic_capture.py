"""Small numerical and admission tests for the diagnostic streamed RAW path."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from camera_platform.lens_sensor_scene_geometry_tile import (
    make_analytic_strict_geometry_provider,
)
from camera_platform.lens_sensor_streamed_analytic_capture import (
    AnalyticCaptureResourcePolicy,
    capture_streamed_analytic_center_roi,
    preflight_streamed_analytic_capture,
)
from pyisetcam.optics import oi_compute
from pyisetcam.sensor import sensor_compute, sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor
from pyisetcam.types import OpticalImage, Scene

WAVE = np.array([500.0, 600.0])
ANGLES = np.arange(0.0, 361.0, 90.0)
ROI = (12, 28, 12, 28)


def _optics() -> dict:
    field = np.array([0.0, 0.1])

    def table(values: np.ndarray) -> dict:
        return {
            "field_height_mm": field.copy(),
            "wavelength_nm": WAVE.copy(),
            "function": values,
            "field_coordinate": "ideal_image_height",
        }

    psf = np.zeros((3, 3, 2, 2), dtype=float)
    psf[1, 1, :, :] = 0.8
    psf[1, 2, :, :] = 0.2
    psf_table = table(psf)
    psf_table.update(
        sample_spacing_mm=np.array([0.001, 0.001]),
        coordinate_convention="pixel_centers",
        row_axis="y_up",
        psf_reference_axis="+x",
    )
    return {
        "model": "raytrace",
        "focal_length_m": 0.01,
        "f_number": 2.0,
        "offaxis_method": "skip",
        "transmittance": {"wave": WAVE.copy(), "scale": np.array([0.7, 0.9])},
        "raytrace": {
            "contract_version": 2,
            "physics_policy": "strict",
            "coordinate_convention": "pixel_centers",
            "object_distance_m": math.inf,
            "effective_focal_length_m": 0.01,
            "effective_f_number": 2.0,
            "max_fov_deg": 1.0,
            "geometry": table(np.array([[0.0, 0.0], [0.08, 0.09]])),
            "relative_illumination": table(np.array([[1.0, 1.0], [0.65, 0.75]])),
            "psf": psf_table,
        },
    }


def _fixture():
    optics = _optics()
    fov = math.degrees(2 * math.atan(40 * 1e-6 / (2 * 0.01)))
    provider = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=(40, 40),
        scene_horizontal_fov_deg=fov,
        wavelengths_nm=WAVE,
        bright_side_photons=np.array([1e13, 1.2e13]),
        analytic_slanted_edge_offset_deg=0.0,
        max_tile_cells=1024,
    )
    sensor = sensor_create("default", {"size": (8, 8), "wave": WAVE})
    sensor = sensor_set(sensor, "pixel size same fill factor", 1e-6)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", 2)
    sensor = configure_e2e_sensor(sensor)
    policy = AnalyticCaptureResourcePolicy(
        estimated_working_budget_bytes=128 * 1024**2,
        max_geometry_query_cells=100_000,
        max_source_tiles=16,
        max_output_roi_cells=1024,
        max_source_tile_cells=1024,
    )
    return provider, sensor, policy


def _args(provider, sensor, policy) -> dict:
    return {
        "optical_roi_bounds": ROI,
        "source_tile_shape_row_col": (16, 16),
        "sample_angles_deg": ANGLES,
        "resource_policy": policy,
    }


def test_streamed_analytic_diagnostic_matches_small_full_oi_to_raw():
    provider, sensor, policy = _fixture()
    preflight = preflight_streamed_analytic_capture(
        provider, sensor, **_args(provider, sensor, policy)
    )
    assert preflight["status"] == "ready_under_declared_policy"
    assert preflight["candidate_source_tile_count"] == 4
    assert preflight["psf_grid_preflight"]["status"] == "within_planner_grid_cap"
    assert preflight["combined_policy_estimate_bytes"] < policy.estimated_working_budget_bytes
    assert preflight["stored_psf_numeric_halo"]["stored_psf_numeric_halo_inside_declared_scene"]
    assert preflight["stored_psf_numeric_halo"]["physical_scene_halo_qualified"] is False
    assert preflight["static_optics_ndarray_payload_bytes"] >= (
        provider.optics["raytrace"]["psf"]["function"].nbytes
    )
    captured, report = capture_streamed_analytic_center_roi(
        provider, sensor, seed=17, **_args(provider, sensor, policy)
    )

    reference_scene_photons = np.zeros((40, 40, WAVE.size), dtype=float)
    for row in range(0, 40, 16):
        for col in range(0, 40, 16):
            stop_row, stop_col = min(row + 16, 40), min(col + 16, 40)
            reference_scene_photons[row:stop_row, col:stop_col] = (
                provider.read_scene_tile(row, stop_row, col, stop_col)
            )
    scene = Scene(fields={
        "wave": WAVE.copy(),
        "fov_deg": provider.horizontal_fov_deg,
        "vfov_deg": provider.vertical_fov_deg,
        "distance_m": math.inf,
        "rows": 40,
        "cols": 40,
    }, data={"photons": reference_scene_photons})
    full_oi = oi_compute(OpticalImage(fields={
        "optics": provider.optics,
        "psf_compute_mode": "bounded_streaming",
        "psf_sample_angles_deg": ANGLES,
    }), scene, crop=False)
    reference = sensor_compute(sensor, full_oi, seed=17)
    np.testing.assert_allclose(
        captured.data["mean_electrons"], reference.data["mean_electrons"],
        rtol=5e-12, atol=1e-12,
    )
    np.testing.assert_array_equal(captured.data["raw_dn"], reference.data["raw_dn"])
    assert float(np.max(captured.data["mean_electrons"])) > float(
        np.min(captured.data["mean_electrons"])
    )
    assert report["status"] == "diagnostic_raw_executed"
    assert report["optical_execution"]["source_identity"] == provider.source_identity
    assert report["sensor_execution"]["geometry_psf_signature_verified"] is True
    assert report["sensor_execution"]["full_optical_response_signature_verified"] is False
    assert report["full_optical_state_sha256"] == provider.optics_state_sha256
    assert report["full_optical_state_verified_before_and_after"] is True
    assert report["resource_preflight"]["resource_policy_is_hardware_limit"] is False
    assert report["actual_peak_rss_bytes_measured"] is None
    assert report["hard_process_rss_limit_enforced"] is False
    assert report["scene_halo_qualified"] is False
    assert report["stored_psf_numeric_halo"] == preflight["stored_psf_numeric_halo"]
    assert report["full_frame_execution_qualified"] is False
    assert report["physical_fov_qualified"] is False
    assert report["worker_submission"] is False


def test_declared_scene_boundary_is_separate_from_stored_psf_halo() -> None:
    provider, sensor, policy = _fixture()
    whole_scene = preflight_streamed_analytic_capture(
        provider,
        sensor,
        optical_roi_bounds=(0, 40, 0, 40),
        source_tile_shape_row_col=(16, 16),
        sample_angles_deg=ANGLES,
        resource_policy=replace(policy, max_output_roi_cells=1600),
    )
    assert whole_scene["status"] == "ready_under_declared_policy"
    assert whole_scene["stored_psf_numeric_halo"]["pregeometry_inside_declared_scene"] is False
    assert (
        whole_scene["stored_psf_numeric_halo"]["stored_psf_numeric_halo_inside_declared_scene"]
        is False
    )
    assert whole_scene["scene_halo_qualified"] is False


def test_policy_blocks_before_streaming_psf_work():
    provider, sensor, policy = _fixture()
    tiny = replace(policy, estimated_working_budget_bytes=1)
    kwargs = _args(provider, sensor, tiny)
    preflight = preflight_streamed_analytic_capture(provider, sensor, **kwargs)
    assert preflight["status"] == "blocked_working_memory_policy"
    with pytest.raises(ValueError, match="resource_blocked: blocked_working_memory_policy"):
        capture_streamed_analytic_center_roi(provider, sensor, seed=3, **kwargs)


def test_psf_scalar_grid_guard_blocks_before_kernel_allocation():
    provider, sensor, policy = _fixture()
    optics = _optics()
    optics["raytrace"]["psf"]["sample_spacing_mm"] = np.array([2.1, 2.1])
    incompatible = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=(40, 40),
        scene_horizontal_fov_deg=provider.horizontal_fov_deg,
        wavelengths_nm=WAVE,
        bright_side_photons=np.array([1e13, 1.2e13]),
        analytic_slanted_edge_offset_deg=0.0,
        max_tile_cells=1024,
    )
    preflight = preflight_streamed_analytic_capture(
        incompatible, sensor, **_args(incompatible, sensor, policy)
    )
    assert preflight["status"] == "blocked_psf_grid_policy"
    assert preflight["psf_grid_preflight"]["status"].startswith("blocked_psf")


def test_provider_pitch_cannot_be_changed_independently_of_fov():
    provider, sensor, policy = _fixture()
    incompatible = replace(provider, oi_pitch_um=provider.oi_pitch_um / 2)
    with pytest.raises(ValueError, match="OI pitch disagrees"):
        preflight_streamed_analytic_capture(
            incompatible, sensor, **_args(incompatible, sensor, policy)
        )


def test_off_center_and_unsupported_sensor_are_rejected_before_execution():
    provider, sensor, policy = _fixture()
    kwargs = _args(provider, sensor, policy)
    kwargs["optical_roi_bounds"] = (11, 27, 12, 28)
    with pytest.raises(ValueError, match="Off-center"):
        preflight_streamed_analytic_capture(provider, sensor, **kwargs)
    unsupported = sensor.clone()
    unsupported.fields["fdtd_sensor"] = {"enabled": True, "mode": "qe+field+crosstalk"}
    with pytest.raises(ValueError, match="fdtd_sensor"):
        preflight_streamed_analytic_capture(
            provider, unsupported, **_args(provider, unsupported, policy)
        )
    auto = sensor.clone()
    auto.fields["auto_exposure"] = True
    with pytest.raises(ValueError, match="fixed-exposure"):
        preflight_streamed_analytic_capture(provider, auto, **_args(provider, auto, policy))


def test_admission_caps_are_declared_policy_not_claimed_hardware():
    with pytest.raises(ValueError, match="diagnostic admission policies"):
        AnalyticCaptureResourcePolicy(
            estimated_working_budget_bytes=9 * 1024**3,
            max_geometry_query_cells=100_000,
            max_source_tiles=16,
            max_output_roi_cells=1024,
            max_source_tile_cells=1024,
        )
    raised_queries = AnalyticCaptureResourcePolicy(
        estimated_working_budget_bytes=128 * 1024**2,
        max_geometry_query_cells=8_000_001,
        max_source_tiles=16,
        max_output_roi_cells=1024,
        max_source_tile_cells=1024,
        max_geometry_query_chunk_cells=4096,
    )
    assert raised_queries.as_dict()["max_geometry_query_cells"] == 8_000_001
    assert raised_queries.as_dict()["max_geometry_query_chunk_cells"] == 4096
    with pytest.raises(ValueError, match="max_geometry_query_chunk_cells"):
        replace(raised_queries, max_geometry_query_chunk_cells=65_537)
    provider, sensor, policy = _fixture()
    smaller = replace(policy, max_source_tile_cells=64)
    preflight = preflight_streamed_analytic_capture(
        provider, sensor,
        optical_roi_bounds=ROI,
        source_tile_shape_row_col=(16, 16),
        sample_angles_deg=ANGLES,
        resource_policy=smaller,
    )
    assert preflight["status"] == "blocked_source_tile_policy"


def test_full_optical_fingerprint_rejects_mutated_provider_before_psf_work():
    provider, sensor, policy = _fixture()
    provider.optics["transmittance"]["scale"][0] = 0.5
    with pytest.raises(ValueError, match="provider optics changed"):
        preflight_streamed_analytic_capture(
            provider, sensor, **_args(provider, sensor, policy)
        )
