"""Global chart phase, physical knot mapping, native geometry and halo gates."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import calibration_sensor_crops as c
from camera_platform.registry import digest


def test_raw_observer_preserves_odd_active_dimensions_and_absolute_cfa_phase():
    from camera_platform.calibration_lab_raw import _layout, _plane

    meta = {
        "raw_format": {
            "cfa": "RGGB",
            "response_type": "linear",
            "bit_depth": 12,
            "black_level_dn": 64,
        },
        "geometry": {"full_sensor_shape": [1097, 1937], "crop_origin": [65, 63]},
        "analysis": {"channel": "G1"},
    }
    _, full, origin, channel = _layout(meta, (128, 128))
    assert full == [1097, 1937] and origin == [65, 63]
    values = np.arange(128 * 128).reshape(128, 128)
    assert np.array_equal(_plane(values, channel, origin), values[1::2, 0::2])
    meta["geometry"]["crop_origin"] = [1000, 1800]
    with pytest.raises(ValueError, match="exceeds"):
        _layout(meta, (128, 128))


@pytest.fixture
def bound(monkeypatch):
    dimensions = {
        "optical_axis_active_row_col": [548.0, 968.0],
        "active_shape_row_col": [1097, 1937],
        "pixel_pitch_um": 2.9,
        "dimension_source": {"document_sha256": "pinned"},
        "profile_sha256": "source",
    }
    monkeypatch.setattr(c.timing, "profile", lambda _: dimensions)
    node = {
        "family": "centered_elliptical_gaussian_v1",
        "sigma_x_px": 1.35,
        "sigma_y_px": 1.35,
        "theta_deg": 0.0,
    }
    outer = {**node, "sigma_x_px": 1.8, "sigma_y_px": 1.7, "theta_deg": 45.0}
    p = {
        "pixel_pitch_um": 2.9,
        "fields": {
            fid: {"blur": {"field_nodes": [n]}}
            for fid, n in [("center", node), ("outer-ne", outer)]
        },
        "spectral_dc_gain": {fid: np.ones(21).tolist() for fid in ("center", "outer-ne")},
        "spectral_response": {fid: np.ones((21, 4)).tolist() for fid in ("center", "outer-ne")},
    }
    prepared = {
        "candidate": {
            "source_identities": {
                "sensor": {
                    "revision_id": c.timing.REVISION,
                    "content_sha256": c.timing.REVISION_SHA,
                }
            }
        },
        "optical": {
            "fields": [
                {"id": "center", "image_xy_mm": [0.0, 0.0]},
                {"id": "outer-ne", "image_xy_mm": [5.6, 5.6]},
            ]
        },
    }
    original = deepcopy(p)
    result = c.bind(p, prepared, "unused")
    assert p == original
    return result, prepared


def test_physical_knots_retained_and_only_interior_interpolation(bound):
    p, _ = bound
    g = p["sensor_crops"]
    c.validate(g)
    assert g["knots_image_xy_mm"]["outer-ne"] == [5.6, 5.6]
    assert len(g["crops"]) == 9
    assert all(0 <= crop["interpolation_weight"] < 0.4 for crop in g["crops"])
    center = c.crop_for(p, "crop-center")
    assert center["image_xy_mm"] == [0.0, 0.0]
    assert center["optical_anchor_active_row_col"] != center["center_active_row_col"]
    assert p["fields"]["outer-ne"]["blur"]["field_nodes"][0]["sigma_x_px"] == 1.8
    assert len(c.expand(p)["fields"]) == 11


@pytest.mark.parametrize("q", [1, 2, 4, 8, 16])
def test_global_chart_tile_is_exact_crop_and_sampling_does_not_recenter(bound, q):
    p, _ = bound
    g = p["sensor_crops"]
    for angle in (0, 90):
        large = c.global_chart(g, [36, 36], [184, 184], q, angle, 0.137)
        crop = c.global_chart(g, [64, 64], [128, 128], q, angle, 0.137)
        assert np.array_equal(crop, large[28 * q : 156 * q, 28 * q : 156 * q])
        native = c.global_chart(g, [64, 64], [128, 128], 1, angle, 0.137)
        assert np.array_equal(native, crop[q // 2 :: q, q // 2 :: q])
        shifted = c.global_chart(g, [64, 66], [128, 128], q, angle, 0.137)
        assert np.array_equal(shifted[:, : 126 * q], crop[:, 2 * q :])


def test_geometry_hash_and_global_cfa_bounds_fail_closed(bound):
    p, _ = bound
    g = deepcopy(p["sensor_crops"])
    g["crops"][0]["origin_row_col"] = [65, 64]
    with pytest.raises(ValueError, match="hash"):
        c.validate(g)
    g["geometry_sha256"] = digest({k: v for k, v in g.items() if k != "geometry_sha256"})
    with pytest.raises(ValueError, match="Even"):
        c.validate(g)
    g["crops"][0]["origin_row_col"] = [1090, 64]
    g["geometry_sha256"] = digest({k: v for k, v in g.items() if k != "geometry_sha256"})
    with pytest.raises(ValueError, match="inside"):
        c.validate(g)


def test_unknown_sensor_and_extrapolation_are_not_silently_reanchored(bound):
    p, prepared = bound
    prepared["candidate"]["source_identities"]["sensor"]["revision_id"] = "other"
    with pytest.raises(ValueError, match="pinned"):
        c.bind(p, prepared, "unused")
    node = p["fields"]["center"]["blur"]["field_nodes"][0]
    with pytest.raises(ValueError, match="extrapolate"):
        c.interpolate_node(node, node, 1.1, 0.0, 0.0)
    with pytest.raises(ValueError, match="isotropic"):
        c.interpolate_node({**node, "sigma_y_px": 2.0}, node, 0.1, 0.0, 0.0)


def test_outer_anisotropic_covariance_rotates_without_changing_eigenvalues(bound):
    p, _ = bound
    center, outer = [p["fields"][fid]["blur"]["field_nodes"][0] for fid in ("center", "outer-ne")]
    a = c.interpolate_node(center, outer, 0.3, 45.0, 45.0)
    b = c.interpolate_node(center, outer, 0.3, 135.0, 45.0)
    assert np.allclose(np.linalg.eigvalsh(c.covariance(a)), np.linalg.eigvalsh(c.covariance(b)))
    assert not np.allclose(c.covariance(a), c.covariance(b))


def test_failed_crop_parity_blocks_numerically_passing_selection(bound, monkeypatch, tmp_path):
    from camera_platform import calibration_psf_convergence as conv

    p, _ = bound
    (tmp_path / "convergence").mkdir()

    def measure(profile, prepared, setting, root, progress):
        return {
            "id": conv.key(setting),
            "setting": setting,
            "fields": {},
            "fft_work_proxy": 1.0,
            "tail_energy_pass": True,
        }

    monkeypatch.setattr(conv, "measure", measure)
    monkeypatch.setattr(c, "parity", lambda *a: {"passed": False, "checks": []})
    result = conv.qualify(p, {}, tmp_path)
    assert result["reference_pass"] and result["selected"] is None
    assert result["status"] == "not_converged" and not result["crop_parity"]["passed"]
