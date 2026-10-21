"""Dense strict PSF spectral extraction preserves the full-array calculation."""

from __future__ import annotations

import numpy as np
import pytest

from pyisetcam import optics


def _old_full_array_reference(table: dict, wavelength_nm: float) -> np.ndarray:
    """Independent, pre-slice calculation retained as a numerical reference."""
    waves = np.asarray(table["wavelength_nm"], dtype=float)
    values = np.asarray(table["function"], dtype=float)
    if not np.isfinite(values).all() or np.any(values < 0):
        raise ValueError("Strict ray-trace tables must contain finite nonnegative values.")
    mass = values.sum(axis=(0, 1), keepdims=True)
    if np.any(mass <= 0):
        raise ValueError("Strict ray-trace PSFs must have positive energy in every kernel.")
    normalized = values / mass
    upper = min(int(np.searchsorted(waves, wavelength_nm, side="right")), waves.size - 1)
    lower = max(upper - 1, 0)
    if upper == lower:
        return normalized[..., lower].copy()
    weight = float(np.clip(
        (wavelength_nm - waves[lower]) / (waves[upper] - waves[lower]), 0.0, 1.0,
    ))
    return (1.0 - weight) * normalized[..., lower] + weight * normalized[..., upper]


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
@pytest.mark.parametrize("wavelength_nm", [450.0, 500.0, 550.0, 600.0, 650.0])
def test_strict_psf_requested_plane_matches_old_full_array(dtype, wavelength_nm):
    rng = np.random.default_rng(1325)
    values = rng.uniform(0.001, 2.0, size=(9, 7, 3, 3)).astype(dtype)
    values *= np.asarray([0.1, 4.0, 20.0], dtype=dtype)[None, None, None, :]
    table = {
        "function": values,
        "field_height_mm": np.asarray([0.0, 0.8, 1.6]),
        "wavelength_nm": np.asarray([450.0, 550.0, 650.0]),
    }
    expected = _old_full_array_reference(table, wavelength_nm)
    actual = optics._strict_raytrace_spectral(table, wavelength_nm, psf=True)
    np.testing.assert_array_equal(actual, expected)
    assert actual.shape == (9, 7, 3)
    assert actual.dtype == np.float64


@pytest.mark.parametrize("bad", ["nonfinite", "negative", "zero_mass"])
def test_strict_psf_validates_unrequested_wavelengths(bad):
    values = np.ones((5, 5, 2, 3), dtype=np.float32)
    if bad == "nonfinite":
        values[1, 1, 0, 2] = np.nan
    elif bad == "negative":
        values[1, 1, 0, 2] = -1
    else:
        values[:, :, 0, 2] = 0
    table = {
        "function": values,
        "field_height_mm": [0.0, 1.0],
        "wavelength_nm": [450.0, 550.0, 650.0],
    }
    with pytest.raises(ValueError, match="finite nonnegative|positive energy"):
        optics._strict_raytrace_spectral(table, 450.0, psf=True)


def test_strict_psf_does_not_cast_entire_lut_to_float64(monkeypatch):
    values = np.ones((17, 19, 2, 3), dtype=np.float32)
    table = {
        "function": values,
        "field_height_mm": [0.0, 1.0],
        "wavelength_nm": [450.0, 550.0, 650.0],
    }
    original_asarray = np.asarray
    cast_shapes = []

    def guarded_asarray(value, *args, **kwargs):
        dtype = args[0] if args else kwargs.get("dtype")
        if value is values and dtype is not None and np.dtype(dtype) == np.dtype(float):
            pytest.fail("The complete 4-D PSF must not be cast to float64.")
        if dtype is not None and np.dtype(dtype) == np.dtype(float):
            shape = getattr(value, "shape", None)
            if shape is not None:
                cast_shapes.append(shape)
        return original_asarray(value, *args, **kwargs)

    monkeypatch.setattr(optics.np, "asarray", guarded_asarray)
    actual = optics._strict_raytrace_spectral(table, 500.0, psf=True)
    assert actual.shape == (17, 19, 2)
    assert cast_shapes
    assert set(cast_shapes) == {(17, 19, 2)}


def test_non_psf_spectral_path_is_unchanged():
    table = {
        "function": np.asarray([[0.0, 2.0, 4.0], [1.0, 3.0, 5.0]], dtype=np.float32),
        "field_height_mm": [0.0, 1.0],
        "wavelength_nm": [450.0, 550.0, 650.0],
    }
    expected = np.asarray([1.0, 2.0], dtype=float)
    np.testing.assert_array_equal(optics._strict_raytrace_spectral(table, 500.0), expected)
