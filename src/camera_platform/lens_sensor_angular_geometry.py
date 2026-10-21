"""Model-space angular coordinates for strict raytrace sensor pixel centers.

This inverts the same piecewise-linear radial distortion table used by
``pyisetcam.optics._strict_raytrace_geometry``.  It describes where a *point*
at each sensor sample center maps into the input scene.  A captured RAW pixel
also integrates a finite area and receives energy from the PSF, so this map
does not make different sensor RAW arrays a common angular output grid.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

STATUS = "model_distortion_aware_pixel_centers_only"
MODEL = "strict_raytrace_radial_geometry_inverse_piecewise_linear"


def strict_raytrace_pixel_center_angles(
    raytrace: dict[str, Any],
    *,
    sensor_shape: tuple[int, int] | list[int],
    sensor_sample_size_m_row_col: tuple[float, float] | list[float],
    image_distance_m: float,
    wavelength_nm: tuple[float, ...] | list[float],
) -> dict[str, Any]:
    """Return distortion-aware input angles at the executed sensor centers.

    The positive row angle follows the simulator's array row direction.  The
    point center is the position used by the digital RAW regrid, not a
    pixel-area or PSF-effective receptive-field centroid.
    """
    if (
        not isinstance(sensor_shape, (tuple, list))
        or len(sensor_shape) != 2
        or any(type(value) is not int or value < 2 for value in sensor_shape)
    ):
        raise ValueError("Sensor shape must contain two integer dimensions of at least two.")
    size = np.asarray(sensor_sample_size_m_row_col, dtype=float)
    if size.shape != (2,) or not np.all(np.isfinite(size)) or np.any(size <= 0):
        raise ValueError("Executed sensor sample size must be two finite positive meters.")
    if not math.isfinite(image_distance_m) or image_distance_m <= 0:
        raise ValueError("Executed raytrace image distance must be finite positive meters.")
    if raytrace.get("physics_policy") != "strict":
        raise ValueError("Only strict raytrace geometry has a supported inverse contract.")
    table = raytrace.get("geometry")
    if not isinstance(table, dict) or table.get("field_coordinate") != "ideal_image_height":
        raise ValueError("Strict geometry must map ideal to distorted image height.")
    ideal = np.asarray(table.get("field_height_mm"), dtype=float)
    wavelengths = np.asarray(table.get("wavelength_nm"), dtype=float)
    distorted_by_field_wave = np.asarray(table.get("function"), dtype=float)
    requested = np.asarray(wavelength_nm, dtype=float)
    if (
        ideal.ndim != 1 or ideal.size < 2
        or wavelengths.ndim != 1 or wavelengths.size < 1
        or requested.ndim != 1 or requested.size < 1
        or distorted_by_field_wave.shape != (ideal.size, wavelengths.size)
        or not np.all(np.isfinite(ideal))
        or not np.all(np.isfinite(wavelengths))
        or not np.all(np.isfinite(requested))
        or not np.all(np.isfinite(distorted_by_field_wave))
        or abs(float(ideal[0])) > 1e-12
        or np.any(np.diff(ideal) <= 0)
        or np.any(np.diff(wavelengths) <= 0)
        or np.any(np.diff(requested) <= 0)
        or requested[0] < wavelengths[0]
        or requested[-1] > wavelengths[-1]
        or np.any(np.abs(distorted_by_field_wave[0]) > 1e-12)
        or np.any(np.diff(distorted_by_field_wave, axis=0) <= 0)
    ):
        raise ValueError("Strict raytrace geometry axes or distortion values are invalid.")

    # The RAW digital-regrid path uses pixel.size_m to place sample centers.
    # Convert to the geometry table's millimeter coordinate convention.
    rows, cols = sensor_shape
    row_mm = (np.arange(rows, dtype=float) - (rows - 1.0) / 2.0) * size[0] * 1e3
    col_mm = (np.arange(cols, dtype=float) - (cols - 1.0) / 2.0) * size[1] * 1e3
    yy, xx = np.meshgrid(row_mm, col_mm, indexing="ij")
    radius = np.hypot(yy, xx)
    image_distance_mm = image_distance_m * 1e3
    coordinates = []
    minimum_steps = []
    maximum_steps = []
    support = []
    for wave in requested:
        # pyisetcam's strict spectral table interpolates linearly by wavelength.
        distorted = np.asarray([
            np.interp(wave, wavelengths, distorted_by_field_wave[field])
            for field in range(ideal.size)
        ], dtype=float)
        if np.any(np.diff(distorted) <= 0):
            raise ValueError("Interpolated strict distortion is not monotone.")
        if float(np.max(radius)) > distorted[-1] + 1e-12:
            raise ValueError("Sensor pixel centers exceed the lens distortion field support.")
        input_radius = np.interp(radius, distorted, ideal)
        scale = np.divide(input_radius, radius, out=np.ones_like(radius), where=radius > 0)
        angle_row = np.degrees(np.arctan(yy * scale / image_distance_mm))
        angle_col = np.degrees(np.arctan(xx * scale / image_distance_mm))
        row_differences = np.diff(angle_row, axis=0)
        col_differences = np.diff(angle_col, axis=1)
        if np.any(row_differences <= 0) or np.any(col_differences <= 0):
            raise ValueError("Distortion-aware pixel-center angles are not monotone.")
        coordinates.append(np.stack((angle_row, angle_col), axis=-1).tolist())
        minimum_steps.append([float(row_differences.min()), float(col_differences.min())])
        maximum_steps.append([float(row_differences.max()), float(col_differences.max())])
        support.append({
            "ideal_image_height_mm": float(ideal[-1]),
            "distorted_image_height_mm": float(distorted[-1]),
        })

    return {
        "status": STATUS,
        "model": MODEL,
        "coordinate_convention": "row_positive_down_col_positive_right_degrees",
        "wavelength_nm": requested.tolist(),
        "sensor_shape": list(sensor_shape),
        "sensor_sample_size_m_row_col": size.tolist(),
        "image_distance_m": float(image_distance_m),
        "coordinates_deg_row_col_by_wavelength": coordinates,
        "min_adjacent_angular_step_deg_row_col_by_wavelength": minimum_steps,
        "max_adjacent_angular_step_deg_row_col_by_wavelength": maximum_steps,
        "distortion_table_support_by_wavelength": support,
        "pixel_integration_and_psf_effective_grid": "not_established",
        "cross_sensor_common_output_grid": "not_established",
    }
