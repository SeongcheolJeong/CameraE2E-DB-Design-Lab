"""Two-reservoir numerical, conservation, clock and state-identity contracts."""

import math
from copy import deepcopy

import numpy as np
import pytest

from camera_platform import trapped_charge as tc


def model(*, eta=0.5, capacity=1000, traps=100, capture=2.0, release=0.5, step_ns=1_000_000):
    return {
        "schema": tc.SCHEMA,
        "capacity_e": capacity,
        "transfer_efficiency": [eta] * 4,
        "trap_capacity_e": [traps] * 4,
        "capture_rate_s": [capture] * 4,
        "release_time_s": [release] * 4,
        "integration": {
            "method": tc.METHOD,
            "max_step_ns": step_ns,
            "max_transition_probability": 0.02,
        },
    }


def segment(phase="exposure", *, duration=10_000_000, photo=0, shape=(2, 2)):
    times = (
        np.full(shape[0], duration, dtype=np.int64)
        if np.isscalar(duration)
        else np.asarray(duration)
    )
    photons = np.full(shape, photo, dtype=float) if np.isscalar(photo) else np.asarray(photo)
    return {"phase": phase, "duration_ns": times, "expected_photo_e": photons}


def run(
    state=None,
    *,
    m=None,
    photo=100,
    duration=10_000_000,
    gap=0,
    dark=0,
    prnu=1,
    clear=False,
    noise=False,
    shape=(2, 2),
    segments=None,
    rng=None,
):
    m = model() if m is None else m
    state = tc.initial_state(shape, m, noise_enabled=noise) if state is None else state
    if segments is None:
        segments = [
            segment("gap", duration=gap, shape=shape),
            segment(duration=duration, photo=photo, shape=shape),
        ]
    return tc.step(
        state,
        segments,
        m,
        np.full(shape, prnu, dtype=float),
        np.full(shape, dark, dtype=float),
        clear,
        noise,
        rng if rng is not None else (np.random.default_rng(11) if noise else None),
    )


def assert_conserved(result):
    a = result["arrays"]
    left = (
        a["initial_residual_e"]
        + a["initial_trapped_e"]
        + a["gap_generated_e"]
        + a["exposure_generated_e"]
    )
    right = (
        a["delivered_e"]
        + a["residual_e"]
        + a["trapped_e"]
        + a["clear_loss_e"]
        + a["gap_overflow_e"]
        + a["exposure_overflow_e"]
    )
    if result["summary"]["noise_enabled"]:
        np.testing.assert_array_equal(left, right)
        assert result["summary"]["conservation_max_error_e"] == 0
    else:
        np.testing.assert_allclose(left, right, rtol=1e-12, atol=1e-10)
    np.testing.assert_allclose(
        a["trapped_e"],
        a["initial_trapped_e"] + a["captured_e"] - a["released_e"],
        rtol=1e-12,
        atol=1e-10,
    )
    np.testing.assert_array_equal(a["pretransfer_e"], a["delivered_e"] + a["residual_e"])


def test_complete_transfer_still_leaves_traps_then_dark_release_reaches_readnode():
    m = model(eta=1)
    bright = run(m=m, photo=500, duration=20_000_000)
    np.testing.assert_array_equal(bright["arrays"]["residual_e"], 0)
    assert np.all(bright["arrays"]["trapped_e"] > 0)
    dark = run(bright["state"], m=m, photo=0, gap=20_000_000, duration=20_000_000)
    assert np.all(dark["arrays"]["delivered_e"] > 0)
    assert np.all(dark["arrays"]["trapped_e"] < bright["arrays"]["trapped_e"])
    for result in (bright, dark):
        assert_conserved(result)


def test_pd_clear_removes_only_free_not_trapped_and_zero_duration_can_transfer():
    m = model(eta=0.5)
    first = run(m=m, photo=300)
    cleared = run(first["state"], m=m, photo=0, duration=0, clear=True)
    np.testing.assert_array_equal(cleared["arrays"]["clear_loss_e"], first["arrays"]["residual_e"])
    np.testing.assert_array_equal(cleared["arrays"]["trapped_e"], first["arrays"]["trapped_e"])
    np.testing.assert_array_equal(cleared["arrays"]["delivered_e"], 0)
    later = run(cleared["state"], m=m, photo=0, duration=10_000_000)
    assert np.all(later["arrays"]["delivered_e"] > 0)
    for result in (first, cleared, later):
        assert_conserved(result)


