"""Wavelength-resolved 2D sensitivity, applied before spectral integration.

The ratio is a differential locally uniform, homogeneous-CFA 2D proxy. It does
not replace a qualified 3D response or provide a spatial crosstalk kernel.
"""
from __future__ import annotations

import numpy as np

CHANNELS = ("red", "green", "blue")
SITE_CHANNELS = ("red", "green", "green", "blue")
NODES = np.array([450., 550., 650.])


def spectral_gains(evidence, field_id, wavelengths):
    wave = np.asarray(wavelengths, dtype=float)
    if (wave.ndim != 1 or not len(wave) or not np.isfinite(wave).all()
            or np.any(np.diff(wave) <= 0) or wave[0] < NODES[0] or wave[-1] > NODES[-1]):
        raise ValueError("Ordered wavelengths within the computed 450–650 nm support required")
    rows = [r for r in evidence if r["field_id"] == field_id]
    gains = {}
    for channel in CHANNELS:
        data = [r for r in rows if r["cfa_channel"] == channel]
        if len(data) != 3 or sorted(r["wavelength_nm"] for r in data) != NODES.tolist():
            raise ValueError("All three computed wavelengths required for every CFA channel")
        data.sort(key=lambda r: r["wavelength_nm"])
        if any(not r["numerical_validity"] for r in data):
            raise ValueError("Numerically invalid spectral node cannot be used for camera projection")
        if any(r.get("response_gain") is None for r in data):
            raise ValueError("Weak-response spectral node has no reliable ratio; no unity fallback")
        baseline = np.array([r["baseline_absorption"] for r in data], dtype=float)
        ratio = np.array([r["response_gain"] for r in data], dtype=float)
        if not np.isfinite(baseline).all() or np.any(baseline <= 1e-6):
            raise ValueError("Vanishing off-band absorption cannot define a reliable ratio")
        if not np.isfinite(ratio).all() or np.any((ratio <= 0) | (ratio > 5)):
            raise ValueError("Invalid or extreme spectral response ratio")
        # Piecewise linear interpolation is explicit, bounded by sampled nodes.
        # Three wavelengths do not establish a continuous measured spectrum.
        gains[channel] = np.interp(wave, NODES, ratio)
    return np.stack([gains[c] for c in SITE_CHANNELS], axis=-1)


def integrate(spectral_planes, gain, *, cfa_origin=(0, 0)):
    """Return native RGGB mosaic and four-site means from [wave,row,col,site]."""
    values = np.asarray(spectral_planes, dtype=float)
    weights = np.asarray(gain, dtype=float)
    if (values.ndim != 4 or values.shape[-1] != 4 or weights.shape != (values.shape[0], 4)
            or not np.isfinite(values).all() or np.any(values < 0)
            or not np.isfinite(weights).all() or np.any(weights <= 0)):
        raise ValueError("Finite nonnegative wave/row/column/site rates and positive per-wave gains required")
    if len(cfa_origin) != 2 or any(type(v) is not int or v < 0 for v in cfa_origin):
        raise ValueError("Nonnegative integer CFA origin required")
    planes = np.sum(values * weights[:, None, None, :], axis=0)
    row, col = np.indices(planes.shape[:2])
    site = ((row + cfa_origin[0]) % 2) * 2 + (col + cfa_origin[1]) % 2
    mosaic = np.take_along_axis(planes, site[..., None], axis=-1)[..., 0]
    return mosaic, planes
