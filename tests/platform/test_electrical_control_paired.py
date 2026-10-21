"""Independent fixed-path feedback over one delivered population and two RAWs.

These authored fixtures establish numerical/causal consistency, not measured
HCG/LCG circuit behavior, HDR recovery, or physical LFM performance.
"""

from copy import deepcopy
from fractions import Fraction

import numpy as np
import pytest
from test_composer import assembly_fixture
from test_hdr_runtime import request_data as request_data

from camera_platform import dcg_electrical, electrical_control, nonlinear_dcg, nonlinear_examples
from camera_platform import electrical_control_runtime as feedback
from camera_platform.electrical_control_workflow import check_exposure_domains
from camera_platform.retained_charge_runtime import RetainedChargeRuntime
from camera_platform.runtime import RuntimeRequestError
from camera_platform.service import PreflightError


def paired_request(request_data, *, path="HCG", nonlinear=False, noise=False):
    data = deepcopy(request_data)
    data["noise_enabled"] = noise
    first = deepcopy(data["control_sequence"]["frames"][0])
    frames = []
    for index in range(6):
        frame = deepcopy(first)
        frame.update(frame_id=f"pair-{index}", frame_start_ns=index * 10_000_000)
        frame["applied"].update(exposure_ns=2_000_000, gain=1, applied_at_ns=index * 10_000_000)
        frames.append(frame)
    data["control_sequence"]["frames"] = frames
    data["scene_sequence"]["knots"][-1]["time_ns"] = 100_000_000
    policy = electrical_control.default_policy([8, 10], initial_exposure_ns=2_000_000)
    policy["exposure_grid_ns"]["maximum"] = 8_000_000
    policy["target_black_relative_dn"] = {"minimum": 500, "maximum": 600}
    for key in policy["latency"]:
        if key != "kind":
            policy["latency"][key] = 0
    program = feedback.default_capture_program(
        data["readout"], [f["frame_id"] for f in frames], policy
    )
    program.update(
        schema="camera_electrical_control_capture_program_v2", observation_readout_id=path,
    )
    model = (
        nonlinear_examples.shared_model(data["readout"])
        if nonlinear else dcg_electrical.default_model(data["readout"])
    )
    # The meter must subtract the selected path's ADC black, not the sensor's
    # legacy black or the other member's black. Keep nonzero noise/FPN as well.
    model["paths"][0]["adc"]["black_level_dn"] = 80.25
    model["paths"][1]["adc"]["black_level_dn"] = 12.75
    model["dark_current_e_per_s"] = 123
    model["fixed_pattern"].update(prnu_sigma=0.02, dsnu_e_per_s=20, seed=91)
    program["electrical_model"] = model
    program["retained_charge_model"].update(
        capacity_e=int(model["shared_charge_capacity_e"]),
        transfer_efficiency=[0.25, 0.5, 0.75, 1.0],
    )
    data["capture_program"] = program
    return data


@pytest.mark.parametrize("path", ["HCG", "LCG"])
@pytest.mark.parametrize("nonlinear", [False, True], ids=["linear", "nonlinear"])
@pytest.mark.parametrize("noise", [False, True], ids=["continuous", "stochastic"])
def test_disabled_feedback_is_exact_existing_paired_retained_capture_and_seeded_retry(
    request_data, path, nonlinear, noise
):
    data = paired_request(request_data, path=path, nonlinear=nonlinear, noise=noise)
    data["capture_program"]["electrical_control_policy"]["enabled"] = False
    fixed = {**data, "capture_program": feedback.retained_program(data["capture_program"])}
    expected = RetainedChargeRuntime().execute(fixed)
    actual, retry = feedback.run(data), feedback.run(data)
    assert set(actual["arrays"]) == set(expected["arrays"]) | {"applied_exposure_ns"}
    for name, value in expected["arrays"].items():
        np.testing.assert_array_equal(actual["arrays"][name], value, err_msg=name)
        np.testing.assert_array_equal(retry["arrays"][name], value, err_msg=name)
    assert actual["summary"]["rng"] == expected["summary"]["rng"]
    assert actual["summary"]["retained_charge"] == expected["summary"]["retained_charge"]
    trace = actual["summary"]["electrical_control"]
    assert trace["readout_id"] == path and not trace["commands"]
    assert actual["arrays"]["raw_groups"].shape == (6, 2, 8, 10)
    assert actual["summary"]["current_exposure_charge_estimation"] is False
    assert actual["summary"]["hdr_merge_applied"] is False


