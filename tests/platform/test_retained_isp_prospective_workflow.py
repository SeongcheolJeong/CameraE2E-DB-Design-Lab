"""A registered prospective holdout is created only after selection completes."""

from test_hdr_workflow import finish
from test_retained_isp_joint_workflow import _spec
from test_retained_isp_observable_workflow import _temporal_test
from test_retained_isp_workflow import payload as replay_payload

from camera_platform import retained_isp_prospective_optimizer as prospective
from camera_platform import retained_isp_prospective_workflow as workflow_module
from camera_platform import sequences
from camera_platform.service import PlatformService

pytest_plugins = ["test_retained_charge_workflow"]


def _runs(service, static_request, scene, control, program, dynamic_test,
          static_seeds, dynamic_seed):
    repeats = [finish(service, service.submit_run({
        **static_request, "parameters": {"seed": seed, "noise_enabled": True},
    })) for seed in static_seeds]
    dynamic = finish(service, service.submit_run({
        **static_request,
        "scene_sequence_id": scene["id"],
        "control_sequence_id": control["id"],
        "capture_program_id": program["id"],
        "test_id": dynamic_test["id"],
        "parameters": {"seed": dynamic_seed, "noise_enabled": True},
    }))
    return repeats, dynamic


def _joint(service, sdk, repeats, dynamic, model, path, suffix):
    temporal_test = _temporal_test(service, sdk, dynamic, model, path, suffix)
    plan = sdk.plan_retained_isp_joint_study(_spec(repeats, dynamic, model, temporal_test))
    return finish(service, sdk.submit_retained_isp_joint_study(plan))


def test_registered_choice_precedes_every_holdout_observation(workflow, tmp_path):
    service, sdk, assembly, _, _, _, _, static_request = workflow
    dynamic_scene = sequences.create_fixture(
        service, sequences.fixture_template(service, assembly["id"])
        | {"shape": [16, 16], "knot_count": 2, "pattern": "moving_spot"},
    )
    dynamic_control = sdk.create_control_sequence(
        sequences.control_template(service, assembly["id"], dynamic_scene["id"]),
    )
    program = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode="retained_charge",
        control_sequence_id=dynamic_control["id"],
    )
    program["payload"]["retained_charge_model"]["transfer_efficiency"] = [0.5] * 4
    dynamic_program = sdk.create_capture_program(program)
    dynamic_test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"],
        scene_sequence_id=dynamic_scene["id"],
        control_sequence_id=dynamic_control["id"],
        capture_program_id=dynamic_program["id"],
    ))
    dev_repeats, dev_dynamic = _runs(
        service, static_request, dynamic_scene, dynamic_control, dynamic_program,
        dynamic_test, (601, 602, 603, 604), 699,
    )
    replay = replay_payload(service, sdk, dev_dynamic, enabled=True)
    model = service.registry.get("models", replay["isp_model_id"])
    development = _joint(
        service, sdk, dev_repeats, dev_dynamic, model,
        tmp_path / "prospective-development-truth.npy", "development",
    )
    request = {
        "schema": prospective.SCHEMA,
        "name": "Prospective noise/time/color ISP choice",
        "development_joint_study_id": development["id"],
        "baseline_candidate_id": "candidate-000",
        "objectives": [
            {"metric": "stationary_repeat_residual_std", "weight": 2, "scale": 0.1},
            {"metric": "temporal_change_rms_delta", "weight": 1, "scale": 0.1},
            {"metric": "display_chroma_rms_delta", "weight": 1, "scale": 0.1},
        ],
        "constraints": [
            {"metric": "temporal_change_rms_delta", "operator": "<=", "value": 1,
             "unit": "normalized_code_rgb"},
            {"metric": "display_chroma_rms_delta", "operator": "<=", "value": 1,
             "unit": "normalized_display_rgb"},
            {"metric": "display_rgb_mae_delta", "operator": "<=", "value": 1,
             "unit": "normalized_display_rgb"},
        ],
    }
    selection_plan = sdk.plan_prospective_isp_selection(request)
    assert selection_plan["cost"]["holdout_id_known_at_selection"] is False
    assert "holdout_joint_study_id" not in selection_plan["frozen_request"]
    selection = finish(service, sdk.submit_prospective_isp_selection(selection_plan))
    assert selection["status"] == "succeeded"
    locked = selection["result"]["summary"]
    assert locked["selected"] is not None
    assert locked["holdout_job_id"] is None
    assert locked["holdout_outputs_read"] is False

    hold_repeats, hold_dynamic = _runs(
        service, static_request, dynamic_scene, dynamic_control, dynamic_program,
        dynamic_test, (701, 702, 703, 704), 799,
    )
    holdout = _joint(
        service, sdk, hold_repeats, hold_dynamic, model,
        tmp_path / "prospective-holdout-truth.npy", "holdout",
    )
    assert holdout["created_at"] > selection["updated_at"]
    hold_plan = sdk.plan_prospective_isp_holdout(selection["id"], holdout["id"])
    assert hold_plan["cost"]["candidate_profiles_cold_verified"] == 2
    assert hold_plan["cost"]["candidate_profiles_scored"] <= 2
    completed = finish(service, sdk.submit_prospective_isp_holdout(hold_plan))
    assert completed["status"] == "succeeded"
    report = completed["result"]["summary"]
    assert report["selected"] == locked["selected"]
    assert report["consistency_status"] in {
        "consistent", "inconsistent", "inconclusive",
    }
    assert report["no_reselection_after_holdout"] is True
    assert report["holdout_outputs_read_at_selection"] is False
    assert report["holdout_blinding_enforced"] is False
    assert report["physical_validation"] == "not_established"
    assert report["provenance"]["selection_completed_at"] < holdout["created_at"]
    assert all(item["created_at"] > selection["updated_at"] for item in report[
        "provenance"]["holdout_original_run_creation"])
    assert sdk.prospective_isp_selection(selection["id"])["id"] == selection["id"]
    assert sdk.prospective_isp_holdout(completed["id"])["id"] == completed["id"]
    assert sdk.export_prospective_isp_selection(selection["id"])[
        "physical_validation"] == "not_established"
    assert sdk.export_prospective_isp_holdout(completed["id"])[
        "physical_validation"] == "not_established"
    assert not any(job["id"] in {selection["id"], completed["id"]}
                   for job in service.list_simulation_runs())
    cold = PlatformService(service.registry.directory)
    assert workflow_module.get_selection_job(cold, selection["id"])["id"] == selection["id"]
    assert workflow_module.get_holdout_job(cold, completed["id"])["id"] == completed["id"]
    assert not service.registry.list("measurements")