def test_generation_overflows_before_capture_and_release_overflows_before_capture():
    m = model(capacity=10, traps=100, capture=1e6, release=1, eta=1, step_ns=10)
    result = run(m=m, photo=100, duration=10)
    np.testing.assert_allclose(result["arrays"]["exposure_overflow_e"], 90)
    expected_capture = 10 * -math.expm1(-0.01)
    np.testing.assert_allclose(result["arrays"]["captured_e"], expected_capture)
    np.testing.assert_allclose(result["arrays"]["delivered_e"], 10 - expected_capture)
    reaction = tc._react(
        np.full((2, 2), 10.0), np.full((2, 2), 100.0), 100, 0, 10, 10, 0.001, False, None
    )
    np.testing.assert_array_equal(reaction["free"], 10)
    np.testing.assert_allclose(reaction["overflow"], 100 * -math.expm1(-0.01))
    assert_conserved(result)


def test_pure_release_closed_form_compounds_exactly_and_stochastic_moments():
    initial = np.full((2, 2), 80.0)
    free = np.zeros((2, 2))
    trapped = initial.copy()
    for _ in range(37):
        reaction = tc._react(free, trapped, 100, 0, 4.0, 1000, 0.003, False, None)
        free, trapped = reaction["free"], reaction["trapped"]
    np.testing.assert_allclose(trapped, initial * math.exp(-4 * 37 * 0.003), rtol=2e-15)
    np.testing.assert_allclose(free + trapped, initial, rtol=2e-15)
    samples = tc._release_population(
        np.full((200, 200), 80, dtype=np.int64), 0.2, True, np.random.default_rng(67)
    )
    assert abs(float(samples.mean()) - 16) < 6 * math.sqrt(12.8 / samples.size)
    assert abs(float(samples.var()) - 12.8) < 0.6


def test_stochastic_entire_history_exact_conservation_shared_population_and_reproducibility():
    m = model(capacity=100, traps=30, eta=0.3, capture=20, release=0.05)
    rng = np.random.default_rng(23)
    again_rng = np.random.default_rng(23)
    state = again_state = None
    for index, photo in enumerate([300, 0, 50, 0]):
        kwargs = dict(
            m=m,
            photo=photo,
            duration=10_000_000,
            gap=0 if index == 0 else 5_000_000,
            dark=10,
            clear=index == 2,
            noise=True,
            shape=(20, 20),
        )
        result = run(state, rng=rng, **kwargs)
        again = run(again_state, rng=again_rng, **kwargs)
        assert result["state"]["state_sha256"] == again["state"]["state_sha256"]
        state, again_state = result["state"], again["state"]
        assert_conserved(result)
        assert all(
            a.dtype == np.int64
            for name, a in result["arrays"].items()
            if name not in {"native_site_index", "expected_gap_e", "expected_exposure_e"}
        )
        assert np.all(state["trapped_e"] <= 30)
        assert np.all(state["charge_e"] <= 100)
        assert result["summary"]["max_capture_probability"] <= 0.02
        assert result["summary"]["max_release_probability"] <= 0.02


def test_native_site_coefficients_row_durations_prnu_dark_and_inactive_rows():
    m = model(capture=0, eta=1)
    m["transfer_efficiency"] = [0, 0.25, 0.5, 1]
    m["capture_rate_s"] = [0, 1, 2, 3]
    m["trap_capacity_e"] = [10, 20, 30, 40]
    m["release_time_s"] = [0.1, 0.2, 0.3, 0.4]
    first = run(
        m=m,
        photo=np.array([[4, 4], [0, 0]], dtype=float),
        duration=np.array([1_000_000, 0]),
        prnu=2,
        dark=1000,
    )
    a = first["arrays"]
    np.testing.assert_array_equal(a["expected_exposure_e"], [[9, 9], [0, 0]])
    np.testing.assert_array_equal(a["native_site_index"], [[0, 1], [2, 3]])
    assert a["trapped_e"][0, 0] == 0 < a["trapped_e"][0, 1]
    np.testing.assert_array_equal(first["state"]["elapsed_ns"], [1_000_000, 0])
    np.testing.assert_array_equal(a["trapped_e"][1], 0)
    second = run(
        first["state"], m=m, photo=0, duration=0, gap=np.array([2_000_000, 3_000_000]), dark=1000
    )
    np.testing.assert_array_equal(second["arrays"]["expected_gap_e"], [[2, 2], [3, 3]])
    np.testing.assert_array_equal(second["state"]["elapsed_ns"], [3_000_000, 3_000_000])
    assert_conserved(second)


