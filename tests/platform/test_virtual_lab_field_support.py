"""Regressions for physical source support and the sensor integrator grid."""

import math
from pathlib import Path

import numpy as np
import pytest

from camera_platform import virtual_lab_engine as engine
from camera_platform import virtual_lab_field as field
from camera_platform.virtual_lab_job import default_specification

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def pinned_l1():
    spec = default_specification(engine.catalog(WORKSPACE))
    spec["optics"] = {"f_number": 4.12, "defocus_um": 0}
    return engine._load(WORKSPACE, spec)


def _assert_inverse_input_fits(optics, receiver_shape, spacing_mm, wavelengths, support):
    from pyisetcam.optics import _strict_raytrace_geometry_axes

    source_rows, source_cols = support["source_shape"]
    r0, c0 = support["crop_row_col"]
    r1, c1 = r0 + receiver_shape[0], c0 + receiver_shape[1]
    assert (r1 - r0, c1 - c0) == tuple(receiver_shape)
    assert r0 == source_rows - r1 and c0 == source_cols - c1
    x, y = field.field_coordinates(receiver_shape, spacing_mm)
    radius = np.hypot(x, y)
    for wave in wavelengths:
        ideal, distorted = _strict_raytrace_geometry_axes(optics["raytrace"], wave)
        input_radius = np.interp(radius, distorted, ideal)
        ratio = np.divide(input_radius, radius, out=np.ones_like(radius), where=radius > 0)
        # Leave interpolation support within actual generated source centers.
        assert np.max(np.abs(x * ratio)) <= (source_cols - 1) / 2 * spacing_mm
        assert np.max(np.abs(y * ratio)) <= (source_rows - 1) / 2 * spacing_mm


def test_source_support_encloses_inverse_distortion_at_every_wavelength():
    ideal = np.array([0.0, 1.0, 2.0, 4.0])
    distorted = np.stack([ideal * 0.9, ideal * 0.6, ideal * 0.8], axis=-1)
    optics = {
        "raytrace": {
            "psf": {"field_height_mm": ideal, "field_coordinate": "ideal_image_height"},
            "relative_illumination": {
                "field_height_mm": ideal,
                "field_coordinate": "ideal_image_height",
            },
            "geometry": {
                "field_coordinate": "ideal_image_height",
                "field_height_mm": ideal,
                "wavelength_nm": [450, 550, 650],
                "function": distorted,
                "interpolation": "linear",
            }
        }
    }
    receiver = (80, 120)
    support = field.source_support(optics, receiver, 0.02, [450, 550, 650])
    assert support["maximum_inverse_scale"] >= 1 / 0.6
    assert support["source_shape"][1] > receiver[1]
    _assert_inverse_input_fits(optics, receiver, 0.02, [450, 550, 650], support)


def test_actual_l1_barrel_distortion_gets_additional_source_halo(pinned_l1):
    _, _, optics, _, geometry = pinned_l1
    spacing_mm = geometry["pixel_pitch_um"] / 2000
    receiver = (360 * 2 + 130, 640 * 2 + 130)
    support = field.source_support(optics, receiver, spacing_mm, engine.WAVE)
    assert support["maximum_inverse_scale"] > 1.18
    assert support["source_shape"][1] > receiver[1] * 1.18
    _assert_inverse_input_fits(optics, receiver, spacing_mm, engine.WAVE, support)


@pytest.mark.parametrize("kind", ["color", "tone", "resolution"])
def test_distorted_rois_remain_in_the_same_source_patch_at_every_band(pinned_l1, kind):
    from pyisetcam.optics import _strict_raytrace_geometry_axes

    _, _, optics, _, geometry = pinned_l1
    if kind == "color":
        rois = {str(i): value for i, value in enumerate(engine._patch_rois())}
    elif kind == "tone":
        rois = {str(i): [156, 204, (i + 0.25) * 40, (i + 0.75) * 40] for i in range(16)}
    else:
        rois = engine.EDGE_ROIS
    mapped = field.mapped_chart_rois(optics, geometry, rois, engine.WAVE)
    spacing = (
        2
        * math.tan(math.radians(geometry["common_horizontal_fov_deg"]) / 2)
        * geometry["focal_length_mm"]
        / 640
    )
    for key, (r0, r1, c0, c1) in mapped.items():
        row, col = np.meshgrid(
            np.arange(r0, r1) + 0.5, np.arange(c0, c1) + 0.5, indexing="ij"
        )
        x, y = (col - 320) * spacing, (row - 180) * spacing
        radius = np.hypot(x, y)
        for wave in engine.WAVE:
            ideal, distorted = _strict_raytrace_geometry_axes(optics["raytrace"], wave)
            ratio = np.divide(
                np.interp(radius, distorted, ideal),
                radius,
                out=np.ones_like(radius),
                where=radius > 0,
            )
            source_row, source_col = 180 + y * ratio / spacing, 320 + x * ratio / spacing
            a, b, c, d = rois[key]
            assert source_row.min() >= a and source_row.max() <= b
            assert source_col.min() >= c and source_col.max() <= d
            if kind == "color":
                patch = (source_row / 90).astype(int) * 6 + (source_col / (640 / 6)).astype(int)
                assert np.all(patch == int(key))
            elif kind == "tone":
                assert np.all((source_col / 40).astype(int) == int(key))
            else:
                cy, cx = (a + b) / 2, (c + d) / 2
                edge_distance = source_col - cx - (source_row - cy) / 12
                assert edge_distance.min() < -5 and edge_distance.max() > 5


def test_capture_preserves_oi_shape_for_sensor_spatial_integration(monkeypatch, pinned_l1):
    from pyisetcam import sensor as sensor_module
    from pyisetcam import sensor_capture
    from pyisetcam.optics import oi_get

    _, _, _, original, geometry = pinned_l1
    sensor = original.clone()
    sensor.fields["size"] = (16, 24)
    spacing = geometry["pixel_pitch_um"] * 1e-6 / 2
    observed = []

    def checked_integrator(density, oi, configured_sensor):
        observed.append((density.shape, oi_get(oi, "hspatialresolution")))
        assert oi_get(oi, "hspatialresolution") == pytest.approx(spacing)
        assert oi_get(oi, "wspatialresolution") == pytest.approx(spacing)
        assert tuple(oi.data["photons"].shape[:2]) == tuple(density.shape[:2])
        return np.ones(configured_sensor.fields["size"]) * field.ELEMENTARY_CHARGE

    monkeypatch.setattr(sensor_module, "_spatial_integrate_current_density", checked_integrator)
    monkeypatch.setattr(sensor_capture, "resample_sensor_capture", lambda value, seed: value)
    monkeypatch.setattr(engine, "WAVE", np.array([550.0]))
    optics = {
        "model": "shiftinvariant",
        "focal_length_m": geometry["focal_length_mm"] / 1000,
        "f_number": 4.12,
        "transmittance": {"wave": [550], "scale": [1]},
    }
    result, _, _ = field.capture(
        optics,
        sensor,
        {"white": np.array([1e18])},
        "uniform",
        geometry,
        {"defocus_um": 0},
        42,
        {"angular_response": {"mode": "off", "exponents_r_gr_gb_b": [2] * 4}},
    )
    assert len(observed) == 2
    assert [shape[-1] for shape, _ in observed] == [4, 3]
    assert result.data["mean_electrons"].shape == (16, 24)
