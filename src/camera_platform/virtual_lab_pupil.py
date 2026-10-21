"""Bounded conditional pupil-direction averaging for the Virtual Lab.

Every valid stored direction contributes its existing conditional weight. The
average multiplies saved normal QE once at the receiver field; it is not joint
PSF landing-position/angle transport or a new absolute-throughput calculation.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from .virtual_lab_angular import interpolate_relative

SCHEMA = "camera_virtual_lab_pupil_angular_v1"


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _array_sha(value):
    return hashlib.sha256(np.ascontiguousarray(value, dtype="<f8").tobytes()).hexdigest()


def build_pupil_table(optics, angular_table, wave_nm, *, azimuth_samples=16):
    """Return wavelength × ideal field × row-down azimuth × RGGB gain table.

    Intermediate wavelengths blend separately averaged source-node ensembles,
    each evaluated at the requested wavelength. Ray masks/IDs are never mixed.
    """
    ray = optics.get("raytrace", {})
    if "image_direction_cosines" not in ray or "pupil_weights" not in ray:
        raise ValueError("Pupil averaging requires saved lens pupil directions and weights")
    psf = ray["psf"]
    source_contract = ray.get("pupil_ray_data", {})
    if psf.get("field_coordinate") != "ideal_image_height":
        raise ValueError("Pupil averaging requires declared ideal image-height field coordinates")
    if source_contract.get("direction_basis") != (
        "image Cartesian: +x radial, +y upward, +z propagation"
    ):
        raise ValueError("Pupil directions have an unsupported or missing coordinate basis")
    if source_contract.get("weights") != (
        "conditional uniform geometric quadrature over admitted rays"
    ):
        raise ValueError("Pupil weights must declare the supported conditional geometric basis")
    if source_contract.get("axes") != ["field_height", "wavelength", "pupil_sample", "xyz"]:
        raise ValueError("Pupil direction array axes are missing or unsupported")
    fields = np.asarray(psf["field_height_mm"], float)
    source_wave = np.asarray(psf["wavelength_nm"], float)
    waves = np.asarray(wave_nm, float)
    if (
        waves.ndim != 1
        or not waves.size
        or not np.isfinite(waves).all()
        or np.any(np.diff(waves) <= 0)
        or waves[0] < source_wave[0]
        or waves[-1] > source_wave[-1]
    ):
        raise ValueError("Pupil wavelengths must be increasing within the saved lens support")
    if type(azimuth_samples) is not int or not 4 <= azimuth_samples <= 64:
        raise ValueError("Pupil field azimuth_samples must be an integer from4 to64")
    directions = np.asarray(ray["image_direction_cosines"], float)
    weights = np.asarray(ray["pupil_weights"], float)
    if (
        directions.ndim != 4
        or directions.shape[:2] != (fields.size, source_wave.size)
        or directions.shape[-1] != 3
        or weights.shape != directions.shape[:-1]
        or not directions.shape[2]
    ):
        raise ValueError(
            "Pupil direction/weight dimensions do not match lens field/wavelength axes"
        )
    if not np.isfinite(weights).all() or np.any(weights < 0):
        raise ValueError("Pupil weights must be finite nonnegative conditional weights")
    valid = weights > 0
    count = valid.sum(axis=2)
    if np.any(count == 0):
        raise ValueError("Every stored field/wavelength needs a positive-weight pupil ensemble")
    selected = directions[valid]
    norms = np.linalg.norm(selected, axis=1)
    if (
        not np.isfinite(selected).all()
        or np.any(selected[:, 2] <= 0)
        or not np.allclose(norms, 1, rtol=1e-6, atol=1e-8)
    ):
        raise ValueError("Positive-weight pupil directions must be finite forward unit vectors")
    all_theta = np.degrees(np.arccos(np.clip(selected[:, 2] / norms, -1, 1)))
    azimuth = np.arange(azimuth_samples) * 360.0 / azimuth_samples
    rotations = np.radians(azimuth)
    cos, sin = np.cos(rotations), np.sin(rotations)
    gain = np.empty((waves.size, fields.size, azimuth_samples, 4), float)
    for wi, wavelength in enumerate(waves):
        upper = min(int(np.searchsorted(source_wave, wavelength)), source_wave.size - 1)
        lower = max(0, upper - 1)
        if source_wave[upper] == wavelength:
            lower = upper
        mix = (
            0.0
            if lower == upper
            else float(
                (wavelength - source_wave[lower]) / (source_wave[upper] - source_wave[lower])
            )
        )
        nodes = [(lower, 1 - mix)] if lower == upper else [(lower, 1 - mix), (upper, mix)]
        for fi in range(fields.size):
            averaged = np.zeros((azimuth_samples, 4), float)
            for source_index, fraction in nodes:
                if fraction == 0:
                    continue
                mask = valid[fi, source_index]
                vectors = directions[fi, source_index, mask]
                normalized = weights[fi, source_index, mask]
                normalized = normalized / normalized.sum()
                vectors = vectors / np.linalg.norm(vectors, axis=1)[:, None]
                theta = np.degrees(np.arccos(np.clip(vectors[:, 2], -1, 1)))
                # Rotate the optical+y-up vector by -sensor_azimuth, then
                # convert its y component to the sensor's increasing row.
                dx = vectors[:, 0, None] * cos + vectors[:, 1, None] * sin
                dy_down = vectors[:, 0, None] * sin - vectors[:, 1, None] * cos
                phi = np.mod(np.degrees(np.arctan2(dy_down, dx)), 360)
                relative = interpolate_relative(
                    angular_table, theta[:, None], phi, float(wavelength)
                )
                averaged += fraction * np.einsum("n,nac->ac", normalized, relative)
            gain[wi, fi] = averaged
    sampling = {
        "source_pupil_sample_count": int(directions.shape[2]),
        "valid_ray_count_min": int(count.min()),
        "valid_ray_count_max": int(count.max()),
        "valid_ray_counts_field_wave": count.tolist(),
        "field_count": int(fields.size),
        "source_wavelength_count": int(source_wave.size),
        "requested_wavelength_count": int(waves.size),
        "azimuth_samples": azimuth_samples,
        "direction_theta_deg_min": float(all_theta.min()),
        "direction_theta_deg_max": float(all_theta.max()),
        "all_positive_weight_rays_used": True,
        "pupil_subsampling": False,
    }
    result = {
        "schema": SCHEMA,
        "wavelength_nm": waves.tolist(),
        "field_height_mm": fields.tolist(),
        "field_azimuth_deg": azimuth.tolist(),
        "site_names": ["R", "Gr", "Gb", "B"],
        "gain_r_gr_gb_b": gain.tolist(),
        "sampling": sampling,
        "provenance": {
            "lens_asset_sha256": ray.get("asset_sha256"),
            "direction_cosines_sha256": _array_sha(directions),
            "pupil_weights_sha256": _array_sha(weights),
            "relative_angular_table_sha256": angular_table.get("table_sha256"),
            "source_wavelength_nm": source_wave.tolist(),
            "source_pupil_contract": source_contract,
            "conditional_weight_sums_field_wave": weights.sum(axis=2).tolist(),
            "weighting": (
                "saved conditional weights normalized per field/wavelength; "
                "no extra cosine or throughput"
            ),
            "wavelength_interpolation": (
                "mix separately averaged source-node ensembles evaluated at requested wavelength"
            ),
            "field_interpolation": (
                "linear interpolation of averaged response, not corresponding ray directions"
            ),
            "azimuth_interpolation": (
                "periodic linear interpolation; finite field-azimuth quadrature"
            ),
            "coordinate_convention": "stored+x field / optical+y_up to sensor row-down azimuth",
            "model": "field_separable_conditional_pupil_angular_average",
            "joint_psf_angle_transport": False,
            "absolute_throughput_claim": False,
            "normal_qe_applied_once": True,
            "on_axis_renormalized": False,
        },
    }
    result["table_sha256"] = _digest(result)
    return result


def pupil_gain_map(optics, table, shape, spacing_mm, wavelength_nm):
    """Interpolate bounded receiver stripes; never allocate image×pupil data."""
    from pyisetcam.optics import _strict_raytrace_geometry_axes

    ray = optics.get("raytrace", {})
    if table.get("schema") != SCHEMA:
        raise ValueError("Unsupported pupil response table")
    if ray.get("asset_sha256") != table["provenance"].get("lens_asset_sha256"):
        raise ValueError("Pupil response table belongs to a different lens asset")
    waves = np.asarray(table["wavelength_nm"], float)
    fields = np.asarray(table["field_height_mm"], float)
    azimuth = np.asarray(table["field_azimuth_deg"], float)
    values = np.asarray(table["gain_r_gr_gb_b"], float)
    if wavelength_nm < waves[0] or wavelength_nm > waves[-1]:
        raise ValueError("Pupil response wavelength would extrapolate")
    upper = min(int(np.searchsorted(waves, wavelength_nm)), waves.size - 1)
    lower = max(0, upper - 1)
    if waves[upper] == wavelength_nm:
        spectral = values[upper]
    else:
        fraction = (wavelength_nm - waves[lower]) / (waves[upper] - waves[lower])
        spectral = values[lower] * (1 - fraction) + values[upper] * fraction
    periodic = np.concatenate([spectral, spectral[:, :1]], axis=1)
    lookup = RegularGridInterpolator((fields, np.r_[azimuth, 360.0]), periodic, bounds_error=True)
    ideal, distorted = _strict_raytrace_geometry_axes(ray, float(wavelength_nm))
    rows, cols = shape
    maxradius = np.hypot((rows - 1) / 2, (cols - 1) / 2) * spacing_mm
    if (
        maxradius > distorted[-1] + 1e-9
        or np.interp(maxradius, distorted, ideal) > fields[-1] + 1e-9
    ):
        raise ValueError("Pupil response receiver field would extrapolate")
    result = np.empty((rows, cols, 4), float)
    x = (np.arange(cols) + 0.5 - cols / 2) * spacing_mm
    for start in range(0, rows, 128):
        stop = min(start + 128, rows)
        y = (np.arange(start, stop) + 0.5 - rows / 2) * spacing_mm
        xx, yy = np.meshgrid(x, y)
        radius = np.hypot(xx, yy)
        height = np.interp(radius, distorted, ideal)
        phi = np.mod(np.degrees(np.arctan2(yy, xx)), 360)
        result[start:stop] = lookup(np.stack([height, phi], axis=-1))
    return result
