"""Exact temporal grid contracts for registered intermediate 3D renders."""

import numpy as np
import pytest

from camera_platform.scene_motion_sampling import (
    MAX_RENDER_SAMPLES,
    _window_integral,
    plan_times,
)


def test_plan_times_retains_exposure_boundaries_and_exact_ns_gaps():
    values = plan_times(0, 101, 20, required_times_ns=(31, 73))
    assert values == [0, 15, 31, 45, 59, 73, 87, 101]
    assert all(0 < right - left <= 20 for left, right in zip(values, values[1:], strict=False))
    assert plan_times(-5, 5, 4) == [-5, -2, 1, 5]


@pytest.mark.parametrize("args", [
    (0, 100, 0), (0, 100, 1.0), (0.0, 100, 10),
    (100, 0, 10), (0, 100, True),
])
def test_plan_times_rejects_invalid_clock_span_or_step(args):
    with pytest.raises(ValueError):
        plan_times(*args)


def test_plan_times_rejects_unbounded_work_and_bad_required_time():
    with pytest.raises(ValueError, match=str(MAX_RENDER_SAMPLES)):
        plan_times(0, 65, 1)
    for required in ((-1,), (11,), (True,), (1.0,)):
        with pytest.raises(ValueError, match="Required render times"):
            plan_times(0, 10, 5, required_times_ns=required)


def test_piecewise_linear_exposure_integral_matches_analytic_triangle():
    values = [np.array([[[value]]], dtype=np.float64) for value in (0, 1, 0)]
    assert _window_integral([0, 5, 10], values, 0, 10)[0, 0, 0] == pytest.approx(5e-9)
    assert _window_integral([0, 5, 10], values, 2, 8)[0, 0, 0] == pytest.approx(4.2e-9)
