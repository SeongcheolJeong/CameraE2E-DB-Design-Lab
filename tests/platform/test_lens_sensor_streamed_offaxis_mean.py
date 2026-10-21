"""A global optical tile and off-axis sensor mean against the native full path."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

import camera_platform.lens_sensor_streamed_offaxis_mean as offaxis_mean
from camera_platform.lens_sensor_scene_geometry_tile import (
    AnalyticStrictGeometryTileProvider,
    make_analytic_strict_geometry_provider,
)
from camera_platform.lens_sensor_streamed_analytic_capture import (
    AnalyticCaptureResourcePolicy,
)
from camera_platform.lens_sensor_streamed_offaxis_mean import (
    compute_streamed_analytic_offaxis_mean,
    preflight_streamed_analytic_offaxis_mean,
)
from pyisetcam.optics import oi_compute
from pyisetcam.sensor import sensor_compute_noise_free, sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor
from pyisetcam.types import OpticalImage, Scene

WAVE = np.array([500.0, 600.0])
ANGLES = np.arange(0.0, 361.0, 90.0)
OFFAXIS_TILE = (1, 5, 4, 8)


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
    psf[1, 1, :, :] = 0.75
    psf[1, 2, :, :] = 0.25
    psf_table = table(psf)
    psf_table.update(
        {
            "sample_spacing_mm": np.array([0.001, 0.001]),
            "coordinate_convention": "pixel_centers",
            "row_axis": "y_up",
            "psf_reference_axis": "+x",
        }
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


def _sensor(shape=(8, 8), *, samples=1):
    sensor = sensor_create("default", {"size": shape, "wave": WAVE.copy()})
    sensor = sensor_set(sensor, "pixel size same fill factor", 1e-6)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor.fields["n_samples_per_pixel"] = samples
    return configure_e2e_sensor(sensor)


def _fixture():
    fov = math.degrees(2 * math.atan(32e-6 / (2 * 0.01)))
    provider = make_analytic_strict_geometry_provider(
        _optics(),
        scene_shape_row_col=(32, 32),
        scene_horizontal_fov_deg=fov,
        wavelengths_nm=WAVE,
        bright_side_photons=np.array([1e13, 1.2e13]),
        analytic_slanted_edge_offset_deg=0.0,
        max_tile_cells=1024,
    )
    policy = AnalyticCaptureResourcePolicy(
        estimated_working_budget_bytes=128 * 1024**2,
        max_geometry_query_cells=100_000,
        max_source_tiles=16,
        max_output_roi_cells=1024,
        max_source_tile_cells=1024,
    )
    return provider, policy


def _args(policy, bounds=OFFAXIS_TILE):
    return {
        "sensor_tile_bounds_row_col_exclusive": bounds,
        "source_tile_shape_row_col": (16, 16),
        "sample_angles_deg": ANGLES,
        "resource_policy": policy,
    }


def _full_native_mean(provider, sensor):
    scene = Scene(
        fields={
            "wave": WAVE.copy(),
            "fov_deg": provider.horizontal_fov_deg,
            "vfov_deg": provider.vertical_fov_deg,
            "distance_m": math.inf,
            "rows": 32,
            "cols": 32,
        },
        data={"photons": provider.read_scene_tile(0, 32, 0, 32)},
    )
    oi = oi_compute(
        OpticalImage(
            fields={
                "optics": provider.optics,
                "psf_compute_mode": "bounded_streaming",
                "psf_sample_angles_deg": ANGLES,
            }
        ),
        scene,
        crop=False,
    )
    native = sensor.clone()
    native.fields.pop("sensor_output_mode", None)
    native.fields.pop("sensor_readout", None)
    native.metadata.pop("sensor_capture", None)
    captured = sensor_compute_noise_free(native, oi, seed=17)
    return np.asarray(captured.data["volts"], dtype=float) / float(
        captured.fields["pixel"]["conversion_gain_v_per_electron"]
    )


@pytest.mark.parametrize("samples", [1, 3])
def test_offaxis_streamed_mean_matches_full_native_optics_and_sensor(samples):
    provider, policy = _fixture()
    sensor = _sensor(samples=samples)
    plan = preflight_streamed_analytic_offaxis_mean(provider, sensor, **_args(policy))
    assert plan["status"] == "ready_under_declared_policy"
    assert plan["stored_psf_numeric_halo"]["status"] == "inside_declared_scene"
    assert plan["combined_policy_estimate_bytes"] < policy.estimated_working_budget_bytes
    assert (
        plan["optical_roi_row_col_exclusive"][0]
        != (
            provider.global_shape[0]
            - (plan["optical_roi_row_col_exclusive"][1] - plan["optical_roi_row_col_exclusive"][0])
        )
        // 2
    )
    actual, report = compute_streamed_analytic_offaxis_mean(provider, sensor, **_args(policy))
    full = _full_native_mean(provider, sensor)
    r0, r1, c0, c1 = OFFAXIS_TILE
    # The native spectral current density is float32.  A local interpolation
    # coordinate can fall one float32 ULP across a rounding boundary even
    # when the strict optical ROI agrees with full OI to double precision.
    np.testing.assert_allclose(actual, full[r0:r1, c0:c1], rtol=2e-7, atol=3e-8)
    assert report["status"] == "diagnostic_offaxis_mean_executed"
    assert report["sensor_execution"]["cfa_phase_origin_sensor_row_col"] == [r0, c0]
    assert report["sensor_execution"]["noise_and_fixed_pattern_executed"] is False
    assert report["optical_execution"]["source_identity"] == provider.source_identity
    assert report["optical_execution"]["strict_psf_validation_once_per_roi"] is True
    assert report["full_frame_execution_qualified"] is False
    assert report["physical_scene_halo_qualified"] is False


def test_batched_tile_integrity_rejects_non_psf_optics_mutation(monkeypatch):
    provider, policy = _fixture()
    sensor = _sensor()
    original = AnalyticStrictGeometryTileProvider._read_tile_after_integrity_preflight
    calls = 0

    def mutating_tile(self, r0, r1, c0, c1):
        nonlocal calls
        result = original(self, r0, r1, c0, c1)
        calls += 1
        if calls == 1:
            self.optics["raytrace"]["relative_illumination"]["function"][0, 0] += 0.01
        return result

    monkeypatch.setattr(
        AnalyticStrictGeometryTileProvider,
        "_read_tile_after_integrity_preflight", mutating_tile,
    )
    with pytest.raises(ValueError, match="provider optics changed during"):
        compute_streamed_analytic_offaxis_mean(provider, sensor, **_args(policy))
    assert calls > 1


def test_scene_boundary_and_sensor_boundary_are_blocked_before_psf():
    provider, policy = _fixture()
    at_scene_edge = _sensor(shape=(32, 32))
    scene_plan = preflight_streamed_analytic_offaxis_mean(
        provider, at_scene_edge, **_args(policy, bounds=(0, 4, 0, 4))
    )
    assert scene_plan["status"] == "blocked_scene_halo"
    assert scene_plan["stored_psf_numeric_halo"]["status"] == (
        "global_constant_zero_boundary_touched"
    )
    with pytest.raises(ValueError, match="resource_blocked: blocked_scene_halo"):
        compute_streamed_analytic_offaxis_mean(
            provider, at_scene_edge, **_args(policy, bounds=(0, 4, 0, 4))
        )

    outside_interior = _sensor(shape=(34, 34))
    outside_plan = preflight_streamed_analytic_offaxis_mean(
        provider, outside_interior, **_args(policy, bounds=(0, 4, 0, 4))
    )
    assert outside_plan["status"] == "blocked_sensor_optical_boundary"


def test_tight_policy_blocks_before_optical_execution():
    provider, policy = _fixture()
    sensor = _sensor()
    tight = replace(policy, estimated_working_budget_bytes=1)
    plan = preflight_streamed_analytic_offaxis_mean(provider, sensor, **_args(tight))
    assert plan["status"] == "blocked_working_memory_policy"
    with pytest.raises(ValueError, match="resource_blocked: blocked_working_memory_policy"):
        compute_streamed_analytic_offaxis_mean(provider, sensor, **_args(tight))


def test_opt_in_cache_cap_is_bound_from_admission_to_optical_execution(monkeypatch):
    provider, policy = _fixture()
    sensor = _sensor()
    opted = replace(policy, estimated_working_budget_bytes=6 * 1024**3)
    selected = 64 * 1024**2
    baseline, _ = compute_streamed_analytic_offaxis_mean(provider, sensor, **_args(policy))

    def select(kernel_shape, declared_budget):
        assert kernel_shape == (3, 3)
        assert declared_budget == opted.estimated_working_budget_bytes
        return selected

    # The small fixture cannot naturally demand >32 MiB of prepared kernels;
    # select an opt-in cap here to exercise its admission-to-execution binding.
    monkeypatch.setattr(
        offaxis_mean, "_strict_roi_psf_cache_budget_for_declared_policy", select
    )
    plan = preflight_streamed_analytic_offaxis_mean(provider, sensor, **_args(opted))
    assert plan["status"] == "ready_under_declared_policy"
    assert plan["strict_psf_preparation_cache_budget_bytes"] == selected
    assert plan["worst_source_tile"]["tile_memory_policy"][
        "strict_shared_psf_preparation_cache_policy_bytes"
    ] == selected
    actual, report = compute_streamed_analytic_offaxis_mean(
        provider, sensor, **_args(opted)
    )
    np.testing.assert_array_equal(actual, baseline)
    assert report["resource_preflight"]["source_identity"] == provider.source_identity
    assert report["optical_execution"]["source_identity"] == provider.source_identity
    assert report["optical_execution"]["shared_psf_preparation_cache_budget_bytes"] == selected
    assert report["optical_execution"]["shared_psf_preparation_cache_peak_bytes"] <= selected


def test_halo_query_policy_is_not_reported_as_scene_boundary():
    provider, policy = _fixture()
    sensor = _sensor()
    tight = replace(policy, max_geometry_query_cells=10)
    plan = preflight_streamed_analytic_offaxis_mean(provider, sensor, **_args(tight))
    assert plan["status"] == "blocked_halo_query_policy"
    assert plan["stored_psf_numeric_halo"]["status"] == "blocked_geometry_query_policy"
    assert "boundary" not in plan["reason"]
    with pytest.raises(ValueError, match="resource_blocked: blocked_halo_query_policy"):
        compute_streamed_analytic_offaxis_mean(provider, sensor, **_args(tight))
