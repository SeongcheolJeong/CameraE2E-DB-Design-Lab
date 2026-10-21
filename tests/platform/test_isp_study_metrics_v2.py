"""Scalar and analytic oracles for fixed-support linear ISP study evidence."""

from copy import deepcopy

import numpy as np
import pytest

from camera_platform import isp_study_metrics_v2 as metrics

SIDE, FRAMES, SCALE = 16, 4, 100.0


def context(kind="flat", direction="column"):
    return {
        "schema": "camera_isp_metric_spec_v2",
        "kind": kind,
        "stationary": True,
        "roi": {"row_start": 0, "row_end": SIDE, "column_start": 0, "column_end": SIDE},
        "texture": {"period_px": 8, "direction": direction} if kind == "texture" else None,
        "edge": {"direction": direction, "position_px": 8, "half_width_px": 2}
        if kind == "edge"
        else None,
    }


def rgb(signal):
    return np.repeat(np.asarray(signal, dtype=float)[..., None], 3, axis=-1) / SCALE


def repeated(signal):
    return np.repeat(rgb(signal)[None], FRAMES, axis=0)


def measure(frames, **kwargs):
    if kwargs.get("baseline_rgb_frames") is not None:
        kwargs.setdefault("baseline_valid_mask_frames", np.ones((FRAMES, SIDE, SIDE), dtype=bool))
    return metrics.measure(
        frames,
        normalization_e=kwargs.pop("normalization_e", SCALE),
        dataset=kwargs.pop("dataset", context()),
        evaluation_mask=kwargs.pop("evaluation_mask", np.ones((SIDE, SIDE), dtype=bool)),
        valid_mask_frames=kwargs.pop(
            "valid_mask_frames", np.ones((FRAMES, SIDE, SIDE), dtype=bool)
        ),
        **kwargs,
    )


def value(result, name):
    record = result["metrics"][name]
    assert record["status"] == "ok" and record["reason"] is None
    return record["value"]


def test_temporal_scalar_oracle_signed_ramp_and_no_spatial_noise_confusion():
    y, x = np.indices((SIDE, SIDE))
    truth = x + 2 * y - 40.0
    static = measure(repeated(truth), reference_rgb=rgb(truth))
    assert value(static, "temporal_residual_std_e") == pytest.approx(0, abs=1e-12)
    offsets = np.array([-3, -1, 1, 3])
    output = repeated(truth + 2) + offsets[:, None, None, None] / SCALE
    result = measure(output, reference_rgb=rgb(truth))
    expected_variance = sum(float(x) ** 2 for x in offsets) / (FRAMES - 1)
    assert value(result, "temporal_residual_std_e") == pytest.approx(expected_variance**0.5)
    assert value(result, "mean_bias_e") == pytest.approx(2)
    assert value(result, "mean_abs_bias_e") == pytest.approx(2)
    assert result["input_stage"] == "signed_normalized_charge_linear_rgb_before_tone"
    assert result["physical_validation"] == "not_established"
    assert result["total_uncertainty"] == {"kind": "unknown"}


def test_absolute_bias_does_not_cancel_opposite_spatial_errors():
    truth = np.full((SIDE, SIDE), 10.0)
    observed = truth.copy()
    observed[:, :8] -= 4
    observed[:, 8:] += 4
    result = measure(repeated(observed), reference_rgb=rgb(truth))
    assert value(result, "mean_bias_e") == pytest.approx(0, abs=1e-12)
    assert value(result, "mean_abs_bias_e") == pytest.approx(4)


def test_arithmetic_rgb_guide_retains_signed_values_and_does_not_apply_tone_clip():
    frames = np.broadcast_to([-2.0, 1.0, 7.0], (FRAMES, SIDE, SIDE, 3))
    result = measure(frames, reference_rgb=np.zeros((SIDE, SIDE, 3)))
    assert value(result, "mean_bias_e") == pytest.approx(200)
    assert value(result, "mean_abs_bias_e") == pytest.approx(200)
    assert value(result, "temporal_residual_std_e") == pytest.approx(0)


