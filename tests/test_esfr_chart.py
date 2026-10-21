"""Geometry, native-pixel sampling, and provenance tests for the 2023 chart."""

import json
import math
from collections import Counter
from xml.etree import ElementTree

import numpy as np
import pytest
from camera_platform.esfr_analysis import edge_metrics
from scipy.ndimage import gaussian_filter

from camera_platform import esfr_chart


@pytest.fixture(scope="module")
def raster():
    return esfr_chart.render_chart((1440, 2560), supersample=4)


def test_official_topology_and_uncertainty_are_preserved():
    m = esfr_chart.chart_manifest()
    assert len(m["stars"]) == 13
    assert Counter(s["role"] for s in m["stars"]) == {"core": 9, "extended_corner": 4}
    assert all(s["cycles"] == 4 and s["radial_edges"] == 8 for s in m["stars"])
    assert len(m["side_squares"]) == 2
    assert len(m["gray_patches"]) == 20
    assert len(m["color_patches"]) == 18
    assert len(m["fiducials"]) == 4
    assert len(m["orientation_dots"]) == 2
    assert len(m["wedges"]) == 16
    assert "12 pairs" in m["wedge_source_description"]
    assert "unverified" in m["wedge_pair_mapping"]
    assert m["photometry"]["screenshot_used_as_radiance"] is False
    assert m["photometry"]["printed_chart_mtf"] == "unavailable"
    assert m["physical_size_mm"] is None
    assert m["validation"]["iso_conformance"] == "not_established"
    json.dumps(m, allow_nan=False)
    m["stars"][0]["center_xy"][0] = -100
    assert esfr_chart.chart_manifest()["stars"][0]["center_xy"][0] == 53


def test_nominal_photometry_does_not_infer_print_spectra():
    m = esfr_chart.chart_manifest()
    assert m["edge_levels"]["background"] / m["edge_levels"]["dark"] == 4
    grays = m["gray_patches"]
    assert np.allclose(
        [p["nominal_linear_reflectance"] for p in grays],
        10 ** -np.array([p["nominal_density"] for p in grays]),
    )
    assert np.all(np.diff([p["nominal_linear_reflectance"] for p in grays]) < 0)
    assert grays[0]["nominal_density"] == 0.10
    assert grays[-1]["nominal_density"] == 2.30
    assert sorted(p["nominal_table_index"] for p in m["color_patches"]) == list(range(1, 19))
    assert m["photometry"]["spectral_reflectance"] == "unavailable"
    assert m["photometry"]["actual_print_reflectance"] == "unavailable"


def test_framing_is_uniform_and_all_native_rois_are_complete(raster):
    rgb, rois, m = raster
    assert rgb.shape == (1440, 2560, 3) and rgb.dtype == np.float32
    assert np.isfinite(rgb).all() and rgb.min() >= 0 and rgb.max() <= 1
    assert Counter(r["kind"] for r in rois) == {"edge": 112, "gray": 20, "color": 18}
    assert len({r["id"] for r in rois}) == len(rois)
    assert m["raster"]["artwork_scale"] == 1440 / 370
    assert m["raster"]["artwork_offset_xy"][0] > 0
    assert m["raster"]["artwork_offset_xy"][1] == 0
    assert m["raster"]["minimum_edge_roi_size_px"] == 62
    assert m["raster"]["max_supersampled_plane_side_px"] == 1024
    for roi in rois:
        y, x, h, w = roi["box"]
        assert min(y, x) >= 0 and y + h <= rgb.shape[0] and x + w <= rgb.shape[1]
        assert all(v % 2 == 0 for v in roi["box"])
        assert min(h, w) >= 32
    assert rois == esfr_chart.chart_rois((1440, 2560))


