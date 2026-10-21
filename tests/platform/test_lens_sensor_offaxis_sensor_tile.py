"""Global-coordinate sensor tile parity against CameraE2E's native full OI."""

from __future__ import annotations

import tracemalloc

import numpy as np
import pytest

from camera_platform.lens_sensor_offaxis_sensor_tile import (
    compute_fixed_sensor_tile_mean_electrons,
    preflight_fixed_sensor_tile_mean_electrons,
)
from camera_platform.lens_sensor_out_of_core_readout import (
    OutOfCoreReadoutPolicy,
    compute_out_of_core_readout,
)
from pyisetcam.sensor import sensor_compute_noise_free, sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor
from pyisetcam.types import OpticalImage

WAVE = np.array([450.0, 550.0, 650.0])


def _pair(
    oi_shape: tuple[int, int],
    sensor_shape: tuple[int, int],
    *,
    pitch_m: float,
    oi_spacing_m: float,
    samples: int,
):
    rows = np.arange(oi_shape[0], dtype=float)[:, None, None]
    cols = np.arange(oi_shape[1], dtype=float)[None, :, None]
    spectral = np.array([1.0, 1.3, 0.7])[None, None, :]
    photons = 3e12 * spectral * (1.0 + 0.37 * np.sin(rows / 4.0) + 0.22 * np.cos(cols / 3.0))
    oi = OpticalImage(
        fields={
            "wave": WAVE.copy(),
            "width_m": oi_shape[1] * oi_spacing_m,
            "height_m": oi_shape[0] * oi_spacing_m,
            "sample_spacing_m": oi_spacing_m,
        },
        data={"photons": photons},
    )
    sensor = sensor_create("default", {"size": sensor_shape, "wave": WAVE.copy()})
    sensor = sensor_set(sensor, "pixel size same fill factor", pitch_m)
    sensor = sensor_set(sensor, "pattern", np.array([[1, 2], [3, 4]]))
    sensor = sensor_set(
        sensor,
        "filter spectra",
        np.array(
            [
                [0.43, 0.12, 0.04, 0.03],
                [0.22, 0.49, 0.31, 0.08],
                [0.03, 0.19, 0.27, 0.46],
            ]
        ),
    )
    sensor = sensor_set(sensor, "filter names", ["R", "Gr", "Gb", "B"])
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor.fields["n_samples_per_pixel"] = samples
    sensor = configure_e2e_sensor(sensor)
    return oi, sensor


def _reference_mean(oi: OpticalImage, sensor) -> np.ndarray:
    # compute_sensor_capture uses this same native noise-free stage before
    # readout; call it explicitly to keep the comparison upstream of noise.
    native = sensor.clone()
    native.fields.pop("sensor_output_mode", None)
    native.fields.pop("sensor_readout", None)
    native.metadata.pop("sensor_capture", None)
    computed = sensor_compute_noise_free(native, oi, seed=17)
    return np.asarray(computed.data["volts"], dtype=float) / float(
        computed.fields["pixel"]["conversion_gain_v_per_electron"]
    )


def _tile(oi: OpticalImage, sensor, sensor_bounds, *, halo_extra=1):
    full_shape = np.asarray(oi.data["photons"]).shape[:2]
    probe = preflight_fixed_sensor_tile_mean_electrons(
        sensor,
        full_oi_shape_row_col=full_shape,
        sensor_tile_bounds_row_col_exclusive=sensor_bounds,
        oi_sample_spacing_m=float(oi.fields["sample_spacing_m"]),
    )
    (r0, r1), (c0, c1) = probe["required_global_oi_indices_inclusive_by_axis"]
    optical_bounds = (
        max(0, r0 - halo_extra),
        min(full_shape[0], r1 + halo_extra + 1),
        max(0, c0 - halo_extra),
        min(full_shape[1], c1 + halo_extra + 1),
    )
    crop = np.asarray(oi.data["photons"])[
        optical_bounds[0] : optical_bounds[1], optical_bounds[2] : optical_bounds[3], :
    ]
    result, evidence = compute_fixed_sensor_tile_mean_electrons(
        sensor,
        crop,
        full_oi_shape_row_col=full_shape,
        optical_roi_bounds_row_col_exclusive=optical_bounds,
        sensor_tile_bounds_row_col_exclusive=sensor_bounds,
        wavelength_nm=WAVE,
        oi_sample_spacing_m=float(oi.fields["sample_spacing_m"]),
    )
    return result, evidence, optical_bounds, crop


