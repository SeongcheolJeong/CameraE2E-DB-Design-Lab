"""Actual AE RAW, causal control replay and original-source binding for code ISP."""

import io
import json
from copy import deepcopy

import numpy as np
import pytest
from test_electrical_control_workflow import workflow as workflow
from test_hdr_workflow import finish, load_array
from test_hdr_workflow import workflow as _hdr_workflow
from test_retained_charge_workflow import workflow as _retained_workflow

from camera_platform import nonlinear_examples
from camera_platform import retained_isp_source as source
from camera_platform.contracts import ControlSequenceCreate
from camera_platform.registry import IntegrityError, canonical

hdr_workflow = _hdr_workflow
retained_workflow = _retained_workflow


def selector(path="single"):
    return {"schema": source.SELECTION_SCHEMA, "readout_id": path}


def acquire(workflow, *, nonlinear=False, disabled=False):
    service, sdk, assembly, scene, control, original, _, request = workflow
    if nonlinear or disabled:
        draft = sdk.template(
            "capture-program",
            assembly_id=assembly["id"],
            mode="electrical_control",
            control_sequence_id=control["id"],
        )
        draft.update(revision="source-variant", parents=[original["id"]])
        draft["payload"]["electrical_control_policy"]["target_black_relative_dn"] = {
            "minimum": 0.5,
            "maximum": 1.0,
        }
        if nonlinear:
            draft["payload"]["electrical_model"] = nonlinear_examples.single_model(
                original["sensor_model_snapshot"]["payload"]["readout"]
            )
        if disabled:
            draft["payload"]["electrical_control_policy"]["enabled"] = False
        program = sdk.create_capture_program(draft)
        test = service.create_test(
            sdk.template(
                "temporal-test",
                assembly_id=assembly["id"],
                scene_sequence_id=scene["id"],
                control_sequence_id=control["id"],
                capture_program_id=program["id"],
            )
        )
        request = {**request, "capture_program_id": program["id"], "test_id": test["id"]}
    return service, finish(service, service.submit_run(request))


def rewrite_summary(service, record):
    artifact = service.registry.store_blob(
        canonical(record["result"]["summary"]).encode(), "summary.json", "application/json"
    )
    record["artifacts"] = [
        artifact if a["name"] == "summary.json" else a for a in record["artifacts"]
    ]


def return_record(monkeypatch, service, record):
    get = service.registry.get_run
    monkeypatch.setattr(
        service.registry,
        "get_run",
        lambda identity: record if identity == record["id"] else get(identity),
    )


@pytest.mark.parametrize("nonlinear,disabled", [(False, False), (True, False), (False, True)])
def test_actual_acquired_exposures_and_causal_trace_replace_only_planning_scaffold(
    workflow, nonlinear, disabled
):
    service, run = acquire(workflow, nonlinear=nonlinear, disabled=disabled)
    before = deepcopy(run)
    selected = source.resolve(service, run["id"], selector())
    raw = load_array(service, run, "raw_groups")[:, 0]
    np.testing.assert_array_equal(source.load_inputs(service, selected), raw)
    summary = run["result"]["summary"]
    actual = summary["control_sequence"]["frames"]
    planned = run["request"]["control_sequence"]["frames"]
    actual_exposures = [f["applied"]["exposure_ns"] for f in actual]
    assert len(set(actual_exposures)) == 1 if disabled else len(set(actual_exposures)) > 1
    assert len({f["applied"]["exposure_ns"] for f in planned}) == 1
    assert selected["acquisition_mode"] == "closed_loop"
    assert selected["source_plugin_id"] == "electrical_control_v1"
    assert selected["planning_contract"] == run["test_snapshot"]["stimulus"]["sample_contract"]
    assert selected["realized_acquisition"] == summary["prediction"]["sample_geometry"]
    assert selected["control_trace"] == summary["electrical_control"]
    assert selected["control_trace_role"] == "evidence_only_not_processing_inputs"
    assert selected["source_snapshot_role"] == "evidence_only_not_processing_inputs"
    assert selected["processing_contract"] == source.PROCESSING_CONTRACT
    assert selected["raw_format"] == summary["raw_formats"][0]
    assert selected["raw_format"]["response_type"] == (
        "nonlinear_charge_voltage" if nonlinear else "linear"
    )
    assert [f["exposure_ns"] for f in selected["frame_conditions"]] == actual_exposures
    assert [f["applied"] for f in selected["frame_records"]] == [f["applied"] for f in actual]
    assert selected["retained_charge_events"] == summary["retained_charge"]["events"]
    assert selected["timing"] == summary["timing"]
    assert {a["name"] for a in selected["input_artifacts"]} == {
        "raw_groups.npy",
        "summary.json",
        "capture-program-source.json",
        "capture-program-normalized.json",
        "applied_exposure_ns.npy",
        "row_start_ns.npy",
        "row_end_ns.npy",
        "row_gap_ns.npy",
    }
    assert selected["source_snapshot"] == before == service.registry.get_run(run["id"])
    selected_raw = source.load_inputs(service, selected)
    selected_raw[:] = 0
    np.testing.assert_array_equal(source.load_inputs(service, selected), raw)
    json.dumps(selected, allow_nan=False)


