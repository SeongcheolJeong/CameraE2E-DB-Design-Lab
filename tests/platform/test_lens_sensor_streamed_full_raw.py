"""Complete synthetic RAW from optical/sensor mean tiles and one readout."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from camera_platform import lens_sensor_streamed_full_raw as stitched
from camera_platform.lens_sensor_scene_geometry_tile import make_analytic_strict_geometry_provider
from camera_platform.lens_sensor_streamed_analytic_capture import AnalyticCaptureResourcePolicy
from pyisetcam.optics import oi_compute
from pyisetcam.sensor import sensor_compute, sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor
from pyisetcam.types import OpticalImage, Scene

WAVE = np.array([500.0, 600.0])
ANGLES = np.arange(0.0, 361.0, 90.0)
TILES = ((0, 5, 0, 3), (0, 5, 3, 8), (5, 8, 0, 3), (5, 8, 3, 8))


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


def _sensor(*, noise_flag: int, samples: int, shape=(8, 8)):
    sensor = sensor_create("default", {"size": shape, "wave": WAVE.copy()})
    sensor = sensor_set(sensor, "pixel size same fill factor", 1e-6)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", noise_flag)
    sensor.fields["n_samples_per_pixel"] = samples
    return configure_e2e_sensor(sensor)


def _fixture(*, noise_flag: int = 0, samples: int = 1):
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
    tile_policy = AnalyticCaptureResourcePolicy(
        estimated_working_budget_bytes=128 * 1024**2,
        max_geometry_query_cells=100_000,
        max_source_tiles=16,
        max_output_roi_cells=1024,
        max_source_tile_cells=1024,
    )
    resource_policy = stitched.StitchedRawResourcePolicy(
        optical_tile=tile_policy,
        estimated_working_budget_bytes=256 * 1024**2,
        max_full_sensor_cells=64,
        max_sensor_tiles=16,
    )
    return provider, _sensor(noise_flag=noise_flag, samples=samples), resource_policy


def _args(policy, tiles=TILES):
    return {
        "sensor_tiles_row_col_exclusive": tiles,
        "source_tile_shape_row_col": (16, 16),
        "sample_angles_deg": ANGLES,
        "resource_policy": policy,
    }


def _native_full_capture(provider, sensor, *, seed: int | None):
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
    return sensor_compute(sensor, oi, seed=seed)


@pytest.mark.parametrize(
    ("noise_flag", "samples", "seed"),
    [(0, 1, 71), (0, 3, 71), (2, 1, 71), (2, 1, None)],
)
def test_all_sensor_tiles_feed_one_native_readout_with_full_frame_parity(
    monkeypatch, noise_flag, samples, seed
):
    provider, sensor, policy = _fixture(noise_flag=noise_flag, samples=samples)
    plan = stitched.preflight_streamed_full_raw(provider, sensor, **_args(policy))
    assert plan["status"] == "ready_under_declared_policy"
    assert plan["sensor_tile_coverage"] == "complete_exact_nonoverlapping"
    assert plan["full_readout_policy_estimate_bytes"] > 8 * 8 * 8
    assert all(
        item["status"] == "ready_under_declared_policy"
        for item in plan["sensor_tile_preflights"]
    )

    native = _native_full_capture(provider, sensor, seed=seed)
    original_readout = stitched.resample_sensor_capture
    calls = []

    def observed_readout(configured, *, seed):
        calls.append((configured.data["mean_electrons"].shape, seed))
        return original_readout(configured, seed=seed)

    monkeypatch.setattr(stitched, "resample_sensor_capture", observed_readout)
    computed, report = stitched.compute_streamed_full_raw(
        provider, sensor, **_args(policy), seed=seed
    )
    selected_seed = 0 if seed is None else seed
    assert calls == [((8, 8), selected_seed)]
    np.testing.assert_allclose(
        computed.data["mean_electrons"], native.data["mean_electrons"],
        rtol=2e-7, atol=3e-8,
    )
    np.testing.assert_array_equal(computed.data["raw_dn"], native.data["raw_dn"])
    np.testing.assert_allclose(
        computed.data["reference_dn"], native.data["reference_dn"], rtol=2e-7
    )
    assert computed.metadata["sensor_capture"]["noise_seed"] == selected_seed
    assert report["native_readout_calls"] == 1
    assert report["native_readout_seed"] == selected_seed
    assert report["status"] == "diagnostic_complete_sensor_raw_executed"
    assert report["full_sensor_arrays_materialized_by_native_readout"] is True
    assert report["physical_fov_qualified"] is False
    assert report["product_full_frame_qualified"] is False
    assert all(
        item["optical_execution"]["full_source_cube_allocated_by_operator"] is False
        for item in report["sensor_tile_execution"]
    )
    assert sensor.data == {}


@pytest.mark.parametrize(
    ("tiles", "reason"),
    [
        (TILES[:-1], "gap"),
        (TILES + ((0, 4, 0, 4),), "overlap"),
        (((0, 8, 0, 9),), "outside"),
    ],
)
def test_preflight_rejects_incomplete_overlapping_or_outside_tiles(tiles, reason):
    provider, sensor, policy = _fixture()
    with pytest.raises(ValueError, match=reason):
        stitched.preflight_streamed_full_raw(provider, sensor, **_args(policy, tiles))


def test_full_readout_policy_blocks_before_any_optical_tile_preflight(monkeypatch):
    provider, sensor, policy = _fixture()

    def forbid_optical_preflight(*args, **kwargs):
        raise AssertionError("Optical preflight should not run after full RAW budget block.")

    monkeypatch.setattr(
        stitched, "preflight_streamed_analytic_offaxis_mean", forbid_optical_preflight
    )
    limited = replace(policy, estimated_working_budget_bytes=1)
    plan = stitched.preflight_streamed_full_raw(provider, sensor, **_args(limited))
    assert plan["status"] == "blocked_full_readout_memory_policy"
    assert plan["hard_process_rss_limit_enforced"] is False
    with pytest.raises(ValueError, match="blocked_full_readout_memory_policy"):
        stitched.compute_streamed_full_raw(provider, sensor, **_args(limited), seed=71)


def test_stale_sensor_capture_is_rejected():
    provider, sensor, policy = _fixture()
    sensor.data["mean_electrons"] = np.ones((8, 8))
    with pytest.raises(ValueError, match="fresh, uncomputed"):
        stitched.preflight_streamed_full_raw(provider, sensor, **_args(policy))


def test_mutated_sensor_model_is_rejected_before_native_readout(monkeypatch):
    provider, sensor, policy = _fixture()
    original = stitched.compute_streamed_analytic_offaxis_mean
    executed = []

    def mutating_tile(*args, **kwargs):
        values, report = original(*args, **kwargs)
        executed.append(kwargs["sensor_tile_bounds_row_col_exclusive"])
        sensor.fields["integration_time"] = 0.006
        return values, report

    monkeypatch.setattr(stitched, "compute_streamed_analytic_offaxis_mean", mutating_tile)
    with pytest.raises(ValueError, match="mutated"):
        stitched.compute_streamed_full_raw(provider, sensor, **_args(policy), seed=71)
    assert len(executed) == 1


def test_every_tile_preflight_passes_before_any_tile_execution(monkeypatch):
    provider, sensor, policy = _fixture()
    original = stitched.preflight_streamed_analytic_offaxis_mean
    planned = []

    def blocking_second_tile(*args, **kwargs):
        tile = kwargs["sensor_tile_bounds_row_col_exclusive"]
        planned.append(tile)
        if len(planned) == 2:
            return {"status": "blocked_scene_halo", "reason": "numeric Scene halo missing"}
        return original(*args, **kwargs)

    def forbid_execution(*args, **kwargs):
        raise AssertionError("No optical tile may execute after a failed preflight.")

    monkeypatch.setattr(
        stitched, "preflight_streamed_analytic_offaxis_mean", blocking_second_tile
    )
    monkeypatch.setattr(stitched, "compute_streamed_analytic_offaxis_mean", forbid_execution)
    with pytest.raises(ValueError, match="blocked_sensor_tile_preflight.*blocked_scene_halo"):
        stitched.compute_streamed_full_raw(provider, sensor, **_args(policy), seed=71)
    assert len(planned) == 2


def test_full_sensor_at_scene_boundary_blocks_without_numeric_halo():
    provider, _, policy = _fixture()
    sensor = _sensor(noise_flag=0, samples=1, shape=(32, 32))
    extended_policy = replace(policy, max_full_sensor_cells=1024)
    plan = stitched.preflight_streamed_full_raw(
        provider,
        sensor,
        **_args(extended_policy, tiles=((0, 32, 0, 32),)),
    )
    assert plan["status"] == "blocked_sensor_tile_preflight"
    assert "boundary" in plan["reason"] or "halo" in plan["reason"]
    assert plan["tile_preflight_order"] == "all_sensor_tiles_before_any_optical_execution"
    with pytest.raises(ValueError, match="blocked_sensor_tile_preflight"):
        stitched.compute_streamed_full_raw(
            provider,
            sensor,
            **_args(extended_policy, tiles=((0, 32, 0, 32),)),
            seed=71,
        )
