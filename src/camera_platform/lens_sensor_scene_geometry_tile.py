"""On-demand analytic Scene and strict geometric OI tiles on one global grid.

This is the Scene -> geometric/distorted optical input boundary only.  It does
not execute a PSF, pixel integration, readout, or ISP.  ``read_tile`` returns
the same *pre-PSF* OI quantity as ``pyisetcam.optics.rt_geometry`` for a
continuous D65 slanted-edge source whose bright-side photon spectrum is
declared.  Output and source query coordinates always refer to the complete
Scene, so a cropped tile is never recentered.
"""

from __future__ import annotations

import copy
import hashlib
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy.ndimage import map_coordinates

from pyisetcam.optics import (
    _image_distance_m,
    _linspace_window,
    _radiance_to_irradiance,
    _raytrace_is_strict,
    _strict_raytrace_axis,
    _strict_raytrace_field_positions,
    _strict_raytrace_geometry_axes,
    _strict_raytrace_spectral,
    _validate_strict_raytrace_optics,
)
from pyisetcam.scene import scene_adjust_luminance, scene_interpolate_w
from pyisetcam.sensor_comparison import _make_scene
from pyisetcam.types import Scene
from pyisetcam.utils import param_format

from .offline_assets import offline_asset_store

SCHEMA = "camera_lens_sensor_analytic_strict_geometry_tiles_v1"
DEFAULT_MAX_TILE_CELLS = 1_048_576
_DARK_SIDE_FRACTION = 0.02
_SLANTED_EDGE_ROW_SLOPE = 1.0 / 12.0
_SOURCE_RASTERIZATIONS = {"point_sampled", "cell_area_integrated"}


def _slanted_half_plane_cell_area(threshold: np.ndarray) -> np.ndarray:
    """Exact bright area of ``u - v / 12 > threshold`` for a unit square.

    ``u`` and ``v`` are continuous column and row positions within each
    Scene cell. The integral is analytic and tile-local; no subpixel array or
    full Scene is allocated. A half-plane boundary has zero area, so its
    strict/closed inequality convention does not affect the result.
    """
    slope = _SLANTED_EDGE_ROW_SLOPE
    fraction = np.zeros_like(threshold, dtype=float)
    fraction[threshold <= -slope] = 1.0
    crossing = (threshold > -slope) & (threshold < 1.0)
    if np.any(crossing):
        t = threshold[crossing]
        # Integrate clip(1 - t - slope*v, 0, 1) for v in [0, 1].
        integral = (
            np.maximum(1.0 - t, 0.0) ** 2
            - np.maximum(1.0 - t - slope, 0.0) ** 2
            - np.maximum(-t, 0.0) ** 2
            + np.maximum(-t - slope, 0.0) ** 2
        ) / (2.0 * slope)
        fraction[crossing] = np.clip(integral, 0.0, 1.0)
    return fraction


def _optical_state_fingerprint(optics: dict[str, Any]) -> str:
    """Hash the complete copied optics state used by this provider.

    This is an in-process mutation check, distinct from a released lens asset
    digest. Unsupported Python objects are rejected rather than silently
    assigning them an unstable string representation.
    """
    digest = hashlib.sha256()

    def frame(tag: bytes, payload: bytes | memoryview) -> None:
        digest.update(tag)
        digest.update(len(payload).to_bytes(8, "little"))
        digest.update(payload)

    def update(value: Any) -> None:
        if isinstance(value, np.generic):
            update(value.item())
        elif isinstance(value, Mapping):
            digest.update(b"{")
            if any(not isinstance(key, str) for key in value):
                raise ValueError("Optics state fingerprint requires string mapping keys.")
            for key in sorted(value):
                frame(b"K", key.encode("utf-8"))
                update(value[key])
            digest.update(b"}")
        elif isinstance(value, np.ndarray):
            if value.dtype.hasobject:
                raise ValueError("Optics state fingerprint cannot accept object arrays.")
            array = np.ascontiguousarray(value)
            digest.update(b"A")
            frame(b"D", array.dtype.str.encode("ascii"))
            frame(b"S", repr(array.shape).encode("ascii"))
            frame(b"B", memoryview(array).cast("B"))
        elif isinstance(value, (tuple, list)):
            digest.update(b"T" if isinstance(value, tuple) else b"L")
            digest.update(len(value).to_bytes(8, "little"))
            for item in value:
                update(item)
        elif value is None:
            digest.update(b"N")
        elif isinstance(value, bool):
            digest.update(b"Y" if value else b"Z")
        elif isinstance(value, int):
            frame(b"I", str(value).encode("ascii"))
        elif isinstance(value, float):
            frame(b"F", struct.pack("<d", value))
        elif isinstance(value, str):
            frame(b"U", value.encode("utf-8"))
        elif isinstance(value, bytes):
            frame(b"B", value)
        else:
            raise ValueError(f"Unsupported optics state value: {type(value).__name__}.")

    update(optics)
    return f"sha256:{digest.hexdigest()}"