@pytest.mark.parametrize("path", ["HCG", "LCG"])
@pytest.mark.parametrize("nonlinear", [False, True], ids=["linear", "nonlinear"])
def test_selected_native_site_statistics_use_original_chosen_adc_codes_only(
    request_data, path, nonlinear, monkeypatch
):
    data = paired_request(request_data, path=path, nonlinear=nonlinear, noise=True)
    policy = data["capture_program"]["electrical_control_policy"]
    policy["statistics_roi"].update(origin=[1, 1], shape=[4, 6])
    policy["meter"]["sites"] = ["R", "Gb"]
    real = electrical_control.raw_statistics
    observed = []

    def chosen_only(raw, fmt, passed_policy):
        assert raw.dtype.kind in "iu" and raw.shape == (8, 10)
        assert not raw.flags.writeable
        assert passed_policy == policy
        observed.append((raw.copy(), deepcopy(fmt)))
        return real(raw, fmt, passed_policy)

    monkeypatch.setattr(electrical_control, "raw_statistics", chosen_only)
    output = feedback.run(data)
    index = ["HCG", "LCG"].index(path)
    chosen = output["arrays"]["raw_groups"][:, index]
    fmt = output["summary"]["raw_formats"][index]
    assert len(observed) == len(chosen)
    assert feedback.observation_path(data["capture_program"]) == (index, path, fmt)
    assert fmt["response_type"] == ("nonlinear_charge_voltage" if nonlinear else "linear")
    for i, frame in enumerate(output["summary"]["electrical_control"]["frames"]):
        np.testing.assert_array_equal(observed[i][0], chosen[i])
        assert observed[i][1] == fmt
        # In origin(1,1), shape(4,6), R/Gb are exactly the even native columns.
        selected = [int(chosen[i, r, c]) for r in range(1, 5) for c in (2, 4, 6)]
        expected = float(
            Fraction(sum(selected), len(selected)) - Fraction(str(fmt["black_level_dn"]))
        )
        stats = frame["statistics"]
        assert stats["sum_codes_dn"] == sum(selected) and stats["sample_count"] == 12
        assert stats["value_dn"] == expected and stats["raw_format"] == fmt
        assert stats["charge_or_irradiance_inversion"] is False
    assert output["summary"]["electrical_control"]["source_truth_used"] is False


@pytest.mark.parametrize("nonlinear", [False, True], ids=["linear", "nonlinear"])
def test_selection_changes_later_exposure_not_the_original_shared_first_pair(
    request_data, nonlinear
):
    high_request = paired_request(request_data, nonlinear=nonlinear)
    low_request = deepcopy(high_request)
    low_request["capture_program"]["observation_readout_id"] = "LCG"
    high, low = feedback.run(high_request), feedback.run(low_request)
    for name in ("raw_groups", "retained_delivered_e", "mean_electrons"):
        np.testing.assert_array_equal(high["arrays"][name][0], low["arrays"][name][0])
        assert not np.array_equal(high["arrays"][name][1:], low["arrays"][name][1:])
    assert high["arrays"]["applied_exposure_ns"][1] < 2_000_000
    assert low["arrays"]["applied_exposure_ns"][1] > 2_000_000
    assert high["summary"]["electrical_control"]["readout_id"] == "HCG"
    assert low["summary"]["electrical_control"]["readout_id"] == "LCG"


@pytest.mark.parametrize("nonlinear", [False, True], ids=["linear", "nonlinear"])
def test_one_readout_invocation_per_frame_receives_one_actual_delivered_population(
    request_data, nonlinear, monkeypatch
):
    data = paired_request(request_data, nonlinear=nonlinear, noise=True)
    module = nonlinear_dcg if nonlinear else dcg_electrical
    read = module.read_delivered_charge
    supplied, encoded = [], []

    def both_paths_once(charge, model, **kwargs):
        assert charge.shape == (8, 10)
        supplied.append(charge.copy())
        result = read(charge, model, **kwargs)
        encoded.append(result["arrays"]["raw_dn"].copy())
        return result

    monkeypatch.setattr(module, "read_delivered_charge", both_paths_once)
    output = feedback.run(data)
    assert len(supplied) == 6
    np.testing.assert_array_equal(supplied, output["arrays"]["retained_delivered_e"])
    np.testing.assert_array_equal(encoded, output["arrays"]["raw_groups"])
    assert all(a.shape == (2, 8, 10) for a in encoded)


