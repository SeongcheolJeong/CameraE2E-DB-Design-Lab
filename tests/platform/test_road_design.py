"""Road proxy -> native engine parity and decision/statistics boundaries."""

import math
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image
from test_lens_sensor_streamed_offaxis_mean import ANGLES, WAVE, _args, _fixture, _optics, _sensor

from camera_platform.road_design_compute import (
    capture_series,
    fixed_isp,
    roi_capture_sensor,
    well_saturation_mask,
)
from camera_platform.road_design_statistics import choose_candidate, cnr_with_interval
from camera_platform.road_rgb_source import (
    file_sha256,
    make_rgb_strict_geometry_provider,
    pixels_to_tangent,
    tangent_to_pixels,
)
from pyisetcam.optics import oi_compute
from pyisetcam.sensor import sensor_compute_noise_free
from pyisetcam.sensor_capture import resample_sensor_capture
from pyisetcam.types import OpticalImage, Scene


def test_intrinsic_brown_inverse_preserves_source_locations():
    intrinsic = [[1280.0, 0.0, 836.0], [0.0, 1281.0, 475.0], [0.0, 0.0, 1.0]]
    distortion = [-0.115, 0.145, 0.0016, -0.0004, 0.0088]
    x, y = np.meshgrid(np.linspace(-0.3, 0.3, 13), np.linspace(-0.2, 0.2, 9))
    u, v = tangent_to_pixels(x, y, intrinsic, distortion)
    actual_x, actual_y = pixels_to_tangent(u, v, intrinsic, distortion)
    np.testing.assert_allclose(actual_x, x, atol=1e-12)
    np.testing.assert_allclose(actual_y, y, atol=1e-12)


def _rgb_provider(tmp_path):
    rgb = np.zeros((12, 12, 3), np.uint8)
    rgb[..., 0] = np.arange(12)[None, :] * 20
    rgb[..., 1] = np.arange(12)[:, None] * 15
    rgb[..., 2] = 45
    path = tmp_path / "rgb.png"
    Image.fromarray(rgb).save(path)
    native, policy = _fixture()
    provider = make_rgb_strict_geometry_provider(
        _optics(),
        image_path=path,
        expected_sha256=file_sha256(path),
        intrinsic=[[2000.0, 0.0, 5.5], [0.0, 2000.0, 5.5], [0.0, 0.0, 1.0]],
        distortion=[0.0] * 5,
        primary_photons=np.array([[1e13, 0.0], [0.0, 1.2e13], [1e12, 2e12]]),
        gamma_table=np.tile(np.linspace(0, 1, 256)[:, None], (1, 3)),
        scene_shape_row_col=native.global_shape,
        scene_horizontal_fov_deg=native.horizontal_fov_deg,
        wavelength_nm=WAVE,
    )
    return provider, policy


def test_rgb_tiles_equal_one_source_cube_without_brightness_normalization(tmp_path):
    provider, _ = _rgb_provider(tmp_path)
    full = provider.read_scene_tile(0, 32, 0, 32)
    tiles = np.concatenate(
        [provider.read_scene_tile(0, 16, 0, 32), provider.read_scene_tile(16, 32, 0, 32)], axis=0
    )
    np.testing.assert_array_equal(tiles, full)
    doubled = replace(provider, primary_photons=provider.primary_photons * 2)
    np.testing.assert_array_equal(doubled.read_scene_tile(0, 32, 0, 32), full * 2)
    assert provider.metadata["full_spectral_cube_materialized"] is False
    assert provider.linear_rgb.flags.writeable is False


