"""Boundary coverage, independent probes, bounded kernels and native sensor replay."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_anchor_density as d
from camera_platform import calibration_lab_engine as lab
from camera_platform.registry import digest


def geometry(h=65, w=97):
    return {
        "full_sensor_shape": [h, w],
        "fields": [
            {
                "id": f"a-{r}-{c}",
                "role": "anchor",
                "optical_anchor_active_row_col": [(h - 1) * r / 4, (w - 1) * c / 4],
            }
            for r in range(5)
            for c in range(5)
        ]
        + [
            {
                "id": f"v-{r}-{c}",
                "role": "validation",
                "optical_anchor_active_row_col": [
                    (h - 1) * (2 * r + 1) / 8,
                    (w - 1) * (2 * c + 1) / 8,
                ],
            }
            for r in range(4)
            for c in range(4)
        ],
    }


@pytest.mark.parametrize("n", [3, 5])
def test_full_native_boundary_basis_and_independent_probe_disjointness(n):
    g = geometry()
    anchors, tris = d.topology(g, n)
    a = {"anchors": anchors, "triangles": tris}
    w = d.weights(a, (0, 65, 0, 97))
    np.testing.assert_allclose(sum(w.values()), 1, atol=3e-16, rtol=0)
    assert len(anchors) == n * n and all(
        c["id"] not in anchors for c in g["fields"] if c["role"] == "validation"
    )
    for fid, (y, x) in anchors.items():
        assert d.weights(a, (int(y), int(y) + 1, int(x), int(x) + 1))[fid][0, 0] == pytest.approx(1)
    assert w["a-0-0"][0, 0] == 1 and w["a-4-4"][-1, -1] == 1
    assert all(v.min() >= 0 for v in w.values())
    with pytest.raises(ValueError, match="outside"):
        d.weights(a, (-1, 65, 0, 97))
    with pytest.raises(ValueError, match="outside"):
        d.weights(a, (0, 66, 0, 97))
    # A field-dependent quantity is interpolated all the way to the edge,
    # unlike the previous interior-hull clamp.
    actual = sum(v * (anchors[k][0] + 2 * anchors[k][1]) for k, v in w.items())
    yy, xx = np.indices((65, 97))
    np.testing.assert_allclose(actual, yy + 2 * xx, atol=1e-13)


def test_kernel_bank_eviction_integrity_and_path_escape(tmp_path):
    rows = []
    for i in range(3):
        p = tmp_path / f"k{i}.npz"
        k = np.zeros((1025, 1025, 3), np.float32)
        k[512, 512] = i + 1
        np.savez_compressed(p, mass_rgb=k)
        rows.append({"field": str(i), "file": p.name, "sha256": d.optics.sha(p)})
    one = k.nbytes
    b = d.KernelBank(tmp_path, rows, budget=2 * one)
    for fid in ("0", "1", "2", "0"):
        assert b[fid].shape == k.shape
    assert b.bytes <= 2 * one and b.peak == 2 * one and len(b.data) == 2
    (tmp_path / "k1.npz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="bytes changed"):
        b["1"]
    b = d.KernelBank(tmp_path, [{"field": "bad", "file": "../escape", "sha256": "a" * 64}])
    with pytest.raises(ValueError, match="escapes"):
        b["bad"]


def test_full_capture_odd_edges_global_cfa_and_partition(tmp_path):
    p0 = lab.default_profile()
    p = {
        "fields": {"center": p0},
        "electronics": p0["electronics"],
        "conditions": {"exposure_ms": 10.0, "gain": 1.0},
        "isp": {"electron_to_output_matrix": np.eye(3).tolist()},
        "isp_tuning": d.reference_isp.default_profile(),
        "sensor_crops": {"full_sensor_shape": [65, 97], "white_e_rgb": [100, 200, 300]},
    }
    anchors, tris = d.topology(geometry(), 5)
    a = {
        "anchors": anchors,
        "triangles": tris,
        "setting": {"samples_per_pixel": 2, "radius_native_px": 2},
        "optical_halo_native_px": 3,
    }
    bank = {}
    for i, fid in enumerate(anchors):
        k = np.zeros((9, 9, 3), np.float32)
        k[4, 4] = 1.0
        k[3, 5] = (i + 1) / 100
        bank[fid] = k

    class TestBank(dict):
        peak = 0

    bank = TestBank(bank)
    rng = np.random.default_rng(73)
    scene = d.spatial.NativeRaster(rng.random((65, 97, 3)) * 0.3)
    c1 = d.capture(p, tmp_path, a, bank, scene, tmp_path / "tile32", tile=32)
    c2 = d.capture(p, tmp_path, a, bank, scene, tmp_path / "tile64", tile=64)
    r1 = np.load(tmp_path / c1["raw"])
    r2 = np.load(tmp_path / c2["raw"])
    l1 = np.load(tmp_path / c1["linear"])
    l2 = np.load(tmp_path / c2["linear"])
    np.testing.assert_array_equal(r1, r2)
    np.testing.assert_allclose(l1, l2, rtol=0, atol=1e-12)
    optical, _ = d.spatial.optical_window(a, bank, scene, (0, 65, 0, 97), tile=96)
    expected = d.spatial.electrons_to_raw(p, optical * np.array([400, 800, 1200]))
    np.testing.assert_array_equal(r1, expected)
    np.testing.assert_array_equal(l1, d.spatial.isp_window(p, r1, (0, 65, 0, 97)))
    assert not (tmp_path / "tile32/work-electrons.npy").exists()


def test_applied_profile_refuses_unvalidated_tampered_and_changed_renderer(tmp_path):
    p = {"boundary_spatial_psf": {"validation_status": "not_converged"}}
    p["profile_sha256"] = digest(p)
    with pytest.raises(ValueError, match="not passed"):
        d.capture_applied(p, tmp_path, None, tmp_path / "out")
    p["boundary_spatial_psf"]["validation_status"] = "passed"
    with pytest.raises(ValueError, match="hash changed"):
        d.capture_applied(p, tmp_path, None, tmp_path / "out")


def test_refined_centers_are_anchors_fresh_checks_remain_disjoint_and_fractional():
    g = geometry()
    for c in g["fields"]:
        if c["role"] == "validation":
            c["role"] = "refinement_anchor"
    g["fields"] += [
        {
            "id": f"check-{r}-{c}",
            "role": "validation",
            "optical_anchor_active_row_col": [16 * (r + 0.25), 24 * (c + 0.25)],
        }
        for r in range(4)
        for c in range(4)
    ]
    anchors, tris = d.topology(g, "refined")
    a = {"anchors": anchors, "triangles": tris}
    assert len(anchors) == 41 and len(tris) == 64
    assert not set(anchors) & {c["id"] for c in g["fields"] if c["role"] == "validation"}
    w = d.weights(a, (0, 65, 0, 97))
    np.testing.assert_allclose(sum(w.values()), 1, atol=3e-16, rtol=0)
    assert w["v-0-0"][8, 12] == 1
    # A half-native-row probe must use its exact position rather than truncation.
    bank = {}
    for fid in ("a-0-0", "a-0-1", "v-0-0"):
        k = np.zeros((1025, 1025, 3), np.float32)
        k[512, 512] = 1
        if fid == "v-0-0":
            k[512, 512] = 0
            k[512, 513] = 1
        bank[fid] = k
    k = d.mixture(a, bank, 2.5, 3.75)
    assert k[512, 513, 1] == pytest.approx(2.5 / 8)
    assert k[512, 512, 1] == pytest.approx(1 - 2.5 / 8)


def test_same_optics_new_ccm_uses_separate_numerical_evidence_cache(tmp_path):
    p = {
        "fields": {"center": {}},
        "electronics": {"gain": 1},
        "conditions": {},
        "isp": {"matrix": [1, 2, 3]},
        "isp_tuning": {},
    }
    g = {"base": {"sha256": "a" * 64}, "extra": {"sha256": "b" * 64}}
    root, context = d.numerical_cache_workspace(p, g, tmp_path)
    assert root != tmp_path and context == digest(p)
    for part in g.values():
        d.optics.save(d.source.cache_root(tmp_path, part) / "numerical-profile.json", p)
    assert d.numerical_cache_workspace(p, g, tmp_path) == (tmp_path, digest(p))
    other = deepcopy(p)
    other["isp"]["matrix"] = [1, 2, 4]
    root2, hash2 = d.numerical_cache_workspace(other, g, tmp_path)
    assert root2 != tmp_path and hash2 != context
    # Original retained RAW/ISP inputs stay unchanged.
    assert (
        __import__("json").loads(
            (d.source.cache_root(tmp_path, g["base"]) / "numerical-profile.json").read_text()
        )
        == p
    )


def test_coarse_candidates_keep_previous_failed_probes_when_refined_mesh_uses_them(
    monkeypatch, tmp_path
):
    g = {
        "fields": [
            {"id": "a", "role": "anchor", "optical_anchor_active_row_col": [0, 0]},
            {"id": "v", "role": "refinement_anchor", "optical_anchor_active_row_col": [1, 1]},
            {"id": "check", "role": "validation", "optical_anchor_active_row_col": [0.5, 0.5]},
        ]
    }
    bank = {"v": np.ones((2, 2, 3)), "check": np.ones((2, 2, 3))}
    monkeypatch.setattr(d, "mixture", lambda *args: np.full((2, 2, 3), 0.25))
    monkeypatch.setattr(d, "stationary", lambda *args: {})
    monkeypatch.setattr(d.direct, "compare", lambda *args: {"passed": True, "checks": []})
    coarse = d.local_gate(
        {}, {"grid_size": 5, "anchors": {"a": [0, 0]}}, bank, g, tmp_path, tmp_path, None
    )
    refined = d.local_gate(
        {},
        {"grid_size": "refined", "anchors": {"a": [0, 0], "v": [1, 1]}},
        bank,
        g,
        tmp_path,
        tmp_path,
        None,
    )
    assert {v["field"] for v in coarse["checks"]} == {"v", "check"}
    assert {v["field"] for v in refined["checks"]} == {"check"}