def test_zero_capture_recovers_retained_flow_when_unsaturated_and_deterministic():
    from camera_platform import retained_charge as rc

    m = model(capture=0)
    old_model = {k: m[k] for k in ("capacity_e", "transfer_efficiency")}
    old_model["schema"] = rc.SCHEMA
    old = rc.initial_state((2, 2), old_model, noise_enabled=False)
    new = None
    for i in range(3):
        photo, gap = (100 if i == 0 else 0), (0 if i == 0 else 0.01)
        original = rc.step(
            old,
            np.full((2, 2), photo, dtype=float),
            0.01,
            gap,
            old_model,
            np.ones((2, 2)),
            np.full((2, 2), 3.0),
            False,
            False,
            None,
        )
        result = run(new, m=m, photo=photo, duration=10_000_000, gap=int(gap * 1e9), dark=3)
        np.testing.assert_allclose(
            result["arrays"]["delivered_e"], original["arrays"]["delivered_e"], rtol=2e-15
        )
        np.testing.assert_array_equal(result["arrays"]["trapped_e"], 0)
        old, new = original["state"], result["state"]


def test_timestep_refinement_converges_to_independent_scalar_mass_action_rk4():
    # Independent high-resolution ODE solve; no product substep helper reused.
    k, tau, capacity, generation, end = 2.0, 0.5, 100.0, 250.0, 0.2

    def derivative(q, t):
        reaction = k * q * (1 - t / capacity) - t / tau
        return np.array([generation - reaction, reaction])

    y = np.zeros(2)
    h = end / 20_000
    for _ in range(20_000):
        a = derivative(*y)
        b = derivative(*(y + h * a / 2))
        c = derivative(*(y + h * b / 2))
        d = derivative(*(y + h * c))
        y += h * (a + 2 * b + 2 * c + d) / 6
    errors = []
    for step_ns in (2_000_000, 1_000_000, 500_000):
        m = model(eta=0, capacity=100, traps=100, capture=k, release=tau, step_ns=step_ns)
        result = run(m=m, photo=generation * end, duration=200_000_000)
        actual = [result["arrays"]["residual_e"][0, 0], result["arrays"]["trapped_e"][0, 0]]
        errors.append(float(np.max(np.abs(actual - y))))
        assert_conserved(result)
    assert errors[2] < errors[1] / 1.8 < errors[0] / (1.8**2)
    assert errors[-1] < 0.021  # Fixture-specific e bound, not a universal accuracy claim.


def test_segment_order_and_clear_boundary_cannot_be_reordered_silently():
    first = run(photo=200)
    parts = [
        segment("gap", duration=10_000_000),
        segment(duration=5_000_000, photo=0),
        segment(duration=5_000_000, photo=100),
    ]
    result = run(first["state"], segments=parts, clear=True)
    assert np.all(result["arrays"]["clear_loss_e"] > first["arrays"]["residual_e"] - 2)
    assert np.all(result["arrays"]["initial_trapped_e"] > 0)
    assert_conserved(result)
    with pytest.raises(ValueError, match="precede"):
        run(first["state"], segments=[parts[1], parts[0]])


def test_detached_state_ledgers_model_and_inputs_no_mutation_or_rng_advance_in_deterministic_mode():
    m = model()
    state = tc.initial_state((2, 2), m, noise_enabled=False)
    parts = [segment(photo=100)]
    before = deepcopy((state, parts, m))
    rng = np.random.default_rng(8)
    rng_before = deepcopy(rng.bit_generator.state)
    result = run(state, m=m, segments=parts, rng=rng)
    assert rng.bit_generator.state == rng_before
    np.testing.assert_array_equal(state["charge_e"], before[0]["charge_e"])
    np.testing.assert_array_equal(parts[0]["expected_photo_e"], before[1][0]["expected_photo_e"])
    assert m == before[2]
    saved = result["state"]["state_sha256"]
    result["arrays"]["trapped_e"][:] = 0
    result["arrays"]["residual_e"][:] = 0
    result["summary"]["model"]["capture_rate_s"][0] = 999
    assert np.all(result["state"]["trapped_e"] > 0)
    assert np.all(result["state"]["charge_e"] > 0)
    assert result["state"]["state_sha256"] == saved
    assert m == before[2]