def test_no_reservoir_or_optical_outputs_and_no_physics_reexecution_are_needed(
    workflow, monkeypatch
):
    from camera_platform.electrical_control_runtime import ElectricalControlRuntime
    from camera_platform.temporal import TemporalSceneRuntime

    service, run = acquire(workflow)
    required = {a["sha256"] for a in run["input_artifacts"]}
    # Content addressing can alias a delivered-readout diagnostic with original
    # RAW, or a zero diagnostic with a timing array. Those exact observed bytes
    # are legitimate inputs even when another descriptor gives them a new name.
    required.update(
        a["sha256"]
        for a in run["artifacts"]
        if a["name"]
        in {
            "raw_groups.npy",
            "summary.json",
            "applied_exposure_ns.npy",
            "row_start_ns.npy",
            "row_end_ns.npy",
            "row_gap_ns.npy",
        }
    )
    forbidden = {
        a["sha256"]
        for a in run["artifacts"]
        if a["name"].startswith(
            (
                "retained_",
                "mean_electrons",
                "channel_electrons",
                "channel_photoelectron_rate",
                "delivered_readout",
            )
        )
    } - required
    get = service.registry.blob_path
    reads = []

    def observed_only(identity):
        assert identity not in forbidden, (
            "Latent optical/charge output entered ISP source resolution"
        )
        reads.append(identity)
        return get(identity)

    def no_simulation(*_args, **_kwargs):
        pytest.fail("ISP source verification must not execute camera physics")

    monkeypatch.setattr(service.registry, "blob_path", observed_only)
    monkeypatch.setattr(ElectricalControlRuntime, "execute", no_simulation)
    monkeypatch.setattr(TemporalSceneRuntime, "_optical_rates", no_simulation)
    selected = source.resolve(service, run["id"], selector())
    assert source.load_inputs(service, selected).shape == (4, 16, 16)
    assert selected["raw_sha256"] in reads and not set(reads) & forbidden


