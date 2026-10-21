"""Bounded approximate source-subcell to receiver-integrated PSF transfer.

The supplied PSF is already integrated over each receiving pixel. Bilinear
translation interpolates that *integrated impulse* at a fractional source
position; it does not apply a second receiver aperture or claim to recover an
unmeasured continuous-position PSF.
"""

from __future__ import annotations

import numpy as np

MODEL = "native_sensor_area_midpoint_retained_subcells_v1"
TRANSFER = "bilinear_receiver_integrated_impulse_v1"
MAX_FFT_WORK = 500_000_000
MAX_RETAINED_NPY_BYTES = 8 * 1024 * 1024


def validate_subcells(subcells, mean, side):
    """Check immutable renderer output against its native-cell mean."""
    values = np.asarray(subcells)
    average = np.asarray(mean)
    if (type(side) is not int or side not in (2, 4, 8, 16)
            or values.dtype.kind not in "uif" or values.ndim != 5
            or values.shape != (*average.shape[:2], side, side, average.shape[2])
            or average.ndim != 3 or average.dtype.kind not in "uif"
            or values.nbytes > MAX_RETAINED_NPY_BYTES - 1024):
        raise ValueError("Retained source-subcell radiance has invalid shape or numeric type")
    if (not np.isfinite(values).all() or np.any(values < 0)
            or not np.isfinite(average).all() or np.any(average < 0)):
        raise ValueError("Retained source-subcell radiance must be finite and nonnegative")
    reconstructed = values.astype(np.float64).mean(axis=(2, 3))
    if not np.allclose(reconstructed, average, rtol=2e-14, atol=1e-300):
        raise ValueError("Retained source-subcell radiance differs from native-cell mean")
    return values.astype(np.float64, copy=False)


def preflight(shape, side, kernels, pitch_um, full, origin, *, field_axes=None):
    """Reject oversized work and missing fractional-position field coverage."""
    from scipy.fft import next_fast_len

    rows, cols, bands = shape
    if (type(side) is not int or side not in (2, 4, 8, 16)
            or not np.isfinite(pitch_um) or pitch_um <= 0
            or any(type(value) is not int for value in (*full, *origin))
            or any(o < 0 or o + n > f for o, n, f in
                   zip(origin, (rows, cols), full, strict=True))):
        raise ValueError("Subcell transfer requires a bounded native source grid")
    kshape = np.shape(kernels)
    field = field_axes is not None
    if ((field and len(kshape) != 5) or (not field and len(kshape) != 3)
            or kshape[0] != bands or any(n < 1 or n % 2 == 0 for n in kshape[-2:])):
        raise ValueError("Subcell transfer PSF shape differs from the spectral source")
    if field:
        if (not isinstance(field_axes, (tuple, list)) or len(field_axes) != 2
                or kshape[1:3] != tuple(len(axis) for axis in field_axes)):
            raise ValueError("Subcell field PSF axes differ from kernel nodes")
        checked_axes = []
        for axis in field_axes:
            values = np.asarray(axis)
            if (values.ndim != 1 or values.dtype.kind not in "uif"
                    or not 2 <= len(values) <= 9 or not np.isfinite(values).all()
                    or not np.all(np.diff(values.astype(np.float64)) > 0)):
                raise ValueError("Subcell field PSF axes require ordered finite nodes")
            checked_axes.append(values.astype(np.float64))
        field_axes = checked_axes
    source_bytes = rows * cols * side**2 * bands * 8
    if source_bytes > MAX_RETAINED_NPY_BYTES - 1024:
        raise ValueError("Retained subcell source exceeds 8 MiB artifact budget")
    nodes = kshape[1] * kshape[2] if field else 1
    fft_shape = (
        next_fast_len(rows + kshape[-2] - 1, real=True),
        next_fast_len(cols + kshape[-1] - 1, real=True),
    )
    fft_samples = int(np.prod(fft_shape))
    convolutions = side**2 * bands * nodes
    work = int(np.ceil(3 * convolutions * fft_samples
                       * np.log2(max(fft_samples, 2))))
    if work > MAX_FFT_WORK:
        raise ValueError("Subcell PSF transfer exceeds FFT work budget")
    if field:
        for axis, native, offset, length in zip(
            field_axes, full, origin, (rows, cols), strict=True
        ):
            first = (offset + 0.5 / side - native / 2) * pitch_um / 1000
            last = (offset + length - 0.5 / side - native / 2) * pitch_um / 1000
            if axis[0] > first or axis[-1] < last:
                raise ValueError("Field PSF does not cover fractional source positions")
    return {
        "fft_work_units_upper_bound": work,
        "fft_work_units_limit": MAX_FFT_WORK,
        "fft_convolution_upper_bound": convolutions,
        "retained_source_bytes": source_bytes,
        "full_output_bytes": (rows + kshape[-2] + 1)
        * (cols + kshape[-1] + 1) * bands * 8,
        "fft_working_array_bytes_estimate": 12 * fft_samples * 8,
    }


def _node_weights(axis, positions):
    low = np.clip(np.searchsorted(axis, positions, side="right") - 1, 0,
                  len(axis) - 2)
    fraction = (positions - axis[low]) / (axis[low + 1] - axis[low])
    weights = np.zeros((len(axis), len(positions)), dtype=np.float64)
    weights[low, np.arange(len(positions))] = 1 - fraction
    weights[low + 1, np.arange(len(positions))] = fraction
    return weights


