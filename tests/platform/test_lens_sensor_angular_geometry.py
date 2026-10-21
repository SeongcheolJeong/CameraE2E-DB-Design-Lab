"""Strict optical distortion changes object angles at native RAW sample centers."""

from __future__ import annotations

import math

import numpy as np
import pytest

from camera_platform.lens_sensor_angular_geometry import (
    MODEL,
    STATUS,
    strict_raytrace_pixel_center_angles,
)


def _raytrace() -> dict:
    return {
        "physics_policy": "strict",
        "geometry": {
            "field_coordinate": "ideal_image_height",
            "field_height_mm": [0.0, 1.0, 2.0],
            "wavelength_nm": [450.0, 550.0],
            # At 450 nm, image height is half the ideal height; at 550 nm
            # the mapping is identity.  Both bands are strict and monotone.
            "function": [[0.0, 0.0], [0.5, 1.0], [1.0, 2.0]],
        },
    }


def test_chromatic_distortion_inverse_maps_native_pixel_centers():
    result = strict_raytrace_pixel_center_angles(
        _raytrace(), sensor_shape=(2, 2),
        sensor_sample_size_m_row_col=(0.001, 0.001),
        image_distance_m=0.02, wavelength_nm=(450.0, 550.0),
    )
    assert result["status"] == STATUS
    assert result["model"] == MODEL
    assert result["pixel_integration_and_psf_effective_grid"] == "not_established"
    assert result["cross_sensor_common_output_grid"] == "not_established"
    points = np.asarray(result["coordinates_deg_row_col_by_wavelength"])
    assert points.shape == (2, 2, 2, 2)
    # 0.5 mm distorted coordinate becomes 1.0 mm ideal at 450 nm.
    assert points[0, 0, 0].tolist() == pytest.approx(
        [-math.degrees(math.atan(0.001 / 0.02))] * 2
    )
    assert points[1, 0, 0].tolist() == pytest.approx(
        [-math.degrees(math.atan(0.0005 / 0.02))] * 2
    )
    assert result["min_adjacent_angular_step_deg_row_col_by_wavelength"][0] == pytest.approx(
        [2.0 * math.degrees(math.atan(0.001 / 0.02))] * 2
    )
    assert result["max_adjacent_angular_step_deg_row_col_by_wavelength"][0] == pytest.approx(
        result["min_adjacent_angular_step_deg_row_col_by_wavelength"][0]
    )


def test_midband_interpolates_the_same_strict_distortion_table_as_optics():
    result = strict_raytrace_pixel_center_angles(
        _raytrace(), sensor_shape=(2, 2),
        sensor_sample_size_m_row_col=(0.001, 0.001),
        image_distance_m=0.02, wavelength_nm=(500.0,),
    )
    # 500 nm distorted/ideal slope is 0.75, so 0.5 mm maps to 2/3 mm.
    corner = result["coordinates_deg_row_col_by_wavelength"][0][0][0]
    expected = -math.degrees(math.atan((0.0005 / 0.75) / 0.02))
    assert corner == pytest.approx([expected, expected])


def test_missing_field_support_and_nonmonotone_geometry_fail_closed():
    with pytest.raises(ValueError, match="exceed the lens distortion field support"):
        strict_raytrace_pixel_center_angles(
            _raytrace(), sensor_shape=(4, 4),
            sensor_sample_size_m_row_col=(0.001, 0.001),
            image_distance_m=0.02, wavelength_nm=(450.0,),
        )
    invalid = _raytrace()
    invalid["geometry"]["function"][2][0] = 0.4
    with pytest.raises(ValueError, match="geometry axes or distortion values"):
        strict_raytrace_pixel_center_angles(
            invalid, sensor_shape=(2, 2),
            sensor_sample_size_m_row_col=(0.001, 0.001),
            image_distance_m=0.02, wavelength_nm=(450.0,),
        )
    invalid = _raytrace()
    invalid["physics_policy"] = "approximate"
    with pytest.raises(ValueError, match="Only strict raytrace"):
        strict_raytrace_pixel_center_angles(
            invalid, sensor_shape=(2, 2),
            sensor_sample_size_m_row_col=(0.001, 0.001),
            image_distance_m=0.02, wavelength_nm=(450.0,),
        )
