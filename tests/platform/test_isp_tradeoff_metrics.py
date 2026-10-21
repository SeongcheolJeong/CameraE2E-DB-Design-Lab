"""Independent acceptance-tool metrics: temporal noise, halo and harmonic loss."""

import importlib.util
from pathlib import Path

import numpy as np
import pytest

SPEC = importlib.util.spec_from_file_location(
    "isp_tradeoffs", Path(__file__).resolve().parents[2] / "tools/verify_camera_isp_tradeoffs.py"
)
tradeoffs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tradeoffs)


def rgb(signal_dn):
    return np.repeat(signal_dn[..., None], 3, axis=-1)


def repeats(signal_dn):
    return np.repeat(rgb(signal_dn)[None], tradeoffs.FRAMES, axis=0)


def test_temporal_noise_ignores_fixed_spatial_texture_and_uses_sample_variance():
    y, x = np.indices((tradeoffs.SIDE, tradeoffs.SIDE))
    truth = 500.0 + 3 * x + 2 * y
    frames = repeats(truth)
    fixed = tradeoffs.metrics(frames / (tradeoffs.WHITE - tradeoffs.BLACK), rgb(truth), "flat")
    assert fixed["temporal_residual_std_dn"] == pytest.approx(0, abs=1e-11)
    offsets = np.arange(tradeoffs.FRAMES) - (tradeoffs.FRAMES - 1) / 2
    frames += 3.0 + offsets[:, None, None, None]
    varied = tradeoffs.metrics(frames / (tradeoffs.WHITE - tradeoffs.BLACK), rgb(truth), "flat")
    # Closed form sample variance of consecutive integers 0 ... n-1.
    expected = np.sqrt(tradeoffs.FRAMES * (tradeoffs.FRAMES + 1) / 12)
    assert varied["temporal_residual_std_dn"] == pytest.approx(expected, abs=1e-11)
    assert varied["mean_bias_dn"] == pytest.approx(3.0, abs=1e-11)


def test_edge_halo_uses_averaged_profile_and_keeps_over_under_separate():
    truth = np.full((tradeoffs.SIDE, tradeoffs.SIDE), 400.0)
    truth[:, tradeoffs.SIDE // 2 :] = 1000.0
    observed = truth.copy()
    observed[:, tradeoffs.SIDE // 2 - 1] = 393.0
    observed[:, tradeoffs.SIDE // 2] = 1011.0
    frames = repeats(observed)
    frames += np.tile([-2.0, 2.0], tradeoffs.FRAMES // 2)[:, None, None, None]
    result = tradeoffs.metrics(frames / (tradeoffs.WHITE - tradeoffs.BLACK), rgb(truth), "edge")
    assert result["edge_overshoot_dn"] == pytest.approx(11.0, abs=1e-10)
    assert result["edge_undershoot_dn"] == pytest.approx(7.0, abs=1e-10)
    assert result["texture_amplitude_ratio"] is None


def test_texture_metric_separates_dc_bias_and_phase_from_amplitude_loss():
    y, x = np.indices((tradeoffs.SIDE, tradeoffs.SIDE))
    truth = 700 + 24 * np.sin(2 * np.pi * x / 8) + 12 * np.cos(2 * np.pi * y / 8)
    observed = 707 + 14.4 * np.cos(2 * np.pi * x / 8) + 12 * np.cos(2 * np.pi * y / 8)
    result = tradeoffs.metrics(
        repeats(observed) / (tradeoffs.WHITE - tradeoffs.BLACK), rgb(truth), "texture"
    )
    assert result["mean_bias_dn"] == pytest.approx(7, abs=1e-10)
    assert result["texture_reference_amplitude_dn"] == pytest.approx(24, abs=1e-10)
    assert result["texture_horizontal_amplitude_dn"] == pytest.approx(14.4, abs=1e-10)
    assert result["texture_amplitude_ratio"] == pytest.approx(0.6, abs=1e-10)
    assert result["texture_attenuation_fraction"] == pytest.approx(0.4, abs=1e-10)
    assert result["edge_overshoot_dn"] is None


def test_scalar_reference_recovers_constant_unequal_rgb_sites():
    mosaic = np.tile([[120.0, 320.0], [320.0, 800.0]], (3, 4))
    expected = np.broadcast_to([120.0, 320.0, 800.0], (6, 8, 3))
    np.testing.assert_array_equal(tradeoffs.scalar_bilinear(mosaic), expected)
