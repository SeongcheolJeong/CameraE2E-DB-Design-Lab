"""Engineering trends and unit/temporal semantics for the Virtual Lab."""

import json

import numpy as np
import pytest
from scipy.ndimage import gaussian_filter

from camera_platform import virtual_lab_metrics as lab


def edge(sigma=0.0):
    y, x = np.indices((64, 64))
    image = 0.1 + 0.7 * (x > 32 + (y - 32) / 12)
    return gaussian_filter(image, sigma) if sigma else image


def test_slanted_edge_blur_reduces_sfr_and_preserves_frequency_units():
    sharp = lab.slanted_edge_metrics(edge(1), pixel_pitch_um=2, degrees_per_pixel=0.05)
    soft = lab.slanted_edge_metrics(edge(2), pixel_pitch_um=2, degrees_per_pixel=0.05)
    assert sharp["mtf50_cycles_per_pixel"] > soft["mtf50_cycles_per_pixel"] > 0
    assert sharp["nyquist_response"] > soft["nyquist_response"]
    assert sharp["mtf50_cycles_per_mm"] == pytest.approx(sharp["mtf50_cycles_per_pixel"] / 0.002)
    assert sharp["mtf50_cycles_per_degree"] == pytest.approx(sharp["mtf50_cycles_per_pixel"] / 0.05)
    assert sharp["nyquist_cycles_per_mm"] == 250
    assert sharp["nyquist_cycles_per_degree"] == 10
    assert sharp["sfr"][0] == pytest.approx(1)


def test_sharpen_can_boost_processed_sfr_without_changing_linear_measurement():
    linear = edge(1.5)
    sharpened = linear + 1.5 * (linear - gaussian_filter(linear, 1))
    result = lab.resolution_metrics(linear, sharpened, rois={"center": [0, 64, 0, 64]})
    site = result["sites"]["center"]
    assert (
        site["processed_system"]["mtf50_cycles_per_pixel"]
        > site["linear_system"]["mtf50_cycles_per_pixel"]
    )
    assert (
        site["processed_system"]["overshoot_fraction"] > site["linear_system"]["overshoot_fraction"]
    )
    json.dumps(result, allow_nan=False)


def test_flat_or_tiny_edge_reports_unavailable_and_no_crossing_is_not_a_number():
    assert lab.slanted_edge_metrics(np.ones((32, 32)))["reason"] == "no_dominant_vertical_edge"
    assert lab.slanted_edge_metrics(np.ones((8, 8)))["reason"] == "edge_roi_too_small"
    result = lab.slanted_edge_metrics(edge())
    assert result["mtf50_cycles_per_pixel"] is None
    assert result["mtf50_reason"] == "no_half_response_crossing_below_nyquist"


def test_optical_psf_matches_gaussian_trend_and_paraxial_angular_units():
    y, x = np.indices((81, 81)) - 40
    narrow = np.exp(-(x * x + y * y) / (2 * 2**2))
    broad = np.exp(-(x * x + y * y) / (2 * 4**2))
    first = lab.optical_psf_metrics(
        narrow, sample_pitch_um=0.5, sensor_pitch_um=2, focal_length_mm=5
    )
    second = lab.optical_psf_metrics(
        broad, sample_pitch_um=0.5, sensor_pitch_um=2, focal_length_mm=5
    )
    a, b = first["axes"]["horizontal"], second["axes"]["horizontal"]
    expected = np.sqrt(np.log(2) / (2 * np.pi**2)) / 0.001
    assert a["mtf50_cycles_per_mm"] == pytest.approx(expected, rel=0.01)
    assert a["mtf50_cycles_per_mm"] == pytest.approx(2 * b["mtf50_cycles_per_mm"], rel=0.01)
    assert a["sensor_nyquist_mtf"] > b["sensor_nyquist_mtf"]
    assert a["cycles_per_degree"][10] == pytest.approx(a["cycles_per_mm"][10] * 5 * np.pi / 180)
    assert second["rms_radius_um"] > first["rms_radius_um"]
    json.dumps(first, allow_nan=False)


def test_optical_psf_does_not_extrapolate_beyond_sampled_nyquist():
    result = lab.optical_psf_metrics(np.ones((3, 3)), sample_pitch_um=4, sensor_pitch_um=1)
    assert result["axes"]["horizontal"]["sensor_nyquist_mtf"] is None


def color_fixture():
    rng = np.random.default_rng(11)
    colors = rng.uniform(0.05, 0.7, (24, 3))
    colors[18:] = np.linspace(0.8, 0.05, 6)[:, None]
    image = np.zeros((40, 60, 3))
    rois = []
    for i, rgb in enumerate(colors):
        row, col = divmod(i, 6)
        image[row * 10 : (row + 1) * 10, col * 10 : (col + 1) * 10] = rgb
        rois.append([row * 10 + 2, row * 10 + 8, col * 10 + 2, col * 10 + 8])
    return image, colors @ lab.LINEAR_SRGB_TO_XYZ.T, rois


