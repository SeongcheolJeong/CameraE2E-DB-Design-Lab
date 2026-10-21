"""Native LSC tradeoffs and temporal noise spectra for Reference ISP tuning."""

from copy import deepcopy

import numpy as np
import pytest
from test_reference_isp import execute, matrix, profile, readout

from camera_platform import reference_isp
from camera_platform.reference_isp_lab import (
    Processor,
    temporal_noise_power_spectrum,
    temporal_rgb,
)


def lsc_profile(strength=None, cap=4):
    result = profile()
    result["lsc_enabled"] = True
    if strength is not None:
        result["lsc_tuning"] = {"strength": strength, "gain_cap": cap}
    return result


def test_absent_lsc_tuning_keeps_original_serialized_profile_and_exact_outputs():
    old = lsc_profile()
    assert reference_isp.validate_profile(old) == old
    assert "lsc_tuning" not in reference_isp.default_profile()
    rng = np.random.default_rng(33)
    raw = rng.integers(80, 1200, (32, 34), dtype=np.uint16)
    frozen = rng.uniform(0.8, 4, raw.shape)
    original = frozen.copy()
    frozen.setflags(write=False)
    before = execute(raw, old, lsc_gain=frozen)
    after = execute(raw, lsc_profile(1, 4), lsc_gain=frozen)
    for key in ("linear", "processed", "display", "noise_hint", "lsc_gain"):
        np.testing.assert_array_equal(before[key], after[key])
    np.testing.assert_array_equal(frozen, original)
    np.testing.assert_array_equal(after["frozen_lsc_gain"], original)


@pytest.mark.parametrize("settings", [
    {"strength": -0.1, "gain_cap": 4},
    {"strength": 1.1, "gain_cap": 4},
    {"strength": True, "gain_cap": 4},
    {"strength": 0.5, "gain_cap": 0.9},
    {"strength": 0.5, "gain_cap": 4.1},
    {"strength": 0.5},
    {"strength": 0.5, "gain_cap": 4, "auto": True},
])
def test_lsc_tuning_contract_rejects_invalid_or_ambiguous_settings(settings):
    candidate = lsc_profile()
    candidate["lsc_tuning"] = settings
    with pytest.raises(ValueError):
        reference_isp.validate_profile(candidate)


def test_lsc_strength_cap_once_and_noise_variance_squared_with_signed_signal():
    raw = np.full((32, 34), 300, dtype=np.uint16)
    raw[10, 10] = 32
    frozen = np.tile([[1.2, 2], [3, 4]], (16, 17))
    full = execute(raw, lsc_profile(), lsc_gain=frozen)
    tuned = execute(raw, lsc_profile(0.5, 1.7), lsc_gain=frozen)
    gains = np.minimum(1 + 0.5 * (frozen - 1), 1.7)
    np.testing.assert_array_equal(tuned["lsc_gain"], gains)
    np.testing.assert_allclose(tuned["noise_hint"] / full["noise_hint"], (gains / frozen)**2)
    np.testing.assert_array_equal(
        tuned["stages"]["lsc_normalized"], tuned["stages"]["blc_normalized"] * gains
    )
    assert tuned["stages"]["lsc_normalized"][10, 10] < 0
    assert tuned["effective_lsc"]["effective_max"] == 1.7
    assert tuned["effective_lsc"]["frozen_max"] == 4
    identity = execute(raw, lsc_profile(0), lsc_gain=frozen)
    np.testing.assert_array_equal(identity["stages"]["lsc_normalized"],
                                  identity["stages"]["blc_normalized"])


def test_less_lsc_reduces_brightness_and_temporal_noise_together_not_free_improvement():
    raws = np.random.default_rng(29).integers(240, 280, (8, 32, 34), dtype=np.uint16)
    gains = np.full(raws.shape[1:], 2.)
    full = np.array([execute(raw, lsc_profile(), lsc_gain=gains)["processed"] for raw in raws])
    tuned = np.array([execute(raw, lsc_profile(0.5), lsc_gain=gains)["processed"] for raw in raws])
    mask = np.zeros(gains.shape, bool)
    mask[8:-8, 8:-8] = True
    _, full_noise = temporal_rgb(full, mask)
    _, tuned_noise = temporal_rgb(tuned, mask)
    assert tuned_noise / full_noise == pytest.approx(0.75)
    assert tuned[:, mask].mean() / full[:, mask].mean() == pytest.approx(0.75)