@pytest.mark.parametrize(
    "field,value",
    [
        ("capacity_e", True),
        ("capacity_e", 1.5),
        ("capacity_e", 10**1000),
        ("trap_capacity_e", [0] * 4),
        ("trap_capacity_e", [1.0] * 4),
        ("capture_rate_s", [-1] * 4),
        ("capture_rate_s", [True] * 4),
        ("capture_rate_s", [float("nan")] * 4),
        ("capture_rate_s", [10**1000] * 4),
        ("release_time_s", [0] * 4),
        ("release_time_s", [float("inf")] * 4),
        ("transfer_efficiency", [1.01] * 4),
        ("trap_capacity_e", [3] * 3),
    ],
)
def test_strict_model_validation(field, value):
    m = model()
    m[field] = value
    with pytest.raises(ValueError):
        tc.validate_model(m)


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_step_ns", True),
        ("max_step_ns", 0),
        ("max_step_ns", 10**1000),
        ("max_transition_probability", 0.03),
        ("method", "exact_ctmc"),
    ],
)
def test_strict_integration_contract(key, value):
    m = model()
    m["integration"][key] = value
    with pytest.raises(ValueError):
        tc.validate_model(m)


def test_probability_bound_includes_vacancy_fill_and_refuses_sub_ns_kinetics():
    m = model(capacity=1000, traps=1, capture=10, release=1, step_ns=1_000_000)
    dt = tc.effective_max_step_ns(m)
    assert dt <= math.floor(-math.log1p(-0.02) / 10_000 * 1e9)
    assert -math.expm1(-10_000 * dt * 1e-9) <= 0.02
    m["capture_rate_s"] = [1e9] * 4
    with pytest.raises(ValueError, match="sub-nanosecond"):
        tc.validate_model(m)
    m = model(release=1e-300)
    with pytest.raises(ValueError, match="sub-nanosecond"):
        tc.validate_model(m)


@pytest.mark.parametrize(
    "field",
    [
        "charge_e",
        "trapped_e",
        "elapsed_ns",
        "step_index",
        "substep_count",
        "state_sha256",
        "predecessor_state_sha256",
    ],
)
def test_state_tampering_refused_before_random_draw(field):
    m = model()
    state = run(m=m, noise=True)["state"]
    if field in {"charge_e", "trapped_e", "elapsed_ns"}:
        state[field].flat[0] += 1
    elif field in {"step_index", "substep_count"}:
        state[field] += 1
    else:
        state[field] = "0" * 64
    rng = np.random.default_rng(23)
    before = deepcopy(rng.bit_generator.state)
    with pytest.raises(ValueError):
        run(state, m=m, noise=True, rng=rng)
    assert rng.bit_generator.state == before


@pytest.mark.parametrize(
    "bad",
    [
        "late_invalid",
        "zero_row_photo",
        "gap_photo",
        "negative_time",
        "fractional_ns",
        "wrong_shape",
        "overflow_mean",
        "budget",
        "initial_gap",
    ],
)
def test_all_known_segment_errors_fail_before_rng_advance(bad):
    m = model()
    parts = [segment(photo=100)]
    if bad == "late_invalid":
        parts.append(segment(photo=float("nan")))
    elif bad == "zero_row_photo":
        parts[0]["duration_ns"][0] = 0
    elif bad == "gap_photo":
        parts.insert(0, segment("gap", photo=1))
    elif bad == "negative_time":
        parts[0]["duration_ns"][0] = -1
    elif bad == "fractional_ns":
        parts[0].pop("duration_ns")
        parts[0]["duration_s"] = np.array([1.1e-9, 1e-9])
    elif bad == "wrong_shape":
        parts[0]["expected_photo_e"] = np.zeros((1, 2))
    elif bad == "overflow_mean":
        parts[0]["expected_photo_e"][:] = 1e13
    elif bad == "budget":
        parts[0]["duration_ns"][:] = tc.MAX_DURATION_NS
    else:
        parts.insert(0, segment("gap", duration=1, photo=0))
    rng = np.random.default_rng(23)
    before = deepcopy(rng.bit_generator.state)
    with pytest.raises(ValueError):
        run(m=m, noise=True, segments=parts, prnu=2, rng=rng)
    assert rng.bit_generator.state == before