def test_reference_absent_baseline_difference_is_not_named_bias():
    source = np.full((SIDE, SIDE), -8.0)
    result = measure(repeated(source + 3), baseline_rgb_frames=repeated(source))
    assert value(result, "mean_baseline_delta_e") == pytest.approx(3)
    assert value(result, "mean_abs_baseline_delta_e") == pytest.approx(3)
    for name in ("mean_bias_e", "mean_abs_bias_e"):
        assert result["metrics"][name] == {
            "value": None,
            "unit": metrics.ELECTRON,
            "status": "unknown",
            "reason": "Independent reference unavailable",
        }


def test_nonstationary_sequence_does_not_become_a_noise_measurement():
    dataset = context()
    dataset["stationary"] = False
    result = measure(repeated(np.ones((SIDE, SIDE))), dataset=dataset)
    assert result["metrics"]["temporal_residual_std_e"]["value"] is None
    assert "Stationary" in result["metrics"]["temporal_residual_std_e"]["reason"]


@pytest.mark.parametrize("direction", ["row", "column"])
def test_texture_analytic_amplitude_oracle_independent_of_phase_and_dc(direction):
    y, x = np.indices((SIDE, SIDE))
    axis, other = (y, x) if direction == "row" else (x, y)
    truth = 700 + 20 * np.sin(2 * np.pi * axis / 8) + 4 * np.cos(2 * np.pi * other / 8)
    observed = 707 + 12 * np.cos(2 * np.pi * axis / 8) + 4 * np.cos(2 * np.pi * other / 8)
    result = measure(
        repeated(observed), reference_rgb=rgb(truth), dataset=context("texture", direction)
    )
    assert value(result, "mean_bias_e") == pytest.approx(7)
    assert value(result, "texture_amplitude_e") == pytest.approx(12)
    assert value(result, "texture_reference_amplitude_e") == pytest.approx(20)
    assert value(result, "texture_amplitude_ratio") == pytest.approx(0.6)
    assert value(result, "texture_attenuation_fraction") == pytest.approx(0.4)


def test_texture_boost_remains_negative_attenuation_not_clipped_improvement():
    _, x = np.indices((SIDE, SIDE))
    truth = 700 + 20 * np.sin(2 * np.pi * x / 8)
    observed = 700 + 30 * np.sin(2 * np.pi * x / 8)
    result = measure(repeated(observed), reference_rgb=rgb(truth), dataset=context("texture"))
    assert value(result, "texture_amplitude_ratio") == pytest.approx(1.5)
    assert value(result, "texture_attenuation_fraction") == pytest.approx(-0.5)


@pytest.mark.parametrize("reference", [None, np.full((SIDE, SIDE, 3), 7.0)])
def test_texture_missing_or_zero_reference_never_falls_back_to_baseline(reference):
    _, x = np.indices((SIDE, SIDE))
    output = repeated(700 + 20 * np.sin(2 * np.pi * x / 8))
    result = measure(
        output, reference_rgb=reference, baseline_rgb_frames=output, dataset=context("texture")
    )
    assert value(result, "texture_amplitude_e") == pytest.approx(20)
    assert result["metrics"]["texture_amplitude_ratio"]["value"] is None


def test_texture_rank_deficient_fixed_support_is_unknown():
    mask = np.zeros((SIDE, SIDE), dtype=bool)
    mask[:, [0, 8]] = True
    result = measure(
        repeated(np.ones((SIDE, SIDE))), dataset=context("texture"), evaluation_mask=mask
    )
    assert "rank" in result["metrics"]["texture_amplitude_e"]["reason"]
    assert result["coverage"]["selected_pixels"] == 32


@pytest.mark.parametrize("direction", ["row", "column"])
@pytest.mark.parametrize("decreasing", [False, True])
def test_edge_scalar_plateau_oracle_keeps_overshoot_and_undershoot(direction, decreasing):
    y, x = np.indices((SIDE, SIDE))
    axis = y if direction == "row" else x
    truth = np.where(axis < 8, 1000.0 if decreasing else 400.0, 400.0 if decreasing else 1000.0)
    observed = truth.copy()
    observed[axis == 7] = 1011.0 if decreasing else 393.0
    observed[axis == 8] = 393.0 if decreasing else 1011.0
    frames = repeated(observed) + np.array([-3, -1, 1, 3])[:, None, None, None] / SCALE
    result = measure(frames, reference_rgb=rgb(truth), dataset=context("edge", direction))
    assert value(result, "edge_overshoot_e") == pytest.approx(11)
    assert value(result, "edge_undershoot_e") == pytest.approx(7)
    assert result["diagnostics"]["edge"]["reference_low_e"] == pytest.approx(400)
    assert result["diagnostics"]["edge"]["reference_high_e"] == pytest.approx(1000)


