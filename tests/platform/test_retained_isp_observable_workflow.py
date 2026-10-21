"""Registered development selection followed by locked independent holdout."""

from __future__ import annotations

import copy

import numpy as np
import pytest
from test_hdr_workflow import finish
from test_hdr_workflow import workflow as hdr_workflow  # noqa: F401
from test_retained_charge_workflow import workflow as workflow  # noqa: F401
from test_retained_isp_joint_workflow import _spec
from test_retained_isp_workflow import payload as replay_payload

from camera_platform import retained_isp_joint_study as joint_engine
from camera_platform import retained_isp_joint_workflow as joint
from camera_platform import retained_isp_observable_optimizer as optimizer
from camera_platform import retained_isp_observable_workflow as observable
from camera_platform import sequences
from camera_platform.service import PlatformService


def _temporal_test(service, sdk, run, model, path, suffix):
    metadata = sdk.temporal_signal_reference_template(run["id"], "single", model["id"])
    count, rows, columns = metadata["source_binding"]["raw_shape"]
    truth = np.full((count, rows, columns, 3), 0.2, dtype=np.float64)
    truth[-1] = 0.8
    metadata.update(
        name="Independent step " + suffix,
        source_uri="synthetic://observable-holdout-" + suffix,
        method_and_version="authored-independent-step:1",
        license="test-fixture",
        description="A declared temporal fixture; never used for optimizer selection.",
        roi={"row_start": 0, "row_end": rows,
             "column_start": 0, "column_end": columns},
        step={"onset_frame": count - 1, "steady_start_frame": count - 1,
              "onset_fraction": 0.5, "settling_fraction": 0.2},
    )
    np.save(path, truth, allow_pickle=False)
    reference = sdk.import_temporal_signal_reference(metadata, path)
    return sdk.post("tests", sdk.temporal_signal_test_template(reference["id"]))


def test_registered_worker_selection_then_holdout_from_saved_rgb(
    workflow, tmp_path, monkeypatch,
):
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
    sets = []
    for suffix, static_seeds, dynamic_seed in (
        ("development", (601, 602, 603, 604), 699),
        ("holdout", (701, 702, 703, 704), 799),
    ):
        repeats = [finish(service, service.submit_run({
            **static_request,
            "parameters": {"seed": seed, "noise_enabled": True},
        })) for seed in static_seeds]
        dynamic = finish(service, service.submit_run({
            **static_request,
            "scene_sequence_id": dynamic_scene["id"],
            "control_sequence_id": dynamic_control["id"],
            "capture_program_id": dynamic_program["id"],
            "test_id": dynamic_test["id"],
            "parameters": {"seed": dynamic_seed, "noise_enabled": True},
        }))
        sets.append((suffix, repeats, dynamic))
    replay = replay_payload(service, sdk, sets[0][2], enabled=True)
    model = service.registry.get("models", replay["isp_model_id"])
    joint_jobs = []
    for suffix, repeats, dynamic in sets:
        temporal_test = _temporal_test(
            service, sdk, dynamic, model, tmp_path / f"{suffix}-truth.npy", suffix,
        )
        spec = _spec(repeats, dynamic, model, temporal_test)
        planned = sdk.plan_retained_isp_joint_study(spec)
        completed = finish(service, sdk.submit_retained_isp_joint_study(planned))
        assert completed["status"] == "succeeded"
        assert completed["result"]["summary"]["schema"] == joint_engine.RESULT_SCHEMA
        joint_jobs.append(completed)
    request = {
        "schema": optimizer.SCHEMA, "name": "Registered noise/time/color choice",
        "development_joint_study_id": joint_jobs[0]["id"],
        "holdout_joint_study_id": joint_jobs[1]["id"],
        "baseline_candidate_id": "candidate-000",
        "objectives": [
            {"metric": joint_engine.NOISE, "weight": 2, "scale": 0.1},
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
    development_npy_shas = {artifact["sha256"] for artifact in joint_jobs[0]["result"][
        "artifacts"] if artifact["name"].endswith(".npy")}
    holdout_npy_shas = {artifact["sha256"] for artifact in joint_jobs[1]["result"][
        "artifacts"] if artifact["name"].endswith(".npy")}
    holdout_only_shas = holdout_npy_shas - development_npy_shas
    assert holdout_only_shas
    read_shas = set()
    original_loader = joint._load_saved_array

    def tracked_loader(current_service, artifact, shape, dtype):
        read_shas.add(artifact["sha256"])
        return original_loader(current_service, artifact, shape, dtype)

    monkeypatch.setattr(joint, "_load_saved_array", tracked_loader)
    selection_plan = sdk.plan_observable_isp_selection(request)
    assert selection_plan["cost"]["holdout_output_arrays_read_at_plan"] is False
    selection = finish(service, sdk.submit_observable_isp_selection(selection_plan))
    assert selection["status"] == "succeeded"
    frozen = selection["result"]["summary"]
    assert frozen["selected"] is not None
    assert frozen["selection_frozen_before_optimizer_holdout_scoring"] is True
    assert frozen["holdout_blinding_enforced"] is False
    assert frozen["holdout_outputs_read"] is False
    assert not read_shas & holdout_only_shas
    holdout_plan = sdk.plan_observable_isp_holdout(selection["id"])
    assert holdout_plan["cost"]["candidate_profiles_scored"] <= 2
    assert holdout_plan["cost"]["candidate_profiles_cold_verified"] == 2
    assert not read_shas & holdout_only_shas
    assessed = finish(service, sdk.submit_observable_isp_holdout(holdout_plan))
    assert assessed["status"] == "succeeded"
    assert holdout_npy_shas <= read_shas
    report = assessed["result"]["summary"]
    assert report["selected"] == frozen["selected"]
    assert report["no_reselection_after_holdout"] is True
    assert report["holdout_semantics"] == "source_disjoint_retrospective_assessment"
    assert report["physical_validation"] == "not_established"
    assert sdk.observable_isp_selection(selection["id"])["id"] == selection["id"]
    assert sdk.observable_isp_holdout(assessed["id"])["id"] == assessed["id"]
    assert sdk.observable_isp_selection_export(selection["id"])["physical_validation"] == (
        "not_established"
    )
    assert sdk.observable_isp_holdout_export(assessed["id"])["physical_validation"] == (
        "not_established"
    )

    def no_processor(*_args, **_kwargs):
        raise AssertionError("Cold optimizer verification must not rerun ISP")

    monkeypatch.setattr(joint_engine, "process_raw", no_processor)
    cold = PlatformService(service.registry.directory)
    assert observable.get_selection_job(cold, selection["id"])["id"] == selection["id"]
    assert observable.get_holdout_job(cold, assessed["id"])["id"] == assessed["id"]
    forged = copy.deepcopy(assessed)
    forged["result"]["summary"]["holdout_status"] = "fail"
    with pytest.raises(Exception, match="result differs"):
        observable.verify_holdout_job(cold, forged)
