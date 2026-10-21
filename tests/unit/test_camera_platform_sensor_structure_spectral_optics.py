"""The OCL bridge uses actual native coordinates and pre-noise spectral units."""
import copy
from pathlib import Path

import numpy as np
import pytest

from camera_platform import sensor_structure_spectral_optics as adapter
from camera_platform import reference_workshop_optics as optics
from camera_platform.registry import digest

WORKSPACE = Path(__file__).resolve().parents[2]


def test_imx662_vendor_geometry_separates_acquired_values_and_crop_assumptions():
    profile = adapter.geometry_profile()
    assert profile["active_shape_row_col"] == [1097, 1937]
    assert profile["effective_shape_row_col"] == [1113, 1965]
    assert profile["total_shape_row_col"] == [1196, 2014]
    assert profile["dimension_source"]["variant"] == "IMX662-AAQR/AAQR1"
    assert profile["dimension_source"]["physical_die_unit_match"] == "not_verified"
    for roi in profile["roi_layout"]:
        assert all(v % 2 == 0 for v in roi["origin_row_col"])
        assert all(0 <= a and a + n <= bound for a, n, bound in zip(
            roi["origin_row_col"], roi["shape_row_col"], profile["active_shape_row_col"], strict=True))
        assert roi["optical_anchor_local_row_col"] == [128.0, 128.0]
    assert profile["roi_layout"][1]["physical_location"] == "right-edge"
    assert profile["roi_layout"][1]["optical_anchor_image_xy_mm"] == [2.436, 0.0]
    supplied = dict(profile)
    expected = supplied.pop("profile_sha256")
    assert expected == digest(supplied)


@pytest.mark.parametrize("evidence", [{"url": "https://example.com/other-sensor.pdf"},
                                     {"document_sha256": "bad"},
                                     {"document_sha256": "z" * 64}])
def test_reject_unbound_dimension_source(evidence):
    with pytest.raises(ValueError):
        adapter.geometry_profile(evidence)


def test_vendor_pdf_hash_changes_geometry_identity():
    first = adapter.geometry_profile({"document_sha256": "a" * 64})
    second = adapter.geometry_profile({"document_sha256": "b" * 64})
    assert first["profile_sha256"] != second["profile_sha256"]


def test_actual_patent_chief_matches_sensor_anchors_without_20_degree_assignment():
    result = adapter.locate(WORKSPACE)
    assert result["fields"][0]["target_image_xy_mm"] == [0.0, 0.0]
    assert result["fields"][1]["target_image_xy_mm"] == [2.436, 0.0]
    assert result["fields"][1]["azimuth_deg"] == 0
    center = result["chief_rays"]["center"]
    edge = result["chief_rays"]["outer-ne"]
    assert len(center) == len(edge) == 21
    assert all(row["signed_x_cra_deg"] == 0 for row in center)
    assert edge[0]["signed_x_cra_deg"] == pytest.approx(4.51923032161, abs=1e-8)
    assert edge[10]["signed_x_cra_deg"] == pytest.approx(4.52475632493, abs=1e-8)
    assert edge[20]["signed_x_cra_deg"] == pytest.approx(4.52514172085, abs=1e-8)
    np.testing.assert_allclose(edge[10]["chief_image_xy_mm"], [2.436, 0], atol=1e-8)
    assert all(abs(row["direction_xyz"][1]) < 1e-12 for row in edge)
    # The virtual-field optical cache cannot be reused at the active-array crop.
    context = copy.deepcopy(result["source_context"])
    old = dict(context)
    old.pop("located_fields")
    assert optics.cache_key(WORKSPACE, result["optical_specification"], source_context=context) != optics.cache_key(
        WORKSPACE, result["optical_specification"], source_context=old)


