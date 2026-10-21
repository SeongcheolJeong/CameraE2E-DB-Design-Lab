"""Chief direction coordinates and single-application pixel response checks."""

from pathlib import Path

import numpy as np
import pytest

from camera_platform import virtual_lab_engine as engine
from camera_platform.virtual_lab_field import (
    chief_ray_maps,
    field_coordinates,
    integrate_current_density,
    relative_gain,
)
from camera_platform.virtual_lab_job import default_specification
from pyisetcam.types import OpticalImage

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def model():
    specification = default_specification(engine.catalog(WORKSPACE))
    return engine._load(WORKSPACE, specification)


def test_optical_coordinates_and_sensor_azimuth_have_explicit_opposite_y(model):
    _, _, optics, _, _ = model
    xx, yy = field_coordinates((3, 3), 0.1)
    assert xx[1, 2] > 0 and yy[0, 1] > 0
    theta, phi, provenance = chief_ray_maps(optics, (3, 3), 0.1, 550)
    assert theta[1, 1] == pytest.approx(0, abs=1e-6)
    assert phi[1, 2] == pytest.approx(0, abs=1e-6)
    assert phi[0, 1] == pytest.approx(270, abs=1e-6)
    assert phi[2, 1] == pytest.approx(90, abs=1e-6)
    assert provenance["origin"] == "lens_db_chief_direction"


def test_chief_direction_refuses_wavelength_and_field_extrapolation(model):
    _, _, optics, _, _ = model
    with pytest.raises(ValueError, match="wavelength"):
        chief_ray_maps(optics, (3, 3), 0.1, 700)
    with pytest.raises(ValueError, match="support"):
        chief_ray_maps(optics, (3, 3), 100, 550)


def test_native_pixel_integration_applies_four_site_gain_once(model):
    _, _, _, sensor, _ = model
    sensor = sensor.clone()
    sensor.fields["size"] = (6, 8)
    pitch = float(sensor.fields["pixel"]["size_m"][0])
    shape = (24, 28)
    oi = OpticalImage(
        fields={"width_m": shape[1] * pitch / 2, "height_m": shape[0] * pitch / 2},
        data={"photons": np.empty((*shape, 0))},
    )
    base = np.broadcast_to(np.array([1, 2, 4]) * 1e18, (*shape, 3))
    normal = integrate_current_density(sensor, oi, base)
    four = base[..., [0, 1, 1, 2]]
    unity = integrate_current_density(sensor, oi, four, separate_greens=True)
    np.testing.assert_allclose(unity, normal, rtol=1e-12)
    gains = np.array([0.5, 0.75, 0.25, 0.125])
    changed = integrate_current_density(sensor, oi, four * gains, separate_greens=True)
    for site, (row, col) in enumerate([(0, 0), (0, 1), (1, 0), (1, 1)]):
        np.testing.assert_allclose(
            changed[row::2, col::2] / normal[row::2, col::2], gains[site], rtol=1e-12
        )


def test_declared_cosine_model_is_exactly_unity_at_normal_and_separate_sites():
    theta = np.array([0, 60])
    angular = {"mode": "cosine_power", "exponents_r_gr_gb_b": [1, 2, 3, 4]}
    values = relative_gain(angular, None, theta, np.zeros_like(theta), 550)
    np.testing.assert_allclose(values[0], 1)
    np.testing.assert_allclose(values[1], [0.5, 0.25, 0.125, 0.0625])


def test_native_admission_policy_does_not_reduce_pitch_or_fov():
    specification = default_specification(engine.catalog(WORKSPACE))
    specification["field_model"]["horizontal_fov_deg"] = 40
    with pytest.raises(ValueError, match="Native"):
        engine._load(WORKSPACE, specification)


def test_null_fov_and_explicit_baseline_preserve_same_physical_window(model):
    specification = default_specification(engine.catalog(WORKSPACE))
    specification["field_model"]["horizontal_fov_deg"] = model[-1]["common_horizontal_fov_deg"]
    explicit = engine._load(WORKSPACE, specification)
    # Ceil-to-even can retain one extra Bayer pair after a floating point
    # angle roundtrip; _common uses the exact physical window, not that border.
    np.testing.assert_allclose(explicit[-1]["native_shape"], model[-1]["native_shape"], atol=2)
    assert explicit[-1]["common_horizontal_fov_deg"] == pytest.approx(
        model[-1]["common_horizontal_fov_deg"], abs=1e-12
    )
    assert explicit[-1]["pixel_pitch_um"] == model[-1]["pixel_pitch_um"]