def test_canonical_rehashed_summary_cannot_replace_actual_controls_with_plan_or_invent_commands(
    workflow, monkeypatch
):
    service, original = acquire(workflow)
    changes = {
        "planned_as_actual": lambda r: r["result"]["summary"].update(
            control_sequence=deepcopy(r["request"]["control_sequence"])
        ),
        "applied": lambda r: r["result"]["summary"]["control_sequence"]["frames"][1][
            "applied"
        ].update(exposure_ns=123),
        "trace_statistics": lambda r: r["result"]["summary"]["electrical_control"]["frames"][0][
            "statistics"
        ].update(value_dn=99),
        "trace_proposal": lambda r: r["result"]["summary"]["electrical_control"]["frames"][0][
            "proposal"
        ].update(observed_dn=99),
        "readiness": lambda r: r["result"]["summary"]["electrical_control"]["frames"][0][
            "events"
        ].update(statistics_ready_ns=0),
        "command": lambda r: r["result"]["summary"]["electrical_control"]["commands"][0].update(
            status="stale"
        ),
        "trace_policy": lambda r: r["result"]["summary"]["electrical_control"]["policy"].update(
            enabled=False
        ),
        "target_outcome": lambda r: r["result"]["summary"]["electrical_control"][
            "target_outcome"
        ].update(settling="physical_pass"),
        "realized": lambda r: r["result"]["summary"]["prediction"]["sample_geometry"].update(
            control_sequence=r["request"]["control_sequence"]
        ),
        "plan": lambda r: r["result"]["summary"]["planning_contract"].update(
            policy_sha256="0" * 64
        ),
        "events": lambda r: r["result"]["summary"]["retained_charge"]["events"]["frames"][1][
            "row_transfer_ns"
        ].__setitem__(0, 1),
        "frame_conditions": lambda r: r["result"]["summary"]["frame_conditions"][1].update(
            exposure_ns=123
        ),
        "format": lambda r: r["result"]["summary"]["raw_formats"][0].update(
            response_type="nonlinear_charge_voltage"
        ),
        "descriptor": lambda r: r["result"]["summary"]["readout_descriptors"][0].update(
            input_quantity="current_photocharge"
        ),
        "bool_alias": lambda r: r["result"]["summary"].update(hdr_merge_applied=0),
    }
    for name, change in changes.items():
        altered = deepcopy(original)
        change(altered)
        rewrite_summary(service, altered)
        with monkeypatch.context() as patch:
            return_record(patch, service, altered)
            try:
                with pytest.raises(IntegrityError, match="Electrical"):
                    source.resolve(service, altered["id"], selector())
            except BaseException as error:
                error.add_note("Counterexample: " + name)
                raise


def test_changed_raw_bytes_with_fresh_hash_fail_original_observation_trace(workflow, monkeypatch):
    service, original = acquire(workflow)
    altered = deepcopy(original)
    raw = load_array(service, original, "raw_groups").copy()
    raw[0, 0] = 4095
    data = io.BytesIO()
    np.save(data, raw, allow_pickle=False)
    old = next(a for a in altered["artifacts"] if a["name"] == "raw_groups.npy")
    artifact = service.registry.store_blob(data.getvalue(), "raw_groups.npy", old["media_type"])
    artifact.update({k: v for k, v in old.items() if k not in artifact})
    altered["artifacts"] = [
        artifact if a["name"] == "raw_groups.npy" else a for a in altered["artifacts"]
    ]
    return_record(monkeypatch, service, altered)
    with pytest.raises(IntegrityError, match="control_sequence|electrical_control"):
        source.resolve(service, altered["id"], selector())


def test_changed_observed_timing_array_with_valid_blob_hash_is_refused(workflow, monkeypatch):
    service, original = acquire(workflow)
    for name in ("applied_exposure_ns", "row_start_ns", "row_end_ns", "row_gap_ns"):
        altered = deepcopy(original)
        array = load_array(service, original, name).copy()
        array.flat[-1] += 1
        data = io.BytesIO()
        np.save(data, array, allow_pickle=False)
        old = next(a for a in altered["artifacts"] if a["name"] == name + ".npy")
        artifact = service.registry.store_blob(data.getvalue(), old["name"], old["media_type"])
        artifact.update({k: v for k, v in old.items() if k not in artifact})
        altered["artifacts"] = [
            artifact if a["name"] == old["name"] else a for a in altered["artifacts"]
        ]
        with monkeypatch.context() as patch:
            return_record(patch, service, altered)
            with pytest.raises(IntegrityError, match="observed-timing"):
                source.resolve(service, altered["id"], selector())


