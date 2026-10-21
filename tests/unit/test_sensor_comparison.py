from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from pyisetcam.assets import AssetStore
from pyisetcam.ip import ip_compute, ip_create
from pyisetcam.sensor import sensor_create, sensor_set
from pyisetcam.sensor_capture import configure_e2e_sensor, resample_sensor_capture
from pyisetcam.sensor_comparison import (
    _fixed_isp_matrices,
    _linear_isp,
    _Moments,
    _physical_geometry,
    _physical_resample,
    _uncertainty_summary,
    _write_report,
    camerae2e_compare_sensors,
    sensor_comparison_assumptions,
)


def test_assumption_sweep_is_baseline_eight_oat_and_two_corners():
    cases = sensor_comparison_assumptions()
    assert len(cases) == 11
    assert len({item["name"] for item in cases}) == 11
    baseline = cases[0]
    for item in cases[1:9]:
        assert sum(item[key] != baseline[key] for key in baseline if key != "name") == 1
    assert cases[-2]["read_noise_e"] == 3
    assert cases[-1]["effective_efficiency"] == 0.5
    assert sensor_comparison_assumptions(False) == [baseline]


def test_physical_crop_coordinates_match_different_native_pitches():
    geometry = _physical_geometry([1e-6, 2e-6], (16, 16))
    assert geometry["native_sizes"] == [[36, 36], [20, 20]]
    output_x = (np.arange(16) - 7.5) * 2e-6
    images = []
    for pitch, size in zip([1e-6, 2e-6], geometry["native_sizes"], strict=True):
        native_x = (np.arange(size[1]) - (size[1] - 1) / 2) * pitch
        image = np.broadcast_to(native_x, size)
        images.append(_physical_resample(image, pitch, geometry["common_extent_m"], (16, 16)))
        np.testing.assert_allclose(images[-1], np.broadcast_to(output_x, (16, 16)), atol=1e-9)
    # Reflect padding shifts extreme output samples by less than 0.001 output pixel.
    np.testing.assert_allclose(images[0], images[1], atol=1e-9)


def test_temporal_moments_ignore_scene_texture_and_use_sample_variance():
    texture = np.arange(30).reshape(5, 6) * 100.0
    images = [texture + offset for offset in [1.0, 3.0, 5.0]]
    moments = _Moments()
    for image in images:
        moments.add(image)
    np.testing.assert_allclose(moments.mean, texture + 3)
    np.testing.assert_allclose(moments.variance, 4)


def test_common_output_noise_is_propagated_through_resampling():
    rng = np.random.default_rng(81)
    moments = _Moments()
    for _ in range(128):
        native = rng.normal(0, 1, (36, 36))
        moments.add(_physical_resample(native, 1e-6, [32e-6, 32e-6], (16, 16)))
    assert 0.04 < np.mean(moments.variance) < 0.3


def test_cached_comparison_isp_matches_actual_digital_ip():
    store = AssetStore.default()
    sensor = sensor_set(sensor_create(asset_store=store), "size", [16, 16])
    sensor = configure_e2e_sensor(sensor, readout={"gain": 2, "black_level_dn": 128})
    sensor.data["mean_electrons"] = np.linspace(0, 5000, 256).reshape(16, 16)
    sensor = resample_sensor_capture(sensor, seed=10)
    ip = ip_create(sensor=sensor, asset_store=store)
    matrices = _fixed_isp_matrices(sensor, ip, store)
    fast_rgb, fast_xyz = _linear_isp(sensor, sensor.data["raw_dn"], matrices)
    actual = ip_compute(ip, sensor, asset_store=store)
    np.testing.assert_allclose(fast_rgb, actual.data["result"], rtol=1e-12, atol=1e-14)
    np.testing.assert_allclose(fast_xyz, actual.data["xyz"], rtol=1e-12, atol=1e-14)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"nframes": 1},
        {"seed": -1},
        {"luminances": [float("nan")]},
        {"exposures": [0]},
        {"output_size": (8.5, 16)},
        {"scene_types": ("missing",)},
        {"readout": {"full_well_e": 1234}},
    ],
)
def test_invalid_comparison_scope_is_rejected(kwargs):
    with pytest.raises(ValueError):
        camerae2e_compare_sensors(**kwargs)


def test_shared_assumption_rank_change_is_reported_without_product_claim():
    cases = []
    for assumption, values in [
        ("baseline", [("a", 20), ("b", 21)]),
        ("corner", [("a", 25), ("b", 22)]),
    ]:
        for sensor_id, snr in values:
            cases.append(
                {
                    "scene": "uniform",
                    "luminance_cd_m2": 1,
                    "exposure_s": 0.001,
                    "sensor_id": sensor_id,
                    "assumption": assumption,
                    "metrics": {"common_output": {"snr_db": snr}},
                }
            )
    summary = _uncertainty_summary(cases)[0]
    assert summary["ranking_changes_under_shared_assumptions"]
    assert summary["snr_db_assumption_range"]["a"] == [20, 25]
    assert "not confidence intervals" in summary["interpretation"]