def test_rgb_streamed_offaxis_mean_matches_native_full_scene_path(tmp_path):
    from camera_platform.lens_sensor_streamed_offaxis_mean import (
        compute_streamed_analytic_offaxis_mean,
    )

    provider, policy = _rgb_provider(tmp_path)
    sensor = _sensor(samples=3)
    mean, report = compute_streamed_analytic_offaxis_mean(provider, sensor, **_args(policy))
    scene = Scene(
        fields={
            "wave": WAVE,
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
    native.fields.pop("sensor_output_mode")
    native.fields.pop("sensor_readout")
    capture = sensor_compute_noise_free(native, oi, seed=4)
    full_mean = capture.data["volts"] / capture.fields["pixel"]["conversion_gain_v_per_electron"]
    np.testing.assert_allclose(mean, full_mean[1:5, 4:8], rtol=3e-7, atol=1e-7)
    assert report["optical_execution"]["source_identity"] == provider.source_identity


def test_original_changed_photo_is_refused(tmp_path):
    provider, _ = _rgb_provider(tmp_path)
    path = tmp_path / "rgb.png"
    before = provider.source_description["original_sha256"]
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        make_rgb_strict_geometry_provider(
            _optics(),
            image_path=path,
            expected_sha256=before,
            intrinsic=np.eye(3),
            distortion=[0.0] * 5,
            primary_photons=np.ones((3, 2)),
            gamma_table=np.ones((256, 3)),
            scene_shape_row_col=[32, 32],
            scene_horizontal_fov_deg=0.1,
            wavelength_nm=WAVE,
        )


def test_temporal_cnr_does_not_count_scene_texture_or_fixed_pattern_as_noise():
    rng = np.random.default_rng(2)
    obj = np.zeros((20, 20), bool)
    obj[:, :10] = True
    bg = ~obj
    mean = np.where(obj, 5.0, 3.0)
    noise = rng.normal(0, 0.5, (64, 20, 20))
    plain = mean + noise
    # The same zero-mean texture in each mask and every capture.
    texture = np.tile(np.arange(10) - 4.5, (20, 2))
    a = cnr_with_interval(plain, obj, bg, replicates=100)
    b = cnr_with_interval(plain + texture, obj, bg, replicates=100)
    assert b["cnr"] == pytest.approx(a["cnr"], rel=1e-12)
    assert b["mean_temporal_pixel_variance_object"] == pytest.approx(
        a["mean_temporal_pixel_variance_object"], rel=1e-12
    )


def test_zero_temporal_noise_has_no_fabricated_cnr():
    frames = np.ones((32, 20, 20))
    obj = np.zeros((20, 20), bool)
    obj[:, :10] = True
    with pytest.raises(ValueError, match="undefined"):
        cnr_with_interval(frames, obj, ~obj)


def test_global_fixed_pattern_crops_agree_and_remain_fixed_across_repeat_seeds():
    sensor = _sensor((20, 20))
    sensor.fields["pixel"]["prnu_sigma"] = 0.05
    sensor.fields["pixel"]["dsnu_sigma_v"] = 0.002
    sensor.fields["fixed_pattern_seed"] = 123
    full = roi_capture_sensor(sensor, (0, 20, 0, 20), np.ones((20, 20)) * 200)
    part = roi_capture_sensor(sensor, (4, 12, 6, 14), np.ones((8, 8)) * 200)
    for key in ("offset_fpn_image", "gain_fpn_image"):
        np.testing.assert_array_equal(part.fields[key], full.fields[key][4:12, 6:14])
    first, second = resample_sensor_capture(part, seed=5), resample_sensor_capture(part, seed=6)
    np.testing.assert_array_equal(first.fields["gain_fpn_image"], second.fields["gain_fpn_image"])
    assert np.any(first.data["raw_dn"] != second.data["raw_dn"])


def test_well_mask_replay_matches_native_capture_and_separates_adc():
    sensor = _sensor((20, 20))
    sensor.fields["sensor_readout"]["gain"] = 2.0
    sensor = roi_capture_sensor(sensor, (0, 20, 0, 20), np.ones((20, 20)) * 7000)
    capture = resample_sensor_capture(sensor, seed=7)
    well = well_saturation_mask(sensor, 7)
    assert well.mean() == capture.metadata["sensor_capture"]["saturation"]["well_fraction"]
    assert well.mean() == 0
    assert capture.metadata["sensor_capture"]["saturation"]["adc_white_fraction"] > 0.99


def test_tiled_isp_equals_full_isp_away_from_temporary_tile_edges(monkeypatch):
    import camera_platform.isp as isp

    sensor = _sensor((600, 600))
    raw = np.random.default_rng(1).integers(64, 2000, (600, 600), dtype=np.int32)
    profile = isp.default_profile()
    full, _ = fixed_isp(raw, sensor, (0, 600, 0, 600), profile)
    monkeypatch.setattr(isp, "MAX_FRAME_PIXELS", 300_000)
    tiled, report = fixed_isp(raw, sensor, (0, 600, 0, 600), profile)
    np.testing.assert_allclose(tiled, full, atol=1e-15)
    assert report["native_tiled_bilinear"] is True


def _selection_fixture():
    spec = {
        "baseline_id": "A",
        "images": [
            {"id": "select", "split": "selection", "rois": [{"id": "object"}, {"id": "highlight"}]}
        ],
        "candidates": [{"id": "A"}, {"id": "B"}],
        "capture": {"brightness_multipliers": [0.25, 1.0, 4.0]},
        "selection_rule": {
            "highlight_well_fraction_max": 0.01,
            "highlight_adc_fraction_max": 0.01,
            "contrast_retention_min": 0.95,
        },
    }
    rows = []
    for cid, score in (("A", 1.0), ("B", 1.5)):
        for kind in ("object", "highlight"):
            for brightness in (0.25, 1.0, 4.0):
                rows.append(
                    {
                        "candidate_id": cid,
                        "image_id": "select",
                        "roi_id": kind,
                        "roi_kind": kind,
                        "brightness": brightness,
                        "cnr": score,
                        "cnr_ci95": [score * 0.98, score * 1.02],
                        "bootstrap_cnr": [score] * 20,
                        "well_fraction": 0.0,
                        "adc_fraction": 0.0,
                        "local_contrast": 0.2,
                    }
                )
    return spec, rows


def test_holdout_is_rejected_by_selection():
    spec, rows = _selection_fixture()
    rows[0]["image_id"] = "heldout"
    with pytest.raises(ValueError, match="Held-out"):
        choose_candidate(spec, rows, sampling_ok={"A": True, "B": True})


def test_strong_signal_cannot_override_highlight_or_sampling_failure():
    spec, rows = _selection_fixture()
    for row in rows:
        if row["candidate_id"] == "B" and row["roi_kind"] == "highlight":
            row["well_fraction"] = 0.5
    result = choose_candidate(spec, rows, sampling_ok={"A": True, "B": True})
    assert result["selected_id"] == "A"
    spec, rows = _selection_fixture()
    result = choose_candidate(spec, rows, sampling_ok={"A": True, "B": False})
    assert result["selected_id"] == "A"


def test_incomplete_selection_cannot_produce_a_decision():
    spec, rows = _selection_fixture()
    with pytest.raises(ValueError, match="Incomplete"):
        choose_candidate(spec, rows[:-1], sampling_ok={"A": True, "B": True})


def test_expanding_captures_preserves_original_raw_files(tmp_path, monkeypatch):
    from camera_platform.isp import default_profile
    from camera_platform.road_design_inputs import write_json

    sensor = _sensor((20, 20))
    mask_path = tmp_path / "mask.npz"
    obj = np.zeros((10, 10), bool)
    obj[:, :5] = True
    np.savez(mask_path, object=obj, background=~obj)
    monkeypatch.setattr(
        "camera_platform.road_design_compute.fixed_profile", lambda *a: (default_profile(), {})
    )
    monkeypatch.setattr(
        "camera_platform.road_design_compute.native_mask", lambda *a: np.ones((20, 20), bool)
    )
    coordinates = np.meshgrid(np.linspace(2, 17, 10), np.linspace(2, 17, 10), indexing="ij")
    report = {
        "native_roi_bounds": [0, 20, 0, 20],
        "package_root": str(tmp_path),
        "spectral_proxy": {"primary_photons": [[1, 0], [0, 1], [1, 1]]},
    }
    spec = {
        "capture": {
            "initial_repeats": 4,
            "seed": 52,
            "fixed_pattern_seed": 19,
            "brightness_multipliers": [1.0],
        },
        "statistics": {"bootstrap_replicates": 50},
        "spectral_proxy": {"wavelength_nm": WAVE},
        "resources": {"min_free_disk_bytes": 0},
    }
    mean = np.zeros((20, 20)) + 100
    mean[:, :10] = 200
    roi = {
        "id": "roi",
        "kind": "object",
        "mask_path": "mask.npz",
        "mask_sha256": file_sha256(mask_path),
    }
    output = tmp_path / "capture"
    args = (spec, {"id": "A"}, {"id": "image"}, roi, output)
    kw = {
        "mean": mean,
        "report": report,
        "optics": {},
        "sensor": sensor,
        "coordinates": coordinates,
    }
    first = capture_series(*args, **kw)[0]
    raw = tmp_path / first["raw_file"]
    luminance = tmp_path / first["luminance_file"]
    before_raw, before_y = np.load(raw).copy(), np.load(luminance).copy()
    unchanged = capture_series(*args, **kw, target_repeats={0.25: 8})[0]
    assert unchanged["repeats"] == 4
    expanded = capture_series(*args, **kw, target_repeats={1.0: 8})[0]
    assert expanded["raw_file"] != first["raw_file"] and expanded["repeats"] == 8
    assert file_sha256(raw) == first["raw_sha256"]
    assert file_sha256(luminance) == first["luminance_sha256"]
    np.testing.assert_array_equal(np.load(tmp_path / expanded["raw_file"])[:4], before_raw)
    np.testing.assert_array_equal(np.load(tmp_path / expanded["luminance_file"])[:4], before_y)
    assert capture_series(*args, **kw)[0]["raw_file"] == expanded["raw_file"]
    expanded["profile_sha256"] = "0" * 64
    write_json(output / "light-1" / "capture.json", expanded)
    with pytest.raises(ValueError, match="frozen condition or ISP"):
        capture_series(*args, **kw)


def test_compact_bootstrap_matches_variance_corrected_frame_residual_reference():
    from camera_platform.road_design_statistics import cnr_samples

    rng = np.random.default_rng(12)
    obj = np.zeros((10, 10), bool)
    obj[:, :5] = True
    frames = rng.normal(size=(8, 10, 10)) + obj[None] * 3
    samples, _, _ = cnr_samples(frames, obj, ~obj, replicates=40, seed=77)
    centre = frames.mean(axis=0)
    residual = (frames - centre) * np.sqrt(8 / 7)
    indices = np.random.default_rng(77).integers(0, 8, (40, 8))
    expected = []
    for index in indices:
        boot = centre + residual[index]
        signal = abs(boot[:, obj].mean() - boot[:, ~obj].mean())
        noise = np.sqrt(
            boot[:, obj].var(axis=0, ddof=1).mean() + boot[:, ~obj].var(axis=0, ddof=1).mean()
        )
        expected.append(signal / noise)
    np.testing.assert_allclose(samples[1:], expected, rtol=1e-12, atol=1e-12)
