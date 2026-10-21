"""Independent convolution, interpolation and global Bayer/ISP boundary checks."""

from copy import deepcopy

import numpy as np
import pytest
from scipy.signal import fftconvolve

from camera_platform import calibration_lab_engine as lab
from camera_platform import calibration_spatial_psf as s
from camera_platform import reference_isp


def application(q=2):
    names = [["nw", "n", "ne"], ["w", "center", "e"], ["sw", "s", "se"]]
    return {
        "anchors": {
            "crop-" + names[i][j]: [y, x]
            for i, y in enumerate((10.0, 50.5, 90.0))
            for j, x in enumerate((10.0, 50.5, 90.0))
        },
        "triangles": deepcopy(s.TRIANGLES),
        "setting": {"samples_per_pixel": q, "radius_native_px": 2},
        "optical_halo_native_px": 3,
    }


def bank():
    result = {}
    for i, f in enumerate(application()["anchors"]):
        a = np.zeros((5, 5, 3), np.float32)
        a[2, 2] = [4, 5, 6]
        a[1, 3] = 1 + i / 10
        a[4, 1] = 0.3
        result[f] = a
    return result


def test_native_coordinate_basis_anchors_clamp_and_partition():
    a = application()
    a["anchors"]["crop-center"] = [51, 51]
    w = s.weights(a, (-20, 121, -30, 131))
    total = sum(w.values())
    np.testing.assert_allclose(total, 1, rtol=0, atol=3e-16)
    assert all(v.min() >= 0 for v in w.values())
    assert np.max(sum(v > 1e-12 for v in w.values())) <= 3
    for k, (y, x) in a["anchors"].items():
        if y == int(y) and x == int(x):
            assert s.weights(a, (int(y), int(y) + 1, int(x), int(x) + 1))[k][0, 0] == pytest.approx(
                1
            )
    assert w["crop-nw"][0, 0] == 1 and w["crop-se"][-1, -1] == 1
    a["triangles"] = a["triangles"][:-2]
    with pytest.raises(ValueError, match="gap"):
        s.weights(a, (0, 100, 0, 100))


@pytest.mark.parametrize("q", [1, 2, 4, 8])
def test_overlap_save_matches_independent_linear_convolution_and_one_aperture(q):
    a = application(q)
    b = bank()
    rng = np.random.default_rng(27)
    scene = s.NativeRaster(rng.random((110, 110, 3)))
    bounds = (13, 44, 17, 52)
    actual = s.convolve_tile(a, b, s.FFTCache(b), scene, bounds, 1, fixed="crop-ne")
    y0, y1, x0, x1 = bounds
    h = a["optical_halo_native_px"]
    large = scene((y0 - h, y1 + h, x0 - h, x1 + h), q, 1)
    k = b["crop-ne"][..., 1].astype(float)
    expected = fftconvolve(large, k / k.sum(), mode="same")[
        h * q : (h + y1 - y0) * q, h * q : (h + x1 - x0) * q
    ]
    expected = expected.reshape(y1 - y0, q, x1 - x0, q).mean((1, 3))
    np.testing.assert_allclose(actual, expected, atol=1e-14, rtol=0)


def test_spatial_superposition_matches_independent_basis_and_tile_partition():
    a = application()
    b = bank()
    rng = np.random.default_rng(13)
    scene = s.NativeRaster(rng.random((105, 103, 3)))
    bounds = (0, 105, 0, 103)
    weights = s.weights(a, bounds)
    expected = np.zeros((105, 103, 3))
    for fid, w in weights.items():
        fixed, _ = s.optical_window(a, b, scene, bounds, tile=64, fixed=fid)
        expected += w[..., None] * fixed
    for tile in (32, 64, 96):
        value, _ = s.optical_window(a, b, scene, bounds, tile=tile)
        np.testing.assert_allclose(value, expected, atol=2e-14, rtol=0)
    constant = s.NativeRaster(np.full((105, 103, 3), 0.37))
    dc, _ = s.optical_window(a, b, constant, bounds, tile=32)
    np.testing.assert_allclose(dc, 0.37, atol=2e-15, rtol=0)


def profile():
    p = lab.default_profile()
    return {
        "fields": {"center": p},
        "electronics": p["electronics"],
        "conditions": {**p["conditions"], "exposure_ms": 1},
        "isp": {"electron_to_output_matrix": np.diag([1 / 4000] * 3).tolist()},
        "isp_tuning": reference_isp.default_profile(),
    }


