"""Strict, signed native Bayer entry point using the existing ISP algorithms.

Kept separate from ip.py so adding this adapter does not invalidate stored
sensor-model runtime identities bound to that implementation file.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .ip import (
    _adaptive_laplacian_demosaic,
    _bayer_pattern_array,
    _ie_bilinear,
    _laplacian_demosaic,
)


def demosaic_bayer(
    mosaic: Any, method: str = "bilinear", *, cfa_pattern: str = "RGGB"
) -> np.ndarray:
    """Demosaic a signed native Bayer mosaic without color or display processing.

    This strict entry point never substitutes a different algorithm. The
    existing Bayer implementations retain their boundary behavior; callers
    comparing crops must exclude their declared interpolation support.
    """
    methods = {"bilinear", "laplacian", "adaptive_laplacian"}
    if not isinstance(method, str) or method not in methods:
        raise ValueError("Demosaic method must be bilinear, laplacian or adaptive_laplacian.")
    if not isinstance(cfa_pattern, str) or cfa_pattern not in {"RGGB", "GRBG", "BGGR", "GBRG"}:
        raise ValueError("Select an explicit RGGB, GRBG, BGGR or GBRG pattern.")
    if method == "adaptive_laplacian" and cfa_pattern == "GBRG":
        raise ValueError("Adaptive Laplacian has no declared GBRG path; no bilinear fallback.")
    values = np.asarray(mosaic)
    if (
        values.ndim != 2
        or values.dtype.kind not in "iuf"
        or min(values.shape) < 2
        or any(size % 2 for size in values.shape)
        or not np.isfinite(values).all()
    ):
        raise ValueError("Demosaic requires a finite numeric, even native two-dimensional mosaic.")
    if method != "bilinear" and min(values.shape) <= 8:
        raise ValueError("Laplacian demosaic requires more than eight samples per axis.")
    values = values.astype(np.float64, copy=False)
    pattern = _bayer_pattern_array(cfa_pattern.lower())
    site = np.tile(pattern, (values.shape[0] // 2, values.shape[1] // 2))
    planes = np.zeros((*values.shape, 3), dtype=np.float64)
    for channel in range(3):
        selected = site == channel + 1
        planes[..., channel][selected] = values[selected]
    if method == "bilinear":
        result = _ie_bilinear(planes, pattern)
    elif method == "laplacian":
        result = _laplacian_demosaic(planes, cfa_pattern.lower())
    else:
        result = _adaptive_laplacian_demosaic(planes, cfa_pattern.lower())
    if result.shape != (*values.shape, 3) or not np.isfinite(result).all():
        raise ValueError("Demosaic exceeded its finite native RGB output contract.")
    return result