def test_edge_missing_profile_coordinate_is_unknown_without_roi_reduction():
    _, x = np.indices((SIDE, SIDE))
    truth = np.where(x < 8, 400.0, 1000.0)
    mask = np.ones((SIDE, SIDE), dtype=bool)
    mask[:, 7] = False
    result = measure(
        repeated(truth), reference_rgb=rgb(truth), dataset=context("edge"), evaluation_mask=mask
    )
    assert result["metrics"]["edge_overshoot_e"]["value"] is None
    assert "missing profile coordinate" in result["metrics"]["edge_overshoot_e"]["reason"]


def test_single_invalid_selected_sample_invalidates_every_metric():
    frames = repeated(np.ones((SIDE, SIDE)))
    valid = np.ones((FRAMES, SIDE, SIDE), dtype=bool)
    valid[2, 4, 5] = False
    result = measure(
        frames, reference_rgb=frames[0], baseline_rgb_frames=frames, valid_mask_frames=valid
    )
    assert all(
        v["value"] is None and "support was not reduced" in v["reason"]
        for v in result["metrics"].values()
    )
    assert result["coverage"]["invalid_samples"] == 1
    assert result["coverage"]["selected_pixels"] == SIDE * SIDE
    assert result["coverage"]["invalid_per_frame"] == [0, 0, 1, 0]


def test_predeclared_fixed_mask_can_exclude_sample_but_is_not_mutated():
    frames = repeated(np.ones((SIDE, SIDE)))
    valid = np.ones((FRAMES, SIDE, SIDE), dtype=bool)
    valid[2, 4, 5] = False
    mask = np.ones((SIDE, SIDE), dtype=bool)
    mask[4, 5] = False
    before = mask.copy()
    result = measure(frames, valid_mask_frames=valid, evaluation_mask=mask)
    assert result["coverage"]["invalid_samples"] == 0
    assert value(result, "temporal_residual_std_e") == pytest.approx(0)
    np.testing.assert_array_equal(mask, before)


@pytest.mark.parametrize("dtype", [bool, int, complex, object])
def test_nonfloating_rgb_types_are_refused(dtype):
    with pytest.raises(ValueError, match="floating"):
        measure(np.ones((FRAMES, SIDE, SIDE, 3), dtype=dtype))


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
@pytest.mark.parametrize("field", ["rgb_frames", "reference_rgb", "baseline_rgb_frames"])
def test_nonfinite_inputs_are_refused_even_outside_roi(bad, field):
    frames = repeated(np.ones((SIDE, SIDE)))
    array = frames[0].copy() if field == "reference_rgb" else frames.copy()
    array.flat[0] = bad
    kwargs = {field: array} if field != "rgb_frames" else {}
    with pytest.raises(ValueError, match="finite"):
        measure(array if field == "rgb_frames" else frames, **kwargs)


@pytest.mark.parametrize("count", [0, 1, 3, 17])
def test_repeat_count_is_not_reinterpreted(count):
    with pytest.raises(ValueError, match="4..16"):
        measure(np.ones((count, SIDE, SIDE, 3), dtype=float))


@pytest.mark.parametrize("scale", [False, 0, -1, np.nan, np.inf, 10**1000])
def test_bad_normalization_is_refused(scale):
    with pytest.raises(ValueError):
        measure(repeated(np.ones((SIDE, SIDE))), normalization_e=scale)


@pytest.mark.parametrize(
    "mask_name,shape",
    [("evaluation_mask", (SIDE, SIDE)), ("valid_mask_frames", (FRAMES, SIDE, SIDE))],
)
def test_integer_masks_are_refused(mask_name, shape):
    with pytest.raises(ValueError, match="boolean"):
        measure(repeated(np.ones((SIDE, SIDE))), **{mask_name: np.ones(shape, dtype=np.uint8)})


