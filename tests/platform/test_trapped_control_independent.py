"""Independent time/RAW/state counterexamples for authored trap exposure AE.

The fixed-control replay is an integration oracle, not independent device truth.
Separate causal-prefix, exact-clock and conservation checks constrain the path.
"""

import hashlib
import json
from copy import deepcopy
from fractions import Fraction
from itertools import product

import numpy as np
import pytest
from test_electrical_control_paired import paired_request
from test_electrical_control_runtime import controlled
from test_hdr_runtime import request_data as request_data

from camera_platform import (
    dcg_electrical,
    electrical_control,
    nonlinear_dcg,
    nonlinear_examples,
    trapped_charge,
)
from camera_platform import electrical_control_runtime as feedback
from camera_platform import trapped_charge_runtime as fixed_trap_time
from camera_platform.retained_charge_runtime import (
    RetainedChargeRuntime,
    default_trapped_capture_program,
)
from camera_platform.runtime import RuntimeRequestError

FAMILIES = ["linear", "nonlinear", "paired", "paired_nonlinear"]
PATHS = [
    (family, path)
    for family in FAMILIES
    for path in (["HCG", "LCG"] if family.startswith("paired") else ["single"])
]


def legacy_request(data, family, path, noise, enabled):
    """Keep the exact pre-dev9 fixture construction used by the saved receipt."""
    if family.startswith("paired"):
        data = paired_request(data, path=path, nonlinear=family == "paired_nonlinear", noise=noise)
    else:
        data = controlled.__wrapped__(data)
        data["noise_enabled"] = noise
        if family == "nonlinear":
            data["capture_program"]["electrical_model"] = nonlinear_examples.single_model(
                data["readout"]
            )
    data["capture_program"]["electrical_control_policy"]["enabled"] = enabled
    return data


