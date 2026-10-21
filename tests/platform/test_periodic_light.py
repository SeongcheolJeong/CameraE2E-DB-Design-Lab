"""Analytic edge cases and independent rational integration of authored PWM."""

import math
import random
import sys
from copy import deepcopy
from fractions import Fraction

import pytest

from camera_platform.periodic_light import (
    MAX_TIME_NS,
    exact_on_duration_ns,
    integrate_linear_basis,
    integrate_waveform_ns,
    validate_waveform,
)


def constant(level=1):
    return {"schema": "camera_light_waveform_v1", "kind": "constant",
            "clock_id": "authored-clock", "level": level}


def pwm(period=10, on=3, origin=0, low=0, high=1):
    return {"schema": "camera_light_waveform_v1", "kind": "pwm",
            "clock_id": "authored-clock", "period_ns": period, "on_time_ns": on,
            "phase_origin_ns": origin, "low": low, "high": high}


def reference(spec, start, end, left, right):
    """Independent exact unit-cell quadrature (linear functions integrate at midpoint)."""
    duration, left_weight, right_weight = Fraction(0), Fraction(0), Fraction(0)
    for t in range(max(start, left), min(end, right)):
        on = (t - spec["phase_origin_ns"]) % spec["period_ns"] < spec["on_time_ns"]
        level = Fraction(spec["high"] if on else spec["low"])
        right_basis = Fraction(2 * (t - left) + 1, 2 * (right - left))
        duration += level
        left_weight += level * (1 - right_basis)
        right_weight += level * right_basis
    return duration, left_weight / 10**9, right_weight / 10**9


def test_normalization_preserves_input_and_authored_clock_and_normalizes_levels():
    original = pwm(origin=-53, low=2, high=5)
    frozen = deepcopy(original)
    normalized = validate_waveform(original)
    assert normalized == original and normalized is not original
    assert original == frozen
    assert type(normalized["low"]) is float
    assert type(normalized["period_ns"]) is int
    assert validate_waveform(constant(3))["level"] == 3.0
    original["clock_id"] = " another clock "
    assert validate_waveform(original)["clock_id"] == " another clock "


@pytest.mark.parametrize("spec", [
    None, [], {}, {**constant(), "schema": "other"}, {**constant(), "kind": "sine"},
    {**constant(), "phase_origin_ns": 0}, {**pwm(), "level": 1},
    {k: v for k, v in pwm().items() if k != "clock_id"},
    {**constant(), "clock_id": ""}, {**constant(), "clock_id": "  "},
    {**constant(), "clock_id": 12},
])
def test_malformed_or_ambiguous_waveforms_are_refused(spec):
    with pytest.raises(ValueError):
        validate_waveform(spec)


@pytest.mark.parametrize("level", [True, False, -1, float("inf"), float("nan"),
                                  -float("inf"), "1", None, 10**1000])
def test_nonfinite_negative_and_non_numeric_levels_are_refused(level):
    for spec in (constant(level), pwm(low=level), pwm(high=level)):
        with pytest.raises(ValueError):
            validate_waveform(spec)


@pytest.mark.parametrize("field,value", [
    ("period_ns", 0), ("period_ns", -1), ("period_ns", True), ("period_ns", 1.0),
    ("period_ns", MAX_TIME_NS + 1), ("on_time_ns", -1), ("on_time_ns", 11),
    ("on_time_ns", False), ("on_time_ns", 3.0), ("phase_origin_ns", 0.5),
    ("phase_origin_ns", False), ("phase_origin_ns", MAX_TIME_NS + 1),
    ("phase_origin_ns", -MAX_TIME_NS - 1),
])
def test_invalid_pwm_times_are_refused(field, value):
    with pytest.raises(ValueError):
        validate_waveform({**pwm(), field: value})


def test_high_below_low_is_refused_and_equal_levels_keep_authored_duty_cycle():
    with pytest.raises(ValueError, match="high"):
        validate_waveform(pwm(low=2, high=1))
    spec = pwm(low=7, high=7)
    assert exact_on_duration_ns(spec, 0, 20) == 6
    assert integrate_waveform_ns(spec, 0, 20) == 140.0
    with pytest.raises(ValueError, match="requires a pwm"):
        exact_on_duration_ns(constant(), 0, 20)


@pytest.mark.parametrize("start,end,expected", [
    (0, 0, 0), (0, 3, 3), (3, 10, 0), (10, 13, 3), (2, 11, 2),
    (-10, -7, 3), (-7, 0, 0), (-1, 1, 1), (-21, 21, 13),
])
def test_half_open_edges_and_negative_phase(start, end, expected):
    actual = exact_on_duration_ns(pwm(), start, end)
    assert type(actual) is int
    assert actual == expected
    assert integrate_waveform_ns(pwm(low=2, high=5), start, end) == (
        2 * (end - start) + 3 * expected
    )


