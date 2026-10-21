"""Reference ISP unit conversion, native sites and retained-RAW invariants."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import isp_v2, reference_isp
from camera_platform.virtual_lab_engine import _process as legacy_process
from pyisetcam.bayer_demosaic import demosaic_bayer
from pyisetcam.ip import (
    _adaptive_laplacian_demosaic,
    _ie_bilinear,
    _laplacian_demosaic,
)


def readout():
    return {
        "bit_depth": 12,
        "black_level_dn": 64,
        "gain": 1.0,
        "full_well_e": 10000.0,
        "read_noise_e": 10.0,
        "dark_current_e_per_s": 10.0,
    }


def profile():
    result = reference_isp.default_profile()
    result["demosaic_method"] = "bilinear"
    return result


def matrix():
    return np.array([[0.0005, -0.0001, 0.0], [-0.00003, 0.0003, -0.0001], [-0.0001, 0.0, 0.0007]])


def execute(raw, tuning=None, **options):
    return reference_isp.process(
        raw, readout(), matrix(), tuning or profile(), return_stages=True, **options
    )


def test_explicit_legacy_migration_preserves_display_not_filter_meaning():
    legacy = {
        "denoise": 0.8,
        "sharpen": 1.2,
        "lsc_enabled": True,
        "wb": [1.2, 1, 0.8],
        "display_white_level": 10,
    }
    p = reference_isp.default_profile(legacy)
    assert p["raw_denoise"]["strength"] == 0
    assert p["linear_sharpen"]["amount"] == 0
    assert p["wb"] == [1.2, 1, 0.8]
    p["wb"][0] = 4
    assert legacy["wb"][0] == 1.2
    assert p["display_white_level"] == 10


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "camera_isp_profile_v2"},
        {"denoise": 0.5},
        {"black_offset_dn": 1},
        {"demosaic_method": "unknown"},
        {"lsc_enabled": 1},
        {"color_calibration_id": ""},
        {"raw_denoise": {"strength": True, "range_floor_dn2": 1}},
        {"raw_denoise": {"strength": 0.5, "range_floor_dn2": 0}},
    ],
)
def test_profile_refuses_ambiguous_or_invalid_contracts(change):
    p = profile()
    p.update(change)
    with pytest.raises(ValueError):
        reference_isp.validate_profile(p)


@pytest.mark.parametrize(
    "method,backend",
    [
        ("bilinear", _ie_bilinear),
        ("laplacian", _laplacian_demosaic),
        ("adaptive_laplacian", _adaptive_laplacian_demosaic),
    ],
)
def test_strict_demosaic_is_exact_existing_backend_and_preserves_signed_input(method, backend):
    mosaic = np.random.default_rng(90).normal(-10, 3, (24, 26))
    before = mosaic.copy()
    mosaic.setflags(write=False)
    pattern = np.array([[1, 2], [2, 3]])
    sites = np.tile(pattern, (12, 13))
    planes = np.zeros((24, 26, 3))
    for c in range(3):
        planes[..., c][sites == c + 1] = mosaic[sites == c + 1]
    expected = backend(planes, pattern if method == "bilinear" else "rggb")
    result = demosaic_bayer(mosaic, method)
    np.testing.assert_array_equal(result, expected)
    np.testing.assert_array_equal(mosaic, before)
    assert result[6:-6, 6:-6].max() < 0


def test_demosaic_does_not_silently_fall_back_or_accept_wrong_native_shape():
    with pytest.raises(ValueError, match="fallback"):
        demosaic_bayer(np.ones((12, 14)), "adaptive_laplacian", cfa_pattern="GBRG")
    for array, method in [
        (np.ones((13, 14)), "bilinear"),
        (np.ones((8, 8)), "laplacian"),
        (np.ones((12, 14)), "nearest"),
    ]:
        with pytest.raises(ValueError):
            demosaic_bayer(array, method)


def test_bypass_matches_legacy_electron_color_units_and_common_display_scale():
    raw = np.random.default_rng(21).integers(20, 800, (24, 26), dtype=np.int32)
    original = raw.copy()
    raw.setflags(write=False)
    p = profile()
    p.update(wb=[1.2, 0.9, 0.8], display_white_level=10)
    p["ccm"] = [[1.1, -0.1, 0], [0, 1, 0], [0, -0.1, 1.1]]
    legacy = {
        "wb": p["wb"],
        "ccm": p["ccm"],
        "denoise": 0,
        "sharpen": 0,
        "tone_gamma": p["tone_gamma"],
    }
    expected, processed, _ = legacy_process(raw, readout(), matrix(), legacy)
    actual = execute(raw, p)
    np.testing.assert_allclose(actual["linear"], expected, rtol=2e-15, atol=2e-16)
    np.testing.assert_allclose(actual["processed"], processed, rtol=2e-15, atol=2e-16)
    np.testing.assert_allclose(
        actual["display"],
        np.clip(processed / 10, 0, 1) ** (1 / p["tone_gamma"]),
        rtol=2e-15,
        atol=2e-16,
    )
    np.testing.assert_array_equal(raw, original)
    assert actual["stages"]["blc_normalized"].min() < 0


def test_lsc_applies_once_to_signed_samples_and_squared_to_noise_hint():
    raw = np.full((20, 22), 200, np.int32)
    raw[6, 6] = 32
    p = profile()
    p["lsc_enabled"] = True
    gains = np.tile([[1.2, 1.5], [2.0, 3.0]], (10, 11))
    before = gains.copy()
    result = execute(raw, p, lsc_gain=gains)
    n = 4095 - 64
    expected_blc = (raw.astype(float) - 64) / n
    np.testing.assert_array_equal(result["stages"]["lsc_normalized"], expected_blc * gains)
    k = n / 10000
    expected_noise = k * np.maximum(raw.astype(float) - 64, 0) + (k * 10) ** 2 + 1 / 12
    np.testing.assert_allclose(result["noise_hint"], expected_noise * gains**2 / n**2, rtol=2e-15)
    np.testing.assert_array_equal(gains, before)
    assert result["stages"]["denoised_normalized"][6, 6] < 0
    with pytest.raises(ValueError):
        execute(raw, p)
    with pytest.raises(ValueError):
        execute(raw, profile(), lsc_gain=gains)


def test_same_site_nr_uses_existing_filter_and_never_blends_cfa_sites():
    p = profile()
    p["raw_denoise"]["strength"] = 0.8
    raw = np.tile([[100, 200], [300, 400]], (12, 13)).astype(np.int32)
    actual = execute(raw, p)
    np.testing.assert_allclose(
        actual["stages"]["denoised_normalized"],
        actual["stages"]["lsc_normalized"],
        rtol=3e-16,
        atol=0,
    )
    raw[10, 10] += 4
    changed = execute(raw, p)
    expected = isp_v2._denoise(
        changed["stages"]["lsc_normalized"][None],
        changed["noise_hint"][None],
        changed["raw_valid_mask"][None],
        0.8,
        1 / 4031**2,
    )[0]
    np.testing.assert_array_equal(changed["stages"]["denoised_normalized"], expected)
    assert (
        changed["stages"]["denoised_normalized"][10, 10]
        < changed["stages"]["lsc_normalized"][10, 10]
    )
    np.testing.assert_array_equal(
        changed["stages"]["denoised_normalized"][1::2],
        actual["stages"]["denoised_normalized"][1::2],
    )


def test_sharpen_uses_shared_bounded_detail_in_display_white_coordinates():
    raw = np.full((24, 26), 100, np.int32)
    raw[:, 13:] = 800
    p = profile()
    p.update(display_white_level=10)
    p["linear_sharpen"] = {"amount": 0.8, "threshold": 0.001, "max_delta": 0.02}
    result = execute(raw, p)
    expected = (
        isp_v2.bounded_detail_delta(
            (result["linear"] / 10).mean(axis=-1)[None],
            result["valid_mask"][None],
            **p["linear_sharpen"],
        )[0]
        * 10
    )
    np.testing.assert_array_equal(result["stages"]["sharpen_delta"], expected)
    assert np.max(np.abs(expected)) <= 0.2
    assert np.any(expected != 0)
    assert not result["valid_mask"][0].any()


def test_rails_keep_original_nr_centers_and_mark_rgb_support_invalid():
    raw = np.full((24, 26), 200, np.int32)
    raw[10, 10], raw[12, 12] = 0, 4095
    p = profile()
    p["raw_denoise"]["strength"] = 1
    result = execute(raw, p)
    for row, col in [(10, 10), (12, 12)]:
        assert (
            result["stages"]["denoised_normalized"][row, col]
            == result["stages"]["lsc_normalized"][row, col]
        )
        assert not result["valid_mask"][row, col]
    assert np.isfinite(result["processed"]).all()


def test_readout_rejects_unsupported_noise_and_noninteger_raw():
    raw = np.full((24, 26), 200, np.int32)
    for change in [
        {"response_type": "nonlinear"},
        {"digital_gain": 2},
        {"fixed_pattern": {"prnu_sigma": 0.1}},
        {"post_gain_noise_v": 0.01},
    ]:
        model = readout()
        model.update(deepcopy(change))
        with pytest.raises(ValueError):
            reference_isp.process(raw, model, matrix(), profile())
    with pytest.raises(ValueError):
        execute(raw.astype(float))


def test_return_tuple_uses_same_unscaled_arrays_as_diagnostics():
    raw = np.full((24, 26), 200, np.int32)
    diagnostics = execute(raw)
    result = reference_isp.process(raw, readout(), matrix(), profile())
    for value, key in zip(result, ["linear", "processed", "display"], strict=True):
        np.testing.assert_array_equal(value, diagnostics[key])