def _shape(value: tuple[int, int] | list[int]) -> tuple[int, int]:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 2
        or any(type(item) is not int or item < 2 for item in value)
    ):
        raise ValueError("scene_shape_row_col requires two integers >= 2.")
    return value[0], value[1]


def _rect(
    value: tuple[int, int, int, int] | list[int], shape: tuple[int, int]
) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 4
        or any(type(item) is not int for item in value)
    ):
        raise ValueError("Tile bounds require four integer half-open row/column limits.")
    r0, r1, c0, c1 = value
    if not (0 <= r0 < r1 <= shape[0] and 0 <= c0 < c1 <= shape[1]):
        raise ValueError("Tile bounds must lie inside the global Scene/OI grid.")
    return r0, r1, c0, c1


def d65_analytic_bright_photons(
    *,
    workspace: str | Path,
    horizontal_fov_deg: float,
    luminance_cd_m2: float,
    wavelengths_nm: tuple[float, ...] | list[float] | np.ndarray,
) -> np.ndarray:
    """Make the existing comparison Scene's bright D65 spectrum in 32x32.

    The current full-grid analytic edge first creates a uniform D65 Scene,
    normalizes it to 1 cd/m2, interpolates its wavelengths, and then sets the
    bright-side luminance before rasterizing.  Repeating those operations on
    a small uniform Scene avoids allocating the requested high-resolution
    raster.  Tiny floating reductions can depend on Scene size, so callers
    needing bit-for-bit replay should supply their pinned bright spectrum.
    """
    wave = _strict_raytrace_axis(wavelengths_nm, "requested wavelength axis")
    luminance = float(luminance_cd_m2)
    if not math.isfinite(luminance) or luminance <= 0:
        raise ValueError("luminance_cd_m2 must be finite and positive.")
    fov = float(horizontal_fov_deg)
    if not math.isfinite(fov) or not 0.001 <= fov < 180.0:
        raise ValueError("horizontal_fov_deg must be within [0.001, 180).")
    assets = offline_asset_store(workspace=workspace)
    with assets.bound_default(assets):
        scene, _ = _make_scene("uniform", (16, 16), fov, assets, source_size=(32, 32))
        scene_interpolate_w(scene, wave, asset_store=assets)
        scene_adjust_luminance(scene, luminance, asset_store=assets)
        photons = np.asarray(scene.data["photons"], dtype=float)
        if not np.all(photons == photons[0, 0]):
            raise ValueError("The pinned D65 bright Scene is not spatially uniform.")
        return photons[0, 0].copy()