@pytest.mark.parametrize("on", [0, 10])
def test_zero_and_full_duty_degenerate_to_constant_over_arbitrary_windows(on):
    spec = pwm(on=on, origin=-7, low=2, high=5)
    for start, end in [(-13, 87), (-1, 0), (0, 0), (3, 9)]:
        assert exact_on_duration_ns(spec, start, end) == (end - start if on else 0)
        assert integrate_waveform_ns(spec, start, end) == (
            (5 if on else 2) * (end - start)
        )


@pytest.mark.parametrize("bad", [True, False, 1.0, 0.5, "0", None,
                                MAX_TIME_NS + 1, -MAX_TIME_NS - 1])
def test_all_timestamp_arguments_refuse_lossy_or_out_of_bounds_values(bad):
    for function in (exact_on_duration_ns, integrate_waveform_ns):
        for start, end in [(bad, 1), (0, bad)]:
            with pytest.raises(ValueError):
                function(pwm(), start, end)
    for index in range(4):
        args = [0, 1, 0, 1]
        args[index] = bad
        with pytest.raises(ValueError):
            integrate_linear_basis(pwm(), *args)


def test_invalid_interval_or_basis_is_refused_even_for_empty_or_disjoint_window():
    for function in (exact_on_duration_ns, integrate_waveform_ns):
        with pytest.raises(ValueError, match="precede"):
            function(pwm(), 1, 0)
    for args in [(1, 0, 0, 1), (0, 0, 1, 1), (10, 11, 1, 0)]:
        with pytest.raises(ValueError):
            integrate_linear_basis(pwm(), *args)
    with pytest.raises(ValueError, match="interpolation"):
        integrate_linear_basis(pwm(), 0, 0, 1, 2, "nearest")


def test_large_origin_and_one_ns_window_are_translation_invariant():
    # Floating absolute-time subtraction would erase these half-ns basis moments.
    for offset in (MAX_TIME_NS - 20, -MAX_TIME_NS):
        spec = pwm(period=7, on=3, origin=offset)
        assert exact_on_duration_ns(spec, offset + 2, offset + 3) == 1
        assert integrate_waveform_ns(spec, offset + 2, offset + 3) == 1.0
        assert integrate_linear_basis(spec, offset + 2, offset + 3,
                                      offset, offset + 7) == pytest.approx(
            (4.5 / 7e9, 2.5 / 7e9), rel=1e-15, abs=0
        )


def test_far_apart_origin_and_interval_do_not_lose_modulo_or_duration():
    spec = pwm(period=7, on=2, origin=-MAX_TIME_NS)
    start, end = MAX_TIME_NS - 5, MAX_TIME_NS
    expected = sum((t + MAX_TIME_NS) % 7 < 2 for t in range(start, end))
    assert exact_on_duration_ns(spec, start, end) == expected
    shifted = pwm(period=7, on=2, origin=0)
    phase = (start + MAX_TIME_NS) % 7
    assert integrate_linear_basis(spec, start, end, start, end) == (
        integrate_linear_basis(shifted, phase, phase + 5, phase, phase + 5)
    )


def test_trillions_of_cycles_and_full_supported_timestamp_span_are_analytic():
    cycles = 10**12
    spec = pwm(period=1000, on=37, origin=-123, low=2, high=10)
    start, end = -123, -123 + cycles * 1000
    assert exact_on_duration_ns(spec, start, end) == cycles * 37
    assert integrate_waveform_ns(spec, start, end) == float(cycles * (2 * 1000 + 8 * 37))
    span = 2 * MAX_TIME_NS
    assert exact_on_duration_ns(pwm(period=1, on=1), -MAX_TIME_NS, MAX_TIME_NS) == span
    assert integrate_waveform_ns(constant(), -MAX_TIME_NS, MAX_TIME_NS) == float(span)
    left, right = integrate_linear_basis(
        pwm(period=1, on=1), -MAX_TIME_NS, MAX_TIME_NS, -MAX_TIME_NS, MAX_TIME_NS
    )
    assert left == right == MAX_TIME_NS / 1e9


def test_constant_basis_closed_form_clipping_and_zero_order_hold():
    assert integrate_linear_basis(constant(3), -100, 100, 0, 10) == pytest.approx(
        (15e-9, 15e-9), rel=1e-15, abs=0
    )
    assert integrate_linear_basis(constant(2), 2, 4, 0, 10) == pytest.approx(
        (2.8e-9, 1.2e-9), rel=1e-15, abs=0
    )
    assert integrate_linear_basis(constant(2), 2, 4, 0, 10, "zero_order_hold") == (4e-9, 0)
    for start, end in [(-2, 0), (10, 12), (3, 3)]:
        assert integrate_linear_basis(constant(), start, end, 0, 10) == (0, 0)


def test_pwm_basis_uses_phase_distribution_not_just_mean_duty_cycle():
    # ON only [0,2): area2, first moment2; knot span10 -> weights1.8,0.2 ns.
    assert integrate_linear_basis(pwm(on=2), 0, 10, 0, 10) == (1.8e-9, 0.2e-9)
    assert integrate_linear_basis(pwm(on=2, origin=8), 0, 10, 0, 10) == (0.2e-9, 1.8e-9)
    assert integrate_linear_basis(pwm(on=2), 0, 10, 0, 10, "zero_order_hold") == (2e-9, 0)