def test_registered_bindings_and_frozen_source_are_rechecked_before_loading(workflow, monkeypatch):
    service, original = acquire(workflow)
    selected = source.resolve(service, original["id"], selector())
    for name in (
        "assembly_snapshot",
        "test_snapshot",
        "scene_sequence_snapshot",
        "control_sequence_snapshot",
        "capture_program_snapshot",
    ):
        altered = deepcopy(original)
        altered[name]["name"] += " changed"
        with monkeypatch.context() as patch:
            return_record(patch, service, altered)
            with pytest.raises(IntegrityError):
                source.load_inputs(service, selected)
    for path in ("HCG", "LCG"):
        with pytest.raises(ValueError, match="single"):
            source.resolve(service, original["id"], selector(path))
    for key, value in (
        ("selected_array_sha256", "0" * 64),
        ("acquisition_mode", "recorded"),
        ("control_trace", {}),
        ("frame_conditions", []),
    ):
        modified = {**selected, key: value}
        with pytest.raises(IntegrityError, match="frozen evidence"):
            source.load_inputs(service, modified)


def test_other_registered_identical_control_cannot_rebind_fixed_test(workflow, monkeypatch):
    service, original = acquire(workflow)
    control = original["control_sequence_snapshot"]
    draft = {k: deepcopy(v) for k, v in control.items() if k in ControlSequenceCreate.model_fields}
    draft.update(name="Another control identity", revision="electrical-source-binding")
    replacement = workflow[1].create_control_sequence(draft)
    assert replacement["payload"] == control["payload"]
    altered = deepcopy(original)
    altered.update(control_sequence_id=replacement["id"], control_sequence_snapshot=replacement)
    return_record(monkeypatch, service, altered)
    with pytest.raises(IntegrityError, match="planned Test"):
        source.resolve(service, altered["id"], selector())


def test_historical_capture_environment_and_read_only_detached_result(workflow, monkeypatch):
    service, original = acquire(workflow)
    historical = deepcopy(original)
    historical["environment"] = {"version": "0.16.1.dev3", "historical_capture": True}
    return_record(monkeypatch, service, historical)
    selected = source.resolve(service, historical["id"], selector())
    assert selected["source_snapshot"]["environment"] == historical["environment"]
    assert source.load_inputs(service, selected).shape == (4, 16, 16)
    selected["control_trace"]["frames"][0]["events"]["statistics_ready_ns"] += 1
    assert (
        original["result"]["summary"]["electrical_control"]
        == historical["result"]["summary"]["electrical_control"]
    )


def test_input_and_original_raw_blob_mutation_is_not_hidden_by_prior_resolution(workflow):
    service, run = acquire(workflow)
    selected = source.resolve(service, run["id"], selector())
    artifacts = [
        selected["raw_artifact"],
        selected["summary_artifact"],
        run["capture_program_snapshot"]["artifacts"][0],
    ]
    for artifact in artifacts:
        path = service.registry.blob_path(artifact["sha256"])
        original = path.read_bytes()
        path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        try:
            with pytest.raises((IntegrityError, ValueError)):
                source.load_inputs(service, selected)
        finally:
            path.write_bytes(original)


@pytest.mark.parametrize("seed", [591.0, -0.0])
def test_original_integral_float_seed_remains_a_valid_historical_capture(workflow, seed):
    service, *_, request = workflow
    request = {**request, "parameters": {**request["parameters"], "seed": seed}}
    run = finish(service, service.submit_run(request))
    assert type(run["request"]["seed"]) is float
    assert type(run["result"]["summary"]["rng"]["seed"]) is int
    selected = source.resolve(service, run["id"], selector())
    assert canonical(selected["source_snapshot"]["parameters"]["seed"]) == canonical(seed)
    np.testing.assert_array_equal(
        source.load_inputs(service, selected), load_array(service, run, "raw_groups")[:, 0]
    )


def test_source_seed_guard_rejects_boolean_fractional_nonfinite_and_unbounded_values(
    workflow, monkeypatch
):
    service, original = acquire(workflow)
    for seed in (True, 1.25, float("nan"), float("inf"), -1, 10**1000):
        altered = deepcopy(original)
        altered["parameters"]["seed"] = seed
        with monkeypatch.context() as patch:
            return_record(patch, service, altered)
            with pytest.raises(IntegrityError, match="finite integral"):
                source.resolve(service, altered["id"], selector())
