"""Ghost transport tests independent of the patent dataset."""

import numpy as np
import pytest

from camera_platform import electrical
from camera_platform import reference_ghost as g
from camera_platform import reference_ghost_engine as engine
from camera_platform import reference_ghost_job as job


def slab():
    return {
        "wavelength_nm": [450.0, 550.0, 650.0],
        "surfaces": [
            {
                "id": "front",
                "radius_mm": None,
                "profile": "plane",
                "semi_diameter_mm": 2.0,
                "thickness_mm": 2.0,
                "refractive_index_after": [1.5] * 3,
                "is_stop": False,
            },
            {
                "id": "rear",
                "radius_mm": None,
                "profile": "plane",
                "semi_diameter_mm": 2.0,
                "thickness_mm": 3.0,
                "refractive_index_after": [1.0] * 3,
                "is_stop": False,
            },
        ],
    }


def test_normal_slab_analytic_double_reflection_power_and_position():
    direct, ghost = list(g.trace_bundles(slab(), 0, 12))
    assert ghost["pair"] == [1, 0]
    valid = direct["valid"] & ghost["valid"]
    assert valid.all()
    np.testing.assert_allclose(direct["positions"][valid], ghost["positions"][valid], atol=1e-12)
    # T_front R_rear R_front T_rear. No per-ghost energy normalization.
    np.testing.assert_allclose(
        direct["weights"][valid], np.tile([0.96**2, 0.995**2, 0.98**2], (valid.sum(), 1))
    )
    np.testing.assert_allclose(
        ghost["weights"][valid],
        np.tile([0.96**2 * 0.04**2, 0.995**2 * 0.005**2, 0.98**2 * 0.02**2], (valid.sum(), 1)),
    )


def test_snell_reverse_and_reflection_vector():
    a = np.deg2rad(25)
    d = np.array([[np.sin(a), 0, np.cos(a)]])
    n = np.array([[0.0, 0.0, -1.0]])
    inside, r, tir = g.interaction(d, n, np.array([1.0]), np.array([1.5]))
    reverse, _, _ = g.interaction(-inside, -n, np.array([1.5]), np.array([1.0]))
    np.testing.assert_allclose(reverse, -d, atol=1e-12)
    reflected, _, _ = g.interaction(d, n, np.array([1.0]), np.array([1.5]), True)
    np.testing.assert_allclose(reflected, [[np.sin(a), 0, -np.cos(a)]])
    assert not tir.any() and 0 < r[0] < 1


def test_total_internal_reflection_and_stop_no_ghost():
    d = np.array([[np.sin(np.deg2rad(60)), 0, np.cos(np.deg2rad(60))]])
    _, r, tir = g.interaction(d, np.array([[0, 0, -1.0]]), np.array([1.5]), np.array([1.0]))
    assert tir[0] and r[0] == 1
    p = slab()
    p["surfaces"][1]["is_stop"] = True
    assert len(list(g.trace_bundles(p, 0, 12))) == 1


def test_angle_changes_ghost_displacement_without_phony_shape():
    p = slab()
    p["surfaces"][0]["semi_diameter_mm"] = 10.0
    p["surfaces"][1]["semi_diameter_mm"] = 10.0
    direct, ghost = list(g.trace_bundles(p, 10, 20))
    good = direct["valid"][1] & ghost["valid"][1]
    shift = ghost["positions"][1, good, 0] - direct["positions"][1, good, 0]
    expected = 4 * np.tan(np.arcsin(np.sin(np.deg2rad(10)) / 1.5))
    np.testing.assert_allclose(shift, expected, atol=1e-10)


def test_surface_vector_geometry_matches_shared_asphere():
    from camera_platform.optics_surfaces import radial_derivative, sag

    surface = {
        "radius_mm": 20.0,
        "semi_diameter_mm": 3.0,
        "profile": "even_asphere",
        "asphere": {"conic_constant": -0.2, "orders": [4, 6], "coefficients": [1e-5, -1e-7]},
    }
    x = np.array([0.1, 0.4, 1.0, 2.0])
    y = x * 0.3
    z, gx, gy, ok = g.sag_gradient(surface, x, y)
    np.testing.assert_allclose(z, [sag(surface, float(r)) for r in np.hypot(x, y)])
    np.testing.assert_allclose(
        np.hypot(gx, gy), [radial_derivative(surface, float(r)) for r in np.hypot(x, y)]
    )
    assert ok.all()


def test_raster_power_conservation_linearity_and_no_renormalization():
    xy = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    triangles = np.array([[0, 1, 2], [0, 2, 3]])
    values = np.ones((4, 1)) * 0.02
    area = np.array([0.5e-6, 0.5e-6])
    out, _ = g.raster_triangles(
        xy, triangles, np.ones(4, bool), values, area, [-0.1, 1.1, -0.1, 1.1], (100, 100)
    )
    assert out.sum() * (1.2 / 100) ** 2 * 1e-6 == pytest.approx(0.02e-6, rel=0.015)
    twice, _ = g.raster_triangles(
        xy, triangles, np.ones(4, bool), values * 2, area, [-0.1, 1.1, -0.1, 1.1], (100, 100)
    )
    np.testing.assert_allclose(twice, 2 * out)
    zero, _ = g.raster_triangles(
        xy, triangles, np.ones(4, bool), values * 0, area, [-0.1, 1.1, -0.1, 1.1], (100, 100)
    )
    assert not zero.any()


def test_aperture_clips_instead_of_reweighting_energy():
    p = slab()
    p["surfaces"][1]["semi_diameter_mm"] = 0.5
    direct = next(g.trace_bundles(p, 0, 24))
    assert 0 < direct["valid"].sum() < direct["valid"].size / 4
    assert direct["diagnostics"]["aperture_blocked"] > 0
    np.testing.assert_allclose(direct["weights"][direct["valid"]][:, 0], 0.96**2)


@pytest.mark.parametrize(
    "update",
    [
        {"angles_deg": [31.0]},
        {"angles_deg": [True]},
        {"irradiance_w_m2": [-1.0]},
        {"azimuth_deg": float("nan")},
        {"pupil_samples": 16},
        {"frames": 1},
        {"source": {}},
        {"coatings": ["product_AR"]},
        {"extra": True},
    ],
)
def test_invalid_requests_refused(update):
    s = job.template()
    s.update(update)
    with pytest.raises(ValueError):
        job.specification(s)


def test_native_acquisition_saturates_instead_of_raw_scaling():
    readout = {
        "full_well_e": 10000.0,
        "read_noise_e": 3.0,
        "dark_current_e_per_s": 0.1,
        "gain": 1.0,
        "bit_depth": 12,
        "black_level_dn": 64.0,
    }
    model = electrical.default_electrical_model(readout)
    calibration = {"color": {"matrix": (np.eye(3) / 1000).tolist()}}
    a = engine.acquire(np.full((32, 32), 1000.0), readout, model, calibration, None, 40)
    b = engine.acquire(np.full((32, 32), 1e9), readout, model, calibration, None, 40)
    assert a["raw_frames"].dtype.kind in "iu"
    assert a["raw_frames"].shape == (8, 32, 32)
    assert not np.array_equal(a["raw_frames"][0], a["raw_frames"][1])
    assert b["well_saturated_frames"].all()
    assert b["raw_frames"].max() <= 4095
