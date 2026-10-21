"""History-bearing capture is an explicit optical/runtime boundary, not HDR inversion."""

import json
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_runtime import request_data as request_data

from camera_platform import dcg_electrical, nonlinear_examples
from camera_platform.retained_charge_runtime import (
    OBSERVATION,
    RetainedChargeRuntime,
    default_capture_program,
    event_schedule,
    validate_capture_program,
)
from camera_platform.runtime import RuntimeRequestError, get_plugin
from camera_platform.temporal import TemporalSceneRuntime


@pytest.fixture
def retained(request_data):
    data = deepcopy(request_data)
    data["capture_program"] = default_capture_program(
        data["readout"], [f["frame_id"] for f in data["control_sequence"]["frames"]]
    )
    data["capture_program"]["electrical_model"].update(
        dark_current_e_per_s=0, read_noise_e=0, post_gain_noise_v=0
    )
    data["capture_program"]["retained_charge_model"]["transfer_efficiency"] = [0.5] * 4
    return data


def run(data):
    return get_plugin("retained_charge_v1").execute(data)


def test_unresolvable_v1_raw_span_is_rejected_before_retained_optics(retained, monkeypatch):
    model = retained["capture_program"]["electrical_model"]
    model.update(conversion_gain_v_per_e=1e-15, digital_gain=1e-6)
    model["adc"]["full_scale_v"] = 1e6
    monkeypatch.setattr(
        TemporalSceneRuntime, "_optical_rates",
        lambda *args, **kwargs: pytest.fail("optics ran before electrical span preflight"),
    )
    issues = RetainedChargeRuntime().validate_request(retained)
    assert len(issues) == 1
    assert "Electron-to-RAW linear span" in issues[0]["message"]


def test_full_sequence_ledger_conserves_delivers_history_and_never_claims_current_charge(retained):
    output = run(retained)
    a, s = output["arrays"], output["summary"]
    first, second = a["retained_delivered_e"]
    np.testing.assert_allclose(first, a["mean_electrons"][0] * 0.5)
    np.testing.assert_allclose(second, (first + a["mean_electrons"][1]) * 0.5)
    for i in range(2):
        np.testing.assert_allclose(
            a["retained_initial_residual_e"][i]
            + a["retained_gap_generated_e"][i]
            + a["retained_exposure_generated_e"][i],
            a["retained_delivered_e"][i]
            + a["retained_residual_e"][i]
            + a["retained_clear_loss_e"][i]
            + a["retained_gap_overflow_e"][i]
            + a["retained_exposure_overflow_e"][i],
        )
    assert a["raw_groups"].shape == (2, 1, 8, 10)
    assert a["rgb_frames"].shape == (2, 8, 10, 3)
    assert s["prediction"]["method_and_version"] == OBSERVATION
    assert s["prediction"]["axes"][1]["values"] == ["single"]
    assert (
        s["retained_charge"]["frame_ledger"][0]["output_state_sha256"]
        == s["retained_charge"]["frame_ledger"][1]["input_state_sha256"]
    )
    assert s["retained_charge"]["state_time_basis"] == "per_row_post_transfer_not_common_wall_time"
    assert s["retained_charge"]["final_state_sha256"]
    assert not s["hdr_merge_applied"] and not s["current_exposure_charge_estimation"]
    assert set(a) == set(s["array_units"])
    assert "calibrations" not in s and not any(k.startswith("calibration_") for k in a)
    estimate = RetainedChargeRuntime().estimate_resources(retained)
    assert estimate["working_array_bytes_estimate"] >= sum(v.nbytes for v in a.values())
    assert estimate["output_array_bytes_estimate"] >= sum(v.nbytes for v in a.values())
    json.dumps(s, allow_nan=False)


def test_clear_removes_prior_charge_but_not_current_exposure(retained):
    before = run(retained)
    retained["capture_program"]["pd_clear_before_exposure"][1]["clear"] = True
    after = run(retained)
    np.testing.assert_allclose(
        after["arrays"]["retained_clear_loss_e"][1], before["arrays"]["retained_residual_e"][0]
    )
    np.testing.assert_allclose(
        after["arrays"]["retained_delivered_e"][1], after["arrays"]["mean_electrons"][1] * 0.5
    )
    assert not np.array_equal(before["arrays"]["raw_groups"], after["arrays"]["raw_groups"])


