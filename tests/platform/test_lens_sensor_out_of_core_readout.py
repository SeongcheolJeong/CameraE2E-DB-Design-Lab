"""Bounded-memory full-frame RAW readout contract and native comparisons."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from camera_platform import lens_sensor_out_of_core_readout as readout_module
from camera_platform.lens_sensor_out_of_core_readout import (
    OutOfCoreReadoutPolicy,
    compute_out_of_core_readout,
    preflight_out_of_core_readout,
)
from pyisetcam.sensor import sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor, resample_sensor_capture


def _sensor(shape: tuple[int, int], *, noise_flag: int, readout: dict | None = None):
    sensor = sensor_create("default", {"size": shape})
    sensor = sensor_set(sensor, "integration time", 0.02)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", noise_flag)
    return configure_e2e_sensor(
        sensor,
        readout={
            "dark_current_e_per_s": 2.0,
            "read_noise_e": 4.0,
            "black_level_dn": 128.0,
            "bit_depth": 16,
            **(readout or {}),
        },
    )


def _policy(*, cells: int = 500_000, resident: int = 10_000_000):
    return OutOfCoreReadoutPolicy(
        max_sensor_cells=cells,
        max_row_chunk_cells=200_000,
        max_transient_bytes=200_000_000,
        max_output_bytes=100_000_000,
        max_resident_input_bytes=resident,
        min_free_after_output_bytes=0,
    )


@pytest.mark.parametrize("flag", [-1, 0])
def test_noise_off_matches_native_readout_and_persists_full_shape(tmp_path: Path, flag: int):
    shape = (9, 11)
    sensor = _sensor(shape, noise_flag=flag)
    mean = np.linspace(0.0, 12000.0, shape[0] * shape[1]).reshape(shape)
    sensor.data["mean_electrons"] = mean
    native = resample_sensor_capture(sensor, seed=73)
    target = tmp_path / "bounded"
    result = compute_out_of_core_readout(
        sensor, mean, target, seed=73, row_chunk_rows=3, policy=_policy()
    )
    np.testing.assert_array_equal(np.load(result["raw_dn_path"]), native.data["raw_dn"])
    np.testing.assert_array_equal(
        np.load(result["reference_dn_path"]), native.data["reference_dn"]
    )
    np.testing.assert_array_equal(np.load(result["volts_path"]), native.data["volts"])
    assert result["sensor_capture"]["saturation"] == native.metadata["sensor_capture"]["saturation"]
    saved = json.loads((target / "metadata.json").read_text())
    assert saved["preflight"]["status"] == "ready_under_declared_policy"
    assert saved["complete_sensor_mean_materialized_by_readout"] is False
    assert saved["sensor_capture"]["native_seed_bit_parity"] is False
    assert saved["dv_alias"] == "raw_dn"
    assert saved["output_files"]["raw_dn"]["bytes"] > shape[0] * shape[1] * 4


def test_coordinate_randomness_is_identical_across_chunk_sizes_and_fixed_patterns(
    tmp_path: Path,
):
    shape = (17, 19)
    sensor = _sensor(shape, noise_flag=2, readout={"bit_depth": 20})
    conversion_gain = sensor.fields["pixel"]["conversion_gain_v_per_electron"]
    sensor.fields["pixel"]["dsnu_sigma_v"] = 2.0 * conversion_gain
    sensor.fields["pixel"]["prnu_sigma"] = 0.015
    sensor.fields["column_fpn"] = np.array([conversion_gain, 0.01])
    sensor.fields["fixed_pattern_seed"] = 19
    mean = np.linspace(1.0, 600.0, shape[0] * shape[1]).reshape(shape)
    one = compute_out_of_core_readout(
        sensor, mean, tmp_path / "one", seed=23, row_chunk_rows=1, policy=_policy()
    )
    four = compute_out_of_core_readout(
        sensor, mean, tmp_path / "four", seed=23, row_chunk_rows=4, policy=_policy()
    )
    changed = compute_out_of_core_readout(
        sensor, mean, tmp_path / "changed", seed=24, row_chunk_rows=3, policy=_policy()
    )
    for name in ("raw_dn", "reference_dn", "volts"):
        np.testing.assert_array_equal(
            np.load(one[f"{name}_path"]), np.load(four[f"{name}_path"])
        )
    np.testing.assert_array_equal(
        np.load(one["reference_dn_path"]), np.load(changed["reference_dn_path"])
    )
    assert not np.array_equal(
        np.load(one["raw_dn_path"]), np.load(changed["raw_dn_path"])
    )
    assert not any(key.endswith("fpn_image") for key in sensor.fields)
    assert one["sensor_capture"]["generated_fixed_pattern_maps_materialized"] is False


@pytest.mark.parametrize("noise_flag", [0, 2])
@pytest.mark.parametrize("calibrated_maps", [False, True])
def test_global_roi_readout_equals_full_capture_slice_for_noise_and_fpn(
    tmp_path: Path, noise_flag: int, calibrated_maps: bool
):
    shape = (9, 11)
    bounds = (1, 7, 3, 10)
    r0, r1, c0, c1 = bounds
    sensor = _sensor(shape, noise_flag=noise_flag, readout={"bit_depth": 16})
    conversion_gain = sensor.fields["pixel"]["conversion_gain_v_per_electron"]
    sensor.fields["fixed_pattern_seed"] = 19
    sensor.fields["pixel"]["dsnu_sigma_v"] = 2.0 * conversion_gain
    sensor.fields["pixel"]["prnu_sigma"] = 0.01
    sensor.fields["column_fpn"] = np.array([conversion_gain, 0.02])
    if calibrated_maps:
        sensor.fields["offset_fpn_image"] = (
            np.arange(shape[0] * shape[1]).reshape(shape) - 20.0
        ) * conversion_gain
        sensor.fields["gain_fpn_image"] = 1.0 + (
            np.arange(shape[0] * shape[1]).reshape(shape) % 7
        ) * 0.001
        sensor.fields["column_offset_fpn"] = np.linspace(-1.0, 1.0, shape[1]) * conversion_gain
        sensor.fields["column_gain_fpn"] = np.linspace(0.9, 1.1, shape[1])
    mean = np.linspace(3.0, 700.0, shape[0] * shape[1]).reshape(shape)
    full = compute_out_of_core_readout(
        sensor, mean, tmp_path / "full", seed=41, row_chunk_rows=4, policy=_policy()
    )
    roi = compute_out_of_core_readout(
        sensor, mean[r0:r1, c0:c1], tmp_path / "roi", seed=41,
        row_chunk_rows=2, policy=_policy(),
        global_sensor_roi_bounds_row_col_exclusive=bounds,
    )
    assert roi["full_sensor_shape_row_col"] == list(shape)
    assert roi["global_sensor_roi_bounds_row_col_exclusive"] == list(bounds)
    assert roi["preflight"]["sensor_cells"] == (r1 - r0) * (c1 - c0)
    for name in ("raw_dn", "reference_dn", "volts"):
        np.testing.assert_array_equal(
            np.load(roi[f"{name}_path"]), np.load(full[f"{name}_path"])[r0:r1, c0:c1]
        )


def test_global_roi_requires_full_sensor_coordinates_and_local_mean_shape(tmp_path: Path):
    sensor = _sensor((9, 11), noise_flag=0)
    with pytest.raises(ValueError, match="half-open bounds"):
        preflight_out_of_core_readout(
            sensor, np.zeros((4, 4)), tmp_path / "bad", seed=1, row_chunk_rows=2,
            policy=_policy(), global_sensor_roi_bounds_row_col_exclusive=(7, 11, 3, 7),
        )
    with pytest.raises(ValueError, match="declared global sensor ROI shape"):
        preflight_out_of_core_readout(
            sensor, np.zeros((4, 4)), tmp_path / "bad", seed=1, row_chunk_rows=2,
            policy=_policy(), global_sensor_roi_bounds_row_col_exclusive=(1, 6, 3, 7),
        )


def test_calibrated_fpn_well_and_adc_order_matches_native_without_random_charge(
    tmp_path: Path,
):
    shape = (8, 10)
    sensor = _sensor(
        shape, noise_flag=1,
        readout={
            "dark_current_e_per_s": 0.0,
            "read_noise_e": 0.0,
            "full_well_e": 100.0,
            "black_level_dn": 4096.0,
        },
    )
    conversion_gain = sensor.fields["pixel"]["conversion_gain_v_per_electron"]
    mean = np.zeros(shape, dtype=float)
    sensor.fields["offset_fpn_image"] = np.tile([-15.0, 15.0], (8, 5)) * conversion_gain
    sensor.fields["gain_fpn_image"] = np.full(shape, 1.2)
    sensor.fields["column_offset_fpn"] = np.linspace(-1.0, 1.0, shape[1]) * conversion_gain
    sensor.fields["column_gain_fpn"] = np.linspace(0.9, 1.1, shape[1])
    sensor.data["mean_electrons"] = mean
    native = resample_sensor_capture(sensor, seed=71)
    result = compute_out_of_core_readout(
        sensor, mean, tmp_path / "maps", seed=71, row_chunk_rows=3, policy=_policy()
    )
    for name in ("raw_dn", "reference_dn", "volts"):
        np.testing.assert_array_equal(
            np.load(result[f"{name}_path"]), native.data[name]
        )


def test_noise_distribution_tracks_native_without_claiming_seed_bit_parity(tmp_path: Path):
    shape = (384, 384)
    mean = np.full(shape, 700.0)
    sensor = _sensor(
        shape, noise_flag=2,
        readout={"bit_depth": 20, "dark_current_e_per_s": 0.0, "read_noise_e": 6.0},
    )
    sensor.data["mean_electrons"] = mean
    native = resample_sensor_capture(sensor, seed=51)
    result = compute_out_of_core_readout(
        sensor, mean, tmp_path / "statistics", seed=51, row_chunk_rows=8, policy=_policy()
    )
    scale = native.metadata["sensor_capture"]["dn_per_electron"]
    legacy_e = (native.data["raw_dn"].astype(float) - 128.0) / scale
    bounded_e = (np.load(result["raw_dn_path"], mmap_mode="r").astype(float) - 128.0) / scale
    for measured in (legacy_e, bounded_e):
        assert float(np.mean(measured)) == pytest.approx(700.0, abs=0.6)
        assert float(np.var(measured)) == pytest.approx(700.0 + 36.0, rel=0.025)
    assert not np.array_equal(native.data["raw_dn"], np.load(result["raw_dn_path"]))


def test_policy_blocks_large_resident_input_and_bad_values_leave_no_output(tmp_path: Path):
    sensor = _sensor((8, 8), noise_flag=0)
    mean = np.full((8, 8), 10.0)
    policy = _policy(resident=1)
    target = tmp_path / "blocked"
    plan = preflight_out_of_core_readout(
        sensor, mean, target, seed=1, row_chunk_rows=2, policy=policy
    )
    assert plan["status"] == "blocked_resident_input_policy"
    with pytest.raises(ValueError, match="blocked_resident_input_policy"):
        compute_out_of_core_readout(
            sensor, mean, target, seed=1, row_chunk_rows=2, policy=policy
        )
    assert not target.exists()
    mean[-1, -1] = np.nan
    with pytest.raises(ValueError, match="finite and nonnegative"):
        compute_out_of_core_readout(
            sensor, mean, target, seed=1, row_chunk_rows=2, policy=_policy()
        )
    assert not target.exists()
    assert not tuple(tmp_path.glob(".readout_tmp_*"))


def test_full_height_row_chunk_is_refused_without_writing(tmp_path: Path):
    sensor = _sensor((8, 8), noise_flag=0)
    mean = np.full((8, 8), 10.0)
    target = tmp_path / "full_height"
    plan = preflight_out_of_core_readout(
        sensor, mean, target, seed=1, row_chunk_rows=8, policy=_policy()
    )
    assert plan["status"] == "blocked_full_height_row_chunk_policy"
    with pytest.raises(ValueError, match="blocked_full_height_row_chunk_policy"):
        compute_out_of_core_readout(
            sensor, mean, target, seed=1, row_chunk_rows=8, policy=_policy()
        )
    assert not target.exists()
    assert not tuple(tmp_path.glob(".readout_tmp_*"))


def test_existing_output_and_disk_budget_are_rejected_before_write(tmp_path: Path):
    sensor = _sensor((8, 8), noise_flag=0)
    mean = np.zeros((8, 8))
    target = tmp_path / "prior"
    target.mkdir()
    assert preflight_out_of_core_readout(
        sensor, mean, target, seed=1, row_chunk_rows=2, policy=_policy()
    )["status"] == "blocked_existing_output"
    policy = OutOfCoreReadoutPolicy(
        max_sensor_cells=100,
        max_row_chunk_cells=100,
        max_transient_bytes=1_000_000,
        max_output_bytes=1,
        max_resident_input_bytes=1_000_000,
        min_free_after_output_bytes=0,
    )
    assert preflight_out_of_core_readout(
        sensor, mean, tmp_path / "new", seed=1, row_chunk_rows=2, policy=policy
    )["status"] == "blocked_output_bytes_policy"


def test_readonly_memmap_input_and_reopened_output_hash(tmp_path: Path):
    shape = (32, 40)
    sensor = _sensor(shape, noise_flag=2)
    mean_path = tmp_path / "input_mean.npy"
    writable = np.lib.format.open_memmap(mean_path, mode="w+", dtype=np.float64, shape=shape)
    writable[:, :] = np.linspace(2.0, 900.0, shape[0] * shape[1]).reshape(shape)
    writable.flush()
    del writable
    mean = np.load(mean_path, mmap_mode="r")
    assert isinstance(mean, np.memmap)
    assert mean.flags.writeable is False
    plan = preflight_out_of_core_readout(
        sensor, mean, tmp_path / "capture", seed=31, row_chunk_rows=4,
        policy=_policy(resident=1_000_000),
    )
    assert plan["status"] == "ready_under_declared_policy"
    assert plan["mean_input_is_memmap"] is True
    assert plan["resident_input_ndarray_payload_bytes"] < mean.nbytes
    result = compute_out_of_core_readout(
        sensor, mean, tmp_path / "capture", seed=31, row_chunk_rows=4,
        policy=_policy(resident=1_000_000),
    )
    reopened = np.load(result["raw_dn_path"], mmap_mode="r")
    assert isinstance(reopened, np.memmap)
    assert reopened.shape == shape
    with Path(result["raw_dn_path"]).open("rb") as handle:
        actual_hash = hashlib.sha256(handle.read()).hexdigest()
    assert actual_hash == result["output_files"]["raw_dn"]["sha256"]
    assert result["mean_input_content_sha256"] == hashlib.sha256(
        np.ascontiguousarray(mean).tobytes()
    ).hexdigest()


def test_small_calibration_view_charges_large_retained_owner(tmp_path: Path):
    sensor = _sensor((8, 8), noise_flag=1)
    owner = np.ones((1000, 1000), dtype=np.float64)
    sensor.fields["offset_fpn_image"] = owner[:8, :8]
    sensor.fields["gain_fpn_image"] = owner[100:108, 100:108]
    mean = np.zeros((8, 8))
    plan = preflight_out_of_core_readout(
        sensor, mean, tmp_path / "blocked_owner", seed=1, row_chunk_rows=2,
        policy=_policy(resident=1_000_000),
    )
    assert plan["status"] == "blocked_resident_input_policy"
    assert plan["resident_input_ndarray_payload_bytes"] >= owner.nbytes
    assert plan["resident_input_ndarray_payload_bytes"] < owner.nbytes * 2
    assert not (tmp_path / "blocked_owner").exists()


def test_calibrated_maps_do_not_generate_discarded_random_fpn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sensor = _sensor(
        (8, 8), noise_flag=1,
        readout={"dark_current_e_per_s": 0.0, "read_noise_e": 0.0},
    )
    sensor.fields["offset_fpn_image"] = np.zeros((8, 8))
    sensor.fields["gain_fpn_image"] = np.ones((8, 8))
    sensor.fields["column_offset_fpn"] = np.zeros(8)
    sensor.fields["column_gain_fpn"] = np.ones(8)
    sensor.fields["pixel"]["dsnu_sigma_v"] = 0.01
    sensor.fields["pixel"]["prnu_sigma"] = 0.01
    sensor.fields["column_fpn"] = np.array([0.01, 0.01])

    def no_fpn_generation(*args, **kwargs):
        raise AssertionError("Supplied FPN maps must bypass random generation.")

    monkeypatch.setattr(readout_module, "_normal", no_fpn_generation)
    result = compute_out_of_core_readout(
        sensor, np.zeros((8, 8)), tmp_path / "calibrated",
        seed=9, row_chunk_rows=2, policy=_policy(),
    )
    np.testing.assert_array_equal(np.load(result["raw_dn_path"]), 128)