@pytest.mark.parametrize(
    "invalid", ["v1_pair", "v1_selector", "v2_single", "missing", "single", "hcg", "list"]
)
def test_v1_stays_single_and_v2_requires_one_explicit_fixed_path(request_data, invalid):
    data = paired_request(request_data)
    program = data["capture_program"]
    if invalid in ("v1_pair", "v1_selector"):
        program["schema"] = feedback.PROGRAM_SCHEMA
        if invalid == "v1_pair":
            del program["observation_readout_id"]
    elif invalid == "v2_single":
        program["electrical_model"] = feedback.default_capture_program(
            data["readout"], [f["frame_id"] for f in data["control_sequence"]["frames"]]
        )["electrical_model"]
    elif invalid == "missing":
        del program["observation_readout_id"]
    else:
        program["observation_readout_id"] = ["HCG", "LCG"] if invalid == "list" else invalid
    with pytest.raises(ValueError):
        feedback.validate_capture_program(program)


@pytest.mark.parametrize("fault", ["frame_gain", "overlap", "coverage"])
def test_unreachable_pair_schedule_refuses_before_any_photon_or_readout_draw(
    request_data, fault, monkeypatch
):
    data = paired_request(request_data)
    if fault == "frame_gain":
        for frame in data["control_sequence"]["frames"]:
            frame["applied"]["gain"] = 2
    elif fault == "overlap":
        grid = data["capture_program"]["electrical_control_policy"]["exposure_grid_ns"]
        grid["maximum"] = 12_000_000
    else:
        data["scene_sequence"]["knots"][-1]["time_ns"] = 55_000_000

    def forbidden(*args, **kwargs):
        pytest.fail("An invalid complete reachable domain must be refused before readout")

    monkeypatch.setattr(dcg_electrical, "read_delivered_charge", forbidden)
    monkeypatch.setattr(feedback.TemporalSceneRuntime, "_optical_rates", forbidden)
    with pytest.raises(RuntimeRequestError):
        feedback.run(data)


@pytest.mark.parametrize("fault", ["grid", "missing_axis"])
def test_paired_policy_reachable_grid_requires_every_model_exposure_domain(request_data, fault):
    data = paired_request(request_data)
    assembly, models = assembly_fixture()
    domain = {"exposure_s": {"min": 0.001, "max": 0.008, "unit": "s"}}
    for model in models:
        model["valid_domain"].update(deepcopy(domain))
    assembly["conditions"]["exposure_s"] = {"value": 0.002, "unit": "s"}
    data["capture_program"]["electrical_control_policy"]["exposure_grid_ns"]["minimum"] = 1_000_000
    check_exposure_domains(
        assembly, models, assembly["conditions"], data["capture_program"], domain
    )
    if fault == "grid":
        models[0]["valid_domain"]["exposure_s"]["max"] = 0.004
    else:
        del models[0]["valid_domain"]["exposure_s"]
    with pytest.raises(PreflightError):
        check_exposure_domains(
            assembly, models, assembly["conditions"], data["capture_program"], domain
        )


def test_pwm_rolling_full_sensor_readiness_keeps_original_pair_until_causal_latch(request_data):
    data = paired_request(request_data, path="LCG")
    data["control_sequence"]["mode"].update(shutter="rolling", line_time_ns=2_000_000)
    data["capture_program"]["waveform"] = {
        "schema": "camera_light_waveform_v1", "kind": "pwm",
        "clock_id": data["control_sequence"]["clock"]["clock_id"],
        "period_ns": 700_000, "on_time_ns": 230_000, "phase_origin_ns": 130_000,
        "low": 0, "high": 1,
    }
    output = feedback.run(data)
    trace = output["summary"]["electrical_control"]
    assert trace["frames"][0]["events"]["statistics_ready_ns"] == 16_000_000
    assert trace["commands"][0]["applied_frame_index"] == 2
    assert output["arrays"]["applied_exposure_ns"].tolist()[:3] == [2_000_000, 2_000_000, 4_000_000]
    assert np.unique(output["arrays"]["waveform_row_exposure_ratio"][0]).size > 1
    assert output["summary"]["readout_ids"] == ["HCG", "LCG"]
    assert output["summary"]["physical_validation"] == "not_established"
