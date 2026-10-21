"""Decision behavior: constraints, photon-aware ranking and held-out isolation."""

from copy import deepcopy
from pathlib import Path
import sys

import numpy as np
import pytest

from camera_platform.constrained_selection import (
    SelectionLimits, select_candidate, summarize_output, violations,
)


def candidate(identity="a", exposure=2.0, gain=1.0):
    return {"candidate_id": identity, "stop": "clear", "exposure_ms": exposure, "gain": gain}


def row(c, scenario="dim", **kwargs):
    return {**c, "scenario_id": scenario, "split": "train", "green_dn": 150.0,
            "green_snr_p10": 12.0, "well_fraction": 0.0, "white_fraction": 0.0, **kwargs}


def choose(candidates, rows):
    return select_candidate(candidates, rows, training_scenario_ids=["dim", "bright"],
                            limits=SelectionLimits())


def test_worst_snr_objective_respects_all_constraints_and_time_preexclusion():
    a, b, c, long = candidate("a", 1), candidate("b", 2), candidate("c", 4), candidate("long", 8)
    values = [row(a, "dim", green_snr_p10=11.0), row(a, "bright", green_snr_p10=40.0),
              row(b, "dim", green_snr_p10=12.0), row(b, "bright", green_snr_p10=25.0),
              row(c, "dim", green_snr_p10=15.0), row(c, "bright", white_fraction=0.02)]
    decision = choose([a, b, c, long], values)
    assert decision["selected_candidate_id"] == "b"
    assert decision["feasible_count"] == 2
    assert decision["failure_counts_nonexclusive"] == {"raw_clipping": 1, "exposure_time": 1}
    assert decision["candidates"][-1]["evaluated_scenarios"] == 0
    assert decision["automatic_approval"] is False


def test_no_feasible_candidate_is_a_decision_not_an_implicit_promotion():
    a = candidate()
    outcome = choose([a], [row(a, "dim", green_dn=119.0), row(a, "bright")])
    assert outcome["status"] == "no_feasible_candidate"
    assert outcome["selected_candidate_id"] is None
    assert outcome["candidates"][0]["failed_scenarios"] == {"dim": ["minimum_signal"]}


def test_limits_are_inclusive_and_missing_metrics_cannot_pass():
    metrics = {"green_dn": 120.0, "green_snr_p10": 10.0, "well_fraction": 0.01,
               "white_fraction": 0.01, "exposure_ms": 4.0}
    assert violations(metrics, SelectionLimits()) == []
    with pytest.raises(ValueError, match="metric"):
        violations({k: v for k, v in metrics.items() if k != "well_fraction"}, SelectionLimits())
    assert violations({**metrics, "well_fraction": 0.0101}, SelectionLimits()) == ["well_saturation"]
    assert violations({**metrics, "white_fraction": 0.0101}, SelectionLimits()) == ["raw_clipping"]


@pytest.mark.parametrize("change", ["holdout", "unknown", "duplicate", "missing", "settings"])
def test_selection_cannot_use_leaked_incomplete_or_mismatched_observations(change):
    a = candidate()
    values = [row(a), row(a, "bright")]
    if change == "holdout": values[1]["split"] = "holdout"
    if change == "unknown": values[1]["scenario_id"] = "unseen"
    if change == "duplicate": values.append(deepcopy(values[0]))
    if change == "missing": values.pop()
    if change == "settings": values[0]["gain"] = 2
    with pytest.raises(ValueError): choose([a], values)


def test_tie_break_is_deterministic_without_holdout_information():
    a, b = candidate("a", 1, 2), candidate("b", 1, 1)
    values = [row(c, s) for c in (a, b) for s in ("dim", "bright")]
    assert choose([a, b], values)["selected_candidate_id"] == "b"
    assert choose([b, a], values[::-1])["selected_candidate_id"] == "b"


def mock_output(gain=1.0):
    from camera_platform.electrical import default_electrical_model, capture_electrons

    model = default_electrical_model({"full_well_e": 10000, "read_noise_e": 2,
        "dark_current_e_per_s": 0, "gain": 1, "bit_depth": 12, "black_level_dn": 64})
    photo = np.full((4, 4), 400.0)
    capture = capture_electrons(photo, 0.002, model, analog_gain=gain, temperature_c=25,
        geometry={"full_sensor_shape": [4, 4], "crop_origin": [0, 0]}, noise_enabled=False, seed=42)
    return {"arrays": {**capture["arrays"], "mean_photocharge_e": photo,
                       "cfa_channel_index": np.tile([[0, 1], [1, 2]], (2, 2))},
            "summary": {"electrical": capture["summary"], "site_metrics": [{}, {}, {}],
                        "analog_gain": gain, "exposure_s": 0.002}}


def test_snr_is_independent_formula_and_gain_does_not_create_photons():
    one, two = summarize_output(mock_output(1)), summarize_output(mock_output(2))
    expected = 400 / np.sqrt(400 + 2**2 + 1/(12 * (4031 / 10000)**2))
    assert one["green_snr_p10"] == pytest.approx(expected, rel=1e-14)
    assert one["green_photocharge_mean_e"] == two["green_photocharge_mean_e"] == 400
    assert two["green_dn"] == 2 * one["green_dn"]
    assert 1 <= two["green_snr_p10"] / one["green_snr_p10"] < 1.001


def test_final_rounded_white_and_well_are_separate_constraints():
    output = mock_output()
    output["arrays"]["raw_dn"][0, 0] = 4095
    output["arrays"]["adc_high_mask"][0, 0] = False
    output["arrays"]["well_saturated_mask"][1, 1] = True
    metrics = summarize_output(output)
    assert metrics["white_fraction"] == metrics["well_fraction"] == 1/16
    assert metrics["adc_high_before_rounding_fraction"] == 0
    assert "raw_clipping" in violations({**metrics, "exposure_ms": 2}, SelectionLimits())
    assert "well_saturation" in violations({**metrics, "exposure_ms": 2}, SelectionLimits())


def test_invalid_limits_refused():
    with pytest.raises(ValueError): SelectionLimits(minimum_green_dn=float("nan"))
    with pytest.raises(ValueError): SelectionLimits(maximum_white_fraction=1.1)
    with pytest.raises(ValueError): SelectionLimits(maximum_exposure_ms=0)


def test_holdout_recipes_preserve_per_stop_shared_radiance_and_original_arrays():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    from run_camera_constrained_selection import scenarios, scenario_request

    class Source:
        def __init__(self, throughput):
            self.image = np.arange(1, 16*16*2 + 1).reshape(16, 16, 2).astype(float) * throughput
        def rate_request(self):
            return {"scene": {"spectral_irradiance": self.image, "source": {"kind": "estimated"}}}

    a, b = Source(np.array([1.0, 1.0])), Source(np.array([0.25, 0.5]))
    original = a.image.copy()
    _, held_out = scenarios()
    for spec in held_out:
        ra = scenario_request(a, spec)["scene"]["spectral_irradiance"]
        rb = scenario_request(b, spec)["scene"]["spectral_irradiance"]
        np.testing.assert_allclose(rb / np.array([0.25, 0.5]), ra, rtol=0, atol=0)
        assert ra.shape == original.shape
    np.testing.assert_array_equal(a.image, original)