@pytest.mark.parametrize(
    "oi_shape,sensor_shape,pitch_m,samples,sensor_bounds",
    [
        ((90, 92), (12, 14), 2.1e-6, 1, (1, 7, 3, 10)),
        ((91, 93), (13, 15), 1.6e-6, 3, (5, 13, 7, 15)),
        ((91, 93), (13, 15), 1.6e-6, 5, (2, 10, 1, 8)),
    ],
)
def test_offcenter_tile_preserves_global_cfa_and_native_spatial_integration(
    oi_shape,
    sensor_shape,
    pitch_m,
    samples,
    sensor_bounds,
):
    oi, sensor = _pair(
        oi_shape,
        sensor_shape,
        pitch_m=pitch_m,
        oi_spacing_m=0.8e-6,
        samples=samples,
    )
    full_mean = _reference_mean(oi, sensor)
    actual, evidence, _, _ = _tile(oi, sensor, sensor_bounds)
    r0, r1, c0, c1 = sensor_bounds
    np.testing.assert_allclose(actual, full_mean[r0:r1, c0:c1], rtol=3e-14, atol=3e-14)
    assert evidence["cfa_phase_origin_sensor_row_col"] == [r0, c0]
    assert evidence["sensor_subpixel_samples_per_pixel"] == samples
    assert evidence["gaussian_kernel_shape_row_col"] == [int(np.ceil(pitch_m / 0.8e-6))] * 2
    assert evidence["noise_and_fixed_pattern_executed"] is False
    assert evidence["readout_executed"] is False


def test_irregular_edge_tiles_reconstruct_full_sensor_mean_in_any_order():
    oi, sensor = _pair(
        (90, 92),
        (12, 14),
        pitch_m=2.1e-6,
        oi_spacing_m=0.8e-6,
        samples=1,
    )
    expected = _reference_mean(oi, sensor)
    assembled = np.empty_like(expected)
    bounds = [
        (r0, r1, c0, c1) for r0, r1 in ((0, 5), (5, 12)) for c0, c1 in ((0, 3), (3, 9), (9, 14))
    ]
    for tile_bounds in reversed(bounds):
        actual, _, _, _ = _tile(oi, sensor, tile_bounds)
        r0, r1, c0, c1 = tile_bounds
        assembled[r0:r1, c0:c1] = actual
    np.testing.assert_allclose(assembled, expected, rtol=3e-14, atol=3e-14)


def test_offaxis_global_cfa_and_coordinate_keyed_raw_equal_full_sensor_slice(tmp_path):
    oi, sensor = _pair(
        (90, 92), (12, 14), pitch_m=2.1e-6, oi_spacing_m=0.8e-6, samples=1
    )
    bounds = (1, 7, 3, 10)
    r0, r1, c0, c1 = bounds
    full_mean = _reference_mean(oi, sensor)
    tile_mean, evidence, _, _ = _tile(oi, sensor, bounds)
    np.testing.assert_allclose(tile_mean, full_mean[r0:r1, c0:c1], rtol=3e-14, atol=3e-14)
    assert evidence["cfa_phase_origin_sensor_row_col"] == [r0, c0]
    sensor.fields["fixed_pattern_seed"] = 13
    sensor.fields["pixel"]["prnu_sigma"] = 0.015
    policy = OutOfCoreReadoutPolicy(
        max_sensor_cells=1000,
        max_row_chunk_cells=1000,
        max_transient_bytes=10_000_000,
        max_output_bytes=1_000_000,
        max_resident_input_bytes=1_000_000,
        min_free_after_output_bytes=0,
    )
    full = compute_out_of_core_readout(
        sensor, full_mean, tmp_path / "full", seed=23, row_chunk_rows=4, policy=policy
    )
    roi = compute_out_of_core_readout(
        sensor, tile_mean, tmp_path / "roi", seed=23, row_chunk_rows=2,
        policy=policy, global_sensor_roi_bounds_row_col_exclusive=bounds,
    )
    np.testing.assert_array_equal(
        np.load(roi["raw_dn_path"]), np.load(full["raw_dn_path"])[r0:r1, c0:c1]
    )
    # Optical tile integration differs from the full path at floating ULPs;
    # the integer RAW and coordinate-keyed noise/FPN realization are identical.
    for name in ("reference_dn", "volts"):
        np.testing.assert_allclose(
            np.load(roi[f"{name}_path"]), np.load(full[f"{name}_path"])[r0:r1, c0:c1],
            rtol=1e-14, atol=1e-12,
        )


