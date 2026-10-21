"""Executed optical sampling and mosaic-integration evidence for RAW previews.

These are numerical-model diagnostics.  A geometric lens PSF is an optical
pre-pixel response; the sensor's spatial operator is recorded separately so
that a later comparison cannot silently apply the pixel aperture twice.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.ndimage import map_coordinates

from pyisetcam.optics import _raytrace_psf_grid, rt_psf_interp


def _first_mtf50(kernel: np.ndarray, spacing_um: float, axis: int) -> float | None:
    profile = np.sum(kernel, axis=axis)
    profile = np.asarray(profile, dtype=float)
    if profile.size < 3 or not np.isfinite(profile).all() or profile.sum() <= 0:
        return None
    # A zero-padded discrete LSF transform, with the first 0.5 crossing only.
    transform = np.abs(np.fft.rfft(profile, n=4 * profile.size))
    transform /= transform[0]
    crossing = np.flatnonzero(transform <= 0.5)
    if crossing.size == 0 or crossing[0] == 0:
        return None
    upper = int(crossing[0])
    lower = upper - 1
    fraction = (0.5 - transform[lower]) / (transform[upper] - transform[lower])
    frequency = (lower + fraction) / (4 * profile.size * spacing_um * 1e-3)
    return float(frequency) if math.isfinite(frequency) else None


def _kernel_metrics(kernel: np.ndarray, pitch_row_col_um: tuple[float, float]) -> dict[str, Any]:
    values = np.asarray(kernel, dtype=float)
    if values.ndim != 2 or not np.isfinite(values).all() or np.any(values < 0):
        raise ValueError("Optical PSF values must be a finite nonnegative plane.")
    mass = float(values.sum())
    if mass <= 0:
        raise ValueError("Optical PSF has no positive sampled energy.")
    normalized = values / mass
    row_axis = ((values.shape[0] - 1) / 2 - np.arange(values.shape[0])) * pitch_row_col_um[0]
    col_axis = (np.arange(values.shape[1]) - (values.shape[1] - 1) / 2) * pitch_row_col_um[1]
    row_mean = float(np.sum(normalized * row_axis[:, None]))
    col_mean = float(np.sum(normalized * col_axis[None, :]))
    return {
        "shape_row_col": list(values.shape),
        "normalized_energy": float(normalized.sum()),
        "centroid_um_row_col": [row_mean, col_mean],
        "rms_width_um_row_col": [
            float(np.sqrt(np.sum(normalized * (row_axis[:, None] - row_mean) ** 2))),
            float(np.sqrt(np.sum(normalized * (col_axis[None, :] - col_mean) ** 2))),
        ],
        "central_lsf_mtf50_cycles_per_mm_row_col": [
            _first_mtf50(normalized, pitch_row_col_um[0], axis=1),
            _first_mtf50(normalized, pitch_row_col_um[1], axis=0),
        ],
    }


def optical_psf_sampling_evidence(
    optics: dict[str, Any], *, convolution_pitch_um: float,
    scene_shape_row_col: tuple[int, int], wavelengths_nm: tuple[float, ...],
) -> dict[str, Any]:
    """Recompute central pre-pixel PSF diagnostics at source and executed OI pitch.

    This calls the same strict PSF interpolation used by the streaming solver,
    but a central kernel is not a measurement of all local field kernels.
    """
    table = optics["raytrace"]["psf"]
    derived = optics["raytrace"].get("geometric_psf_rehistogram")
    source_pitch = tuple(float(value) * 1000 for value in np.asarray(
        table["sample_spacing_mm"], dtype=float
    ).reshape(-1))
    if len(source_pitch) != 2 or any(value <= 0 for value in source_pitch):
        raise ValueError("Stored optical PSF pitch is missing or invalid.")
    source_shape = np.asarray(table["function"]).shape[:2]
    if derived is not None and (
        not isinstance(derived, dict)
        or not isinstance(derived.get("derived_psf_pitch_um_row_col"), list)
        or len(derived["derived_psf_pitch_um_row_col"]) != 2
        or not np.allclose(
            derived["derived_psf_pitch_um_row_col"], source_pitch,
            rtol=1e-12, atol=0,
        )
        or derived.get("derived_psf_shape_row_col") != list(source_shape)
        or optics["raytrace"].get("asset_sha256")
        != derived.get("source_psf_asset_sha256")
    ):
        raise ValueError("Derived PSF sampling metadata differs from the executed table.")
    grid_table = dict(table, coordinate_convention="pixel_centers")
    cols_mm, rows_mm = _raytrace_psf_grid(convolution_pitch_um * 1e-3, grid_table)
    rows = rows_mm * 1e-3
    cols = cols_mm * 1e-3
    xx, yy = np.meshgrid(cols, rows)
    bands = []
    for wave in wavelengths_nm:
        source = rt_psf_interp(optics, wavelength_nm=wave)
        sampled = rt_psf_interp(
            optics, wavelength_nm=wave, x_grid_m=xx, y_grid_m=yy
        )
        source_rows = ((source.shape[0] - 1) / 2 - np.arange(source.shape[0])) * source_pitch[0]
        source_cols = (np.arange(source.shape[1]) - (source.shape[1] - 1) / 2) * source_pitch[1]
        row_coords = (yy * 1e6 - source_rows[0]) / (source_rows[1] - source_rows[0])
        col_coords = (xx * 1e6 - source_cols[0]) / source_pitch[1]
        resampled_before_unit_sum = map_coordinates(
            source, [row_coords, col_coords], order=1, mode="constant",
            cval=0.0, prefilter=False,
        )
        before_mass_ratio = (
            float(resampled_before_unit_sum.sum()) * convolution_pitch_um**2
            / (float(source.sum()) * source_pitch[0] * source_pitch[1])
        )
        if not np.allclose(
            sampled, resampled_before_unit_sum / resampled_before_unit_sum.sum(),
            rtol=2e-6, atol=2e-7,
        ):
            raise ValueError("Central PSF diagnostic does not match executed interpolation grid.")
        r0 = max(0, (sampled.shape[0] - scene_shape_row_col[0]) // 2)
        c0 = max(0, (sampled.shape[1] - scene_shape_row_col[1]) // 2)
        central_mass = float(sampled[
            r0:r0 + scene_shape_row_col[0], c0:c0 + scene_shape_row_col[1]
        ].sum())
        band = {
            "wavelength_nm": wave,
            "resampled_for_optical_convolution": _kernel_metrics(
                sampled, (convolution_pitch_um, convolution_pitch_um)
            ),
            "assumed_point_density_quadrature_ratio": before_mass_ratio,
            "quadrature_assumption_applicable_to_stored_ray_histogram": None,
            "unit_sum_is_only_dc_normalization": True,
            "central_source_window_energy_fraction": central_mass / float(sampled.sum()),
        }
        band[
            "derived_geometric_psf_table" if derived is not None else "source"
        ] = _kernel_metrics(source, source_pitch)
        bands.append(band)
    result = {
        "status": "central_geometric_pre_pixel_numeric_diagnostic",
        "sampling_model": "optical_psf_before_mosaic_pixel_integration",
        "optical_convolution_pitch_um_row_col": [convolution_pitch_um] * 2,
        "scene_shape_row_col": list(scene_shape_row_col),
        "bands": bands,
        "field_scope": "stored_axis_center_only_not_local_or_broadband_system_mtf",
        "rotation_clipping_and_full_field_energy_preservation": "not_assessed",
        "stored_psf_semantics": "bilinearly_deposited_geometric_ray_histogram",
        "physical_validation": "not_established",
    }
    if derived is None:
        result.update({
            "stored_psf_pitch_um_row_col": list(source_pitch),
            "stored_psf_shape_row_col": list(source_shape),
            "independent_ray_rehistogram_validation": "not_available",
        })
    else:
        result.update({
            "stored_psf_pitch_um_row_col": derived["source_psf_pitch_um_row_col"],
            "stored_psf_shape_row_col": derived["source_psf_shape_row_col"],
            "derived_psf_pitch_um_row_col": list(source_pitch),
            "derived_psf_shape_row_col": list(source_shape),
            "derived_psf_signature_sha256": derived["derived_psf_signature_sha256"],
            "independent_ray_rehistogram_validation": (
                "same_admitted_rays_all_stored_field_wavelength_nodes_reproduced"
            ),
            "rotation_clipping_and_full_field_energy_preservation": (
                "selected_outer_field_45_degree_diagnostic_only"
                if isinstance(
                    derived.get("selected_off_axis_45_degree_rotation_diagnostic"), dict
                ) else "not_assessed_for_this_derived_grid"
            ),
            "source_halo_qualified": False,
        })
    return result


def executed_mosaic_integration_evidence(sensor: Any, oi: Any) -> dict[str, Any]:
    """Record the actual sensor/OI operator selected by ``sensor.py``."""
    pixel = sensor.fields["pixel"]
    sample_size = [float(value) for value in np.asarray(pixel["size_m"]).reshape(-1)]
    oi_pitch = float(oi.fields["sample_spacing_m"])
    n = int(sensor.fields.get("n_samples_per_pixel", 1))
    kernel = [max(1, math.ceil(value / oi_pitch)) for value in sample_size]
    group = sensor.fields.get("ocl_group_proxy")
    group_mode = "off" if group is None else group.get("mode", "unknown")
    group_shape = [1, 1] if group is None else list(group.get("shape", (1, 1)))
    result = {
        "status": "executed_operator_parameters_recorded",
        "source_oi_shape_row_col": list(np.asarray(oi.data["photons"]).shape[:2]),
        "oi_pitch_um_row_col": [oi_pitch * 1e6] * 2,
        "native_pixel_sample_pitch_um_row_col": [value * 1e6 for value in sample_size],
        "mosaic": bool(sensor.fields.get("mosaic")),
        "n_samples_per_pixel": n,
        "gaussian_kernel_shape_row_col": kernel,
        "gaussian_kernel_sigma_samples": kernel[0] / 4.0,
        "interpolation": "bilinear_constant_zero",
        "cfa_selection": "after_per_spectral_plane_sampling",
        "ocl_group_proxy_mode": group_mode,
        "ocl_group_shape_row_col": group_shape,
        "fill_factor": float(pixel["fill_factor"]),
        "pixel_area_operator": (
            "fill_factor_times_pixel_area" if n == 1 else "pd_mask_then_subpixel_box_area"
        ),
        "pixel_aperture_already_in_optical_psf": False,
        "fdtd_sensor_attached": sensor.fields.get("fdtd_sensor") is not None,
        "tcad_sensor_attached": sensor.fields.get("tcad_sensor") is not None,
        "physical_pixel_response_validated": False,
    }
    if not result["mosaic"] or n < 1 or n % 2 == 0 or group_mode not in {"off", "uniform"}:
        raise ValueError("Executed mosaic spatial operator is unsupported or not identifiable.")
    return result
