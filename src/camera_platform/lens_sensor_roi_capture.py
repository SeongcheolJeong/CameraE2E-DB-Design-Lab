"""Continue a globally computed central optical ROI through a fixed RAW sensor.

The optical ROI and its provenance are inputs from the strict optical tile
operator. This adapter does not build a full optical image, undo distortion,
or perform optical convolution. It delegates the actual pixel-aperture,
spectral QE, and digital readout operations to CameraE2E's sensor_compute.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from pyisetcam.optics import _strict_raytrace_cache_signature
from pyisetcam.sensor import sensor_compute
from pyisetcam.types import OpticalImage, Sensor

from .lens_sensor_centered_oi import _positive, _required_axis, _sensor_contract, _shape

SCHEMA = "camerae2e_strict_optical_roi_sensor_capture_v1"
_OPTICAL_MODE = "strict_bounded_source_tiles_optical_roi"


def _optical_contract(
    full_oi_fields: Mapping[str, Any],
    optical_roi_evidence: Mapping[str, Any],
    roi_photons: np.ndarray,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int], float, float, np.ndarray]:
    if optical_roi_evidence.get("mode") != _OPTICAL_MODE:
        raise ValueError("An executed strict optical ROI record is required.")
    if optical_roi_evidence.get("global_field_coordinates_preserved") is not True:
        raise ValueError("Optical ROI must preserve global field coordinates.")
    if optical_roi_evidence.get("sensor_or_isp_executed") is not False:
        raise ValueError("Optical ROI must precede sensor and ISP processing.")
    if optical_roi_evidence.get("full_source_cube_allocated_by_operator") is not False:
        raise ValueError(
            "Strict optical ROI must declare that the operator did not allocate a full source cube."
        )
    caller_residency = optical_roi_evidence.get("caller_source_cube_residency")
    if caller_residency not in {"unknown", "present"}:
        raise ValueError(
            "Strict optical ROI must declare known or unknown caller source residency."
        )
    if (
        optical_roi_evidence.get("caller_source_memory_qualified")
        is not (caller_residency == "present")
    ):
        raise ValueError("Caller source memory evidence disagrees with declared residency.")

    full_shape = _shape(
        optical_roi_evidence.get("global_grid_shape_row_col"), "Global OI shape"
    )
    bounds = optical_roi_evidence.get("output_roi_row_col")
    if (
        not isinstance(bounds, (tuple, list)) or len(bounds) != 4
        or any(type(value) is not int for value in bounds)
    ):
        raise ValueError("Optical ROI bounds must be four integer indices.")
    row0, row1, col0, col1 = bounds
    if not (0 <= row0 < row1 <= full_shape[0] and 0 <= col0 < col1 <= full_shape[1]):
        raise ValueError("Optical ROI bounds lie outside the global OI grid.")
    roi_shape = (row1 - row0, col1 - col0)
    if roi_shape[0] < 2 or roi_shape[1] < 2:
        raise ValueError("Optical ROI must have at least two samples per axis.")
    if 2 * row0 + roi_shape[0] != full_shape[0] or 2 * col0 + roi_shape[1] != full_shape[1]:
        raise ValueError("Off-center optical ROI cannot preserve the current sensor origin.")
    photons = np.asarray(roi_photons)
    if (
        photons.ndim != 3 or photons.shape[:2] != roi_shape
        or not (
            np.issubdtype(photons.dtype, np.floating)
            or np.issubdtype(photons.dtype, np.integer)
        )
        or not np.isfinite(photons).all()
    ):
        raise ValueError("Optical ROI photons must be finite real spectral samples.")

    wave = np.asarray(full_oi_fields.get("wave"), dtype=float)
    if (
        wave.ndim != 1 or wave.size != photons.shape[2] or wave.size < 1
        or not np.isfinite(wave).all() or np.any(np.diff(wave) <= 0)
    ):
        raise ValueError("Full OI wavelength axis must match the optical ROI planes.")
    record_wave = np.asarray(optical_roi_evidence.get("wavelength_nm"), dtype=float)
    if record_wave.shape != wave.shape or not np.array_equal(record_wave, wave):
        raise ValueError("Optical ROI wavelengths disagree with full OI metadata.")
    spacing = _positive(full_oi_fields.get("sample_spacing_m"), "Full OI spacing")
    record_spacing = _positive(
        optical_roi_evidence.get("sample_spacing_m"), "Optical ROI spacing"
    )
    if not math.isclose(spacing, record_spacing, rel_tol=1e-12, abs_tol=0):
        raise ValueError("Optical ROI spacing disagrees with full OI metadata.")
    distance = _positive(full_oi_fields.get("image_distance_m"), "Full OI image distance")
    for key, samples in (("width_m", full_shape[1]), ("height_m", full_shape[0])):
        extent = _positive(full_oi_fields.get(key), f"Full OI {key}")
        if not math.isclose(extent, samples * spacing, rel_tol=1e-10, abs_tol=1e-15):
            raise ValueError(f"Full OI {key} disagrees with the global optical grid.")
    for key, samples in (("fov_deg", full_shape[1]), ("vfov_deg", full_shape[0])):
        declared = full_oi_fields.get(key)
        expected = math.degrees(2 * math.atan(samples * spacing / (2 * distance)))
        if (
            type(declared) not in (int, float) or not math.isfinite(declared)
            or not math.isclose(float(declared), expected, rel_tol=1e-9, abs_tol=1e-10)
        ):
            raise ValueError(f"Full OI {key} disagrees with physical extent and distance.")
    return full_shape, roi_shape, (row0, col0), spacing, distance, wave


def capture_fixed_sensor_from_strict_optical_roi(
    sensor: Sensor,
    roi_photons: np.ndarray,
    *,
    full_oi_fields: Mapping[str, Any],
    optical_roi_evidence: Mapping[str, Any],
    seed: int | None = None,
) -> tuple[Sensor, dict[str, Any]]:
    """Capture a centered optical ROI without allocating the full OI array.

    Equivalence is to CameraE2E sensor_compute on the *same computed* full OI
    photons, sensor configuration, and seed. It does not qualify the source
    Scene resolution, full physical FOV, or optical boundary/halo.
    """
    full_shape, roi_shape, origin, spacing, distance, wave = _optical_contract(
        full_oi_fields, optical_roi_evidence, roi_photons
    )
    caller_residency = optical_roi_evidence["caller_source_cube_residency"]
    sensor_shape, pixel_size, subpixel_samples = _sensor_contract(sensor)
    dependency_by_axis = [
        _required_axis(full, target, spacing, float(pixel), subpixel_samples)
        for full, target, pixel in zip(full_shape, sensor_shape, pixel_size, strict=True)
    ]
    for axis, (start, extent, sensor_pixels, pixel) in enumerate(zip(
        origin, roi_shape, sensor_shape, pixel_size, strict=True
    )):
        first, last = dependency_by_axis[axis][
            "required_full_oi_source_indices_inclusive"
        ]
        if start > first or start + extent - 1 < last:
            raise ValueError(
                f"Optical ROI omits sensor interpolation/convolution halo on axis {axis}."
            )
        if extent * spacing + 1e-15 < sensor_pixels * float(pixel):
            raise ValueError(f"Optical ROI misses active pixel area on axis {axis}.")

    # The current spatial sensor operator is centered on the local OI array.
    # The exact central crop keeps local and global center coordinates equal.
    fields = {
        "wave": wave.copy(),
        "rows": roi_shape[0],
        "cols": roi_shape[1],
        "sample_spacing_m": spacing,
        "image_distance_m": distance,
        "width_m": roi_shape[1] * spacing,
        "height_m": roi_shape[0] * spacing,
        "fov_deg": math.degrees(2 * math.atan(roi_shape[1] * spacing / (2 * distance))),
        "vfov_deg": math.degrees(2 * math.atan(roi_shape[0] * spacing / (2 * distance))),
    }
    roi_oi = OpticalImage(fields=fields, data={"photons": np.asarray(roi_photons)})
    captured = sensor_compute(sensor, roi_oi, seed=seed)
    assert isinstance(captured, Sensor)
    evidence = {
        "schema": SCHEMA,
        "status": "fixed_sensor_capture_from_strict_optical_roi",
        "full_oi_shape_row_col": list(full_shape),
        "optical_roi_bounds_row_col_exclusive": [
            origin[0], origin[0] + roi_shape[0], origin[1], origin[1] + roi_shape[1]
        ],
        "optical_roi_shape_row_col": list(roi_shape),
        "sample_spacing_m": spacing,
        "sensor_shape_row_col": list(sensor_shape),
        "sensor_pixel_size_m_row_col": pixel_size.tolist(),
        "sensor_dependency_by_axis": dependency_by_axis,
        "noise_seed": captured.metadata.get("sensor_capture", {}).get("noise_seed"),
        "full_oi_photon_array_allocated_by_adapter": False,
        "full_source_cube_allocated_by_optical_operator": False,
        "caller_source_cube_residency": caller_residency,
        "caller_source_memory_qualified": caller_residency == "present",
        "upstream_full_source_cube_absence_verified": False,
        "scene_halo_qualified": bool(optical_roi_evidence.get("scene_halo_qualified")),
        "full_frame_execution_qualified": False,
        "comparison_scope": "sensor stage relative to identical computed full OI photons",
        "full_oi_mean_electron_parity_tested_in_this_capture": False,
    }
    captured.metadata["strict_optical_roi_sensor_capture"] = dict(evidence)
    return captured, evidence


def capture_fixed_sensor_from_optical_roi(
    optical_roi_array: np.ndarray,
    global_scene_shape: tuple[int, int],
    optical_roi_bounds: tuple[int, int, int, int],
    optics: dict[str, Any],
    wave: np.ndarray,
    sample_spacing_m: float,
    image_distance_m: float,
    sensor: Sensor,
    optical_evidence: Mapping[str, Any],
    seed: int | None = None,
) -> tuple[Sensor, dict[str, Any]]:
    """Bind the strict optical result, lens identity, and native RAW capture.

    ``global_scene_shape`` is the post-preparation optical input grid shape,
    before PSF padding. An upstream caller remains responsible for ensuring
    that Scene generation, radiometry, and distortion produced the declared
    global optical tile values; this binding checks optical provenance and
    sensor placement only.
    """
    scene_rows, scene_cols = _shape(global_scene_shape, "Global optical grid shape")
    if list(global_scene_shape) != optical_evidence.get("global_grid_shape_row_col"):
        raise ValueError("Global optical shape disagrees with optical ROI evidence.")
    if list(optical_roi_bounds) != optical_evidence.get("output_roi_row_col"):
        raise ValueError("Optical ROI bounds disagree with optical ROI evidence.")
    padding = optical_evidence.get("full_kernel_padding_row_col")
    if (
        not isinstance(padding, (tuple, list)) or len(padding) != 2
        or any(type(value) is not int or value < 0 for value in padding)
    ):
        raise ValueError("Strict optical padding must be two nonnegative integer widths.")
    pad_rows, pad_cols = padding
    full_rows, full_cols = scene_rows + 2 * pad_rows, scene_cols + 2 * pad_cols
    padded_bounds = [
        optical_roi_bounds[0] + pad_rows, optical_roi_bounds[1] + pad_rows,
        optical_roi_bounds[2] + pad_cols, optical_roi_bounds[3] + pad_cols,
    ]
    signature = _strict_raytrace_cache_signature(optics)
    if optical_evidence.get("source_signature") != signature:
        raise ValueError("Optical ROI source signature differs from the selected lens.")
    spacing = _positive(sample_spacing_m, "Global optical sample spacing")
    distance = _positive(image_distance_m, "Global OI image distance")
    fields = {
        "wave": np.asarray(wave, dtype=float),
        "width_m": full_cols * spacing,
        "height_m": full_rows * spacing,
        "sample_spacing_m": spacing,
        "image_distance_m": distance,
        "fov_deg": math.degrees(2 * math.atan(full_cols * spacing / (2 * distance))),
        "vfov_deg": math.degrees(2 * math.atan(full_rows * spacing / (2 * distance))),
    }
    padded_optical_evidence = {
        **optical_evidence,
        "global_grid_shape_row_col": [full_rows, full_cols],
        "output_roi_row_col": padded_bounds,
    }
    capture, evidence = capture_fixed_sensor_from_strict_optical_roi(
        sensor, optical_roi_array, full_oi_fields=fields,
        optical_roi_evidence=padded_optical_evidence, seed=seed,
    )
    evidence["geometry_psf_signature_verified"] = True
    evidence["geometry_psf_signature_sha256"] = signature
    evidence["geometry_psf_signature_scope"] = ["geometry", "psf"]
    evidence["full_optical_response_signature_verified"] = False
    evidence["optics_offaxis_method"] = optics.get("offaxis_method")
    evidence["unpadded_scene_shape_row_col"] = [scene_rows, scene_cols]
    evidence["unpadded_optical_roi_bounds_row_col_exclusive"] = list(optical_roi_bounds)
    evidence["strict_psf_padding_row_col"] = [pad_rows, pad_cols]
    capture.metadata["strict_optical_roi_sensor_capture"] = dict(evidence)
    return capture, evidence
