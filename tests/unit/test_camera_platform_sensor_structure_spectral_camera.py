import numpy as np
import pytest

from camera_platform.sensor_structure_spectral_camera import spectral_gains, integrate


def evidence():
    return [dict(field_id='center', cfa_channel=c, wavelength_nm=w,
                 baseline_absorption=.2, response_gain=g, numerical_validity=True)
            for c, gains in [('red', [1., 1.2, 1.4]), ('green', [.8, 1., 1.2]), ('blue', [.6, .8, 1.])]
            for w, g in zip([450., 550., 650.], gains)]


def test_interpolation_keeps_cfa_and_cross_wavelength_response():
    g = spectral_gains(evidence(), 'center', [450., 500., 550., 650.])
    np.testing.assert_allclose(g[1], [1.1, .9, .9, .7])
    np.testing.assert_allclose(g[:, 1], g[:, 2])


@pytest.mark.parametrize('waves', [[440., 550.], [550., 660.], [550., 450.], [450., 450.], [np.nan]])
def test_no_spectral_extrapolation_or_invalid_axis(waves):
    with pytest.raises(ValueError): spectral_gains(evidence(), 'center', waves)


def test_invalid_or_missing_color_node_is_not_silently_replaced():
    e = evidence(); e[0]['numerical_validity'] = False
    with pytest.raises(ValueError): spectral_gains(e, 'center', [450., 550., 650.])
    with pytest.raises(ValueError): spectral_gains(e[1:], 'center', [450., 550., 650.])


def test_weak_response_withheld_ratio_is_explicitly_rejected():
    e = evidence(); e[0]['response_gain'] = None
    with pytest.raises(ValueError, match='Weak-response'):
        spectral_gains(e, 'center', [450., 550., 650.])


def test_weights_apply_before_spectral_sum_not_as_broadband_gain():
    a = np.zeros((3, 4, 4, 4)); a[0, :, :2] = 3; a[2, :, 2:] = 3
    weights = np.array([[2]*4, [1]*4, [.5]*4])
    m, p = integrate(a, weights)
    np.testing.assert_allclose(m[:, :2], 6)
    np.testing.assert_allclose(m[:, 2:], 1.5)
    _, p0 = integrate(a, np.ones((3, 4)))
    np.testing.assert_allclose(p0, a.sum(axis=0))


def test_mosaic_cfa_origin_does_not_mix_sites():
    a = np.broadcast_to(np.arange(1., 5.), (3, 4, 4, 4))
    m, _ = integrate(a, np.ones((3, 4)), cfa_origin=(1, 0))
    np.testing.assert_array_equal(m[:2, :2], [[9, 12], [3, 6]])