def test_overflow_is_not_reported_as_a_finite_metric_or_unknown():
    frames = np.full((FRAMES, SIDE, SIDE, 3), 1e308)
    with pytest.raises(ValueError, match="overflow"):
        measure(frames)
    frames = np.ones((FRAMES, SIDE, SIDE, 3))
    frames[::2] = -1
    with pytest.raises(ValueError, match="overflow"):
        measure(frames * 1e155, normalization_e=1.0)


def test_mask_roi_and_stage_settings_rejected_without_automatic_repair():
    frames = repeated(np.ones((SIDE, SIDE)))
    with pytest.raises(ValueError, match="empty"):
        measure(frames, evaluation_mask=np.zeros((SIDE, SIDE), dtype=bool))
    bad = context()
    bad["roi"]["row_end"] = SIDE + 1
    with pytest.raises(ValueError, match="ROI"):
        measure(frames, dataset=bad)
    bad = context("texture")
    bad["texture"]["period_px"] = 2
    with pytest.raises(ValueError, match="period"):
        measure(frames, dataset=bad)
    bad = context("edge")
    bad["edge"]["half_width_px"] = 9
    with pytest.raises(ValueError, match="plateaus"):
        measure(frames, dataset=bad)
    for position, half_width in ((8.5, 0.1), (14.8, 0.1)):
        bad = context("edge")
        bad["edge"].update(position_px=position, half_width_px=half_width)
        with pytest.raises(ValueError, match="sampled pixel centers"):
            metrics.validate_dataset(bad, (SIDE, SIDE))


def record(value, unit="1"):
    return {
        "value": value,
        "unit": unit,
        "status": "unknown" if value is None else "ok",
        "reason": "not available" if value is None else None,
    }


def row(identity, a, b, *, status="succeeded"):
    return {
        "candidate_id": identity,
        "status": status,
        "metrics": {"noise": record(a), "texture": record(b)},
    }


OBJECTIVES = [
    {"metric": "noise", "direction": "minimize"},
    {"metric": "texture", "direction": "maximize"},
]


def test_pareto_scalar_pairs_tradeoffs_ties_and_constraints():
    rows = [
        row("A", 1, 1),
        row("A-tie", 1, 1),
        row("B", 2, 1),
        row("C", 2, 2),
        row("bad", 0.5, 0.1),
    ]
    result = metrics.aggregate(
        rows, OBJECTIVES, [{"metric": "texture", "operator": ">=", "value": 0.5, "unit": "1"}]
    )
    statuses = {r["candidate_id"]: r["pareto_status"] for r in result["rows"]}
    assert statuses == {
        "A": "non_dominated",
        "A-tie": "non_dominated",
        "B": "dominated",
        "C": "non_dominated",
        "bad": "excluded",
    }
    assert not result["pareto_provisional"]
    assert result["rows"][-1]["feasibility"] == "infeasible"
    assert result["rows"][-1]["constraints"][0]["status"] == "violated"
    assert "winner" not in result


def test_missing_failed_cancelled_unknown_are_provisional_and_never_dominate():
    rows = [
        row("ok", 2, 1),
        row("failed", 0, 99, status="failed"),
        row("cancel", 0, 99, status="cancelled"),
        row("unknown", None, 99),
    ]
    result = metrics.aggregate(
        rows,
        OBJECTIVES,
        [],
        expected_candidate_ids=["ok", "failed", "cancel", "unknown", "missing"],
    )
    assert result["pareto_provisional"]
    assert result["rows"][0]["pareto_status"] == "non_dominated"
    assert all(r["pareto_status"] == "excluded" for r in result["rows"][1:])
    assert result["coverage"]["missing"] == 1
    assert result["rows"][1]["feasibility"] == "unknown"


def test_unknown_constraint_never_passes_and_bad_units_are_rejected():
    result = metrics.aggregate(
        [row("A", 1, 1)],
        OBJECTIVES,
        [{"metric": "halo", "operator": "<=", "value": 2, "unit": metrics.ELECTRON}],
    )
    assert result["rows"][0]["constraints"][0]["status"] == "unknown"
    assert result["rows"][0]["feasibility"] == "unknown"
    assert result["pareto_provisional"]
    with pytest.raises(ValueError, match="units"):
        metrics.aggregate(
            [row("A", 1, 1)],
            OBJECTIVES,
            [{"metric": "noise", "operator": "<=", "value": 2, "unit": metrics.ELECTRON}],
        )


