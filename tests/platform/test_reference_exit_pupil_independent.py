"""Independent scalar propagation, sign and pupil-measure checks."""

import math

import numpy as np
import pytest
from numpy.polynomial.legendre import leggauss
from scipy.special import j1

from camera_platform import prescription_psf
from camera_platform import reference_exit_pupil as exit_pupil


def test_unconverged_patent_chief_aim_is_corrected_without_changing_apertures(monkeypatch):
    from camera_platform import lens_psf_lut as lut
    from rayoptics.raytr import trace
    from rayoptics.raytr.opticalspec import Field
    solver=lut.PatentSolver('.',lut.specification({}))
    model=prescription_psf._model(solver.request,0)
    field=Field(y=1.6774236376290472,fov=model.optical_spec['fov'])
    apertures=[s.max_aperture for s in model.seq_model.ifcs]
    monkeypatch.setattr(trace,'aim_chief_ray',lambda *a,**k:np.array([0.,8.20391165]))
    diagnostic=exit_pupil.aim_at_stop(model,field,450.)
    assert diagnostic['method']=='bracketed_stop_intercept'
    assert abs(diagnostic['original_stop_residual_mm'])>18
    assert abs(diagnostic['stop_residual_mm'])<=1e-6
    assert [s.max_aperture for s in model.seq_model.ifcs]==apertures


def _centroid(mass, pitch):
    coordinate = (np.arange(len(mass)) - len(mass) // 2) * pitch
    return np.array([mass.sum(0) @ coordinate, mass.sum(1) @ coordinate]) / mass.sum()


@pytest.mark.parametrize("shift", [(0.0, 0.0), (8.7, -5.8)])
def test_continuous_airy_integrals_and_signed_two_axis_shift(shift):
    """Independent Bessel intensity and 24-point Gauss receiver quadrature."""
    count, radius, span, wave, pitch, size = 512, 0.1, 0.24, 550.0, 2.9, 41
    axis = (np.arange(count) - count // 2) * span / count
    x, y = np.meshgrid(axis, axis)
    power = (x * x + y * y <= radius**2).astype(float)
    pupil = {
        "power": power,
        "opd_nm": -(x * shift[0] + y * shift[1]) * 1000,
        "spacing": span / count,
    }
    actual = exit_pupil.propagate(pupil, wave, size, pitch, 11)
    nodes, weights = leggauss(24)
    centers = (np.arange(size) - size // 2) * pitch
    physical_x = centers[None, :, None, None] + nodes[None, None, None, :] * pitch / 2
    physical_y = -centers[:, None, None, None] + nodes[None, None, :, None] * pitch / 2
    radial = np.hypot(physical_x - shift[0], physical_y - shift[1])
    wavelength_um = wave * 1e-3
    z = 2 * np.pi * radius * radial / wavelength_um
    amplitude = np.divide(2 * j1(z), z, out=np.ones_like(z), where=z != 0)
    area_weights = weights[None, None, :, None] * weights[None, None, None, :] * (pitch / 2) ** 2
    exact = (amplitude**2 * area_weights).sum(axis=(-1, -2)) * np.pi * radius**2 / wavelength_um**2
    assert np.abs(actual - exact).sum() < 0.006
    assert abs(actual.sum() - exact.sum()) < 0.001
    np.testing.assert_allclose(_centroid(actual, pitch), _centroid(exact, pitch), atol=0.025)


def test_incident_flux_jacobian_and_no_filling_clipped_pupil_hole():
    count = 48
    step = 2.4 / count
    axis = (np.arange(count) - count // 2) * step
    x, y = np.meshgrid(axis, axis)
    admitted = (x * x + y * y < 1.0) & (x * x + y * y > 0.25**2)
    entrance = np.column_stack((x[admitted], y[admitted]))
    mapping = np.array([[0.1, 0.02], [0.0, 0.08]])
    traced = {"q": entrance @ mapping.T, "entrance_xy": entrance,
              "opd_nm": np.zeros(len(entrance)), "step": step}
    pupil = exit_pupil.regular_pupil(traced, 512)
    nonzero = pupil["power"] > 0
    expected_density = 1 / np.linalg.det(mapping)
    np.testing.assert_allclose(pupil["power"][nonzero], expected_density, rtol=1e-12)
    assert np.count_nonzero(pupil["power"][245:268, 245:268]) == 0
    flux = pupil["power"].sum() * pupil["spacing"]**2
    # Cell-topology clipping deliberately omits unresolved boundary slivers.
    assert 0.88 * math.pi * (1 - 0.25**2) < flux < math.pi * (1 - 0.25**2)
    reflected = {**traced, "q": traced["q"] * [-1, 1]}
    with pytest.raises(ValueError, match="folds"):
        exit_pupil.regular_pupil(reflected, 128)


def test_fixed_sensor_anchor_matches_actual_ray_centroid_without_recenter():
    request = prescription_psf.request_template()
    anchor = np.array([0.0, 0.06])
    traced = exit_pupil.trace_pupil(request, 0, 0.3, anchor, 64, 90)
    pupil = exit_pupil.regular_pupil(traced, 512)
    # Wide support prevents asymmetric Airy-tail truncation from biasing the
    # centroid of the deliberately displaced reference point.
    mass = exit_pupil.propagate(pupil, 500.0, 129, 3.0, 11)
    # Boundary samples characterize the aperture, not an extra incident flux.
    geometric = traced["landing_xy_mm"][:traced["interior_count"]].mean(axis=0) * 1000
    geometric[1] *= -1  # Physical +Y maps to rows decreasing.
    assert geometric[1] < -30  # An automatic chief recenter would erase this.
    np.testing.assert_allclose(_centroid(mass, 3.0), geometric, atol=0.4)
    np.testing.assert_array_equal(traced["anchor_xy_mm"], anchor)
