"""Selected ghost ROI response preserves physical position and source contracts."""

import copy
import json

import numpy as np
import pytest

from camera_platform import reference_ghost_engine as engine
from camera_platform import reference_ghost_probe as probe
from camera_platform import reference_workshop_optics as optics


def solution():
    spec = optics.template()
    fields = []
    for i, radius in enumerate((0.0, 0.5, 0.85)):
        fields.append(
            {
                "id": f"node-{i}",
                "normalized_radius": radius,
                "status": "succeeded",
                "image_xy_mm": [i * 2.0, 0.0],
                "azimuth_deg": 0.0,
                "object_angle_deg": i * 10.0,
                "geometry": optics.geometry(spec),
            }
        )
    psf = np.zeros((21, 3, 7, 7))
    psf[..., 3, 3] = 1
    return {
        "result": {"revision": "pinned-fixture", "arrays_sha256": "fixture-hash", "fields": fields},
        "arrays": {
            "psf": psf,
            "irradiance_per_radiance_sr": np.broadcast_to([0.02, 0.04, 0.08], (21, 3)).copy(),
            "conditional_weights": np.ones((21, 3, 1)),
            "directions_xyz": np.tile([[0.0, 0.0, 1.0]], (3, 1, 1)),
        },
    }


@pytest.fixture
def absolute_qe(monkeypatch):
    def qe(response, theta, phi, wavelength):
        return np.tile([0.1, 0.2, 0.3, 0.4], (len(theta), 1))

    monkeypatch.setattr(probe.pixel, "absolute_angular_response", qe)


def test_kernel_rotation_preserves_chromatic_centroid_and_physical_y_sign():
    kernel = np.zeros((7, 7))
    kernel[3, 5] = 1
    rotated, retained = probe._rotate_kernel(kernel, 90, 11)
    assert retained == pytest.approx(1)
    assert rotated[3, 5] == pytest.approx(1)  # physical +X becomes +Y (array upward)
    assert rotated.sum() == pytest.approx(1)
    other = np.zeros((7, 7))
    other[3, 4] = 1
    blue, _ = probe._rotate_kernel(other, 90, 11)
    assert blue[4, 5] == pytest.approx(1)
    diagonal, retained = probe._rotate_kernel(kernel, 45, 11)
    assert retained == pytest.approx(1)
    rr, cc = np.indices(diagonal.shape)
    assert np.sum((rr - 5) * diagonal) == pytest.approx(-np.sqrt(2))
    assert np.sum((cc - 5) * diagonal) == pytest.approx(np.sqrt(2))


def test_interpolation_uses_actual_image_height_and_absolute_angular_transfer(absolute_qe):
    src = solution()
    before = copy.deepcopy(src)
    psf, transfer, metadata = probe.interpolated_response(src, {}, [3.0, 0.0])
    np.testing.assert_allclose(psf.sum(axis=(1, 2)), 1)
    np.testing.assert_allclose(transfer, np.tile(np.array([0.1, 0.2, 0.3, 0.4]) * 0.06, (21, 1)))
    assert [row["weight"] for row in metadata["radial_weights"]] == [0.5, 0.5]
    assert metadata["object_angle_deg"] == 15
    assert metadata["probe_xy_mm"] == [3.0, 0.0]
    assert metadata["source_arrays_sha256"] == "fixture-hash"
    assert "disabled" in metadata["lsc_policy"]
    np.testing.assert_array_equal(src["arrays"]["psf"], before["arrays"]["psf"])
    assert probe.supported_radius_mm(src) == 4


def test_selected_field_not_nearest_and_centroid_rotates_without_recentering(absolute_qe):
    src = solution()
    src["arrays"]["psf"][:] = 0
    src["arrays"]["psf"][..., 3, 4] = 1
    psf, _, metadata = probe.interpolated_response(src, {}, [0.0, 2.0])
    assert metadata["radial_weights"][0]["field_id"] == "node-1"
    assert metadata["radial_weights"][0]["rotation_deg"] == 90
    peak = np.unravel_index(psf[0].argmax(), psf[0].shape)
    center = psf.shape[-1] // 2
    assert peak == (center - 1, center)


def test_out_of_support_is_actionable_and_not_extrapolated(absolute_qe):
    with pytest.raises(ValueError, match="black_source.*new optical field solve"):
        probe.interpolated_response(solution(), {}, [4.1, 0])
    for xy in ([float("nan"), 0], [13.66, 0], [0]):
        with pytest.raises(ValueError):
            probe.interpolated_response(solution(), {}, xy)
    src = solution()
    src["result"]["fields"][1]["status"] = "failed"
    with pytest.raises(ValueError, match="missing radial node"):
        probe.interpolated_response(src, {}, [1, 0])


def test_black_probe_beyond_background_radius_needs_no_background_assets(tmp_path, monkeypatch):
    from camera_platform.reference_ghost_charts import chart_scene

    monkeypatch.setattr(
        optics, "load_solution", lambda *_: pytest.fail("Black needs no optical background")
    )
    rate, metadata = probe.background(None, tmp_path, {}, {}, 0, [12, 0], "black", 64, chart_scene)
    assert rate.shape == (256, 256) and not rate.any()
    assert metadata["chart_definition"]["chart"] == "black"
    assert metadata["transmission"] == [1.0] * 21


def test_background_mosaic_units_spectral_bin_pixel_area_and_transmission_once(
    tmp_path, monkeypatch, absolute_qe
):
    from camera_platform import virtual_lab_engine

    monkeypatch.setattr(optics, "load_solution", lambda *_: solution())
    (tmp_path / "pixel-response.json").write_text(json.dumps({}))
    calls = []

    def transmission(prescription, angle, samples):
        calls.append(angle)
        return np.ones((21, 3)) * 0.5

    monkeypatch.setattr(engine, "direct_transmission", transmission)
    monkeypatch.setattr(virtual_lab_engine, "_spectra", lambda *_: {"white": np.ones(21)})

    def scene(chart, geometry, spectral):
        return np.ones((*geometry["input_shape_row_col"], 21)) * 1e12, {"chart": chart}

    rate, metadata = probe.background(None, tmp_path, {}, {}, 0, [3, 0], "dark_contrast", 64, scene)
    scale = 1e12 * 0.06 * 210 * 0.5 * (2.9e-6) ** 2
    np.testing.assert_allclose(rate[:2, :2], scale * np.array([[0.1, 0.2], [0.3, 0.4]]))
    assert calls == [15]
    assert metadata["chart_definition"] == {"chart": "dark_contrast"}
    assert metadata["pixel_aperture_applications"] == 1
