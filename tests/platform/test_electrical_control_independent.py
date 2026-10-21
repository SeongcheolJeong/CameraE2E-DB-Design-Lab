"""Independent integration boundaries and realized-acquisition cross-path parity.

These checks establish numerical/contract consistency, not physical AE or LFM
performance. Small in-memory fixtures need no server, registry or Factory run.
"""

from copy import deepcopy

import numpy as np
import pytest
from test_composer import assembly_fixture
from test_hdr_runtime import request_data as request_data

from camera_platform import electrical_control
from camera_platform.composer import check_assembly
from camera_platform.electrical_control_runtime import (
    default_capture_program,
    retained_program,
    run,
)
from camera_platform.electrical_control_workflow import (
    attach_realized_contract,
    check_exposure_domains,
    update_test,
)
from camera_platform.registry import digest
from camera_platform.retained_charge_runtime import RetainedChargeRuntime
from camera_platform.service import PreflightError


def domain_fixture():
    assembly, models = assembly_fixture()
    domain = {"exposure_s": {"min": 0.001, "max": 0.004, "unit": "s"}}
    for model in models:
        model["valid_domain"].update(deepcopy(domain))
    assembly["conditions"]["exposure_s"] = {"value": 0.002, "unit": "s"}
    policy = electrical_control.default_policy([8, 10], initial_exposure_ns=2_000_000)
    return assembly, models, {"electrical_control_policy": policy}, domain


@pytest.mark.parametrize("model_index", [0, 1, 2], ids=["lens", "sensor", "isp"])
def test_each_model_must_declare_exposure_applicability(model_index):
    assembly, models, program, domain = domain_fixture()
    check_exposure_domains(assembly, models, assembly["conditions"], program, domain)
    missing = models[model_index]
    del missing["valid_domain"]["exposure_s"]
    # The ordinary composer only checks axes that were declared. AE must add the
    # missing-axis refusal even though the other models still supply an interval.
    assert check_assembly(assembly, models)["status"] == "PASS"
    with pytest.raises(PreflightError) as exc:
        check_exposure_domains(assembly, models, assembly["conditions"], program, domain)
    issue = exc.value.report["issues"][0]
    assert issue["code"] == "control_domain_missing"
    assert issue["path"] == f"models.{missing['id']}.valid_domain.exposure_s"


@pytest.mark.parametrize("edge,value", [("minimum", 800_000), ("maximum", 4_200_000)])
def test_valid_initial_exposure_does_not_authorize_out_of_domain_grid(edge, value):
    assembly, models, program, domain = domain_fixture()
    assert check_assembly(assembly, models)["status"] == "PASS"
    program["electrical_control_policy"]["exposure_grid_ns"][edge] = value
    # Still a well-formed actuator grid: refusal must be model applicability.
    electrical_control.validate_policy(program["electrical_control_policy"])
    with pytest.raises(PreflightError) as exc:
        check_exposure_domains(assembly, models, assembly["conditions"], program, domain)
    assert exc.value.report["status"] == "OUT_OF_DOMAIN"
    assert any(issue["severity"] == "out_of_domain" for issue in exc.value.report["issues"])


def feedback_request(request_data, noise=False):
    data = deepcopy(request_data)
    data["noise_enabled"] = noise
    for frame in data["control_sequence"]["frames"]:
        frame["applied"]["exposure_ns"] = 2_000_000
    policy = electrical_control.default_policy([8, 10], initial_exposure_ns=2_000_000)
    policy["target_black_relative_dn"] = {"minimum": 0.5, "maximum": 1.0}
    data["capture_program"] = default_capture_program(
        data["readout"], [f["frame_id"] for f in data["control_sequence"]["frames"]], policy
    )
    data["capture_program"]["retained_charge_model"]["transfer_efficiency"] = [0.25, 0.5, 0.75, 1]
    data["capture_program"]["electrical_model"]["dark_current_e_per_s"] = 1000
    data["capture_program"]["waveform"] = {
        "schema": "camera_light_waveform_v1",
        "kind": "pwm",
        "clock_id": data["control_sequence"]["clock"]["clock_id"],
        "period_ns": 1_000_000,
        "on_time_ns": 320_000,
        "phase_origin_ns": 170_000,
        "low": 0,
        "high": 1,
    }
    return data


