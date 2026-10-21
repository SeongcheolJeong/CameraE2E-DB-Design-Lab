"""Causal multi-exposure RAW estimator with retained PD state and strict gates."""

import hashlib
from copy import deepcopy

import numpy as np
import pytest
from test_hdr_workflow import finish, load_array
from test_retained_charge_workflow import hdr_workflow as hdr_workflow
from test_retained_charge_workflow import workflow as workflow

from camera_platform import electrical, retained_charge, retained_hdr, sequences
from camera_platform.contracts import ControlSequenceCreate
from camera_platform.retained_charge_runtime import validate_capture_program
from camera_platform.retained_isp_source import resolve
from camera_platform.service import PreflightError


def _models():
    legacy = {
        "full_well_e": 10000.0, "read_noise_e": 1.0,
        "dark_current_e_per_s": 0.0, "gain": 1.0,
        "bit_depth": 12, "black_level_dn": 64.0,
    }
    model = electrical.default_electrical_model(legacy)
    reservoir = {
        "schema": retained_charge.SCHEMA, "capacity_e": 10000,
        "transfer_efficiency": [0.8] * 4,
    }
    return model, reservoir


def _raw_for_rates(rates, exposures, clears, dark=0.0):
    model, reservoir = _models()
    model["dark_current_e_per_s"] = dark
    state = retained_charge.initial_state((4, 4), reservoir, noise_enabled=False)
    raws, truth = [], []
    rng = np.random.default_rng(12)
    for index, (rate, exposure, clear) in enumerate(zip(rates, exposures, clears, strict=True)):
        step = retained_charge.step(
            state, np.full((4, 4), rate * exposure), exposure,
            0.0 if index == 0 else 0.01, reservoir,
            np.ones((4, 4)), np.full((4, 4), dark), clear, False, rng,
        )
        state = step["state"]
        truth.append(step["arrays"])
        raws.append(electrical.read_delivered_charge(
            step["arrays"]["delivered_e"], model, analog_gain=1,
            temperature_c=25, noise_enabled=False, rng=rng,
        )["arrays"]["raw_dn"])
    return np.stack(raws), model, reservoir, truth


def _estimate(raw, model, reservoir, exposures, clears):
    return retained_hdr.reconstruct_and_select(
        raw, model, reservoir, retained_hdr.default_profile(),
        exposures_s=exposures,
        gaps_s=np.array([[0.0] * 4] + [[0.01] * 4] * (len(exposures) - 1)),
        clears=clears, analog_gains=[1.0] * len(exposures),
        temperature_c=25, noise_enabled=False,
        geometry={"kind": "native_sensor", "full_sensor_shape": [4, 4],
                  "crop_origin": [0, 0]},
    )


def test_raw_only_causal_inverse_recovers_current_photo_rate_with_history_and_dark():
    raw, model, reservoir, truth = _raw_for_rates(
        [100000.0, 100000.0, 100000.0], [0.002, 0.008, 0.004],
        [False, False, False], dark=5000,
    )
    original = raw.copy()
    result = _estimate(raw, model, reservoir, [0.002, 0.008, 0.004], [False] * 3)
    arrays = result["arrays"]
    np.testing.assert_array_equal(raw, original)
    assert result["summary"]["source_raw_sha256"] == hashlib.sha256(raw.tobytes()).hexdigest()
    assert result["summary"]["state_truth_used"] is False
    assert result["summary"]["valid_pixels"] == 16
    assert result["summary"]["selected_frame_counts"] == [0, 16, 0]
    assert np.all(np.abs(arrays["merged_rate_e_s"] - 100000) < 1500)
    assert np.all(truth[1]["initial_residual_e"] > 0)
    assert np.all(truth[1]["pretransfer_e"] > truth[1]["exposure_generated_e"])


def test_saturated_long_exposure_uses_short_and_unknown_state_requires_clear():
    raw, model, reservoir, _ = _raw_for_rates(
        [1.4e6, 1.4e6, 1.4e6], [0.002, 0.008, 0.002],
        [False, False, True],
    )
    result = _estimate(raw, model, reservoir, [0.002, 0.008, 0.002],
                       [False, False, True])
    arrays = result["arrays"]
    assert result["summary"]["frame_diagnostics"][1]["valid_pixels"] == 0
    assert result["summary"]["frame_diagnostics"][2]["valid_pixels"] == 16
    assert result["summary"]["valid_pixels"] == 16
    assert np.all(np.abs(arrays["merged_rate_e_s"] - 1.4e6) < 1500)
    no_clear = _estimate(raw, model, reservoir, [0.002, 0.008, 0.002],
                         [False, False, False])
    assert no_clear["summary"]["frame_diagnostics"][2]["valid_pixels"] == 0


def test_disagreeing_raw_rates_are_unknown_instead_of_silently_fused():
    raw, model, reservoir, _ = _raw_for_rates(
        [100000.0, 300000.0], [0.002, 0.008], [False, False]
    )
    result = _estimate(raw, model, reservoir, [0.002, 0.008], [False, False])
    assert result["summary"]["temporal_inconsistent_pixels"] == 16
    assert result["summary"]["valid_pixels"] == 0
    assert np.isnan(result["arrays"]["merged_rate_e_s"]).all()