def test_preflight_clips_optical_boundary_halo_to_true_global_grid():
    oi, sensor = _pair(
        (34, 37),
        (12, 14),
        pitch_m=2.1e-6,
        oi_spacing_m=0.8e-6,
        samples=1,
    )
    bounds = (0, 4, 10, 14)
    preflight = preflight_fixed_sensor_tile_mean_electrons(
        sensor,
        full_oi_shape_row_col=(34, 37),
        sensor_tile_bounds_row_col_exclusive=bounds,
        oi_sample_spacing_m=0.8e-6,
    )
    assert preflight["required_global_oi_indices_inclusive_by_axis"][1][1] == 36
    actual, _, optical_bounds, _ = _tile(oi, sensor, bounds)
    assert optical_bounds[3] == 37
    expected = _reference_mean(oi, sensor)
    np.testing.assert_allclose(actual, expected[0:4, 10:14], rtol=3e-14, atol=3e-14)


def test_tiny_tile_preflight_does_not_allocate_huge_virtual_axes():
    _, sensor = _pair(
        (90, 92),
        (12, 14),
        pitch_m=2.1e-6,
        oi_spacing_m=0.8e-6,
        samples=3,
    )
    sensor.fields["size"] = (1_000_000_000, 1_000_000_000)
    tracemalloc.start()
    try:
        plan = preflight_fixed_sensor_tile_mean_electrons(
            sensor,
            full_oi_shape_row_col=(1_000_000_000, 1_000_000_000),
            sensor_tile_bounds_row_col_exclusive=(
                500_000_000,
                500_000_002,
                500_000_000,
                500_000_002,
            ),
            oi_sample_spacing_m=2.1e-6,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert plan["status"] == "noise_free_sensor_tile_preflight_passed"
    assert peak < 1_000_000
    assert all(
        last - first < 20 for first, last in plan["required_global_oi_indices_inclusive_by_axis"]
    )


def test_missing_one_global_oi_halo_sample_is_rejected():
    oi, sensor = _pair(
        (90, 92),
        (12, 14),
        pitch_m=2.1e-6,
        oi_spacing_m=0.8e-6,
        samples=1,
    )
    bounds = (1, 7, 3, 10)
    _, evidence, _, _ = _tile(oi, sensor, bounds, halo_extra=0)
    (r0, r1), (c0, c1) = evidence["required_global_oi_indices_inclusive_by_axis"]
    with pytest.raises(ValueError, match="halo"):
        compute_fixed_sensor_tile_mean_electrons(
            sensor,
            oi.data["photons"][r0 + 1 : r1 + 1, c0 : c1 + 1, :],
            full_oi_shape_row_col=(90, 92),
            optical_roi_bounds_row_col_exclusive=(r0 + 1, r1 + 1, c0, c1 + 1),
            sensor_tile_bounds_row_col_exclusive=bounds,
            wavelength_nm=WAVE,
            oi_sample_spacing_m=0.8e-6,
        )


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("fdtd_sensor", {"enabled": True}, "fdtd_sensor"),
        ("tcad_sensor", {"enabled": True}, "tcad_sensor"),
        ("ocl_group_proxy", {"mode": "uniform", "shape": [2, 2], "equalization": 1.0}, "OCL"),
        ("vignetting", "centered", "vignetting"),
    ],
)
def test_unsupported_spatial_sensor_responses_fail_closed(field, value, match):
    oi, sensor = _pair(
        (90, 92),
        (12, 14),
        pitch_m=2.1e-6,
        oi_spacing_m=0.8e-6,
        samples=1,
    )
    sensor.fields[field] = value
    with pytest.raises(ValueError, match=match):
        compute_fixed_sensor_tile_mean_electrons(
            sensor,
            oi.data["photons"],
            full_oi_shape_row_col=(90, 92),
            optical_roi_bounds_row_col_exclusive=(0, 90, 0, 92),
            sensor_tile_bounds_row_col_exclusive=(1, 7, 3, 10),
            wavelength_nm=WAVE,
            oi_sample_spacing_m=0.8e-6,
        )


def test_unknown_readout_and_nonunity_etendue_fail_closed():
    oi, sensor = _pair(
        (90, 92),
        (12, 14),
        pitch_m=2.1e-6,
        oi_spacing_m=0.8e-6,
        samples=1,
    )
    args = dict(
        full_oi_shape_row_col=(90, 92),
        optical_roi_bounds_row_col_exclusive=(0, 90, 0, 92),
        sensor_tile_bounds_row_col_exclusive=(1, 7, 3, 10),
        wavelength_nm=WAVE,
        oi_sample_spacing_m=0.8e-6,
    )
    unknown = sensor.clone()
    unknown.fields["sensor_readout"] = {"gain": 1.0}
    with pytest.raises(ValueError, match="readout"):
        compute_fixed_sensor_tile_mean_electrons(unknown, oi.data["photons"], **args)
    etendue = sensor.clone()
    etendue.fields["etendue"] = np.full((12, 14), 0.9)
    with pytest.raises(ValueError, match="etendue"):
        compute_fixed_sensor_tile_mean_electrons(etendue, oi.data["photons"], **args)