def test_large_cycle_linear_basis_retains_first_moment_phase_bias():
    cycles = 10**12
    # Each pulse occupies the first quarter of a cycle. Its centroid precedes the
    # whole-window midpoint by 1.5ns, giving left/right duration shifts of 3/8ns.
    left, right = integrate_linear_basis(pwm(period=4, on=1), 0, 4 * cycles,
                                          0, 4 * cycles)
    assert (left, right) == pytest.approx(
        ((cycles / 2 + 0.375) / 1e9, (cycles / 2 - 0.375) / 1e9), rel=1e-15, abs=0
    )
    assert left > right


def test_fractional_levels_and_integer_cycle_phase_shifts_preserve_integrals():
    spec = pwm(period=11, on=7, origin=-17, low=0.125, high=1.75)
    duration, left, right = reference(spec, -31, 42, -12, 19)
    actual = integrate_linear_basis(spec, -31, 42, -12, 19)
    assert actual == pytest.approx((float(left), float(right)), rel=1e-15, abs=0)
    assert sum(actual) == pytest.approx(float(duration / 1e9), rel=1e-15, abs=0)
    shifted = {**spec, "phase_origin_ns": spec["phase_origin_ns"] + 11 * 10**12}
    assert integrate_linear_basis(shifted, -31, 42, -12, 19) == actual
    assert exact_on_duration_ns(shifted, -31, 42) == exact_on_duration_ns(spec, -31, 42)


def test_small_positive_basis_survives_near_far_knot_without_float_cancellation():
    left, right = integrate_linear_basis(constant(), MAX_TIME_NS - 1, MAX_TIME_NS,
                                          0, MAX_TIME_NS)
    assert left > 0
    assert left == float(Fraction(1, 2 * MAX_TIME_NS * 10**9))
    assert left + right == pytest.approx(1e-9, rel=1e-15, abs=0)
    left, right = integrate_linear_basis(pwm(period=1, on=1),
                                          MAX_TIME_NS - 1, MAX_TIME_NS, 0, MAX_TIME_NS)
    assert left > 0 and right > 0


def test_random_integer_pwm_matches_independent_rational_cell_integrals():
    rng = random.Random(8701)
    for _ in range(300):
        period = rng.randint(1, 17)
        spec = pwm(period, rng.randint(0, period), rng.randint(-40, 40),
                   rng.randint(0, 3), rng.randint(4, 9))
        start = rng.randint(-50, 30)
        end = start + rng.randint(0, 50)
        knot_left = rng.randint(-50, 30)
        knot_right = knot_left + rng.randint(1, 70)
        expected_duration, expected_left, expected_right = reference(
            spec, start, end, knot_left, knot_right
        )
        actual_left, actual_right = integrate_linear_basis(
            spec, start, end, knot_left, knot_right
        )
        assert actual_left >= 0 and actual_right >= 0
        assert (actual_left, actual_right) == pytest.approx(
            (float(expected_left), float(expected_right)), rel=2e-15, abs=0
        )
        assert actual_left + actual_right == pytest.approx(
            float(expected_duration / 10**9), rel=2e-15, abs=0
        )
        assert integrate_linear_basis(spec, start, end, knot_left, knot_right,
                                      "zero_order_hold") == pytest.approx(
            (float(expected_duration / 10**9), 0), rel=2e-15, abs=0
        )
        on = sum((t - spec["phase_origin_ns"]) % period < spec["on_time_ns"]
                 for t in range(start, end))
        assert exact_on_duration_ns(spec, start, end) == on
        assert integrate_waveform_ns(spec, start, end) == (
            spec["low"] * (end - start - on) + spec["high"] * on
        )
        middle = (start + end) // 2
        first = integrate_linear_basis(spec, start, middle, knot_left, knot_right)
        second = integrate_linear_basis(spec, middle, end, knot_left, knot_right)
        assert (first[0] + second[0], first[1] + second[1]) == pytest.approx(
            (actual_left, actual_right), rel=2e-15, abs=0
        )


def test_unrepresentable_weighted_duration_is_explicitly_refused_not_infinite():
    assert integrate_waveform_ns(constant(sys.float_info.max), 0, 0) == 0
    with pytest.raises(ValueError, match="finite float range"):
        integrate_waveform_ns(constant(sys.float_info.max), 0, 2)
    with pytest.raises(ValueError, match="finite float range"):
        integrate_linear_basis(constant(sys.float_info.max), 0, 10**10,
                               0, 10**10, "zero_order_hold")
    # Seconds-valued weights can fit even where a nanoseconds-valued result cannot.
    result = integrate_linear_basis(constant(sys.float_info.max), 0, 2, 0, 2)
    assert all(math.isfinite(weight) and weight > 0 for weight in result)
