"""Independent shutter quadrature, inversion, midpoint alignment and fixed WB."""
import numpy as np
import pytest
from camera_platform.motion_color_tradeoff import (
    box_shutter_transfer, exposure_averaged_grating, projected_transfer,
    fit_neutral_channel_gains, channel_ratio_error,
)


@pytest.mark.parametrize("velocity,exposure", [(0, .004), (750, .002), (1000, .004),
                                               (2000, .004), (3000, .004), (-1500, .002)])
def test_exact_time_average_against_independent_midpoint_quadrature(velocity, exposure):
    f, phase, modulation = .125, .23, .6
    actual = exposure_averaged_grating((8, 16), frequency_cpp=f, velocity_px_s=velocity,
                                      exposure_s=exposure, modulation=modulation,
                                      phase_at_midpoint_cycles=phase)
    # Direct finite-time quadrature, not the analytic transfer used by production.
    samples = 40000
    times = (np.arange(samples) + .5) / samples * exposure - exposure / 2
    x = np.arange(16) - 7.5
    expected = (1 + modulation*np.cos(2*np.pi*(f*(x[None, :] - velocity*times[:, None])
                                                       + phase))).mean(axis=0)
    np.testing.assert_allclose(actual, np.broadcast_to(expected, actual.shape), atol=3e-9, rtol=0)


def test_shutter_zero_and_signed_inversion_are_not_hidden():
    assert box_shutter_transfer(.125, 2000, .004) == pytest.approx(0, abs=1e-15)
    assert box_shutter_transfer(.125, 3000, .004) < 0
    assert box_shutter_transfer(.125, 1000, .002) == pytest.approx(.9003163161571061)
    assert box_shutter_transfer(.125, 1000, .004) == pytest.approx(2/np.pi)


def test_exposure_midpoint_pose_same_for_both_candidates():
    short = exposure_averaged_grating((8, 16), frequency_cpp=.125, velocity_px_s=1000,
                                      exposure_s=.002, phase_at_midpoint_cycles=.31)
    long = exposure_averaged_grating((8, 16), frequency_cpp=.125, velocity_px_s=1000,
                                     exposure_s=.004, phase_at_midpoint_cycles=.31)
    np.testing.assert_allclose((short-1)/box_shutter_transfer(.125,1000,.002),
                               (long-1)/box_shutter_transfer(.125,1000,.004), atol=1e-15)


def test_flat_envelope_removed_and_signed_projection_recovers_known_scale():
    flat = np.arange(32, dtype=float).reshape(4,8) + 200
    template = np.sin(np.arange(32)).reshape(4,8)*30
    mask = np.indices(flat.shape).sum(axis=0) % 2 == 1
    answer = projected_transfer(flat-.25*template, flat, flat+template, mask)
    assert answer["signed_transfer"] == pytest.approx(-.25)
    assert answer["template_relative_residual"] < 1e-14
    with pytest.raises(ValueError, match="template"):
        projected_transfer(flat, flat, flat, mask)


def test_common_brightness_scaling_cannot_change_channel_ratio_error():
    assert channel_ratio_error([40,50,60],[80,100,120])["maximum_absolute_relative_error"] == 0


def test_frozen_neutral_gains_do_not_refit_a_new_spectrum():
    gains = fit_neutral_channel_gains([60,100,90],[80,100,60])
    frozen = gains.copy()
    assert channel_ratio_error([60,100,90],[80,100,60],gains)["maximum_absolute_relative_error"] < 1e-15
    test = channel_ratio_error([30,100,120],[55,100,75],gains)
    assert test["maximum_absolute_relative_error"] > .2
    np.testing.assert_array_equal(gains, frozen)


@pytest.mark.parametrize("kwargs", [{"frequency_cpp": .6}, {"velocity_px_s": float("nan")},
                                    {"exposure_s": 0}, {"modulation": 1.5}])
def test_invalid_input_cannot_produce_a_plausible_result(kwargs):
    params = dict(frequency_cpp=.125, velocity_px_s=1000, exposure_s=.002)
    params.update(kwargs)
    with pytest.raises(ValueError):
        exposure_averaged_grating((8,16), **params)