def _spectral_fixture(monkeypatch):
    field = {"id": "center", "label": "center", "geometry": {
        "active_shape_row_col": [1097, 1937],
        "field_approximation": "local_shift_invariant_at_declared_inside_ROI_optical_anchor"}}
    solution = {"result": {"revision": "new-located-optics", "fields": [field]}}
    cell = {"source_report": "DEF-2206-805", "pixel_pitch_um": 2.9, "cell_sha256": "bound-cell"}
    monkeypatch.setattr(adapter.pixels, "build_response", lambda *a, **k: {"response": "fixed"})
    monkeypatch.setattr(adapter, "_spectra", lambda *a, **k: {"asset": {"sha256": "fixed"}})
    monkeypatch.setattr(adapter.charts_engine, "_scene", lambda *a, **k: (np.zeros((1, 1, 21)), None))
    bands = np.ones((21, 256, 256, 4), dtype=np.float64)
    for band in range(21):
        bands[band] *= band + 1
    planes = bands.sum(axis=0)
    rr, cc = np.indices((256, 256))
    sites = np.array([[0, 1], [2, 3]])[rr % 2, cc % 2]
    mosaic = np.take_along_axis(planes, sites[..., None], axis=-1)[..., 0]
    called = []
    def apply(*args, **kwargs):
        called.append(kwargs)
        return {"spectral_planes_electron_rate": bands, "planes_electron_rate": planes,
                "mean_electron_rate": mosaic, "metadata": {"pixel_aperture_applications": 1}}
    monkeypatch.setattr(adapter.optics, "apply_field", apply)
    return solution, cell, bands, called


def test_preserves_band_contributions_before_mosaic_and_noise(tmp_path, monkeypatch):
    solution, cell, bands, called = _spectral_fixture(monkeypatch)
    result = adapter.spectral_inputs(WORKSPACE, tmp_path, solution, cell, charts=("uniform",))
    actual = np.load(tmp_path / "center/uniform/spectral-planes-electron-rate.npy", allow_pickle=False)
    np.testing.assert_array_equal(actual, bands)
    assert actual.shape == (21, 256, 256, 4)
    assert result["array_unit"] == "collected_electrons_per_second_in_each_declared_10nm_bin"
    assert result["pixel_aperture_applications"] == 1
    assert len(called) == 1 and called[0]["return_spectral"] is True
    assert callable(called[0]["angular_response"])
    assert all(row["bytes"] > 0 and len(row["sha256"]) == 64 for row in result["files"])


def test_virtual_optics_cannot_be_relabelled_as_actual_sensor(tmp_path, monkeypatch):
    solution, cell, _, _ = _spectral_fixture(monkeypatch)
    solution["result"]["fields"][0]["geometry"].pop("active_shape_row_col")
    with pytest.raises(ValueError, match="Old virtual"):
        adapter.spectral_inputs(WORKSPACE, tmp_path, solution, cell, charts=("uniform",))


@pytest.mark.parametrize("change", [{"source_report": "DEF-2011-804"}, {"pixel_pitch_um": 3.0}])
def test_cell_cannot_be_substituted(tmp_path, monkeypatch, change):
    solution, cell, _, _ = _spectral_fixture(monkeypatch)
    with pytest.raises(ValueError, match="IMX662"):
        adapter.spectral_inputs(WORKSPACE, tmp_path, solution, {**cell, **change}, charts=("uniform",))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_luminance_rejected_before_acquisition(tmp_path, monkeypatch, value):
    solution, cell, _, _ = _spectral_fixture(monkeypatch)
    with pytest.raises(ValueError, match="Luminance"):
        adapter.spectral_inputs(WORKSPACE, tmp_path, solution, cell, luminance_cd_m2=value)


@pytest.mark.parametrize("charts", [(), ("fake",), ("color", "color")])
def test_unsupported_or_repeated_charts_rejected(tmp_path, monkeypatch, charts):
    solution, cell, _, _ = _spectral_fixture(monkeypatch)
    with pytest.raises(ValueError, match="unique"):
        adapter.spectral_inputs(WORKSPACE, tmp_path, solution, cell, charts=charts)