def transfer(subcells, kernels, pitch_um, full, origin, *, field_axes=None):
    """Return native-crop irradiance and a full/support/native flux ledger.

    `subcells` is [source row, source col, subrow, subcol, wavelength], already
    after absolute on-axis projection in W/m²/nm. The caller may separately
    weight each source subcell by Lens relative illumination before transfer;
    this operator itself applies no RI, sensor QE, or further receiver aperture.
    """
    from scipy.signal import fftconvolve

    source = np.asarray(subcells)
    if source.nbytes > MAX_RETAINED_NPY_BYTES - 1024:
        raise ValueError("Retained subcell source exceeds 8 MiB artifact budget")
    source = source.astype(np.float64, copy=False)
    if source.ndim != 5 or not np.isfinite(source).all() or np.any(source < 0):
        raise ValueError("Subcell irradiance requires finite nonnegative 5-D values")
    rows, cols, side, other_side, bands = source.shape
    if side != other_side:
        raise ValueError("Subcell irradiance requires a square quadrature grid")
    kernel = np.asarray(kernels, dtype=np.float64)
    if (not np.isfinite(kernel).all() or np.any(kernel < 0)
            or not np.allclose(kernel.sum(axis=(-2, -1)), 1, rtol=0, atol=1e-10)):
        raise ValueError("Subcell PSF kernels must be finite nonnegative unit-sum")
    limits = preflight((rows, cols, bands), side, kernel, pitch_um, full, origin,
                       field_axes=field_axes)
    ky, kx = kernel.shape[-2] // 2, kernel.shape[-1] // 2
    py, px = ky + 1, kx + 1
    padded = np.zeros((rows + 2 * py, cols + 2 * px, bands), dtype=np.float64)
    axes = tuple(np.asarray(axis, dtype=np.float64) for axis in field_axes) if field_axes else None
    weight = 1.0 / side**2
    trimmed = np.zeros(bands)
    for sy in range(side):
        dy = (sy + 0.5) / side - 0.5
        shift_y = int(np.floor(dy))
        frac_y = dy - shift_y
        if axes is not None:
            y_position = (origin[0] + np.arange(rows) + dy - (full[0] - 1) / 2)
            y_weights = _node_weights(axes[0], y_position * pitch_um / 1000)
        for sx in range(side):
            dx = (sx + 0.5) / side - 0.5
            shift_x = int(np.floor(dx))
            frac_x = dx - shift_x
            if axes is not None:
                x_position = (origin[1] + np.arange(cols) + dx - (full[1] - 1) / 2)
                x_weights = _node_weights(axes[1], x_position * pitch_um / 1000)
            start_y, start_x = 1 + shift_y, 1 + shift_x
            conv_shape = (rows + 2 * ky, cols + 2 * kx)
            for band in range(bands):
                plane = source[:, :, sy, sx, band] * weight
                if not np.any(plane):
                    continue
                convolution = np.zeros(conv_shape, dtype=np.float64)
                if axes is None:
                    terms = ((plane, kernel[band]),)
                else:
                    terms = (
                        (plane * y_weights[node_y, :, None] * x_weights[node_x],
                         kernel[band, node_y, node_x])
                        for node_y in range(kernel.shape[1])
                        for node_x in range(kernel.shape[2])
                    )
                for weighted, impulse in terms:
                    if not np.any(weighted):
                        continue
                    contribution = fftconvolve(weighted, impulse, mode="full")
                    tolerance = max(float(weighted.max()), 1e-300) * 1e-12
                    if (not np.isfinite(contribution).all()
                            or float(contribution.min()) < -tolerance):
                        raise RuntimeError("Subcell PSF FFT produced nonphysical irradiance")
                    trimmed[band] += float(-np.minimum(contribution, 0).sum())
                    convolution += np.maximum(contribution, 0)
                end_y, end_x = start_y + conv_shape[0], start_x + conv_shape[1]
                padded[start_y:end_y, start_x:end_x, band] += (
                    convolution * (1 - frac_y) * (1 - frac_x)
                )
                padded[start_y + 1:end_y + 1, start_x:end_x, band] += (
                    convolution * frac_y * (1 - frac_x)
                )
                padded[start_y:end_y, start_x + 1:end_x + 1, band] += (
                    convolution * (1 - frac_y) * frac_x
                )
                padded[start_y + 1:end_y + 1, start_x + 1:end_x + 1, band] += (
                    convolution * frac_y * frac_x
                )
    support = padded[py:py + rows, px:px + cols].copy()
    native_y = np.arange(padded.shape[0]) - py + origin[0]
    native_x = np.arange(padded.shape[1]) - px + origin[1]
    native = padded[np.ix_((native_y >= 0) & (native_y < full[0]),
                           (native_x >= 0) & (native_x < full[1]))]
    area = (pitch_um * 1e-6) ** 2
    input_flux = source.sum(axis=(0, 1, 2, 3)) * weight * area
    full_flux = padded.sum(axis=(0, 1)) * area
    native_flux = native.sum(axis=(0, 1)) * area
    support_flux = support.sum(axis=(0, 1)) * area
    if not np.allclose(full_flux, input_flux, rtol=1e-9, atol=1e-300):
        raise RuntimeError("Subcell optical transfer failed full-plane flux conservation")
    ledger = {
        "unit": "W/nm",
        "input_spectral_flux": input_flux.tolist(),
        "full_convolution_spectral_flux": full_flux.tolist(),
        "retained_crop_spectral_flux": support_flux.tolist(),
        "outside_crop_spectral_flux": np.maximum(full_flux - support_flux, 0).tolist(),
        "native_sensor_spectral_flux": native_flux.tolist(),
        "outside_native_sensor_spectral_flux": np.maximum(full_flux - native_flux, 0).tolist(),
        "fft_negative_roundoff_trimmed_spectral_flux": (trimmed * area).tolist(),
        "scope": "Before relative illumination; fractional-source interpolation of a "
        "receiver-integrated unit-sum PSF; no second receiver aperture",
        "source_subcell_transfer": {"model": TRANSFER, "grid_side": side, **limits},
    }
    return support, ledger