def test_all_112_native_bayer_edges_are_single_measureable_edges(raster):
    rgb, rois, _ = raster
    blurred = gaussian_filter(rgb[:, :, 1], sigma=1.4)
    mtf50 = []
    for roi in rois:
        if roi["kind"] != "edge":
            continue
        y, x, h, w = roi["box"]
        # Global RGGB phase: green-on-red-row is x odd, y even.
        result = edge_metrics(blurred[y : y + h : 2, x + 1 : x + w : 2], pitch_native=2)
        assert result["status"] == "ok", (roi["id"], result)
        diff = abs(result["edge_angle_deg"] - roi["edge_tangent_deg"])
        assert min(diff, abs(diff - 180)) < 0.25
        mtf50.append(result["mtf50"])
    assert len(mtf50) == 112
    expected = math.sqrt(math.log(2)) / (math.sqrt(2) * math.pi * 1.4)
    assert max(abs(np.array(mtf50) - expected)) < 0.007


def test_mirrored_corner_star_phase_matches_reference_layout(raster):
    rgb, _, m = raster
    scale = m["raster"]["artwork_scale"]
    ox, oy = m["raster"]["artwork_offset_xy"]
    for star in m["stars"]:
        # The two negative-slant extreme corners reverse dark/light phase.
        phase = 45 if star["slant_deg"] < 0 else 0
        angle = math.radians(star["slant_deg"] + phase + 22.5)
        x = int(ox + (star["center_xy"][0] + 22 * math.cos(angle)) * scale)
        y = int(oy + (star["center_xy"][1] + 22 * math.sin(angle)) * scale)
        assert rgb[y, x, 1] == pytest.approx(esfr_chart.EDGE_DARK)
        angle += math.pi / 4
        x = int(ox + (star["center_xy"][0] + 22 * math.cos(angle)) * scale)
        y = int(oy + (star["center_xy"][1] + 22 * math.sin(angle)) * scale)
        assert rgb[y, x, 1] == pytest.approx(esfr_chart.BACKGROUND)


def test_svg_is_vector_and_has_full_native_roi_overlay():
    plain = esfr_chart.svg_document(width=1920)
    overlay = esfr_chart.svg_document(width=1920, overlay=True)
    root = ElementTree.fromstring(plain)
    assert root.attrib["viewBox"] == "0 0 640 370"
    assert float(root.attrib["height"]) / float(root.attrib["width"]) == 370 / 640
    assert "<image" not in plain and "base64" not in plain
    assert overlay.count('stroke-width=".45"') == 150
    assert 'data-feature="star-center"' in plain


def test_joint_pixel_integration_has_no_background_leak(monkeypatch):
    # Two adjacent opaque fields cover the entire chart. A pixel crossing their
    # diagonal boundary must integrate only their red/green spectra; separate
    # antialias-alpha blends would leave a nonzero blue/background component.
    primitives = [
        dict(kind="polygon", points=[[0, 0], [640, 0], [0, 370]], rgb=[1, 0, 0], feature="red"),
        dict(
            kind="polygon", points=[[640, 0], [640, 370], [0, 370]], rgb=[0, 1, 0], feature="green"
        ),
    ]
    monkeypatch.setattr(esfr_chart, "_primitives", lambda: primitives)
    rgb, _, _ = esfr_chart.render_chart((370, 640), supersample=4)
    interior = rgb[4:-4, 4:-4]
    assert np.max(np.abs(interior[:, :, 2])) == 0
    assert np.max(np.abs(interior[:, :, :2].sum(axis=2) - 1)) == 0
    assert np.any((interior[:, :, 0] > 0) & (interior[:, :, 0] < 1))


@pytest.mark.parametrize("shape", [(0, 1920), (1080.5, 1920), (True, 1920), (8000, 8000)])
def test_invalid_chart_resource_request_rejected(shape):
    with pytest.raises(ValueError):
        esfr_chart.render_chart(shape)


@pytest.mark.parametrize("supersample", [0, 1.5, 9, True])
def test_invalid_sampling_request_rejected(supersample):
    with pytest.raises(ValueError):
        esfr_chart.render_chart((370, 640), supersample)
