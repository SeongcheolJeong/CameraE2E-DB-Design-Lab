"""Independent synthetic observations and signed, unfitted holdout error metrics."""

import numpy as np
import pytest

from tools.run_calibration_db_holdout import authored_reference, errors, reduction, reference_raw


def readout(**changes):
    return {
        "dark_current_e_per_s": 0,
        "full_well_e": 10000,
        "read_noise_e": 10,
        "black_level_dn": 64,
        "dn_per_e": 0.4031,
        "bit_depth": 12,
        **changes,
    }


def test_reference_is_authored_and_does_not_retain_modified_candidate_values():
    original = authored_reference([450, 540, 615])
    assert original["qe_rgb"][0][2] == pytest.approx(0.58)
    assert original["qe_rgb"][1][1] == pytest.approx(0.7)
    assert original["qe_rgb"][2][0] == pytest.approx(0.65)
    original["matrix"][0][0] = 99
    assert authored_reference([450, 540, 615])["matrix"][0][0] == 1.3


def test_independent_acquisitions_repeat_only_for_the_same_seed():
    rate = np.full((64, 64), 40000.0)
    first = reference_raw(rate, readout(), 0.01, 100)
    np.testing.assert_array_equal(first, reference_raw(rate, readout(), 0.01, 100))
    assert not np.array_equal(first, reference_raw(rate, readout(), 0.01, 101))
    assert first.dtype == np.int32 and first.min() >= 0 and first.max() <= 4095


def test_reference_sampler_obeys_absolute_dn_gain_and_poisson_read_noise():
    raw = reference_raw(np.full((512, 512), 1000.0), readout(), 1.0, 301)
    assert raw.mean() == pytest.approx(64 + 0.4031 * 1000, abs=0.15)
    assert raw.var() == pytest.approx(0.4031**2 * (1000 + 10**2) + 1 / 12, rel=0.015)


def test_reference_sampler_has_db_black_and_adc_capacity():
    settings = readout(read_noise_e=0)
    np.testing.assert_array_equal(reference_raw(np.zeros((8, 8)), settings, 1.0, 4), 64)
    np.testing.assert_array_equal(reference_raw(np.full((8, 8), 1e8), settings, 1.0, 5), 4095)


def test_errors_preserve_negative_and_superwhite_linear_values():
    result = errors([[-0.5, 0, 2]], [[0, 0, 1]])
    assert result["rmse"] == pytest.approx(np.sqrt(1.25 / 3))
    assert result["residual_rgb"] == [[-0.5, 0.0, 1.0]]


def test_real_error_reduction_and_regression_are_both_reported():
    assert reduction(4, 1) == 75
    assert reduction(1, 2) == -100
    assert reduction(0, 0) is None
    assert errors(np.ones((24, 3)), np.zeros((24, 3)))["rmse"] == 1


@pytest.mark.parametrize(
    "prediction,observation", [([[1, 2]], [[1, 2]]), ([[1, 2, 3]], [[1, 2, float("nan")]])]
)
def test_invalid_metric_inputs_are_rejected(prediction, observation):
    with pytest.raises(ValueError):
        errors(prediction, observation)