@pytest.mark.parametrize("noise", [False, True], ids=["continuous", "stochastic"])
def test_enabled_realized_acquisition_replays_exactly_in_retained_runtime(request_data, noise):
    data = feedback_request(request_data, noise)
    original = deepcopy(data)
    feedback = run(data)
    realized = feedback["summary"]["control_sequence"]
    exposures = [frame["applied"]["exposure_ns"] for frame in realized["frames"]]
    assert exposures[0] == 2_000_000 and exposures[1] != exposures[0]
    replay_request = deepcopy(data)
    replay_request["control_sequence"] = deepcopy(realized)
    replay_request["capture_program"] = retained_program(data["capture_program"])
    recorded = RetainedChargeRuntime().execute(replay_request)
    assert set(feedback["arrays"]) == set(recorded["arrays"]) | {"applied_exposure_ns"}
    for name, expected in recorded["arrays"].items():
        np.testing.assert_array_equal(feedback["arrays"][name], expected, err_msg=name)
    assert feedback["summary"]["retained_charge"] == recorded["summary"]["retained_charge"]
    assert feedback["summary"]["raw_formats"] == recorded["summary"]["raw_formats"]
    np.testing.assert_array_equal(feedback["arrays"]["applied_exposure_ns"], exposures)
    # Neither the controller nor the replay may replace the authored scaffold.
    assert data["control_sequence"] == original["control_sequence"]
    assert data["capture_program"] == original["capture_program"]


def test_planned_test_never_claims_realized_exposures_or_row_events(request_data):
    data = feedback_request(request_data)
    summary = run(data)["summary"]
    control = {
        "payload": data["control_sequence"],
        "content_hash": digest(data["control_sequence"]),
    }
    program = {
        "id": "authored-program",
        "payload": data["capture_program"],
        "content_hash": digest(data["capture_program"]),
    }
    scene = data["scene_sequence"]["knots"][0]["scene"]
    scene_record = {
        "image_snapshots": [
            {
                "sampling": deepcopy(scene["sampling"]),
                "shape": list(scene["spectral_irradiance"].shape),
            }
        ]
    }
    test = {
        "frame_conditions": [{"exposure_ns": 2_000_000}],
        "stimulus": {"schema": "temporal_scene_test_v1"},
    }
    update_test(test, program, control, scene_record)
    expected_plan = deepcopy(test["stimulus"]["sample_contract"])
    assert test["frame_conditions"] is None
    assert test["stimulus"]["schema"] == "electrical_control_test_v1"
    assert expected_plan["schema"] == "camera_electrical_control_plan_v1"
    assert "events" not in expected_plan and "control_sequence" not in expected_plan
    assert expected_plan["policy_sha256"] == digest(
        data["capture_program"]["electrical_control_policy"]
    )
    attach_realized_contract(summary, {"test_snapshot": test})
    actual = summary["prediction"]["sample_geometry"]
    assert actual["schema"] == "camera_electrical_control_realized_samples_v1"
    assert actual["plan"] == expected_plan
    assert actual["control_sequence"] == summary["control_sequence"] != control["payload"]
    assert actual["events"] == summary["retained_charge"]["events"]
    assert summary["prediction"]["acquisition_mode"] == "closed_loop"
    assert test["stimulus"]["sample_contract"] == expected_plan
    # Saved evidence is detached from the mutable runtime objects it describes.
    summary["control_sequence"]["frames"][0]["applied"]["exposure_ns"] = 999
    summary["retained_charge"]["events"]["frames"][0]["row_gap_ns"][0] = 999
    assert actual["control_sequence"]["frames"][0]["applied"]["exposure_ns"] == 2_000_000
    assert actual["events"]["frames"][0]["row_gap_ns"][0] == 0