def test_continuous_dark_gap_and_row_event_ns_are_actual(retained):
    retained["capture_program"]["electrical_model"]["dark_current_e_per_s"] = 1000
    retained["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=250_000)
    output = run(retained)
    a, events = output["arrays"], output["summary"]["retained_charge"]["events"]
    np.testing.assert_array_equal(a["row_gap_ns"][0], 0)
    np.testing.assert_allclose(
        a["retained_gap_generated_e"][1], a["row_gap_ns"][1, :, None] * np.ones((8, 10)) * 1e-6
    )
    assert np.diff(events["frames"][0]["row_open_ns"]).tolist() == [250_000] * 7
    np.testing.assert_allclose(a["retained_expected_exposure_e"][0], a["mean_electrons"][0] + 2)


@pytest.mark.parametrize("family", ["linear", "nonlinear", "paired", "paired_nonlinear"])
def test_all_four_encode_only_families_use_same_retained_population(retained, family):
    model = {
        "linear": lambda: retained["capture_program"]["electrical_model"],
        "nonlinear": lambda: nonlinear_examples.single_model(retained["readout"]),
        "paired": lambda: dcg_electrical.default_model(retained["readout"]),
        "paired_nonlinear": lambda: nonlinear_examples.shared_model(retained["readout"]),
    }[family]()
    model["dark_current_e_per_s"] = 0
    retained["capture_program"]["electrical_model"] = model
    output = run(retained)
    a, s = output["arrays"], output["summary"]
    assert a["raw_groups"].shape[1] == (2 if family.startswith("paired") else 1)
    np.testing.assert_allclose(
        a["retained_delivered_e"][1], (a["mean_electrons"][0] * 0.5 + a["mean_electrons"][1]) * 0.5
    )
    assert s["preview_contract"]["charge_inversion"] is False
    assert all(
        d["input_quantity"] == "delivered_total_read_node_charge_e"
        for d in s["readout_descriptors"]
    )
    expected = []
    for raw_frame in a["raw_groups"]:
        for codes, fmt in zip(raw_frame, s["raw_formats"], strict=True):
            expected.extend(
                float(
                    (codes.astype(float) - fmt["black_level_dn"])[
                        a["cfa_channel_index"] == c
                    ].mean()
                )
                for c in range(3)
            )
    assert s["prediction"]["values"] == expected


def test_charge_rng_is_independent_of_read_noise_gain_and_retry(retained):
    retained["noise_enabled"] = True
    baseline = run(retained)
    retained["capture_program"]["electrical_model"]["read_noise_e"] = 100
    retained["control_sequence"]["frames"][0]["applied"]["gain"] = 2
    changed = run(retained)
    retry = run(retained)
    for name in baseline["arrays"]:
        if name.startswith("retained_"):
            np.testing.assert_array_equal(baseline["arrays"][name], changed["arrays"][name])
        np.testing.assert_array_equal(changed["arrays"][name], retry["arrays"][name])
    assert not np.array_equal(baseline["arrays"]["raw_groups"], changed["arrays"]["raw_groups"])


@pytest.mark.parametrize(
    "change", ["crop", "missing_policy", "policy_order", "capacity", "hdr", "events", "preview"]
)
def test_invalid_state_timing_estimator_and_geometry_are_refused(retained, change):
    if change == "crop":
        for knot in retained["scene_sequence"]["knots"]:
            knot["scene"]["sampling"]["full_sensor_shape"] = [6, 8]
    elif change == "missing_policy":
        retained["capture_program"]["pd_clear_before_exposure"].pop()
    elif change == "policy_order":
        retained["capture_program"]["pd_clear_before_exposure"].reverse()
    elif change == "capacity":
        retained["capture_program"]["retained_charge_model"]["capacity_e"] += 1
    elif change == "hdr":
        retained["capture_program"]["hdr_merge"] = None
    elif change == "events":
        retained["retained_charge_events"] = {}
    else:
        retained["capture_program"]["preview"]["charge_inversion"] = True
    with pytest.raises(RuntimeRequestError):
        run(retained)


def test_event_contract_tie_order_and_detached_normalization(retained):
    program = validate_capture_program(retained["capture_program"])
    control = retained["control_sequence"]
    control["frames"][1]["frame_start_ns"] = (
        control["frames"][0]["frame_start_ns"] + control["frames"][0]["applied"]["exposure_ns"]
    )
    schedule = event_schedule(
        control,
        program,
        {"kind": "native_sensor", "full_sensor_shape": [8, 10], "crop_origin": [0, 0]},
        [8, 10],
    )
    assert schedule["frames"][1]["row_gap_ns"] == [0] * 8
    assert schedule["event_contract"]["simultaneous_order"] == ["transfer", "clear", "open"]
    program["event_contract"]["simultaneous_order"].reverse()
    assert retained["capture_program"]["event_contract"]["simultaneous_order"][0] == "transfer"


def test_bright_then_dark_has_delayed_signal_and_full_transfer_boundary(retained):
    sequence = retained["scene_sequence"]
    sequence["interpolation"] = "zero_order_hold"
    bright = deepcopy(sequence["knots"][0]["scene"])
    dark = deepcopy(bright)
    dark["spectral_irradiance"] = np.zeros_like(bright["spectral_irradiance"])
    sequence["knots"] = [
        {"time_ns": 0, "scene": bright},
        {"time_ns": 2_000_000, "scene": dark},
        {"time_ns": sequence["knots"][-1]["time_ns"], "scene": dark},
    ]
    half = run(retained)
    assert np.max(half["arrays"]["mean_electrons"][1]) == 0
    assert np.min(half["arrays"]["retained_delivered_e"][1]) > 0
    np.testing.assert_allclose(
        half["arrays"]["retained_delivered_e"][1], half["arrays"]["retained_delivered_e"][0] * 0.5
    )
    retained["capture_program"]["retained_charge_model"]["transfer_efficiency"] = [1] * 4
    full = run(retained)
    np.testing.assert_allclose(
        full["arrays"]["retained_delivered_e"][0], full["arrays"]["mean_electrons"][0]
    )
    assert not np.any(full["arrays"]["retained_residual_e"])
    assert not np.any(full["arrays"]["retained_delivered_e"][1])


def test_four_native_transfer_efficiencies_do_not_merge_green_sites(retained):
    efficiencies = [0.1, 0.2, 0.7, 0.9]
    retained["capture_program"]["retained_charge_model"]["transfer_efficiency"] = efficiencies
    output = run(retained)
    a = output["arrays"]
    site_map = a["retained_native_site_index"][0]
    assert site_map[:2, :2].tolist() == [[0, 1], [2, 3]]
    np.testing.assert_allclose(
        a["retained_delivered_e"][0], a["mean_electrons"][0] * np.asarray(efficiencies)[site_map]
    )


@pytest.mark.parametrize("case", ["temperature", "gap_dark", "preview_numeric_boolean"])
def test_known_dark_overflow_and_ambiguous_preview_refused_in_preflight(retained, case):
    model = retained["capture_program"]["electrical_model"]
    if case == "temperature":
        model.update(dark_current_doubling_c=1e-6, reference_temperature_c=-273)
    elif case == "gap_dark":
        model["dark_current_e_per_s"] = 1e12
        frame = retained["control_sequence"]["frames"][1]
        frame["frame_start_ns"] = 100_000_000_000
        frame["applied"]["applied_at_ns"] = 100_000_000_000
        retained["scene_sequence"]["knots"][-1]["time_ns"] = 101_000_000_000
    else:
        retained["capture_program"]["preview"]["charge_inversion"] = 0
    assert RetainedChargeRuntime().validate_request(retained)


@pytest.mark.parametrize("alias", [False, 0.0])
def test_direct_frozen_integer_row_events_reject_equal_valued_type_aliases(retained, alias):
    schedule = event_schedule(
        retained["control_sequence"],
        retained["capture_program"],
        {"kind": "native_sensor", "full_sensor_shape": [8, 10], "crop_origin": [0, 0]},
        [8, 10],
    )
    schedule["frames"][0]["row_gap_ns"][0] = alias
    retained["retained_charge_events"] = schedule
    with pytest.raises(RuntimeRequestError, match="Frozen retained row events"):
        run(retained)