def test_color_identity_and_fixed_exposure_or_wb_error_are_not_fitted_away():
    image, reference, rois = color_fixture()
    white = lab.LINEAR_SRGB_TO_XYZ @ np.ones(3)
    perfect = lab.color_metrics(image, reference, patch_rois=rois, white_xyz=white)
    dimmed = lab.color_metrics(image * 0.5, reference, patch_rois=rois, white_xyz=white)
    cast = lab.color_metrics(image * [1.2, 1, 0.8], reference, patch_rois=rois, white_xyz=white)
    assert perfect["delta_e00_mean"] < 1e-10
    assert dimmed["delta_e00_mean"] > 5
    assert cast["neutral_xy_error_mean"] > 0.01
    assert "none" in perfect["fitting"]
    json.dumps(cast, allow_nan=False)


def test_temporal_noise_excludes_static_texture_and_spatial_fixed_pattern():
    y, x = np.indices((32, 48))
    textured = 100 + x * 2 + y
    frames = np.broadcast_to(textured, (8, 32, 48))
    result = lab.temporal_noise_metrics(frames, unit="e-")
    assert result["temporal_variance"] == 0
    assert result["spatial_std_of_temporal_mean"] > 10
    assert result["fixed_pattern_and_shading_std_estimate"] > 10
    assert result["snr_db"] is None
    json.dumps(result, allow_nan=False)


def test_temporal_noise_known_repeat_offsets_and_gain_invariance():
    offsets = np.arange(8) - 3.5
    frames = np.broadcast_to(100 + offsets[:, None, None], (8, 32, 32))
    base = lab.temporal_noise_metrics(frames, unit="e-")
    gain = lab.temporal_noise_metrics(frames * 4 + 64, black_level=64)
    assert base["temporal_variance"] == pytest.approx(np.var(offsets, ddof=1))
    assert gain["temporal_std"] == pytest.approx(4 * base["temporal_std"])
    assert gain["snr_db"] == pytest.approx(base["snr_db"])
    assert gain["mean_signal"] == 400


def test_temporal_noise_denoising_and_more_photons_change_snr_as_expected():
    rng = np.random.default_rng(17)
    low = rng.poisson(50, (8, 64, 64)).astype(float)
    high = rng.poisson(200, (8, 64, 64)).astype(float)
    denoised = gaussian_filter(low, (0, 1, 1))
    low_result = lab.temporal_noise_metrics(low)
    assert lab.temporal_noise_metrics(high)["snr_db"] > low_result["snr_db"] + 5
    assert lab.temporal_noise_metrics(denoised)["temporal_std"] < low_result["temporal_std"]


def test_temporal_noise_rgb_roi_keeps_repeat_axis_and_frame_count():
    frames = np.broadcast_to(np.arange(8)[:, None, None, None], (8, 16, 16, 3)) + 100
    result = lab.temporal_noise_metrics(frames, roi=[2, 10, 4, 12])
    assert result["repeat_count"] == 8
    assert result["pixel_count"] == 64
    assert result["temporal_variance"] == pytest.approx(
        np.var(np.arange(8), ddof=1) * lab.LUMA.sum() ** 2
    )
    assert lab.temporal_noise_metrics(frames[:1])["status"] == "unavailable"


def test_tone_distinguishes_well_capacity_from_gain_driven_adc_clipping():
    electrons = np.array([[100, 200], [400, 500], [900, 1200]])
    raw = np.clip(electrons * 2, 0, 1023)
    result = lab.tone_metrics([0.25, 1, 4], electrons, raw, full_well_e=1000, adc_white=1023)
    assert result["response"][0]["well_saturation_fraction"] == 0
    assert result["response"][2]["well_saturation_fraction"] == 0.5
    assert result["response"][2]["adc_saturation_fraction"] == 1


def test_five_sites_and_flat_shading_are_explicit_and_serializable():
    rois = lab.five_site_rois((360, 640))
    assert len(rois) == 5 and "center" in rois
    result = lab.shading_metrics(np.ones((360, 640)))
    assert all(row["relative_to_center"] == 1 for row in result["sites"].values())
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [np.zeros((3, 3)), np.full((3, 3), -1), np.full((3, 3), np.nan)])
def test_bad_psf_is_rejected(bad):
    with pytest.raises(ValueError):
        lab.optical_psf_metrics(bad, sample_pitch_um=1)
