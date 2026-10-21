"""Explicit native-grid ISP chart and co-located model references.

The chirp extends analytically into optical support; it is not a crop resized
after convolution. Co-located color planes are model diagnostics, not captures.
"""

from __future__ import annotations

import numpy as np

SCHEMA = "camera_reference_isp_chirp_v1"
CHART_SPECIFICATION = {
    "schema": SCHEMA,
    "frequency_norm_cycles_per_native_pixel": [0.02, 0.45],
    "direction_deg": 45,
    "reflectance_bias": 0.35,
    "reflectance_amplitude": 0.25,
    "source_sampling": "native_cell_center_with_analytic_halo",
    "continuous_optical_convergence": "not_established",
}


def _geometry(geometry):
    rows, cols = map(int, geometry["native_shape_row_col"])
    height, width = map(int, geometry["input_shape_row_col"])
    bounds = geometry["crop_bounds_row_col"]
    if len(bounds) == 2:
        bounds = [*bounds[0], *bounds[1]]
    r0, r1, c0, c1 = map(int, bounds)
    if (
        min(rows, cols) < 8
        or (r1 - r0, c1 - c0) != (rows, cols)
        or not (0 <= r0 < r1 <= height and 0 <= c0 < c1 <= width)
    ):
        raise ValueError("Chirp requires a supported native crop inside its declared halo")
    return rows, cols, height, width, r0, r1, c0, c1


def chirp_pattern(geometry, *, full_support=False):
    """Diagonal chirp whose native frequency norm increases from .02 to .45.

    Frequencies are held at their endpoint outside the crop, with continuous
    phase and frequency. The native center coordinates do not change with halo.
    """
    rows, cols, height, width, r0, r1, c0, c1 = _geometry(geometry)
    yy, xx = np.indices((height, width), dtype=float)
    coordinate = ((xx - c0) + (yy - r0)) / np.sqrt(2)
    length = (rows + cols - 2) / np.sqrt(2)
    position = np.clip(coordinate, 0, length)
    slope = (0.45 - 0.02) / length
    phase = 0.02 * position + 0.5 * slope * position**2
    phase += 0.02 * np.minimum(coordinate, 0) + 0.45 * np.maximum(coordinate - length, 0)
    values = 0.35 + 0.25 * np.cos(2 * np.pi * phase)
    return values if full_support else values[r0:r1, c0:c1].copy()


def chirp_scene(geometry, spectral):
    """Return full-support spectral photons and native scalar reflectance."""
    white = np.asarray(spectral["white"], float)
    if white.shape != (21,) or not np.isfinite(white).all() or np.any(white < 0):
        raise ValueError("Reference chirp requires the fixed nonnegative 21-band illuminant")
    pattern = chirp_pattern(geometry, full_support=True)
    _, _, _, _, r0, r1, c0, c1 = _geometry(geometry)
    return pattern[..., None] * white, pattern[r0:r1, c0:c1].copy()


def mosaic_from_planes(planes_electron_rate, *, origin=(0, 0)):
    """Sample R/Gr/Gb/B model planes at the declared native RGGB sites."""
    planes = np.asarray(planes_electron_rate, float)
    phase = np.asarray(origin)
    if (
        planes.ndim != 3
        or planes.shape[-1] != 4
        or not np.isfinite(planes).all()
        or phase.shape != (2,)
        or not np.isfinite(phase.astype(float)).all()
        or not np.array_equal(phase, phase.astype(int))
    ):
        raise ValueError("Require finite co-located R/Gr/Gb/B planes and integer CFA origin")
    row, col = np.indices(planes.shape[:2])
    site = ((row + phase[0]) % 2) * 2 + ((col + phase[1]) % 2)
    return np.take_along_axis(planes, site.astype(int)[..., None], axis=-1)[..., 0]


def colocated_reference(planes_electron_rate, exposure_s, matrix, *, gains=None, wb=None, ccm=None):
    """Signed model RGB after the same fixed linear corrections, without CFA loss.

    ``gains`` accepts four co-located planes, four channel constants, or a native
    map that is constant within every CFA site (the Reference field LSC policy).
    No denoising, sharpening, clipping or tone is included in this reference.
    """
    planes = np.asarray(planes_electron_rate, float)
    matrix = np.asarray(matrix, float)
    exposure_s = float(exposure_s)
    if (
        planes.ndim != 3
        or planes.shape[-1] != 4
        or not np.isfinite(planes).all()
        or np.any(planes < 0)
        or not np.isfinite(exposure_s)
        or exposure_s <= 0
        or matrix.shape != (3, 3)
        or not np.isfinite(matrix).all()
    ):
        raise ValueError(
            "Reference requires nonnegative rate planes, positive exposure and 3x3 matrix"
        )
    electrons = planes * exposure_s
    if gains is not None:
        gains = np.asarray(gains, float)
        if gains.shape == planes.shape[:2]:
            if min(gains.shape) < 2:
                raise ValueError("Native LSC requires all four CFA sites")
            constants = np.array([gains[0, 0], gains[0, 1], gains[1, 0], gains[1, 1]])
            for site, value in enumerate(constants):
                if not np.allclose(
                    gains[site // 2 :: 2, site % 2 :: 2], value, rtol=1e-12, atol=1e-12
                ):
                    raise ValueError(
                        "Spatially varying native LSC needs explicit co-located four-site maps"
                    )
            gains = constants
        if (
            gains.shape not in {(4,), planes.shape}
            or not np.isfinite(gains).all()
            or np.any(gains <= 0)
        ):
            raise ValueError(
                "Co-located correction requires four positive site gains at each position"
            )
        electrons = electrons * gains
    rgb = electrons[..., [0, 1, 3]].copy()
    rgb[..., 1] = electrons[..., 1:3].mean(axis=-1)
    result = rgb @ matrix
    if wb is not None:
        wb = np.asarray(wb, float)
        if wb.shape != (3,) or not np.isfinite(wb).all():
            raise ValueError("White balance must contain three finite gains")
        result *= wb
    if ccm is not None:
        ccm = np.asarray(ccm, float)
        if ccm.shape != (3, 3) or not np.isfinite(ccm).all():
            raise ValueError("Manual color transform must be finite 3x3")
        result = result @ ccm.T
    return result
