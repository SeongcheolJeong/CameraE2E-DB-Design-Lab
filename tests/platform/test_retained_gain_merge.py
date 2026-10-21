"""Stateful HCG/LCG delivered-charge fusion keeps history and RAW provenance."""

import hashlib
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow

from camera_platform import dcg_electrical, nonlinear_dcg, nonlinear_examples, retained_gain_merge
from camera_platform.retained_charge_runtime import validate_capture_program
from camera_platform.retained_isp_source import resolve


@pytest.mark.parametrize("nonlinear", [False, True])
def test_raw_only_delivered_merge_does_not_claim_current_exposure_photocharge(nonlinear):
    legacy = {
        "full_well_e": 10000.0, "read_noise_e": 4.0,
        "dark_current_e_per_s": 10.0, "gain": 1.0,
        "bit_depth": 12, "black_level_dn": 64.0,
    }
    model = (nonlinear_examples.shared_model if nonlinear else dcg_electrical.default_model)(
        legacy
    )
    # A previous frame can supply most of the delivered population. The merger
    # receives only two actual RAW paths and cannot identify the current 100 e.
    previous_residual_e, current_photo_e = 500.0, 100.0
    delivered_e = np.full((4, 4), previous_residual_e + current_photo_e)
    reader = nonlinear_dcg if nonlinear else dcg_electrical
    raw = reader.read_delivered_charge(
        delivered_e, model, temperature_c=25, noise_enabled=False,
        rng=np.random.default_rng(71),
    )["arrays"]["raw_dn"]
    original = raw.copy()
    merged = retained_gain_merge.merge_delivered_raw(
        raw, model, retained_gain_merge.default_profile(model),
        exposure_s=0.01, temperature_c=25, noise_enabled=False,
        geometry={"full_sensor_shape": [4, 4], "crop_origin": [0, 0]},
    )
    np.testing.assert_array_equal(raw, original)
    estimate = merged["arrays"]["delivered_charge_e"]
    valid = merged["arrays"]["valid_mask"]
    assert valid.any()
    assert np.all(estimate[valid] > current_photo_e * 2)
    assert np.all(np.abs(estimate[valid] - delivered_e[valid]) < 25)
    assert merged["summary"]["source_raw_sha256"] == hashlib.sha256(raw.tobytes()).hexdigest()
    assert merged["summary"]["actual_exposure_normalization"] is False
    assert merged["summary"]["current_exposure_photocharge_estimation"] is False
    assert merged["summary"]["dark_or_history_correction"] is False


def test_new_schema_requires_paired_model_profile_and_cannot_relabel_old_program(workflow):
    _, _, _, _, _, parent, _, _ = workflow
    original = deepcopy(parent["payload"])
    altered = deepcopy(original)
    altered["schema"] = "camera_retained_charge_capture_program_v3"
    with pytest.raises(ValueError, match="every explicit contract field"):
        validate_capture_program(altered)
    altered["gain_merge_profile"] = {"schema": "forged"}
    with pytest.raises(ValueError, match="shared HCG/LCG"):
        validate_capture_program(altered)
    assert validate_capture_program(original) == original


@pytest.mark.parametrize("mode", ["retained_gain_merge", "trapped_gain_merge"])
@pytest.mark.parametrize("nonlinear", [False, True])
def test_versioned_gain_merge_worker_preserves_raw_state_and_isp_source(
    workflow, mode, nonlinear
):
    service, sdk, assembly, scene, control, parent, _, request = workflow
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"], mode=mode,
        control_sequence_id=control["id"],
    )
    assert draft["payload"]["schema"] == (
        "camera_retained_charge_capture_program_v4"
        if mode == "trapped_gain_merge" else "camera_retained_charge_capture_program_v3"
    )
    if nonlinear:
        model = nonlinear_examples.shared_model(
            parent["sensor_model_snapshot"]["payload"]["readout"]
        )
        draft["payload"]["electrical_model"] = model
        draft["payload"]["retained_charge_model"]["capacity_e"] = int(
            model["shared_charge_capacity_e"]
        )
        draft["payload"]["gain_merge_profile"] = retained_gain_merge.default_profile(model)
    draft["revision"] = mode + ("-nonlinear" if nonlinear else "-linear")
    program = sdk.create_capture_program(draft)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    ))
    run = finish(service, service.submit_run({
        **request, "capture_program_id": program["id"], "test_id": test["id"],
    }))
    summary = run["result"]["summary"]
    raw = load_array(service, run, "raw_groups")
    estimate = load_array(service, run, "gain_merge_delivered_charge_e")
    validity = load_array(service, run, "gain_merge_valid_mask")
    assert raw.shape[1] == 2 and estimate.shape == validity.shape == raw.shape[:1] + raw.shape[2:]
    assert summary["gain_path_merge_applied"] is True
    assert summary["hdr_merge_applied"] is False
    assert summary["current_exposure_charge_estimation"] is False
    assert summary["gain_path_merge"]["temporal_hdr_fusion"] is False
    assert len(summary["gain_path_merge"]["frames"]) == raw.shape[0]
    for i, frame in enumerate(summary["gain_path_merge"]["frames"]):
        assert frame["source_raw_sha256"] == hashlib.sha256(raw[i].tobytes()).hexdigest()
        assert frame["shared_population_provenance"] == (
            "same_runtime_delivered_charge_two_readouts"
        )
    delivered_truth = load_array(service, run, "retained_delivered_e")
    assert np.all(np.abs(estimate[validity] - delivered_truth[validity]) < 100)
    assert summary["array_units"]["gain_merge_delivered_charge_e"] == "electron/pixel"
    assert summary["physical_validation"] == "not_established"
    assert resolve(service, run["id"], {
        "schema": "camera_retained_raw_selection_v1", "readout_id": "LCG"
    })["source_id"] == run["id"]