def test_small_real_comparison_reuses_optics_and_writes_reviewable_report(tmp_path):
    report = camerae2e_compare_sensors(
        ["DEF-1807-801"],
        nframes=3,
        luminances=(1, 2),
        exposures=(0.001, 0.002),
        scene_types=("uniform",),
        output_size=(16, 16),
        assumption_sweep=False,
        output_dir=tmp_path,
        seed=21,
    )
    assert report["case_count"] == 4
    assert report["optical_solve_count"] == 1
    assert report["sample_count"] == 12
    means = [case["metrics"]["native_raw"]["mean_photoelectrons"] for case in report["cases"]]
    np.testing.assert_allclose(np.array(means) / means[0], [1, 2, 2, 4])
    assert all(
        case["metrics"]["common_output"]["temporal_noise_rms"] > 0 for case in report["cases"]
    )
    assert set(report["cases"][0]["metrics"]["saturation"]) == {
        "well_fraction",
        "adc_white_fraction",
        "adc_zero_fraction",
    }
    loaded = json.loads(Path(report["artifacts"]["json"]).read_text())
    assert loaded["estimate_only"] is True
    assert len(loaded["inventory"]["records"]) == loaded["inventory"]["record_count"]
    assert "model_provenance" in loaded["selected_sensors"][0]
    evaluation = loaded["method"]["color_evaluation"]
    assert evaluation["contract"] == "in_sample_calibration_fit_residual"
    assert evaluation["calibration"]["surface_asset"] == evaluation["evaluation"]["surface_asset"]
    assert evaluation["independent_color_accuracy"] is False
    with np.load(report["artifacts"]["npz"]) as arrays:
        assert arrays["case_0000_rgb_reference"].shape == (16, 16, 3)
        assert arrays["case_0000_rgb_mean"].shape == (16, 16, 3)
        assert arrays["case_0000_luma_variance"].shape == (16, 16)
        raw = arrays["case_0000_raw_dn_frame"]
        assert raw.dtype == np.dtype("int32")
        assert np.all((raw >= 0) & (raw <= 2 ** report["controls"]["readout"]["bit_depth"] - 1))
        assert arrays["case_0000_raw_dn_mean"].dtype == np.dtype("float32")
        assert arrays["case_0000_raw_dn_variance"].dtype == np.dtype("float32")
        assert loaded["method"]["array_dtypes"]["raw_dn_frame"] == "int32"
        assert len(arrays.files) == 4 * 9
    document = Path(report["artifacts"]["html"]).read_text()
    assert "data:image/png;base64," in document
    assert "Estimate only; no measured product ranking" in document
    assert "not independent color accuracy" in document
    array_path = Path(report["artifacts"]["npz"])
    original_hash = hashlib.sha256(array_path.read_bytes()).hexdigest()
    with np.load(array_path) as stored:
        _write_report(loaded, stored, tmp_path, write_arrays=False)
    assert hashlib.sha256(array_path.read_bytes()).hexdigest() == original_hash


def test_mtf50_interpolates_curve_and_resolves_increased_optical_blur():
    from scipy.ndimage import gaussian_filter

    from pyisetcam.sensor_comparison import _edge_metrics

    yy, xx = np.indices((128, 128))
    edge = (xx > 64 + (yy - 64) / 12).astype(float)
    values = []
    for sigma in (1.0, 1.5, 2.0, 3.0):
        image = gaussian_filter(edge, sigma)
        result = _edge_metrics(np.repeat(image[..., None], 3, axis=2))
        assert result["available"]
        values.append(result["mtf50_cycles_per_output_pixel"])
    assert all(0 < value <= 0.5 for value in values)
    assert all(first > second for first, second in zip(values, values[1:], strict=False))
    assert len(set(np.round(values, 4))) == 4


def test_clipped_snr_cannot_win_assumption_ranking():
    cases = [
        {
            "scene": "uniform",
            "luminance_cd_m2": 100,
            "exposure_s": 0.016,
            "sensor_id": "a",
            "assumption": "baseline",
            "metrics": {"common_output": {"snr_db": 999}, "snr_ranking_eligible": False},
        },
        {
            "scene": "uniform",
            "luminance_cd_m2": 100,
            "exposure_s": 0.016,
            "sensor_id": "b",
            "assumption": "baseline",
            "metrics": {"common_output": {"snr_db": 20}, "snr_ranking_eligible": True},
        },
    ]
    result = _uncertainty_summary(cases)[0]
    assert result["orders_by_shared_assumption"] == {}
    assert result["excluded_from_ranking"][0]["sensor_id"] == "a"
    assert not result["ranking_changes_under_shared_assumptions"]
