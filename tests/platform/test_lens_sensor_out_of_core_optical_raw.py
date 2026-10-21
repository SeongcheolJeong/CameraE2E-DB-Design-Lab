"""Complete strict optical tile capture through disk-backed mean and RAW."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from camera_platform import lens_sensor_out_of_core_optical_raw as optical_raw
from camera_platform.lens_sensor_out_of_core_readout import OutOfCoreReadoutPolicy
from camera_platform.lens_sensor_scene_geometry_tile import make_analytic_strict_geometry_provider
from camera_platform.lens_sensor_streamed_analytic_capture import AnalyticCaptureResourcePolicy
from camera_platform.lens_sensor_streamed_full_raw import (
    StitchedRawResourcePolicy,
    compute_streamed_full_raw,
)
from pyisetcam.sensor import sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor

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
    psf_table.update({
        "sample_spacing_mm": np.array([0.001, 0.001]),
        "coordinate_convention": "pixel_centers",
        "row_axis": "y_up",
        "psf_reference_axis": "+x",
    })
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


def _fixture(*, noise_flag: int = 0):
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
    sensor = sensor_create("default", {"size": (8, 8), "wave": WAVE.copy()})
    sensor = sensor_set(sensor, "pixel size same fill factor", 1e-6)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = sensor_set(sensor, "noise flag", noise_flag)
    sensor.fields["n_samples_per_pixel"] = 1
    sensor = configure_e2e_sensor(sensor)
    optical_policy = AnalyticCaptureResourcePolicy(
        estimated_working_budget_bytes=128 * 1024**2,
        max_geometry_query_cells=100_000,
        max_source_tiles=16,
        max_output_roi_cells=1024,
        max_source_tile_cells=1024,
    )
    readout_policy = OutOfCoreReadoutPolicy(
        max_sensor_cells=64,
        max_row_chunk_cells=64,
        max_transient_bytes=1_000_000,
        max_output_bytes=1_000_000,
        max_resident_input_bytes=1_000_000,
        min_free_after_output_bytes=0,
    )
    policy = optical_raw.OpticalDiskRawResourcePolicy(
        optical_tile=optical_policy,
        readout=readout_policy,
        max_sensor_tiles=16,
        max_mean_disk_bytes=1_000_000,
        max_total_output_bytes=5_000_000,
        max_combined_working_bytes=200_000_000,
        min_free_after_output_bytes=0,
    )
    return provider, sensor, policy


def _kwargs(policy, tiles=TILES):
    return {
        "sensor_tiles_row_col_exclusive": tiles,
        "source_tile_shape_row_col": (16, 16),
        "sample_angles_deg": ANGLES,
        "seed": 71,
        "row_chunk_rows": 3,
        "resource_policy": policy,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_complete_disk_capture_matches_existing_stitched_noise_off_and_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    provider, sensor, policy = _fixture()
    native_policy = StitchedRawResourcePolicy(
        optical_tile=policy.optical_tile,
        estimated_working_budget_bytes=256 * 1024**2,
        max_full_sensor_cells=64,
        max_sensor_tiles=16,
    )
    native, _ = compute_streamed_full_raw(
        provider, sensor,
        sensor_tiles_row_col_exclusive=TILES,
        source_tile_shape_row_col=(16, 16),
        sample_angles_deg=ANGLES,
        resource_policy=native_policy,
        seed=71,
    )
    observed = []
    original = optical_raw.compute_out_of_core_readout
    admitted_tiles = []
    original_preflight = optical_raw.preflight_streamed_analytic_offaxis_mean

    def inspected_readout(configured, mean, output_dir, **kwargs):
        observed.append((isinstance(mean, np.memmap), mean.flags.writeable, mean.shape))
        return original(configured, mean, output_dir, **kwargs)

    monkeypatch.setattr(optical_raw, "compute_out_of_core_readout", inspected_readout)

    def counted_preflight(*args, **kwargs):
        admitted_tiles.append(kwargs["sensor_tile_bounds_row_col_exclusive"])
        return original_preflight(*args, **kwargs)

    monkeypatch.setattr(optical_raw, "preflight_streamed_analytic_offaxis_mean", counted_preflight)
    target = tmp_path / "full_disk"
    result = optical_raw.compute_optical_to_disk_raw(
        provider, sensor, target, **_kwargs(policy)
    )
    assert observed == [(True, False, (8, 8))]
    assert admitted_tiles == list(TILES)
    for key, path_key in (
        ("mean_electrons", "mean_electrons_path"),
        ("raw_dn", "raw_dn_path"),
        ("reference_dn", "reference_dn_path"),
        ("volts", "volts_path"),
    ):
        actual = np.load(result[path_key], mmap_mode="r")
        assert isinstance(actual, np.memmap)
        np.testing.assert_array_equal(actual, native.data[key])
    assert result["status"] == "diagnostic_complete_sensor_raw_executed"
    assert result["sensor_tile_execution_count"] == len(TILES)
    assert result["sensor_tile_admitted_plans_verified_before_optics"] is True
    assert result["sensor_tile_geometry_halo_preflight_count"] == len(TILES)
    assert result["sensor_tile_geometry_halo_preflight_repeated_during_optics"] is False
    assert result["full_sensor_mean_or_raw_materialized_in_ram"] is False
    assert result["native_seed_bit_parity"] is False
    assert _sha256(Path(result["mean_electrons_path"])) == result["mean_electrons_npy_sha256"]
    tile_file = target / "tile_execution.jsonl"
    assert _sha256(tile_file) == result["sensor_tile_execution_jsonl_sha256"]
    assert len(tile_file.read_text().splitlines()) == len(TILES)
    nested = json.loads((target / "readout" / "metadata.json").read_text())
    assert nested["preflight"]["output_directory"] == str(target / "readout")
    assert nested["output_files"]["raw_dn"]["sha256"] == _sha256(target / "readout" / "raw_dn.npy")
    assert result["mean_electrons_content_sha256"] == nested["mean_input_content_sha256"]


def test_partition_change_preserves_mean_and_noise_off_raw(tmp_path: Path):
    provider, sensor, policy = _fixture()
    first = optical_raw.compute_optical_to_disk_raw(
        provider, sensor, tmp_path / "partitioned", **_kwargs(policy)
    )
    second = optical_raw.compute_optical_to_disk_raw(
        provider, sensor, tmp_path / "row_stripes",
        **_kwargs(policy, tiles=((0, 4, 0, 8), (4, 8, 0, 8))),
    )
    np.testing.assert_allclose(
        np.load(first["mean_electrons_path"]), np.load(second["mean_electrons_path"]),
        rtol=2e-7, atol=3e-8,
    )
    np.testing.assert_array_equal(
        np.load(first["raw_dn_path"]), np.load(second["raw_dn_path"])
    )


def test_optical_to_disk_noisy_capture_is_row_chunk_invariant(tmp_path: Path):
    provider, sensor, policy = _fixture(noise_flag=2)
    first = optical_raw.compute_optical_to_disk_raw(
        provider, sensor, tmp_path / "noise_1",
        **{**_kwargs(policy), "row_chunk_rows": 1},
    )
    second = optical_raw.compute_optical_to_disk_raw(
        provider, sensor, tmp_path / "noise_3", **_kwargs(policy)
    )
    for name in ("mean_electrons", "raw_dn", "reference_dn", "volts"):
        np.testing.assert_array_equal(
            np.load(first[f"{name}_path"]), np.load(second[f"{name}_path"])
        )
    assert first["native_seed_bit_parity"] is False


def test_group_local_optical_reuse_preserves_disk_raw_and_reduces_optical_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    provider, sensor, policy = _fixture(noise_flag=0)
    legacy = optical_raw.compute_optical_to_disk_raw(
        provider, sensor, tmp_path / "legacy", **_kwargs(policy)
    )
    sensor_calls = []
    original_sensor = optical_raw.compute_fixed_sensor_tile_mean_electrons

    def counted_sensor(configured, optical_child, **kwargs):
        sensor_calls.append((optical_child.shape, kwargs["sensor_tile_bounds_row_col_exclusive"]))
        return original_sensor(configured, optical_child, **kwargs)

    monkeypatch.setattr(optical_raw, "compute_fixed_sensor_tile_mean_electrons", counted_sensor)
    reused = optical_raw.compute_optical_to_disk_raw(
        provider, sensor, tmp_path / "reused",
        **_kwargs(policy), reuse_optical_groups=True,
    )
    assert len(sensor_calls) == len(TILES)
    assert [tuple(bounds) for _, bounds in sensor_calls] == list(TILES)
    assert all(shape[0] * shape[1] < provider.global_shape[0] * provider.global_shape[1]
               for shape, _ in sensor_calls)
    assert reused["optical_reuse_group_count"] >= 1
    assert reused["optical_source_tiles_processed_actual"] < legacy[
        "optical_source_tiles_processed_actual"
    ]
    assert reused["optical_sparse_convolutions_actual"] < legacy[
        "optical_sparse_convolutions_actual"
    ]
    for name in ("mean_electrons", "raw_dn", "reference_dn", "volts"):
        np.testing.assert_array_equal(
            np.load(reused[f"{name}_path"]), np.load(legacy[f"{name}_path"])
        )
    tile_lines = (tmp_path / "reused" / "tile_execution.jsonl").read_text().splitlines()
    evidence = [json.loads(line) for line in tile_lines]
    assert len(evidence) == len(TILES)
    assert all(item["optical_reuse"]["key_sha256"] for item in evidence)
    assert reused["optical_work_counter_aggregation"] == "once_per_reuse_group_or_singleton"
    for group_key in {item["optical_reuse"]["key_sha256"] for item in evidence}:
        members = [item for item in evidence if item["optical_reuse"]["key_sha256"] == group_key]
        assert [item["optical_reuse"]["group_member_index"] for item in members] == list(
            range(len(members))
        )
        assert all(
            item["optical_reuse"]["counter_aggregation"]
            == "shared_group_count_once_by_key_sha256"
            for item in members
        )
        assert [
            item["optical_reuse"]["optical_execution_counted_in_summary"]
            for item in members
        ] == [True, *([False] * (len(members) - 1))]
    assert reused["optical_source_tiles_processed_actual"] == sum(
        item["optical_execution"]["source_tiles_processed"]
        for item in evidence
        if item["optical_reuse"]["optical_execution_counted_in_summary"]
    )
    assert reused["optical_sparse_convolutions_actual"] == sum(
        item["optical_execution"]["sparse_convolutions"]
        for item in evidence
        if item["optical_reuse"]["optical_execution_counted_in_summary"]
    )
    assert all(
        item["optical_reuse"]["child_optical_roi_row_col_exclusive"]
        == item["resource_preflight"]["optical_roi_row_col_exclusive"]
        for item in evidence
    )
    assert reused["full_sensor_mean_or_raw_materialized_in_ram"] is False


def test_changed_reuse_group_plan_is_rejected_before_optics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    provider, sensor, policy = _fixture()
    original = optical_raw.preflight_optical_to_disk_raw

    def changed_group(*args, **kwargs):
        plan = original(*args, **kwargs)
        assert plan["optical_reuse_group_count"] >= 1
        plan["optical_reuse_group_plans"][0]["key_sha256"] = "0" * 64
        return plan

    def forbidden(*args, **kwargs):
        raise AssertionError("A changed group plan must not execute optics.")

    monkeypatch.setattr(optical_raw, "preflight_optical_to_disk_raw", changed_group)
    monkeypatch.setattr(optical_raw, "_strict_raytrace_apply_psf_bounded_roi_provider", forbidden)
    target = tmp_path / "changed_group"
    with pytest.raises(ValueError, match="group plans changed before execution"):
        optical_raw.compute_optical_to_disk_raw(
            provider, sensor, target, **_kwargs(policy), reuse_optical_groups=True,
        )
    assert not target.exists()
    assert not tuple(tmp_path.glob(".opticalraw_tmp_*"))


def test_preflight_refuses_disk_policy_without_optical_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    provider, sensor, policy = _fixture()
    policy = optical_raw.OpticalDiskRawResourcePolicy(
        optical_tile=policy.optical_tile,
        readout=policy.readout,
        max_sensor_tiles=16,
        max_mean_disk_bytes=1,
        max_total_output_bytes=5_000_000,
        max_combined_working_bytes=200_000_000,
        min_free_after_output_bytes=0,
    )
    target = tmp_path / "denied"
    plan = optical_raw.preflight_optical_to_disk_raw(
        provider, sensor, target, **_kwargs(policy)
    )
    assert plan["status"] == "blocked_mean_disk_policy"

    def forbidden(*args, **kwargs):
        raise AssertionError("No optical tile may execute after policy refusal.")

    monkeypatch.setattr(
        optical_raw, "_compute_preflighted_streamed_analytic_offaxis_mean", forbidden
    )
    with pytest.raises(ValueError, match="blocked_mean_disk_policy"):
        optical_raw.compute_optical_to_disk_raw(provider, sensor, target, **_kwargs(policy))
    assert not target.exists()
    assert not tuple(tmp_path.glob(".opticalraw_tmp_*"))


def test_full_height_readout_chunk_refused_before_optical_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    provider, sensor, policy = _fixture()
    target = tmp_path / "full_chunk"
    inputs = {**_kwargs(policy), "row_chunk_rows": 8}
    plan = optical_raw.preflight_optical_to_disk_raw(provider, sensor, target, **inputs)
    assert plan["status"] == "blocked_full_readout_row_chunk_policy"

    def forbidden(*args, **kwargs):
        raise AssertionError("A full-height readout chunk must not execute optics.")

    monkeypatch.setattr(
        optical_raw, "_compute_preflighted_streamed_analytic_offaxis_mean", forbidden
    )
    with pytest.raises(ValueError, match="blocked_full_readout_row_chunk_policy"):
        optical_raw.compute_optical_to_disk_raw(provider, sensor, target, **inputs)
    assert not target.exists()


def test_changed_admitted_plan_spool_fails_before_optical_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    provider, sensor, policy = _fixture()
    original = optical_raw.preflight_optical_to_disk_raw

    def changed_spool(*args, **kwargs):
        sink = kwargs.pop("_admitted_plan_sink")
        return original(
            *args, **kwargs,
            _admitted_plan_sink=lambda encoded: sink(encoded + b" "),
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("A changed admitted plan must not execute optics.")

    monkeypatch.setattr(optical_raw, "preflight_optical_to_disk_raw", changed_spool)
    monkeypatch.setattr(
        optical_raw, "_compute_preflighted_streamed_analytic_offaxis_mean", forbidden
    )
    target = tmp_path / "changed_plan"
    with pytest.raises(ValueError, match="plan spool changed before execution"):
        optical_raw.compute_optical_to_disk_raw(provider, sensor, target, **_kwargs(policy))
    assert not target.exists()
    assert not tuple(tmp_path.glob(".opticalraw_tmp_*"))


def test_complete_coverage_and_scene_halo_are_required(tmp_path: Path):
    provider, sensor, policy = _fixture()
    with pytest.raises(ValueError, match="gap"):
        optical_raw.preflight_optical_to_disk_raw(
            provider, sensor, tmp_path / "gap",
            **_kwargs(policy, tiles=((0, 8, 0, 7),)),
        )
    single = optical_raw.preflight_optical_to_disk_raw(
        provider, sensor, tmp_path / "single",
        **_kwargs(policy, tiles=((0, 8, 0, 8),)),
    )
    assert single["status"] == "blocked_full_sensor_mean_tile_policy"
    sensor = sensor_create("default", {"size": (32, 32), "wave": WAVE.copy()})
    sensor = sensor_set(sensor, "pixel size same fill factor", 1e-6)
    sensor = sensor_set(sensor, "integration time", 0.004)
    sensor = sensor_set(sensor, "auto exposure", False)
    sensor = configure_e2e_sensor(sensor)
    wider_readout = OutOfCoreReadoutPolicy(
        max_sensor_cells=1024,
        max_row_chunk_cells=1024,
        max_transient_bytes=1_000_000,
        max_output_bytes=1_000_000,
        max_resident_input_bytes=1_000_000,
        min_free_after_output_bytes=0,
    )
    wider_policy = optical_raw.OpticalDiskRawResourcePolicy(
        optical_tile=policy.optical_tile,
        readout=wider_readout,
        max_sensor_tiles=16,
        max_mean_disk_bytes=1_000_000,
        max_total_output_bytes=5_000_000,
        max_combined_working_bytes=200_000_000,
        min_free_after_output_bytes=0,
    )
    plan = optical_raw.preflight_optical_to_disk_raw(
        provider, sensor, tmp_path / "edge",
        **_kwargs(wider_policy, tiles=((0, 16, 0, 32), (16, 32, 0, 32))),
    )
    assert plan["status"] == "blocked_sensor_tile_preflight"
    assert "halo" in plan["reason"] or "boundary" in plan["reason"]
    assert not (tmp_path / "edge").exists()


def test_partial_tile_failure_removes_only_this_calls_temporary_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    provider, sensor, policy = _fixture()
    original = optical_raw._compute_preflighted_streamed_analytic_offaxis_mean
    calls = 0

    def failing_second_tile(*args, **kwargs):
        nonlocal calls
        calls += 1
        values, evidence = original(*args, **kwargs)
        if calls == 2:
            values[0, 0] = np.nan
        return values, evidence

    monkeypatch.setattr(
        optical_raw, "_compute_preflighted_streamed_analytic_offaxis_mean", failing_second_tile
    )
    target = tmp_path / "failed"
    with pytest.raises(ValueError, match="invalid mean electrons"):
        optical_raw.compute_optical_to_disk_raw(
            provider, sensor, target, **_kwargs(policy)
        )
    assert calls == 2
    assert not target.exists()
    assert not tuple(tmp_path.glob(".opticalraw_tmp_*"))
