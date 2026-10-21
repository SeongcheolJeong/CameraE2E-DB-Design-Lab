"""Pupil quadrature preserves weights, ray validity and sensor coordinates."""

import copy
from pathlib import Path

import numpy as np
import pytest

from camera_platform import virtual_lab_engine as engine
from camera_platform.virtual_lab_angular import cosine_power_response
from camera_platform.virtual_lab_job import default_specification
from camera_platform.virtual_lab_pupil import build_pupil_table, pupil_gain_map

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def model():
    spec = default_specification(engine.catalog(WORKSPACE))
    return engine._load(WORKSPACE, spec)[2]


def bundle(model, angles=(0, 30), weights=(0.25, 0.75), phi=0):
    optics = copy.deepcopy(model)
    ray = optics["raytrace"]
    directions = np.full_like(ray["image_direction_cosines"], np.nan)
    values = np.zeros_like(ray["pupil_weights"])
    for i, (theta, weight) in enumerate(zip(angles, weights, strict=True)):
        t, p = np.radians([theta, phi])
        directions[:, :, i] = [np.sin(t) * np.cos(p), np.sin(t) * np.sin(p), np.cos(t)]
        values[:, :, i] = weight
    ray["image_direction_cosines"] = directions
    ray["pupil_weights"] = values
    return optics


def test_weighted_response_ignores_blocked_nan_and_does_not_add_cosine(model):
    optics = bundle(model)
    response = cosine_power_response([450, 550, 650], power=2)
    table = build_pupil_table(optics, response, [450, 550, 650])
    gain = pupil_gain_map(optics, table, (3, 3), 0.1, 550)
    # A chief-only model would yield 1 on axis; an extra cosine factor would
    # also change this explicitly weighted expected result.
    np.testing.assert_allclose(gain, 0.25 + 0.75 * 0.75, atol=1e-12)


def test_unity_angular_response_stays_unity_over_entire_pupil(model):
    optics = bundle(model)
    response = cosine_power_response([450, 550, 650], power=0)
    table = build_pupil_table(optics, response, [450, 500, 550, 650])
    np.testing.assert_allclose(pupil_gain_map(optics, table, (5, 7), 0.1, 500), 1, atol=1e-12)


def test_wavelength_interpolates_ensemble_response_not_ray_direction(model):
    optics = bundle(model, angles=(0,), weights=(1,))
    optics["raytrace"]["image_direction_cosines"][:, 1, 0] = [0.5, 0, np.sqrt(0.75)]
    response = cosine_power_response([450, 550, 650], power=2)
    table = build_pupil_table(optics, response, [450, 500, 550, 650])
    np.testing.assert_allclose(pupil_gain_map(optics, table, (3, 3), 0.1, 500), 0.875, atol=1e-12)


def test_pupil_direction_rotates_into_sensor_row_down_azimuth(model):
    optics = bundle(model, angles=(30,), weights=(1,))
    response = cosine_power_response([450, 550, 650], power=0)
    values = np.asarray(response["relative_response"])
    values[:, 1:, :, :] = np.array([0.9, 0.8, 0.7, 0.6])[None, None, :, None]
    response["relative_response"] = values.tolist()
    table = build_pupil_table(optics, response, [550])
    gain = pupil_gain_map(optics, table, (3, 3), 0.1, 550)
    np.testing.assert_allclose(gain[1, 2], 0.9)  # right, phi=0
    np.testing.assert_allclose(gain[0, 1], 0.6)  # top, phi=270
    np.testing.assert_allclose(gain[2, 1], 0.8)  # bottom, phi=90
    np.testing.assert_allclose(gain[1, 0], 0.7)  # left, phi=180


@pytest.mark.parametrize("invalid", ["missing", "weight", "nan", "reverse"])
def test_invalid_bundle_never_silently_falls_back_to_chief(model, invalid):
    optics = bundle(model)
    ray = optics["raytrace"]
    if invalid == "missing":
        del ray["pupil_weights"]
    elif invalid == "weight":
        ray["pupil_weights"][0, 0, 0] = -1
    elif invalid == "nan":
        ray["image_direction_cosines"][0, 0, 0] = np.nan
    else:
        ray["image_direction_cosines"][0, 0, 0] = [0, 0, -1]
    with pytest.raises(ValueError):
        build_pupil_table(optics, cosine_power_response([450, 550, 650]), [550])


def test_pupil_queries_refuse_wavelength_and_field_extrapolation(model):
    optics = bundle(model)
    table = build_pupil_table(optics, cosine_power_response([450, 550, 650]), [450, 550, 650])
    for spacing, wavelength in [(0.1, 700), (100, 550)]:
        with pytest.raises(ValueError):
            pupil_gain_map(optics, table, (3, 3), spacing, wavelength)