def fingerprint(result):
    arrays = {
        name: {
            "shape": list(a.shape),
            "dtype": a.dtype.str,
            "sha256": hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest(),
        }
        for name, a in result["arrays"].items()
    }
    return hashlib.sha256(
        json.dumps(
            {"arrays": arrays, "summary": result["summary"]},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


# Whole-result constants were captured before the v3 implementation. The receipt
# retains per-array and summary hashes and the original runtime/source identities.
LEGACY_FINGERPRINTS = {
    "linear/single/noise=False/enabled=False": (
        "9fb633cfa412dd1881a7694606cebddabd48494ef5b79e8b7ab65299fdb84cef"
    ),
    "linear/single/noise=False/enabled=True": (
        "03783530e30e07ec1ec4d1b0410c30a20aa64f05ef75d45cd0101b781b14440f"
    ),
    "linear/single/noise=True/enabled=False": (
        "51d5ec3dcfdfb70bff511bcc7a1208f3f28d19efeaa72802f4e019cc10865dd8"
    ),
    "linear/single/noise=True/enabled=True": (
        "2894a78a2e1780ad0f4c440384ba46b1222ce5bca55fe45b833ea0af2aff0375"
    ),
    "nonlinear/single/noise=False/enabled=False": (
        "8b73439a62a899c12421d40df29b1cae621c975afdf9726a00837b5bd1a3d9a6"
    ),
    "nonlinear/single/noise=False/enabled=True": (
        "c61e8f5f7a847845c0945fc9f683bccf06da4ed7a10678ba36795006a2411e69"
    ),
    "nonlinear/single/noise=True/enabled=False": (
        "17d3c4b51f91e1187345cc80ac695c7ecb73dab4d3feecf64518c57e0e315afe"
    ),
    "nonlinear/single/noise=True/enabled=True": (
        "4eb39e8516d5a5b2dc27fda23adf6ccfa5bd999654f0a8a570aa9c194f583f4d"
    ),
    "paired/HCG/noise=False/enabled=False": (
        "a7cb2b6bf783062c736336cce3ad268d1b749120f76492dfc6fb4c7a77b94c91"
    ),
    "paired/HCG/noise=False/enabled=True": (
        "af06f3c1618b6236c7c760a3a5ececf8761c1da555af8ded1f78538a2430ec13"
    ),
    "paired/HCG/noise=True/enabled=False": (
        "f876adf52a082c4ce3e19ae2a543249911f19cb6fc5179e4d6ceec2e4d3a418e"
    ),
    "paired/HCG/noise=True/enabled=True": (
        "7728fbc4849052cfdf6faed8b257b80804b3a7d8108e0590f18b0f1128dc8e93"
    ),
    "paired/LCG/noise=False/enabled=False": (
        "f47c21082e1d1a5188651a94863092cd284f488c95a0985c606cd594062d9221"
    ),
    "paired/LCG/noise=False/enabled=True": (
        "89cbfd7b3273825f3f599f9971ec6e189b6d879d118e4d91dc938d5e9aa8f952"
    ),
    "paired/LCG/noise=True/enabled=False": (
        "e812c9a324122547f6abc078ff2b82732eb0ce9ad65f75136950e3fe2cf6466d"
    ),
    "paired/LCG/noise=True/enabled=True": (
        "4ecd9995624eb1db13db1ece3df7ebb2e47aa8b209a83c5edcfccd61446f226d"
    ),
    "paired_nonlinear/HCG/noise=False/enabled=False": (
        "3b75c5519c96804e9c1c90ffc36a42491992356d3a72a567e0162e3e9b2be3ed"
    ),
    "paired_nonlinear/HCG/noise=False/enabled=True": (
        "608a4c9eeb915a3418cf0d4fa74293167b84247c532961f83feca803464512fa"
    ),
    "paired_nonlinear/HCG/noise=True/enabled=False": (
        "052af06cab7cb4c93bc4055c28fecc292c11834d575c4767dac99787bf44abbd"
    ),
    "paired_nonlinear/HCG/noise=True/enabled=True": (
        "99675c1cfd53e55acf3f7df7853a10ad1b566bd670537df8988c239fafe87da3"
    ),
    "paired_nonlinear/LCG/noise=False/enabled=False": (
        "3574957266195577ab016f7775b5a3a0ece2a717eae0a685f38d975147d70417"
    ),
    "paired_nonlinear/LCG/noise=False/enabled=True": (
        "e012267f697b1590eb431b9f835c6ec4cbfdaaad4c03b0da8fd3eecbcfa98944"
    ),
    "paired_nonlinear/LCG/noise=True/enabled=False": (
        "bfc69220a75ff26e46820d7fb3176bc12933e650d976a5469a2d412e8252b023"
    ),
    "paired_nonlinear/LCG/noise=True/enabled=True": (
        "9df52abce69732fa4402e1ef4e7f749d9e6d9f66e47a169076ce1e4ff0b575ba"
    ),
}


@pytest.mark.parametrize("family,path", PATHS)
@pytest.mark.parametrize("noise", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_old_single_and_paired_ae_outputs_match_complete_pre_trap_ae_fingerprints(
    request_data, family, path, noise, enabled
):
    output = feedback.run(legacy_request(request_data, family, path, noise, enabled))
    key = f"{family}/{path}/noise={noise}/enabled={enabled}"
    assert fingerprint(output) == LEGACY_FINGERPRINTS[key]


def trap_request(data, family="linear", path="single", noise=False, enabled=True):
    data = legacy_request(data, family, path, noise, enabled)
    previous = data["capture_program"]
    program = default_trapped_capture_program(
        data["readout"], [f["frame_id"] for f in data["control_sequence"]["frames"]]
    )
    program["schema"] = "camera_electrical_control_capture_program_v3"
    program["electrical_model"] = previous["electrical_model"]
    program["electrical_control_policy"] = previous["electrical_control_policy"]
    program["observation_readout_id"] = path
    model = program["retained_charge_model"]
    model["capacity_e"] = previous["retained_charge_model"]["capacity_e"]
    model["transfer_efficiency"] = [0.25, 0.5, 0.75, 1]
    model["trap_capacity_e"] = [1000, 1200, 900, 1100]
    model["capture_rate_s"] = [8, 11, 6, 10]
    model["release_time_s"] = [0.008, 0.010, 0.012, 0.014]
    data["capture_program"] = program
    return data


def assert_conservation(a, noise):
    left = sum(
        a["retained_" + name]
        for name in (
            "initial_residual_e",
            "initial_trapped_e",
            "gap_generated_e",
            "exposure_generated_e",
        )
    )
    right = sum(
        a["retained_" + name]
        for name in (
            "gap_overflow_e",
            "exposure_overflow_e",
            "clear_loss_e",
            "delivered_e",
            "residual_e",
            "trapped_e",
        )
    )
    if noise:
        np.testing.assert_array_equal(left, right)
        assert a["retained_trapped_e"].dtype.kind in "iu"
    else:
        np.testing.assert_allclose(left, right, rtol=3e-14, atol=2e-10)
    np.testing.assert_array_equal(a["retained_initial_trapped_e"][1:], a["retained_trapped_e"][:-1])


@pytest.mark.parametrize("family,path", PATHS)
@pytest.mark.parametrize("noise", [False, True])
def test_realized_closed_loop_replays_exactly_as_fixed_trap_sequence(
    request_data, family, path, noise
):
    data = trap_request(request_data, family, path, noise)
    actual = feedback.run(data)
    replay = deepcopy(data)
    replay["control_sequence"] = actual["summary"]["control_sequence"]
    replay["capture_program"] = feedback.retained_program(data["capture_program"])
    expected = RetainedChargeRuntime().execute(replay)
    assert set(actual["arrays"]) == set(expected["arrays"]) | {"applied_exposure_ns"}
    for name, value in expected["arrays"].items():
        np.testing.assert_array_equal(actual["arrays"][name], value, err_msg=name)
    assert actual["summary"]["rng"] == expected["summary"]["rng"]
    assert actual["summary"]["retained_charge"] == expected["summary"]["retained_charge"]
    assert actual["summary"]["schema_version"] == "camera_electrical_control_result_v2"
    assert actual["summary"]["electrical_control"]["source_truth_used"] is False
    assert_conservation(actual["arrays"], noise)


@pytest.mark.parametrize("noise", [False, True])
def test_disabled_policy_equals_fixed_trap_capture_and_retry_restarts_reservoirs(
    request_data, noise
):
    data = trap_request(request_data, noise=noise, enabled=False)
    data["capture_program"]["pd_clear_before_exposure"][2]["clear"] = True
    expected = RetainedChargeRuntime().execute(
        {**data, "capture_program": feedback.retained_program(data["capture_program"])}
    )
    actual, retry = feedback.run(data), feedback.run(data)
    for name, a in expected["arrays"].items():
        np.testing.assert_array_equal(actual["arrays"][name], a, err_msg=name)
        np.testing.assert_array_equal(retry["arrays"][name], a, err_msg=name)
    assert not actual["summary"]["electrical_control"]["commands"]
    assert not np.any(actual["arrays"]["retained_initial_trapped_e"][0])
    assert np.all(actual["arrays"]["retained_initial_trapped_e"][2] > 0)
    # Eta=1 sites may draw zero release during this gap. A clear must remove
    # existing free charge, not manufacture a strictly positive loss per site.
    assert np.any(actual["arrays"]["retained_clear_loss_e"][2] > 0)
    assert_conservation(actual["arrays"], noise)


def test_actual_exposure_changes_true_gap_duration_and_trap_occupancy(request_data):
    active_data = trap_request(request_data)
    fixed_data = deepcopy(active_data)
    fixed_data["capture_program"]["electrical_control_policy"]["enabled"] = False
    active, fixed = feedback.run(active_data), feedback.run(fixed_data)
    a, f = active["arrays"], fixed["arrays"]
    np.testing.assert_array_equal(a["raw_groups"][0], f["raw_groups"][0])
    assert np.unique(a["applied_exposure_ns"]).size > 1
    expected_gap = 10_000_000 - a["applied_exposure_ns"][:-1]
    np.testing.assert_array_equal(a["row_gap_ns"][1:], np.repeat(expected_gap[:, None], 8, 1))
    assert not np.array_equal(a["row_gap_ns"][1:], f["row_gap_ns"][1:])
    assert not np.array_equal(a["retained_trapped_e"][1:], f["retained_trapped_e"][1:])
    assert not np.array_equal(a["raw_groups"][1:], f["raw_groups"][1:])
    assert_conservation(a, False)


def test_future_stimulus_has_no_effect_on_prior_raw_reservoir_or_issued_policy(request_data):
    data = trap_request(request_data, noise=True)
    timeline = data["scene_sequence"]
    timeline["interpolation"] = "zero_order_hold"
    future = deepcopy(timeline["knots"][0])
    future["time_ns"] = 30_000_000
    timeline["knots"].insert(1, future)
    changed = deepcopy(data)
    for knot in changed["scene_sequence"]["knots"][1:]:
        knot["scene"]["spectral_irradiance"] *= 4
    before, after = feedback.run(data), feedback.run(changed)
    for name in ("raw_groups", "retained_delivered_e", "retained_trapped_e", "row_gap_ns"):
        np.testing.assert_array_equal(before["arrays"][name][:3], after["arrays"][name][:3])
    assert (
        before["summary"]["electrical_control"]["frames"][:3]
        == (after["summary"]["electrical_control"]["frames"][:3])
    )
    assert not np.array_equal(before["arrays"]["raw_groups"][3:], after["arrays"]["raw_groups"][3:])


@pytest.mark.parametrize("nonlinear", [False, True])
def test_meter_observes_only_chosen_integer_raw_and_paired_reader_uses_one_population(
    request_data, nonlinear, monkeypatch
):
    data = trap_request(request_data, "paired_nonlinear" if nonlinear else "paired", "LCG", True)
    policy = data["capture_program"]["electrical_control_policy"]
    policy["statistics_roi"].update(origin=[1, 1], shape=[4, 6])
    policy["meter"]["sites"] = ["R", "Gb"]
    module = nonlinear_dcg if nonlinear else dcg_electrical
    original_read, original_stats = module.read_delivered_charge, electrical_control.raw_statistics
    received, observed = [], []

    def encode(charge, model, **kwargs):
        received.append(charge.copy())
        assert charge.shape == (8, 10)
        return original_read(charge, model, **kwargs)

    def statistics(raw, fmt, p):
        assert raw.shape == (8, 10) and raw.dtype.kind in "iu" and not raw.flags.writeable
        observed.append((raw.copy(), deepcopy(fmt)))
        return original_stats(raw, fmt, p)

    monkeypatch.setattr(module, "read_delivered_charge", encode)
    monkeypatch.setattr(electrical_control, "raw_statistics", statistics)
    out = feedback.run(data)
    assert len(received) == len(observed) == 6
    np.testing.assert_array_equal(received, out["arrays"]["retained_delivered_e"])
    np.testing.assert_array_equal([x[0] for x in observed], out["arrays"]["raw_groups"][:, 1])
    for (raw, fmt), frame in zip(
        observed, out["summary"]["electrical_control"]["frames"], strict=True
    ):
        codes = [int(raw[r, c]) for r in range(1, 5) for c in (2, 4, 6)]
        expected = float(Fraction(sum(codes), len(codes)) - Fraction(str(fmt["black_level_dn"])))
        assert frame["statistics"]["value_dn"] == expected
        assert frame["statistics"]["sample_count"] == 12
    assert_conservation(out["arrays"], True)


def test_full_rolling_readiness_and_transport_delay_prevent_early_command_effect(request_data):
    data = trap_request(request_data)
    data["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=2_000_000)
    data["capture_program"]["electrical_control_policy"]["latency"]["actuator_ns"] = 8_000_001
    data["capture_program"]["waveform"] = {
        "schema": "camera_light_waveform_v1",
        "kind": "pwm",
        "clock_id": data["control_sequence"]["clock"]["clock_id"],
        "period_ns": 700_000,
        "on_time_ns": 230_000,
        "phase_origin_ns": 130_000,
        "low": 0,
        "high": 1,
    }
    out = feedback.run(data)
    trace = out["summary"]["electrical_control"]
    assert trace["frames"][0]["events"]["statistics_ready_ns"] == 16_000_000
    assert trace["commands"][0]["applied_frame_index"] == 3
    assert out["arrays"]["applied_exposure_ns"].tolist()[:3] == [2_000_000] * 3
    assert out["arrays"]["applied_exposure_ns"][3] != 2_000_000
    assert np.unique(out["arrays"]["waveform_row_exposure_ratio"][0]).size > 1


def test_extreme_kinetics_are_refused_before_optics_rng_or_state_allocation(
    request_data, monkeypatch
):
    data = trap_request(request_data)
    data["capture_program"]["retained_charge_model"]["integration"]["max_step_ns"] = 1

    def forbidden(*args, **kwargs):
        pytest.fail("Excessive reachable integration work must fail during preflight")

    monkeypatch.setattr(feedback.TemporalSceneRuntime, "_optical_rates", forbidden)
    monkeypatch.setattr(trapped_charge, "initial_state", forbidden)
    with pytest.raises(RuntimeRequestError):
        feedback.run(data)


def test_whole_grid_work_majorant_covers_every_interior_predecessor_current_combination(
    request_data,
):
    data = trap_request(request_data)
    control = data["control_sequence"]
    control["frames"] = control["frames"][:3]
    control["mode"].update(shutter="rolling", line_time_ns=12_345, hidden_top_rows=2)
    for frame in control["frames"]:
        frame["frame_start_ns"] -= 50_000_000
        frame["applied"]["applied_at_ns"] = frame["frame_start_ns"] + 24_690
    program = data["capture_program"]
    program["pd_clear_before_exposure"] = program["pd_clear_before_exposure"][:3]
    program["electrical_control_policy"]["exposure_grid_ns"] = {
        "minimum": 1_000_000, "maximum": 3_000_000, "step": 1_000_000,
    }
    program["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": control["clock"]["clock_id"],
        "period_ns": 713_003, "on_time_ns": 271_001, "phase_origin_ns": -49_900_003,
        "low": 0, "high": 1,
    }
    knot = data["scene_sequence"]["knots"][0]
    data["scene_sequence"]["knots"] = [
        {**deepcopy(knot), "time_ns": ns}
        for ns in [-50_000_000, -38_765_433, -33_333_337, 50_000_000]
    ]
    parsed = feedback._parse(data)
    bound = parsed["trap_work_envelope"]
    assert bound["grid_point_count"] == 3
    observed_max = {key: 0 for key in bound["resources"]}
    for exposures in product([1_000_000, 2_000_000, 3_000_000], repeat=3):
        events, previous_ends = [], None
        for frame, exposure in zip(control["frames"], exposures, strict=True):
            starts = [frame["frame_start_ns"] + (2 + row) * 12_345 for row in range(8)]
            ends = [ns + exposure for ns in starts]
            gaps = [0] * 8 if previous_ends is None else [
                a - b for a, b in zip(starts, previous_ends, strict=True)
            ]
            events.append({
                "frame_id": frame["frame_id"], "row_open_ns": starts,
                "row_transfer_ns": ends, "row_clear_ns": None, "row_gap_ns": gaps,
                "clear_before_exposure": False,
            })
            previous_ends = ends
        # Use the pre-existing exact fixed-control planner, not the new majorant
        # or its actual-frame helper, to enumerate every grid tuple independently.
        actual = fixed_trap_time.plan({
            **parsed, "events": {**parsed["events"], "frames": events},
        })
        for value, envelope in zip(actual["frames"], bound["frames"], strict=True):
            assert value["segment_count"] <= envelope["segment_count_bound"]
            assert value["pixel_substeps"] == value["segment_count"] * 80
            assert value["pixel_substeps"] <= envelope["pixel_substeps_bound"]
        for key, value in actual["resources"].items():
            assert value <= bound["resources"][key], (exposures, key, value)
            observed_max[key] = max(observed_max[key], value)
    assert observed_max["trap_pixel_substeps"] > 0
    assert observed_max["trap_timing_evidence_bytes_estimate"] > 0


@pytest.mark.parametrize("family", ["paired", "paired_nonlinear"])
def test_noisy_active_retry_preserves_both_path_raw_and_complete_trap_history(request_data, family):
    data = trap_request(request_data, family, "LCG", True)
    left, right = feedback.run(data), feedback.run(data)
    assert fingerprint(left) == fingerprint(right)
    assert np.unique(left["arrays"]["applied_exposure_ns"]).size > 1