@pytest.mark.parametrize("bad", [False, np.nan, np.inf, 1 + 2j])
def test_invalid_scalar_comparison_values_are_refused(bad):
    with pytest.raises(ValueError):
        metrics.aggregate([row("A", bad, 1)], OBJECTIVES, [])


def test_comparison_is_detached_and_unknown_record_cannot_carry_value():
    source = [row("A", 1, 1)]
    result = metrics.aggregate(source, OBJECTIVES, [])
    source[0]["metrics"]["noise"]["value"] = 0
    assert result["rows"][0]["metrics"]["noise"]["value"] == 1
    source[0]["metrics"]["noise"].update(status="unknown", reason="unknown")
    with pytest.raises(ValueError, match="null"):
        metrics.aggregate(source, OBJECTIVES, [])


def test_explicit_reducers_keep_scene_identity_and_propagate_missing():
    results = {
        "a": {"metrics": {"mean_bias_e": record(-4, metrics.ELECTRON)}},
        "b": {"metrics": {"mean_bias_e": record(2, metrics.ELECTRON)}},
    }
    specs = [
        {
            "id": "worst_bias",
            "metric": "mean_bias_e",
            "dataset_ids": ["a", "b"],
            "reducer": "max_abs",
        },
        {
            "id": "lowest_bias",
            "metric": "mean_bias_e",
            "dataset_ids": ["a", "b"],
            "reducer": "min",
        },
        {
            "id": "unknown_bias",
            "metric": "mean_bias_e",
            "dataset_ids": ["a", "missing"],
            "reducer": "max",
        },
    ]
    result = metrics.reduce_datasets(results, specs)
    assert value(result, "worst_bias") == 4
    assert value(result, "lowest_bias") == -4
    assert result["metrics"]["unknown_bias"]["value"] is None
    assert "missing" in result["metrics"]["unknown_bias"]["reason"]
    assert result["coverage"] == {"defined": 2, "unknown": 1}
    changed = deepcopy(specs)
    changed[0]["dataset_ids"] = ["a", "a"]
    with pytest.raises(ValueError, match="distinct"):
        metrics.reduce_datasets(results, changed)


def test_masked_nan_outside_roi_is_not_a_false_numeric_failure():
    frames = repeated(np.ones((SIDE, SIDE)))
    valid = np.ones((FRAMES, SIDE, SIDE), dtype=bool)
    valid[:, 0, :] = False
    frames[:, 0, :] = np.nan
    dataset = context()
    dataset["roi"]["row_start"] = 1
    result = measure(frames, valid_mask_frames=valid, dataset=dataset)
    assert result["coverage"]["complete"]
    assert value(result, "temporal_residual_std_e") == 0
    # The same invalid row in the fixed support yields unknown, not a smaller ROI.
    unknown = measure(frames, valid_mask_frames=valid)
    assert not unknown["coverage"]["complete"]
    assert all(v["status"] == "unknown" for v in unknown["metrics"].values())
    assert np.isnan(frames[:, 0]).all()


def test_baseline_mask_is_required_and_cannot_hide_invalid_support():
    frames = repeated(np.ones((SIDE, SIDE)))
    valid = np.ones((FRAMES, SIDE, SIDE), dtype=bool)
    valid[1, 5, 5] = False
    baseline = frames.copy()
    baseline[1, 5, 5] = np.nan
    result = measure(frames, baseline_rgb_frames=baseline, baseline_valid_mask_frames=valid)
    assert result["coverage"]["invalid_samples"] == 1
    assert all(v["status"] == "unknown" for v in result["metrics"].values())


def test_charge_metric_contract_never_accepts_legacy_unit_or_missing_version():
    frames = repeated(np.ones((SIDE, SIDE)))
    dataset = context()
    dataset.pop("schema")
    with pytest.raises(ValueError, match="camera_isp_metric_spec_v2"):
        measure(frames, dataset=dataset)
    result = measure(frames)
    assert result["normalization_e"] == SCALE
    assert "normalization_dn" not in result
    assert metrics.catalog()["metrics"]["temporal_residual_std_e"]["unit"] == "electron_equivalent"
