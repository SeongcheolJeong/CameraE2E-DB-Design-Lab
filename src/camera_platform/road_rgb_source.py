"""RGB-derived spectral proxy tiles on the existing strict optical grid.

Only the original RGB raster is resident. No upsampled RGB or full spectral
cube is constructed. Camera intrinsics and Brown distortion preserve source
coordinates; lens geometry and radiometry remain CameraE2E operations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy.ndimage import map_coordinates

from .lens_sensor_scene_geometry_tile import (
    AnalyticStrictGeometryTileProvider,
    _rect,
    make_analytic_strict_geometry_provider,
)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=json_default,
        ).encode()
    ).hexdigest()


def json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def tangent_to_pixels(x, y, intrinsic, distortion):
    """Project camera-axis tangent coordinates using k1,k2,p1,p2,k3."""
    k = np.asarray(intrinsic, dtype=float)
    d = np.asarray(distortion, dtype=float)
    if k.shape != (3, 3) or d.shape != (5,) or not np.isfinite(k).all() or not np.isfinite(d).all():
        raise ValueError("Finite 3x3 intrinsics and five Brown coefficients are required.")
    if k[0, 0] <= 0 or k[1, 1] <= 0 or not np.array_equal(k[2], [0, 0, 1]):
        raise ValueError("Unsupported camera intrinsic matrix.")
    x, y = np.broadcast_arrays(np.asarray(x, float), np.asarray(y, float))
    k1, k2, p1, p2, k3 = d
    radius = x * x + y * y
    radial = 1 + radius * (k1 + radius * (k2 + radius * k3))
    xd = x * radial + 2 * p1 * x * y + p2 * (radius + 2 * x * x)
    yd = y * radial + p1 * (radius + 2 * y * y) + 2 * p2 * x * y
    return k[0, 0] * xd + k[0, 1] * yd + k[0, 2], k[1, 1] * yd + k[1, 2]


def pixels_to_tangent(u, v, intrinsic, distortion):
    """Invert Brown projection; refuse a nonconverged coordinate transform."""
    k = np.asarray(intrinsic, float)
    x = (np.asarray(u, float) - k[0, 2]) / k[0, 0]
    y = (np.asarray(v, float) - k[1, 2]) / k[1, 1]
    x, y = np.broadcast_arrays(x, y)
    x, y = x.copy(), y.copy()
    target_u, target_v = np.broadcast_arrays(u, v)
    epsilon = 1e-6
    for _ in range(15):
        pu, pv = tangent_to_pixels(x, y, intrinsic, distortion)
        ux, vx = tangent_to_pixels(x + epsilon, y, intrinsic, distortion)
        uy, vy = tangent_to_pixels(x, y + epsilon, intrinsic, distortion)
        a, b, c, d = (
            (ux - pu) / epsilon,
            (uy - pu) / epsilon,
            (vx - pv) / epsilon,
            (vy - pv) / epsilon,
        )
        det = a * d - b * c
        if np.any(abs(det) < 1e-9):
            raise ValueError("Singular camera distortion inverse.")
        du, dv = pu - target_u, pv - target_v
        x -= (d * du - b * dv) / det
        y -= (a * dv - c * du) / det
    pu, pv = tangent_to_pixels(x, y, intrinsic, distortion)
    if np.max(np.maximum(abs(pu - target_u), abs(pv - target_v))) > 1e-5:
        raise ValueError("Camera distortion inverse did not converge.")
    return x, y


def display_proxy_basis(workspace: Path, wavelength_nm, grey_luminance_cd_m2=100.0):
    """Use scene_from_file's display spectra/gamma, with one shared scale.

    The scale is defined by linear grey RGB=(.18,.18,.18), never by each
    photograph's mean or a simulated camera's output maximum.
    """
    from pyisetcam.color import luminance_from_energy
    from pyisetcam.display import display_get
    from pyisetcam.scene import _scene_display
    from pyisetcam.utils import energy_to_quanta

    from .offline_assets import offline_asset_store

    wave = np.asarray(wavelength_nm, float)
    assets = offline_asset_store(workspace=workspace, source=workspace / "camerae2e_db")
    with assets.bound_default(assets):
        # "default" has identical equal-energy primaries and destroys RGB
        # chromatic information. Bind an existing measured display asset.
        display = _scene_display("LCD-Apple", wave, asset_store=assets)
        spd = np.asarray(display_get(display, "spd"), float)
        gamma = np.asarray(display_get(display, "gamma table"), float)
        reference = float(luminance_from_energy(spd @ np.full(3, 0.18), wave, asset_store=assets))
    if not reference > 0 or not np.isfinite(reference):
        raise ValueError("Display reference luminance is invalid.")
    scale = float(grey_luminance_cd_m2) / reference
    primary_photons = energy_to_quanta(spd.T * scale, wave)
    return (
        primary_photons,
        gamma,
        {
            "kind": "RGB-derived spectral proxy",
            "display": display.name,
            "display_asset_sha256": file_sha256(assets.resolve("data/displays/LCD-Apple.mat")),
            "wavelength_nm": wave.tolist(),
            "linear_grey_rgb": [0.18] * 3,
            "linear_grey_luminance_cd_m2": grey_luminance_cd_m2,
            "energy_scale": scale,
            "primary_photons": primary_photons.tolist(),
            "gamma_table_sha256": hashlib.sha256(gamma.tobytes()).hexdigest(),
            "rgb_linearization": "scene_from_file display gamma table",
            "spatial_interpolation": "bilinear linear RGB; no recovered source detail",
            "original_blur_noise_sharpening_saturation_retained": True,
            "original_scene_spectrum_recovered": False,
            "original_capture_illuminance_recovered": False,
            "per_image_luminance_normalization": False,
        },
    )


@dataclass(frozen=True)
class RGBStrictGeometryTileProvider(AnalyticStrictGeometryTileProvider):
    """Reuse lens inverse geometry, PSF and sensor integration without recentering."""

    linear_rgb: np.ndarray
    primary_photons: np.ndarray
    intrinsic: np.ndarray
    distortion: np.ndarray
    source_description: dict[str, Any]

    @property
    def resident_source_payload_bytes(self):
        return (
            self.linear_rgb.nbytes
            + self.primary_photons.nbytes
            + self.intrinsic.nbytes
            + self.distortion.nbytes
        )

    @property
    def source_identity(self):
        return "sha256:" + json_sha256(
            {
                "source": self.source_description,
                "shape": self.global_shape,
                "fov": self.horizontal_fov_deg,
                "intrinsic": self.intrinsic.tolist(),
                "distortion": self.distortion.tolist(),
                "basis": self.primary_photons.tolist(),
            }
        )

    @property
    def metadata(self):
        return {
            **super().metadata,
            "schema": "camera_rgb_strict_geometry_tile_v1",
            "source_kind": "RGB-derived spectral proxy",
            "source_description": self.source_description,
            "source_cell_interpretation": "bilinear original linear RGB at global cell centers",
            "camera_intrinsic": self.intrinsic.tolist(),
            "camera_distortion": self.distortion.tolist(),
            "original_rgb_shape": list(self.linear_rgb.shape),
            "resident_rgb_bytes": self.linear_rgb.nbytes,
            "full_spectral_cube_materialized": False,
        }

    def read_scene_tile(self, r0, r1, c0, c1):
        r0, r1, c0, c1 = _rect((r0, r1, c0, c1), self.global_shape)
        if (r1 - r0) * (c1 - c0) > self.max_tile_cells:
            raise ValueError("RGB Scene tile exceeds the declared tile-cell policy.")
        rows, cols = self.global_shape
        step = 2 * np.tan(np.deg2rad(self.horizontal_fov_deg) / 2) / cols
        y, x = np.meshgrid(
            (np.arange(r0, r1) + 0.5 - rows / 2) * step,
            (np.arange(c0, c1) + 0.5 - cols / 2) * step,
            indexing="ij",
        )
        u, v = tangent_to_pixels(x, y, self.intrinsic, self.distortion)
        if (
            np.any(u < 0)
            or np.any(v < 0)
            or np.any(u > self.linear_rgb.shape[1] - 1)
            or np.any(v > self.linear_rgb.shape[0] - 1)
        ):
            raise ValueError("Requested source/halo extends outside the original RGB photo.")
        rgb = np.stack(
            [
                map_coordinates(
                    self.linear_rgb[..., channel],
                    [v, u],
                    order=1,
                    mode="constant",
                    prefilter=False,
                )
                for channel in range(3)
            ],
            axis=-1,
        )
        return rgb @ self.primary_photons


def make_rgb_strict_geometry_provider(
    optics,
    *,
    image_path,
    expected_sha256,
    intrinsic,
    distortion,
    primary_photons,
    gamma_table,
    scene_shape_row_col,
    scene_horizontal_fov_deg,
    wavelength_nm,
    source_description=None,
    max_tile_cells=1_048_576,
):
    from pyisetcam.scene import _display_linear_rgb, _prepare_display_image

    path = Path(image_path)
    if file_sha256(path) != expected_sha256:
        raise ValueError("Original RGB photo changed after specification freeze.")
    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB"))
    linear = _display_linear_rgb(_prepare_display_image(rgb, "rgb", 3), gamma_table)
    primary = np.asarray(primary_photons, float).copy()
    wave = np.asarray(wavelength_nm, float)
    if primary.shape != (3, len(wave)) or not np.isfinite(primary).all() or np.any(primary < 0):
        raise ValueError("Three nonnegative primary photon spectra are required.")
    k, d = np.asarray(intrinsic, float).copy(), np.asarray(distortion, float).copy()
    tangent_to_pixels(0.0, 0.0, k, d)
    base = make_analytic_strict_geometry_provider(
        optics,
        scene_shape_row_col=scene_shape_row_col,
        scene_horizontal_fov_deg=scene_horizontal_fov_deg,
        wavelengths_nm=wave,
        bright_side_photons=primary.sum(axis=0),
        analytic_slanted_edge_offset_deg=0.0,
        max_tile_cells=max_tile_cells,
    )
    for array in (linear, primary, k, d):
        array.setflags(write=False)
    return RGBStrictGeometryTileProvider(
        **{field.name: getattr(base, field.name) for field in fields(base)},
        linear_rgb=linear,
        primary_photons=primary,
        intrinsic=k,
        distortion=d,
        source_description={**(source_description or {}), "original_sha256": expected_sha256},
    )