@dataclass(frozen=True)
class AnalyticStrictGeometryTileProvider:
    """Provide global-coordinate Scene and pre-PSF geometry tiles on demand."""

    optics: dict[str, Any]
    global_shape: tuple[int, int]
    horizontal_fov_deg: float
    vertical_fov_deg: float
    wavelengths_nm: np.ndarray
    bright_photons: np.ndarray
    edge_offset_deg: float
    source_rasterization: str
    oi_pitch_um: float
    oi_pitch_mm: float
    offaxis_method: str
    max_tile_cells: int
    optics_state_sha256: str

    @property
    def source_identity(self) -> str:
        """Hash the numerical analytic source, independent of lens identity."""
        digest = hashlib.sha256(SCHEMA.encode("ascii"))
        digest.update(np.asarray(self.global_shape, dtype="<i8").tobytes())
        digest.update(
            np.asarray(
                [self.horizontal_fov_deg, self.vertical_fov_deg, self.edge_offset_deg],
                dtype="<f8",
            ).tobytes()
        )
        digest.update(np.asarray(self.wavelengths_nm, dtype="<f8").tobytes())
        digest.update(np.asarray(self.bright_photons, dtype="<f8").tobytes())
        if self.source_rasterization != "point_sampled":
            # Keep existing point-sampled identities stable for saved jobs.
            digest.update(b"cell_area_integrated_v1")
        return f"sha256:{digest.hexdigest()}"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "source_identity": self.source_identity,
            "full_optical_state_sha256": self.optics_state_sha256,
            "global_shape_row_col": list(self.global_shape),
            "source_kind": (
                "continuous_tangent_plane_slanted_half_plane_cell_area_v1"
                if self.source_rasterization == "cell_area_integrated"
                else "continuous_tangent_plane_slanted_half_plane_v1"
            ),
            "source_rasterization": self.source_rasterization,
            "source_cell_interpretation": (
                "exact_half_plane_area_average_before_geometry_interpolation"
                if self.source_rasterization == "cell_area_integrated"
                else "cell_center_point_sample_before_geometry_interpolation"
            ),
            "horizontal_fov_deg": self.horizontal_fov_deg,
            "vertical_fov_deg": self.vertical_fov_deg,
            "edge_offset_deg": self.edge_offset_deg,
            "dark_side_fraction": _DARK_SIDE_FRACTION,
            "wavelength_nm": self.wavelengths_nm.tolist(),
            "oi_pitch_um": self.oi_pitch_um,
            "geometry": "strict_raytrace_inverse_map_and_relative_illumination",
            "pregeometry_offaxis_method": self.offaxis_method,
            "source_boundary": "global_constant_zero",
            "full_scene_materialized": False,
            "psf_sensor_isp_executed": False,
        }

    def read_scene_tile(self, r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        """Return the exact analytic raster cells at global Scene indices."""
        r0, r1, c0, c1 = _rect((r0, r1, c0, c1), self.global_shape)
        rows, cols = self.global_shape
        if (r1 - r0) * (c1 - c0) > self.max_tile_cells:
            raise ValueError("Scene tile exceeds the declared numerical tile-cell guard.")
        row, col = np.indices((r1 - r0, c1 - c0), dtype=float)
        row += r0
        col += c0
        step = 2 * math.tan(math.radians(self.horizontal_fov_deg) / 2) / cols
        offset = math.tan(math.radians(self.edge_offset_deg))
        if self.source_rasterization == "point_sampled":
            # Preserve the historical numerical operation order and parity.
            edge = ((col + 0.5 - cols / 2) - (row + 0.5 - rows / 2) / 12) * step
            mask = np.where(edge > offset, 1.0, _DARK_SIDE_FRACTION)
        else:
            cell_origin = (col - cols / 2) - (row - rows / 2) / 12
            threshold = offset / step - cell_origin
            bright_area = _slanted_half_plane_cell_area(threshold)
            mask = _DARK_SIDE_FRACTION + (1.0 - _DARK_SIDE_FRACTION) * bright_area
        return np.broadcast_to(
            self.bright_photons, (r1 - r0, c1 - c0, self.wavelengths_nm.size)
        ).copy() * mask[..., None]

    def _cos4th_tile(self, r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        """Apply ``_cos4th_factor`` on global source coordinates, pre-warp."""
        rows, cols = self.global_shape
        image_distance_m = _image_distance_m(
            self.optics, Scene(fields={"wave": self.wavelengths_nm}, data={})
        )
        width_m = 2 * image_distance_m * np.tan(np.deg2rad(self.horizontal_fov_deg) / 2)
        height_m = 2 * image_distance_m * np.tan(np.deg2rad(self.vertical_fov_deg) / 2)
        focal_length_m = float(self.optics["focal_length_m"])
        x = _linspace_window(
            -width_m / 2 + width_m / (2 * cols),
            width_m / 2 - width_m / (2 * cols),
            cols, c0, c1,
        )
        y = _linspace_window(
            -height_m / 2 + height_m / (2 * rows),
            height_m / 2 - height_m / (2 * rows),
            rows, r0, r1,
        )
        xx, yy = np.meshgrid(x, y)
        s_factor = np.sqrt(focal_length_m**2 + xx**2 + yy**2)
        image_diagonal = np.sqrt(width_m**2 + height_m**2)
        if focal_length_m > 10 * image_diagonal:
            return (focal_length_m / np.maximum(s_factor, 1e-12)) ** 4
        magnification = float(self.optics["raytrace"].get("magnification", 0.0))
        cos_phi = focal_length_m / np.maximum(s_factor, 1e-12)
        sin_phi = np.sqrt(np.clip(1.0 - cos_phi**2, 0.0, None))
        tan_phi = sin_phi / np.maximum(cos_phi, 1e-12)
        f_number = float(self.optics["f_number"])
        sin_theta = 1.0 / (1.0 + 4.0 * (f_number * (1.0 - magnification)) ** 2)
        cos_theta = np.sqrt(max(1e-12, 1.0 - sin_theta**2))
        tan_theta = sin_theta / cos_theta
        numerator = 1.0 - tan_theta**2 + tan_phi**2
        denominator = np.sqrt(
            tan_phi**4 + 2.0 * tan_phi**2 * (1.0 - tan_theta**2) + 1.0 / (cos_theta**4)
        )
        spatial_fall = (np.pi / 2.0) * (
            1.0 - numerator / np.maximum(denominator, 1e-12)
        )
        return spatial_fall / max(np.pi * (sin_theta**2), 1e-12)

    def _query(
        self, r0: int, r1: int, c0: int, c1: int, wavelength_nm: float
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        rows, cols = self.global_shape
        spacing_mm = self.oi_pitch_mm
        x_start = -(cols * spacing_mm) / 2.0 + spacing_mm / 2.0
        x_stop = (cols * spacing_mm) / 2.0 - spacing_mm / 2.0
        y_start = -(rows * spacing_mm) / 2.0 + spacing_mm / 2.0
        y_stop = (rows * spacing_mm) / 2.0 - spacing_mm / 2.0
        x = _linspace_window(x_start, x_stop, cols, c0, c1)
        y = _linspace_window(y_start, y_stop, rows, r0, r1)
        xx, yy = np.meshgrid(x, y)
        radius = np.hypot(xx, yy)
        raytrace = self.optics["raytrace"]
        ideal, distorted = _strict_raytrace_geometry_axes(raytrace, wavelength_nm)
        table = raytrace["relative_illumination"]
        ri = _strict_raytrace_spectral(table, wavelength_nm)
        ri_fields = _strict_raytrace_field_positions(raytrace, table, wavelength_nm)
        valid = (radius <= distorted[-1] + 1e-12) & (radius <= ri_fields[-1] + 1e-12)
        input_radius = np.interp(radius, distorted, ideal)
        factor = np.divide(input_radius, radius, out=np.ones_like(radius), where=radius > 0.0)
        query_rows = (rows - 1.0) / 2.0 + yy * factor / spacing_mm
        query_cols = (cols - 1.0) / 2.0 + xx * factor / spacing_mm
        return query_rows, query_cols, np.interp(radius, ri_fields, ri), valid

    def read_tile(self, r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        """Return a post-geometry/pre-PSF OI tile at global pixel centers.

        Each wavelength's strict inverse distortion identifies the source
        Scene samples.  Their union is generated once, including bilinear
        neighbors.  SciPy's constant-zero interpolation remains referenced to
        the *global* Scene boundary, not the temporary source rectangle.
        """
        if _optical_state_fingerprint(self.optics) != self.optics_state_sha256:
            raise ValueError("The provider's copied optics changed after construction.")
        return self._read_tile_after_integrity_preflight(r0, r1, c0, c1)

    def _read_tile_after_integrity_preflight(
        self, r0: int, r1: int, c0: int, c1: int,
    ) -> np.ndarray:
        """Read one tile while an enclosing operation checks full optics at both ends."""
        r0, r1, c0, c1 = _rect((r0, r1, c0, c1), self.global_shape)
        if (r1 - r0) * (c1 - c0) > self.max_tile_cells:
            raise ValueError("Geometry tile exceeds the declared numerical tile-cell guard.")
        rows, cols = self.global_shape
        source_r0, source_c0 = rows, cols
        source_r1 = source_c1 = 0
        for wavelength in self.wavelengths_nm:
            query_r, query_c, _, field_valid = self._query(
                r0, r1, c0, c1, float(wavelength)
            )
            in_bounds = (
                field_valid
                & (query_r >= 0.0)
                & (query_r <= rows - 1)
                & (query_c >= 0.0)
                & (query_c <= cols - 1)
            )
            if not np.any(in_bounds):
                continue
            source_r0 = min(source_r0, max(0, math.floor(float(query_r[in_bounds].min()))))
            source_r1 = max(source_r1, min(rows, math.ceil(float(query_r[in_bounds].max())) + 1))
            source_c0 = min(source_c0, max(0, math.floor(float(query_c[in_bounds].min()))))
            source_c1 = max(source_c1, min(cols, math.ceil(float(query_c[in_bounds].max())) + 1))

        result = np.zeros((r1 - r0, c1 - c0, self.wavelengths_nm.size), dtype=float)
        if source_r1 <= source_r0 or source_c1 <= source_c0:
            return result
        scene_tile = self.read_scene_tile(source_r0, source_r1, source_c0, source_c1)
        scene_fields = {
            "wave": self.wavelengths_nm,
            "distance_m": self.optics["raytrace"]["object_distance_m"],
        }
        irradiance = _radiance_to_irradiance(
            scene_tile, self.optics, Scene(fields=scene_fields, data={})
        )
        if self.offaxis_method == "cos4th":
            irradiance = irradiance * self._cos4th_tile(
                source_r0, source_r1, source_c0, source_c1
            )[:, :, None]
        for band, wavelength in enumerate(self.wavelengths_nm):
            query_r, query_c, ri, valid = self._query(
                r0, r1, c0, c1, float(wavelength)
            )
            image = map_coordinates(
                irradiance[:, :, band],
                [query_r - source_r0, query_c - source_c0],
                order=1,
                mode="constant",
                cval=0.0,
                prefilter=False,
            )
            result[:, :, band] = image * ri * valid
        return result


def make_analytic_strict_geometry_provider(
    optics: dict[str, Any],
    *,
    scene_shape_row_col: tuple[int, int] | list[int],
    scene_horizontal_fov_deg: float,
    wavelengths_nm: tuple[float, ...] | list[float] | np.ndarray,
    bright_side_photons: tuple[float, ...] | list[float] | np.ndarray,
    analytic_slanted_edge_offset_deg: float,
    source_rasterization: str = "point_sampled",
    declared_oi_pitch_um: float | None = None,
    max_tile_cells: int = DEFAULT_MAX_TILE_CELLS,
) -> AnalyticStrictGeometryTileProvider:
    """Bind one immutable numeric Scene description to strict raytrace optics.

    ``bright_side_photons`` is spectral *Scene radiance in photons*, after the
    same D65 interpolation and bright-side luminance normalization used by the
    full path.  It must not be a sensor-plane irradiance or a pixel-integrated
    quantity.  The factory does not materialize an array of global Scene size.
    """
    rows, cols = _shape(scene_shape_row_col)
    if not isinstance(optics, dict):
        raise ValueError("Strict raytrace optics must be a dictionary.")
    copied_optics = copy.deepcopy(optics)
    fov = float(scene_horizontal_fov_deg)
    offset = float(analytic_slanted_edge_offset_deg)
    if not math.isfinite(fov) or not 0.001 <= fov < 180.0:
        raise ValueError("scene_horizontal_fov_deg must be within [0.001, 180).")
    if not math.isfinite(offset) or abs(offset) > fov / 2:
        raise ValueError(
            "analytic_slanted_edge_offset_deg must lie within the declared "
            "Scene horizontal half-FOV."
        )
    if type(source_rasterization) is not str or source_rasterization not in _SOURCE_RASTERIZATIONS:
        raise ValueError(
            "source_rasterization must be point_sampled or cell_area_integrated."
        )
    if type(max_tile_cells) is not int or max_tile_cells < 1:
        raise ValueError("max_tile_cells must be a positive integer.")
    wave = _strict_raytrace_axis(wavelengths_nm, "requested wavelength axis")
    bright = np.asarray(bright_side_photons, dtype=float).reshape(-1)
    if bright.shape != wave.shape or not np.all(np.isfinite(bright)) or np.any(bright < 0):
        raise ValueError("bright_side_photons must match wavelengths and be finite, nonnegative.")
    if copied_optics.get("model") != "raytrace" or not _raytrace_is_strict(
        copied_optics.get("raytrace", {})
    ):
        raise ValueError("Only strict raytrace optics have the required geometry contract.")
    offaxis_method = param_format(copied_optics.get("offaxis_method", "cos4th"))
    if offaxis_method not in {"skip", "cos4th"}:
        raise ValueError("Only skip and cos4th off-axis methods have tiled parity support.")
    _validate_strict_raytrace_optics(copied_optics)
    image_distance_m = _image_distance_m(copied_optics, Scene(fields={"wave": wave}, data={}))
    if not math.isfinite(image_distance_m) or image_distance_m <= 0:
        raise ValueError("Strict raytrace image distance must be positive and finite.")
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(fov) / 2) * rows / cols))
    diagonal = math.degrees(
        2 * math.atan(
            math.hypot(math.tan(math.radians(fov) / 2), math.tan(math.radians(vfov) / 2))
        )
    )
    if diagonal > float(copied_optics["raytrace"].get("max_fov_deg", math.inf)) + 1e-9:
        raise ValueError("Scene field of view exceeds the loaded ray-trace analysis.")
    width_m = 2 * image_distance_m * np.tan(np.deg2rad(fov) / 2)
    pitch_mm = width_m * 1e3 / cols
    pitch_um = pitch_mm * 1e3
    if declared_oi_pitch_um is not None:
        declared = float(declared_oi_pitch_um)
        if not math.isfinite(declared) or not math.isclose(
            pitch_um, declared, rel_tol=1e-9, abs_tol=1e-9
        ):
            raise ValueError("Declared OI pitch differs from the global Scene/raytrace grid.")
    frozen_wave = wave.copy()
    frozen_bright = bright.copy()
    frozen_wave.setflags(write=False)
    frozen_bright.setflags(write=False)
    return AnalyticStrictGeometryTileProvider(
        optics=copied_optics,
        global_shape=(rows, cols),
        horizontal_fov_deg=fov,
        vertical_fov_deg=vfov,
        wavelengths_nm=frozen_wave,
        bright_photons=frozen_bright,
        edge_offset_deg=offset,
        source_rasterization=source_rasterization,
        oi_pitch_um=pitch_um,
        oi_pitch_mm=pitch_mm,
        offaxis_method=offaxis_method,
        max_tile_cells=max_tile_cells,
        optics_state_sha256=_optical_state_fingerprint(copied_optics),
    )
