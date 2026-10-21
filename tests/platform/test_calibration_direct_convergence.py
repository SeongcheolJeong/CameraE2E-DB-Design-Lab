"""Direct optical grid decimation, phase-preserving edge convolution and gates."""

from copy import deepcopy

import numpy as np
import pytest
from scipy.signal import fftconvolve

from camera_platform import calibration_direct_convergence as d
from camera_platform import calibration_direct_optics as optics
from camera_platform import calibration_lab_engine as lab
from camera_platform import reference_isp


def kernel():
    k = np.zeros((5, 5, 3))
    for ch in range(3):
        k[2, 2, ch] = 4 + ch
        k[1, 3, ch] = 2
        k[3, 1, ch] = 1
        k[4, 2, ch] = 0.5
    return k / k.sum(axis=(0, 1))


def geometry():
    c = {"id": "crop-ne", "origin_row_col": [64, 1744], "center_active_row_col": [127.5, 1807.5]}
    return {
        "full_sensor_shape": [1097, 1937],
        "white_e_rgb": [300.0, 400.0, 200.0],
        "incident_photons_per_pixel": 8000.0,
    }, c


@pytest.mark.parametrize("q", [1, 2, 4, 8])
@pytest.mark.parametrize("angle", [0, 90])
@pytest.mark.parametrize("phase", [0.0, 0.25])
def test_projected_cdf_matches_direct_asymmetric_2d_convolution(q, angle, phase):
    g, c = geometry()
    k = kernel()
    cdf, offset = d.projection_cdf(k, angle)
    setting = {"samples_per_pixel": q, "radius_native_px": 8}
    actual, origin = d.edge_charge(g, c, setting, angle, phase, cdf, offset)
    n = 144
    radius = 2
    # Independently render the infinite physical slanted edge, with kernel halo.
    coords = (np.arange(-radius, n * q + radius) + 0.5) / q - 0.5
    y = (origin[0] + coords - c["center_active_row_col"][0])[:, None]
    x = (origin[1] + coords - c["center_active_row_col"][1])[None, :]
    # Use rational integer threshold, including exact boundary equality.
    across = x - 0.07 * y if angle == 0 else y + 0.07 * x
    scene = 0.1 + 0.7 * (across > phase + 1e-12)
    expected = []
    for ch in range(3):
        convolved = fftconvolve(scene, k[..., ch], mode="valid")
        expected.append(
            convolved.reshape(n, q, n, q).mean(axis=(1, 3)) * g["white_e_rgb"][ch] * d.PHOTON_SCALE
        )
    from camera_platform.calibration_unified_engine import remosaic

    np.testing.assert_allclose(actual, remosaic(np.stack(expected, axis=-1)), atol=1e-10, rtol=0)


def test_exact_global_crop_and_isp_halo_parity_for_direct_asymmetric_psf():
    g, c = geometry()
    p = lab.default_profile()
    profile = {
        "fields": {"center": p},
        "electronics": p["electronics"],
        "conditions": {**p["conditions"], "exposure_ms": 1.0},
        "isp": {"electron_to_output_matrix": np.diag([1 / 4000] * 3).tolist()},
        "isp_tuning": reference_isp.default_profile(),
    }
    for angle in (0, 90):
        cdf, offset = d.projection_cdf(kernel(), angle)
        a = d.capture(profile, g, c, d.setting(4, 8), angle, 0.25, cdf, offset)
        b = d.capture(profile, g, c, d.setting(4, 8), angle, 0.25, cdf, offset, extra=16)
        np.testing.assert_array_equal(a[0], b[0])
        np.testing.assert_array_equal(a[1], b[1])
        assert a[3]["geometry"] == {"full_sensor_shape": [1097, 1937], "crop_origin": [64, 1744]}
        replay = reference_isp.process(
            a[2],
            {**profile["electronics"], "gain": p["conditions"]["gain"]},
            profile["isp"]["electron_to_output_matrix"],
            profile["isp_tuning"],
        )[0]
        np.testing.assert_array_equal(a[1], replay[8:-8, 8:-8])


def test_point_psf_decimation_never_upsamples_native_pixel_integrated_source(monkeypatch):
    monkeypatch.setattr(optics, "REFERENCE_RADIUS", 8)
    axis = np.arange(-128, 129) / 16
    mass = np.exp(-((axis[:, None] - 0.3) ** 2 + (axis[None, :] + 0.2) ** 2) / 2)[..., None]
    mass = np.repeat(mass, 3, axis=-1).astype(np.float32) / 16**2
    original = mass.copy()
    for q in (1, 2, 4, 8, 16):
        actual = d.sampled_kernel(mass, d.setting(q, 8))
        step = 16 // q
        np.testing.assert_array_equal(actual, original[::step, ::step] * step**2)
    np.testing.assert_array_equal(mass, original)
    with pytest.raises(ValueError, match="reference grid"):
        d.sampled_kernel(mass[::16, ::16], d.setting(4, 8))


def curve(sigma=0.3, end=0.25):
    x = np.linspace(0, end, 100)
    return {"x": x.tolist(), "y": np.exp(-2 * np.pi**2 * sigma**2 * x * x).tolist()}


def test_out_of_band_mtf50_is_explicit_but_curve_gate_is_not_relaxed():
    result = d.compare_curve(curve(), curve())
    assert result["passed"] and result["mtf50"] is None
    assert result["reason"] == "both_mtf50_above_measurable_band"
    assert not d.compare_curve(curve(0.4), curve(0.3))["passed"]
    bad = curve()
    bad["y"][10] = float("nan")
    assert not d.compare_curve(bad, curve())["passed"]
    assert not d.compare_curve(curve(end=0.2), curve())["passed"]


def test_finite_reference_energy_loss_and_unmeasured_fields_fail_selection():
    field = {
        "status": "measured",
        "energy_rgb": [1.0, 1.0, 1.0],
        "kernel_curves": {"G-0": curve()},
        "captures": {"edge": {"curves": {"raw": curve(), "isp": curve()}}},
    }
    reference = {"fields": {"crop-center": field}}
    assert d.compare(reference, reference)["passed"]
    current = deepcopy(reference)
    current["fields"]["crop-center"]["energy_rgb"][1] = 0.998
    assert not d.compare(current, reference)["passed"]
    current = {
        "fields": {
            "crop-center": {
                "status": "refused",
                "reason": "optical_intensity_nyquist_not_satisfied",
            }
        }
    }
    assert not d.compare(current, reference)["passed"]


def test_geometry_rejects_interpolated_nodes_and_wrong_pinned_digest(monkeypatch):
    monkeypatch.setattr(optics, "backend", lambda: {"version": "test"})
    g = {
        "schema": optics.SCHEMA,
        "backend": {"version": "test"},
        "wavelength_nm": np.arange(450.0, 651.0, 10.0).tolist(),
        "full_sensor_shape": [1097, 1937],
        "crops": [
            {"id": str(i), "origin_row_col": [64, 64], "shape_row_col": [128, 128]}
            for i in range(9)
        ],
    }
    g["geometry_sha256"] = optics.geometry_digest(g)
    optics.validate(g)
    g["crops"][0]["node"] = {"family": "gaussian"}
    with pytest.raises(ValueError, match="geometry changed"):
        optics.validate(g)
    g["geometry_sha256"] = optics.geometry_digest(g)
    with pytest.raises(ValueError, match="interpolated"):
        optics.validate(g)
