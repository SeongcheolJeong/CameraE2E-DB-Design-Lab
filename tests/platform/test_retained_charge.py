"""Conservation, state identity and authored retained-photodiode behavior."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import retained_charge as rc


def model(eta=0.5, capacity=1000):
    return {"schema": rc.SCHEMA, "capacity_e": capacity, "transfer_efficiency": [eta] * 4}


def transition(
    state=None,
    *,
    photo=0,
    eta=0.5,
    capacity=1000,
    exposure=1.0,
    gap=0.0,
    dark=0.0,
    clear=False,
    noise=False,
    seed=11,
    shape=(2, 2),
    data=None,
):
    m = model(eta, capacity) if data is None else data
    state = rc.initial_state(shape, m, noise_enabled=noise) if state is None else state
    return rc.step(
        state,
        np.full(shape, photo, dtype=float),
        exposure,
        gap,
        m,
        np.ones(shape),
        np.full(shape, dark, dtype=float),
        clear,
        noise,
        np.random.default_rng(seed) if noise else None,
    )


def test_bright_dark_transfer_history_reset_and_complete_transfer_limits():
    bright = transition(photo=800)
    dark = transition(bright["state"])
    darker = transition(dark["state"])
    for result, expected in zip((bright, dark, darker), (400, 200, 100), strict=True):
        np.testing.assert_array_equal(result["arrays"]["delivered_e"], expected)
        np.testing.assert_array_equal(result["arrays"]["residual_e"], expected)
    cleared = transition(bright["state"], clear=True)
    np.testing.assert_array_equal(cleared["arrays"]["clear_loss_e"], 400)
    np.testing.assert_array_equal(cleared["arrays"]["delivered_e"], 0)
    complete = transition(photo=800, eta=1)
    np.testing.assert_array_equal(complete["arrays"]["delivered_e"], 800)
    np.testing.assert_array_equal(transition(complete["state"], eta=1)["arrays"]["delivered_e"], 0)


def test_gap_dark_is_continuous_but_residual_has_no_spontaneous_release():
    prior = transition(photo=800)["state"]
    short = transition(prior, gap=1)
    long = transition(prior, gap=30)
    np.testing.assert_array_equal(short["arrays"]["delivered_e"], long["arrays"]["delivered_e"])
    short_dark = transition(prior, gap=1, dark=4)
    long_dark = transition(prior, gap=30, dark=4)
    np.testing.assert_array_equal(short_dark["arrays"]["delivered_e"], 204)
    np.testing.assert_array_equal(long_dark["arrays"]["delivered_e"], 262)
    cleared = transition(prior, gap=30, dark=4, clear=True)
    np.testing.assert_array_equal(cleared["arrays"]["clear_loss_e"], 520)
    np.testing.assert_array_equal(cleared["arrays"]["delivered_e"], 2)


def test_saturates_before_transfer_and_accounts_for_both_overflows_and_clear():
    m = model(0.2, 100)
    prior = transition(photo=100, data=m, capacity=100)["state"]
    result = transition(prior, photo=80, data=m, capacity=100)
    np.testing.assert_array_equal(result["arrays"]["initial_residual_e"], 80)
    np.testing.assert_array_equal(result["arrays"]["exposure_overflow_e"], 60)
    np.testing.assert_array_equal(result["arrays"]["delivered_e"], 20)
    both = transition(prior, photo=200, dark=50, gap=2, clear=True, data=m)
    a = both["arrays"]
    np.testing.assert_array_equal(a["gap_overflow_e"], 80)
    np.testing.assert_array_equal(a["clear_loss_e"], 100)
    np.testing.assert_array_equal(a["exposure_overflow_e"], 150)
    np.testing.assert_array_equal(a["delivered_e"], 20)
    assert both["summary"]["conservation_max_error_e"] == 0


def test_capacity100_prior80_new80_eta_half_counterexample():
    prior = transition(photo=100, capacity=100, eta=0.5)["state"]
    # Retained50 + gap dark30 gives80 before exposure; new photo50 + dark30
    # adds80. The total160 must saturate to100 before its half transfer.
    result = transition(prior, photo=50, dark=30, gap=1, capacity=100, eta=0.5)
    np.testing.assert_array_equal(result["arrays"]["exposure_overflow_e"], 60)
    np.testing.assert_array_equal(result["arrays"]["delivered_e"], 50)
    np.testing.assert_array_equal(result["arrays"]["residual_e"], 50)


def test_native_site_mapping_prnu_dark_and_row_gap_are_applied_once():
    shape = (4, 4)
    m = model()
    m["transfer_efficiency"] = [0, 0.25, 0.5, 1]
    state = rc.initial_state(shape, m, noise_enabled=False)
    first = rc.step(
        state,
        np.full(shape, 8.0),
        2,
        0,
        m,
        np.full(shape, 2.0),
        np.full(shape, 3.0),
        False,
        False,
        None,
    )
    eta = np.tile([[0, 0.25], [0.5, 1]], (2, 2))
    np.testing.assert_array_equal(first["arrays"]["delivered_e"], 22 * eta)
    next_step = rc.step(
        first["state"],
        np.zeros(shape),
        0,
        np.arange(4.0),
        m,
        np.ones(shape),
        np.full(shape, 3.0),
        False,
        False,
        None,
    )
    np.testing.assert_array_equal(
        next_step["arrays"]["expected_gap_e"], np.broadcast_to(np.arange(4.0)[:, None] * 3, shape)
    )


def test_stochastic_binomial_population_conservation_and_moments():
    result = transition(photo=10000, capacity=100, eta=0.3, noise=True, shape=(200, 200))
    a = result["arrays"]
    np.testing.assert_array_equal(a["pretransfer_e"], 100)
    np.testing.assert_array_equal(a["delivered_e"] + a["residual_e"], 100)
    np.testing.assert_array_equal(
        a["exposure_generated_e"], a["exposure_overflow_e"] + a["delivered_e"] + a["residual_e"]
    )
    assert abs(a["delivered_e"].mean() - 30) < 6 * np.sqrt(21 / 40000)
    assert abs(a["delivered_e"].var() - 21) < 0.8
    assert np.cov(a["delivered_e"].ravel(), a["residual_e"].ravel())[0, 1] < -20
    assert result["summary"]["conservation_max_error_e"] == 0
    assert result["state"]["charge_e"].dtype == np.int64


def test_state_and_inputs_are_untouched_and_output_arrays_do_not_alias_state():
    m = model()
    state = rc.initial_state((2, 2), m, noise_enabled=False)
    before = deepcopy(state)
    photo = np.full((2, 2), 100.0)
    result = rc.step(state, photo, 1, 0, m, np.ones((2, 2)), np.zeros((2, 2)), False, False, None)
    np.testing.assert_array_equal(photo, 100)
    np.testing.assert_array_equal(state["charge_e"], before["charge_e"])
    assert state["state_sha256"] == before["state_sha256"]
    assert result["state"]["predecessor_state_sha256"] == state["state_sha256"]
    result["arrays"]["residual_e"][:] = 0
    np.testing.assert_array_equal(result["state"]["charge_e"], 50)
    again = rc.step(state, photo, 1, 0, m, np.ones((2, 2)), np.zeros((2, 2)), False, False, None)
    assert result["state"]["state_sha256"] == again["state"]["state_sha256"]


@pytest.mark.parametrize("change", ["charge", "model", "mode", "step", "predecessor", "hash"])
def test_rejects_state_tampering_before_rng_advances(change):
    result = transition(photo=50, noise=True)
    state = result["state"]
    m = model()
    if change == "charge":
        state["charge_e"][0, 0] += 1
    elif change == "model":
        m["transfer_efficiency"][0] = 0.25
    elif change == "mode":
        state["noise_enabled"] = False
    elif change == "step":
        state["step_index"] += 1
    elif change == "predecessor":
        state["predecessor_state_sha256"] = "x" * 64
    else:
        state["state_sha256"] = "0" * 64
    rng = np.random.default_rng(41)
    before = deepcopy(rng.bit_generator.state)
    with pytest.raises(ValueError):
        rc.step(
            state, np.zeros((2, 2)), 1, 0, m, np.ones((2, 2)), np.zeros((2, 2)), False, True, rng
        )
    assert rng.bit_generator.state == before


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5, 1000.0, 10**12 + 1, 10**1000])
def test_rejects_noninteger_or_out_of_range_capacity_without_rounding(capacity):
    with pytest.raises(ValueError, match="integer"):
        rc.validate_model(model(capacity=capacity))


@pytest.mark.parametrize(
    "efficiency",
    [[0.5] * 3, [True] * 4, [float("nan")] * 4, [float("inf")] * 4, [-0.1] * 4, [1.1] * 4],
)
def test_rejects_invalid_native_efficiency(efficiency):
    m = model()
    m["transfer_efficiency"] = efficiency
    with pytest.raises(ValueError):
        rc.validate_model(m)


def test_initial_dark_start_is_explicit_and_large_allocations_are_refused(monkeypatch):
    with pytest.raises(ValueError, match="first gap"):
        transition(gap=1)

    def forbidden(*args, **kwargs):
        raise AssertionError("must reject before allocation")

    monkeypatch.setattr(np, "zeros", forbidden)
    with pytest.raises(ValueError, match="bounded"):
        rc.initial_state((10**100, 2), model(), noise_enabled=False)


@pytest.mark.parametrize(
    "argument,value",
    [
        ("photo", float("nan")),
        ("photo", -1),
        ("exposure", float("inf")),
        ("gap", -1),
        ("dark", 1e14),
        ("clear", 1),
    ],
)
def test_rejects_invalid_flow_arguments(argument, value):
    with pytest.raises(ValueError):
        transition(**{argument: value})


def test_expected_population_bound_is_checked_after_prnu_and_gap():
    m = model()
    state = rc.initial_state((2, 2), m, noise_enabled=False)
    with pytest.raises(ValueError, match="1e13"):
        rc.step(
            state,
            np.full((2, 2), 1e13),
            1,
            0,
            m,
            np.full((2, 2), 2.0),
            np.zeros((2, 2)),
            False,
            False,
            None,
        )
    prior = transition()["state"]
    with pytest.raises(ValueError, match="1e13"):
        transition(prior, dark=1e13, gap=2)


def test_zero_duration_has_no_photo_inflow_but_can_transfer_existing_residual():
    with pytest.raises(ValueError, match="zero exposure"):
        transition(photo=1, exposure=0)
    prior = transition(photo=100)["state"]
    np.testing.assert_array_equal(transition(prior, exposure=0)["arrays"]["delivered_e"], 25)


def test_integer_total_does_not_wrap_at_int64_and_step_bound_is_explicit():
    a = np.full((400, 400), 10**14, dtype=np.int64)
    assert rc._total(a) == 16_000_000_000_000_000_000
    state = None
    for _ in range(rc.MAX_STEPS):
        state = transition(state)["state"]
    with pytest.raises(ValueError, match="16-step"):
        transition(state)