def test_program_refuses_forged_trap_or_gain_and_requires_explicit_profile(workflow):
    _, _, _, _, _, parent, _, _ = workflow
    program = deepcopy(parent["payload"])
    program["schema"] = "camera_retained_charge_capture_program_v5"
    with pytest.raises(ValueError, match="every explicit"):
        validate_capture_program(program)
    program["photo_hdr_profile"] = retained_hdr.default_profile()
    assert validate_capture_program(program)["schema"] == program["schema"]
    program["retained_charge_model"]["schema"] = "camera_trapped_photodiode_v1"
    with pytest.raises(ValueError):
        validate_capture_program(program)


def test_worker_static_two_exposure_hdr_preserves_raw_and_code_isp_source(workflow):
    service, sdk, assembly, scene, control, parent, _, request = workflow
    fields = {key: deepcopy(value) for key, value in control.items()
              if key in ControlSequenceCreate.model_fields}
    fields.update(name="Retained HDR 2/8 ms", revision="retained-hdr-1")
    for frame in fields["payload"]["frames"]:
        frame["applied"]["exposure_ns"] = 2_000_000 if frame["frame_id"] in {
            "frame-0000", "frame-0002"
        } else 8_000_000
    selected_control = sdk.create_control_sequence(fields)
    draft = sdk.template(
        "capture-program", assembly_id=assembly["id"],
        mode="retained_stateful_hdr", control_sequence_id=selected_control["id"],
    )
    draft["revision"] = "retained-hdr-worker-1"
    draft["payload"]["retained_charge_model"]["transfer_efficiency"] = [0.5] * 4
    program = sdk.create_capture_program(draft)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=selected_control["id"], capture_program_id=program["id"],
    ))
    run = finish(service, service.submit_run({
        **request, "control_sequence_id": selected_control["id"],
        "capture_program_id": program["id"], "test_id": test["id"],
    }))
    summary = run["result"]["summary"]
    assert summary["schema_version"] == "camera_retained_charge_result_v4"
    assert summary["hdr_merge_applied"] is True
    assert summary["current_exposure_charge_estimation"] is True
    assert summary["photo_hdr"]["state_truth_used"] is False
    assert summary["physical_validation"] == "not_established"
    raw = load_array(service, run, "raw_groups")
    assert raw.shape == (4, 1, 16, 16)
    assert summary["photo_hdr"]["source_raw_sha256"] == hashlib.sha256(
        raw[:, 0].tobytes()).hexdigest()
    rates = load_array(service, run, "photo_hdr_merged_rate_e_s")
    valid = load_array(service, run, "photo_hdr_valid_mask")
    assert valid.any() and np.isfinite(rates[valid]).all()
    estimated_photo = load_array(service, run, "photo_hdr_photo_estimate_e")
    true_exposure_charge = load_array(service, run, "retained_exposure_generated_e")
    dark = load_array(service, run, "retained_dark_rate_e_s")
    exposure_s = np.asarray([
        frame["applied"]["exposure_ns"] / 1e9
        for frame in summary["control_sequence"]["frames"]
    ])
    true_photo = true_exposure_charge - exposure_s[:, None, None] * dark[None]
    frame_valid = load_array(service, run, "photo_hdr_exposure_valid_mask")
    assert np.max(np.abs(estimated_photo[frame_valid] - true_photo[frame_valid])) < 20
    assert resolve(service, run["id"], {
        "schema": "camera_retained_raw_selection_v1", "readout_id": "single"
    })["source_id"] == run["id"]


def test_moving_scene_is_refused_before_stateful_hdr_queue(workflow):
    service, sdk, assembly, _, _, _, _, request = workflow
    scene = sequences.create_fixture(
        service, sequences.fixture_template(service, assembly["id"])
        | {"shape": [16, 16], "knot_count": 2, "pattern": "moving_spot"},
    )
    fields = sequences.control_template(service, assembly["id"], scene["id"])
    for index, frame in enumerate(fields["payload"]["frames"]):
        frame["applied"]["exposure_ns"] = (2 if index % 2 == 0 else 8) * 1_000_000
    control = sdk.create_control_sequence(fields)
    draft = sdk.template("capture-program", assembly_id=assembly["id"],
                         mode="retained_stateful_hdr", control_sequence_id=control["id"])
    program = sdk.create_capture_program(draft)
    test = service.create_test(sdk.template(
        "temporal-test", assembly_id=assembly["id"], scene_sequence_id=scene["id"],
        control_sequence_id=control["id"], capture_program_id=program["id"],
    ))
    with pytest.raises(PreflightError) as refused:
        service.submit_run({
            **request, "scene_sequence_id": scene["id"], "control_sequence_id": control["id"],
            "capture_program_id": program["id"], "test_id": test["id"],
        })
    assert "one exact static scene" in str(refused.value.report["issues"])