def test_exact_ns_duration_s_equivalence_and_work_budget_before_sampling():
    parts = [segment(photo=100, duration=1_234_567)]
    seconds = deepcopy(parts)
    seconds[0]["duration_s"] = seconds[0].pop("duration_ns") * 1e-9
    a, b = run(segments=parts), run(segments=seconds)
    assert a["state"]["state_sha256"] == b["state"]["state_sha256"]
    assert a["summary"]["output_array_bytes_bound"] >= sum(v.nbytes for v in a["arrays"].values())
    with pytest.raises(ValueError, match="512"):
        run(segments=[segment(duration=0)] * 513)
    with pytest.raises(ValueError, match="bounded"):
        tc.initial_state((1000, 1000), model(), noise_enabled=False)


def test_only_empty_initial_state_and_sixteen_frames_are_accepted():
    m = model()
    state = tc.initial_state((2, 2), m, noise_enabled=False)
    state["trapped_e"][:] = 1
    state["state_sha256"] = tc._state_digest(state)
    with pytest.raises(ValueError, match="initial"):
        run(state, m=m)
    state = None
    for _ in range(16):
        state = run(state, m=m, photo=0, duration=0)["state"]
    with pytest.raises(ValueError, match="16-frame"):
        run(state, m=m)


def test_small_trap_stochastic_attempts_are_capped_without_creating_or_losing_free_charge():
    # A permitted p<.02 can still draw more attempts than a single vacant trap.
    # The authored tau-leap approximation explicitly clips that one population.
    shape = (200, 200)
    free = np.full(shape, 100, dtype=np.int64)
    trapped = np.zeros(shape, dtype=np.int64)
    seed = 481
    probability = -math.expm1(-0.0002)
    expected_rng = np.random.default_rng(seed)
    expected_rng.binomial(trapped, 0.0)  # Same explicit zero release call.
    attempts = expected_rng.binomial(free, probability)
    assert np.any(attempts > 1)
    actual = tc._react(free, trapped, 1, 1, 0, 100, 0.0002, True, np.random.default_rng(seed))
    np.testing.assert_array_equal(actual["captured"], np.minimum(attempts, 1))
    np.testing.assert_array_equal(actual["free"] + actual["trapped"], 100)
    np.testing.assert_array_equal(free, 100)
    np.testing.assert_array_equal(trapped, 0)


@pytest.mark.parametrize("change", ["model", "mode", "boolean_clear", "boolean_mode", "rng"])
def test_state_identity_modes_and_control_types_are_strict(change):
    m = model()
    first = run(m=m, noise=True)
    state = first["state"]
    rng = np.random.default_rng(39)
    before = deepcopy(rng.bit_generator.state)
    clear, noise, passed_rng = False, True, rng
    if change == "model":
        m["capture_rate_s"][0] = 3
    elif change == "mode":
        noise = False
    elif change == "boolean_clear":
        clear = 1
    elif change == "boolean_mode":
        noise = 1
    else:
        passed_rng = 3
    with pytest.raises(ValueError):
        tc.step(
            state,
            [segment(photo=0)],
            m,
            np.ones((2, 2)),
            np.zeros((2, 2)),
            clear,
            noise,
            passed_rng,
        )
    assert rng.bit_generator.state == before


@pytest.mark.parametrize("phase", [[], {}, True, 3, None, "unknown"])
def test_malformed_phase_has_contract_error_before_random_sampling(phase):
    parts = [segment(photo=0)]
    parts[0]["phase"] = phase
    rng = np.random.default_rng(31)
    before = deepcopy(rng.bit_generator.state)
    with pytest.raises(ValueError, match="segment phase"):
        run(segments=parts, noise=True, rng=rng)
    assert rng.bit_generator.state == before
