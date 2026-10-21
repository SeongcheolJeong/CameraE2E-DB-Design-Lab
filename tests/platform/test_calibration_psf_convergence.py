"""Numerical selection must fail closed and apply the same renderer to holdout."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_psf_convergence as c
from camera_platform import calibration_unified_engine as engine
from camera_platform.registry import digest


def node():
    return {
        "family": "centered_elliptical_gaussian_v1",
        "sigma_x_px": 1.35,
        "sigma_y_px": 1.35,
        "theta_deg": 0.0,
    }


def curve(sigma=1.35, end=0.5):
    x = np.linspace(0, end, 201)
    return {"x": x.tolist(), "y": np.exp(-2 * np.pi**2 * sigma**2 * x * x).tolist()}


def test_reference_sampling_and_band_are_not_hidden_by_interpolation():
    assert c.compare_curve(curve(), curve())["passed"]
    assert not c.compare_curve(curve(end=0.25), curve())["passed"]
    assert not c.compare_curve(curve(0.01), curve(0.01))["passed"]
    wrong = curve()
    wrong["y"][10] = float("nan")
    assert not c.compare_curve(wrong, curve())["passed"]
    wrong = curve()
    wrong["x"][10] = wrong["x"][9]
    assert not c.compare_curve(wrong, curve())["passed"]
    assert not c.compare_curve(curve(1.42), curve())["passed"]


def test_gaussian_kernel_normalization_support_and_pre_normalization_tail():
    n = node()
    for q in (1, 2, 4, 8, 16):
        s = c.setting(q, 4)
        k = c.kernel(n, s)
        cost = c.cost(n, s)
        assert list(k.shape) == cost["kernel_shape"]
        assert abs(k.sum() - 1) < 1e-12
        assert np.array_equal(k, k[::-1, ::-1])
        assert cost["tail_energy_bound"] < c.LIMITS["tail_energy_bound"]
    assert c.cost(n, c.setting(4, 3))["tail_energy_bound"] > c.LIMITS["tail_energy_bound"]
    assert (
        c.cost(n, c.setting(8, 4))["fft_work_proxy"] > c.cost(n, c.setting(4, 4))["fft_work_proxy"]
    )


def test_q4_legacy_render_parity_and_constant_field_energy():
    n = node()
    s = c.setting(4, 5)
    for angle in (0, 90):
        scene = c.edge_scene(s, angle)
        old, _ = engine.core.lab._effective_blur(scene, n)
        assert np.allclose(c.blur(scene, n, s), old, atol=1e-14, rtol=0)
    for q in (1, 2, 4, 8):
        source = np.ones((16 * q, 16 * q, 3)) * 0.37
        actual = c.blur(source, n, c.setting(q, 4))
        assert actual.shape == (16, 16, 3)
        assert np.allclose(actual, 0.37, atol=1e-14)


def test_native_centers_do_not_shift_when_changing_sampling():
    for q in (1, 2, 4, 8, 16):
        high = c.edge_scene(c.setting(q, 4), 0, 0.137)
        native = high[q // 2 :: q, q // 2 :: q]
        reference = c.edge_scene(c.setting(1, 4), 0, 0.137)
        assert np.array_equal(native, reference)


def test_selection_requires_all_fields_and_reference_and_energy():
    def candidate(cost, passed, tail):
        return {
            "fft_work_proxy": cost,
            "setting": c.setting(4, 4),
            "comparison": {"passed": passed},
            "tail_energy_pass": tail,
        }

    valid = candidate(10, True, True)
    rows = [candidate(1, False, True), candidate(2, True, False), candidate(20, True, True), valid]
    assert c.select(rows, True) is valid
    assert c.select(rows, False) is None
    assert c.select(rows[:2], True) is None
    x = curve()
    evidence = {
        "fields": {
            f: {
                "kernel_curves": {"0": deepcopy(x)},
                "captures": {"edge": {"curves": {"raw": deepcopy(x), "isp": deepcopy(x)}}},
            }
            for f in c.core.FIELDS
        }
    }
    bad = deepcopy(evidence)
    bad["fields"]["outer-ne"]["captures"]["edge"]["curves"]["isp"] = curve(1.8)
    result = c.compare(bad, evidence)
    assert not result["passed"]
    assert any(
        not x["passed"] and x["field"] == "outer-ne" and x["stage"] == "isp"
        for x in result["checks"]
    )


def test_fixed_physical_parameters_immutable_when_numerical_settings_selected():
    p = {
        "electronics": {"dn_per_e": 0.46},
        "fields": {"center": {"blur": {"field_nodes": [node()]}}},
    }
    p["profile_sha256"] = digest(p)
    before = deepcopy(p)
    after = c.with_numerics(p, c.setting(4, 4))
    assert p == before
    assert after["electronics"] == p["electronics"] and after["fields"] == p["fields"]
    assert after["profile_sha256"] != p["profile_sha256"]
    assert after["profile_sha256"] == digest(
        {k: v for k, v in after.items() if k != "profile_sha256"}
    )


@pytest.mark.parametrize("q,t", [(True, 4), (3, 4), (32, 4), (4, 0), (4, 4.0), (4, True)])
def test_unbounded_and_ambiguous_settings_rejected(q, t):
    with pytest.raises(ValueError):
        c.setting(q, t)


def test_sampled_source_cannot_be_upsampled_as_a_fake_optical_reference():
    with pytest.raises(ValueError, match="analytic"):
        c.kernel({"family": "sampled_native_kernel_v1"}, c.setting(4, 4))
    n = node()
    n["sigma_x_px"] = float("nan")
    with pytest.raises(ValueError):
        c.kernel(n, c.setting(4, 4))
    n = node()
    n["sigma_x_px"] = 8
    assert c.cost(n, c.setting(16, 8))["working_bytes_estimate"] > c.MAX_WORK_BYTES


def test_selected_setting_is_consumed_by_actual_unified_doses(monkeypatch):
    profile = {
        "psf_numerics": {"setting": c.setting(2, 4)},
        "spectral_response": {"center": np.ones((21, 4))},
        "spectral_dc_gain": {"center": np.ones(21)},
    }
    expected = np.ones((128, 128)) * 234
    seen = []
    monkeypatch.setattr(
        c,
        "image_charge",
        lambda p, prepared, task: seen.append(p["psf_numerics"]["setting"]) or expected,
    )
    result = engine.doses(
        profile, {"spec": {"exposure_ms": 1}}, {"chart": "mtf", "field": "center", "orientation": 0}
    )
    assert seen == [c.setting(2, 4)] and result[1][0] is expected