def test_whole_sensor_odd_boundary_cfa_and_isp_tiling_match_extended_global_reference():
    p = profile()
    rng = np.random.default_rng(2)
    planes = rng.random((105, 103, 3)) * 900
    raw = s.electrons_to_raw(p, planes)
    assembled = np.zeros_like(raw)
    for y0, y1, x0, x1 in s.tiles((0, 105, 0, 103), 32):
        assembled[y0:y1, x0:x1] = s.electrons_to_raw(p, planes[y0:y1, x0:x1], (y0, x0))
    np.testing.assert_array_equal(raw, assembled)
    extent = 16
    yr = s.reflected_indices(-extent, 106 + extent, 105)
    xr = s.reflected_indices(-extent, 104 + extent, 103)
    global_linear = reference_isp.process(
        raw[yr[:, None], xr[None, :]],
        {**p["electronics"], "gain": p["conditions"]["gain"]},
        p["isp"]["electron_to_output_matrix"],
        p["isp_tuning"],
    )[0][extent : extent + 105, extent : extent + 103]
    result = np.empty((105, 103, 3))
    for b in s.tiles((0, 105, 0, 103), 32):
        y0, y1, x0, x1 = b
        result[y0:y1, x0:x1] = s.isp_window(p, raw, b)
    np.testing.assert_array_equal(result, global_linear)


def test_resource_and_input_bounds_are_enforced():
    a = application(8)
    a["setting"]["radius_native_px"] = 64
    a["optical_halo_native_px"] = 65
    assert s.estimate(a, 128)["maximum_array_working_bytes_estimate"] < s.MAX_ARRAY_BYTES
    for t in (31, 33, 194, 1000):
        with pytest.raises(ValueError, match="Tile"):
            s.estimate(a, t)
    with pytest.raises(ValueError, match="nonnegative"):
        s.NativeRaster(np.full((5, 5, 3), -1.0))
    c = s.FFTCache(bank(), budget=5)
    with pytest.raises(ValueError, match="exceeds"):
        c.kernel("crop-nw", 0, (10, 10))


@pytest.mark.parametrize("angle", [0, 90])
@pytest.mark.parametrize("phase", [0.0, 0.25])
def test_continuous_edge_fft_preserves_prior_rational_phase_at_q8(angle, phase):
    from camera_platform import calibration_direct_convergence as direct

    a = application(8)
    b = bank()
    crops = []
    for i, y in enumerate((64, 484, 904)):
        for j, x in enumerate((64, 904, 1744)):
            name = [["nw", "n", "ne"], ["w", "center", "e"], ["sw", "s", "se"]][i][j]
            crops.append(
                {
                    "id": "crop-" + name,
                    "origin_row_col": [y, x],
                    "center_active_row_col": [y + 63.5, x + 63.5],
                }
            )
    g = {"crops": crops, "white_e_rgb": [300.0, 400.0, 200.0], "full_sensor_shape": [1097, 1937]}
    crop = crops[2]
    scene = s.EdgeAtlas(g, angle, phase)
    cdf, offset = direct.projection_cdf(b["crop-ne"], angle)
    expected, origin = direct.edge_charge(g, crop, a["setting"], angle, phase, cdf, offset)
    y, x = origin
    values, _ = s.optical_window(a, b, scene, (y, y + 144, x, x + 144), fixed="crop-ne")
    values *= np.array(g["white_e_rgb"]) * direct.PHOTON_SCALE
    from camera_platform.calibration_unified_engine import remosaic

    np.testing.assert_allclose(remosaic(values), expected, atol=1e-10, rtol=0)


def test_active_spatial_profile_binds_renderer_and_validated_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(s.optics, "validate", lambda g: None)
    p = profile()
    g = {
        "geometry_sha256": "g",
        "crops": [
            {"id": k, "optical_anchor_active_row_col": v}
            for k, v in application()["anchors"].items()
        ],
        "numerical_application": {
            "status": "passed",
            "policy": s.direct.POLICY,
            "selected": {"setting": {"samples_per_pixel": 8, "radius_native_px": 64}},
            "capture_photons_per_pixel": 32000,
            "kernels": [],
        },
    }
    p["sensor_crops"] = g
    p["profile_sha256"] = s._hash(p)
    a = s.contract(p)
    frozen = s.apply(p, {"application": a, "status": "passed"})
    calls = []
    monkeypatch.setattr(s, "capture_full", lambda *args: calls.append(args) or {"ok": True})
    assert (
        s.capture_applied(frozen, tmp_path, object(), tmp_path / "out") == {"ok": True}
        and len(calls) == 1
    )
    for mutate in ("renderer", "validation", "triangles"):
        bad = deepcopy(frozen)
        if mutate == "renderer":
            bad["spatial_psf"]["renderer_sha256"] = "different"
        elif mutate == "validation":
            bad["spatial_psf"]["validation_status"] = "not_validated"
        else:
            bad["spatial_psf"]["triangles"].pop()
        bad["profile_sha256"] = s._hash(bad)
        with pytest.raises(ValueError, match="renderer differs"):
            s.capture_applied(bad, tmp_path, object(), tmp_path / "bad")
    frozen["electronics"]["dn_per_e"] = 99
    with pytest.raises(ValueError, match="hash differs"):
        s.capture_applied(frozen, tmp_path, object(), tmp_path / "bad")