def test_texture_bypass_uses_full_frozen_lsc_for_every_strength_and_cap():
    raw = np.random.default_rng(67).integers(200, 600, (32, 34), dtype=np.uint16)
    gains = np.full(raw.shape, 2.)
    capture = {"calibration": {"matrix": matrix()}}
    full, tuned = lsc_profile(), lsc_profile(0.2, 1.1)
    tuned["raw_denoise"]["strength"] = 0.7
    processor_a = Processor(capture, full, gains)
    processor_b = Processor(capture, tuned, gains)
    original = deepcopy(tuned)
    before = processor_a.bypass(raw, readout(), matrix(), full)
    after = processor_b.bypass(raw, readout(), matrix(), tuned)
    for a, b in zip(before, after, strict=True):
        np.testing.assert_array_equal(a, b)
    assert tuned == original
    assert not np.array_equal(execute(raw, tuned, lsc_gain=gains)["processed"], before[1])


def test_nps_unbiased_parseval_and_fixed_scene_invariance():
    rng = np.random.default_rng(43)
    noise = rng.normal(0, .03, (128, 32, 40))
    frames = np.repeat(noise[..., None], 3, axis=-1)
    mask = np.ones((32, 40), bool)
    result = temporal_noise_power_spectrum(frames, mask)
    assert result["status"] == "ok"
    assert result["frequency_cycles_per_pixel"][0] == 0
    assert result["frequency_cycles_per_pixel"][-1] == .5
    assert result["integrated_2d_nps_variance"] == pytest.approx(
        result["window_weighted_temporal_variance"], rel=1e-14)
    assert result["integrated_2d_nps_variance"] == pytest.approx(.03**2, rel=.03)
    fixed_texture = rng.uniform(-4, 4, (32, 40, 3))
    changed = temporal_noise_power_spectrum(frames + fixed_texture, mask)
    np.testing.assert_allclose(result["radial_power"], changed["radial_power"], rtol=1e-13)
    assert changed["integrated_2d_nps_variance"] == pytest.approx(
        result["integrated_2d_nps_variance"], rel=1e-13)


def test_nps_keeps_temporal_frame_wide_fluctuation_and_reports_source_only_support():
    frames = np.ones((8, 32, 40, 3)) * np.arange(8)[:, None, None, None]
    mask = np.ones((32, 40), bool)
    mask[4:7, 10:13] = False
    result = temporal_noise_power_spectrum(frames, mask)
    r0, r1, c0, c1 = result["roi"]
    assert mask[r0:r1, c0:c1].all()
    assert result["integrated_2d_nps_variance"] == pytest.approx(np.var(np.arange(8), ddof=1))
    assert result["radial_power"][0] > result["radial_power"][-1]
    assert temporal_noise_power_spectrum(frames[:1], mask)["status"] == "unavailable"
    mask[:] = False
    assert (temporal_noise_power_spectrum(frames, mask)["reason"]
            == "no_fully_supported_8x8_rectangle")


def test_uniform_diagnostics_save_nps_brightness_and_effective_lsc(tmp_path):
    tuning = lsc_profile(0.5)
    raws = np.random.default_rng(24).integers(240, 280, (8, 32, 34), dtype=np.uint16)
    capture = {"calibration": {"matrix": matrix()}, "readout": readout()}
    processor = Processor(capture, tuning, np.full((32, 34), 2.))
    outputs = [processor(raw, readout(), matrix(), tuning) for raw in raws]
    linears = [item[0] for item in outputs]
    processed = [item[1] for item in outputs]
    processor.chart("uniform", tmp_path, raws, linears, processed, {})
    assert processor.tests["uniform_mean_luma"] > 0
    assert processor.tests["noise_power_spectrum"]["status"] == "ok"
    assert processor.tests["effective_lsc"]["effective_mean"] == 1.5
    assert (tmp_path / "noise-power-spectrum.json").is_file()
